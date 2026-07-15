"""Thin wrappers that turn config resolution errors into clean CLI exits."""
from __future__ import annotations

import typer

from g3dt import config


def env_of(env: str) -> config.EnvConfig:
    try:
        return config.resolve_env(env)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


def study_of(study: str, env: str) -> config.StudyConfig:
    try:
        return config.resolve_study(study, env)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
