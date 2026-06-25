#!/usr/bin/env python3
"""
three_pane_runner.py

Run an arbitrary number of subprocesses and display their output in
vertically-stacked console panes.

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

import collections
import ctypes
import dataclasses
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import typing


class MultiPaneConsole:
    """
    Run an arbitrary number of subprocesses and display their output in
    vertically-stacked terminal panes.

    Usage::

        panes = [
            MultiPaneConsole.PaneProcess(title="Server", command=["python", "server.py"]),
            MultiPaneConsole.PaneProcess(title="Worker", command=["python", "worker.py"]),
        ]
        MultiPaneConsole(panes).run()
    """

    # ------------------------------------------------------------------
    # Nested process data class
    # ------------------------------------------------------------------

    @dataclasses.dataclass
    class PaneProcess:
        """One subprocess with its output buffer and lifecycle helpers."""

        title: str
        command: typing.Sequence[str]
        max_lines: int = 500
        lines: typing.Deque[str] = dataclasses.field(default_factory=lambda: collections.deque(maxlen=500))
        process: typing.Optional[subprocess.Popen[str]] = None
        output_queue: typing.Optional[queue.Queue] = dataclasses.field(default_factory=queue.Queue)
        reader_thread: typing.Optional[threading.Thread] = None

        def start(self) -> None:
            self.lines = collections.deque(maxlen=self.max_lines)

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
                try:
                    self.process.terminate()
                except Exception:
                    pass

        def kill_if_needed(self, timeout: float = 3.0) -> None:
            if self.process is None:
                return
            if self.process.poll() is not None:
                return
            try:
                self.process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(
        self,
        panes: typing.List["MultiPaneConsole.PaneProcess"],
        refresh_seconds: float = 0.1,
    ) -> None:
        if not panes:
            raise ValueError("At least one pane is required.")
        self.panes = panes
        self.refresh_seconds = refresh_seconds
        self.running = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.enable_ansi_on_windows()

        for pane in self.panes:
            pane.start()

        self.running = True

        try:
            self.hide_cursor()
            self.clear_screen()

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

            self.show_cursor()
            self.move_cursor(1, self.terminal_size().lines)
            print()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> None:
        size = self.terminal_size()
        width = max(size.columns, 40)
        height = max(size.lines, 10)

        pane_heights = self.split_height(height, len(self.panes))

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
        pane: "MultiPaneConsole.PaneProcess",
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
        title = self.truncate(title, inner_width)

        # Border top
        self.move_cursor(left, top)
        self.write("+" + title.ljust(inner_width, "-") + "+")

        # Content
        visible_lines = list(pane.lines)[-content_height:]

        # Pad so old content gets cleared.
        while len(visible_lines) < content_height:
            visible_lines.insert(0, "")

        for i, line in enumerate(visible_lines):
            y = top + 1 + i
            display_line = self.truncate(line, inner_width)
            self.move_cursor(left, y)
            self.write("|" + display_line.ljust(inner_width) + "|")

        # Border bottom
        self.move_cursor(left, top + height - 1)
        self.write("+" + ("-" * inner_width) + "+")

    def render_status(self, message: str) -> None:
        size = self.terminal_size()
        self.move_cursor(1, size.lines)
        self.write(self.truncate(message, size.columns))

    # ------------------------------------------------------------------
    # Layout helpers
    # ------------------------------------------------------------------

    @staticmethod
    def split_height(total: int, parts: int) -> typing.List[int]:
        base = total // parts
        remainder = total % parts
        return [base + (1 if i < remainder else 0) for i in range(parts)]

    # ------------------------------------------------------------------
    # Terminal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def terminal_size() -> os.terminal_size:
        return shutil.get_terminal_size(fallback=(100, 30))

    @staticmethod
    def truncate(value: str, width: int) -> str:
        if width <= 0:
            return ""
        # Very simple truncation. This does not perfectly handle wide Unicode.
        if len(value) <= width:
            return value
        if width <= 1:
            return value[:width]
        return value[: width - 1] + "…"

    @staticmethod
    def write(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    @staticmethod
    def clear_screen() -> None:
        MultiPaneConsole.write("\x1b[2J\x1b[H")

    @staticmethod
    def move_cursor(x: int, y: int) -> None:
        MultiPaneConsole.write(f"\x1b[{y};{x}H")

    @staticmethod
    def hide_cursor() -> None:
        MultiPaneConsole.write("\x1b[?25l")

    @staticmethod
    def show_cursor() -> None:
        MultiPaneConsole.write("\x1b[?25h")

    @staticmethod
    def enable_ansi_on_windows() -> None:
        """
        Enable ANSI escape processing in the Windows console where possible.

        Modern Windows Terminal usually already supports this, but classic
        cmd.exe can need this flag.
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

    @staticmethod
    def python_unbuffered_command(code: str) -> typing.List[str]:
        """Build a command list that runs inline Python code with -u (unbuffered)."""
        return [sys.executable, "-u", "-c", code]


# Convenience alias so callers can write PaneProcess(...) directly.
PaneProcess = MultiPaneConsole.PaneProcess


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    panes = [
        MultiPaneConsole.PaneProcess(
            title="Process 1",
            command=MultiPaneConsole.python_unbuffered_command(
                "import time\n"
                "i = 0\n"
                "while True:\n"
                "    print(f'alpha tick {i}', flush=True)\n"
                "    i += 1\n"
                "    time.sleep(0.5)\n"
            ),
        ),
        MultiPaneConsole.PaneProcess(
            title="Process 2",
            command=MultiPaneConsole.python_unbuffered_command(
                "import time, random\n"
                "i = 0\n"
                "while True:\n"
                "    print(f'beta value {i} random={random.randint(1, 100)}', flush=True)\n"
                "    i += 1\n"
                "    time.sleep(0.8)\n"
            ),
        ),
        MultiPaneConsole.PaneProcess(
            title="Process 3",
            command=MultiPaneConsole.python_unbuffered_command(
                "import time\n"
                "i = 0\n"
                "while True:\n"
                "    print(f'gamma doing work step {i}', flush=True)\n"
                "    i += 1\n"
                "    time.sleep(1.2)\n"
            ),
        ),
    ]

    MultiPaneConsole(panes).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
