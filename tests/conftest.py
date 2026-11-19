"""pytest 公共夹具：假桥 + 按环境变量重新加载的 app 模块 + 起在随机端口的真服务。"""
from __future__ import annotations

import base64
import importlib.util
import json
import pathlib
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = ROOT / "server" / "app.py"
BRIDGE = ROOT / "bridge" / "dodaios-bridge.py"


class FakeBridge(ThreadingHTTPServer):
    """记录命中次数、返回固定 JSON 的假桥。"""

    daemon_threads = True

    def __init__(self, routes):
        self.routes = dict(routes)
        self.hits = []
        super().__init__(("127.0.0.1", 0), _FakeHandler)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/" % self.server_address[1]

    def count(self, path: str) -> int:
        return sum(1 for h in self.hits if h == path)


class _FakeHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def _serve(self, method):
        path = self.path.split("?", 1)[0].strip("/")
        srv = self.server
        srv.hits.append(path)
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
        body = srv.routes.get(path, {"ok": False, "error": "not found"})
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")


@pytest.fixture
def fake_bridge():
    routes = {
        "cdp/status": {
            "ok": True, "title": "测试歌曲_哔哩哔哩_bilibili",
            "url": "https://www.bilibili.com/video/BV1xx411c7mD/",
            "paused": False, "t": 65, "dur": 240, "vvol": 88, "rate": 1.0,
            "dm": True, "eq": False, "id": "TAB1",
            "queue": {"n": 2, "idx": 0, "next": "第二首", "auto": True},
        },
        "volume": 40,
        "muted": False,
        "list": {"ok": True, "chosen": {"title": "SMTC 歌", "app": "x", "status_name": "playing"},
                 "volume": 40, "muted": False},
        "health": {"ok": True},
    }
    srv = FakeBridge(routes)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def load_app(monkeypatch, tmp_path, bridge_url, **env):
    """按给定环境变量重新加载 app.py（它只在 import 时读 env）。"""
    base = {
        "MEDIA_WEB_USER": "media",
        "MEDIA_WEB_PASSWORD": "secret-pass",
        "MEDIA_WEB_CSRF_FILE": str(tmp_path / "csrf.token"),
        "MEDIA_H5_TOKEN_FILE": str(tmp_path / "h5.token"),
        "FEISHU_OPENID_FILE": str(tmp_path / "openid"),
        "MEDIA_BRIDGE_URL": bridge_url,
        "MEDIA_BRIDGE_SSH": "0",
        "MEDIA_WIN_HOST": "",
        "MEDIA_STATUS_TTL": "0.8",
    }
    base.update(env)
    for k, v in base.items():
        monkeypatch.setenv(k, str(v))
    spec = importlib.util.spec_from_file_location("media_web_app_test", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def start_server(mod):
    srv = mod.ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def http(url, method="GET", body=None, headers=None, timeout=5):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def basic(user, pw):
    return "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
