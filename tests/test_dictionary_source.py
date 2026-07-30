"""Tests for composing the data-dictionary download URL and local filename.

The URL used to be a hardcoded template in ``cli/dict_cmds.py``, duplicated as a
literal in two shell scripts. Only the ``{repo}`` segment came from config, so a
project whose schema repo stored its dictionary at a different path could not be
operated by this toolkit at all -- which contradicts its one architectural rule,
that no resource name is compiled into the package.

These are pure functions of an ``EnvConfig``, so nothing here needs AWS or
mocking. The SSM side of the resolution (reading ``app/dictionary_*``) is
covered in test_cli_config.py; the argv the CLI builds from these values is
covered in test_cli_commands.py.
"""
from g3dt.config import (
    DEFAULT_DICT_BASE_URL,
    DEFAULT_DICT_PATH,
    EnvConfig,
    dictionary_filename,
    dictionary_url,
    dictionary_version_of,
)

#: The exact URL the toolkit fetched before the source became configurable.
#: Pinned once, as the anchor proving this change is behaviour-preserving.
PRE_CHANGE_URL = (
    "https://raw.githubusercontent.com/AustralianBioCommons/acdc-schema-json"
    "/refs/tags/v1.1.6/dictionary/prod_dict/acdc_schema.json"
)


def _env(**overrides) -> EnvConfig:
    """An EnvConfig with the real defaults, overridable per test."""
    base = dict(
        name="test",
        is_ec2=False,
        region="ap-southeast-2",
        dictionary_version="v1.1.6",
        aws_profile=None,
        aws_secret_name="sec",
        schema_s3_uri="u",
        domain="d",
        app_name="a",
        namespace="n",
        cluster_name="c",
        schema_repo="AustralianBioCommons/acdc-schema-json",
    )
    base.update(overrides)
    return EnvConfig(**base)


def test_defaults_compose_the_pre_change_url():
    """
    Inputs:   an env with no dictionary_base_url/dictionary_path set (the
              defaults), schema_repo AustralianBioCommons/acdc-schema-json, v1.1.6
    Expected: exactly the URL the toolkit used before this change

    Both new inputs are optional so environments already deployed keep fetching
    the dictionary they always did -- the CDK can publish the new keys later
    without a coordinated release. This is the regression anchor for that claim.
    """
    assert dictionary_url(_env()) == PRE_CHANGE_URL


def test_configured_source_replaces_host_and_path():
    """
    Inputs:   base_url https://git.internal, repo org/dict,
              dictionary_path schemas/dict.json, version v2
    Expected: https://git.internal/org/dict/refs/tags/v2/schemas/dict.json

    The point of the change: an operator repoints the dictionary at a mirror or
    a differently-laid-out repo by editing config, with no code change.
    """
    e = _env(
        dictionary_base_url="https://git.internal",
        schema_repo="org/dict",
        dictionary_path="schemas/dict.json",
        dictionary_version="v2",
    )
    assert dictionary_url(e) == "https://git.internal/org/dict/refs/tags/v2/schemas/dict.json"


def test_version_argument_overrides_the_env_version():
    """
    Inputs:   env pinned at v1.1.6; dictionary_url(e, "v1.1.7")
    Expected: the URL carries /refs/tags/v1.1.7/, not the env's v1.1.6

    This is what backs `dict deploy --version` -- promoting one dictionary
    across environments without a cdk deploy per env.
    """
    url = dictionary_url(_env(), "v1.1.7")
    assert "/refs/tags/v1.1.7/" in url
    assert "v1.1.6" not in url


def test_stray_slashes_do_not_produce_a_double_slash():
    """
    Inputs:   base_url with a trailing '/', repo and path with leading slashes
    Expected: one '//' in the whole URL -- the one in 'https://'

    SSM values are hand-authored in the CDK config, so a stray slash is a matter
    of time. A '//' mid-path 404s on raw GitHub, and the failure would look like
    a network problem rather than a config typo. The scheme's own '//' must
    survive, since only the ends of each segment are stripped.
    """
    e = _env(
        dictionary_base_url="https://git.internal/",
        schema_repo="/org/dict/",
        dictionary_path="/schemas/dict.json",
    )
    url = dictionary_url(e)
    assert url == "https://git.internal/org/dict/refs/tags/v1.1.6/schemas/dict.json"
    assert url.count("//") == 1


