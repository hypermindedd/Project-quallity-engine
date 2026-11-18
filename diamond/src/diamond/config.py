"""Configuration with explicit threshold provenance.

Every threshold is either a published standard (``std``) or a heuristic
default (``heuristic``) that a project can override in ``.diamond.toml``.
The provenance is copied into each finding's evidence so a reader always
knows whether a number is a rule or a judgement call.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

CONFIG_FILE = ".diamond.toml"


@dataclass(frozen=True)
class Threshold:
    value: float
    origin: str  # "std:<reference>" or "heuristic"

    def as_evidence(self) -> dict[str, Any]:
        return {"threshold": self.value, "threshold_origin": self.origin}


def _t(value: float, origin: str) -> Any:
    return field(default_factory=lambda: Threshold(value, origin))


@dataclass
class Thresholds:
    # Published standards
    cls: Threshold = _t(0.1, "std:web.dev Core Web Vitals (CLS good ≤ 0.1)")
    inp_ms: Threshold = _t(200, "std:web.dev Core Web Vitals (INP good ≤ 200ms)")
    long_frame_ms: Threshold = _t(50, "std:W3C Long Animation Frames (frame > 50ms)")
    contrast_normal: Threshold = _t(4.5, "std:WCAG 2.2 SC 1.4.3 AA")
    contrast_large: Threshold = _t(3.0, "std:WCAG 2.2 SC 1.4.3 AA (large text)")
    target_size_px: Threshold = _t(24, "std:WCAG 2.2 SC 2.5.8 AA")
    # Heuristic defaults (overridable)
    dead_click_ms: Threshold = _t(1000, "heuristic")
    rage_click_count: Threshold = _t(3, "heuristic")
    rage_click_window_ms: Threshold = _t(800, "heuristic")
    rage_click_radius_px: Threshold = _t(30, "heuristic")
    color_near_delta_e: Threshold = _t(5.0, "heuristic (CIEDE76; ~2.3 is a just-noticeable difference)")
    max_button_variants: Threshold = _t(4, "heuristic")
    max_font_sizes: Threshold = _t(8, "heuristic")
    spacing_grid_px: Threshold = _t(4, "heuristic (4/8-pt grid)")
    off_grid_ratio: Threshold = _t(0.3, "heuristic")
    animation_max_ms: Threshold = _t(700, "heuristic")


@dataclass
class FusionConfig:
    lookback_ms: int = 1500       # pointer history considered before a deictic word
    lookahead_ms: int = 800       # people often point right after speaking
    pause_split_ms: int = 500     # silence that splits two utterances (typical sentence gap 300–600ms)
    dwell_ms: int = 700           # pointer rest that triggers a screenshot


@dataclass
class SpeechConfig:
    provider: str = "auto"                     # auto | mlx | faster | openai | none
    model: str = "large-v3-turbo"
    language: str = "fa"
    # Domain vocabulary passed as the initial prompt: improves recognition of
    # English UI terms inside Persian speech.
    vocabulary: list[str] = field(default_factory=lambda: [
        "باتن", "دکمه", "هدر", "فوتر", "منو", "مودال", "اسکرول", "انیمیشن", "لگ", "فونت",
        "پدینگ", "مارجین", "کارت", "نوار", "آیکون", "لودینگ", "فرم", "اینپوت", "دراپ‌داون",
    ])


@dataclass
class ModelRouting:
    """Which model tier handles which task (cheapest model that is good enough)."""

    light: str = "claude-haiku-5-5"
    standard: str = "claude-sonnet-5-5"
    heavy: str = "claude-opus-5-5"
    tasks: dict[str, str] = field(default_factory=lambda: {
        "transcript_cleanup": "light",
        "intent_classification": "light",
        "visual_critique": "standard",
        "root_cause_single_file": "standard",
        "fix_implementation": "standard",
        "root_cause_cross_file": "heavy",
        "architecture_review": "heavy",
    })

    def model_for(self, task: str) -> str:
        tier = self.tasks.get(task, "standard")
        return getattr(self, tier, self.standard)


@dataclass
class DiamondConfig:
    repo: Path | None = None
    sessions_dir: Path = Path(".diamond/sessions")
    viewport: tuple[int, int] = (1440, 900)
    browser_channel: str = ""                  # "" = bundled Chromium, "chrome" = installed Chrome
    source_attributes: list[str] = field(default_factory=lambda: [
        "data-insp-path",        # code-inspector-plugin (Vite / Webpack / Next / Vue / Svelte)
        "data-source",           # generic "file:line[:col]"
        "data-diamond-src",      # Diamond's own convention
    ])
    axe_script: str = ""                       # optional path to axe.min.js
    thresholds: Thresholds = field(default_factory=Thresholds)
    fusion: FusionConfig = field(default_factory=FusionConfig)
    speech: SpeechConfig = field(default_factory=SpeechConfig)
    routing: ModelRouting = field(default_factory=ModelRouting)
    anthropic_api_key_env: str = "ANTHROPIC_API_KEY"
    visual_critic: bool = False                # opt-in: costs tokens

    @property
    def anthropic_api_key(self) -> str:
        return os.environ.get(self.anthropic_api_key_env, "")


def load_config(repo: Path | None = None, path: Path | None = None) -> DiamondConfig:
    """Load defaults, then ``.diamond.toml`` from ``path`` or the repo root."""
    cfg = DiamondConfig(repo=repo)
    candidate = path or (repo / CONFIG_FILE if repo else None)
    if candidate and candidate.is_file():
        with candidate.open("rb") as fh:
            _apply(cfg, tomllib.load(fh))
    # The launcher sets this when it uses the already-installed Google Chrome
    # instead of downloading Chromium (the download host can be unreachable).
    cfg.browser_channel = os.environ.get("DIAMOND_BROWSER_CHANNEL", cfg.browser_channel)
    return cfg


def _apply(cfg: DiamondConfig, data: dict[str, Any]) -> None:
    simple = {f.name for f in fields(DiamondConfig)} - {"thresholds", "fusion", "speech", "routing", "repo"}
    for key, value in data.items():
        if key in simple:
            if key == "viewport":
                value = tuple(value)
            elif key == "sessions_dir":
                value = Path(value)
            setattr(cfg, key, value)
    for name, section in (("fusion", cfg.fusion), ("speech", cfg.speech), ("routing", cfg.routing)):
        for key, value in (data.get(name) or {}).items():
            if not hasattr(section, key):
                raise ValueError(f"unknown setting [{name}].{key} in {CONFIG_FILE}")
            setattr(section, key, value)
    for key, value in (data.get("thresholds") or {}).items():
        if not hasattr(cfg.thresholds, key):
            raise ValueError(f"unknown threshold '{key}' in {CONFIG_FILE}")
        # A project override is a deliberate decision, recorded as such.
        setattr(cfg.thresholds, key, Threshold(float(value), f"project:{CONFIG_FILE}"))
