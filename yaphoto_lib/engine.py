"""Engine implementation for YaPhoto."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from . import console
from .api import Cooldown, Yandex, transfer
from .catalog import connect, remove_redundant_preview
from .console import log_event
from .errors import AuthError, ExportError, IntegrityError, RemoteError, StorageUnavailable
from .files import apply_dates, digests, file_fingerprint, store_fingerprint, validate_file
from .options import DownloadOptions, media_scope
from .util import atomic_json, auxiliary_path, progress_bar, safe_name


def download_one(
    api: Yandex, row: dict[str, Any], output: Path | str, date_policy: str
) -> tuple[str, str]:
    dst = Path(output) / row["relpath"]
    part = auxiliary_path(output, row["path"], "part")
    # A previous crash may have left a complete original before DB commit.
    # Reuse it only when a cloud hash proves its identity.
    reuse = False
    if dst.exists() and row["md5"]:
        try:
            sha = validate_file(dst, row)
            reuse = True
        except IntegrityError:
            pass
    if not reuse:
        url = api.original_url(row["path"])
        part.parent.mkdir(parents=True, exist_ok=True)
        resume = row.get("media_kind") in ("video", "file") and bool(row["md5"])
        if resume:
            identity = part.with_name(part.name + ".identity.json")
            expected = {"path": row["path"], "md5": row["md5"], "size": row["size"]}
            try:
                known = json.loads(identity.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                known = None
            if known != expected or (
                row["size"] is not None and part.exists() and (part.stat().st_size > row["size"])
            ):
                part.unlink(missing_ok=True)
            atomic_json(identity, expected)
        complete_part = False
        if (
            resume
            and row["size"] is not None
            and part.exists()
            and (part.stat().st_size == row["size"])
        ):
            try:
                validate_file(part, row)
                complete_part = True
            except IntegrityError:
                part.unlink(missing_ok=True)
        if not complete_part:
            transfer(
                api,
                url,
                part,
                resume=resume,
                expected_size=row["size"],
                allow_documents=row.get("media_kind") == "file",
            )
        log_event(f"Проверка размера и MD5: {safe_name(row['name'])}")
        sha = validate_file(part, row)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, dst)
    raw = json.loads(row["raw_json"])
    dates = apply_dates(dst, raw, date_policy)
    metadata = auxiliary_path(output, row["path"], "metadata")
    metadata.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(
        metadata,
        {
            "remote_path": row["path"],
            "original_name": row["name"],
            "metadata": raw,
            "local_sha256": sha,
            "cloud_md5_checked": bool(row["md5"]),
            "dates": dates,
        },
    )
    return (sha, "verified" if row["md5"] else "downloaded")


PREVIEW_RANK = {
    "ORIGINAL": 100,
    "XXXL": 90,
    "XXL": 80,
    "XL": 70,
    "L": 60,
    "M": 50,
    "S": 40,
    "XS": 30,
    "XXS": 20,
    "XXXS": 10,
    "DEFAULT": 0,
}


def save_preview(
    api: Yandex,
    row: dict[str, Any],
    output: Path | str,
    *,
    connection: sqlite3.Connection | None = None,
) -> str:
    """Save a decoded, unmodified preview, never mark the original downloaded."""
    from PIL import Image

    conn = connection if connection is not None else connect(Path(output) / "catalog.sqlite3")
    try:
        old = conn.execute("SELECT * FROM previews WHERE path=?", (row["path"],)).fetchone()
        old_valid = False
        if old and old["relpath"]:
            existing = Path(output) / old["relpath"]
            if existing.is_file() and digests(existing)[2] == old["sha256"]:
                with Image.open(existing) as image:
                    image.load()
                old_valid = True
                auto_upgrade = (
                    getattr(api, "auto_upgrade_previews", False)
                    and PREVIEW_RANK.get(old["variant"], -1) < PREVIEW_RANK["XXXL"]
                )
                if not getattr(api, "refresh_previews", False) and (not auto_upgrade):
                    return f"превью уже есть {old['width']}x{old['height']}"
        data = api.api("mpfs/info", {"path": row["path"], "meta": "sizes"})
        if not isinstance(data, dict):
            raise RemoteError("mpfs/info: ожидался объект метаданных")
        sizes = (data.get("meta") or {}).get("sizes") or []
        candidates = sorted(
            (
                x
                for x in sizes
                if isinstance(x, dict)
                and x.get("url")
                and (
                    not (
                        x.get("name") in ("ORIGINAL", "DEFAULT")
                        and "/disk/" in urlsplit(x["url"]).path
                    )
                )
            ),
            key=lambda x: PREVIEW_RANK.get(str(x.get("name", "")), -1),
            reverse=True,
        )
        if old_valid:
            candidates = [
                x
                for x in candidates
                if PREVIEW_RANK.get(str(x.get("name", "")), -1)
                > PREVIEW_RANK.get(old["variant"], -1)
            ]
            if not candidates:
                return f"сохранено лучшее доступное превью {old['width']}x{old['height']}"
        if not candidates:
            raise RemoteError("API не вернул вариантов превью")
        key = hashlib.sha256(row["path"].encode()).hexdigest()
        part = Path(output) / "_partial" / (key + ".preview.part")
        part.parent.mkdir(parents=True, exist_ok=True)
        last_error = "нет доступного варианта"
        for candidate in candidates:
            variant = str(candidate.get("name", "unknown"))
            url = candidate["url"]
            if url.startswith("//"):
                url = "https:" + url
            try:
                # Preview URLs require cookies; the original-file session intentionally has none.
                transfer(api, url, part, session=api.session)
                with Image.open(part) as image:
                    image.load()
                    width, height = image.size
                    fmt = image.format
                if old_valid and width * height <= old["width"] * old["height"]:
                    continue
                extension = {
                    "JPEG": ".jpg",
                    "PNG": ".png",
                    "WEBP": ".webp",
                    "GIF": ".gif",
                    "TIFF": ".tif",
                    "AVIF": ".avif",
                }.get(fmt or "")
                if not extension or min(width, height) < 1:
                    raise IntegrityError("Превью не является поддерживаемым изображением")
                sha = digests(part)[2]
                # Original filename remains visible even for HEIC -> JPEG previews.
                base = safe_name(Path(row["relpath"]).name)
                if Path(base).suffix.lower() not in (
                    extension,
                    ".jpeg" if extension == ".jpg" else extension,
                ):
                    base += extension
                relpath = str(Path("previews") / base)
                dst = Path(output) / relpath
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(part, dst)
                conn.execute(
                    "INSERT OR REPLACE INTO previews VALUES(?,?,?,?,?,?,NULL,?)",
                    (
                        row["path"],
                        relpath,
                        width,
                        height,
                        sha,
                        variant,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                conn.commit()
                log_event(f"PREVIEW {safe_name(row['name'])}: {variant} {width}x{height}")
                return f"превью {variant} {width}x{height}"
            except AuthError:
                raise
            except (requests.RequestException, ExportError, OSError, ValueError) as exc:
                conn.rollback()
                last_error = type(exc).__name__
                log_event(f"PREVIEW {safe_name(row['name'])}: {variant} {last_error}")
        if old_valid:
            return (
                f"улучшение пока недоступно; сохранено {old['variant']} "
                f"{old['width']}x{old['height']}"
            )
        raise RemoteError("Превью недоступно: " + last_error)
    except BaseException:
        conn.rollback()
        raise
    finally:
        if connection is None:
            conn.close()


def try_preview(api: Yandex, row: dict[str, Any], output: Path | str) -> str:
    if row.get("media_kind") == "video":
        return "видео остаётся в очереди оригиналов"
    if not getattr(api, "save_previews", True):
        return "превью отключено"
    try:
        connection = getattr(api, "preview_connection", None)
        if connection is not None:
            return save_preview(api, row, output, connection=connection)
        return save_preview(api, row, output)
    except AuthError:
        raise
    except Exception as exc:
        log_event(f"PREVIEW ERROR {safe_name(row['name'])}: {type(exc).__name__}")
        return f"превью не сохранено ({type(exc).__name__})"


class DownloadWorker:
    """Own network sessions and one preview connection per executor thread."""

    def __init__(
        self,
        output: Path | str,
        jar: requests.cookies.RequestsCookieJar,
        options: DownloadOptions,
        stop: threading.Event,
        bound_uid: str,
    ) -> None:
        self.output = output
        self.jar = jar
        self.options = options
        self.stop = stop
        self.bound_uid = bound_uid
        self.cooldown = Cooldown()
        self.local = threading.local()
        self.clients: list[Yandex] = []
        self.clients_lock = threading.Lock()
        self.preview_connections: list[sqlite3.Connection] = []

    def __call__(self, row: dict[str, Any]) -> tuple[str, str, str | None, str | None, int] | None:
        if self.stop.is_set():
            return None
        api: Yandex | None = getattr(self.local, "api", None)
        try:
            if api is None:
                api = Yandex(self.jar, self.cooldown)
                api.read_timeout = self.options.read_timeout
                api.save_previews = self.options.previews
                api.refresh_previews = self.options.refresh_previews
                api.auto_upgrade_previews = True
                if console.CONSOLE:
                    api.worker_id = console.CONSOLE.worker(
                        safe_name(row["name"]), "вход", path=row["path"]
                    )
                with self.clients_lock:
                    self.clients.append(api)
                if self.options.auth_context is None:
                    uid = api.login()
                else:
                    api.sk, uid = self.options.auth_context
                    api.uid = uid
                if uid != self.bound_uid:
                    raise AuthError("Аккаунт cookies не совпадает с каталогом")
                if self.options.previews:
                    connection = sqlite3.connect(
                        Path(self.output) / "catalog.sqlite3", timeout=30, check_same_thread=False
                    )
                    connection.row_factory = sqlite3.Row
                    api.preview_connection = connection
                    with self.clients_lock:
                        self.preview_connections.append(connection)
                self.local.api = api
            for attempt in range(self.options.attempts):
                if self.stop.is_set():
                    return None
                try:
                    api.label = safe_name(row["name"])
                    if console.CONSOLE:
                        api.worker_id = console.CONSOLE.worker(
                            api.label,
                            f"попытка {attempt + 1}/{self.options.attempts}",
                            path=row["path"],
                        )
                    log_event(f"FILE {api.label}: попытка {attempt + 1}/{self.options.attempts}")
                    if self.options.preview_only:
                        rescue = try_preview(api, row, self.output)
                        if console.CONSOLE:
                            console.CONSOLE.file_done(api.worker_id, api.label, rescue)
                        return (row["path"], row["status"], row["local_sha256"], row["error"], 0)
                    sha, status = download_one(api, row, self.output, self.options.date_policy)
                    if console.CONSOLE:
                        console.CONSOLE.file_done(api.worker_id, api.label, "OK оригинал")
                    return (row["path"], status, sha, None, attempt + 1)
                except AuthError:
                    self.stop.set()
                    raise
                except StorageUnavailable as exc:
                    rescue = try_preview(api, row, self.output)
                    if console.CONSOLE:
                        console.CONSOLE.file_done(
                            api.worker_id, api.label, "НЕТ оригинала; " + rescue
                        )
                    return (row["path"], "deferred", None, str(exc) + "; " + rescue, attempt + 1)
                except (requests.RequestException, RemoteError, IntegrityError) as exc:
                    log_event(
                        f"RETRY {safe_name(row['name'])}: {type(exc).__name__}, "
                        f"попытка {attempt + 1}/{self.options.attempts}"
                    )
                    if attempt + 1 == self.options.attempts:
                        rescue = try_preview(api, row, self.output)
                        raise ExportError(
                            type(exc).__name__ + ": оригинал не получен; " + rescue
                        ) from None
                    time.sleep(2**attempt + random.random())
                except OSError:
                    self.stop.set()
                    raise
            return None
        except Exception as exc:
            # Never print URLs/cookies or arbitrary network exception strings.
            if isinstance(exc, AuthError):
                self.stop.set()
            error = str(exc) if isinstance(exc, ExportError) else type(exc).__name__
            if console.CONSOLE and api is not None and hasattr(api, "worker_id"):
                console.CONSOLE.file_done(api.worker_id, safe_name(row["name"]), error)
            return (row["path"], "failed", None, error, self.options.attempts)

    def close(self) -> None:
        for connection in self.preview_connections:
            connection.close()
        for client in self.clients:
            client.close()


def select_download_rows(
    conn: sqlite3.Connection, options: DownloadOptions, scan_id: int
) -> list[dict[str, Any]]:
    scope, media_params = media_scope(options.media)
    params: tuple[str | int, ...] = media_params
    if options.current_scan:
        scope += " AND path IN (SELECT path FROM scan_items WHERE scan_id=?)"
        params += (scan_id,)
    if options.selected_paths is not None:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS download_selection(path TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM download_selection")
        conn.executemany(
            "INSERT INTO download_selection VALUES(?)", ((p,) for p in options.selected_paths)
        )
        scope += " AND path IN (SELECT path FROM download_selection)"
    if options.refresh_previews:
        scope += " AND path IN (SELECT path FROM previews WHERE variant NOT IN ('XXXL','ORIGINAL'))"
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM resources WHERE status "
            + (
                "IN ('failed','deferred')"
                if options.only_missing
                else "NOT IN ('verified','downloaded')"
            )
            + scope
            + (
                " ORDER BY path"
                if options.only_missing
                else " ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END,path"
            ),
            params,
        )
    ]
    if options.refresh_previews:
        variants = dict(conn.execute("SELECT path,variant FROM previews"))
        rows.sort(key=lambda r: (PREVIEW_RANK.get(variants.get(r["path"], ""), -1), r["path"]))
    if options.order == "reverse":
        rows.reverse()
    elif options.order == "size":
        rows.sort(key=lambda r: (r["size"] is None, r["size"] or 0, r["path"]))
    if options.limit is not None and options.limit < len(rows):
        if options.order == "spread" and options.limit > 1:
            rows = [rows[(len(rows) - 1) * i // (options.limit - 1)] for i in range(options.limit)]
        else:
            rows = rows[: options.limit]
    return rows


def download(
    conn: sqlite3.Connection,
    output: Path | str,
    jar: requests.cookies.RequestsCookieJar,
    options: DownloadOptions,
    stop: threading.Event,
) -> None:
    latest = conn.execute(
        "SELECT * FROM scans WHERE media IN (?, 'all') ORDER BY id DESC LIMIT 1", (options.media,)
    ).fetchone()
    if not latest or not latest["complete"]:
        raise ExportError("Сначала завершите scan: последний обход отсутствует или неполон")
    verify(conn, output, options.media, full=False, selected_paths=options.selected_paths)
    rows = select_download_rows(conn, options, latest["id"])
    bound_uid = conn.execute("SELECT value FROM settings WHERE key='uid'").fetchone()[0]
    if console.CONSOLE:
        console.CONSOLE.reset_workers()
    worker = DownloadWorker(output, jar, options, stop, bound_uid)
    completed = 0
    successes = previews = errors = 0
    log_event(
        f"ОБРАБОТАНО {progress_bar(0, len(rows))} 0/{len(rows)} | "
        f"Всего: {len(rows)} · Успешно: 0 · Превью: 0 · Ошибок: 0"
    )
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=options.workers) as pool:
            # Bound submitted work to worker count, even for a million-photo catalog.
            iterator = iter(rows)
            futures = set()
            for row in iterator:
                futures.add(pool.submit(worker, row))
                if len(futures) >= options.workers:
                    break
            while futures:
                done, futures = concurrent.futures.wait(
                    futures, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    result = future.result()
                    if result:
                        path, status, sha, error, used = result
                        conn.execute(
                            (
                                "UPDATE resources SET "
                                "status=?,local_sha256=?,error=?,attempts=attempts+? "
                                "WHERE path=?"
                            ),
                            (status, sha, error, used, path),
                        )
                        if status in ("verified", "downloaded") and (not options.preview_only):
                            saved = conn.execute(
                                "SELECT * FROM resources WHERE path=?", (path,)
                            ).fetchone()
                            if (Path(output) / saved["relpath"]).is_file():
                                store_fingerprint(conn, saved, output)
                                remove_redundant_preview(conn, saved, output, original_checked=True)
                        conn.commit()
                        completed += 1
                        if status in ("verified", "downloaded"):
                            successes += 1
                        elif conn.execute(
                            "SELECT 1 FROM previews WHERE path=?", (path,)
                        ).fetchone():
                            previews += 1
                        else:
                            errors += 1
                        message = f"FILE {status}: {safe_name(path.rsplit('/', 1)[-1])}" + (
                            f" ({error})" if error else ""
                        )
                        overall = (
                            f"ОБРАБОТАНО {progress_bar(completed, len(rows))} "
                            f"{completed}/{len(rows)} | Всего: {len(rows)} · "
                            f"Успешно: {successes} · Превью: {previews} · Ошибок: {errors}"
                        )
                        if console.CONSOLE:
                            log_event(message)
                            log_event(overall)
                        else:
                            print(message + "\n" + overall, flush=True)
                    if not stop.is_set():
                        next_row = next(iterator, None)
                        if next_row is not None:
                            futures.add(pool.submit(worker, next_row))
    finally:
        worker.close()
        if console.CONSOLE:
            console.CONSOLE.end_live()


def verify(
    conn: sqlite3.Connection,
    output: Path | str,
    media: str = "all",
    full: bool = True,
    selected_paths: set[str] | None = None,
) -> None:
    scope, params = media_scope(media)
    if selected_paths is not None:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS verify_selection(path TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM verify_selection")
        conn.executemany("INSERT INTO verify_selection VALUES(?)", ((p,) for p in selected_paths))
        scope += " AND path IN (SELECT path FROM verify_selection)"
    rows = conn.execute(
        "SELECT * FROM resources WHERE status IN ('verified','downloaded')" + scope, params
    ).fetchall()
    for index, row in enumerate(rows):
        if console.CONSOLE:
            console.CONSOLE.worker(safe_name(row["name"]), "проверка", path=row["path"])
            log_event(
                f"ОБРАБОТАНО {progress_bar(index, len(rows))} {index}/{len(rows)} проверка файлов"
            )
        try:
            local = Path(output) / row["relpath"]
            stamp = file_fingerprint(local)
            if row["size"] is not None and stamp[0] != row["size"]:
                raise IntegrityError("Размер локального файла изменился")
            known = (row["validated_size"], row["validated_mtime_ns"], row["validated_ctime_ns"])
            if not full and row["local_sha256"] and (known == stamp):
                remove_redundant_preview(conn, row, output, original_checked=True)
                continue
            sha = validate_file(Path(output) / row["relpath"], row, require_local_hash=True)
            conn.execute(
                "UPDATE resources SET local_sha256=?,status=?,error=NULL WHERE path=?",
                (sha, "verified" if row["md5"] else "downloaded", row["path"]),
            )
            store_fingerprint(conn, row, output)
            remove_redundant_preview(conn, row, output, original_checked=True)
        except (OSError, IntegrityError) as exc:
            conn.execute(
                "UPDATE resources SET status='failed',error=? WHERE path=?",
                (type(exc).__name__ + ": локальный файл не прошёл проверку", row["path"]),
            )
    if full:
        preview_scope, preview_params = media_scope(media, "r.media_kind")
        for preview in conn.execute(
            "SELECT p.path,p.relpath FROM previews p JOIN resources r ON p.path=r.path WHERE 1=1"
            + preview_scope,
            preview_params,
        ).fetchall():
            if not (Path(output) / preview["relpath"]).is_file():
                conn.execute("DELETE FROM previews WHERE path=?", (preview["path"],))
    conn.commit()
    if console.CONSOLE:
        log_event(
            f"ОБРАБОТАНО {progress_bar(len(rows), len(rows))} "
            f"{len(rows)}/{len(rows)} проверка завершена"
        )
