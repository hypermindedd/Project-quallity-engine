"""Code quality: correctness-level lint.

By default only the error classes that indicate real defects are enforced
(syntax errors, undefined names, invalid comparisons) — the same subset the
standard GitHub Python workflow uses. Style is the project's own business and
belongs in a declared command, not in a release gate.
"""

from __future__ import annotations

import json

from ..context import RunContext
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status
from .base import Runner, register

DEFECT_RULES = "E9,F63,F7,F82"


@register
class RuffRunner(Runner):
    id = "quality.python.defects"
    title = "Python defect-class lint (ruff)"
    dimension = Dimension.CODE_QUALITY
    phase = 12

    def _paths(self, ctx: RunContext) -> list[str]:
        return ctx.config.architecture.python_roots

    def applicable(self, ctx: RunContext) -> str | None:
        return None if self._paths(ctx) else "no architecture.python_roots declared"

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        proc = ctx.run(
            ["ruff", "check", "--no-cache", "--output-format", "json", "--select", DEFECT_RULES, *self._paths(ctx)],
            timeout_s=600,
        )
        try:
            items = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError:
            return [CheckResult.errored(self.id, self.title, self.dimension, "ruff returned non-JSON output:\n" + proc.tail())]
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        for item in items:
            code = item.get("code") or "?"
            severity = Severity.SEV1 if code.startswith("E9") else Severity.SEV2
            path = ctx.rel(ctx.root / item.get("filename", "")) if item.get("filename") else ""
            line = (item.get("location") or {}).get("row")
            result.findings.append(
                Finding(self.id, self.dimension, severity, f"{code}: {item.get('message')}", location=Location(path, line),
                        evidence={"key": f"{code}:{path}:{line}"})
            )
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(result.findings)} defect-class issue(s)" if result.findings else "no defect-class issues"
        return [result]
