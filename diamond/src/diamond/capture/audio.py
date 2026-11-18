"""Microphone capture to a 16 kHz mono WAV with an epoch-ms start time.

The start time is taken from the ADC timestamp of the first audio block and
converted to wall-clock epoch ms, the same clock the in-page recorder uses.
Measured skew is typically well under 50 ms on macOS (CoreAudio).
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

from ..session import SessionStore

SAMPLE_RATE = 16_000


class AudioUnavailable(RuntimeError):
    pass


class MicRecorder:
    def __init__(self, store: SessionStore, device: int | str | None = None) -> None:
        self.store = store
        self.device = device
        self._stream = None
        self._wav: wave.Wave_write | None = None
        self._start_ms: float | None = None
        self._clock_offset = 0.0
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self.heard = False   # any non-zero sample so far
        self._frames = 0

    def start(self) -> None:
        try:
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            raise AudioUnavailable(
                "microphone capture needs sounddevice: pip install 'diamond-engine[audio]' "
                "(macOS wheels bundle PortAudio; on Linux also install libportaudio2)"
            ) from exc

        path: Path = self.store.root / "audio.wav"
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SAMPLE_RATE)

        stopping = self._stopping

        def callback(indata, frames, time_info, status):  # noqa: ARG001 - PortAudio signature
            # Runs on CoreAudio's thread. Never touch the stream object or block here:
            # calling stream methods from the callback deadlocks Pa_StopStream on macOS.
            if stopping.is_set():
                raise sd.CallbackStop
            if self._start_ms is None:
                self._start_ms = (time_info.inputBufferAdcTime + self._clock_offset) * 1000
            data = bytes(indata)
            if not self.heard and any(data):
                self.heard = True
            self._frames += frames
            with self._lock:
                if self._wav is not None:
                    self._wav.writeframes(data)

        self._stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                         device=self.device, callback=callback)
        # stream clock → wall clock, measured once on the caller's thread
        self._clock_offset = time.time() - self._stream.time
        self._stream.start()
        self._write_meta()  # audio.json exists even if the process dies mid-session

    @property
    def silent_so_far(self) -> bool:
        """True after 3 s of exact digital silence (typical of a denied macOS permission)."""
        return not self.heard and self._frames >= SAMPLE_RATE * 3

    def _write_meta(self) -> None:
        self.store.write_json("audio.json", {"start_epoch_ms": self._start_ms or time.time() * 1000,
                                             "samplerate": SAMPLE_RATE})

    def stop(self) -> None:
        """Stop without ever hanging: ask the callback to stop, then abort with a deadline."""
        self._stopping.set()
        stream = self._stream
        if stream is not None:
            done = threading.Event()

            def _close() -> None:
                try:
                    stream.abort()  # abort, not stop: no waiting for buffers to drain
                    stream.close()
                finally:
                    done.set()

            threading.Thread(target=_close, name="diamond-mic-close", daemon=True).start()
            done.wait(timeout=3)
        with self._lock:
            if self._wav is not None:
                self._wav.close()  # writes the correct WAV header
                self._wav = None
        self._write_meta()
