"""Static security: committed secrets, SAST and vulnerable dependencies.

Every external scanner is optional. When it is not installed the check is
SKIPPED with the reason, never PASS. The built-in secret scanner always runs
so the most damaging class of defect is covered with zero dependencies.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..context import RunContext, match_any
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status
from .base import Runner, register

# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SecretPattern:
    name: str
    regex: re.Pattern[str]
    severity: Severity
    min_entropy: float = 0.0


SECRET_PATTERNS = [
    SecretPattern("Private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"), Severity.SEV0),
    SecretPattern("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), Severity.SEV0),
    SecretPattern("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"), Severity.SEV0),
    SecretPattern("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{30,}"), Severity.SEV0, 3.5),
    SecretPattern("OpenAI API key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_\-]{32,}"), Severity.SEV0, 3.8),
    SecretPattern("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), Severity.SEV0),
    SecretPattern("Stripe live key", re.compile(r"\b(?:sk|rk)_live_[0-9a-zA-Z]{24,}"), Severity.SEV0),
    SecretPattern("Google API key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"), Severity.SEV1),
    SecretPattern("Notion integration token", re.compile(r"\b(?:secret_|ntn_)[A-Za-z0-9]{40,}"), Severity.SEV0, 3.5),
    SecretPattern(
        "Hard-coded credential",
        re.compile(r"""(?i)\b(?:api[_-]?key|secret|token|passw(?:or)?d|auth)\b["']?\s*[:=]\s*["']([^"'\s]{20,})["']"""),
        Severity.SEV1,
        3.8,
    ),
]

GENERIC_PATTERN = SECRET_PATTERNS[-1]
if GENERIC_PATTERN.name != "Hard-coded credential":  # it must stay last: it only fills gaps
    raise RuntimeError("secret pattern order changed: the generic credential rule must be last")

_PLACEHOLDER = re.compile(r"(?i)(x{4,}|your[_-]|example|changeme|placeholder|dummy|<[^>]+>|\$\{|\{\{|test|fake|sample|redacted|\*{3,})")
_ALLOW_MARKER = "pqe:allow-secret"
_SKIP_SUFFIXES = (".lock", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".zip", ".pdf", ".pyc", ".svg")


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    return -sum((c / len(value)) * math.log2(c / len(value)) for c in counts.values())


def scan_text(text: str) -> list[tuple[int, SecretPattern, str]]:
    hits: list[tuple[int, SecretPattern, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if _ALLOW_MARKER in line or len(line) > 4000:
            continue
        specific_hit = False
        for pattern in SECRET_PATTERNS:
            if pattern is GENERIC_PATTERN and specific_hit:
                continue  # one secret, one finding: the specific rule already named it
            for match in pattern.regex.finditer(line):
                value = match.group(1) if match.groups() else match.group(0)
                if _PLACEHOLDER.search(value):
                    continue
                if pattern.min_entropy and shannon_entropy(value) < pattern.min_entropy:
                    continue
                hits.append((lineno, pattern, value))
                specific_hit = specific_hit or pattern is not GENERIC_PATTERN
    return hits


def tracked_files(ctx: RunContext) -> list[Path]:
    """Files that are (or would be) committed; falls back to a full walk."""
    if (ctx.root / ".git").exists() and ctx.which("git"):
        proc = ctx.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], timeout_s=120)
        if proc.ok:
            files = []
            for rel in proc.stdout.split("\0"):
                if rel and not ctx.is_excluded(rel):
                    path = ctx.root / rel
                    if path.is_file():
                        files.append(path)
            return files
    return list(ctx.iter_files())


