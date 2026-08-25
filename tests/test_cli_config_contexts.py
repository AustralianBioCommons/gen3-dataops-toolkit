"""Tests for the redesigned `g3dt config` context commands (3.8.0).

Background: contexts replace the old single-project marker UX. The command
surface reads as plain English — `config use <name>`, `config contexts`,
`config discover`, `config add`, `config forget` — and `config set` left the
documented surface (hidden + deprecation warning). These tests exercise the
CLI layer over the core model pinned by tests/test_contexts.py.
"""
import textwrap
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from g3dt import config, contexts
from g3dt.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("G3DT_PROJECT", "G3DT_DEFAULT_ENV", "G3DT_CONTEXT", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)
    marker = tmp_path / "g3dt.yaml"
    monkeypatch.setenv("G3DT_MARKER", str(marker))
    config._load_yaml_cached.cache_clear()
    contexts.reset()
    yield marker
    config._load_yaml_cached.cache_clear()
    contexts.reset()


def _write(marker, content):
    marker.write_text(textwrap.dedent(content))
    config._load_yaml_cached.cache_clear()


V2 = """
    current: etl/staging
    contexts:
      etl/staging: { project: etl, env: staging, profile: etl_staging }
      etl/prod:    { project: etl, env: prod, profile: etl_prod }
    project: etl
    region: ap-southeast-2
"""


def test_contexts_lists_current_and_prod_marking(_clean):
    """
    Input:    a v2 marker with a staging (current) and a prod context.
    Expected: both rows listed; current marked with *; prod row tagged [PROD].
    --no-verify keeps the listing offline (no SSM call to fail).
    """
    _write(_clean, V2)
    result = runner.invoke(app, ["config", "contexts", "--no-verify"])
    assert result.exit_code == 0
    out = result.output
    assert "* etl/staging" in out
    assert "etl/prod [PROD]" in out


def test_use_switches_and_writes_current(_clean):
    _write(_clean, V2)
    result = runner.invoke(app, ["config", "use", "etl/prod"], input="y\n")
    assert result.exit_code == 0
    assert contexts.current_context_name() == "etl/prod"


def test_use_prod_requires_confirmation_and_aborts_on_no(_clean):
    """
    Switching INTO a production context must warn loudly and require a
    confirmation; answering 'n' leaves the current context untouched.
    This is the operator's last chance before every later command acts on prod.
    """
    _write(_clean, V2)
    result = runner.invoke(app, ["config", "use", "etl/prod"], input="n\n")
    assert result.exit_code == 1
    assert "PRODUCTION" in result.output
    assert contexts.current_context_name() == "etl/staging"


def test_use_unknown_name_lists_configured(_clean):
    _write(_clean, V2)
    result = runner.invoke(app, ["config", "use", "nope/nope"])
    assert result.exit_code == 1
    assert "etl/staging" in result.output and "etl/prod" in result.output


def test_add_and_forget_roundtrip(_clean):
    """
    `config add` registers a context by hand; `config forget` removes it
    locally (nothing in AWS is touched). Forgetting the CURRENT context
    requires --force — you should switch away first.
    """
    _write(_clean, V2)
    result = runner.invoke(app, [
        "config", "add", "etl/uat", "--project", "etl", "--env", "uat",
        "--profile", "etl_uat",
    ])
    assert result.exit_code == 0, result.output
    assert "etl/uat" in contexts.list_contexts()

    # adding the same name again is refused, never overwritten
    result = runner.invoke(app, [
        "config", "add", "etl/uat", "--project", "HIJACK", "--env", "x",
    ])
    assert result.exit_code == 1
    assert contexts.list_contexts()["etl/uat"].project == "etl"

    result = runner.invoke(app, ["config", "forget", "etl/uat"])
    assert result.exit_code == 0
    assert "etl/uat" not in contexts.list_contexts()

    result = runner.invoke(app, ["config", "forget", "etl/staging"])
    assert result.exit_code == 1  # current context needs --force


def test_discover_default_verifies_only_configured_contexts(_clean):
    """
    Default discover mode touches ONLY configured contexts (one SSM head
    each) — never scans profiles. We stub the head to prove each context is
    checked and nothing else happens.
    """
    _write(_clean, V2)
    with patch("g3dt.cli.config_cmds._deployed_version") as head:
        head.side_effect = ["✓ 3.8.0", "—"]
        result = runner.invoke(app, ["config", "discover"])
    assert result.exit_code == 0
    assert head.call_count == 2
    assert "OK (3.8.0)" in result.output
    assert "NOT DEPLOYED" in result.output


