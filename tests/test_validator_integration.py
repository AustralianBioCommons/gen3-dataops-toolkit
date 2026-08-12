"""Integration tests exercising the real gen3_validator dependency.

Background
----------
The toolkit's validation pipeline (``g3dt.validate.validate``) delegates schema
resolution and record validation to the ``gen3_validator`` package:
``load_and_resolve_schema`` constructs ``gen3_validator.ResolveSchema`` over a
downloaded schema bundle, and ``validate_pipeline`` feeds records through
``gen3_validator.validate.validate_list_dict``.

Every other test in this suite mocks gen3_validator away, which means an
upstream regression in reference resolution would sail through the toolkit's
CI unnoticed — exactly what happened when the resolver could not handle
node-level ``$ref`` entries into ``_terms.yaml`` (the shape the official Gen3
dictionary uses): affected nodes were silently dropped and their data was
never validated.

These tests run the REAL resolver and validator over a small in-repo bundle
so the toolkit's suite fails if the upstream contract breaks. They need
gen3-validator >= 2.2.0 (bundle-aware resolution).
"""

import json

import gen3_validator
import pytest


@pytest.fixture
def schema_bundle_path(tmp_path):
    """Write a minimal Gen3-style schema bundle to disk and return its path.

    The bundle deliberately includes the two shapes that broke the old
    resolver: a node-level ``$ref`` into ``_terms.yaml``, and the ``UUID`` key
    defined in both ``_terms.yaml`` and ``_definitions.yaml`` with different
    content.
    """
    bundle = {
        "_settings.yaml": {"_dict_version": "1.0.0"},
        "_terms.yaml": {
            "sample_note": {"description": "Documentation about samples"},
            "UUID": {"description": "terms-flavoured UUID"},
        },
        "_definitions.yaml": {
            "UUID": {"type": "string", "pattern": "^[a-f0-9-]{36}$"},
        },
        "sample.yaml": {
            "id": "sample",
            "type": "object",
            "required": ["sample_id"],
            "properties": {
                "type": {"type": "string"},
                "sample_id": {"$ref": "_definitions.yaml#/UUID"},
                "volume": {
                    "type": "number",
                    "term": {"$ref": "_terms.yaml#/sample_note"},
                },
            },
        },
    }
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(bundle))
    return str(path)


def test_resolve_schema_handles_node_level_terms_refs(schema_bundle_path):
    """The real resolver resolves a bundle shaped like the official dictionary.

    This is the exact call ``load_and_resolve_schema`` makes
    (``g3dt/validate/validate.py``). Before gen3-validator 2.2.0 the
    node-level ``_terms.yaml`` ref made the resolver drop ``sample.yaml``
    entirely, so downstream validation would raise "key not found in resolved
    schema" for every sample record.

    Expected: sample.yaml survives resolution, its term block carries the
    _terms.yaml content, and the collision key resolved to the
    _definitions.yaml flavour (a pattern, not a description).
    """
    resolver = gen3_validator.ResolveSchema(schema_path=schema_bundle_path)
    resolver.resolve_schema()

    assert "sample.yaml" in resolver.schema_resolved
    properties = resolver.schema_resolved["sample.yaml"]["properties"]
    assert properties["volume"]["term"] == {
        "description": "Documentation about samples"
    }
    assert properties["sample_id"]["pattern"] == "^[a-f0-9-]{36}$"


def test_validate_list_dict_over_really_resolved_schema(schema_bundle_path):
    """Records validate end to end through the resolved schema, no mocks.

    Mirrors validate_pipeline's usage: records carry a ``type`` field naming
    their node, and validate_list_dict pulls each node's resolved schema by
    that name. A structurally valid record must produce no FAIL rows; a
    record violating the resolved ``_definitions.yaml#/UUID`` pattern must
    produce one — proving the $ref actually resolved into an enforceable
    constraint rather than being dropped.
    """
    resolver = gen3_validator.ResolveSchema(schema_path=schema_bundle_path)
    resolver.resolve_schema()

    valid_record = {
        "type": "sample",
        "sample_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
        "volume": 1.5,
    }
    invalid_record = {
        "type": "sample",
        "sample_id": "NOT-A-UUID",
        "volume": 1.5,
    }

    valid_results = gen3_validator.validate.validate_list_dict(
        [valid_record], resolver.schema_resolved
    )
    invalid_results = gen3_validator.validate.validate_list_dict(
        [invalid_record], resolver.schema_resolved
    )

    assert [r for r in valid_results if r["validation_result"] == "FAIL"] == []
    failures = [r for r in invalid_results if r["validation_result"] == "FAIL"]
    assert len(failures) == 1
    assert failures[0]["invalid_key"] == "sample_id"
    assert failures[0]["validator"] == "pattern"
