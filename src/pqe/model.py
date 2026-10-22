"""Core domain model of the Project Quality Engine.

Every runner produces :class:`CheckResult` objects; the goal evaluator turns
them into :class:`GoalVerdict` objects; the decision policy turns both into a
release :class:`Decision`. Nothing in this module performs I/O.

Design rules (enforced across the engine):

* A check that did not actually run is never reported as passed. It is
  ``SKIPPED`` with a reason, or ``ERROR`` when the engine itself failed.
* A measurement without an approved threshold is ``UNCALIBRATED``: the value
  is recorded, but it neither passes nor fails. Thresholds are never invented.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"
    UNCALIBRATED = "UNCALIBRATED"


class Severity(StrEnum):
    """Impact-based severity. Mirrors a QA-SEV-0..3 style taxonomy."""

    SEV0 = "SEV0"  # critical / release blocking
    SEV1 = "SEV1"  # major
    SEV2 = "SEV2"  # moderate
    SEV3 = "SEV3"  # minor
    INFO = "INFO"  # observation, never blocks

    @property
    def rank(self) -> int:
        return {"SEV0": 0, "SEV1": 1, "SEV2": 2, "SEV3": 3, "INFO": 4}[self.value]


class Dimension(StrEnum):
    """Every quality dimension the engine knows about.

    The report lists each dimension with the checks that covered it, so a
    blind spot is always visible instead of silently missing.
    """

    GOAL = "GOAL"
    FUNCTIONAL = "FUNCTIONAL"
    BUILD = "BUILD"
    ARCHITECTURE = "ARCHITECTURE"
    CODE_QUALITY = "CODE_QUALITY"
    TEST_QUALITY = "TEST_QUALITY"
    API_CONTRACT = "API_CONTRACT"
    API_ROBUSTNESS = "API_ROBUSTNESS"
    E2E_FLOW = "E2E_FLOW"
    SECURITY_SECRETS = "SECURITY_SECRETS"
    SECURITY_SAST = "SECURITY_SAST"
    SECURITY_DEPENDENCIES = "SECURITY_DEPENDENCIES"
    SECURITY_RUNTIME = "SECURITY_RUNTIME"
    PERFORMANCE = "PERFORMANCE"
    LOAD = "LOAD"
    UI_INTERACTION = "UI_INTERACTION"
    UI_VISUAL = "UI_VISUAL"
    UI_RESPONSIVE = "UI_RESPONSIVE"
    ACCESSIBILITY = "ACCESSIBILITY"
    INTEGRITY = "INTEGRITY"
    DOCUMENTATION = "DOCUMENTATION"


class GoalStatus(StrEnum):
    MET = "MET"
    NOT_MET = "NOT_MET"
    PARTIAL = "PARTIAL"
    UNVERIFIED = "UNVERIFIED"


class Decision(StrEnum):
    GO = "GO"
    CONDITIONAL = "CONDITIONAL"
    NO_GO = "NO_GO"


@dataclass(frozen=True)
class Location:
    path: str
    line: int | None = None

    def render(self) -> str:
        return f"{self.path}:{self.line}" if self.line else self.path


@dataclass
class Finding:
    check_id: str
    dimension: Dimension
    severity: Severity
    title: str
    detail: str = ""
    location: Location | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    module: str = ""  # architecture owner, filled by triage
    lifecycle: str = ""  # NEW / EXISTING, filled by triage against baseline

    @property
    def fingerprint(self) -> str:
        """Stable identity used for regression tracking across runs.

        Built from what the problem *is* and *where* it is, never from the
        free-text detail (which may contain timings or run-specific values).
        """
        loc = self.location.render() if self.location else ""
        raw = "|".join([self.check_id, self.title, loc, str(self.evidence.get("key", ""))])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["dimension"] = self.dimension.value
        data["severity"] = self.severity.value
        data["fingerprint"] = self.fingerprint
        return data


@dataclass
class TestCase:
    """One executed test case (from JUnit XML or a command)."""

    __test__ = False  # keep pytest from collecting this class

    id: str
    file: str
    name: str
    status: Status
    duration_s: float = 0.0
    message: str = ""


@dataclass
class CheckResult:
    check_id: str
    title: str
    dimension: Dimension
    status: Status
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    tests: list[TestCase] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @classmethod
    def skipped(cls, check_id: str, title: str, dimension: Dimension, reason: str) -> CheckResult:
        return cls(check_id, title, dimension, Status.SKIPPED, summary=reason)

    @classmethod
    def errored(cls, check_id: str, title: str, dimension: Dimension, reason: str) -> CheckResult:
        return cls(check_id, title, dimension, Status.ERROR, summary=reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "title": self.title,
            "dimension": self.dimension.value,
            "status": self.status.value,
            "summary": self.summary,
            "findings": [f.to_dict() for f in self.findings],
            "metrics": self.metrics,
            "tests": [
                {**asdict(t), "status": t.status.value} for t in self.tests
            ],
            "artifacts": self.artifacts,
            "duration_s": round(self.duration_s, 3),
        }


@dataclass
class CriterionVerdict:
    kind: str
    reference: str
    status: Status
    detail: str = ""


@dataclass
class GoalVerdict:
    goal_id: str
    title: str
    status: GoalStatus
    hard_gate: bool
    source: str = ""
    criteria: list[CriterionVerdict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "title": self.title,
            "status": self.status.value,
            "hard_gate": self.hard_gate,
            "source": self.source,
            "criteria": [{**asdict(c), "status": c.status.value} for c in self.criteria],
        }


@dataclass
class DecisionRecord:
    decision: Decision
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"decision": self.decision.value, "reasons": self.reasons}
