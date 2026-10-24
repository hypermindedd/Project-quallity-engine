"""What *kind* of software is this, and how is it started?

A repository may contain several runnable components (a mobile app and its
API, a site and its backend). Each component is classified from concrete
evidence (manifests, build files, entrypoints), never from names alone, and
carries the hints the planner needs to boot it in an isolated sandbox.

Classification is deterministic and read-only.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from ..context import ALWAYS_EXCLUDED_DIRS


class Kind(StrEnum):
    STATIC_SITE = "static_site"
    WEB_APP = "web_app"
    API_SERVICE = "api_service"
    ANDROID_APP = "android_app"
    IOS_APP = "ios_app"
    FLUTTER_APP = "flutter_app"
    REACT_NATIVE_APP = "react_native_app"
    DESKTOP_APP = "desktop_app"
    CLI_TOOL = "cli_tool"
    LIBRARY = "library"
    TRADING_EA = "trading_ea"


#: Which execution environment each kind needs. The CI workflow fans out on this.
TARGET = {
    Kind.STATIC_SITE: "web",
    Kind.WEB_APP: "web",
    Kind.API_SERVICE: "web",
    Kind.CLI_TOOL: "web",
    Kind.LIBRARY: "web",
    Kind.DESKTOP_APP: "web",
    Kind.ANDROID_APP: "android",
    Kind.REACT_NATIVE_APP: "android",
    Kind.FLUTTER_APP: "android",
    Kind.IOS_APP: "ios",
    Kind.TRADING_EA: "windows",
}

# Order matters: the most specific tool wins (vinext wraps next, nuxt wraps vue, ...).
WEB_FRAMEWORKS = {
    "vinext": "vinext", "next": "next", "nuxt": "nuxt", "@remix-run/react": "remix", "@sveltejs/kit": "sveltekit", "astro": "astro",
    "vite": "vite", "react-scripts": "cra", "@angular/core": "angular", "vue": "vue", "svelte": "svelte",
    "gatsby": "gatsby",
}
NODE_API = {"express", "fastify", "@nestjs/core", "koa", "hono", "@hapi/hapi"}
MAX_FILES = 40_000
#: Test data and samples are not product components (a repo's fixtures must not be "tested as the app").
NON_PRODUCT_DIRS = {"fixtures", "__fixtures__", "testdata", "test-data", "__mocks__", "examples", "example", "samples"}


@dataclass
class Component:
    kind: Kind
    path: str  # relative directory, "." for the root
    evidence: list[str] = field(default_factory=list)
    framework: str = ""
    # boot hints (all optional; the planner decides what to use)
    start_command: list[str] = field(default_factory=list)
    build_command: list[str] = field(default_factory=list)
    install_command: list[str] = field(default_factory=list)
    test_commands: list[list[str]] = field(default_factory=list)
    ready_path: str = "/"
    health_path: str = ""
    openapi_path: str = ""
    app_id: str = ""  # Android applicationId / iOS bundle id / python module:app
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def target(self) -> str:
        return TARGET[self.kind]

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["kind"] = self.kind.value
        data["target"] = self.target
        return data


@dataclass
class Classification:
    root: str
    components: list[Component]

    @property
    def kinds(self) -> list[str]:
        return sorted({c.kind.value for c in self.components})

    @property
    def targets(self) -> list[str]:
        return sorted({c.target for c in self.components})

    def first(self, *kinds: Kind) -> Component | None:
        return next((c for c in self.components if c.kind in kinds), None)

    def to_json(self) -> str:
        return json.dumps({"root": self.root, "kinds": self.kinds, "targets": self.targets,
                           "components": [c.to_dict() for c in self.components]}, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# File inventory
# --------------------------------------------------------------------------- #


class Inventory:
    """One walk of the repository, shared by every detector."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: list[str] = []
        self.dirs: set[str] = set()
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in ALWAYS_EXCLUDED_DIRS and d not in NON_PRODUCT_DIRS
                                 and (not d.startswith(".") or d in {".github", ".maestro"}))
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            for d in dirnames:
                self.dirs.add(f"{rel_dir}/{d}".lstrip("/"))
            for name in sorted(filenames):
                count += 1
                if count > MAX_FILES:
                    return
                self.files.append(f"{rel_dir}/{name}".lstrip("/"))

    def named(self, *names: str) -> list[str]:
        return [f for f in self.files if f.rsplit("/", 1)[-1] in names]

    def suffix(self, *suffixes: str) -> list[str]:
        return [f for f in self.files if f.endswith(suffixes)]

    def read(self, rel: str, limit: int = 500_000) -> str:
        path = self.root / rel
        try:
            if path.stat().st_size > limit:
                return ""
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


