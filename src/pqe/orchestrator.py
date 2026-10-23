"""The pipeline: run every applicable check, verify goals, triage, decide, report."""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from . import runners as _runners  # noqa: F401 - registers built-in runners
from .analysis.goals import coverage_matrix, evaluate_goals, traceability
from .analysis.triage import assign_modules, decide, load_baseline, mark_lifecycle, write_fix_briefs
from .config import Config
from .context import RunContext
from .model import CheckResult, Decision, Dimension, Finding, Location, Severity, Status
from .report import notion
from .report.render import render_junit, render_markdown, render_sarif
from .runners.base import REGISTRY, Runner

log = logging.getLogger("pqe")

EXIT_CODES = {Decision.GO: 0, Decision.CONDITIONAL: 0, Decision.NO_GO: 1}


@dataclass
class RunOptions:
    only: list[str] = field(default_factory=list)
    skip: list[str] = field(default_factory=list)
    out_dir: Path | None = None
    setup: bool = False
    publish: bool = False
    fail_on: str = "no_go"  # no_go | conditional
    target: str = ""  # web | android | ios | windows | "" (all): one CI job per environment
    prior_results: list[CheckResult] = field(default_factory=list)  # e.g. setup already run by `pqe auto`
    extra_report: dict[str, Any] = field(default_factory=dict)  # merged into report.json (plan, planner)

#: Runners bound to a device environment; every other runner belongs to the web/Linux job.
RUNNER_TARGET = {"android": "android", "ios": "ios"}


def _selected(runner: type[Runner], options: RunOptions) -> bool:
    if options.target and RUNNER_TARGET.get(runner.id, "web") != options.target:
        return False
    keys = {runner.id, runner.dimension.value}

    def hit(patterns: list[str]) -> bool:
        return any(fnmatch.fnmatch(k, p) or k.startswith(p + ".") for p in patterns for k in keys)

    if options.only and not hit(options.only):
        return False
    return not (options.skip and hit(options.skip))


def _commit(ctx: RunContext) -> str:
    if not (ctx.root / ".git").exists() or not ctx.which("git"):
        return ""
    proc = ctx.run(["git", "rev-parse", "--short=12", "HEAD"], timeout_s=30)
    return proc.stdout.strip() if proc.ok else ""


def _tracked_changes(ctx: RunContext) -> set[str] | None:
    if not (ctx.root / ".git").exists() or not ctx.which("git"):
        return None
    proc = ctx.run(["git", "status", "--porcelain", "--untracked-files=no"], timeout_s=60)
    return {line[3:] for line in proc.stdout.splitlines() if line.strip()} if proc.ok else None


def run_setup(ctx: RunContext) -> list[CheckResult]:
    """Run setup commands and verify they are hermetic (no tracked file changes)."""
    results = []
    before = _tracked_changes(ctx)
    for spec in [s for s in ctx.config.setup if ctx.in_target(s.target)]:
        if not spec.enabled:
            continue
        proc = ctx.run(spec.argv, cwd=spec.cwd, env=ctx.build_env(spec.env), timeout_s=spec.timeout_s)
        log_name = f"setup-{spec.id}.log"
        (ctx.out_dir / log_name).write_text(ctx.redact(proc.stdout + "\n" + proc.stderr), encoding="utf-8")
        status = Status.PASS if proc.ok else Status.ERROR
        result = CheckResult(f"setup.{spec.id}", spec.title, Dimension.BUILD, status,
                             summary="ok" if proc.ok else f"setup failed (exit {proc.returncode})", artifacts=[log_name],
                             duration_s=proc.duration_s)
        if not proc.ok:
            result.findings.append(Finding(result.check_id, Dimension.BUILD, Severity.SEV1, f"Setup step failed: {spec.title}",
                                           detail=ctx.redact(proc.tail())))
        results.append(result)
    after = _tracked_changes(ctx)
    if before is not None and after is not None:
        mutated = sorted(after - before)
        check = CheckResult("setup.hermetic", "Environment setup does not modify tracked files", Dimension.BUILD,
                            Status.FAIL if mutated else Status.PASS,
                            summary=f"{len(mutated)} tracked file(s) modified by setup" if mutated else "no tracked file modified")
        for path in mutated:
            check.findings.append(Finding(
                check.check_id, Dimension.BUILD, Severity.SEV2, f"Setup modified a tracked file: {path}",
                location=Location(path), evidence={"key": f"setup-mutation:{path}"},
                suggestion="An install/build step must not rewrite committed files; the next verification sees a different source tree. Generate into an ignored path instead.",
            ))
        results.append(check)
    return results


