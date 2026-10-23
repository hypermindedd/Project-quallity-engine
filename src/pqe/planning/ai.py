"""AI test designer: turns the project's documents into goal-level tests.

What the model sees: the classification, excerpts of the project's documents
(README, docs/, specs), the API's OpenAPI document when available, and a
compact snapshot of the UI pages (visible text, buttons, fields, links).

What it may return: *proposed* goals, UI journeys and API scenarios in a strict
JSON shape. Every item is validated by the same contract parser humans use;
an invalid item is dropped and listed, never "repaired" by guessing. Proposed
items run and report from the first run but can never block a release until a
human accepts them (removes ``proposed = true``). The model never sees
secrets and never writes code that runs on its own.

Backends: Anthropic Messages API (``ANTHROPIC_API_KEY``) or a logged-in
``claude`` CLI (``PQE_PLANNER=claude-cli``).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..config import ConfigError, parse_config
from ..discovery.classify import Classification
from ..discovery.profile import ProjectProfile

log = logging.getLogger("pqe")

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5-5"
DOC_BUDGET = 60_000
UI_BUDGET = 20_000

Transport = Callable[[str], str]  # prompt -> model text

SYSTEM = """You are a principal QA architect. You design acceptance tests strictly from a project's own documents.
Rules:
- Every goal must cite the document (path and heading) it comes from. Never invent requirements.
- Expected results come from the documents, never from guessing how the code behaves.
- Prefer few, high-value, goal-level tests over many shallow ones.
- UI selectors: use only what the page snapshot shows. Prefer Playwright role or text selectors,
  e.g. role=button[name="Save"] or text=Contact. Never invent ids or classes.
