#!/usr/bin/env python3
"""
our_cloud_server.py

A tiny fake cloud/web server running locally, using SSE + POST.

Run:
    python our_cloud_server.py

Open:
    http://127.0.0.1:8001/

The local bridge opens a persistent SSE stream:
    GET /api/events

When the web UI creates a high-level job, the cloud server pushes it down
that SSE stream as:

    event: job
    data: {...json...}

The bridge still reports back with an ordinary POST:
    POST /api/result

This remains deliberately local and standard-library only.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
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

# Each connected bridge gets one Queue. Creating a job broadcasts an SSE event
# to every currently connected bridge. ThreadingHTTPServer is fine for this POC;
# a production version should use an async server or another scalable event layer.
CLIENTS: list[queue.Queue[dict[str, Any]]] = []
CLIENTS_LOCK = threading.Lock()
JOBS_LOCK = threading.Lock()

# Separate queue list for browser EventSource connections.  Browsers receive
# job_sent and result events; bridges receive job events.
BROWSER_CLIENTS: list[queue.Queue[dict[str, Any]]] = []
BROWSER_CLIENTS_LOCK = threading.Lock()

dummy_remote_site_name = "Our Cloud"

# The cloud server is allowed to ask for a capture from this demo target origin,
# but it deliberately does not choose an exact page path. The local bridge
# finds the currently open target page under this origin.
TARGET_ALLOWED_URL_PREFIX = ["http://127.0.0.1:8002/", "https://chatgpt.com/"]
CAPTURE_JOB_TYPE = "capture_current_page_from_target_origin"

BRIDGE_STATUS: dict[str, Any] = {}
BRIDGE_STATUS_LOCK = threading.Lock()


def as_prefix_list(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


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
    .capture-grid {{ display: grid; grid-template-columns: auto minmax(260px, 1fr) minmax(260px, 1.2fr); gap: 14px; align-items: center; }}
    .capture-action button {{ white-space: nowrap; }}
    .capture-target select {{ width: 100%; font-size: 15px; padding: 4px 8px; }}
    .capture-status p {{ margin: 0; }}
    @media (max-width: 760px) {{ .capture-grid {{ grid-template-columns: 1fr; }} }}
    .latest-table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; }}
    .latest-table th, .latest-table td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; vertical-align: top; }}
    .latest-table th {{ background: #f5f5f5; }}
    .latest-table td {{ word-break: break-word; }}
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
    """Send an event to all connected SSE bridge streams and return the count."""
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

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {k: v[-1] if v else "" for k, v in parsed.items()}

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
            parsed_url = urlparse(self.path)
            query = parse_qs(parsed_url.query, keep_blank_values=True)
            target_prefixes = as_prefix_list(TARGET_ALLOWED_URL_PREFIX)
            requested_selected_prefix = (query.get("selected_prefix") or [""])[-1]
            selected_prefix = (
                requested_selected_prefix
                if requested_selected_prefix in target_prefixes
                else (target_prefixes[0] if target_prefixes else "")
            )

            def option_html(prefix: str) -> str:
                selected = " selected" if prefix == selected_prefix else ""
                # These prefixes are controlled by this demo config, but keep the
                # HTML generation safe and boring.
                escaped = (
                    prefix.replace("&", "&amp;")
                    .replace("<", "&lt;")
                    .replace(">", "&gt;")
                    .replace('"', "&quot;")
                )
                return f'      <option value="{escaped}"{selected}>{escaped}</option>'

            options_html = "\n".join(option_html(p) for p in target_prefixes)
            target_prefixes_json = json.dumps(target_prefixes, ensure_ascii=False)
            with JOBS_LOCK:
                pending = [j for j in JOBS if j.get("status") == "pending"]
            with CLIENTS_LOCK:
                connected = len(CLIENTS)
            with JOBS_LOCK:
                results_snapshot = list(reversed(RESULTS[-20:]))
            results_json = json.dumps(results_snapshot, indent=2, ensure_ascii=False)
            with BRIDGE_STATUS_LOCK:
                bridge_status_snapshot = dict(BRIDGE_STATUS)
            bridge_status_json = json.dumps(bridge_status_snapshot, ensure_ascii=False)
            body = f"""
<h1>{dummy_remote_site_name} Server UI</h1>
<p>This is where our users configure software we have deployed for them.<br/>In this demo, our software is a simple "web-scrape" capture of a page the user has open in their local browser.</p>
<div class="box">
  <form method="post" action="/create-demo-job" class="capture-form">
    <div class="capture-grid">
      <div class="capture-action">
        <button type="submit">Create capture job</button>
      </div>
      <div class="capture-target">
        <!--<label for="allowed-url-prefix"><strong>Capture target</strong></label><br>-->
        <select id="allowed-url-prefix" name="allowed_url_prefix">
{options_html}
        </select>
      </div>
      <div class="capture-status">
        <p id="bridge-prefix-status"></p>
      </div>
    </div>
  </form>
