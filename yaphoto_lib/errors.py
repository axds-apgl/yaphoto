"""Errors implementation for YaPhoto."""

from __future__ import annotations


class ExportError(Exception):
    pass


class AuthError(ExportError):
    pass


class RemoteError(ExportError):
    pass


class IntegrityError(ExportError):
    pass


class StorageUnavailable(ExportError):
    pass
