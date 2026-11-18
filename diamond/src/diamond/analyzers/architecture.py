"""Front-end architecture signals that explain *why* UI drift happens.

Runs on the repository (not the browser): hard-coded colours outside the
token/theme layer, oversized components, inline-style sprawl, and Next.js
App Router pages that opt the whole route into client rendering. These are
the code-level roots of the consistency findings.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ..model import AnalyzerResult, Category, Finding, Severity, SourceLocation, Status
from .base import AnalysisContext, register

CODE_EXT = {".tsx", ".jsx", ".ts", ".js", ".vue", ".svelte", ".css", ".scss", ".sass", ".less", ".astro"}
SKIP_DIRS = {"node_modules", ".next", "dist", "build", "out", ".git", ".turbo", "coverage", ".diamond", ".nuxt", ".svelte-kit"}
TOKEN_FILE = re.compile(r"(token|theme|palette|colors?|tailwind\.config|variables|design-system)", re.I)
HARD_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(\s*\d")
COMPONENT_EXT = {".tsx", ".jsx", ".vue", ".svelte"}
MAX_COMPONENT_LINES = 400


def iter_source_files(repo: Path):
    for path in repo.rglob("*"):
        if path.suffix in CODE_EXT and not any(part in SKIP_DIRS for part in path.relative_to(repo).parts):
            if path.is_file() and path.stat().st_size < 1_000_000:
                yield path


@register
class ArchitectureAnalyzer:
    name = "architecture"

    def run(self, ctx: AnalysisContext) -> AnalyzerResult:
        repo = ctx.repo
        if repo is None or not repo.is_dir():
            return AnalyzerResult.skipped(self.name, "no repository path given (--repo)")
        result = AnalyzerResult(self.name, Status.PASS)
        colors_per_file: Counter = Counter()
        big: list[tuple[str, int]] = []
        inline: Counter = Counter()
        client_routes: list[str] = []
        files = 0
        for path in iter_source_files(repo):
            files += 1
            rel = str(path.relative_to(repo))
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not TOKEN_FILE.search(rel):
                n = len(HARD_COLOR.findall(text))
                if n:
                    colors_per_file[rel] = n
            if path.suffix in COMPONENT_EXT:
                lines = text.count("\n") + 1
                if lines > MAX_COMPONENT_LINES:
                    big.append((rel, lines))
                inline[rel] = text.count("style={{")
            if path.name in ("page.tsx", "page.jsx", "layout.tsx", "layout.jsx") and "/app/" in f"/{rel}":
                head = text.lstrip()[:40]
                if head.startswith(("'use client'", '"use client"')):
                    client_routes.append(rel)

        total_colors = sum(colors_per_file.values())
        if total_colors:
            top = colors_per_file.most_common(10)
            result.findings.append(Finding(
                check_id="arch.hardcoded_colors", category=Category.ARCHITECTURE, severity=Severity.SEV3,
                title=f"{total_colors} رنگ هاردکد در {len(colors_per_file)} فایل، بیرون از لایه‌ی توکن/تم",
                detail="ریشه‌ی معمول رنگ‌های تقریباً یکسان در صفحات همین است.",
                sources=[SourceLocation(p, None, None, 0.9, "repo-scan") for p, _ in top],
                evidence={"key": "hardcoded_colors", "files": dict(top)},
                suggestion="رنگ‌ها را به متغیرهای CSS یا توکن‌های تم (Tailwind theme) منتقل کنید و lint بگذارید.",
            ))
        for rel, lines in sorted(big, key=lambda x: -x[1])[:10]:
            result.findings.append(Finding(
                check_id="arch.oversized_component", category=Category.ARCHITECTURE, severity=Severity.SEV3,
                title=f"کامپوننت بزرگ ({lines} خط)", detail=rel,
                sources=[SourceLocation(rel, 1, None, 1.0, "repo-scan")], evidence={"key": rel, "lines": lines},
                suggestion="بخش‌های نمایشی و منطق را به کامپوننت‌ها/هوک‌های کوچک‌تر بشکنید.",
            ))
        sprawl = {f: n for f, n in inline.items() if n > 10}
        if sprawl:
            result.findings.append(Finding(
                check_id="arch.inline_styles", category=Category.ARCHITECTURE, severity=Severity.INFO,
                title=f"استایل درون‌خطی زیاد در {len(sprawl)} فایل",
                sources=[SourceLocation(f, None, None, 1.0, "repo-scan") for f in sprawl],
                evidence={"key": "inline_styles", "files": sprawl},
            ))
        for rel in client_routes:
            result.findings.append(Finding(
                check_id="arch.client_route", category=Category.ARCHITECTURE, severity=Severity.SEV3,
                title="کل یک مسیر Next.js سمت کلاینت رندر می‌شود",
                detail=f"{rel} با 'use client' شروع می‌شود؛ همه‌ی زیردرخت آن به باندل کلاینت می‌رود.",
                sources=[SourceLocation(rel, 1, None, 1.0, "repo-scan")], evidence={"key": rel},
                suggestion="'use client' را به کوچک‌ترین کامپوننت تعاملی منتقل کنید.",
            ))
        result.metrics = {"files_scanned": files, "hardcoded_colors": total_colors}
        return result
