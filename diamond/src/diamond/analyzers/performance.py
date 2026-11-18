"""Measured smoothness: CLS (session windows), slow interactions (INP proxy), long frames."""

from __future__ import annotations

from collections import defaultdict

from ..model import AnalyzerResult, Category, Finding, Severity, SourceLocation, Status
from .base import AnalysisContext, register

INTERACTIONS = {"click", "pointerdown", "pointerup", "keydown", "keyup", "mousedown", "mouseup"}
INP_POOR_MS = 500  # web.dev: INP > 500ms is "poor"


def cls_session_windows(shifts: list[dict]) -> float:
    """CLS = largest session window (gap < 1s, window ≤ 5s), per the web.dev definition."""
    best = current = 0.0
    first = last = None
    for s in sorted(shifts, key=lambda e: e.get("start", e["ts"])):
        if s.get("hadRecentInput"):
            continue
        t = s.get("start", s["ts"])
        if first is None or t - last >= 1000 or t - first >= 5000:
            first, current = t, 0.0
        current += s["value"]
        last = t
        best = max(best, current)
    return best


@register
class PerformanceAnalyzer:
    name = "performance"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        th = ctx.cfg.thresholds
        result = AnalyzerResult(self.name, Status.PASS)
        pages: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for e in ctx.of_type("layout_shift", "event_timing", "loaf", "longtask"):
            pages[e["url"].split("#", 1)[0]][e["type"]].append(e)

        metrics = {}
        for url, kinds in pages.items():
            cls = cls_session_windows(kinds["layout_shift"])
            interactions = [e for e in kinds["event_timing"] if e["name"] in INTERACTIONS]
            worst_inp = max(interactions, key=lambda e: e["duration"], default=None)
            frames = [e for e in kinds["loaf"] if e["duration"] > th.long_frame_ms.value]
            metrics[url] = {"cls": round(cls, 4), "worst_interaction_ms": round(worst_inp["duration"]) if worst_inp else None,
                            "long_frames": len(frames), "long_tasks": len(kinds["longtask"])}

            if cls > th.cls.value:
                sources = sorted({s for e in kinds["layout_shift"] for s in e.get("sources", [])})
                result.findings.append(Finding(
                    check_id="perf.cls", category=Category.PERFORMANCE, severity=Severity.SEV2,
                    title=f"پرش چیدمان صفحه (CLS = {cls:.3f})",
                    detail="عناصر بعد از نمایش جابه‌جا می‌شوند. منبع‌های جابه‌جایی در شواهد آمده‌اند.",
                    page_url=url, target={"selector": sources[0]} if sources else None,
                    evidence={"key": "cls", "value": round(cls, 4), "sources": sources[:8], **th.cls.as_evidence()},
                    suggestion="برای تصاویر/ویدیوها/بنرها ابعاد یا aspect-ratio ثابت بگذارید و محتوای دیرآینده را در فضای رزروشده بارگذاری کنید.",
                ))
            if worst_inp and worst_inp["duration"] > th.inp_ms.value:
                severe = worst_inp["duration"] > INP_POOR_MS
                result.findings.append(Finding(
                    check_id="perf.slow_interaction", category=Category.PERFORMANCE,
                    severity=Severity.SEV1 if severe else Severity.SEV2,
                    title=f"پاسخ کند به تعامل ({round(worst_inp['duration'])} میلی‌ثانیه)",
                    detail=f"رویداد {worst_inp['name']}؛ زمان پردازش هندلر {round(worst_inp.get('processing', 0))} میلی‌ثانیه.",
                    page_url=url, target={"selector": worst_inp.get("target", "")},
                    evidence={"key": worst_inp.get("target", ""), "duration_ms": round(worst_inp["duration"]), **th.inp_ms.as_evidence()},
                    suggestion="کار سنگین هندلر را به بعد از رندر (یا Web Worker) منتقل کنید و اول بازخورد بصری بدهید.",
                    ts=worst_inp.get("start"), screenshot=ctx.store.shot_near(worst_inp.get("start", 0)),
                ))
            by_script: dict[str, list[dict]] = defaultdict(list)
            for f in frames:
                scripts = f.get("scripts") or [{}]
                top = max(scripts, key=lambda s: s.get("duration", 0))
                key = f"{top.get('sourceURL', '')}#{top.get('sourceFunctionName', '')}" if top else "unattributed"
                by_script[key].append({**f, "_top": top})
            for key, group in by_script.items():
                worst = max(group, key=lambda e: e["duration"])
                top = worst["_top"]
                sources = []
                if top.get("sourceURL"):
                    sources.append(SourceLocation(top["sourceURL"], None, None, 0.4, "loaf-script"))
                result.findings.append(Finding(
                    check_id="perf.long_frames", category=Category.PERFORMANCE,
                    severity=Severity.SEV2 if worst["duration"] >= 200 else Severity.SEV3,
                    title=f"فریم‌های طولانی ({len(group)} بار، بدترین {round(worst['duration'])} میلی‌ثانیه)",
                    detail=f"اسکریپت مسئول: {top.get('sourceFunctionName') or '؟'} — فراخوان: {top.get('invoker') or '؟'}",
                    page_url=url, sources=sources,
                    evidence={"key": key, "count": len(group), "worst_ms": round(worst["duration"]),
                              "blocking_ms": round(worst.get("blockingDuration", 0)), "script": top,
                              **th.long_frame_ms.as_evidence()},
                    suggestion="کار را تکه‌تکه کنید (yield به مرورگر)، محاسبات را memoize کنید یا به Worker ببرید.",
                    ts=worst.get("start"),
                ))
        result.metrics = metrics
        if not pages:
            result.summary = "هیچ داده‌ی کارایی ثبت نشد (مرورگر از این API ها پشتیبانی نکرد یا تعاملی نبود)"
        return result
