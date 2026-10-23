"""User-interface verification in a real browser (Playwright / Chromium).

For every reachable page and every configured viewport the engine:

* crawls same-origin links (bounded) and records console errors, uncaught
  exceptions and failed requests;
* clicks every visible interactive element (bounded, destructive labels
  excluded) and attributes any new error to the element that caused it;
* detects horizontal overflow, broken images and wrong text direction;
* runs axe-core accessibility rules when an axe script is available;
* compares full-page screenshots with approved baselines;
* optionally asks a vision model for an *advisory* design review.

The UI must run against an isolated instance (``ui.start_command``) or an
explicit test URL. It is never pointed at production by default.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from ..context import RunContext, ToolMissing
from ..http import ManagedServer
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status, TestCase
from .base import Runner, register

CLICKABLE = (
    "button:not([disabled]), a[href], [role=button]:not([aria-disabled=true]), input[type=submit], "
    "input[type=button], summary, [role=tab], [role=menuitem], [role=switch], [role=checkbox]"
)

OVERFLOW_JS = """
() => {
  const vw = window.innerWidth;
  const doc = document.documentElement;
  const offenders = [];
  if (doc.scrollWidth > vw + 1) {
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && (r.right > vw + 1 || r.left < -1)) {
        const id = el.id ? '#' + el.id : '';
        const cls = (el.className && typeof el.className === 'string') ? '.' + el.className.trim().split(/\\s+/).slice(0,2).join('.') : '';
        offenders.push(el.tagName.toLowerCase() + id + cls);
        if (offenders.length >= 5) break;
      }
    }
  }
  const broken = [...document.images].filter(i => i.complete && i.naturalWidth === 0).map(i => i.currentSrc || i.src).slice(0, 10);
  return {scrollWidth: doc.scrollWidth, viewport: vw, offenders, broken,
          dir: (doc.getAttribute('dir') || getComputedStyle(document.body).direction || '').toLowerCase(),
          lang: doc.getAttribute('lang') || ''};
}
"""

DESCRIBE_JS = """
(el) => {
  const t = (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('title') || '').trim().replace(/\\s+/g,' ');
  return el.tagName.toLowerCase() + (t ? ' "' + t.slice(0, 60) + '"' : '') + (el.id ? '#' + el.id : '');
}
"""

_AXE_IMPACT = {"critical": Severity.SEV1, "serious": Severity.SEV2, "moderate": Severity.SEV3, "minor": Severity.INFO}


_VIEWPORT_SUFFIX = re.compile(r"\s*(?:\((?:{v})\)|at (?:{v}))")


def merge_viewports(findings: list[Finding], viewports: list[str]) -> list[Finding]:
    """One finding per problem: the same issue seen at several screen sizes is merged.

    Issues that are genuinely size-specific (overflow at mobile only) stay separate
    because their titles differ by more than the viewport name.
    """
    pattern = re.compile(_VIEWPORT_SUFFIX.pattern.format(v="|".join(map(re.escape, viewports))))
    merged: dict[tuple[str, str, str, str], Finding] = {}
    for f in findings:
        base_title = pattern.sub("", f.title)
        if "overflow" in f.title.lower():
            base_title = f.title  # size-specific by nature
        loc = f.location.render() if f.location else ""
        key = (f.check_id, base_title, loc, f.detail[:200])
        seen_at = [v for v in viewports if re.search(rf"\b{re.escape(v)}\b", f.title) or f.evidence.get("viewport") == v]
        if key in merged:
            merged[key].evidence.setdefault("viewports", [])
            merged[key].evidence["viewports"] = sorted(set(merged[key].evidence["viewports"]) | set(seen_at))
            continue
        f.title = base_title
        f.evidence["key"] = f"{base_title}:{loc}:{f.detail[:80]}"
        if seen_at:
            f.evidence["viewports"] = seen_at
        merged[key] = f
    return list(merged.values())


def launch_options() -> dict[str, Any]:
    """Use Playwright's bundled Chromium, or a system/portable one when
    ``PQE_CHROMIUM_PATH`` is set (e.g. hardened CI images, offline machines)."""
    path = os.environ.get("PQE_CHROMIUM_PATH", "")
    if not path:
        return {}
    args = os.environ.get("PQE_CHROMIUM_ARGS", "--no-sandbox").split()
    return {"executable_path": path, "args": args}


def same_origin_links(base: str, hrefs: list[str]) -> list[str]:
    """Normalise anchors to absolute same-origin URLs without fragments."""
    origin = urlsplit(base)
    found: list[str] = []
    for href in hrefs:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        url = urldefrag(urljoin(base, href)).url
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc) == (origin.scheme, origin.netloc) and url not in found:
            found.append(url)
    return found


def slug(url: str) -> str:
    path = urlsplit(url).path.strip("/") or "index"
    query = urlsplit(url).query
    raw = path + ("_" + query if query else "")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)[:80]


def image_diff_ratio(baseline: Path, candidate: Path) -> tuple[float, str]:
    """Fraction of pixels that differ visibly (any channel > 16)."""
    from PIL import Image, ImageChops  # optional dependency

    with Image.open(baseline) as a_img, Image.open(candidate) as b_img:
        a, b = a_img.convert("RGB"), b_img.convert("RGB")
        if a.size != b.size:
            return 1.0, f"size changed {a.size} → {b.size}"
        diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 16 else 0)
        changed = sum(1 for v in diff.getdata() if v)
        return changed / (a.size[0] * a.size[1]), ""


@dataclass
class PageReport:
    url: str
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)


class _Collector:
    """Collects browser-side problems for the page currently loaded."""

    def __init__(self, ignore: list[str]) -> None:
        self.console: list[str] = []
        self.errors: list[str] = []
        self.failed: list[str] = []
        self.ignore = [re.compile(p) for p in ignore]

    def _keep(self, text: str) -> bool:
        return not any(p.search(text) for p in self.ignore)

    def attach(self, page: Any, origin: str) -> None:
        page.on("console", lambda m: m.type == "error" and self._keep(m.text) and self.console.append(m.text[:300]))
        page.on("pageerror", lambda e: self._keep(str(e)) and self.errors.append(str(e)[:300]))
        page.on("requestfailed", lambda r: r.url.startswith(origin) and self.failed.append(f"{r.method} {r.url} ({r.failure})"[:300]))
        page.on("response", lambda r: r.url.startswith(origin) and r.status >= 400 and self.failed.append(f"{r.request.method} {r.url} → {r.status}"[:300]))
        page.on("dialog", lambda d: d.dismiss())

    def snapshot(self) -> tuple[int, int, int]:
        return len(self.console), len(self.errors), len(self.failed)


@register
class UiRunner(Runner):
    id = "ui"
    title = "Browser UI verification"
    dimension = Dimension.UI_INTERACTION
    phase = 45
    _skip_words: list[str] = []

    def applicable(self, ctx: RunContext) -> str | None:
        spec = ctx.config.ui
        if not spec.enabled:
            return "ui verification not enabled in contract"
        if not (spec.base_url or spec.start_command):
            return "ui requires base_url or start_command"
        missing = [r for r in spec.requires if not (ctx.root / r).exists()]
        return f"prerequisite missing: {', '.join(missing)} (run with --setup)" if missing else None

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ToolMissing("playwright (pip install 'pqe[ui]' && playwright install chromium)") from exc
        spec = ctx.config.ui
        for name in spec.generate_secrets:
            ctx.secret(name)
        if spec.base_url:
            return self._drive(ctx, ctx.interpolate(spec.base_url), sync_playwright, PlaywrightError)
        server = ManagedServer(ctx, spec.start_command, spec.cwd, spec.env, [], spec.ready_path, spec.ready_timeout_s, "ui-server.log")
        try:
            with server:
                return self._drive(ctx, server.base_url, sync_playwright, PlaywrightError)
        except ToolMissing:
            raise  # a missing browser is a SKIP, not an application failure
        except RuntimeError as exc:
            return [CheckResult.errored(self.id, "UI application could not be started", self.dimension, str(exc))]

    # ------------------------------------------------------------------ #
    def _drive(self, ctx: RunContext, base: str, sync_playwright: Any, PlaywrightError: type[Exception]) -> list[CheckResult]:
        spec = ctx.config.ui
        self._skip_words = spec.click_skip_text
        out = ctx.out_dir / "ui"
        out.mkdir(parents=True, exist_ok=True)
        interaction = CheckResult("ui.interaction", "Every page loads and every control works without errors", Dimension.UI_INTERACTION, Status.PASS)
        responsive = CheckResult("ui.responsive", "Layout fits every viewport (no overflow, no broken images)", Dimension.UI_RESPONSIVE, Status.PASS)
        visual = CheckResult("ui.visual", "Screens match approved visual baselines", Dimension.UI_VISUAL, Status.PASS)
        a11y = CheckResult("ui.accessibility", "Accessibility rules (axe-core)", Dimension.ACCESSIBILITY, Status.PASS)
        axe_source = self._axe_source(ctx)
        start_urls = [urljoin(base + "/", p.lstrip("/")) for p in spec.pages]
        discovered: list[str] = []
        shots: list[tuple[str, str, Path]] = []

        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(**launch_options())
            except PlaywrightError as exc:
                raise ToolMissing(f"chromium for playwright ({str(exc).splitlines()[0]})") from exc
            session_state: Any = None  # signed-in browser state, reused by every later viewport
            try:
                for v_index, viewport in enumerate(spec.viewports):
                    context = browser.new_context(viewport={"width": viewport.width, "height": viewport.height},
                                                  storage_state=session_state)
                    page = context.new_page()
                    collector = _Collector(spec.ignore_console)
                    collector.attach(page, base)
                    if spec.actions and session_state is None:
                        if not self._bootstrap(ctx, interaction, page, collector, base, viewport.name, PlaywrightError):
                            context.close()
                            break  # without the documented sign-in no viewport can reach the app
                        session_state = context.storage_state()
                    queue = list(start_urls) if v_index == 0 else list(discovered)
                    visited: list[str] = []
                    while queue and len(visited) < spec.max_pages:
                        url = queue.pop(0)
                        if url in visited:
                            continue
                        visited.append(url)
                        before = collector.snapshot()
                        try:
                            page.goto(url, wait_until="networkidle", timeout=30_000)
                        except PlaywrightError as exc:
                            interaction.findings.append(Finding("ui.interaction", Dimension.UI_INTERACTION, Severity.SEV1, f"Page failed to load ({viewport.name})",
                                                                detail=str(exc)[:500], location=Location(url), evidence={"key": f"load:{url}"}))
                            continue
                        self._record_errors(interaction, collector, before, url, f"loading page ({viewport.name})")
                        if v_index == 0:
                            hrefs = page.eval_on_selector_all("a[href]", "els => els.map(e => e.getAttribute('href'))")
                            for link in same_origin_links(url, hrefs):
                                if link not in visited and link not in queue:
                                    queue.append(link)
                        self._layout(responsive, page, url, viewport.name, spec.expect_dir)
                        shot = out / viewport.name / f"{slug(url)}.png"
                        shot.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(shot), full_page=True)
                        shots.append((viewport.name, url, shot))
                        if axe_source:
                            self._axe(a11y, page, url, viewport.name, axe_source)
                        if v_index == 0:
                            self._click_everything(interaction, page, collector, url, spec.max_clicks_per_page, PlaywrightError)
                    if v_index == 0:
                        discovered = visited
                    interaction.tests.append(TestCase(f"ui.interaction::{viewport.name}", base, f"{len(visited)} page(s) at {viewport.name}", Status.PASS))
                    context.close()
                journey_results = self._journeys(ctx, browser, base, session_state, PlaywrightError) if spec.journeys else []
            finally:
                browser.close()

        self._compare(ctx, visual, shots)
        results = [interaction, responsive, visual, *journey_results]
        if axe_source:
            results.append(a11y)
        else:
            results.append(CheckResult.skipped("ui.accessibility", a11y.title, Dimension.ACCESSIBILITY,
                                               "axe-core not found (set ui.axe_script or install axe-core in node_modules)"))
        if spec.llm_review:
            from ..analysis.llm import review_screens

            results.append(review_screens(ctx, shots))
        for r in (interaction, responsive, a11y):
            if r.status == Status.PASS and r.findings and any(f.severity != Severity.INFO for f in r.findings):
                r.status = Status.FAIL
            r.summary = r.summary or (f"{len(r.findings)} issue(s)" if r.findings else "no issues")
        interaction.metrics = {"pages": len(discovered), "viewports": len(spec.viewports)}
        for r in results:
            r.findings = merge_viewports(r.findings, [v.name for v in spec.viewports])
            if r in (interaction, responsive, a11y):
                r.summary = f"{len(r.findings)} issue(s)" if r.findings else "no issues"
        return results

    # ----------------------------------------------------------- helpers
    @staticmethod
    def _axe_source(ctx: RunContext) -> str:
        candidates = [ctx.config.ui.axe_script] if ctx.config.ui.axe_script else []
        candidates += [os.path.join(ctx.config.ui.cwd, "node_modules/axe-core/axe.min.js"), "node_modules/axe-core/axe.min.js"]
        for rel in candidates:
            path = ctx.root / rel
            if rel and path.exists():
                return path.read_text(encoding="utf-8")
        from ..tools import npm_file

        cached = npm_file("axe-core@4", "axe-core/axe.min.js")
        return cached.read_text(encoding="utf-8") if cached else ""

    @staticmethod
    def perform(page: Any, step: Any, base: str, value: str) -> None:
        """Execute one interaction or assertion. Raises AssertionError on a failed expectation."""
        action, sel = step.action, step.selector
        if action == "goto":
            page.goto(urljoin(base + "/", value.lstrip("/")), wait_until="networkidle", timeout=60_000)
        elif action == "click":
            page.click(sel, timeout=10_000)
        elif action == "fill":
            page.fill(sel, value, timeout=10_000)
        elif action == "select":
            page.select_option(sel, value, timeout=10_000)
        elif action == "press":
            page.press(sel, value or "Enter", timeout=10_000)
        elif action == "wait_for":
            page.wait_for_selector(sel, timeout=int(value or 20_000))
        elif action == "wait_ms":
            page.wait_for_timeout(int(value or 500))
        elif action == "expect_text":
            text = page.locator(sel or "body").first.inner_text(timeout=5_000)  # waits for the element
            if value not in text:
                raise AssertionError(f"text {value!r} not found in {sel or 'page'}")
        elif action == "expect_visible":
            try:
                page.wait_for_selector(sel, state="visible", timeout=int(value or 8_000))
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"{sel} is not visible") from exc
        elif action == "expect_hidden":
            if page.locator(sel).count() and page.locator(sel).first.is_visible():
                raise AssertionError(f"{sel} should not be visible")
        elif action == "expect_url":
            if value not in page.url:
                raise AssertionError(f"url {page.url!r} does not contain {value!r}")
        elif action == "expect_title":
            if value not in page.title():
                raise AssertionError(f"title {page.title()!r} does not contain {value!r}")

    def _journeys(self, ctx: RunContext, browser: Any, base: str, state: Any, PlaywrightError: type[Exception]) -> list[CheckResult]:
        spec = ctx.config.ui
        desktop = spec.viewports[-1]
        results = []
        for journey in spec.journeys:
            cid = f"ui.journey.{journey.id}"
            title = ("[proposed] " if journey.proposed else "") + journey.title
            result = CheckResult(cid, title, Dimension.E2E_FLOW, Status.PASS)
            severity = Severity.SEV3 if journey.proposed else journey.severity
            context = browser.new_context(viewport={"width": desktop.width, "height": desktop.height}, storage_state=state)
            page = context.new_page()
            collector = _Collector(spec.ignore_console)
            collector.attach(page, base)
            try:
                page.goto(base + "/", wait_until="networkidle", timeout=60_000)
                for index, step in enumerate(journey.steps, 1):
                    value = ctx.interpolate(step.value)
                    before = collector.snapshot()
                    label = f"{step.action} {step.selector or value}".strip()
                    try:
                        self.perform(page, step, base, value)
                    except (AssertionError, PlaywrightError) as exc:
                        message = str(exc).splitlines()[0][:300]
                        result.tests.append(TestCase(f"{cid}::{index:02d}", page.url, label, Status.FAIL, message=message))
                        shot = ctx.out_dir / "ui" / "journeys" / f"{journey.id}-step{index}.png"
                        shot.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(shot), full_page=True)
                        result.findings.append(Finding(
                            cid, Dimension.E2E_FLOW, severity, f"Journey '{journey.id}' failed at step {index}: {label}",
                            detail=ctx.redact(message), location=Location(page.url),
                            evidence={"key": f"journey:{journey.id}:{index}", "screenshot": ctx.rel(shot), "source": journey.source},
                        ))
                        break
                    result.tests.append(TestCase(f"{cid}::{index:02d}", page.url, label, Status.PASS))
                    for text in collector.errors[before[1]:]:
                        result.findings.append(Finding(cid, Dimension.E2E_FLOW, severity, f"Uncaught exception during journey '{journey.id}' step {index}",
                                                       detail=text, location=Location(page.url), evidence={"key": f"journey:{journey.id}:{index}:exc"}))
            finally:
                context.close()
            result.status = Status.FAIL if result.findings else Status.PASS
            result.summary = f"{len(result.tests)}/{len(journey.steps)} step(s)" + (" passed" if not result.findings else ", stopped at first failure")
            results.append(result)
        return results

    @staticmethod
    def _bootstrap(ctx: RunContext, result: CheckResult, page: Any, c: _Collector, base: str, viewport: str,
                   PlaywrightError: type[Exception]) -> bool:
        """Run the contract's bootstrap actions (e.g. create a test account and sign in)."""
        run_vars = {"PQE_RUN_ID": ctx.out_dir.name.replace("-", "").replace("T", "")[-10:].lower() or "run", "PQE_VIEWPORT": viewport}
        for index, step in enumerate(ctx.config.ui.actions, 1):
            value = ctx.interpolate(step.value, run_vars)
            before = c.snapshot()
            try:
                UiRunner.perform(page, step, base, value)
            except (AssertionError, PlaywrightError) as exc:
                result.findings.append(Finding(
                    "ui.interaction", Dimension.UI_INTERACTION, Severity.SEV1,
                    f"Bootstrap step {index} ({step.action} {step.selector or value}) failed at {viewport}",
                    detail=ctx.redact(str(exc).splitlines()[0])[:400], location=Location(page.url),
                    evidence={"key": f"bootstrap:{index}:{viewport}"},
                    suggestion="The documented sign-in/onboarding path does not work, or the contract's selectors are outdated.",
                ))
                return False
            UiRunner._record_errors(result, c, before, page.url, f"bootstrap step {index} ({step.action})")
        result.tests.append(TestCase(f"ui.bootstrap::{viewport}", base, f"bootstrap at {viewport}", Status.PASS))
        return True

    @staticmethod
    def _record_errors(result: CheckResult, c: _Collector, before: tuple[int, int, int], url: str, action: str) -> None:
        for text in c.errors[before[1]:]:
            result.findings.append(Finding("ui.interaction", Dimension.UI_INTERACTION, Severity.SEV1, f"Uncaught exception while {action}",
                                           detail=text, location=Location(url), evidence={"key": f"{url}:{action}:exc:{text[:80]}"}))
        for text in c.console[before[0]:]:
            result.findings.append(Finding("ui.interaction", Dimension.UI_INTERACTION, Severity.SEV2, f"Console error while {action}",
                                           detail=text, location=Location(url), evidence={"key": f"{url}:{action}:console:{text[:80]}"}))
        for text in c.failed[before[2]:]:
            result.findings.append(Finding("ui.interaction", Dimension.UI_INTERACTION, Severity.SEV2, f"Failed request while {action}",
                                           detail=text, location=Location(url), evidence={"key": f"{url}:{action}:req:{text[:80]}"}))

    def _click_everything(self, result: CheckResult, page: Any, c: _Collector, url: str, limit: int, PlaywrightError: type[Exception]) -> None:
        skip = [s.lower() for s in self._skip_words]
        handles = page.query_selector_all(CLICKABLE)
        clicked = 0
        for index in range(len(handles)):
            if clicked >= limit:
                break
            handles = page.query_selector_all(CLICKABLE) if index else handles
            if index >= len(handles):
                break
            el = handles[index]
            try:
                if not el.is_visible():
                    continue
                label = el.evaluate(DESCRIBE_JS)
                if any(word in label.lower() for word in skip):
                    continue
                href = el.get_attribute("href") or ""
                if href and not href.startswith(("#", "/")) and urlsplit(href).netloc and urlsplit(href).netloc != urlsplit(url).netloc:
                    continue  # never leave the application under test
                before = c.snapshot()
                el.click(timeout=3000, no_wait_after=False)
                page.wait_for_timeout(400)
                clicked += 1
                self._record_errors(result, c, before, url, f"clicking {label}")
                result.tests.append(TestCase(f"ui.click::{slug(url)}::{index}", url, f"click {label}", Status.PASS if c.snapshot() == before else Status.FAIL))
            except PlaywrightError as exc:
                message = str(exc).splitlines()[0]
                if "not attached" in message or "not visible" in message or "intercepts pointer" in message:
                    continue  # element moved or is covered; not a defect by itself
                result.findings.append(Finding("ui.interaction", Dimension.UI_INTERACTION, Severity.SEV3, "Control could not be clicked",
                                               detail=message[:300], location=Location(url), evidence={"key": f"{url}:noclick:{index}"}))
            finally:
                if page.url.split("#")[0] != url.split("#")[0]:
                    try:
                        page.goto(url, wait_until="networkidle", timeout=30_000)
                    except PlaywrightError:
                        break
        result.metrics.setdefault("clicks", 0)
        result.metrics["clicks"] += clicked

    @staticmethod
    def _layout(result: CheckResult, page: Any, url: str, viewport: str, expect_dir: str) -> None:
        data = page.evaluate(OVERFLOW_JS)
        if data["scrollWidth"] > data["viewport"] + 1:
            result.findings.append(Finding("ui.responsive", Dimension.UI_RESPONSIVE, Severity.SEV2,
                                           f"Horizontal overflow at {viewport} ({data['scrollWidth']}px > {data['viewport']}px)",
                                           detail="offending elements: " + ", ".join(data["offenders"]), location=Location(url),
                                           evidence={"key": f"{url}:{viewport}:overflow"}))
        for src in data["broken"]:
            result.findings.append(Finding("ui.responsive", Dimension.UI_RESPONSIVE, Severity.SEV2, "Broken image", detail=src,
                                           location=Location(url), evidence={"key": f"{url}:img:{src}"}))
        if expect_dir and data["dir"] != expect_dir:
            result.findings.append(Finding("ui.responsive", Dimension.UI_RESPONSIVE, Severity.SEV2,
                                           f"Text direction is '{data['dir'] or 'unset'}', expected '{expect_dir}'", location=Location(url),
                                           evidence={"key": f"{url}:dir"}))
        result.tests.append(TestCase(f"ui.layout::{viewport}::{slug(url)}", url, f"layout {viewport}", Status.PASS))

    @staticmethod
    def _axe(result: CheckResult, page: Any, url: str, viewport: str, source: str) -> None:
        page.add_script_tag(content=source)
        report = page.evaluate("async () => await axe.run(document, {resultTypes: ['violations']})")
        for violation in report.get("violations", []):
            severity = _AXE_IMPACT.get(violation.get("impact") or "minor", Severity.INFO)
            targets = [", ".join(n.get("target", [])) for n in violation.get("nodes", [])][:5]
            result.findings.append(Finding("ui.accessibility", Dimension.ACCESSIBILITY, severity,
                                           f"{violation.get('id')}: {violation.get('help')}", detail="; ".join(targets),
                                           location=Location(url), evidence={"key": f"{url}:{violation.get('id')}", "viewport": viewport},
                                           suggestion=violation.get("helpUrl", "")))

    @staticmethod
    def _compare(ctx: RunContext, result: CheckResult, shots: list[tuple[str, str, Path]]) -> None:
        threshold = ctx.config.ui.visual_diff_threshold
        base_dir = ctx.config.qa_dir / "baselines" / "ui"
        try:
            import PIL  # noqa: F401
        except ImportError:
            result.status = Status.SKIPPED
            result.summary = "Pillow not installed; screenshots captured but not compared"
            return
        missing = 0
        diffs: dict[str, float] = {}
        for viewport, url, shot in shots:
            baseline = base_dir / viewport / shot.name
            if not baseline.exists():
                missing += 1
                continue
            ratio, note = image_diff_ratio(baseline, shot)
            key = f"{viewport}/{shot.name}"
            diffs[key] = round(ratio, 5)
            if threshold is not None and ratio > threshold:
                result.findings.append(Finding("ui.visual", Dimension.UI_VISUAL, Severity.SEV2,
                                               f"Visual change {ratio:.2%} at {viewport} exceeds {threshold:.2%}", detail=note,
                                               location=Location(url), evidence={"key": f"{url}:{viewport}:visual"},
                                               suggestion="Review the screenshot; if the change is intended, approve it as the new baseline."))
        result.metrics = {"screens": len(shots), "without_baseline": missing, "diff_ratio": diffs}
        result.artifacts.append("ui/")
        if result.findings:
            result.status = Status.FAIL
        elif threshold is None or missing == len(shots):
            result.status = Status.UNCALIBRATED
        result.summary = f"{len(shots)} screen(s); {missing} without approved baseline" + ("" if threshold is not None else "; no approved diff threshold")

