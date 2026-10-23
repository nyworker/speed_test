#!/usr/bin/env python3
"""Minimal bandwidth test server.

Serves index.html plus these endpoints:
  GET  /download?duration=S&mbps=M  stream random bytes for S seconds, paced to M Mbps (0 = unlimited)
  POST /upload                      read and discard the request body
  GET  /ping                        tiny response for latency measurement
  POST /begin?test=ID               claim the server for one test; 503 if another test is running
  POST /end?test=ID                 release it again
  GET  /config                      server limits as JSON
  GET  /results                     recent test results (newest first), shared by all browsers
  POST /results                     store one result; the server adds time and src/dst IPs

Standard library only:  python3 server.py --port 8080 --max-mbps 500
"""
import argparse
import json
import math
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.1"
HERE = os.path.dirname(os.path.abspath(__file__))
BLOB = os.urandom(1024 * 1024)  # incompressible payload, reused
MAX_CHUNK = 64 * 1024
MAX_DURATION = 60
BYTES_PER_MBIT = 125_000

SERVER_CAP_MBPS = 0.0  # set from --max-mbps; 0 = no server-side cap
IDLE_SECONDS = 2.0  # a flow with no traffic this long is reported as finished
LEASE_SECONDS = 3.0  # a test with no requests this long loses its claim on the server


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def fmt_limit(mbps):
    return f"{mbps:g} Mbps" if mbps > 0 else "unlimited"


class Monitor:
    """Tracks bytes per flow and prints live throughput with source/destination IPs.

    An upload test is many POST requests, so traffic is grouped into a flow per
    (client IP, server IP, direction); a flow ends after IDLE_SECONDS without traffic.
    """

    def __init__(self, interval):
        self.interval = interval
        self.lock = threading.Lock()
        self.flows = {}
        self.log_file = None  # set from --log in main()

    def add(self, client, server, direction, n, limit_mbps):
        now = time.monotonic()
        key = (client, server, direction)
        with self.lock:
            flow = self.flows.get(key)
            if flow is None:
                flow = self.flows[key] = {
                    "start": now, "last": now, "tick": now, "total": 0, "reported": 0,
                    "limit": limit_mbps, "announced": False,
                }
            flow["total"] += n
            flow["last"] = now

    @staticmethod
    def label(client, server, direction):
        src, dst = (server, client) if direction == "download" else (client, server)
        return f"{direction:<8}  src {src:<15} -> dst {dst:<15}"

    def run(self):
        while True:
            time.sleep(self.interval)
            now = time.monotonic()
            lines = []
            with self.lock:
                for key, f in sorted(self.flows.items()):
                    label = self.label(*key)
                    if not f["announced"]:
                        f["announced"] = True
                        lines.append(f"{label}  started (limit {fmt_limit(f['limit'])})")
                    # Rate since the previous line (or since the flow started, on the first one)
                    delta = f["total"] - f["reported"]
                    mbps = delta * 8 / 1e6 / max(now - f["tick"], 1e-6)
                    f["reported"] = f["total"]
                    f["tick"] = now
                    if delta:
                        lines.append(f"{label}  {mbps:9.1f} Mbps   {fmt_bytes(f['total']):>9} total")
                    if now - f["last"] > IDLE_SECONDS:
                        secs = max(f["last"] - f["start"], 1e-6)
                        avg = f["total"] * 8 / 1e6 / secs
                        lines.append(f"{label}  finished: avg {avg:.1f} Mbps, "
                                     f"{fmt_bytes(f['total'])} in {secs:.1f} s")
                        del self.flows[key]
            if not lines:
                continue
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            text = "".join(f"[{stamp}] {line}\n" for line in lines)
            sys.stdout.write(text)
            sys.stdout.flush()
            if self.log_file:
                with open(self.log_file, "a") as f:
                    f.write(text)


MONITOR = Monitor(1.0)  # interval replaced from --interval in main()


