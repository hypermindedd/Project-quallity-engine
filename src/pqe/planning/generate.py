"""From "what is this project" to "everything it must be tested on".

``build_plan`` turns a :class:`Classification` into

* a **test plan**: every suite the project needs, why, how, and in which
  environment it runs (this machine, the Android emulator job, the macOS job,
  or a human sign-off), and
* a complete **quality contract** (``.qa/pqe.toml``) the engine can execute
  with no human editing.

The generated goals form a universal quality bar (tests pass, no secrets, no
crash, no broken page, …). Goals that come from the project's own documents
are added by :mod:`pqe.planning.ai` as *proposed* goals.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..discovery.classify import Classification, Component, Kind
from ..discovery.profile import ProjectProfile

READY, CI, HUMAN, UNAVAILABLE = "READY", "CI", "HUMAN", "UNAVAILABLE"


@dataclass
class PlannedSuite:
    id: str
    title: str
    why: str
    checks: list[str]
    target: str  # web | android | ios | windows
    component: str
    status: str = READY  # READY here, CI (another environment), HUMAN (sign-off), UNAVAILABLE (tool missing)
    note: str = ""


@dataclass
class TestPlan:
    project: str
    kinds: list[str]
    suites: list[PlannedSuite] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"project": self.project, "kinds": self.kinds, "suites": [asdict(s) for s in self.suites], "gaps": self.gaps}


def _join(base: str, rel: str) -> str:
    return rel if base in {"", "."} else f"{base}/{rel}"


def _here(target: str) -> bool:
    """Can this machine run a given environment's device checks?"""
    if target == "web":
        return True
    if target == "android":
        return bool(shutil.which("adb"))
    if target == "ios":
        return sys.platform == "darwin" and bool(shutil.which("xcodebuild"))
    return sys.platform == "win32"


def _rtl(root: Path) -> bool:
    """Does the UI declare right-to-left text (Persian/Arabic/Hebrew products)?"""
    for pattern in ("**/*.html", "**/layout.tsx", "**/layout.jsx", "**/_document.tsx", "**/index.html"):
        for path in list(root.glob(pattern))[:40]:
            if "node_modules" in path.parts:
                continue
            try:
                if re.search(r"""dir=["'{]?\s*["']?rtl""", path.read_text(encoding="utf-8", errors="ignore")):
                    return True
            except OSError:
                continue
    return False


_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(.*)$")
_PROVIDER_KEY = re.compile(r"(OPENAI|ANTHROPIC|GEMINI|GOOGLE|AZURE|AWS|STRIPE|TWILIO|SENDGRID|MAILGUN|SLACK|NOTION|GITHUB|HUGGINGFACE|REPLICATE|COHERE|MISTRAL)", re.I)


def sandbox_env(root: Path, folder: str) -> tuple[dict[str, str], list[str], list[str]]:
    """Derive an isolated environment from the project's documented env template.

    * credentials the app *issues or checks* (tokens, secrets, passwords) → fresh random values
    * keys for paid third-party providers → empty: the sandbox never calls a real paid service
    * data directories / paths → fresh temporary folders
    Returns (env, generated secret names, temp dir names).
    """
    env: dict[str, str] = {}
    secrets: list[str] = []
    temps: list[str] = []
    for name in (".env.example", ".env.sample", ".env.template", ".env.dist", "example.env"):
        path = root / folder / name if folder not in {"", "."} else root / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = _ENV_LINE.match(line)
            if not match:
                continue
            key, default = match.group(1), match.group(2).strip().strip("'\"")
            if _PROVIDER_KEY.search(key) and re.search(r"(KEY|TOKEN|SECRET)", key):
                env[key] = ""
            elif re.search(r"(TOKEN|SECRET|PASSWORD|PASSWD|SALT|SIGNING|PRIVATE_KEY|API_KEY)$", key):
                secrets.append(key)
                env[key] = "${" + key + "}"
            elif re.search(r"(_DIR|_PATH|_ROOT|_FOLDER)$", key):
                temps.append(key)
            elif re.search(r"(DATABASE_URL|DB_URL)$", key) and default.startswith("sqlite"):
                temps.append("PQE_SQLITE_DIR")
                env[key] = "sqlite:///${PQE_SQLITE_DIR}/pqe.db"
        break
    return env, secrets, temps


