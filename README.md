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
  `cdk deploy`. To change a deployed setting (e.g. the dictionary version),
  edit that file and redeploy — the value flows to SSM.
- **OUTPUTS** — every resource name the CDK creates plus the mirrored Gen3
  app facts, published to SSM under `/{project}/{env}/...` on deploy. `g3dt`
  reads these live (cached one round-trip per invocation) and never stores
  them locally.

Because the CLI and the infrastructure read the same parameters, they cannot
disagree — and because each environment has its own tree (including its own
`ec2/instanceId`), running a job against the wrong environment's resources is
structurally impossible.

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
