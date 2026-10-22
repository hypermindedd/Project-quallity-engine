"""Discovery → draft contract → full run → reports, and the repair loop guard."""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET

import pytest

from pqe.cli import main
from pqe.config import load_config
from pqe.discovery.profile import discover
from pqe.orchestrator import RunOptions, run_pipeline
from pqe.remediation.loop import RemediationError, remediate
from pqe.report.notion import build_page, publish

from .conftest import git, write


def _project(root):
    write(root, "pyproject.toml", '[project]\nname = "demo"\ndependencies = ["fastapi"]\n')
    write(root, "src/demo/__init__.py", "")
    write(root, "src/demo/calc.py", "def add(a, b):\n    return a + b\n")
    write(root, "tests/test_calc.py", "from demo.calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    write(root, "docs/SPEC.md", "# Purpose\n\nAdd numbers correctly.\n\n## REQ-01 add\n## REQ-02 subtract\n## REQ-03 report\n")


def test_discovery_profiles_the_project(tmp_path):
    _project(tmp_path)
    profile = discover(tmp_path)
    assert profile.languages["python"] == 3
    assert "fastapi" in profile.frameworks
    assert profile.python_roots == ["src"] and profile.test_dirs == ["tests"]
    assert profile.requirement_families["REQ"]["distinct_ids"] == 3
    assert any("Add numbers correctly" in g for g in profile.goal_statements)


def test_init_writes_a_contract_that_loads_and_runs(tmp_path, monkeypatch):
    _project(tmp_path)
    assert main(["init", str(tmp_path)]) == 0
    assert main(["init", str(tmp_path)]) == 2  # never overwrites silently
    config = load_config(tmp_path)
    config.pytest.env = {"PYTHONPATH": str(tmp_path / "src")}
    report = run_pipeline(config, RunOptions(only=["functional", "goals"], out_dir=tmp_path / "out"))
    assert report["goals"][0]["status"] == "MET"
    unmapped = next(c for c in report["checks"] if c["check_id"] == "goals.traceability") if config.trace_sources else None
    assert unmapped is None  # draft adds no trace sources by itself; families become TODO goals instead
    assert any(g["goal_id"] == "G-REQ" and g["status"] == "UNVERIFIED" for g in report["goals"])
    out = tmp_path / "out"
    assert {"report.json", "report.md", "junit.xml", "results.sarif"} <= {p.name for p in out.iterdir()}
    ET.fromstring((out / "junit.xml").read_text())
    sarif = json.loads((out / "results.sarif").read_text())
    assert sarif["version"] == "2.1.0"
    markdown = (out / "report.md").read_text()
    assert 'dir="rtl"' in markdown and "تصمیم انتشار" in markdown


def test_failing_test_drives_the_decision(tmp_path):
    _project(tmp_path)
    write(tmp_path, "tests/test_calc.py", "from demo.calc import add\n\ndef test_add():\n    assert add(2, 2) == 5\n")
    main(["init", str(tmp_path)])
    config = load_config(tmp_path)
    config.pytest.env = {"PYTHONPATH": str(tmp_path / "src")}
    report = run_pipeline(config, RunOptions(only=["functional"], out_dir=tmp_path / "o"))
    assert report["decision"]["decision"] == "NO_GO"
    pytest_check = next(c for c in report["checks"] if c["check_id"] == "functional.pytest")
    assert pytest_check["findings"][0]["severity"] == "SEV1"


FIXER = """
import pathlib, sys
brief = sys.stdin.read()
root = pathlib.Path.cwd()
(root / "src/demo/calc.py").write_text("def add(a, b):\\n    return a + b\\n")
if "CHEAT" in (root / "mode").read_text():
    (root / "tests/test_calc.py").write_text("def test_add():\\n    assert True\\n")
"""


@pytest.mark.parametrize("mode", ["honest", "CHEAT"])
def test_remediation_loop_fixes_on_a_branch_and_blocks_cheating(tmp_path, mode):
    _project(tmp_path)
    write(tmp_path, "src/demo/calc.py", "def add(a, b):\n    return a - b\n")
    write(tmp_path, "mode", mode)
    write(tmp_path, "fixer.py", FIXER)
    main(["init", str(tmp_path)])
    toml = (tmp_path / ".qa/pqe.toml").read_text()
    toml += f'\n[remediation]\nmax_iterations = 3\nfixer = ["{sys.executable}", "fixer.py"]\nprotected = [".qa/**", "tests/**"]\n'
    toml = toml.replace("[pytest]\nenabled = true", f'[pytest]\nenabled = true\nenv = {{ PYTHONPATH = "{tmp_path / "src"}" }}')
    (tmp_path / ".qa/pqe.toml").write_text(toml)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    outcome = remediate(load_config(tmp_path), RunOptions(only=["functional"]))
    assert outcome.branch.startswith("pqe/fix-")
    assert git(tmp_path, "rev-parse", "--abbrev-ref", "HEAD").strip() == outcome.branch
    if mode == "honest":
        assert outcome.stopped_because == "no actionable findings remain"
        assert outcome.final_decision != "NO_GO"
    else:
        assert "protected paths" in outcome.stopped_because
        assert outcome.iterations[0]["protected_violations"] == ["tests/test_calc.py"]
        assert "add(2, 3) == 5" in (tmp_path / "tests/test_calc.py").read_text()  # oracle restored
    assert git(tmp_path, "log", "main", "--oneline").count("\n") == 1  # main untouched


def test_remediation_refuses_a_dirty_tree(tmp_path):
    _project(tmp_path)
    main(["init", str(tmp_path)])
    git(tmp_path, "init", "-q")
    with pytest.raises(RemediationError, match="no commits"):
        remediate(load_config(tmp_path), RunOptions())
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    write(tmp_path, "src/demo/calc.py", "# uncommitted edit\n")
    with pytest.raises(RemediationError, match="not clean"):
        remediate(load_config(tmp_path), RunOptions())


def test_notion_page_ticks_met_goals(monkeypatch):
    report = {
        "project": "P", "finished_at": "2026-10-22T10:00:00+00:00",
        "decision": {"decision": "CONDITIONAL", "reasons": ["r"]},
        "goals": [{"goal_id": "G1", "title": "a", "status": "MET", "hard_gate": True},
                  {"goal_id": "G2", "title": "b", "status": "UNVERIFIED", "hard_gate": False}],
        "checks": [{"status": "PASS", "check_id": "c", "summary": "s"}],
    }
    page = build_page(report, "parent")
    todos = [b["to_do"]["checked"] for b in page["children"] if b["type"] == "to_do"]
    assert todos == [True, False]
    sent = {}

    def transport(url, headers, body):
        sent.update(url=url, auth=headers["Authorization"], body=json.loads(body))
        return {"url": "https://notion.so/x"}

    from pqe.config import NotionSpec

    monkeypatch.setenv("NOTION_TOKEN", "ntn_test")
    assert publish(report, NotionSpec(True, "parent"), transport) == "https://notion.so/x"
    assert sent["auth"] == "Bearer ntn_test" and sent["body"]["parent"] == {"page_id": "parent"}
    monkeypatch.delenv("NOTION_TOKEN")
    with pytest.raises(RuntimeError, match="NOTION_TOKEN"):
        publish(report, NotionSpec(True, "parent"), transport)


def test_setup_that_rewrites_tracked_files_is_reported(tmp_path):
    _project(tmp_path)
    write(tmp_path, "config.yaml", "store: ${VAR:-default}\n")
    main(["init", str(tmp_path)])
    toml = (tmp_path / ".qa/pqe.toml").read_text()
    script = "import pathlib; pathlib.Path('config.yaml').write_text('store: default\\\\n')"
    toml = f'[[setup]]\nid = "install"\nargv = ["{sys.executable}", "-c", "{script}"]\n\n' + toml
    (tmp_path / ".qa/pqe.toml").write_text(toml)
    git(tmp_path, "init", "-q")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    report = run_pipeline(load_config(tmp_path), RunOptions(only=["none"], setup=True, out_dir=tmp_path / "o"))
    hermetic = next(c for c in report["checks"] if c["check_id"] == "setup.hermetic")
    assert hermetic["status"] == "FAIL"
    assert hermetic["findings"][0]["title"] == "Setup modified a tracked file: config.yaml"
