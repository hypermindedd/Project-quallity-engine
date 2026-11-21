"""Browser capture: a headed Chromium driven by Playwright.

Why an engine-controlled browser instead of an extension (decision record):

* full Chrome DevTools Protocol access (LoAF, layout-shift sources, screenshots)
* the recorder is injected before any page script, so nothing is missed
* reproducible: the same viewport and flags every session
* microphone capture stays *outside* the page, so the app under test is untouched

The reviewer uses the window normally. Closing it (or ``stop()``) ends the session.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..config import DiamondConfig
from ..session import SessionStore

log = logging.getLogger("diamond.capture")

RECORDER_JS = (Path(__file__).parent / "recorder.js").read_text(encoding="utf-8")
SHOT_EVENTS = {"dwell", "click", "mark", "inventory"}
AXE_JS = """async () => {
  const r = await axe.run(document, {resultTypes: ['violations']});
  return r.violations.map(v => ({id: v.id, impact: v.impact, help: v.help, description: v.description,
    helpUrl: v.helpUrl, nodes: v.nodes.slice(0, 10).map(n => n.target.join(' '))}));
}"""


class BrowserUnavailable(RuntimeError):
    pass


_MAC_VISIBLE_FRAME_JXA = (
    'ObjC.import("AppKit"); var s=$.NSScreen.mainScreen; var v=s.visibleFrame, f=s.frame;'
    '[v.origin.x, f.size.height - v.origin.y - v.size.height, v.size.width, v.size.height].join(",")'
)


def screen_work_area() -> tuple[int, int, int, int] | None:
    """(x, y, width, height) of the usable screen area — menu bar and Dock excluded.

    A reviewer's window must fit on the screen. When the page is told it is taller
    than the window (a fixed emulated viewport), its bottom part is drawn outside
    the window and can never be scrolled into view.
    """
    override = os.environ.get("DIAMOND_WINDOW", "")  # "WIDTHxHEIGHT" or "X,Y,WIDTHxHEIGHT"
    if override:
        try:
            *pos, size = override.split(",")
            w, h = (int(v) for v in size.lower().split("x"))
            x, y = (int(v) for v in pos) if len(pos) == 2 else (0, 0)
            return x, y, w, h
        except ValueError:
            log.warning("ignoring malformed DIAMOND_WINDOW=%r (expected 1280x800)", override)
    if platform.system() != "Darwin":
        return None
    try:
        out = subprocess.run(["osascript", "-l", "JavaScript", "-e", _MAC_VISIBLE_FRAME_JXA],
                             capture_output=True, text=True, timeout=5, check=True).stdout
        x, y, w, h = (int(float(v)) for v in out.strip().split(","))
        return (x, y, w, h) if w > 200 and h > 200 else None
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("screen size unavailable: %s", exc)
        return None


def window_launch_args(area: tuple[int, int, int, int] | None) -> list[str]:
    if area is None:
        return ["--start-maximized"]
    x, y, w, h = area
    return [f"--window-position={x},{y}", f"--window-size={w},{h}"]


class BrowserCapture:
    """Runs one capture session. Thread-safe ``stop()``; ``run()`` blocks."""

    def __init__(self, cfg: DiamondConfig, store: SessionStore, headless: bool = False) -> None:
        self.cfg = cfg
        self.store = store
        self.headless = headless
        self._stop = threading.Event()
        self._shots: queue.Queue[tuple[str, float]] = queue.Queue()
        self._axe_pending = threading.Event()
        self.page: Any = None  # exposed for scripted sessions/tests
        self.ready = threading.Event()
        self._cdp: dict[int, Any] = {}

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------- handlers
    def _on_batch(self, batch: list[dict[str, Any]]) -> None:
        self.store.append_events(batch)
        for event in batch:
            if event.get("type") in SHOT_EVENTS:
                self._shots.put((event["type"], float(event["ts"])))
            if event.get("type") == "inventory" and self.cfg.axe_script:
                self._axe_pending.set()

    def _engine_event(self, type_: str, **data: Any) -> None:
        self.store.append_events([{"type": type_, "ts": time.time() * 1000, **data}])

    def _wire_page(self, page: Any) -> None:
        page.on("console", lambda m: m.type == "error" and self._engine_event(
            "console_error", url=page.url, text=m.text[:500], location=m.location))
        page.on("pageerror", lambda e: self._engine_event("page_error", url=page.url, text=str(e)[:800]))
        page.on("requestfailed", lambda r: self._engine_event(
            "request_failed", url=page.url, request=r.url[:300], failure=(r.failure or "")[:200], method=r.method))

        def on_response(resp: Any) -> None:
            if resp.status >= 400:
                self._engine_event("http_error", url=page.url, request=resp.url[:300], status=resp.status,
                                   method=resp.request.method)
        page.on("response", on_response)
        page.on("framenavigated", lambda f: f == page.main_frame and self._engine_event("navigation", url=f.url))

    def _take_screenshots(self) -> None:
        while not self._shots.empty() and self.page is not None:
            kind, ts = self._shots.get_nowait()
            path = self.store.shots / f"{int(ts)}_{kind}.jpg"
            if path.exists():
                continue
            started = time.time() * 1000
            try:
                self._screenshot(path)
            except Exception as exc:  # page navigating / closed: a missing shot is not fatal
                log.debug("screenshot skipped: %s", exc)
            # Frames the engine itself stalled must not be reported as the app's lag.
            self._engine_event("engine_busy", what="screenshot", url=getattr(self.page, "url", ""),
                               start=started, end=time.time() * 1000)

    def _screenshot(self, path: Path) -> None:
        if self.headless:
            self.page.screenshot(path=str(path), type="jpeg", quality=60, animations="allow")
            return
        # In the reviewer's real window, Playwright's screenshot re-applies device-metric
        # emulation, which makes the visible page jump. A plain CDP capture of what is on
        # screen changes nothing.
        import base64

        key = id(self.page)
        if key not in self._cdp:
            self._cdp[key] = self.page.context.new_cdp_session(self.page)
        data = self._cdp[key].send("Page.captureScreenshot", {"format": "jpeg", "quality": 60})["data"]
        path.write_bytes(base64.b64decode(data))

    def _run_axe(self) -> None:
        """Run axe-core on the current page after each inventory (only if configured)."""
        if not self._axe_pending.is_set() or self.page is None:
            return
        self._axe_pending.clear()
        try:
            if not self.page.evaluate("typeof window.axe !== 'undefined'"):
                self.page.add_script_tag(path=self.cfg.axe_script)
            violations = self.page.evaluate(AXE_JS)
            self._engine_event("axe", url=self.page.url, violations=violations)
        except Exception as exc:  # axe failure must not break the recording
            log.debug("axe skipped: %s", exc)

    def _tick(self) -> None:
        self._take_screenshots()
        self._run_axe()

    # ------------------------------------------------------------------ run
    def run(self, url: str, script: Any = None) -> None:
        """Open ``url`` and record until the window closes or ``stop()``.

        ``script(page)`` — optional callable used for scripted sessions (tests,
        replays). When given, the session ends as soon as it returns.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - dependency of the package
            raise BrowserUnavailable("Playwright is not installed: pip install playwright") from exc

        recorder_cfg = {
            "dwellMs": self.cfg.fusion.dwell_ms,
            "sourceAttributes": self.cfg.source_attributes,
        }
        init = f"window.__diamondConfig = {json.dumps(recorder_cfg)};\n{RECORDER_JS}"
        width, height = self.cfg.viewport

        with sync_playwright() as pw:
            launch: dict[str, Any] = {"headless": self.headless}
            if self.cfg.browser_channel:
                launch["channel"] = self.cfg.browser_channel
            if not self.headless:
                # The window opens at its final size, so nothing resizes after the page paints.
                launch["args"] = window_launch_args(screen_work_area())
            try:
                browser = pw.chromium.launch(**launch)
            except Exception as exc:
                raise BrowserUnavailable(
                    f"could not start Chromium ({exc}). Run: python -m playwright install chromium") from exc
            if self.headless:
                # Unattended passes: a fixed, reproducible viewport.
                context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
            else:
                # A reviewer's window: the page uses the real window size and the screen's own
                # pixel density. A fixed emulated viewport larger than the window hides the
                # bottom of the page (unscrollable), and forcing DPR 1 on a Retina screen
                # re-renders the page and blurs it.
                context = browser.new_context(no_viewport=True)
            context.expose_function("__diamondEmit", self._on_batch)
            context.add_init_script(init)
            context.on("page", self._wire_page)
            self.page = context.new_page()
            self.page.goto(url, wait_until="domcontentloaded")
            actual = [width, height]
            if not self.headless:
                try:
                    actual = self.page.evaluate("[innerWidth, innerHeight, devicePixelRatio]")
                except Exception as exc:  # navigation in flight; the inventory records it later
                    log.debug("viewport read skipped: %s", exc)
            self.store.update_meta(browser_version=browser.version, viewport=actual)
            self.ready.set()
            try:
                if script is not None:
                    script(self.page)
                    self.page.wait_for_timeout(1500)  # let the last batches and click effects arrive
                    self._tick()
                else:
                    while not self._stop.is_set() and context.pages:
                        self.page = context.pages[-1]
                        self._tick()
                        try:
                            self.page.wait_for_timeout(200)
                        except Exception:  # the reviewer closed this tab/window
                            time.sleep(0.2)
                # Final inventory of the open page, so the last state is analysed.
                try:
                    if context.pages:
                        self.page = context.pages[-1]
                        self.page.evaluate("window.__diamondInventory && window.__diamondInventory()")
                        self.page.wait_for_timeout(600)
                        self._tick()
                except Exception as exc:
                    log.debug("final inventory skipped: %s", exc)
            finally:
                self.store.update_meta(ended_ms=time.time() * 1000)
                for closer in (context.close, browser.close):
                    try:
                        closer()
                    except Exception as exc:  # already closed by the reviewer
                        log.debug("close: %s", exc)
