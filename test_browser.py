"""Real Chromium regressions against an isolated local archive, with no Yandex access."""

import json
import os
import re

import pytest
from playwright.sync_api import expect

import test_web
import yaphoto


@pytest.fixture(scope="session", autouse=True)
def browser_expect_timeout():
    # Allow cold browser/process starts on busy Windows and CI machines.
    expect.set_options(timeout=15000)


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    executable = os.environ.get("YAPHOTO_CHROMIUM")
    return {**browser_type_launch_args, **({"executable_path": executable} if executable else {})}


@pytest.fixture
def archive(page):
    fixture = test_web.WebTests(methodName="runTest")
    fixture.setUp()
    try:
        yield fixture
    finally:
        # Stop polling and image requests before deleting their temporary files on Windows.
        try:
            page.close()
        finally:
            fixture.tearDown()


def enter(page, archive):
    page.goto(archive.base + "/?key=" + archive.app.access_token)
    expect(page.locator("#status")).to_have_text("Готов к работе")
    expect(page.locator("#total")).to_have_text("3")


def test_gallery_filter_viewer_and_csp(page, archive):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    enter(page, archive)
    page.evaluate(
        (
            "window.cspErrors=[]; "
            "document.addEventListener('securitypolicyviolation', e "
            "=> window.cspErrors.push(e.violatedDirective))"
        )
    )
    expect(page.locator(".tile")).to_have_count(2)
    expect(page.locator(".tile.original img")).to_have_attribute("src", re.compile("^blob:"))
    page.locator('[data-filter="preview"]').click()
    expect(page.locator(".tile")).to_have_count(1)
    page.locator(".tile").press("Enter")
    assert errors == []
    expect(page.locator("#viewer")).to_be_visible()
    expect(page.locator("#viewer-name")).to_have_text("2024-01-03.JPG")
    expect(page.locator("#viewer-info")).to_contain_text("Сохранённое превью")
    expect(page.locator("#viewer-media img")).to_have_js_property("complete", True)
    page.locator("#close-viewer").click()
    expect(page.locator("#viewer")).not_to_be_visible()
    assert errors == []
    assert page.evaluate("window.cspErrors") == []


def test_media_selection_and_timeline_keyboard(page, archive):
    enter(page, archive)
    page.locator("#scale").press("End")
    expect(page.locator('#gallery > section[data-date="2024-01-03"]')).to_be_visible()
    page.locator("#video").check()
    page.locator("#photo").uncheck()
    expect(page.locator(".tile")).to_have_count(1)
    page.locator("details > summary").click()
    expect(page.locator('[data-command="upgrade-previews"]')).to_be_disabled()
    expect(page.locator(".tile-name")).to_have_text("2023-05-02.MP4")


def test_pagination_has_no_duplicates(page, archive):
    conn = yaphoto.connect(archive.app.output / "catalog.sqlite3")
    try:
        conn.executemany(
            (
                "INSERT INTO "
                "resources(path,name,relpath,status,media_kind,raw_json) "
                "VALUES(?,?,?,'pending','photo','{}')"
            ),
            [(f"new-{i}", f"2022-01-01 {i:03}.jpg", f"photos/new-{i}.jpg") for i in range(70)],
        )
        conn.commit()
    finally:
        conn.close()
    page.goto(archive.base + "/?key=" + archive.app.access_token)
    expect(page.locator(".tile")).to_have_count(60)
    page.locator("#scroll").evaluate("el => el.scrollTop = el.scrollHeight")
    expect(page.locator(".tile")).to_have_count(72)
    identifiers = page.locator(".tile").evaluate_all("nodes => nodes.map(n => n.dataset.id)")
    assert len(set(identifiers)) == 72