@register
class SecretsRunner(Runner):
    id = "security.secrets"
    title = "No secrets committed"
    dimension = Dimension.SECURITY_SECRETS
    phase = 5

    def applicable(self, ctx: RunContext) -> str | None:
        return None if ctx.config.security.secrets else "disabled in contract"

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        allow = ctx.config.security.secrets_allowlist
        scanned = 0
        for path in tracked_files(ctx):
            rel = ctx.rel(path)
            if match_any(rel, allow) or rel.endswith(_SKIP_SUFFIXES):
                continue
            name = path.name
            if name.startswith(".env") and not any(s in name for s in ("example", "sample", "template")):
                result.findings.append(
                    Finding(self.id, self.dimension, Severity.SEV1, f"Environment file is tracked: {rel}", location=Location(rel),
                            evidence={"key": rel}, suggestion="Remove it from version control, rotate anything it contained, and add it to .gitignore.")
                )
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:4096]:
                continue
            scanned += 1
            for lineno, pattern, value in scan_text(raw.decode("utf-8", errors="replace")):
                masked = value[:4] + "…" + f"({len(value)} chars)"
                result.findings.append(
                    Finding(
                        self.id, self.dimension, pattern.severity, f"{pattern.name} in source", detail=f"value {masked}",
                        location=Location(rel, lineno), evidence={"key": f"{rel}:{lineno}:{pattern.name}"},
                        suggestion="Rotate the credential now, remove it from history, and load it from the environment or a secret store.",
                    )
                )
        result.metrics = {"files_scanned": scanned, "secrets": len(result.findings)}
        if result.findings:
            result.status = Status.FAIL
            result.summary = f"{len(result.findings)} potential secret(s)"
        else:
            result.summary = f"{scanned} file(s) clean"
        return [result]


# --------------------------------------------------------------------------- #
# SAST
# --------------------------------------------------------------------------- #

_BANDIT_SEVERITY = {"HIGH": Severity.SEV1, "MEDIUM": Severity.SEV2, "LOW": Severity.SEV3}


@register
class BanditRunner(Runner):
    id = "security.sast.python"
    title = "Python static security analysis (bandit)"
    dimension = Dimension.SECURITY_SAST
    phase = 30

    def _paths(self, ctx: RunContext) -> list[str]:
        sec = ctx.config.security
        return sec.bandit_paths or ctx.config.architecture.python_roots

    def applicable(self, ctx: RunContext) -> str | None:
        if not ctx.config.security.bandit:
            return "disabled in contract"
        if not self._paths(ctx):
            return "no Python source paths (security.bandit_paths or architecture.python_roots)"
        return None

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        paths = self._paths(ctx)
        proc = ctx.run(["bandit", "-r", *paths, "-f", "json", "-q", "--severity-level", "low"], timeout_s=900)
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return [CheckResult.errored(self.id, self.title, self.dimension, "bandit returned non-JSON output:\n" + proc.tail())]
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        low = 0
        for issue in data.get("results", []):
            severity = _BANDIT_SEVERITY.get(issue.get("issue_severity", "LOW"), Severity.SEV3)
            confidence = issue.get("issue_confidence", "LOW")
            if severity == Severity.SEV3 or confidence == "LOW":
                low += 1
                continue
            rel = ctx.rel(Path(issue.get("filename", "")).resolve()) if issue.get("filename") else ""
            result.findings.append(
                Finding(
                    self.id, self.dimension, severity, f"{issue.get('test_id')}: {issue.get('issue_text')}",
                    detail=(issue.get("code") or "").strip()[:600], location=Location(rel, issue.get("line_number")),
                    evidence={"key": f"{issue.get('test_id')}:{rel}:{issue.get('line_number')}", "cwe": (issue.get("issue_cwe") or {}).get("id")},
                    suggestion=issue.get("more_info", ""),
                )
            )
        result.metrics = {"reported": len(result.findings), "low_or_unconfident": low}
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(result.findings)} medium/high issue(s); {low} low/unconfident observation(s)"
        return [result]


_SEMGREP_SEVERITY = {"ERROR": Severity.SEV1, "WARNING": Severity.SEV2, "INFO": Severity.SEV3}


