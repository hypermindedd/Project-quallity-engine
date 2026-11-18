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
        self._lock = threading.Lock()

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

        def callback(indata, frames, time_info, status):  # noqa: ARG001 - PortAudio signature
            with self._lock:
                if self._start_ms is None:
                    # stream clock → wall clock at the moment the first block was digitised
                    offset = time.time() - self._stream.time
                    self._start_ms = (time_info.inputBufferAdcTime + offset) * 1000
                if self._wav is not None:
                    self._wav.writeframes(bytes(indata))

        self._stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                         device=self.device, callback=callback)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        with self._lock:
            if self._wav is not None:
                self._wav.close()
                self._wav = None
        self.store.write_json("audio.json", {"start_epoch_ms": self._start_ms, "samplerate": SAMPLE_RATE})
