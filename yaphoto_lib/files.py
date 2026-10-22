"""Files implementation for YaPhoto."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import sqlite3
import sys
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from .errors import ExportError, IntegrityError


def acquire_archive_lock(path: Path | str) -> BinaryIO:
    """Keep the OS lock until the returned file is closed; never read a locked byte."""
    lock = open(path, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
        try:
            if sys.platform == "win32":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN) or getattr(exc, "winerror", None) == 33:
                raise ExportError(
                    f"Архив уже занят другим процессом: {path}. "
                    "Дождитесь завершения или остановите загрузку в веб-интерфейсе/другой консоли."
                ) from None
            raise
        return lock
    except BaseException:
        lock.close()
        raise


def file_fingerprint(path: Path | str) -> tuple[int, int, int]:
    stat = Path(path).stat()
    return (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def store_fingerprint(
    conn: sqlite3.Connection, row: Mapping[str, Any] | sqlite3.Row, output: Path | str
) -> None:
    size, mtime, ctime = file_fingerprint(Path(output) / row["relpath"])
    conn.execute(
        (
            "UPDATE resources SET "
            "validated_size=?,validated_mtime_ns=?,validated_ctime_ns=? WHERE path=?"
        ),
        (size, mtime, ctime, row["path"]),
    )


def digests(path: Path | str) -> tuple[int, str, str]:
    md5, sha = hashlib.md5(), hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            md5.update(chunk)
            sha.update(chunk)
    return (size, md5.hexdigest(), sha.hexdigest())


def validate_file(
    path: Path | str, row: Mapping[str, Any] | sqlite3.Row, require_local_hash: bool = False
) -> str:
    size, md5, sha = digests(path)
    ordinary = "media_kind" in row.keys() and row["media_kind"] == "file"
    if size == 0 and (not ordinary) or (row["size"] is not None and size != row["size"]):
        raise IntegrityError("Размер файла не совпал или файл пуст")
    if row["md5"] and md5 != row["md5"]:
        raise IntegrityError("MD5 не совпал с облаком")
    if require_local_hash and row["local_sha256"] and (sha != row["local_sha256"]):
        raise IntegrityError("Локальный SHA256 изменился")
    return sha


def parse_time(value: object) -> float | None:
    try:
        if isinstance(value, (int, float)):
            if value > 100000000000:
                value /= 1000
            return float(value) if value > 0 else None
        if isinstance(value, str) and value:
            if value.isdigit():
                return parse_time(int(value))
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # Do not guess timezone of an ambiguous camera timestamp.
            return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        pass
    return None


def apply_dates(path: Path | str, raw: dict[str, Any], policy: str) -> dict[str, Any]:
    if policy == "none":
        return {"policy": "none"}
    meta = raw.get("meta") or {}
    created = parse_time(raw.get("ctime", meta.get("ctime")))
    modified = parse_time(raw.get("mtime", meta.get("mtime")))
    created = created or parse_time(raw.get("created", meta.get("created")))
    modified = modified or parse_time(raw.get("modified", meta.get("modified")))
    if modified or created:
        os.utime(path, (time.time(), float(modified or created or 0)))
    if sys.platform == "win32" and created:
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.SetFileTime.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel.SetFileTime.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        ticks = int((created + 11644473600) * 10000000)
        stamp = wintypes.FILETIME(ticks & 0xFFFFFFFF, ticks >> 32)
        handle = kernel.CreateFileW(str(Path(path).resolve()), 256, 7, None, 3, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not kernel.SetFileTime(handle, ctypes.byref(stamp), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)
    return {
        "policy": policy,
        "cloud_created": created,
        "cloud_modified": modified,
        "creation_time_applied": bool(created and sys.platform == "win32"),
    }
