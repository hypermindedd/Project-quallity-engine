"""Black-box API verification against a freshly booted, isolated instance.

One boot serves every API-level check, each reported separately:

* ``api.contract``           declared endpoints answer as the contract says
* ``api.security.auth``      authentication is enforced before anything else
* ``api.security.headers``   response hardening headers and CORS policy
* ``api.robustness``         hostile paths, bodies and methods never cause 5xx
                             or leak internals
* ``api.scenario.<id>``      goal-level end-to-end flows through the real API
* ``performance.latency``    latency distribution per safe endpoint
* ``load.http``              behaviour under concurrent load

Thresholds come only from the contract. Without one, measurements are
recorded as ``UNCALIBRATED`` — never silently passed.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from ..config import ApiEndpoint, ApiSpec, Scenario
from ..context import RunContext
from ..http import HttpClient, ManagedServer, Response, dotted_get, percentile
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status, TestCase
from .base import Runner, register

_PARAM = re.compile(r"\{([^}/]+)\}")
_EXACT_VAR = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

PATH_FUZZ = [
    "pqe-nonexistent-0000",
    "..%2F..%2F..%2Fetc%2Fpasswd",
    "%00",
    "a" * 1024,
    "'%20OR%20'1'%3D'1",
    "%3Cscript%3Ealert(1)%3C%2Fscript%3E",
    "%F0%9D%95%8F%C3%BC%E5%AD%97",
    "-1",
]
BODY_FUZZ: list[tuple[str, bytes]] = [
    ("invalid JSON", b"{"),
    ("empty body", b""),
    ("JSON array", b"[]"),
    ("JSON null", b"null"),
    ("empty object", b"{}"),
    # 2k levels passes most JSON parsers and stresses validators/serialisers;
    # 50k levels stresses the parser itself. Both have crashed real services.
    ("deeply nested (2k levels)", b"[" * 2_000 + b"]" * 2_000),
    ("deeply nested (50k levels)", b"[" * 50_000 + b"]" * 50_000),
]


@dataclass
class Endpoint:
    method: str
    path: str
    auth: bool
    json: Any
    expect: list[int]
    load_safe: bool

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"

    def concrete_path(self, value: str = "pqe-nonexistent-0000") -> str:
        return _PARAM.sub(value, self.path)

    @property
    def has_params(self) -> bool:
        return bool(_PARAM.search(self.path))

    @property
    def has_body(self) -> bool:
        return self.method in {"POST", "PUT", "PATCH"}


def _from_spec(e: ApiEndpoint) -> Endpoint:
    return Endpoint(e.method, e.path, e.auth, e.json, e.expect, e.load_safe)


def endpoints_from_openapi(document: dict[str, Any]) -> list[Endpoint]:
    found = []
    for path, ops in (document.get("paths") or {}).items():
        for method, op in ops.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            secured = bool(op.get("security", document.get("security")))
            found.append(Endpoint(method.upper(), path, secured, None, [], method.upper() == "GET" and "{" not in path))
    return found


class ApiProbe:
    def __init__(self, ctx: RunContext, client: HttpClient, spec: ApiSpec, endpoints: list[Endpoint]) -> None:
        self.ctx = ctx
        self.client = client
        self.spec = spec
        self.endpoints = endpoints
        self.latencies: dict[str, list[float]] = {}

    # ------------------------------------------------------------ helpers
    def auth_headers(self) -> dict[str, str]:
        return {k: self.ctx.interpolate(v) for k, v in self.spec.auth_headers.items()}

    def send(self, endpoint_label: str, method: str, path: str, *, auth: bool, json_body: Any = None,
             raw: bytes | None = None, headers: dict[str, str] | None = None) -> Response:
        hdrs = dict(self.auth_headers() if auth else {})
        hdrs.update(headers or {})
        resp = self.client.request(method, path, headers=hdrs, json_body=json_body, raw_body=raw)
        if resp.status:
            self.latencies.setdefault(endpoint_label, []).append(resp.elapsed_ms)
        return resp

    def leaks(self, resp: Response) -> list[str]:
        text = resp.text
        return [m for m in self.spec.forbidden_markers if m in text]

    def finding(self, check_id: str, dimension: Dimension, severity: Severity, title: str, endpoint: str,
                detail: str = "", suggestion: str = "", key: str = "") -> Finding:
        return Finding(check_id, dimension, severity, title, detail=self.ctx.redact(detail)[:1500],
                       location=Location(endpoint), evidence={"key": key or f"{endpoint}:{title}"}, suggestion=suggestion)

    # ------------------------------------------------------------- checks
    def contract(self) -> CheckResult:
        cid, dim = "api.contract", Dimension.API_CONTRACT
        result = CheckResult(cid, "API answers as the contract declares", dim, Status.PASS)
        if self.spec.health_path:  # empty = the project declares no health endpoint; do not invent one
            health = self.send(f"GET {self.spec.health_path}", "GET", self.spec.health_path, auth=False)
            result.tests.append(TestCase(f"{cid}::health", self.spec.health_path, "health", Status.PASS if health.status == 200 else Status.FAIL))
            if health.status != 200:
                result.findings.append(self.finding(cid, dim, Severity.SEV1, f"Health endpoint returned {health.status or health.error}", self.spec.health_path))
        for ep in self.endpoints:
            resp = self.send(ep.label, ep.method, ep.concrete_path(), auth=ep.auth, json_body=ep.json)
            ok = (resp.status in ep.expect) if ep.expect else (0 < resp.status < 500)
            result.tests.append(TestCase(f"{cid}::{ep.label}", ep.path, ep.label, Status.PASS if ok else Status.FAIL, resp.elapsed_ms / 1000))
            if not ok:
                expected = ep.expect or ["< 500"]
                result.findings.append(
                    self.finding(cid, dim, Severity.SEV1, f"{ep.label} returned {resp.status or resp.error}, expected {expected}",
                                 ep.label, detail=resp.text[:800], key=f"{ep.label}:contract")
                )
            elif "json" in resp.header("content-type"):
                try:
                    resp.json()
                except ValueError:
                    result.findings.append(self.finding(cid, dim, Severity.SEV2, f"{ep.label} declares JSON but body is not JSON", ep.label))
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(result.tests)} call(s), {len(result.findings)} contract issue(s)"
        return result

    def auth(self) -> CheckResult:
        cid, dim = "api.security.auth", Dimension.SECURITY_RUNTIME
        result = CheckResult(cid, "Authentication is enforced on protected endpoints", dim, Status.PASS)
        protected = [ep for ep in self.endpoints if ep.auth]
        if not self.spec.auth_headers or not protected:
            return CheckResult.skipped(cid, result.title, dim, "no protected endpoints or auth_headers declared")
        wrong = {k: "pqe-invalid-credential" for k in self.spec.auth_headers}
        for ep in protected:
            for label, auth, headers in (("no credentials", False, {}), ("invalid credentials", False, wrong)):
                resp = self.send(ep.label, ep.method, ep.concrete_path(), auth=auth, json_body=ep.json, headers=headers)
                passed = resp.status in (401, 403)
                result.tests.append(TestCase(f"{cid}::{ep.label}::{label}", ep.path, f"{ep.label} with {label}", Status.PASS if passed else Status.FAIL))
                if passed:
                    continue
                if 200 <= resp.status < 400:
                    sev, title = Severity.SEV0, f"{ep.label} succeeds with {label} (HTTP {resp.status})"
                elif resp.status >= 500 or resp.status == 0:
                    sev, title = Severity.SEV1, f"{ep.label} fails with {resp.status or resp.error} instead of rejecting {label}"
                else:
                    sev, title = Severity.SEV2, f"{ep.label} answers {resp.status} to {label}; input is processed before identity is checked"
                result.findings.append(self.finding(
                    cid, dim, sev, title, ep.label, detail=resp.text[:500], key=f"{ep.label}:{label}",
                    suggestion="Resolve and verify identity before parsing input or touching storage; answer 401/403 uniformly.",
                ))
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(result.tests)} probe(s), {len(result.findings)} issue(s)"
        return result

    def headers(self) -> CheckResult:
        cid, dim = "api.security.headers", Dimension.SECURITY_RUNTIME
        result = CheckResult(cid, "Response hardening headers and CORS", dim, Status.PASS)
        sev = Severity.SEV2 if self.spec.require_security_headers else Severity.INFO
        sample = self.send("headers", "GET", self.spec.health_path or "/", auth=False)
        required = {
            "x-content-type-options": "nosniff prevents MIME sniffing",
            "referrer-policy": "limits referrer leakage",
        }
        if self.client.scheme == "https":
            required["strict-transport-security"] = "enforces HTTPS"
        if "html" in sample.header("content-type"):
            required["content-security-policy"] = "mitigates XSS"
        for header, why in required.items():
            if not sample.header(header):
                result.findings.append(self.finding(cid, dim, sev, f"Missing response header: {header}", self.spec.health_path,
                                                    suggestion=f"Add '{header}' ({why}) at the app or reverse-proxy layer.", key=f"header:{header}"))
        server = sample.header("server")
        if re.search(r"\d", server):
            result.findings.append(self.finding(cid, dim, Severity.SEV3, f"Server header discloses version: {server}", self.spec.health_path, key="header:server"))
        target = next((ep for ep in self.endpoints if ep.auth), None)
        if target:
            evil = "https://pqe-untrusted-origin.example"
            pre = self.send(target.label, "OPTIONS", target.concrete_path(), auth=False,
                            headers={"Origin": evil, "Access-Control-Request-Method": target.method})
            allow_origin = pre.header("access-control-allow-origin")
            creds = pre.header("access-control-allow-credentials").lower() == "true"
            if allow_origin in (evil, "*") and creds:
                result.findings.append(self.finding(cid, dim, Severity.SEV1, "CORS allows arbitrary origins with credentials", target.label, key="cors:credentials"))
            elif allow_origin == evil:
                result.findings.append(self.finding(cid, dim, Severity.SEV2, "CORS reflects arbitrary origins", target.label, key="cors:reflect"))
        blocking = [f for f in result.findings if f.severity != Severity.INFO]
        result.status = Status.FAIL if blocking else Status.PASS
        result.summary = f"{len(result.findings)} observation(s)" + ("" if self.spec.require_security_headers else " (advisory: contract does not require headers)")
        return result

    def robustness(self) -> CheckResult:
        cid, dim = "api.robustness", Dimension.API_ROBUSTNESS
        result = CheckResult(cid, "Hostile input never causes 5xx or leaks internals", dim, Status.PASS)

        def judge(ep: Endpoint, case: str, resp: Response) -> None:
            leaks = self.leaks(resp)
            reflected = "<script>alert(1)</script>" in resp.text and "html" in resp.header("content-type")
            ok = 0 < resp.status < 500 and not leaks and not reflected
            result.tests.append(TestCase(f"{cid}::{ep.label}::{case}", ep.path, f"{ep.label} {case}", Status.PASS if ok else Status.FAIL))
            if leaks:
                result.findings.append(self.finding(cid, dim, Severity.SEV1, f"{ep.label} leaks internals on {case}", ep.label,
                                                    detail=f"markers: {leaks}", key=f"{ep.label}:{case}:leak"))
            elif reflected:
                result.findings.append(self.finding(cid, dim, Severity.SEV1, f"{ep.label} reflects script input unescaped ({case})", ep.label, key=f"{ep.label}:{case}:xss"))
            elif not ok:
                result.findings.append(self.finding(
                    cid, dim, Severity.SEV2, f"{ep.label} answers {resp.status or resp.error} to {case}", ep.label,
                    detail=resp.text[:500], key=f"{ep.label}:{case}",
                    suggestion="Validate input at the boundary and map failures to 4xx; a 5xx means the input reached code that did not expect it.",
                ))

        for ep in self.endpoints:
            if self.spec.fuzz_paths and ep.has_params:
                for value in PATH_FUZZ:
                    judge(ep, f"path param {value[:24]!r}", self.send(ep.label, ep.method, ep.concrete_path(value), auth=ep.auth, json_body=ep.json))
            if self.spec.fuzz_bodies and ep.has_body:
                for case, raw in BODY_FUZZ:
                    judge(ep, case, self.send(ep.label, ep.method, ep.concrete_path(), auth=ep.auth, raw=raw))
                if isinstance(ep.json, dict):
                    for key in list(ep.json)[:12]:
                        for wrong in (12345, None, "x" * 100_000):
                            body = {**ep.json, key: wrong}
                            label = f"field '{key}' = {type(wrong).__name__}{'(100k)' if isinstance(wrong, str) else ''}"
                            judge(ep, label, self.send(ep.label, ep.method, ep.concrete_path(), auth=ep.auth, json_body=body))
                    judge(ep, "unexpected extra field", self.send(ep.label, ep.method, ep.concrete_path(), auth=ep.auth,
                                                                  json_body={**ep.json, "__pqe_unexpected__": True}))
        paths = {ep.path: {e.method for e in self.endpoints if e.path == ep.path} for ep in self.endpoints}
        for path, methods in paths.items():
            unsupported = next((m for m in ("PATCH", "PUT", "DELETE", "POST") if m not in methods), None)
            if unsupported:
                ep = Endpoint(unsupported, path, True, None, [], False)
                judge(ep, "unsupported method", self.send(ep.label, unsupported, ep.concrete_path(), auth=True))
        unknown = Endpoint("GET", "/pqe-route-that-does-not-exist", False, None, [], False)
        resp = self.send(unknown.label, "GET", unknown.path, auth=True)
        judge(unknown, "unknown route", resp)
        if resp.status and resp.status != 404:
            result.findings.append(self.finding(cid, dim, Severity.SEV3, f"Unknown route answers {resp.status}, not 404", unknown.path, key="unknown-route"))
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(result.tests)} hostile request(s), {len(result.findings)} issue(s)"
        return result

    # ---------------------------------------------------------- scenarios
    def _resolve(self, value: Any, variables: dict[str, Any]) -> Any:
        if isinstance(value, str):
            exact = _EXACT_VAR.match(value)
            if exact and exact.group(1) in variables:
                return variables[exact.group(1)]  # keep the saved type (e.g. int revisions)
            return self.ctx.interpolate(value, {k: str(v) for k, v in variables.items()})
        if isinstance(value, dict):
            return {k: self._resolve(v, variables) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v, variables) for v in value]
        return value

    def scenario(self, scenario: Scenario) -> CheckResult:
        cid, dim = f"api.scenario.{scenario.id}", Dimension.E2E_FLOW
        result = CheckResult(cid, scenario.title, dim, Status.PASS)
        variables: dict[str, Any] = {}
        for index, step in enumerate(scenario.steps, 1):
            path = self._resolve(step.path, variables)
            body = self._resolve(step.json, variables)
            headers = {k: self._resolve(v, variables) for k, v in step.headers.items()}
            label = f"{step.method} {step.path}"
            resp = self.send(label, step.method, path, auth=step.auth, json_body=body, headers=headers)
            problems: list[str] = []
            if resp.status not in step.expect:
                problems.append(f"status {resp.status or resp.error}, expected {step.expect}")
            payload: Any = None
            if step.save or step.expect_json:
                try:
                    payload = resp.json()
                except ValueError:
                    problems.append("response is not JSON")
            if payload is not None:
                for var, dotted in step.save.items():
                    try:
                        variables[var] = dotted_get(payload, dotted)
                    except (KeyError, IndexError, ValueError):
                        problems.append(f"cannot save '{var}': '{dotted}' absent")
                for dotted, expected in step.expect_json.items():
                    try:
                        actual = dotted_get(payload, dotted)
                    except (KeyError, IndexError, ValueError):
                        problems.append(f"'{dotted}' absent")
                        continue
                    if actual != self._resolve(expected, variables):
                        problems.append(f"'{dotted}' = {json.dumps(actual)[:80]}, expected {json.dumps(expected)[:80]}")
            for text in step.forbid_text:
                if self._resolve(text, variables) in resp.text:
                    problems.append(f"response contains forbidden text {text!r}")
            problems += [f"leaks {m!r}" for m in self.leaks(resp)]
            status = Status.FAIL if problems else Status.PASS
            result.tests.append(TestCase(f"{cid}::{index:02d}-{step.name}", step.path, step.name, status, resp.elapsed_ms / 1000, "; ".join(problems)))
            if problems:
                result.findings.append(self.finding(
                    cid, dim, scenario.severity, f"Scenario '{scenario.id}' step {index} '{step.name}' failed", label,
                    detail="; ".join(problems) + "\n" + resp.text[:600], key=f"{scenario.id}:{step.name}",
                ))
                break  # later steps depend on this one; do not report cascades as separate defects
        result.status = Status.FAIL if result.findings else Status.PASS
        done = len(result.tests)
        result.summary = f"{done}/{len(scenario.steps)} step(s) executed" + (", all passed" if not result.findings else ", stopped at first failure")
        return result

    # ------------------------------------------------------- performance
    def _safe_targets(self) -> list[Endpoint]:
        targets = [ep for ep in self.endpoints if ep.load_safe]
        if not self.spec.health_path:
            return targets or [Endpoint("GET", self.spec.openapi_path or "/", False, None, [], True)]
        health = Endpoint("GET", self.spec.health_path, False, None, [200], True)
        return [health, *targets]

    def performance(self, samples: int, p95_ms: float | None, max_ms: float | None,
                    tolerance: float | None, baseline: dict[str, Any]) -> CheckResult:
        cid, dim = "performance.latency", Dimension.PERFORMANCE
        result = CheckResult(cid, "Response latency per safe endpoint", dim, Status.PASS)
        observed: dict[str, dict[str, float]] = {}
        for ep in self._safe_targets():
            times = []
            errors = 0
            for _ in range(samples):
                resp = self.client.request(ep.method, ep.concrete_path(), headers=self.auth_headers() if ep.auth else {}, json_body=ep.json)
                if resp.status == 0 or resp.status >= 500:
                    errors += 1
                else:
                    times.append(resp.elapsed_ms)
            stats = {"p50_ms": round(percentile(times, 50), 2), "p95_ms": round(percentile(times, 95), 2),
                     "max_ms": round(max(times), 2) if times else 0.0, "errors": errors, "samples": samples}
            observed[ep.label] = stats
            if errors:
                result.findings.append(self.finding(cid, dim, Severity.SEV2, f"{ep.label} failed {errors}/{samples} times during sampling", ep.label, key=f"{ep.label}:perf-errors"))
            if p95_ms is not None and stats["p95_ms"] > p95_ms:
                result.findings.append(self.finding(cid, dim, Severity.SEV2, f"{ep.label} p95 {stats['p95_ms']}ms > {p95_ms}ms", ep.label, key=f"{ep.label}:p95"))
            if max_ms is not None and stats["max_ms"] > max_ms:
                result.findings.append(self.finding(cid, dim, Severity.SEV3, f"{ep.label} max {stats['max_ms']}ms > {max_ms}ms", ep.label, key=f"{ep.label}:max"))
            prior = (baseline.get(ep.label) or {}).get("p95_ms")
            if tolerance is not None and prior:
                limit = prior * (1 + tolerance / 100)
                if stats["p95_ms"] > limit:
                    result.findings.append(self.finding(cid, dim, Severity.SEV2, f"{ep.label} p95 regressed {prior}ms → {stats['p95_ms']}ms (> {tolerance}%)", ep.label, key=f"{ep.label}:regression"))
        result.metrics = {"endpoints": observed}
        (self.ctx.out_dir / "perf-observed.json").write_text(json.dumps(observed, indent=2), encoding="utf-8")
        result.artifacts.append("perf-observed.json")
        calibrated = p95_ms is not None or max_ms is not None or (tolerance is not None and baseline)
        if result.findings:
            result.status = Status.FAIL
        elif not calibrated:
            result.status = Status.UNCALIBRATED
        result.summary = (
            f"{len(observed)} endpoint(s) sampled {samples}x"
            + ("" if calibrated else "; no approved threshold — recorded as baseline candidate")
        )
        return result

    def load(self, concurrency: int, total: int, max_error_rate: float | None, p95_ms: float | None) -> CheckResult:
        cid, dim = "load.http", Dimension.LOAD
        result = CheckResult(cid, f"Concurrent load ({concurrency} workers, {total} requests)", dim, Status.PASS)
        targets = self._safe_targets()
        headers = self.auth_headers()

        def one(i: int) -> Response:
            ep = targets[i % len(targets)]
            return self.client.request(ep.method, ep.concrete_path(), headers=headers if ep.auth else {}, json_body=ep.json)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            responses = list(pool.map(one, range(total)))
        wall = time.perf_counter() - started
        times = [r.elapsed_ms for r in responses if r.status]
        server_errors = sum(1 for r in responses if r.status >= 500)
        transport_errors = sum(1 for r in responses if r.status == 0)
        error_rate = (server_errors + transport_errors) / total if total else 0.0
        result.metrics = {
            "requests": total, "concurrency": concurrency, "throughput_rps": round(total / wall, 1) if wall else 0,
            "p50_ms": round(percentile(times, 50), 2), "p95_ms": round(percentile(times, 95), 2),
            "server_errors": server_errors, "transport_errors": transport_errors, "error_rate": round(error_rate, 4),
        }
        if server_errors or transport_errors:
            result.findings.append(self.finding(
                cid, dim, Severity.SEV2, f"{server_errors} server error(s) and {transport_errors} transport error(s) under concurrency {concurrency}",
                "load", key="load:errors",
                suggestion="Errors that appear only under concurrency point to shared mutable state, connection limits or locking.",
            ))
        if max_error_rate is not None and error_rate > max_error_rate:
            result.findings.append(self.finding(cid, dim, Severity.SEV2, f"error rate {error_rate:.2%} > {max_error_rate:.2%}", "load", key="load:error-rate"))
        if p95_ms is not None and result.metrics["p95_ms"] > p95_ms:
            result.findings.append(self.finding(cid, dim, Severity.SEV2, f"p95 under load {result.metrics['p95_ms']}ms > {p95_ms}ms", "load", key="load:p95"))
        if result.findings:
            result.status = Status.FAIL
        elif max_error_rate is None and p95_ms is None:
            result.status = Status.UNCALIBRATED
        result.summary = f"{result.metrics['throughput_rps']} req/s, p95 {result.metrics['p95_ms']}ms, errors {server_errors + transport_errors}"
        return result


@register
class ApiRunner(Runner):
    id = "api"
    title = "API verification suite"
    dimension = Dimension.API_CONTRACT
    phase = 40

    def applicable(self, ctx: RunContext) -> str | None:
        if not ctx.config.api.enabled:
            return "api verification not enabled in contract"
        missing = [r for r in ctx.config.api.requires if not (ctx.root / r).exists()]
        return f"prerequisite missing: {', '.join(missing)} (run with --setup)" if missing else None

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        spec = ctx.config.api
        for name in spec.generate_secrets:
            ctx.secret(name)
        endpoints = [_from_spec(e) for e in spec.endpoints]
        if spec.base_url:
            return self._suite(ctx, HttpClient(ctx.interpolate(spec.base_url)), endpoints)
        server = ManagedServer(ctx, spec.start_command, spec.cwd, spec.env, spec.temp_dirs,
                               spec.health_path or spec.openapi_path or "/", spec.ready_timeout_s, "api-server.log")
        try:
            with server:
                results = self._suite(ctx, HttpClient(server.base_url), endpoints)
        except RuntimeError as exc:
            return [CheckResult.errored(self.id, "Application could not be started for API verification", Dimension.API_CONTRACT, str(exc))]
        for r in results:
            r.artifacts.append("api-server.log")
        return results

    def _suite(self, ctx: RunContext, client: HttpClient, endpoints: list[Endpoint]) -> list[CheckResult]:
        spec = ctx.config.api
        if spec.openapi_path:
            resp = client.request("GET", spec.openapi_path)
            if resp.status == 200:
                declared = {e.label for e in endpoints}
                endpoints += [e for e in endpoints_from_openapi(resp.json()) if e.label not in declared]
        probe = ApiProbe(ctx, client, spec, endpoints)
        results = [probe.contract(), probe.auth(), probe.headers(), probe.robustness()]
        results += [probe.scenario(s) for s in spec.scenarios]
        perf, load = ctx.config.performance, ctx.config.load
        baseline_path = ctx.config.qa_dir / "baselines" / "perf.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else {}
        if perf.enabled:
            results.append(probe.performance(perf.samples, perf.p95_ms, perf.max_ms, perf.regression_tolerance_pct, baseline))
        else:
            results.append(CheckResult.skipped("performance.latency", "Response latency", Dimension.PERFORMANCE, "disabled in contract"))
        if load.enabled:
            results.append(probe.load(load.concurrency, load.total_requests, load.max_error_rate, load.p95_ms))
        else:
            results.append(CheckResult.skipped("load.http", "Concurrent load", Dimension.LOAD, "disabled in contract"))
        return results
