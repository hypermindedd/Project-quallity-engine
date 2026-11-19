"""Deictic resolution: "this button is laggy" → which button?

Algorithm (all times epoch ms):

1. Words are grouped into utterances, split on silences longer than
   ``pause_split_ms`` or on sentence punctuation.
2. Each utterance gets an *anchor*: the start of its first deictic word
   ("این", "اینجا", "اون", …) or, if none, the utterance start.
3. Pointer events (click > mark > dwell > hover) inside
   ``[anchor - lookback, anchor + lookahead]`` are scored by distance to the
   anchor; future events cost 1.5× (people usually point *while* speaking,
   sometimes just after). The element under the pointer before the window
   is a fallback, because a resting mouse emits no events.
4. The chosen element is enriched with computed styles from the nearest
   dwell/click/inventory record of the same selector on the same page.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import FusionConfig
from ..model import Category, Severity
from ..session import Word
from ..speech.lexicon import classify, is_deictic

POINTER_KINDS = {"click": 300.0, "mark": 300.0, "dwell": 200.0, "hover": 0.0}  # bonus in ms
SENTENCE_END = ("؟", "?", ".", "!", "؛", "،", ",")


@dataclass
class Utterance:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end

    @property
    def anchor(self) -> float:
        for w in self.words:
            if is_deictic(w.text):
                return w.start
        return self.start


@dataclass
class VoiceNote:
    text: str
    start: float
    end: float
    anchor: float
    category: Category | None
    severity: Severity
    commands: set[str] = field(default_factory=set)
    positive: bool = False
    target: dict[str, Any] | None = None
    target_kind: str = ""
    page_url: str = ""
    confidence: float = 0.0  # 1.0 = pointer event exactly at the anchor


REANCHOR_GAP_MS = 120  # a short breath before a new deictic word starts a new remark
REANCHOR_MIN_WORDS = 3


def split_utterances(words: list[Word], pause_ms: int) -> list[Utterance]:
    """Split on long silences, sentence punctuation, or a re-anchoring deictic word.

    Reviewers often chain remarks without pausing ("this text is faded … this one does
    nothing"); a new deictic word after a short breath, once the current remark has a few
    words, starts a new utterance so each remark gets its own pointer target.
    """
    utterances: list[Utterance] = []
    current: list[Word] = []
    for w in words:
        gap = w.start - current[-1].end if current else 0
        reanchor = (gap > REANCHOR_GAP_MS and len(current) >= REANCHOR_MIN_WORDS and is_deictic(w.text))
        if current and (gap > pause_ms or reanchor or current[-1].text.endswith(SENTENCE_END)):
            utterances.append(Utterance(current))
            current = []
        current.append(w)
    if current:
        utterances.append(Utterance(current))
    return utterances


def _pointer_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") in POINTER_KINDS and e.get("el")]


def _styled_index(events: list[dict[str, Any]]) -> dict[tuple[str, str], list[tuple[float, dict[str, Any]]]]:
    index: dict[tuple[str, str], list[tuple[float, dict[str, Any]]]] = {}
    for e in events:
        if e.get("type") in ("dwell", "click", "mark") and e.get("el", {}).get("styles"):
            index.setdefault((e["url"], e["el"]["selector"]), []).append((e["ts"], e["el"]))
        elif e.get("type") == "inventory":
            for el in e.get("elements", []):
                index.setdefault((e["url"], el["selector"]), []).append((e["ts"], el))
    return index


def resolve_target(anchor: float, pointer: list[dict[str, Any]], cfg: FusionConfig) -> tuple[dict[str, Any] | None, float]:
    """Return (pointer event, confidence) for an anchor time."""
    lo, hi = anchor - cfg.lookback_ms, anchor + cfg.lookahead_ms
    best: tuple[float, dict[str, Any]] | None = None
    before_window: dict[str, Any] | None = None
    for e in pointer:
        ts = e["ts"]
        if ts < lo:
            before_window = e
            continue
        if ts > hi:
            break
        dist = (anchor - ts) if ts <= anchor else (ts - anchor) * 1.5
        score = dist - POINTER_KINDS[e["type"]]
        if best is None or score < best[0]:
            best = (score, e)
    if best is not None:
        confidence = max(0.2, 1.0 - max(best[0], 0) / (cfg.lookback_ms * 1.5))
        return best[1], round(confidence, 2)
    if before_window is not None:
        return before_window, 0.35  # pointer was resting on it, no fresh evidence
    return None, 0.0


def align(words: list[Word], events: list[dict[str, Any]], cfg: FusionConfig) -> list[VoiceNote]:
    pointer = _pointer_events(events)
    styled = _styled_index(events)
    notes: list[VoiceNote] = []
    for utt in split_utterances(words, cfg.pause_split_ms):
        category, severity, commands, positive = classify(utt.text)
        event, confidence = resolve_target(utt.anchor, pointer, cfg)
        target = None
        url = ""
        if event is not None:
            target = dict(event["el"])
            url = event.get("url", "")
            if not target.get("styles"):
                candidates = styled.get((url, target.get("selector", "")), [])
                if candidates:
                    _, enriched = min(candidates, key=lambda c: abs(c[0] - utt.anchor))
                    target = {**enriched, **{k: v for k, v in target.items() if v}}
        notes.append(VoiceNote(utt.text, utt.start, utt.end, utt.anchor, category, severity, commands, positive,
                               target, event["type"] if event else "", url, confidence))
    return notes
