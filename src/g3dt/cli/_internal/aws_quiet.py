"""Scoped botocore log suppression for credential-probing commands.

botocore logs its own WARNING tracebacks (with ``exc_info``) when an SSO
token refresh fails — e.g. ``tokens.py: "SSO token refresh attempt failed"``
and ``credentials.py: "Refreshing temporary credentials failed"``. Commands
that deliberately probe possibly-stale profiles (``config contexts
--verify``, ``config discover``) already render those failures as a tidy
``?`` / "skipped" line, so the tracebacks are pure noise that drowns the
intended output.

A permanent ``logging.getLogger("botocore").setLevel(logging.ERROR)`` would
also hide real botocore warnings for the rest of the process; this context
manager scopes the silence to the probing block and restores the previous
levels afterwards, even on error.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

_NOISY_LOGGERS = ("botocore", "boto3", "urllib3")


@contextmanager
def quiet_botocore():
    """Silence botocore/boto3/urllib3 warnings for the enclosed block."""
    saved = {}
    for name in _NOISY_LOGGERS:
        logger = logging.getLogger(name)
        saved[name] = logger.level
        logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
