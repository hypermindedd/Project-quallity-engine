"""Functional runners: the project's own test suites and declared commands."""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: S405 - parses files produced by our own pytest run
from pathlib import Path

from ..context import RunContext, ToolMissing
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status, TestCase
from .base import Runner, register


def parse_junit(xml_path: Path) -> list[TestCase]:
    """Parse JUnit XML (pytest, jest-junit, vitest, node --test reporters)."""
    tree = ET.parse(xml_path)  # noqa: S314  # nosec B314 - JUnit file written by our own test run
    cases: list[TestCase] = []
    for case in tree.iter("testcase"):
        name = case.get("name", "?")
        classname = case.get("classname", "")
        file = case.get("file") or ""
        if not file and classname:
            # pytest xunit2 omits 'file'; derive it from the dotted classname.
            parts = classname.split(".")
            while parts and parts[-1][:1].isupper():  # drop TestClass components
                parts.pop()
            file = "/".join(parts) + ".py" if parts else classname
        klass = ""
        if classname:
            tail = classname.split(".")[-1]
            if tail[:1].isupper():
                klass = tail + "::"
        status = Status.PASS
        message = ""
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                status = Status.FAIL
                message = (node.get("message") or node.text or "").strip()[:2000]
        if case.find("skipped") is not None:
            status = Status.SKIPPED
            skipped = case.find("skipped")
            message = (skipped.get("message") or "").strip() if skipped is not None else ""
        try:
            duration = float(case.get("time", "0") or 0)
        except ValueError:
            duration = 0.0
        cases.append(TestCase(f"{file}::{klass}{name}", file, name, status, duration, message))
    return cases


@register
class PytestRunner(Runner):
    id = "functional.pytest"
    title = "Python test suite"
    dimension = Dimension.FUNCTIONAL
    phase = 20

    def applicable(self, ctx: RunContext) -> str | None:
        spec = ctx.config.pytest
        if not spec.enabled:
            return "disabled in contract"
        root = ctx.root / spec.cwd
        markers = ["pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "tests", "test"]
        if not any((root / m).exists() for m in markers):
            return "no Python test layout detected"
        return None

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        spec = ctx.config.pytest
        junit = ctx.out_dir / "pytest-junit.xml"
        argv = [*spec.argv, f"--junitxml={junit}", "-o", "junit_family=xunit1", "-p", "no:cacheprovider"]
        proc = ctx.run(argv, cwd=spec.cwd, env=ctx.build_env(spec.env), timeout_s=spec.timeout_s)
        (ctx.out_dir / "pytest.log").write_text(ctx.redact(proc.stdout + "\n" + proc.stderr), encoding="utf-8")
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS, artifacts=["pytest.log", "pytest-junit.xml"])
        if proc.timed_out:
            result.status = Status.ERROR
            result.summary = f"timed out after {spec.timeout_s}s"
            return [result]
        if not junit.exists():
            result.status = Status.ERROR
            result.summary = "pytest produced no JUnit report:\n" + proc.tail()
            return [result]
        result.tests = parse_junit(junit)
        failed = [t for t in result.tests if t.status == Status.FAIL]
        passed = sum(1 for t in result.tests if t.status == Status.PASS)
        skipped = sum(1 for t in result.tests if t.status == Status.SKIPPED)
        result.metrics = {"tests": len(result.tests), "passed": passed, "failed": len(failed), "skipped": skipped}
        if proc.returncode == 5 or not result.tests:
            result.status = Status.ERROR
            result.summary = "no tests were collected"
        elif failed or proc.returncode != 0:
            result.status = Status.FAIL
            result.summary = f"{len(failed)} of {len(result.tests)} tests failed"
            for t in failed:
                result.findings.append(
                    Finding(
                        self.id,
                        self.dimension,
                        Severity.SEV1,
                        f"Test failed: {t.id}",
                        detail=t.message,
                        location=Location(t.file),
                        evidence={"key": t.id},
                    )
                )
            if not failed:
                result.findings.append(
                    Finding(self.id, self.dimension, Severity.SEV1, "pytest exited non-zero", detail=proc.tail())
                )
        else:
            result.summary = f"{passed} passed, {skipped} skipped"
        return [result]


@register
class CommandsRunner(Runner):
    """Generic language-agnostic check: any command declared in the contract.

    This is how the engine covers stacks it has no dedicated runner for
    (TypeScript, Go, Rust, MQL5 compilers, framework builds, custom scripts).
    Exit code 0 is PASS, anything else FAIL.
    """

    id = "commands"
    title = "Declared project commands"
    dimension = Dimension.BUILD
    phase = 15

    def applicable(self, ctx: RunContext) -> str | None:
        if not any(c.enabled for c in ctx.config.commands):
            return "no commands declared in contract"
        return None

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        results = []
        for spec in [c for c in ctx.config.commands if ctx.in_target(c.target)]:
            check_id = f"command.{spec.id}"
            if not spec.enabled:
                results.append(CheckResult.skipped(check_id, spec.title, spec.dimension, "disabled in contract"))
                continue
            missing = [r for r in spec.requires if not (ctx.root / r).exists()]
            if missing:
                results.append(
                    CheckResult.skipped(
                        check_id, spec.title, spec.dimension, f"prerequisite missing: {', '.join(missing)} (run setup)"
                    )
                )
                continue
            try:
                proc = ctx.run(spec.argv, cwd=spec.cwd, env=ctx.build_env(spec.env), timeout_s=spec.timeout_s)
            except (ToolMissing, OSError) as exc:  # one broken command must not stop the others
                status = Status.SKIPPED if isinstance(exc, ToolMissing) else Status.ERROR
                results.append(CheckResult(check_id, spec.title, spec.dimension, status, summary=f"cannot execute: {exc}"))
                continue
            log_name = f"command-{spec.id}.log"
            (ctx.out_dir / log_name).write_text(ctx.redact(proc.stdout + "\n" + proc.stderr), encoding="utf-8")
            status = Status.PASS if proc.ok else Status.FAIL
            result = CheckResult(
                check_id,
                spec.title,
                spec.dimension,
                status,
                summary="ok" if proc.ok else ("timed out" if proc.timed_out else f"exit code {proc.returncode}"),
                artifacts=[log_name],
                duration_s=proc.duration_s,
                tests=[TestCase(check_id, spec.cwd, spec.title, status, proc.duration_s)],
            )
            if not proc.ok:
                result.findings.append(
                    Finding(
                        check_id,
                        spec.dimension,
                        spec.severity,
                        f"Command failed: {spec.title}",
                        detail=ctx.redact(proc.tail()),
                        evidence={"key": spec.id, "argv": spec.argv},
                    )
                )
            results.append(result)
        return results