class Results:
    """Recent test results, kept in memory and saved to a JSON file so they survive restarts."""

    def __init__(self, path, keep):
        self.path = path
        self.keep = keep
        self.lock = threading.Lock()
        try:
            with open(path) as f:
                rows = json.load(f)
            self.rows = rows[-keep:] if isinstance(rows, list) else []
        except (OSError, ValueError):
            self.rows = []

    def add(self, row):
        with self.lock:
            self.rows.append(row)
            del self.rows[:-self.keep]
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.rows, f)
            os.replace(tmp, self.path)

    def recent(self):
        with self.lock:
            return self.rows[::-1]


RESULTS = None  # created in main() from --results / --keep


class Gate:
    """Lets one test run at a time, so concurrent tests don't split the link and skew results.

    A test is identified by the id the page sends with each request. It holds the gate
    while its requests keep arriving, and loses it at /end or LEASE_SECONDS after the last one.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None
        self.client = None
        self.last = 0.0

    def claim(self, test, client):
        """Take or refresh the gate for this test. Returns the busy client's IP, or None on success."""
        now = time.monotonic()
        with self.lock:
            if self.owner not in (None, test) and now - self.last < LEASE_SECONDS:
                return self.client
            self.owner, self.client, self.last = test, client, now
            return None

    def release(self, test):
        with self.lock:
            if self.owner == test:
                self.owner = None


GATE = Gate()


def effective_mbps(requested):
    """Client-requested rate, clamped to the server cap if one is set."""
    if SERVER_CAP_MBPS > 0 and (requested <= 0 or requested > SERVER_CAP_MBPS):
        return SERVER_CAP_MBPS
    return max(requested, 0.0)


