#!/usr/bin/env python3
"""
three_pane_runner.py

Run three subprocesses and display their output in three split console panes.

This is an output monitor, not a fully interactive terminal multiplexer.

Works best in:
  - Windows Terminal
  - PowerShell
  - modern cmd.exe with VT/ANSI enabled
  - Linux/macOS terminals

Usage:
  python three_pane_runner.py

Press Ctrl+C to stop.
"""

from __future__ import annotations

import ctypes
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Sequence


@dataclass
class PaneProcess:
    title: str
    command: Sequence[str]
    max_lines: int = 500
    lines: Deque[str] = field(default_factory=lambda: deque(maxlen=500))
    process: Optional[subprocess.Popen[str]] = None
    output_queue: "queue.Queue[str]" = field(default_factory=queue.Queue)
    reader_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.lines = deque(maxlen=self.max_lines)

        creationflags = 0

        if os.name == "nt":
            # Keep Ctrl+C handling saner on Windows.
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        self.process = subprocess.Popen(
            list(self.command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )

        self.reader_thread = threading.Thread(
            target=self._reader_loop,
            name=f"reader:{self.title}",
            daemon=True,
        )
        self.reader_thread.start()

    def _reader_loop(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None

        try:
            for line in self.process.stdout:
                self.output_queue.put(line.rstrip("\r\n"))
        except Exception as exc:
            self.output_queue.put(f"[reader error: {exc!r}]")
        finally:
            rc = self.process.poll()
            if rc is None:
                rc = self.process.wait()
            self.output_queue.put(f"[process exited with code {rc}]")

    def drain_output(self) -> None:
        while True:
            try:
                line = self.output_queue.get_nowait()
            except queue.Empty:
                return
            self.lines.append(line)

    def terminate(self) -> None:
        if self.process is None:
            return

        if self.process.poll() is not None:
            return

        try:
            if os.name == "nt":
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self.process.terminate()
        except Exception:
            with contextlib_suppress():
                self.process.terminate()

    def kill_if_needed(self, timeout: float = 3.0) -> None:
        if self.process is None:
            return

        if self.process.poll() is not None:
            return

        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()


class contextlib_suppress:
    """
    Tiny local replacement to avoid importing contextlib for one use.
    """

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return True


class ThreePaneConsole:
    def __init__(self, panes: List[PaneProcess], refresh_seconds: float = 0.1) -> None:
        if len(panes) != 3:
            raise ValueError("This runner expects exactly three panes.")

        self.panes = panes
        self.refresh_seconds = refresh_seconds
        self.running = False

    def run(self) -> None:
        enable_ansi_on_windows()

        for pane in self.panes:
            pane.start()

        self.running = True

        try:
            hide_cursor()
            clear_screen()

            while self.running:
                for pane in self.panes:
                    pane.drain_output()

                self.render()
                time.sleep(self.refresh_seconds)

        except KeyboardInterrupt:
            self.render_status("Stopping subprocesses...")

        finally:
            self.running = False

            for pane in self.panes:
                pane.terminate()

            for pane in self.panes:
                pane.kill_if_needed()

            show_cursor()
            move_cursor(1, terminal_size().lines)
            print()

    def render(self) -> None:
        size = terminal_size()
        width = max(size.columns, 40)
        height = max(size.lines, 10)

        # Three horizontal panes stacked vertically.
        pane_heights = split_height(height, 3)

        top = 1
        for pane, pane_height in zip(self.panes, pane_heights):
            self.draw_pane(
                pane=pane,
                top=top,
                left=1,
                width=width,
                height=pane_height,
            )
            top += pane_height

    def draw_pane(
        self,
        *,
        pane: PaneProcess,
        top: int,
        left: int,
        width: int,
        height: int,
    ) -> None:
        if height < 3:
            return

        inner_width = max(width - 2, 1)
        content_height = max(height - 2, 1)

        status = "running"
        if pane.process is not None and pane.process.poll() is not None:
            status = f"exited {pane.process.returncode}"

        title = f" {pane.title} | {status} | {' '.join(pane.command)} "
        title = truncate(title, inner_width)

        # Border top
        move_cursor(left, top)
        write("+" + title.ljust(inner_width, "-") + "+")

        # Content
        visible_lines = list(pane.lines)[-content_height:]

        # Pad so old content gets cleared.
        while len(visible_lines) < content_height:
            visible_lines.insert(0, "")

        for i, line in enumerate(visible_lines):
            y = top + 1 + i
            display_line = truncate(line, inner_width)
            move_cursor(left, y)
            write("|" + display_line.ljust(inner_width) + "|")

        # Border bottom
        move_cursor(left, top + height - 1)
        write("+" + ("-" * inner_width) + "+")

    def render_status(self, message: str) -> None:
        size = terminal_size()
        move_cursor(1, size.lines)
        write(truncate(message, size.columns))


def split_height(total: int, parts: int) -> List[int]:
    base = total // parts
    remainder = total % parts

    result = []
    for i in range(parts):
        result.append(base + (1 if i < remainder else 0))

    return result


def terminal_size() -> os.terminal_size:
    return shutil.get_terminal_size(fallback=(100, 30))


def truncate(value: str, width: int) -> str:
    if width <= 0:
        return ""

    # Very simple truncation. This does not perfectly handle wide Unicode.
    if len(value) <= width:
        return value

    if width <= 1:
        return value[:width]

    return value[: width - 1] + "…"


def write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def clear_screen() -> None:
    write("\x1b[2J\x1b[H")


def move_cursor(x: int, y: int) -> None:
    write(f"\x1b[{y};{x}H")


def hide_cursor() -> None:
    write("\x1b[?25l")


def show_cursor() -> None:
    write("\x1b[?25h")


def enable_ansi_on_windows() -> None:
    """
    Enable ANSI escape processing in the Windows console where possible.

    Modern Windows Terminal usually already supports this, but classic cmd.exe
    can need this flag.
    """

    if os.name != "nt":
        return

    kernel32 = ctypes.windll.kernel32

    STD_OUTPUT_HANDLE = -11
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

    handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

    mode = ctypes.c_uint32()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return

    new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
    kernel32.SetConsoleMode(handle, new_mode)


def python_unbuffered_command(code: str) -> List[str]:
    """
    Helper for demo commands that emit output continuously.
    """
    return [
        sys.executable,
        "-u",
        "-c",
        code,
    ]


def main() -> int:
    panes = [
        PaneProcess(
            title="Process 1",
            command=python_unbuffered_command(
                "import time\n"
                "i = 0\n"
                "while True:\n"
                "    print(f'alpha tick {i}', flush=True)\n"
                "    i += 1\n"
                "    time.sleep(0.5)\n"
            ),
        ),
        PaneProcess(
            title="Process 2",
            command=python_unbuffered_command(
                "import time, random\n"
                "i = 0\n"
                "while True:\n"
                "    print(f'beta value {i} random={random.randint(1, 100)}', flush=True)\n"
                "    i += 1\n"
                "    time.sleep(0.8)\n"
            ),
        ),
        PaneProcess(
            title="Process 3",
            command=python_unbuffered_command(
                "import time\n"
                "i = 0\n"
                "while True:\n"
                "    print(f'gamma doing work step {i}', flush=True)\n"
                "    i += 1\n"
                "    time.sleep(1.2)\n"
            ),
        ),
    ]

    console = ThreePaneConsole(panes)
    console.run()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())