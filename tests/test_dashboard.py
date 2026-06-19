"""Dashboard HTTP layer (regression for the missing-coverage audit gap)."""

from __future__ import annotations

import io
import json

from leptin.api import Leptin
from leptin.dashboard import make_handler


class _FakeRequest:
    """Drive a BaseHTTPRequestHandler subclass without real sockets."""

    def __init__(self, handler_cls, method, path, body=None, host="127.0.0.1"):
        self.handler_cls = handler_cls
        self.method = method
        self.path = path
        self.body = json.dumps(body).encode() if body is not None else b""
        self.host = host
        self.wfile = io.BytesIO()

    def run(self):
        rq = self
        body = self.body

        class H(self.handler_cls):
            def __init__(self):  # bypass socket setup
                self.path = rq.path
                self.command = rq.method
                self.client_address = ("127.0.0.1", 0)
                self.headers = {"Host": rq.host, "Content-Length": str(len(body))}
                self.rfile = io.BytesIO(body)
                self.wfile = rq.wfile
                self.requestline = f"{rq.method} {rq.path}"
                self.request_version = "HTTP/1.1"

            def send_response(self, code, message=None):
                self.wfile.write(f"HTTP {code}\n".encode())

            def send_header(self, *a):
                pass

            def end_headers(self):
                self.wfile.write(b"\n")

        h = H()
        if self.method == "GET":
            h.do_GET()
        else:
            h.do_POST()
        raw = self.wfile.getvalue().decode()
        _, _, payload = raw.partition("\n\n")
        return json.loads(payload) if payload.strip() else None


def _mem_with_data():
    mem = Leptin(":memory:")
    mem.remember("The user prefers dark mode.", subject="prefs")
    mem.remember("The user prefers dark mode.", subject="prefs")  # merge
    mem.recall("theme")
    return mem


def test_report_endpoint():
    mem = _mem_with_data()
    handler = make_handler(mem)
    out = _FakeRequest(handler, "GET", "/api/report?window=all").run()
    assert "tokens_saved" in out and "tuning" in out
    mem.close()


def test_memories_and_inspect_endpoints():
    mem = _mem_with_data()
    handler = make_handler(mem)
    mems = _FakeRequest(handler, "GET", "/api/memories?status=active").run()["memories"]
    assert mems
    mid = mems[0]["memory_id"]
    info = _FakeRequest(handler, "GET", f"/api/inspect?memory_id={mid}").run()
    assert info["memory"]["memory_id"] == mid
    assert "events" in info
    mem.close()


def test_forget_restore_roundtrip_over_http():
    mem = _mem_with_data()
    handler = make_handler(mem)
    mems = _FakeRequest(handler, "GET", "/api/memories?status=active").run()["memories"]
    mid = mems[0]["memory_id"]
    f = _FakeRequest(handler, "POST", "/api/forget", {"memory_id": mid}).run()
    assert f["count"] == 1
    r = _FakeRequest(handler, "POST", "/api/restore", {"memory_id": mid}).run()
    assert r["restored"] is True
    mem.close()


def test_forbidden_host_is_rejected():
    mem = _mem_with_data()
    handler = make_handler(mem)
    out = _FakeRequest(handler, "GET", "/api/report", host="evil.example.com").run()
    assert out["error"] == "forbidden host"
    mem.close()


def test_tuning_endpoint():
    mem = _mem_with_data()
    handler = make_handler(mem)
    out = _FakeRequest(handler, "GET", "/api/tuning").run()
    assert "tuning" in out and "history" in out
    mem.close()
