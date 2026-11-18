"""Local speech-model store with a resumable, parallel, verified downloader.

Why: on some networks a single connection to Hugging Face crawls (tens of
KB/s), which makes a 1.6 GB model unusable. Splitting the file into ranges
over many connections multiplies throughput; every chunk is kept on disk so
an interrupted download resumes, and the result is checked against the
SHA-256 published by the hub before it is used.

Layout: ``$DIAMOND_MODELS_DIR`` (default ``~/.diamond-engine/models``)/<name>/
with a ``.complete`` marker once verified.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "small": "mlx-community/whisper-small-mlx",
}
CHUNK = 8 * 1024 * 1024
USER_AGENT = "diamond-engine/0.1 (+model fetch)"


class ModelFetchError(RuntimeError):
    pass


def models_dir() -> Path:
    return Path(os.environ.get("DIAMOND_MODELS_DIR", Path.home() / ".diamond-engine" / "models"))


def endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")


def local_model(name: str) -> Path | None:
    """Path of a verified local copy, or None."""
    path = models_dir() / name.replace("/", "--")
    return path if (path / ".complete").is_file() else None


def _get(url: str, headers: dict[str, str] | None = None, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})  # noqa: S310 - https endpoint
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def _file_list(repo: str) -> list[dict]:
    data = json.loads(_get(f"{endpoint()}/api/models/{repo}/tree/main"))
    return [f for f in data if f.get("type") == "file"]


def _download_ranges(url: str, size: int, parts_dir: Path, connections: int,
                     progress: Callable[[int, int], None]) -> None:
    parts_dir.mkdir(parents=True, exist_ok=True)
    ranges = [(i, start, min(start + CHUNK, size) - 1) for i, start in enumerate(range(0, size, CHUNK))]
    done_bytes = sum((parts_dir / f"{i:05d}").stat().st_size for i, s, e in ranges
                     if (parts_dir / f"{i:05d}").is_file() and (parts_dir / f"{i:05d}").stat().st_size == e - s + 1)
    lock = threading.Lock()
    state = {"done": done_bytes}

    def fetch(item: tuple[int, int, int]) -> None:
        i, start, end = item
        part = parts_dir / f"{i:05d}"
        want = end - start + 1
        if part.is_file() and part.stat().st_size == want:
            return
        for attempt in range(8):
            try:
                data = _get(url, {"Range": f"bytes={start}-{end}"}, timeout=120)
                if len(data) != want:
                    raise ModelFetchError(f"chunk {i}: got {len(data)} bytes, expected {want}")
                tmp = part.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(part)
                with lock:
                    state["done"] += want
                    progress(state["done"], size)
                return
            except Exception:  # noqa: BLE001 - retried with backoff, re-raised at the end
                if attempt == 7:
                    raise
                time.sleep(min(2 ** attempt, 30))

    with ThreadPoolExecutor(max_workers=connections) as pool:
        list(pool.map(fetch, ranges))


def fetch_model(name: str = "large-v3-turbo", connections: int = 16,
                progress: Callable[[int, int], None] | None = None) -> Path:
    """Download and verify a model; returns its local directory. Safe to re-run (resumes)."""
    repo = REPOS.get(name, name)
    target = models_dir() / name.replace("/", "--")
    if (target / ".complete").is_file():
        return target
    target.mkdir(parents=True, exist_ok=True)
    progress = progress or _print_progress
    for f in _file_list(repo):
        path, size = f["path"], int(f.get("size") or 0)
        sha = (f.get("lfs") or {}).get("oid", "")
        dest = target / path
        if dest.is_file() and dest.stat().st_size == size:
            continue
        url = f"{endpoint()}/{repo}/resolve/main/{path}"
        if size < CHUNK:
            dest.write_bytes(_get(url))
            continue
        parts = target / ".parts" / path
        _download_ranges(url, size, parts, connections, progress)
        digest = hashlib.sha256()
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        with tmp.open("wb") as out:
            for part in sorted(parts.iterdir()):
                if part.suffix == ".tmp":
                    continue
                data = part.read_bytes()
                digest.update(data)
                out.write(data)
        if sha and digest.hexdigest() != sha:
            tmp.unlink(missing_ok=True)
            for part in parts.iterdir():
                part.unlink()
            raise ModelFetchError(f"{path}: checksum mismatch; parts discarded, run again")
        tmp.replace(dest)
        for part in parts.iterdir():
            part.unlink()
        parts.rmdir()
    (target / ".complete").write_text(json.dumps({"repo": repo, "fetched": time.time()}))
    return target


_last = {"t": 0.0}


def _print_progress(done: int, total: int) -> None:
    now = time.time()
    if now - _last["t"] >= 3 or done >= total:
        _last["t"] = now
        print(f"\r  {done / 1e6:8.1f} / {total / 1e6:.1f} MB ({100 * done / total:5.1f}%)", end="", file=sys.stderr, flush=True)
