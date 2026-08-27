"""Tests for ``config.normalize_s3_location`` — forgiving S3 location parsing.

Background: the SSM app fact ``app/schema_s3_uri`` is contractually a
scheme-less ``bucket/key``, because three call sites (deploy_dd.sh,
full_deploy_dd_and_synth.sh, dict_cmds.py) prepend ``s3://`` themselves. An
operator who set it to a full ``s3://...`` URI produced
``s3://s3://schema-bpsyc-test-biocommons-org-au/schema_dev.json``, which
slipped past upload_dictionary.py's ``startswith("s3://")`` guard and split
into bucket ``"s3:"`` — boto3 then crashed with ``Invalid bucket name "s3:"``.

The fix normalizes the value once, at resolve time, tolerating every form an
operator plausibly pastes: bare ``bucket/key``, ``s3://`` URIs (even doubled),
and S3 endpoint https URLs. These tests pin that contract; the SSM round-trip
is covered in test_cli_config.py and the upload layer in
test_upload_dictionary.py.
"""
import pytest

from g3dt.config import ConfigError, normalize_s3_location

#: The exact SSM value + prepended scheme that triggered the incident.
INCIDENT_URI = "s3://s3://schema-bpsyc-test-biocommons-org-au/schema_dev.json"


def test_bare_bucket_key_is_unchanged():
    """Input: the canonical 'bucket/key' form -> Expected: returned verbatim.

    The canonical form must be a fixed point so already-correct SSM values
    (every env deployed to spec today) pass through byte-identical.
    """
    assert normalize_s3_location("my-bucket/schema.json") == "my-bucket/schema.json"


@pytest.mark.parametrize("scheme", ["s3://", "S3://"])
def test_s3_scheme_is_stripped(scheme):
    """Input: '<scheme>bucket/key' -> Expected: 'bucket/key'.

    Case-insensitive: an operator pasting from docs or a console tooltip may
    carry an uppercase scheme; the bucket/key themselves are untouched.
    """
    assert normalize_s3_location(f"{scheme}b/k.json") == "b/k.json"


def test_repeated_s3_scheme_is_stripped():
    """Input: the literal incident value 's3://s3://...' -> a clean bucket/key.

    This is the regression anchor for the outage this module exists for: the
    doubled scheme arises whenever a scheme-carrying config value meets a call
    site that prepends 's3://'. Both layers of scheme must go.
    """
    assert normalize_s3_location(INCIDENT_URI) == (
        "schema-bpsyc-test-biocommons-org-au/schema_dev.json"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://my.bucket.s3.ap-southeast-2.amazonaws.com/schema.json",
        "https://my.bucket.s3.amazonaws.com/schema.json",
        "https://my.bucket.s3-ap-southeast-2.amazonaws.com/schema.json",
        "https://my.bucket.s3.dualstack.ap-southeast-2.amazonaws.com/schema.json",
    ],
)
def test_https_virtual_hosted_url_is_normalized(url):
    """Input: virtual-hosted endpoint URLs -> Expected: 'my.bucket/schema.json'.

    Covers the regional, legacy-global, dash-region, and dualstack host
    spellings, all of which AWS surfaces in different consoles/eras. The
    dotted bucket name proves the bucket group is greedy — a lazy match would
    truncate 'my.bucket' to 'my'.
    """
    assert normalize_s3_location(url) == "my.bucket/schema.json"


@pytest.mark.parametrize(
    "url",
    [
        "https://s3.ap-southeast-2.amazonaws.com/my-bucket/schema.json",
        "https://s3.amazonaws.com/my-bucket/schema.json",
    ],
)
def test_https_path_style_url_is_normalized(url):
    """Input: path-style endpoint URLs -> Expected: 'my-bucket/schema.json'.

    Path-style hosts start with a bare 's3.' label; they must be recognised
    BEFORE the virtual-hosted pattern or the bucket would come out as 's3'.
    """
    assert normalize_s3_location(url) == "my-bucket/schema.json"


def test_https_query_string_and_encoding_are_handled():
    """Input: a share/presigned-style URL with %20 and ?versionId=...

    Expected: query dropped, percent-encoding decoded. The console's
    'Copy URL' button produces exactly this shape, so it must not smuggle
    '?versionId=' into the object key.
    """
    url = "https://b.s3.ap-southeast-2.amazonaws.com/my%20schema.json?versionId=abc"
    assert normalize_s3_location(url) == "b/my schema.json"


def test_whitespace_is_stripped():
    """Input: a value with a trailing newline (an SSM console paste artifact).

    Expected: trimmed. SSM string parameters happily store trailing
    whitespace, which would otherwise end up inside the object key.
    """
    assert normalize_s3_location("  b/k.json\n") == "b/k.json"


def test_bucket_only_value_passes_through():
    """Input: a bare bucket with no key -> Expected: passes through unchanged.

    Deliberately lenient: resolve_env gates EVERY command, so rejecting a
    key-less value here would break commands that never touch the schema
    upload. The 'needs an object key' check lives at the point of use
    (upload_dictionary.py).
    """
    assert normalize_s3_location("u") == "u"


def test_trailing_slash_is_preserved():
    """Input: 'bucket/prefix/' -> Expected: trailing slash kept.

    The key is data, not representation: this repo uses trailing-slash S3
    prefixes on purpose (e.g. StudyConfig.s3_metadata_path), so normalization
    must only ever touch the scheme/host, never the key.
    """
    assert normalize_s3_location("s3://b/prefix/") == "b/prefix/"


def test_empty_value_raises():
    """Input: whitespace-only -> Expected: ConfigError, not a silent ''.

    An empty location would compose 's3://' with no bucket and fail later in
    boto3 with a far less actionable message.
    """
    with pytest.raises(ConfigError):
        normalize_s3_location("   ")


def test_unrecognized_https_host_raises_with_accepted_forms():
    """Input: an AWS *console page* URL (not an object endpoint).

    Expected: ConfigError listing the accepted forms. Passing the URL through
    would guarantee a confusing boto3 failure later; failing at resolve time
    with guidance is the repo's established style for config mistakes.
    """
    url = "https://ap-southeast-2.console.aws.amazon.com/s3/buckets/b?prefix=k"
    with pytest.raises(ConfigError) as exc:
        normalize_s3_location(url)
    assert "Accepted forms" in str(exc.value)
    assert "s3://bucket/key" in str(exc.value)


def test_colon_in_bucket_raises():
    """Input: 's3:/b/k' (the one-slash typo) -> Expected: ConfigError.

    's3:/b/k' survives scheme-stripping (no double slash) and would hand
    boto3 the bucket 's3:' — the exact bug class of the original incident.
    A colon can never appear in a bucket name, so reject it outright.
    """
    with pytest.raises(ConfigError):
        normalize_s3_location("s3:/b/k.json")
