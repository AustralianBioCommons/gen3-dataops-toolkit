#!/usr/bin/env python3
"""Verify that registered file objects are actually downloadable.

Walks the chain a portal user hits — Indexd record -> storage URLs -> DRS
object -> access methods -> Fence signed URL — and reports PASS/FAIL per
object. Exits non-zero if any object fails, so it can gate a deployment step.

The environment selects the credential and the credential selects the commons:
``--env`` resolves the env's ``aws_secret_name`` (AWS Secrets Manager, or a
local file when the value is an absolute path), and the commons URL comes from
that key's JWT. There is no URL to pass and therefore none to get wrong — see
``src/g3dt/indexd/file_access.py`` for why that matters.

GUIDs are optional: with none given, the script samples the most recently
registered objects for this commons from the indexd registry (Athena) — the
latest revision per baseid, newest first, ``--limit`` of them. The registry
may live in a different AWS account than the commons, so sampling needs an
env whose AWS profile can reach it; explicit GUIDs skip Athena entirely.

Prefer the CLI wrapper, which resolves the env the same way:

    g3dt indexd check-download --env staging                # auto-sample
    g3dt indexd check-download --env staging PREFIX/<uuid>

Direct usage:

    poetry run python src/g3dt/services/indexd/verify_file_access.py \
        --env staging PREFIX/005d97ab-... PREFIX/ffff19f8-...
"""

import argparse
import sys

from g3dt import config as g3dt_config
from g3dt.indexd.file_access import (
    api_key_for_env,
    commons_auth,
    sample_recent_guids,
    verify_objects,
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "guids",
        nargs="*",
        help="object GUIDs, e.g. PREFIX/uuid; omit to sample the registry",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="how many recently registered objects to sample when no GUIDs "
        "are given (default: 25)",
    )
    parser.add_argument(
        "--env",
        required=True,
        help="environment, e.g. staging (selects the API key secret)",
    )
    parser.add_argument(
        "--key-path",
        default=None,
        help="break-glass: local Gen3 API key JSON file, instead of the env's secret",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        env_cfg = g3dt_config.resolve_env(args.env)
    except g3dt_config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        api_key = api_key_for_env(env_cfg, args.key_path)
    except Exception as exc:  # boto/OSError/JSON — all mean "no usable key"
        print(
            f"ERROR: could not load the Gen3 API key for env '{args.env}': {exc}",
            file=sys.stderr,
        )
        return 2

    commons, auth = commons_auth(api_key)
    # Provenance: an operator can eyeball exactly what was checked, and with
    # which credential, before trusting a PASS.
    print(
        f"Env: {env_cfg.name} | Secret: {args.key_path or env_cfg.aws_secret_name} "
        f"| Commons: {commons}"
    )

    guids = args.guids
    if not guids:
        print(
            f"No GUIDs given — sampling the {args.limit} most recently "
            f"registered object(s) for {commons} from the indexd registry."
        )
        try:
            guids = sample_recent_guids(env_cfg, commons, args.limit)
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        if not guids:
            print(
                f"ERROR: the registry has no objects for {commons}/index/index"
                " — nothing to check. Register files first, or pass GUIDs.",
                file=sys.stderr,
            )
            return 2
        print(f"Sampled: {', '.join(guids)}")

    failures = verify_objects(commons, auth, guids)

    print(f"\n{'=' * 80}")
    print(f"Checked {len(guids)} object(s): {len(failures)} failure(s).")
    for guid in failures:
        print(f"  FAILED: {guid}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
