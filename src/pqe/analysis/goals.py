"""Goal verification: does the project actually achieve what its documents say?

Each goal in the contract is satisfied only by *evidence*:

* ``tests``       – selected test cases ran and passed (at least ``min_tests``)
* ``check``       – an engine check passed
* ``attestation`` – a named human signed it off in ``.qa/attestations.toml``

A goal with no runnable evidence is ``UNVERIFIED`` — never assumed met.

Traceability closes the loop the other way: every requirement identifier
declared in the project's own documents (``trace_sources``) must be covered by
at least one goal, otherwise the gap is reported.
"""

from __future__ import annotations

import fnmatch
import re

from ..config import Config, Criterion, Goal
from ..model import (
    CheckResult,
    CriterionVerdict,
    Dimension,
    Finding,
    GoalStatus,
    GoalVerdict,
    Location,
    Severity,
    Status,
    TestCase,
)


def _select(tests: list[TestCase], patterns: list[str]) -> list[TestCase]:
    chosen = []
    for test in tests:
        for pattern in patterns:
            if fnmatch.fnmatch(test.id, pattern) or fnmatch.fnmatch(test.file, pattern) or pattern == test.id:
                chosen.append(test)
                break
    return chosen


def evaluate_criterion(criterion: Criterion, results: list[CheckResult], attestations: dict[str, dict[str, str]]) -> CriterionVerdict:
    if criterion.kind == "tests":
        all_tests = [t for r in results for t in r.tests]
        chosen = _select(all_tests, criterion.select)
        ref = ", ".join(criterion.select)
        ran = [t for t in chosen if t.status in (Status.PASS, Status.FAIL)]
        failed = [t for t in chosen if t.status == Status.FAIL]
        if failed:
            return CriterionVerdict("tests", ref, Status.FAIL, f"{len(failed)}/{len(chosen)} failed: " + ", ".join(t.id for t in failed[:5]))
        if len(ran) < criterion.min_tests:
            return CriterionVerdict("tests", ref, Status.SKIPPED, f"{len(ran)} executed test(s) match; {criterion.min_tests} required")
        return CriterionVerdict("tests", ref, Status.PASS, f"{len(ran)} test(s) passed")
    if criterion.kind == "check":
        matches = [r for r in results if r.check_id == criterion.check or fnmatch.fnmatch(r.check_id, criterion.check)]
        if not matches:
            return CriterionVerdict("check", criterion.check, Status.SKIPPED, "check did not run")
        if criterion.findings:
            ref = f"{criterion.check} [{', '.join(criterion.findings)}]"
            blocked = [r for r in matches if r.status in (Status.ERROR, Status.SKIPPED)]
            if blocked:
                return CriterionVerdict("check", ref, blocked[0].status, blocked[0].summary[:400])
            hits = [f for r in matches for f in r.findings
                    if any(fnmatch.fnmatch(str(f.evidence.get(k, "")), pat) for pat in criterion.findings for k in ("rule", "key"))]
            if hits:
                return CriterionVerdict("check", ref, Status.FAIL, "; ".join(f.title for f in hits[:5])[:400])
            return CriterionVerdict("check", ref, Status.PASS, "no matching findings")
        statuses = {r.status for r in matches}
        for status in (Status.FAIL, Status.ERROR, Status.SKIPPED, Status.UNCALIBRATED):
            if status in statuses:
                detail = "; ".join(f"{r.check_id}: {r.summary}" for r in matches if r.status == status)[:400]
                return CriterionVerdict("check", criterion.check, status, detail)
        return CriterionVerdict("check", criterion.check, Status.PASS, "; ".join(r.summary for r in matches)[:400])
    # attestation
    entry = attestations.get(criterion.attestation)
    if not entry:
        return CriterionVerdict("attestation", criterion.attestation, Status.SKIPPED, "no human attestation recorded")
    return CriterionVerdict("attestation", criterion.attestation, Status.PASS, f"attested by {entry['by']} on {entry['date']}: {entry['evidence']}")


def evaluate_goal(goal: Goal, results: list[CheckResult], attestations: dict[str, dict[str, str]]) -> GoalVerdict:
    verdicts = [evaluate_criterion(c, results, attestations) for c in goal.criteria]
    statuses = [v.status for v in verdicts]
    if not verdicts:
        status = GoalStatus.UNVERIFIED
    elif any(s in (Status.FAIL, Status.ERROR) for s in statuses):
        status = GoalStatus.NOT_MET
    elif all(s == Status.PASS for s in statuses):
        status = GoalStatus.MET
    elif any(s == Status.PASS for s in statuses):
        status = GoalStatus.PARTIAL
    else:
        status = GoalStatus.UNVERIFIED
    return GoalVerdict(goal.id, goal.title, status, goal.hard_gate, goal.source, verdicts)


def evaluate_goals(config: Config, results: list[CheckResult]) -> list[GoalVerdict]:
    return [evaluate_goal(g, results, config.attestations) for g in config.goals]


def traceability(config: Config) -> CheckResult:
    """Every requirement ID in the declared sources must be covered by a goal."""
    result = CheckResult("goals.traceability", "Every documented requirement maps to a verified goal", Dimension.GOAL, Status.PASS)
    if not config.trace_sources:
        return CheckResult.skipped(result.check_id, result.title, Dimension.GOAL, "no trace_sources declared in contract")
    covered: set[str] = {c for g in config.goals for c in g.covers}
    declared: dict[str, str] = {}
    for source in config.trace_sources:
        files = sorted(p for p in config.root.glob(source.path) if p.is_file())
        if not files:
            result.findings.append(Finding(result.check_id, Dimension.GOAL, Severity.SEV2, f"Trace source not found: {source.path}",
                                           location=Location(source.path), evidence={"key": f"trace:{source.path}"}))
            continue
        pattern = re.compile(source.pattern)
        for file in files:
            rel = file.relative_to(config.root).as_posix()
            for match in pattern.finditer(file.read_text(encoding="utf-8", errors="replace")):
                ident = match.group(1) if match.groups() else match.group(0)
                declared.setdefault(ident, rel)
    for ident, rel in sorted(declared.items()):
        if ident not in covered:
            result.findings.append(Finding(
                result.check_id, Dimension.GOAL, Severity.SEV2, f"Requirement {ident} has no goal mapping",
                location=Location(rel), evidence={"key": f"untraced:{ident}"},
                suggestion=f"Add {ident} to the 'covers' list of a goal with real evidence, or document why it is out of scope.",
            ))
    unknown = sorted(covered - set(declared))
    result.metrics = {"declared": len(declared), "covered": len(set(declared) & covered), "unknown_references": unknown}
    result.status = Status.FAIL if result.findings else Status.PASS
    result.summary = f"{len(set(declared) & covered)}/{len(declared)} documented requirement(s) mapped"
    return result


def coverage_matrix(results: list[CheckResult]) -> dict[str, dict[str, object]]:
    """For every dimension: which checks covered it and the worst status."""
    order = [Status.FAIL, Status.ERROR, Status.UNCALIBRATED, Status.SKIPPED, Status.PASS]
    matrix: dict[str, dict[str, object]] = {}
    for dim in Dimension:
        checks = [r for r in results if r.dimension == dim]
        ran = [r for r in checks if r.status != Status.SKIPPED]
        if not checks:
            state = "NOT_COVERED"
        elif not ran:
            state = "SKIPPED"
        else:
            state = next(s.value for s in order if any(r.status == s for r in ran))
        matrix[dim.value] = {"state": state, "checks": [r.check_id for r in checks]}
    return matrix
