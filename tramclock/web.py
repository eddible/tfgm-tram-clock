"""Small JSON + static web UI for managing the clock from a phone."""

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import ConfigError
from .tfgm import TfGMError

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(__file__), "static")
KEY_MASK = "•" * 8


def make_handler(controller, screen, log_buffer):
    config = controller.config

    def public_config():
        cfg = config.get()
        key = cfg["api_key"]
        cfg["api_key"] = KEY_MASK + key[-4:] if key else ""
        return cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "TramClock"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code, body, ctype="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode())

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path in ("/", "/index.html"):
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if url.path == "/screen.png":
                return self._send(200, screen.png(), "image/png")
            if url.path == "/api/status":
                return self._send(200, controller.status())
            if url.path == "/api/config":
                return self._send(200, public_config())
            if url.path == "/api/log":
                return self._send(200, list(log_buffer))
            if url.path == "/api/platforms":
                try:
                    return self._send(200, [p._asdict() for p in controller.api.platforms()])
                except TfGMError as e:
                    return self._send(502, {"error": str(e)})
            if url.path == "/api/board":
                pids = [p for p in q.get("pids", [""])[0].split(",") if p]
                try:
                    board = controller.api.board(pids)
                except TfGMError as e:
                    return self._send(502, {"error": str(e)})
                return self._send(200, {
                    "departures": [d._asdict() for d in board.departures],
                    "messages": board.messages,
                })
            self._send(404, {"error": "Not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                body = self._body()
            except ValueError:
                return self._send(400, {"error": "Invalid JSON"})

            if path == "/api/wake":
                controller.wake(body.get("minutes"))
            elif path == "/api/sleep":
                controller.sleep()
            elif path == "/api/view":
                controller.set_view(int(body.get("index", 0)))
                controller.wake()
            elif path == "/api/config":
                if str(body.get("api_key", "")).startswith(KEY_MASK):
                    body.pop("api_key")  # unchanged
                try:
                    config.update(body)
                except ConfigError as e:
                    return self._send(400, {"error": str(e)})
                log.info("Settings updated from web UI")
                return self._send(200, public_config())
            else:
                return self._send(404, {"error": "Not found"})
            self._send(200, controller.status())

    return Handler


def serve(controller, screen, log_buffer, port):
    httpd = ThreadingHTTPServer(("", port), make_handler(controller, screen, log_buffer))
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="web", daemon=True)
    thread.start()
    log.info("Web UI on port %d", port)
    return httpd
