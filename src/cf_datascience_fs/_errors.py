"""Exceptions shared by the cfds clients."""

from __future__ import annotations


class FileChangedError(OSError):
    """The remote file was modified after it was opened or stat-ed."""


class QuotaExceededError(OSError):
    """The upload would take the account over its storage quota."""
