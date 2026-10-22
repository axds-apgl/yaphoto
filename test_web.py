import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image

import web
import yaphoto
from yaphoto_lib import cli


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = web.App(root / "backup", root / "cookies.json")
        conn = yaphoto.connect(self.app.output / "catalog.sqlite3")
        (self.app.output / "photos").mkdir()
        (self.app.output / "previews").mkdir()
        for i, (name, status) in enumerate(
            [
                ("2025-07-10 12-00-00.JPG", "verified"),
                ("2024-01-03.JPG", "failed"),
                ("2023-05-02.MP4", "pending"),
            ]
        ):
            rel = "photos/" + name
            conn.execute(
                (
                    "INSERT INTO "
                    "resources(path,name,relpath,status,media_kind,raw_json) VALUES(?,?,?,?,?,'{}')"
                ),
                (str(i), name, rel, status, "video" if i == 2 else "photo"),
            )
        Image.new("RGB", (100, 80), "red").save(self.app.output / "photos/2025-07-10 12-00-00.JPG")
        Image.new("RGB", (50, 40), "blue").save(self.app.output / "previews/2024-01-03.JPG")
        conn.execute(
            "INSERT INTO previews(path,relpath,width,height) VALUES(?,?,?,?)",
            ("1", "previews/2024-01-03.JPG", 50, 40),
        )
        conn.commit()
        conn.close()
        self.app.rows()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.headers = {"X-YaPhoto-Token": self.app.token}
        self.client = requests.Session()
        self.client.get(self.base + "/?key=" + self.app.access_token).raise_for_status()

    def tearDown(self):
        if self.app.process and self.app.process.poll() is None:
            try:
                self.app.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.app.process.kill()
                self.app.process.wait()
        self.server.shutdown()
        self.server.server_close()
        self.app.thumbs.shutdown()
        self.app.close_catalog()
        self.client.close()
        self.temp.cleanup()

    def test_catalog_pagination_filters_and_month_jump(self):
        data = self.client.get(self.base + "/api/catalog?limit=1").json()
        self.assertEqual(
            data["summary"],
            dict(total=3, photo=2, video=1, original=1, preview=1, error=0, pending=1),
        )
        self.assertEqual(data["next"], 1)
        self.assertNotIn("relpath", data["items"][0])
        self.assertNotIn("path", data["items"][0])
        page = self.client.get(self.base + "/api/catalog?month=2024-01&limit=1").json()
        self.assertEqual(page["items"][0]["state"], "preview")
        page = self.client.get(self.base + "/api/catalog?media=video").json()
        self.assertEqual(len(page["items"]), 1)

    def test_media_range_and_thumbnail(self):
        row = self.app.rows()[0]
        original = self.app.safe(row["relpath"]).read_bytes()
        response = self.client.get(
            self.base + "/api/media?id=" + row["id"], headers={"Range": "bytes=2-9"}
        )
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, original[2:10])
        response = self.client.get(
            self.base + "/api/media?id=" + row["id"], headers={"Range": "bytes=999999-"}
        )
        self.assertEqual(response.status_code, 416)
        for _ in range(30):
            response = self.client.get(self.base + "/api/thumb?id=" + row["id"])
            if response.status_code != 202:
                break
            time.sleep(0.1)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\xff\xd8"))

    def test_security_and_cookie_validation(self):
        self.assertEqual(self.client.post(self.base + "/api/stop", json={}).status_code, 403)
        self.assertEqual(
            self.client.get(self.base + "/", headers={"Host": "evil.example"}).status_code, 403
        )
        self.app.cookies.write_text("unchanged")
        response = self.client.post(
            self.base + "/api/cookies", json={"cookies": []}, headers=self.headers
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.app.cookies.read_text(), "unchanged")
        self.assertFalse(self.app.cookies.with_suffix(".upload").exists())
        with self.assertRaises(ValueError):
            self.app.safe("../outside")

    def test_cli_verify_job_and_no_cookies_in_state(self):
        response = self.client.post(
            self.base + "/api/start",
            json={"command": "verify", "media": "all"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        deadline = time.monotonic() + 30
        while True:
            state = self.client.get(self.base + "/api/state").json()
            if not state["running"] or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        self.assertFalse(state["running"], state)
        self.assertTrue(any("ИТОГ" in line for line in state["log"]))
        self.assertNotIn(self.app.token, json.dumps(state))

    def test_download_stop_bridge(self):
        stop_file = Path(self.temp.name) / "stop-request"
        stop_file.touch()
        observed = []

        def fake_download(*args, stop, **kwargs):
            observed.append(stop.wait(3))

        with (
            patch.dict("os.environ", {"YAPHOTO_STOP": str(stop_file)}),
            patch.object(cli, "cookies_from_file", return_value={}),
            patch.object(cli, "Yandex"),
            patch.object(cli, "bind_account"),
            patch.object(cli, "download", side_effect=fake_download),
        ):
            code = yaphoto.main(["download", "--output", str(self.app.output)])
        self.assertEqual(observed, [True])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
