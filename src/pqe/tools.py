"""Self-provisioning of helper tools the engine needs (cached per machine)."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("pqe")


def tools_dir() -> Path:
    path = Path(os.environ.get("PQE_TOOLS_DIR", Path.home() / ".cache" / "pqe" / "tools"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def npm_file(package: str, rel_file: str) -> Path | None:
    """Return a file from an npm package, installing it into the tools cache once."""
    base = tools_dir() / "npm"
    target = base / "node_modules" / rel_file
    if target.exists():
        return target
    if not shutil.which("npm"):
        return None
    log.info("installing helper tool %s", package)
    proc = subprocess.run(["npm", "install", "--no-save", "--no-package-lock", "--prefix", str(base), package],  # noqa: S603, S607
                          capture_output=True, text=True, timeout=600, check=False)
    if proc.returncode:
        log.warning("could not install %s: %s", package, proc.stderr[-300:])
        return None
    return target if target.exists() else None
