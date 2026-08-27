"""Tests for ``g3dt.import_order`` — node-order resolution for deletes.

Background: `g3dt delete metadata` crashed with FileNotFoundError whenever the
caller's working directory lacked a ``DataImportOrder.txt`` — the only source
the workers knew. The resolver replaces that with a chain (explicit flag ->
release bucket -> cwd -> derive from the dictionary), and the derivation is a
sorted-Kahn topological sort over the raw bundle's ``links`` — verified
byte-identical to simulator-written DataImportOrder.txt files for the real
bpsych and omix3 dictionaries. Deletion correctness needs children deleted
before parents, i.e. the reverse of the submission order produced here.

The chain tests patch the lazy-imported S3 helpers on
``g3dt.upload.metadata_submitter`` (the resolver binds them at call time).
"""
import json
import logging
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from g3dt.config import EnvConfig
from g3dt.import_order import (
    ImportOrderError,
    derive_import_order,
    ensure_local_dictionary,
    resolve_import_order,
    to_deletion_order,
)


def _env(**overrides) -> EnvConfig:
    base = dict(
        name="test",
        is_ec2=False,
        region="ap-southeast-2",
        dictionary_version="v1.0.0",
        aws_profile=None,
        aws_secret_name="sec",
        schema_s3_uri="schema-bucket/schema.json",
        domain="d",
        app_name="a",
        namespace="n",
        cluster_name="c",
        schema_repo="Org/schema-repo",
    )
    base.update(overrides)
    return EnvConfig(**base)


def _link(target):
    return {"name": f"{target}s", "target_type": target, "multiplicity": "many_to_one"}


#: A miniature raw bundle shaped exactly like the live dictionaries: meta
#: blobs, an unsubmittable program, a properties-less entry, a subgroup link,
#: an unknown link target, and the non-alphabetical rna_assay->expression_file
#: dependency that pins the sorted-Kahn tie-breaking.
FIXTURE = {
    "_definitions.yaml": {"datetime": {}},
    "_terms.yaml": {},
    "_settings.yaml": {"_dict_version": "v1.0.0"},
    "notes.yaml": {"id": "notes"},  # no properties -> not a node
    "program.yaml": {
        "id": "program", "submittable": False, "properties": {"name": {}}, "links": [],
    },
    "project.yaml": {
        "id": "project", "properties": {"code": {}}, "links": [_link("program")],
    },
    "subject.yaml": {
        "id": "subject", "properties": {"x": {}}, "links": [_link("project")],
    },
    "core_metadata_collection.yaml": {
        "id": "core_metadata_collection", "properties": {"x": {}},
        "links": [_link("project")],
    },
    "sample.yaml": {
        "id": "sample", "properties": {"x": {}},
        "links": [{"exclusive": False, "required": True,
                   "subgroup": [_link("subject"), _link("core_metadata_collection")]}],
    },
    "rna_assay.yaml": {
        "id": "rna_assay", "properties": {"x": {}}, "links": [_link("sample")],
    },
    "expression_file.yaml": {
        "id": "expression_file", "properties": {"x": {}}, "links": [_link("rna_assay")],
    },
    "orphan.yaml": {
        "id": "orphan", "properties": {"x": {}}, "links": [_link("missing_node")],
    },
}

#: Hand-computed sorted-Kahn result for FIXTURE (see each derivation test).
EXPECTED_ORDER = [
    "orphan", "project", "core_metadata_collection", "subject",
    "sample", "rna_assay", "expression_file",
]


# --------------------------------------------------------------------------- #
# derive_import_order                                                          #
# --------------------------------------------------------------------------- #
def test_derive_reproduces_known_bundle_order():
    """Input: the fixture bundle -> Expected: the exact hand-computed order.

    Byte-parity with simulator-written DataImportOrder.txt files (verified
    against the real bpsych/omix3 bundles during design) is what makes derived
    and file-sourced deletes interchangeable. The exact list also pins the
    sorted tie-break: rna_assay precedes expression_file despite alphabetical
    order, because the file DEPENDS on the assay.
    """
    assert derive_import_order(FIXTURE) == EXPECTED_ORDER


def test_derive_skips_meta_unsubmittable_and_propertyless_entries():
    """The _*.yaml blobs, submittable:false (program), and entries without
    'properties' are not nodes.

    'program' is server-side infrastructure — including it would make it the
    final deletion target after reversal, and sheepdog refuses that anyway.
    """
    order = derive_import_order(FIXTURE)
    assert "program" not in order
    assert "notes" not in order
    assert not any(n.startswith("_") for n in order)


