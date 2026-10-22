"""Options implementation for YaPhoto."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_ATTEMPTS = 4

DEFAULT_READ_TIMEOUT = 30


@dataclass(frozen=True)
class DownloadOptions:
    workers: int = 4
    limit: int | None = None
    attempts: int = DEFAULT_ATTEMPTS
    date_policy: str = "cloud"
    order: str = "path"
    read_timeout: int = DEFAULT_READ_TIMEOUT
    previews: bool = True
    only_missing: bool = False
    preview_only: bool = False
    media: str = "photo"
    refresh_previews: bool = False
    current_scan: bool = False
    auth_context: tuple[str, str] | None = None
    selected_paths: set[str] | None = None

    def __post_init__(self) -> None:
        validate_media(self.media)
        if not 1 <= self.workers <= 32 or not 1 <= self.attempts <= 8:
            raise ValueError("Воркеры: от 1 до 32; попытки: от 1 до 8")
        if self.limit is not None and self.limit < 1 or self.read_timeout < 1:
            raise ValueError("Лимит и таймаут должны быть положительными")
        if self.order not in ("path", "reverse", "spread", "size") or self.date_policy not in (
            "cloud",
            "none",
        ):
            raise ValueError("Неверный порядок или политика дат")
        if (self.preview_only or self.refresh_previews) and self.media != "photo":
            raise ValueError("Спасение и улучшение превью применяется только к фото")


def validate_media(media: str) -> None:
    if media not in ("photo", "video", "all"):
        raise ValueError("Тип медиа должен быть photo, video или all")


def media_scope(media: str, column: str = "media_kind") -> tuple[str, tuple[str, ...]]:
    validate_media(media)
    if column not in ("media_kind", "r.media_kind"):
        raise ValueError("Неверный столбец типа медиа")
    return ("", ()) if media == "all" else (" AND " + column + "=?", (media,))


def scan_scope(media: str) -> tuple[str, tuple[str, ...]]:
    validate_media(media)
    return ("", ()) if media == "all" else (" WHERE media IN (?,?)", (media, "all"))
