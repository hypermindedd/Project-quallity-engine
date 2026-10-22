"""Architecture conformance.

The architecture recorded in the contract is *law*: the engine detects
violations and drift, it never rewrites the rules to make code pass.

* ``architecture.boundaries`` – forbidden imports between modules (Python AST,
  so it sees exactly what the interpreter would import, including relative
  imports) and dependency cycles between top-level packages.
* ``architecture.mirrors`` – trees that must stay byte-identical copies (for
  example a vendored deployment copy of the core) must not drift.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..context import ALWAYS_EXCLUDED_DIRS, RunContext
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status
from .base import Runner, register


@dataclass(frozen=True)
class ImportEdge:
    module: str
    target: str
    path: str
    line: int


def module_name(root: Path, file: Path) -> str:
    rel = file.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def resolve_relative(module: str, is_package: bool, level: int, target: str | None) -> str:
    """Resolve ``from ..x import y`` into an absolute dotted name."""
    base = module.split(".") if is_package else module.split(".")[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)] if level - 1 <= len(base) else []
    if target:
        base = [*base, *target.split(".")]
    return ".".join(base)


def collect_imports(source_root: Path, rel_root: Path) -> tuple[list[ImportEdge], list[str]]:
    """Return import edges for every module under ``source_root`` plus parse errors."""
    edges: list[ImportEdge] = []
    errors: list[str] = []
    for file in sorted(source_root.rglob("*.py")):
        if any(part in ALWAYS_EXCLUDED_DIRS for part in file.parts):
            continue
        module = module_name(source_root, file)
        rel_path = file.relative_to(rel_root).as_posix()
        try:
            tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        except (SyntaxError, UnicodeDecodeError) as exc:
            errors.append(f"{rel_path}: {exc}")
            continue
        is_package = file.name == "__init__.py"
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    edges.append(ImportEdge(module, alias.name, rel_path, node.lineno))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    target = resolve_relative(module, is_package, node.level, node.module)
                else:
                    target = node.module or ""
                # "from pkg import sub" may import a submodule: record both forms.
                edges.append(ImportEdge(module, target, rel_path, node.lineno))
                for alias in node.names:
                    if alias.name != "*":
                        edges.append(ImportEdge(module, f"{target}.{alias.name}", rel_path, node.lineno))
    return edges, errors


def _within(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components; returns components of size > 1."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    result: list[list[str]] = []
    counter = [0]

    def strong(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt not in index:
                strong(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            component = []
            while True:
                item = stack.pop()
                on_stack.discard(item)
                component.append(item)
                if item == node:
                    break
            if len(component) > 1:
                result.append(sorted(component))

    for node in sorted(graph):
        if node not in index:
            strong(node)
    return result


@register
class BoundaryRunner(Runner):
    id = "architecture.boundaries"
    title = "Module boundaries and dependency cycles"
    dimension = Dimension.ARCHITECTURE
    phase = 10

    def applicable(self, ctx: RunContext) -> str | None:
        arch = ctx.config.architecture
        if not arch.python_roots:
            return "no python_roots declared in contract"
        if not arch.rules and not arch.detect_cycles:
            return "no boundary rules and cycle detection disabled"
        return None

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        arch = ctx.config.architecture
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        all_edges: list[ImportEdge] = []
        for root in arch.python_roots:
            source_root = ctx.root / root
            if not source_root.is_dir():
                result.findings.append(
                    Finding(self.id, self.dimension, Severity.SEV2, f"Declared python root missing: {root}", location=Location(root))
                )
                continue
            edges, errors = collect_imports(source_root, ctx.root)
            all_edges.extend(edges)
            for err in errors:
                result.findings.append(Finding(self.id, self.dimension, Severity.SEV1, "Module cannot be parsed", detail=err))

        seen: set[tuple[str, str, int]] = set()
        for rule in arch.rules:
            for edge in all_edges:
                if not _within(edge.module, rule.source):
                    continue
                for forbidden in rule.forbid:
                    if _within(edge.target, forbidden) and (rule.name, edge.path, edge.line) not in seen:
                        seen.add((rule.name, edge.path, edge.line))
                        result.findings.append(
                            Finding(
                                self.id,
                                self.dimension,
                                rule.severity,
                                f"Boundary '{rule.name}' violated: {edge.module} imports {edge.target}",
                                detail=rule.reason,
                                location=Location(edge.path, edge.line),
                                evidence={"key": f"{rule.name}:{edge.module}->{forbidden}", "rule": rule.name},
                                suggestion=f"Route this dependency through an allowed module; '{rule.source}' must not depend on '{forbidden}'.",
                            )
                        )

        cycles: list[list[str]] = []
        if arch.detect_cycles:
            internal_tops = {e.module.split(".")[0] for e in all_edges}
            graph: dict[str, set[str]] = {}
            for edge in all_edges:
                src = ".".join(edge.module.split(".")[:2])
                dst_parts = edge.target.split(".")
                if dst_parts[0] not in internal_tops:
                    continue
                dst = ".".join(dst_parts[:2])
                if src != dst and len(dst_parts) >= 2:
                    graph.setdefault(src, set()).add(dst)
            known = {".".join(e.module.split(".")[:2]) for e in all_edges}
            graph = {k: {v for v in vs if v in known} for k, vs in graph.items()}
            cycles = find_cycles(graph)
            for cycle in cycles:
                result.findings.append(
                    Finding(
                        self.id,
                        self.dimension,
                        Severity.SEV2,
                        "Dependency cycle between packages: " + " <-> ".join(cycle),
                        evidence={"key": "cycle:" + ",".join(cycle)},
                        suggestion="Break the cycle by moving the shared contract into a lower layer both can depend on.",
                    )
                )
        result.metrics = {"import_edges": len(all_edges), "rules": len(arch.rules), "cycles": len(cycles)}
        if result.findings:
            result.status = Status.FAIL
        result.summary = (
            f"{len(result.findings)} issue(s) across {len(arch.rules)} rule(s)" if result.findings else f"{len(arch.rules)} rule(s) respected, no cycles"
        )
        return [result]


def tree_digest(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for file in sorted(root.rglob("*")):
        if not file.is_file() or any(p in ALWAYS_EXCLUDED_DIRS for p in file.parts) or file.suffix == ".pyc":
            continue
        digests[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    return digests


@register
class MirrorRunner(Runner):
    id = "architecture.mirrors"
    title = "Mirrored source trees stay identical"
    dimension = Dimension.ARCHITECTURE
    phase = 10

    def applicable(self, ctx: RunContext) -> str | None:
        return None if ctx.config.architecture.mirrors else "no mirrors declared in contract"

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        compared = 0
        for spec in ctx.config.architecture.mirrors:
            src, dst = ctx.root / spec.source, ctx.root / spec.mirror
            for path in (src, dst):
                if not path.is_dir():
                    result.findings.append(
                        Finding(self.id, self.dimension, spec.severity, f"Mirror path missing: {ctx.rel(path)}", location=Location(ctx.rel(path)))
                    )
            if not (src.is_dir() and dst.is_dir()):
                continue
            a, b = tree_digest(src), tree_digest(dst)
            compared += len(a)
            for rel in sorted(set(a) | set(b)):
                if a.get(rel) == b.get(rel):
                    continue
                state = "missing in mirror" if rel not in b else "only in mirror" if rel not in a else "content differs"
                result.findings.append(
                    Finding(
                        self.id,
                        self.dimension,
                        spec.severity,
                        f"Mirror drift ({state}): {rel}",
                        detail=f"{spec.source} vs {spec.mirror}",
                        location=Location(f"{spec.mirror}/{rel}"),
                        evidence={"key": f"{spec.mirror}/{rel}:{state}"},
                        suggestion=f"Regenerate {spec.mirror} from {spec.source} with the project's packaging script; never hand-edit the mirror.",
                    )
                )
        result.metrics = {"files_compared": compared, "drifted": len(result.findings)}
        if result.findings:
            result.status = Status.FAIL
            result.summary = f"{len(result.findings)} drifted file(s)"
        else:
            result.summary = f"{compared} file(s) identical"
        return [result]
