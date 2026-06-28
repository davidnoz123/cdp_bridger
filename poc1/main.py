#!/usr/bin/env python3
"""
main.py

Run the CDP-bridger POC stack in a single multi-pane console.

  - Pane 1: cloud_server.py   (fake cloud server, SSE + POST)
  - Pane 2: target_server.py  (fake target website + CDP browser launcher)
  - Pane 3: local_helper.py   (local CDP helper, subscribes to cloud via SSE)

Press  q / Q  or  Ctrl+C  to stop all three processes and exit.
Mouse wheel scrolls the pane under the cursor.
Middle-click returns a pane to live/follow mode.
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
from dataclasses import dataclass

from cdp_tools import ChromeCdpError, ChromeCdpLauncher
from multi_command_pane_runner import MultiPaneConsole

_HERE = os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class ServerPort:
    name: str
    port: int


POC_SERVER_PORTS = [
    ServerPort("Our Remote Server / cloud_server.py", 8001),
    ServerPort("User Account Target / target_server.py", 8002),
]


def _script(name: str) -> list[str]:
    """Return an unbuffered, UTF-8-forced Python command for a script in this directory."""
    return [sys.executable, "-u", "-X", "utf8", os.path.join(_HERE, name)]


def _run_capture(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a small diagnostic command and capture text output without raising."""
    try:
        return subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            check=False,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(command, returncode=999, stdout=f"[diagnostic command failed: {exc!r}]\n")


def _windows_netstat_lines_for_port(port: int) -> list[str]:
    """Return `netstat -ano | findstr :PORT`-style lines on Windows."""
    # Avoid shell pipelines so quoting is simple and predictable.
    result = _run_capture(["netstat", "-ano"])
    token = f":{port}"
    return [line for line in result.stdout.splitlines() if token in line]


def _print_port_diagnostics(port: int) -> None:
    """Print the platform-specific equivalent of `netstat -ano | findstr :PORT`."""
    print(f"[main] Port preflight for :{port}")

    if platform.system().lower() == "windows":
        print(f"[main] > netstat -ano | findstr :{port}")
        lines = _windows_netstat_lines_for_port(port)
        if lines:
            for line in lines:
                print(f"[main]   {line}")
        else:
            print(f"[main]   no netstat rows for :{port}")
        return

    # Cross-platform fallback: show whether a bind would succeed.
    if _is_port_free(port):
        print(f"[main]   port {port} appears free")
    else:
        print(f"[main]   port {port} appears busy")


def _windows_listening_pids_for_port(port: int) -> list[int]:
    """Extract LISTENING PIDs from `netstat -ano` output on Windows."""
    pids: list[int] = []
    for line in _windows_netstat_lines_for_port(port):
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            try:
                pids.append(int(parts[4]))
            except ValueError:
                pass
    return sorted(set(pids))


def _is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """Return True if this process can bind host:port right now."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Do not use SO_REUSEADDR here. We want a conservative check that
        # catches an existing POC server before the pane runner starts.
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _fail_if_server_port_busy(server: ServerPort) -> None:
    """Refuse to start if a POC server port already has a listener."""
    _print_port_diagnostics(server.port)

    if _is_port_free(server.port):
        print(f"[main] OK: {server.name} port {server.port} is free")
        return

    print(f"[main] ERROR: {server.name} port {server.port} is already in use", file=sys.stderr)

    if platform.system().lower() == "windows":
        pids = _windows_listening_pids_for_port(server.port)
        if pids:
            print(f"[main] LISTENING PID(s) on :{server.port}: {', '.join(map(str, pids))}", file=sys.stderr)
            print("[main] Stop stale POC processes, for example:", file=sys.stderr)
            for pid in pids:
                print(f"[main]   taskkill /PID {pid} /F", file=sys.stderr)
        else:
            print(f"[main] No LISTENING PID found, but bind check says :{server.port} is busy.", file=sys.stderr)
    else:
        print(f"[main] Stop the process using 127.0.0.1:{server.port}, then rerun main.py.", file=sys.stderr)

    raise SystemExit(2)


def _preflight_server_ports() -> None:
    """Check all POC-owned server ports before launching subprocess panes."""
    print("[main] Checking POC server ports before starting panes...")
    for server in POC_SERVER_PORTS:
        _fail_if_server_port_busy(server)
    print("[main] Port preflight passed")


def _launch_cdp_browser() -> None:
    """Start or attach to the shared CDP Chrome instance before any subprocess needs it.

    Doing this here eliminates the race where cloud_server and target_server both
    try to create Chrome at the same time. The subprocesses call
    launch(reuse_existing_if_available=True) and always find it already running.
    """
    try:
        chrome = ChromeCdpLauncher.launch(reuse_existing_if_available=True)
        print(f"[main] CDP browser ready at http://{chrome.host}:{chrome.port}")
    except ChromeCdpError as e:
        print(f"[main] WARNING: could not start CDP browser: {e}", file=sys.stderr)
        print(
            "[main] Start Chrome manually with --remote-debugging-port=9222 "
            "and a dedicated --user-data-dir (not your default profile).",
            file=sys.stderr,
        )


def main() -> int:
    # These are strict POC-owned HTTP ports. If stale listeners exist, launching
    # another pane-run stack makes the behaviour intermittent and confusing.
    _preflight_server_ports()

    # CDP/9222 is intentionally different: Chrome may already be running, and
    # ChromeCdpLauncher knows how to attach to it.
    _launch_cdp_browser()

    panes = [
        MultiPaneConsole.PaneProcess(
            title="Our Remote Server",
            command=_script("cloud_server.py"),
            max_lines=2000,
        ),
        MultiPaneConsole.PaneProcess(
            title="User Account + CDP Browser",
            command=_script("target_server.py"),
            max_lines=2000,
        ),
        MultiPaneConsole.PaneProcess(
            title="Local Python Bridge",
            command=_script("local_helper.py"),
            max_lines=2000,
        ),
    ]

    MultiPaneConsole(panes).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
