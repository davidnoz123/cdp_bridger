#!/usr/bin/env python3
"""
cloud_server.py

A tiny fake cloud/web server running locally, using SSE + POST.

Run:
    python cloud_server.py

Open:
    http://127.0.0.1:8001/

The local helper opens a persistent SSE stream:
    GET /api/events

When the web UI creates a high-level job, the cloud server pushes it down
that SSE stream as:

    event: job
    data: {...json...}

The helper still reports back with an ordinary POST:
    POST /api/result

This remains deliberately local and standard-library only.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import json
import queue
import threading
import time
from typing import Any

HOST = "127.0.0.1"
PORT = 8001

JOBS: list[dict[str, Any]] = []
RESULTS: list[dict[str, Any]] = []
NEXT_JOB_ID = 1

# Each connected helper gets one Queue. Creating a job broadcasts an SSE event
# to every currently connected helper. ThreadingHTTPServer is fine for this POC;
# a production version should use an async server or another scalable event layer.
CLIENTS: list[queue.Queue[dict[str, Any]]] = []
CLIENTS_LOCK = threading.Lock()


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
    .ok {{ color: #080; }}
  </style>
</head>
<body>
{body}
</body>
</html>""".encode("utf-8")


def sse_format(event_type: str, data: dict[str, Any]) -> bytes:
    """Return one Server-Sent Event frame as UTF-8 bytes."""
    json_data = json.dumps(data, ensure_ascii=False)
    # SSE data may span lines; prefix every line with data: if that happens.
    data_lines = "\n".join(f"data: {line}" for line in json_data.splitlines())
    return f"event: {event_type}\n{data_lines}\n\n".encode("utf-8")


def broadcast(event_type: str, data: dict[str, Any]) -> None:
    """Send an event to all connected SSE helper streams."""
    message = {"event": event_type, "data": data}
    with CLIENTS_LOCK:
        clients = list(CLIENTS)
    for q in clients:
        q.put(message)


class CloudHandler(BaseHTTPRequestHandler):
    server_version = "FakeCloudSSE/0.2"

    def log_message(self, fmt, *args):
        print(f"[cloud] {self.address_string()} - {fmt % args}")

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, obj: dict[str, Any], status: int = 200):
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

    def write_sse(self, event_type: str, data: dict[str, Any]) -> None:
        self.wfile.write(sse_format(event_type, data))
        self.wfile.flush()

    def do_GET(self):
        global JOBS, RESULTS
        path = urlparse(self.path).path

        if path == "/":
            pending = [j for j in JOBS if j.get("status") == "pending"]
            results_json = json.dumps(RESULTS[-5:], indent=2, ensure_ascii=False)
            with CLIENTS_LOCK:
                connected = len(CLIENTS)
            body = f"""
<h1>Fake Cloud Server</h1>
<p>This pretends to be your web app / cloud job server.</p>
<div class="box">
  <form method="post" action="/create-demo-job">
    <button type="submit">Create demo capture job</button>
  </form>
</div>
<p><strong>Connected SSE helpers:</strong> <span class="ok">{connected}</span></p>
<p><strong>Pending jobs:</strong> {len(pending)}</p>
<h2>Recent results</h2>
<pre>{results_json}</pre>
<p>Helper API:</p>
<ul>
  <li><code>GET /api/events</code> &mdash; SSE stream for cloud-to-helper jobs</li>
  <li><code>POST /api/result</code> &mdash; helper-to-cloud result upload</li>
  <li><code>GET /api/results</code> &mdash; inspect all results</li>
</ul>
"""
            self.send_html("Fake Cloud", body)
            return

        if path == "/api/events":
            self.handle_sse_events()
            return

        # Kept as a debugging endpoint; the helper no longer uses it.
        if path == "/api/jobs":
            pending = [j for j in JOBS if j.get("status") == "pending"]
            self.send_json({"jobs": pending})
            return

        if path == "/api/results":
            self.send_json({"results": RESULTS})
            return

        self.send_html("404", "<h1>404</h1>", status=404)

    def handle_sse_events(self) -> None:
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        with CLIENTS_LOCK:
            CLIENTS.append(q)
            connected = len(CLIENTS)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            self.write_sse("hello", {"ok": True, "server_time": now(), "connected_helpers": connected})

            # If jobs already exist when the helper connects, push them immediately.
            for job in [j for j in JOBS if j.get("status") == "pending"]:
                job["status"] = "sent"
                self.write_sse("job", job)

            while True:
                try:
                    message = q.get(timeout=15)
                    self.write_sse(message["event"], message["data"])
                except queue.Empty:
                    # SSE comments are standard heartbeat frames; clients ignore them.
                    self.wfile.write(f": heartbeat {now()}\n\n".encode("utf-8"))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            self.log_message("SSE client disconnected: %s", e)
        finally:
            with CLIENTS_LOCK:
                if q in CLIENTS:
                    CLIENTS.remove(q)

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

            # Push to any connected helper immediately.
            job["status"] = "sent"
            broadcast("job", job)

            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if path == "/api/result":
            result = self.read_json()
            result["received_at"] = now()
            RESULTS.append(result)
            for job in JOBS:
                if job.get("job_id") == result.get("job_id"):
                    job["status"] = "done" if result.get("ok") else "error"
                    job["finished_at"] = now()
            self.send_json({"ok": True})
            return

        self.send_json({"ok": False, "error": "not found"}, status=404)


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), CloudHandler)
    print(f"Fake cloud server running at http://{HOST}:{PORT}/")
    print(f"SSE stream available at http://{HOST}:{PORT}/api/events")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
