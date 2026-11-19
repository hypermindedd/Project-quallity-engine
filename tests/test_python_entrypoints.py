"""Python web services: which entrypoint is the real app, and does it serve pages?"""

from pathlib import Path

from pqe.discovery.classify import Kind, classify
from pqe.discovery.profile import discover
from pqe.planning.generate import build_plan


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8")
    return tmp_path


APP_UI = '''from fastapi import FastAPI
from fastapi.responses import FileResponse
app = FastAPI()
@app.get("/")
def index():
    return FileResponse("static/index.html")
@app.get("/api/health")
def health():
    return {"ok": True}
'''

CORE = '''from fastapi import FastAPI
app = FastAPI()
@app.get("/a")
def a(): ...
@app.get("/b")
def b(): ...
@app.get("/c")
def c(): ...
'''


def test_the_app_the_project_starts_wins_and_its_pages_are_tested(tmp_path):
    root = _project(tmp_path, {
        "server/__init__.py": "", "server/app.py": APP_UI,
        "src/core/__init__.py": "", "src/core/app.py": CORE,
        "static/index.html": "<html lang='fa' dir='rtl'></html>",
        "requirements.txt": "fastapi\n",
        "START_DEMO.command": "#!/bin/bash\npython -m uvicorn server.app:app --host 127.0.0.1 --port 8775\n",
    })
    comps = [c for c in classify(root).components if c.kind == Kind.API_SERVICE]
    assert comps and comps[0].app_id == "server.app:app"
    assert any("started by START_DEMO.command" in e for e in comps[0].evidence)
    assert comps[0].extra.get("serves_ui") == "true"
    _, contract = build_plan(classify(root), discover(root))
    assert contract["ui"]["enabled"] and contract["api"]["enabled"]


def test_pure_api_gets_no_ui_section(tmp_path):
    root = _project(tmp_path, {"main.py": CORE, "requirements.txt": "fastapi\n"})
    _, contract = build_plan(classify(root), discover(root))
    assert "ui" not in contract and contract["api"]["enabled"]
