"""Tiny stdlib HTTP app used to test the API runner end-to-end.

It is deliberately imperfect in two documented ways so the tests can prove
the engine finds real defects:

* a deeply nested JSON body on POST /items crashes with a traceback in the
  response (5xx + information leak);
* GET /items/{id}/preview answers 404 "item not found" for an item that
  exists (dishonest precondition error).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("SAMPLE_TOKEN", "")
ITEMS: dict[str, dict] = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep test output clean
        pass

    def _send(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        return bool(TOKEN) and self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def _route(self, method: str) -> None:
        path = self.path.split("?")[0]
        if path == "/health" and method == "GET":
            return self._send(200, {"status": "ok"})
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] != "items":
            return self._send(404, {"detail": "not found"})
        if not self._authorized():
            return self._send(401, {"detail": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if method == "POST" and len(parts) == 1:
            try:
                data = json.loads(raw)
            except RecursionError:
                body = ("Traceback (most recent call last):\n" + traceback.format_exc()).encode()
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None
            except ValueError:
                return self._send(422, {"detail": "invalid json"})
            if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"] or len(data["name"]) > 200:
                return self._send(422, {"detail": "name required"})
            if set(data) - {"name"}:
                return self._send(422, {"detail": "unexpected field"})
            item_id = f"item{len(ITEMS) + 1}"
            ITEMS[item_id] = {"id": item_id, "name": data["name"], "revision": 1}
            return self._send(200, {"item": ITEMS[item_id]})
        if len(parts) == 2 and method == "GET":
            item = ITEMS.get(parts[1])
            return self._send(200, {"item": item}) if item else self._send(404, {"detail": "item not found"})
        if len(parts) == 3 and parts[2] == "preview" and method == "GET":
            return self._send(404, {"detail": "item not found"})  # dishonest: the item may exist
        if len(parts) == 2 and method == "DELETE":
            ITEMS.pop(parts[1], None)
            return self._send(200, {"deleted": True})
        return self._send(405, {"detail": "method not allowed"})

    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._route("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._route("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._route("PATCH")


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
