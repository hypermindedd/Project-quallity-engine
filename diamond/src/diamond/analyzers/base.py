"""Analyzer contract and shared analysis context."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import DiamondConfig
from ..fusion import VoiceNote
from ..model import AnalyzerResult
from ..session import SessionStore

log = logging.getLogger("diamond.analyzers")


@dataclass
class AnalysisContext:
    store: SessionStore
    cfg: DiamondConfig
    events: list[dict[str, Any]]
    notes: list[VoiceNote] = field(default_factory=list)
    repo: Path | None = None

    def of_type(self, *types: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") in types]

    def inventories(self) -> dict[str, dict[str, Any]]:
        """Latest inventory per page URL (without fragment)."""
        latest: dict[str, dict[str, Any]] = {}
        for inv in self.of_type("inventory"):
            latest[inv["url"].split("#", 1)[0]] = inv
        return latest


class Analyzer(Protocol):
    name: str

    def run(self, ctx: AnalysisContext) -> AnalyzerResult: ...


_REGISTRY: list[type] = []


def register(cls: type) -> type:
    _REGISTRY.append(cls)
    return cls


def all_analyzers() -> list[Analyzer]:
    return [cls() for cls in _REGISTRY]


def run_safely(analyzer: Analyzer, ctx: AnalysisContext) -> AnalyzerResult:
    """An analyzer crash is reported as ERROR, never hidden and never fatal."""
    try:
        return analyzer.run(ctx).finalize()
    except Exception as exc:  # noqa: BLE001 - isolation boundary
        log.exception("analyzer %s failed", analyzer.name)
        return AnalyzerResult.errored(analyzer.name, f"{type(exc).__name__}: {exc}")