def test_cookie_upload_validation_and_authenticated_post(page, archive):
    enter(page, archive)
    archive.app.cookies.write_text("unchanged", encoding="utf-8")
    page.locator("#cookies").set_input_files(
        {"name": "cookies.json", "mimeType": "application/json", "buffer": b"[]"}
    )
    expect(page.locator("#toast")).to_contain_text("непустой JSON-массив")
    assert archive.app.cookies.read_text(encoding="utf-8") == "unchanged"
    cookies = [{"name": "synthetic", "value": "test-only", "domain": ".yandex.ru"}]
    page.locator("#cookies").set_input_files(
        {
            "name": "cookies.json",
            "mimeType": "application/json",
            "buffer": json.dumps(cookies).encode(),
        }
    )
    expect(page.locator("#cookie-note")).to_contain_text("Cookies загружены")
    assert json.loads(archive.app.cookies.read_text(encoding="utf-8")) == cookies


def test_progress_polling_and_mobile_layout(page, archive):
    enter(page, archive)
    archive.app.job = {"running": True, "command": "download", "code": None}
    archive.app.progress.write_text(
        json.dumps(
            {
                "overall": "ОБРАБОТАНО 50.0% 1/2",
                "workers": {"1": {"name": "test.jpg", "stage": "загрузка", "attempt": "1/4"}},
            }
        ),
        encoding="utf-8",
    )
    expect(page.locator("#percentage")).to_have_text("50.0%")
    expect(page.locator("#worker-list")).to_contain_text("W1")
    expect(page.locator("#start")).to_be_disabled()
    expect(page.locator("#stop")).to_be_enabled()
    archive.app.job = {"running": False, "command": "download", "code": 0}
    expect(page.locator("#status")).to_have_text("Завершено")
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_reload_during_job_retries_loading_catalog(page, archive):
    enter(page, archive)
    archive.app.job = {"running": True, "command": "download", "code": None}
    requests = []

    def catalog(route):
        requests.append(route.request.url)
        if len(requests) <= 2:
            route.fulfill(status=202, json={"loading": True})
        else:
            route.continue_()

    page.route("**/api/catalog?*", catalog)
    page.reload()
    expect(page.locator("#status")).to_have_text("Скачивание")
    expect(page.locator("#total")).to_have_text("3")
    expect(page.locator(".tile")).to_have_count(2)
    expect(page.locator(".tile.original img")).to_have_attribute("src", re.compile("^blob:"))
    expect(page.locator("#sentinel")).to_have_text("Всё показано")
    assert len(requests) >= 3


def test_catalog_failure_preserves_tiles_and_recovers_without_reload(page, archive):
    enter(page, archive)
    expect(page.locator(".tile")).to_have_count(2)
    failed = []

    def catalog(route):
        if not failed:
            failed.append(True)
            route.fulfill(status=503, json={"error": "Каталог временно недоступен"})
        else:
            route.continue_()

    page.route("**/api/catalog?*", catalog)
    page.locator('[data-filter="preview"]').click()
    expect(page.locator("#sentinel")).to_contain_text("Повторяем")
    expect(page.locator(".tile")).to_have_count(2)
    expect(page.locator("#total")).to_have_text("3")
    expect(page.locator(".tile")).to_have_count(1)
    expect(page.locator(".tile.preview")).to_have_count(1)


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_month_jump_keeps_both_pagination_directions(page, archive, order):
    conn = yaphoto.connect(archive.app.output / "catalog.sqlite3")
    try:
        conn.execute("DELETE FROM previews")
        conn.execute("DELETE FROM resources")
        conn.executemany(
            "INSERT INTO resources(path,name,relpath,status,media_kind,raw_json) "
            "VALUES(?,?,?,'pending','photo','{}')",
            [
                (f"{month}-{i}", f"2035-{month}-01 {i:03}.jpg", f"photos/{month}-{i}.jpg")
                for month in ("10", "11", "12")
                for i in range(80)
            ],
        )
        conn.commit()
    finally:
        conn.close()
    archive.app.rows()
    page.goto(archive.base + "/?key=" + archive.app.access_token)
    expect(page.locator("#total")).to_have_text("240")
    page.locator("#gallery-order").select_option(order)
    page.locator('.tick[data-month="2035-11"]').click()
    target = page.locator('#gallery > section[data-date="2035-11-01"]')
    expect(target).to_be_visible()
    # The selected month stays at the viewport while a preceding page is inserted.
    expect(page.locator(".tile").nth(119)).to_be_attached()
    expect(target.locator(".tile").first).to_be_in_viewport()
    first = "2035-10-01" if order == "asc" else "2035-12-01"
    last = "2035-12-01" if order == "asc" else "2035-10-01"
    page.locator("#scroll").evaluate("el => el.scrollTop = 0")
    expect(page.locator("#previous")).to_be_hidden()
    expect(page.locator("#gallery > section").first).to_have_attribute("data-date", first)
    for _ in range(3):
        count = page.locator(".tile").count()
        if count == 240:
            break
        page.locator("#scroll").evaluate("el => el.scrollTop = el.scrollHeight")
        expect(page.locator(".tile")).to_have_count(min(240, count + 60))
    expect(page.locator(".tile")).to_have_count(240)
    expect(page.locator("#gallery > section").last).to_have_attribute("data-date", last)
    ids = page.locator(".tile").evaluate_all("nodes => nodes.map(n => n.dataset.id)")
    assert len(set(ids)) == 240


