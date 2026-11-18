from collections import Counter
from pathlib import Path

import pytest

from diamond.analyzers.colors import contrast_ratio, delta_e
from diamond.analyzers.consistency import near_duplicate_clusters
from diamond.analyzers.performance import cls_session_windows
from diamond.config import FusionConfig, load_config
from diamond.fix import route_task
from diamond.fusion import align, split_utterances
from diamond.locate import SourceIndex, locate_finding, parse_location_string
from diamond.model import Category, Finding, Severity, SourceLocation
from diamond.report.pqe_export import regression_tests, to_pqe
from diamond.session import Word
from diamond.speech.lexicon import classify, is_deictic, normalize


# ---------------------------------------------------------------- lexicon
@pytest.mark.parametrize("text, category", [
    ("این دکمه خیلی لگ داره", Category.PERFORMANCE),
    ("اینجا انیمیشنش نرم نیست", Category.ANIMATION),
    ("روش کلیک میکنم هیچی نمیشه", Category.INTERACTION),
    ("این باتن زیادیه، فاصله‌اش هم کمه", Category.LAYOUT),
    ("رنگ این متن کمرنگه", Category.VISUAL),
    ("این متن غلط املایی داره", Category.CONTENT),
])
def test_classify_colloquial_persian(text, category):
    assert classify(text)[0] == category


def test_cue_does_not_match_inside_other_words():
    # "کند" (slow) must not fire inside "نمیکند", "جا" must not fire inside "اینجا"
    assert classify("این دکمه کار نمیکند")[0] == Category.INTERACTION
    assert classify("اینجا")[0] is None


def test_severity_intensifier_and_command():
    _, sev, _, _ = classify("این صفحه لگ داره")
    _, sev_hi, _, _ = classify("این صفحه خیلی لگ داره")
    assert sev_hi.rank < sev.rank
    _, sev_cmd, commands, _ = classify("رنگش بده، اولویت بالا")
    assert "priority_high" in commands and sev_cmd == Severity.SEV1


def test_positive_without_issue():
    category, sev, _, positive = classify("این قسمت خوبه")
    assert category is None and positive and sev == Severity.INFO


def test_normalize_arabic_letters_and_zwnj():
    assert normalize("كيك‌ها") == "کیک ها"
    assert is_deictic("این") and is_deictic("اینجا،") and not is_deictic("دکمه")


# ---------------------------------------------------------------- fusion
def _words(*spec):
    return [Word(t, s, e) for t, s, e in spec]


def test_split_utterances_on_pause():
    utts = split_utterances(_words(("این", 0, 200), ("دکمه", 210, 400), ("بعدی", 2000, 2200)), pause_ms=700)
    assert [u.text for u in utts] == ["این دکمه", "بعدی"]


def test_align_resolves_deictic_to_pointer():
    events = [
        {"type": "hover", "ts": 900, "url": "u", "el": {"selector": "#a"}},
        {"type": "hover", "ts": 4000, "url": "u", "el": {"selector": "#b"}},
        {"type": "dwell", "ts": 4300, "url": "u", "el": {"selector": "#b", "styles": {"color": "x"}}},
    ]
    words = _words(("رنگ", 3900, 4000), ("این", 4100, 4200), ("زشته", 4210, 4400))
    note = align(words, events, FusionConfig())[0]
    assert note.target["selector"] == "#b" and note.target.get("styles")
    assert note.category == Category.VISUAL and note.confidence > 0.5


def test_align_falls_back_to_resting_pointer():
    events = [{"type": "hover", "ts": 0, "url": "u", "el": {"selector": "#rest"}}]
    note = align(_words(("این", 10_000, 10_200), ("کنده", 10_210, 10_400)), events, FusionConfig())[0]
    assert note.target["selector"] == "#rest" and note.confidence == 0.35


# ---------------------------------------------------------------- colour & perf math
def test_contrast_known_values():
    assert round(contrast_ratio((0, 0, 0), (255, 255, 255)), 1) == 21.0
    assert contrast_ratio((181, 181, 181), (255, 255, 255)) < 4.5


def test_near_duplicate_colors_cluster():
    colors = Counter({(26, 115, 232): 5, (28, 118, 235): 2, (0, 0, 0): 9})
    clusters = near_duplicate_clusters(colors, 5.0)
    assert len(clusters) == 1 and clusters[0][0] == (26, 115, 232)
    assert delta_e((0, 0, 0), (0, 0, 0)) == 0


def test_cls_uses_largest_session_window():
    shifts = [{"ts": 0, "start": 0, "value": 0.05}, {"ts": 0, "start": 500, "value": 0.05},
              {"ts": 0, "start": 3000, "value": 0.02}, {"ts": 0, "start": 3100, "value": 0.5, "hadRecentInput": True}]
    assert round(cls_session_windows(shifts), 3) == 0.1


# ---------------------------------------------------------------- locate
def test_parse_code_inspector_attribute():
    loc = parse_location_string("src/components/Hero.tsx:12:5:button", None)
    assert (loc.path, loc.line, loc.column, loc.confidence) == ("src/components/Hero.tsx", 12, 5, 1.0)


def test_locate_by_text_search(fixture_repo):
    f = Finding("x", Category.VISUAL, Severity.SEV3, "t", target={"text": "افزودن به سبد", "classes": ["fake"]})
    locate_finding(f, SourceIndex(fixture_repo))
    paths = {s.path for s in f.sources}
    assert "src/components/Hero.tsx" in paths and "src/styles/hero.css" in paths


def test_locate_prefers_instrumentation(fixture_repo):
    f = Finding("x", Category.VISUAL, Severity.SEV3, "t",
                target={"text": "ذخیره", "source": {"attr": {"name": "data-insp-path", "value": "src/components/Hero.tsx:8:9:button", "depth": 0}}})
    locate_finding(f, SourceIndex(fixture_repo))
    assert f.sources[0].render() == "src/components/Hero.tsx:8:9" and f.sources[0].method.startswith("attribute")


# ---------------------------------------------------------------- outputs
def test_pqe_export_and_regression_test():
    f = Finding("a11y.contrast", Category.ACCESSIBILITY, Severity.SEV2, "low contrast", page_url="http://x/a",
                target={"selector": "#hint"}, evidence={"threshold": 4.5}, sources=[SourceLocation("src/a.tsx", 3)])
    row = to_pqe([f])[0]
    assert row["dimension"] == "ACCESSIBILITY" and row["location"] == {"path": "src/a.tsx", "line": 3}
    code = regression_tests([f])
    compile(code, "generated.py", "exec")
    assert "def test_a11y_contrast_" in code


def test_route_task_tiers():
    strong = Finding("x", Category.VISUAL, Severity.SEV3, "t", sources=[SourceLocation("a.tsx", 1, None, 1.0)])
    weak = Finding("x", Category.VISUAL, Severity.SEV3, "t", sources=[SourceLocation("a.tsx", 1, None, 0.3)])
    arch = Finding("x", Category.ARCHITECTURE, Severity.SEV3, "t")
    assert route_task(strong) == "root_cause_single_file"
    assert route_task(weak) == "root_cause_cross_file"
    assert route_task(arch) == "architecture_review"


def test_config_override_records_provenance(tmp_path: Path):
    (tmp_path / ".diamond.toml").write_text("[thresholds]\nmax_button_variants = 6\n[fusion]\ndwell_ms = 500\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.thresholds.max_button_variants.value == 6
    assert cfg.thresholds.max_button_variants.origin.startswith("project:")
    assert cfg.fusion.dwell_ms == 500
    (tmp_path / ".diamond.toml").write_text("[thresholds]\nnope = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(tmp_path)
