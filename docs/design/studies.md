# The study registry (normative, 4.1.0)

The study registry defines exactly what `g3dt metadata upload` sends to a
commons. This document is the contract for where it lives, how names
resolve, and how the legacy registry retires. Sections 2–5 are normative
and pinned by tests (`test_studies_registry.py`,
`test_study_resolver_compat.py`, `test_cli_study_cmds.py`,
`test_cli_study_migrate.py`).

## 1. Why it moved

Through 4.0.x the registry was `s3://<metadata-bucket>/config/studies.yaml`
with a marker `studies:` block override. Live use surfaced four traps: the
loader swallowed every error (an expired SSO session read as "no studies
configured"); env separation hung on a `{study}_{env}` key-suffix
convention with a literal-key fallback that defeated it; editing was a raw
download/edit/reupload with whole-file clobber risk; and a local marker
block silently shadowed the shared registry.

## 2. Storage (normative)

One SSM **String** parameter per study:

```
/{project}/{env}/studies/<name> = {"project_id": "...",
                                   "program_id": "...",
                                   "s3_metadata_path": "s3://..."}
```

* The tree path IS the environment — staging and prod cannot cross-resolve.
* One parameter per study makes every write atomic; repointing one study
  can never drop another.
* The subtree rides along in `resolver._fetch_params`'s recursive
  (paginated) fetch, so reads share the same cached round-trip and the same
  loud failure modes as every other resolved name.
* This is the toolkit's **OPERATIONAL** config category: written by
  `g3dt study` (and only it), never by `cdk deploy`. The CDK repo's
  ssm-publishing drift guard does not count it.

## 3. Names and resolution (normative)

* Canonical names are `^[a-z][a-z0-9_]*$` — enforced on write, with
  guidance (`CDAH` → "use 'cdah'; the mixed-case Gen3 project_id goes in
  --project-id"). Names ending in `_staging`/`_prod`/`_test` are rejected
  on write: the env lives in the path now.
* `StudyConfig.key` is the **bare** name.
* Lookups are forgiving: `resolve_study(X, env)` tries `X.lower()`, then
  `X.lower()` with a trailing `_{env_base}` stripped. The strip is the wire
  compat: dispatched service scripts re-resolve the key the CLI passed,
  which was `{study}_{env}` under toolkits < 4.1 — both forms hit the same
  record, and only THIS env's suffix is ever stripped.
* A miss lists the registered names from the source actually consulted,
  adds a did-you-mean, and an empty registry prints the exact
  `g3dt study add` / `g3dt study migrate` commands. Auth failures
  propagate — never an empty registry.

## 4. Fallback and deprecation (normative)

Resolution order: SSM subtree → (only if EMPTY) the legacy S3
`studies.yaml`, with a once-per-process stderr `DEPRECATED` warning naming
`g3dt study migrate`. Legacy keys are lowercased and stripped of this env's
suffix; other-env keys stay suffixed (so `ausdiab_prod` can never silently
resolve in staging, and the bulk-upload `is_prod` key check still sees it).
The marker `studies:` block is ignored with a one-time notice. The S3
fallback is removed in 5.0.

Mutating commands (`add`/`set`/`remove`/`repoint`) refuse to run while the
fallback is serving: writing one SSM record would shadow the whole legacy
file at once. `g3dt study migrate` is the only path from one world to the
other.

## 5. The commands

| Command | Gate | Contract |
|---|---|---|
| `study list` | — | Bare names on stdout (script-friendly); source + guidance on stderr; empty registry exits 0. `config studies` is an alias. |
| `study show <name>` | — | Record + liveness (DataImportOrder.txt + node-JSON count — the exact upload preflight). |
| `study add <name> --project-id --program-id --path` | typed prod | `Overwrite=False`; an existing name points at `study set`. No S3 existence check (paths are often exported later). |
| `study set <name> [fields]` | typed prod | Read-merge-write; prints `field: old -> new`; never auto-creates. |
| `study remove <name> [--yes]` | destructive (typed on prod; `--yes` never bypasses) | Registry entry only — data and uploads untouched. |
| `study repoint --release <tag>\|--latest [--studies a,b] [--dry-run]` | typed prod, after the diff | Swaps the `vX.Y.Z` path segment. Validates EVERY target (upload's own helpers) before writing ANY — one bad target writes nothing. `--latest` reads `max(release_tag)` from the releases ledger; tags normalise `v2.1.0`/`2.1.0` → path segment `v2.1.0` (the ledger stores no `v`). |
| `study migrate [--force] [--dry-run]` | typed prod | Imports the legacy file (all-or-nothing: malformed entries or un-`--force`d conflicts write NOTHING), verifies every record by re-reading SSM, and only then renames the file to `studies.yaml.migrated`. Idempotent. |

## 6. Rollout

1. Release 4.1.0; envs keep resolving via the fallback (older EC2 boxes
   included — their pinned toolkits read the still-present S3 file).
2. Per env: `g3dt study migrate` (file retired → SSM provably the only
   source), then bump the wrapper's `toolkitVersion` so the box runs 4.1+.
3. 5.0 removes the S3 fallback and the legacy suffix candidates.

IAM note: operators need `ssm:PutParameter`/`DeleteParameter` on
`/{project}/{env}/studies/*`; the EC2 box and CodeBuild only read.
