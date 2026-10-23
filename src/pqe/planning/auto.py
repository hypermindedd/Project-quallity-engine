"""Autonomous mode: the engine is added to a repository and does everything.

    classify → plan → write contract → prepare environment → explore the
    running app → let the AI design goal-level tests → run everything → report

A contract a human already reviewed (``.qa/pqe.toml`` without the generated
marker) is never overwritten: human decisions win. The generated contract,
plan and planner log are always written so they can be reviewed and committed.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ..config import CONFIG_DIR, CONFIG_FILE, Config, load_attestations, load_config, parse_config
from ..context import RunContext
from ..discovery.classify import Classification, classify
from ..discovery.profile import discover
from ..http import HttpClient, ManagedServer
from ..orchestrator import RunOptions, run_pipeline, run_setup
from .ai import propose, snapshot_html
from .generate import TestPlan, build_plan, render_plan
from .tomlw import dumps

log = logging.getLogger("pqe")

GENERATED_MARKER = "pqe:generated"
HEADER = f"""{GENERATED_MARKER} — written by `pqe auto`. Review, then delete this line to take ownership:
a contract without this marker is never overwritten by the engine.
Goals marked proposed = true were designed by the AI planner from the project documents;
they run and report but do not block a release until a human removes `proposed`."""


@dataclass
class AutoResult:
    classification: Classification
    plan: TestPlan
    contract_source: str  # generated | existing
    planner: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)


def _is_generated(path: Path) -> bool:
    return path.exists() and GENERATED_MARKER in path.read_text(encoding="utf-8", errors="ignore")[:400]


def _explore(ctx: RunContext, config: Config) -> tuple[str, dict[str, str]]:
    """Boot the app briefly and capture what the AI planner should see."""
    openapi, pages = "", {}
    api = config.api
    if api.enabled and api.start_command and api.openapi_path:
        for name in api.generate_secrets:
            ctx.secret(name)
        try:
            with ManagedServer(ctx, api.start_command, api.cwd, api.env, api.temp_dirs, api.health_path or api.openapi_path,
                               api.ready_timeout_s, "explore-api.log") as server:
                resp = HttpClient(server.base_url).request("GET", api.openapi_path)
                openapi = resp.text if resp.status == 200 else ""
        except RuntimeError as exc:
            log.warning("explore: API did not start: %s", str(exc).splitlines()[0])
    ui = config.ui
    if ui.enabled and ui.start_command and not any(not (ctx.root / r).exists() for r in ui.requires):
        try:
            with ManagedServer(ctx, ui.start_command, ui.cwd, ui.env, [], ui.ready_path, ui.ready_timeout_s, "explore-ui.log") as server:
                pages = _render(server.base_url, ui.pages)
        except RuntimeError as exc:
            log.warning("explore: UI did not start: %s", str(exc).splitlines()[0])
    return openapi, pages


def _render(base: str, paths: list[str], limit: int = 6) -> dict[str, str]:
    """Rendered DOM via the browser when available (SPAs), else raw HTML."""
    from ..runners.web import crawl

    _, checked = crawl(HttpClient(base, timeout_s=20), base, paths, limit)
    def is_page(url: str) -> bool:
        last = url.split("?")[0].rsplit("/", 1)[-1]
        return "." not in last or last.endswith(".html")

    urls = [u for u, (status, _) in checked.items() if status == 200 and is_page(u)][:limit]
    urls = urls or [urljoin(base + "/", p.lstrip("/")) for p in paths]
    snaps: dict[str, str] = {}
    try:
        from playwright.sync_api import sync_playwright

        from ..runners.ui import launch_options

        with sync_playwright() as pw:
            browser = pw.chromium.launch(**launch_options())
            page = browser.new_page()
            for url in urls:
                try:
                    page.goto(url, wait_until="networkidle", timeout=30_000)
                    snaps[url.replace(base, "") or "/"] = snapshot_html(page.content())
                except Exception as exc:  # noqa: BLE001 - one page must not stop exploration
                    log.debug("explore: %s failed: %s", url, exc)
            browser.close()
        return snaps
    except Exception as exc:  # noqa: BLE001 - no browser: fall back to raw HTML
        log.debug("explore: browser unavailable (%s); using raw HTML", exc)
    client = HttpClient(base, timeout_s=20)
    for url in urls:
        resp = client.request("GET", url.replace(base, "") or "/")
        if resp.status == 200:
            snaps[url.replace(base, "") or "/"] = snapshot_html(resp.text)
    return snaps


def write_contract(root: Path, contract: dict[str, Any]) -> Path:
    target = root / CONFIG_DIR / CONFIG_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(contract, HEADER)
    parse_config(tomllib.loads(text), root)  # never write a contract the engine itself cannot load
    target.write_text(text, encoding="utf-8")
    gitignore = target.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("out/\n.tools/\n", encoding="utf-8")
    return target


def run_auto(root: Path, target: str = "", use_ai: bool = True, setup: bool = True, regenerate: bool = False,
             out_dir: Path | None = None, publish: bool = False, fail_on: str = "no_go") -> AutoResult:
    root = root.resolve()
    classification = classify(root)
    profile = discover(root)
    plan, contract = build_plan(classification, profile)
    qa = root / CONFIG_DIR
    qa.mkdir(parents=True, exist_ok=True)
    (qa / "classification.json").write_text(classification.to_json(), encoding="utf-8")
    (qa / "plan.md").write_text(render_plan(plan, classification), encoding="utf-8")
    (qa / "plan.json").write_text(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    contract_path = qa / CONFIG_FILE
    owned_by_human = contract_path.exists() and not _is_generated(contract_path)
    planner: dict[str, Any] = {"status": "SKIPPED", "reason": "human-owned contract in place" if owned_by_human else "disabled"}
    if owned_by_human and not regenerate:
        config = load_config(root)
        source = "existing"
    else:
        config = parse_config(copy.deepcopy(contract), root)
        config.attestations = load_attestations(root)
        source = "generated"

    stamp_dir = out_dir or (qa / "out" / ("auto-" + (target or "all")))
    stamp_dir.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(config=config, out_dir=stamp_dir.resolve(), target=target)
    prior = run_setup(ctx) if setup else []

    if source == "generated":
        if use_ai and target in ("", "web"):
            openapi, pages = _explore(ctx, config)
            contract, planner = propose(contract, classification, profile, root, openapi, pages)
            planner["explored"] = {"openapi": bool(openapi), "pages": sorted(pages)}
        elif not use_ai:
            planner = {"status": "SKIPPED", "reason": "--no-ai"}
        write_contract(root, contract)
        config = load_config(root)
    (qa / "planner.json").write_text(json.dumps(planner, indent=2, ensure_ascii=False), encoding="utf-8")

    options = RunOptions(out_dir=stamp_dir, setup=False, publish=publish, fail_on=fail_on, target=target, prior_results=prior,
                         extra_report={"auto": {"contract": source, "kinds": classification.kinds, "targets": classification.targets,
                                                "planned_suites": len(plan.suites), "planner": planner,
                                                "ci": bool(os.environ.get("CI"))}})
    report = run_pipeline(config, options)
    return AutoResult(classification, plan, source, planner, report)
