"""Tests for the context model (src/g3dt/contexts.py) and its config writers.

Background for a new developer: a "context" is a named
(project, env, profile, region) tuple — the answer to "what is this command
pointed at?". The hard part is back-compat: three marker generations must keep
working byte-identically (design doc docs/design/contexts.md section 4):

  1. v1 laptop marker  — project/region/default_env + profiles: map
  2. v1 box marker     — ONLY project/region/default_env (what CDK user-data
                         writes onto the EC2 job box)
  3. no marker at all  — CodeBuild: just $G3DT_PROJECT + --env

These tests pin the synthesis rules for all three, the resolution precedence,
and the strict matching that only applies once an explicit contexts: block
exists. If any of these breaks, deployed EC2 boxes or CodeBuild pipelines
(which cannot be upgraded atomically with laptops) misresolve.
"""
import textwrap

import pytest

from g3dt import config, contexts


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    """Every test starts with no marker, no env overrides, no process state."""
    for var in ("G3DT_PROJECT", "G3DT_DEFAULT_ENV", "G3DT_CONTEXT", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)
    marker = tmp_path / "g3dt.yaml"
    monkeypatch.setenv("G3DT_MARKER", str(marker))
    config._load_yaml_cached.cache_clear()
    contexts.reset()
    yield marker
    config._load_yaml_cached.cache_clear()
    contexts.reset()


def _write(marker_file, content: str) -> None:
    marker_file.write_text(textwrap.dedent(content))
    config._load_yaml_cached.cache_clear()


V1_LAPTOP = """
    project: etl
    region: ap-southeast-2
    default_env: staging
    profiles:
      test: etl_test
      staging: etl_staging
"""

V1_BOX = """
    project: etl
    region: ap-southeast-2
    default_env: staging
"""

V2 = """
    current: etl/staging
    contexts:
      etl/test:    { project: etl, env: test, profile: etl_test }
      etl/staging: { project: etl, env: staging, profile: etl_staging }
      etl/prod:    { project: etl, env: prod, profile: etl_prod }
      other/demo:  { project: other, env: demo, profile: other_demo, production: true }
    project: etl
    region: ap-southeast-2
"""


# --------------------------------------------------------------------------- #
# Legacy synthesis (design doc section 4)                                      #
# --------------------------------------------------------------------------- #

def test_v1_laptop_marker_synthesizes_one_context_per_profile(_clean):
    """
    Input:    a v1 laptop marker with profiles for test and staging.
    Expected: two synthesized contexts named {project}/{env}, source 'legacy',
              each carrying its profile; current implied from default_env.
    """
    _write(_clean, V1_LAPTOP)
    ctxs = contexts.list_contexts()
    assert set(ctxs) == {"etl/test", "etl/staging"}
    assert ctxs["etl/staging"].profile == "etl_staging"
    assert all(c.source == "legacy" for c in ctxs.values())
    assert contexts.current_context_name() == "etl/staging"


def test_v1_box_marker_synthesizes_single_ambient_context(_clean):
    """
    The EC2 box marker has ONLY project/region/default_env — no profiles.

    Expected: exactly one synthesized context for default_env with
              profile=None (ambient credential chain), and it is current.
    This is the whole back-compat story for already-deployed boxes.
    """
    _write(_clean, V1_BOX)
    ctxs = contexts.list_contexts()
    assert set(ctxs) == {"etl/staging"}
    assert ctxs["etl/staging"].profile is None
    assert contexts.current_context_name() == "etl/staging"


def test_no_marker_with_g3dt_project_synthesizes_ephemeral_for_env(_clean, monkeypatch):
    """
    The CodeBuild path: no marker file anywhere, only $G3DT_PROJECT, and every
    command passes an explicit --env.

    Expected: resolve_context(env='staging') returns a synthetic context with
              ambient auth — byte-identical behavior to toolkit 3.7.x.
    """
    monkeypatch.setenv("G3DT_PROJECT", "etl")
    ctx, effective = contexts.resolve_context(env="staging")
    assert (ctx.project, ctx.env, ctx.profile) == ("etl", "staging", None)
    assert ctx.source == "synthetic"
    assert effective == "staging"


# --------------------------------------------------------------------------- #
# Resolution precedence and matching (design doc section 5)                    #
# --------------------------------------------------------------------------- #

def test_precedence_ctx_flag_beats_env_and_current(_clean):
    """--ctx wins outright; --env alongside it must agree or error."""
    _write(_clean, V2)
    ctx, _ = contexts.resolve_context(ctx_name="etl/prod")
    assert ctx.name == "etl/prod"
    with pytest.raises(config.ConfigError, match="conflicts"):
        contexts.resolve_context(ctx_name="etl/prod", env="staging")


def test_precedence_current_used_when_nothing_passed(_clean):
    _write(_clean, V2)
    ctx, effective = contexts.resolve_context()
    assert ctx.name == "etl/staging"
    assert effective == "staging"


def test_g3dt_context_env_var_selects_context(_clean, monkeypatch):
    _write(_clean, V2)
    monkeypatch.setenv("G3DT_CONTEXT", "etl/test")
    ctx, _ = contexts.resolve_context()
    assert ctx.name == "etl/test"


def test_env_alias_matches_within_active_project(_clean):
    """
    --env test on a v2 marker with current=etl/staging must pick etl/test —
    NOT other/demo (a different project) even though only one context is
    named 'test' overall. Matching is scoped to the active project.
    """
    _write(_clean, V2)
    ctx, effective = contexts.resolve_context(env="test")
    assert ctx.name == "etl/test"
    assert effective == "test"


