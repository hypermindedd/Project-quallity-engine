"""The reviewer's real window: the whole page must be reachable and nothing may jump.

Regression for: the bottom of the reviewed site was invisible and unscrollable
(the page was emulated at 1440x900 inside a smaller window), and the page jumped
on every screenshot (Playwright re-applied device-metric emulation).
Needs a display (macOS, or Linux with xvfb-run); skipped otherwise.
"""

import os
import platform

import pytest

from diamond.capture.browser import BrowserCapture
from diamond.config import load_config
from diamond.session import SessionStore

pytestmark = pytest.mark.skipif(platform.system() != "Darwin" and not os.environ.get("DISPLAY"),
                                reason="headed browser needs a display")


@pytest.fixture()
def headed(site_url, tmp_path, monkeypatch):
    monkeypatch.setenv("DIAMOND_WINDOW", "0,0,1000x620")
    cfg = load_config(None)
    cfg.browser_channel = os.environ.get("DIAMOND_BROWSER_CHANNEL", "")
    store = SessionStore.create(tmp_path, {"url": f"{site_url}/tall.html"})
    return BrowserCapture(cfg, store, headless=False), store, site_url


def test_page_fits_the_window_and_scrolls_to_the_bottom(headed):
    capture, store, site_url = headed
    seen = {}

    def script(page):
        page.wait_for_timeout(600)
        seen["inner"] = page.evaluate("[innerWidth, innerHeight]")
        seen["outer"] = page.evaluate("[outerWidth, outerHeight]")
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(500)
        seen["footer"] = page.evaluate(
            "(() => { const r = document.getElementById('bottom').getBoundingClientRect();"
            " return [r.top, r.bottom, innerHeight]; })()")
        before = page.evaluate("[scrollY, document.getElementById('bottom').getBoundingClientRect().top]")
        capture._screenshot(store.shots / "probe.jpg")
        page.wait_for_timeout(200)
        seen["jump"] = (before, page.evaluate("[scrollY, document.getElementById('bottom').getBoundingClientRect().top]"))
        seen["size"] = page.evaluate("[innerWidth, innerHeight]")

    capture.run(f"{site_url}/tall.html", script=script)
    w, h = seen["inner"]
    assert w <= 1000 and h <= 620, f"page viewport {seen['inner']} is larger than the window"
    assert seen["outer"][1] <= 620 + 2
    top, bottom, inner_h = seen["footer"]
    assert 0 <= top and bottom <= inner_h + 1, "the bottom of the page cannot be scrolled into view"
    before, after = seen["jump"]
    assert before == after, "taking a screenshot moved the page"
    assert seen["size"] == seen["inner"], "taking a screenshot resized the page"
    assert (store.shots / "probe.jpg").stat().st_size > 1000
    assert store.meta["viewport"][:2] == seen["inner"]