- Never perform destructive or paid actions (delete, pay, purchase, send real messages).
- If the documents do not support a test, do not write it.
Answer with JSON only, no prose."""

SCHEMA = """{
  "product_goal": "one sentence, from the documents",
  "goals": [
    {"id": "D-01", "title": "...", "source": "path#heading", "acceptance": "observable outcome",
     "proven_by": ["ui.journey.<id>" | "api.scenario.<id>"]}
  ],
  "ui_journeys": [
    {"id": "kebab-id", "title": "...", "source": "path#heading",
     "steps": [{"action": "goto|click|fill|select|press|wait_for|wait_ms|expect_text|expect_visible|expect_hidden|expect_url|expect_title",
                "selector": "...", "value": "..."}]}
  ],
  "api_scenarios": [
    {"id": "kebab-id", "title": "...",
     "steps": [{"name": "...", "method": "GET|POST|PUT|PATCH|DELETE", "path": "/x/${var}", "json": {},
                "expect": [200], "save": {"var": "dotted.path"}, "expect_json": {"dotted.path": "value"}}]}
  ]
}"""


# --------------------------------------------------------------------------- #
# Context collection
# --------------------------------------------------------------------------- #


def collect_docs(root: Path, budget: int = DOC_BUDGET) -> str:
    """README first, then docs/, specs and other markdown, until the budget is used."""
    candidates: list[Path] = []
    for pattern in ("README*.md", "readme*.md", "docs/**/*.md", "doc/**/*.md", "spec*/**/*.md", "*.md"):
        for path in sorted(root.glob(pattern)):
            if path.is_file() and path not in candidates and not any(p in path.parts for p in ("node_modules", ".venv", ".qa", "vendor")):
                candidates.append(path)
    chunks, used = [], 0
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        piece = f"\n\n=== {path.relative_to(root).as_posix()} ===\n{text[: max(0, budget - used)]}"
        chunks.append(piece)
        used += len(piece)
        if used >= budget:
            break
    return "".join(chunks)


class _Snapshot(HTMLParser):
    """Visible text and interactive elements of a page, compact."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag in {"script", "style", "svg", "noscript"}:
            self._skip += 1
        elif tag in {"button", "a", "input", "select", "textarea", "form", "h1", "h2", "h3", "label"}:
            keep = {k: a[k] for k in ("type", "name", "placeholder", "aria-label", "href", "role", "autocomplete") if a.get(k)}
            self.lines.append(f"<{tag} {json.dumps(keep, ensure_ascii=False)}>" if keep else f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "svg", "noscript"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text and not self._skip:
            self.lines.append(text[:160])


def snapshot_html(html: str, limit: int = 4000) -> str:
    parser = _Snapshot()
    parser.feed(html)
    return "\n".join(parser.lines)[:limit]


# --------------------------------------------------------------------------- #
# Model backends
# --------------------------------------------------------------------------- #


def api_transport(prompt: str) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    body = json.dumps({"model": os.environ.get("PQE_PLANNER_MODEL", DEFAULT_MODEL), "max_tokens": 8000, "system": SYSTEM,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    request = urllib.request.Request(API_URL, data=body, method="POST", headers={  # noqa: S310  # nosec B310 - constant https URL
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as resp:  # noqa: S310  # nosec B310 - constant https URL
        payload = json.loads(resp.read())
    return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")


def cli_transport(prompt: str) -> str:
    proc = subprocess.run(["claude", "-p", "--output-format", "json", "--append-system-prompt", SYSTEM],  # noqa: S603, S607
                          input=prompt, capture_output=True, text=True, timeout=600, check=False)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-500:])
    data = json.loads(proc.stdout)
    if data.get("is_error") or data.get("terminal_reason") in {"api_error", "error"}:
        raise RuntimeError(f"claude CLI could not answer ({data.get('terminal_reason') or 'error'}); is it signed in?")
    return data.get("result", "")


def default_transport() -> Transport | None:
    if os.environ.get("PQE_PLANNER") == "claude-cli" and shutil.which("claude"):
        return cli_transport
    if os.environ.get("ANTHROPIC_API_KEY"):
        return api_transport
    return None


# --------------------------------------------------------------------------- #
# Validation and merge
# --------------------------------------------------------------------------- #


def _json_block(text: str) -> dict[str, Any]:
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("model answer is not a JSON object")
    return data


def _valid(contract: dict[str, Any], root: Path) -> str:
    try:
        parse_config(copy.deepcopy(contract), root)
        return ""
    except (ConfigError, TypeError, ValueError, KeyError) as exc:
        return str(exc)


_ID = re.compile(r"[^a-z0-9-]+")


def merge_proposals(contract: dict[str, Any], answer: dict[str, Any], root: Path) -> tuple[dict[str, Any], list[str]]:
    """Add every valid proposal; return the contract and a list of rejected items with reasons."""
    rejected: list[str] = []
    merged = copy.deepcopy(contract)
    if answer.get("product_goal") and not merged["project"].get("goal"):
        merged["project"]["goal"] = str(answer["product_goal"])[:400]

    def attempt(section: str, key: str, item: dict[str, Any], label: str) -> bool:
        trial = copy.deepcopy(merged)
        trial.setdefault(section, {"enabled": False}).setdefault(key, []).append(item)
        reason = _valid(trial, root)
        if reason:
            rejected.append(f"{label}: {reason}")
            return False
        merged.clear()
        merged.update(trial)
        return True

    accepted_checks: set[str] = set()
    if "ui" in merged:
        for j in answer.get("ui_journeys") or []:
            if not isinstance(j, dict):
                continue
            jid = _ID.sub("-", str(j.get("id", "")).lower()).strip("-")[:40] or "journey"
            steps = [{k: str(v) for k, v in s.items() if k in ("action", "selector", "value") and v not in (None, "")}
                     for s in j.get("steps") or [] if isinstance(s, dict)]
            item = {"id": jid, "title": str(j.get("title", jid))[:160], "source": str(j.get("source", ""))[:200], "proposed": True, "steps": steps}
            if attempt("ui", "journeys", item, f"ui journey {jid}"):
                accepted_checks.add(f"ui.journey.{jid}")
    elif answer.get("ui_journeys"):
        rejected.append("ui journeys ignored: the project has no web UI component")
    if "api" in merged:
        for sc in answer.get("api_scenarios") or []:
            if not isinstance(sc, dict):
                continue
            sid = _ID.sub("-", str(sc.get("id", "")).lower()).strip("-")[:40] or "scenario"
            steps = []
            for st in sc.get("steps") or []:
                if not isinstance(st, dict):
                    continue
                step = {k: st[k] for k in ("name", "method", "path", "json", "expect", "save", "expect_json") if st.get(k) not in (None, "", {}, [])}
                step.setdefault("name", f"{step.get('method', 'GET')} {step.get('path', '')}")
                steps.append(step)
            item = {"id": sid, "title": str(sc.get("title", sid))[:160], "severity": "SEV3", "steps": steps}
            if attempt("api", "scenarios", item, f"api scenario {sid}"):
                accepted_checks.add(f"api.scenario.{sid}")
    for g in answer.get("goals") or []:
        if not isinstance(g, dict):
            continue
        proofs = [p for p in g.get("proven_by") or [] if p in accepted_checks]
        criteria = [{"kind": "check", "check": p} for p in proofs] or [{"kind": "attestation", "attestation": f"doc-goal-{g.get('id', '?')}"}]
        gid = re.sub(r"[^A-Za-z0-9_-]", "", str(g.get("id", "")))[:20] or f"D-{len(merged.get('goals', [])):02d}"
        if any(x["id"] == gid for x in merged.get("goals", [])):
            gid = f"D-{gid}"
        item = {"id": gid, "title": str(g.get("title", ""))[:200] or gid, "source": str(g.get("source", ""))[:200],
                "proposed": True, "criteria": criteria}
        trial = copy.deepcopy(merged)
        trial.setdefault("goals", []).append(item)
        reason = _valid(trial, root)
        if reason:
            rejected.append(f"goal {gid}: {reason}")
        else:
            merged = trial
    return merged, rejected


def build_prompt(classification: Classification, profile: ProjectProfile, docs: str, openapi: str, ui_pages: dict[str, str]) -> str:
    parts = [
        "Design acceptance tests for this project.",
        f"\n## Components\n{classification.to_json()[:6000]}",
        "\n## Detected goal statements\n" + "\n".join(profile.goal_statements[:8]),
        f"\n## Documents\n{docs}",
    ]
    if openapi:
        parts.append(f"\n## OpenAPI (truncated)\n{openapi[:12000]}")
    if ui_pages:
        used, snaps = 0, []
        for url, snap in ui_pages.items():
            if used > UI_BUDGET:
                break
            snaps.append(f"--- {url}\n{snap}")
            used += len(snap)
        parts.append("\n## UI page snapshots (visible text and controls)\n" + "\n".join(snaps))
    parts.append(f"\n## Answer format (JSON only)\n{SCHEMA}")
    parts.append("Write at most 12 goals, 8 UI journeys and 8 API scenarios. Journeys start from the home page.")
    return "\n".join(parts)


def propose(contract: dict[str, Any], classification: Classification, profile: ProjectProfile, root: Path,
            openapi: str = "", ui_pages: dict[str, str] | None = None, transport: Transport | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (contract with proposals, planner report)."""
    transport = transport or default_transport()
    if transport is None:
        return contract, {"status": "SKIPPED", "reason": "no ANTHROPIC_API_KEY and no claude CLI (PQE_PLANNER=claude-cli)"}
    prompt = build_prompt(classification, profile, collect_docs(root), openapi, ui_pages or {})
    try:
        answer = _json_block(transport(prompt))
    except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        log.warning("AI planner failed: %s", exc)
        return contract, {"status": "ERROR", "reason": f"{type(exc).__name__}: {str(exc)[:300]}"}
    merged, rejected = merge_proposals(contract, answer, root)
    added = {
        "goals": len([g for g in merged.get("goals", []) if g.get("proposed")]),
        "ui_journeys": len((merged.get("ui") or {}).get("journeys", [])),
        "api_scenarios": len((merged.get("api") or {}).get("scenarios", [])),
    }
    return merged, {"status": "OK", "added": added, "rejected": rejected}
