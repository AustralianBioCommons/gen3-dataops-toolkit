"""Shared test isolation for process-scoped context state.

Background: every CLI command now resolves the acting context first (the
universal banner, design doc contexts.md section 6) and records it in
process-global state — ``g3dt.contexts._active`` / ``_override`` /
``_banner_printed``. ``config.require_project`` prefers the active context's
project, so a context set by a command invoked in one test would silently
override the marker/project a later test seeds (e.g. the developer's real
``~/.g3dt/g3dt.yaml`` project leaking into a moto-seeded ``etl`` tree).

Why this matters: without this reset the suite's outcome depends on test
ORDER and on the machine's real marker — the exact class of flake a new
maintainer cannot debug. Clearing before and after every test keeps each one
hermetic regardless of which file ran first.
"""
import pytest

from g3dt import config, contexts, resolver, studies


def _clear_registry_caches():
    # The resolver and the legacy studies.yaml reader are lru_cached for the
    # life of the process; the studies module keeps once-per-process warning
    # flags. Without clearing all three here, study state written by one
    # test (or a fallback warning it triggered) bleeds into the next in file
    # order — the exact flake class this conftest exists to prevent.
    # getattr-guarded: a test may have monkeypatched the cached function with
    # a plain stub that has no cache_clear (teardown runs before the undo).
    for fn in (resolver.resolve, config._studies_from_s3_cached):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()
    studies.reset_warning_flags()


@pytest.fixture(autouse=True)
def _reset_context_state(monkeypatch, tmp_path):
    # Deterministic rendering: rich force-enables ANSI color when it sees
    # GITHUB_ACTIONS (even with no TTY), so CliRunner output on CI is full of
    # escape codes while local runs render plain text — and substring
    # assertions on --help output pass locally but fail on CI. NO_COLOR is
    # the standard override rich honors; setting it here makes every test's
    # rendered output identical in both environments.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    # Hermetic default marker: never let a test read the developer's real
    # ~/.g3dt/g3dt.yaml (or fail on a bare CI box that has none). Tests that
    # need a specific marker overwrite G3DT_MARKER themselves.
    default_marker = tmp_path / "conftest-g3dt.yaml"
    default_marker.write_text("project: etl\nregion: ap-southeast-2\n")
    monkeypatch.setenv("G3DT_MARKER", str(default_marker))
    config._load_yaml_cached.cache_clear()
    contexts.reset()
    _clear_registry_caches()
    yield
    config._load_yaml_cached.cache_clear()
    contexts.reset()
    _clear_registry_caches()