def _parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else "."


def _join(base: str, rel: str) -> str:
    return rel if base in {"", "."} else f"{base}/{rel}"


# --------------------------------------------------------------------------- #
# Detectors
# --------------------------------------------------------------------------- #


def _node_install(inv: Inventory, folder: str) -> list[str]:
    # corepack ships with Node and fetches the exact pnpm/yarn version the project pins (packageManager),
    # so CI runners need nothing preinstalled.
    if _join(folder, "pnpm-lock.yaml") in inv.files:
        return ["corepack", "pnpm", "install", "--frozen-lockfile"]
    if _join(folder, "yarn.lock") in inv.files:
        return ["corepack", "yarn", "install", "--frozen-lockfile"]
    if _join(folder, "package-lock.json") in inv.files:
        return ["npm", "ci"]
    return ["npm", "install"]


def _node_start(framework: str, scripts: dict[str, str]) -> list[str]:
    """A dev/start command that binds to the engine-chosen port on loopback."""
    if "dev" in scripts:
        base = ["npm", "run", "dev", "--"]
        flags = {
            "next": ["-p", "{port}", "-H", "127.0.0.1"],
            "vite": ["--port", "{port}", "--strictPort", "--host", "127.0.0.1"],
            "vinext": ["--port", "{port}", "--hostname", "127.0.0.1"],
            "astro": ["--port", "{port}", "--host", "127.0.0.1"],
            "sveltekit": ["--port", "{port}", "--strictPort", "--host", "127.0.0.1"],
            "nuxt": ["--port", "{port}", "--host", "127.0.0.1"],
            "angular": ["--port", "{port}", "--host", "127.0.0.1"],
        }.get(framework)
        return [*base, *flags] if flags else ["npm", "run", "dev"]
    if "start" in scripts:
        return ["npm", "start"]
    return []


def detect_node(inv: Inventory, out: list[Component]) -> None:
    for manifest in inv.named("package.json"):
        try:
            data = json.loads(inv.read(manifest))
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        folder = _parent(manifest)
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        scripts = data.get("scripts", {}) or {}
        tests = [["npm", "run", s] for s in ("typecheck", "lint", "test") if s in scripts]
        install = _node_install(inv, folder)
        build = ["npm", "run", "build"] if "build" in scripts else []
        ev = [f"{manifest}"]
        if "react-native" in deps or "expo" in deps:
            app_json = inv.read(_join(folder, "app.json"))
            out.append(Component(Kind.REACT_NATIVE_APP, folder, ev + ["react-native/expo dependency"],
                                 framework="expo" if "expo" in deps else "react-native",
                                 install_command=install, test_commands=tests,
                                 app_id=_expo_package(app_json)))
            continue
        if "electron" in deps or "@tauri-apps/api" in deps:
            out.append(Component(Kind.DESKTOP_APP, folder, ev + ["electron/tauri dependency"],
                                 framework="electron" if "electron" in deps else "tauri", install_command=install,
                                 build_command=build, test_commands=tests))
            continue
        framework = next((name for dep, name in WEB_FRAMEWORKS.items() if dep in deps), "")
        if framework and (scripts.get("dev") or scripts.get("start")):
            out.append(Component(Kind.WEB_APP, folder, ev + [f"framework {framework}", "dev/start script"],
                                 framework=framework, install_command=install, build_command=build,
                                 test_commands=tests, start_command=_node_start(framework, scripts),
                                 extra={"env_PORT": "{port}"}))
            continue
        api = sorted(NODE_API & set(deps))
        if api and scripts.get("start"):
            out.append(Component(Kind.API_SERVICE, folder, ev + [f"server framework {api[0]}"], framework=api[0],
                                 install_command=install, test_commands=tests, start_command=["npm", "start"],
                                 extra={"env_PORT": "{port}"}))
            continue
        if data.get("bin"):
            out.append(Component(Kind.CLI_TOOL, folder, ev + ["package.json bin"], framework="node",
                                 install_command=install, test_commands=tests, build_command=build))
            continue
        if data.get("main") or data.get("exports") or data.get("module"):
            out.append(Component(Kind.LIBRARY, folder, ev + ["package.json main/exports"], framework="node",
                                 install_command=install, test_commands=tests, build_command=build))


def _expo_package(app_json: str) -> str:
    try:
        data = json.loads(app_json) if app_json else {}
        return ((data.get("expo") or {}).get("android") or {}).get("package", "")
    except ValueError:
        return ""


