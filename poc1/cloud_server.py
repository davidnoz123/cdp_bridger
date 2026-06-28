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
import sys
import threading
import time
from typing import Any

from cdp_tools import ChromeCdpLauncher, ChromeCdpError

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
JOBS_LOCK = threading.Lock()

# Separate queue list for browser EventSource connections.  Browsers receive
# job_sent and result events; helpers receive job events.
BROWSER_CLIENTS: list[queue.Queue[dict[str, Any]]] = []
BROWSER_CLIENTS_LOCK = threading.Lock()

dummy_remote_site_name = "Our Remote"

# The cloud server is allowed to ask for a capture from this demo target origin,
# but it deliberately does not choose an exact page path. The local helper
# finds the currently open target page under this origin.
TARGET_ALLOWED_URL_PREFIX = "http://127.0.0.1:8002/"
CAPTURE_JOB_TYPE = "capture_current_page_from_target_origin"


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


def broadcast(event_type: str, data: dict[str, Any]) -> int:
    """Send an event to all connected SSE helper streams and return the count."""
    with CLIENTS_LOCK:
        clients = list(CLIENTS)
    for q in clients:
        # Copy the payload so later JOBS mutations do not change what was queued.
        q.put({"event": event_type, "data": dict(data)})
    return len(clients)


def broadcast_to_browsers(event_type: str, data: dict[str, Any]) -> None:
    """Send an event to all connected browser EventSource streams."""
    with BROWSER_CLIENTS_LOCK:
        clients = list(BROWSER_CLIENTS)
    for q in clients:
        q.put({"event": event_type, "data": dict(data)})




def find_job_by_id(job_id: str) -> dict[str, Any] | None:
    for job in JOBS:
        if job.get("job_id") == job_id:
            return job
    return None


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that suppresses expected browser/SSE socket abort noise."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)

