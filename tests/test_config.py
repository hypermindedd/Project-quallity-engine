from __future__ import annotations

import pytest

from pqe.config import ConfigError
from pqe.model import Dimension, Severity

from .conftest import make_config


def test_minimal_contract_has_safe_defaults(tmp_path):
    config = make_config(tmp_path, '[project]\nname = "x"\n')
    assert config.project.name == "x"
    assert config.api.enabled is False and config.ui.enabled is False
    assert ".qa/**" in config.remediation.protected
    assert config.performance.p95_ms is None  # never an invented threshold


def test_unknown_keys_are_rejected_with_their_path(tmp_path):
    with pytest.raises(ConfigError, match=r"pytest: unknown key\(s\): enabeld"):
        make_config(tmp_path, '[project]\nname = "x"\n[pytest]\nenabeld = false\n')


def test_root_key_after_a_table_is_caught(tmp_path):
    # TOML puts a key written after [[architecture.modules]] inside that table.
    text = '[project]\nname = "x"\n[[architecture.modules]]\nname = "m"\npaths = []\nchecksum_files = ["S"]\n'
    with pytest.raises(ConfigError, match=r"architecture.modules\[0\]: unknown key"):
        make_config(tmp_path, text)


def test_bool_is_not_accepted_as_int(tmp_path):
    with pytest.raises(ConfigError, match="expected int, got bool"):
        make_config(tmp_path, '[project]\nname = "x"\n[load]\nconcurrency = true\n')


def test_enums_and_commands(tmp_path):
    config = make_config(tmp_path, """
        [project]
        name = "x"
        [[commands]]
        id = "tsc"
        argv = ["tsc"]
        dimension = "BUILD"
        severity = "SEV2"
    """)
    assert config.commands[0].dimension is Dimension.BUILD
    assert config.commands[0].severity is Severity.SEV2
    with pytest.raises(ConfigError, match="not one of"):
        make_config(tmp_path, '[project]\nname = "x"\n[[commands]]\nid = "a"\nargv = ["a"]\nseverity = "HIGH"\n')


def test_goal_validation(tmp_path):
    with pytest.raises(ConfigError, match="duplicate goal"):
        make_config(tmp_path, """
            [project]
            name = "x"
            [[goals]]
            id = "G1"
            title = "a"
            criteria = [{ kind = "check", check = "c" }]
            [[goals]]
            id = "G1"
            title = "b"
            criteria = []
        """)
    with pytest.raises(ConfigError, match="tests criterion requires"):
        make_config(tmp_path, '[project]\nname = "x"\n[[goals]]\nid = "G"\ntitle = "t"\ncriteria = [{ kind = "tests" }]\n')


def test_contract_cannot_unprotect_itself(tmp_path):
    config = make_config(tmp_path, '[project]\nname = "x"\n[remediation]\nprotected = ["src/generated/**"]\n')
    assert config.remediation.protected[0] == ".qa/**"


def test_api_requires_a_target(tmp_path):
    with pytest.raises(ConfigError, match="start_command or base_url"):
        make_config(tmp_path, '[project]\nname = "x"\n[api]\nenabled = true\n')
