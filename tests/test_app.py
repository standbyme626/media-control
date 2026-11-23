"""服务端行为测试：缓存 / 原因码 / 鉴权顺序 / 登录锁定 / 真实 IP / CSRF。"""
from __future__ import annotations

import json
import socket
import time

import pytest

from conftest import basic, http, load_app, start_server


def test_rate_limit_bucket():
    from conftest import load_app as _l  # noqa: F401  (保持导入路径一致)

    # 直接测纯函数，不依赖网络
    import importlib.util, pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("app_rate", root / "server" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._allow("9.9.9.9", limit=2) is True
    assert mod._allow("9.9.9.9", limit=2) is True
    assert mod._allow("9.9.9.9", limit=2) is False
    assert mod._allow("8.8.8.8", limit=2) is True      # 分桶互不影响


def test_status_ok_and_cached(monkeypatch, tmp_path, fake_bridge):
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url)
    first = mod._status()
    assert first["ok"] and first["title"] == "测试歌曲"
    assert first["pos"] == 65 and first["dur"] == 240
    second = mod._status()
    assert second == first
    assert fake_bridge.count("cdp/status") == 1        # TTL 内只打一次桥


def test_status_reason_no_player(monkeypatch, tmp_path, fake_bridge):
    fake_bridge.routes["cdp/status"] = {"ok": False, "error": "no cdp video"}
    fake_bridge.routes["list"] = {"ok": True, "chosen": {}}
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url)
    st = mod._status()
    assert st["ok"] is False and st["reason"] == "no_player"


def test_status_reason_offline(monkeypatch, tmp_path, fake_bridge):
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url,
                   MEDIA_WIN_HOST="127.0.0.1", MEDIA_SSH_PORT="1")   # 1 端口必然拒连
    st = mod._status()
    assert st["ok"] is False and st["reason"] == "offline" and st["pc_online"] is False


def test_status_reason_bridge_down(monkeypatch, tmp_path):
    mod = load_app(monkeypatch, tmp_path, "http://127.0.0.1:1/")   # 桥不可达
    mod.SSH_FALLBACK = False
    st = mod._status()
    assert st["ok"] is False and st["reason"] == "bridge_down"


def test_http_auth_and_csrf(monkeypatch, tmp_path, fake_bridge):
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url)
    srv = start_server(mod)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        code, _, _ = http(base + "/api/status")
        assert code == 401
        code, _, body = http(base + "/api/status", headers={"Authorization": basic("media", "secret-pass")})
        assert code == 200 and json.loads(body)["ok"] is True
        # 设备薄客户端用 Basic（不带浏览器 Cookie），按设计免 CSRF；应放行到桥
        code, _, _ = http(base + "/api/pause", method="POST",
                          headers={"Authorization": basic("media", "secret-pass")})
        assert code != 403
        # 带对 CSRF 也放行（这里 pause 会走桥，假桥返回 not found → 502/200 均可，只要不是 403）
        code, _, _ = http(base + "/api/pause", method="POST",
                          headers={"Authorization": basic("media", "secret-pass"),
                                   "X-CSRF-Token": mod.CSRF})
        assert code != 403
    finally:
        srv.shutdown()


def test_login_lockout(monkeypatch, tmp_path, fake_bridge):
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url, MEDIA_FAIL_MAX="3", MEDIA_LOCK_SECONDS="30")
    srv = start_server(mod)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        for _ in range(2):
            code, _, _ = http(base + "/api/status", headers={"Authorization": basic("media", "wrong")})
            assert code == 401
        code, headers, _ = http(base + "/api/status", headers={"Authorization": basic("media", "wrong")})
        assert code == 429 and "Retry-After" in headers      # 触发锁定的那一次直接 429
        code, _, _ = http(base + "/api/status", headers={"Authorization": basic("media", "secret-pass")})
        assert code == 429                                   # 锁定期内正确口令也不放行
    finally:
        srv.shutdown()


def test_unauthenticated_body_not_read(monkeypatch, tmp_path, fake_bridge):
    """未认证 + 声明 3MB 的语音上传：必须立刻 401，不能等 body。"""
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url)
    srv = start_server(mod)
    port = srv.server_address[1]
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=3)
        s.settimeout(3)
        s.sendall(b"POST /api/voice/audio?rate=16000 HTTP/1.1\r\n"
                  b"Host: 127.0.0.1\r\n"
                  b"Content-Length: 3000000\r\n"
                  b"Content-Type: application/octet-stream\r\n\r\n")
        head = s.recv(200)                     # 一个字节的 body 都还没发
        assert b" 401 " in head.split(b"\r\n")[0], head[:80]
        s.close()
    finally:
        srv.shutdown()


def test_real_ip_uses_cf_header(monkeypatch, tmp_path, fake_bridge):
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url)

    class Req:
        client_address = ("127.0.0.1", 5555)
        headers = {"CF-Connecting-IP": "203.0.113.9"}

    assert mod.Handler._ip(Req()) == "203.0.113.9"

    class Req2:
        client_address = ("10.0.0.7", 5555)
        headers = {"CF-Connecting-IP": "203.0.113.9"}

    assert mod.Handler._ip(Req2()) == "10.0.0.7"   # 非本机来源不信这个头


def test_pc_online_cache(monkeypatch, tmp_path, fake_bridge):
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url,
                   MEDIA_WIN_HOST="127.0.0.1", MEDIA_SSH_PORT="1")
    t0 = time.monotonic()
    assert mod._pc_online() is False
    d1 = time.monotonic() - t0
    t1 = time.monotonic()
    assert mod._pc_online() is False
    d2 = time.monotonic() - t1
    assert d2 < d1 or d2 < 0.01


def test_health_reports_version(monkeypatch, tmp_path, fake_bridge):
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url)
    srv = start_server(mod)
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        code, _, body = http(base + "/api/health", headers={"Authorization": basic("media", "secret-pass")})
        data = json.loads(body)
        assert code == 200 and data["ok"] is True and data["version"] == mod.VERSION
    finally:
        srv.shutdown()


def test_session_state_helpers(monkeypatch, tmp_path, fake_bridge):
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url)
    mod.H5_TOKEN = "t" * 32
    st = mod._make_state()
    assert mod._check_state(st) is True
    assert mod._check_state(st[:-1] + ("0" if st[-1] != "0" else "1")) is False
    assert mod._clean_tab_url("https://www.bilibili.com/video/BV1ab411c7mD?p=2") == \
        "https://www.bilibili.com/video/BV1ab411c7mD/"
    assert mod._clean_tab_url("https://example.com/x?y=1") == "https://example.com/x"


def test_openid_whitelist(monkeypatch, tmp_path, fake_bridge):
    mod = load_app(monkeypatch, tmp_path, fake_bridge.url,
                   MEDIA_FEISHU_OPENIDS="ou_a,ou_b")
    assert mod._allowed_openid("ou_a") is True
    assert mod._allowed_openid("ou_c") is False
