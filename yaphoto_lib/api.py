"""Api implementation for YaPhoto."""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import requests

from . import console
from .console import log_event
from .errors import AuthError, ExportError, IntegrityError, RemoteError, StorageUnavailable

BASE = "https://disk.yandex.ru"


def request(
    api: Yandex, session: requests.Session, verb: str, url: str, label: str, **kwargs: Any
) -> requests.Response:
    """Instrument headers wait; streamed body keeps the same request ID until consumed."""
    key = (
        console.CONSOLE.begin(
            f"{getattr(api, 'label', '')} {verb.upper()} {label}".strip(),
            getattr(api, "worker_id", 0),
        )
        if console.CONSOLE
        else None
    )
    try:
        response = cast(requests.Response, getattr(session, verb.lower())(url, **kwargs))
    except BaseException as exc:
        if console.CONSOLE:
            console.CONSOLE.finish(key, f"ERROR {type(exc).__name__}")
        raise
    if console.CONSOLE:
        console.CONSOLE.event(f"REQ {key:05d} HTTP {response.status_code} | {label}")
        if kwargs.get("stream"):
            console.CONSOLE.update(key, phase=f"HTTP {response.status_code}; чтение тела")
        else:
            console.CONSOLE.finish(key, f"HTTP {response.status_code}")
    setattr(response, "_yaphoto_request_id", key)
    return response