class CloudHandler(BaseHTTPRequestHandler):
    server_version = "FakeCloudSSE/0.2"

    def log_message(self, fmt, *args):
        print(f"[cloud] {self.address_string()} - {fmt % args}")

    def handle_error(self, request, client_address):
        # Suppress noisy tracebacks for routine connection-reset events that
        # happen whenever a browser closes a keep-alive or SSE connection.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        import traceback
        traceback.print_exc()

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
            with JOBS_LOCK:
                pending = [j for j in JOBS if j.get("status") == "pending"]
                results_json = json.dumps(RESULTS[-5:], indent=2, ensure_ascii=False)
            with CLIENTS_LOCK:
                connected = len(CLIENTS)
            body = f"""
<h1>{dummy_remote_site_name} Server</h1>
<p>This pretends to be your web app / cloud job server.</p>
<div class="box">
  <form method="post" action="/create-demo-job">
    <button type="submit">Create demo capture job</button>
  </form>
</div>
<p><strong>Connected SSE helpers:</strong> <span class="ok">{connected}</span></p>
<p><strong>Pending jobs:</strong> {len(pending)}</p>
<h2>Recent results <small id="results-status" style="font-weight:normal;"></small></h2>
<pre id="results-box">{results_json}</pre>
<script>
(function () {{
    var box = document.getElementById('results-box');
    var status = document.getElementById('results-status');
    var results = [];
    try {{ results = JSON.parse(box.textContent) || []; }} catch (e) {{}}
    function render() {{
        box.textContent = JSON.stringify(results.slice(-5), null, 2);
    }}
    var es = new EventSource('/api/browser-events');
    es.addEventListener('job_sent', function (e) {{
        var d = JSON.parse(e.data);
        status.style.color = '#a60';
        status.textContent = d.job_id + ' ' + (d.delivery || 'sent') + ' at ' + d.sent_at + ' \u2014 waiting for result\u2026';
    }});
    es.addEventListener('result', function (e) {{
        results.push(JSON.parse(e.data));
        render();
        status.style.color = '#080';
        status.textContent = 'result received ' + new Date().toLocaleTimeString();
    }});
    es.onerror = function () {{
        status.style.color = '#c00';
        status.textContent = 'SSE disconnected \u2014 reconnecting\u2026';
    }};
}})();
</script>
<p>Helper API:</p>
<ul>
  <li><code>GET /api/events</code> &mdash; SSE stream for cloud-to-helper jobs</li>
  <li><code>POST /api/result</code> &mdash; helper-to-cloud result upload</li>
  <li><code>GET /api/results</code> &mdash; inspect all results</li>
</ul>
"""
            self.send_html(dummy_remote_site_name, body)
            return

        if path == "/api/events":
            self.handle_sse_events()
            return

        if path == "/api/browser-events":
            self.handle_browser_sse_events()
            return

        # Kept as a debugging endpoint; the helper no longer uses it.
        if path == "/api/jobs":
            with JOBS_LOCK:
                pending = [j for j in JOBS if j.get("status") == "pending"]
            self.send_json({"jobs": pending})
            return

        if path == "/api/results":
            with JOBS_LOCK:
                results_snapshot = list(RESULTS)
            self.send_json({"results": results_snapshot})
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
            # Important: jobs stay pending until a helper actually connects.
            with JOBS_LOCK:
                pending_jobs = [j for j in JOBS if j.get("status") == "pending"]
                for job in pending_jobs:
                    job["status"] = "sent"
                    job["sent_at"] = now()
                pending_jobs_to_send = [dict(job) for job in pending_jobs]
            for job in pending_jobs_to_send:
                self.write_sse("job", job)
                broadcast_to_browsers("job_sent", {
                    "job_id": job["job_id"],
                    "sent_at": job.get("sent_at", job.get("created_at")),
                    "delivery": "sent_to_reconnected_helper",
                })

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

    def handle_browser_sse_events(self) -> None:
        q: queue.Queue[dict[str, Any]] = queue.Queue()
        with BROWSER_CLIENTS_LOCK:
            BROWSER_CLIENTS.append(q)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            while True:
                try:
                    message = q.get(timeout=15)
                    self.write_sse(message["event"], message["data"])
                except queue.Empty:
                    self.wfile.write(f": heartbeat {now()}\n\n".encode("utf-8"))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            self.log_message("browser SSE client disconnected: %s", e)
        finally:
            with BROWSER_CLIENTS_LOCK:
                if q in BROWSER_CLIENTS:
                    BROWSER_CLIENTS.remove(q)

    def do_POST(self):
        global JOBS, RESULTS, NEXT_JOB_ID
        path = urlparse(self.path).path

        if path == "/create-demo-job":
            created_at = now()
            with JOBS_LOCK:
                job = {
                    "job_id": f"job_{NEXT_JOB_ID}",
                    "created_at": created_at,
                    # Keep the job pending until at least one helper has really
                    # received it. This avoids losing jobs created before the
                    # SSE helper stream is connected.
                    "status": "pending",
                    "result_received": False,
                    # High-level instruction only. The cloud does not send raw
                    # CDP commands and does not choose the exact tab. The local
                    # helper finds the currently open target page under this
                    # allowed origin and captures that page.
                    "type": CAPTURE_JOB_TYPE,
                    "allowed_url_prefix": TARGET_ALLOWED_URL_PREFIX,
                }
                NEXT_JOB_ID += 1
                JOBS.append(job)

            # Broadcast outside the lock to avoid holding it during I/O.
            delivered_to_helpers = broadcast("job", job)

            with JOBS_LOCK:
                if delivered_to_helpers:
                    job["status"] = "sent"
                    job["sent_at"] = now()
                    delivery = "sent"
                else:
                    delivery = "queued_waiting_for_helper"

            broadcast_to_browsers("job_sent", {
                "job_id": job["job_id"],
                "sent_at": job.get("sent_at", job["created_at"]),
                "delivery": delivery,
                "connected_helpers": delivered_to_helpers,
            })

            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return

        if path == "/api/result":
            try:
                result = self.read_json()
            except Exception as exc:
                self.send_json({
                    "ok": False,
                    "error": "invalid or incomplete JSON POST body",
                    "detail": repr(exc),
                }, status=400)
                return

            job_id = result.get("job_id")
            if not isinstance(job_id, str) or not job_id:
                self.send_json({
                    "ok": False,
                    "error": "result POST missing required string job_id",
                }, status=400)
                return

            received_at = now()
            with JOBS_LOCK:
                job = find_job_by_id(job_id)
                if job is None:
                    self.send_json({
                        "ok": False,
                        "error": "result POST references unknown job_id",
                        "job_id": job_id,
                    }, status=404)
                    return

                if job.get("status") in {"complete", "failed"}:
                    self.send_json({
                        "ok": False,
                        "error": "duplicate final result for job_id",
                        "job_id": job_id,
                        "current_status": job.get("status"),
                    }, status=409)
                    return

                # At this point the cloud server has received a complete HTTP
                # request body, parsed valid JSON, correlated it to a known
                # job_id, and accepted it as the one final result for that job.
                result["received_at"] = received_at
                result["job_status_before_result"] = job.get("status")

                job["status"] = "complete" if result.get("ok") else "failed"
                job["finished_at"] = received_at
                job["result_received"] = True
                job["result_ok"] = bool(result.get("ok"))

                RESULTS.append(result)

            broadcast_to_browsers("result", result)
            self.send_json({
                "ok": True,
                "job_id": job_id,
                "job_status": "complete" if result.get("ok") else "failed",
                "accepted_at": received_at,
            })
            return

        self.send_json({"ok": False, "error": "not found"}, status=404)


def main():
    httpd = QuietThreadingHTTPServer((HOST, PORT), CloudHandler)
    base_url = f"http://{HOST}:{PORT}/"
    print(f"{dummy_remote_site_name} server running at {base_url}")
    print(f"SSE stream available at http://{HOST}:{PORT}/api/events")

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    # Chrome is guaranteed to be running by main.py; attach to it.
    try:
        chrome = ChromeCdpLauncher.launch(reuse_existing_if_available=True)
        existing = [
            t for t in chrome.list_targets()
            if t.get("type") == "page" and str(t.get("url", "")).startswith(base_url)
        ]
        if existing:
            print(f"Cloud UI tab already open: {existing[0].get('url')}")
        else:
            print(f"Opening cloud UI: {base_url}")
            chrome.open_url_via_cdp(base_url)
    except ChromeCdpError as e:
        print(f"Could not open cloud UI tab: {e}", file=sys.stderr)

    try:
        server_thread.join()
    except KeyboardInterrupt:
        print("Stopping...")
        httpd.shutdown()


if __name__ == "__main__":
    main()
