#!/usr/bin/env python3
"""Mock del proveedor Ghost eShop PRO para tests.

Replica el protocolo real del portal (ver docs/api-ghostland.md):

  - POST /api/auth/login      username/password en Base64 → {"token"}.
                             La sesión viaja en cookie "session" (no Bearer).
  - GET  /api/user            valida la cookie de sesión.
  - GET  /api/games/fetch     ?gameTid= → {title, files:{base,update,dlc}}.
                             Los DLC comparten los 12 primeros dígitos hex
                             con la base (familia tid>>16).
  - POST /api/games/request-dl {fileRef: b64(nombre)} → {dlLink}.
  - GET  /api/download-info/<token> → {fileName, fileSize, chunks[{url,size}]}
                             (los ficheros se parten en 2 chunks).
  - GET  /chunk/<token>/<i>   datos; EXIGE Referer de la página /d/<token>,
                             como hace el CDN real (sin él → 403).
  - POST /api/download-complete/<token> → cortesía.

También sirve titledb (ES.es.json + versions.txt) con ETag para poder
ejecutar el flujo completo (tests e2e) sin tocar la red.

Uso:
    from tests.mock_portal import MockPortal
    mock = MockPortal()          # puerto efímero en 127.0.0.1
    mock.start()
    ...
    mock.stop()
    print(mock.url)              # http://127.0.0.1:<puerto>

O en solitario:  python3 tests/mock_portal.py [puerto]
"""

from __future__ import annotations

import base64
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

USER = "testuser"
PASS = "testpass"

# (nombre catálogo, tid, categoría, tamaño) — la versión sale de "…[vN]"
CATALOG = [
    ("Death Howl [0100CF70241E8000][v0]", "0100CF70241E8000", "base", 500_000),
    ("Death Howl [0100CF70241E8800][v65536]", "0100CF70241E8800", "update", 100_000),
    ("Death Howl [0100CF70241E8800][v131072]", "0100CF70241E8800", "update", 120_000),
    ("Zelda BOTW [01007EF00011E000][v0]", "01007EF00011E000", "base", 5_000_000),
    ("Zelda BOTW [01007EF00011E800][v1114112]", "01007EF00011E800", "update", 300_000),
    ("Zelda BOTW DLC1 [01007EF00011F001]", "01007EF00011F001", "dlc", 50_000),
    # 01007EF00011F002 está en versions.txt pero NO aquí (caso "se omite")
    ("Lost In Space [01007A30262D2000][v0]", "01007A30262D2000", "base", 900_000),
    ("Lost In Space [01007A30262D2800][v131072]", "01007A30262D2800", "update", 60_000),
]

VERSIONS = "\n".join(
    f"{tid}|{'0' * 32}|{version}" for tid, version in [
        ("0100CF70241E8000", 0), ("0100CF70241E8800", 131072),
        ("01007EF00011E000", 0), ("01007EF00011E800", 1114112),
        ("01007EF00011F001", 0), ("01007EF00011F002", 0),
        ("01007A30262D2000", 0), ("01007A30262D2800", 131072),
        ("01007A30262D3001", 0),
    ]) + "\n"

DB = {
    "70010000000023": {"id": "01007EF00011E000",
                       "name": "The Legend of Zelda: Breath of the Wild"},
    "70010000000099": {"id": "0100CF70241E8000", "name": "Death Howl"},
    "70010000000101": {"id": "01007A30262D2000",
                       "name": "Lost In Space - The First Adventure"},
}

TITLES = {
    "01007EF00011E000": "The Legend of Zelda: Breath of the Wild",
    "0100CF70241E8000": "Death Howl",
    "01007A30262D2000": "Lost In Space - The First Adventure",
}


