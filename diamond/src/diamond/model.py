"""Core domain model. No I/O lives here.

Principles shared with PQE (so findings can flow between the two engines):

* A check that did not run is never reported as passed: it is ``SKIPPED``
  with a reason, or ``ERROR`` when the engine itself failed.
* Every threshold has an explicit origin (a published standard or a
  configurable heuristic default). Heuristic findings never exceed SEV2.
* A finding always carries the evidence it was derived from.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    SEV0 = "SEV0"  # blocks release
    SEV1 = "SEV1"  # major
    SEV2 = "SEV2"  # moderate
    SEV3 = "SEV3"  # minor
    INFO = "INFO"  # observation

    @property
    def rank(self) -> int:
        return ["SEV0", "SEV1", "SEV2", "SEV3", "INFO"].index(self.value)

    def bump(self, steps: int = 1) -> Severity:
        order = list(Severity)
        return order[max(0, self.rank - steps)]


class Category(StrEnum):
    VISUAL = "VISUAL"            # colour, typography, alignment, beauty
    LAYOUT = "LAYOUT"            # placement, spacing, overflow, clutter
    CONSISTENCY = "CONSISTENCY"  # same thing styled differently across pages
    INTERACTION = "INTERACTION"  # clicks that do nothing, rage clicks, broken flows
    ANIMATION = "ANIMATION"      # smoothness, non-composited animation
    PERFORMANCE = "PERFORMANCE"  # lag, jank, layout shift, slow response
    ACCESSIBILITY = "ACCESSIBILITY"
    RTL = "RTL"                  # direction / Persian typography
    CONTENT = "CONTENT"          # copy, typos
    RUNTIME = "RUNTIME"          # console errors, failed requests
    ARCHITECTURE = "ARCHITECTURE"
    OTHER = "OTHER"


class Origin(StrEnum):
    VOICE = "VOICE"  # reported by the human reviewer (narration)
    AUTO = "AUTO"    # detected by an analyzer
    MARK = "MARK"    # explicit hotkey mark without speech


class Status(StrEnum):
    PASS = "PASS"  # noqa: S105 - status value, not a secret
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SourceLocation:
    path: str
    line: int | None = None
    column: int | None = None
    confidence: float = 1.0       # 1.0 = from build-time instrumentation, lower = heuristic search
    method: str = ""              # how it was resolved (attr / react-fiber / text-search / sourcemap)

    def render(self) -> str:
        if self.line is None:
            return self.path
        return f"{self.path}:{self.line}" + (f":{self.column}" if self.column else "")


@dataclass
class Finding:
    check_id: str
    category: Category
    severity: Severity
    title: str
    detail: str = ""
    origin: Origin = Origin.AUTO
    page_url: str = ""
    target: dict[str, Any] | None = None          # element descriptor captured in the browser
    evidence: dict[str, Any] = field(default_factory=dict)
    sources: list[SourceLocation] = field(default_factory=list)
    suggestion: str = ""
    utterance: str = ""                            # what the reviewer said, verbatim
    ts: float | None = None                        # epoch ms of the moment it refers to
    screenshot: str = ""                           # relative path inside the session dir

    @property
    def fingerprint(self) -> str:
        """Stable identity for regression tracking (never built from free text)."""
        selector = (self.target or {}).get("selector", "")
        key = str(self.evidence.get("key", ""))
        raw = "|".join([self.check_id, self.title, _page_key(self.page_url), selector, key])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["category"] = self.category.value
        data["severity"] = self.severity.value
        data["origin"] = self.origin.value
        data["id"] = self.fingerprint
        return data


@dataclass
class AnalyzerResult:
    analyzer: str
    status: Status
    summary: str = ""
    findings: list[Finding] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def skipped(cls, analyzer: str, reason: str) -> AnalyzerResult:
        return cls(analyzer, Status.SKIPPED, reason)

    @classmethod
    def errored(cls, analyzer: str, reason: str) -> AnalyzerResult:
        return cls(analyzer, Status.ERROR, reason)

    def finalize(self) -> AnalyzerResult:
        if self.status not in (Status.SKIPPED, Status.ERROR):
            self.status = Status.FAIL if self.findings else Status.PASS
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "analyzer": self.analyzer,
            "status": self.status.value,
            "summary": self.summary,
            "metrics": self.metrics,
            "findings": [f.to_dict() for f in self.findings],
        }


def _page_key(url: str) -> str:
    """URL without query/fragment, so the same page matches across runs."""
    return url.split("#", 1)[0].split("?", 1)[0]
