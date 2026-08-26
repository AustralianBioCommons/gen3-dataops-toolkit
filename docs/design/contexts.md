# Design: contexts — one obvious answer to "what am I pointed at?"

Status: accepted (2026-08-25). Ships in 3.8.0; amended 2026-08-26 for 4.0.0
(local-first `contexts`, guidance-mode `discover`, `config current`, widened
prod gates). Sections marked **normative** are
contracts that tests cite.

## 1. Motivation

Operating g3dt requires joining four independent sources: a single-project marker
file, three environment variables (`G3DT_PROJECT`, `AWS_REGION`,
`G3DT_DEFAULT_ENV`), a required `--env` flag on almost every command, and the
ambient AWS SSO profile. Nothing enumerates what infrastructure is actually
available — that knowledge lives in the operator's head, and misalignment
surfaces as `No SSM parameters found` errors at best, or a command aimed at the
wrong environment at worst.

Reality is a matrix: `(project, env) → (AWS account/profile, region)`,
many-to-many in both directions. One account can host several projects; one
project spans several accounts (test/staging/prod). The marker's single
`project:` key cannot express this, and `default_env` — written by `config set`,
documented everywhere — is consumed by nothing.

Observed live during the ACDC platform port: the operator's marker pointed at a
different project, so every command needed a
`G3DT_PROJECT=... AWS_PROFILE=... AWS_REGION=...` prefix, and the first
dispatched upload initially tailed the wrong project's log group.

## 2. The Context model (normative)

A **context** is a named 5-tuple that fully determines where a command acts:

```
(name, project, env, profile, region)   + optional production: true
```

- `env` is always the **base** environment name. `_ec2` never appears in a
  context — it remains a wire/auth mechanism (section 8).
- `profile: absent` means the ambient credential chain (instance profile,
  CodeBuild role, exported AWS_PROFILE).
- Switching environment IS switching context. `--env` survives only as a
  compatibility alias that selects the matching configured context.

## 3. Marker schema v2

```yaml
current: acdc/staging
contexts:
  acdc/staging: { project: acdc, env: staging, profile: acdc_staging }
  acdc/prod:    { project: acdc, env: prod, profile: acdc_prod, production: true }
# v1 keys remain top-level and honored:
project: acdc          # active-project fallback + older-toolkit goodwill
region: ap-southeast-2
default_env: staging   # kept in sync with `current` on switch
profiles: { ... }      # legacy; honored when contexts: is absent
studies: { ... }       # DEAD since 4.1.0 — no longer read (see studies.md)
ssh_key: ...           # GLOBAL, is_ec2-only consumption
ssh_user: ...
llm_api_key_file: ...  # GLOBAL operator secret path
```

Validation on load: a context `env` carrying `_ec2` is rejected; reserved names
(`use`, `contexts`, `discover`, `add`, `forget`, `current`, `show`, `set`) are
rejected. `ssh_*`/`llm_api_key_file` stay global. The marker `studies:`
block is IGNORED since 4.1.0 (a one-time stderr notice points at
`g3dt study migrate`) — the registry lives per env in SSM
(`/{project}/{env}/studies/*`, design doc `studies.md`).

## 4. Legacy synthesis (normative — the back-compat core)

When the marker has **no `contexts:` key** (v1 laptop marker, the EC2 box's
3-key `/etc/g3dt/g3dt.yaml`, or no marker at all with `G3DT_PROJECT` set),
contexts are synthesized in memory, never written:

1. Each `profiles:` entry `env: profile` → context `{project}/{env}` with that
   profile (`source="legacy"`).
2. `default_env` naming an env with no `profiles:` entry → an ambient context
   `{project}/{default_env}` (this IS the box marker).
3. `current` is implied from `default_env` when that context exists.
4. `--env X` matching no synthesized context (including no-marker +
   `G3DT_PROJECT` — the CodeBuild path) → an **ephemeral** context
   `{project}/{env_base(X)}` with `profile = aws_profile_for(X)` and marker
   region (`source="synthetic"`). Behavior is byte-identical to 3.7.x plus the
   banner.

Only when an explicit `contexts:` block exists does an unmatched `--env` become
an error, with guidance to `g3dt config discover --add` / `g3dt config add`.

## 5. Resolution precedence (normative)

```
--ctx flag  >  --env flag (compat alias)  >  $G3DT_CONTEXT  >
marker `current`  >  legacy default_env synthesis  >  error (or None if optional)
```

- `--ctx` + `--env` together: allowed only when the context's env equals
  `env_base(env)`; otherwise an error.
