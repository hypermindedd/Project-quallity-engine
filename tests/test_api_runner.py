"""End-to-end test of the API runner against a real (tiny) HTTP server."""

from __future__ import annotations

import sys

from pqe.model import Severity, Status
from pqe.runners.api import ApiRunner

from .conftest import FIXTURES

CONTRACT = f"""
[project]
name = "sample"

[api]
enabled = true
start_command = ["{sys.executable}", "{FIXTURES / 'sample_api.py'}", "{{port}}"]
generate_secrets = ["SAMPLE_TOKEN"]
health_path = "/health"
ready_timeout_s = 20

[api.env]
SAMPLE_TOKEN = "${{SAMPLE_TOKEN}}"

[api.auth_headers]
Authorization = "Bearer ${{SAMPLE_TOKEN}}"

[[api.endpoints]]
method = "POST"
path = "/items"
json = {{ name = "probe" }}
expect = [200]

[[api.endpoints]]
method = "GET"
path = "/items/{{id}}"
expect = [404]
load_safe = false

[[api.scenarios]]
id = "lifecycle"
title = "create, read, delete"
  [[api.scenarios.steps]]
  name = "create"
  method = "POST"
  path = "/items"
  json = {{ name = "abc" }}
  save = {{ id = "item.id", rev = "item.revision" }}
  [[api.scenarios.steps]]
  name = "read"
  path = "/items/${{id}}"
  expect_json = {{ "item.id" = "${{id}}", "item.revision" = "${{rev}}" }}
  [[api.scenarios.steps]]
  name = "foreign token is refused"
  path = "/items/${{id}}"
  auth = false
  headers = {{ Authorization = "Bearer nope" }}
  expect = [401]
  [[api.scenarios.steps]]
  name = "delete"
  method = "DELETE"
  path = "/items/${{id}}"
  [[api.scenarios.steps]]
  name = "gone"
  path = "/items/${{id}}"
  expect = [404]

[[api.scenarios]]
id = "honesty"
title = "preconditions are reported honestly"
severity = "SEV2"
  [[api.scenarios.steps]]
  name = "create"
  method = "POST"
  path = "/items"
  json = {{ name = "p" }}
  save = {{ id = "item.id" }}
  [[api.scenarios.steps]]
  name = "preview of an existing item is not 'not found'"
  path = "/items/${{id}}/preview"
  expect = [409, 422]
  forbid_text = ["item not found"]

[performance]
samples = 5

[load]
concurrency = 4
total_requests = 40
"""


def test_api_suite_finds_real_defects_and_keeps_honest_states(ctx_factory):
    ctx = ctx_factory(CONTRACT)
    results = {r.check_id: r for r in ApiRunner().run(ctx)}

    assert results["api.contract"].status == Status.PASS, results["api.contract"].findings
    assert results["api.security.auth"].status == Status.PASS, results["api.security.auth"].findings
    assert results["api.scenario.lifecycle"].status == Status.PASS, results["api.scenario.lifecycle"].findings

    robust = results["api.robustness"]
    assert robust.status == Status.FAIL
    leak = [f for f in robust.findings if "leaks internals" in f.title]
    assert leak and leak[0].severity == Severity.SEV1 and "deeply nested" in leak[0].title

    honesty = results["api.scenario.honesty"]
    assert honesty.status == Status.FAIL
    assert honesty.findings[0].severity == Severity.SEV2
    assert "forbidden text" in honesty.findings[0].detail

    # No thresholds in the contract → measured, never judged.
    assert results["performance.latency"].status == Status.UNCALIBRATED
    assert results["load.http"].status == Status.UNCALIBRATED
    assert results["load.http"].metrics["requests"] == 40

    # The generated secret never leaks into findings or logs.
    secret = ctx.secrets["SAMPLE_TOKEN"]
    assert all(secret not in f.detail for r in results.values() for f in r.findings)
    assert secret not in (ctx.out_dir / "api-server.log").read_text()


def test_unauthenticated_success_is_critical(ctx_factory):
    contract = CONTRACT.replace('Authorization = "Bearer ${SAMPLE_TOKEN}"', 'Authorization = "Bearer ${SAMPLE_TOKEN}"\n')
    contract = contract.replace('path = "/items/{id}"\nexpect = [404]', 'path = "/health"\nexpect = [200]')
    ctx = ctx_factory(contract)
    results = {r.check_id: r for r in ApiRunner().run(ctx)}
    auth = results["api.security.auth"]
    assert any(f.severity == Severity.SEV0 and "/health" in f.title for f in auth.findings)


def test_unstartable_application_is_an_error_not_a_pass(ctx_factory):
    ctx = ctx_factory(f"""
        [project]
        name = "x"
        [api]
        enabled = true
        start_command = ["{sys.executable}", "-c", "raise SystemExit(3)"]
        ready_timeout_s = 5
    """)
    [result] = ApiRunner().run(ctx)
    assert result.status == Status.ERROR and "exited with code 3" in result.summary
