"""Node import-order resolution for metadata deletion.

Deleting Gen3 metadata must walk nodes children-before-parents, which the
toolkit historically took from a ``DataImportOrder.txt`` in the caller's
working directory — a silent dependency that crashed runs started anywhere
else. This module resolves the order from an explicit chain of sources:

1. an explicit ``--import-order`` (local path or ``s3://`` URI) — fatal if
   unreadable, never silently skipped;
2. the study's release bucket (registered studies only — releases ship a
   ``DataImportOrder.txt`` next to the node JSONs);
3. a ``DataImportOrder.txt`` in the current directory (legacy behavior);
4. derivation from the dictionary itself: a topological sort over the raw
   bundle's ``links`` (proven byte-identical to simulator-written order
   files), reading either the deployed dictionary at ``schema_s3_uri`` or a
   ``--dict-version`` bundle cached under ``~/.g3dt/schemas``.

Deliberately NOT used: ``gen3_validator``'s ``DataDictionary.get_node_order``
(it force-moves core_metadata_collection last, which a deletion reversal would
delete FIRST — before the file nodes that link to it) and its
``get_node_link`` (inspects only ``links[0]`` for subgroups).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Bundle keys that are shared definitions, not nodes.
_META_KEYS = frozenset(
    {"_definitions.yaml", "_terms.yaml", "_settings.yaml", "root.yaml", "metaschema.yaml"}
)

_ORDER_FILENAME = "DataImportOrder.txt"


class ImportOrderError(Exception):
    """A node order could not be resolved; the message names the fix."""


def derive_import_order(schema: dict) -> List[str]:
    """Topologically sort a raw Gen3 dictionary bundle into submission order.

    Works on the UNRESOLVED bundle ({"<node>.yaml": {...}, ...}): ``links``
    blocks are self-contained (no $refs), so no schema resolution is needed.
    Skips the shared ``_*.yaml`` definitions, entries without ``properties``,
    and ``submittable: false`` nodes (which removes ``program``). Every
    ``links`` entry is flattened — subgroup members and plain links alike —
    into edges ``parent -> child``; edges to nodes outside the set are
    dropped (this handles ``project -> program``).

    Kahn's algorithm with sorted tie-breaking keeps the output deterministic
    and byte-compatible with simulator-written DataImportOrder.txt files.
    Members of a dependency cycle (impossible in a valid Gen3 dictionary) are
    appended at the end with a warning rather than dropped.

    Returns SUBMISSION order (parents first); callers reverse for deletion.
    """
    nodes = {}
    for key, value in schema.items():
        if key in _META_KEYS or not isinstance(value, dict):
            continue
        if "properties" not in value or value.get("submittable") is False:
            continue
        name = key[: -len(".yaml")] if key.endswith(".yaml") else key
        nodes[name] = value

    graph = {n: [] for n in nodes}
    in_degree = {n: 0 for n in nodes}
    for name, node in nodes.items():
        for entry in node.get("links", []) or []:
            members = entry.get("subgroup", [entry]) if isinstance(entry, dict) else []
            for member in members:
                parent = member.get("target_type")
                if parent in nodes:
                    graph[parent].append(name)
                    in_degree[name] += 1

    queue = deque(sorted(n for n, d in in_degree.items() if d == 0))
    ordered: List[str] = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for child in sorted(graph[node]):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)
        queue = deque(sorted(queue))

    leftovers = sorted(n for n in nodes if n not in ordered)
    if leftovers:
        logger.warning(
            "Dictionary link cycle detected; appending unordered node(s): %s "
            "— deleting them may require a re-run.",
            ", ".join(leftovers),
        )
        ordered.extend(leftovers)
    return ordered


def ensure_local_dictionary(env_cfg, version: str, schema_dir: Optional[Path] = None) -> Path:
    """Return the local bundle for ``version``, downloading it if needed.

    The cache is ``$G3DT_SCHEMA_DIR`` (else ``~/.g3dt/schemas``), shared with
    pull_dict.sh. A cache hit requires a non-empty file that parses as JSON —
    ``wget -O`` leaves a zero-byte file behind on a failed pull, so mere
    existence is not trusted. Downloads are validated as JSON BEFORE being
    written, to a temp name replaced atomically, so a failed or partial
    download can never poison the cache.
    """
    from g3dt import config

    if schema_dir is None:
        schema_dir = Path(
            os.environ.get("G3DT_SCHEMA_DIR", "~/.g3dt/schemas")
        ).expanduser()
    target = schema_dir / config.dictionary_filename(env_cfg, version)

    if target.exists() and target.stat().st_size > 0:
        try:
            json.loads(target.read_text(encoding="utf-8"))
            return target
        except ValueError:
            logger.warning("Cached dictionary %s is corrupt; re-downloading.", target)

    url = config.dictionary_url(env_cfg, version)
    logger.info("Downloading dictionary %s from %s", version, url)
    try:
        with urllib.request.urlopen(url) as resp:
            body = resp.read()
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise ImportOrderError(
            f"Could not download dictionary tag '{version}' from {url}: {exc}. "
            f"Check the tag exists in the schema repo, or pass --import-order."
        )
    try:
        json.loads(body.decode("utf-8"))
    except ValueError:
        raise ImportOrderError(
            f"Downloaded dictionary tag '{version}' from {url} is not valid "
            f"JSON — is the tag/path right?"
        )
    schema_dir.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".part")
    tmp.write_bytes(body)
    os.replace(tmp, target)
    return target


def to_deletion_order(nodes: Sequence[str], exclude_nodes: Sequence[str]) -> List[str]:
    """Filter excluded nodes out of a submission order and reverse it."""
    kept = [n for n in nodes if n not in exclude_nodes]
    kept.reverse()
    return kept


def resolve_import_order(
    *,
    env_cfg,
    session,
    import_order: Optional[str] = None,
    dict_version: Optional[str] = None,
    study_cfg=None,
    cwd: Optional[Path] = None,
) -> Tuple[List[str], str]:
    """Resolve the node SUBMISSION order for a delete, returning (nodes, source).

    The chain (first hit wins): explicit ``import_order`` (fatal on failure —
    an explicit source is never silently skipped), the registered study's
    release bucket (``study_cfg.s3_metadata_path``; a listing error warns and
    falls through), a ``DataImportOrder.txt`` in the current directory
    (legacy behavior), then derivation from the dictionary — the
    ``dict_version`` bundle when given, else the deployed dictionary at
    ``s3://{env_cfg.schema_s3_uri}``.

    ``source`` is a human-readable description of the winning step; workers
    log it so operators can always see what ordered a destructive run.
    """
    from g3dt.upload.metadata_submitter import (
        find_data_import_order_file_s3,
        read_data_import_order_txt_s3,
        read_metadata_json_s3,
    )

    # 1. Explicit flag: local path or s3:// URI. Failures are fatal.
    if import_order:
        if import_order.startswith("s3://"):
            try:
                nodes = read_data_import_order_txt_s3(import_order, session)
            except Exception as exc:
                raise ImportOrderError(
                    f"Could not read --import-order {import_order}: {exc}"
                )
            return nodes, f"explicit --import-order {import_order}"
        path = Path(import_order)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ImportOrderError(
                f"Could not read --import-order {import_order}: {exc}"
            )
        return (
            [line.strip() for line in lines if line.strip()],
            f"explicit --import-order {path}",
        )

    # 2. Registered study: the release ships its own DataImportOrder.txt.
    if study_cfg is not None and str(
        getattr(study_cfg, "s3_metadata_path", "")
    ).startswith("s3://"):
        try:
            uri = find_data_import_order_file_s3(
                s3_uri=study_cfg.s3_metadata_path, session=session
            )
            return (
                read_data_import_order_txt_s3(uri, session),
                f"release bucket {uri}",
            )
        except Exception as exc:
            logger.warning(
                "No usable DataImportOrder.txt under %s (%s) — falling back.",
                study_cfg.s3_metadata_path,
                exc,
            )

    # 3. Legacy: a DataImportOrder.txt in the working directory.
    local = (cwd or Path.cwd()) / _ORDER_FILENAME
    if local.exists():
        lines = local.read_text(encoding="utf-8").splitlines()
        return (
            [line.strip() for line in lines if line.strip()],
            f"{_ORDER_FILENAME} in current directory ({local.resolve()})",
        )

    # 4. Derive from the dictionary.
    if dict_version:
        bundle_path = ensure_local_dictionary(env_cfg, dict_version)
        schema = json.loads(bundle_path.read_text(encoding="utf-8"))
        source = f"derived from dictionary {dict_version} ({bundle_path})"
    else:
        uri = f"s3://{env_cfg.schema_s3_uri}"
        try:
            schema = read_metadata_json_s3(uri, session)
        except Exception as exc:
            raise ImportOrderError(
                f"No DataImportOrder.txt found and the deployed dictionary at "
                f"{uri} (SSM app/schema_s3_uri) could not be read: {exc}. "
                f"Pass --import-order <path|s3://...> or --dict-version <tag>."
            )
        source = f"derived from deployed dictionary {uri}"

    nodes = derive_import_order(schema)
    if not nodes:
        raise ImportOrderError(
            f"Deriving the node order produced no submittable nodes — is the "
            f"object read for '{source}' a Gen3 dictionary bundle?"
        )
    return nodes, source
