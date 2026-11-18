"""On-disk session store.

Layout of one session directory::

    meta.json          url, repo, start/end, viewport, versions
    events.jsonl       every browser/engine event, one JSON object per line
    audio.wav          microphone recording (if enabled)
    audio.json         {start_epoch_ms, samplerate}
    transcript.json    words with epoch-ms timestamps
    shots/<ts>.jpg     viewport screenshots taken on dwell / click / mark / page
    report.md, findings.json, pqe-findings.json, briefs/   (written by the pipeline)

Everything is plain files so a session can be zipped, attached to an issue,
or re-analysed later with a newer engine.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Word:
    text: str
    start: float  # epoch ms
    end: float    # epoch ms
    probability: float = 1.0


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.shots = root / "shots"
        self._lock = threading.Lock()

    # ------------------------------------------------------------- creation
    @classmethod
    def create(cls, sessions_dir: Path, meta: dict[str, Any]) -> SessionStore:
        sid = time.strftime("%Y%m%d-%H%M%S")
        root = sessions_dir / sid
        suffix = 1
        while root.exists():
            suffix += 1
            root = sessions_dir / f"{sid}-{suffix}"
        root.mkdir(parents=True)
        store = cls(root)
        store.shots.mkdir()
        store.write_json("meta.json", {"id": root.name, "started_ms": time.time() * 1000, **meta})
        return store

    @classmethod
    def open(cls, root: Path) -> SessionStore:
        if not (root / "meta.json").is_file():
            raise FileNotFoundError(f"not a Diamond session directory: {root}")
        return cls(root)

    # ------------------------------------------------------------------ I/O
    def append_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        lines = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        with self._lock, (self.root / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(lines)

    def events(self) -> list[dict[str, Any]]:
        path = self.root / "events.jsonl"
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        out.sort(key=lambda e: e.get("ts", 0))
        return out

    def iter_type(self, *types: str) -> Iterator[dict[str, Any]]:
        return (e for e in self.events() if e.get("type") in types)

    def write_json(self, name: str, data: Any) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_json(self, name: str, default: Any = None) -> Any:
        path = self.root / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default

    def update_meta(self, **values: Any) -> None:
        meta = self.read_json("meta.json", {})
        meta.update(values)
        self.write_json("meta.json", meta)

    @property
    def meta(self) -> dict[str, Any]:
        return self.read_json("meta.json", {})

    def words(self) -> list[Word]:
        return [Word(**w) for w in self.read_json("transcript.json", {}).get("words", [])]

    def shot_near(self, ts: float, max_gap_ms: float = 2500) -> str:
        """Relative path of the screenshot closest in time to ``ts`` (or '')."""
        best, gap = "", max_gap_ms
        for shot in self.shots.glob("*.jpg"):
            try:
                t = float(shot.stem.split("_", 1)[0])
            except ValueError:
                continue
            if abs(t - ts) <= gap:
                best, gap = f"shots/{shot.name}", abs(t - ts)
        return best
