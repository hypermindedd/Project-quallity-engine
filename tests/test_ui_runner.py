"""Browser UI runner against a static fixture site.

Needs a Chromium: Playwright's own (``playwright install chromium``) or one
named by ``PQE_CHROMIUM_PATH``. Skipped otherwise — never silently passed.
"""

from __future__ import annotations

import sys

import pytest

from pqe.model import Severity, Status
from pqe.runners.ui import UiRunner, same_origin_links, slug

from .conftest import FIXTURES

pytest.importorskip("playwright")


def test_link_normalisation_stays_on_origin():
    links = same_origin_links("http://h:1/a/", ["b", "/c#x", "http://evil/x", "mailto:a@b", "/c", "#top"])
    assert links == ["http://h:1/a/b", "http://h:1/c", "http://h:1/a/"]
    assert slug("http://h/a/b?x=1") == "a_b_x_1"


def test_ui_runner_finds_click_errors_overflow_broken_images_and_direction(ctx_factory):
    contract = f"""
        [project]
        name = "site"
        [ui]
        enabled = true
        start_command = ["{sys.executable}", "-m", "http.server", "{{port}}", "--bind", "127.0.0.1", "--directory", "{FIXTURES / 'site'}"]
        ready_path = "/index.html"
        pages = ["/index.html"]
        expect_dir = "rtl"
        viewports = [{{ name = "mobile", width = 390, height = 844 }}, {{ name = "desktop", width = 1280, height = 800 }}]
    """
    ctx = ctx_factory(contract)
    results = {r.check_id: r for r in UiRunner().run(ctx)}
    if "ui" in results and results["ui"].status == Status.SKIPPED:
        pytest.skip(results["ui"].summary)

    interaction = results["ui.interaction"]
    titles = [f.title for f in interaction.findings]
    assert any("Uncaught exception while clicking button" in t and "ارسال گزارش" in t for t in titles), titles
    assert not any("DESTRUCTIVE" in f.detail for f in interaction.findings), "destructive control must never be clicked"
    assert interaction.status == Status.FAIL

    responsive = results["ui.responsive"]
    rtitles = [f.title for f in responsive.findings]
    assert any(t.startswith("Horizontal overflow at mobile") for t in rtitles), rtitles
    assert "Broken image" in rtitles
    assert any("Text direction is" in t for t in rtitles)  # page2 forgot dir="rtl"
    assert all(f.severity == Severity.SEV2 for f in responsive.findings)

    assert results["ui.visual"].status == Status.UNCALIBRATED  # no approved baselines yet
    assert results["ui.accessibility"].status == Status.SKIPPED  # no axe script configured
    shots = {p.relative_to(ctx.out_dir / "ui").as_posix() for p in (ctx.out_dir / "ui").rglob("*.png")}
    assert {"mobile/index.html.png", "mobile/page2.html.png", "desktop/page2.html.png"} <= shots
    assert len(shots) == 2 * interaction.metrics["pages"]  # every crawled page at every viewport
