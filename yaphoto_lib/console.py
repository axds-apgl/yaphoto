"""Console implementation for YaPhoto."""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import sys
import threading
import time
import unicodedata
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TypedDict

from .util import atomic_json, progress_bar


class RequestState(TypedDict):
    label: str
    start: float
    done: int
    total: int | None
    phase: str
    worker: int


class WorkerState(TypedDict):
    name: str
    attempt: str
    stage: str
    path: str | None


CONSOLE: Console | None = None


class ServerStats:
    """Rebuild cumulative storage statistics from the append-only, sanitized request log."""

    pattern = re.compile(
        r"\bREQ \d+ (START|BODY OK)\b.*\bGET (s[0-9a-z]+)\.storage\.yandex\.net\s*$", re.I
    )

    def __init__(self, log_path: Path | str, output_path: Path | str) -> None:
        self.output_path = Path(output_path)
        self.log_path = Path(log_path)
        self.state_path = self.output_path.with_suffix(".json")
        self.counts: dict[str, list[int]] = {}
        self.dirty = True
        self.last_save = 0.0
        offset = 0
        loaded = False
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            counts = state["counts"]
            if not isinstance(counts, dict) or any(
                not re.fullmatch("s[0-9a-z]+", k)
                or not isinstance(v, list)
                or len(v) != 2
                or any(type(n) is not int or n < 0 for n in v)
                for k, v in counts.items()
            ):
                raise ValueError("Invalid statistics")
            self.counts = counts
            loaded = True
            if self.log_path.exists():
                stat = self.log_path.stat()
                if (
                    state.get("inode") == stat.st_ino
                    and 0 <= state.get("offset", -1) <= stat.st_size
                ):
                    offset = state["offset"]
        except (OSError, ValueError, KeyError, TypeError):
            if self.output_path.exists():
                for line in self.output_path.read_text(
                    encoding="utf-8-sig", errors="replace"
                ).splitlines():
                    match = re.fullmatch(r"(s[0-9a-z]+)\s+(\d+)\s+(\d+)", line.strip())
                    if match:
                        self.counts[match[1]] = [int(match[2]), int(match[3])]
                if self.counts:
                    loaded = True
                    offset = self.log_path.stat().st_size if self.log_path.exists() else 0
        if self.log_path.exists():
            with open(self.log_path, "rb") as stream:
                stream.seek(offset if loaded else 0)
                for raw_line in stream:
                    self.consume(raw_line.decode("utf-8", errors="replace").strip(), save=False)
        self.save()

    def consume(self, text: str, save: bool = True) -> None:
        match = self.pattern.search(text)
        if not match:
            return
        kind, server = match.group(1).upper(), match.group(2).lower()
        counts = self.counts.setdefault(server, [0, 0])
        counts[0 if kind == "START" else 1] += 1
        self.dirty = True
        if save and time.monotonic() - self.last_save >= 1:
            self.save()

    def save(self) -> None:
        lines = [
            "Статистика серверов хранения Яндекс Диска",
            "По console.log, накопительно за все запуски с журналом.",
            "Запросы: каждый GET, включая повторы, таймауты и ошибки.",
            "Файлы фото/видео: ответ полностью получен (BODY OK); повторы учитываются отдельно.",
            "Перенаправления и оборванные ответы не считаются полученным фото.",
            "",
            f"{'Сервер':<18} {'Запросов':>10} {'Вернул файл':>14}",
            "-" * 44,
        ]
        for server, (requests_count, photos) in sorted(
            self.counts.items(), key=lambda item: (-item[1][0], item[0])
        ):
            lines.append(f"{server:<18} {requests_count:>10} {photos:>14}")
        lines.extend(
            [
                "-" * 44,
                (
                    f"{'ВСЕГО':<18} "
                    f"{sum(x[0] for x in self.counts.values()):>10} "
                    f"{sum(x[1] for x in self.counts.values()):>14}"
                ),
            ]
        )
        tmp = self.output_path.with_name(self.output_path.name + ".tmp")
        try:
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            for attempt in range(3):
                try:
                    os.replace(tmp, self.output_path)
                    self.last_save = time.monotonic()
                    self.dirty = False
                    stat = self.log_path.stat() if self.log_path.exists() else None
                    atomic_json(
                        self.state_path,
                        {
                            "counts": self.counts,
                            "inode": stat.st_ino if stat else None,
                            "offset": stat.st_size if stat else 0,
                        },
                    )
                    break
                except PermissionError:
                    if attempt == 2:
                        return
                    time.sleep(0.02 * (attempt + 1))
        except OSError:
            # A busy statistics file must not stop downloads.
            return


