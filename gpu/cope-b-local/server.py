"""Local CoPE-B server for developer tools (#437).

Speaks the RunPod worker's wire format, so Charcoal's `RunPodCopeBClient`
talks to it unchanged:

  POST /runsync      {"input": {"contents": ["…", …]}}
               ->    {"id", "status": "COMPLETED", "delayTime": 0,
                      "executionTime": <ms>, "output": {"verdicts": [slot, …]}}
  GET  /status/{id}  the same envelope for a recent job

Developer tooling only: it binds to loopback, has no auth, and the web app
can never select it (`CHARCOAL_CLASSIFIER=local` is rejected on the Rust
side). Run:

  POLICY_VERSION=policy-ddac468-2026-07-03 \\
    ~/.venvs/cope-b-mlx/bin/python gpu/cope-b-local/server.py \\
    --model ~/models/cope-b-a4b-mlx-8bit
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import socketserver
import sys
import threading
import time
import uuid
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 8765
LOOPBACK_HOSTS = ("127.0.0.1", "::1")
# /status only needs to find a job the client just ran; keep a few.
KEPT_JOBS = 64


def require_loopback(host: str) -> str:
    if host not in LOOPBACK_HOSTS:
        raise SystemExit(f"refusing to bind {host!r}: the local CoPE-B server is "
                         f"loopback-only ({', '.join(LOOPBACK_HOSTS)})")
    return host


def require_policy_version(value: str | None) -> str:
    """No default: a server reporting the wrong policy version would defeat
    the client's policy check (the #385 incident), so refuse to start."""
    if value is None or not value.strip():
        raise SystemExit("POLICY_VERSION must be set to the deployed policy version, "
                         "e.g. policy-ddac468-2026-07-03")
    return value.strip()


def classify_batch(engine, contents: list[str]) -> list[dict]:
    """One slot per input, in order. A post that raises becomes an ok:false
    slot for that post only, like the RunPod worker's per-slot isolation."""
    slots = []
    for content in contents:
        try:
            slots.append(engine.classify_one(content))
        except Exception as exc:  # noqa: BLE001 — deliberate per-slot isolation
            slots.append({"ok": False, "error": str(exc)})
    return slots


class _LoopbackHTTPServer(ThreadingHTTPServer):
    # HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup
    # that took ~35 s on the Mac mini. The name is only used in logs, and we
    # are loopback-only, so skip the lookup.
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = host, port


def make_server(engine, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    require_loopback(host)
    # One model, one Metal device: classify one batch at a time (spec D8).
    lock = threading.Lock()
    jobs: OrderedDict[str, dict] = OrderedDict()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):  # noqa: N802 — http.server naming
            if self.path.rstrip("/") != "/runsync":
                return self._send(404, {"error": f"unknown path {self.path}"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                contents = body["input"]["contents"]
                if not isinstance(contents, list) or not all(isinstance(c, str) for c in contents):
                    raise TypeError
            except (KeyError, TypeError, ValueError):
                return self._send(400, {"error": 'body must be {"input": {"contents": [str, …]}}'})

            start = time.perf_counter()
            with lock:
                verdicts = classify_batch(engine, contents)
            envelope = {
                "id": uuid.uuid4().hex,
                "status": "COMPLETED",
                "delayTime": 0,
                "executionTime": int((time.perf_counter() - start) * 1000),
                "output": {"verdicts": verdicts},
            }
            jobs[envelope["id"]] = envelope
            while len(jobs) > KEPT_JOBS:
                jobs.popitem(last=False)
            self._send(200, envelope)

        def do_GET(self):  # noqa: N802
            prefix = "/status/"
            if self.path.startswith(prefix) and self.path[len(prefix):] in jobs:
                return self._send(200, jobs[self.path[len(prefix):]])
            self._send(404, {"error": f"unknown job or path {self.path}"})

        def log_message(self, fmt, *args):  # quieter than the default per-request line
            sys.stderr.write("local-cope-b: " + fmt % args + "\n")

    # ::1 needs an IPv6 socket; set it on a per-call subclass so one server's
    # family never leaks into another.
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    server_cls = type("LocalCopeBServer", (_LoopbackHTTPServer,), {"address_family": family})
    return server_cls((host, port), Handler)


def main() -> None:
    p = argparse.ArgumentParser(description="Local CoPE-B server (RunPod wire format)")
    p.add_argument("--model", required=True, help="MLX model directory")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args()

    require_loopback(args.host)
    policy_version = require_policy_version(os.environ.get("POLICY_VERSION"))

    from engine import load_engine

    engine = load_engine(args.model, policy_version)
    httpd = make_server(engine, args.host, args.port)
    print(f"local CoPE-B serving {policy_version} on http://{args.host}:{args.port} "
          f"(prefix {engine.prefix_len} tokens)", file=sys.stderr)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
