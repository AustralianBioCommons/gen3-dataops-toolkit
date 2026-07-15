"""Resolve a project's deployed resource names from AWS SSM Parameter Store.

The CDK app (gen3-aws-data-pipeline) writes one SSM parameter per resource it
creates, under the tree ``/{project}/{env}/...``, and mirrors the human-authored
Gen3 app facts under ``/{project}/{env}/app/*``. This module reads that tree
once per process and exposes it as a typed :class:`ResolvedConfig`, so nothing
else in the toolkit ever hard-codes an AWS resource name.

The tree's exact shape is enforced on the infrastructure side by the CDK repo's
drift-guard test (``test/ssm-publishing.test.ts``): 38 parameters from the SSM
stack plus ``ec2/instanceId`` published by the EC2 stack (39 total).
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import Mapping, Optional

import boto3

from g3dt.config import ConfigError


def _fetch_params(project: str, env: str, *, session: boto3.Session) -> dict:
    """Return ``{relative_name: value}`` for every parameter under /{project}/{env}.

    ``relative_name`` is the path with the ``/{project}/{env}/`` prefix removed,
    e.g. ``/etl/test/buckets/metadata`` -> ``buckets/metadata``.
    """
    client = session.client("ssm")
    base = f"/{project}/{env}"
    # get_parameters_by_path returns at most 10 params per call, so paginate.
    paginator = client.get_paginator("get_parameters_by_path")
    out: dict = {}
    for page in paginator.paginate(Path=base, Recursive=True, WithDecryption=True):
        for p in page["Parameters"]:
            out[p["Name"][len(base) + 1 :]] = p["Value"]
    if not out:
        raise ConfigError(
            f"No SSM parameters found under {base} — has CDK been deployed for "
            f"this env? Run `cdk deploy --all -c project={project} -c env={env}` "
            f"in gen3-aws-data-pipeline, then verify with:\n"
            f"    aws ssm get-parameters-by-path --path {base} --recursive"
        )
    return out


@dataclass(frozen=True)
class ResolvedConfig:
    """All resource names CDK published for one project/env.

    ``params`` is the raw ``{relative_name: value}`` map (used by ``config show``
    to print the whole subtree). The properties are typed accessors for the
    names the toolkit reads most often; each raises a friendly
    :class:`ConfigError` if its parameter is missing, which surfaces an
    incomplete CDK deploy immediately instead of a cryptic AWS error later.
    """

    project: str
    env: str
    params: Mapping[str, str] = field(repr=False)

    def _req(self, key: str) -> str:
        try:
            return self.params[key]
        except KeyError:
            raise ConfigError(
                f"SSM parameter /{self.project}/{self.env}/{key} is missing. "
                f"Found: {', '.join(sorted(self.params)) or '(none)'}"
            )

    # --- OUTPUT names (CDK-created) ---
    @property
    def metadata_bucket(self) -> str:
        return self._req("buckets/metadata")

    @property
    def metadata_db(self) -> str:
        return self._req("glue/db/metadata")

    @property
    def release_db(self) -> str:
        return self._req("release/db")

    @property
    def release_table(self) -> str:
        return self._req("release/table")

    @property
    def athena_workgroup(self) -> str:
        return self._req("athena/workgroup")

    @property
    def athena_output_location(self) -> str:
        return self._req("athena/outputLocation")

    @property
    def ec2_instance_id(self) -> str:
        return self._req("ec2/instanceId")

    @property
    def ec2_log_group(self) -> str:
        return self._req("ec2/logGroup")

    @property
    def ec2_log_bucket(self) -> str:
        return self._req("ec2/logBucket")

    @property
    def ec2_log_prefix(self) -> str:
        return self._req("ec2/logPrefix")

    @property
    def region(self) -> str:
        return self._req("meta/region")

    @property
    def toolkit_version(self) -> str:
        return self._req("meta/toolkitVersion")

    def app(self, key: str) -> str:
        """A mirrored Gen3 app fact, e.g. ``app("domain")`` (snake_case keys)."""
        return self._req(f"app/{key}")

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """A raw leaf by relative name, or ``default`` if absent."""
        return self.params.get(key, default)


def _session(profile: Optional[str], region: Optional[str]) -> boto3.Session:
    """A boto3 session with an EXPLICIT region.

    The region always comes from the marker (file value, or its AWS_REGION
    env-var override) rather than boto3's own chain: botocore only honours
    AWS_DEFAULT_REGION, so on the EC2 job box — where user-data exports
    AWS_REGION and there is no ~/.aws/config — an ambient-chain session has
    no region at all (NoRegionError).
    """
    if region is None:
        from g3dt.config import load_marker  # late import (config imports us)

        region = load_marker()["region"]
    return boto3.Session(profile_name=profile, region_name=region)


@functools.lru_cache(maxsize=None)
def resolve(
    project: str,
    env: str,
    profile: Optional[str] = None,
    region: Optional[str] = None,
) -> ResolvedConfig:
    """Resolve all deployed names for ``project``/``env`` from SSM (cached).

    Cached on ``(project, env, profile, region)`` for the life of the process,
    so a single CLI invocation makes exactly one ``get_parameters_by_path``
    round-trip no matter how many commands ask for a name. Call
    ``resolve.cache_clear()`` in tests.

    ``profile`` is the local AWS named profile to authenticate the read (e.g.
    ``etl_test``); ``None`` uses the default credential chain — which is the
    instance profile on the EC2 job box and the build role in CodeBuild.
    ``region`` defaults to the marker's region.
    """
    session = _session(profile, region)
    return ResolvedConfig(
        project=project, env=env, params=_fetch_params(project, env, session=session)
    )


def list_envs(
    project: str, profile: Optional[str] = None, region: Optional[str] = None
) -> list:
    """Return the environment names that have an SSM tree under /{project}/.

    Reads the parameter names one level down (e.g. ``/etl/test/...`` -> ``test``).
    """
    session = _session(profile, region)
    client = session.client("ssm")
    paginator = client.get_paginator("get_parameters_by_path")
    envs = set()
    for page in paginator.paginate(Path=f"/{project}", Recursive=True):
        for p in page["Parameters"]:
            parts = p["Name"].split("/")  # ['', project, env, ...]
            if len(parts) > 2:
                envs.add(parts[2])
    return sorted(envs)
