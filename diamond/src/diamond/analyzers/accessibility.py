"""Accessibility checks from the captured inventory (+ axe-core results when configured)."""

from __future__ import annotations

from collections import defaultdict

from ..model import AnalyzerResult, Category, Finding, Severity, Status
from .base import AnalysisContext, register
from .colors import blend, contrast_ratio, hex_of, parse_rgb
from .consistency import px

_AXE_IMPACT = {"critical": Severity.SEV1, "serious": Severity.SEV2, "moderate": Severity.SEV3, "minor": Severity.INFO}


def is_large_text(st: dict) -> bool:
    size = px(st.get("fontSize")) or 16
    weight = int(st.get("fontWeight") or 400) if str(st.get("fontWeight", "400")).isdigit() else 400
    return size >= 24 or (size >= 18.66 and weight >= 700)


@register
class AccessibilityAnalyzer:
    name = "accessibility"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        th = ctx.cfg.thresholds
        inventories = ctx.inventories()
        if not inventories:
            return AnalyzerResult.skipped(self.name, "no page inventory captured")
        result = AnalyzerResult(self.name, Status.PASS)
        for url, inv in inventories.items():
            low: dict[tuple, list] = defaultdict(list)
            small: list[dict] = []
            unnamed: list[dict] = []
            on_image: list[dict] = []
            for el in inv.get("elements", []):
                st = el.get("styles") or {}
                if el.get("text") and st.get("effectiveBackground") == "image":
                    on_image.append(el)  # gradient/image background: colours alone cannot decide
                elif el.get("text"):
                    fg, bg = parse_rgb(st.get("color")), parse_rgb(st.get("effectiveBackground"))
                    if fg and bg:
                        fg_rgb = blend(fg[0], fg[1] * float(st.get("opacity") or 1), bg[0])
                        ratio = contrast_ratio(fg_rgb, bg[0])
                        need = (th.contrast_large if is_large_text(st) else th.contrast_normal).value
                        if ratio < need:
                            low[(hex_of(fg_rgb), hex_of(bg[0]), need)].append((el, ratio))
                if el.get("interactive"):
                    inline_link = el.get("tag") == "a" and st.get("display") == "inline"
                    r = el["rect"]
                    if not inline_link and min(r["w"], r["h"]) < th.target_size_px.value and el.get("tag") not in ("input", "select", "textarea"):
                        small.append(el)
                    if el.get("tag") in ("button", "a") or el.get("role") == "button":
                        if not el.get("name"):
                            unnamed.append(el)
            for (fg, bg, need), items in low.items():
                el, ratio = items[0]
                result.findings.append(Finding(
                    check_id="a11y.contrast", category=Category.ACCESSIBILITY, severity=Severity.SEV2,
                    title=f"کنتراست ناکافی متن ({ratio:.2f}:1، حداقل {need:g}:1)",
                    detail=f"رنگ متن {fg} روی زمینه {bg} — {len(items)} عنصر.",
                    page_url=url, target=el,
                    evidence={"key": f"{fg}/{bg}", "ratio": round(ratio, 2), "elements": len(items),
                              "threshold": need, "threshold_origin": th.contrast_normal.origin},
                    suggestion="رنگ متن را تیره‌تر (یا زمینه را روشن‌تر) کنید تا به حداقل نسبت برسد.",
                ))
            if on_image:
                result.findings.append(Finding(
                    check_id="a11y.contrast_unverified", category=Category.ACCESSIBILITY, severity=Severity.INFO,
                    title=f"{len(on_image)} متن روی گرادیان/تصویر؛ کنتراست با رنگ قابل محاسبه نیست",
                    detail="، ".join((e.get("text") or "")[:20] for e in on_image[:6]),
                    page_url=url, target=on_image[0],
                    evidence={"key": "contrast_on_image", "elements": [e["selector"] for e in on_image[:20]]},
                    suggestion="با چشم یا با نقد بصری (visual_critic) بررسی شود.",
                ))
            if small:
                result.findings.append(Finding(
                    check_id="a11y.target_size", category=Category.ACCESSIBILITY, severity=Severity.SEV3,
                    title=f"{len(small)} هدف لمسی/کلیکی کوچک‌تر از {int(th.target_size_px.value)} پیکسل",
                    detail="، ".join(f"{e.get('name') or e['tag']} ({e['rect']['w']}×{e['rect']['h']})" for e in small[:6]),
                    page_url=url, target=small[0],
                    evidence={"key": "target_size", "elements": [e["selector"] for e in small[:20]], **th.target_size_px.as_evidence()},
                    suggestion="ناحیه‌ی کلیک را با padding یا min-width/min-height بزرگ کنید (ظاهر می‌تواند کوچک بماند).",
                ))
            for el in unnamed:
                result.findings.append(Finding(
                    check_id="a11y.accessible_name", category=Category.ACCESSIBILITY, severity=Severity.SEV2,
                    title="دکمه/لینک بدون نام قابل‌خواندن",
                    detail="صفحه‌خوان چیزی برای خواندن ندارد (معمولاً دکمه‌ی فقط-آیکون).",
                    page_url=url, target=el, evidence={"key": el["selector"]},
                    suggestion="aria-label یا متن مخفی برای صفحه‌خوان اضافه کنید.",
                ))
            for img in inv.get("images", []):
                if img.get("alt") is None and not img.get("broken"):
                    result.findings.append(Finding(
                        check_id="a11y.img_alt", category=Category.ACCESSIBILITY, severity=Severity.SEV3,
                        title="تصویر بدون ویژگی alt", detail=img.get("src", ""), page_url=url,
                        target={"selector": img["selector"]}, evidence={"key": img["selector"]},
                        suggestion="برای تصویر معنادار متن جایگزین و برای تزئینی alt خالی بگذارید.",
                    ))
        for axe in ctx.of_type("axe"):
            for v in axe.get("violations", []):
                result.findings.append(Finding(
                    check_id=f"a11y.axe.{v['id']}", category=Category.ACCESSIBILITY,
                    severity=_AXE_IMPACT.get(v.get("impact") or "", Severity.SEV3),
                    title=f"axe: {v.get('help', v['id'])}", detail=v.get("description", ""), page_url=axe["url"],
                    target={"selector": v["nodes"][0]} if v.get("nodes") else None,
                    evidence={"key": v["id"], "nodes": v.get("nodes", [])[:10], "help_url": v.get("helpUrl", "")},
                ))
        return result
