"""End-to-end sequencing tests for the ArgoCD restart scripts.

``argocd_restart_schema.sh`` / ``argocd_restart_ms.sh`` restart the commons'
schema microservices **serially, in list order**, waiting for each rollout to
report Healthy before starting the next; ``argocd_restart_etl.sh`` creates and
watches a run of a named ETL cronjob. Since v3.5.0 the targets are no longer
hardcoded: the list and cronjob name default to ``$G3DT_RESTART_SERVICES`` /
``$G3DT_ETL_CRONJOB`` — the env's SSM ``app/restart_services`` /
``app/etl_cronjob`` facts, exported by g3dt — with the classic Gen3 set as the
fallback for direct invocations.

That resolution and the restart ORDER are only observable by running the real
scripts, so these tests stub ``argocd``, ``jq``, ``kubectl``, and ``sleep`` on
PATH (recording every call, answering Healthy/succeeded immediately) and
assert exactly which resources are restarted, in which sequence. This is the
closest an offline test can get to a live `g3dt dict deploy` restart cycle.
"""
import os
import subprocess
from pathlib import Path

import pytest

K8S_OPS = Path(__file__).resolve().parent.parent / "src" / "g3dt" / "services" / "k8s_ops"

CLASSIC = [
    "sheepdog-deployment",
    "peregrine-deployment",
    "guppy-deployment",
    "portal-deployment",
]


