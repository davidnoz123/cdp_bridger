#!/usr/bin/env python3
"""
local_helper.py

A deliberately tiny local helper POC using only the Python standard library.

It demonstrates this flow:

1. A fake target website is open in a Chrome profile that has a login cookie.
2. A fake cloud server creates a high-level capture job.
3. This helper polls the cloud server.
4. The helper finds the target tab through Chrome DevTools Protocol on localhost.
5. The helper captures visible page text through CDP.
6. The helper uploads the captured text back to the fake cloud server.

Safety boundaries in this POC:
- CDP is expected at http://127.0.0.1:9222 only.
- The cloud server is local: http://127.0.0.1:8001 only.
- The target server is local: http://127.0.0.1:8002 only.
- The helper accepts one high-level job type only.
- The helper does not read cookies, localStorage, IndexedDB, passwords, or raw browser profile files.
- The helper does not accept raw CDP commands from the cloud.

Run Chrome separately with CDP enabled, for example on Windows:

    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" ^
      --remote-debugging-address=127.0.0.1 ^
      --remote-debugging-port=9222 ^
      --user-data-dir="%LOCALAPPDATA%\\nielsoln-poc-chrome"

Then visit:
    http://127.0.0.1:8002/login
    http://127.0.0.1:8002/account

Then run:
    python local_helper.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request
from typing import Any

CLOUD_BASE = "http://127.0.0.1:8001"
CDP_BASE = "http://127.0.0.1:9222"
ALLOWED_JOB_TYPE = "capture_visible_text_from_target_tab"
ALLOWED_TARGET_PREFIX = "http://127.0.0.1:8002/account"


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def http_json(url: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "nielsoln-local-helper-poc/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def post_json(url: str, obj: dict, timeout: float = 10.0) -> Any:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "nielsoln-local-helper-poc/0.1",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class TinyWebSocket:
    """Minimal client for CDP text JSON messages, standard library only."""

    def __init__(self, ws_url: str):
        parsed = urllib.parse.urlparse(ws_url)
        if parsed.scheme != "ws":
            raise ValueError("This tiny POC only supports ws://, not wss://")
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")

        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.sendall(request)
        response = sock.recv(4096)
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket handshake failed: {response[:200]!r}")

        expected_accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if expected_accept.encode("ascii") not in response:
            raise RuntimeError("WebSocket accept header did not match")

        self.sock = sock

    def close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        chunks = []
        remaining = n
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise RuntimeError("socket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def send_text(self, text: str) -> None:
        assert self.sock is not None
        payload = text.encode("utf-8")
        first = 0x81  # FIN + text
        mask_bit = 0x80

        header = bytearray([first])
        length = len(payload)
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(mask_bit | 127)
            header.extend(struct.pack("!Q", length))

        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def recv_text(self) -> str:
        while True:
            b1, b2 = self._recv_exact(2)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]

            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x1:  # text
                return payload.decode("utf-8")
            if opcode == 0x8:  # close
                raise RuntimeError("websocket closed")
            if opcode == 0x9:  # ping; ignore in tiny POC
                continue
            if opcode == 0xA:  # pong
                continue


class CdpClient:
    def __init__(self, ws_url: str):
        self.ws = TinyWebSocket(ws_url)
        self.next_id = 1

    def __enter__(self):
        self.ws.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ws.close()

    def call(self, method: str, params: dict | None = None) -> dict:
        msg_id = self.next_id
        self.next_id += 1
        self.ws.send_text(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                return msg.get("result", {})


def list_cdp_tabs() -> list[dict[str, Any]]:
    data = http_json(f"{CDP_BASE}/json/list")
    if not isinstance(data, list):
        raise RuntimeError("CDP /json/list did not return a list")
    return data


def find_target_tab(allowed_prefix: str) -> dict[str, Any] | None:
    for tab in list_cdp_tabs():
        url = str(tab.get("url", ""))
        if url.startswith(allowed_prefix):
            return tab
    return None


def capture_visible_text(tab: dict[str, Any]) -> str:
    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        raise RuntimeError("target tab has no webSocketDebuggerUrl")

    with CdpClient(ws_url) as cdp:
        result = cdp.call("Runtime.evaluate", {
            "expression": "document.body ? document.body.innerText : ''",
            "returnByValue": True,
        })
        return str(result.get("result", {}).get("value", ""))


def job_allowed(job: dict[str, Any]) -> tuple[bool, str]:
    if job.get("type") != ALLOWED_JOB_TYPE:
        return False, "unsupported job type"
    prefix = str(job.get("allowed_url_prefix", ""))
    if prefix != ALLOWED_TARGET_PREFIX:
        return False, "target prefix not allowed by local helper policy"
    return True, "allowed"


def handle_job(job: dict[str, Any]) -> dict[str, Any]:
    allowed, reason = job_allowed(job)
    if not allowed:
        return {"ok": False, "job_id": job.get("job_id"), "error": reason}

    tab = find_target_tab(ALLOWED_TARGET_PREFIX)
    if not tab:
        return {
            "ok": False,
            "job_id": job.get("job_id"),
            "error": "No matching target account tab found; open http://127.0.0.1:8002/account in the CDP Chrome profile.",
        }

    text = capture_visible_text(tab)
    return {
        "ok": True,
        "job_id": job.get("job_id"),
        "captured_from_url": tab.get("url"),
        "captured_title": tab.get("title"),
        "visible_text": text,
        "note": "Captured through local CDP from the logged-in browser tab; cookies were not read or uploaded.",
    }


def main() -> None:
    print("""
╔════════════════════════════════════════════════════╗
║          NIELSOLN LOCAL HELPER - TINY POC          ║
╚════════════════════════════════════════════════════╝
Cloud:  http://127.0.0.1:8001
CDP:    http://127.0.0.1:9222
Target: http://127.0.0.1:8002/account

Press Ctrl+C to stop.
""".strip())

    while True:
        try:
            jobs = http_json(f"{CLOUD_BASE}/api/jobs").get("jobs", [])
            if jobs:
                log(f"received {len(jobs)} job(s)")
            for job in jobs:
                log(f"handling {job.get('job_id')} type={job.get('type')}")
                result = handle_job(job)
                post_json(f"{CLOUD_BASE}/api/result", result)
                log(f"uploaded result for {job.get('job_id')} ok={result.get('ok')}")
        except KeyboardInterrupt:
            log("stopped")
            return
        except Exception as e:
            log(f"error: {e!r}")
        time.sleep(2)


if __name__ == "__main__":
    main()
