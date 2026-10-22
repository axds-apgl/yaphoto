"""Local browser UI over the unchanged YaPhoto command line workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from browser_auth import COOKIE_NAME, authorized, same_token
from catalog_cache import CatalogCache
from yaphoto import DEFAULT_ATTEMPTS, ExportError

ROOT = Path(__file__).resolve().parent


class App:
    def __init__(self, output, cookies):
        self.output = Path(output).resolve()
        self.cookies = Path(cookies).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.token = secrets.token_urlsafe(32)
        self.access_token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.process = None
        self.job = {"running": False, "command": None, "code": None}
        self.logs = deque(maxlen=100)
        self.progress = self.output / ".web-progress.json"
        self.stop_file = self.output / ".web-stop"
        self.thumb_lock = threading.Lock()
        self.thumbs = ThreadPoolExecutor(max_workers=2)
        self.pending = set()
        self.failed_thumbs = set()
        self.resource_ids = {}
        self.catalog = CatalogCache(self.output / "catalog.sqlite3", self.read_rows)

    def rows(self, wait=True):
        return self.catalog.rows(wait=wait)

    def row(self, identifier):
        return self.catalog.get(identifier)

    def close_catalog(self):
        self.catalog.close()

    def read_rows(self, conn):
        rows = conn.execute("""SELECT r.path,r.name,r.size,r.relpath,r.status,r.error,r.media_kind,
            p.relpath AS preview_rel,p.width,p.height,p.variant
            FROM resources r LEFT JOIN previews p ON p.path=r.path""").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            path = d.pop("path")
            if path not in self.resource_ids:
                self.resource_ids[path] = hashlib.sha256(path.encode()).hexdigest()
            d["id"] = self.resource_ids[path]
            match = re.search(r"((?:19|20)\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", d["name"])
            d["date"] = "-".join(match.groups()) if match else "Без даты"
            try:
                datetime.strptime(d["date"], "%Y-%m-%d")
            except ValueError:
                d["date"] = "Без даты"
            # Display the last verified catalog state; verify checks missing files.
            # Filesystem checks belong to the requested media, never to a full gallery refresh.
            original = d["status"] in ("verified", "downloaded")
            preview = bool(d["preview_rel"])
            d["state"] = (
                "original"
                if original
                else "preview"
                if preview
                else "error"
                if d["status"] in ("failed", "deferred")
                else "pending"
            )
            result.append(d)
        return result

    def safe(self, rel):
        path = (self.output / rel).resolve()
        if not path.is_relative_to(self.output):
            raise ValueError("Путь вне архива")
        return path

    def state(self):
        with self.lock:
            job = dict(self.job)
            job["log"] = list(self.logs)
        try:
            job["progress"] = (
                json.loads(self.progress.read_text(encoding="utf-8")) if job["running"] else {}
            )
        except (OSError, ValueError):
            job["progress"] = {}
        return job

    def start(self, data):
        command = data.get("command", "download")
        if command not in ("check", "scan", "verify", "download", "upgrade-previews"):
            raise ValueError("Неизвестная команда")
        media = data.get("media", "photo")
        if media not in ("photo", "video", "all"):
            raise ValueError("Выберите фото или видео")
        if command == "upgrade-previews" and media != "photo":
            raise ValueError("Улучшение превью доступно только для фото; выберите только Фото")
        workers = int(data.get("workers", 4))
        attempts = int(data.get("attempts", DEFAULT_ATTEMPTS))
        timeout = int(data.get("timeout", 30))
        limit = int(data.get("limit") or 0)
        if not 1 <= workers <= 32 or not 1 <= attempts <= 8 or not 1 <= timeout <= 600 or limit < 0:
            raise ValueError("Проверьте настройки")
        order = data.get("order", "path")
        if order not in ("path", "reverse", "spread", "size"):
            raise ValueError("Неизвестный порядок")
        args = [
            sys.executable,
            "-u",
            str(ROOT / "yaphoto.py"),
            command,
            "--output",
            str(self.output),
            "--cookies",
            str(self.cookies),
            "--media",
            media,
            "--workers",
            str(workers),
            "--attempts",
            str(attempts),
            "--read-timeout",
            str(timeout),
            "--order",
            order,
            "--dates",
            "cloud" if data.get("dates", True) else "none",
        ]
        if limit:
            args += ["--limit", str(limit)]
        if not data.get("previews", True):
            args += ["--no-previews"]
        if data.get("only_missing"):
            args += ["--only-missing"]
        with self.lock:
            if self.job["running"]:
                raise ValueError("Процесс уже выполняется")
            self.stop_file.unlink(missing_ok=True)
            self.progress.unlink(missing_ok=True)
            env = dict(
                os.environ,
                PYTHONUTF8="1",
                YAPHOTO_PROGRESS=str(self.progress),
                YAPHOTO_STOP=str(self.stop_file),
            )
            self.process = subprocess.Popen(
                args,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.logs.clear()
            self.job = {"running": True, "command": command, "code": None, "stopping": False}
            threading.Thread(target=self.collect, args=(self.process,), daemon=True).start()

    def collect(self, process):
        for line in process.stdout:
            # Defense in depth: never return signed storage URLs to the browser.
            line = re.sub(r"https?://\S+", "[ссылка]", line.strip())
            if line:
                with self.lock:
                    self.logs.append(line)
        code = process.wait()
        with self.lock:
            self.job.update(running=False, code=code, stopping=False)
        process.stdout.close()

    def stop(self):
        with self.lock:
            if self.job["running"]:
                if self.job["command"] not in ("download", "upgrade-previews"):
                    raise ValueError(
                        (
                            "Эта команда завершится самостоятельно; безопасная "
                            "остановка доступна для скачивания"
                        )
                    )
                self.stop_file.touch()
                self.job["stopping"] = True

    def thumbnail(self, row):
        source = self.safe(row["relpath"] if row["state"] == "original" else row["preview_rel"])
        st = source.stat()
        key = hashlib.sha256(f"{source}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()
        dest = self.output / "_thumbnails" / (key + ".jpg")
        if dest.exists():
            return dest
        with self.thumb_lock:
            if key in self.failed_thumbs:
                return False
            if key not in self.pending:
                self.pending.add(key)
                self.thumbs.submit(self.make_thumb, source, dest, row["media_kind"], key)
        return None

    def make_thumb(self, source, dest, kind, key):
        try:
            from PIL import Image, ImageOps

            try:
                import pillow_heif

                pillow_heif.register_heif_opener()
            except ImportError:
                pass
            dest.parent.mkdir(exist_ok=True)
            if kind == "video":
                import imageio_ffmpeg

                tmp = dest.with_suffix(".frame.jpg")
                subprocess.run(
                    [
                        imageio_ffmpeg.get_ffmpeg_exe(),
                        "-loglevel",
                        "error",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        "scale=480:-2",
                        "-y",
                        str(tmp),
                    ],
                    timeout=25,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                os.replace(tmp, dest)
            else:
                with Image.open(source) as im:
                    im = ImageOps.exif_transpose(im)
                    im.thumbnail((480, 480))
                    im.convert("RGB").save(dest.with_suffix(".tmp"), "JPEG", quality=82)
                os.replace(dest.with_suffix(".tmp"), dest)
        except Exception:
            # A format unavailable to Pillow remains a visible file tile.
            with self.thumb_lock:
                self.failed_thumbs.add(key)
        finally:
            with self.thumb_lock:
                self.pending.discard(key)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    @property
    def app(self):
        return self.server.app

    def send_json(self, obj, status=200):
        blob = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    def trusted_host(self):
        return self.headers.get("Host") in (
            f"127.0.0.1:{self.server.server_port}",
            f"localhost:{self.server.server_port}",
        )

    def end_headers(self):
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' blob:; "
            "media-src 'self'; "
            "connect-src 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'; "
            "form-action 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def authenticated(self):
        return authorized(
            self.app.access_token, self.headers.get("Cookie"), self.headers.get("Authorization")
        )

    def do_GET(self):
        try:
            if not self.trusted_host():
                return self.send_json({"error": "Недопустимый Host"}, 403)
            url = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/" and q.get("key"):
                if not same_token(self.app.access_token, q["key"]):
                    return self.send_json({"error": "Неверная ссылка входа"}, 403)
                self.send_response(303)
                self.send_header(
                    "Set-Cookie",
                    COOKIE_NAME
                    + "="
                    + self.app.access_token
                    + "; Path=/; HttpOnly; SameSite=Strict",
                )
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if not self.authenticated():
                return self.send_json(
                    {"error": "Откройте ссылку входа из консоли веб-сервера"}, 401
                )
            if url.path == "/api/state":
                return self.send_json(self.app.state())
            if url.path == "/api/catalog":
                result = self.app.catalog.page(q)
                return self.send_json(result, 202 if result["loading"] else 200)
            if url.path in ("/api/thumb", "/api/media"):
                row = self.app.row(q.get("id", ""))
                if (
                    not row
                    or row["state"] not in ("original", "preview")
                    or not self.app.safe(
                        row["relpath"] if row["state"] == "original" else row["preview_rel"]
                    ).is_file()
                ):
                    return self.send_json({"error": "Копия отсутствует"}, 404)
                if url.path == "/api/thumb":
                    path = self.app.thumbnail(row)
                    if path is False:
                        return self.send_json({"error": "Формат миниатюры не поддержан"}, 404)
                    if path is None:
                        self.send_response(202)
                        self.send_header("Retry-After", "2")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                else:
                    path = self.app.safe(
                        row["relpath"] if row["state"] == "original" else row["preview_rel"]
                    )
                return self.send_file(path, ranges=True)
            if url.path == "/":
                content = (
                    (ROOT / "web" / "index.html")
                    .read_text(encoding="utf-8")
                    .replace("__TOKEN__", self.app.token)
                    .encode()
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                return self.wfile.write(content)
            if url.path in ("/app.js", "/style.css"):
                return self.send_file(ROOT / "web" / url.path[1:])
            self.send_json({"error": "Не найдено"}, 404)
        except ConnectionError:
            pass
        except (ValueError, OSError, sqlite3.Error) as exc:
            self.send_json({"error": "Не удалось прочитать архив: " + type(exc).__name__}, 400)

    def do_POST(self):
        try:
            if (
                not self.trusted_host()
                or not self.authenticated()
                or not same_token(self.app.token, self.headers.get("X-YaPhoto-Token"))
            ):
                return self.send_json({"error": "Обновите страницу"}, 403)
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 2_000_000:
                raise ValueError("Слишком большой или пустой запрос")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Ожидается объект JSON")
            if self.path == "/api/start":
                self.app.start(data)
            elif self.path == "/api/stop":
                self.app.stop()
            elif self.path == "/api/cookies":
                with self.app.lock:
                    if self.app.job["running"]:
                        raise ValueError("Дождитесь завершения процесса")
                    if "cookies" not in data:
                        raise ValueError(
                            "В запросе отсутствует поле cookies: выберите JSON-файл cookies"
                        )
                    from yaphoto import cookies_from_file

                    tmp = self.app.cookies.with_suffix(".upload")
                    try:
                        tmp.write_text(json.dumps(data["cookies"]), encoding="utf-8")
                        cookies_from_file(tmp)
                        os.replace(tmp, self.app.cookies)
                    finally:
                        tmp.unlink(missing_ok=True)
            else:
                return self.send_json({"error": "Не найдено"}, 404)
            self.send_json({"ok": True})
        except ConnectionError:
            pass
        except (ValueError, KeyError, OSError, ExportError) as exc:
            self.send_json(
                {
                    "error": str(exc)
                    if isinstance(exc, (ValueError, KeyError, ExportError))
                    else type(exc).__name__
                },
                400,
            )

    def send_file(self, path, ranges=False):
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get("Range") if ranges else None
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
            if not match or not any(match.groups()):
                return self.send_json({"error": "Некорректный Range"}, 416)
            if match[1]:
                start = int(match[1])
                end = min(int(match[2]), end) if match[2] else end
            else:
                start = max(0, size - int(match[2]))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if requested else 200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(end - start + 1))
        if ranges:
            self.send_header("Accept-Ranges", "bytes")
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    block = stream.read(min(262144, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
        except ConnectionError:
            pass


def main():
    from local_config import defaults

    try:
        default_output, default_cookies = defaults(ROOT, "backup")
    except ValueError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description="Локальный веб-интерфейс YaPhoto")
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--cookies", type=Path, default=default_cookies)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    app = App(args.output, args.cookies)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.app = app
    address = f"http://127.0.0.1:{server.server_port}"
    login_address = address + "/?key=" + app.access_token
    print(
        f"YaPhoto — ссылка входа: {login_address}\n"
        f"Архив: {app.output}\n"
        "Закрывайте сервер после завершения процесса.",
        flush=True,
    )
    if not args.no_browser:
        webbrowser.open(login_address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if app.state()["running"]:
            if app.job["command"] in ("download", "upgrade-previews"):
                app.stop()
            print("Ожидание завершения текущих файлов…", flush=True)
            app.process.wait()
    finally:
        server.server_close()
        app.thumbs.shutdown(wait=True, cancel_futures=True)
        app.close_catalog()


if __name__ == "__main__":
    sys.exit(main())
