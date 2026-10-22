"""Catalog implementation for YaPhoto."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .api import Yandex
from .console import log_event
from .errors import ExportError, IntegrityError, RemoteError
from .files import validate_file
from .options import media_scope, scan_scope
from .util import atomic_json, auxiliary_path, canonical_path, progress_bar, safe_name

IMAGE_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".heic",
    ".heif",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".avif",
    ".dng",
    ".raw",
}

VIDEO_EXT = {
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".avi",
    ".3gp",
    ".3g2",
    ".webm",
    ".mts",
    ".m2ts",
    ".mpg",
    ".mpeg",
}


def is_photo(item: dict[str, Any]) -> bool:
    meta = item.get("meta") or {}
    kind = str(meta.get("mediatype") or item.get("media_type") or "").lower()
    mime = str(meta.get("mimetype") or item.get("mimetype") or item.get("mime_type") or "").lower()
    if kind == "video" or mime.startswith("video/"):
        return False
    return (
        kind == "image"
        or mime.startswith("image/")
        or Path(item.get("name") or "").suffix.lower() in IMAGE_EXT
    )


def media_kind(item: dict[str, Any]) -> str | None:
    meta = item.get("meta") or {}
    kind = str(meta.get("mediatype") or item.get("media_type") or "").lower()
    mime = str(meta.get("mimetype") or item.get("mimetype") or item.get("mime_type") or "").lower()
    if kind == "video" or mime.startswith("video/"):
        return "video"
    if is_photo(item):
        return "photo"
    return "video" if Path(item.get("name") or "").suffix.lower() in VIDEO_EXT else None


def local_relpath(path: str, name: str, kind: str = "photo") -> str:
    return str(Path("videos" if kind == "video" else "photos") / safe_name(name))


def allocate_relpath(
    conn: sqlite3.Connection,
    path: str,
    name: str,
    output: Path | str | None = None,
    reserved: set[str] | None = None,
    kind: str = "photo",
) -> str:
    """Preserve names; suffix only real case-insensitive filesystem/catalog collisions."""
    base = Path(local_relpath(path, name, kind))
    occupied = (
        reserved
        if reserved is not None
        else {
            r[0].replace("\\", "/").casefold()
            for r in conn.execute("SELECT relpath FROM resources WHERE path<>?", (path,))
        }
    )
    candidate = base
    suffix = hashlib.sha256(path.encode()).hexdigest()[:12]
    n = 0
    while candidate.as_posix().casefold() in occupied or (
        output is not None and (Path(output) / candidate).exists()
    ):
        n += 1
        candidate = base.with_name(
            base.stem + "__" + suffix + (f"_{n}" if n > 1 else "") + base.suffix
        )
    return str(candidate)


def connect(db_path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, source TEXT, started TEXT,
            finished TEXT, complete INTEGER DEFAULT 0, count INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS scan_items (scan_id INTEGER, path TEXT,
            PRIMARY KEY(scan_id,path));
        CREATE TABLE IF NOT EXISTS layout_moves (path TEXT PRIMARY KEY, old_rel TEXT,
            new_rel TEXT, done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS previews (path TEXT PRIMARY KEY, relpath TEXT, width INTEGER,
            height INTEGER, sha256 TEXT, variant TEXT, error TEXT, updated TEXT);
        CREATE TABLE IF NOT EXISTS preview_moves (path TEXT PRIMARY KEY, old_rel TEXT,
            new_rel TEXT, done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS resources (
            path TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER, md5 TEXT,
            raw_json TEXT NOT NULL, relpath TEXT NOT NULL UNIQUE,
            scan_id INTEGER, status TEXT NOT NULL DEFAULT 'pending',
            local_sha256 TEXT, error TEXT, attempts INTEGER DEFAULT 0);
    """
    )
    for table, column, definition in (
        ("resources", "media_kind", "TEXT NOT NULL DEFAULT 'photo'"),
        ("scans", "media", "TEXT NOT NULL DEFAULT 'photo'"),
        ("resources", "validated_size", "INTEGER"),
        ("resources", "validated_mtime_ns", "INTEGER"),
        ("resources", "validated_ctime_ns", "INTEGER"),
    ):
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    conn.commit()
    return conn


