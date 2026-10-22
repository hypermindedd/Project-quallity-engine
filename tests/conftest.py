from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from pqe.config import Config, parse_config
from pqe.context import RunContext

FIXTURES = Path(__file__).parent / "fixtures"


def make_config(tmp_path: Path, toml_text: str) -> Config:
    import tomllib

    data = tomllib.loads(textwrap.dedent(toml_text))
    return parse_config(data, tmp_path.resolve())


@pytest.fixture
def ctx_factory(tmp_path: Path):
    def factory(toml_text: str) -> RunContext:
        config = make_config(tmp_path, toml_text)
        out = tmp_path / ".qa" / "out" / "test"
        out.mkdir(parents=True, exist_ok=True)
        return RunContext(config=config, out_dir=out)

    return factory


def write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    return path


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout
