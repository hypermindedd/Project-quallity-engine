"""Execution context shared by all runners: process execution, file walking,
secret generation and environment interpolation.

Commands are always executed as argument lists (never through a shell) so a
value in the contract can never be interpreted as shell syntax.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import secrets as _secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

log = logging.getLogger("pqe")

ALWAYS_EXCLUDED_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    ".next",
    ".wrangler",
    ".turbo",
    ".cache",
    ".pqe-engine",
    "pqe-report",
}

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class ProcResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def tail(self, lines: int = 40) -> str:
        text = (self.stdout + "\n" + self.stderr).strip().splitlines()
        return "\n".join(text[-lines:])


class ToolMissing(RuntimeError):
    """A required executable is not installed."""


@dataclass
class RunContext:
    config: Config
    out_dir: Path
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def root(self) -> Path:
        return self.config.root

    # ------------------------------------------------------------------ env
    def secret(self, name: str) -> str:
        """Return an engine-generated secret, creating it on first use."""
        if name not in self.secrets:
            self.secrets[name] = _secrets.token_urlsafe(36)
        return self.secrets[name]

    def interpolate(self, value: str, extra: dict[str, str] | None = None) -> str:
        """Expand ``${NAME}`` from extra vars, generated secrets, then os.environ.

        Unknown names are left untouched so the failure is visible downstream
        instead of silently becoming an empty string.
        """
        scope = {**os.environ, **self.secrets, **(extra or {})}

        def repl(match: re.Match[str]) -> str:
            return scope.get(match.group(1), match.group(0))

        return _VAR.sub(repl, value)

    def build_env(self, overrides: dict[str, str], extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        # Bytecode caches are keyed by mtime (1s) + size; an edit of equal size within
        # the same second (exactly what an automated fixer does) would run stale code.
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        env.update(self.secrets)
        for key, value in overrides.items():
            env[key] = self.interpolate(value, extra)
        return env

    def redact(self, text: str) -> str:
        """Remove generated secrets from any text that may end in a report."""
        for value in self.secrets.values():
            if value:
                text = text.replace(value, "***")
        return text

    # ----------------------------------------------------------- processes
    def which(self, tool: str) -> str | None:
        return shutil.which(tool)

    def resolve_argv(self, argv: list[str], extra: dict[str, str] | None = None) -> list[str]:
        resolved = [self.interpolate(a, extra) for a in argv]
        if resolved and resolved[0] == "python":
            # Use the interpreter the project environment provides, if any.
            resolved[0] = shutil.which("python") or shutil.which("python3") or "python3"
        return resolved

    def run(
        self,
        argv: list[str],
        cwd: str | Path = ".",
        env: dict[str, str] | None = None,
        timeout_s: int = 900,
        stdin: str | None = None,
    ) -> ProcResult:
        argv = self.resolve_argv(argv)
        workdir = (self.root / cwd).resolve() if not Path(cwd).is_absolute() else Path(cwd)
        if not shutil.which(argv[0]) and not Path(argv[0]).exists():
            raise ToolMissing(argv[0])
        started = time.monotonic()
        log.debug("exec %s (cwd=%s)", argv, workdir)
        try:
            proc = subprocess.run(  # noqa: S603 - argv list, no shell
                argv,
                cwd=workdir,
                env=env if env is not None else self.build_env({}),
                input=stdin,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            return ProcResult(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - started)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return ProcResult(argv, -1, out, err, time.monotonic() - started, timed_out=True)

    # ------------------------------------------------------------- files
    def is_excluded(self, rel: str) -> bool:
        parts = Path(rel).parts
        if any(p in ALWAYS_EXCLUDED_DIRS for p in parts):
            return True
        return any(fnmatch.fnmatch(rel, pattern) for pattern in self.config.project.exclude)

    def iter_files(self, suffixes: tuple[str, ...] | None = None) -> Iterator[Path]:
        """Walk project files, skipping vendored/generated directories."""
        root = self.root
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            dirnames[:] = [
                d for d in dirnames
                if d not in ALWAYS_EXCLUDED_DIRS and not self.is_excluded(os.path.normpath(os.path.join(rel_dir, d)))
            ]
            for name in filenames:
                if suffixes and not name.endswith(suffixes):
                    continue
                rel = os.path.normpath(os.path.join(rel_dir, name))
                if not self.is_excluded(rel):
                    yield root / rel

    def rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def glob(self, patterns: list[str]) -> list[Path]:
        found: dict[str, Path] = {}
        for pattern in patterns:
            for path in self.root.glob(pattern):
                if path.is_file() and not self.is_excluded(self.rel(path)):
                    found[self.rel(path)] = path
        return [found[k] for k in sorted(found)]


def match_any(path: str, patterns: list[str]) -> bool:
    """Glob match where ``**/`` also matches zero directories."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return True
    return False
