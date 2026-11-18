"""Reviewer narration → findings, corroborated with measured telemetry.

When the reviewer says something is laggy or jumpy, the measured long frames,
layout shifts and slow interactions around that moment are attached as
evidence. If nothing was measured, the finding says so: the problem may be
perceptual (easing, duration, missing feedback) rather than computational.
"""

from __future__ import annotations

from ..model import AnalyzerResult, Category, Finding, Origin, Severity, Status
from .base import AnalysisContext, register

MERGE_WINDOW_MS = 8000
CORROBORATION_WINDOW_MS = 2500


def _telemetry_near(ctx: AnalysisContext, ts: float) -> dict:
    lo, hi = ts - CORROBORATION_WINDOW_MS, ts + CORROBORATION_WINDOW_MS
    frames = [e for e in ctx.of_type("loaf", "longtask") if lo <= e.get("start", e["ts"]) <= hi]
    shifts = [e for e in ctx.of_type("layout_shift") if lo <= e.get("start", e["ts"]) <= hi and not e.get("hadRecentInput")]
    slow = [e for e in ctx.of_type("event_timing") if lo <= e.get("start", e["ts"]) <= hi]
    out: dict = {}
    if frames:
        worst = max(frames, key=lambda e: e["duration"])
        out["long_frames"] = {"count": len(frames), "worst_ms": round(worst["duration"]),
                              "scripts": worst.get("scripts", [])[:3]}
    if shifts:
        out["layout_shift"] = {"sum": round(sum(e["value"] for e in shifts), 4),
                               "sources": sorted({s for e in shifts for s in e.get("sources", [])})[:5]}
    if slow:
        worst = max(slow, key=lambda e: e["duration"])
        out["slow_interaction"] = {"name": worst["name"], "duration_ms": round(worst["duration"]), "target": worst.get("target")}
    return out


@register
class VoiceAnalyzer:
    name = "voice"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        marks = ctx.of_type("mark")
        if not ctx.notes and not marks:
            return AnalyzerResult.skipped(self.name, "no narration or marks in this session")
        result = AnalyzerResult(self.name, Status.PASS)
        findings: list[Finding] = []

        for note in ctx.notes:
            if "ignore" in note.commands:
                continue
            if note.category is None and "record" not in note.commands:
                continue
            category = note.category or Category.OTHER
            evidence = {"key": int(note.anchor), "target_confidence": note.confidence, "pointer_event": note.target_kind,
                        "commands": sorted(note.commands)}
            if category in (Category.PERFORMANCE, Category.ANIMATION, Category.LAYOUT):
                telemetry = _telemetry_near(ctx, note.anchor)
                evidence["telemetry"] = telemetry or {
                    "note": "در این بازه فریم طولانی، پرش چیدمان یا تعامل کند اندازه‌گیری نشد؛ احتمالاً مسئله ادراکی است "
                            "(مدت/منحنی انیمیشن، نبود بازخورد بصری) و نه محاسباتی."}
            target_name = ""
            if note.target:
                target_name = note.target.get("name") or note.target.get("text") or note.target.get("tag", "")
            findings.append(Finding(
                check_id="voice.note", category=category, severity=note.severity, origin=Origin.VOICE,
                title=f"گزارش بازبین: {note.text[:70]}",
                detail=f"عنصر هدف: «{target_name[:60]}»" if target_name else "عنصر هدف با اطمینان کافی پیدا نشد.",
                page_url=note.page_url, target=note.target, evidence=evidence, utterance=note.text,
                ts=note.anchor, screenshot=ctx.store.shot_near(note.anchor),
            ))

        findings = self._merge(findings)

        voiced = [f.ts for f in findings if f.ts]
        for mark in marks:
            if any(abs(mark["ts"] - t) < 3000 for t in voiced):
                continue
            findings.append(Finding(
                check_id="voice.mark", category=Category.OTHER, severity=Severity.SEV3, origin=Origin.MARK,
                title="علامت‌گذاری دستی (بدون توضیح صوتی)", page_url=mark.get("url", ""), target=mark.get("el"),
                evidence={"key": int(mark["ts"])}, ts=mark["ts"], screenshot=ctx.store.shot_near(mark["ts"]),
            ))
        result.findings = findings
        result.metrics = {"utterances": len(ctx.notes), "reported": len(findings), "marks": len(marks)}
        result.summary = f"{len(findings)} مورد از روایت بازبین ثبت شد"
        return result

    @staticmethod
    def _merge(findings: list[Finding]) -> list[Finding]:
        """Consecutive remarks about the same element become one finding."""
        merged: list[Finding] = []
        for f in findings:
            prev = merged[-1] if merged else None
            same_target = prev and prev.target and f.target and prev.target.get("selector") == f.target.get("selector")
            if prev and same_target and prev.page_url == f.page_url and (f.ts or 0) - (prev.ts or 0) < MERGE_WINDOW_MS:
                prev.utterance += " / " + f.utterance
                prev.severity = min(prev.severity, f.severity, key=lambda s: s.rank)
                if prev.category == Category.OTHER:
                    prev.category = f.category
                prev.evidence.setdefault("merged", []).append(f.evidence)
                continue
            merged.append(f)
        return merged
