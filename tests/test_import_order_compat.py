"""The release-export Glue job's import of ``construct_data_import_order``.

Background: the aws-gen3-pipeline template's ``write_data_release_to_json.py``
imports ``construct_data_import_order`` from ``g3dt.utils.athena_utils`` to
write ``DataImportOrder.txt`` next to each study's release JSONs. Version
5.0.0 of this toolkit moved the derivation into ``g3dt.import_order`` and
dropped that name, so a pipeline pinned to ``toolkitVersion >= 5.0.0`` failed
every release export with an ImportError (ACDC prod, 2026-09-09). The name is
kept as a thin wrapper; these tests pin its contract so it cannot silently
disappear again.
"""
from g3dt.utils import athena_utils


def _bundle():
    """A three-node dictionary bundle: program <- project <- subject."""
    return {
        "_definitions.yaml": {"UUID": {"type": "string"}},
        "program.yaml": {"properties": {"name": {}}, "submittable": False, "links": []},
        "project.yaml": {
            "properties": {"code": {}},
            "links": [{"name": "programs", "target_type": "program"}],
        },
        "subject.yaml": {
            "properties": {"submitter_id": {}},
            "links": [{"name": "projects", "target_type": "project"}],
        },
    }


def test_construct_data_import_order_is_importable_from_athena_utils():
    """The Glue job does ``from g3dt.utils.athena_utils import construct_data_import_order``.

    Input: the module. Expected: the attribute exists and is callable. If this
    fails, every release export on a >= 5.0 deployment fails at import time.
    """
    assert callable(getattr(athena_utils, "construct_data_import_order", None))


def test_construct_data_import_order_reads_the_bundle_at_the_uri(monkeypatch):
    """Given an s3 URI, the wrapper loads that bundle and returns parents first.

    Input: a fake loader returning the three-node bundle above.
    Expected: ``["project", "subject"]`` -- ``program`` is unsubmittable and
    is left out, ``project`` precedes ``subject`` because subject links to it.
    """
    seen = {}

    def fake_loader(uri):
        seen["uri"] = uri
        return _bundle()

    monkeypatch.setattr("g3dt.validate.validate.load_schema_from_s3_uri", fake_loader)

    order = athena_utils.construct_data_import_order("s3://schemas/acdc.json")

    assert seen["uri"] == "s3://schemas/acdc.json"
    assert order == ["project", "subject"]