_FASTAPI = re.compile(r"^(\w+)\s*(?::[^=]+)?=\s*FastAPI\(", re.M)
_FLASK = re.compile(r"^(\w+)\s*=\s*Flask\(", re.M)
_FACTORY_CALL = re.compile(r"^(\w+)\s*=\s*(\w+)\(", re.M)
_ROUTE_HEALTH = re.compile(r"""["'](/(?:health|healthz|ready|readyz|status|ping))["']""")
_PY_PROJECT_MARKERS = ("pyproject.toml", "requirements.txt", "setup.py", "manage.py")


def _project_dir(inv: Inventory, rel: str) -> str:
    """Nearest ancestor folder that declares a Python project."""
    folder = _parent(rel)
    while True:
        if any(_join(folder, m) in inv.files for m in _PY_PROJECT_MARKERS):
            return folder
        if folder in {".", ""}:
            return "."
        folder = _parent(folder)


def _module_of(inv: Inventory, project: str, rel: str) -> tuple[str, str]:
    """(import root inside project, dotted module) — handles the src/ layout."""
    inner = rel if project == "." else rel[len(project) + 1:]
    if inner.startswith("src/"):
        return "src", inner[4:-3].replace("/", ".").removesuffix(".__init__")
    return ".", inner[:-3].replace("/", ".").removesuffix(".__init__")


def _python_install(inv: Inventory, project: str) -> list[str]:
    if _join(project, "pyproject.toml") in inv.files:
        try:
            data = tomllib.loads(inv.read(_join(project, "pyproject.toml")))
        except tomllib.TOMLDecodeError:
            data = {}
        extras = list((data.get("project") or {}).get("optional-dependencies") or {})
        wanted = extras  # every extra: the app must boot with its runtime extras, tests need dev extras
        if data.get("project"):
            return ["python", "-m", "pip", "install", "-q", "-e", f".[{','.join(wanted)}]" if wanted else "."]
    if _join(project, "requirements.txt") in inv.files:
        return ["python", "-m", "pip", "install", "-q", "-r", "requirements.txt"]
    return []


def _app_variable(text: str) -> tuple[str, str] | None:
    """Find the ASGI/WSGI app object: direct construction or a module-level factory call."""
    for regex, framework in ((_FASTAPI, "fastapi"), (_FLASK, "flask")):
        match = regex.search(text)
        if match:
            return match.group(1), framework
    for framework, ctor in (("fastapi", "FastAPI("), ("flask", "Flask(")):
        if ctor not in text:
            continue
        for match in _FACTORY_CALL.finditer(text):
            var, func = match.groups()
            if re.search(rf"^def {func}\(", text, re.M) and var in {"app", "application", "api"}:
                return var, framework
    return None


