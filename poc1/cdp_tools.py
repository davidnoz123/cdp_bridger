
#!/usr/bin/env python3
"""
chrome_cdp_launcher.py

Verbose, robust-ish, cross-platform Chrome CDP launcher.

Purpose
-------
Launch Google Chrome / Chromium with a dedicated user profile and a local
Chrome DevTools Protocol endpoint, then use CDP to open a given URL.

No third-party packages are required.

Example
-------
    python chrome_cdp_launcher.py https://example.com

Windows equivalent command being wrapped
----------------------------------------
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
      --remote-debugging-address=127.0.0.1 ^
      --remote-debugging-port=9222 ^
      --user-data-dir="%LOCALAPPDATA%\\nielsoln-poc-chrome"

Important
---------
Use a dedicated --user-data-dir. Do not point this at your normal Chrome
profile unless you really know what you are doing.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import hashlib
import json
import os
import platform
import random
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


class ChromeCdpError(RuntimeError):
    """Raised when Chrome/CDP launch or communication fails."""


@dataclasses.dataclass(frozen=True)
class CdpEndpointInfo:
    """
    Information returned by http://127.0.0.1:<port>/json/version
    """

    browser: Optional[str]
    protocol_version: Optional[str]
    user_agent: Optional[str]
    web_socket_debugger_url: str
    raw: Dict[str, Any]


class MinimalWebSocket:
    """
    Tiny stdlib-only WebSocket client.

    This is intentionally minimal but sufficient for Chrome DevTools Protocol
    JSON messages.

    Supports:
      - ws:// only
      - text frames
      - close frames
      - ping/pong enough for this use case

    Does not support:
      - wss://
      - extensions
      - fragmented messages beyond basic continuation handling
      - high-throughput production use

    For serious production use, you may eventually prefer the third-party
    `websocket-client` package. For a self-contained local 'bridge', this is
    usually enough.
    """

    def __init__(self, url: str, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "ws":
            raise ChromeCdpError(
                f"MinimalWebSocket only supports ws:// URLs, got: {url!r}"
            )

        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query

        if not self.host:
            raise ChromeCdpError(f"Invalid WebSocket URL: {url!r}")

    def connect(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")

        raw_sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw_sock.settimeout(self.timeout)

        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )

        raw_sock.sendall(request.encode("ascii"))

        response = self._recv_http_response(raw_sock)

        status_line = response.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        if " 101 " not in status_line:
            raw_sock.close()
            raise ChromeCdpError(
                f"WebSocket handshake failed. Status was: {status_line!r}"
            )

        expected_accept = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")

        headers = self._parse_headers(response)
        actual_accept = headers.get("sec-websocket-accept")
        if actual_accept != expected_accept:
            raw_sock.close()
            raise ChromeCdpError(
                "WebSocket handshake failed: Sec-WebSocket-Accept mismatch."
            )

        self.sock = raw_sock

    def close(self) -> None:
        if not self.sock:
            return

        with contextlib.suppress(Exception):
            self._send_frame(opcode=0x8, payload=b"")

        with contextlib.suppress(Exception):
            self.sock.close()

        self.sock = None

    def send_text(self, text: str) -> None:
        self._send_frame(opcode=0x1, payload=text.encode("utf-8"))

    def recv_text(self) -> str:
        """
        Receive one complete text message.
        """
        fragments: List[bytes] = []

        while True:
            fin, opcode, payload = self._recv_frame()

            if opcode == 0x8:
                raise ChromeCdpError("WebSocket closed by peer.")

            if opcode == 0x9:
                # Ping; reply with pong.
                self._send_frame(opcode=0xA, payload=payload)
                continue

            if opcode == 0xA:
                # Pong; ignore.
                continue

            if opcode in (0x1, 0x0):
                fragments.append(payload)

                if fin:
                    return b"".join(fragments).decode("utf-8", errors="replace")

            else:
                raise ChromeCdpError(f"Unsupported WebSocket opcode: {opcode}")

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if not self.sock:
            raise ChromeCdpError("WebSocket is not connected.")

        # Client-to-server frames must be masked.
        fin_and_opcode = 0x80 | opcode
        mask_bit = 0x80
        length = len(payload)

        header = bytearray()
        header.append(fin_and_opcode)

        if length < 126:
            header.append(mask_bit | length)
        elif length <= 0xFFFF:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))

        masking_key = os.urandom(4)
        masked_payload = bytes(
            b ^ masking_key[i % 4] for i, b in enumerate(payload)
        )

        self.sock.sendall(bytes(header) + masking_key + masked_payload)

    def _recv_frame(self) -> Tuple[bool, int, bytes]:
        if not self.sock:
            raise ChromeCdpError("WebSocket is not connected.")

        first_two = self._recv_exact(2)
        b1, b2 = first_two

        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F

        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]

        masking_key = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""

        if masked:
            payload = bytes(
                b ^ masking_key[i % 4] for i, b in enumerate(payload)
            )

        return fin, opcode, payload

    def _recv_exact(self, n: int) -> bytes:
        if not self.sock:
            raise ChromeCdpError("WebSocket is not connected.")

        chunks: List[bytes] = []
        remaining = n

        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise ChromeCdpError("Socket closed while reading WebSocket frame.")
            chunks.append(chunk)
            remaining -= len(chunk)

        return b"".join(chunks)

    @staticmethod
    def _recv_http_response(sock: socket.socket) -> bytes:
        chunks: List[bytes] = []
        data = b""

        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            data = b"".join(chunks)

        return data

    @staticmethod
    def _parse_headers(response: bytes) -> Dict[str, str]:
        header_blob = response.split(b"\r\n\r\n", 1)[0]
        lines = header_blob.split(b"\r\n")[1:]

        headers: Dict[str, str] = {}

        for line in lines:
            if b":" not in line:
                continue
            name, value = line.split(b":", 1)
            headers[name.decode("latin1").strip().lower()] = (
                value.decode("latin1").strip()
            )

        return headers


class ChromeCdpLauncher:
    """
    Dedicated helper for launching Chrome with CDP enabled.

    Main class methods:
      - find_chrome_executable()
      - default_user_data_dir()
      - launch()
      - find_existing_cdp()
      - open_url_via_cdp()
    """

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 9222
    DEFAULT_PROFILE_DIR_NAME = "nielsoln-poc-chrome"

    def __init__(
        self,
        *,
        chrome_executable: Path,
        user_data_dir: Path,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        process: Optional[subprocess.Popen[str]] = None,
    ) -> None:
        self.chrome_executable = chrome_executable
        self.user_data_dir = user_data_dir
        self.host = host
        self.port = port
        self.process = process

    @classmethod
    def launch(
        cls,
        *,
        url_to_open_after_launch: Optional[str] = None,
        chrome_executable: Optional[os.PathLike[str] | str] = None,
        user_data_dir: Optional[os.PathLike[str] | str] = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        extra_args: Optional[Iterable[str]] = None,
        wait_timeout_seconds: float = 20.0,
        reuse_existing_if_available: bool = True,
    ) -> "ChromeCdpLauncher":
        """
        Launch Chrome with CDP enabled.

        If Chrome is already listening on the requested host/port, this can
        attach to it rather than launching another Chrome, if
        reuse_existing_if_available=True.
        """

        if reuse_existing_if_available:
            existing = cls.find_existing_cdp(host=host, port=port)
            if existing is not None:
                exe = Path(chrome_executable) if chrome_executable else cls.find_chrome_executable()
                profile = Path(user_data_dir) if user_data_dir else cls.default_user_data_dir()
                launcher = cls(
                    chrome_executable=exe,
                    user_data_dir=profile,
                    host=host,
                    port=port,
                    process=None,
                )

                if url_to_open_after_launch:
                    launcher.open_url_via_cdp(url_to_open_after_launch)

                return launcher

        exe = Path(chrome_executable) if chrome_executable else cls.find_chrome_executable()
        profile = Path(user_data_dir) if user_data_dir else cls.default_user_data_dir()

        profile.mkdir(parents=True, exist_ok=True)

        command = [
            str(exe),
            f"--remote-debugging-address={host}",
            f"--remote-debugging-port={port}",
            f"--remote-allow-origins=http://localhost:{port},http://127.0.0.1:{port}",
            f"--user-data-dir={str(profile)}",
            "--no-first-run",
            "--no-default-browser-check",
        ]

        if extra_args:
            command.extend(list(extra_args))

        if url_to_open_after_launch:
            # This opens a normal tab during launch. We also support opening
            # via CDP after launch below. You can remove this if you want the
            # URL only opened through Target.createTarget.
            command.append(url_to_open_after_launch)

        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )

        launcher = cls(
            chrome_executable=exe,
            user_data_dir=profile,
            host=host,
            port=port,
            process=process,
        )

        launcher.wait_until_ready(timeout_seconds=wait_timeout_seconds)

        return launcher

    @classmethod
    def find_existing_cdp(
        cls,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout_seconds: float = 0.5,
    ) -> Optional[CdpEndpointInfo]:
        """
        Return endpoint info if something that looks like Chrome CDP is already
        listening on host:port.
        """

        try:
            return cls.get_endpoint_info(
                host=host,
                port=port,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            return None

    @classmethod
    def find_chrome_executable(cls) -> Path:
        """
        Try to find a Chrome or Chromium executable on Windows, macOS, or Linux.
        """

        system = platform.system().lower()

        candidates: List[Path] = []

        if system == "windows":
            program_files = os.environ.get("PROGRAMFILES")
            program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
            local_app_data = os.environ.get("LOCALAPPDATA")

            possible_roots = [
                program_files,
                program_files_x86,
                local_app_data,
            ]

            for root in possible_roots:
                if not root:
                    continue

                candidates.extend(
                    [
                        Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe",
                        Path(root) / "Chromium" / "Application" / "chrome.exe",
                        Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
                    ]
                )

            path_names = [
                "chrome.exe",
                "chrome",
                "chromium.exe",
                "chromium",
                "msedge.exe",
            ]

        elif system == "darwin":
            candidates.extend(
                [
                    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                    Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
                    Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                ]
            )

            path_names = [
                "google-chrome",
                "chrome",
                "chromium",
                "chromium-browser",
                "microsoft-edge",
            ]

        else:
            path_names = [
                "google-chrome",
                "google-chrome-stable",
                "chrome",
                "chromium",
                "chromium-browser",
                "microsoft-edge",
                "microsoft-edge-stable",
            ]

        for candidate in candidates:
            if candidate.exists():
                return candidate

        for name in path_names:
            found = shutil.which(name)
            if found:
                return Path(found)

        raise ChromeCdpError(
            "Could not find Chrome/Chromium/Edge executable. "
            "Pass chrome_executable=... explicitly."
        )

    @classmethod
    def default_user_data_dir(cls) -> Path:
        """
        Return a dedicated cross-platform profile dir.

        Windows:
            %LOCALAPPDATA%\\nielsoln-poc-chrome

        macOS:
            ~/Library/Application Support/nielsoln-poc-chrome

        Linux:
            ~/.config/nielsoln-poc-chrome
        """

        system = platform.system().lower()

        if system == "windows":
            base = os.environ.get("LOCALAPPDATA")
            if not base:
                base = str(Path.home() / "AppData" / "Local")
            return Path(base) / cls.DEFAULT_PROFILE_DIR_NAME

        if system == "darwin":
            return (
                Path.home()
                / "Library"
                / "Application Support"
                / cls.DEFAULT_PROFILE_DIR_NAME
            )

        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / cls.DEFAULT_PROFILE_DIR_NAME

        return Path.home() / ".config" / cls.DEFAULT_PROFILE_DIR_NAME

    @classmethod
    def get_endpoint_info(
        cls,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        timeout_seconds: float = 5.0,
    ) -> CdpEndpointInfo:
        """
        Read http://host:port/json/version and extract the browser WebSocket URL.
        """

        url = f"http://{host}:{port}/json/version"
        data = cls._http_get_json(url, timeout_seconds=timeout_seconds)

        ws_url = data.get("webSocketDebuggerUrl")
        if not ws_url:
            raise ChromeCdpError(
                f"CDP endpoint responded but did not provide webSocketDebuggerUrl: {data!r}"
            )

        return CdpEndpointInfo(
            browser=data.get("Browser"),
            protocol_version=data.get("Protocol-Version"),
            user_agent=data.get("User-Agent"),
            web_socket_debugger_url=ws_url,
            raw=data,
        )

    def wait_until_ready(self, *, timeout_seconds: float = 20.0) -> CdpEndpointInfo:
        """
        Wait until the CDP HTTP endpoint is available.
        """

        deadline = time.monotonic() + timeout_seconds
        last_error: Optional[BaseException] = None

        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise ChromeCdpError(
                    f"Chrome process exited early with code {self.process.returncode}."
                )

            try:
                return self.get_endpoint_info(
                    host=self.host,
                    port=self.port,
                    timeout_seconds=1.0,
                )
            except Exception as exc:
                last_error = exc
                time.sleep(0.2)

        raise ChromeCdpError(
            f"Timed out waiting for Chrome CDP at http://{self.host}:{self.port}. "
            f"Last error: {last_error!r}"
        )

    def open_url_via_cdp(self, url: str, *, new_window: bool = False) -> Dict[str, Any]:
        """
        Open a URL by calling the real CDP method Target.createTarget.

        This talks to the browser-level WebSocket from /json/version.
        """

        if not self._looks_like_url(url):
            # Make common input like "example.com" work.
            url = "https://" + url

        params: Dict[str, Any] = {
            "url": url,
        }

        if new_window:
            params["newWindow"] = True

        return self.send_browser_cdp_command(
            method="Target.createTarget",
            params=params,
        )

    def send_browser_cdp_command(
        self,
        *,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Send a single command to the browser-level CDP WebSocket and wait for
        its matching response.
        """

        endpoint = self.get_endpoint_info(
            host=self.host,
            port=self.port,
            timeout_seconds=timeout_seconds,
        )

        ws = MinimalWebSocket(
            endpoint.web_socket_debugger_url,
            timeout=timeout_seconds,
        )

        command_id = random.randint(1, 2_000_000_000)

        message = {
            "id": command_id,
            "method": method,
        }

        if params is not None:
            message["params"] = params

        try:
            ws.connect()
            ws.send_text(json.dumps(message, separators=(",", ":")))

            deadline = time.monotonic() + timeout_seconds

            while time.monotonic() < deadline:
                raw = ws.recv_text()
                decoded = json.loads(raw)

                # CDP also emits events. We only want the response with our id.
                if decoded.get("id") != command_id:
                    continue

                if "error" in decoded:
                    raise ChromeCdpError(
                        f"CDP command failed: {decoded['error']!r}"
                    )

                return decoded.get("result", {})

            raise ChromeCdpError(
                f"Timed out waiting for CDP response to {method!r}."
            )

        finally:
            ws.close()

    def list_targets(self) -> List[Dict[str, Any]]:
        """
        Return the list of current CDP targets from /json/list.
        Useful for checking which tabs/pages are open.
        """

        url = f"http://{self.host}:{self.port}/json/list"
        data = self._http_get_json(url, timeout_seconds=5.0)

        if not isinstance(data, list):
            raise ChromeCdpError(f"Expected list from /json/list, got: {data!r}")

        return data

    def terminate_launched_process(self) -> None:
        """
        Terminate Chrome only if this object actually launched it.

        If we merely attached to an existing CDP endpoint, self.process is None
        and this does nothing.
        """

        if self.process is None:
            return

        if self.process.poll() is not None:
            return

        self.process.terminate()

        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    @staticmethod
    def _http_get_json(url: str, *, timeout_seconds: float) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            raise ChromeCdpError(f"HTTP request failed for {url}: {exc}") from exc

        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ChromeCdpError(
                f"Endpoint returned invalid JSON from {url}: {raw[:500]!r}"
            ) from exc

    @staticmethod
    def _looks_like_url(value: str) -> bool:
        parsed = urllib.parse.urlparse(value)
        return parsed.scheme in {"http", "https", "file", "data", "about"}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    url = argv[0] if argv else "https://example.com"

    chrome = ChromeCdpLauncher.launch(
        # You can set this to None if you only want Chrome opened,
        # then call open_url_via_cdp() afterwards.
        url_to_open_after_launch=None,
        port=9222,
        reuse_existing_if_available=True,
    )

    print(f"Chrome executable: {chrome.chrome_executable}")
    print(f"User data dir:     {chrome.user_data_dir}")
    print(f"CDP endpoint:      http://{chrome.host}:{chrome.port}")

    endpoint = chrome.get_endpoint_info(host=chrome.host, port=chrome.port)
    print(f"Browser:           {endpoint.browser}")
    print(f"Protocol version:  {endpoint.protocol_version}")
    print(f"Browser WS URL:    {endpoint.web_socket_debugger_url}")

    result = chrome.open_url_via_cdp(url)
    print()
    print("Opened URL via CDP Target.createTarget:")
    print(json.dumps(result, indent=2))

    print()
    print("Current targets:")
    for target in chrome.list_targets():
        print(f"- {target.get('type')}: {target.get('title')} :: {target.get('url')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())