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