class Pacer:
    """Sleeps as needed so that bytes moved never run ahead of the target rate."""

    def __init__(self, mbps):
        self.bps = mbps * BYTES_PER_MBIT
        self.start = time.monotonic()
        self.moved = 0

    def chunk_size(self):
        # Smaller chunks at low rates keep the stream smooth (~20 chunks/s).
        if self.bps <= 0:
            return MAX_CHUNK
        return int(max(4096, min(MAX_CHUNK, self.bps / 20)))

    def account(self, n):
        self.moved += n
        if self.bps > 0:
            ahead = self.moved / self.bps - (time.monotonic() - self.start)
            if ahead > 0:
                time.sleep(ahead)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"BandwidthTest/{VERSION}"

    def log_message(self, fmt, *args):
        # Test traffic is reported by the Monitor instead of per request.
        if not self.path.startswith(("/ping", "/upload", "/download")):
            super().log_message(fmt, *args)

    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8", headers=()):
        self.send_response(code)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _params(self):
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _test_id(self, params):
        # Old pages send no id; fall back to the client IP so they still get one test at a time.
        return params.get("test") or self.client_address[0]

    def _claim(self, params, close=False):
        """Claim the gate for this request's test; if another test holds it, send 503 and return False."""
        busy = GATE.claim(self._test_id(params), self.client_address[0])
        if busy is None:
            return True
        BaseHTTPRequestHandler.log_message(
            self, "rejected %s %s: test from %s is running", self.command, self.path, busy)
        body = json.dumps({"error": "busy", "client": busy}).encode()
        headers = [("Retry-After", str(math.ceil(LEASE_SECONDS)))]
        if close:
            # Don't read the request body: that would eat into the running test's bandwidth.
            headers.append(("Connection", "close"))
            self.close_connection = True
        self._send(503, body, "application/json", headers)
        return False

    @staticmethod
    def _float(params, key, default):
        try:
            return float(params.get(key, default))
        except ValueError:
            return default

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif path == "/ping":
            params = self._params()
            if "test" in params:
                GATE.claim(params["test"], self.client_address[0])  # keeps the claim alive
            self._send(200, b"pong")
        elif path == "/results":
            self._send(200, json.dumps(RESULTS.recent()).encode(), "application/json")
        elif path == "/config":
            body = json.dumps({"version": VERSION, "max_mbps": SERVER_CAP_MBPS,
                               "max_duration": MAX_DURATION})
            self._send(200, body.encode(), "application/json")
        elif path == "/download":
            self._download()
        else:
            self._send(404, b"not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/upload":
            self._upload()
        elif path == "/results":
            self._post_result()
        elif path == "/begin":
            if self._claim(self._params()):
                self._send(200, b"ok")
        elif path == "/end":
            GATE.release(self._test_id(self._params()))
            self._send(200, b"ok")
        else:
            self._send(404, b"not found")

    def _post_result(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 4096:
                raise ValueError("bad length")
            data = json.loads(self.rfile.read(length))
            kind = data["kind"]
            if kind not in ("download", "upload"):
                raise ValueError("bad kind")
            nums = {k: float(data[k]) for k in ("limit_mbps", "avg_mbps", "peak_mbps", "bytes", "seconds")}
            if not all(math.isfinite(v) and v >= 0 for v in nums.values()):
                raise ValueError("bad number")
        except (KeyError, TypeError, ValueError):
            self._send(400, b"bad result")
            return
        # Addresses come from the connection itself, not from the browser.
        client, server = self.client_address[0], self.connection.getsockname()[0]
        src, dst = (server, client) if kind == "download" else (client, server)
        RESULTS.add({"time": time.time(), "kind": kind, "src": src, "dst": dst,
                     **nums, "stopped": bool(data.get("stopped"))})
        self._send(200, json.dumps(RESULTS.recent()).encode(), "application/json")

    def _download(self):
        params = self._params()
        if not self._claim(params):
            return
        test = self._test_id(params)
        duration = min(max(self._float(params, "duration", 10), 1), MAX_DURATION)
        limit = effective_mbps(self._float(params, "mbps", 0))
        pacer = Pacer(limit)
        client, server = self.client_address[0], self.connection.getsockname()[0]

        # No Content-Length: the body ends when we close the connection.
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        view = memoryview(BLOB)
        offset = 0
        deadline = time.monotonic() + duration
        try:
            while time.monotonic() < deadline:
                n = pacer.chunk_size()
                if offset + n > len(BLOB):
                    offset = 0
                self.wfile.write(view[offset:offset + n])
                offset += n
                MONITOR.add(client, server, "download", n, limit)
                GATE.claim(test, client)
                pacer.account(n)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client stopped the test

    def _upload(self):
        length = self.headers.get("Content-Length")
        if length is None:
            self._send(411, b"Content-Length required")
            return
        params = self._params()
        if not self._claim(params, close=True):
            return
        test = self._test_id(params)
        remaining = int(length)
        # Only the server cap is enforced here; the client paces its own requested rate
        # and passes it along just for display.
        pacer = Pacer(SERVER_CAP_MBPS)
        limit = effective_mbps(self._float(params, "mbps", 0))
        client, server = self.client_address[0], self.connection.getsockname()[0]
        received = 0
        try:
            while remaining > 0:
                data = self.rfile.read(min(remaining, pacer.chunk_size()))
                if not data:
                    break
                remaining -= len(data)
                received += len(data)
                MONITOR.add(client, server, "upload", len(data), limit)
                GATE.claim(test, client)
                pacer.account(len(data))
        except (BrokenPipeError, ConnectionResetError):
            return
        self._send(200, json.dumps({"bytes": received}).encode(), "application/json")


def main():
    global SERVER_CAP_MBPS, RESULTS
    ap = argparse.ArgumentParser(description="Bandwidth test server")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--max-mbps", type=float, default=0,
                    help="hard per-test cap clients cannot exceed (0 = none)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between live bandwidth lines (default 1)")
    ap.add_argument("--log", metavar="FILE",
                    help="also append the bandwidth lines to this file")
    ap.add_argument("--results", metavar="FILE", default=os.path.join(HERE, "results.json"),
                    help="where test results are saved (default results.json next to this script)")
    ap.add_argument("--keep", type=int, default=200,
                    help="number of recent results to keep (default 200)")
    args = ap.parse_args()
    SERVER_CAP_MBPS = args.max_mbps
    RESULTS = Results(args.results, max(args.keep, 1))
    MONITOR.interval = max(args.interval, 0.1)
    MONITOR.log_file = args.log
    threading.Thread(target=MONITOR.run, daemon=True).start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    cap = f"{args.max_mbps:g} Mbps" if args.max_mbps else "none"
    print(f"Bandwidth test v{VERSION} on http://{args.host}:{args.port}  (server cap: {cap})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
