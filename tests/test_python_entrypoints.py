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


def test_pip_audit_resolves_unpinned_requirements(ctx_factory, tmp_path, monkeypatch):
    """`fastapi>=0.120,<1` cannot be audited with --no-deps; the runner retries with resolution."""
    import json

    from pqe.context import ProcResult
    from pqe.runners.security import PipAuditRunner

    (tmp_path / "requirements.txt").write_text("fastapi>=0.120,<1\n")
    ctx = ctx_factory('[project]\nname = "x"\n[security]\npip_audit = true\npip_audit_requirements = ["requirements.txt"]\n')
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "--no-deps" in argv:
            return ProcResult(argv, 1, "", "ERROR: requirement fastapi is not pinned to an exact version", 0.1)
        return ProcResult(argv, 0, json.dumps({"dependencies": [{"name": "fastapi", "version": "0.121.0", "vulns": []}]}), "", 0.1)

    monkeypatch.setattr(ctx, "run", fake_run)
    result = PipAuditRunner().execute(ctx)[0]
    assert result.status.value == "PASS" and len(calls) == 2 and "--no-deps" not in calls[1]