def normalize_preview_names(conn: sqlite3.Connection, output: Path | str) -> int:
    """Remove legacy ## prefixes, journal moves, preserve collisions and catalog links."""
    root = Path(output).resolve()
    folder = root / "previews"
    plans = conn.execute("SELECT * FROM preview_moves WHERE done=0").fetchall()
    if not plans:
        indexed = {
            str((root / r["relpath"]).resolve()): r["path"]
            for r in conn.execute("SELECT path,relpath FROM previews")
            if r["relpath"]
        }
        reserved = set()
        for old in sorted(folder.glob("##*")):
            if not old.is_file() or old.is_symlink():
                continue
            if not old.resolve().is_relative_to(folder.resolve()):
                continue
            name = old.name[2:] or "preview"
            new = folder / name
            key = indexed.get(str(old.resolve()), "orphan:" + str(old.relative_to(root)))
            suffix = hashlib.sha256(key.encode()).hexdigest()[:12]
            n = 0
            while new.exists() or str(new).casefold() in reserved:
                n += 1
                new = folder / (
                    Path(name).stem + "__" + suffix + (f"_{n}" if n > 1 else "") + Path(name).suffix
                )
            reserved.add(str(new).casefold())
            conn.execute(
                "INSERT OR REPLACE INTO preview_moves VALUES(?,?,?,0)",
                (key, str(old.relative_to(root)), str(new.relative_to(root))),
            )
        conn.commit()
        plans = conn.execute("SELECT * FROM preview_moves WHERE done=0").fetchall()
    renamed = 0
    for plan in plans:
        old, new = root / plan["old_rel"], root / plan["new_rel"]
        if not old.resolve().is_relative_to(folder.resolve()) or not new.resolve().is_relative_to(
            folder.resolve()
        ):
            raise ExportError("Переименование превью выходит за пределы previews")
        if old.exists():
            if new.exists():
                raise ExportError("Переименование превью остановлено: целевой файл существует")
            old.rename(new)
            renamed += 1
        conn.execute("UPDATE previews SET relpath=? WHERE path=?", (plan["new_rel"], plan["path"]))
        conn.execute("UPDATE preview_moves SET done=1 WHERE path=?", (plan["path"],))
        conn.commit()
    if renamed:
        log_event(f"Плоская структура: убран префикс ## у {renamed} превью")
    return renamed


def remove_redundant_preview(
    conn: sqlite3.Connection,
    row: Mapping[str, Any] | sqlite3.Row,
    output: Path | str,
    original_checked: bool = False,
) -> bool:
    preview = conn.execute("SELECT relpath FROM previews WHERE path=?", (row["path"],)).fetchone()
    if not preview or not preview["relpath"]:
        return False
    root = Path(output).resolve()
    original = root / row["relpath"]
    target = root / preview["relpath"]
    if (
        not original.resolve().is_relative_to(root)
        or not target.resolve().is_relative_to(root / "previews")
        or target.is_symlink()
    ):
        log_event("PREVIEW CLEANUP: небезопасный путь, превью сохранено")
        return False
    try:
        if not original_checked:
            validate_file(original, row, require_local_hash=True)
        target.unlink(missing_ok=True)
    except (OSError, IntegrityError):
        log_event(f"PREVIEW CLEANUP: удаление отложено для {safe_name(row['name'])}")
        return False
    conn.execute("DELETE FROM previews WHERE path=?", (row["path"],))
    conn.commit()
    log_event(f"PREVIEW CLEANUP: удалено после проверки оригинала {safe_name(row['name'])}")
    return True


