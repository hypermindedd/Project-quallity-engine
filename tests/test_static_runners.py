"""Architecture, integrity, docs, secrets and JUnit parsing."""

from __future__ import annotations

import hashlib

from pqe.model import Severity, Status
from pqe.runners.architecture import BoundaryRunner, MirrorRunner, find_cycles, resolve_relative
from pqe.runners.functional import parse_junit
from pqe.runners.integrity import ChecksumRunner, DocsReferenceRunner, referenced_paths
from pqe.runners.security import SecretsRunner, scan_text, shannon_entropy

from .conftest import write

ARCH = """
[project]
name = "demo"
[architecture]
python_roots = ["src"]
[[architecture.rules]]
name = "domain-pure"
source = "app.domain"
forbid = ["app.web"]
reason = "domain must not know the web layer"
"""


def _package(root):
    write(root, "src/app/__init__.py", "")
    write(root, "src/app/domain/__init__.py", "")
    write(root, "src/app/domain/model.py", "from ..web import routes  # violation\nfrom . import rules\n")
    write(root, "src/app/domain/rules.py", "import json\n")
    write(root, "src/app/web/__init__.py", "")
    write(root, "src/app/web/routes.py", "from app.domain.model import x\n")


def test_relative_import_resolution():
    assert resolve_relative("app.domain.model", False, 2, "web") == "app.web"
    assert resolve_relative("app.domain", True, 1, "rules") == "app.domain.rules"


def test_boundary_violation_and_cycle_are_reported(tmp_path, ctx_factory):
    _package(tmp_path)
    ctx = ctx_factory(ARCH)
    [result] = BoundaryRunner().run(ctx)
    assert result.status == Status.FAIL
    titles = [f.title for f in result.findings]
    assert any("domain-pure" in t and "app.web" in t for t in titles)
    assert any("cycle" in t and "app.domain" in t and "app.web" in t for t in titles)
    violation = next(f for f in result.findings if "domain-pure" in f.title)
    assert violation.location.render() == "src/app/domain/model.py:1"
    assert violation.evidence["rule"] == "domain-pure"


def test_clean_architecture_passes(tmp_path, ctx_factory):
    write(tmp_path, "src/app/__init__.py", "")
    write(tmp_path, "src/app/domain/model.py", "import json\n")
    write(tmp_path, "src/app/web/routes.py", "from app.domain import model\n")
    [result] = BoundaryRunner().run(ctx_factory(ARCH))
    assert result.status == Status.PASS, result.findings


def test_find_cycles_only_reports_real_components():
    assert find_cycles({"a": {"b"}, "b": {"c"}, "c": set()}) == []
    assert find_cycles({"a": {"b"}, "b": {"a"}, "c": {"a"}}) == [["a", "b"]]


def test_mirror_drift(tmp_path, ctx_factory):
    write(tmp_path, "core/a.py", "x = 1\n")
    write(tmp_path, "deploy/core/a.py", "x = 2\n")
    write(tmp_path, "core/b.py", "y = 1\n")
    ctx = ctx_factory('[project]\nname="d"\n[[architecture.mirrors]]\nsource = "core"\nmirror = "deploy/core"\n')
    [result] = MirrorRunner().run(ctx)
    assert result.status == Status.FAIL
    states = sorted(f.title for f in result.findings)
    assert states == ["Mirror drift (content differs): a.py", "Mirror drift (missing in mirror): b.py"]


def test_checksums(tmp_path, ctx_factory):
    good = write(tmp_path, "good.txt", "ok\n")
    write(tmp_path, "bad.txt", "changed\n")
    digest = hashlib.sha256(good.read_bytes()).hexdigest()
    write(tmp_path, "SUMS", f"{digest}  good.txt\n{'0' * 64}  bad.txt\n{'1' * 64}  gone.txt\n{'2' * 64}  ../escape.txt\n")
    [result] = ChecksumRunner().run(ctx_factory('checksum_files = ["SUMS"]\n[project]\nname="d"\n'))
    titles = {f.title: f.severity for f in result.findings}
    assert titles["Checksum mismatch: bad.txt"] == Severity.SEV1
    assert titles["File listed in checksums is missing: gone.txt"] == Severity.SEV2
    assert titles["Checksum entry escapes repository: ../escape.txt"] == Severity.SEV1
    assert result.metrics["verified"] == 2


def test_doc_references_resolve_package_relative_paths(tmp_path, ctx_factory):
    write(tmp_path, "src/pkg/state/models.py", "")
    write(tmp_path, "README.md", "See `state/models.py`, `src/pkg/` and `docs/missing.md` and `http://x/y.md`.\n")
    assert [t for _, t in referenced_paths("`a/b.py` `x` `https://h/a.md` `c/*.py`")] == ["a/b.py"]
    [result] = DocsReferenceRunner().run(ctx_factory('[project]\nname="d"\n[docs_check]\nfiles=["README.md"]\n'))
    assert [f.title for f in result.findings] == ["Document references a path that does not exist: docs/missing.md"]
    assert result.findings[0].severity == Severity.SEV3


def test_secret_detection_rules():
    aws = "AKIA" + "Q3JX7ZLMN4PB2TWE"
    private = "-----BEGIN " + "RSA PRIVATE KEY-----"
    hits = scan_text(f'key = "{aws}"\n{private}\napi_key = "your-api-key-here-xxxxxxxxxxxx"\n')
    assert [h[1].name for h in hits] == ["AWS access key id", "Private key"]
    assert scan_text(f'k = "{aws}"  # pqe:allow-secret\n') == []
    assert shannon_entropy("aaaaaaaa") == 0.0


def test_secrets_runner_masks_values_and_flags_env_files(tmp_path, ctx_factory):
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    write(tmp_path, "app/settings.py", f'TOKEN = "{token}"\n')
    write(tmp_path, ".env.production", "X=1\n")
    write(tmp_path, ".env.example", "X=\n")
    [result] = SecretsRunner().run(ctx_factory('[project]\nname="d"\n'))
    titles = sorted(f.title for f in result.findings)
    assert titles == ["Environment file is tracked: .env.production", "GitHub token in source"]
    assert all(token not in f.detail for f in result.findings)


def test_parse_junit_handles_classes_failures_and_skips(tmp_path):
    xml = tmp_path / "j.xml"
    xml.write_text(
        '<testsuites><testsuite>'
        '<testcase classname="tests.test_a" name="test_ok" file="tests/test_a.py" time="0.1"/>'
        '<testcase classname="tests.test_a.TestX" name="test_bad" time="0.2"><failure message="boom">trace</failure></testcase>'
        '<testcase classname="tests.test_b" name="test_skip"><skipped message="why"/></testcase>'
        '</testsuite></testsuites>'
    )
    cases = parse_junit(xml)
    assert [(c.id, c.status) for c in cases] == [
        ("tests/test_a.py::test_ok", Status.PASS),
        ("tests/test_a.py::TestX::test_bad", Status.FAIL),
        ("tests/test_b.py::test_skip", Status.SKIPPED),
    ]
    assert cases[1].message == "boom"
