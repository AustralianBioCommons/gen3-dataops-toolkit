# gen3-dataops-toolkit (`g3dt`)

Operate Gen3 AWS data-pipeline environments from one pip-installable CLI.

`g3dt` is the tooling half of the Gen3 DataOps platform: the
[gen3-aws-data-pipeline](https://github.com/AustralianBioCommons/gen3-aws-data-pipeline)
CDK app deploys a complete pipeline per project/environment and publishes every
resource name to AWS SSM Parameter Store; `g3dt` resolves those names at
runtime and gives operators one command surface for dictionary deploys,
metadata upload/delete, indexd registration, EC2 job dispatch, and Kubernetes
restarts. The dbt half of the platform lives in
[gen3-dbt-template](https://github.com/AustralianBioCommons/gen3-dbt-template).

**No AWS resource name is compiled into this package.** The same wheel
operates any project: it is targeted purely by `--env`, the project's SSM tree
(`/{project}/{env}/...`), and a tiny local bootstrap marker.

## Install

```bash
pip install gen3-dataops-toolkit
```

## Bootstrap (the only local configuration)

`g3dt` needs to know just the project and region — everything else comes from
SSM. Create `~/.g3dt/g3dt.yaml`:

```yaml
project: etl                # your projectId
region: ap-southeast-2
default_env: test
profiles:                   # optional: AWS named profile per env
  test: etl_test            # (omit entirely on EC2/CodeBuild — ambient
  staging: etl_staging      #  role credentials are used)
studies:                    # optional: the project's study registry;
  mystudy_test:             # alternatively upload it once per env to
    project_id: MyStudy     # s3://<metadata-bucket>/config/studies.yaml
    program_id: program1
    s3_metadata_path: s3://my-bucket/metadata/mystudy/
```

Search order: `./g3dt.yaml` → `~/.g3dt/g3dt.yaml` → `/etc/g3dt/g3dt.yaml`
(the EC2 job box's copy, written by CDK user-data). Env vars override:
`G3DT_PROJECT`, `AWS_REGION`, `G3DT_DEFAULT_ENV`.

## Quick start

```bash
g3dt config envs                 # environments with a deployed SSM tree
g3dt config show --env test      # every resolved name — the safety check
g3dt ec2 up --env test           # start the env's job box (SSM-managed)
g3dt metadata upload --study mystudy --env test --on ec2
g3dt jobs logs <run-id> --follow # live logs; laptop can sleep, job keeps going
g3dt ec2 down --env test         # or let the auto-stop alarm handle it
g3dt docs                        # the full operations overview
```

## How configuration works

There are exactly two kinds of configuration:

- **INPUTS** — human-authored values, committed as
  `config/<projectId>.<env>.json` in the CDK repo and read only by
  `cdk deploy`. To change what an environment *declares*, edit that file and
  redeploy — the value flows to SSM.
- **OUTPUTS** — every resource name the CDK creates plus the mirrored Gen3
  app facts, published to SSM under `/{project}/{env}/...` on deploy. `g3dt`
  reads these live (cached one round-trip per invocation) and never stores
  them locally.

Because the CLI and the infrastructure read the same parameters, they cannot
disagree — and because each environment has its own tree (including its own
`ec2/instanceId`), running a job against the wrong environment's resources is
structurally impossible.


## CI isolation and the release contract

**Only the dbt template's `ci` target is prefixed.** `g3dt config dbt-env`
emits, alongside the real names, the CI-isolation variants the template's
`ci` target consumes: `G3DT_DB_RAW_SILVER_CI` / `G3DT_DB_RAW_GOLD_CI`
(`ci_` + the real database name) and `G3DT_S3_SILVER_DATA_DIR_CI` /
`G3DT_S3_GOLD_DATA_DIR_CI` (`dbt_ci/` under the same buckets). Commit-
triggered CI builds land there; every other target (default, local) and the
release build keep the real, unprefixed names — so CI can never advance the
warehouse's Iceberg snapshots that releases pin. The library enforces the
other half: `find_db_for_model` always skips `ci_`-prefixed databases, so
`g3dt release write` can never pin a release to a CI-build snapshot.

**Snapshot pinning.** `AthenaValidationWriter.construct_json` /
`AthenaGoldWriter.construct_json` honour a pre-set `snapshot_id` (reading the
table `FOR VERSION AS OF` that snapshot) and only fetch the latest snapshot
when unpinned — the contract the release-JSON export relies on for
reproducible releases.

**Concurrency.** `release_writer.run` processes models with a bounded thread
pool (`max_workers`, default 8) and fails at the end naming every failed
model (inserts are idempotent — re-run to fill the remainder). The S3
writers (`write_release_jsons_to_s3`, `write_validation_json_to_s3`) accept
`s3_client=` (pass one per worker thread) and `key_prefix=` (write a
verification tree without touching real artifacts).

**The validation gate.** `g3dt.validate.run_validation_gate(glue_database,
athena_s3_output, aws_region, workgroup)` queries the latest
`validation_id` in `full_validation_results` for REAL failures — the
known-noise patterns in `VALIDATION_GATE_IGNORED_ERRORS` and synthetic
studies are excluded. The validator Glue job fails when rows come back, so a
green validation Step Function means schema-clean data; the operator loop is
gate fails -> inspect the results table -> fix data -> re-run until green.
`validate_pipeline` also accepts pre-computed loop-invariants
(`schema=`/`resolver=`/`metadata_table=`) and `write_iceberg=False` so a
multi-study caller resolves the schema once, lists the validation prefix
once, and batches all studies into a single Iceberg INSERT.

### Where the data dictionary comes from

Composed from the env's inputs as
`{dictionary_base_url}/{schema_repo}/refs/tags/{dictionary_version}/{dictionary_path}`.
Only `schema_repo` and `dictionary_version` are required; `app/dictionary_base_url`
and `app/dictionary_path` are optional and default to raw GitHub and the schema
repo's conventional layout, so environments deployed before they existed keep
working. `g3dt config show --env <env>` prints the composed URL.

### Promoting a dictionary across environments

A dictionary version is *content*, not infrastructure: it changes far more often
than buckets or clusters do. Rather than a `cdk deploy` per environment per
version, `dict pull`, `dict upload` and `dict deploy` all accept `--version`:

```bash
g3dt dict deploy --env test    --version v1.1.7
g3dt dict deploy --env staging --version v1.1.7   # same tag, no cdk deploy
```

An override does not persist, so `config show` keeps reporting the declared
version until the CDK config catches up — `g3dt config diff --env <env>` reports
exactly that gap and exits 1, so it can gate CI.

Synthetic data is only schema-valid against the dictionary that generated it, so
`synth generate` records the dictionary version in each batch and `synth upload`
refuses a batch that doesn't match the version being uploaded (override with
`--allow-version-mismatch`).

## Development

```bash
poetry install
poetry run python3 -m pytest
```

## Provenance

This toolkit was ported (working tree only) from
[AustralianBioCommons/acdc-aws-etl-pipeline](https://github.com/AustralianBioCommons/acdc-aws-etl-pipeline),
the ACDC ETL monolith, as part of the Gen3 DataOps platform refactor (2026).
It starts at version **2.0.0**; versions ≤ 1.2.0 on PyPI are the legacy
`acdc_aws_etl_pipeline` package, which continues to operate the legacy ACDC
pipeline unchanged.