def detect_python(inv: Inventory, out: list[Component]) -> None:
    py_files = [f for f in inv.suffix(".py") if not re.search(r"(^|/)(tests?|test_[^/]+|conftest)\b", f) and "/migrations/" not in f]
    if not py_files:
        return
    has_tests = any(re.search(r"(^|/)tests?/|(^|/)test_\w+\.py$", f) for f in inv.suffix(".py"))
    tests = [["python", "-m", "pytest", "-q"]] if has_tests else []

    found_service = False
    for manage in inv.named("manage.py"):
        if "django" in inv.read(manage):
            project = _parent(manage)
            out.append(Component(Kind.API_SERVICE, project, [f"{manage} (Django)"], framework="django",
                                 install_command=_python_install(inv, project), test_commands=[["python", "manage.py", "test"]],
                                 start_command=["python", "manage.py", "runserver", "127.0.0.1:{port}", "--noreload"]))
            found_service = True
            break
    candidates: list[tuple[int, Component]] = []
    for rel in py_files:
        text = inv.read(rel, limit=300_000)
        found = _app_variable(text) if text else None
        if not found:
            continue
        var, framework = found
        project = _project_dir(inv, rel)
        root, module = _module_of(inv, project, rel)
        health = (_ROUTE_HEALTH.search(text) or [None, ""])[1]
        if framework == "fastapi":
            start = ["python", "-m", "uvicorn", f"{module}:{var}", "--host", "127.0.0.1", "--port", "{port}"]
            openapi = "" if "openapi_url=None" in text.replace(" ", "") else "/openapi.json"
        else:
            start = ["python", "-m", "flask", "--app", f"{module}:{var}", "run", "--host", "127.0.0.1", "--port", "{port}"]
            openapi = ""
        routes = len(re.findall(r"@\w+\.(get|post|put|patch|delete|route)\(", text))
        comp = Component(Kind.API_SERVICE, project, [f"{rel}: '{var}' is a {framework} application ({routes} route(s))"],
                         framework=framework, install_command=_python_install(inv, project), test_commands=tests,
                         start_command=start, health_path=health, ready_path=health or (openapi or "/"),
                         openapi_path=openapi, app_id=f"{module}:{var}", extra={"python_root": root})
        # Prefer the richest entrypoint with a health route; legacy/demo/example folders lose.
        score = routes * 2 + (5 if health else 0) - (40 if re.search(r"(^|/)(legacy|demo|examples?|server|deployment)/", rel) else 0)
        candidates.append((score, comp))
    if candidates and not found_service:
        candidates.sort(key=lambda item: -item[0])
        best = candidates[0][1]
        if len(candidates) > 1:
            best.evidence.append(f"chosen over {len(candidates) - 1} other entrypoint(s): " + ", ".join(c.app_id for _, c in candidates[1:4]))
        out.append(best)
        found_service = True
    if found_service:
        return
    pyproject = {}
    if "pyproject.toml" in inv.files:
        try:
            pyproject = tomllib.loads(inv.read("pyproject.toml"))
        except tomllib.TOMLDecodeError:
            pyproject = {}
    project = pyproject.get("project") or {}
    install = _python_install(inv, ".")
    if project.get("scripts"):
        out.append(Component(Kind.CLI_TOOL, ".", ["pyproject [project.scripts]"], framework="python",
                             install_command=install, test_commands=tests, extra={"scripts": ",".join(project["scripts"])}))
    elif project:
        out.append(Component(Kind.LIBRARY, ".", ["pyproject [project]"], framework="python",
                             install_command=install, test_commands=tests))


_APP_ID = re.compile(r"""applicationId\s*[=(]?\s*["']([\w.]+)["']""")


def detect_android(inv: Inventory, out: list[Component], claimed: set[str]) -> None:
    for gradle in inv.named("build.gradle", "build.gradle.kts"):
        text = inv.read(gradle)
        if "com.android.application" not in text:
            continue
        module = _parent(gradle)
        project = module
        while project not in {".", ""} and not any(_join(project, n) in inv.files for n in ("settings.gradle", "settings.gradle.kts")):
            project = _parent(project)
        if any(project == c or project.startswith(c + "/") for c in claimed):
            continue  # Flutter / React Native own their android folder
        app_id = (_APP_ID.search(text) or [None, ""])[1]
        gradlew = "./gradlew" if _join(project, "gradlew") in inv.files else "gradle"
        mod_name = module[len(project) + 1:] if module != project else "app"
        task = f":{mod_name.replace('/', ':')}"
        out.append(Component(Kind.ANDROID_APP, project, [f"{gradle}: com.android.application"], framework="gradle",
                             build_command=[gradlew, f"{task}:assembleDebug"],
                             test_commands=[[gradlew, f"{task}:testDebugUnitTest"], [gradlew, f"{task}:lintDebug"]],
                             app_id=app_id, extra={"module": module, "gradle_task_prefix": task}))
    if not any(c.kind == Kind.ANDROID_APP for c in out):
        for manifest in inv.named("AndroidManifest.xml"):
            folder = _parent(manifest)
            if "src/main" in manifest and not any(folder.startswith(c) for c in claimed if c != "."):
                text = inv.read(manifest)
                pkg = (re.search(r'package="([\w.]+)"', text) or [None, ""])[1]
                out.append(Component(Kind.ANDROID_APP, folder.split("/src/main")[0].rsplit("/", 1)[0] or ".",
                                     [f"{manifest}"], framework="gradle", app_id=pkg))
                break


def detect_flutter(inv: Inventory, out: list[Component]) -> set[str]:
    claimed: set[str] = set()
    for pubspec in inv.named("pubspec.yaml"):
        text = inv.read(pubspec)
        if not re.search(r"^\s*flutter:\s*$|sdk:\s*flutter", text, re.M):
            continue
        folder = _parent(pubspec)
        if _join(folder, "lib/main.dart") not in inv.files:
            continue
        claimed.add(folder)
        tests = [["flutter", "analyze"], ["flutter", "test"]]
        app_id = ""
        for gradle in ("android/app/build.gradle", "android/app/build.gradle.kts"):
            app_id = app_id or (_APP_ID.search(inv.read(_join(folder, gradle))) or [None, ""])[1]
        out.append(Component(Kind.FLUTTER_APP, folder, [f"{pubspec}: flutter sdk", "lib/main.dart"], framework="flutter",
                             install_command=["flutter", "pub", "get"], test_commands=tests,
                             build_command=["flutter", "build", "apk", "--debug"], app_id=app_id,
                             extra={"integration_tests": str(any(f.startswith(_join(folder, "integration_test/")) for f in inv.files))}))
    return claimed


