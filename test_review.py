"""Regressions for performance, validation and browser access control."""

import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import requests

import local_config
import test_web
import test_yaphoto
import web
import yaphoto as app
from test_yaphoto import DATA, FakeAPI, resource
from yaphoto_lib import cli, engine, files


class ReviewCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = app.connect(self.root / "catalog.sqlite3")
        self.addCleanup(self.conn.close)
        app.save_resource(self.conn, 1, resource())
        self.conn.commit()
        self.row = dict(self.conn.execute("SELECT * FROM resources").fetchone())

    def saved(self):
        path = self.root / self.row["relpath"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(DATA)
        self.conn.execute("UPDATE resources SET status='verified'")
        self.conn.commit()
        app.verify(self.conn, self.root)
        return path

    def test_worker_limit_is_32_and_attempt_limit_stays_8(self):
        self.assertEqual(app.DownloadOptions(workers=32).workers, 32)
        for options in ({"workers": 33}, {"attempts": 9}):
            with self.assertRaises(ValueError):
                app.DownloadOptions(**options)
        with patch("builtins.print"):
            self.assertEqual(app.main(["verify", "--workers", "32", "--output", str(self.root)]), 2)

    def test_live_summary_survives_worker_completion_and_fits_terminal(self):
        console = app.Console(self.root / "console.log", interval=100, completion_delay=0)
        console.stop.set()
        console.thread.join(timeout=3)
        console.interactive = True
        summary = "ОБРАБОТАНО 50.0% 16/32 | Всего: 32 · Успешно: 10 · Превью: 4 · Ошибок: 2"
        console.event(summary)
        for wid in range(1, 33):
            console.workers[wid] = dict(
                name=f"{wid}.jpg", attempt="1/4", stage="загрузка", path=None
            )
        stream = io.StringIO()
        try:
            with (
                patch("sys.stdout", stream),
                patch("shutil.get_terminal_size", return_value=os.terminal_size((120, 15))),
            ):
                console.frame_lines = 40
                console._draw()
                first = stream.getvalue()
                stream.seek(0)
                stream.truncate()
                console.file_done(1, "1.jpg", "OK оригинал")
                second = stream.getvalue()
            for frame in (first, second):
                self.assertIn("Успешно: 10", frame)
                self.assertIn("Превью: 4", frame)
                self.assertIn("Ошибок: 2", frame)
                live = frame[frame.index("ОБРАБОТАНО") :]
                self.assertLess(live.index("ОБРАБОТАНО"), live.index("W01"))
                self.assertNotIn("\x1b[2K", frame)
                self.assertNotIn("\x1b[40A", frame)
                self.assertLessEqual(live.count("\n"), 14)
                self.assertIn("Ещё воркеров: 24", frame)
            self.assertLess(console.frame_lines, 15)
            self.assertIn("OK оригинал", second)
        finally:
            console.interactive = False
            console.frame_lines = 0
            console.close()

    def test_fixed_columns_long_names_completion_log_and_pause(self):
        console = app.Console(self.root / "console.log", interval=100)
        console.stop.set()
        console.thread.join(timeout=3)
        console.interactive = True
        stream = io.StringIO()
        try:
            wid = console.worker("a.jpg", "1/3")
            console.worker_ids[9999] = 32
            console.reset_workers()
            wid = console.worker("very-long-name-" * 30 + ".jpg", "1/3")
            self.assertEqual(wid, 1)
            with patch("sys.stdout", stream), patch("time.sleep") as sleep:
                console._draw()
                console.file_done(wid, "very-long-name-" * 30 + ".jpg", "OK оригинал")
            sleep.assert_called_once_with(1.5)
            text = stream.getvalue()
            self.assertIn("RESULT W01", (self.root / "console.log").read_text(encoding="utf8"))
            self.assertIn("OK оригинал", text)
            live = text[text.rindex("ОБРАБОТАНО") :]
            self.assertLess(live.index("┃"), live.index("very-long-name"))
            self.assertEqual(console._clip_columns("照片📷abc", 6), "照片📷")
        finally:
            console.interactive = False
            console.frame_lines = 0
            console.close()

    def test_status_colors_and_color_modes(self):
        for status, color in (
            ("OK оригинал", "32"),
            ("ChunkedEncodingError", "31"),
            ("OK оригинал; превью удалено", "32"),
            ("RemoteError: оригинал не получен; превью XXXL 4000x3000", "31"),
            ("ChunkedEncodingError: оригинал не получен; превью уже есть 1280x597", "31"),
            ("RemoteError: оригинал не получен", "31"),
            ("НЕТ оригинала; превью XXXL 4000x3000", "33"),
            ("превью не сохранено (RemoteError)", "31"),
            ("оригинал не получен", "33"),
            ("ожидание ответа", "37"),
        ):
            self.assertEqual(app.Console.status_color(status), color)
        for mode in ("auto", "always", "never"):
            console = app.Console(
                self.root / "console.log", interval=100, completion_delay=0, color=mode
            )
            console.stop.set()
            console.thread.join(timeout=3)
            stream = io.StringIO()
            try:
                with patch("sys.stdout", stream):
                    console.file_done(1, "error-in-filename.jpg", "OK оригинал")
                self.assertEqual("\x1b[32m" in stream.getvalue(), mode == "always")
                console.interactive = True
                self.assertEqual("\x1b[33m" in console._paint("preview", "33"), mode != "never")
                text = (self.root / "console.log").read_text(encoding="utf8")
                self.assertNotIn("\x1b[", text)
            finally:
                console.interactive = False
                console.close()

    def test_summary_segment_colors_and_margin(self):
        console = app.Console(
            self.root / "console.log", interval=100, completion_delay=0, color="always"
        )
        console.stop.set()
        console.thread.join(timeout=3)
        text = "Всего: 10 · Успешно: 5 · Превью: 3 · Ошибок: 2"
        try:
            painted = console._paint_summary(text)
            for label, shade in (("Успешно: 5", "32"), ("Превью: 3", "33"), ("Ошибок: 2", "31")):
                self.assertIn(f"\x1b[{shade}m{label}\x1b[0m", painted)
            console.color = "never"
            self.assertEqual(console._paint_summary(text), text)
            console.overall = "ОБРАБОТАНО 50% | " + text
            console.completed_lines.append(("finished file", "32"))
            stream = io.StringIO()
            with patch("sys.stdout", stream):
                console._draw()
            self.assertIn("finished file\x1b[K\n\x1b[K\nОБРАБОТАНО", stream.getvalue())
        finally:
            console.interactive = False
            console.frame_lines = 0
            console.close()

    def test_source_package_normalizes_lf_and_preserves_batch_crlf(self):
        project = self.root / "source"
        (project / "tools").mkdir(parents=True)
        package = Path(app.__file__).parent / "tools/package.py"
        (project / "tools/package.py").write_bytes(package.read_bytes())
        names = ["main.py", "README.md", "setup.bat", "PUBLIC_FILES.json", "tools/package.py"]
        (project / "main.py").write_bytes(b"print('ok')\r\n")
        (project / "README.md").write_bytes(b"Read me\r\n")
        (project / "setup.bat").write_bytes(b"@echo off\r\n")
        (project / "PUBLIC_FILES.json").write_text(json.dumps(names), encoding="utf8")
        subprocess.run(
            [sys.executable, str(project / "tools/package.py")],
            check=True,
            capture_output=True,
            timeout=30,
        )
        with zipfile.ZipFile(self.root / "source-source.zip") as archive:
            self.assertEqual(archive.read("source/setup.bat"), b"@echo off\r\n")
            for name in names:
                if not name.endswith(".bat"):
                    self.assertNotIn(b"\r", archive.read("source/" + name))

    def test_preview_commit_failure_rolls_back_borrowed_connection(self):
        class FailedCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("commit failed")

        connection = sqlite3.connect(self.root / "catalog.sqlite3", factory=FailedCommit)
        connection.row_factory = sqlite3.Row
        api, _ = test_yaphoto.Tests.preview_api(self)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                app.save_preview(api, self.row, self.root, connection=connection)
            self.assertFalse(connection.in_transaction)
            self.conn.execute("INSERT INTO settings VALUES('rollback-test','ok')")
            self.conn.commit()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM previews").fetchone()[0], 0)
        finally:
            connection.close()

    def test_reports_keep_separate_media_files(self):
        app.report(self.conn, self.root, "photo")
        saved = {
            name: (self.root / f"{name}_photo.json").read_bytes()
            for name in ("errors", "previews", "missing_originals", "report")
        }
        app.report(self.conn, self.root, "video")
        for name, content in saved.items():
            self.assertEqual((self.root / f"{name}_photo.json").read_bytes(), content)
            self.assertTrue((self.root / f"{name}_video.json").exists())

    def test_reports_separate_rescued_errors_and_filter_media(self):
        self.conn.execute("UPDATE resources SET status='deferred'")
        self.conn.execute(
            "INSERT INTO previews(path,relpath,width,height) VALUES(?,?,1,1)",
            (self.row["path"], "previews/a.jpg"),
        )
        for kind in ("photo", "video"):
            item = resource(f"/photounlim/{kind}.jpg")
            app.save_resource(self.conn, 1, item)
            self.conn.execute(
                "UPDATE resources SET status='failed',media_kind=? WHERE path=?",
                (kind, item["path"]),
            )
        self.conn.commit()
        with patch("builtins.print"):
            app.report(self.conn, self.root, "photo")
        errors = json.loads((self.root / "errors.json").read_text(encoding="utf8"))
        missing = json.loads((self.root / "missing_originals.json").read_text(encoding="utf8"))
        self.assertEqual([r["path"] for r in errors], ["/photounlim/photo.jpg"])
        self.assertEqual(len(missing), 2)
        self.assertTrue(all(r["media_kind"] == "photo" for r in missing))
        self.assertTrue(next(r for r in missing if r["path"] == self.row["path"])["has_preview"])
        self.assertIn("verify", (self.root / "report.json").read_text(encoding="utf8"))

    def test_preview_workers_reuse_connection_without_schema_initialization(self):
        client = FakeAPI()
        client.close = lambda: None
        worker = engine.DownloadWorker(
            self.root,
            {},
            app.DownloadOptions(preview_only=True, auth_context=("sk", "123")),
            threading.Event(),
            "123",
        )
        connections = []

        def saved(api, row, output, *, connection=None):
            connections.append(connection)
            return "saved"

        try:
            with (
                patch.object(engine, "Yandex", return_value=client),
                patch.object(engine, "save_preview", side_effect=saved),
                patch.object(
                    engine, "connect", side_effect=AssertionError("Schema initialization")
                ),
            ):
                worker(self.row)
                worker(self.row)
            self.assertEqual(len(connections), 2)
            self.assertIsNotNone(connections[0])
            self.assertIs(connections[0], connections[1])
            self.assertEqual(len(worker.preview_connections), 1)
        finally:
            worker.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")

    def test_busy_archive_is_reported_before_database_or_network_access(self):
        output = self.root / "locked-archive"
        output.mkdir()
        path = output / ".export.lock"
        command = [
            sys.executable,
            "-X",
            "utf8",
            "yaphoto.py",
            "download",
            "--output",
            str(output),
            "--cookies",
            str(self.root / "missing-cookies.json"),
        ]
        with files.acquire_archive_lock(path):
            result = subprocess.run(
                command,
                cwd=Path(app.__file__).parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Архив уже занят другим процессом", result.stderr)
        self.assertIn(str(path), result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((output / "catalog.sqlite3").exists())
        with files.acquire_archive_lock(path):
            pass
        self.assertEqual(path.read_bytes(), b"0" if os.name == "nt" else b"")

    def test_permission_error_names_the_inaccessible_path(self):
        path = self.root / ".export.lock"
        error = PermissionError(13, "Access denied", str(path))
        stream = io.StringIO()
        with (
            patch.object(cli, "acquire_archive_lock", side_effect=error),
            contextlib.redirect_stderr(stream),
        ):
            code = app.main(["download", "--output", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("Нет доступа к файлу или папке", stream.getvalue())
        self.assertIn(str(path), stream.getvalue())
        self.assertNotIn("Traceback", stream.getvalue())

    def test_unchanged_download_does_not_rehash_verified_files(self):
        self.saved()
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(1,'list',1)")
        app.bind_account(self.conn, "123", self.root)
        with (
            patch.object(engine, "validate_file", side_effect=AssertionError("Rehash")),
            patch.object(engine, "Yandex") as client,
        ):
            app.download(
                self.conn, self.root, {}, options=app.DownloadOptions(), stop=threading.Event()
            )
        client.assert_not_called()

    def test_changed_stamp_and_explicit_verify_rehash(self):
        path = self.saved()
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
        original = app.validate_file
        with patch.object(engine, "validate_file", wraps=original) as hashing:
            app.verify(self.conn, self.root, full=False)
            self.assertEqual(hashing.call_count, 1)
            app.verify(self.conn, self.root, full=False)
            self.assertEqual(hashing.call_count, 1)
            app.verify(self.conn, self.root, full=True)
            self.assertEqual(hashing.call_count, 2)

    def test_legacy_entry_is_validated_once_and_missing_file_requeued(self):
        self.saved()
        self.conn.execute("UPDATE resources SET validated_mtime_ns=NULL")
        self.conn.commit()
        with patch.object(engine, "validate_file", wraps=app.validate_file) as hashing:
            app.verify(self.conn, self.root, full=False)
            app.verify(self.conn, self.root, full=False)
            self.assertEqual(hashing.call_count, 1)
        (self.root / self.row["relpath"]).unlink()
        app.verify(self.conn, self.root, full=False)
        self.assertEqual(self.conn.execute("SELECT status FROM resources").fetchone()[0], "failed")

    def test_download_one_has_no_database_initialization(self):
        with patch.object(
            engine, "connect", side_effect=AssertionError("Per-file schema initialization")
        ):
            app.download_one(FakeAPI(), self.row, self.root, "none")

    def test_log_rotation_preserves_cumulative_stats(self):
        log = self.root / "console.log"
        console = app.Console(log, log_max_bytes=512, log_backups=2)
        try:
            for i in range(40):
                console.event(f"REQ {i} START GET s175klg.storage.yandex.net")
                console.event(f"REQ {i} BODY OK GET s175klg.storage.yandex.net")
        finally:
            console.close()
        logs = list(self.root.glob("console.log*"))
        self.assertLessEqual(len(logs), 3)
        self.assertTrue(all(p.stat().st_size <= 512 for p in logs))
        with patch.object(
            app.ServerStats, "consume", side_effect=AssertionError("Full journal replay")
        ):
            restored = app.ServerStats(log, self.root / "server_stats.txt")
        self.assertEqual(restored.counts, {"s175klg": [40, 40]})

    def test_invalid_media_rejected_for_python_callers(self):
        with self.assertRaises(ValueError):
            app.verify(self.conn, self.root, media="photo' OR 1=1--")
        with self.assertRaises(ValueError):
            app.report(self.conn, self.root, media="bad")

    def test_local_config_errors_are_human_readable(self):
        path = self.root / "local.json"
        for value in ("broken", "[]", '{"output":4}', '{"output":"\\u0000"}'):
            path.write_text(value)
            with self.assertRaisesRegex(ValueError, "local.json"):
                local_config.defaults(self.root, "backup")
        for main in (app.main, web.main):
            stream = io.StringIO()
            with (
                patch("local_config.defaults", side_effect=ValueError("Некорректный local.json")),
                contextlib.redirect_stderr(stream),
            ):
                self.assertEqual(main(), 2)
            self.assertNotIn("Traceback", stream.getvalue())


class ReviewWebTests(unittest.TestCase):
    setUp = test_web.WebTests.setUp
    tearDown = test_web.WebTests.tearDown

    def test_cache_refresh_is_throttled_and_explicit_refresh_bypasses_delay(self):
        cache = self.app.catalog
        cache._background_refresh()
        with (
            patch("catalog_cache.time.monotonic", return_value=cache.next_check - 1),
            patch("catalog_cache.threading.Thread") as thread,
        ):
            for _ in range(50):
                cache.page({})
            thread.assert_not_called()
            cache.page({"refresh": "1"})
            thread.assert_called_once()
        cache.refreshing = False
        self.assertEqual(cache.refresh_interval, 15)

    def test_explicit_refresh_during_rebuild_is_not_lost(self):
        cache = self.app.catalog
        entered, release = threading.Event(), threading.Event()
        loader = cache.loader
        conn = app.connect(self.app.output / "catalog.sqlite3")
        conn.execute("UPDATE resources SET status='failed' WHERE media_kind='video'")
        conn.commit()

        def delayed(connection):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Refresh was not released")
            return loader(connection)

        try:
            with patch.object(cache, "loader", side_effect=delayed):
                cache.page({"refresh": "1"})
                self.assertTrue(entered.wait(3))
                conn.execute("UPDATE resources SET status='verified' WHERE media_kind='video'")
                conn.commit()
                cache.page({"refresh": "1"})
                release.set()
                cache.thread.join(timeout=10)
                self.assertFalse(cache.thread.is_alive())
                self.assertFalse(cache.refresh_requested)
                self.assertTrue(
                    any(
                        r["media_kind"] == "video" and r["state"] == "original"
                        for r in cache.rows()
                    )
                )
        finally:
            release.set()
            conn.close()

    def test_undated_records_are_last_in_both_orders(self):
        conn = app.connect(self.app.output / "catalog.sqlite3")
        conn.execute(
            "INSERT INTO resources(path,name,relpath,raw_json) VALUES('undated','image.jpg',"
            "'photos/image.jpg','{}')"
        )
        conn.commit()
        conn.close()
        self.app.rows()
        for order in ("asc", "desc"):
            data = self.app.catalog.page({"order": order})
            self.assertEqual(data["items"][-1]["date"], "Без даты")
            identifier = data["items"][-1]["id"]
            data = self.app.catalog.page({"order": order, "ids": identifier})
            self.assertEqual(data["items"][0]["date"], "Без даты")

    def test_catalog_pages_reuse_preordered_views(self):
        cache = self.app.catalog
        expected = sorted(self.app.rows(), key=lambda r: (r["date"], r["name"], r["id"]))
        with (
            patch.object(cache, "_ensure"),
            patch("builtins.sorted", side_effect=AssertionError("Per-page sort")),
        ):
            for _ in range(10):
                for order in ("asc", "desc"):
                    rows = expected if order == "asc" else list(reversed(expected))
                    result = cache.page({"order": order, "offset": "1", "limit": "1"})
                    self.assertEqual(result["items"][0]["id"], rows[1]["id"])
                result = cache.page({"media": "photo", "state": "preview", "month": "2024-01"})
                self.assertEqual(result["total"], 1)
                self.assertEqual(result["items"][0]["state"], "preview")
        ids = ",".join([expected[0]["id"], expected[0]["id"], expected[-1]["id"]])
        result = cache.page({"ids": ids, "order": "asc"})
        self.assertEqual(
            [r["id"] for r in result["items"]], [expected[0]["id"], expected[-1]["id"]]
        )

    def test_response_security_headers_are_consistent_and_unique(self):
        root = self.client.get(self.base + "/", timeout=5)
        self.assertEqual(root.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", root.headers["Content-Security-Policy"])
        identifier = self.app.rows()[0]["id"]
        for endpoint in ("media", "thumb"):
            with patch.object(
                self.app, "thumbnail", return_value=self.app.safe(self.app.rows()[0]["relpath"])
            ):
                response = self.client.get(
                    self.base + f"/api/{endpoint}", params={"id": identifier}, timeout=5
                )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.raw.headers.getlist("X-Content-Type-Options"), ["nosniff"])

    def test_slow_refresh_does_not_block_catalog_media_or_status(self):
        old_rows = self.app.rows()
        entered, release = threading.Event(), threading.Event()
        loader = self.app.catalog.loader

        def delayed(conn):
            entered.set()
            if not release.wait(10):
                raise AssertionError("Refresh was not released")
            return loader(conn)

        conn = app.connect(self.app.output / "catalog.sqlite3")
        conn.execute("UPDATE resources SET status='failed' WHERE media_kind='video'")
        conn.commit()
        conn.close()
        try:
            with patch.object(self.app.catalog, "loader", side_effect=delayed) as loading:
                self.app.catalog.rows(wait=False)
                self.assertTrue(entered.wait(3))
                for _ in range(5):
                    response = self.client.get(self.base + "/api/catalog", timeout=2)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["summary"]["total"], 3)
                    self.assertEqual(len(response.json()["items"]), 3)
                    response = self.client.get(
                        self.base + "/api/media", params={"id": old_rows[0]["id"]}, timeout=2
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(
                        self.client.get(self.base + "/api/state", timeout=2).status_code, 200
                    )
                self.assertEqual(loading.call_count, 1)
                release.set()
                self.app.catalog.thread.join(timeout=5)
        finally:
            release.set()
        self.assertEqual(self.app.rows()[-1]["state"], "error")

    def test_cold_catalog_returns_loading_while_status_remains_available(self):
        self.app.close_catalog()
        entered, release = threading.Event(), threading.Event()

        def delayed(conn):
            entered.set()
            release.wait(10)
            return self.app.read_rows(conn)

        self.app.catalog = web.CatalogCache(self.app.output / "catalog.sqlite3", delayed)
        try:
            response = self.client.get(self.base + "/api/catalog", timeout=2)
            self.assertEqual(response.status_code, 202)
            self.assertTrue(response.json()["loading"])
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.client.get(self.base + "/api/state", timeout=2).status_code, 200)
        finally:
            release.set()
        self.app.catalog.thread.join(timeout=5)
        response = self.client.get(self.base + "/api/catalog", timeout=2)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["summary"]["total"], 3)

    def test_gallery_refresh_does_not_stat_entire_archive(self):
        with contextlib.closing(sqlite3.connect(self.app.output / "catalog.sqlite3")) as conn:
            conn.row_factory = sqlite3.Row
            with patch.object(self.app, "safe", side_effect=AssertionError("Filesystem scan")):
                self.assertEqual(len(self.app.read_rows(conn)), 3)

    def test_malformed_access_and_csrf_tokens_are_denied(self):
        response = requests.get(self.base + "/", params={"key": "неверный"})
        self.assertEqual(response.status_code, 403)
        response = requests.get(self.base + "/api/state", headers={"Authorization": "Bearer é"})
        self.assertEqual(response.status_code, 401)
        response = self.client.post(
            self.base + "/api/stop", json={}, headers={"X-YaPhoto-Token": "é"}
        )
        self.assertEqual(response.status_code, 403)

    def test_media_requests_use_id_index_without_copying_catalog(self):
        row = self.app.rows()[0]
        with patch.object(self.app, "rows", side_effect=AssertionError("Full copy")):
            for _ in range(10):
                r = self.client.get(
                    self.base + "/api/media",
                    params={"id": row["id"]},
                    headers={"Range": "bytes=0-1"},
                )
                self.assertEqual(r.status_code, 206)

    def test_thumbnail_requests_use_id_index_without_copying_catalog(self):
        row = self.app.rows()[0]
        with (
            patch.object(self.app, "rows", side_effect=AssertionError("Full copy")),
            patch.object(self.app, "thumbnail", return_value=self.app.safe(row["relpath"])),
        ):
            for _ in range(10):
                response = self.client.get(self.base + "/api/thumb", params={"id": row["id"]})
                self.assertEqual(response.status_code, 200)

    def test_cache_reloads_only_on_commit_and_reuses_ids(self):
        self.app.close_catalog()
        self.app.resource_ids.clear()
        self.app.catalog = web.CatalogCache(self.app.output / "catalog.sqlite3", self.app.read_rows)
        loader = self.app.catalog.loader
        with (
            patch.object(self.app.catalog, "loader", wraps=loader) as loading,
            patch.object(web.hashlib, "sha256", wraps=web.hashlib.sha256) as hashing,
        ):
            original = self.app.rows()
            for _ in range(60):
                self.assertIs(self.app.rows(), original)
            self.assertEqual(loading.call_count, 1)
            self.assertEqual(hashing.call_count, 3)
            conn = app.connect(self.app.output / "catalog.sqlite3")
            conn.execute("UPDATE resources SET status='failed' WHERE name LIKE '2023%'")
            conn.commit()
            conn.close()
            changed = self.app.rows()
            self.assertIsNot(changed, original)
            self.assertEqual(loading.call_count, 2)
            self.assertEqual(hashing.call_count, 3)
            self.assertEqual(changed[-1]["state"], "error")
            with self.assertRaises(TypeError):
                changed[0]["state"] = "changed"

    def test_preview_upgrade_rejected_before_spawning(self):
        with patch("web.subprocess.Popen") as process:
            for media in ("video", "all"):
                r = self.client.post(
                    self.base + "/api/start",
                    json={"command": "upgrade-previews", "media": media},
                    headers=self.headers,
                )
                self.assertEqual(r.status_code, 400)
                self.assertIn("только", r.json()["error"])
            process.assert_not_called()

    def test_missing_cookies_field_and_default_attempts(self):
        r = self.client.post(self.base + "/api/cookies", json={}, headers=self.headers)
        self.assertEqual(r.status_code, 400)
        self.assertIn("поле cookies", r.json()["error"])
        with patch("web.subprocess.Popen") as process, patch("web.threading.Thread.start"):
            self.app.start({"command": "download"})
            args = process.call_args.args[0]
            self.assertEqual(
                int(args[args.index("--attempts") + 1]), app.DownloadOptions().attempts
            )

    def test_unauthenticated_media_denied_and_csp_has_no_inline_script(self):
        identifier = self.app.rows()[0]["id"]
        for path in ("/api/media", "/api/thumb", "/api/catalog", "/api/state"):
            self.assertEqual(
                requests.get(self.base + path, params={"id": identifier}).status_code, 401
            )
        response = self.client.get(self.base + "/")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertNotIn("<script>", response.text)
        self.assertIn('name="yaphoto-csrf"', response.text)
        login = requests.get(self.base + "/?key=" + self.app.access_token, allow_redirects=False)
        self.assertIn("HttpOnly", login.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", login.headers["Set-Cookie"])


if __name__ == "__main__":
    unittest.main()