@register
class SemgrepRunner(Runner):
    id = "security.sast.semgrep"
    title = "Multi-language static analysis (semgrep)"
    dimension = Dimension.SECURITY_SAST
    phase = 30

    def applicable(self, ctx: RunContext) -> str | None:
        return None if ctx.config.security.semgrep_configs else "no semgrep_configs declared in contract"

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        argv = ["semgrep", "scan", "--json", "--quiet", "--metrics=off"]
        for cfg in ctx.config.security.semgrep_configs:
            argv += ["--config", cfg]
        proc = ctx.run([*argv, "."], timeout_s=1800)
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return [CheckResult.errored(self.id, self.title, self.dimension, "semgrep returned non-JSON output:\n" + proc.tail())]
        if data.get("errors") and not data.get("results"):
            return [CheckResult.errored(self.id, self.title, self.dimension, json.dumps(data["errors"])[:1500])]
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        for item in data.get("results", []):
            extra = item.get("extra", {})
            path = item.get("path", "")
            line = (item.get("start") or {}).get("line")
            result.findings.append(
                Finding(
                    self.id, self.dimension, _SEMGREP_SEVERITY.get(extra.get("severity", "INFO"), Severity.SEV3),
                    f"{item.get('check_id')}: {extra.get('message', '')[:200]}", location=Location(path, line),
                    evidence={"key": f"{item.get('check_id')}:{path}:{line}"},
                )
            )
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(result.findings)} finding(s)"
        return [result]


# --------------------------------------------------------------------------- #
# Dependencies
# --------------------------------------------------------------------------- #


@register
class PipAuditRunner(Runner):
    id = "security.dependencies.python"
    title = "Known vulnerabilities in Python dependencies (pip-audit)"
    dimension = Dimension.SECURITY_DEPENDENCIES
    phase = 30

    def _requirements(self, ctx: RunContext) -> list[str]:
        configured = ctx.config.security.pip_audit_requirements
        if configured:
            return configured
        found = [ctx.rel(p) for p in ctx.glob(["requirements*.txt"])]
        if not found and (ctx.root / "pyproject.toml").exists():
            found = ["pyproject.toml"]  # audited as a project (pip-audit resolves its declared dependencies)
        return found

    def applicable(self, ctx: RunContext) -> str | None:
        if not ctx.config.security.pip_audit:
            return "disabled in contract"
        if not self._requirements(ctx):
            return "no requirements files to audit (security.pip_audit_requirements)"
        return None

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        audited = 0
        errors = []
        for req in self._requirements(ctx):
            if req.endswith("pyproject.toml"):
                argv = ["pip-audit", str((ctx.root / req).parent), "-f", "json", "--progress-spinner", "off"]
            else:
                argv = ["pip-audit", "-r", req, "-f", "json", "--progress-spinner", "off", "--no-deps", "--disable-pip"]
            proc = ctx.run(argv, timeout_s=900)
            try:
                data = json.loads(proc.stdout or "")
            except json.JSONDecodeError:
                errors.append(f"{req}: {proc.tail(8)}")
                continue
            deps = data.get("dependencies", data if isinstance(data, list) else [])
            for dep in deps:
                audited += 1
                for vuln in dep.get("vulns", []):
                    fixes = ", ".join(vuln.get("fix_versions", [])) or "no fixed version published"
                    result.findings.append(
                        Finding(
                            self.id, self.dimension, Severity.SEV1, f"{dep.get('name')} {dep.get('version')}: {vuln.get('id')}",
                            detail=(vuln.get("description") or "")[:500], location=Location(req),
                            evidence={"key": f"{dep.get('name')}:{vuln.get('id')}", "aliases": vuln.get("aliases", [])},
                            suggestion=f"Upgrade to: {fixes}",
                        )
                    )
        result.metrics = {"dependencies": audited, "vulnerabilities": len(result.findings)}
        if errors and not audited:
            return [CheckResult.errored(self.id, self.title, self.dimension, "pip-audit could not run: " + " | ".join(errors))]
        result.status = Status.FAIL if result.findings else Status.PASS
        result.summary = f"{len(result.findings)} known vulnerability(ies) in {audited} dependency(ies)"
        if errors:
            result.summary += f"; {len(errors)} file(s) not audited"
        return [result]


