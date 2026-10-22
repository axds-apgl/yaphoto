"""Проверяемые локальные настройки, исключённые из Git."""

import json
from pathlib import Path


def defaults(root: Path, output_name: str) -> tuple[Path, Path]:
    root = Path(root).resolve()
    path = root / "local.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise ValueError(
            "Не удалось прочитать local.json: проверьте доступ и синтаксис JSON"
        ) from exc
    if not isinstance(config, dict):
        raise ValueError("local.json должен содержать объект JSON")

    def resolve(key: str, fallback: str) -> Path:
        value = config.get(key, fallback)
        if not isinstance(value, str) or not value or "\0" in value:
            raise ValueError("В local.json поле " + key + " должно содержать непустой путь")
        try:
            p = Path(value)
            return p.resolve() if p.is_absolute() else (root / p).resolve()
        except (OSError, ValueError) as exc:
            raise ValueError("В local.json поле " + key + " содержит недопустимый путь") from exc

    return resolve("output", output_name), resolve("cookies", "cookies.json")