def blob_for(name: str, size: int) -> bytes:
    """Contenido determinista: permite validar descargas byte a byte."""
    base = b"GHOSTLAND-TEST-BLOB:" + name.encode() + b"\x00"
    return (base * (size // len(base) + 1))[:size]


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tokens: dict[str, str] = {}     # token -> nombre de fichero
        self.seq = 0
        self.session_tokens: set[str] = set()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: _State = _State()
    port: int = 0

    # ------------------------------------------------------------- utilidades
    def log_message(self, fmt, *args):  # silencioso por defecto en tests
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, data: bytes):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _session_cookie(self) -> str:
        m = re.search(r"(?:^|;\s*)session=([^;]+)", self.headers.get("Cookie", ""))
        return m.group(1) if m else ""

    def _authorized(self) -> bool:
        return self._session_cookie() in self.state.session_tokens

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}

        if self.path == "/api/auth/login":
            user = self._b64(payload.get("username"))
            password = self._b64(payload.get("password"))
            if user == USER and password == PASS:
                token = f"sess-{self.state.seq}"
                with self.state.lock:
                    self.state.seq += 1
                    self.state.session_tokens.add(token)
                self._json({"token": token})
            else:
                self._json({"statusCode": 401, "message": "Invalid credentials"}, 401)
            return

        if self.path == "/api/games/request-dl":
            if not self._authorized():
                self._json({"statusCode": 401, "message": "Unauthorized"}, 401)
                return
            try:
                name = base64.b64decode(payload.get("fileRef", "")).decode("utf-8")
            except Exception:
                name = "\0invalid"
            if not any(e[0] == name for e in CATALOG):
                self._json({"statusCode": 404, "message": "Not found"}, 404)
                return
            with self.state.lock:
                self.state.seq += 1
                token = f"tok{self.state.seq:04d}"
                self.state.tokens[token] = name
            self._json({"dlLink": f"http://127.0.0.1:{self.port}/d/{token}"})
            return

        if re.match(r"^/api/download-complete/[^/]+$", self.path):
            self._json({"ok": True})
            return

        self._json({"statusCode": 404, "message": "Not found"}, 404)

    @staticmethod
    def _b64(value) -> str:
        try:
            return base64.b64decode(str(value or "")).decode("utf-8")
        except Exception:
            return ""

    # ----------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/user":
            if not self._authorized():
                self._json({"statusCode": 401, "message": "Unauthorized"}, 401)
            else:
                self._json({"id": 1, "username": USER, "subscription": "2099-01-01"})
            return

        if parsed.path == "/api/games/fetch":
            if not self._authorized():
                self._json({"statusCode": 401, "message": "Unauthorized"}, 401)
                return
            game_tid = (parse_qs(parsed.query).get("gameTid") or [""])[0].upper()
            if game_tid not in TITLES:
                self._json({"statusCode": 404, "message": "Not found"}, 404)
                return
            files = {"base": [], "update": [], "dlc": []}
            for name, tid, cat, size in CATALOG:
                if tid[:12] == game_tid[:12]:  # familia = 12 primeros dígitos
                    files.setdefault(cat, []).append(
                        {"tid": tid, "name": name, "size": size, "type": cat})
            self._json({"basetid": game_tid, "title": TITLES[game_tid],
                        "files": files})
            return

        if parsed.path == "/api/games/fetch-list":
            if not self._authorized():
                self._json({"statusCode": 401, "message": "Unauthorized"}, 401)
                return
            search = (parse_qs(parsed.query).get("search") or [""])[0].lower()
            results = [{"basetid": tid, "title": title}
                       for tid, title in TITLES.items()
                       if not search or search in title.lower()]
            self._json({"results": results, "page": 1, "total": len(results)})
            return

        m = re.match(r"^/api/download-info/([^/]+)$", parsed.path)
        if m:
            if not self._authorized():
                self._json({"statusCode": 401, "message": "Unauthorized"}, 401)
                return
            name = self.state.tokens.get(m.group(1))
            if not name:
                self._json({"statusCode": 404, "message": "Not found"}, 404)
                return
            size = next(e[3] for e in CATALOG if e[0] == name)
            half = size // 2
            parts = [(0, half), (half, size)]
            token = m.group(1)
            chunks = [{"index": i + 1, "size": end - start,
                       "url": f"http://127.0.0.1:{self.port}/chunk/{token}/{i}"}
                      for i, (start, end) in enumerate(parts)]
            self._json({"fileName": name, "fileSize": size, "tid": "",
                        "fileType": "nsp", "numberOfChunks": len(chunks),
                        "chunks": chunks})
            return

        m = re.match(r"^/chunk/([^/]+)/(\d+)$", parsed.path)
        if m:
            token, idx = m.group(1), int(m.group(2))
            name = self.state.tokens.get(token)
            if not name:
                self._json({"statusCode": 404, "message": "Not found"}, 404)
                return
            if f"/d/{token}" not in self.headers.get("Referer", ""):
                # como el CDN real: sin Referer correcto no se sirve
                self._json({"statusCode": 403, "message": "Forbidden referer"}, 403)
                return
            size = next(e[3] for e in CATALOG if e[0] == name)
            blob = blob_for(name, size)
            half = size // 2
            self._bytes(blob[0:half] if idx == 0 else blob[half:size])
            return

        if parsed.path == "/titledb/ES.es.json":
            self._serve_titledb(json.dumps(DB).encode(), "application/json")
            return
        if parsed.path == "/titledb/versions.txt":
            self._serve_titledb(VERSIONS.encode(), "text/plain")
            return

        self._json({"statusCode": 404, "message": "Not found"}, 404)

    def _serve_titledb(self, body: bytes, content_type: str):
        etag = '"mock-etag-1"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MockPortal:
    """Servidor del mock en un hilo demonio, puerto efímero por defecto."""

    def __init__(self, port: int = 0):
        self._port = port
        self._server: ThreadingHTTPServer | None = None

    def start(self) -> "MockPortal":
        handler = type(f"Handler_{self._port}", (Handler,), {})
        handler.state = _State()
        self._server = ThreadingHTTPServer(("127.0.0.1", self._port), handler)
        handler.port = self._server.server_address[1]
        self._port = handler.port
        self.state = handler.state
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()
        return self

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18500
    print(f"[mock] portal en http://127.0.0.1:{port}", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    Handler.port = port
    server.serve_forever()