class _Builder:
    def __init__(self, classification: Classification, profile: ProjectProfile) -> None:
        self.c = classification
        self.profile = profile
        self.root = Path(classification.root)
        self.contract: dict[str, Any] = {}
        self.plan = TestPlan(profile.name, classification.kinds)
        self.setup: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.goals: list[dict[str, Any]] = []
        self.python_roots: list[str] = []
        self.node_dirs: list[str] = []
        self.requirements: list[str] = []

    # ------------------------------------------------------------ helpers
    def suite(self, sid: str, title: str, why: str, checks: list[str], target: str, comp: Component | None, note: str = "",
              status: str | None = None) -> None:
        if status is None:
            status = READY if _here(target) else CI
        self.plan.suites.append(PlannedSuite(sid, title, why, checks, target, comp.path if comp else ".", status, note))

    def goal(self, gid: str, title: str, checks: list[str], target: str = "", hard: bool = False, source: str = "engine universal bar") -> None:
        self.goals.append({"id": gid, "title": title, "source": source, "hard_gate": hard, "target": target,
                           "criteria": [{"kind": "check", "check": c} for c in checks]})

    def command(self, cid: str, title: str, argv: list[str], cwd: str, dimension: str, target: str = "", requires: list[str] | None = None,
                severity: str = "SEV1") -> None:
        if any(c["id"] == cid for c in self.commands):
            return
        entry: dict[str, Any] = {"id": cid, "title": title, "argv": argv, "cwd": cwd, "dimension": dimension, "severity": severity}
        if target:
            entry["target"] = target
        if requires:
            entry["requires"] = requires
        self.commands.append(entry)

    def install(self, comp: Component, target: str = "") -> None:
        if not comp.install_command:
            return
        sid = f"install-{(comp.path if comp.path != '.' else 'root').replace('/', '-')}-{comp.kind.value}"
        if any(s["argv"] == comp.install_command and s["cwd"] == comp.path for s in self.setup):
            return
        entry: dict[str, Any] = {"id": sid, "title": f"Install dependencies ({comp.path})", "argv": comp.install_command, "cwd": comp.path,
                                 "timeout_s": 1800}
        if target:
            entry["target"] = target
        self.setup.append(entry)

    @staticmethod
    def slug(comp: Component) -> str:
        return (comp.path if comp.path != "." else comp.kind.value).replace("/", "-")

    # ------------------------------------------------------------- kinds
    def tests_and_build(self, comp: Component, target: str = "") -> None:
        node_modules = [_join(comp.path, "node_modules")] if comp.framework not in {"python", "fastapi", "flask", "django", "flutter", "gradle", "xcode", "html", "mql5"} else None
        for argv in comp.test_commands:
            if argv[:3] == ["python", "-m", "pytest"] and comp.path == ".":
                continue  # handled by the dedicated pytest runner (richer per-test evidence)
            name = argv[-1] if argv[0] in {"npm", "flutter", "python"} else argv[-1].split(":")[-1]
            dim = "FUNCTIONAL" if "test" in name.lower() else ("CODE_QUALITY" if any(w in name for w in ("lint", "analyze")) else "BUILD")
            self.command(f"{self.slug(comp)}-{name}", f"{comp.path}: {' '.join(argv)}", argv, comp.path, dim, target, node_modules,
                         "SEV2" if dim == "CODE_QUALITY" else "SEV1")
        if comp.build_command and comp.kind not in (Kind.ANDROID_APP, Kind.FLUTTER_APP, Kind.IOS_APP):
            self.command(f"{self.slug(comp)}-build", f"{comp.path}: {' '.join(comp.build_command)}", comp.build_command, comp.path, "BUILD",
                         target, node_modules)

    def python_service(self, comp: Component) -> None:
        self.install(comp)
        root = comp.extra.get("python_root", ".")
        self.python_roots.append(_join(comp.path, root) if root != "." else (comp.path if comp.path != "." else (comp.app_id.split(".")[0] or ".")))
        if comp.path != "." and comp.test_commands:
            self.command(f"{self.slug(comp)}-pytest", f"{comp.path}: pytest", ["python", "-m", "pytest", "-q"], comp.path, "FUNCTIONAL")
        env, secrets, temps = sandbox_env(self.root, comp.path)
        if root != ".":
            env["PYTHONPATH"] = root
        api: dict[str, Any] = {
            "enabled": True,
            "start_command": comp.start_command,
            "cwd": comp.path,
            "health_path": comp.health_path,
            "openapi_path": comp.openapi_path,
            "ready_timeout_s": 90,
        }
        if env:
            api["env"] = env
        if secrets:
            api["generate_secrets"] = secrets
        if temps:
            api["temp_dirs"] = temps
        self.contract["api"] = api
        self.suite("api-contract", "API endpoints answer as declared", "Every endpoint the service exposes must answer, with JSON where it says JSON.",
                   ["api.contract"], "web", comp, "endpoints are read from OpenAPI when the service publishes it" if comp.openapi_path else
                   "no OpenAPI: add endpoints/scenarios to the contract (or let the AI planner propose them)")
        self.suite("api-robustness", "Hostile input never causes 5xx or leaks internals",
                   "Malformed JSON, wrong types, huge strings, path traversal and injection strings must be refused with 4xx.", ["api.robustness"], "web", comp)
        self.suite("api-security", "Authentication, hardening headers, CORS", "Protected endpoints must refuse missing or invalid credentials before doing work.",
                   ["api.security.auth", "api.security.headers"], "web", comp, "auth probing needs credentials in [api.auth_headers]")
        self.suite("api-performance", "Latency and behaviour under concurrent load", "Response time and error rate are measured on every run; regressions are compared to the approved baseline.",
                   ["performance.latency", "load.http"], "web", comp, "recorded as UNCALIBRATED until a human approves a baseline")
        self.goal("U-API-1", "The API never fails with 5xx or leaks internals on hostile input", ["api.robustness"])
        self.goal("U-API-2", "Every declared API endpoint answers as specified", ["api.contract"])

    def web_ui(self, comp: Component, start: list[str], cwd: str, requires: list[str], env: dict[str, str]) -> None:
        ui: dict[str, Any] = {"enabled": True, "start_command": start, "cwd": cwd, "pages": ["/"], "ready_timeout_s": 240,
                              "requires": requires, "llm_review": True}
        if env:
            ui["env"] = env
        if _rtl(self.root):
            ui["expect_dir"] = "rtl"
        self.contract["ui"] = ui
        self.contract["web"] = {"enabled": True}
        checks = {
            "ui-interaction": ("Every page loads and every control works without errors",
                               "The engine crawls every reachable page and clicks every visible control (destructive ones excluded), recording console errors, exceptions and failed requests.",
                               ["ui.interaction"]),
            "ui-responsive": ("Layout fits phone, tablet and desktop", "No horizontal overflow, no broken images, correct text direction at three viewports.", ["ui.responsive"]),
            "ui-visual": ("Screens match approved visual baselines", "Full-page screenshots are compared pixel-wise with the last approved set.", ["ui.visual"]),
            "ui-a11y": ("Accessibility (WCAG rules via axe-core)", "Contrast, labels, landmarks, keyboard and screen-reader basics.", ["ui.accessibility"]),
            "web-links": ("No broken links or missing assets", "Every internal page, link, image, script and stylesheet must answer.", ["web.links"]),
            "web-seo": ("Search and sharing readiness", "Titles, descriptions, language, viewport, headings, robots.txt and sitemap.", ["web.seo"]),
            "web-lighthouse": ("Lighthouse performance, accessibility, best practices, SEO", "Industry-standard page quality scores, recorded on every run.", ["web.lighthouse"]),
            "ui-design": ("Advisory design review by a vision model", "Visible layout and typography defects; advisory, never blocks a release.", ["ui.design_review"]),
        }
        for sid, (title, why, cks) in checks.items():
            note = "needs ANTHROPIC_API_KEY" if sid == "ui-design" else ""
            self.suite(sid, title, why, cks, "web", comp, note)
        self.goal("U-WEB-1", "Every page loads and every control works without errors", ["ui.interaction"], hard=False)
        self.goal("U-WEB-2", "The layout works on phone, tablet and desktop", ["ui.responsive"])
        self.goal("U-WEB-3", "No broken links or missing assets", ["web.links"])
        self.goal("U-WEB-4", "Accessibility rules hold", ["ui.accessibility"])
        self.goal("U-WEB-5", "Pages are ready for search and sharing", ["web.seo"])

    def node_web_app(self, comp: Component) -> None:
        self.install(comp)
        self.node_dirs.append(comp.path)
        self.tests_and_build(comp)
        env = {"PORT": "{port}"} if comp.extra.get("env_PORT") else {}
        self.web_ui(comp, comp.start_command, comp.path, [_join(comp.path, "node_modules")], env)

    def static_site(self, comp: Component) -> None:
        if comp.build_command:
            self.setup.append({"id": f"build-{comp.framework}", "title": f"Build site ({comp.framework})", "argv": comp.build_command, "cwd": comp.path})
        self.web_ui(comp, comp.start_command, comp.path, [], {})

    def android(self, comp: Component) -> None:
        android: dict[str, Any] = {"enabled": True, "project_dir": comp.path, "package": comp.app_id}
        if comp.kind == Kind.ANDROID_APP:
            android["build_command"] = comp.build_command or ["./gradlew", "assembleDebug"]
            unit, lint = (comp.test_commands + [[], []])[:2]
            android["unit_test_command"] = unit
            android["lint_command"] = lint
            prefix = comp.extra.get("gradle_task_prefix", ":app")
            android["instrumented_command"] = ["./gradlew", f"{prefix}:connectedDebugAndroidTest"]
        elif comp.kind == Kind.FLUTTER_APP:
            self.setup.append({"id": f"flutter-pub-{self.slug(comp)}", "title": "flutter pub get", "argv": ["flutter", "pub", "get"], "cwd": comp.path, "target": "android"})
            for argv in comp.test_commands:
                self.command(f"{self.slug(comp)}-{argv[-1]}", f"{comp.path}: {' '.join(argv)}", argv, comp.path,
                             "FUNCTIONAL" if argv[-1] == "test" else "CODE_QUALITY", "android")
            android["build_command"] = comp.build_command
            android["apk_glob"] = "build/app/outputs/flutter-apk/*.apk"
            if comp.extra.get("integration_tests") == "True":
                android["instrumented_command"] = ["flutter", "test", "integration_test"]
        else:  # React Native / Expo
            self.install(comp, "android")
            self.node_dirs.append(comp.path)
            self.tests_and_build(comp, "android")
            if comp.framework == "expo":
                self.setup.append({"id": f"expo-prebuild-{self.slug(comp)}", "title": "Generate native Android project (expo prebuild)",
                                   "argv": ["npx", "expo", "prebuild", "--platform", "android", "--no-install"], "cwd": comp.path, "target": "android"})
            android["project_dir"] = _join(comp.path, "android")
            android["build_command"] = ["./gradlew", "assembleDebug"]
            android["apk_glob"] = "app/build/outputs/apk/debug/*.apk"
        self.contract["android"] = android
        why_static = "Debuggable builds, app-wide cleartext HTTP, exported components and dangerous permissions are classic mobile vulnerabilities."
        self.suite("android-manifest", "Manifest security and permissions", why_static, ["android.manifest"], "android", comp)
        self.suite("android-build", "Debug APK builds and its size is tracked", "The app must build reproducibly from source.", ["android.build"], "android", comp)
        self.suite("android-unit", "Unit tests and static analysis", "JVM/Dart/JS unit tests and Android Lint.", ["android.unit", "android.lint"], "android", comp)
        self.suite("android-launch", "Installs and launches on the emulator; cold-start time", "Measured on a real Android system image in CI.", ["android.launch"], "android", comp)
        self.suite("android-monkey", "Survives thousands of random taps, swipes and key presses", "Random-event stress finds crashes and frozen screens (ANR) nobody wrote a test for; the seed makes every crash reproducible.",
                   ["android.monkey"], "android", comp)
        self.suite("android-instrumented", "On-device tests and Maestro user flows", "Project-written device tests and declarative user journeys.", ["android.instrumented", "android.maestro"], "android", comp,
                   "Maestro flows run when a .maestro/ folder exists (the AI planner can draft them)")
        self.goal("U-MOB-1", "The app installs, launches and survives random-event stress without a crash or ANR",
                  ["android.launch", "android.monkey"], target="android", hard=True)
        self.goal("U-MOB-2", "The Android build and its unit tests pass", ["android.build", "android.unit"], target="android", hard=True)
        self.goal("U-MOB-3", "No insecure manifest configuration", ["android.manifest"], target="android")

    def ios(self, comp: Component) -> None:
        args = [comp.extra.get("flag", "-project"), comp.extra.get("project", "").rsplit("/", 1)[-1], "-scheme", comp.extra.get("scheme", "")]
        self.contract["ios"] = {"enabled": True, "project_dir": comp.path, "xcodebuild_args": args}
        self.suite("ios-plist", "App Transport Security and privacy strings", "Disabled ATS and empty permission strings are security and App Store blockers.",
                   ["ios.plist"], "ios", comp)
        self.suite("ios-test", "Build and run tests on the iOS Simulator", "xcodebuild test on a simulated iPhone in the macOS CI job.", ["ios.test"], "ios", comp)
        self.goal("U-IOS-1", "The iOS app builds and its tests pass on the simulator", ["ios.test"], target="ios", hard=True)
        self.goal("U-IOS-2", "No insecure transport configuration", ["ios.plist"], target="ios")

    def cli(self, comp: Component) -> None:
        self.install(comp)
        self.tests_and_build(comp)
        for script in [s for s in comp.extra.get("scripts", "").split(",") if s][:3]:
            self.command(f"cli-{script}-help", f"CLI '{script}' starts and prints help", [script, "--help"], comp.path, "FUNCTIONAL")
        self.suite("cli-smoke", "Command-line entrypoints start", "Every declared command must at least start and print its help.", ["command.cli-*"], "web", comp)

    def library(self, comp: Component) -> None:
        self.install(comp)
        self.tests_and_build(comp)

    def trading(self, comp: Component) -> None:
        self.suite("ea-compile", "Expert Advisor compiles in MetaEditor", "MQL5 sources must compile without errors or warnings.", [], "windows", comp,
                   "needs a Windows runner with MetaTrader 5 / MetaEditor", status=CI if sys.platform != "win32" else READY)
        self.suite("ea-backtest", "Strategy Tester backtest against the documented strategy", "Profit factor, drawdown and trade count against the strategy document.",
                   [], "windows", comp, "needs MetaTrader 5 Strategy Tester and history data; record results as an attestation", status=HUMAN)
        self.plan.gaps.append("MQL5 Expert Advisors cannot be compiled or backtested on Linux/macOS CI; plan a Windows runner or attest results.")

    # -------------------------------------------------------------- build
    def build(self) -> tuple[TestPlan, dict[str, Any]]:
        handlers = {
            Kind.API_SERVICE: lambda c: self.python_service(c) if c.framework in {"fastapi", "flask", "django"} else self.node_api(c),
            Kind.WEB_APP: self.node_web_app,
            Kind.STATIC_SITE: self.static_site,
            Kind.ANDROID_APP: self.android,
            Kind.FLUTTER_APP: self.android,
            Kind.REACT_NATIVE_APP: self.android,
            Kind.IOS_APP: self.ios,
            Kind.CLI_TOOL: self.cli,
            Kind.LIBRARY: self.library,
            Kind.DESKTOP_APP: self.library,
            Kind.TRADING_EA: self.trading,
        }
        seen_api = seen_ui = False
        for comp in self.c.components:
            if comp.kind == Kind.API_SERVICE:
                if seen_api:
                    self.plan.gaps.append(f"second API service at {comp.path} not probed (one per contract); add a separate contract or extend [api].")
                    continue
                seen_api = True
            if comp.kind in (Kind.WEB_APP, Kind.STATIC_SITE):
                if seen_ui:
                    self.plan.gaps.append(f"second web front-end at {comp.path} not crawled (one per contract).")
                    continue
                seen_ui = True
            handlers[comp.kind](comp)
        if not self.c.components:
            self.plan.gaps.append("No runnable component recognised; only universal static checks apply.")
        self.universal()
        return self.plan, self.assemble()

    def node_api(self, comp: Component) -> None:
        self.install(comp)
        self.node_dirs.append(comp.path)
        self.tests_and_build(comp)
        self.contract["api"] = {"enabled": True, "start_command": comp.start_command, "cwd": comp.path, "health_path": comp.health_path,
                                "env": {"PORT": "{port}"}, "ready_timeout_s": 90, "requires": [_join(comp.path, "node_modules")]}
        self.suite("api-robustness", "Hostile input never causes 5xx or leaks internals", "Malformed input must be refused with 4xx.", ["api.robustness"], "web", comp)
        self.goal("U-API-1", "The API never fails with 5xx or leaks internals on hostile input", ["api.robustness"])

    def universal(self) -> None:
        has_pytest = any(Path(self.c.root, d).is_dir() for d in ("tests", "test")) and any(
            Path(self.c.root, m).exists() for m in ("pyproject.toml", "setup.cfg", "pytest.ini", "requirements.txt"))
        self.suite("own-tests", "The project's own automated tests", "Whatever tests the team wrote must pass on every change.",
                   ["functional.pytest", "command.*"], "web", None)
        self.suite("secrets", "No secrets committed", "Leaked keys are the most expensive class of defect; scanned on every run.", ["security.secrets"], "web", None)
        self.suite("dependencies", "No known-vulnerable dependencies", "Lockfiles are checked against public vulnerability databases.",
                   ["security.dependencies.python", "security.dependencies.node"], "web", None)
        self.suite("sast", "Static security analysis of the code", "Dangerous calls, injection patterns and unsafe defaults.", ["security.sast.python"], "web", None)
        self.suite("architecture", "No dependency cycles; declared boundaries respected", "Architecture rules are law once declared; cycles are reported from day one.",
                   ["architecture.boundaries"], "web", None, "declare module boundaries in [architecture.rules] to enforce more")
        self.suite("docs", "Documentation references real files", "Stale docs mislead people and the AI agents that read them.", ["docs.references"], "web", None)
        self.suite("doc-goals", "Goals and user journeys extracted from the project documents", "The AI planner reads the documents and proposes goal-level tests; they report from the first run and become gates once a human accepts them.",
                   ["goals.*"], "web", None, "needs ANTHROPIC_API_KEY (or a logged-in claude CLI)")
        self.suite("human", "Human acceptance where machines cannot judge", "Brand fit, legal and domain correctness need a named person's sign-off (.qa/attestations.toml).",
                   [], "web", None, status=HUMAN)
        self.contract["pytest"] = {"enabled": has_pytest}
        test_commands = [c for c in self.commands if c["dimension"] == "FUNCTIONAL" and not c.get("target")]
        if has_pytest:
            self.goal("U-00", "The project's own automated tests pass", ["functional.*"], hard=True)
        elif test_commands:
            self.goal("U-00", "The project's own automated tests pass", [f"command.{c['id']}" for c in test_commands], hard=True)
        else:
            self.plan.gaps.append("The project has no automated tests of its own; the engine's generated checks are the only safety net.")
        self.goal("U-01", "No secrets are committed", ["security.secrets"], hard=True)
        self.goal("U-02", "No known-vulnerable dependencies", ["security.dependencies.*"])
        self.goal("U-03", "No dependency cycles or boundary violations", ["architecture.boundaries"])

    def assemble(self) -> dict[str, Any]:
        goal_text = (self.profile.goal_statements[0].split(": ", 1)[-1] if self.profile.goal_statements else "")[:400]
        contract: dict[str, Any] = {
            "project": {"name": self.profile.name, "goal": goal_text, "docs": ["README*.md", "docs/**/*.md"], "exclude": []},
        }
        if self.setup:
            contract["setup"] = self.setup
        contract["pytest"] = self.contract.pop("pytest")
        if self.commands:
            contract["commands"] = self.commands
        roots = sorted({r for r in self.python_roots if r and Path(self.c.root, r).is_dir()})
        if not roots and "python" in self.profile.languages:
            roots = self.profile.python_roots
        contract["architecture"] = {"python_roots": roots, "detect_cycles": True}
        requirements = [p.relative_to(self.c.root).as_posix() for p in Path(self.c.root).glob("**/requirements*.txt")
                        if "node_modules" not in p.parts and ".venv" not in p.parts][:5]
        has_python = bool(requirements) or Path(self.c.root, "pyproject.toml").exists()
        contract["security"] = {"secrets": True, "bandit": bool(roots), "pip_audit": has_python,
                                "pip_audit_requirements": requirements,
                                "npm_audit_dirs": sorted({d for d in self.node_dirs if any(Path(self.c.root, d, lf).exists() for lf in ("pnpm-lock.yaml", "package-lock.json"))})}
        for section in ("api", "ui", "web", "android", "ios"):
            if section in self.contract:
                contract[section] = self.contract[section]
        contract["goals"] = self.goals
        contract["report"] = {"language": "fa"}
        return contract