@pytest.fixture
def stub_bin(tmp_path):
    """Stub argocd/jq/kubectl/sleep on PATH, recording every invocation.

    - ``argocd`` logs its args and exits 0 (its ``app get`` output is unused
      because ``jq`` is also stubbed).
    - ``jq`` answers '"Healthy"' so the per-resource wait loop exits on the
      first check.
    - ``sleep`` is a no-op so the serial waits don't slow the suite.
    - ``kubectl`` answers the etl script's job lifecycle: created job name,
      succeeded status, a pod name, and logs containing "Exit code: 0".
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record.txt"

    (bin_dir / "argocd").write_text(
        '#!/usr/bin/env bash\necho "argocd $*" >> "$STUB_RECORD"\nexit 0\n'
    )
    (bin_dir / "jq").write_text(
        '#!/usr/bin/env bash\necho \'"Healthy"\'\n'
    )
    (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    (bin_dir / "kubectl").write_text(
        "#!/usr/bin/env bash\n"
        'echo "kubectl $*" >> "$STUB_RECORD"\n'
        'case "$*" in\n'
        "  *current-context*) echo ctx ;;\n"
        "  *create\\ job*) echo job.batch/test-job ;;\n"
        "  *succeeded*) echo 1 ;;\n"
        "  *failed*) echo '' ;;\n"
        "  *get\\ pods*) echo test-pod ;;\n"
        '  *logs*) echo "Exit code: 0" ;;\n'
        "esac\nexit 0\n"
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir, record


def _run(script, stub_bin, args=(), extra_env=None, expect_rc=0):
    bin_dir, record = stub_bin
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        STUB_RECORD=str(record),
    )
    env.pop("G3DT_RESTART_SERVICES", None)
    env.pop("G3DT_ETL_CRONJOB", None)
    env.pop("G3DT_NAMESPACE", None)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", str(K8S_OPS / script), "-l", *args],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == expect_rc, result.stdout + result.stderr
    return record.read_text() if record.exists() else ""


def _restart_order(recorded):
    """Deployment names from the 'actions run ... restart' lines, in order."""
    names = []
    for line in recorded.splitlines():
        if "actions run" in line and "restart" in line:
            parts = line.split()
            names.append(parts[parts.index("--resource-name") + 1])
    return names


@pytest.mark.parametrize("script", ["argocd_restart_schema.sh", "argocd_restart_ms.sh"])
def test_env_restart_services_define_the_set_and_order(script, stub_bin):
    """
    Inputs:  G3DT_RESTART_SERVICES with a custom, reordered subset (what an
             env like omix3 publishes — no portal, since its frontend is
             redeployed manually outside this flow)
    Expected: exactly those deployments are restarted, serially, in the given
             order — for both the schema and ms variants of the script.
    """
    recorded = _run(
        script, stub_bin,
        args=("-d", "cd.example.org", "-a", "testgen3", "-n", "omix3"),
        extra_env={
            "G3DT_RESTART_SERVICES": "guppy-deployment,sheepdog-deployment",
        },
    )
    assert _restart_order(recorded) == ["guppy-deployment", "sheepdog-deployment"]
    assert "portal-deployment" not in recorded


@pytest.mark.parametrize("script", ["argocd_restart_schema.sh", "argocd_restart_ms.sh"])
def test_classic_set_when_nothing_configured(script, stub_bin):
    """
    Inputs:  no G3DT_RESTART_SERVICES and no -r (a pre-k8s-block deployment,
             or a direct invocation outside g3dt)
    Expected: the classic Gen3 four, in the historical order — existing
             environments keep restarting exactly what they always did.
    """
    recorded = _run(
        script, stub_bin,
        args=("-d", "cd.example.org", "-a", "testgen3", "-n", "cad"),
    )
    assert _restart_order(recorded) == CLASSIC


def test_r_flag_beats_env(stub_bin):
    """
    Inputs:  both G3DT_RESTART_SERVICES and an explicit -r
    Expected: -r wins — the flag is the per-run escape hatch above SSM.
    """
    recorded = _run(
        "argocd_restart_schema.sh", stub_bin,
        args=("-d", "d", "-a", "a", "-n", "ns", "-r", "portal-deployment"),
        extra_env={"G3DT_RESTART_SERVICES": "sheepdog-deployment"},
    )
    assert _restart_order(recorded) == ["portal-deployment"]


def test_missing_namespace_fails_fast(stub_bin):
    """
    Inputs:  no -n and no G3DT_NAMESPACE
    Expected: exit 1 before any argocd call. The old script silently defaulted
             to the legacy 'cad' namespace, which would restart another
             project's services when run outside g3dt.
    """
    recorded = _run(
        "argocd_restart_schema.sh", stub_bin,
        args=("-d", "d", "-a", "a"),
        expect_rc=1,
    )
    assert "actions run" not in recorded


def test_etl_cronjob_name_from_env(stub_bin):
    """
    Inputs:  G3DT_ETL_CRONJOB=custom-etl (the env's SSM app/etl_cronjob)
    Expected: the job is created from cronjob/custom-etl; with nothing set the
             classic etl-cronjob name is used.
    """
    recorded = _run(
        "argocd_restart_etl.sh", stub_bin,
        args=("-d", "d", "-a", "a", "-n", "ns"),
        extra_env={"G3DT_ETL_CRONJOB": "custom-etl"},
    )
    assert "--from=cronjob/custom-etl" in recorded

    recorded = _run(
        "argocd_restart_etl.sh", stub_bin,
        args=("-d", "d", "-a", "a", "-n", "ns"),
    )
    assert "--from=cronjob/etl-cronjob" in recorded


# --- restart_etl_and_ms.sh: the `g3dt k8s restart-ms` wrapper --------------
#
# The wrapper takes no flags: every setting arrives as a G3DT_* variable. It
# calls argocd_restart_etl.sh and then argocd_restart_ms.sh. Two regressions
# are pinned here. Before 5.0.0 it hardcoded ``-s`` on the ETL call (so every
# restart-ms began with an ``argocd app sync`` that could fail on unrelated
# drift and abort the whole run) and hardcoded ``-r <classic list>`` on the
# ms call (so ``--restart-services`` was silently ignored).

WRAPPER_ENV = {
    "G3DT_CLUSTER_NAME": "test-cluster",
    "G3DT_DOMAIN": "cd.example.org",
    "G3DT_APP_NAME": "testgen3",
    "G3DT_NAMESPACE": "cad",
}


def _run_wrapper(stub_bin, extra_env=None):
    """Run restart_etl_and_ms.sh end to end against the stubs.

    Unlike ``_run`` this passes no ``-l``: the wrapper has no flags, and the
    child scripts' ``argocd login --sso`` is answered by the argocd stub.
    ``aws eks update-kubeconfig`` is answered by an ``aws`` stub added here.
    """
    bin_dir, record = stub_bin
    (bin_dir / "aws").write_text(
        '#!/usr/bin/env bash\necho "aws $*" >> "$STUB_RECORD"\nexit 0\n'
    )
    (bin_dir / "aws").chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        STUB_RECORD=str(record),
        **WRAPPER_ENV,
    )
    env.pop("G3DT_RESTART_SERVICES", None)
    env.pop("G3DT_ETL_CRONJOB", None)
    env.pop("G3DT_SYNC", None)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", str(K8S_OPS / "restart_etl_and_ms.sh"), "staging"],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return record.read_text()


def test_restart_ms_wrapper_does_not_sync_by_default(stub_bin):
    """
    Background: on 2026-09-08 `g3dt k8s restart-ms -e staging` failed before
    touching any service because the wrapper always ran `argocd app sync`
    first and the app had unrelated drift (immutable Job templates).

    Inputs:  the wrapper run with the usual G3DT_* settings and NO G3DT_SYNC
    Expected: no `argocd app sync` call at all; the ETL job is still created
             and the services still restart.
    """
    recorded = _run_wrapper(stub_bin)
    assert "app sync" not in recorded
    assert "create job" in recorded
    assert _restart_order(recorded) == CLASSIC


def test_restart_ms_wrapper_syncs_once_when_asked(stub_bin):
    """
    Inputs:  G3DT_SYNC=1 (what `g3dt k8s restart-ms --sync` exports)
    Expected: exactly one `argocd app sync testgen3`, and it happens before
             the ETL job is created — the schema restart afterwards does not
             sync again.
    """
    recorded = _run_wrapper(stub_bin, extra_env={"G3DT_SYNC": "1"})
    lines = recorded.splitlines()
    sync_lines = [i for i, l in enumerate(lines) if "app sync testgen3" in l]
    assert len(sync_lines) == 1
    first_job = next(i for i, l in enumerate(lines) if "create job" in l)
    assert sync_lines[0] < first_job


def test_restart_ms_wrapper_honours_restart_services(stub_bin):
    """
    Background: the wrapper used to pass a hardcoded -r list to
    argocd_restart_ms.sh, which beat the G3DT_RESTART_SERVICES the CLI
    exported from --restart-services, so the flag never did anything.

    Inputs:  G3DT_RESTART_SERVICES=guppy-deployment
    Expected: only guppy is restarted; none of the classic four others appear.
    """
    recorded = _run_wrapper(
        stub_bin, extra_env={"G3DT_RESTART_SERVICES": "guppy-deployment"}
    )
    assert _restart_order(recorded) == ["guppy-deployment"]
