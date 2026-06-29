#!/usr/bin/env python3
"""
target_server.py

A tiny fake target website running locally.

Run:
    python target_server.py

Open in the CDP Chrome profile:
    http://127.0.0.1:8002/

Click login, then open the account page.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.parse import urlparse
import json
import random
import sys
import threading
import time

from cdp_tools import ChromeCdpLauncher, ChromeCdpError, MinimalWebSocket

HOST = "127.0.0.1"
PORT = 8002
SESSION_COOKIE_NAME = "target_session"
SESSION_COOKIE_VALUE = "demo-user-session"

dummy_target_site_name = "User's Private"


def html_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 40px auto; line-height: 1.45; }}
    code, pre {{ background: #f3f3f3; padding: 2px 4px; border-radius: 4px; }}
    .box {{ border: 1px solid #ccc; padding: 16px; border-radius: 8px; }}
  </style>
</head>
<body>
{body}
</body>
</html>""".encode("utf-8")


class TargetHandler(BaseHTTPRequestHandler):
    server_version = "FakeTarget/0.1"

    def log_message(self, fmt, *args):
        print(f"[target] {self.address_string()} - {fmt % args}")

    def get_cookie_value(self, name: str) -> str | None:
        raw = self.headers.get("Cookie", "")
        cookie = SimpleCookie(raw)
        if name in cookie:
            return cookie[name].value
        return None

    def is_logged_in(self) -> bool:
        return self.get_cookie_value(SESSION_COOKIE_NAME) == SESSION_COOKIE_VALUE

    def send_html(self, title: str, body: str, status: int = 200, headers: dict[str, str] | None = None):
        data = html_page(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/":
            status = "logged in" if self.is_logged_in() else "not logged in"
            self.send_html(f"{dummy_target_site_name} Target", f"""
<h1>{dummy_target_site_name} Target Website</h1>
<p>This server pretends to be a third-party website.</p>
<p>Status: <strong>{status}</strong></p>
<ul>
  <li><a href="/login">Login / create demo session cookie</a></li>
  <li><a href="/account">Open account page</a></li>
  <li><a href="/logout">Logout</a></li>
</ul>
""")
            return

        if path == "/login":
            self.send_html(
                f"{dummy_target_site_name} Logged in",
                f"""
<h1>{dummy_target_site_name} Logged in</h1>
<p>A demo session cookie has been set in this browser profile.</p>
<p><a href="/account">Go to account page</a></p>
""",
                headers={
                    "Set-Cookie": f"{SESSION_COOKIE_NAME}={SESSION_COOKIE_VALUE}; Path=/; SameSite=Lax"
                },
            )
            return

        if path == "/logout":
            self.send_html(
                f"{dummy_target_site_name} Logged out",
                f"""
<h1>{dummy_target_site_name} Logged out</h1>
<p>The demo session cookie has been cleared.</p>
<p><a href="/">Home</a></p>
""",
                headers={
                    "Set-Cookie": f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax"
                },
            )
            return

        if path == "/account":
            if not self.is_logged_in():
                self.send_html(f"{dummy_target_site_name} Not logged in", f"""
<h1>{dummy_target_site_name} Not logged in</h1>
<p>The account page requires the demo browser session cookie.</p>
<p><a href="/login">Login</a></p>
""", status=401)
                return

            self.send_html(f"{dummy_target_site_name} Account", f"""
<h1>{dummy_target_site_name} Account</h1>
<div class="box" id="account-data">
  <p><strong>Private note:</strong> This text is visible only because this browser is logged in.</p>
  <textarea id="text-editor" rows="6" style="width:100%;box-sizing:border-box;font-family:inherit;font-size:1em;padding:8px;border:1px solid #ccc;border-radius:4px;resize:vertical;">Edit this text…</textarea>
</div>
<p>This is the page the Local Python Bridge will read through CDP, without reading cookies directly.</p>
""")
            return

        self.send_html("404", "<h1>404</h1>", status=404)


def _wait_for_tab(chrome: ChromeCdpLauncher, url_prefix: str, timeout: float = 10.0) -> dict | None:
    """Poll list_targets() until a page tab whose URL starts with url_prefix appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for target in chrome.list_targets():
                if target.get("type") == "page" and str(target.get("url", "")).startswith(url_prefix):
                    return target
        except ChromeCdpError:
            pass
        time.sleep(0.2)
    return None


def _navigate_tab_to(ws_url: str, url: str, timeout: float = 10.0) -> None:
    """
    Navigate an already-open tab to a new URL via Page.navigate.

    Using this instead of Target.createTarget avoids opening an extra tab and
    guarantees the previous page's response (e.g. Set-Cookie) has been fully
    processed before the new navigation starts.
    """
    ws = MinimalWebSocket(ws_url, timeout=timeout)
    cmd_id = random.randint(1, 2_000_000_000)
    try:
        ws.connect()
        ws.send_text(json.dumps({"id": cmd_id, "method": "Page.navigate", "params": {"url": url}}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(ws.recv_text())
            if msg.get("id") == cmd_id:
                if "error" in msg:
                    raise ChromeCdpError(f"Page.navigate failed: {msg['error']!r}")
                return
        raise ChromeCdpError("Timed out waiting for Page.navigate response")
    finally:
        ws.close()


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), TargetHandler)
    
    base_address = f"http://{HOST}:{PORT}/"
    print(f"{dummy_target_site_name} target website running at {base_address}")

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    login_url = f"{base_address}login"
    account_url = f"{base_address}account"

    # Chrome is guaranteed to be running by main.py; attach to it.
    try:
        chrome = ChromeCdpLauncher.launch(reuse_existing_if_available=True)

        # Avoid duplicate tabs if the account page is already open from a previous run.
        existing = [
            t for t in chrome.list_targets()
            if t.get("type") == "page" and str(t.get("url", "")).startswith(account_url)
        ]
        if existing:
            print(f"Account tab already open: {existing[0].get('url')}")
        else:
            # Open the login page so Chrome stores the session cookie.
            print(f"Opening login page: {login_url}")
            chrome.open_url_via_cdp(login_url)

            # Wait until Chrome has committed the login navigation before
            # proceeding — the URL only appears in list_targets() after the
            # response headers (including Set-Cookie) have been processed.
            login_tab = _wait_for_tab(chrome, login_url)
            if login_tab is None:
                raise ChromeCdpError("Login tab did not appear within timeout")

            # Navigate the same tab to the account page to avoid an extra tab
            # and any cookie race.
            ws_url = login_tab.get("webSocketDebuggerUrl")
            if not ws_url:
                raise ChromeCdpError("Login tab has no webSocketDebuggerUrl")
            print(f"Navigating to account page: {account_url}")
            _navigate_tab_to(ws_url, account_url)
            print("Account tab ready.")
    except ChromeCdpError as e:
        print(f"Could not open target tabs: {e}", file=sys.stderr)

    try:
        server_thread.join()
    except KeyboardInterrupt:
        print("Stopping...")
        httpd.shutdown()


if __name__ == "__main__":
    main()