_NPM_SEVERITY = {"critical": Severity.SEV1, "high": Severity.SEV1, "moderate": Severity.SEV2, "low": Severity.SEV3, "info": Severity.INFO}


def parse_node_audit(data: dict) -> list[tuple[str, str, Severity, str]]:
    """Normalise npm (v7+) and pnpm audit JSON into (package, id, severity, title)."""
    items: list[tuple[str, str, Severity, str]] = []
    if "vulnerabilities" in data:  # npm
        for name, vuln in data["vulnerabilities"].items():
            titles = [v.get("title", "") for v in vuln.get("via", []) if isinstance(v, dict)]
            items.append((name, vuln.get("range", ""), _NPM_SEVERITY.get(vuln.get("severity", "low"), Severity.SEV3), "; ".join(titles) or "vulnerable dependency"))
    for adv_id, adv in (data.get("advisories") or {}).items():  # pnpm / npm v6
        items.append((adv.get("module_name", "?"), str(adv_id), _NPM_SEVERITY.get(adv.get("severity", "low"), Severity.SEV3), adv.get("title", "")))
    return items


@register
class NodeAuditRunner(Runner):
    id = "security.dependencies.node"
    title = "Known vulnerabilities in Node dependencies"
    dimension = Dimension.SECURITY_DEPENDENCIES
    phase = 30

    def applicable(self, ctx: RunContext) -> str | None:
        return None if ctx.config.security.npm_audit_dirs else "no npm_audit_dirs declared in contract"

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        skipped = []
        for rel in ctx.config.security.npm_audit_dirs:
            folder = ctx.root / rel
            prod = ctx.config.security.npm_audit_production_only
            if (folder / "pnpm-lock.yaml").exists():
                tool = ["pnpm", "audit", "--json", *(["--prod"] if prod else [])]
            elif (folder / "package-lock.json").exists():
                tool = ["npm", "audit", "--json", *(["--omit=dev"] if prod else [])]
            else:
                skipped.append(f"{rel}: no lockfile")
                continue
            if not ctx.which(tool[0]) and tool[0] in {"pnpm", "yarn"} and ctx.which("corepack"):
                tool = ["corepack", *tool]  # same pinned package manager the project uses, nothing preinstalled
            if not ctx.which(tool[0]):
                skipped.append(f"{rel}: {tool[0]} not installed")
                continue
            proc = ctx.run(tool, cwd=rel, timeout_s=600)
            try:
                data = json.loads(proc.stdout or "{}")
            except json.JSONDecodeError:
                skipped.append(f"{rel}: audit output unreadable ({proc.tail(3)})")
                continue
            for name, ident, severity, title in parse_node_audit(data):
                result.findings.append(
                    Finding(self.id, self.dimension, severity, f"{name}: {title}"[:200], location=Location(f"{rel}/{'pnpm-lock.yaml' if 'pnpm' in tool else 'package-lock.json'}"),
                            evidence={"key": f"{rel}:{name}:{ident}"}, suggestion="Upgrade or override the affected package and re-run the audit.")
                )
        if skipped and not result.findings and len(skipped) == len(ctx.config.security.npm_audit_dirs):
            return [CheckResult.skipped(self.id, self.title, self.dimension, "; ".join(skipped))]
        result.status = Status.FAIL if any(f.severity != Severity.INFO for f in result.findings) else Status.PASS
        result.summary = f"{len(result.findings)} advisory(ies)" + (f"; not audited: {'; '.join(skipped)}" if skipped else "")
        return [result]
