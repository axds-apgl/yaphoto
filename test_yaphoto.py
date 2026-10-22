"""Offline tests. No Yandex account, cookies, or network access required."""

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yaphoto as app
from yaphoto_lib import catalog, engine
from yaphoto_lib import console as console_module

DATA = b"original-photo-bytes"


def resource(path="/photounlim/IMG_0001.jpg", data=DATA, **kwargs):
    return {
        "path": path,
        "name": "IMG_0001.jpg",
        "type": "file",
        "meta": {"size": len(data), "md5": hashlib.md5(data).hexdigest(), "mimetype": "image/jpeg"},
        **kwargs,
    }


class Response:
    def __init__(self, data=DATA, status=200, headers=None):
        self.data, self.status_code = (data, status)
        self.headers = (
            headers
            if headers is not None
            else {"Content-Length": str(len(data)), "Content-Type": "image/jpeg"}
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def iter_content(self, _):
        yield self.data


class Content:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return next(self.responses)


class FakeAPI:
    def __init__(self, responses=None):
        self.content = Content(responses or [Response()])
        self.cooldown = app.Cooldown()

    def original_url(self, path):
        return "https://downloader.yandex.ru/file"


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = app.connect(self.root / "catalog.sqlite3")
        self.addCleanup(self.conn.close)

    def row(self, item=None):
        item = item or resource()
        app.save_resource(self.conn, 1, item)
        self.conn.commit()
        return dict(
            self.conn.execute(
                "SELECT * FROM resources WHERE path=?", (app.canonical_path(item["path"]),)
            ).fetchone()
        )

    def test_names_and_resource_collisions(self):
        a = self.row(resource("/photounlim/a/IMG_0001.jpg"))
        b = self.row(resource("/photounlim/b/IMG_0001.jpg"))
        self.assertNotEqual(a["relpath"], b["relpath"])
        self.assertEqual(Path(a["relpath"]).name, "IMG_0001.jpg")

    def test_unsafe_names(self):
        for name in ("../outside.jpg", "CON", "aux.jpg", "x:y?.jpg", "name. "):
            safe = app.safe_name(name)
            self.assertNotIn("/", safe)
            self.assertNotIn("\\", safe)
            self.assertFalse(safe.endswith((" ", ".")))
        self.assertEqual(app.safe_name("CON"), "_CON")

    def test_source_alias_does_not_duplicate(self):
        self.row()
        app.save_resource(self.conn, 2, resource("photounlim:/IMG_0001.jpg"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1)

    def test_video_not_queued(self):
        video = resource()
        video["meta"]["mimetype"] = "video/mp4"
        self.assertFalse(app.save_resource(self.conn, 1, video))

    def test_changed_metadata_returns_to_pending(self):
        row = self.row()
        self.conn.execute("UPDATE resources SET status='verified'")
        app.save_resource(self.conn, 2, resource(data=b"changed"))
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM resources WHERE path=?", (row["path"],)
            ).fetchone()[0],
            "pending",
        )

    def test_wrong_same_size_existing_file_is_replaced(self):
        row = self.row()
        dst = self.root / row["relpath"]
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"x" * len(DATA))
        sha, status = app.download_one(FakeAPI(), row, self.root, "none")
        self.assertEqual(dst.read_bytes(), DATA)
        self.assertEqual(status, "verified")
        self.assertEqual(sha, hashlib.sha256(DATA).hexdigest())

    def test_crash_after_rename_reuses_only_verified_original(self):
        row = self.row()
        dst = self.root / row["relpath"]
        dst.parent.mkdir(parents=True)
        dst.write_bytes(DATA)
        fake = FakeAPI()
        app.download_one(fake, row, self.root, "none")
        self.assertEqual(fake.content.urls, [])
        self.assertTrue(app.auxiliary_path(self.root, row["path"], "metadata").exists())

    def test_bad_download_is_never_final(self):
        row = self.row()
        with self.assertRaises(app.IntegrityError):
            app.download_one(FakeAPI([Response(b"x" * len(DATA))]), row, self.root, "none")
        self.assertFalse((self.root / row["relpath"]).exists())

    def test_unknown_cloud_hash_is_not_verified(self):
        item = resource()
        del item["meta"]["md5"]
        row = self.row(item)
        _, status = app.download_one(FakeAPI(), row, self.root, "none")
        self.assertEqual(status, "downloaded")

    def test_interrupted_content_is_not_final(self):
        row = self.row()
        with self.assertRaises(app.IntegrityError):
            app.download_one(
                FakeAPI([Response(b"short", headers={"Content-Length": "999"})]),
                row,
                self.root,
                "none",
            )
        self.assertFalse((self.root / row["relpath"]).exists())

    def test_html_not_saved_as_original(self):
        with self.assertRaises(app.IntegrityError):
            app.transfer(
                FakeAPI([Response(b"<html>", headers={"Content-Type": "text/html"})]),
                "https://downloader.yandex.ru/file",
                self.root / "part",
            )

    def test_expired_content_link_is_retryable_not_session_failure(self):
        with self.assertRaises(app.RemoteError):
            app.transfer(
                FakeAPI([Response(status=403)]),
                "https://downloader.yandex.ru/file",
                self.root / "part",
            )

    def test_redirect_domain_validated_before_request(self):
        fake = FakeAPI([Response(status=302, headers={"Location": "https://example.com/file"})])
        with self.assertRaises(app.ExportError):
            app.transfer(fake, "https://downloader.yandex.ru/file", self.root / "part")
        self.assertEqual(len(fake.content.urls), 1)

    def test_pagination_and_nested_folder(self):

        class ListAPI:
            def api(self, method, params):
                root, offset = (params["path"], params["offset"])
                if root == "/photounlim/nested":
                    return [{"path": root, "type": "dir"}, resource(root + "/IMG_0001.jpg")]
                if offset == 0:
                    return [
                        {"path": root, "type": "dir"},
                        resource(),
                        {"path": root + "/nested", "type": "dir"},
                    ]
                return [{"path": root, "type": "dir"}]

        found = []
        app.collect_list(ListAPI(), found.append, page_size=2)
        self.assertEqual(len(found), 2)

    def test_repeated_page_rejected(self):

        class ListAPI:
            def api(self, *_):
                return [resource()]

        with self.assertRaises(app.RemoteError):
            app.collect_list(ListAPI(), lambda _: None, page_size=1)

    def test_failed_scan_keeps_rows_but_not_success(self):

        def broken(api, emit):
            emit(resource())
            raise app.RemoteError("broken")

        with patch.object(catalog, "collect_list", broken):
            with self.assertRaises(app.RemoteError):
                app.scan(None, self.conn, "list")
        self.assertEqual(self.conn.execute("SELECT complete FROM scans").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1)

    def test_partial_photoslice_rejected(self):

        class PhotoAPI:
            def api(self, method, params=None):
                if method.endswith("init-snapshot"):
                    return {"photoslice_id": "s"}
                if method.endswith("get-snapshot"):
                    return {"items": [{"id": "c", "size": 2}]}
                return {
                    "clusters": {"fetched": [{"id": "c", "items": [{"id": "photounlim:/one.jpg"}]}]}
                }

        with self.assertRaises(app.RemoteError):
            app.collect_photoslice(PhotoAPI(), lambda _: None)

    def test_verify_detects_corruption_even_same_size(self):
        row = self.row()
        sha, status = app.download_one(FakeAPI(), row, self.root, "none")
        self.conn.execute("UPDATE resources SET status=?,local_sha256=?", (status, sha))
        (self.root / row["relpath"]).write_bytes(b"z" * len(DATA))
        app.verify(self.conn, self.root)
        self.assertEqual(self.conn.execute("SELECT status FROM resources").fetchone()[0], "failed")

    def test_verify_promotes_after_cloud_md5_available(self):
        row = self.row()
        dst = self.root / row["relpath"]
        dst.parent.mkdir(parents=True)
        dst.write_bytes(DATA)
        self.conn.execute(
            "UPDATE resources SET status='downloaded',local_sha256=?",
            (hashlib.sha256(DATA).hexdigest(),),
        )
        app.verify(self.conn, self.root)
        self.assertEqual(
            self.conn.execute("SELECT status FROM resources").fetchone()[0], "verified"
        )

    def test_account_binding(self):
        app.bind_account(self.conn, "123", self.root)
        with self.assertRaises(app.ExportError):
            app.bind_account(self.conn, "456", self.root)

    def test_report_compares_scan_membership(self):
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(1,'list',1)")
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(2,'photoslice',1)")
        app.save_resource(self.conn, 1, resource("/photounlim/old.jpg"))
        app.save_resource(self.conn, 1, resource("/photounlim/shared.jpg"))
        app.save_resource(self.conn, 2, resource("/photounlim/shared.jpg"))
        app.save_resource(self.conn, 2, resource("/photounlim/new.jpg"))
        with patch("builtins.print"):
            app.report(self.conn, self.root)
        data = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(data["scan_comparison"]["missing_since_previous"], 1)
        self.assertEqual(data["scan_comparison"]["new_since_previous"], 1)

    def test_bad_cookie_shape_has_clear_error(self):
        target = self.root / "cookies.json"
        target.write_text('["not an object"]', encoding="utf-8")
        with self.assertRaises(app.ExportError):
            app.cookies_from_file(target)

    def test_overlap_between_pages_is_not_silent(self):

        class ListAPI:
            def api(self, method, params):
                if not params["offset"]:
                    return [resource("/photounlim/a.jpg"), resource("/photounlim/b.jpg")]
                return [resource("/photounlim/b.jpg"), resource("/photounlim/c.jpg")]

        with self.assertRaises(app.RemoteError):
            app.collect_list(ListAPI(), lambda _: None, page_size=2)

    def test_ambiguous_date_not_guessed(self):
        self.assertIsNone(app.parse_time("2020-01-01T12:00:00"))
        self.assertEqual(app.parse_time("2020-01-01T00:00:00Z"), 1577836800)

    def test_windows_dates(self):
        target = self.root / "date.jpg"
        target.write_bytes(DATA)
        result = app.apply_dates(target, {"ctime": 1577836800, "mtime": 1577923200}, "cloud")
        self.assertAlmostEqual(target.stat().st_mtime, 1577923200, delta=2)
        if app.os.name == "nt":
            self.assertTrue(result["creation_time_applied"])
            self.assertAlmostEqual(target.stat().st_birthtime, 1577836800, delta=2)

    def test_parallel_download_failures_and_retry_use_fresh_url(self):
        self.row()
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(1,'list',1)")
        app.bind_account(self.conn, "123", self.root)
        all_clients = []

        class Client(FakeAPI):
            def __init__(self, jar, cooldown):
                super().__init__([Response(status=403), Response()])
                self.links = 0
                all_clients.append(self)

            def login(self):
                return "123"

            def original_url(self, path):
                self.links += 1
                return super().original_url(path)

            def close(self):
                pass

        with patch.object(engine, "Yandex", Client), patch.object(app.time, "sleep"):
            app.download(
                self.conn,
                self.root,
                None,
                options=app.DownloadOptions(workers=2, limit=None, attempts=2, date_policy="none"),
                stop=threading.Event(),
            )
        self.assertEqual(all_clients[0].links, 2)
        self.assertEqual(
            self.conn.execute("SELECT status FROM resources").fetchone()[0], "verified"
        )

    def test_network_timeout_does_not_stop_other_photos(self):
        self.row(resource("/photounlim/a.jpg"))
        self.row(resource("/photounlim/b.jpg"))
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(1,'list',1)")
        app.bind_account(self.conn, "123", self.root)
        stop = threading.Event()
        calls = []

        class Client:
            def __init__(self, *args):
                pass

            def login(self):
                return "123"

            def close(self):
                pass

        def fetch(api, row, *args):
            calls.append(row["path"])
            if row["path"].endswith("a.jpg"):
                raise app.requests.exceptions.ReadTimeout("must never be printed")
            return ("sha", "verified")

        with (
            patch.object(engine, "Yandex", Client),
            patch.object(engine, "download_one", fetch),
            patch.object(app.time, "sleep"),
        ):
            app.download(
                self.conn,
                self.root,
                None,
                options=app.DownloadOptions(workers=1, limit=None, attempts=2, date_policy="none"),
                stop=stop,
            )
        self.assertFalse(stop.is_set())
        self.assertEqual(calls.count("/photounlim/a.jpg"), 2)
        self.assertEqual(calls.count("/photounlim/b.jpg"), 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM resources WHERE path='/photounlim/b.jpg'"
            ).fetchone()[0],
            "verified",
        )

    def test_storage_failure_does_not_hide_other_files_on_same_host(self):

        class BrokenContent:
            calls = 0

            def get(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise app.requests.exceptions.ReadTimeout("private URL must never be printed")
                return Response()

        api = FakeAPI()
        api.content = BrokenContent()
        with self.assertRaises(app.StorageUnavailable):
            app.transfer(api, "https://s108klg.storage.yandex.net/file-a", self.root / "part")
        app.transfer(api, "https://s108klg.storage.yandex.net/file-b", self.root / "part")
        self.assertEqual(api.content.calls, 2)
        self.assertEqual((self.root / "part").read_bytes(), DATA)

    def test_genuine_disk_error_stops_queue(self):
        self.row(resource("/photounlim/a.jpg"))
        self.row(resource("/photounlim/b.jpg"))
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(1,'list',1)")
        app.bind_account(self.conn, "123", self.root)
        stop = threading.Event()

        class Client:
            def __init__(self, *args):
                pass

            def login(self):
                return "123"

            def close(self):
                pass

        with (
            patch.object(engine, "Yandex", Client),
            patch.object(engine, "download_one", side_effect=OSError(28, "disk full")),
        ):
            app.download(
                self.conn,
                self.root,
                None,
                options=app.DownloadOptions(workers=1, limit=None, attempts=2, date_policy="none"),
                stop=stop,
            )
        self.assertTrue(stop.is_set())
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM resources WHERE path='/photounlim/b.jpg'"
            ).fetchone()[0],
            "pending",
        )

    def test_flat_names_case_insensitive_collision(self):
        first = resource("/photounlim/a.JPG")
        first["name"] = "Photo.JPG"
        second = resource("/photounlim/b.jpg")
        second["name"] = "photo.jpg"
        a, b = (self.row(first), self.row(second))
        self.assertEqual(len(Path(a["relpath"]).parts), 2)
        self.assertEqual(len(Path(b["relpath"]).parts), 2)
        self.assertNotEqual(a["relpath"].casefold(), b["relpath"].casefold())
        self.assertIn("__", b["relpath"])

    def test_failed_link_does_not_create_photo_directories(self):
        row = self.row()
        api = FakeAPI()
        with patch.object(api, "original_url", side_effect=app.RemoteError("no link")):
            with self.assertRaises(app.RemoteError):
                app.download_one(api, row, self.root, "none")
        self.assertFalse((self.root / "photos").exists())

    def test_flatten_moves_original_metadata_and_partial(self):
        row = self.row()
        old_rel = str(Path("photos") / "ab" / "legacy-id" / "IMG_0001.jpg")
        self.conn.execute("UPDATE resources SET relpath=?,status=?", (old_rel, "verified"))
        self.conn.commit()
        old = self.root / old_rel
        old.parent.mkdir(parents=True)
        old.write_bytes(DATA)
        old.with_name(old.name + ".metadata.json").write_text("{}")
        old.with_name(old.name + ".part").write_bytes(b"partial")
        moved, removed = app.flatten_layout(self.conn, self.root)
        current = self.conn.execute("SELECT * FROM resources").fetchone()
        self.assertEqual((self.root / current["relpath"]).read_bytes(), DATA)
        self.assertEqual(current["status"], "verified")
        self.assertEqual(moved, 3)
        self.assertGreater(removed, 0)
        self.assertFalse(old.parent.exists())
        self.assertTrue(app.auxiliary_path(self.root, row["path"], "metadata").exists())
        self.assertEqual(app.flatten_layout(self.conn, self.root), (0, 0))

    def test_flatten_resumes_after_file_moved_before_db_commit(self):
        row = self.row()
        old_rel = str(Path("photos") / "ab" / "legacy-id" / "IMG_0001.jpg")
        self.conn.execute("UPDATE resources SET relpath=?", (old_rel,))
        self.conn.execute(
            "INSERT INTO layout_moves VALUES(?,?,?,0)",
            (row["path"], old_rel, "photos/IMG_0001.jpg"),
        )
        self.conn.commit()
        new = self.root / "photos/IMG_0001.jpg"
        new.parent.mkdir()
        new.write_bytes(DATA)
        app.flatten_layout(self.conn, self.root)
        self.assertEqual(
            self.conn.execute("SELECT relpath FROM resources").fetchone()[0], "photos/IMG_0001.jpg"
        )
        self.assertEqual(new.read_bytes(), DATA)

    def test_request_logging_never_contains_signed_url(self):
        import io

        stream = io.StringIO()
        console = app.Console(self.root / "console.log", interval=0.02)
        try:
            with patch.object(console_module, "CONSOLE", console), patch("sys.stdout", stream):
                app.transfer(
                    FakeAPI(),
                    "https://downloader.yandex.ru/file?secret=TOPSECRET",
                    self.root / "part",
                )
            text = (self.root / "console.log").read_text(encoding="utf-8")
            self.assertIn("START", text)
            self.assertIn("HTTP 200", text)
            self.assertIn("100.0%", text)
            self.assertNotIn("TOPSECRET", text)
        finally:
            console.close()

    def test_waiting_request_does_not_repeat_progress_in_log(self):
        import io

        stream = io.StringIO()
        console = app.Console(self.root / "console.log", interval=0.01)

        class Slow:
            def get(self, *args, **kwargs):
                import time

                time.sleep(0.06)
                raise app.requests.exceptions.ReadTimeout("secret")

        try:
            with patch.object(console_module, "CONSOLE", console), patch("sys.stdout", stream):
                with self.assertRaises(app.requests.exceptions.ReadTimeout):
                    app.request(
                        FakeAPI(),
                        Slow(),
                        "get",
                        "https://downloader.yandex.ru/private",
                        "safe-label",
                    )
            text = (self.root / "console.log").read_text(encoding="utf-8")
            self.assertEqual(text.count("START"), 1)
            self.assertNotIn("[--------------------]", text)
            self.assertIn("ERROR ReadTimeout", text)
            self.assertNotIn("private", text)
            self.assertEqual(console.active, {})
        finally:
            console.close()

    def preview_api(self):
        import io

        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (32, 16), "blue").save(buffer, format="JPEG")
        payload = buffer.getvalue()

        class PreviewAPI(FakeAPI):
            calls = 0

            def __init__(self):
                super().__init__([Response(payload)])
                self.session = self.content

            def api(self, *args):
                self.calls += 1
                return {
                    "meta": {
                        "sizes": [
                            {"name": "S", "url": "https://downloader.yandex.ru/preview/small"},
                            {"name": "XXXL", "url": "https://downloader.yandex.ru/preview/large"},
                            {
                                "name": "ORIGINAL",
                                "url": "https://downloader.yandex.ru/disk/original",
                            },
                        ]
                    }
                }

        return (PreviewAPI(), payload)

    def test_preview_saved_without_prefix_without_completing_original(self):
        api, payload = self.preview_api()
        row = self.row()
        result = app.save_preview(api, row, self.root)
        saved = self.conn.execute("SELECT * FROM previews").fetchone()
        self.assertIn("XXXL 32x16", result)
        self.assertEqual(Path(saved["relpath"]).parent, Path("previews"))
        self.assertFalse(Path(saved["relpath"]).name.startswith("##"))
        self.assertEqual((self.root / saved["relpath"]).read_bytes(), payload)
        self.assertEqual(api.content.urls, ["https://downloader.yandex.ru/preview/large"])
        self.assertEqual(self.conn.execute("SELECT status FROM resources").fetchone()[0], "pending")

    def test_existing_preview_is_not_downloaded_again(self):
        api, _ = self.preview_api()
        row = self.row()
        app.save_preview(api, row, self.root)
        api2, _ = self.preview_api()
        result = app.save_preview(api2, row, self.root)
        self.assertIn("уже есть", result)
        self.assertEqual(api2.calls, 0)
        self.assertEqual(api2.content.urls, [])

    def test_heic_preview_gets_real_jpeg_extension(self):
        api, _ = self.preview_api()
        item = resource()
        item["name"] = "IMG_0001.HEIC"
        row = self.row(item)
        app.save_preview(api, row, self.root)
        rel = self.conn.execute("SELECT relpath FROM previews").fetchone()[0]
        self.assertTrue(rel.endswith(".HEIC.jpg"))

    def test_verify_removes_preview_after_original_passes_check(self):
        api, _ = self.preview_api()
        row = self.row()
        app.save_preview(api, row, self.root)
        saved_path = self.root / self.conn.execute("SELECT relpath FROM previews").fetchone()[0]
        original = self.root / row["relpath"]
        original.parent.mkdir()
        original.write_bytes(DATA)
        self.conn.execute("UPDATE resources SET status='verified'")
        self.conn.commit()
        app.verify(self.conn, self.root)
        with patch("builtins.print"):
            app.report(self.conn, self.root)
        saved = json.loads((self.root / "previews.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, [])
        self.assertFalse(saved_path.exists())

    def test_missing_completed_original_is_requeued(self):
        self.row()
        self.conn.execute("UPDATE resources SET status='verified'")
        self.conn.commit()
        app.verify(self.conn, self.root)
        self.assertEqual(self.conn.execute("SELECT status FROM resources").fetchone()[0], "failed")

    def test_console_keeps_request_details_out_of_terminal(self):
        import io

        stream = io.StringIO()
        console = app.Console(self.root / "console.log")
        try:
            with patch("sys.stdout", stream):
                console.event("REQ 00001 HTTP 200")
                wid = console.worker("photo.jpg", "1/4")
                console.file_done(wid, "photo.jpg", "OK оригинал")
            self.assertNotIn("REQ", stream.getvalue())
            self.assertEqual(stream.getvalue().count("photo.jpg"), 1)
            self.assertTrue(stream.getvalue().startswith("W01 │"))
        finally:
            console.close()

    def test_only_missing_does_not_attempt_unseen_photos(self):
        self.row(resource("/photounlim/a.jpg"))
        self.row(resource("/photounlim/b.jpg"))
        self.conn.execute("UPDATE resources SET status='deferred' WHERE path='/photounlim/b.jpg'")
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(1,'list',1)")
        app.bind_account(self.conn, "123", self.root)
        calls = []

        class Client:
            def __init__(self, *args):
                pass

            def login(self):
                return "123"

            def close(self):
                pass

        def fetch(api, row, *args):
            calls.append(row["path"])
            return ("sha", "verified")

        with patch.object(engine, "Yandex", Client), patch.object(engine, "download_one", fetch):
            app.download(
                self.conn,
                self.root,
                None,
                options=app.DownloadOptions(
                    workers=1, limit=None, attempts=1, date_policy="none", only_missing=True
                ),
                stop=threading.Event(),
            )
        self.assertEqual(calls, ["/photounlim/b.jpg"])

    def test_normal_limited_run_prioritizes_unseen_photos_in_order(self):
        for path in ("a", "b", "c"):
            self.row(resource("/photounlim/" + path + ".jpg"))
        self.conn.execute("UPDATE resources SET status='deferred' WHERE path='/photounlim/a.jpg'")
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(1,'list',1)")
        app.bind_account(self.conn, "123", self.root)
        calls = []

        class Client:
            def __init__(self, *args):
                pass

            def login(self):
                return "123"

            def close(self):
                pass

        def fetch(api, row, *args):
            calls.append(row["path"])
            return ("sha", "verified")

        with patch.object(engine, "Yandex", Client), patch.object(engine, "download_one", fetch):
            app.download(
                self.conn,
                self.root,
                None,
                options=app.DownloadOptions(workers=1, limit=2, attempts=1, date_policy="none"),
                stop=threading.Event(),
            )
        self.assertEqual(calls, ["/photounlim/b.jpg", "/photounlim/c.jpg"])

    def test_recovered_original_completes_queue_and_removes_preview(self):
        api, _ = self.preview_api()
        row = self.row()
        app.save_preview(api, row, self.root)
        preview_path = self.root / self.conn.execute("SELECT relpath FROM previews").fetchone()[0]
        self.conn.execute("UPDATE resources SET status='deferred'")
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(1,'list',1)")
        app.bind_account(self.conn, "123", self.root)

        class Client(FakeAPI):
            def __init__(self, *args):
                super().__init__()

            def login(self):
                return "123"

            def close(self):
                pass

        with patch.object(engine, "Yandex", Client):
            app.download(
                self.conn,
                self.root,
                None,
                options=app.DownloadOptions(
                    workers=1, limit=None, attempts=1, date_policy="none", only_missing=True
                ),
                stop=threading.Event(),
            )
        self.assertEqual(
            self.conn.execute("SELECT status FROM resources").fetchone()[0], "verified"
        )
        self.assertEqual((self.root / row["relpath"]).read_bytes(), DATA)
        with patch("builtins.print"):
            app.report(self.conn, self.root)
        previews = json.loads((self.root / "previews.json").read_text(encoding="utf-8"))
        self.assertEqual(previews, [])
        self.assertFalse(preview_path.exists())

    def test_corrupt_original_does_not_allow_preview_deletion(self):
        api, _ = self.preview_api()
        row = self.row()
        app.save_preview(api, row, self.root)
        preview_path = self.root / self.conn.execute("SELECT relpath FROM previews").fetchone()[0]
        original = self.root / row["relpath"]
        original.parent.mkdir()
        original.write_bytes(b"x" * len(DATA))
        self.conn.execute("UPDATE resources SET status='verified'")
        self.conn.commit()
        app.verify(self.conn, self.root)
        self.assertTrue(preview_path.exists())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM previews").fetchone()[0], 1)

    def test_preview_rename_updates_catalog_and_preserves_collision(self):
        api, payload = self.preview_api()
        row = self.row()
        app.save_preview(api, row, self.root)
        preview = self.conn.execute("SELECT relpath FROM previews").fetchone()[0]
        original = self.root / preview
        legacy = original.with_name("##" + original.name)
        original.rename(legacy)
        self.conn.execute("UPDATE previews SET relpath=?", (str(legacy.relative_to(self.root)),))
        self.conn.commit()
        original.write_bytes(b"unrelated existing file")
        self.assertEqual(app.normalize_preview_names(self.conn, self.root), 1)
        renamed = self.root / self.conn.execute("SELECT relpath FROM previews").fetchone()[0]
        self.assertFalse(renamed.name.startswith("##"))
        self.assertEqual(renamed.read_bytes(), payload)
        self.assertEqual(original.read_bytes(), b"unrelated existing file")
        self.assertEqual(app.normalize_preview_names(self.conn, self.root), 0)

    def test_verify_removes_missing_preview_record(self):
        api, _ = self.preview_api()
        row = self.row()
        app.save_preview(api, row, self.root)
        path = self.root / self.conn.execute("SELECT relpath FROM previews").fetchone()[0]
        path.unlink()
        app.verify(self.conn, self.root)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM previews").fetchone()[0], 0)

    def test_valid_original_is_never_downloaded_again(self):
        row = self.row()
        sha, status = app.download_one(FakeAPI(), row, self.root, "none")
        self.conn.execute("UPDATE resources SET status=?,local_sha256=?", (status, sha))
        self.conn.execute("INSERT INTO scans(id,source,complete) VALUES(1,'list',1)")
        app.bind_account(self.conn, "123", self.root)
        with patch.object(engine, "Yandex") as client:
            app.download(
                self.conn,
                self.root,
                None,
                options=app.DownloadOptions(workers=1, limit=None, attempts=1, date_policy="none"),
                stop=threading.Event(),
            )
            client.assert_not_called()

    def test_summary_does_not_count_rescued_preview_as_error(self):
        rows = {
            name: self.row(resource("/photounlim/" + name + ".jpg"))
            for name in ("original", "preview", "error", "pending")
        }
        self.conn.execute(
            "UPDATE resources SET status='verified' WHERE path=?", (rows["original"]["path"],)
        )
        self.conn.execute(
            "UPDATE resources SET status='deferred' WHERE path=?", (rows["preview"]["path"],)
        )
        self.conn.execute(
            "UPDATE resources SET status='failed' WHERE path=?", (rows["error"]["path"],)
        )
        self.conn.commit()
        api, _ = self.preview_api()
        app.save_preview(api, rows["preview"], self.root)
        with patch("builtins.print"):
            app.report(self.conn, self.root)
        data = json.loads((self.root / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(
            data["summary"],
            {"total": 4, "originals": 1, "previews": 1, "errors": 1, "unprocessed": 1},
        )
        # The failed original is retried even though its preview has been saved.
        missing = json.loads((self.root / "missing_originals.json").read_text(encoding="utf-8"))
        self.assertEqual(len(missing), 3)

    def test_server_stats_count_attempts_and_only_complete_bodies(self):
        log = self.root / "console.log"
        log.write_text(
            "\n".join(
                [
                    "12:00:00 REQ 00001 START a.jpg GET s175klg.storage.yandex.net",
                    "12:00:01 REQ 00001 HTTP 200 | s175klg.storage.yandex.net",
                    (
                        "12:00:02 REQ 00001 ERROR ChunkedEncodingError | a.jpg "
                        "GET s175klg.storage.yandex.net"
                    ),
                    "12:00:03 REQ 00002 START a.jpg GET s175klg.storage.yandex.net",
                    (
                        "12:00:04 REQ 00002 BODY OK [###] 100.0% 1 MiB | a.jpg "
                        "GET s175klg.storage.yandex.net"
                    ),
                    "12:00:05 REQ 00003 START b.jpg GET s20nrg.storage.yandex.net",
                    "12:00:06 REQ 00003 ERROR ReadTimeout | b.jpg GET s20nrg.storage.yandex.net",
                    "12:00:07 REQ 00004 START b.jpg GET 1.downloader.disk.yandex.ru",
                    "12:00:08 REQ 00004 BODY OK 100.0% | b.jpg GET 1.downloader.disk.yandex.ru",
                    "12:00:09 REQ 00003 [----] waiting | b.jpg GET s20nrg.storage.yandex.net",
                ]
            ),
            encoding="utf-8",
        )
        stats = app.ServerStats(log, self.root / "server_stats.txt")
        self.assertEqual(stats.counts, {"s175klg": [2, 1], "s20nrg": [1, 0]})
        rebuilt = app.ServerStats(log, self.root / "server_stats.txt")
        self.assertEqual(rebuilt.counts, stats.counts)
        text = (self.root / "server_stats.txt").read_text(encoding="utf-8-sig")
        self.assertIn("s175klg", text)
        self.assertNotIn("downloader.disk", text)

    def test_server_stats_update_live_and_survive_console_restart(self):
        path = self.root / "console.log"
        console = app.Console(path)
        try:
            console.event("REQ 00001 START a.jpg GET s175klg.storage.yandex.net")
            console.event("REQ 00001 BODY OK 100.0% | a.jpg GET s175klg.storage.yandex.net")
            self.assertEqual(console.server_stats.counts, {"s175klg": [1, 1]})
        finally:
            console.close()
        console = app.Console(path)
        try:
            self.assertEqual(console.server_stats.counts, {"s175klg": [1, 1]})
            console.event("REQ 00001 START b.jpg GET s175klg.storage.yandex.net")
            self.assertEqual(console.server_stats.counts, {"s175klg": [2, 1]})
        finally:
            console.close()

    def test_video_scan_keeps_photo_status_and_separate_paths(self):
        photo = self.row()
        self.conn.execute("UPDATE resources SET status='verified'")
        self.conn.commit()
        video = resource("/photounlim/a.mp4")
        video["name"] = "a.mp4"
        video["meta"]["mimetype"] = "video/mp4"
        self.assertTrue(app.save_resource(self.conn, 2, video, media="video"))
        saved = self.conn.execute("SELECT * FROM resources WHERE media_kind='video'").fetchone()
        self.assertEqual(Path(saved["relpath"]).parent, Path("videos"))
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM resources WHERE path=?", (photo["path"],)
            ).fetchone()[0],
            "verified",
        )

    def test_video_original_uses_same_md5_check(self):
        video = resource("/photounlim/a.mp4")
        video["name"] = "a.mp4"
        video["meta"]["mimetype"] = "video/mp4"
        app.save_resource(self.conn, 1, video, media="video")
        self.conn.commit()
        row = dict(self.conn.execute("SELECT * FROM resources").fetchone())
        sha, status = app.download_one(FakeAPI(), row, self.root, "none")
        self.assertEqual(status, "verified")
        self.assertEqual((self.root / row["relpath"]).read_bytes(), DATA)
        self.assertFalse((self.root / "photos").exists())

    def test_video_poster_is_not_saved_as_video(self):
        row = self.row()
        row["media_kind"] = "video"
        api = FakeAPI()
        with patch.object(engine, "save_preview") as save:
            app.try_preview(api, row, self.root)
            save.assert_not_called()

    def test_valid_range_appends_only_requested_bytes(self):
        part = self.root / "video.part"
        part.write_bytes(DATA[:5])
        headers = {
            "Content-Length": str(len(DATA) - 5),
            "Content-Range": f"bytes 5-{len(DATA) - 1}/{len(DATA)}",
        }
        api = FakeAPI([Response(DATA[5:], status=206, headers=headers)])
        app.transfer(
            api, "https://downloader.yandex.ru/file", part, resume=True, expected_size=len(DATA)
        )
        self.assertEqual(part.read_bytes(), DATA)

    def test_ignored_range_restarts_instead_of_appending_full_file(self):
        part = self.root / "video.part"
        part.write_bytes(DATA[:5])
        app.transfer(
            FakeAPI(),
            "https://downloader.yandex.ru/file",
            part,
            resume=True,
            expected_size=len(DATA),
        )
        self.assertEqual(part.read_bytes(), DATA)

    def test_wrong_range_does_not_modify_partial_file(self):
        part = self.root / "video.part"
        part.write_bytes(DATA[:5])
        api = FakeAPI(
            [
                Response(
                    DATA[5:],
                    status=206,
                    headers={"Content-Range": f"bytes 6-{len(DATA) - 1}/{len(DATA)}"},
                )
            ]
        )
        with self.assertRaises(app.IntegrityError):
            app.transfer(
                api, "https://downloader.yandex.ru/file", part, resume=True, expected_size=len(DATA)
            )
        self.assertEqual(part.read_bytes(), DATA[:5])

    def test_video_range_is_enabled_only_with_matching_identity(self):
        video = resource("/photounlim/a.mp4")
        video["name"] = "a.mp4"
        video["meta"]["mimetype"] = "video/mp4"
        app.save_resource(self.conn, 1, video, media="video")
        self.conn.commit()
        row = dict(self.conn.execute("SELECT * FROM resources").fetchone())
        part = app.auxiliary_path(self.root, row["path"], "part")
        part.parent.mkdir()
        part.write_bytes(DATA[:5])
        app.atomic_json(
            part.with_name(part.name + ".identity.json"),
            {"path": row["path"], "md5": row["md5"], "size": row["size"]},
        )
        api = FakeAPI(
            [
                Response(
                    DATA[5:],
                    status=206,
                    headers={
                        "Content-Length": str(len(DATA) - 5),
                        "Content-Range": f"bytes 5-{len(DATA) - 1}/{len(DATA)}",
                    },
                )
            ]
        )
        _, status = app.download_one(api, row, self.root, "none")
        self.assertEqual(status, "verified")

    def test_upgrade_preview_replaces_only_with_more_pixels(self):
        api, _ = self.preview_api()
        row = self.row()
        app.save_preview(api, row, self.root)
        self.conn.execute("UPDATE previews SET variant='XL'")
        self.conn.commit()
        small_api, payload = self.preview_api()
        small_api.refresh_previews = True
        result = app.save_preview(small_api, row, self.root)
        self.assertIn("сохранено XL", result)
        saved = self.conn.execute("SELECT * FROM previews").fetchone()
        self.assertEqual((self.root / saved["relpath"]).read_bytes(), payload)

    def test_upgrade_preview_can_recover_larger_variant(self):
        import io

        from PIL import Image

        api, _ = self.preview_api()
        row = self.row()
        app.save_preview(api, row, self.root)
        self.conn.execute("UPDATE previews SET variant='XL'")
        self.conn.commit()
        bigger = io.BytesIO()
        Image.new("RGB", (64, 32)).save(bigger, format="JPEG")
        api, _ = self.preview_api()
        api.content = Content([Response(bigger.getvalue())])
        api.session = api.content
        api.refresh_previews = True
        app.save_preview(api, row, self.root)
        saved = self.conn.execute("SELECT * FROM previews").fetchone()
        self.assertEqual((saved["width"], saved["height"], saved["variant"]), (64, 32, "XXXL"))

    def test_video_download_ignores_unrelated_photo_verification(self):
        photo = self.row()
        self.conn.execute("UPDATE resources SET status='verified'")
        video = resource("/photounlim/a.mp4")
        video["name"] = "a.mp4"
        video["meta"]["mimetype"] = "video/mp4"
        app.save_resource(self.conn, 2, video, media="video")
        self.conn.execute("INSERT INTO scans(id,source,complete,media) VALUES(2,'list',1,'video')")
        app.bind_account(self.conn, "123", self.root)

        class Client(FakeAPI):
            def __init__(self, *args):
                super().__init__()

            def login(self):
                return "123"

            def close(self):
                pass

        with patch.object(engine, "Yandex", Client):
            app.download(
                self.conn,
                self.root,
                None,
                options=app.DownloadOptions(
                    workers=1, limit=None, attempts=1, date_policy="none", media="video"
                ),
                stop=threading.Event(),
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM resources WHERE path=?", (photo["path"],)
            ).fetchone()[0],
            "verified",
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM resources WHERE media_kind='video'").fetchone()[
                0
            ],
            "verified",
        )

    def test_busy_server_stats_file_does_not_stop_request_logging(self):
        stats = app.ServerStats(self.root / "console.log", self.root / "server_stats.txt")
        with (
            patch.object(app.os, "replace", side_effect=PermissionError("busy")),
            patch.object(app.time, "sleep"),
        ):
            stats.consume("REQ 1 START a.jpg GET s175klg.storage.yandex.net", save=False)
            stats.save()
        self.assertTrue(stats.dirty)
        self.assertEqual(stats.counts, {"s175klg": [1, 0]})
        stats.save()
        self.assertFalse(stats.dirty)

    def test_changed_video_identity_discards_incompatible_partial(self):
        video = resource("/photounlim/a.mp4")
        video["name"] = "a.mp4"
        video["meta"]["mimetype"] = "video/mp4"
        app.save_resource(self.conn, 1, video, media="video")
        self.conn.commit()
        row = dict(self.conn.execute("SELECT * FROM resources").fetchone())
        part = app.auxiliary_path(self.root, row["path"], "part")
        part.parent.mkdir()
        part.write_bytes(b"wrong")
        app.atomic_json(
            part.with_name(part.name + ".identity.json"),
            {"path": row["path"], "md5": "0" * 32, "size": row["size"]},
        )
        api = FakeAPI()
        captured = []
        real_get = api.content.get

        def get(*args, **kwargs):
            captured.append(kwargs.get("headers"))
            return real_get(*args, **kwargs)

        api.content.get = get
        app.download_one(api, row, self.root, "none")
        self.assertEqual(captured, [{}])


if __name__ == "__main__":
    unittest.main()