def flatten_layout(conn: sqlite3.Connection, output: Path | str) -> tuple[int, int]:
    """Journal filesystem moves before updating paths; resume safely after interruption."""
    root = Path(output).resolve()
    plans = conn.execute("SELECT * FROM layout_moves WHERE done=0").fetchall()
    if not plans:
        rows = conn.execute("SELECT * FROM resources ORDER BY path").fetchall()
        reserved = {
            Path(r["relpath"]).as_posix().casefold()
            for r in rows
            if len(Path(r["relpath"]).parts) == 2
        }
        for row in rows:
            if len(Path(row["relpath"]).parts) == 2:
                continue
            target = allocate_relpath(
                conn, row["path"], row["name"], root, reserved, row["media_kind"]
            )
            reserved.add(Path(target).as_posix().casefold())
            conn.execute(
                "INSERT OR REPLACE INTO layout_moves VALUES(?,?,?,0)",
                (row["path"], row["relpath"], target),
            )
        conn.commit()
        plans = conn.execute("SELECT * FROM layout_moves WHERE done=0").fetchall()
    moved = 0
    for index, plan in enumerate(plans, 1):
        src, dst = root / plan["old_rel"], root / plan["new_rel"]
        pairs = [
            (src, dst),
            (
                src.with_name(src.name + ".metadata.json"),
                auxiliary_path(root, plan["path"], "metadata"),
            ),
            (src.with_name(src.name + ".part"), auxiliary_path(root, plan["path"], "part")),
        ]
        for old, new in pairs:
            if not old.resolve().is_relative_to(root) or not new.resolve().is_relative_to(root):
                raise ExportError("Путь переноса выходит за пределы папки архива")
            if old.exists():
                if new.exists():
                    raise ExportError(
                        "Перенос остановлен: целевой файл уже существует; данные сохранены"
                    )
                new.parent.mkdir(parents=True, exist_ok=True)
                old.rename(new)
                moved += 1
        conn.execute("UPDATE resources SET relpath=? WHERE path=?", (plan["new_rel"], plan["path"]))
        conn.execute("UPDATE layout_moves SET done=1 WHERE path=?", (plan["path"],))
        conn.commit()
        if index % 500 == 0:
            log_event(f"ПЕРЕНОС {progress_bar(index, len(plans))} {index}/{len(plans)}")
    photos = root / "photos"
    removed = 0
    if photos.exists():
        directories = sorted(
            (p for p in photos.rglob("*") if p.is_dir() and (not p.is_symlink())),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        for directory in directories:
            if directory.resolve().is_relative_to(photos.resolve()):
                try:
                    directory.rmdir()
                    # Empty directories only. Never delete contained files.
                    removed += 1
                except OSError:
                    pass
    if plans or removed:
        log_event(
            f"Плоская структура: {len(plans)} записей, перенесено "
            f"{moved} файлов, убрано {removed} пустых папок"
        )
    return (moved, removed)


def bind_account(conn: sqlite3.Connection, uid: str, output: Path | str) -> None:
    # Protect against silently reusing another account's catalog or another destination.
    for key, value in (("uid", uid), ("output", str(Path(output).resolve()))):
        old = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if old and old[0] != value:
            raise ExportError(f"Каталог относится к другому {key}; используйте отдельную папку")
        conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()


def save_resource(
    conn: sqlite3.Connection, scan_id: int, item: dict[str, Any], media: str = "photo"
) -> bool:
    path = canonical_path(str(item.get("path") or item.get("id") or ""))
    kind = media_kind(item)
    if not path.startswith("/photounlim/") or not kind or (media != "all" and media != kind):
        return False
    meta = item.get("meta") or {}
    name = item.get("name") or path.rsplit("/", 1)[-1]
    size = meta.get("size", item.get("size"))
    size = int(size) if size is not None else None
    if size is not None and size < 0:
        raise RemoteError("API вернул отрицательный размер")
    md5 = meta.get("md5") or item.get("md5")
    md5 = md5.lower() if isinstance(md5, str) and re.fullmatch("[a-fA-F0-9]{32}", md5) else None
    old = conn.execute("SELECT * FROM resources WHERE path=?", (path,)).fetchone()
    if old:
        changed = (
            old["size"] is not None
            and size is not None
            and (old["size"] != size)
            or (old["md5"] and md5 and (old["md5"] != md5))
            or old["name"] != name
        )
        status = "pending" if changed else old["status"]
        relpath = (
            old["relpath"]
            if old["name"] == name and old["media_kind"] == kind
            else allocate_relpath(conn, path, name, kind=kind)
        )
        conn.execute(
            """UPDATE resources SET name=?,size=COALESCE(?,size),md5=COALESCE(?,md5),
            raw_json=?,scan_id=?,status=?,relpath=?,media_kind=? WHERE path=?""",
            (
                name,
                size,
                md5,
                json.dumps(item, ensure_ascii=False),
                scan_id,
                status,
                relpath,
                kind,
                path,
            ),
        )
    else:
        conn.execute(
            """INSERT INTO resources(path,name,size,md5,raw_json,relpath,scan_id,media_kind)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                path,
                name,
                size,
                md5,
                json.dumps(item, ensure_ascii=False),
                allocate_relpath(conn, path, name, kind=kind),
                scan_id,
                kind,
            ),
        )
    conn.execute("INSERT OR IGNORE INTO scan_items VALUES(?,?)", (scan_id, path))
    return True


def collect_list(api: Yandex, emit: Callable[[dict[str, Any]], None], page_size: int = 500) -> None:
    directories = ["/photounlim"]
    visited = set()
    while directories:
        root = directories.pop()
        if root in visited:
            continue
        visited.add(root)
        offset = 0
        page_signatures = set()
        seen_paths = set()
        while True:
            page = api.api(
                "mpfs/list",
                {
                    "path": root,
                    "amount": page_size,
                    "offset": offset,
                    "meta": "size,md5,mimetype,mediatype,exif,etime,ctime,mtime",
                },
            )
            if not isinstance(page, list) or any(not isinstance(x, dict) for x in page):
                raise RemoteError("mpfs/list: ожидался список ресурсов")
            children = [
                x for x in page if canonical_path(str(x.get("path") or "")).rstrip("/") != root
            ]
            if children:
                signature = tuple(str(x.get("path")) for x in children)
                if signature in page_signatures:
                    raise RemoteError(
                        "mpfs/list повторяет страницу; полнота каталога не подтверждена"
                    )
                page_signatures.add(signature)
            for item in children:
                path = canonical_path(str(item.get("path") or ""))
                if not path.startswith(root + "/"):
                    raise RemoteError("mpfs/list: ресурс вне запрошенной папки")
                if path in seen_paths:
                    raise RemoteError(
                        "mpfs/list: ресурс повторяется между страницами; повторите scan"
                    )
                seen_paths.add(path)
                if item.get("type") == "dir":
                    directories.append(path)
                elif item.get("type") == "file":
                    emit(item)
                else:
                    raise RemoteError("mpfs/list: неизвестный тип ресурса")
            if len(children) < page_size:
                break
            offset += len(children)


def collect_photoslice(
    api: Yandex, emit: Callable[[dict[str, Any]], None], page_size: int = 500
) -> None:
    snapshot = api.api("intapi/photo-init-snapshot")
    if not isinstance(snapshot, dict):
        raise RemoteError("Photoslice: ожидался объект снимка")
    psid = snapshot.get("photoslice_id")
    if not psid:
        raise RemoteError("Photoslice: нет photoslice_id")
    offset = 0
    known = set()
    while True:
        data = api.api(
            "intapi/photo-get-snapshot",
            {"id": psid, "locale": "ru", "amount": page_size, "offset": offset},
        )
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise RemoteError("Photoslice: нет списка кластеров")
        clusters = data["items"]
        for cluster in clusters:
            cid, expected = cluster.get("id"), int(cluster.get("size", 0))
            if not cid or cid in known or expected < 0:
                raise RemoteError("Photoslice: повторный или некорректный кластер")
            known.add(cid)
            # Request each cluster in bounded ranges, never silently accept a partial cluster.
            ids = set()
            for start in range(0, expected, page_size):
                end = min(start + page_size, expected) - 1
                resources = api.api(
                    "intapi/photo-get-clusters-with-resources",
                    {
                        "photosliceId": psid,
                        "clusters": {cid: {"range": [start, end]}},
                        "hideScreenshots": False,
                    },
                )
                if not isinstance(resources, dict):
                    raise RemoteError("Photoslice: ожидался объект ресурсов")
                fetched = (resources.get("clusters") or {}).get("fetched")
                if not isinstance(fetched, list):
                    raise RemoteError("Photoslice: нет списка полученных кластеров")
                items = [x for c in fetched if c.get("id") == cid for x in c.get("items", [])]
                if len(items) != end - start + 1:
                    raise RemoteError("Photoslice: неполный диапазон кластера")
                for item in items:
                    fid = item.get("id") or item.get("path")
                    if not fid or fid in ids:
                        raise RemoteError("Photoslice: отсутствует или повторяется идентификатор")
                    ids.add(fid)
                    emit(item)
            if len(ids) != expected:
                raise RemoteError("Photoslice: число ресурсов не совпало с размером кластера")
        if len(clusters) < page_size:
            break
        offset += len(clusters)


def scan(api: Yandex, conn: sqlite3.Connection, source: str, media: str = "photo") -> None:
    cursor = conn.execute(
        "INSERT INTO scans(source,started,media) VALUES(?,?,?)",
        (source, datetime.now(timezone.utc).isoformat(), media),
    )
    scan_id = cursor.lastrowid
    if scan_id is None:
        raise RemoteError("Не удалось создать запись сканирования")
    conn.commit()
    count = 0

    def emit(item: dict[str, Any]) -> None:
        nonlocal count
        if save_resource(conn, scan_id, item, media):
            count += 1
            if count % 100 == 0:
                conn.commit()
                print(f"Каталог {media}: обработано {count} файлов", flush=True)
                log_event(f"КАТАЛОГ {media}: найдено {count} файлов; обход продолжается")

    try:
        (collect_list if source == "list" else collect_photoslice)(api, emit)
        unique = conn.execute(
            "SELECT COUNT(*) FROM resources WHERE scan_id=?", (scan_id,)
        ).fetchone()[0]
        if not unique:
            raise RemoteError(
                "Найдено 0 файлов выбранного типа. Проверьте аккаунт или другой --source"
            )
        conn.execute(
            "UPDATE scans SET complete=1,count=?,finished=? WHERE id=?",
            (unique, datetime.now(timezone.utc).isoformat(), scan_id),
        )
        conn.commit()
        print(f"Обход {media} завершён: {unique} уникальных файлов. Полнота требует сверки.")
    except BaseException:
        conn.commit()
        # Keep discovered rows, but leave scan explicitly incomplete.
        raise


def report(conn: sqlite3.Connection, output: Path | str, media: str = "all") -> dict[str, int]:
    scope, params = media_scope(media)
    counts = dict(
        conn.execute(
            "SELECT status,COUNT(*) FROM resources WHERE 1=1" + scope + " GROUP BY status", params
        ).fetchall()
    )
    latest_scope, latest_params = scan_scope(media)
    latest = conn.execute(
        "SELECT * FROM scans" + latest_scope + " ORDER BY id DESC LIMIT 1", latest_params
    ).fetchone()
    data: dict[str, Any] = {
        "media": media,
        "counts": counts,
        "latest_scan": dict(latest) if latest else None,
        "note": (
            "verified = совпал MD5 облака; downloaded = облачный "
            "MD5 отсутствует; полнота облака требует сверки"
        ),
    }
    previous = (
        conn.execute(
            (
                "SELECT id,source FROM scans WHERE complete=1 AND id<? "
                "AND media=? ORDER BY id DESC LIMIT 1"
            ),
            (latest["id"], latest["media"]),
        ).fetchone()
        if latest and latest["complete"]
        else None
    )
    if previous:
        missing = conn.execute(
            """SELECT COUNT(*) FROM scan_items a WHERE a.scan_id=? AND NOT EXISTS
            (SELECT 1 FROM scan_items b WHERE b.scan_id=? AND b.path=a.path)""",
            (previous["id"], latest["id"]),
        ).fetchone()[0]
        added = conn.execute(
            """SELECT COUNT(*) FROM scan_items a WHERE a.scan_id=? AND NOT EXISTS
            (SELECT 1 FROM scan_items b WHERE b.scan_id=? AND b.path=a.path)""",
            (latest["id"], previous["id"]),
        ).fetchone()[0]
        data["scan_comparison"] = {
            "previous_scan_id": previous["id"],
            "previous_source": previous["source"],
            "missing_since_previous": missing,
            "new_since_previous": added,
        }
    errors = [
        dict(r)
        for r in conn.execute(
            (
                "SELECT path,name,status,error,media_kind FROM "
                "resources WHERE status IN ('failed','deferred')"
            )
        )
    ]
    preview_rows = [
        dict(r)
        for r in conn.execute(
            """SELECT p.*,r.name,r.media_kind,r.status AS original_status,
        CASE WHEN r.status IN ('verified','downloaded') THEN 0 ELSE 1 END AS original_missing
        FROM previews p JOIN resources r ON p.path=r.path ORDER BY r.path"""
        )
    ]
    if media != "all":
        errors = [r for r in errors if r["media_kind"] == media]
        preview_rows = [r for r in preview_rows if r["media_kind"] == media]
    preview_paths = {p["path"] for p in preview_rows if p["original_missing"]}
    missing_originals = [
        dict(row, has_preview=row["path"] in preview_paths)
        for row in conn.execute(
            "SELECT path,name,status,error,media_kind FROM resources "
            "WHERE status NOT IN ('verified','downloaded')" + scope,
            params,
        )
    ]
    reports = {
        "previews": preview_rows,
        "missing_originals": missing_originals,
        "errors": [r for r in errors if r["path"] not in preview_paths],
    }
    for name, rows in reports.items():
        atomic_json(Path(output) / f"{name}_{media}.json", rows)
        # Compatibility aliases describe the most recent report only.
        atomic_json(Path(output) / f"{name}.json", rows)
    data["integrity_reminder"] = (
        "Быстрая проверка доверяет размеру, mtime и ctime. "
        "Периодически запускайте python yaphoto.py verify для полной проверки содержимого."
    )
    print(data["integrity_reminder"])
    data["previews_saved"] = len(preview_rows)
    data["previews_without_original"] = sum(p["original_missing"] for p in preview_rows)
    original_count = counts.get("verified", 0) + counts.get("downloaded", 0)
    error_count = sum(r["path"] not in preview_paths for r in errors)
    total_count = sum(counts.values())
    data["summary"] = {
        "total": total_count,
        "originals": original_count,
        "previews": len(preview_paths),
        "errors": error_count,
        "unprocessed": total_count - original_count - len(preview_paths) - error_count,
    }
    atomic_json(Path(output) / "report.json", data)
    atomic_json(Path(output) / f"report_{media}.json", data)
    print(
        f"ИТОГ {media} | Всего: {total_count} | Оригиналы: "
        f"{original_count} | Превью: {len(preview_paths)} | "
        f"Ошибки (без копии): {error_count}"
    )
    print(
        f"Ещё не обработано: {data['summary']['unprocessed']}. "
        f"Ожидают оригинала, превью сохранено: "
        f"{len(preview_paths)}."
    )
    return counts
