from __future__ import annotations

from pqe.analysis.goals import coverage_matrix, evaluate_goal, traceability
from pqe.analysis.triage import decide, mark_lifecycle, write_fix_briefs
from pqe.config import Criterion, Goal
from pqe.model import CheckResult, Decision, Dimension, Finding, GoalStatus, Location, Severity, Status, TestCase

from .conftest import make_config, write


def _results():
    tests = CheckResult("functional.pytest", "t", Dimension.FUNCTIONAL, Status.FAIL, tests=[
        TestCase("tests/test_a.py::test_one", "tests/test_a.py", "test_one", Status.PASS),
        TestCase("tests/test_a.py::test_two", "tests/test_a.py", "test_two", Status.PASS),
        TestCase("tests/test_b.py::test_bad", "tests/test_b.py", "test_bad", Status.FAIL),
    ])
    arch = CheckResult("architecture.boundaries", "a", Dimension.ARCHITECTURE, Status.FAIL, findings=[
        Finding("architecture.boundaries", Dimension.ARCHITECTURE, Severity.SEV2, "cycle", evidence={"key": "cycle:x"}),
    ])
    perf = CheckResult("performance.latency", "p", Dimension.PERFORMANCE, Status.UNCALIBRATED)
    return [tests, arch, perf]


def test_goal_states():
    results = _results()
    met = Goal("G1", "a", [Criterion("tests", select=["tests/test_a.py::*"], min_tests=2)])
    assert evaluate_goal(met, results, {}).status == GoalStatus.MET
    failing = Goal("G2", "b", [Criterion("tests", select=["tests/test_b.py::*"])])
    assert evaluate_goal(failing, results, {}).status == GoalStatus.NOT_MET
    too_few = Goal("G3", "c", [Criterion("tests", select=["tests/test_a.py::test_one"], min_tests=3)])
    assert evaluate_goal(too_few, results, {}).status == GoalStatus.UNVERIFIED
    partial = Goal("G4", "d", [Criterion("tests", select=["tests/test_a.py::*"]), Criterion("check", check="performance.latency")])
    assert evaluate_goal(partial, results, {}).status == GoalStatus.PARTIAL
    missing = Goal("G5", "e", [Criterion("check", check="ui.*")])
    assert evaluate_goal(missing, results, {}).status == GoalStatus.UNVERIFIED


def test_findings_filter_binds_a_goal_to_one_rule():
    results = _results()
    unrelated = Goal("G", "x", [Criterion("check", check="architecture.boundaries", findings=["rule-a"])])
    assert evaluate_goal(unrelated, results, {}).status == GoalStatus.MET
    related = Goal("G", "x", [Criterion("check", check="architecture.boundaries", findings=["cycle:*"])])
    assert evaluate_goal(related, results, {}).status == GoalStatus.NOT_MET


def test_attestation_requires_complete_entry():
    goal = Goal("G", "human", [Criterion("attestation", attestation="visual")])
    assert evaluate_goal(goal, [], {}).status == GoalStatus.UNVERIFIED
    entry = {"visual": {"by": "Lead", "date": "2026-11-01", "evidence": "docs/x.json"}}
    verdict = evaluate_goal(goal, [], entry)
    assert verdict.status == GoalStatus.MET and "Lead" in verdict.criteria[0].detail


def test_decision_policy():
    hard_unmet = evaluate_goal(Goal("H", "h", [Criterion("tests", select=["tests/test_b.py::*"])], hard_gate=True), _results(), {})
    assert decide(_results(), [hard_unmet]).decision == Decision.NO_GO
    sev0 = [CheckResult("s", "s", Dimension.SECURITY_SECRETS, Status.FAIL, findings=[Finding("s", Dimension.SECURITY_SECRETS, Severity.SEV0, "key")])]
    assert decide(sev0, []).decision == Decision.NO_GO
    sev1 = [CheckResult("s", "s", Dimension.FUNCTIONAL, Status.FAIL, findings=[Finding("s", Dimension.FUNCTIONAL, Severity.SEV1, "x")])]
    assert decide(sev1, []).decision == Decision.CONDITIONAL
    unverified_gate = evaluate_goal(Goal("H", "h", [], hard_gate=True), [], {})
    assert decide([], [unverified_gate]).decision == Decision.CONDITIONAL
    assert decide([CheckResult("ok", "ok", Dimension.BUILD, Status.PASS)], []).decision == Decision.GO


def test_traceability_reports_unmapped_requirements(tmp_path):
    write(tmp_path, "docs/spec.md", "## REQ-01 login\n## REQ-02 export\n## REQ-03 audit\n")
    config = make_config(tmp_path, """
        [project]
        name = "x"
        [[trace_sources]]
        path = "docs/spec.md"
        pattern = "(?m)^## (REQ-\\\\d{2})"
        [[goals]]
        id = "G1"
        title = "login"
        covers = ["REQ-01", "REQ-99"]
        criteria = [{ kind = "check", check = "x" }]
    """)
    result = traceability(config)
    assert result.status == Status.FAIL
    assert sorted(f.title for f in result.findings) == ["Requirement REQ-02 has no goal mapping", "Requirement REQ-03 has no goal mapping"]
    assert result.metrics["unknown_references"] == ["REQ-99"]


def test_coverage_matrix_exposes_blind_spots():
    matrix = coverage_matrix(_results())
    assert matrix["FUNCTIONAL"]["state"] == "FAIL"
    assert matrix["PERFORMANCE"]["state"] == "UNCALIBRATED"
    assert matrix["UI_VISUAL"]["state"] == "NOT_COVERED"


def test_lifecycle_and_fix_briefs(tmp_path):
    config = make_config(tmp_path, """
        [project]
        name = "x"
        goal = "ship"
        [[architecture.modules]]
        name = "core"
        paths = ["src/**"]
    """)
    finding = Finding("c", Dimension.FUNCTIONAL, Severity.SEV1, "broken", location=Location("src/a.py", 3), module="core")
    known = Finding("c", Dimension.FUNCTIONAL, Severity.SEV2, "old", location=Location("src/b.py"), module="core")
    stats = mark_lifecycle([finding, known], {known.fingerprint, "gone"})
    assert (finding.lifecycle, known.lifecycle) == ("NEW", "EXISTING")
    assert stats == {"new": 1, "existing": 1, "resolved_since_baseline": 1}
    [brief] = write_fix_briefs(config, [finding, known], tmp_path)
    text = brief.read_text()
    assert "Do NOT modify, delete, skip or weaken any test" in text and "src/a.py:3" in text and "`.qa/**`" in text
