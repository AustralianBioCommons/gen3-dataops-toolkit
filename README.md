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
operates any project: it is targeted by a **context** — a named
(project, env, profile, region) tuple — and the project's SSM tree
(`/{project}/{env}/...`).

## Install

```bash
pip install gen3-dataops-toolkit
```

## Bootstrap: discover, then use

```bash
g3dt config discover <aws-profile> --add    # logs the profile in if needed,
                                            # lists every deployed env its
                                            # account holds, registers them
g3dt config contexts                        # list them (current marked *);
                                            # offline — add --verify to check
                                            # what each account has deployed
g3dt config use myproj/test                 # point g3dt at one
```

That writes `~/.g3dt/g3dt.yaml` for you. **Every command prints the active
context first** (on stderr) — read that line before anything else:

```
ctx myproj/test → project=myproj env=test profile=myproj_test region=ap-southeast-2
```

Production contexts are marked `[PROD]`, switching to one asks for
confirmation, and destructive actions on them require typing the context
name — `--yes` never bypasses that.

The marker can also be written by hand (design doc:
`docs/design/contexts.md`); the study registry lives either in a top-level
`studies:` block or per env at `s3://<metadata-bucket>/config/studies.yaml`.
Search order: `./g3dt.yaml` → `~/.g3dt/g3dt.yaml` → `/etc/g3dt/g3dt.yaml`
(the EC2 job box's copy, written by CDK user-data). Legacy markers
(`project`/`default_env`/`profiles:` keys) keep working unchanged, as do the
env-var overrides `G3DT_PROJECT`, `AWS_REGION`, `G3DT_DEFAULT_ENV` — that is
the file-less CodeBuild/EC2 path.

## Quick start

```bash
g3dt config show                 # every resolved name — the safety check
g3dt ec2 up                      # start the context's job box (SSM-managed)
g3dt metadata upload --study mystudy --on ec2
g3dt jobs logs <run-id> --follow # live logs; laptop can sleep, job keeps going
g3dt ec2 down                    # or let the auto-stop alarm handle it
g3dt docs                        # the full operations overview
```

(`--env <name>` still works everywhere and selects the matching context;
CodeBuild's `g3dt config dbt-env --env $ENV` contract is unchanged.)

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
`ci` target consumes: `G3DT_DB_SILVER_CI` / `G3DT_DB_GOLD_CI`
(`ci_` + the real database name) and `G3DT_S3_SILVER_DATA_DIR_CI` /
`G3DT_S3_GOLD_DATA_DIR_CI` (`dbt_ci/` under the same buckets). Bronze is
ingest-only — dbt never writes it, and the template's synthetic demo data
generates at silver — so bronze gets only `G3DT_DB_BRONZE` (real-ingest
deployments resolve their sources.yml schema from it); pairs with an
aws-gen3-pipeline deployment >= v2.2.0. Toolkit
releases >= 3 read the raw-free medallion SSM keys and therefore require a
pipeline deployment >= v2.0.0, which publishes them. Commit-
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
version until the CDK config catches up —
`g3dt config diff --env <env> --file <wrapper>/config/<project>.<env>.json`
reports exactly that gap and exits 1, so it can gate CI.

Synthetic data is only schema-valid against the dictionary that generated it, so
`synth generate` records the dictionary version in each batch and `synth upload`
refuses a batch that doesn't match the version being uploaded (override with
`--allow-version-mismatch`).

### Synthetic data: LLM configuration

`synth generate --llm` and `synth deploy` generate LLM-realistic values with
[gen3-metadata-simulator](https://github.com/AustralianBioCommons/gen3-metadata-simulator).
The provider and model resolve with precedence **CLI flags > SSM > default**:
the CDK config's optional `llm` block publishes `app/llm_provider` /
`app/llm_model` to SSM, so every operator gets the deployment's values, and
`--llm-provider` / `--llm-model` override them for one run (e.g. to try a
model before adding it to the CDK config). Environments deployed without the
block fall back to provider `anthropic`, and the `--llm` path errors with
guidance when no model is configured anywhere.

Only the API key stays local — as a *path* to the file holding it, never the
key itself, set once per operator:

```bash
g3dt synth set-key ~/.g3dt/anthropic_api_key
g3dt synth generate AusDiab_Simulated --llm -n 5 -e test
```

(or per run with `--llm-api-key-file`; the vendor env var `ANTHROPIC_API_KEY`
/ `OPENAI_API_KEY` also works as a fallback.) The old `~/.g3dt/.env`
(`LLM_PROVIDER`/`LLM_MODEL`/`LLM_API_KEY_FILE`) is **no longer read**.
`g3dt config show --env <env>` prints the resolved provider, model, and key
path.

### The synthetic data lifecycle

`synth generate` builds a batch locally (keyless `random` by default, `--llm`
for realistic values); `synth upload` pushes a batch to the commons; `synth
delete` removes one; and `synth deploy` runs the whole cycle in one command —
dictionary pull + upload, schema restarts, delete of the previous batch,
LLM-generate, upload, and the ETL run.

**Studies and record counts are batch inputs, not environment facts** — they
are passed per command and never come from SSM:

```bash
g3dt synth generate synthetic_dataset_1 --llm -n 100 -e test
g3dt synth deploy -e test --studies synthetic_dataset_1 -n 100 --prev-version v1.2.0
```

`deploy --skip-dict` runs the synthetic-data-only flow (delete previous
batch, generate, upload, ETL) without re-uploading the dictionary or
restarting the schema microservices — use it when the deployed dictionary is
already current. Deleting the previous batch is the flow's one destructive
step, so it asks for confirmation (decline to skip it, or pass
`--skip-delete` to skip without prompting); on a first deploy, when no
previous batch exists locally, deletion is skipped automatically.

`deploy` requires `--studies`: the list scopes every step — the previous
batch deleted (at `--prev-version`), the batch generated, and the batch
uploaded. Stale study directories from an earlier run that happen to sit in
the same version directory are skipped with a log line, never submitted.
(Before 4.0.0, omitting `--studies` silently deployed a hardcoded ACDC demo
set — wrong for every other project.)

### Kubernetes restart targets

Restarts and dictionary uploads against a production context require typing
the context name to confirm, the same gate the synth and delete commands use.

`k8s restart-schema`, `k8s restart-ms`, `dict deploy`, and `synth deploy`
restart the commons' schema microservices **serially, in a configured order**,
waiting for each rollout to go Healthy before the next; `k8s restart-etl` and
the deploy flows run a named ETL cronjob. Both targets resolve with precedence
**CLI flags > SSM > default**: the CDK config's optional `k8s` block publishes
`app/restart_services` (comma-separated deployment names) and
`app/etl_cronjob`, `--restart-services` / `--etl-cronjob` override them for
one run, and environments deployed without the block keep the classic Gen3 set
(`sheepdog-deployment,peregrine-deployment,guppy-deployment,portal-deployment`
/ `etl-cronjob`). A commons that manages some service outside this flow (e.g.
a manually redeployed frontend) simply omits it from the list in its wrapper
config. `g3dt config show --env <env>` prints the resolved values.

## Verifying download access (check-download)

Registration alone does not prove a file can be downloaded. Two failure modes
are invisible until a user clicks the file in the portal: an Indexd record
with no storage URL (nothing to download, ever), and a record Fence refuses
to sign a URL for. `g3dt indexd check-download` walks the exact chain the
portal hits — Indexd record → storage URL → DRS object → access methods →
Fence signed URL — and reports PASS/FAIL per object, exiting non-zero if any
object fails so it can gate a deployment step.

Run it before a release, and after registering new files. The env selects the
API key secret and the key's JWT selects the commons, so there is no URL to
pass (and none to get wrong).

```bash
g3dt indexd check-download --env staging                    # sample the 25 newest
g3dt indexd check-download --env staging --limit 50
g3dt indexd check-download --env prod PREFIX/<uuid-1> PREFIX/<uuid-2>
```

With no GUIDs, the newest objects for the env's commons are sampled from the
indexd registry (latest revision per baseid). The registry may live in a
different AWS account than the commons being checked; if the env's AWS
profile cannot reach it, pass GUIDs explicitly.

Reading a failure:

| Symptom | Meaning |
|---|---|
| `Indexd status: 404` | the object is not registered — a registration problem, not a download one |
| `urls: []` / no access methods | registered but with no storage location; it can never download |
| `Access endpoint … 401` | authorization: the API key's user lacks `read-storage` on the record's `authz` resource — an authz gap, not a broken key |
| `Access endpoint … 500` | Fence has the permission but failed to sign — a service-side fault |

On a 401, compare what the record requires
(`https://commons.example.org/index/<did>`, the `authz` field) with what the
key's user actually holds (`https://commons.example.org/user/user`):
downloads require `read-storage` on the record's authz resource, which a user
holding only `create` does not have.

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
