import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from pqe.config import ConfigError
from pqe.discovery.classify import classify
from pqe.discovery.profile import discover
from pqe.model import CheckResult, Dimension, Status
from pqe.planning.generate import build_plan
from pqe.postgres import fresh_postgres
from pqe.runners.functional import CommandsRunner
from pqe.runners.ui import UiRunner, launch_options
from .conftest import make_config


def test_plain_node_runs_without_installed_dependencies(tmp_path, ctx_factory):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"start":"node server.mjs","check":"node --check server.mjs","test":"node --test demo.test.mjs"}}))
    (tmp_path / "server.mjs").write_text("import {createServer} from 'node:http';createServer();")
    (tmp_path / "demo.test.mjs").write_text("import test from 'node:test';test('sample',()=>{});")
    classification = classify(tmp_path)
    assert "Node HTTP service candidate: server.mjs" in classification.components[0].evidence
    _, contract = build_plan(classification, discover(tmp_path))
    from pqe.config import parse_config
    ctx = ctx_factory('[project]\nname="sample"')
    ctx.config = parse_config(contract, tmp_path)
    results = CommandsRunner().execute(ctx)
    for name in ("test","check"):
        assert any(r.check_id.endswith("-"+name) and r.status == Status.PASS for r in results)


def test_conflicting_server_contract(tmp_path):
    with pytest.raises(ConfigError,match="choose start_command"):
        make_config(tmp_path,'[project]\nname="sample"\n[api]\nenabled=true\nstart_command=["node","server.mjs"]\nbase_url="http://127.0.0.1:8080"')


@pytest.mark.skipif(not shutil.which("initdb") or not shutil.which("psql"), reason="PostgreSQL binaries required")
def test_fresh_clusters_isolate_schema_and_roles():
    for _ in range(2):
        with fresh_postgres() as env:
            subprocess.run(["psql","-v","ON_ERROR_STOP=1","-c","CREATE ROLE ei_probe; CREATE SCHEMA ei_probe;"],env={**os.environ, **env},check=True)


def test_accessibility_under_strict_csp():
    path=os.environ.get("PQE_AXE_TEST_SOURCE")
    if not path:
        pytest.skip("set PQE_AXE_TEST_SOURCE to the local axe-core distribution")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser=pw.chromium.launch(**launch_options())
        page=browser.new_page()
        page.route("https://fixture.test/",lambda route:route.fulfill(status=200,headers={"Content-Type":"text/html","Content-Security-Policy":"script-src 'self'"},body='<html lang="en"><title>Test</title><button></button><script>window.inlineRan=true</script></html>'))
        page.goto("https://fixture.test/")
        result=CheckResult("ui.accessibility","axe",Dimension.ACCESSIBILITY,Status.PASS)
        UiRunner._axe(result,page,page.url,"desktop",Path(path).read_text())
        assert any("button-name" in f.title for f in result.findings)
        assert not page.evaluate("Boolean(window.inlineRan)")
        browser.close()
