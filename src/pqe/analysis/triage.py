"""Triage: attribute findings to architecture modules, track them across runs,
decide the release state and write fix briefs that respect the architecture.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Config
from ..context import match_any
from ..model import CheckResult, Decision, DecisionRecord, Finding, GoalStatus, GoalVerdict, Severity, Status

BASELINE_FILE = "findings.json"


def assign_modules(config: Config, findings: list[Finding]) -> None:
    for finding in findings:
        if finding.location is None:
            continue
        for module in config.architecture.modules:
            if match_any(finding.location.path, module.paths):
                finding.module = module.name
                break


def load_baseline(config: Config) -> set[str]:
    path = config.qa_dir / "baselines" / BASELINE_FILE
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("fingerprints", []))
    except (ValueError, AttributeError):
        return set()


def mark_lifecycle(findings: list[Finding], baseline: set[str]) -> dict[str, int]:
    """Classify findings as NEW or EXISTING relative to the accepted baseline."""
    current = set()
    for finding in findings:
        fp = finding.fingerprint
        current.add(fp)
        finding.lifecycle = "EXISTING" if fp in baseline else "NEW"
    return {
        "new": sum(1 for f in findings if f.lifecycle == "NEW"),
        "existing": sum(1 for f in findings if f.lifecycle == "EXISTING"),
        "resolved_since_baseline": len(baseline - current) if baseline else 0,
    }


def decide(results: list[CheckResult], goals: list[GoalVerdict]) -> DecisionRecord:
    """Release decision. Quality signals never compensate for a hard failure."""
    findings = [f for r in results for f in r.findings]
    blocking: list[str] = []
    conditions: list[str] = []
    sev0 = [f for f in findings if f.severity == Severity.SEV0]
    if sev0:
        blocking.append(f"{len(sev0)} critical (SEV0) finding(s)")
    for goal in goals:
        if goal.hard_gate and goal.status == GoalStatus.NOT_MET:
            blocking.append(f"hard-gate goal {goal.goal_id} NOT_MET")
        elif goal.hard_gate and goal.status != GoalStatus.MET:
            conditions.append(f"hard-gate goal {goal.goal_id} {goal.status.value}")
        elif goal.status == GoalStatus.NOT_MET and not goal.proposed:
            conditions.append(f"goal {goal.goal_id} NOT_MET")
    sev1 = [f for f in findings if f.severity == Severity.SEV1]
    if sev1:
        conditions.append(f"{len(sev1)} major (SEV1) finding(s)")
    errored = [r.check_id for r in results if r.status == Status.ERROR]
    if errored:
        conditions.append("check(s) could not run: " + ", ".join(errored))
    if blocking:
        return DecisionRecord(Decision.NO_GO, blocking + conditions)
    if conditions:
        return DecisionRecord(Decision.CONDITIONAL, conditions)
    return DecisionRecord(Decision.GO, ["no blocking findings; all hard-gate goals met"])


def write_fix_briefs(config: Config, findings: list[Finding], out_dir: Path) -> list[Path]:
    """One brief per architecture module, for actionable findings only."""
    limit = config.remediation.min_severity.rank
    actionable = [f for f in findings if f.severity.rank <= limit and f.location is not None]
    by_module: dict[str, list[Finding]] = {}
    for finding in actionable:
        by_module.setdefault(finding.module or "unassigned", []).append(finding)
    brief_dir = out_dir / "fix-briefs"
    brief_dir.mkdir(parents=True, exist_ok=True)
    written = []
    rules = "\n".join(f"- `{r.source}` must not import {', '.join(f'`{x}`' for x in r.forbid)} — {r.reason}" for r in config.architecture.rules) or "- (none declared)"
    protected = "\n".join(f"- `{p}`" for p in config.remediation.protected)
    for module, items in sorted(by_module.items()):
        items.sort(key=lambda f: f.severity.rank)
        checks = sorted({f.check_id for f in items})
        lines = [
            f"# Fix brief — {config.project.name} — module: {module}",
            "",
            "You are fixing defects found by the Project Quality Engine. Work strictly inside the existing architecture.",
            "",
            "## Project goal",
            config.project.goal or "(see project documents)",
            "",
            "## Non-negotiable constraints",
            "1. Do NOT modify, delete, skip or weaken any test, fixture, baseline or the quality contract.",
            "2. Do NOT edit any protected path:",
            protected,
            "3. Respect these architecture boundaries (they are law, not suggestions):",
            rules,
            "4. Fix the root cause in the owning module. No catch-all exception swallowing, no special-casing test inputs.",
            "5. Keep the change minimal and consistent with the surrounding code style.",
            "6. If a finding cannot be fixed without changing a protected path or the architecture, STOP and explain why instead of working around it.",
            "",
            "## Findings to resolve",
        ]
        for f in items:
            lines += [
                f"### [{f.severity.value}] {f.title}",
                f"- check: `{f.check_id}`  ",
                f"- location: `{f.location.render() if f.location else '-'}`  ",
                f"- status vs baseline: {f.lifecycle or 'n/a'}",
            ]
            if f.detail:
                lines += ["", "```", f.detail[:1500], "```"]
            if f.suggestion:
                lines += ["", f"Suggested direction: {f.suggestion}"]
            lines.append("")
        lines += [
            "## Acceptance",
            "The engine will re-run these checks; the brief is done only when they pass without new findings:",
            *[f"- `{c}`" for c in checks],
        ]
        path = brief_dir / f"{module.replace('/', '_').replace(' ', '_')}.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        written.append(path)
    return written
