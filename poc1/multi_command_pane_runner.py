import collections
import ctypes
import dataclasses
import os
import queue
import re
import select
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

    This is an output monitor, not a fully interactive terminal multiplexer.

    Simple mouse support:
      - Mouse wheel over a pane scrolls that pane's output history.
      - Middle-click over a pane jumps that pane back to live/follow mode.
      - q exits.
      - Ctrl+C exits.

    Mouse support depends on the terminal supporting ANSI/VT mouse reporting.
    It works best in Windows Terminal, modern PowerShell terminals, VS Code
    terminal, Linux terminals, and macOS Terminal/iTerm2.

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

        # New scrollback state.
        # 0 means live/follow mode. Positive values mean "show this many lines
        # above the live tail".
        scroll_offset: int = 0

        # Last rendered screen geometry. These are updated by draw_pane() and
        # used to map mouse y-coordinates back to a pane.
        last_render_top: int = 0
        last_render_height: int = 0

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
            assert self.output_queue is not None

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
            assert self.output_queue is not None

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
    # Console input helper
    # ------------------------------------------------------------------

    class ConsoleInput:
        """
        Small cross-platform non-blocking input helper.

        On Unix-like systems we temporarily put stdin in cbreak mode so ANSI
        mouse sequences are delivered immediately.

        On Windows we use msvcrt.kbhit/getwch and enable virtual-terminal input
        where possible.
        """

        def __init__(self) -> None:
            self._is_windows = os.name == "nt"
            self._old_termios = None
            self._old_stdin_mode = None

        def __enter__(self) -> "MultiPaneConsole.ConsoleInput":
            if self._is_windows:
                self._enable_windows_virtual_terminal_input()
            else:
                self._enable_unix_cbreak()
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            if self._is_windows:
                self._restore_windows_input_mode()
            else:
                self._restore_unix_terminal()

        def read_available(self) -> str:
            if self._is_windows:
                return self._read_available_windows()
            return self._read_available_unix()

        def _read_available_windows(self) -> str:
            try:
                import msvcrt
            except ImportError:
                return ""

            chars: typing.List[str] = []
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                chars.append(ch)
            return "".join(chars)

        def _read_available_unix(self) -> str:
            if not sys.stdin.isatty():
                return ""

            chars: typing.List[str] = []
            while True:
                readable, _, _ = select.select([sys.stdin], [], [], 0)
                if not readable:
                    break
                chunk = os.read(sys.stdin.fileno(), 4096)
                if not chunk:
                    break
                chars.append(chunk.decode("utf-8", errors="replace"))
            return "".join(chars)

        def _enable_unix_cbreak(self) -> None:
            if not sys.stdin.isatty():
                return

            import termios
            import tty

            fd = sys.stdin.fileno()
            self._old_termios = termios.tcgetattr(fd)
            tty.setcbreak(fd)

        def _restore_unix_terminal(self) -> None:
            if self._old_termios is None:
                return

            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_termios)
            self._old_termios = None

        def _enable_windows_virtual_terminal_input(self) -> None:
            if os.name != "nt":
                return

            try:
                kernel32 = ctypes.windll.kernel32

                STD_INPUT_HANDLE = -10
                ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
                ENABLE_MOUSE_INPUT = 0x0010
                ENABLE_EXTENDED_FLAGS = 0x0080
                ENABLE_QUICK_EDIT_MODE = 0x0040

                handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
                mode = ctypes.c_uint32()
                if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    return

                self._old_stdin_mode = mode.value

                # Disable Quick Edit if possible because it can interfere with
                # mouse reporting in some console hosts.
                new_mode = mode.value
                new_mode |= ENABLE_VIRTUAL_TERMINAL_INPUT
                new_mode |= ENABLE_MOUSE_INPUT
                new_mode |= ENABLE_EXTENDED_FLAGS
                new_mode &= ~ENABLE_QUICK_EDIT_MODE

                kernel32.SetConsoleMode(handle, new_mode)
            except Exception:
                # Mouse still may work in Windows Terminal even if this fails.
                pass

        def _restore_windows_input_mode(self) -> None:
            if os.name != "nt":
                return
            if self._old_stdin_mode is None:
                return

            try:
                kernel32 = ctypes.windll.kernel32
                STD_INPUT_HANDLE = -10
                handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
                kernel32.SetConsoleMode(handle, self._old_stdin_mode)
            except Exception:
                pass
            finally:
                self._old_stdin_mode = None

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(
        self,
        panes: typing.List["MultiPaneConsole.PaneProcess"],
        refresh_seconds: float = 0.1,
        mouse_scroll_lines: int = 3,
    ) -> None:
        if not panes:
            raise ValueError("At least one pane is required.")
        self.panes = panes
        self.refresh_seconds = refresh_seconds
        self.mouse_scroll_lines = mouse_scroll_lines
        self.running = False
        self._input_buffer = ""

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        self.enable_ansi_on_windows()

        for pane in self.panes:
            pane.start()

        self.running = True

        try:
            with self.ConsoleInput() as console_input:
                self.hide_cursor()
                self.enable_mouse_reporting()
                self.clear_screen()

                while self.running:
                    for pane in self.panes:
                        pane.drain_output()

                    self.handle_input(console_input.read_available())
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

            self.disable_mouse_reporting()
            self.show_cursor()
            self.move_cursor(1, self.terminal_size().lines)
            print()

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    _SGR_MOUSE_RE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")

    def handle_input(self, text: str) -> None:
        if not text:
            return

        self._input_buffer += text

        # Plain keyboard fallback: q exits. Ctrl+C is still handled by Python
        # as KeyboardInterrupt in normal terminals.
        if "q" in self._input_buffer or "Q" in self._input_buffer:
            self.running = False
            self._input_buffer = ""
            return

        # Parse any complete SGR mouse events in the buffer.
        pos = 0
        for match in self._SGR_MOUSE_RE.finditer(self._input_buffer):
            pos = match.end()
            button = int(match.group(1))
            x = int(match.group(2))
            y = int(match.group(3))
            final = match.group(4)
            pressed = final == "M"
            self.handle_mouse_event(button=button, x=x, y=y, pressed=pressed)

        # Keep any trailing partial escape sequence. Do not let garbage grow
        # forever if the terminal sends something we do not parse.
        self._input_buffer = self._input_buffer[pos:]
        if len(self._input_buffer) > 100:
            self._input_buffer = self._input_buffer[-20:]

    def handle_mouse_event(self, *, button: int, x: int, y: int, pressed: bool) -> None:
        # SGR wheel events normally arrive as pressed events with button codes:
        #   64 = wheel up
        #   65 = wheel down
        # Middle button is normally code 1.
        # Modifier keys can add bits, so we use the low/simple cases and keep
        # this intentionally conservative.
        if not pressed:
            return

        pane = self.find_pane_at_y(y)
        if pane is None:
            return

        if button == 64:
            # Wheel up: go back in history.
            self.scroll_pane(pane, self.mouse_scroll_lines)
        elif button == 65:
            # Wheel down: go toward live tail.
            self.scroll_pane(pane, -self.mouse_scroll_lines)
        elif button == 1:
            # Middle click: jump to live/follow mode.
            pane.scroll_offset = 0

    def find_pane_at_y(self, y: int) -> typing.Optional["MultiPaneConsole.PaneProcess"]:
        for pane in self.panes:
            top = pane.last_render_top
            height = pane.last_render_height
            if top <= y < top + height:
                return pane
        return None

    def scroll_pane(self, pane: "MultiPaneConsole.PaneProcess", amount: int) -> None:
        # Positive amount scrolls back into history. Negative amount scrolls
        # toward the live tail.
        content_height = max(pane.last_render_height - 2, 1)
        max_scroll = max(len(pane.lines) - content_height, 0)
        pane.scroll_offset = max(0, min(max_scroll, pane.scroll_offset + amount))

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
        pane.last_render_top = top
        pane.last_render_height = height

        if height < 3:
            return

        inner_width = max(width - 2, 1)
        content_height = max(height - 2, 1)

        status = "running"
        if pane.process is not None and pane.process.poll() is not None:
            status = f"exited {pane.process.returncode}"

        scroll_note = ""
        if pane.scroll_offset:
            scroll_note = f" | scroll +{pane.scroll_offset}"

        title = f" {pane.title} | {status}{scroll_note} | {' '.join(pane.command)} "
        title = self.truncate(title, inner_width)

        # Border top
        self.move_cursor(left, top)
        self.write("+" + title.ljust(inner_width, "-") + "+")

        # Content
        visible_lines = self.get_visible_lines(pane, content_height)

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

    def get_visible_lines(
        self,
        pane: "MultiPaneConsole.PaneProcess",
        content_height: int,
    ) -> typing.List[str]:
        all_lines = list(pane.lines)

        if not all_lines:
            return []

        max_scroll = max(len(all_lines) - content_height, 0)
        pane.scroll_offset = max(0, min(pane.scroll_offset, max_scroll))

        if pane.scroll_offset == 0:
            return all_lines[-content_height:]

        end = max(len(all_lines) - pane.scroll_offset, 0)
        start = max(end - content_height, 0)
        return all_lines[start:end]

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
    def enable_mouse_reporting() -> None:
        # SGR extended mouse coordinates are the important one here.
        # 1000 enables button events; 1006 makes coordinates parseable as
        # ESC [ < button ; x ; y M.
        MultiPaneConsole.write("\x1b[?1000h")
        MultiPaneConsole.write("\x1b[?1006h")

    @staticmethod
    def disable_mouse_reporting() -> None:
        MultiPaneConsole.write("\x1b[?1006l")
        MultiPaneConsole.write("\x1b[?1000l")

    @staticmethod
    def enable_ansi_on_windows() -> None:
        """
        Enable ANSI escape processing in the Windows console where possible.

        Modern Windows Terminal usually already supports this, but classic
        cmd.exe can need this flag.
        """
        if os.name != "nt":
            return

        try:
            kernel32 = ctypes.windll.kernel32
            STD_OUTPUT_HANDLE = -11
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

            handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                return

            new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            kernel32.SetConsoleMode(handle, new_mode)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Testbed
    # ------------------------------------------------------------------

    @staticmethod
    def MultiPaneConsole_testbed() -> int:
        def python_unbuffered_command(code: str) -> typing.List[str]:
            """Build a command list that runs inline Python code with -u (unbuffered)."""
            return [sys.executable, "-u", "-c", code]

        panes = [
            MultiPaneConsole.PaneProcess(
                title="Process 1",
                command=python_unbuffered_command(
                    "import time\n"
                    "i = 0\n"
                    "while True:\n"
                    "    print(f'alpha tick {i}', flush=True)\n"
                    "    i += 1\n"
                    "    time.sleep(0.5)\n"
                ),
                max_lines=2000,
            ),
            MultiPaneConsole.PaneProcess(
                title="Process 2",
                command=python_unbuffered_command(
                    "import time, random\n"
                    "i = 0\n"
                    "while True:\n"
                    "    print(f'beta value {i} random={random.randint(1, 100)}', flush=True)\n"
                    "    i += 1\n"
                    "    time.sleep(0.8)\n"
                ),
                max_lines=2000,
            ),
            MultiPaneConsole.PaneProcess(
                title="Process 3",
                command=python_unbuffered_command(
                    "import time\n"
                    "i = 0\n"
                    "while True:\n"
                    "    print(f'gamma doing work step {i}', flush=True)\n"
                    "    i += 1\n"
                    "    time.sleep(1.2)\n"
                ),
                max_lines=2000,
            ),
        ]

        MultiPaneConsole(panes).run()
        return 0


if __name__ == "__main__":
    MultiPaneConsole.MultiPaneConsole_testbed()
