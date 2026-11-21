"""Disposable PostgreSQL cluster, isolated across command runs (including roles)."""
from __future__ import annotations

import secrets
import shutil
import socket
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from .context import ToolMissing


@contextmanager
def fresh_postgres():
    binaries = {}
    for name in ("initdb", "pg_ctl"):
        executable = shutil.which(name)
        if not executable:
            raise ToolMissing(name)
        binaries[name] = executable

    def run(*argv):
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=90)
        except subprocess.TimeoutExpired as exc:
            raise OSError(f"{Path(argv[0]).name}: timed out") from exc
        if result.returncode:
            raise OSError(f"{Path(argv[0]).name}: {(result.stderr or result.stdout)[-1500:]}")

    with TemporaryDirectory(prefix="pqe-pg-") as root:
        data = str(Path(root) / "data")
        password = secrets.token_hex(24)
        pwfile = Path(root) / "pw"
        pwfile.write_text(password)
        run(binaries["initdb"], "-D", data, "-U", "pqe", "--auth=scram-sha-256", "--no-locale", f"--pwfile={pwfile}")
        pwfile.unlink()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = str(probe.getsockname()[1])
        opts = f"-p {port} -c listen_addresses=127.0.0.1 -c unix_socket_directories=''"
        run(binaries["pg_ctl"], "-D", data, "-l", str(Path(root) / "server.log"), "-o", opts, "-w", "start")
        try:
            yield {"PGHOST": "127.0.0.1", "PGPORT": port, "PGDATABASE": "postgres", "PGUSER": "pqe", "PGPASSWORD": password,
                   "DATABASE_URL": f"postgresql://pqe:{password}@127.0.0.1:{port}/postgres"}
        finally:
            run(binaries["pg_ctl"], "-D", data, "-m", "fast", "-w", "stop")