class Cooldown:
    """Shared account-wide pause for API throttling."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.until = 0.0

    def defer(self, seconds: float) -> None:
        with self.lock:
            self.until = max(self.until, time.monotonic() + seconds)
        log_event(f"COOLDOWN: общая пауза {seconds:.0f}с по ограничению Яндекса")

    def wait(self) -> None:
        while True:
            with self.lock:
                delay = self.until - time.monotonic()
            if delay <= 0:
                return
            time.sleep(min(delay, 1))


def retry_after(value: str | None) -> float:
    if value is None:
        return 30
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            from email.utils import parsedate_to_datetime

            return max(0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return 30


def check_response(response: requests.Response, cooldown: Cooldown) -> None:
    if response.status_code == 429:
        cooldown.defer(max(30, retry_after(response.headers.get("Retry-After"))))
        raise RemoteError("HTTP 429: лимит запросов; включена общая пауза")
    if response.status_code in (401, 403):
        raise AuthError(f"HTTP {response.status_code}: доступ отклонён; проверьте сессию")
    if response.status_code >= 400:
        raise RemoteError(f"HTTP {response.status_code}")


def cookies_from_file(path: Path | str) -> requests.cookies.RequestsCookieJar:
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list) or not raw:
        raise ExportError("cookies.json: ожидается непустой JSON-массив Cookie-Editor")
    jar = requests.cookies.RequestsCookieJar()
    for item in raw:
        if not isinstance(item, dict):
            raise ExportError("cookies.json: элементы массива должны быть объектами")
        domain = item.get("domain", "")
        if not isinstance(domain, str):
            raise ExportError("cookies.json: domain должен быть строкой")
        if not (domain.lstrip(".") == "yandex.ru" or domain.endswith(".yandex.ru")):
            continue
        if item.get("name") and isinstance(item.get("value"), str):
            jar.set(
                item["name"],
                item["value"],
                domain=domain,
                path=item.get("path") or "/",
                secure=bool(item.get("secure", True)),
            )
    if not jar:
        raise ExportError("В cookies.json нет cookies домена yandex.ru")
    return jar


class Yandex:
    def __init__(
        self, jar: requests.cookies.RequestsCookieJar, cooldown: Cooldown | None = None
    ) -> None:
        self.session = requests.Session()
        self.session.cookies.update(jar)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"
                ),
                "Referer": BASE + "/client/photo",
                "Origin": BASE,
                "X-Requested-With": "XMLHttpRequest",
            }
        )
        self.content = requests.Session()
        self.content.headers.update(
            {"User-Agent": self.session.headers["User-Agent"], "Accept-Encoding": "identity"}
        )
        self.cooldown = cooldown or Cooldown()
        self.sk: str | None = None
        self.uid: str | None = None
        self.read_timeout = 30
        self.save_previews = True
        self.refresh_previews = False
        self.auto_upgrade_previews = False
        self.worker_id = 0
        self.label = ""
        self.preview_connection: sqlite3.Connection | None = None

    def close(self) -> None:
        self.session.close()
        self.content.close()

    def login(self) -> str:
        for attempt in range(4):
            try:
                self.cooldown.wait()
                with request(
                    self,
                    self.session,
                    "get",
                    BASE + "/client/disk",
                    "/client/disk",
                    timeout=(15, 60),
                ) as response:
                    check_response(response, self.cooldown)
                    if "passport.yandex" in response.url or "captcha" in response.url.lower():
                        raise AuthError(
                            "Требуется вход или капча в браузере и обновление cookies.json"
                        )
                    sk = re.search(r'"sk"\s*:\s*"([^"\s]+)"', response.text)
                    uid = re.search(r'"uid"\s*:\s*"?(\d+)"?', response.text)
                    if not sk or not uid:
                        raise AuthError(
                            "Не найдены sk/uid: проверьте вход в Диск; возможны изменения веб-API"
                        )
                    self.sk, self.uid = sk.group(1), uid.group(1)
                    return self.uid
            except (requests.RequestException, RemoteError) as exc:
                log_event(f"Вход: попытка {attempt + 1}/4, {type(exc).__name__}")
                if attempt == 3:
                    raise RemoteError("Не удалось открыть Диск после 4 попыток") from None
                time.sleep(2**attempt + random.random())

        raise RemoteError("Вход не завершён")

    def api(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        for attempt in range(4):
            try:
                self.cooldown.wait()
                body: dict[str, Any] = {
                    "sk": self.sk,
                    "connection_id": f"{self.uid}{time.time_ns()}",
                    "apiMethod": method,
                }
                if params is not None:
                    body["requestParams"] = params
                with request(
                    self,
                    self.session,
                    "post",
                    BASE + "/models-v2",
                    method,
                    params={"m": method},
                    json=body,
                    timeout=(15, 90),
                ) as response:
                    check_response(response, self.cooldown)
                    if "captcha" in response.url.lower():
                        raise AuthError("Яндекс требует капчу: откройте Диск в браузере")
                    try:
                        data = response.json()
                    except ValueError:
                        raise RemoteError(f"{method}: вместо JSON получен другой ответ") from None
                    if isinstance(data, dict) and data.get("error"):
                        # Do not include arbitrary server payloads: they may contain secrets.
                        raise RemoteError(f"{method}: API вернул ошибку")
                    if not isinstance(data, (dict, list)):
                        raise RemoteError(f"{method}: неожиданный формат ответа")
                    return data
            except (requests.RequestException, RemoteError) as exc:
                log_event(f"{method}: попытка {attempt + 1}/4, {type(exc).__name__}")
                if attempt == 3:
                    raise RemoteError(f"{method}: запрос не выполнен после 4 попыток") from None
                time.sleep(2**attempt + random.random())

        raise RemoteError("Запрос не завершён")

    def original_url(self, path: str) -> str:
        data = self.api("mpfs/url", {"path": path})
        url = data.get("file") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise RemoteError("mpfs/url: нет ссылки на оригинал")
        if url.startswith("//"):
            url = "https:" + url
        validate_url(url)
        return url


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or (
            not any(
                host == suffix or host.endswith("." + suffix)
                for suffix in ("yandex.ru", "yandex.net", "yandex.com", "yandexcloud.net")
            )
        )
    ):
        raise ExportError("Ссылка на файл ведёт за пределы разрешённых HTTPS-доменов Яндекса")


def transfer(
    api: Yandex,
    url: str,
    part: Path,
    session: requests.Session | None = None,
    resume: bool = False,
    expected_size: int | None = None,
    allow_documents: bool = False,
) -> None:
    # Use a cookie-free session for content, validate every redirect before following it.
    content = session if session is not None else api.content
    offset = Path(part).stat().st_size if resume and Path(part).is_file() else 0
    for _ in range(8):
        validate_url(url)
        api.cooldown.wait()
        host = urlsplit(url).hostname or ""
        try:
            response = request(
                api,
                content,
                "get",
                url,
                host,
                stream=True,
                allow_redirects=False,
                timeout=(10, getattr(api, "read_timeout", 15)),
                headers={"Range": f"bytes={offset}-"} if offset else {},
            )
        except (requests.Timeout, requests.ConnectionError):
            if host.endswith(".storage.yandex.net"):
                raise StorageUnavailable(
                    f"Сервер хранения {host} не ответил; файл отложен"
                ) from None
            raise RemoteError("Сервер скачивания не ответил") from None
        key = getattr(response, "_yaphoto_request_id", None)
        outcome = "ERROR"
        try:
            with response:
                if response.status_code in (301, 302, 303, 307, 308):
                    from urllib.parse import urljoin

                    url = urljoin(url, response.headers.get("Location", ""))
                    outcome = f"REDIRECT HTTP {response.status_code}"
                    continue
                if response.status_code in (401, 403):
                    raise RemoteError("Ссылка на оригинал отклонена; требуется свежая ссылка")
                if response.status_code == 416 and offset:
                    Path(part).unlink(missing_ok=True)
                    raise RemoteError("Сервер отклонил диапазон; следующая попытка начнётся заново")
                check_response(response, api.cooldown)
                if response.status_code not in (200, 206):
                    raise RemoteError("Ожидался HTTP 200 или корректный HTTP 206")
                if response.status_code == 206:
                    content_range = re.fullmatch(
                        r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
                    )
                    if (
                        not offset
                        or not content_range
                        or int(content_range[1]) != offset
                        or (int(content_range[2]) < offset)
                    ):
                        raise IntegrityError("Некорректный Content-Range; файл не дополнен")
                    if expected_size is not None and int(content_range[3]) != expected_size:
                        raise IntegrityError("Размер в Content-Range не совпал с каталогом")
                else:
                    offset = 0
                # Range ignored: overwrite, never append a full response.
                mime = response.headers.get("Content-Type", "").lower()
                if not allow_documents and any(
                    x in mime for x in ("text/html", "application/json", "text/xml")
                ):
                    raise IntegrityError("Вместо оригинала пришла страница или ответ API")
                length = response.headers.get("Content-Length")
                response_length = int(length) if length else None
                total = expected_size or (
                    response_length + offset if response_length is not None else None
                )
                if console.CONSOLE:
                    console.CONSOLE.update(
                        key, total=total, done=offset, phase="докачка" if offset else "скачивание"
                    )
                count = 0
                with open(part, "ab" if offset else "wb") as stream:
                    for chunk in response.iter_content(256 * 1024):
                        if chunk:
                            stream.write(chunk)
                            count += len(chunk)
                            if console.CONSOLE:
                                console.CONSOLE.update(key, done=count + offset)
                    stream.flush()
                    os.fsync(stream.fileno())
                if response_length is not None and response_length != count:
                    raise IntegrityError("Ответ оборвался: Content-Length не совпал")
                if console.CONSOLE and (not total):
                    console.CONSOLE.update(key, total=count + offset)
                outcome = "BODY OK"
                return
        except BaseException as exc:
            outcome = f"ERROR {type(exc).__name__}"
            raise
        finally:
            if console.CONSOLE:
                console.CONSOLE.finish(key, outcome)
    raise RemoteError("Слишком много перенаправлений")
