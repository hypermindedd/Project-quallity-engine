"""Project quality contract: ``.qa/pqe.toml``.

The contract is the *oracle* of the engine. Tests and checks are derived from
the project's goals and documents as recorded here, never from the current
code. The engine reads this file but never writes to it during ``run`` or
``fix`` (it is a protected path).

Parsing is strict: unknown keys are rejected so a typo cannot silently turn a
check off.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .model import Dimension, Severity

CONFIG_DIR = ".qa"
CONFIG_FILE = "pqe.toml"


class ConfigError(ValueError):
    """Raised when the quality contract is missing or malformed."""


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #


@dataclass
class ProjectSpec:
    name: str
    goal: str = ""
    docs: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class CommandSpec:
    id: str
    title: str
    argv: list[str]
    dimension: Dimension = Dimension.BUILD
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: int = 900
    severity: Severity = Severity.SEV1
    requires: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass
class PytestSpec:
    enabled: bool = True
    argv: list[str] = field(default_factory=lambda: ["python", "-m", "pytest", "-q"])
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: int = 1800


@dataclass
class ArchitectureRule:
    name: str
    source: str
    forbid: list[str]
    reason: str = ""
    severity: Severity = Severity.SEV1


@dataclass
class MirrorSpec:
    source: str
    mirror: str
    severity: Severity = Severity.SEV1


@dataclass
class ModuleOwner:
    name: str
    paths: list[str]


@dataclass
class ArchitectureSpec:
    python_roots: list[str] = field(default_factory=list)
    rules: list[ArchitectureRule] = field(default_factory=list)
    mirrors: list[MirrorSpec] = field(default_factory=list)
    modules: list[ModuleOwner] = field(default_factory=list)
    detect_cycles: bool = True


@dataclass
class ApiEndpoint:
    method: str
    path: str
    auth: bool = True
    json: Any = None
    expect: list[int] = field(default_factory=list)
    load_safe: bool = False


@dataclass
class ScenarioStep:
    name: str
    method: str
    path: str
    json: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    auth: bool = True
    expect: list[int] = field(default_factory=lambda: [200])
    save: dict[str, str] = field(default_factory=dict)
    expect_json: dict[str, Any] = field(default_factory=dict)
    forbid_text: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    id: str
    title: str
    steps: list[ScenarioStep]
    severity: Severity = Severity.SEV1


@dataclass
class ApiSpec:
    enabled: bool = False
    start_command: list[str] = field(default_factory=list)
    base_url: str = ""
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    generate_secrets: list[str] = field(default_factory=list)
    temp_dirs: list[str] = field(default_factory=list)
    health_path: str = "/health"
    ready_timeout_s: int = 60
    openapi_path: str = ""
    auth_headers: dict[str, str] = field(default_factory=dict)
    endpoints: list[ApiEndpoint] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)
    fuzz_paths: bool = True
    fuzz_bodies: bool = True
    forbidden_markers: list[str] = field(
        default_factory=lambda: ["Traceback (most recent call last)", "root:x:0:0", "BEGIN PRIVATE KEY"]
    )
    require_security_headers: bool = False
    requires: list[str] = field(default_factory=list)


@dataclass
class PerformanceSpec:
    enabled: bool = True
    samples: int = 30
    p95_ms: float | None = None
    max_ms: float | None = None
    regression_tolerance_pct: float | None = None


@dataclass
class LoadSpec:
    enabled: bool = True
    concurrency: int = 8
    total_requests: int = 200
    max_error_rate: float | None = None
    p95_ms: float | None = None


@dataclass
class Viewport:
    name: str
    width: int
    height: int


@dataclass
class UiAction:
    """One step of the per-context bootstrap (e.g. a test-account sign-up)."""

    action: str  # goto | click | fill | select | wait_for | wait_ms | press
    selector: str = ""
    value: str = ""


@dataclass
class UiSpec:
    enabled: bool = False
    base_url: str = ""
    start_command: list[str] = field(default_factory=list)
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)
    ready_path: str = "/"
    ready_timeout_s: int = 120
    pages: list[str] = field(default_factory=lambda: ["/"])
    actions: list[UiAction] = field(default_factory=list)
    generate_secrets: list[str] = field(default_factory=list)
    max_pages: int = 25
    max_clicks_per_page: int = 40
    viewports: list[Viewport] = field(
        default_factory=lambda: [
            Viewport("mobile", 390, 844),
            Viewport("tablet", 834, 1194),
            Viewport("desktop", 1440, 900),
        ]
    )
    expect_dir: str = ""  # "rtl" / "ltr" / "" (not checked)
    requires: list[str] = field(default_factory=list)
    axe_script: str = ""
    visual_diff_threshold: float | None = None
    llm_review: bool = False
    ignore_console: list[str] = field(default_factory=list)
    click_skip_text: list[str] = field(
        default_factory=lambda: [
            "delete", "remove", "logout", "log out", "sign out", "pay", "purchase",
            "حذف", "خروج", "پرداخت", "خرید", "پاک",
        ]
    )


@dataclass
class SecuritySpec:
    secrets: bool = True
    secrets_allowlist: list[str] = field(default_factory=list)
    bandit: bool = True
    bandit_paths: list[str] = field(default_factory=list)
    pip_audit: bool = True
    pip_audit_requirements: list[str] = field(default_factory=list)
    npm_audit_dirs: list[str] = field(default_factory=list)
    npm_audit_production_only: bool = True
    semgrep_configs: list[str] = field(default_factory=list)


@dataclass
class Criterion:
    kind: str  # tests | check | attestation
    select: list[str] = field(default_factory=list)
    check: str = ""
    attestation: str = ""
    min_tests: int = 1
    findings: list[str] = field(default_factory=list)  # judge a check only by findings whose key/rule match


@dataclass
class Goal:
    id: str
    title: str
    criteria: list[Criterion]
    source: str = ""
    hard_gate: bool = False
    covers: list[str] = field(default_factory=list)


@dataclass
class TraceSource:
    path: str
    pattern: str
    description: str = ""


@dataclass
class RemediationSpec:
    max_iterations: int = 3
    fixer: list[str] = field(
        default_factory=lambda: ["claude", "-p", "--permission-mode", "acceptEdits"]
    )
    protected: list[str] = field(
        default_factory=lambda: [".qa/**", "tests/**", "test/**", "**/*.test.*", "**/test_*.py"]
    )
    branch_prefix: str = "pqe/fix"
    min_severity: Severity = Severity.SEV2


@dataclass
class NotionSpec:
    enabled: bool = False
    parent_page_id: str = ""
    token_env: str = "NOTION_TOKEN"


@dataclass
class ReportSpec:
    language: str = "fa"
    notion: NotionSpec = field(default_factory=NotionSpec)


@dataclass
class DocsCheckSpec:
    enabled: bool = True
    files: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)


@dataclass
class Config:
    root: Path
    project: ProjectSpec
    setup: list[CommandSpec] = field(default_factory=list)
    pytest: PytestSpec = field(default_factory=PytestSpec)
    commands: list[CommandSpec] = field(default_factory=list)
    architecture: ArchitectureSpec = field(default_factory=ArchitectureSpec)
    checksum_files: list[str] = field(default_factory=list)
    docs_check: DocsCheckSpec = field(default_factory=DocsCheckSpec)
    api: ApiSpec = field(default_factory=ApiSpec)
    performance: PerformanceSpec = field(default_factory=PerformanceSpec)
    load: LoadSpec = field(default_factory=LoadSpec)
    ui: UiSpec = field(default_factory=UiSpec)
    security: SecuritySpec = field(default_factory=SecuritySpec)
    goals: list[Goal] = field(default_factory=list)
    trace_sources: list[TraceSource] = field(default_factory=list)
    attestations: dict[str, dict[str, str]] = field(default_factory=dict)
    remediation: RemediationSpec = field(default_factory=RemediationSpec)
    report: ReportSpec = field(default_factory=ReportSpec)

    @property
    def qa_dir(self) -> Path:
        return self.root / CONFIG_DIR


# --------------------------------------------------------------------------- #
# Strict reader helpers
# --------------------------------------------------------------------------- #


class _Reader:
    """Typed, path-aware access to a TOML table that rejects unknown keys."""

    def __init__(self, data: Any, path: str) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: expected a table, got {type(data).__name__}")
        self._data = data
        self._path = path
        self._seen: set[str] = set()

    def _at(self, key: str) -> str:
        return f"{self._path}.{key}" if self._path else key

    def get(self, key: str, kind: type | tuple[type, ...], default: Any = None, required: bool = False) -> Any:
        self._seen.add(key)
        if key not in self._data:
            if required:
                raise ConfigError(f"{self._at(key)}: required key is missing")
            return default
        value = self._data[key]
        # TOML booleans are ints in Python; never accept bool where int is asked.
        if kind in (int, float) and isinstance(value, bool):
            raise ConfigError(f"{self._at(key)}: expected {kind.__name__}, got bool")
        if kind is float and isinstance(value, int):
            value = float(value)
        if not isinstance(value, kind):
            names = kind.__name__ if isinstance(kind, type) else "/".join(k.__name__ for k in kind)
            raise ConfigError(f"{self._at(key)}: expected {names}, got {type(value).__name__}")
        return value

    def str_list(self, key: str, default: list[str] | None = None) -> list[str]:
        values = self.get(key, list, default if default is not None else [])
        for i, item in enumerate(values):
            if not isinstance(item, str):
                raise ConfigError(f"{self._at(key)}[{i}]: expected string")
        return list(values)

    def str_map(self, key: str) -> dict[str, str]:
        table = self.get(key, dict, {})
        for k, v in table.items():
            if not isinstance(v, str):
                raise ConfigError(f"{self._at(key)}.{k}: expected string")
        return dict(table)

    def sub(self, key: str) -> _Reader:
        return _Reader(self.get(key, dict, {}), self._at(key))

    def subs(self, key: str) -> list[_Reader]:
        items = self.get(key, list, [])
        return [_Reader(item, f"{self._at(key)}[{i}]") for i, item in enumerate(items)]

    def enum(self, key: str, enum_type: type, default: Any) -> Any:
        raw = self.get(key, str, None)
        if raw is None:
            return default
        try:
            return enum_type(raw)
        except ValueError as exc:
            allowed = ", ".join(e.value for e in enum_type)
            raise ConfigError(f"{self._at(key)}: '{raw}' is not one of: {allowed}") from exc

    def done(self) -> None:
        unknown = sorted(set(self._data) - self._seen)
        if unknown:
            raise ConfigError(f"{self._path or '<root>'}: unknown key(s): {', '.join(unknown)}")


def _command(r: _Reader, default_dimension: Dimension) -> CommandSpec:
    spec = CommandSpec(
        id=r.get("id", str, required=True),
        title=r.get("title", str, "") or r.get("id", str),
        argv=r.str_list("argv"),
        dimension=r.enum("dimension", Dimension, default_dimension),
        cwd=r.get("cwd", str, "."),
        env=r.str_map("env"),
        timeout_s=r.get("timeout_s", int, 900),
        severity=r.enum("severity", Severity, Severity.SEV1),
        requires=r.str_list("requires"),
        enabled=r.get("enabled", bool, True),
    )
    r.done()
    if not spec.argv:
        raise ConfigError(f"command '{spec.id}': argv must not be empty")
    return spec


def _goal(r: _Reader) -> Goal:
    criteria = []
    for c in r.subs("criteria"):
        kind = c.get("kind", str, required=True)
        if kind not in {"tests", "check", "attestation"}:
            raise ConfigError(f"goal criterion kind '{kind}' must be tests/check/attestation")
        crit = Criterion(
            kind=kind,
            select=c.str_list("select"),
            check=c.get("check", str, ""),
            attestation=c.get("attestation", str, ""),
            min_tests=c.get("min_tests", int, 1),
            findings=c.str_list("findings"),
        )
        c.done()
        if kind == "tests" and not crit.select:
            raise ConfigError("tests criterion requires a non-empty 'select'")
        if kind == "check" and not crit.check:
            raise ConfigError("check criterion requires 'check'")
        if kind == "attestation" and not crit.attestation:
            raise ConfigError("attestation criterion requires 'attestation'")
        criteria.append(crit)
    goal = Goal(
        id=r.get("id", str, required=True),
        title=r.get("title", str, required=True),
        criteria=criteria,
        source=r.get("source", str, ""),
        hard_gate=r.get("hard_gate", bool, False),
        covers=r.str_list("covers"),
    )
    r.done()
    return goal


def _scenario(r: _Reader) -> Scenario:
    steps = []
    for s in r.subs("steps"):
        step = ScenarioStep(
            name=s.get("name", str, required=True),
            method=s.get("method", str, "GET").upper(),
            path=s.get("path", str, required=True),
            json=s.get("json", (dict, list, str, int, float, bool), None),
            headers=s.str_map("headers"),
            auth=s.get("auth", bool, True),
            expect=[int(x) for x in s.get("expect", list, [200])],
            save=s.str_map("save"),
            expect_json=s.get("expect_json", dict, {}),
            forbid_text=s.str_list("forbid_text"),
        )
        s.done()
        steps.append(step)
    scenario = Scenario(
        id=r.get("id", str, required=True),
        title=r.get("title", str, required=True),
        steps=steps,
        severity=r.enum("severity", Severity, Severity.SEV1),
    )
    r.done()
    if not steps:
        raise ConfigError(f"scenario '{scenario.id}' has no steps")
    return scenario


def parse_config(data: dict[str, Any], root: Path) -> Config:
    """Build a :class:`Config` from already-parsed TOML data."""
    top = _Reader(data, "")

    p = top.sub("project")
    project = ProjectSpec(
        name=p.get("name", str, required=True),
        goal=p.get("goal", str, ""),
        docs=p.str_list("docs"),
        exclude=p.str_list("exclude"),
    )
    p.done()

    setup = [_command(r, Dimension.BUILD) for r in top.subs("setup")]

    py = top.sub("pytest")
    pytest_spec = PytestSpec(
        enabled=py.get("enabled", bool, True),
        argv=py.str_list("argv", ["python", "-m", "pytest", "-q"]),
        cwd=py.get("cwd", str, "."),
        env=py.str_map("env"),
        timeout_s=py.get("timeout_s", int, 1800),
    )
    py.done()

    commands = [_command(r, Dimension.BUILD) for r in top.subs("commands")]

    a = top.sub("architecture")
    rules = []
    for r in a.subs("rules"):
        rules.append(
            ArchitectureRule(
                name=r.get("name", str, required=True),
                source=r.get("source", str, required=True),
                forbid=r.str_list("forbid"),
                reason=r.get("reason", str, ""),
                severity=r.enum("severity", Severity, Severity.SEV1),
            )
        )
        r.done()
    mirrors = []
    for r in a.subs("mirrors"):
        mirrors.append(
            MirrorSpec(
                source=r.get("source", str, required=True),
                mirror=r.get("mirror", str, required=True),
                severity=r.enum("severity", Severity, Severity.SEV1),
            )
        )
        r.done()
    modules = []
    for r in a.subs("modules"):
        modules.append(ModuleOwner(name=r.get("name", str, required=True), paths=r.str_list("paths")))
        r.done()
    architecture = ArchitectureSpec(
        python_roots=a.str_list("python_roots"),
        rules=rules,
        mirrors=mirrors,
        modules=modules,
        detect_cycles=a.get("detect_cycles", bool, True),
    )
    a.done()

    checksum_files = top.str_list("checksum_files")

    d = top.sub("docs_check")
    docs_check = DocsCheckSpec(
        enabled=d.get("enabled", bool, True), files=d.str_list("files"), ignore=d.str_list("ignore")
    )
    d.done()

    api_r = top.sub("api")
    endpoints = []
    for e in api_r.subs("endpoints"):
        endpoints.append(
            ApiEndpoint(
                method=e.get("method", str, "GET").upper(),
                path=e.get("path", str, required=True),
                auth=e.get("auth", bool, True),
                json=e.get("json", (dict, list, str, int, float, bool), None),
                expect=[int(x) for x in e.get("expect", list, [])],
                load_safe=e.get("load_safe", bool, False),
            )
        )
        e.done()
    api = ApiSpec(
        enabled=api_r.get("enabled", bool, False),
        start_command=api_r.str_list("start_command"),
        base_url=api_r.get("base_url", str, ""),
        cwd=api_r.get("cwd", str, "."),
        env=api_r.str_map("env"),
        generate_secrets=api_r.str_list("generate_secrets"),
        temp_dirs=api_r.str_list("temp_dirs"),
        health_path=api_r.get("health_path", str, "/health"),
        ready_timeout_s=api_r.get("ready_timeout_s", int, 60),
        openapi_path=api_r.get("openapi_path", str, ""),
        auth_headers=api_r.str_map("auth_headers"),
        endpoints=endpoints,
        scenarios=[_scenario(s) for s in api_r.subs("scenarios")],
        fuzz_paths=api_r.get("fuzz_paths", bool, True),
        fuzz_bodies=api_r.get("fuzz_bodies", bool, True),
        forbidden_markers=api_r.str_list("forbidden_markers", ApiSpec().forbidden_markers),
        require_security_headers=api_r.get("require_security_headers", bool, False),
        requires=api_r.str_list("requires"),
    )
    api_r.done()
    if api.enabled and not (api.start_command or api.base_url):
        raise ConfigError("api: enabled requires start_command or base_url")

    pf = top.sub("performance")
    performance = PerformanceSpec(
        enabled=pf.get("enabled", bool, True),
        samples=pf.get("samples", int, 30),
        p95_ms=pf.get("p95_ms", float, None),
        max_ms=pf.get("max_ms", float, None),
        regression_tolerance_pct=pf.get("regression_tolerance_pct", float, None),
    )
    pf.done()

    ld = top.sub("load")
    load = LoadSpec(
        enabled=ld.get("enabled", bool, True),
        concurrency=ld.get("concurrency", int, 8),
        total_requests=ld.get("total_requests", int, 200),
        max_error_rate=ld.get("max_error_rate", float, None),
        p95_ms=ld.get("p95_ms", float, None),
    )
    ld.done()

    u = top.sub("ui")
    actions = []
    for a in u.subs("actions"):
        action = UiAction(a.get("action", str, required=True), a.get("selector", str, ""), a.get("value", str, ""))
        a.done()
        if action.action not in {"goto", "click", "fill", "select", "wait_for", "wait_ms", "press"}:
            raise ConfigError(f"ui.actions: unknown action '{action.action}'")
        if action.action in {"click", "fill", "select", "wait_for", "press"} and not action.selector:
            raise ConfigError(f"ui.actions: '{action.action}' needs a selector")
        actions.append(action)
    viewports = []
    for v in u.subs("viewports"):
        viewports.append(Viewport(v.get("name", str, required=True), v.get("width", int, required=True), v.get("height", int, required=True)))
        v.done()
    ui = UiSpec(
        enabled=u.get("enabled", bool, False),
        base_url=u.get("base_url", str, ""),
        start_command=u.str_list("start_command"),
        cwd=u.get("cwd", str, "."),
        env=u.str_map("env"),
        ready_path=u.get("ready_path", str, "/"),
        ready_timeout_s=u.get("ready_timeout_s", int, 120),
        pages=u.str_list("pages", ["/"]),
        actions=actions,
        generate_secrets=u.str_list("generate_secrets"),
        max_pages=u.get("max_pages", int, 25),
        max_clicks_per_page=u.get("max_clicks_per_page", int, 40),
        viewports=viewports or UiSpec().viewports,
        expect_dir=u.get("expect_dir", str, ""),
        requires=u.str_list("requires"),
        axe_script=u.get("axe_script", str, ""),
        visual_diff_threshold=u.get("visual_diff_threshold", float, None),
        llm_review=u.get("llm_review", bool, False),
        ignore_console=u.str_list("ignore_console"),
        click_skip_text=u.str_list("click_skip_text", UiSpec().click_skip_text),
    )
    u.done()
    if ui.expect_dir not in {"", "rtl", "ltr"}:
        raise ConfigError("ui.expect_dir must be 'rtl', 'ltr' or empty")

    s = top.sub("security")
    security = SecuritySpec(
        secrets=s.get("secrets", bool, True),
        secrets_allowlist=s.str_list("secrets_allowlist"),
        bandit=s.get("bandit", bool, True),
        bandit_paths=s.str_list("bandit_paths"),
        pip_audit=s.get("pip_audit", bool, True),
        pip_audit_requirements=s.str_list("pip_audit_requirements"),
        npm_audit_dirs=s.str_list("npm_audit_dirs"),
        npm_audit_production_only=s.get("npm_audit_production_only", bool, True),
        semgrep_configs=s.str_list("semgrep_configs"),
    )
    s.done()

    goals = [_goal(g) for g in top.subs("goals")]
    ids = [g.id for g in goals]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ConfigError(f"duplicate goal id(s): {', '.join(sorted(dupes))}")

    trace_sources = []
    for t in top.subs("trace_sources"):
        trace_sources.append(
            TraceSource(
                path=t.get("path", str, required=True),
                pattern=t.get("pattern", str, required=True),
                description=t.get("description", str, ""),
            )
        )
        t.done()

    rm = top.sub("remediation")
    remediation = RemediationSpec(
        max_iterations=rm.get("max_iterations", int, 3),
        fixer=rm.str_list("fixer", RemediationSpec().fixer),
        protected=rm.str_list("protected", RemediationSpec().protected),
        branch_prefix=rm.get("branch_prefix", str, "pqe/fix"),
        min_severity=rm.enum("min_severity", Severity, Severity.SEV2),
    )
    rm.done()
    if not 1 <= remediation.max_iterations <= 10:
        raise ConfigError("remediation.max_iterations must be between 1 and 10")
    # The contract itself is always protected, whatever the project says.
    if ".qa/**" not in remediation.protected:
        remediation.protected.insert(0, ".qa/**")

    rp = top.sub("report")
    n = rp.sub("notion")
    notion = NotionSpec(
        enabled=n.get("enabled", bool, False),
        parent_page_id=n.get("parent_page_id", str, ""),
        token_env=n.get("token_env", str, "NOTION_TOKEN"),
    )
    n.done()
    report = ReportSpec(language=rp.get("language", str, "fa"), notion=notion)
    rp.done()
    if report.language not in {"fa", "en"}:
        raise ConfigError("report.language must be 'fa' or 'en'")

    top.done()
    return Config(
        root=root,
        project=project,
        setup=setup,
        pytest=pytest_spec,
        commands=commands,
        architecture=architecture,
        checksum_files=checksum_files,
        docs_check=docs_check,
        api=api,
        performance=performance,
        load=load,
        ui=ui,
        security=security,
        goals=goals,
        trace_sources=trace_sources,
        remediation=remediation,
        report=report,
    )


def load_attestations(root: Path) -> dict[str, dict[str, str]]:
    """Human sign-offs live in ``.qa/attestations.toml``.

    Each entry must name who attested, when, and the evidence reference.
    Incomplete entries are ignored (they do not count as attested).
    """
    path = root / CONFIG_DIR / "attestations.toml"
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    valid: dict[str, dict[str, str]] = {}
    for key, entry in data.items():
        if isinstance(entry, dict) and all(isinstance(entry.get(k), str) and entry.get(k) for k in ("by", "date", "evidence")):
            valid[key] = {k: str(v) for k, v in entry.items()}
    return valid


def load_config(root: Path) -> Config:
    path = root / CONFIG_DIR / CONFIG_FILE
    if not path.exists():
        raise ConfigError(f"no quality contract at {path}. Run 'pqe init {root}' first.")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    config = parse_config(data, root.resolve())
    config.attestations = load_attestations(root)
    return config
