"""Persian / RTL correctness: page direction, misaligned Persian text, digits."""

from __future__ import annotations

import re

from ..model import AnalyzerResult, Category, Finding, Severity, Status
from .base import AnalysisContext, register

PERSIAN = re.compile(r"[؀-ۿ]")
LATIN_DIGIT_IN_PERSIAN = re.compile(r"[؀-ۿ][^\n]{0,20}\d|\d[^\n]{0,20}[؀-ۿ]")


def persian_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if PERSIAN.match(c)) / len(letters) if letters else 0.0


@register
class RtlAnalyzer:
    name = "rtl"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        inventories = ctx.inventories()
        if not inventories:
            return AnalyzerResult.skipped(self.name, "no page inventory captured")
        result = AnalyzerResult(self.name, Status.PASS)
        for url, inv in inventories.items():
            texts = [e for e in inv.get("elements", []) if e.get("text")]
            persian = [e for e in texts if persian_ratio(e["text"]) > 0.5]
            if not persian:
                continue
            if len(persian) / max(len(texts), 1) > 0.5 and inv.get("dir") != "rtl":
                result.findings.append(Finding(
                    check_id="rtl.page_direction", category=Category.RTL, severity=Severity.SEV2,
                    title="صفحه‌ی فارسی بدون جهت راست‌به‌چپ",
                    detail=f"جهت سند «{inv.get('dir') or 'ltr'}» است در حالی که {len(persian)} از {len(texts)} متن فارسی‌اند.",
                    page_url=url, evidence={"key": "dir", "dir": inv.get("dir"), "lang": inv.get("lang")},
                    suggestion="روی تگ html ویژگی dir=\"rtl\" و lang=\"fa\" بگذارید (در Next.js در app/layout).",
                ))
            wrong = [e for e in persian if (e.get("styles") or {}).get("direction") == "ltr"
                     or (e.get("styles") or {}).get("textAlign") == "left"]
            if wrong and inv.get("dir") == "rtl":
                result.findings.append(Finding(
                    check_id="rtl.misaligned_text", category=Category.RTL, severity=Severity.SEV3,
                    title=f"{len(wrong)} متن فارسی چپ‌چین یا با جهت چپ‌به‌راست",
                    detail="، ".join(e["text"][:30] for e in wrong[:5]),
                    page_url=url, target=wrong[0], evidence={"key": "align", "elements": [e["selector"] for e in wrong[:20]]},
                    suggestion="به جای left/right از start/end (text-align: start، margin-inline) استفاده کنید.",
                ))
            digits = [e for e in persian if LATIN_DIGIT_IN_PERSIAN.search(e["text"])]
            if digits:
                result.findings.append(Finding(
                    check_id="rtl.latin_digits", category=Category.RTL, severity=Severity.INFO,
                    title=f"{len(digits)} متن فارسی با اعداد لاتین",
                    detail="، ".join(e["text"][:30] for e in digits[:5]),
                    page_url=url, target=digits[0], evidence={"key": "digits"},
                    suggestion="برای متن فارسی از Intl.NumberFormat('fa-IR') یا فونت با ارقام فارسی استفاده کنید (اگر سیاست محصول این است).",
                ))
        return result