def test_derive_flattens_subgroup_links():
    """'sample' sorts after BOTH its subgroup targets.

    Subgroup links are real FKs. gen3_validator's get_node_link only inspects
    links[0] for a subgroup — the exact class of miss this self-contained
    parser exists to avoid; the fixture's subgroup must yield both edges.
    """
    order = derive_import_order(FIXTURE)
    assert order.index("sample") > order.index("subject")
    assert order.index("sample") > order.index("core_metadata_collection")


def test_derive_places_cmc_by_its_links_not_last():
    """core_metadata_collection sits right after project, not forced last.

    gen3_validator's get_node_order force-appends cmc last, which a deletion
    reversal would delete FIRST — before the file nodes that link to it,
    breaking FK order. Placement by links means reversal deletes it last.
    """
    order = derive_import_order(FIXTURE)
    assert order.index("core_metadata_collection") == order.index("project") + 1


def test_derive_drops_edges_to_unknown_targets():
    """A link to a node not in the bundle is ignored, not an error.

    'orphan' links to 'missing_node'; it must still be emitted (as a root),
    since dictionaries legitimately reference excluded targets (program).
    """
    assert "orphan" in derive_import_order(FIXTURE)


def test_derive_appends_cycle_members_with_warning(caplog):
    """Mutually-linked nodes are appended at the end and named in a warning.

    A cycle is impossible in a valid Gen3 dictionary, but if one sneaks in,
    silently dropping the nodes would leave data undeleted with a clean exit.
    """
    cyclic = {
        "project.yaml": {"id": "project", "properties": {"x": {}}, "links": []},
        "a.yaml": {"id": "a", "properties": {"x": {}}, "links": [_link("b")]},
        "b.yaml": {"id": "b", "properties": {"x": {}}, "links": [_link("a")]},
    }
    with caplog.at_level(logging.WARNING):
        order = derive_import_order(cyclic)
    assert order == ["project", "a", "b"]
    assert "a" in caplog.text and "b" in caplog.text


def test_to_deletion_order_filters_then_reverses():
    """Excluded nodes are removed BEFORE reversing (same as the old workers)."""
    assert to_deletion_order(["project", "subject", "sample"], ["project"]) == [
        "sample", "subject",
    ]


# --------------------------------------------------------------------------- #
# ensure_local_dictionary                                                      #
# --------------------------------------------------------------------------- #
def _urlopen_returning(body: bytes):
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=BytesIO(body))
    cm.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=cm)


def test_ensure_local_dictionary_downloads_once_then_caches(tmp_path):
    """First call downloads and writes the bundle; second call is pure cache.

    Delete workers may resolve the order several times across a batch — the
    dictionary must not be re-fetched per study.
    """
    body = json.dumps(FIXTURE).encode()
    fake = _urlopen_returning(body)
    with patch("g3dt.import_order.urllib.request.urlopen", fake):
        p1 = ensure_local_dictionary(_env(), "v9.9.9", schema_dir=tmp_path)
        p2 = ensure_local_dictionary(_env(), "v9.9.9", schema_dir=tmp_path)
    assert p1 == p2
    assert fake.call_count == 1
    assert json.loads(p1.read_text())["project.yaml"]["id"] == "project"


def test_ensure_local_dictionary_rejects_zero_byte_cache(tmp_path):
    """A zero-byte cached file triggers a re-download.

    `wget -O` (pull_dict.sh) creates the target before downloading, so a
    failed pull leaves a zero-byte artifact behind — observed in the real
    ~/.g3dt/schemas cache. Trusting existence alone would derive an order
    from nothing.
    """
    from g3dt.config import dictionary_filename

    (tmp_path / dictionary_filename(_env(), "v9.9.9")).write_bytes(b"")
    fake = _urlopen_returning(json.dumps(FIXTURE).encode())
    with patch("g3dt.import_order.urllib.request.urlopen", fake):
        p = ensure_local_dictionary(_env(), "v9.9.9", schema_dir=tmp_path)
    assert fake.call_count == 1
    assert p.stat().st_size > 0


