"""Design-system leakage across every page the reviewer opened.

Extracts the *de facto* design tokens (colours, font sizes, families, button
variants, spacing) and reports where the product drifts: near-duplicate
colours, too many type sizes, too many button styles, off-grid spacing.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..model import AnalyzerResult, Category, Finding, Severity, Status
from .base import AnalysisContext, register
from .colors import delta_e, hex_of, parse_rgb

_PX = re.compile(r"^(-?\d+(?:\.\d+)?)px$")


def px(value: str | None) -> float | None:
    m = _PX.match((value or "").strip())
    return float(m.group(1)) if m else None


def is_button(el: dict) -> bool:
    if el.get("tag") == "button" or el.get("role") == "button":
        return True
    st = el.get("styles", {})
    bg = parse_rgb(st.get("backgroundColor"))
    return el.get("tag") == "a" and bg is not None and bg[1] > 0.5 and (px(st.get("paddingLeft")) or 0) >= 8


def is_icon_button(el: dict) -> bool:
    """Round/square icon buttons (mic, close…) are a separate, legitimate shape."""
    r = el.get("rect", {})
    return not el.get("text") and abs(r.get("w", 0) - r.get("h", 0)) <= 4


def near_duplicate_clusters(colors: Counter, threshold: float) -> list[list[tuple]]:
    """Union-find over colours whose ΔE is > 0 and < threshold."""
    items = list(colors)
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if 0 < delta_e(items[i], items[j]) < threshold:
                parent[find(i)] = find(j)
    groups: dict[int, list] = defaultdict(list)
    for i, c in enumerate(items):
        groups[find(i)].append(c)
    return [sorted(g, key=lambda c: -colors[c]) for g in groups.values() if len(g) > 1]


@register
class ConsistencyAnalyzer:
    name = "consistency"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        th = ctx.cfg.thresholds
        inventories = ctx.inventories()
        if not inventories:
            return AnalyzerResult.skipped(self.name, "no page inventory captured")
        result = AnalyzerResult(self.name, Status.PASS)

        colors: Counter = Counter()
        color_examples: dict[tuple, str] = {}
        font_sizes: Counter = Counter()
        families: Counter = Counter()
        variants: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
        spacing: Counter = Counter()

        for url, inv in inventories.items():
            for el in inv.get("elements", []):
                st = el.get("styles") or {}
                has_text = bool(el.get("text"))
                for prop in (("color",) if has_text else ()) + (("backgroundColor",) if el.get("interactive") else ()):
                    parsed = parse_rgb(st.get(prop))
                    if parsed and parsed[1] >= 0.5:
                        colors[parsed[0]] += 1
                        color_examples.setdefault(parsed[0], el["selector"])
                if has_text and (size := px(st.get("fontSize"))):
                    font_sizes[size] += 1
                if has_text and st.get("fontFamily"):
                    families[st["fontFamily"].split(",")[0].strip().strip("\"'").lower()] += 1
                if is_button(el) and not is_icon_button(el):
                    # Size/shape only: colour variants (primary, ghost, danger…) are intentional design,
                    # inconsistent height, radius, type size and padding are drift.
                    sig = (round(el["rect"]["h"] / 4) * 4, st.get("borderRadius"), st.get("fontSize"),
                           st.get("fontWeight"), st.get("paddingLeft"))
                    variants[sig].append((url, el["selector"], st.get("backgroundColor")))
                for prop in ("paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
                             "marginTop", "marginRight", "marginBottom", "marginLeft"):
                    v = px(st.get(prop))
                    if v and v > 0:
                        spacing[v] += 1

        for cluster in near_duplicate_clusters(colors, th.color_near_delta_e.value):
            result.findings.append(Finding(
                check_id="consistency.near_duplicate_colors", category=Category.CONSISTENCY, severity=Severity.SEV3,
                title=f"{len(cluster)} رنگ تقریباً یکسان (احتمالاً باید یک توکن باشند)",
                detail="، ".join(f"{hex_of(c)} ×{colors[c]}" for c in cluster),
                target={"selector": color_examples[cluster[-1]]},
                evidence={"key": "|".join(sorted(hex_of(c) for c in cluster)),
                          "colors": [{"hex": hex_of(c), "uses": colors[c], "example": color_examples[c]} for c in cluster],
                          **th.color_near_delta_e.as_evidence()},
                suggestion="رنگ پرتکرار را توکن کنید و بقیه را به آن ارجاع دهید.",
            ))
        if len(font_sizes) > th.max_font_sizes.value:
            result.findings.append(Finding(
                check_id="consistency.font_sizes", category=Category.CONSISTENCY, severity=Severity.SEV3,
                title=f"{len(font_sizes)} اندازه‌ی فونت متفاوت در صفحات بازبینی‌شده",
                detail="، ".join(f"{s:g}px ×{n}" for s, n in sorted(font_sizes.items())),
                evidence={"key": "font_sizes", "sizes": dict(sorted(font_sizes.items())), **th.max_font_sizes.as_evidence()},
                suggestion="یک مقیاس تایپوگرافی (مثلاً ۶ تا ۸ پله) تعریف کنید و اندازه‌های پراکنده را به نزدیک‌ترین پله ببرید.",
            ))
        if len(families) > 3:
            result.findings.append(Finding(
                check_id="consistency.font_families", category=Category.CONSISTENCY, severity=Severity.SEV3,
                title=f"{len(families)} خانواده‌ی فونت متفاوت",
                detail="، ".join(f"{f} ×{n}" for f, n in families.most_common()),
                evidence={"key": "families", "families": dict(families)},
                suggestion="حداکثر یک فونت متن و یک فونت عنوان (و برای فارسی یک فونت فارسی مشخص).",
            ))
        if len(variants) > th.max_button_variants.value:
            listed = sorted(variants.items(), key=lambda kv: -len(kv[1]))
            result.findings.append(Finding(
                check_id="consistency.button_variants", category=Category.CONSISTENCY, severity=Severity.SEV3,
                title=f"{len(variants)} اندازه/شکل متفاوت برای دکمه‌ها",
                detail="ارتفاع، گردی گوشه، اندازه و وزن فونت یا پدینگ دکمه‌ها یکدست نیست (رنگ‌ها جدا حساب نمی‌شوند).",
                target={"selector": listed[-1][1][0][1]},
                evidence={"key": "buttons", **th.max_button_variants.as_evidence(), "variants": [
                    {"height": s[0], "radius": s[1], "font_size": s[2], "weight": s[3], "padding_x": s[4],
                     "count": len(v), "example": v[0][1], "page": v[0][0]} for s, v in listed]},
                suggestion="یک کامپوننت دکمه با چند واریانت تعریف‌شده (primary/secondary/ghost × اندازه) بسازید.",
            ))
        grid = th.spacing_grid_px.value
        total = sum(spacing.values())
        off = sum(n for v, n in spacing.items() if v % grid)
        if total and off / total > th.off_grid_ratio.value:
            result.findings.append(Finding(
                check_id="consistency.spacing_grid", category=Category.CONSISTENCY, severity=Severity.INFO,
                title=f"{round(100 * off / total)}٪ فاصله‌ها خارج از گرید {int(grid)} پیکسلی",
                detail="، ".join(f"{v:g}px ×{n}" for v, n in sorted(spacing.items()) if v % grid)[:400],
                evidence={"key": "spacing", "off_grid_ratio": round(off / total, 3), **th.off_grid_ratio.as_evidence()},
                suggestion="مقیاس فاصله (۴، ۸، ۱۲، ۱۶، ۲۴، ۳۲…) را توکن کنید.",
            ))
        result.metrics = {"pages": len(inventories), "colors": len(colors), "font_sizes": len(font_sizes),
                          "button_variants": len(variants), "font_families": len(families)}
        return result
