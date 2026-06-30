#!/usr/bin/env python3
"""
main.py

Run the CDP-bridger POC stack in a single multi-pane console.

  - Pane 1: our_cloud_server.py   (fake cloud server, SSE + POST)
  - Pane 2: target_server.py  (fake target website + CDP browser launcher)
  - Pane 3: local_bridge.py   (local CDP helper, subscribes to cloud via SSE)

Press  q / Q  or  Ctrl+C  to stop all three processes and exit.
Mouse wheel scrolls the pane under the cursor.
Middle-click returns a pane to live/follow mode.

This version includes a cross-platform port preflight so stale cloud/target
servers are detected before the pane runner starts.
"""

from __future__ import annotations

import os
import platform
import socket
import sys
from dataclasses import dataclass
from typing import Iterable

from cdp_tools import ChromeCdpError, ChromeCdpLauncher
from multi_command_pane_runner import MultiPaneConsole

_HERE = os.path.dirname(os.path.abspath(__file__))

HOST = "127.0.0.1"
CLOUD_PORT = 8001
TARGET_PORT = 8002


@dataclass(frozen=True)
class PortCheck:
    host: str
    port: int
    description: str


def _script(name: str) -> list[str]:
    """Return an unbuffered, UTF-8-forced Python command for a script in this directory."""
    return [sys.executable, "-u", "-X", "utf8", os.path.join(_HERE, name)]


def _port_is_accepting_connections(host: str, port: int, timeout_seconds: float = 0.25) -> bool:
    """Return True if something is already listening on host:port.

    This is deliberately implemented with the Python standard library instead
    of shelling out to netstat/ss/lsof, so it works on Windows, Linux, WSL,
    and macOS.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout_seconds)
        return sock.connect_ex((host, port)) == 0


def _diagnostic_commands_for_port(port: int) -> list[str]:
    """Return human-friendly commands for identifying stale listeners.

    The actual check is cross-platform Python. These commands are only printed
    as hints when a conflict is found.
    """
    system = platform.system().lower()

    if system == "windows":
        return [
            f'netstat -ano | findstr :{port}',
            f'for /f "tokens=5" %p in (\'netstat -ano ^| findstr :{port} ^| findstr LISTENING\') do tasklist /FI "PID eq %p"',
            f'taskkill /PID <PID> /F',
        ]

    if system == "darwin":
        return [
            f"lsof -nP -iTCP:{port} -sTCP:LISTEN",
            "kill <PID>",
            "kill -9 <PID>   # only if the normal kill does not work",
        ]

    # Linux, WSL, and most other Unix-like environments.
    return [
        f"ss -ltnp 'sport = :{port}'",
        f"lsof -nP -iTCP:{port} -sTCP:LISTEN",
        "kill <PID>",
        "kill -9 <PID>   # only if the normal kill does not work",
    ]


def _assert_required_ports_are_free(checks: Iterable[PortCheck]) -> None:
    conflicts: list[PortCheck] = []

    print("[main] Checking POC server ports before starting panes...", flush=True)

    for check in checks:
        print(
            f"[main] Checking {check.description}: {check.host}:{check.port}",
            flush=True,
        )
        if _port_is_accepting_connections(check.host, check.port):
            conflicts.append(check)

    if not conflicts:
        print("[main] Port preflight OK: no stale cloud/target servers detected.", flush=True)
        return

    print("", file=sys.stderr)
    print("[main] ERROR: one or more required POC ports are already in use.", file=sys.stderr)
    print("[main] This usually means a stale our_cloud_server.py or target_server.py is still running.", file=sys.stderr)
    print("", file=sys.stderr)

    for conflict in conflicts:
        print(
            f"[main] Port conflict: {conflict.description} is already listening on "
            f"{conflict.host}:{conflict.port}",
            file=sys.stderr,
        )
        print("[main] Diagnostic commands you can run:", file=sys.stderr)
        for command in _diagnostic_commands_for_port(conflict.port):
            print(f"  {command}", file=sys.stderr)
        print("", file=sys.stderr)

    raise SystemExit(2)


def _launch_cdp_browser() -> None:
    """Start or attach to the shared CDP Chrome instance before subprocesses need it.

    Doing this here eliminates the race where our_cloud_server and target_server both
    try to create Chrome at the same time. The subprocesses call
    launch(reuse_existing_if_available=True) and should find it already running.
    """
    try:
        chrome = ChromeCdpLauncher.launch(reuse_existing_if_available=True)
        print(f"[main] CDP browser ready at http://{chrome.host}:{chrome.port}", flush=True)
    except ChromeCdpError as e:
        print(f"[main] WARNING: could not start CDP browser: {e}", file=sys.stderr)
        print(
            "[main] Start Chrome manually with --remote-debugging-port=9222 "
            "and a dedicated --user-data-dir (not your default profile).",
            file=sys.stderr,
        )


def main() -> int:
    _assert_required_ports_are_free(
        [
            PortCheck(HOST, CLOUD_PORT, "Our Remote cloud server"),
            PortCheck(HOST, TARGET_PORT, "fake target website server"),
        ]
    )

    _launch_cdp_browser()

    panes = [
        MultiPaneConsole.PaneProcess(
            title="Our Cloud Server",
            command=_script("our_cloud_server.py"),
            max_lines=2000,
        ),
        MultiPaneConsole.PaneProcess(
            title="User Account + CDP Browser",
            command=_script("target_server.py"),
            max_lines=2000,
        ),
        MultiPaneConsole.PaneProcess(
            title="Local Python Bridge",
            command=_script("local_bridge.py"),
            max_lines=2000,
        ),
    ]

    MultiPaneConsole(panes).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
