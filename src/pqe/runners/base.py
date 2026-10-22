"""Runner contract.

A runner owns exactly one concern (Single Responsibility) and is discovered
through :data:`REGISTRY` (Open/Closed: new runners register themselves, the
orchestrator never changes). A runner must:

* declare whether it applies to the project (``applicable``) and say *why not*
  when it does not;
* never raise for an expected failure — it returns a ``CheckResult``;
* never report ``PASS`` for something it did not actually verify.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import ClassVar

from ..context import RunContext, ToolMissing
from ..model import CheckResult, Dimension


class Runner(ABC):
    id: ClassVar[str]
    title: ClassVar[str]
    dimension: ClassVar[Dimension]
    #: Optional runners are not executed by default unless the contract enables them.
    phase: ClassVar[int] = 50  # lower runs earlier

    def applicable(self, ctx: RunContext) -> str | None:
        """Return ``None`` when the runner applies, otherwise a skip reason."""
        return None

    @abstractmethod
    def execute(self, ctx: RunContext) -> list[CheckResult]:
        """Do the work. May return several results (one per sub-check)."""

    def run(self, ctx: RunContext) -> list[CheckResult]:
        reason = self.applicable(ctx)
        if reason:
            return [CheckResult.skipped(self.id, self.title, self.dimension, reason)]
        started = time.monotonic()
        try:
            results = self.execute(ctx)
        except ToolMissing as exc:
            return [CheckResult.skipped(self.id, self.title, self.dimension, f"required tool not installed: {exc}")]
        except Exception as exc:  # noqa: BLE001 - engine must survive any runner bug
            return [CheckResult.errored(self.id, self.title, self.dimension, f"{type(exc).__name__}: {ctx.redact(str(exc))}")]
        elapsed = time.monotonic() - started
        for result in results:
            if not result.duration_s:
                result.duration_s = elapsed
            result.summary = ctx.redact(result.summary)
            for finding in result.findings:
                finding.detail = ctx.redact(finding.detail)
        return results


REGISTRY: list[type[Runner]] = []


def register(cls: type[Runner]) -> type[Runner]:
    if any(existing.id == cls.id for existing in REGISTRY):
        raise ValueError(f"duplicate runner id: {cls.id}")
    REGISTRY.append(cls)
    return cls