class Console:
    """Thread-safe request logs and periodic per-request progress, no terminal libraries."""

    def __init__(
        self,
        log_path: Path | str,
        interval: float = 0.2,
        max_worker_rows: int | None = None,
        progress_path: Path | None = None,
        log_max_bytes: int = 5 * 1024 * 1024,
        log_backups: int = 3,
        completion_delay: float = 1.5,
        color: str = "auto",
    ) -> None:
        if color not in ("auto", "always", "never"):
            raise ValueError("Цвет: auto, always или never")
        self.color = color
        self.completion_delay = completion_delay
        self.completed_lines: deque[tuple[str, str]] = deque()
        self.progress_path = progress_path
        self.max_worker_rows = max_worker_rows
        self.lock = threading.RLock()
        self.server_stats = ServerStats(log_path, Path(log_path).parent / "server_stats.txt")
        self.active: dict[int, RequestState] = {}
        self.sequence = 0
        self.interval = interval
        self.workers: dict[int, WorkerState] = {}
        self.worker_ids: dict[int, int] = {}
        self.overall = ""
        self.frame_lines = 0
        self.interactive = sys.stdout.isatty()
        if self.interactive and sys.platform == "win32":
            from ctypes import wintypes

            kernel = ctypes.windll.kernel32
            kernel.GetStdHandle.restype = wintypes.HANDLE
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulong)]
            kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            self.interactive = bool(
                kernel.GetConsoleMode(handle, ctypes.byref(mode))
                and kernel.SetConsoleMode(handle, mode.value | 4)
            )
        self.log = RotatingFileHandler(
            log_path, maxBytes=log_max_bytes, backupCount=log_backups, encoding="utf-8"
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._render, daemon=True)
        self.thread.start()

    def event(self, text: str) -> None:
        # Only caller-produced messages: never pass response bodies or signed URLs here.
        line = f"{datetime.now().strftime('%H:%M:%S')} {text}"
        with self.lock:
            record = logging.LogRecord("yaphoto", logging.INFO, "", 0, line, (), None)
            if self.log.shouldRollover(record):
                self.server_stats.save()
            self.log.emit(record)
            self.server_stats.consume(line)
            if text.startswith(("ОБРАБОТАНО", "КАТАЛОГ")):
                self.overall = text
            elif text.startswith(("START ", "Плоская", "ПЕРЕНОС", "COOLDOWN")):
                if not self.interactive:
                    print(line, flush=True)

    def worker(self, name: str, attempt: str = "", path: str | None = None) -> int:
        with self.lock:
            ident = threading.get_ident()
            wid = self.worker_ids.setdefault(ident, len(self.worker_ids) + 1)
            self.workers[wid] = dict(name=name, attempt=attempt, stage="подготовка", path=path)
            return wid

    def reset_workers(self) -> None:
        """Verification uses the main thread; download numbering starts anew at W01."""
        with self.lock:
            self.workers.clear()
            self.worker_ids.clear()

    @staticmethod
    def status_color(status: str) -> str:
        """Classify only status text, never filenames that might contain 'error'."""
        text = status.casefold()
        if re.search(r"error|exception|ошиб|chunk|integrity|timeout|отказано", text):
            return "31"
        if re.match(r"ok(?:\b|$)|verified\b|downloaded\b", text):
            return "32"
        if re.search(r"превью|нет оригинала|не получен|не скач|недоступ|отложен|deferred", text):
            return "33"
        return "37"

    def _paint(self, text: str, shade: str) -> str:
        if self.color == "always" or self.color == "auto" and self.interactive:
            return f"\x1b[{shade}m{text}\x1b[0m"
        return text

    def _paint_summary(self, text: str) -> str:
        shades = {"Успешно": "32", "Превью": "33", "Ошибок": "31"}
        result = ""
        start = 0
        for match in re.finditer(r"(Успешно|Превью|Ошибок):\s*\d+", text):
            result += self._paint(text[start : match.start()], "36")
            result += self._paint(match.group(), shades[match[1]])
            start = match.end()
        return result + self._paint(text[start:], "36")

    def file_done(self, wid: int, name: str, status: str) -> None:
        line = f"W{wid:02} │ {status} │ {name}"
        with self.lock:
            if wid in self.workers:
                self.workers[wid]["stage"] = status
            self.event("RESULT " + line)
            if self.interactive:
                self.completed_lines.append((line, self.status_color(status)))
                self._draw()
            else:
                print(self._paint(line, self.status_color(status)), flush=True)
        # Never hold the console lock while this worker keeps its result on screen.
        if self.completion_delay > 0:
            time.sleep(self.completion_delay)

    @staticmethod
    def _clip_columns(text: str, width: int) -> str:
        result = ""
        columns = 0
        for char in text:
            if unicodedata.category(char).startswith("C"):
                char = " "
            size = (
                0
                if unicodedata.combining(char)
                else (2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
            )
            if columns + size > width:
                break
            result += char
            columns += size
        return result

    def end_live(self) -> None:
        with self.lock:
            if self.interactive:
                self._draw()
                self.frame_lines = 0
            elif self.overall:
                print(self.overall, flush=True)
            self.interactive = False

    def _draw(self) -> None:
        """Update one reserved frame in a single write, without blanking it first."""
        import shutil
        import textwrap

        terminal = shutil.get_terminal_size((120, 25))
        width = max(20, terminal.columns - 1)
        header, separator, counters = self.overall.partition(" | ")
        lines = [""] + textwrap.wrap(header or "ОБРАБОТАНО: подготовка…", width=width)
        if separator:
            lines.extend(textwrap.wrap(counters, width=width))
        summary_lines = len(lines)
        lines.append("")
        shades = ["36"] * summary_lines + ["37"]
        # Leave room for every summary field even in a narrow or short terminal.
        capacity = max(0, terminal.lines - len(lines) - 3)
        if self.max_worker_rows is not None:
            capacity = min(capacity, self.max_worker_rows)
        shown = sorted(self.workers.items())[:capacity]
        for wid, worker in shown:
            active = next(
                (r for r in reversed(list(self.active.values())) if r["worker"] == wid), None
            )
            bar_width = max(4, min(16, width // 8))
            bar = (
                progress_bar(active["done"], active["total"], bar_width)
                if active
                else (
                    progress_bar(1, 1, bar_width)
                    if worker["stage"] != "подготовка"
                    else progress_bar(0, None, bar_width)
                )
            )
            # Even waiting/completed requests reserve exactly the same progress column.
            bar = f"{bar:<{bar_width + 12}}"
            elapsed = f"{time.monotonic() - active['start']:.0f}с" if active else "—"
            amount = f"{active['done'] / 1048576:.1f} МБ" if active else "—"
            attempt = worker["attempt"].replace("попытка ", "")
            stage = active["phase"] if active else worker["stage"]
            prefix = f"W{wid:02} │ {bar} │ {attempt:>8} │ {elapsed:>6} │ {amount:>10} │ "
            lines.append(prefix + f"{stage} · {worker['name']}")
            shades.append(self.status_color(stage))
        if len(shown) < len(self.workers):
            lines.append(
                f"Ещё воркеров: {len(self.workers) - len(shown)} "
                f"· активных запросов: {len(self.active)}"
            )
            shades.append("37")
        old_count = min(self.frame_lines, max(1, terminal.lines - 1))
        output = f"\x1b[{old_count}A\r" if old_count else ""
        # Emit completed files above the live frame; terminal scrollback is the growing log.
        while self.completed_lines:
            remaining, shade = self.completed_lines.popleft()
            while remaining:
                piece = self._clip_columns(remaining, width)
                output += self._paint(piece, shade) + "\x1b[K\n"
                remaining = remaining[len(piece) :]
        for index in range(max(len(lines), old_count)):
            text = self._clip_columns(lines[index], width) if index < len(lines) else ""
            shade = shades[index] if index < len(shades) else "37"
            painted = (
                self._paint_summary(text) if index < summary_lines else (self._paint(text, shade))
            )
            output += painted + "\x1b[K\n"
        if old_count > len(lines):
            output += f"\x1b[{old_count - len(lines)}A\r"
        sys.stdout.write(output)
        sys.stdout.flush()
        self.frame_lines = len(lines)

    def begin(self, label: str, worker: int = 0) -> int:
        with self.lock:
            self.sequence += 1
            key = self.sequence
            self.active[key] = dict(
                label=label,
                start=time.monotonic(),
                done=0,
                total=None,
                phase="ожидание ответа",
                worker=worker,
            )
            self.event(f"REQ {key:05d} START {label}")
            return key

    def update(
        self,
        key: int | None,
        *,
        phase: str | None = None,
        done: int | None = None,
        total: int | None = None,
    ) -> None:
        if key is None:
            return
        with self.lock:
            state = self.active.get(key)
            if state is not None:
                if phase is not None:
                    state["phase"] = phase
                if done is not None:
                    state["done"] = done
                if total is not None:
                    state["total"] = total

    def finish(self, key: int | None, outcome: str) -> None:
        if key is None:
            return
        with self.lock:
            state = self.active.pop(key, None)
            if state is None:
                return
            elapsed = time.monotonic() - state["start"]
            transfer = ""
            if state["done"]:
                transfer = (
                    f" {progress_bar(state['done'], state['total'])} "
                    f"{state['done'] / 1048576:.2f} MiB"
                )
            self.event(f"REQ {key:05d} {outcome}{transfer} {elapsed:.1f}с | {state['label']}")

    def _render(self) -> None:
        last_snapshot = 0.0
        while not self.stop.wait(self.interval):
            with self.lock:
                snapshot = self.progress_path or os.environ.get("YAPHOTO_PROGRESS")
                now = time.monotonic()
                if snapshot and now - last_snapshot >= 1:
                    last_snapshot = now
                    try:
                        atomic_json(
                            Path(snapshot),
                            {
                                "workers": self.workers,
                                "requests": self.active,
                                "overall": self.overall,
                                "running": True,
                                "command": getattr(self, "command", None),
                            },
                        )
                    except OSError:
                        pass
                if self.server_stats.dirty and now - self.server_stats.last_save >= 1:
                    self.server_stats.save()
                if self.interactive and (self.workers or self.overall):
                    self._draw()

    def close(self) -> None:
        self.stop.set()
        self.thread.join(timeout=3)
        snapshot = self.progress_path or os.environ.get("YAPHOTO_PROGRESS")
        if snapshot:
            try:
                atomic_json(
                    Path(snapshot),
                    {"running": False, "overall": self.overall, "workers": {}, "requests": {}},
                )
            except OSError:
                pass
        with self.lock:
            if self.interactive:
                self._draw()
                self.frame_lines = 0
            self.server_stats.save()
            self.log.close()


def log_event(text: str) -> None:
    if CONSOLE:
        CONSOLE.event(text)
