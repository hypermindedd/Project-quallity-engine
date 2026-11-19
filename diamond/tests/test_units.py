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


def test_repair_wav_written_by_a_killed_process(tmp_path):
    import wave

    from diamond.speech.base import repair_wav
    wav = tmp_path / "audio.wav"
    w = wave.open(str(wav), "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(16000)
    w.writeframes(b"")  # header is written now, with 0 frames
    w._file.flush()     # …and the process is killed before close() patches it
    with open(wav, "ab") as fh:
        fh.write(b"\x01\x00" * 16000)
    assert repair_wav(wav)
    with wave.open(str(wav), "rb") as r:
        assert r.getnframes() == 16000
    assert not repair_wav(wav)


def test_contrast_on_gradient_is_not_a_false_failure():
    from diamond.analyzers.accessibility import AccessibilityAnalyzer
    from diamond.analyzers.base import AnalysisContext
    from diamond.config import DiamondConfig

    el = {"selector": "#send", "tag": "button", "text": "ارسال", "name": "ارسال", "interactive": True,
          "rect": {"x": 0, "y": 0, "w": 80, "h": 40},
          "styles": {"color": "rgb(0, 22, 23)", "effectiveBackground": "image", "fontSize": "11px"}}
    inv = {"type": "inventory", "url": "u", "ts": 1, "elements": [el], "images": []}

    class Store:  # minimal stand-in
        def shot_near(self, ts):
            return ""

    res = AccessibilityAnalyzer().run(AnalysisContext(Store(), DiamondConfig(), [inv]))
    ids = [f.check_id for f in res.findings]
    assert "a11y.contrast" not in ids and "a11y.contrast_unverified" in ids


def test_parallel_resumable_model_fetch(tmp_path, monkeypatch):
    """Range download over a local HTTP server, interrupted once, then resumed and verified."""
    import functools
    import hashlib
    import http.server
    import json as _json
    import threading

    from diamond.speech import models

    blob = bytes(range(256)) * 80_000  # ~20 MB → 3 chunks
    root = tmp_path / "srv"
    (root / "api/models/org/m/tree").mkdir(parents=True)
    (root / "org/m/resolve/main").mkdir(parents=True)
    (root / "org/m/resolve/main/weights.safetensors").write_bytes(blob)
    (root / "org/m/resolve/main/config.json").write_text("{}")
    (root / "api/models/org/m/tree/main").write_text(_json.dumps([
        {"type": "file", "path": "config.json", "size": 2},
        {"type": "file", "path": "weights.safetensors", "size": len(blob),
         "lfs": {"oid": hashlib.sha256(blob).hexdigest()}}]))

    class Ranged(http.server.SimpleHTTPRequestHandler):
        fail_once = {"done": False}

        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            if not rng:
                return super().do_GET()
            if not Ranged.fail_once["done"]:
                Ranged.fail_once["done"] = True
                self.send_error(503)
                return
            start, end = (int(x) for x in rng.split("=")[1].split("-"))
            data = (root / self.path.lstrip("/")).read_bytes()[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Ranged, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("HF_ENDPOINT", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("DIAMOND_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setattr(models.time, "sleep", lambda s: None)
    try:
        path = models.fetch_model("org/m", connections=4, progress=lambda d, t: None)
    finally:
        server.shutdown()
    assert (path / "weights.safetensors").read_bytes() == blob
    assert models.local_model("org/m") == path


def test_load_wav_without_ffmpeg(tmp_path):
    import wave

    import numpy as np

    from diamond.speech.base import load_wav
    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.array([0, 16384, -32768], dtype=np.int16)).tobytes())
    samples = load_wav(wav)
    assert samples.dtype == np.float32 and list(samples) == [0.0, 0.5, -1.0]


def test_silent_recording_is_reported_not_transcribed(tmp_path):
    import json as _json
    import wave

    from diamond.config import SpeechConfig
    from diamond.session import SessionStore
    from diamond.speech.base import TranscriptionUnavailable, transcribe_session
    store = SessionStore.create(tmp_path, {"url": "u"})
    with wave.open(str(store.root / "audio.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    (store.root / "audio.json").write_text(_json.dumps({"start_epoch_ms": 1.0}))
    with pytest.raises(TranscriptionUnavailable, match="silence"):
        transcribe_session(store, SpeechConfig(provider="openai"))


def test_comma_chained_remarks_become_separate_notes():
    words = []
    t = 0
    for w in "اینجا باید بیشتر توضیح داده بشه، این قسمت‌ها همه‌ش تو هم قاطی شده، این فونت خیلی گنده است".split():
        words.append(Word(w, t, t + 150))
        t += 160
    events = [{"type": "hover", "ts": 0, "url": "u", "el": {"selector": "#a"}},
              {"type": "hover", "ts": 900, "url": "u", "el": {"selector": "#b"}},
              {"type": "hover", "ts": 1880, "url": "u", "el": {"selector": "#c"}}]
    notes = align(words, events, FusionConfig())
    cats = [n.category for n in notes]
    assert cats == [Category.CONTENT, Category.LAYOUT, Category.VISUAL], cats
    assert [n.target["selector"] for n in notes] == ["#a", "#b", "#c"]


def test_hallucination_filter():
    from diamond.speech.base import plausible_word
    assert plausible_word("دکمه", 0.9) and plausible_word("Previous", 0.8)
    assert not plausible_word("тер", 0.9) and not plausible_word("よう", 0.9) and not plausible_word("رسول", 0.01)
