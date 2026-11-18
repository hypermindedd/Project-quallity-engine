"""Persian Markdown report.

Formatting rule (team preference): English code/paths/selectors never sit
inline inside Persian sentences or table cells — RTL/LTR mixing scrambles
them. Every selector, path and URL goes into its own fenced block.
"""

from __future__ import annotations

from collections import Counter

from ..model import AnalyzerResult, Finding, Severity

SEV_FA = {"SEV0": "🔴 بحرانی", "SEV1": "🟠 شدید", "SEV2": "🟡 متوسط", "SEV3": "🔵 جزئی", "INFO": "⚪ اطلاع"}
CAT_FA = {
    "VISUAL": "ظاهر و زیبایی", "LAYOUT": "جانمایی", "CONSISTENCY": "یکدستی طراحی", "INTERACTION": "تعامل",
    "ANIMATION": "انیمیشن", "PERFORMANCE": "کارایی و لگ", "ACCESSIBILITY": "دسترس‌پذیری", "RTL": "راست‌به‌چپ",
    "CONTENT": "محتوا", "RUNTIME": "خطای اجرا", "ARCHITECTURE": "معماری", "OTHER": "سایر",
}
ORIGIN_FA = {"VOICE": "🎙️ بازبین", "AUTO": "🤖 خودکار", "MARK": "📌 علامت دستی"}
STATUS_FA = {"PASS": "✅ بدون ایراد", "FAIL": "⚠️ ایراد دارد", "SKIPPED": "⏭️ اجرا نشد", "ERROR": "❌ خطای موتور"}


def _block(lang: str, text: str) -> str:
    return f"\n</div>\n\n```{lang}\n{text}\n```\n\n<div dir=\"rtl\">\n"


def render(meta: dict, results: list[AnalyzerResult], findings: list[Finding], speech_status: str) -> str:
    sev = Counter(f.severity.value for f in findings)
    cat = Counter(f.category.value for f in findings)
    lines = ['<div dir="rtl">', "", "# 💎 گزارش بازبینی Diamond", ""]
    lines += [f"**جلسه:** {meta.get('id', '')}  ", f"**گفتار به متن:** {speech_status}", ""]
    lines.append(_block("text", f"URL:  {meta.get('url', '')}\nREPO: {meta.get('repo', '') or '-'}"))

    lines += ["## خلاصه", "", "| شدت | تعداد |", "|---|---|"]
    lines += [f"| {SEV_FA[s.value]} | {sev.get(s.value, 0)} |" for s in Severity]
    lines += ["", "| دسته | تعداد |", "|---|---|"]
    lines += [f"| {CAT_FA[c]} | {n} |" for c, n in cat.most_common()]
    lines += ["", "## پوشش تحلیلگرها", "", "| تحلیلگر | وضعیت | تعداد ایراد |", "|---|---|---|"]
    for r in results:
        lines.append(f"| {r.analyzer} | {STATUS_FA[r.status.value]} | {len(r.findings)} |")
    skipped = [r for r in results if r.status.value in ("SKIPPED", "ERROR")]
    if skipped:
        lines += ["", "دلیل اجرا نشدن:"]
        lines.append(_block("text", "\n".join(f"{r.analyzer}: {r.summary}" for r in skipped)))

    lines += ["## ایرادها (به ترتیب شدت)", ""]
    for i, f in enumerate(findings, 1):
        lines += [f"### {i}. {f.title}", "",
                  f"{SEV_FA[f.severity.value]} · {CAT_FA[f.category.value]} · {ORIGIN_FA[f.origin.value]} · شناسه: {f.fingerprint}", ""]
        if f.utterance:
            lines += [f"> 🎙️ «{f.utterance}»", ""]
        if f.detail:
            lines += [f.detail, ""]
        if f.suggestion:
            lines += [f"**پیشنهاد اصلاح:** {f.suggestion}", ""]
        tech = []
        if f.page_url:
            tech.append(f"page:     {f.page_url}")
        if f.target and f.target.get("selector"):
            tech.append(f"element:  {f.target['selector']}")
        for s in f.sources:
            tech.append(f"source:   {s.render()}   ({s.method}, confidence {s.confidence:.1f})")
        if f.screenshot:
            tech.append(f"shot:     {f.screenshot}")
        if tech:
            lines.append(_block("text", "\n".join(tech)))
    if not findings:
        lines += ["هیچ ایرادی ثبت نشد. ✨", ""]
    lines += ["", "</div>", ""]
    return "\n".join(lines)