def test_env_alias_unmatched_on_v2_marker_errors_with_guidance(_clean):
    """
    Once an explicit contexts: block exists, an unmatched --env is an ERROR
    (with discover/add guidance) — never a silent synthetic context. This is
    the strictness the redesign introduces, deliberately.
    """
    _write(_clean, V2)
    with pytest.raises(config.ConfigError, match="config discover --add"):
        contexts.resolve_context(env="uat")


def test_env_alias_with_ec2_suffix_matches_base_and_keeps_suffix(_clean):
    """
    --env staging_ec2 (the dispatch wire form) must match the 'staging'
    context while returning the effective env WITH the suffix — downstream
    resolve_env() still keys ambient-credentials off that suffix.
    """
    _write(_clean, V2)
    ctx, effective = contexts.resolve_context(env="staging_ec2")
    assert ctx.name == "etl/staging"
    assert effective == "staging_ec2"


def test_nothing_configured_raises_setup_guidance(_clean):
    with pytest.raises(config.ConfigError, match="config use|discover"):
        contexts.resolve_context()


# --------------------------------------------------------------------------- #
# Production classification and validation                                     #
# --------------------------------------------------------------------------- #

def test_is_production_by_flag_env_and_name(_clean):
    """
    Three independent triggers, any one suffices:
      - explicit production: true (other/demo — env name says nothing)
      - 'prod' in the env name (etl/prod)
      - 'prod' in the context name
    """
    _write(_clean, V2)
    ctxs = contexts.list_contexts()
    assert contexts.is_production(ctxs["etl/prod"]) is True
    assert contexts.is_production(ctxs["other/demo"]) is True
    assert contexts.is_production(ctxs["etl/staging"]) is False


def test_context_env_with_ec2_suffix_is_rejected(_clean):
    _write(_clean, """
        contexts:
          bad/one: { project: p, env: staging_ec2 }
    """)
    with pytest.raises(config.ConfigError, match="_ec2"):
        contexts.list_contexts()


def test_reserved_context_names_are_rejected(_clean):
    _write(_clean, """
        contexts:
          use: { project: p, env: test }
    """)
    with pytest.raises(config.ConfigError, match="reserved"):
        contexts.list_contexts()


# --------------------------------------------------------------------------- #
# config.py integration: aws_profile_for / require_project / writers           #
# --------------------------------------------------------------------------- #

def test_aws_profile_for_reads_v2_context_and_legacy_map(_clean):
    """
    v2 marker: profile comes from the matching context (project-scoped);
    the _ec2 suffix still maps to the same base env's profile — this is what
    keeps `g3dt jobs logs` working on run records that stored 'staging_ec2'.
    """
    _write(_clean, V2)
    assert config.aws_profile_for("staging") == "etl_staging"
    assert config.aws_profile_for("staging_ec2") == "etl_staging"
    _write(_clean, V1_LAPTOP)
    assert config.aws_profile_for("staging_ec2") == "etl_staging"


def test_require_project_prefers_active_context(_clean):
    _write(_clean, V2)
    ctx, _ = contexts.resolve_context(ctx_name="other/demo")
    contexts.set_active(ctx)
    assert config.require_project() == "other"


def test_use_writer_migrates_v1_marker_preserving_legacy_keys(_clean):
    """
    First `config use` on a v1 marker materializes the synthesized contexts
    AND keeps every v1 key — an older toolkit reading the same file (e.g. a
    not-yet-replaced EC2 box sharing a home dir in tests) must still work.
    """
    _write(_clean, V1_LAPTOP)
    config.set_current_context("etl/test")
    data = config.load_marker()
    assert data["current"] == "etl/test"
    assert data["default_env"] == "test"          # synced for old toolkits
    assert data["project"] == "etl"               # v1 keys preserved
    assert data["profiles"] == {"test": "etl_test", "staging": "etl_staging"}
    assert set(data["contexts"]) == {"etl/test", "etl/staging"}


def test_upsert_contexts_never_overwrites_and_forget_removes(_clean):
    _write(_clean, V2)
    new = contexts.Context(name="etl/uat", project="etl", env="uat",
                           profile="etl_uat", region="ap-southeast-2")
    clobber = contexts.Context(name="etl/test", project="HIJACK", env="x",
                               profile=None, region="r")
    _, added, skipped = config.upsert_contexts([new, clobber])
    assert added == ["etl/uat"] and skipped == ["etl/test"]
    assert contexts.list_contexts()["etl/test"].project == "etl"  # untouched

    config.forget_context("etl/uat")
    assert "etl/uat" not in contexts.list_contexts()
    with pytest.raises(config.ConfigError, match="Unknown context"):
        config.forget_context("etl/uat")


def test_banner_line_formats(_clean):
    """The banner grammar is a contract (design doc section 6) — pin it."""
    _write(_clean, V2)
    ctxs = contexts.list_contexts()
    line = contexts.banner_line(ctxs["etl/staging"], "staging")
    assert line == ("ctx etl/staging → project=etl env=staging "
                    "profile=etl_staging region=ap-southeast-2")
    assert "[PROD]" in contexts.banner_line(ctxs["etl/prod"], "prod")
    remote = contexts.banner_line(ctxs["etl/staging"], "staging_ec2")
    assert "(remote)" in remote and "profile=(ambient)" in remote
    assert "none configured" in contexts.banner_line(None, None)