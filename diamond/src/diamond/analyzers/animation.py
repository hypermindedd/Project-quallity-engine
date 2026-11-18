"""Animation hygiene: layout-bound transitions, ``transition: all``, overly long durations."""

from __future__ import annotations

import re
from collections import defaultdict

from ..model import AnalyzerResult, Category, Finding, Severity, Status
from .base import AnalysisContext, register

_NTH = re.compile(r":nth-of-type\(\d+\)")


def pattern_of(selector: str) -> str:
    """Repeated items (list cards) collapse into one pattern."""
    return _NTH.sub("", selector)


@register
class AnimationAnalyzer:
    name = "animation"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        th = ctx.cfg.thresholds
        inventories = ctx.inventories()
        if not inventories:
            return AnalyzerResult.skipped(self.name, "no page inventory captured")
        result = AnalyzerResult(self.name, Status.PASS)
        layout: dict[str, list] = defaultdict(list)
        slow: dict[str, list] = defaultdict(list)
        for url, inv in inventories.items():
            for a in inv.get("animations", []):
                key = pattern_of(a["selector"])
                if a.get("layoutBound"):
                    layout[key].append((url, a))
                if max(a.get("transitionDurationMs", 0), a.get("animationDurationMs", 0)) > th.animation_max_ms.value:
                    slow[key].append((url, a))
        for key, items in layout.items():
            url, a = items[0]
            prop = a["transitionProperty"]
            result.findings.append(Finding(
                check_id="animation.layout_bound", category=Category.ANIMATION, severity=Severity.SEV3,
                title="ترنزیشن روی خاصیت‌های چیدمانی (احتمال لگ و نرم‌نبودن)",
                detail=f"خاصیت: {prop} — {len(items)} عنصر. تغییر اندازه/موقعیت باعث محاسبه‌ی مجدد چیدمان در هر فریم می‌شود.",
                page_url=url, target={"selector": a["selector"], "source": a.get("source", {})},
                evidence={"key": key, "transition_property": prop, "elements": len(items)},
                suggestion="به جای width/height/top/left از transform و opacity استفاده کنید؛ از transition: all پرهیز کنید.",
            ))
        for key, items in slow.items():
            url, a = items[0]
            dur = max(a.get("transitionDurationMs", 0), a.get("animationDurationMs", 0))
            result.findings.append(Finding(
                check_id="animation.too_long", category=Category.ANIMATION, severity=Severity.SEV3,
                title=f"انیمیشن طولانی ({int(dur)} میلی‌ثانیه)",
                detail="انیمیشن‌های بازخورد رابط معمولاً کوتاه‌اند؛ مدت طولانی حس کندی می‌دهد.",
                page_url=url, target={"selector": a["selector"], "source": a.get("source", {})},
                evidence={"key": key, "duration_ms": dur, **th.animation_max_ms.as_evidence()},
                suggestion="مدت را کوتاه‌تر کنید یا فقط برای انتقال‌های بزرگ صفحه نگه دارید.",
            ))
        result.metrics = {"animated_patterns": len({pattern_of(a['selector']) for inv in inventories.values() for a in inv.get('animations', [])})}
        return result
