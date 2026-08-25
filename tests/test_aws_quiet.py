"""Tests for the scoped botocore log suppression helper.

Background: botocore logs WARNING tracebacks when an SSO token refresh
fails. Commands that deliberately probe possibly-stale profiles (`config
contexts --verify`, `config discover`) render those failures as tidy
one-liners, so the tracebacks are noise. The old fix — a permanent
`logging.getLogger("botocore").setLevel(ERROR)` — also silenced real
warnings for the rest of the process; `quiet_botocore()` scopes the
silence to the probing block. These tests pin the "scoped" part: levels
must be restored afterwards, even when the block raises.
"""
import logging

import pytest

from g3dt.cli._internal.aws_quiet import quiet_botocore


def test_quiet_botocore_silences_then_restores_levels():
    """
    Input:    botocore logger at WARNING (a common baseline).
    Expected: ERROR inside the block, WARNING again after it.
    """
    logger = logging.getLogger("botocore")
    logger.setLevel(logging.WARNING)
    try:
        with quiet_botocore():
            assert logger.level == logging.ERROR
        assert logger.level == logging.WARNING
    finally:
        logger.setLevel(logging.NOTSET)


def test_quiet_botocore_restores_levels_when_body_raises():
    """
    A probe that blows up mid-loop must not leave botocore muted for the
    rest of the process — the finally-restore is the whole point.
    """
    logger = logging.getLogger("botocore")
    logger.setLevel(logging.INFO)
    try:
        with pytest.raises(RuntimeError):
            with quiet_botocore():
                raise RuntimeError("probe failed")
        assert logger.level == logging.INFO
    finally:
        logger.setLevel(logging.NOTSET)