def build_plan(classification: Classification, profile: ProjectProfile) -> tuple[TestPlan, dict[str, Any]]:
    return _Builder(classification, profile).build()


#: Persian title and rationale per suite (no Latin text inside Persian cells: it breaks RTL rendering).
SUITE_FA = {
    "api-contract": ("پاسخ‌گویی درست همهٔ نقاط ورودی سرویس", "هر نقطهٔ ورودی باید همان‌طور که اعلام شده جواب بدهد."),
    "api-robustness": ("مقاومت سرویس در برابر ورودی خراب و خصمانه", "ورودی ناقص، نوع اشتباه، رشتهٔ خیلی بلند و تلاش نفوذ باید با خطای کاربر رد شود، نه با خطای سرور یا افشای جزئیات داخلی."),
    "api-security": ("احراز هویت، هدرهای امنیتی و سیاست دسترسی بین‌دامنه‌ای", "نقاط محافظت‌شده باید بدون اعتبارنامهٔ درست، قبل از هر کاری درخواست را رد کنند."),
    "api-performance": ("سرعت پاسخ و رفتار زیر بار هم‌زمان", "زمان پاسخ و نرخ خطا در هر اجرا اندازه‌گیری و با خط مبنای تأییدشده مقایسه می‌شود."),
    "ui-interaction": ("باز شدن همهٔ صفحه‌ها و کار کردن همهٔ دکمه‌ها", "موتور همهٔ صفحه‌های قابل دسترس را می‌گردد و روی همهٔ کنترل‌ها کلیک می‌کند و هر خطای برنامه، پیام خطای مرورگر و درخواست ناموفق را ثبت می‌کند."),
    "ui-responsive": ("نمایش درست در گوشی، تبلت و دسکتاپ", "بدون بیرون‌زدگی افقی، بدون تصویر شکسته و با جهت درست متن در سه اندازهٔ صفحه."),
    "ui-visual": ("مقایسهٔ تصویری با نسخهٔ تأییدشده", "از کل صفحه عکس گرفته و پیکسل‌به‌پیکسل با آخرین نسخهٔ تأییدشده مقایسه می‌شود."),
    "ui-a11y": ("دسترس‌پذیری برای همهٔ کاربران", "کنتراست، برچسب‌ها، ساختار صفحه و پایهٔ صفحه‌خوان و صفحه‌کلید."),
    "web-links": ("نبود لینک یا فایل شکسته", "همهٔ صفحه‌ها، لینک‌ها، تصاویر، اسکریپت‌ها و استایل‌های داخلی باید جواب بدهند."),
    "web-seo": ("آمادگی برای موتور جستجو و اشتراک‌گذاری", "عنوان، توضیح، زبان صفحه، تنظیم نمایش موبایل، تیترها و فایل‌های راهنمای موتور جستجو."),
    "web-lighthouse": ("امتیاز استاندارد کیفیت صفحه", "امتیاز سرعت، دسترس‌پذیری، رعایت اصول و سئو در هر اجرا ثبت می‌شود."),
    "ui-design": ("بازبینی طراحی با هوش مصنوعی بینایی", "ایرادهای دیداری چیدمان و تایپوگرافی؛ فقط مشورتی و هرگز مانع انتشار نیست."),
    "android-manifest": ("امنیت پیکربندی و مجوزهای اپ اندروید", "حالت اشکال‌زدایی، ترافیک رمزنشده، اجزای در دسترس بقیهٔ اپ‌ها و مجوزهای حساس، آسیب‌پذیری‌های رایج موبایل‌اند."),
    "android-build": ("ساخته شدن اپ از سورس و پایش حجم آن", "اپ باید از روی سورس، تکرارپذیر ساخته شود."),
    "android-unit": ("تست‌های واحد و تحلیل ایستای اندروید", "تست‌هایی که تیم نوشته و بررسی‌های خودکار اندروید."),
    "android-launch": ("نصب، اجرا و زمان باز شدن اپ روی شبیه‌ساز", "روی یک سیستم‌عامل واقعی اندروید در محیط آزمایشی اندازه‌گیری می‌شود."),
    "android-monkey": ("دوام اپ در برابر هزاران لمس و حرکت تصادفی", "رفتار تصادفی کاربر، کرش‌ها و هنگ‌هایی را پیدا می‌کند که هیچ‌کس برایشان تست ننوشته؛ با عدد بذر، هر کرش دوباره قابل تکرار است."),
    "android-instrumented": ("تست‌های روی دستگاه و مسیرهای کاربر", "تست‌های دستگاهی پروژه و مسیرهای تعریف‌شدهٔ کاربر."),
    "ios-plist": ("امنیت ارتباطات و متن‌های اجازهٔ اپ آیفون", "خاموش بودن امنیت ارتباطات و متن خالی درخواست مجوز، هم ریسک امنیتی دارد هم رد شدن در فروشگاه."),
    "ios-test": ("ساخت و اجرای تست‌ها روی شبیه‌ساز آیفون", "روی آیفون شبیه‌سازی‌شده در محیط مک اجرا می‌شود."),
    "cli-smoke": ("اجرا شدن فرمان‌های خط فرمان", "هر فرمان اعلام‌شده باید دست‌کم اجرا شود و راهنمایش را نشان دهد."),
    "ea-compile": ("کامپایل شدن اکسپرت در متاادیتور", "سورس اکسپرت باید بدون خطا و هشدار کامپایل شود."),
    "ea-backtest": ("بک‌تست در استراتژی تستر بر اساس سند استراتژی", "ضریب سود، افت سرمایه و تعداد معاملات در برابر سند استراتژی."),
    "own-tests": ("تست‌های خودکار خود پروژه", "هر تستی که تیم نوشته باید در هر تغییر پاس شود."),
    "secrets": ("نبود کلید و رمز در کد", "کلید لو رفته پرهزینه‌ترین نوع ایراد است؛ در هر اجرا بررسی می‌شود."),
    "dependencies": ("نبود وابستگی آسیب‌پذیر", "کتابخانه‌های پروژه با پایگاه‌های عمومی آسیب‌پذیری مقایسه می‌شوند."),
    "sast": ("تحلیل امنیتی ایستای کد", "فراخوانی‌های خطرناک، الگوهای تزریق و پیش‌فرض‌های ناامن."),
    "architecture": ("نبود وابستگی چرخه‌ای و رعایت مرزهای معماری", "قواعد معماری بعد از اعلام، قانون‌اند؛ چرخه‌ها از روز اول گزارش می‌شوند."),
    "docs": ("درستی ارجاع‌های مستندات", "مستند کهنه هم آدم‌ها را گمراه می‌کند هم ایجنت‌هایی که آن را می‌خوانند."),
    "doc-goals": ("اهداف و مسیرهای کاربر استخراج‌شده از مستندات پروژه", "هوش مصنوعی مستندات را می‌خواند و تست‌های سطح هدف پیشنهاد می‌دهد؛ از اولین اجرا گزارش می‌شوند و بعد از تأیید انسانی، گیت انتشار می‌شوند."),
    "human": ("تأیید انسانی جایی که ماشین نمی‌تواند قضاوت کند", "تناسب با برند، درستی حقوقی و تخصصی، نیازمند امضای یک فرد مشخص است."),
}
NOTE_FA = {
    "needs ANTHROPIC_API_KEY": "نیازمند کلید دسترسی به هوش مصنوعی",
    "needs ANTHROPIC_API_KEY (or a logged-in claude CLI)": "نیازمند کلید دسترسی به هوش مصنوعی",
    "recorded as UNCALIBRATED until a human approves a baseline": "تا تأیید خط مبنا توسط انسان، فقط ثبت می‌شود",
    "auth probing needs credentials in [api.auth_headers]": "برای آزمون احراز هویت، اعتبارنامهٔ آزمایشی لازم است",
    "declare module boundaries in [architecture.rules] to enforce more": "با اعلام مرزهای ماژول‌ها، سخت‌گیری بیشتر می‌شود",
}

