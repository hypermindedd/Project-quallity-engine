"""Bounded self-repair loop with a human gate.

1. Work happens on a fresh branch; the default branch is never touched.
2. Each iteration: run the engine → write fix briefs → hand each brief to the
   configured fixer (default: Claude Code headless) → verify the diff.
3. A fixer that touches a protected path (tests, contract, baselines, …) has
   those edits reverted and the loop stops: weakening the oracle to get a green
   build is the failure this guard exists to prevent.
4. The loop stops on success, on no progress, or after ``max_iterations``.
5. Nothing is pushed unless asked; merging is always a human decision.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Config
from ..context import RunContext, match_any
from ..model import Severity
from ..orchestrator import RunOptions, run_pipeline

log = logging.getLogger("pqe")


@dataclass
class FixOutcome:
    branch: str
    iterations: list[dict[str, Any]] = field(default_factory=list)
    stopped_because: str = ""
    final_decision: str = ""
    pushed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class RemediationError(RuntimeError):
    pass


def _git(ctx: RunContext, *args: str) -> str:
    proc = ctx.run(["git", *args], timeout_s=300)
    if not proc.ok:
        raise RemediationError(f"git {' '.join(args)} failed: {proc.tail(5)}")
    return proc.stdout


def changed_paths(ctx: RunContext) -> list[str]:
    tracked = _git(ctx, "diff", "--name-only", "HEAD").splitlines()
    untracked = _git(ctx, "ls-files", "--others", "--exclude-standard").splitlines()
    # Generated caches (bytecode, tool caches) are not edits by the fixer.
    return sorted({
        p for p in tracked + untracked
        if p and not p.startswith(".qa/out/") and not ctx.is_excluded(p) and not p.endswith((".pyc", ".pyo"))
    })


def revert_paths(ctx: RunContext, paths: list[str]) -> None:
    tracked = set(_git(ctx, "ls-files").splitlines())
    restore = [p for p in paths if p in tracked]
    if restore:
        _git(ctx, "checkout", "HEAD", "--", *restore)
    for p in paths:
        if p not in tracked:
            (ctx.root / p).unlink(missing_ok=True)


def actionable_count(report: dict[str, Any], min_severity: Severity) -> int:
    return sum(
        1 for c in report["checks"] for f in c["findings"]
        if Severity(f["severity"]).rank <= min_severity.rank and f.get("location")
    )


def remediate(config: Config, options: RunOptions, push: bool = False) -> FixOutcome:
    spec = config.remediation
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d-%H%M%S")
    base_out = config.qa_dir / "out" / f"fix-{stamp}"
    base_out.mkdir(parents=True, exist_ok=True)
    ctx = RunContext(config=config, out_dir=base_out)
    if not (config.root / ".git").exists():
        raise RemediationError("remediation requires a git repository")
    if not ctx.run(["git", "rev-parse", "--verify", "-q", "HEAD"], timeout_s=30).ok:
        raise RemediationError("repository has no commits yet; commit the current state first")
    if changed_paths(ctx):
        raise RemediationError("working tree is not clean; commit or stash first so every engine change is reviewable")
    branch = f"{spec.branch_prefix}-{stamp}"
    _git(ctx, "checkout", "-b", branch)
    outcome = FixOutcome(branch=branch)
    previous = None
    for iteration in range(1, spec.max_iterations + 1):
        run_opts = RunOptions(only=options.only, skip=options.skip, out_dir=base_out / f"iter-{iteration}")
        report = run_pipeline(config, run_opts)
        remaining = actionable_count(report, spec.min_severity)
        record = {"iteration": iteration, "decision": report["decision"]["decision"], "actionable": remaining}
        outcome.iterations.append(record)
        outcome.final_decision = report["decision"]["decision"]
        if remaining == 0:
            outcome.stopped_because = "no actionable findings remain"
            break
        if previous is not None and remaining >= previous:
            outcome.stopped_because = f"no progress ({previous} → {remaining} actionable findings)"
            break
        previous = remaining
        if iteration == spec.max_iterations:
            outcome.stopped_because = f"reached max_iterations={spec.max_iterations}"
            break
        briefs = [Path(report["out_dir"]) / b for b in report["fix_briefs"]]
        for brief in briefs:
            log.info("fixer ← %s", brief.name)
            proc = ctx.run(spec.fixer, timeout_s=3600, stdin=brief.read_text(encoding="utf-8"))
            record.setdefault("fixer", []).append({"brief": brief.name, "exit": proc.returncode})
        changes = changed_paths(ctx)
        violations = [p for p in changes if match_any(p, spec.protected)]
        if violations:
            revert_paths(ctx, violations)
            record["protected_violations"] = violations
            outcome.stopped_because = "fixer tried to modify protected paths; those edits were reverted"
            changes = [p for p in changes if p not in violations]
            if changes:
                _git(ctx, "add", "-A", "--", *changes)
                _git(ctx, "commit", "-q", "-m", f"pqe: iteration {iteration} (protected-path edits reverted)")
            break
        if not changes:
            outcome.stopped_because = "fixer made no changes"
            break
        _git(ctx, "add", "-A", "--", *changes)
        _git(ctx, "commit", "-q", "-m", f"pqe: iteration {iteration} — address {remaining} finding(s)")
        record["changed"] = changes
    if push and any("changed" in it for it in outcome.iterations):
        _git(ctx, "push", "-u", "origin", branch)
        outcome.pushed = True
    (base_out / "remediation.json").write_text(json.dumps(outcome.to_dict(), indent=2), encoding="utf-8")
    return outcome