- `--env X_ec2` matches on `env_base(X)` and forces ambient auth exactly as
  today (`resolve_env`'s `is_ec2` guard is untouched).
- `--env` against explicit contexts matches `env == context.env` within the
  active project; zero matches → error with guidance; more than one → error
  listing candidates ("use --ctx").

## 6. The banner (normative)

Every command — read or write — prints the resolved context as its **first
output line, on stderr**, exactly once per process:

```
ctx acdc/staging → project=acdc env=staging profile=acdc_staging region=ap-southeast-2
ctx acdc/prod [PROD] → project=acdc env=prod profile=acdc_prod region=ap-southeast-2
ctx acdc/staging (remote) → project=acdc env=staging_ec2 profile=(ambient) region=ap-southeast-2
ctx (none configured) → run 'g3dt config discover <aws-profile> --add' or pass --env/--ctx
```

- **stderr** because `eval "$(g3dt config dbt-env ...)"` must keep a byte-clean
  stdout.
- `[PROD]` red+bold when the context is production-classified; `(remote)` when
  the effective env carries `_ec2`; `(ambient)` when profile is None.
- No network calls — the banner is pure marker data.
- On EC2 dispatch the remote re-entry prints its own banner, which lands in the
  CloudWatch/S3 job logs via the existing tee.

## 7. Command surface

All context operations live in the `config` group; each command reads as a
plain-English intent. **No aliases, no new top-level commands.**

| Command | Purpose |
|---|---|
| `g3dt config use <name>` | Switch context. Prod target → loud warning + confirm. Auto-migrates a v1 marker (preserving all v1 keys). |
| `g3dt config contexts` | List contexts from the local marker only — offline by default: current `*`, `[PROD]`, `(legacy)` source. `--verify` adds the DEPLOYED column (one `get_parameter meta/toolkitVersion` per context, botocore tracebacks suppressed; failures → `?` with an `aws sso login` hint). `--no-verify` stays accepted as the default's name. |
| `g3dt config discover [PROFILE]` | Scan one AWS profile's account for deployed `/{project}/{env}` trees (one filtered `describe_parameters`), offering `aws sso login` when the session is stale — the primary flow. No arguments: print the model and the local profile list, no network. `--all-profiles`: sweep every profile (stale sessions skipped). `--add`: register findings (never overwrites). Verification of registered contexts lives in `contexts --verify`. |
| `g3dt config current` | Print the bare current context name to stdout (script-friendly); exit 1 with guidance when none is selected. |
| `g3dt config add <name> --project P --env E [--profile PR] [--region R] [--production]` | Explicit manual registration. |
| `g3dt config forget <name>` | Local-only removal ("forget", deliberately — nothing in AWS is touched). Refuses `current` without `--force`. |
| `g3dt config show` | Now defaults to the current context; `--env/--ctx/--study/--full` still accepted. |
| `envs` / `diff` / `dbt-env` | Unchanged names, context-aware. `dbt-env --env` and `release write --env` are accepted forever (buildspec contract). |
| `config studies` | Alias of `g3dt study list` since 4.1.0 (the SSM registry; `studies.md`). |
| `config set` | **Deprecated + hidden.** Still functional for the four legacy keys. Its one remaining real job moves to `g3dt synth set-key <path>`. |

## 8. Invariants (normative — each pinned by a test)

a. File-less `G3DT_PROJECT` + `--env` (the CodeBuild path) resolves unchanged.
b. Region resolves with no file (`AWS_REGION` → default), passed explicitly to
   every boto3 session.
c. The 3-key box marker (`project/region/default_env`) keeps working; deployed
   boxes run older toolkits until replaced.
d. `--env` stays accepted on `dbt-env` and `release write`.
e. The remote dispatch argv keeps the exact form `--env <base>_ec2` — it is a
   **cross-version wire protocol** (the box runs the SSM-pinned toolkit).
f. Confirmation always happens locally, before EC2 dispatch.
g. `--yes` never bypasses a production prompt.

## 9. Production guards

Classification: `is_prod(env)` (substring, unchanged, still applied to resolved
study keys) OR the context's `production: true` OR "prod" in the context name.
With a named context active, the typed confirmation token is the **context
name** (`Type 'acdc/prod' to confirm`); with only a legacy/synthetic context,
the token remains the env name/target (preserving the pinned safety suite).
`g3dt config use <prod-context>` warns loudly and requires confirmation.

Gated commands (4.0.0): the synth lifecycle, `delete metadata`,
`metadata upload-all` — and now `dict upload`/`dict deploy` and every
`k8s restart-*` (the widest-blast-radius operations). `dict pull` stays
ungated (local download); `release write` is deliberately ungated because it
runs non-interactively in CodeBuild against real envs (section 8d).

## 10. Migration

Laptop v1 markers migrate automatically on the first `config use` (the
synthesized contexts are materialized; every v1 key is preserved so an older
toolkit reading the same file still works). Box markers are untouched until the
pipeline redeploys them. Optional upstream follow-up (not required): the CDK
user-data may additionally write `current: {project}/{env}`.

## 11. Out of scope / future

Comment-preserving marker rewrites (ruamel), a shell-prompt helper, the
user-data v2 marker. (Per-env study registries shipped in 4.1.0 —
`docs/design/studies.md`.)
