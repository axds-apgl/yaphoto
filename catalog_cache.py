"""Immutable catalog snapshots with coalesced refresh outside HTTP request threads."""

import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

Record = Mapping[str, Any]


class CatalogCache:
    def __init__(
        self, path: Path, loader: Callable[[sqlite3.Connection], list[dict[str, Any]]]
    ) -> None:
        self.path = path
        self.loader = loader
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.connection: sqlite3.Connection | None = None
        self.identity: tuple[int, int] | None = None
        self.version: int | None = None
        self.records: tuple[Record, ...] = ()
        self.by_id: dict[str, Record] = {}
        self.counts = dict(total=0, photo=0, video=0, original=0, preview=0, error=0, pending=0)
        self.views: dict[
            tuple[str, str, bool], tuple[tuple[Record, ...], tuple[str, ...], dict[str, int]]
        ] = {}
        self.ready = False
        self.refreshing = False
        self.error: str | None = None
        self.next_check = 0.0
        self.refresh_interval = 15.0
        self.refresh_requested = False
        self.closed = False
        self.thread: threading.Thread | None = None
        self.check = 0
        self.checked = 0
        self.revision = 0

    def _refresh(self) -> None:
        # Only the refresh worker (or an explicit synchronous caller) touches SQLite.
        with self.refresh_lock:
            if self.closed:
                return
            if not self.path.exists():
                with self.lock:
                    self.ready = True
                return
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self.connection is None or identity != self.identity:
                if self.connection is not None:
                    self.connection.close()
                self.connection = sqlite3.connect(
                    self.path.as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=0.5
                )
                self.connection.row_factory = sqlite3.Row
                self.identity = identity
                self.version = None
            version = int(self.connection.execute("PRAGMA data_version").fetchone()[0])
            if version == self.version:
                with self.lock:
                    self.error = None
                return
            # Build separately: the last complete snapshot stays readable throughout.
            records = tuple(MappingProxyType(row) for row in self.loader(self.connection))
            by_id: dict[str, Record] = {row["id"]: row for row in records}
            counts = {
                key: sum(row["state"] == key for row in records)
                for key in ("original", "preview", "error", "pending")
            }
            counts.update(
                total=len(records),
                photo=sum(row["media_kind"] == "photo" for row in records),
                video=sum(row["media_kind"] == "video" for row in records),
            )
            dated = sorted(
                (row for row in records if row["date"] != "Без даты"),
                key=lambda row: (row["date"], row["name"], row["id"]),
            )
            undated = sorted(
                (row for row in records if row["date"] == "Без даты"),
                key=lambda row: (row["name"], row["id"]),
            )
            views = {}
            for ascending in (True, False):
                buckets: dict[tuple[str, str], list[Record]] = {
                    (media, state): []
                    for media in ("all", "photo", "video")
                    for state in ("all", "original", "preview", "error", "pending")
                }
                ordered = (dated if ascending else list(reversed(dated))) + undated
                for row in ordered:
                    for media in ("all", row["media_kind"]):
                        for state in ("all", row["state"]):
                            buckets[media, state].append(row)
                for (media, state), bucket in buckets.items():
                    offsets: dict[str, int] = {}
                    for index, row in enumerate(bucket):
                        if row["date"] != "Без даты":
                            offsets.setdefault(row["date"][:7], index)
                    views[media, state, ascending] = tuple(bucket), tuple(offsets), offsets
            with self.lock:
                self.views = views
                self.records, self.by_id, self.counts = records, by_id, counts
                self.version, self.ready, self.error = version, True, None
                self.revision += 1

    def request_refresh(self) -> None:
        """Queue a fresh snapshot even if a refresh is already in flight."""
        with self.lock:
            self.refresh_requested = True
            self.next_check = 0.0

    def _background_refresh(self) -> None:
        try:
            while True:
                with self.lock:
                    self.refresh_requested = False
                self._refresh()
                with self.lock:
                    if self.refresh_requested and not self.closed:
                        continue
                    self.refreshing = False
                    self.checked = self.check
                    self.next_check = time.monotonic() + self.refresh_interval
                    return
        except (OSError, ValueError, sqlite3.Error) as exc:
            with self.lock:
                self.error = "Не удалось обновить каталог: " + type(exc).__name__
                self.refreshing = False
                self.checked = self.check
                self.next_check = (
                    0.0 if self.refresh_requested else time.monotonic() + self.refresh_interval
                )

    def _ensure(self, wait: bool) -> None:
        if wait:
            self._refresh()
            return
        with self.lock:
            if self.closed or self.refreshing or time.monotonic() < self.next_check:
                return
            self.refreshing = True
            self.check += 1
            self.thread = threading.Thread(target=self._background_refresh, daemon=True)
            self.thread.start()

    def rows(self, wait: bool = True) -> tuple[Record, ...]:
        self._ensure(wait)
        with self.lock:
            return self.records

    def get(self, identifier: str, wait: bool = False) -> Record | None:
        self._ensure(wait)
        with self.lock:
            return self.by_id.get(identifier)

    def summary(self, wait: bool = True) -> dict[str, int]:
        self._ensure(wait)
        with self.lock:
            return dict(self.counts)

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "loading": not self.ready,
                "refreshing": self.refreshing,
                "error": self.error,
                "check": self.check,
                "checked": self.checked,
                "revision": self.revision,
            }

    def page(self, query: Mapping[str, str]) -> dict[str, Any]:
        """Slice a preordered snapshot; normal pagination never traverses the catalog."""
        if query.get("refresh") == "1":
            self.request_refresh()
        self._ensure(False)
        media = query.get("media", "all")
        state = query.get("state", "all")
        if media not in ("photo", "video"):
            media = "all"
        if state not in ("original", "preview", "error", "pending"):
            state = "all"
        ascending = query.get("order") == "asc"
        with self.lock:
            rows, months, offsets = self.views.get((media, state, ascending), ((), (), {}))
            if query.get("ids"):
                # Polling only resolves requested IDs, never scans the full snapshot.
                rows = tuple(
                    sorted(
                        (
                            row
                            for identifier in dict.fromkeys(query["ids"].split(","))
                            if (row := self.by_id.get(identifier)) is not None
                            and (media == "all" or row["media_kind"] == media)
                            and (state == "all" or row["state"] == state)
                        ),
                        key=lambda row: (row["date"], row["name"], row["id"]),
                        reverse=not ascending,
                    )
                )
                rows = tuple(row for row in rows if row["date"] != "Без даты") + tuple(
                    row for row in rows if row["date"] == "Без даты"
                )
                offsets = {}
                for index, row in enumerate(rows):
                    if row["date"] != "Без даты":
                        offsets.setdefault(row["date"][:7], index)
                months = tuple(offsets)
            summary, status = dict(self.counts), self.status()
        offset = max(0, int(query.get("offset", str(offsets.get(query.get("month", ""), 0)))))
        limit = min(100, max(1, int(query.get("limit", "60"))))
        items = [
            {
                key: value
                for key, value in row.items()
                if key not in ("relpath", "preview_rel", "error")
            }
            for row in rows[offset : offset + limit]
        ]
        return dict(
            items=items,
            months=list(months),
            summary=summary,
            offset=offset,
            next=offset + len(items),
            total=len(rows),
            **status,
        )

    def close(self) -> None:
        with self.lock:
            self.closed = True
        with self.refresh_lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
