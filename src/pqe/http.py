"""Minimal, dependency-free HTTP client and managed server process.

The engine boots the application under test itself, in an isolated
environment (fresh temporary data directories, generated secrets, free port),
so probing can never touch real data.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .context import RunContext


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: float
    error: str = ""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body or b"null")

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), "")


class HttpClient:
    def __init__(self, base_url: str, timeout_s: float = 30.0) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(f"invalid base_url: {base_url!r}")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.timeout_s = timeout_s

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        raw_body: bytes | None = None,
    ) -> Response:
        body = raw_body
        hdrs = {"Accept": "application/json, text/html;q=0.9, */*;q=0.1", "User-Agent": "pqe/1"}
        if json_body is not None and raw_body is None:
            body = json.dumps(json_body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif raw_body is not None:
            hdrs.setdefault("Content-Type", "application/json")
        hdrs.update(headers or {})
        conn_cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(self.host, self.port, timeout=self.timeout_s)
        started = time.perf_counter()
        try:
            conn.request(method, self.prefix + path, body=body, headers=hdrs)
            resp = conn.getresponse()
            data = resp.read()
            elapsed = (time.perf_counter() - started) * 1000
            return Response(resp.status, {k.lower(): v for k, v in resp.getheaders()}, data, elapsed)
        except (OSError, http.client.HTTPException) as exc:
            return Response(0, {}, b"", (time.perf_counter() - started) * 1000, error=f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class ManagedServer:
    """Start the application under test and stop it reliably."""

    ctx: RunContext
    argv: list[str]
    cwd: str
    env: dict[str, str]
    temp_dir_vars: list[str]
    ready_path: str
    ready_timeout_s: int
    log_name: str
    base_url: str = ""
    process: subprocess.Popen[bytes] | None = None
    _temps: list[str] = field(default_factory=list)
    _log: Any = None

    def start(self) -> None:
        port = free_port()
        extra = {"PQE_PORT": str(port)}
        for var in self.temp_dir_vars:
            path = tempfile.mkdtemp(prefix=f"pqe-{var.lower()}-")
            self._temps.append(path)
            extra[var] = path
        env = self.ctx.build_env(self.env, extra)
        env.update(extra)
        argv = [a.replace("{port}", str(port)) for a in self.ctx.resolve_argv(self.argv, extra)]
        log_path = self.ctx.out_dir / self.log_name
        self._log = log_path.open("wb")
        workdir = self.ctx.root / self.cwd
        self.process = subprocess.Popen(  # noqa: S603 - argv list, no shell
            argv, cwd=workdir, env=env, stdout=self._log, stderr=subprocess.STDOUT, start_new_session=True
        )
        self.base_url = f"http://127.0.0.1:{port}"
        self._wait_ready()

    def _wait_ready(self) -> None:
        client = HttpClient(self.base_url, timeout_s=5)
        deadline = time.monotonic() + self.ready_timeout_s
        last = ""
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"application exited with code {self.process.returncode} before becoming ready:\n{self.log_tail()}")
            resp = client.request("GET", self.ready_path)
            if resp.status and resp.status < 500:
                return
            last = resp.error or f"HTTP {resp.status}"
            time.sleep(0.3)
        raise RuntimeError(f"application not ready after {self.ready_timeout_s}s ({last}):\n{self.log_tail()}")

    def log_tail(self, lines: int = 40) -> str:
        if self._log:
            self._log.flush()
        path = self.ctx.out_dir / self.log_name
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return self.ctx.redact("\n".join(text[-lines:]))

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self._log:
            self._log.close()
        log_path = self.ctx.out_dir / self.log_name
        if log_path.exists():  # never leave generated secrets in an artifact
            log_path.write_text(self.ctx.redact(log_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        for path in self._temps:
            shutil.rmtree(path, ignore_errors=True)

    def __enter__(self) -> ManagedServer:
        try:
            self.start()
        except Exception:
            self.stop()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def dotted_get(data: Any, path: str) -> Any:
    """``state.items.0.id`` style lookup; raises KeyError when absent."""
    current = data
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            if part not in current:
                raise KeyError(path)
            current = current[part]
        else:
            raise KeyError(path)
    return current


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct / 100
    lower = int(k)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower)


def ensure_path(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path
