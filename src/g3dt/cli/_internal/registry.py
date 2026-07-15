"""A tiny local registry of EC2-dispatched runs (``~/.g3dt/runs.json``).

Maps a human-friendly ``run_id`` to the underlying SSM command id so users
never have to copy long AWS identifiers — ``g3dt jobs status <run_id>`` and
``g3dt jobs logs <run_id>`` look everything up here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

REGISTRY_DIR = Path(os.path.expanduser("~")) / ".g3dt"
REGISTRY_FILE = REGISTRY_DIR / "runs.json"


def _load() -> Dict[str, dict]:
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(data: Dict[str, dict]) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))


def record(
    run_id: str,
    *,
    command_id: Optional[str],
    instance_id: Optional[str],
    env: str,
    argv: List[str],
    mechanism: str,
    s3_log_uri: Optional[str] = None,
    cw_log_group: Optional[str] = None,
    started_at: Optional[str] = None,
) -> None:
    """Persist a dispatched run."""
    data = _load()
    data[run_id] = {
        "run_id": run_id,
        "command_id": command_id,
        "instance_id": instance_id,
        "env": env,
        "argv": list(argv),
        "mechanism": mechanism,
        "s3_log_uri": s3_log_uri,
        "cw_log_group": cw_log_group,
        "started_at": started_at,
    }
    _save(data)


def get(run_id: str) -> Optional[dict]:
    return _load().get(run_id)


def all_runs() -> Dict[str, dict]:
    return _load()
