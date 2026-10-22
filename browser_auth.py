"""Loopback browser authentication; does not isolate files from their OS owner."""

import secrets
from http.cookies import CookieError, SimpleCookie

COOKIE_NAME = "yaphoto_session"


def same_token(secret: str, value: str | None) -> bool:
    return secrets.compare_digest(secret.encode("utf-8"), (value or "").encode("utf-8"))


def authorized(secret: str, cookie_header: str | None, authorization: str | None) -> bool:
    if authorization and same_token("Bearer " + secret, authorization):
        return True
    try:
        cookies = SimpleCookie()
        cookies.load(cookie_header or "")
        value = cookies[COOKIE_NAME].value if COOKIE_NAME in cookies else ""
        return same_token(secret, value)
    except CookieError:
        return False