def test_ensure_local_dictionary_never_caches_a_failed_download(tmp_path):
    """Non-JSON download -> ImportOrderError, and nothing is written.

    The whole point of validate-before-write + atomic replace: a failed
    download must not poison the cache the way wget -O demonstrably does.
    """
    fake = _urlopen_returning(b"<html>404-ish body</html>")
    with patch("g3dt.import_order.urllib.request.urlopen", fake):
        with pytest.raises(ImportOrderError):
            ensure_local_dictionary(_env(), "v9.9.9", schema_dir=tmp_path)
    # No bundle and no temp .part left behind (the autouse conftest marker
    # file lives in tmp_path too, so scope to dictionary artifacts).
    assert not list(tmp_path.glob("*.json")) and not list(tmp_path.glob("*.part"))


# --------------------------------------------------------------------------- #
# resolve_import_order — the chain                                             #
# --------------------------------------------------------------------------- #
_STUDY = SimpleNamespace(s3_metadata_path="s3://gold/release_jsons/v2.0.0/cdah/")


def test_explicit_local_path_beats_every_other_source(tmp_path):
    """An explicit --import-order wins over a cwd file AND a release bucket."""
    explicit = tmp_path / "custom_order.txt"
    explicit.write_text("project\nsubject\n")
    (tmp_path / "DataImportOrder.txt").write_text("decoy\n")

    nodes, source = resolve_import_order(
        env_cfg=_env(), session=MagicMock(),
        import_order=str(explicit), study_cfg=_STUDY, cwd=tmp_path,
    )
    assert nodes == ["project", "subject"]
    assert "explicit" in source and "custom_order.txt" in source


def test_explicit_s3_uri_is_read_via_s3_reader(tmp_path, monkeypatch):
    """An s3:// --import-order goes through the session-respecting S3 reader."""
    from g3dt.upload import metadata_submitter as ms

    monkeypatch.setattr(
        ms, "read_data_import_order_txt_s3",
        lambda uri, session: ["project", "subject"],
    )
    nodes, source = resolve_import_order(
        env_cfg=_env(), session=MagicMock(),
        import_order="s3://b/DataImportOrder.txt", cwd=tmp_path,
    )
    assert nodes == ["project", "subject"]
    assert source == "explicit --import-order s3://b/DataImportOrder.txt"


def test_explicit_path_missing_is_fatal_never_falls_through(tmp_path):
    """A typo'd explicit path errors — it must NEVER fall through the chain.

    Falling through would silently order a destructive run from a source the
    operator did not choose. (The old read_data_import_order_txt helper
    returned [] on a missing file — deleting zero nodes with a clean exit.)
    """
    (tmp_path / "DataImportOrder.txt").write_text("decoy\n")
    with pytest.raises(ImportOrderError):
        resolve_import_order(
            env_cfg=_env(), session=MagicMock(),
            import_order=str(tmp_path / "nope.txt"), cwd=tmp_path,
        )


def test_release_bucket_wins_over_cwd_for_registered_study(tmp_path, monkeypatch):
    """The release's own DataImportOrder.txt outranks a cwd file.

    User-chosen precedence: the release copy is authoritative for the study's
    pinned version, exactly as the upload path treats it — a stale stray file
    in whatever directory the operator happens to be in must not win.
    """
    from g3dt.upload import metadata_submitter as ms

    (tmp_path / "DataImportOrder.txt").write_text("stale\n")
    monkeypatch.setattr(
        ms, "find_data_import_order_file_s3",
        lambda s3_uri, session: f"{s3_uri}DataImportOrder.txt",
    )
    monkeypatch.setattr(
        ms, "read_data_import_order_txt_s3",
        lambda uri, session: ["project", "subject"],
    )
    nodes, source = resolve_import_order(
        env_cfg=_env(), session=MagicMock(), study_cfg=_STUDY, cwd=tmp_path,
    )
    assert nodes == ["project", "subject"]
    assert source.startswith("release bucket s3://gold/")


def test_release_bucket_error_warns_and_falls_through(tmp_path, monkeypatch, caplog):
    """A bucket without the file warns and falls through to the cwd file.

    A listing failure must not block a delete another source can serve
    correctly — but it must stay visible in the log.
    """
    from g3dt.upload import metadata_submitter as ms

    def _boom(s3_uri, session):
        raise FileNotFoundError("No DataImportOrder.txt file found")

    monkeypatch.setattr(ms, "find_data_import_order_file_s3", _boom)
    (tmp_path / "DataImportOrder.txt").write_text("project\nsubject\n")
    with caplog.at_level(logging.WARNING):
        nodes, source = resolve_import_order(
            env_cfg=_env(), session=MagicMock(), study_cfg=_STUDY, cwd=tmp_path,
        )
    assert nodes == ["project", "subject"]
    assert "current directory" in source
    assert "falling back" in caplog.text