def detect_ios(inv: Inventory, out: list[Component], claimed: set[str]) -> None:
    projects = sorted({d for d in inv.dirs if d.endswith((".xcworkspace", ".xcodeproj")) and "/Pods/" not in d and ".xcodeproj/" not in d})
    workspaces = [p for p in projects if p.endswith(".xcworkspace") and not p.endswith("project.xcworkspace")]
    chosen = workspaces[0] if workspaces else next((p for p in projects if p.endswith(".xcodeproj")), "")
    if not chosen:
        return
    folder = _parent(chosen)
    if any(folder == c or folder.startswith(c + "/") for c in claimed):
        return
    scheme = chosen.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    flag = "-workspace" if chosen.endswith(".xcworkspace") else "-project"
    out.append(Component(Kind.IOS_APP, folder, [chosen], framework="xcode",
                         build_command=["xcodebuild", flag, chosen.rsplit("/", 1)[-1], "-scheme", scheme, "build"],
                         extra={"project": chosen, "scheme": scheme, "flag": flag}))


def detect_static_site(inv: Inventory, out: list[Component], taken: set[str]) -> None:
    generators = {"_config.yml": "jekyll", "hugo.toml": "hugo", "config.toml": "hugo", "mkdocs.yml": "mkdocs"}
    for name, gen in generators.items():
        if name in inv.files and (gen != "hugo" or "baseURL" in inv.read(name)):
            build = {"jekyll": ["bundle", "exec", "jekyll", "build"], "hugo": ["hugo"], "mkdocs": ["mkdocs", "build"]}[gen]
            out_dir = {"jekyll": "_site", "hugo": "public", "mkdocs": "site"}[gen]
            out.append(Component(Kind.STATIC_SITE, ".", [f"{name} ({gen})"], framework=gen, build_command=build,
                                 start_command=["python", "-m", "http.server", "{port}", "--bind", "127.0.0.1", "--directory", out_dir],
                                 extra={"site_dir": out_dir}))
            return
    for candidate in ("index.html", "public/index.html", "site/index.html", "docs/index.html", "www/index.html", "dist/index.html"):
        folder = _parent(candidate)
        if candidate in inv.files and folder not in taken and not any(folder.startswith(t + "/") for t in taken if t != "."):
            if folder == "." and any(t == "." for t in taken):
                continue
            out.append(Component(Kind.STATIC_SITE, ".", [candidate], framework="html",
                                 start_command=["python", "-m", "http.server", "{port}", "--bind", "127.0.0.1", "--directory", folder],
                                 extra={"site_dir": folder}))
            return


def detect_trading(inv: Inventory, out: list[Component]) -> None:
    experts = inv.suffix(".mq5", ".mq4")
    if experts:
        folder = _parent(experts[0])
        out.append(Component(Kind.TRADING_EA, folder, [f"{len(experts)} MQL file(s), e.g. {experts[0]}"], framework="mql5",
                             extra={"sources": ",".join(experts[:20])}))


def classify(root: Path) -> Classification:
    root = root.resolve()
    inv = Inventory(root)
    components: list[Component] = []
    claimed = detect_flutter(inv, components)
    detect_node(inv, components)
    claimed |= {c.path for c in components if c.kind == Kind.REACT_NATIVE_APP}
    detect_android(inv, components, claimed)
    detect_ios(inv, components, claimed)
    detect_python(inv, components)
    taken = {c.path for c in components if c.kind in (Kind.WEB_APP, Kind.REACT_NATIVE_APP, Kind.FLUTTER_APP)}
    if not any(c.kind == Kind.WEB_APP for c in components):
        detect_static_site(inv, components, taken)
    detect_trading(inv, components)
    # Libraries next to an application in the same folder are the app's own package, not a product.
    app_paths = {c.path for c in components if c.kind not in (Kind.LIBRARY, Kind.CLI_TOOL)}
    components = [c for c in components if not (c.kind == Kind.LIBRARY and c.path in app_paths)]
    order = list(Kind)
    components.sort(key=lambda c: (order.index(c.kind), c.path))
    return Classification(str(root), components)
