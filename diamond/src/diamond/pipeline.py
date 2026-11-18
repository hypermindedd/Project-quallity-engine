"""Analysis pipeline: session directory → findings, report, PQE export, fix briefs.

Pure post-processing: it can be re-run on an old session with a newer engine,
and costs no tokens unless the opt-in visual critic is enabled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .analyzers import AnalysisContext, all_analyzers, run_safely
from .config import DiamondConfig
from .fix import fix_brief
from .fusion import align
from .locate import SourceIndex, locate_finding
from .model import AnalyzerResult, Finding
from .report import markdown, pqe_export
from .session import SessionStore
from .speech import TranscriptionUnavailable, transcribe_session

log = logging.getLogger("diamond.pipeline")


@dataclass
class AnalysisOutcome:
    session: Path
    findings: list[Finding]
    results: list[AnalyzerResult]
    report: Path
    speech_status: str

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.severity.value] = counts.get(f.severity.value, 0) + 1
        return {"session": str(self.session), "report": str(self.report), "findings": len(self.findings),
                "by_severity": counts, "speech": self.speech_status,
                "analyzers": {r.analyzer: r.status.value for r in self.results}}


def _speech(store: SessionStore, cfg: DiamondConfig) -> tuple[list, str]:
    words = store.words()
    if words:
        return words, f"✅ {len(words)} کلمه (از قبل)"
    if cfg.speech.provider == "none":
        return [], "⏭️ غیرفعال"
    if not (store.root / "audio.wav").is_file():
        return [], "⏭️ صدایی ضبط نشده"
    try:
        words = transcribe_session(store, cfg.speech)
        return words, f"✅ {len(words)} کلمه"
    except TranscriptionUnavailable as exc:
        return [], f"⏭️ {exc}"


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: dict[str, Finding] = {}
    for f in findings:
        if f.fingerprint not in seen:
            seen[f.fingerprint] = f
    return list(seen.values())


def _cross_link(findings: list[Finding]) -> None:
    """Human report + machine measurement on the same element = stronger evidence.

    Both findings reference each other; the automatic one is raised one severity
    step (never above SEV1) because a reviewer independently noticed it.
    """
    auto = [f for f in findings if f.origin.value == "AUTO" and f.target and f.target.get("selector")]
    for voice in (f for f in findings if f.origin.value != "AUTO" and f.target and f.target.get("selector")):
        related = [a for a in auto if a.target["selector"] == voice.target["selector"]
                   and a.page_url.split("#")[0] == voice.page_url.split("#")[0]]
        if not related:
            continue
        voice.evidence["corroborated_by"] = [a.fingerprint for a in related]
        for a in related:
            a.evidence.setdefault("reported_by_reviewer", []).append(voice.fingerprint)
            if a.severity.rank > 1:
                a.severity = a.severity.bump()
        if not voice.sources:
            voice.sources = list(next((a.sources for a in related if a.sources), []))


def analyze(session_dir: Path, cfg: DiamondConfig, repo: Path | None = None) -> AnalysisOutcome:
    store = SessionStore.open(session_dir)
    repo = repo or (Path(store.meta["repo"]) if store.meta.get("repo") else cfg.repo)
    words, speech_status = _speech(store, cfg)
    events = store.events()
    ctx = AnalysisContext(store=store, cfg=cfg, events=events, notes=align(words, events, cfg.fusion), repo=repo)

    results = [run_safely(a, ctx) for a in all_analyzers()]
    findings = _dedupe([f for r in results for f in r.findings])

    index = SourceIndex(repo)
    for f in findings:
        locate_finding(f, index)
    _cross_link(findings)
    # Human-reported problems first within the same severity: they are what the reviewer cares about.
    findings.sort(key=lambda f: (f.severity.rank, f.origin.value != "VOICE", f.category.value))

    store.write_json("findings.json", {"meta": store.meta, "speech": speech_status,
                                       "analyzers": [r.to_dict() | {"findings": len(r.findings)} for r in results],
                                       "findings": [f.to_dict() for f in findings]})
    store.write_json("pqe-findings.json", {"source": "diamond", "session": store.root.name,
                                           "findings": pqe_export.to_pqe(findings)})
    tests = pqe_export.regression_tests(findings)
    if tests:
        (store.root / "test_diamond_regressions.py").write_text(tests, encoding="utf-8")
    briefs = store.root / "briefs"
    briefs.mkdir(exist_ok=True)
    for f in findings:
        (briefs / f"{f.fingerprint}.md").write_text(fix_brief(f, cfg.routing), encoding="utf-8")
    report = store.root / "report.md"
    report.write_text(markdown.render(store.meta, results, findings, speech_status), encoding="utf-8")
    return AnalysisOutcome(store.root, findings, results, report, speech_status)