def test_filename_matches_the_existing_naming_convention():
    """
    Inputs:   the default dictionary_path, version v1.1.6
    Expected: acdc_schema_v1.1.6.json

    pull_dict.sh has always named downloads <stem>_<version>.<ext>, and the
    upload and synth steps read that path. Deriving the name must reproduce the
    convention exactly or those later steps read a file that isn't there.
    """
    assert dictionary_filename(_env()) == "acdc_schema_v1.1.6.json"


def test_filename_follows_a_reconfigured_path():
    """
    Inputs:   dictionary_path dictionary/dev/other.json, version v2
    Expected: other_v2.json

    The local filename was hardcoded in five places. If the configured path ever
    names a different file, the download, the S3 upload and the generator must
    all agree on the new name -- otherwise the pull succeeds and everything
    downstream silently reads a missing file.
    """
    e = _env(dictionary_path="dictionary/dev/other.json", dictionary_version="v2")
    assert dictionary_filename(e) == "other_v2.json"


def test_filename_ignores_directory_components_in_the_path():
    """
    Inputs:   a dictionary_path containing a parent-directory hop
    Expected: only the basename survives, so the name cannot escape the dir

    The filename is passed to pull_dict.sh, which interpolates it straight into
    its output path. Reducing to the basename means a malformed config value
    cannot steer a download outside ~/.g3dt/schemas.
    """
    e = _env(dictionary_path="a/../../etc/evil.json", dictionary_version="v1")
    assert dictionary_filename(e) == "evil_v1.json"


def test_version_of_filename_round_trips():
    """
    Inputs:   acdc_schema_v1.1.6.json, then an unversioned name
    Expected: 'v1.1.6', then None

    `synth generate` uses this to catch a --schema that disagrees with
    --version, which would mislabel a whole batch. An unversioned name has no
    claim to check, so it must return None rather than guess.

    The 'my_draft.json' case is why the match is version-shaped rather than
    "whatever follows the last underscore": read loosely, that name claims
    version "draft" and a legitimate draft dictionary gets rejected.
    """
    assert dictionary_version_of("acdc_schema_v1.1.6.json") == "v1.1.6"
    assert dictionary_version_of("/tmp/schemas/acdc_schema_v2.json") == "v2"
    assert dictionary_version_of("dict_v2.0.0-rc1.json") == "v2.0.0-rc1"
    assert dictionary_version_of("schema.json") is None
    assert dictionary_version_of("my_draft.json") is None
    assert dictionary_version_of("acdc_schema.json") is None


def test_defaults_are_the_conventional_public_source():
    """
    Inputs:   the module-level default constants
    Expected: raw GitHub and the schema repo's conventional dictionary path

    Stated explicitly so a change to either default is a deliberate, reviewed
    edit -- these are what every environment without the new keys resolves to.
    """
    assert DEFAULT_DICT_BASE_URL == "https://raw.githubusercontent.com"
    assert DEFAULT_DICT_PATH == "dictionary/prod_dict/acdc_schema.json"


def test_config_show_reports_the_resolved_url(monkeypatch):
    """
    Inputs:  g3dt config show --env test, with env resolution stubbed
    Expected: a dictionary_url line carrying the composed URL

    `config show` is the documented pre-deploy check, and the README points at it
    for "where will the dictionary come from?". That answer now spans schema_repo
    plus two optional inputs, so the composed URL has to be visible -- this keeps
    the documented behaviour and the code from drifting apart.
    """
    from typer.testing import CliRunner

    from g3dt.cli.main import app

    monkeypatch.setattr("g3dt.cli.config_cmds.env_of", lambda env: _env())
    result = CliRunner().invoke(app, ["config", "show", "--env", "test"])
    assert result.exit_code == 0, result.output
    assert f"dictionary_url     : {PRE_CHANGE_URL}" in result.output
