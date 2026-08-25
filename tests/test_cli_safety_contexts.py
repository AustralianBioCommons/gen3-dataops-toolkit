"""Context-aware production guard tests (3.8.0 additions).

Background: with a NAMED context active (configured in the marker), the typed
production confirmation token becomes the context name — the operator confirms
*where* they are acting (`acdc/prod`), which is the thing the redesign makes
first-class. Legacy/synthetic contexts keep the historical tokens (pinned by
tests/test_cli_safety.py, untouched). And `--yes` must NEVER bypass a
production prompt — that invariant predates 3.8.0 and gets an explicit
regression test here.
"""
import pytest
import typer

from g3dt import contexts
from g3dt.cli._internal import safety


@pytest.fixture(autouse=True)
def _reset():
    contexts.reset()
    yield
    contexts.reset()


def _activate(name="etl/prod", env="prod", production=None, source="marker"):
    ctx = contexts.Context(
        name=name, project="etl", env=env, profile="etl_prod",
        region="ap-southeast-2", production=production, source=source,
    )
    contexts.set_active(ctx)
    return ctx


def test_named_prod_context_requires_typing_the_context_name(monkeypatch):
    """
    Input:    active marker context etl/prod; delete confirmation invoked.
    Expected: the typed token is the CONTEXT NAME. Typing the old-style
              target no longer satisfies the gate; typing the context name does.
    """
    _activate()
    answers = iter(["ausdiab", "etl/prod"])
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: next(answers))
    with pytest.raises(typer.Exit):
        safety.confirm_destructive("delete", "ausdiab", "prod", assume_yes=False)
    # second call: correct token passes silently
    safety.confirm_destructive("delete", "ausdiab", "prod", assume_yes=False)


def test_production_flag_gates_a_nonprod_named_env(monkeypatch):
    """
    A context named without 'prod' but flagged production: true (e.g. a live
    demo commons) must trigger the strict gate even though the env name alone
    would not.
    """
    _activate(name="etl/demo", env="demo", production=True)
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "etl/demo")
    safety.confirm_prod_strict("synth deploy", "demo")  # gates, then passes

    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "wrong")
    with pytest.raises(typer.Exit):
        safety.confirm_prod_strict("synth deploy", "demo")


def test_yes_never_bypasses_production(monkeypatch):
    """
    --yes exists for automation convenience on NON-prod targets only. On any
    production classification the typed prompt still fires; an empty response
    (what unattended automation would produce) aborts.
    """
    _activate()
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "")
    with pytest.raises(typer.Exit):
        safety.confirm_destructive("delete", "x", "prod", assume_yes=True)


def test_synthetic_context_keeps_legacy_tokens(monkeypatch):
    """
    Back-compat: with only a synthetic context (no contexts: block — the
    3.7.x world), tokens stay the historical target/env name so nothing about
    the documented flow changes for un-migrated operators.
    """
    _activate(source="synthetic")
    monkeypatch.setattr(typer, "prompt", lambda *a, **k: "ausdiab")
    safety.confirm_destructive("delete", "ausdiab", "prod", assume_yes=False)