STATUS_FA = {READY: "✅ همین‌جا اجرا می‌شود", CI: "☁️ در محیط مخصوص CI", HUMAN: "🧑‍⚖️ نیازمند تأیید انسانی", UNAVAILABLE: "⚠️ ابزار در دسترس نیست"}
KIND_FA = {
    "static_site": "سایت ایستا", "web_app": "وب‌اپلیکیشن", "api_service": "سرویس پشتیبان", "android_app": "اپ اندروید",
    "ios_app": "اپ آیفون", "flutter_app": "اپ فلاتر", "react_native_app": "اپ ری‌اکت‌نیتیو", "desktop_app": "اپ دسکتاپ",
    "cli_tool": "ابزار خط فرمان", "library": "کتابخانه", "trading_ea": "اکسپرت معاملاتی",
}
TARGET_FA = {"web": "لینوکس / مرورگر", "android": "شبیه‌ساز اندروید", "ios": "شبیه‌ساز iOS (مک)", "windows": "ویندوز"}


def render_plan(plan: TestPlan, classification: Classification) -> str:
    lines = ['<div dir="rtl">', "", f"# نقشهٔ آزمون — {plan.project}", "", "## نوع پروژه", ""]
    for comp in classification.components:
        lines += [f"**{KIND_FA.get(comp.kind.value, comp.kind.value)}** — مسیر و شواهد:", "", "```text",
                  f"{comp.path}  ({comp.framework or '-'})", *comp.evidence, "```", ""]
    lines += ["", f"## آزمون‌های طراحی‌شده ({len(plan.suites)})", "", "| آزمون | چرا | محیط | وضعیت |", "|---|---|---|---|"]
    for s in plan.suites:
        title, why = SUITE_FA.get(s.id, (s.title, s.why))
        note_text = NOTE_FA.get(s.note, "") if s.note else ""
        note = f"<br><small>{note_text}</small>" if note_text else ""
        lines.append(f"| **{title}** | {why}{note} | {TARGET_FA.get(s.target, s.target)} | {STATUS_FA[s.status]} |")
    if plan.gaps:
        lines += ["", "## محدودیت‌های شناخته‌شده", "", "```text", *plan.gaps, "```"]
    lines += ["", "</div>", ""]
    return "\n".join(lines)