def run_pipeline(config: Config, options: RunOptions) -> dict[str, Any]:
    started = dt.datetime.now(dt.UTC)
    out_dir = options.out_dir or (config.qa_dir / "out" / started.strftime("%Y%m%dT%H%M%SZ"))
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(config=config, out_dir=out_dir.resolve(), target=options.target)

    results: list[CheckResult] = list(options.prior_results)
    if options.setup:
        results += run_setup(ctx)
    for runner_cls in sorted(REGISTRY, key=lambda r: (r.phase, r.id)):
        if not _selected(runner_cls, options):
            continue
        log.info("▶ %s", runner_cls.id)
        produced = runner_cls().run(ctx)
        for r in produced:
            log.info("  %s %s — %s", r.status.value, r.check_id, r.summary)
        results += produced
    if options.target in ("", "web") and (not options.only or any(p in ("goals", "GOAL") for p in options.only)):
        results.append(traceability(config))

    findings = [f for r in results for f in r.findings]
    assign_modules(config, findings)
    lifecycle = mark_lifecycle(findings, load_baseline(config))
    goals = [g for g in evaluate_goals(config, results)
             if ctx.in_target(next((x.target for x in config.goals if x.id == g.goal_id), ""))]
    decision = decide(results, goals)
    briefs = write_fix_briefs(config, findings, out_dir) if findings else []

    report: dict[str, Any] = {
        "schema": "pqe.report/v1",
        "engine_version": __version__,
        "project": config.project.name,
        "project_goal": config.project.goal,
        "language": config.report.language,
        "commit": _commit(ctx),
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "decision": decision.to_dict(),
        "goals": [g.to_dict() for g in goals],
        "coverage": coverage_matrix(results),
        "lifecycle": lifecycle,
        "checks": [r.to_dict() for r in results],
        "fix_briefs": [p.relative_to(out_dir).as_posix() for p in briefs],
        "target": options.target or "all",
        **options.extra_report,
        "totals": {
            "checks": len(results),
            "findings": len(findings),
            "by_severity": {s.value: sum(1 for f in findings if f.severity == s) for s in Severity},
        },
    }
    write_outputs(report, out_dir, config.root)
    if options.publish and config.report.notion.enabled:
        try:
            report["notion_url"] = notion.publish(report, config.report.notion)
        except RuntimeError as exc:
            report["notion_error"] = str(exc)
            log.warning("Notion publish failed: %s", exc)
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["out_dir"] = str(out_dir)
    return report


def write_outputs(report: dict[str, Any], out_dir: Path, root: Path) -> None:
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown = render_markdown(report)
    (out_dir / "report.md").write_text(markdown, encoding="utf-8")
    (out_dir / "junit.xml").write_text(render_junit(report), encoding="utf-8")
    (out_dir / "results.sarif").write_text(render_sarif(report, root), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(markdown)


def exit_code(report: dict[str, Any], fail_on: str) -> int:
    if fail_on == "never":  # report-only mode: findings are published, the job stays green
        return 0
    decision = Decision(report["decision"]["decision"])
    if fail_on == "conditional" and decision == Decision.CONDITIONAL:
        return 1
    return EXIT_CODES[decision]