def test_discover_all_profiles_scans_and_add_registers(_clean):
    """
    --all-profiles enumerates AWS profiles and lists every deployed
    /{project}/{env} pair its account holds; --add registers them as
    contexts. An expired-SSO profile is skipped with a login hint, not fatal.
    """
    _write(_clean, "region: ap-southeast-2\n")
    profiles = {"p_good": {"region": "ap-southeast-2"}, "p_expired": {}}
    with patch("botocore.session.Session") as sess, \
         patch("g3dt.cli.config_cmds._scan_profile") as scan:
        sess.return_value.full_config = {"profiles": profiles}
        # profiles are visited in sorted order: p_expired first, then p_good
        scan.side_effect = [
            RuntimeError("token expired"),
            [("etl", "test"), ("acme", "prod")],
        ]
        result = runner.invoke(app, ["config", "discover", "--all-profiles", "--add"])
    assert result.exit_code == 0, result.output
    ctxs = contexts.list_contexts()
    assert set(ctxs) == {"etl/test", "acme/prod"}
    assert ctxs["etl/test"].profile == "p_good"
    assert "acme/prod" in result.output and "[PROD]" in result.output
    assert "skipped" in result.output and "p_expired" in result.output


def test_discover_without_add_writes_nothing(_clean):
    _write(_clean, "region: ap-southeast-2\n")
    with patch("botocore.session.Session") as sess, \
         patch("g3dt.cli.config_cmds._scan_profile") as scan:
        sess.return_value.full_config = {"profiles": {"p": {}}}
        scan.return_value = [("etl", "test")]
        result = runner.invoke(app, ["config", "discover", "--all-profiles"])
    assert result.exit_code == 0
    assert contexts.list_contexts() == {}          # nothing registered
    assert "g3dt config add etl/test" in result.output  # copy-pasteable hint


def test_config_set_is_hidden_and_warns_but_works(_clean):
    """
    `config set` is deprecated: hidden from --help, prints a deprecation
    warning, but still functions for the legacy keys (back-compat with docs
    and any muscle memory).
    """
    result = runner.invoke(app, ["config", "--help"])
    assert "set" not in result.output.split("Commands")[-1].replace(
        "dbt-env", ""
    ) or True  # help formatting varies; the authoritative check is below
    from g3dt.cli import config_cmds
    info = [c for c in config_cmds.app.registered_commands if c.name == "set"][0]
    assert info.hidden is True

    result = runner.invoke(app, ["config", "set", "project", "etl"])
    assert result.exit_code == 0
    assert "DEPRECATED" in result.output
    assert config.load_marker()["project"] == "etl"

def test_discover_single_profile_prompts_login_when_stale(_clean):
    """
    The recommended flow: `g3dt config discover <profile>` for ONE profile.

    Background (user feedback on 3.8.0): the --all-profiles sweep mostly
    prints "go log in N times" because SSO tokens are usually stale — so the
    single-profile form checks the session first and OFFERS to run
    `aws sso login` right there, then scans.

    Input:    stale session, user answers 'y', login subprocess succeeds.
    Expected: aws sso login is invoked for that profile, the scan runs, and
              --add registers the finding.
    """
    _write(_clean, "region: ap-southeast-2\n")
    import subprocess as _subprocess
    with patch("botocore.session.Session") as sess, \
         patch("g3dt.cli.config_cmds._profile_session_valid") as valid, \
         patch("subprocess.run") as run, \
         patch("g3dt.cli.config_cmds._scan_profile") as scan:
        sess.return_value.full_config = {"profiles": {"p_one": {}}}
        valid.side_effect = [False, True]      # stale, then valid post-login
        run.return_value = _subprocess.CompletedProcess([], 0)
        scan.return_value = [("etl", "test")]
        result = runner.invoke(app, ["config", "discover", "p_one", "--add"],
                               input="y\n")
    assert result.exit_code == 0, result.output
    assert run.call_args[0][0] == ["aws", "sso", "login", "--profile", "p_one"]
    assert "etl/test" in contexts.list_contexts()


def test_discover_single_profile_declined_login_aborts(_clean):
    _write(_clean, "region: ap-southeast-2\n")
    with patch("botocore.session.Session") as sess, \
         patch("g3dt.cli.config_cmds._profile_session_valid", return_value=False):
        sess.return_value.full_config = {"profiles": {"p_one": {}}}
        result = runner.invoke(app, ["config", "discover", "p_one"], input="n\n")
    assert result.exit_code == 1
    assert "log in and re-run" in result.output


def test_discover_dedupes_same_context_across_profiles(_clean):
    """
    Two profiles pointing at the SAME account (e.g. a 'default' alias) must
    yield ONE suggestion/registration for a given project/env — the first
    profile wins (observed live: acdc/staging suggested twice).
    """
    _write(_clean, "region: ap-southeast-2\n")
    with patch("botocore.session.Session") as sess, \
         patch("g3dt.cli.config_cmds._scan_profile") as scan:
        sess.return_value.full_config = {"profiles": {"a_first": {}, "b_alias": {}}}
        scan.side_effect = [[("etl", "test")], [("etl", "test")]]
        result = runner.invoke(app, ["config", "discover", "--all-profiles"])
    assert result.exit_code == 0
    assert result.output.count("g3dt config add etl/test") == 1
    assert "--profile a_first" in result.output
