"""Website audit, as an anonymous visitor sees the site.

* ``web.links``      every internal page, link and asset answers (no 404/5xx)
* ``web.seo``        title, description, language, viewport, headings, canonical,
                     duplicate titles, robots.txt / sitemap.xml
* ``web.lighthouse`` Lighthouse performance / accessibility / best practices / SEO
                     scores (recorded, never judged without an approved threshold)

Runs against the same isolated instance as the UI runner (``[ui]`` start
command or test URL). Links and SEO need no browser.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

from ..context import RunContext, ToolMissing
from ..http import HttpClient, ManagedServer
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status, TestCase
from .base import Runner, register


@dataclass
class PageFacts:
    url: str
    status: int = 0
    content_type: str = ""
    title: str | None = None
    description: str | None = None
    lang: str = ""
    viewport: bool = False
    canonical: str = ""
    h1: int = 0
    noindex: bool = False
    links: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)


class _Extractor(HTMLParser):
    def __init__(self, facts: PageFacts) -> None:
        super().__init__(convert_charrefs=True)
        self.facts = facts
        self._in_title = False
        self._title: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        f = self.facts
        if tag == "html":
            f.lang = a.get("lang", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "description":
                f.description = a.get("content", "")
            elif name == "viewport":
                f.viewport = True
            elif name == "robots" and "noindex" in a.get("content", "").lower():
                f.noindex = True
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if "canonical" in rel:
                f.canonical = a.get("href", "")
            elif a.get("href") and any(r in rel for r in ("stylesheet", "icon", "manifest", "preload")):
                f.assets.append(a["href"])
        elif tag == "a" and a.get("href"):
            f.links.append(a["href"])
        elif tag in {"img", "script", "source", "video", "audio", "iframe"} and a.get("src"):
            f.assets.append(a["src"])
        elif tag == "h1":
            f.h1 += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            self.facts.title = "".join(self._title).strip()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title.append(data)


def parse_page(url: str, html: str) -> PageFacts:
    facts = PageFacts(url)
    _Extractor(facts).feed(html)
    return facts


def _internal(base: str, href: str) -> str | None:
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "data:", "#")):
        return None
    url = urldefrag(urljoin(base, href)).url
    b, u = urlsplit(base), urlsplit(url)
    return url if (b.scheme, b.netloc) == (u.scheme, u.netloc) else None


def crawl(client: HttpClient, base: str, start_paths: list[str], max_pages: int) -> tuple[list[PageFacts], dict[str, tuple[int, list[str]]]]:
    """BFS over same-origin HTML pages. Returns page facts and every checked URL → (status, referrers)."""
    pages: list[PageFacts] = []
    checked: dict[str, tuple[int, list[str]]] = {}
    queue = [urljoin(base + "/", p.lstrip("/")) for p in start_paths]
    seen: set[str] = set()
    prefix = client.prefix

    def fetch(url: str) -> tuple[int, str, str]:
        parts = urlsplit(url)
        path = (parts.path or "/")[len(prefix):] if prefix and parts.path.startswith(prefix) else (parts.path or "/")
        resp = client.request("GET", path + (f"?{parts.query}" if parts.query else ""))
        return resp.status, resp.header("content-type"), resp.text if "html" in resp.header("content-type") else ""

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        status, ctype, html = fetch(url)
        checked.setdefault(url, (status, []))
        facts = parse_page(url, html) if html else PageFacts(url)
        facts.status, facts.content_type = status, ctype
        if not html:
            continue
        pages.append(facts)
        for href in facts.links:
            target = _internal(url, href)
            if target:
                checked.setdefault(target, (0, []))[1].append(url)
                if target not in seen:
                    queue.append(target)
        for src in facts.assets:
            target = _internal(url, src)
            if target:
                checked.setdefault(target, (0, []))[1].append(url)
    for url, (status, refs) in list(checked.items()):
        if status == 0:
            checked[url] = (fetch(url)[0], refs)
    return pages, checked


def parse_lighthouse(report: dict) -> tuple[dict[str, int], list[tuple[str, str, float]]]:
    """Category scores (0–100) and the worst failing audits (id, title, score)."""
    scores = {k: round((v.get("score") or 0) * 100) for k, v in (report.get("categories") or {}).items()}
    failing = []
    for audit_id, audit in (report.get("audits") or {}).items():
        score = audit.get("score")
        if isinstance(score, (int, float)) and score < 0.5 and audit.get("scoreDisplayMode") in ("binary", "numeric", "metricSavings"):
            failing.append((audit_id, audit.get("title", audit_id), float(score)))
    failing.sort(key=lambda item: item[2])
    return scores, failing[:15]


@register
class WebAuditRunner(Runner):
    id = "web"
    title = "Website audit"
    dimension = Dimension.WEB_LINKS
    phase = 44

    def applicable(self, ctx: RunContext) -> str | None:
        if not ctx.config.web.enabled:
            return "web audit not enabled in contract"
        ui = ctx.config.ui
        if not (ui.base_url or ui.start_command):
            return "web audit uses the [ui] application; set ui.base_url or ui.start_command"
        missing = [r for r in ui.requires if not (ctx.root / r).exists()]
        return f"prerequisite missing: {', '.join(missing)} (run with --setup)" if missing else None

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        ui = ctx.config.ui
        if ui.base_url:
            return self._audit(ctx, ctx.interpolate(ui.base_url))
        server = ManagedServer(ctx, ui.start_command, ui.cwd, ui.env, [], ui.ready_path, ui.ready_timeout_s, "web-server.log")
        try:
            with server:
                return self._audit(ctx, server.base_url)
        except RuntimeError as exc:
            return [CheckResult.errored(self.id, "Website could not be started", Dimension.WEB_LINKS, str(exc))]

    # ------------------------------------------------------------------ #
    def _audit(self, ctx: RunContext, base: str) -> list[CheckResult]:
        spec = ctx.config.web
        client = HttpClient(base, timeout_s=20)
        pages, checked = crawl(client, base, ctx.config.ui.pages, spec.max_pages)
        results: list[CheckResult] = []
        results.append(self._links(pages, checked) if spec.links else CheckResult.skipped("web.links", "Links", Dimension.WEB_LINKS, "disabled"))
        results.append(self._seo(client, pages) if spec.seo else CheckResult.skipped("web.seo", "SEO", Dimension.WEB_SEO, "disabled"))
        results.append(self._lighthouse(ctx, base) if spec.lighthouse else CheckResult.skipped("web.lighthouse", "Lighthouse", Dimension.PERFORMANCE, "disabled"))
        return results

    @staticmethod
    def _links(pages: list[PageFacts], checked: dict[str, tuple[int, list[str]]]) -> CheckResult:
        result = CheckResult("web.links", "Every internal page, link and asset answers", Dimension.WEB_LINKS, Status.PASS)
        for url, (status, refs) in sorted(checked.items()):
            ok = 200 <= status < 400
            result.tests.append(TestCase(f"web.links::{urlsplit(url).path or '/'}", url, url, Status.PASS if ok else Status.FAIL))
            if ok:
                continue
            is_page = any(url == p.url for p in pages) or not urlsplit(url).path.rsplit("/", 1)[-1].count(".") or url.endswith(".html")
            result.findings.append(Finding(
                "web.links", Dimension.WEB_LINKS, Severity.SEV2,
                f"Broken {'link' if is_page else 'asset'} ({status or 'no response'}): {urlsplit(url).path}",
                detail="referenced from: " + ", ".join(sorted(set(urlsplit(r).path or "/" for r in refs))[:8]),
                location=Location(sorted(refs)[0] if refs else url), evidence={"key": f"broken:{urlsplit(url).path}"},
                suggestion="Fix the reference or restore the target; visitors and search engines both hit this.",
            ))
        result.metrics = {"pages": len(pages), "urls_checked": len(checked), "broken": len(result.findings)}
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(checked)} URL(s) from {len(pages)} page(s), {len(result.findings)} broken"
        return result

    @staticmethod
    def _seo(client: HttpClient, pages: list[PageFacts]) -> CheckResult:
        result = CheckResult("web.seo", "Pages are discoverable and well-formed for search and sharing", Dimension.WEB_SEO, Status.PASS)

        def add(sev: Severity, title: str, page: PageFacts, key: str, hint: str = "") -> None:
            result.findings.append(Finding("web.seo", Dimension.WEB_SEO, sev, title, location=Location(page.url),
                                           evidence={"key": f"{urlsplit(page.url).path}:{key}"}, suggestion=hint))

        titles = Counter(p.title for p in pages if p.title)
        for p in pages:
            if p.status >= 400:
                continue
            if not p.title:
                add(Severity.SEV2, "Missing or empty <title>", p, "title", "Every page needs a unique, descriptive title.")
            elif titles[p.title] > 1:
                add(Severity.SEV3, f"Duplicate title shared by {titles[p.title]} pages: {p.title[:60]}", p, "dup-title")
            if not p.description:
                add(Severity.SEV3, "Missing meta description", p, "description")
            if not p.lang:
                add(Severity.SEV3, "Missing <html lang>", p, "lang", "Screen readers and search engines need the page language (fa, en, …).")
            if not p.viewport:
                add(Severity.SEV2, "Missing viewport meta: the page will not render properly on phones", p, "viewport")
            if p.h1 == 0:
                add(Severity.SEV3, "No <h1> heading", p, "h1")
            elif p.h1 > 1:
                add(Severity.INFO, f"{p.h1} <h1> headings", p, "h1-multi")
            if p.noindex:
                add(Severity.INFO, "Page is marked noindex", p, "noindex")
            result.tests.append(TestCase(f"web.seo::{urlsplit(p.url).path or '/'}", p.url, "seo", Status.PASS))
        for path in ("/robots.txt", "/sitemap.xml"):
            status = client.request("GET", path).status
            result.metrics[path] = status
            if status != 200 and pages:
                add(Severity.INFO, f"{path} not found", pages[0], path)
        blocking = [f for f in result.findings if f.severity != Severity.INFO]
        result.status = Status.FAIL if blocking else Status.PASS
        result.summary = f"{len(pages)} page(s), {len(blocking)} issue(s), {len(result.findings) - len(blocking)} observation(s)"
        return result

    @staticmethod
    def _lighthouse(ctx: RunContext, base: str) -> CheckResult:
        cid, title = "web.lighthouse", "Lighthouse scores (performance, accessibility, best practices, SEO)"
        if not ctx.which("npx"):
            raise ToolMissing("npx (Node.js) for Lighthouse")
        out = ctx.out_dir / "lighthouse.json"
        url = urljoin(base + "/", ctx.config.ui.pages[0].lstrip("/"))
        env = ctx.build_env({})
        if os.environ.get("PQE_CHROMIUM_PATH"):
            env["CHROME_PATH"] = os.environ["PQE_CHROMIUM_PATH"]
        proc = ctx.run(["npx", "--yes", "lighthouse@12", url, "--output=json", f"--output-path={out}", "--quiet",
                        "--chrome-flags=--headless=new --no-sandbox --disable-gpu"], env=env, timeout_s=600)
        if not out.exists():
            return CheckResult.skipped(cid, title, Dimension.PERFORMANCE, "Lighthouse could not run here: " + proc.tail(4)[-400:])
        scores, failing = parse_lighthouse(json.loads(out.read_text(encoding="utf-8")))
        result = CheckResult(cid, title, Dimension.PERFORMANCE, Status.UNCALIBRATED, metrics={"scores": scores}, artifacts=["lighthouse.json"])
        for audit_id, audit_title, score in failing:
            result.findings.append(Finding(cid, Dimension.PERFORMANCE, Severity.SEV3, f"Lighthouse: {audit_title}",
                                           detail=f"score {score:.2f}", location=Location(url), evidence={"key": f"lh:{audit_id}"}))
        result.summary = ", ".join(f"{k} {v}" for k, v in scores.items()) + " — recorded; no approved threshold"
        return result
