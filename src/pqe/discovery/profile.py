"""Project discovery: what is this project, how is it built, what does it promise?

Discovery is deterministic and read-only. It produces a :class:`ProjectProfile`
that ``pqe init`` turns into a *draft* contract. Mapping goals to evidence is a
reasoning task done with a human (or the Claude Code ``pqe-onboard`` skill),
because only the project's documents — not its code — can say what "correct"
means.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..context import ALWAYS_EXCLUDED_DIRS

LANGUAGE_BY_EXT = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".js": "javascript",
    ".mjs": "javascript", ".jsx": "javascript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".cs": "csharp", ".php": "php", ".rb": "ruby", ".dart": "dart", ".swift": "swift", ".mq5": "mql5",
    ".mqh": "mql5", ".sql": "sql", ".sh": "shell", ".vue": "vue", ".svelte": "svelte",
}
PY_FRAMEWORKS = ["fastapi", "flask", "django", "starlette", "aiohttp", "pytest", "pydantic", "sqlalchemy", "celery"]
NODE_FRAMEWORKS = ["next", "react", "vue", "svelte", "vite", "express", "@nestjs/core", "jest", "vitest",
                   "@playwright/test", "playwright", "cypress", "typescript", "eslint", "drizzle-orm", "wrangler", "vinext"]
GOAL_HEADING = re.compile(r"(?im)^#{1,4}\s*(?:\d+[.)]?\s*)?(purpose|mission|goal|goals|objective|vision|هدف|اهداف|ماموریت|مأموریت|چشم.?انداز)\b.*$")
REQ_ID = re.compile(r"\b([A-Z]{1,6}(?:-[A-Z]{1,6})?)-(\d{2,3}[A-Z]?)\b")


@dataclass
class DocInfo:
    path: str
    title: str
    bytes: int


@dataclass
class ProjectProfile:
    root: str
    name: str
    languages: dict[str, int] = field(default_factory=dict)
    frameworks: list[str] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    python_roots: list[str] = field(default_factory=list)
    test_dirs: list[str] = field(default_factory=list)
    node_projects: dict[str, dict[str, str]] = field(default_factory=dict)
    lockfiles: list[str] = field(default_factory=list)
    ci_workflows: list[str] = field(default_factory=list)
    containers: list[str] = field(default_factory=list)
    app_entrypoints: list[str] = field(default_factory=list)
    docs: list[DocInfo] = field(default_factory=list)
    requirement_families: dict[str, dict[str, object]] = field(default_factory=dict)
    goal_statements: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, ensure_ascii=False)


def _walk(root: Path, max_files: int = 50_000):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ALWAYS_EXCLUDED_DIRS and not d.startswith(".") or d == ".github"]
        for name in filenames:
            count += 1
            if count > max_files:
                return
            yield Path(dirpath) / name


def _python_deps(root: Path) -> set[str]:
    deps: set[str] = set()
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project", {})
            entries = list(project.get("dependencies", []))
            for extra in project.get("optional-dependencies", {}).values():
                entries += extra
            deps |= {re.split(r"[\s<>=!~;\[]", e, maxsplit=1)[0].lower() for e in entries}
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            pass
    for req in root.glob("requirements*.txt"):
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith(("#", "-")):
                deps.add(re.split(r"[\s<>=!~;\[]", line, maxsplit=1)[0].lower())
    return deps


def discover(root: Path) -> ProjectProfile:
    root = root.resolve()
    profile = ProjectProfile(root=str(root), name=root.name)
    langs: Counter[str] = Counter()
    families: dict[str, set[str]] = {}
    family_files: dict[str, Counter[str]] = {}
    for path in _walk(root):
        rel = path.relative_to(root).as_posix()
        lang = LANGUAGE_BY_EXT.get(path.suffix.lower())
        if lang:
            langs[lang] += 1
        name = path.name
        if name in {"pyproject.toml", "requirements.txt", "package.json", "go.mod", "Cargo.toml", "pom.xml",
                    "build.gradle", "composer.json", "Gemfile", "pubspec.yaml", "setup.py"}:
            profile.manifests.append(rel)
        if name in {"pnpm-lock.yaml", "package-lock.json", "yarn.lock", "poetry.lock", "uv.lock", "Cargo.lock", "go.sum"}:
            profile.lockfiles.append(rel)
        if rel.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
            profile.ci_workflows.append(rel)
        if name.startswith("Dockerfile") or name in {"compose.yaml", "compose.yml", "docker-compose.yml"}:
            profile.containers.append(rel)
        if path.suffix == ".py" and path.stat().st_size < 400_000:
            text = path.read_text(encoding="utf-8", errors="replace")
            if re.search(r"\b(FastAPI|Flask|Starlette)\(", text) and "test" not in rel:
                profile.app_entrypoints.append(rel)
        if path.suffix.lower() in {".md", ".markdown", ".txt"} and path.stat().st_size < 2_000_000 and "node_modules" not in rel:
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix.lower() != ".txt":
                heading = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), name)
                profile.docs.append(DocInfo(rel, heading[:120], path.stat().st_size))
                for match in GOAL_HEADING.finditer(text):
                    after = text[match.end():].strip().split("\n\n")[0].strip()
                    if after and len(profile.goal_statements) < 12:
                        profile.goal_statements.append(f"{rel}: {after[:400]}")
            for match in REQ_ID.finditer(text):
                prefix, ident = match.group(1), match.group(0)
                families.setdefault(prefix, set()).add(ident)
                family_files.setdefault(prefix, Counter())[rel] += 1
    profile.languages = dict(langs.most_common())
    py_deps = _python_deps(root)
    profile.frameworks += [f for f in PY_FRAMEWORKS if f in py_deps]
    for manifest in [m for m in profile.manifests if m.endswith("package.json")]:
        try:
            data = json.loads((root / manifest).read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        profile.frameworks += [f for f in NODE_FRAMEWORKS if f in deps and f not in profile.frameworks]
        profile.node_projects[str(Path(manifest).parent.as_posix())] = dict(data.get("scripts", {}))
    for candidate in ("src", "lib", "app", "server"):
        folder = root / candidate
        if folder.is_dir() and any(folder.rglob("*.py")):
            profile.python_roots.append(candidate)
    if not profile.python_roots and langs.get("python"):
        profile.python_roots.append(".")
    for candidate in ("tests", "test", "__tests__", "spec", "e2e"):
        if (root / candidate).is_dir():
            profile.test_dirs.append(candidate)
    for prefix, ids in families.items():
        if len(ids) >= 3:
            top_files = [f for f, _ in family_files[prefix].most_common(3)]
            profile.requirement_families[prefix] = {"distinct_ids": len(ids), "examples": sorted(ids)[:6], "defined_in": top_files}
    profile.docs.sort(key=lambda d: d.path)
    return profile
