"""Static contract checks for full_deploy_dd_and_synth.sh.

The full-deploy shell script is never executed by the suite (it drives
aws/kubectl/argocd), so regressions in its wiring only surface in live
deploys — after the billable LLM generation has already run. These checks
pin the two contracts that broke (or nearly broke) live:

1. Step [6] must scope the upload to the run's studies. Without
   `--projects "${STUDIES}"`, the upload step re-derives "what to upload"
   from whatever is on disk; a stale 'synth50' directory from an earlier
   batch against a different commons was picked up, 404'd, and aborted a
   deploy.
2. Wrapped Python entrypoints must run under ${G3DT_PYTHON} (the
   interpreter that owns this g3dt installation), not a bare python3 —
   on AL2023 the system python3 is not the pipx venv's interpreter and
   lacks the package's dependencies.
"""
import re
from pathlib import Path

SERVICES = (
    Path(__file__).resolve().parent.parent / "src" / "g3dt" / "services"
)
DEPLOY_SH = SERVICES / "synthetic_data" / "full_deploy_dd_and_synth.sh"


def test_step6_upload_is_scoped_to_the_runs_studies():
    """
    Expected: the upload invocation passes --projects "${STUDIES}" so step
    [6] is scoped exactly like the delete (step [4], -p "${STUDIES}") and
    generate (step [5], --studies "${STUDIES}") steps already are.
    """
    text = DEPLOY_SH.read_text()
    assert '--projects "${STUDIES}"' in text


def test_deploy_scripts_use_g3dt_python_not_bare_python3():
    """
    Expected: no wrapped script invokes a bare `python3 ` — every Python
    entrypoint runs under "${G3DT_PYTHON:-python3}" (exported by
    runner.bash_script; the :-python3 fallback keeps direct/manual runs
    working).
    """
    for script in (
        DEPLOY_SH,
        SERVICES / "dictionary" / "deploy_dd.sh",
        SERVICES / "delete" / "delete_metadata.sh",
        SERVICES / "upload" / "metadata" / "upload_all_studies.sh",
    ):
        for lineno, line in enumerate(script.read_text().splitlines(), 1):
            stripped = line.split("#", 1)[0]  # ignore comments
            assert not re.search(r"(?<![\w${:-])python3 ", stripped), (
                f"{script.name}:{lineno} invokes bare python3: {line.strip()}"
            )


def test_studies_env_var_is_required_by_the_script():
    """
    Expected: the script refuses to run without G3DT_SYNTH_STUDIES — the
    old fallback silently deployed the ACDC demo study set on any project.
    """
    text = DEPLOY_SH.read_text()
    assert 'G3DT_SYNTH_STUDIES:?' in text
    assert "AusDiab" not in text