</div>
<!--<p><strong>Connected SSE bridges:</strong> <span class="ok">{connected}</span></p>
<p><strong>Pending jobs:</strong> {len(pending)}</p>-->
<section class="box">
  <h2>Latest capture</h2>
  <div id="latest-friendly">No capture yet.</div>
</section>
<h2>Raw results JSON <small id="results-status" style="font-weight:normal;"></small></h2>
<pre id="results-box"></pre>
<script>
(function () {{
    var results = {results_json};
    var bridgeStatus = {bridge_status_json};
    var targetPrefixes = {target_prefixes_json};

    function escapeHtml(s) {{
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }}

    function selectedPrefix() {{
        return document.getElementById('allowed-url-prefix').value;
    }}

    function bridgeSupportsPrefix(prefix) {{
        if (!bridgeStatus || !Array.isArray(bridgeStatus.allowed_target_prefixes)) {{
            return null;
        }}
        return bridgeStatus.allowed_target_prefixes.indexOf(prefix) !== -1;
    }}

    function renderBridgePrefixStatus() {{
        var el = document.getElementById('bridge-prefix-status');
        var prefix = selectedPrefix();
        var supported = bridgeSupportsPrefix(prefix);
        if (supported === null) {{
            el.textContent = 'No bridge capability report received yet.';
            el.style.color = '#a60';
        }} else if (supported) {{
            el.textContent = '\u2713 Local bridge supports this target.';
            el.style.color = '#080';
        }} else {{
            el.textContent = '\u26a0 Warning: local bridge does not report support for this target. The job will likely fail policy validation.';
            el.style.color = '#c00';
        }}
    }}

    function latestSummaryTable(r) {{
        var statusText = r.ok ? 'OK' : 'Failed';
        var statusColor = r.ok ? '#080' : '#c00';
        return '' +
            '<table class="latest-table">' +
            '<thead><tr>' +
            '<th style="width:155px">Received</th>' +
            '<th style="width:70px">Job</th>' +
            '<th style="width:62px">Status</th>' +
            '<th style="min-width:200px">Captured URL</th>' +
            '<th>Title</th>' +
            '</tr></thead>' +
            '<tbody><tr>' +
            '<td>' + escapeHtml(r.received_at || '') + '</td>' +
            '<td>' + escapeHtml(r.job_id || '') + '</td>' +
            '<td><span style="color:' + statusColor + '">' + escapeHtml(statusText) + '</span></td>' +
            '<td>' + escapeHtml(r.captured_from_url || '') + '</td>' +
            '<td>' + escapeHtml(r.captured_title || '') + '</td>' +
            '</tr></tbody>' +
            '</table>';
    }}

    function renderLatestFriendly() {{
        var el = document.getElementById('latest-friendly');
        if (!results.length) {{
            el.textContent = 'No capture yet.';
            return;
        }}
        var r = results[0];
        var tableHtml = latestSummaryTable(r);
        if (!r.ok) {{
            el.innerHTML =
                tableHtml +
                '<p><strong>Error:</strong> ' + escapeHtml(r.error || '') + '</p>';
            return;
        }}
        var preview = (r.visible_text || '').slice(0, 500);
        var areasHtml = '';
        if (r.areas && typeof r.areas === 'object') {{
            var keys = Object.keys(r.areas);
            if (keys.length) {{
                areasHtml = '<h3>Textarea values</h3><ul>' +
                    keys.map(function (k) {{
                        return '<li><strong>' + escapeHtml(k) + ':</strong> ' +
                               escapeHtml((r.areas[k] || '').slice(0, 200)) + '</li>';
                    }}).join('') + '</ul>';
            }}
        }}
        el.innerHTML =
            tableHtml +
            '<h3>Preview</h3><pre>' + escapeHtml(preview) + '</pre>' +
            areasHtml;
    }}

    function renderRawResults() {{
        document.getElementById('results-box').textContent =
            JSON.stringify(results.slice(0, 20), null, 2);
    }}

    function render() {{
        renderLatestFriendly();
        renderRawResults();
    }}

    render();
    renderBridgePrefixStatus();
    document.getElementById('allowed-url-prefix').addEventListener('change', renderBridgePrefixStatus);

    var status = document.getElementById('results-status');
    var es = new EventSource('/api/browser-events');
    es.addEventListener('job_sent', function (e) {{
        var d = JSON.parse(e.data);
        status.style.color = '#a60';
        status.textContent = d.job_id + ' ' + (d.delivery || 'sent') + ' at ' + d.sent_at +
            ' for ' + (d.allowed_url_prefix || selectedPrefix()) + ' \u2014 waiting for result\u2026';
    }});
    es.addEventListener('result', function (e) {{
        results.unshift(JSON.parse(e.data));
        render();
        status.style.color = '#080';
        status.textContent = 'result received ' + new Date().toLocaleTimeString();
    }});
    es.addEventListener('bridge_status', function (e) {{
        bridgeStatus = JSON.parse(e.data);
        renderBridgePrefixStatus();
    }});
    es.onerror = function () {{
        status.style.color = '#c00';
        status.textContent = 'SSE disconnected \u2014 reconnecting\u2026';
    }};
}})();
</script>
<p>Bridge API:</p>
<ul>
  <li><code>GET /api/events</code> &mdash; SSE stream for cloud-to-bridge jobs</li>
  <li><code>POST /api/result</code> &mdash; bridge-to-cloud result upload</li>
  <li><code>POST /api/bridge-status</code> &mdash; bridge capability reporting</li>
  <li><code>GET /api/results</code> &mdash; inspect all results</li>
