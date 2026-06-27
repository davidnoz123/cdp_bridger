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

import os
import sys

from multi_command_pane_runner import MultiPaneConsole

_HERE = os.path.dirname(os.path.abspath(__file__))


def _script(name: str) -> list:
    """Return an unbuffered, UTF-8-forced Python command for a script in this directory."""
    return [sys.executable, "-u", "-X", "utf8", os.path.join(_HERE, name)]


def main() -> int:
    panes = [
        MultiPaneConsole.PaneProcess(
            title="Cloud Server",
            command=_script("cloud_server.py"),
            max_lines=2000,
        ),
        MultiPaneConsole.PaneProcess(
            title="Target Server + CDP Browser",
            command=_script("target_server.py"),
            max_lines=2000,
        ),
        MultiPaneConsole.PaneProcess(
            title="Local Helper",
            command=_script("local_helper.py"),
            max_lines=2000,
        ),
    ]

    MultiPaneConsole(panes).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
