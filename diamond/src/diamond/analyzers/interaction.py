"""Clicks that do nothing, rage clicks, and runtime errors during the session."""

from __future__ import annotations

from collections import defaultdict

from ..model import AnalyzerResult, Category, Finding, Severity, SourceLocation, Status
from .base import AnalysisContext, register

_NOISE_REQUESTS = ("favicon.ico", "/__nextjs", "webpack-hmr", "/_next/webpack", "hot-update")


@register
class InteractionAnalyzer:
    name = "interaction"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        th = ctx.cfg.thresholds
        result = AnalyzerResult(self.name, Status.PASS)
        clicks = ctx.of_type("click")
        by_ts = {round(c["ts"]): c for c in clicks}

        # ---- dead clicks: something that looks clickable, clicked, and nothing happened
        dead: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for eff in ctx.of_type("click_effect"):
            el = eff.get("el") or {}
            looks_clickable = el.get("interactive") or el.get("cursor") == "pointer"
            no_effect = not (eff["mutations"] or eff["requests"] or eff["navigated"] or eff["focusChanged"])
            if looks_clickable and no_effect:
                dead[(eff["url"], el.get("selector", ""))].append(eff)
        for (url, selector), effs in dead.items():
            click = by_ts.get(round(effs[0]["clickTs"]), {})
            result.findings.append(Finding(
                check_id="interaction.dead_click", category=Category.INTERACTION, severity=Severity.SEV2,
                title="کلیک بی‌اثر: عنصر قابل‌کلیک به نظر می‌رسد اما هیچ واکنشی ندارد",
                detail=f"{len(effs)} کلیک، در {int(th.dead_click_ms.value)} میلی‌ثانیه بعد از هر کلیک نه تغییری در صفحه، "
                       "نه درخواست شبکه، نه ناوبری و نه تغییر فوکوس دیده شد.",
                page_url=url, target=click.get("el") or {"selector": selector},
                evidence={"key": selector, "clicks": len(effs), **th.dead_click_ms.as_evidence()},
                suggestion="یا رفتار کلیک را پیاده کنید، یا ظاهر قابل‌کلیک (نشانگر دست، استایل دکمه) را بردارید.",
                ts=effs[0]["clickTs"], screenshot=ctx.store.shot_near(effs[0]["clickTs"]),
            ))

        # ---- rage clicks
        count, window, radius = int(th.rage_click_count.value), th.rage_click_window_ms.value, th.rage_click_radius_px.value
        i = 0
        while i < len(clicks):
            burst = [clicks[i]]
            for c in clicks[i + 1:]:
                if (c["ts"] - clicks[i]["ts"] <= window and c["url"] == clicks[i]["url"]
                        and abs(c["x"] - clicks[i]["x"]) <= radius and abs(c["y"] - clicks[i]["y"]) <= radius):
                    burst.append(c)
            if len(burst) >= count:
                result.findings.append(Finding(
                    check_id="interaction.rage_click", category=Category.INTERACTION, severity=Severity.SEV2,
                    title="کلیک‌های عصبی (پشت‌سرهم روی یک نقطه)",
                    detail=f"{len(burst)} کلیک در کمتر از {int(window)} میلی‌ثانیه؛ معمولاً یعنی بازخورد دیر یا ناکافی است.",
                    page_url=burst[0]["url"], target=burst[0].get("el"),
                    evidence={"key": burst[0].get("el", {}).get("selector", ""), "clicks": len(burst),
                              **th.rage_click_count.as_evidence()},
                    suggestion="بازخورد فوری (حالت فشرده، اسپینر، غیرفعال‌سازی موقت) اضافه کنید و زمان پاسخ را بسنجید.",
                    ts=burst[0]["ts"], screenshot=ctx.store.shot_near(burst[0]["ts"]),
                ))
                i += len(burst)
            else:
                i += 1

        # ---- runtime errors
        errors: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for e in ctx.of_type("console_error", "page_error"):
            if e["text"].startswith("Failed to load resource"):
                continue  # the same failure is reported once, with its URL, as a network finding
            errors[(e["type"], e["text"][:200])].append(e)
        for (kind, text), group in errors.items():
            loc = group[0].get("location") or {}
            sources = []
            if loc.get("url"):
                sources.append(SourceLocation(loc["url"], loc.get("lineNumber"), loc.get("columnNumber"), 0.5, "console"))
            result.findings.append(Finding(
                check_id=f"runtime.{kind}", category=Category.RUNTIME, severity=Severity.SEV1,
                title="خطای جاوااسکریپت در صفحه" if kind == "page_error" else "خطا در کنسول مرورگر",
                detail=text, page_url=group[0]["url"], evidence={"key": text[:120], "occurrences": len(group)},
                sources=sources, ts=group[0]["ts"],
            ))
        for e in ctx.of_type("http_error", "request_failed"):
            req = e.get("request", "")
            if any(n in req for n in _NOISE_REQUESTS):
                continue
            status = e.get("status")
            severity = Severity.SEV1 if (status or 0) >= 500 else Severity.SEV2
            result.findings.append(Finding(
                check_id="runtime.network", category=Category.RUNTIME, severity=severity,
                title=f"درخواست ناموفق ({status or e.get('failure', 'failed')})",
                detail=f"{e.get('method', '')} {req}", page_url=e["url"],
                evidence={"key": req.split("?", 1)[0], "status": status}, ts=e["ts"],
            ))
        result.metrics = {"clicks": len(clicks), "dead_click_targets": len(dead)}
        return result
