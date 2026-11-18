"""The one-command launcher: engine discovery and project detection (no installs)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "bin" / "diamond-run"
ENGINE = Path(__file__).resolve().parents[1]


def doctor(project: Path, tmp_path: Path, *extra: str, **env: str) -> dict:
    full_env = {**os.environ, "HOME": str(tmp_path / "home"), "DIAMOND_HOME": str(tmp_path / "engine"), **env}
    (tmp_path / "home").mkdir(exist_ok=True)
    out = subprocess.run(["bash", str(LAUNCHER), str(project), "--doctor", *extra], env=full_env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return dict(line.split(":", 1) for line in out.stdout.splitlines())


def make_node(tmp_path: Path, deps: dict, scripts: dict, lock: str = "") -> Path:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"dependencies": deps, "scripts": scripts}))
    if lock:
        (project / lock).write_text("")
    return project


@pytest.mark.parametrize("deps, lock, expected", [
    ({"next": "15"}, "pnpm-lock.yaml", "node:next:dev:pnpm:port 3000"),
    ({"vite": "6", "react": "19"}, "", "node:vite:dev:npm:port 5173"),
    ({"astro": "5"}, "yarn.lock", "node:astro:dev:yarn:port 4321"),
])
def test_detects_node_projects(tmp_path, deps, lock, expected):
    info = doctor(make_node(tmp_path, deps, {"dev": "x"}, lock), tmp_path, DIAMOND_SRC=str(ENGINE))
    assert info["target"].strip() == expected


def test_detects_static_site(tmp_path):
    site = Path(__file__).parent / "fixtures" / "site"
    info = doctor(site, tmp_path, DIAMOND_SRC=str(ENGINE))
    assert info["target"].strip() == f"static:{site}"


def test_explicit_url_and_smoke_mode(tmp_path):
    info = doctor(tmp_path, tmp_path, "--url", "http://localhost:9999", "--smoke", "5", DIAMOND_SRC=str(ENGINE))
    assert info["target"].strip() == "url:http://localhost:9999"
    assert info["microphone"].strip() == "off" and info["mode"].strip() == "automatic 5s"


def test_engine_found_in_synced_drive_folder(tmp_path):
    drive = tmp_path / "home" / "Library" / "CloudStorage" / "GoogleDrive-a@b.c" / "My Drive" / "Diamondengine"
    drive.mkdir(parents=True)
    (drive / "Diamondengine-src-v0.1.0-aaaa.zip").write_bytes(b"old")
    newest = drive / "Diamondengine-src-v0.2.0-bbbb.zip"
    newest.write_bytes(b"new")
    os.utime(drive / "Diamondengine-src-v0.1.0-aaaa.zip", (1, 1))
    info = doctor(tmp_path, tmp_path)
    assert info["engine source"].strip().startswith(f"zip → {newest}")


def test_falls_back_to_github(tmp_path):
    info = doctor(tmp_path, tmp_path)
    assert info["engine source"].strip().startswith("git → https://github.com/hypermindedd/")


@pytest.mark.parametrize("files, expected", [
    ({"server/app.py": "from fastapi import FastAPI\napp = FastAPI()\n"}, "python:fastapi|server.app:app"),
    ({"main.py": "from fastapi import FastAPI\napi: FastAPI = FastAPI(title='x')\n"}, "python:fastapi|main:api"),
    ({"web.py": "from flask import Flask\napplication = Flask(__name__)\n"}, "python:flask|web"),
    ({"manage.py": "", "mysite/settings.py": ""}, "python:django|manage.py"),
    ({".venv/lib/x.py": "app = FastAPI()\n", "index.html": "<h1>x</h1>"}, "static:{project}"),
])
def test_detects_python_web_apps(tmp_path, files, expected):
    project = tmp_path / "proj"
    for rel, text in files.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_text(text)
    info = doctor(project, tmp_path, DIAMOND_SRC=str(ENGINE))
    assert info["target"].strip() == expected.format(project=project)
