"""Run local subprocesses with live-streamed output and clean exit handling.

Commands shell out through :func:`run` so failures surface as ``typer.Exit``
with the child's exit code (preserving the ``set -e`` semantics of the wrapped
shell scripts). Tests patch :func:`run` to assert the exact argv built.

Service scripts ship *inside* the installed package (``g3dt/services/...``),
so the toolkit works from a bare ``pip install`` with no repository checkout —
:func:`package_path` resolves them from the package directory.
"""
from __future__ import annotations

import subprocess
import sys
from importlib import resources
from pathlib import Path
from typing import List, Optional, Sequence

import typer


def package_path(relpath: str) -> Path:
    """Resolve a path shipped inside the ``g3dt`` package (e.g. ``services/...``).

    ``relpath`` may use the historical ``services/...`` form; it is resolved
    against the installed package directory, never a repo checkout.
    """
    return Path(str(resources.files("g3dt"))) / relpath


def python_script(relpath: str, *args: str) -> List[str]:
    """Build an argv that runs a packaged service script with this interpreter.

    Using ``sys.executable`` guarantees the script runs inside the same
    environment as the CLI itself.
    """
    return [sys.executable, str(package_path(relpath)), *[str(a) for a in args]]


def bash_script(relpath: str, *args: str) -> List[str]:
    """Build an argv that runs a packaged shell script via bash."""
    return ["bash", str(package_path(relpath)), *[str(a) for a in args]]


def run(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict] = None,
    echo: bool = True,
) -> int:
    """Run ``argv``, streaming output live. Raise ``typer.Exit`` on failure.

    Returns 0 on success. The working directory defaults to the caller's
    current directory (there is no repo root to default to — the toolkit is
    installable-only).
    """
    argv = [str(a) for a in argv]
    workdir = Path(cwd) if cwd else Path.cwd()
    if echo:
        typer.secho(f"$ {' '.join(argv)}", fg=typer.colors.BRIGHT_BLACK)
    try:
        completed = subprocess.run(argv, cwd=str(workdir), env=env, check=False)
    except FileNotFoundError as exc:
        typer.secho(
            f"Command not found: {argv[0]} ({exc})", fg=typer.colors.RED, err=True
        )
        raise typer.Exit(127)
    if completed.returncode != 0:
        typer.secho(
            f"Command failed (exit {completed.returncode}): {' '.join(argv)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(completed.returncode)
    return 0