def test_timeline_year_labels_do_not_overlap(page, archive):
    conn = yaphoto.connect(archive.app.output / "catalog.sqlite3")
    try:
        conn.executemany(
            "INSERT INTO resources(path,name,relpath,status,media_kind,raw_json) "
            "VALUES(?,?,?,'pending','photo','{}')",
            [(str(year), f"{year}-01-01.jpg", f"photos/{year}.jpg") for year in range(2000, 2036)],
        )
        conn.commit()
    finally:
        conn.close()
    archive.app.rows()
    page.goto(archive.base + "/?key=" + archive.app.access_token)
    expect(page.locator("#total")).to_have_text("39")
    positions = page.locator(".tick.year").evaluate_all(
        "nodes => nodes.filter(n => getComputedStyle(n).visibility === 'visible')"
        ".map(n => n.getBoundingClientRect().top)"
    )
    assert len(positions) >= 5
    assert all(b - a >= 29 for a, b in zip(positions, positions[1:]))
    assert page.locator("#timeline").bounding_box()["width"] >= 70


def test_worker_bars_reuse_nodes_and_keep_summary(page, archive):
    enter(page, archive)
    page.locator("details > summary").click()
    page.locator("#workers").fill("32")
    expect(page.locator("#workers")).to_have_attribute("max", "32")
    archive.app.job = {"running": True, "command": "download", "code": None}
    summary = "ОБРАБОТАНО 50.0% 16/32 | Всего: 32 · Успешно: 10 · Ошибок: 2 · Превью: 4"
    archive.app.progress.write_text(
        json.dumps(
            {
                "overall": summary,
                "workers": {"1": {"name": "test.jpg", "stage": "загрузка", "attempt": "1/4"}},
                "requests": {"1": {"worker": 1, "phase": "чтение", "done": 50, "total": 100}},
            }
        ),
        encoding="utf8",
    )
    expect(page.locator("#overall")).to_have_text(summary)
    expect(page.locator(".worker progress")).to_have_js_property("value", 50)
    page.locator(".worker").evaluate("el => el.dataset.reused = 'yes'")
    archive.app.progress.write_text(
        json.dumps(
            {
                "overall": summary,
                "workers": {"1": {"name": "test.jpg", "stage": "загрузка", "attempt": "1/4"}},
                "requests": {"1": {"worker": 1, "phase": "чтение", "done": 75, "total": 100}},
            }
        ),
        encoding="utf8",
    )
    expect(page.locator(".worker progress")).to_have_js_property("value", 75)
    expect(page.locator(".worker")).to_have_attribute("data-reused", "yes")
    expect(page.locator("#overall")).to_have_text(summary)
    archive.app.job = {"running": False, "command": "download", "code": 0}
    expect(page.locator("#status")).to_have_text("Завершено")
    expect(page.locator("#overall")).to_have_text(summary)
    expect(page.locator("#percentage")).to_have_text("50.0%")
