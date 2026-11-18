"""Review session orchestration: browser + microphone, start/stop from any thread."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from .capture.audio import AudioUnavailable, MicRecorder
from .capture.browser import BrowserCapture
from .config import DiamondConfig
from .session import SessionStore

log = logging.getLogger("diamond.recorder")


class ReviewSession:
    """One narrated review. ``start()`` returns once the browser is ready."""

    def __init__(self, cfg: DiamondConfig, url: str, repo: Path | None = None, audio: bool = True,
                 headless: bool = False, base_dir: Path | None = None) -> None:
        sessions = (base_dir or repo or Path.cwd()) / cfg.sessions_dir
        self.store = SessionStore.create(sessions, {"url": url, "repo": str(repo.resolve()) if repo else ""})
        self.capture = BrowserCapture(cfg, self.store, headless=headless)
        self.mic = MicRecorder(self.store) if audio else None
        self.audio_status = "disabled"
        self.url = url
        self._thread: threading.Thread | None = None
        self._error: BaseException | None = None

    def _run(self, script: Any = None) -> None:
        try:
            self.capture.run(self.url, script=script)
        except BaseException as exc:  # surfaced to the caller in wait()
            self._error = exc
            self.capture.ready.set()

    def start(self, script: Any = None, timeout: float = 60) -> None:
        if self.mic is not None:
            try:
                self.mic.start()
                self.audio_status = "recording"
            except AudioUnavailable as exc:
                self.audio_status = f"unavailable: {exc}"
                self.mic = None
                log.warning("%s", exc)
        self._thread = threading.Thread(target=self._run, args=(script,), name="diamond-capture", daemon=True)
        self._thread.start()
        self.capture.ready.wait(timeout)
        if self._error:
            self.stop()
            raise self._error

    def stop(self) -> Path:
        self.capture.stop()
        if self._thread is not None:
            self._thread.join(timeout=30)
        if self.mic is not None:
            self.mic.stop()
        self.store.update_meta(audio=self.audio_status)
        return self.store.root

    def wait(self) -> Path:
        """Block until the reviewer closes the browser window."""
        if self._thread is not None:
            self._thread.join()
        if self._error:
            raise self._error
        return self.stop()
