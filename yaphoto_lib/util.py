"""Util implementation for YaPhoto."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path


def progress_bar(done: int, total: int | None, width: int = 20) -> str:
    if total:
        ratio = max(0, min(1, done / total))
        filled = int(width * ratio)
        return "┃" + "█" * filled + "░" * (width - filled) + f"┃ {ratio * 100:5.1f}%"
    return "┃" + "░" * width + "┃ ожидание"


def canonical_path(path: str) -> str:
    if path.startswith("photounlim:/"):
        return "/photounlim/" + path[len("photounlim:/") :]
    return path


def safe_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(" .") or "photo"
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        name = "_" + name
    if len(name) > 120:
        name = (
            name[:100]
            + "_"
            + hashlib.sha256(name.encode()).hexdigest()[:8]
            + Path(name).suffix[:10]
        )
    return name


def auxiliary_path(output: Path | str, remote_path: str, kind: str) -> Path:
    key = hashlib.sha256(remote_path.encode()).hexdigest()
    return (
        Path(output)
        / ("_partial" if kind == "part" else "_metadata")
        / (key + (".part" if kind == "part" else ".json"))
    )


def atomic_json(path: Path, data: object) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
