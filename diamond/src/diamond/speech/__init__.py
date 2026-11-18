"""Speech-to-text with word-level timestamps, plus the Persian UI lexicon."""

from .base import TranscriptionUnavailable, get_transcriber, transcribe_session

__all__ = ["TranscriptionUnavailable", "get_transcriber", "transcribe_session"]
