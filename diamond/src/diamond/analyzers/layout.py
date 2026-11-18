"""Layout breakage: horizontal overflow and broken images."""

from __future__ import annotations

from ..model import AnalyzerResult, Category, Finding, Severity, Status
from .base import AnalysisContext, register


@register
class LayoutAnalyzer:
    name = "layout"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        inventories = ctx.inventories()
        if not inventories:
            return AnalyzerResult.skipped(self.name, "no page inventory captured")
        result = AnalyzerResult(self.name, Status.PASS)
        for url, inv in inventories.items():
            vw = inv.get("viewport", {}).get("w", 0)
            if vw and inv.get("scrollWidth", 0) > vw + 1:
                offenders = [e for e in inv.get("elements", []) if e["rect"]["x"] + e["rect"]["w"] > vw + 1 or e["rect"]["x"] < -1]
                result.findings.append(Finding(
                    check_id="layout.horizontal_overflow", category=Category.LAYOUT, severity=Severity.SEV2,
                    title=f"اسکرول افقی ناخواسته (عرض محتوا {inv['scrollWidth']} در برابر {vw})",
                    detail="، ".join(e["selector"] for e in offenders[:5]),
                    page_url=url, target=offenders[0] if offenders else None,
                    evidence={"key": "overflow", "offenders": [e["selector"] for e in offenders[:10]]},
                    suggestion="عنصر بیرون‌زده را با max-width: 100%، flex-wrap یا overflow-wrap مهار کنید.",
                ))
            for img in inv.get("images", []):
                if img.get("broken"):
                    result.findings.append(Finding(
                        check_id="layout.broken_image", category=Category.VISUAL, severity=Severity.SEV2,
                        title="تصویر شکسته", detail=img.get("src", ""), page_url=url,
                        target={"selector": img["selector"]}, evidence={"key": img.get("src", "")},
                    ))
        return result
