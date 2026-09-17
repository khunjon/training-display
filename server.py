#!/usr/bin/env python3
"""Serve the rendered frame on the LAN, and speak the TRMNL device protocol.

Routes
  /                today.png in a phone-sized page that refreshes itself
  /today.png       the frame render.py wrote (800x480, 1-bit)
  /today.json      the data behind it
  /health          {"ok": true, "age_s": ...}
  /api/setup       TRMNL: {status: 200, api_key, friendly_id, image_url, message}
  /api/display     TRMNL: {status: 0, image_url, filename, refresh_rate, ...}
  /api/log         TRMNL: device log lines (POST) -> 204, echoed to stderr

TRMNL notes (from the open-source firmware, 2026-09):
  - `filename` is what the device compares to decide whether the image changed,
    so it carries a hash of the PNG; SPIFFS caps it at 31 characters.
  - `status` 0 means "here is your image"; 202 means "not registered" and makes
    the device poll fast, so this server never sends it.
  - The image is fetched from `image_url`, which must be reachable from the
    device: by default it is built from the Host header the device used.

Stdlib only; no auth (LAN only by design — see README). Writes only
<output_dir>/device.json, the last headers a device sent, for debugging.

Usage
  server.py                     # bind 0.0.0.0:8787 (or config "server")
  server.py --port 8787 --bind 0.0.0.0 --config path.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from render import DEFAULT_CONFIG, load_config

DEFAULTS = {
    "bind": "0.0.0.0",
    "port": 8787,
    "refresh_rate": 3600,  # seconds the device sleeps between fetches
    "friendly_id": "training-display",
    "image_url": None,  # e.g. "http://192.168.1.10:8787/today.png"; default: from the Host header
}

PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="900">
<title>Training Display</title>
<style>
  html,body{margin:0;height:100%;background:#111;display:flex;align-items:center;justify-content:center}
  img{width:100%;max-width:800px;height:auto;image-rendering:pixelated;background:#fff}
</style>
<img src="/today.png?t={stamp}" alt="today">
"""


class State:
    """What the handlers need: where the frame lives and how to describe it."""

    def __init__(self, output_dir: Path, opts: dict):
        self.output_dir = output_dir
        self.opts = opts

    @property
    def png(self) -> Path:
        return self.output_dir / "today.png"

    def frame(self) -> tuple[bytes, str] | None:
        """(bytes, short filename) for the current frame, or None if none rendered yet."""
        try:
            data = self.png.read_bytes()
        except OSError:
            return None
        return data, "today-" + hashlib.sha1(data).hexdigest()[:10] + ".png"

    def image_url(self, host_header: str | None) -> str:
        if self.opts.get("image_url"):
            return self.opts["image_url"]
        host = host_header or f"localhost:{self.opts['port']}"
        return f"http://{host}/today.png"

    def note_device(self, headers, path: str) -> None:
        keep = ("ID", "FW-Version", "Model", "Battery-Voltage", "Battery-Charging", "USB-Connected",
                "RSSI", "Refresh-Rate", "Image-Cached", "Width", "Height", "Wake-Time")
        seen = {k: headers.get(k) for k in keep if headers.get(k) is not None}
        if not seen:
            return
        seen["path"] = path
        seen["at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            (self.output_dir / "device.json").write_text(json.dumps(seen, indent=1))
        except OSError:
            pass
        sys.stderr.write(f"device {path} {json.dumps(seen)}\n")


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "training-display/1"

        def log_message(self, fmt, *args):  # quieter default log: one line, no date
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

        # ---- helpers
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, obj: dict, code: int = 200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _route(self) -> str:
            return self.path.split("?", 1)[0].rstrip("/") or "/"

        # ---- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            route = self._route()
            if route == "/":
                body = PAGE.replace("{stamp}", str(int(time.time()))).encode()
                self._send(200, body, "text/html; charset=utf-8")
            elif route == "/today.png":
                fr = state.frame()
                if not fr:
                    self._send(404, b"no frame rendered yet\n", "text/plain")
                    return
                self._send(200, fr[0], "image/png", {"Content-Disposition": f'inline; filename="{fr[1]}"'})
            elif route == "/today.json":
                try:
                    body = (state.output_dir / "today.json").read_bytes()
                except OSError:
                    self._send(404, b"{}", "application/json")
                    return
                self._send(200, body, "application/json; charset=utf-8")
            elif route == "/health":
                try:
                    age = int(time.time() - state.png.stat().st_mtime)
                except OSError:
                    age = None
                self._json({"ok": age is not None, "age_s": age})
            elif route == "/api/setup":
                state.note_device(self.headers, route)
                self._json(
                    {
                        "status": 200,
                        "api_key": "lan",  # the firmware stores and echoes this; nothing checks it
                        "friendly_id": state.opts["friendly_id"],
                        "image_url": state.image_url(self.headers.get("Host")),
                        "message": "Training Display: registered",
                    }
                )
            elif route == "/api/display":
                state.note_device(self.headers, route)
                fr = state.frame()
                if not fr:
                    # 500 makes the device retry soon rather than sleep an hour on nothing
                    self._json({"status": 500, "error": "no frame rendered yet", "refresh_rate": 60})
                    return
                self._json(
                    {
                        "status": 0,
                        "image_url": state.image_url(self.headers.get("Host")),
                        "image_url_timeout": 30,
                        "filename": fr[1],
                        "refresh_rate": int(state.opts["refresh_rate"]),
                        "update_firmware": False,
                        "firmware_url": None,
                        "reset_firmware": False,
                        "special_function": "none",
                    }
                )
            else:
                self._send(404, b"not found\n", "text/plain")

        # ---- POST
        def do_POST(self):
            route = self._route()
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            if route == "/api/log":
                state.note_device(self.headers, route)
                sys.stderr.write(f"device log {body.decode('utf-8', 'replace').strip()}\n")
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._send(404, b"not found\n", "text/plain")

    return Handler


def server_options(cfg: dict, overrides: dict | None = None) -> dict:
    opts = dict(DEFAULTS)
    opts.update(cfg.get("server") or {})
    opts.update({k: v for k, v in (overrides or {}).items() if v is not None})
    return opts


def serve(output_dir: Path, opts: dict) -> ThreadingHTTPServer:
    """Bind and return the server (call serve_forever yourself); port 0 picks a free port."""
    state = State(output_dir, opts)
    httpd = ThreadingHTTPServer((opts["bind"], int(opts["port"])), make_handler(state))
    httpd.daemon_threads = True
    opts["port"] = httpd.server_address[1]
    return httpd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--bind")
    ap.add_argument("--port", type=int)
    ap.add_argument("--out", help="dir holding today.png (default: config output_dir)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    opts = server_options(cfg, {"bind": args.bind, "port": args.port})
    out = Path(args.out or cfg["output_dir"]).expanduser()
    httpd = serve(out, opts)
    sys.stderr.write(f"training-display serving {out} on http://{opts['bind']}:{opts['port']}/ (pid {os.getpid()})\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
