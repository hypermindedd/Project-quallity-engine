"""Integrity and documentation consistency."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..context import RunContext, match_any
from ..model import CheckResult, Dimension, Finding, Location, Severity, Status
from .base import Runner, register

_SUM_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$")
_CODE_SPAN = re.compile(r"`([^`\n]{3,200})`")
_PATHLIKE = re.compile(r"^[A-Za-z0-9_.\-/\[\]]+$")
_KNOWN_EXT = re.compile(
    r"\.(py|ts|tsx|js|mjs|mts|cjs|json|toml|ya?ml|md|txt|sh|command|css|html|sql|zip|png|jpe?g|webp|svg|woff2?|ttf|lock|cfg|ini|xml|mq5|mqh|go|rs|java|kt|swift|dart|rb|php)$"
)


@register
class ChecksumRunner(Runner):
    id = "integrity.checksums"
    title = "Checksum manifests match files"
    dimension = Dimension.INTEGRITY
    phase = 10

    def applicable(self, ctx: RunContext) -> str | None:
        return None if ctx.config.checksum_files else "no checksum_files declared in contract"

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        verified = 0
        for manifest_rel in ctx.config.checksum_files:
            manifest = ctx.root / manifest_rel
            if not manifest.exists():
                result.findings.append(
                    Finding(self.id, self.dimension, Severity.SEV2, f"Checksum manifest missing: {manifest_rel}", location=Location(manifest_rel))
                )
                continue
            base = manifest.parent
            for lineno, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                match = _SUM_LINE.match(line)
                if not match:
                    result.findings.append(
                        Finding(self.id, self.dimension, Severity.SEV3, "Unparseable checksum line", location=Location(manifest_rel, lineno))
                    )
                    continue
                expected, rel = match.group(1).lower(), match.group(2)
                target = (base / rel).resolve()
                if not str(target).startswith(str(ctx.root)):
                    result.findings.append(
                        Finding(self.id, self.dimension, Severity.SEV1, f"Checksum entry escapes repository: {rel}", location=Location(manifest_rel, lineno))
                    )
                    continue
                if not target.exists():
                    result.findings.append(
                        Finding(
                            self.id, self.dimension, Severity.SEV2, f"File listed in checksums is missing: {rel}",
                            location=Location(manifest_rel, lineno), evidence={"key": rel},
                        )
                    )
                    continue
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                verified += 1
                if actual != expected:
                    result.findings.append(
                        Finding(
                            self.id, self.dimension, Severity.SEV1, f"Checksum mismatch: {rel}",
                            detail=f"expected {expected[:16]}…, actual {actual[:16]}…",
                            location=Location(manifest_rel, lineno), evidence={"key": rel},
                            suggestion="Either the artifact changed without regenerating the manifest, or the artifact was tampered with. Regenerate with the project's release script after review.",
                        )
                    )
        result.metrics = {"verified": verified, "issues": len(result.findings)}
        if result.findings:
            result.status = Status.FAIL
            result.summary = f"{len(result.findings)} integrity issue(s)"
        else:
            result.summary = f"{verified} file(s) verified"
        return [result]


def referenced_paths(markdown: str) -> list[tuple[int, str]]:
    """Code spans in a document that look like repository paths."""
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(markdown.splitlines(), 1):
        for match in _CODE_SPAN.finditer(line):
            token = match.group(1).strip().rstrip(".,:;")
            if "/" not in token or token.startswith(("http", "/", "~", "-")) or "://" in token:
                continue
            if not _PATHLIKE.match(token) or "*" in token:
                continue
            if not (_KNOWN_EXT.search(token) or token.endswith("/")):
                continue
            found.append((lineno, token))
    return found


@register
class DocsReferenceRunner(Runner):
    id = "docs.references"
    title = "Documents reference files that exist"
    dimension = Dimension.DOCUMENTATION
    phase = 10

    def applicable(self, ctx: RunContext) -> str | None:
        return None if ctx.config.docs_check.enabled else "disabled in contract"

    def _documents(self, ctx: RunContext) -> list[Path]:
        patterns = ctx.config.docs_check.files or ["README*.md", "*.md", "docs/*.md"]
        return ctx.glob(patterns)

    @staticmethod
    def _index(ctx: RunContext) -> set[str]:
        """Every file and directory path in the project, for suffix resolution.

        Documents often cite paths relative to a package (``state/models.py``
        inside ``src/pkg/``); such a reference is valid when some real path
        ends with it.
        """
        paths: set[str] = set()
        for file in ctx.iter_files():
            rel = ctx.rel(file)
            paths.add(rel)
            parts = rel.split("/")
            for i in range(1, len(parts)):
                paths.add("/".join(parts[:i]) + "/")
        return paths

    def execute(self, ctx: RunContext) -> list[CheckResult]:
        result = CheckResult(self.id, self.title, self.dimension, Status.PASS)
        checked = 0
        ignore = ctx.config.docs_check.ignore
        index = self._index(ctx)
        for doc in self._documents(ctx):
            text = doc.read_text(encoding="utf-8", errors="replace")
            for lineno, token in referenced_paths(text):
                if match_any(token, ignore):
                    continue
                checked += 1
                candidates = [ctx.root / token, doc.parent / token]
                if any(c.exists() for c in candidates):
                    continue
                needle = token.lstrip("./")
                if any(p == needle or p.endswith("/" + needle) for p in index):
                    continue
                result.findings.append(
                    Finding(
                        self.id, self.dimension, Severity.SEV3, f"Document references a path that does not exist: {token}",
                        location=Location(ctx.rel(doc), lineno), evidence={"key": f"{ctx.rel(doc)}:{token}"},
                        suggestion="Update the document or restore the file; stale references mislead the next engineer and the AI agents that read these docs.",
                    )
                )
        result.metrics = {"references_checked": checked, "broken": len(result.findings)}
        result.summary = f"{len(result.findings)} broken of {checked} reference(s)" if result.findings else f"{checked} reference(s) resolved"
        # Status reflects what was found; the SEV3 severity keeps it from blocking a release.
        result.status = Status.FAIL if result.findings else Status.PASS
        return [result]
