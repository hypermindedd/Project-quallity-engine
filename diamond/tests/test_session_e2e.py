"""End-to-end: a real Chromium session on a deliberately flawed site, with a
synthetic narration aligned to the scripted pointer, analysed with the repo."""

import json
import time

import pytest

from diamond.config import load_config
from diamond.pipeline import analyze
from diamond.recorder import ReviewSession


def _center(page, selector):
    box = page.locator(selector).bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


@pytest.fixture(scope="module")
def outcome(site_url, fixture_repo, tmp_path_factory):
    cfg = load_config(fixture_repo)
    cfg.speech.provider = "none"
    spoken: list[tuple[str, float]] = []

    def say(text):
        spoken.append((text, time.time() * 1000))

    def script(page):
        page.wait_for_timeout(1500)  # late banner + first inventory
        x, y = _center(page, "#hint")
        page.mouse.move(x, y, steps=5)
        say("این متن خیلی کمرنگه و دیده نمیشه")
        page.wait_for_timeout(900)
        x, y = _center(page, "#fake-buy")
        page.mouse.move(x, y, steps=5)
        page.wait_for_timeout(800)
        page.mouse.click(x, y)
        say("روی این کلیک میکنم هیچی نمیشه")
        page.wait_for_timeout(1300)
        x, y = _center(page, "#heavy")
        page.mouse.move(x, y, steps=5)
        page.mouse.click(x, y)
        say("این دکمه لگ داره")
        page.wait_for_timeout(1200)
        x, y = _center(page, "#progress")
        page.mouse.move(x, y, steps=5)
        say("انیمیشن این نوار نرم نیست")
        page.wait_for_timeout(1200)
        page.click("#next-page")
        page.wait_for_load_state("load")
        page.wait_for_timeout(1800)
        x, y = _center(page, "#intro")
        page.mouse.move(x, y, steps=5)
        say("اینجا متن چپ چین شده")
        page.wait_for_timeout(1000)

    session = ReviewSession(cfg, f"{site_url}/index.html", repo=fixture_repo, audio=False, headless=True,
                            base_dir=tmp_path_factory.mktemp("sessions"))
    session.start(script=script)
    root = session.wait()

    words = []
    for text, t in spoken:
        for i, w in enumerate(text.split()):
            words.append({"text": w, "start": t + i * 180, "end": t + i * 180 + 160, "probability": 1.0})
    (root / "transcript.json").write_text(json.dumps({"provider": "synthetic", "words": words}, ensure_ascii=False))
    return analyze(root, cfg, fixture_repo)


def _ids(outcome):
    return {f.check_id for f in outcome.findings}


def test_all_analyzers_ran(outcome):
    statuses = {r.analyzer: r.status.value for r in outcome.results}
    assert statuses["visual_critic"] == "SKIPPED"  # opt-in
    assert all(s in ("PASS", "FAIL") for name, s in statuses.items() if name != "visual_critic"), statuses


def test_voice_notes_bound_to_the_right_elements(outcome):
    voice = [f for f in outcome.findings if f.origin.value == "VOICE"]
    by_selector = {f.target["selector"]: f for f in voice if f.target}
    assert "#hint" in by_selector and by_selector["#hint"].category.value == "VISUAL"
    assert "#fake-buy" in by_selector and by_selector["#fake-buy"].category.value == "INTERACTION"
    assert "#heavy" in by_selector and by_selector["#heavy"].category.value == "PERFORMANCE"
    assert "#intro" in by_selector and by_selector["#intro"].category.value == "RTL"


def test_lag_claim_is_corroborated_by_telemetry(outcome):
    lag = next(f for f in outcome.findings if f.origin.value == "VOICE" and (f.target or {}).get("selector") == "#heavy")
    telemetry = lag.evidence["telemetry"]
    assert "long_frames" in telemetry or "slow_interaction" in telemetry, telemetry


def test_automatic_findings(outcome):
    ids = _ids(outcome)
    for expected in ("interaction.dead_click", "a11y.contrast", "a11y.accessible_name", "a11y.img_alt",
                     "rtl.page_direction", "rtl.misaligned_text", "consistency.near_duplicate_colors",
                     "consistency.button_variants", "consistency.font_sizes", "animation.layout_bound",
                     "animation.too_long", "perf.cls", "perf.slow_interaction", "layout.horizontal_overflow",
                     "layout.broken_image", "arch.hardcoded_colors", "arch.client_route"):
        assert expected in ids, f"missing {expected}; got {sorted(ids)}"


def test_root_cause_traced_to_source(outcome):
    contrast = next(f for f in outcome.findings if f.check_id == "a11y.contrast" and f.target["selector"] == "#hint")
    assert any(s.path.endswith(("Hero.tsx", "hero.css")) for s in contrast.sources), contrast.sources


def test_outputs_written(outcome):
    root = outcome.session
    for name in ("report.md", "findings.json", "pqe-findings.json", "test_diamond_regressions.py"):
        assert (root / name).is_file(), name
    assert list((root / "shots").glob("*.jpg")), "screenshots"
    assert len(list((root / "briefs").glob("*.md"))) == len(outcome.findings)
    compile((root / "test_diamond_regressions.py").read_text(encoding="utf-8"), "regressions", "exec")


def test_voice_and_measurement_are_cross_linked(outcome):
    voice = next(f for f in outcome.findings if f.origin.value == "VOICE" and f.target["selector"] == "#fake-buy")
    dead = next(f for f in outcome.findings if f.check_id == "interaction.dead_click")
    assert dead.fingerprint in voice.evidence["corroborated_by"]
    assert voice.fingerprint in dead.evidence["reported_by_reviewer"] and dead.severity.value == "SEV1"