</ul>
"""
            self.send_html(f"{dummy_remote_site_name} Server UI", body)
            return

        if path == "/api/events":
            self.handle_sse_events()
            return

        if path == "/api/browser-events":
            self.handle_browser_sse_events()
            return

        # Kept as a debugging endpoint; the bridge no longer uses it.
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

        if path == "/api/bridge-status":
            with CLIENTS_LOCK:
                connected = len(CLIENTS)
            with BRIDGE_STATUS_LOCK:
                status_snapshot = dict(BRIDGE_STATUS)
            self.send_json({
                "ok": True,
                "connected_bridges": connected,
                "bridge_status": status_snapshot,
            })
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
            self.write_sse("hello", {"ok": True, "server_time": now(), "connected_bridges": connected})

            # If jobs already exist when the bridge connects, push them immediately.
            # Important: jobs stay pending until a bridge actually connects.
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
                    "delivery": "sent_to_reconnected_bridge",
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
            form = self.read_form()
            target_prefixes = as_prefix_list(TARGET_ALLOWED_URL_PREFIX)
            selected_prefix = form.get("allowed_url_prefix", "").strip()
            if not selected_prefix:
                self.send_json({"ok": False, "error": "missing allowed_url_prefix"}, status=400)
                return
            if selected_prefix not in target_prefixes:
                self.send_json({
                    "ok": False,
                    "error": "selected allowed_url_prefix is not configured on this cloud server",
                    "selected_allowed_url_prefix": selected_prefix,
                    "configured_allowed_url_prefixes": target_prefixes,
                }, status=400)
                return
            created_at = now()
            with JOBS_LOCK:
                job = {
                    "job_id": f"job_{NEXT_JOB_ID}",
                    "created_at": created_at,
                    # Keep the job pending until at least one bridge has really
                    # received it. This avoids losing jobs created before the
                    # SSE bridge stream is connected.
                    "status": "pending",
                    "result_received": False,
                    # High-level instruction only. The cloud does not send raw
                    # CDP commands and does not choose the exact tab. The local
                    # bridge finds the currently open target page under this
                    # allowed origin and captures that page.
                    "type": CAPTURE_JOB_TYPE,
                    "allowed_url_prefix": selected_prefix,
                }
                NEXT_JOB_ID += 1
                JOBS.append(job)

            # Broadcast outside the lock to avoid holding it during I/O.
            delivered_to_bridges = broadcast("job", job)

            with JOBS_LOCK:
                if delivered_to_bridges:
                    job["status"] = "sent"
                    job["sent_at"] = now()
                    delivery = "sent"
                else:
                    delivery = "queued_waiting_for_bridge"

            broadcast_to_browsers("job_sent", {
                "job_id": job["job_id"],
                "sent_at": job.get("sent_at", job["created_at"]),
                "delivery": delivery,
                "connected_bridges": delivered_to_bridges,
                "allowed_url_prefix": selected_prefix,
            })

            self.send_response(303)
            self.send_header("Location", "/?selected_prefix=" + quote(selected_prefix, safe=""))
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

        if path == "/api/bridge-status":
            try:
                status_data = self.read_json()
            except Exception as exc:
                self.send_json({"ok": False, "error": "invalid JSON", "detail": repr(exc)}, status=400)
                return
            received_at = now()
            status_data["received_at"] = received_at
            with BRIDGE_STATUS_LOCK:
                BRIDGE_STATUS.clear()
                BRIDGE_STATUS.update(status_data)
            broadcast_to_browsers("bridge_status", dict(BRIDGE_STATUS))
            self.send_json({"ok": True, "received_at": received_at})
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
