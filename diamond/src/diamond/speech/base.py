"""Pluggable transcribers. All return words with *epoch-ms* timestamps.

Provider choice on macOS (decision record):

* ``mlx``    — mlx-whisper on Apple Silicon (Metal). Local, private, no
               sanctions/network risk, fast with ``large-v3-turbo``. Default.
* ``faster`` — faster-whisper (CTranslate2). Local; CPU on Mac, CUDA on Linux.
* ``openai`` — cloud ``whisper-1`` with word timestamps. Needs network + key.

Persian accuracy with mixed English UI jargon is not assumed: the domain
vocabulary is passed as the initial prompt, and accuracy must be measured on
the reviewer's own voice (see docs/ARCHITECTURE.md, "Speech evaluation").
"""

from __future__ import annotations

import importlib.util
import json
import os
import urllib.request
import uuid
from pathlib import Path
from typing import Protocol

from ..config import SpeechConfig
from ..session import SessionStore, Word


class TranscriptionUnavailable(RuntimeError):
    pass


class Transcriber(Protocol):
    name: str

    def transcribe(self, wav: Path, cfg: SpeechConfig) -> list[tuple[str, float, float, float]]:
        """Return (text, start_s, end_s, probability) relative to the file start."""


def _prompt(cfg: SpeechConfig) -> str:
    return "بازبینی رابط کاربری وب‌سایت: " + "، ".join(cfg.vocabulary)


class MlxTranscriber:
    name = "mlx"

    def transcribe(self, wav: Path, cfg: SpeechConfig) -> list[tuple[str, float, float, float]]:
        import mlx_whisper  # type: ignore[import-not-found]

        repo = cfg.model if "/" in cfg.model else f"mlx-community/whisper-{cfg.model}"
        result = mlx_whisper.transcribe(str(wav), path_or_hf_repo=repo, language=cfg.language,
                                        word_timestamps=True, initial_prompt=_prompt(cfg))
        return [(w["word"], w["start"], w["end"], w.get("probability", 1.0))
                for seg in result.get("segments", []) for w in seg.get("words", [])]


class FasterTranscriber:
    name = "faster"

    def transcribe(self, wav: Path, cfg: SpeechConfig) -> list[tuple[str, float, float, float]]:
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        model = WhisperModel(cfg.model, device="auto", compute_type="int8")
        segments, _ = model.transcribe(str(wav), language=cfg.language, word_timestamps=True,
                                       initial_prompt=_prompt(cfg), vad_filter=True)
        return [(w.word, w.start, w.end, w.probability) for seg in segments for w in (seg.words or [])]


class OpenAITranscriber:
    name = "openai"
    endpoint = "https://api.openai.com/v1/audio/transcriptions"

    def transcribe(self, wav: Path, cfg: SpeechConfig) -> list[tuple[str, float, float, float]]:
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise TranscriptionUnavailable("OPENAI_API_KEY is not set")
        boundary = uuid.uuid4().hex
        fields = {"model": "whisper-1", "response_format": "verbose_json", "language": cfg.language,
                  "prompt": _prompt(cfg), "timestamp_granularities[]": "word"}
        body = b""
        for k, v in fields.items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
                 "Content-Type: audio/wav\r\n\r\n").encode() + wav.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            self.endpoint, data=body, method="POST", headers={
            "Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=600) as resp:  # noqa: S310 - fixed https endpoint
            data = json.loads(resp.read())
        return [(w["word"], w["start"], w["end"], 1.0) for w in data.get("words", [])]


_PROVIDERS = {"mlx": ("mlx_whisper", MlxTranscriber), "faster": ("faster_whisper", FasterTranscriber),
              "openai": (None, OpenAITranscriber)}


def get_transcriber(provider: str) -> Transcriber:
    if provider == "auto":
        for name in ("mlx", "faster"):
            module, cls = _PROVIDERS[name]
            if importlib.util.find_spec(module):
                return cls()
        if os.environ.get("OPENAI_API_KEY"):
            return OpenAITranscriber()
        raise TranscriptionUnavailable(
            "no speech-to-text provider found. On a Mac install: pip install 'diamond-engine[stt-mlx]'")
    if provider not in _PROVIDERS:
        raise TranscriptionUnavailable(f"unknown speech provider '{provider}'")
    module, cls = _PROVIDERS[provider]
    if module and not importlib.util.find_spec(module):
        raise TranscriptionUnavailable(f"speech provider '{provider}' is not installed ({module})")
    return cls()


def transcribe_session(store: SessionStore, cfg: SpeechConfig) -> list[Word]:
    """Transcribe ``audio.wav`` and store ``transcript.json`` with epoch-ms words."""
    wav = store.root / "audio.wav"
    audio = store.read_json("audio.json", {})
    if not wav.is_file() or not audio.get("start_epoch_ms"):
        raise TranscriptionUnavailable("this session has no audio recording")
    transcriber = get_transcriber(cfg.provider)
    t0 = float(audio["start_epoch_ms"])
    words = [Word(text.strip(), t0 + s * 1000, t0 + e * 1000, float(p))
             for text, s, e, p in transcriber.transcribe(wav, cfg) if text.strip()]
    store.write_json("transcript.json", {"provider": transcriber.name, "model": cfg.model,
                                         "words": [w.__dict__ for w in words]})
    return words
