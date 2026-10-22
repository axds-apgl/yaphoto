"""Cli implementation for YaPhoto."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

import requests

from . import console
from .api import Yandex, cookies_from_file
from .catalog import bind_account, connect, flatten_layout, normalize_preview_names, report, scan
from .console import Console, log_event
from .engine import download, verify
from .errors import ExportError
from .files import acquire_archive_lock
from .options import (
    DEFAULT_ATTEMPTS,
    DEFAULT_READ_TIMEOUT,
    DownloadOptions,
    media_scope,
    scan_scope,
)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    from local_config import defaults

    try:
        default_output, default_cookies = defaults(Path(__file__).resolve().parents[1], "backup")
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(
        description="Экспорт оригиналов фото и видео из безлимита Яндекс Диска"
    )
    parser.add_argument(
        "command",
        choices=[
            "check",
            "scan",
            "download",
            "verify",
            "status",
            "retry-failed",
            "flatten",
            "rescue-previews",
            "upgrade-previews",
            "normalize-previews",
        ],
    )
    parser.add_argument(
        "--media", choices=["photo", "video", "all"], default="photo", help="Тип медиа для команды"
    )
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--cookies", type=Path, default=default_cookies)
    parser.add_argument("--source", choices=["list", "photoslice"], default="list")
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Раскраска статусов: auto — только терминал, always — всегда, never — без цвета",
    )
    parser.add_argument("--workers", type=int, choices=range(1, 33), default=4)
    parser.add_argument("--limit", type=int, help="Скачать не более N фото за запуск")
    parser.add_argument("--attempts", type=int, choices=range(1, 9), default=DEFAULT_ATTEMPTS)
    parser.add_argument("--dates", choices=["cloud", "none"], default="cloud")
    parser.add_argument(
        "--order",
        choices=["spread", "path", "reverse", "size"],
        default="path",
        help="Для --limit: выборка из всего каталога / первые / последние пути",
    )
    parser.add_argument(
        "--read-timeout",
        type=int,
        default=DEFAULT_READ_TIMEOUT,
        help="Таймаут чтения оригинала, секунды",
    )
    parser.add_argument(
        "--no-previews", action="store_true", help="Не сохранять превью при отказе оригинала"
    )
    parser.add_argument(
        "--only-missing",
        action="store_true",
        help="Повторить только failed/deferred, без ещё не проверенных фото",
    )
    args = parser.parse_args(argv)
    stop_file = os.environ.get("YAPHOTO_STOP")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit должен быть положительным")
    if args.read_timeout < 1:
        parser.error("--read-timeout должен быть положительным")
    if args.command in ("rescue-previews", "upgrade-previews") and args.media != "photo":
        parser.error("Спасение и улучшение превью применяется к фото")
    lock = None
    conn = api = None
    try:
        args.output.mkdir(parents=True, exist_ok=True)
        # Exclusive process lock. OS automatically releases it after a crash.
        lock = acquire_archive_lock(args.output / ".export.lock")
        conn = connect(args.output / "catalog.sqlite3")
        owner = conn.execute("SELECT value FROM settings WHERE key='program'").fetchone()
        if owner and owner[0] != "yaphoto":
            raise ExportError(
                "Этот каталог принадлежит загрузчику обычных файлов; используйте отдельный --output"
            )
        conn.execute("INSERT OR IGNORE INTO settings VALUES('program','yaphoto')")
        conn.commit()
        console.CONSOLE = Console(args.output / "console.log", color=args.color)
        log_event(
            f"START {args.command}; workers={args.workers}; "
            f"media={args.media}; папка={args.output.resolve()}"
        )
        if args.command in (
            "normalize-previews",
            "verify",
            "download",
            "rescue-previews",
            "upgrade-previews",
        ):
            normalize_preview_names(conn, args.output)
        if args.command in (
            "flatten",
            "download",
            "scan",
            "verify",
            "rescue-previews",
            "upgrade-previews",
        ):
            flatten_layout(conn, args.output)
        if args.command in ("check", "scan", "download", "rescue-previews", "upgrade-previews"):
            jar = cookies_from_file(args.cookies)
            api = Yandex(jar)
            bind_account(conn, api.login(), args.output)
        if args.command == "check":
            print("Вход в аккаунт подтверждён. Cookies и токены не выводятся.")
            return 0
        if args.command == "scan":
            if api is None:
                raise ExportError("Не удалось создать клиент Яндекс Диска")
            scan(api, conn, args.source, args.media)
        elif args.command in ("download", "rescue-previews", "upgrade-previews"):
            stop = threading.Event()
            if stop_file:

                def watch_stop() -> None:
                    while not stop.wait(0.3):
                        if Path(stop_file).exists():
                            stop.set()

                threading.Thread(target=watch_stop, daemon=True).start()
            import signal

            old_handler = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            try:
                options = DownloadOptions(
                    workers=args.workers,
                    limit=args.limit,
                    attempts=args.attempts,
                    date_policy=args.dates,
                    order=args.order,
                    read_timeout=args.read_timeout,
                    previews=not args.no_previews,
                    only_missing=args.only_missing
                    or args.command in ("rescue-previews", "upgrade-previews"),
                    preview_only=args.command in ("rescue-previews", "upgrade-previews"),
                    media=args.media,
                    refresh_previews=args.command == "upgrade-previews",
                )
                download(conn, args.output, jar, options=options, stop=stop)
            finally:
                signal.signal(signal.SIGINT, old_handler)
            if stop.is_set():
                print("Работа остановлена; завершённые файлы сохранены. Снова запустите download.")
                report(conn, args.output, args.media)
                return 2
        elif args.command == "verify":
            verify(conn, args.output, args.media)
        elif args.command == "retry-failed":
            scope, params = media_scope(args.media)
            conn.execute(
                (
                    "UPDATE resources SET status='pending',error=NULL WHERE "
                    "status IN ('failed','deferred')"
                )
                + scope,
                params,
            )
            conn.commit()
            print("Неудачные файлы возвращены в очередь. Запустите download.")
        counts = report(conn, args.output, args.media)
        scope, params = scan_scope(args.media)
        latest = conn.execute(
            "SELECT complete FROM scans" + scope + " ORDER BY id DESC LIMIT 1", params
        ).fetchone()
        return (
            2
            if not latest
            or not latest[0]
            or any(counts.get(s, 0) for s in ("failed", "pending", "deferred"))
            else 0
        )
    except (ExportError, requests.RequestException, OSError, ValueError, sqlite3.Error) as exc:
        if isinstance(exc, ExportError):
            message = str(exc)
        elif isinstance(exc, PermissionError):
            message = (
                f"Нет доступа к файлу или папке: {exc.filename}"
                if exc.filename
                else "Отказано в доступе: проверьте права и блокировку файла другой программой"
            )
        else:
            message = type(exc).__name__
        print(f"Ошибка: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Остановлено. Каталог и готовые файлы сохранены.", file=sys.stderr)
        return 130
    finally:
        if api:
            api.close()
        if conn:
            conn.close()
        if console.CONSOLE:
            console.CONSOLE.close()
            console.CONSOLE = None
        if lock:
            lock.close()