def test_registry_free_worker_skips_release_bucket(tmp_path, monkeypatch):
    """study_cfg=None (synthetic mode) never touches S3 listing."""
    from g3dt.upload import metadata_submitter as ms

    def _fail(*a, **k):
        raise AssertionError("release bucket consulted in registry-free mode")

    monkeypatch.setattr(ms, "find_data_import_order_file_s3", _fail)
    (tmp_path / "DataImportOrder.txt").write_text("project\n")
    nodes, _ = resolve_import_order(
        env_cfg=_env(), session=MagicMock(), study_cfg=None, cwd=tmp_path,
    )
    assert nodes == ["project"]


def test_cwd_file_preserves_legacy_behaviour(tmp_path):
    """With no flag, no registry, no bucket: the cwd file is used, as always."""
    (tmp_path / "DataImportOrder.txt").write_text("project\nsubject\nsample\n")
    nodes, source = resolve_import_order(
        env_cfg=_env(), session=MagicMock(), cwd=tmp_path,
    )
    assert nodes == ["project", "subject", "sample"]
    assert "current directory" in source


def test_dict_version_derives_from_downloaded_dictionary(tmp_path, monkeypatch):
    """--dict-version pulls that tag's bundle and derives the order from it.

    This is the escape hatch for deleting data submitted under an OLDER
    dictionary whose node layout differs from today's deployed one.
    """
    monkeypatch.setenv("G3DT_SCHEMA_DIR", str(tmp_path / "schemas"))
    fake = _urlopen_returning(json.dumps(FIXTURE).encode())
    with patch("g3dt.import_order.urllib.request.urlopen", fake):
        nodes, source = resolve_import_order(
            env_cfg=_env(), session=MagicMock(),
            dict_version="v0.9.0", cwd=tmp_path,
        )
    assert nodes == EXPECTED_ORDER
    assert "derived from dictionary v0.9.0" in source


def test_default_derives_from_deployed_schema_s3_uri(tmp_path, monkeypatch):
    """No file anywhere and no --dict-version: derive from the deployed dict.

    The deployed schema at s3://{schema_s3_uri} is readable from both a
    laptop and the EC2 box, making the zero-flag default work everywhere —
    the exact scenario that used to crash with FileNotFoundError.
    """
    from g3dt.upload import metadata_submitter as ms

    seen = {}

    def _read(uri, session):
        seen["uri"] = uri
        return FIXTURE

    monkeypatch.setattr(ms, "read_metadata_json_s3", _read)
    nodes, source = resolve_import_order(
        env_cfg=_env(), session=MagicMock(), cwd=tmp_path,
    )
    assert nodes == EXPECTED_ORDER
    assert seen["uri"] == "s3://schema-bucket/schema.json"
    assert "deployed dictionary" in source


def test_missing_deployed_dictionary_error_names_the_ssm_fact(tmp_path, monkeypatch):
    """If even the deployed dictionary is unreadable, the error is actionable:
    it names app/schema_s3_uri and both flags that bypass derivation."""
    from g3dt.upload import metadata_submitter as ms

    def _boom(uri, session):
        raise FileNotFoundError("NoSuchKey")

    monkeypatch.setattr(ms, "read_metadata_json_s3", _boom)
    with pytest.raises(ImportOrderError) as exc:
        resolve_import_order(env_cfg=_env(), session=MagicMock(), cwd=tmp_path)
    msg = str(exc.value)
    assert "app/schema_s3_uri" in msg
    assert "--dict-version" in msg and "--import-order" in msg


def test_empty_dictionary_is_fatal(tmp_path, monkeypatch):
    """A derivation yielding zero nodes errors instead of deleting nothing.

    An object that parses as JSON but is not a Gen3 bundle (wrong key, wrong
    file) must not produce a clean 'nothing to delete' run.
    """
    from g3dt.upload import metadata_submitter as ms

    monkeypatch.setattr(ms, "read_metadata_json_s3", lambda uri, session: {})
    with pytest.raises(ImportOrderError):
        resolve_import_order(env_cfg=_env(), session=MagicMock(), cwd=tmp_path)
