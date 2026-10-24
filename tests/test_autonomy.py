"""The engine on projects it has never seen: classify → plan → contract → run."""

from __future__ import annotations

import copy
import json
import shutil
import tomllib
from pathlib import Path

import pytest

from pqe.config import parse_config
from pqe.discovery.classify import Kind, classify
from pqe.discovery.profile import discover
from pqe.model import Severity
from pqe.planning import ai
from pqe.planning.auto import GENERATED_MARKER, run_auto
from pqe.planning.generate import build_plan, sandbox_env
from pqe.planning.tomlw import dumps
from pqe.runners.mobile import audit_manifest, audit_plist, parse_monkey, parse_start_time, parse_xcodebuild
from pqe.runners.web import parse_lighthouse, parse_page

from .conftest import FIXTURES, git, write

PROJECTS = FIXTURES / "projects"

EXPECTED = {
    "static_site": [("static_site", ".")],
    "fastapi_service": [("api_service", ".")],
    "next_app": [("web_app", ".")],
    "android_app": [("android_app", ".")],
    "flutter_app": [("flutter_app", ".")],
    "rn_app": [("react_native_app", ".")],
    "ios_app": [("ios_app", ".")],
    "python_cli": [("cli_tool", ".")],
    "mql5_ea": [("trading_ea", "Experts")],
    "monorepo": [("api_service", "backend"), ("react_native_app", "mobile")],
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_classification_of_every_kind(name):
    result = classify(PROJECTS / name)
    assert [(c.kind.value, c.path) for c in result.components] == EXPECTED[name]
    assert all(c.evidence for c in result.components)  # never classified without evidence


def test_classifier_boot_hints():
    api = classify(PROJECTS / "fastapi_service").first(Kind.API_SERVICE)
    assert api.start_command[:4] == ["python", "-m", "uvicorn", "shop.api:app"] and "{port}" in api.start_command
    assert api.health_path == "/health" and api.openapi_path == "/openapi.json" and api.extra["python_root"] == "src"
    web = classify(PROJECTS / "next_app").first(Kind.WEB_APP)
    assert web.start_command == ["npm", "run", "dev", "--", "-p", "{port}", "-H", "127.0.0.1"] and web.install_command == ["npm", "ci"]
    assert classify(PROJECTS / "android_app").first(Kind.ANDROID_APP).app_id == "com.example.notes"
    assert classify(PROJECTS / "flutter_app").first(Kind.FLUTTER_APP).app_id == "com.example.wallet"
    assert classify(PROJECTS / "rn_app").first(Kind.REACT_NATIVE_APP).app_id == "com.example.chat"
    assert classify(PROJECTS / "ios_app").first(Kind.IOS_APP).extra["scheme"] == "Todo"


def test_factory_app_and_legacy_folders(tmp_path):
    write(tmp_path, "pyproject.toml", '[project]\nname = "x"\n')
    write(tmp_path, "server/app.py", "from fastapi import FastAPI\napp = FastAPI()\n@app.get('/a')\ndef a(): ...\n")
    write(tmp_path, "src/core/api.py", "from fastapi import FastAPI\n\ndef create_app():\n    app = FastAPI()\n"
          "    @app.get('/health')\n    def h(): ...\n    @app.post('/x')\n    def x(): ...\n    return app\n\napp = create_app()\n")
    api = classify(tmp_path).first(Kind.API_SERVICE)
    assert api.app_id == "core.api:app"  # factory recognised, legacy server/ folder loses
    assert "chosen over" in api.evidence[-1]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_generated_contract_is_valid_and_round_trips(name):
    root = PROJECTS / name
    plan, contract = build_plan(classify(root), discover(root))
    text = dumps(contract, "header")
    assert tomllib.loads(text) == contract
    config = parse_config(tomllib.loads(text), root)
    assert config.goals and plan.suites
    ids = {s.id for s in plan.suites}
    assert {"secrets", "dependencies", "own-tests"} <= ids


def test_plans_follow_the_kind():
    def plan_of(name):
        root = PROJECTS / name
        return build_plan(classify(root), discover(root))

    plan, contract = plan_of("static_site")
    assert contract["ui"]["enabled"] and contract["web"]["enabled"] and contract["ui"]["expect_dir"] == "rtl"
    assert {"ui-interaction", "web-links", "web-seo", "web-lighthouse", "ui-a11y"} <= {s.id for s in plan.suites}

    plan, contract = plan_of("android_app")
    assert contract["android"]["package"] == "com.example.notes"
    monkey = next(s for s in plan.suites if s.id == "android-monkey")
    assert monkey.target == "android"
    assert any(g["id"] == "U-MOB-1" and g["hard_gate"] and g["target"] == "android" for g in contract["goals"])

    plan, contract = plan_of("rn_app")
    assert contract["android"]["project_dir"] == "android"
    assert any(s["id"].startswith("expo-prebuild") for s in contract["setup"])

    plan, contract = plan_of("mql5_ea")
    assert any("Windows" in g for g in plan.gaps)
    assert next(s for s in plan.suites if s.id == "ea-backtest").status == "HUMAN"

    plan, contract = plan_of("monorepo")
    assert contract["api"]["cwd"] == "backend" and "android" in contract


def test_sandbox_env_never_uses_real_provider_keys(tmp_path):
    write(tmp_path, ".env.example", "OPENAI_API_KEY=sk-xxx\nAPP_SECRET=\nCORE_TOKEN=change-me\nDATA_DIR=./data\n"
          "DATABASE_URL=sqlite:///./app.db\nPORT=8000\n")
    env, secrets, temps = sandbox_env(tmp_path, ".")
    assert env["OPENAI_API_KEY"] == ""  # paid provider: never called from the sandbox
    assert set(secrets) == {"APP_SECRET", "CORE_TOKEN"} and env["CORE_TOKEN"] == "${CORE_TOKEN}"
    assert "DATA_DIR" in temps and env["DATABASE_URL"].startswith("sqlite:///${PQE_SQLITE_DIR}")


# --------------------------------------------------------------------------- AI


def _site_contract():
    root = PROJECTS / "static_site"
    classification = classify(root)
    _, contract = build_plan(classification, discover(root))
    return root, classification, contract


def test_ai_proposals_are_validated_and_never_gates():
    root, classification, contract = _site_contract()
    answer = {
        "product_goal": "Visitors find the contact page",
        "goals": [{"id": "D-01", "title": "Visitors reach contact from home", "source": "README.md#Goal",
                   "proven_by": ["ui.journey.reach-contact"]},
                  {"id": "D-02", "title": "Unprovable claim", "source": "README.md", "proven_by": ["ui.journey.nope"]}],
        "ui_journeys": [
            {"id": "Reach Contact", "title": "Home → contact", "source": "README.md#Goal",
             "steps": [{"action": "click", "selector": "text=تماس"}, {"action": "expect_url", "value": "contact"}]},
            {"id": "broken", "title": "invalid", "steps": [{"action": "teleport", "selector": "x"}]},
        ],
        "api_scenarios": [{"id": "x", "title": "no api here", "steps": [{"method": "GET", "path": "/"}]}],
    }
    merged, report = ai.propose(copy.deepcopy(contract), classification, discover(root), root,
                                transport=lambda prompt: "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```")
    assert report["status"] == "OK"
    journeys = merged["ui"]["journeys"]
    assert [j["id"] for j in journeys] == ["reach-contact"] and journeys[0]["proposed"] is True
    assert any("teleport" in r for r in report["rejected"])
    goals = {g["id"]: g for g in merged["goals"] if g.get("proposed")}
    assert goals["D-01"]["criteria"] == [{"kind": "check", "check": "ui.journey.reach-contact"}]
    assert goals["D-02"]["criteria"][0]["kind"] == "attestation"  # no runnable proof → human must confirm
    assert not any(g.get("hard_gate") for g in goals.values())
    assert "api" not in merged  # never invents a component the project does not have
    parse_config(merged, root)


def test_ai_failure_keeps_the_deterministic_contract():
    root, classification, contract = _site_contract()

    def broken(prompt):
        raise RuntimeError("model unavailable")

    merged, report = ai.propose(copy.deepcopy(contract), classification, discover(root), root, transport=broken)
    assert merged == contract and report["status"] == "ERROR"


def test_prompt_contains_docs_and_page_snapshot():
    root, classification, _ = _site_contract()
    snap = ai.snapshot_html("<html><script>x=1</script><body><h1>خوش آمدید</h1><button aria-label='ارسال'>ثبت</button></body></html>")
    assert "خوش آمدید" in snap and "ارسال" in snap and "x=1" not in snap
    prompt = ai.build_prompt(classification, discover(root), ai.collect_docs(root), "", {"/": snap})
    assert "Visitors can read about the company" in prompt and "خوش آمدید" in prompt


# ----------------------------------------------------------------------- parsers


def test_manifest_audit_finds_classic_mobile_flaws():
    text = (PROJECTS / "android_app/app/src/main/AndroidManifest.xml").read_text()
    findings, metrics = audit_manifest(text, "AndroidManifest.xml")
    titles = {f.title: f.severity for f in findings}
    assert titles['android:debuggable="true" is hard-coded in the manifest'] == Severity.SEV1
    assert titles["Cleartext (HTTP) traffic is allowed app-wide"] == Severity.SEV2
    assert titles["Exported service without a permission: .SyncService"] == Severity.SEV2
    assert not any("MainActivity" in t for t in titles)  # the launcher must be exported
    assert metrics["dangerous_permissions"] == ["READ_CONTACTS"]


def test_monkey_and_start_parsers():
    out = ("Events injected: 4210\n// CRASH: com.example.notes (pid 4242)\n// Long Msg: java.lang.NullPointerException\n"
           "// NOT RESPONDING: com.example.notes (pid 4300)\n** Monkey aborted due to error.\n")
    data = parse_monkey(out)
    assert data["crashes"] == ["com.example.notes (pid 4242)"] and len(data["anrs"]) == 1
    assert data["events_injected"] == 4210 and data["aborted"]
    assert parse_start_time("Status: ok\nTotalTime: 812\nWaitTime: 820\n") == 812


def test_xcodebuild_and_plist_parsers():
    out = ("Test Case '-[TodoTests testAdd]' passed (0.012 seconds).\n"
           "Test Case '-[TodoTests testDelete]' failed (0.031 seconds).\n")
    cases = parse_xcodebuild(out)
    assert [(c.id, c.status.value) for c in cases] == [("TodoTests::testAdd", "PASS"), ("TodoTests::testDelete", "FAIL")]
    import plistlib

    data = plistlib.loads((PROJECTS / "ios_app/Todo/Info.plist").read_bytes())
    assert [f.title for f in audit_plist(data, "Info.plist")] == ["App Transport Security disabled (NSAllowsArbitraryLoads = true)"]


def test_web_parsers():
    facts = parse_page("http://h/", '<html lang="fa"><head><title> T </title><meta name="viewport" content="x">'
                       '<link rel="stylesheet" href="/a.css"></head><body><h1>x</h1><a href="/b">b</a><img src="c.png"></body></html>')
    assert (facts.title, facts.lang, facts.viewport, facts.h1) == ("T", "fa", True, 1)
    assert facts.links == ["/b"] and facts.assets == ["/a.css", "c.png"]
    scores, failing = parse_lighthouse({"categories": {"performance": {"score": 0.91}},
                                        "audits": {"a": {"score": 0, "title": "Bad", "scoreDisplayMode": "binary"},
                                                   "b": {"score": 1, "title": "Good", "scoreDisplayMode": "binary"}}})
    assert scores == {"performance": 91} and failing == [("a", "Bad", 0.0)]


# --------------------------------------------------------------------------- auto


def statuses_of(report: dict) -> dict[str, str]:
    return {c["check_id"]: c["status"] for c in report["checks"]}


def _copy_project(name: str, tmp_path: Path) -> Path:
    root = tmp_path / name
    shutil.copytree(PROJECTS / name, root)
    git(root, "init", "-q")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return root


def test_auto_on_an_unknown_api_project_runs_every_planned_check(tmp_path):
    root = _copy_project("fastapi_service", tmp_path)
    result = run_auto(root, use_ai=False, setup=False)
    report = result.report
    assert result.contract_source == "generated" and result.classification.kinds == ["api_service"]
    contract_text = (root / ".qa/pqe.toml").read_text()
    assert GENERATED_MARKER in contract_text and (root / ".qa/plan.md").exists()
    checks = {c["check_id"]: c for c in report["checks"]}
    assert checks["api.contract"]["status"] == "PASS" and len(checks["api.contract"]["tests"]) >= 3  # endpoints from OpenAPI
    robustness = checks["api.robustness"]
    assert any("deeply nested" in t["name"] for t in robustness["tests"])  # the hostile probe was sent
    # Whether this app fails it depends on the Python runtime (3.13 answers 500, 3.11 refuses with 4xx);
    # either way every 5xx must surface as a finding. Defect detection itself is proven deterministically
    # in test_api_runner with a deliberately vulnerable server.
    failed = [t for t in robustness["tests"] if t["status"] == "FAIL"]
    assert len(robustness["findings"]) >= len(failed)
    assert checks["performance.latency"]["status"] == "UNCALIBRATED"
    expected = {"CONDITIONAL", "NO_GO"} if robustness["findings"] else {"GO", "CONDITIONAL", "NO_GO"}
    assert report["decision"]["decision"] in expected  # a real 5xx can never yield GO


def test_auto_never_overwrites_a_human_owned_contract(tmp_path):
    root = _copy_project("fastapi_service", tmp_path)
    run_auto(root, use_ai=False, setup=False, target="android")  # first run writes a generated contract
    text = (root / ".qa/pqe.toml").read_text().replace(GENERATED_MARKER, "owned by A.del")
    (root / ".qa/pqe.toml").write_text(text)
    result = run_auto(root, use_ai=False, setup=False, target="android")
    assert result.contract_source == "existing"
    assert (root / ".qa/pqe.toml").read_text() == text


def test_targets_split_the_work_between_ci_jobs(tmp_path):
    root = _copy_project("android_app", tmp_path)
    android = run_auto(root, use_ai=False, setup=False, target="android").report
    ran = {c["check_id"].split(".")[0] for c in android["checks"]}
    assert ran == {"android"}
    assert {g["goal_id"] for g in android["goals"]} == {"U-MOB-1", "U-MOB-2", "U-MOB-3"}
    assert statuses_of(android)["android.lint"] == "ERROR"  # stub build: no lint report means nothing was verified
    statuses = {c["check_id"]: c["status"] for c in android["checks"]}
    assert statuses["android.monkey"] == "SKIPPED"  # no emulator here: skipped with the reason, never passed
    web = run_auto(root, use_ai=False, setup=False, target="web").report
    assert any(c["check_id"] == "android.manifest" for c in web["checks"]) is False  # android runner belongs to the android job
    assert "U-MOB-1" not in {g["goal_id"] for g in web["goals"]}
