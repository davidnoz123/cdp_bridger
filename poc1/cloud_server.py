#!/usr/bin/env python3
"""
cloud_server.py

A tiny fake cloud/web server running locally.

Run:
    python cloud_server.py

Open:
    http://127.0.0.1:8001/

The local helper polls this server for one high-level job:
    capture_visible_text_from_target_tab
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import json
import time

HOST = "127.0.0.1"
PORT = 8001

JOBS = []
RESULTS = []
NEXT_JOB_ID = 1


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def html_page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 40px auto; line-height: 1.45; }}
    button {{ font-size: 16px; padding: 8px 12px; }}
    pre {{ background: #f5f5f5; padding: 12px; overflow: auto; border-radius: 8px; }}
    .box {{ border: 1px solid #ccc; padding: 16px; border-radius: 8px; margin: 16px 0; }}
  </style>
</head>
<body>
{body}
</body>
</html>""".encode("utf-8")


class CloudHandler(BaseHTTPRequestHandler):
    server_version = "FakeCloud/0.1"

    def log_message(self, fmt, *args):
        print(f"[cloud] {self.address_string()} - {fmt % args}")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, obj: dict, status: int = 200):
        data = json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, title: str, body: str, status: int = 200):
        data = html_page(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        global JOBS, RESULTS
        path = urlparse(self.path).path

        if path == "/":
            pending = [j for j in JOBS if j.get("status") == "pending"]
            results_json = json.dumps(RESULTS[-5:], indent=2, ensure_ascii=False)
            body = f"""
<h1>Fake Cloud Server</h1>
<p>This pretends to be your web app / cloud job server.</p>
<div class="box">
  <form method="post" action="/create-demo-job">
    <button type="submit">Create demo capture job</button>
  </form>
</div>
<p><strong>Pending jobs:</strong> {len(pending)}</p>
<h2>Recent results</h2>
<pre>{results_json}</pre>
<p>Helper API:</p>
<ul>
  <li><code>GET /api/jobs</code></li>
  <li><code>POST /api/result</code></li>
</ul>
"""
            self.send_html("Fake Cloud", body)
            return

        if path == "/api/jobs":
            pending = [j for j in JOBS if j.get("status") == "pending"]
            for j in pending:
                j["status"] = "sent"
            self.send_json({"jobs": pending})
            return

        if path == "/api/results":
            self.send_json({"results": RESULTS})
            return

        self.send_html("404", "<h1>404</h1>", status=404)

    def do_POST(self):
        global JOBS, RESULTS, NEXT_JOB_ID
        path = urlparse(self.path).path

        if path == "/create-demo-job":
            job = {
                "job_id": f"job_{NEXT_JOB_ID}",
                "created_at": now(),
                "status": "pending",
                "type": "capture_visible_text_from_target_tab",
                "allowed_url_prefix": "http://127.0.0.1:8002/account",
            }
            NEXT_JOB_ID += 1
            JOBS.append(job)
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if path == "/api/result":
            result = self.read_json()
            result["received_at"] = now()
            RESULTS.append(result)
            self.send_json({"ok": True})
            return

        self.send_json({"ok": False, "error": "not found"}, status=404)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), CloudHandler)
    print(f"Fake cloud server running at http://{HOST}:{PORT}/")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
