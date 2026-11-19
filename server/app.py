#!/usr/bin/env python3
"""media-web：手机端控制 Windows 受控浏览器播放 B 站音乐的 Web 服务。

架构：
  手机浏览器 / Android 壳  --HTTP(S)-->  media-web(:8097)  --常驻 SSH 隧道-->  Windows bridge(:8765)  --CDP-->  Edge

安全模型：
- 只暴露固定端点白名单，任何用户输入都不会进 shell（argv 直传 + shlex 远程引用）。
- 鉴权：HTTP Basic（公网走 TLS 反代）+ H5 一次性 token + 飞书 OAuth 三条路，统一签发 HMAC Cookie。
- 鉴权在读取请求体之前完成；同 IP 登录失败 5 次锁 15 分钟；分 IP 令牌桶限流。
- 写操作要求 X-CSRF-Token（进程级随机值，随页面下发）；CSP nonce、no CORS、不做静态目录服务。
- 未配置 MEDIA_WEB_PASSWORD 时整体拒服务（fail closed）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shlex
import pathlib
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

HOST = os.environ.get("MEDIA_WEB_HOST", "0.0.0.0")
PORT = int(os.environ.get("MEDIA_WEB_PORT", "8097"))
USER = os.environ.get("MEDIA_WEB_USER", "media")
PASSWORD = os.environ.get("MEDIA_WEB_PASSWORD", "")
AUTH_CONFIGURED = bool(PASSWORD)          # 未设置密码时 fail closed
TRUST_PROXY_HEADERS = os.environ.get("MEDIA_TRUST_PROXY", "1") == "1"
def _load_csrf() -> str:
    path = pathlib.Path(os.environ.get("MEDIA_WEB_CSRF_FILE", "/etc/media-web-csrf.token"))
    try:
        token = path.read_text(encoding="utf-8").strip()
        if 20 <= len(token) <= 128:
            return token
    except Exception:
        pass
    token = secrets.token_urlsafe(32)
    try:
        path.write_text(token + "\n", encoding="utf-8")
        path.chmod(0o600)
    except Exception:
        pass
    return token


CSRF = _load_csrf()

try:
    H5_TOKEN = pathlib.Path("/etc/media-web-h5.token").read_text(encoding="utf-8").strip()
except Exception:
    H5_TOKEN = ""
COOKIE_NAME = "media_h5"
COOKIE_TTL = 7 * 24 * 3600

PUBLIC_URL = (os.environ.get("MEDIA_PUBLIC_URL", "").rstrip("/") + "/") if os.environ.get("MEDIA_PUBLIC_URL") else ""
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_REDIRECT_URI = os.environ.get("FEISHU_REDIRECT_URI", "")
FEISHU_OPENID_FILE = os.environ.get("FEISHU_OPENID_FILE", "/etc/media-web-feishu-openid")
# 预置白名单（逗号分隔）；为空时退化为「首次登录即绑定」并把 open_id 打日志
FEISHU_OPENIDS = {x.strip() for x in os.environ.get("MEDIA_FEISHU_OPENIDS", "").split(",") if x.strip()}
FEISHU_AUTH_URL = "https://open.feishu.cn/open-apis/authen/v1/index"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/access_token"
FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
_APP_TOKEN = {"token": "", "exp": 0.0}

# 温湿度卡片是可选的旁路模块，默认关闭；配置 MEDIA_CLIMATE_HOST 才启用
CLIMATE_HOST = os.environ.get("MEDIA_CLIMATE_HOST", "")
CLIMATE_CMD = os.environ.get("MEDIA_CLIMATE_CMD", "iot-latest")
_CLIMATE = {"ts": 0.0, "data": None}
_CLIMATE_LOCK = threading.Lock()
_TABS = {"ts": 0.0, "data": None}
_TABS_LOCK = threading.Lock()
_VIDEOS = {"ts": 0.0, "data": None}
_VIDEOS_LOCK = threading.Lock()

WIN_HOST = os.environ.get("MEDIA_WIN_HOST", "")
WIN_MAC = os.environ.get("MEDIA_WIN_MAC", "")
SSH_TARGET = os.environ.get("MEDIA_SSH_TARGET", "winpc")
SSH_PORT = int(os.environ.get("MEDIA_SSH_PORT", "22"))
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", SSH_TARGET]
BRIDGE = os.environ.get("MEDIA_BRIDGE_URL", "http://127.0.0.1:8765/")
SSH_FALLBACK = os.environ.get("MEDIA_BRIDGE_SSH", "1") == "1"
H5_TOKEN_FILE = os.environ.get("MEDIA_H5_TOKEN_FILE", "/etc/media-web-h5.token")
ACTIONS = {"pause", "play", "next", "prev", "mute"}
_SLEEP = {"timer": None, "due": 0.0, "gen": 0}
_SLEEP_LOCK = threading.Lock()

_RATE: dict[str, deque] = defaultdict(deque)
_RATE_LOCK = threading.Lock()
_ACTION_LOCK = threading.Lock()
VERSION = "3.5.0"
START_TS = time.time()
STATUS_TTL = float(os.environ.get("MEDIA_STATUS_TTL", "0.8"))
_STATUS = {"ts": 0.0, "data": None}     # /api/status 短 TTL 缓存
_STATUS_LOCK = threading.Lock()
_PC = {"ts": 0.0, "online": None}       # 电脑在线探测缓存
_PC_LOCK = threading.Lock()

# 登录失败锁定：同一 IP 10 分钟内失败 5 次，锁 15 分钟
FAIL_MAX = int(os.environ.get("MEDIA_FAIL_MAX", "5"))
FAIL_WINDOW = float(os.environ.get("MEDIA_FAIL_WINDOW", "600"))
LOCK_SECONDS = float(os.environ.get("MEDIA_LOCK_SECONDS", "900"))
_FAILS: dict[str, deque] = defaultdict(deque)
_LOCKS: dict[str, float] = {}
_FAIL_LOCK = threading.Lock()


def _allow(ip: str, limit: int = 60, window: float = 60.0) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        q = _RATE[ip]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def _login_locked(ip: str) -> float:
    """返回剩余锁定秒数，0 表示未锁定。"""
    now = time.monotonic()
    with _FAIL_LOCK:
        until = _LOCKS.get(ip, 0.0)
        if until <= now:
            _LOCKS.pop(ip, None)
            return 0.0
        return until - now


def _login_failed(ip: str) -> int:
    """记一次失败；达到阈值就锁定并返回剩余秒数（0 表示还没锁）。"""
    now = time.monotonic()
    with _FAIL_LOCK:
        q = _FAILS[ip]
        while q and now - q[0] > FAIL_WINDOW:
            q.popleft()
        q.append(now)
        if len(q) >= FAIL_MAX:
            _LOCKS[ip] = now + LOCK_SECONDS
            _FAILS[ip].clear()
            print("[auth] lockout ip=%s for %ds" % (ip, int(LOCK_SECONDS)), flush=True)
            return LOCK_SECONDS
        return 0.0


def _login_ok(ip: str) -> None:
    with _FAIL_LOCK:
        _FAILS.pop(ip, None)
        _LOCKS.pop(ip, None)


def _status_invalidate() -> None:
    """任何写操作之后丢掉快照，避免前端 0.8s 内看到旧状态。"""
    with _STATUS_LOCK:
        _STATUS["ts"] = 0.0
        _STATUS["data"] = None


def _deny_ip(ip: str) -> str:
    """去掉端口、压缩过长的 IPv6，作为限流桶 key。"""
    ip = (ip or "").strip()
    if ip.count(":") == 1 and "." in ip:      # 1.2.3.4:5678
        ip = ip.rsplit(":", 1)[0]
    return ip[:64] or "unknown"


_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ssh_curl(path: str, body: bytes | None = None, ctype: str = "", timeout: int = 8) -> dict:
    """隧道没起来时的兜底：让 Windows 自己去 curl 本机桥。

    argv 直传、URL 只做远程 shell 引用（shlex.quote），不拼字符串。
    """
    cmd = SSH + ["curl", "-s", "--max-time", str(timeout)]
    if body is not None:
        cmd += ["-X", "POST", "-H", "Content-Type: " + shlex.quote(ctype), "--data-binary", "@-"]
    cmd.append(shlex.quote(BRIDGE + path))
    r = subprocess.run(cmd, input=body, capture_output=True, timeout=timeout + 10)
    if r.returncode != 0:
        raise RuntimeError("bridge unreachable")
    out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else r.stdout
    data = json.loads(out)
    if not isinstance(data, dict):
        raise RuntimeError("bad bridge response")
    return data


def _bridge(path: str) -> dict:
    try:
        try:
            with _DIRECT.open(BRIDGE + path, timeout=12) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("bad bridge response")
        return data
    except Exception as exc:
        if not SSH_FALLBACK:
            raise RuntimeError("bridge unreachable: %s" % exc)
    return _ssh_curl(path)


def _bridge_post(path: str, body: bytes, ctype: str) -> dict:
    try:
        req = urllib.request.Request(BRIDGE + path, data=body, method="POST")
        req.add_header("Content-Type", ctype)
        with _DIRECT.open(req, timeout=40) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("bad bridge response")
        return data
    except Exception as exc:
        if not SSH_FALLBACK:
            raise RuntimeError("bridge unreachable: %s" % exc)
    return _ssh_curl(path, body=body, ctype=ctype, timeout=30)


def _status_error(reason: str, error: str, pc_online) -> dict:
    """失败也要带 reason，前端才能给出不同文案和不同按钮。"""
    return {"ok": False, "reason": reason, "error": error, "pc_online": pc_online,
            "sleep_due": _sleep_due(), "updated": int(time.time())}


def _status_uncached() -> dict:
    online = _pc_online()
    if online is False:
        return _status_error("offline", "电脑离线", False)
    try:
        cdp = _bridge("cdp/status")
    except Exception as exc:
        return _status_error("bridge_down", "媒体桥不可达：%s" % exc, online)
    if cdp.get("ok"):
        return {
            "ok": True,
            "title": TITLE_SUFFIX_RE.sub("", cdp.get("title") or "").strip(),
            "app": "Microsoft Edge（受控）",
            "status": "paused" if cdp.get("paused") else "playing",
            "volume": cdp.get("volume"),
            "muted": cdp.get("muted"),
            "url": cdp.get("url") or "",
            "pos": cdp.get("t"),
            "dur": cdp.get("dur"),
            "backend": "cdp",
            "vvol": cdp.get("vvol"),
            "rate": cdp.get("rate"),
            "dm": cdp.get("dm"),
            "eq": cdp.get("eq"),
            "queue": cdp.get("queue"),
            "pc_online": online,
            "sleep_due": _sleep_due(),
            "updated": int(time.time()),
        }
    # CDP 通了但没有可播放目标，退到系统媒体会话（SMTC）
    try:
        data = _bridge("list")
    except Exception:
        return _status_error("no_player", cdp.get("error") or "没有受控播放页", online)
    chosen = data.get("chosen") or {}
    if not chosen:
        return _status_error("no_player", "没有受控播放页", online)
    return {
        "ok": True,
        "title": chosen.get("title") or "",
        "app": chosen.get("app") or "",
        "status": chosen.get("status_name") or "unknown",
        "volume": data.get("volume"),
        "muted": data.get("muted"),
        "backend": "smtc",
        "pc_online": online,
        "sleep_due": _sleep_due(),
        "updated": int(time.time()),
    }


def _status() -> dict:
    """多端同时轮询时共享同一份快照，默认 TTL 0.8s。"""
    now = time.time()
    with _STATUS_LOCK:
        cached = _STATUS["data"]
        if cached and now - _STATUS["ts"] < STATUS_TTL:
            return cached
    data = _status_uncached()
    with _STATUS_LOCK:
        _STATUS["ts"] = time.time()
        _STATUS["data"] = data
    return data


def _set_volume(level: int) -> dict:
    return _bridge("volume?level=%d" % level)


def _pc_online(timeout: float = 1.2, ttl: float = 10.0):
    """SSH 端口探测，带 10s 缓存（否则 5s 轮询会一直开新连接）。"""
    if not WIN_HOST:
        return None
    now = time.monotonic()
    with _PC_LOCK:
        val = _PC["online"]
        if val is not None and now - _PC["ts"] < ttl:
            return val
    try:
        with socket.create_connection((WIN_HOST, SSH_PORT), timeout=timeout):
            val = True
    except OSError:
        val = False
    with _PC_LOCK:
        _PC["ts"] = now
        _PC["online"] = val
    return val


def _wake() -> dict:
    mac = re.sub(r"[^0-9A-Fa-f]", "", WIN_MAC or "")
    if len(mac) != 12:
        return {"ok": False, "error": "wake unavailable"}
    payload = b"\xff" * 6 + bytes.fromhex(mac) * 16
    targets = ["255.255.255.255"]
    parts = WIN_HOST.split(".")
    if len(parts) == 4:
        targets.append(".".join(parts[:3]) + ".255")
    sent = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            for target in targets:
                for port in (9, 7):
                    try:
                        s.sendto(payload, (target, port))
                        sent.append(target)
                    except OSError:
                        pass
        finally:
            s.close()
    except OSError:
        pass
    return {"ok": bool(sent), "sent": sorted(set(sent))}


def _sleep_due() -> int:
    with _SLEEP_LOCK:
        return int(_SLEEP["due"]) if _SLEEP["due"] > time.time() else 0


def _sleep_worker(gen: int) -> None:
    with _SLEEP_LOCK:
        due = _SLEEP["due"]
    time.sleep(max(0.0, due - time.time() - 20))
    with _SLEEP_LOCK:
        if gen != _SLEEP["gen"]:
            return
    vvol = None
    try:
        st = _bridge("cdp/status")
        if st.get("ok") and st.get("vvol") is not None:
            vvol = int(st.get("vvol"))
    except Exception:
        pass
    for i in range(1, 21):
        with _SLEEP_LOCK:
            if gen != _SLEEP["gen"]:
                if vvol is not None:
                    try:
                        _bridge("cdp/volume?level=%d" % vvol)
                    except Exception:
                        pass
                return
        if vvol is not None:
            try:
                _bridge("cdp/volume?level=%d" % int(vvol * (1 - i / 20.0)))
            except Exception:
                pass
        time.sleep(1)
    try:
        _bridge("pause")
    except Exception:
        pass
    if vvol is not None:
        try:
            _bridge("cdp/volume?level=%d" % vvol)
        except Exception:
            pass
    with _SLEEP_LOCK:
        if gen == _SLEEP["gen"]:
            _SLEEP["timer"] = None
            _SLEEP["due"] = 0.0


def _set_sleep(minutes: int) -> dict:
    with _SLEEP_LOCK:
        _SLEEP["gen"] += 1
        gen = _SLEEP["gen"]
        _SLEEP["timer"] = None
        _SLEEP["due"] = 0.0
        if minutes <= 0:
            return {"ok": True, "sleep": 0, "due": 0}
        due = time.time() + minutes * 60
        _SLEEP["due"] = due
    t = threading.Thread(target=_sleep_worker, args=(gen,), daemon=True)
    t.start()
    with _SLEEP_LOCK:
        _SLEEP["timer"] = t
    return {"ok": True, "sleep": minutes, "due": int(due)}


def _climate() -> dict:
    """可选的旁路模块：从另一台内网主机读温湿度快照。未配置 MEDIA_CLIMATE_HOST 时直接返回 disabled。"""
    if not CLIMATE_HOST:
        return {"ok": False, "disabled": True, "error": "climate module disabled"}
    now = time.time()
    with _CLIMATE_LOCK:
        cached = _CLIMATE["data"]
        if cached and now - _CLIMATE["ts"] < 5:
            return cached
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", CLIMATE_HOST, CLIMATE_CMD],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            raise RuntimeError("climate unreachable")
        raw = json.loads(r.stdout)
    except Exception as exc:
        with _CLIMATE_LOCK:
            cached = _CLIMATE["data"]
        if cached:                      # 失败回落上次快照，前端只标 stale
            return dict(cached, stale=True, error=str(exc))
        raise
    latest = raw.get("latest") or {}
    devices = raw.get("devices") or []
    ts = int(latest.get("ts") or 0)
    age = max(0, int(now - ts)) if ts else None
    data = {
        "ok": True,
        "temp": latest.get("temp"),
        "hum": latest.get("hum"),
        "rssi": latest.get("rssi"),
        "ts": ts,
        "age_s": age,
        "online": bool(age is not None and age < 180),
        "dev": latest.get("dev") or (devices[0].get("dev") if devices else ""),
        "iot_url": raw.get("iot_url") or "",
    }
    with _CLIMATE_LOCK:
        _CLIMATE["ts"] = now
        _CLIMATE["data"] = data
    return data


MUSIC_RE = re.compile(
    r"bilibili|哔哩哔哩|youtube|youtu\.be|music|spotify|网易云|QQ音乐|酷狗|酷我|虾米|"
    r"专辑|歌|音乐|\bmv\b", re.I
)
TAB_SUFFIX_RE = re.compile(r"\s*-\s*(?:已丢弃|内存使用率|内存用量|音频正在播放|已固定|睡眠).*$")
TITLE_SUFFIX_RE = re.compile(r"_哔哩哔哩_bilibili$")
BV_RE = re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]+)", re.I)


def _clean_tab_url(url: str) -> str:
    m = BV_RE.search(url or "")
    if m:
        return "https://www.bilibili.com/video/%s/" % m.group(1)
    if "bilibili.com" in (url or ""):
        return "https://www.bilibili.com/"
    return (url or "").split("?", 1)[0]


def _tabs(force: bool = False) -> dict:
    now = time.time()
    with _TABS_LOCK:
        cached = _TABS["data"]
        if cached and not force and now - _TABS["ts"] < 10:
            return cached
    result = None
    try:
        cd = _bridge("cdp/list")
        if cd.get("ok"):
            out = []
            for tab in cd.get("tabs") or []:
                title = TITLE_SUFFIX_RE.sub("", (tab.get("title") or "").strip()).strip()
                url = _clean_tab_url(tab.get("url") or "")
                if not title or not MUSIC_RE.search(title + " " + url):
                    continue
                out.append({
                    "id": tab.get("id"),
                    "title": title,
                    "url": url,
                    "browser": "msedge.exe",
                })
            result = {"ok": True, "tabs": out, "backend": "cdp"}
    except Exception:
        result = None
    if result is None:
        data = _bridge("tabs")
        out = []
        for win in data.get("windows") or []:
            for idx, title in enumerate(win.get("tabs") or []):
                clean = TITLE_SUFFIX_RE.sub("", TAB_SUFFIX_RE.sub("", title).strip()).strip()
                if not clean or not MUSIC_RE.search(clean):
                    continue
                out.append({
                    "w": win.get("id"),
                    "t": idx,
                    "title": clean,
                    "browser": win.get("browser") or "",
                })
        result = {"ok": True, "tabs": out, "backend": "uia"}
    with _TABS_LOCK:
        _TABS["ts"] = now
        _TABS["data"] = result
    return result


def _switch_tab(tid: str = "", w: int = -1, t: int = -1) -> dict:
    if tid:
        return _bridge("cdp/play?id=" + tid)
    return _bridge("tab?w=%d&t=%d" % (w, t))


def _videos(force: bool = False) -> dict:
    now = time.time()
    with _VIDEOS_LOCK:
        cached = _VIDEOS["data"]
        if cached and not force and now - _VIDEOS["ts"] < 5:
            return cached
    data = _bridge("cdp/videos")
    with _VIDEOS_LOCK:
        _VIDEOS["ts"] = now
        _VIDEOS["data"] = data
    return data


def _search(q: str, page: int = 1, music: bool = False, order: str = "") -> dict:
    path = "cdp/search?q=" + quote(q) + "&page=%d" % page
    if music:
        path += "&music=1"
    if order in ("click", "pubdate", "dm", "stow"):
        path += "&order=" + order
    return _bridge(path)


def _open_bv(bv: str) -> dict:
    return _bridge("cdp/open?bv=" + bv)


def _feishu_app_token() -> str:
    now = time.time()
    if _APP_TOKEN["token"] and _APP_TOKEN["exp"] > now + 60:
        return _APP_TOKEN["token"]
    body = json.dumps({"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}).encode()
    req = urllib.request.Request(
        FEISHU_APP_TOKEN_URL, body, {"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    if data.get("code") != 0 or not data.get("app_access_token"):
        raise RuntimeError("feishu app token failed")
    _APP_TOKEN["token"] = data["app_access_token"]
    _APP_TOKEN["exp"] = now + int(data.get("expire") or 7200)
    return _APP_TOKEN["token"]


def _feishu_exchange(code: str) -> dict:
    body = json.dumps({"grant_type": "authorization_code", "code": code}).encode()
    req = urllib.request.Request(
        FEISHU_TOKEN_URL,
        body,
        {"Content-Type": "application/json", "Authorization": "Bearer " + _feishu_app_token()},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    if data.get("code") != 0:
        raise RuntimeError("feishu exchange failed")
    result = data.get("data") or {}
    if not result.get("open_id"):
        raise RuntimeError("feishu exchange missing open_id")
    return result


def _make_state() -> str:
    exp = str(int(time.time()) + 600)
    sig = hmac.new(H5_TOKEN.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return exp + "." + sig


def _check_state(value: str) -> bool:
    exp_s, _, sig = (value or "").partition(".")
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()) or not H5_TOKEN:
        return False
    want = hmac.new(H5_TOKEN.encode(), exp_s.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


def _allowed_openid(open_id: str) -> bool:
    """优先用 MEDIA_FEISHU_OPENIDS 预置白名单；没配时才退化成首次登录绑定。"""
    if not open_id:
        return False
    if FEISHU_OPENIDS:
        return any(hmac.compare_digest(x, open_id) for x in FEISHU_OPENIDS)
    p = pathlib.Path(FEISHU_OPENID_FILE)
    try:
        lines = [x.strip() for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    except FileNotFoundError:
        lines = []
    if not lines:
        print("[feishu-oauth] binding first open_id=%r" % open_id, flush=True)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(open_id + "\n", encoding="utf-8")
            p.chmod(0o600)
        except Exception as exc:
            print("[feishu-oauth] cannot persist open_id: %r" % exc, flush=True)
        return True
    return any(hmac.compare_digest(x, open_id) for x in lines)


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>媒体控制</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0b0f14;color:#e8eef5;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif}
.card{width:min(92vw,460px);background:#121922;border:1px solid #223042;border-radius:18px;padding:22px;box-shadow:0 24px 60px #000a}
.head{display:flex;align-items:center;gap:12px;margin-bottom:20px}
.logo{width:42px;height:42px;border-radius:12px;background:#1b2a3a;display:flex;align-items:center;justify-content:center;font-size:22px}
h1{font-size:17px;margin:0;font-weight:650;letter-spacing:.2px}
.muted{color:#8fa1b3;font-size:12px;margin:2px 0 0}
.badge{margin-left:auto;font-size:12px;padding:5px 10px;border-radius:999px;background:#26313d;color:#aab8c5;white-space:nowrap}
.badge.ok{background:#12351f;color:#6ee7a0}
.badge.warn{background:#3a2e12;color:#f5c451}
.badge.bad{background:#3a1717;color:#f28b82}
.track{background:#0d141c;border:1px solid #1d2a38;border-radius:14px;padding:16px;margin-bottom:12px}
.title{font-size:16px;line-height:1.45;font-weight:600;word-break:break-word}
.vols{display:flex;flex-direction:column;gap:8px;margin:0 2px 12px}
.vrow{display:flex;align-items:center;gap:10px}
.vrow .vlabel{width:34px;flex:none;font-size:12px;color:#8fa1b3}
.vrow input[type=range]{flex:1;min-width:0;accent-color:#2563eb;height:4px;cursor:pointer}
.vrow .vv{min-width:42px;text-align:right;font-size:12px;color:#8fa1b3;font-variant-numeric:tabular-nums}
.tools{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 12px;font-size:12px;color:#8fa1b3}
.tools select{background:#0d141c;border:1px solid #223042;color:#e8eef5;border-radius:8px;padding:6px 8px;font-size:12px}
.tools button{border:1px solid #2a3a4d;background:#1a2634;color:#e8eef5;border-radius:8px;padding:7px 10px;font-size:12px;cursor:pointer}
.tools label{display:flex;align-items:center;gap:4px}
.tools #qinfo{margin-left:auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.queuebox{max-height:150px;overflow:auto;border:1px solid #1d2a38;border-radius:10px;background:#0d141c;margin:-4px 0 10px;padding:4px 10px}
.qitem{display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid #17222e;font-size:12px;color:#c7d2de}
.qitem:first-child{border-top:0}
.qitem .qi{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.qitem .qx{border:0;background:none;color:#f28b82;font-size:14px;cursor:pointer;padding:0 2px}
.qitem.cur .qi{color:#7fb0ff}
.qactions{display:flex;gap:8px;padding:6px 0 2px;border-top:1px solid #17222e}
.qactions button{border:1px solid #2a3a4d;background:#1a2634;color:#e8eef5;border-radius:8px;padding:5px 9px;font-size:12px;cursor:pointer}
.volbtn{flex:none;width:38px;height:38px;padding:0;border:1px solid #2a3a4d;background:#1a2634;color:#e8eef5;border-radius:10px;font-size:19px;line-height:1;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:center;user-select:none;-webkit-user-select:none;touch-action:manipulation}
.volbtn:hover{background:#22303f}
.volbtn:active{background:#2563eb;border-color:#2563eb;color:#fff}
.progress{display:flex;align-items:center;gap:10px;margin:0 2px 14px}
.progress input[type=range]{flex:1;min-width:0;accent-color:#2563eb;height:4px;cursor:pointer}
.progress input[type=range]:disabled{opacity:.35}
.progress #posTxt{min-width:88px;text-align:right;font-variant-numeric:tabular-nums}
.actions .btn.small{flex:0 0 52px;padding:13px 0;font-size:17px}
.row2{display:flex;align-items:center;gap:10px;margin:0 0 14px}
.row2 .btn.mute{flex:0 0 auto;padding:10px 14px;font-size:13px}
.sleep{display:flex;align-items:center;gap:8px;font-size:12px;color:#8fa1b3;margin-left:auto}
.sleep select{background:#0d141c;border:1px solid #223042;color:#e8eef5;border-radius:8px;padding:7px 8px;font-size:12px}
.wake{display:none;margin:0 0 12px}
.wake.show{display:block}
.search{display:flex;gap:8px;margin:0 0 12px}
.search input{flex:1;min-width:0;background:#0d141c;border:1px solid #223042;border-radius:10px;color:#e8eef5;padding:10px 12px;font-size:14px;outline:none}
.search input:focus{border-color:#2563eb}
.search button{border:1px solid #2563eb;background:#2563eb;color:#fff;border-radius:10px;padding:0 16px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap}
.search label{display:flex;align-items:center;gap:4px;font-size:12px;color:#8fa1b3;white-space:nowrap}
.refreshbar{display:flex;align-items:center;gap:10px;margin:-4px 0 10px;font-size:11px;color:#8fa1b3}
.refreshbar #pageBtn{margin-left:auto}
.refreshbar .link{font-size:11px}
#results{max-height:46vh;overflow:auto;border:1px solid #1d2a38;border-radius:14px;background:#0d141c;margin-bottom:16px}
.video{display:flex;gap:10px;padding:10px;border:0;border-top:1px solid #1d2a38;background:none;width:100%;text-align:left;color:inherit;cursor:pointer}
.video:first-child{border-top:0}
.video:hover{background:#17222e}
.video.busy{opacity:.5;cursor:wait}
.video img{width:112px;height:63px;object-fit:cover;border-radius:8px;flex:none;background:#1b2a3a}
.video .meta{min-width:0;flex:1}
.video .vt{font-size:13px;line-height:1.4;color:#e8eef5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word}
.video .vs{font-size:11px;color:#8fa1b3;margin-top:5px;display:flex;gap:8px;flex-wrap:wrap}
.tab-empty{padding:14px 12px;color:#64748b;font-size:12px;line-height:1.5}
.actions{display:flex;gap:10px;margin-bottom:14px}
.btn{flex:1;border:1px solid #2a3a4d;background:#1a2634;color:#e8eef5;border-radius:12px;padding:13px 10px;font-size:15px;font-weight:600;cursor:pointer}
.btn.primary{background:#2563eb;border-color:#2563eb;color:#fff}
.btn:disabled{opacity:.4;cursor:not-allowed}
.foot{display:flex;align-items:center;gap:10px;flex-wrap:wrap;row-gap:6px}
.link{border:0;background:none;color:#7fb0ff;font-size:13px;cursor:pointer;padding:0}
.note{margin-top:16px;padding-top:12px;border-top:1px solid #1d2a38;color:#64748b;font-size:11px;line-height:1.5}
#msg{margin-left:auto}
.body{display:flex;flex-direction:column}
.controls,.browse{display:contents}
.track{order:1}.progress{order:2}.search{order:3}.refreshbar{order:4}#results{order:5}.vols{order:6}.tools{order:7}.actions{order:8}.row2{order:9}.foot{order:10}
@media (max-width:899px){
  html,body{height:100%}
  body{overflow:hidden;align-items:stretch;justify-content:flex-start;padding:8px}
  .card{width:100%;height:calc(100vh - 16px);height:calc(100dvh - 16px);display:flex;flex-direction:column;padding:14px;border-radius:16px}
  .head{margin-bottom:12px;gap:10px}
  .logo{width:38px;height:38px;border-radius:10px;font-size:20px}
  h1{font-size:16px}
  .head .muted{font-size:11px}
  .body{flex:1 1 auto;min-height:0}
  .track,.progress,.search,.refreshbar,.vols,.tools,.actions,.row2,.foot{flex:none}
  .track{padding:12px 14px;margin-bottom:8px}
  .progress{margin:0 2px 8px}
  .search{margin-bottom:8px}
  .refreshbar{margin:0 0 8px}
  #results{flex:1 1 auto;min-height:120px;max-height:none;margin-bottom:8px}
  .vols{gap:5px;margin:0 2px 8px}
  .vrow{gap:6px}
  .vrow .vlabel{width:30px;font-size:11px}
  .vrow .vv{min-width:38px;font-size:11px}
  .volbtn{width:34px;height:34px;font-size:17px;border-radius:9px}
  .tools{gap:6px;margin-bottom:8px}
  .tools button{padding:6px 9px}
  .foot{gap:8px}
  .actions{margin-bottom:8px}
  .btn{padding:11px 8px;font-size:14px}
  .actions .btn.small{padding:11px 0;font-size:16px}
  .row2{margin-bottom:8px}
  .note{display:none}
}
@media (min-width:900px){
  body{align-items:flex-start;padding:26px 18px}
  .card{width:min(94vw,1120px);padding:26px}
  .head{margin-bottom:22px}
  .body{display:grid;grid-template-columns:380px minmax(0,1fr);gap:0 24px;align-items:start}
  .controls,.browse{display:block}
  .controls{position:sticky;top:26px}
  #results{max-height:64vh;min-height:300px}
}
@media (min-width:1150px){
  #results{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:1px;align-content:start;background:#1d2a38}
  #results .video{background:#0d141c;border:0;padding:12px}
  #results .video:hover{background:#17222e}
  #results .video img{width:132px;height:74px}
  #results .tab-empty{grid-column:1/-1;background:#0d141c}
}
</style>
</head>
<body>
<main class="card">
  <div class="head">
    <div class="logo">🎵</div>
    <div><h1>媒体控制</h1><p class="muted">DSH · Windows 媒体桥</p></div>
    <span id="badge" class="badge">加载中</span>
  </div>
  <div class="body">
    <div class="controls">
      <div class="track">
        <div id="title" class="title">—</div>
        <div id="app" class="muted">—</div>
      </div>
      <div class="wake" id="wakeBox">
        <button id="wake" class="btn primary">⚡ 唤醒电脑</button>
      </div>
      <div class="progress">
        <input id="seek" type="range" min="0" max="1" step="1" value="0" disabled aria-label="播放进度">
        <span id="posTxt" class="muted">--:-- / --:--</span>
      </div>
      <div class="vols">
        <div class="vrow">
          <span class="vlabel">视频</span>
          <button id="vvolDown" class="volbtn" title="视频音量 -1" aria-label="视频音量减一">−</button>
          <input id="vvol" type="range" min="0" max="100" step="1" value="100" aria-label="视频音量">
          <span id="vvolVal" class="vv">--%</span>
          <button id="vvolUp" class="volbtn" title="视频音量 +1" aria-label="视频音量加一">+</button>
        </div>
        <div class="vrow">
          <span class="vlabel">系统</span>
          <button id="volDown" class="volbtn" title="系统音量 -1" aria-label="系统音量减一">−</button>
          <input id="vol" type="range" min="0" max="100" step="1" value="30" aria-label="系统音量">
          <span id="volVal" class="vv">--%</span>
          <button id="volUp" class="volbtn" title="系统音量 +1" aria-label="系统音量加一">+</button>
        </div>
      </div>
      <div class="tools">
        <select id="speed" aria-label="倍速">
          <option value="0.75">0.75x</option>
          <option value="1">1x</option>
          <option value="1.25">1.25x</option>
          <option value="1.5">1.5x</option>
          <option value="2">2x</option>
        </select>
        <button id="viewBtn" title="网页全屏">网页全屏</button>
        <button id="dmBtn" title="弹幕开关">弹幕</button>
        <label><input id="autoNext" type="checkbox" checked>连播</label>
        <button id="collBtn" title="把当前合集/分P加入队列">合集</button>
        <button id="eqBtn" title="响度均衡">均衡</button>
        <button id="qbtn" title="查看队列">队列</button>
        <span id="qinfo"></span>
      </div>
      <div id="queueBox" class="queuebox" hidden></div>
      <div class="actions">
        <button id="prev" class="btn small" title="上一首">⏮</button>
        <button id="play" class="btn">▶ 播放</button>
        <button id="pause" class="btn primary">⏸ 暂停</button>
        <button id="next" class="btn small" title="下一首">⏭</button>
      </div>
      <div class="row2">
        <button id="mute" class="btn mute">🔊 静音</button>
        <label class="sleep">定时暂停
          <select id="sleep" aria-label="定时暂停">
            <option value="0">关</option>
            <option value="1">1 分钟</option>
            <option value="15">15 分钟</option>
            <option value="30">30 分钟</option>
            <option value="45">45 分钟</option>
            <option value="60">60 分钟</option>
            <option value="90">90 分钟</option>
            <option value="120">120 分钟</option>
          </select>
          <span id="sleepInfo" class="muted"></span>
        </label>
      </div>
      <div class="foot">
        <button id="refresh" class="link">刷新</button>
        <button id="mineLater" class="link">稍后再看</button>
        <button id="mineHist" class="link">历史</button>
        <button id="mineFav" class="link">收藏夹</button>
        <a class="link" href="/voice">语音测试</a>
        <span id="msg" class="muted"></span>
      </div>
    </div>
    <div class="browse">
      <div class="search">
        <input id="q" placeholder="搜 B站 歌单 / 歌曲" autocomplete="off">
        <label><input id="music" type="checkbox" checked>音乐区</label>
        <button id="searchBtn">搜索</button>
      </div>
      <div class="refreshbar">
        <span id="refreshInfo">—</span>
        <button id="pageBtn" class="link">换一批</button>
        <button id="refreshBtn" class="link">刷新</button>
      </div>
      <div id="results"><div class="tab-empty">加载中…</div></div>
    </div>
  </div>
  <div class="note">只允许播放/暂停/切歌/静音/进度/音量/定时暂停/唤醒/搜索并播放 B站 视频。固定动作端点，无任意命令、无路径参数。</div>
</main>
<script nonce="__NONCE__">
const CSRF = "__CSRF__";
const $ = id => document.getElementById(id);
let busy = false;
let sleepDue = 0;
let seekDur = 0;
let seekDragging = false;
function msg(t){ $('msg').textContent = t || ''; }
function fmtSec(s){ s = Math.max(0, Math.round(Number(s) || 0)); return Math.floor(s/60) + ':' + String(s%60).padStart(2,'0'); }
function showMute(m){
  if(m === null || m === undefined) return;
  $('mute').textContent = m ? '🔇 取消静音' : '🔊 静音';
}
let posBase = null, posAt = 0, playing = false;
function renderProgress(pos, dur){
  const el = $('seek');
  seekDur = Number(dur) || 0;
  if(!seekDur || pos === null || pos === undefined){
    el.disabled = true;
    posBase = null;
    $('posTxt').textContent = '--:-- / --:--';
    return;
  }
  posBase = Number(pos) || 0;
  posAt = performance.now();
  el.max = Math.max(1, Math.round(seekDur));
  if(!seekDragging){
    el.value = Math.min(seekDur, Math.max(0, Math.round(pos)));
    $('posTxt').textContent = fmtSec(pos) + ' / ' + fmtSec(seekDur);
  }
  el.disabled = busy;
}
// 两次采样之间本地外推，进度条不再每 5 秒跳一格
function tickProgress(){
  if(posBase === null || !seekDur || seekDragging || busy || $('seek').disabled) return;
  if(!playing) return;
  const pos = Math.min(seekDur, posBase + (performance.now() - posAt) / 1000);
  $('seek').value = Math.round(pos);
  $('posTxt').textContent = fmtSec(pos) + ' / ' + fmtSec(seekDur);
}
setInterval(tickProgress, 1000);
function setOffline(pcOff){
  $('badge').textContent = pcOff ? '电脑离线' : '离线';
  $('badge').className = 'badge bad';
  $('wakeBox').classList.toggle('show', pcOff);
  ['play','pause','prev','next','mute'].forEach(id => { $(id).disabled = true; });
  $('sleep').disabled = true;
  $('seek').disabled = true;
}
function render(d){
  if(!d.ok){
    playing = false;
    const reason = d.reason || '';
    setOffline(d.pc_online === false);
    $('title').textContent = d.error || '桥不可达';
    $('app').textContent = reason === 'offline' ? '点上方按钮尝试唤醒'
      : reason === 'bridge_down' ? '检查 Windows 上的媒体桥是否在跑'
      : reason === 'no_player' ? '在电脑上打开一个 B 站视频页'
      : (d.pc_online === true ? '电脑在线，等待媒体桥启动' : '');
    sleepDue = d.sleep_due || 0;
    syncMediaSession(null);
    return;
  }
  const st = d.status || 'unknown';
  const map = {playing:['▶ 播放中','ok'], paused:['⏸ 已暂停','warn'], stopped:['已停止','warn']};
  const [txt, cls] = map[st] || ['未知','bad'];
  $('badge').textContent = txt; $('badge').className = 'badge ' + cls;
  $('title').textContent = d.title || '未知';
  $('app').textContent = d.app || '';
  $('wakeBox').classList.remove('show');
  showVol(d.volume);
  showVVol(d.vvol);
  showMute(d.muted);
  const q = d.queue || {};
  $('qinfo').textContent = q.n ? ((((q.idx|0) + 1)) + '/' + q.n + (q.next ? ' · 下一条 ' + q.next : '')) : '';
  $('autoNext').checked = q.auto !== false;
  if(d.rate) $('speed').value = String(d.rate);
  if(d.dm !== null && d.dm !== undefined) $('dmBtn').textContent = d.dm ? '弹幕开' : '弹幕关';
  if(d.eq !== null && d.eq !== undefined) $('eqBtn').textContent = d.eq ? '均衡开' : '均衡';
  playing = st === 'playing';
  // 交给 Android 壳：系统媒体会话 + 通知栏 + 锁屏
  if(window.AndroidMedia && AndroidMedia.update){
    try { AndroidMedia.update(d.title || '', playing); } catch(e){}
  }
  renderProgress(d.pos, d.dur);
  syncMediaSession({title: d.title, playing: playing, pos: d.pos, dur: d.dur});
  sleepDue = d.sleep_due || 0;
  const offline = d.pc_online === false;
  $('pause').disabled = busy || offline || st === 'paused';
  $('play').disabled = busy || offline || st === 'playing';
  $('prev').disabled = busy || offline;
  $('next').disabled = busy || offline;
  $('mute').disabled = busy || offline;
  $('sleep').disabled = offline;
}
async function refresh(){
  try {
    const r = await fetch('/api/status', {cache:'no-store'});
    const d = await r.json();
    failCount = 0;
    render(d);
    return d;
  } catch(e){
    failCount++;
    render({ok:false, error:'请求失败'});
    return null;
  }
}
// 锁屏 / 通知栏（Android Chrome、PWA、以及支持的 WebView 宿主）
function syncMediaSession(info){
  if(!('mediaSession' in navigator)) return;
  try{
    if(info === null){ navigator.mediaSession.playbackState = 'none'; return; }
    const art = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512">' +
      '<rect width="512" height="512" fill="#111922"/>' +
      '<text x="256" y="330" font-size="240" text-anchor="middle" fill="#5b8def">♪</text></svg>');
    navigator.mediaSession.metadata = new MediaMetadata({
      title: info.title || '未知',
      artist: 'B站音乐',
      artwork: [{src: art, sizes: '512x512', type: 'image/svg+xml'}]
    });
    navigator.mediaSession.playbackState = info.playing ? 'playing' : 'paused';
    if(info.dur > 0 && info.pos !== null && info.pos !== undefined){
      navigator.mediaSession.setPositionState({
        duration: info.dur, position: Math.min(info.dur, Math.max(0, info.pos)), playbackRate: 1
      });
    }
  } catch(e){}
}
// Android 壳收到锁屏/耳机按键后回调这里
window.mcMediaAction = function(a){ if(!busy) act(a); };
if('mediaSession' in navigator && navigator.mediaSession.setActionHandler){
  const MS_ACTIONS = {play:'play', pause:'pause', nexttrack:'next', previoustrack:'prev'};
  for(const a in MS_ACTIONS){
    try { navigator.mediaSession.setActionHandler(a, () => act(MS_ACTIONS[a])); } catch(e){}
  }
  try {
    navigator.mediaSession.setActionHandler('seekto', (e) => {
      if(e && e.seekTime !== null && e.seekTime !== undefined && !busy && seekDur){
        $('seek').value = Math.round(Math.min(seekDur, e.seekTime));
        sendSeek();
      }
    });
  } catch(e){}
}
async function post(url, body){
  const r = await fetch(url, {
    method:'POST',
    headers:{'X-CSRF-Token':CSRF, 'Content-Type':'application/x-www-form-urlencoded'},
    body: body || ''
  });
  if(r.status === 403){
    msg('页面已过期，正在刷新…');
    setTimeout(() => location.reload(), 600);
    return null;
  }
  if(r.status === 401){ msg('登录失效，请重新打开页面'); return null; }
  if(r.status === 429){
    const wait = r.headers.get('Retry-After');
    msg(wait ? ('太频繁或登录被锁，约 ' + wait + ' 秒后再试') : '操作太频繁，稍后重试');
    return null;
  }
  try { return await r.json(); } catch(e){ msg('响应异常'); return null; }
}
async function act(a){
  if(busy) return;
  busy = true; msg('执行中…'); $('play').disabled = true; $('pause').disabled = true;
  try {
    const d = await post('/api/' + a);
    if(!d) return;
    if(!d.ok) msg(d.error || '操作失败');
    else if(a === 'mute'){ showMute(d.muted); msg(d.muted ? '已静音' : '已取消静音'); }
    else msg({pause:'已暂停', play:'已播放', next:'下一首', prev:'上一首'}[a] || '已完成');
  } catch(e) { msg('请求失败'); }
  finally { busy = false; setTimeout(refresh, 1500); }
}
const vol = $('vol');
let volDragging = false;
let volTimer = null;
let volPending = null;
function showVol(v){
  if(v === null || v === undefined || volDragging || volPending !== null) return;
  v = Math.max(0, Math.min(100, Math.round(Number(v))));
  vol.value = v;
  $('volVal').textContent = v + '%';
}
async function sendVol(){
  const v = Number(vol.value);
  volPending = v;
  try {
    const d = await post('/api/volume', 'level=' + v);
    if(!d) return;
    if(!d.ok) msg(d.error || '音量调整失败');
    else if(volPending === v){ vol.value = d.volume; $('volVal').textContent = d.volume + '%'; }
  } catch(e){ msg('音量请求失败'); }
  finally { if(volPending === v) volPending = null; }
}
vol.addEventListener('pointerdown', () => { volDragging = true; });
addEventListener('pointerup', () => { volDragging = false; });
vol.addEventListener('input', () => {
  const v = Number(vol.value);
  $('volVal').textContent = v + '%';
  clearTimeout(volTimer);
  volTimer = setTimeout(sendVol, 250);
});
vol.addEventListener('change', () => { clearTimeout(volTimer); sendVol(); });
function nudgeVol(d){
  vol.value = Math.max(0, Math.min(100, Number(vol.value) + d));
  vol.dispatchEvent(new Event('input'));
}
$('volDown').addEventListener('click', () => nudgeVol(-1));
$('volUp').addEventListener('click', () => nudgeVol(1));
const vvol = $('vvol');
let vvolDragging = false;
let vvolTimer = null;
let vvolPending = null;
function showVVol(v){
  if(v === null || v === undefined || vvolDragging || vvolPending !== null) return;
  v = Math.max(0, Math.min(100, Math.round(Number(v))));
  vvol.value = v;
  $('vvolVal').textContent = v + '%';
}
async function sendVVol(){
  const v = Number(vvol.value);
  vvolPending = v;
  try {
    const d = await post('/api/vvol', 'level=' + v);
    if(!d) return;
    if(!d.ok) msg(d.error || '视频音量失败');
    else if(vvolPending === v && d.vvol !== null && d.vvol !== undefined){
      vvol.value = d.vvol;
      $('vvolVal').textContent = d.vvol + '%';
    }
  } catch(e){ msg('视频音量请求失败'); }
  finally { if(vvolPending === v) vvolPending = null; }
}
vvol.addEventListener('pointerdown', () => { vvolDragging = true; });
addEventListener('pointerup', () => { vvolDragging = false; });
vvol.addEventListener('input', () => {
  const v = Number(vvol.value);
  $('vvolVal').textContent = v + '%';
  clearTimeout(vvolTimer);
  vvolTimer = setTimeout(sendVVol, 250);
});
vvol.addEventListener('change', () => { clearTimeout(vvolTimer); sendVVol(); });
function nudgeVVol(d){
  vvol.value = Math.max(0, Math.min(100, Number(vvol.value) + d));
  vvol.dispatchEvent(new Event('input'));
}
$('vvolDown').addEventListener('click', () => nudgeVVol(-1));
$('vvolUp').addEventListener('click', () => nudgeVVol(1));
let videoBusy = false;
let currentList = [];
function renderVideos(list){
  currentList = Array.isArray(list) ? list : [];
  const box = $('results');
  box.textContent = '';
  if(!list || !list.length){
    const e = document.createElement('div');
    e.className = 'tab-empty';
    e.textContent = '没有视频，换个关键词试试';
    box.appendChild(e);
    return;
  }
  let vidx = -1;
  for(const v of list){
    vidx++;
    const b = document.createElement('button');
    b.className = 'video';
    const img = document.createElement('img');
    img.loading = 'lazy';
    img.alt = '';
    if(v.cover) img.src = v.cover;
    const meta = document.createElement('div');
    meta.className = 'meta';
    const t = document.createElement('div');
    t.className = 'vt';
    t.textContent = v.title || v.bv;
    const sub = document.createElement('div');
    sub.className = 'vs';
    const parts = [];
    if(v.author) parts.push(v.author);
    if(v.play) parts.push('▶ ' + v.play);
    if(v.danmaku) parts.push('弹幕 ' + v.danmaku);
    if(v.duration) parts.push(v.duration);
    sub.textContent = parts.join(' · ');
    meta.appendChild(t); meta.appendChild(sub);
    b.appendChild(img); b.appendChild(meta);
    b.onclick = async () => {
      try {
        const payload = currentList.map(x => ({bv:x.bv, title:x.title || ''}));
        await post('/api/queue/set', 'videos=' + encodeURIComponent(JSON.stringify(payload)) + '&idx=' + vidx);
      } catch(e) {}
      openVideo(v, b);
    };
    box.appendChild(b);
  }
}
let lastQuery = '';
let searchPage = 1;
let listMode = 'search';
let lastRefresh = 0;
const AUTO_MS = 10 * 60 * 1000;
function fmtTime(ts){
  const d = new Date(ts);
  return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}
function updateRefreshInfo(){
  const el = $('refreshInfo');
  if(listMode !== 'search') return;
  if(!lastRefresh){ el.textContent = '—'; return; }
  const remain = Math.max(0, AUTO_MS - (Date.now() - lastRefresh));
  if(remain <= 0){ el.textContent = '上次 ' + fmtTime(lastRefresh) + ' · 即将刷新'; return; }
  const m = Math.floor(remain / 60000);
  const s = Math.floor((remain % 60000) / 1000);
  el.textContent = '上次 ' + fmtTime(lastRefresh) + ' · ' + m + '分' + String(s).padStart(2,'0') + '秒后自动刷新';
}
setInterval(updateRefreshInfo, 1000);
async function loadVideos(force, silent){
  if(!silent){
    const box = $('results');
    box.textContent = '';
    const e = document.createElement('div');
    e.className = 'tab-empty';
    e.textContent = '加载中…';
    box.appendChild(e);
  }
  try {
    const r = await fetch('/api/videos' + (force ? '?force=1' : ''), {cache:'no-store'});
    const d = await r.json();
    if(!d.ok){ msg(d.error || '加载失败'); if(!silent) renderVideos([]); updateRefreshInfo(); return; }
    listMode = 'search';
    renderVideos(d.videos || []);
    lastRefresh = Date.now();
    lastQuery = '';
    searchPage = 1;
    const u = (d.tab || {}).url || '';
    const m = u.match(/[?&]keyword=([^&]+)/);
    if(m){
      try { lastQuery = decodeURIComponent(m[1].replace(/\+/g, ' ')).trim(); } catch(e) { lastQuery = ''; }
      const pm = u.match(/[?&]page=(\d+)/);
      if(pm) searchPage = Math.max(1, parseInt(pm[1], 10) || 1);
    }
    updateRefreshInfo();
  } catch(e) { msg('加载失败'); if(!silent) renderVideos([]); updateRefreshInfo(); }
}
async function searchVideos(opts){
  opts = opts || {};
  const q = (opts.q !== undefined ? opts.q : $('q').value).trim();
  if(!q){ return loadVideos(true, !!opts.silent); }
  if(opts.page !== undefined) searchPage = Math.max(1, Math.min(50, opts.page));
  else if(!opts.silent) searchPage = 1;
  if(!opts.silent){
    const box = $('results');
    box.textContent = '';
    const e = document.createElement('div');
    e.className = 'tab-empty';
    e.textContent = '搜索中…';
    box.appendChild(e);
  } else {
    $('refreshInfo').textContent = '刷新中…';
  }
  try {
    const musicQ = ($('music') && $('music').checked) ? '&music=1&order=click' : '';
    const r = await fetch('/api/search?q=' + encodeURIComponent(q) + '&page=' + searchPage + musicQ, {cache:'no-store'});
    const d = await r.json();
    if(!d.ok){ msg(d.error || '搜索失败'); if(!opts.silent) renderVideos([]); updateRefreshInfo(); return; }
    listMode = 'search';
    renderVideos(d.videos || []);
    lastQuery = q;
    lastRefresh = Date.now();
    updateRefreshInfo();
  } catch(e) { msg('搜索失败'); updateRefreshInfo(); }
}
function manualRefresh(){
  if(lastQuery) searchVideos({q:lastQuery, page:searchPage, silent:true});
  else loadVideos(true, true);
}
function nextPage(){
  if(!lastQuery){ msg('先搜索一个关键词'); return; }
  searchVideos({q:lastQuery, page:searchPage + 1});
}
async function openVideo(v, btn){
  if(videoBusy) return;
  videoBusy = true; btn.classList.add('busy'); msg('打开中…');
  try {
    const d = await post('/api/open', 'bv=' + encodeURIComponent(v.bv));
    if(!d) return;
    if(!d.ok) msg(d.error || '打开失败');
    else { msg('播放中'); setTimeout(refresh, 1200); }
  } catch(e) { msg('打开失败'); }
  finally { videoBusy = false; btn.classList.remove('busy'); }
}
$('searchBtn').onclick = () => searchVideos();
$('q').addEventListener('keydown', e => { if(e.key === 'Enter') searchVideos(); });
$('pageBtn').onclick = nextPage;
$('refreshBtn').onclick = manualRefresh;
$('speed').addEventListener('change', async () => {
  const v = $('speed').value;
  const d = await post('/api/speed', 'rate=' + v);
  if(d && d.ok) msg('倍速 ' + v + 'x'); else if(d) msg(d.error || '倍速失败');
});
$('viewBtn').onclick = async () => {
  const d = await post('/api/view', 'mode=web');
  msg(d && d.ok ? '已切换网页全屏' : (d ? d.error : '失败'));
};
$('dmBtn').onclick = async () => {
  const on = !($('dmBtn').textContent || '').includes('开');
  const d = await post('/api/danmaku', 'on=' + (on ? 1 : 0));
  if(d && d.ok) $('dmBtn').textContent = d.danmaku ? '弹幕开' : '弹幕关';
};
$('autoNext').addEventListener('change', () => post('/api/queue/auto', 'on=' + ($('autoNext').checked ? 1 : 0)));
async function renderQueue(){
  const box = $('queueBox');
  try {
    const r = await fetch('/api/queue/status', {cache:'no-store'});
    const d = await r.json();
    box.textContent = '';
    const q = d.queue || [];
    if(!q.length){
      const e = document.createElement('div'); e.className = 'tab-empty'; e.textContent = '队列为空';
      box.appendChild(e);
    }
    q.forEach((it, i) => {
      const row = document.createElement('div');
      row.className = 'qitem' + (i === d.idx ? ' cur' : '');
      const s = document.createElement('span'); s.className = 'qi';
      s.textContent = (i + 1) + '. ' + (it.title || it.bv) + (it.p > 1 ? ' P' + it.p : '');
      const x = document.createElement('button'); x.className = 'qx'; x.textContent = '×';
      x.onclick = async () => {
        const r2 = await post('/api/queue/del', 'i=' + i);
        if(r2 && r2.ok){ renderQueue(); refresh(); }
      };
      row.append(s, x); box.appendChild(row);
    });
    const act = document.createElement('div'); act.className = 'qactions';
    const fill = document.createElement('button'); fill.textContent = '追加相关';
    fill.onclick = async () => {
      const r2 = await post('/api/queue/fill');
      msg(r2 && r2.ok ? ('追加 ' + r2.added + ' 条') : '没有相关推荐');
      renderQueue(); refresh();
    };
    const clear = document.createElement('button'); clear.textContent = '清空';
    clear.onclick = async () => { await post('/api/queue/set', 'videos=[]&idx=0'); renderQueue(); refresh(); };
    act.append(fill, clear); box.appendChild(act);
  } catch(e) { msg('队列加载失败'); }
}
$('qbtn').onclick = async () => {
  const box = $('queueBox');
  box.hidden = !box.hidden;
  if(!box.hidden) await renderQueue();
};
$('collBtn').onclick = async () => {
  const d = await post('/api/queue/collection');
  if(d && d.ok){ msg('合集 ' + d.n + ' 条，从第 ' + (d.idx + 1) + ' 条开始'); setTimeout(refresh, 800); }
  else msg(d && d.error === 'no collection' ? '当前不是合集/多P' : (d ? d.error : '失败'));
};
$('eqBtn').onclick = async () => {
  const on = !($('eqBtn').textContent || '').includes('开');
  const d = await post('/api/eq', 'on=' + (on ? 1 : 0));
  if(d && d.ok) $('eqBtn').textContent = d.eq ? '均衡开' : '均衡';
  else msg(d ? (d.error || d.result || '均衡失败') : '均衡失败');
};
async function loadMine(kind){
  try {
    if(kind === 'fav'){
      const r = await fetch('/api/bili/favs', {cache:'no-store'});
      const d = await r.json();
      if(!d.ok){ msg(d.error || '收藏夹加载失败'); return; }
      renderFolders(d.folders || []);
      return;
    }
    const r = await fetch(kind === 'hist' ? '/api/bili/history' : '/api/bili/watchlater', {cache:'no-store'});
    const d = await r.json();
    if(!d.ok){ msg(d.error || '加载失败'); return; }
    showList(kind === 'hist' ? '历史' : '稍后再看', d.videos || []);
  } catch(e) { msg('加载失败'); }
}
function showList(title, list){
  listMode = 'list';
  currentList = Array.isArray(list) ? list : [];
  $('refreshInfo').textContent = title + ' · ' + currentList.length + ' 条';
  renderVideos(currentList);
}
function renderFolders(folders){
  listMode = 'folders';
  const box = $('results');
  box.textContent = '';
  $('refreshInfo').textContent = '收藏夹 · ' + folders.length + ' 个';
  if(!folders.length){
    const e = document.createElement('div'); e.className = 'tab-empty'; e.textContent = '没有收藏夹';
    box.appendChild(e); return;
  }
  for(const f of folders){
    const b = document.createElement('button'); b.className = 'video';
    const meta = document.createElement('div'); meta.className = 'meta';
    const t = document.createElement('div'); t.className = 'vt';
    t.textContent = (f.title || '收藏夹') + '（' + (f.count || 0) + '）';
    meta.appendChild(t); b.appendChild(meta);
    b.onclick = async () => {
      const r = await fetch('/api/bili/fav?id=' + encodeURIComponent(f.id), {cache:'no-store'});
      const d = await r.json();
      if(!d.ok){ msg(d.error || '加载失败'); return; }
      showList('收藏夹 ' + (f.title || ''), d.videos || []);
    };
    box.appendChild(b);
  }
}
$('mineLater').onclick = () => loadMine('later');
$('mineHist').onclick = () => loadMine('hist');
$('mineFav').onclick = () => loadMine('fav');
loadVideos(false, false);
setInterval(() => {
  if(document.hidden || listMode !== 'search') return;
  if(lastRefresh && Date.now() - lastRefresh >= AUTO_MS) manualRefresh();
}, 30000);
document.addEventListener('visibilitychange', () => {
  if(document.hidden) return;
  if(lastRefresh && Date.now() - lastRefresh >= AUTO_MS) manualRefresh();
});
async function sendSeek(){
  if(!seekDur || busy || $('seek').disabled) return;
  busy = true; msg('跳转中…');
  try {
    const d = await post('/api/seek', 'pos=' + Math.round(Number($('seek').value)));
    if(!d) return;
    msg(d.ok ? ('已跳到 ' + fmtSec($('seek').value)) : (d.error || '跳转失败'));
  } catch(e){ msg('跳转失败'); }
  finally { busy = false; setTimeout(refresh, 1200); }
}
$('seek').addEventListener('pointerdown', () => { seekDragging = true; });
addEventListener('pointerup', () => { if(seekDragging){ seekDragging = false; sendSeek(); } });
$('seek').addEventListener('input', () => {
  $('posTxt').textContent = fmtSec($('seek').value) + ' / ' + fmtSec(seekDur);
});
function updateSleepInfo(){
  const el = $('sleepInfo');
  if(!el) return;
  if(!sleepDue){ el.textContent = ''; return; }
  const remain = Math.round(sleepDue - Date.now() / 1000);
  if(remain <= 0){ el.textContent = '即将暂停'; return; }
  const m = Math.floor(remain / 60), s = remain % 60;
  el.textContent = m ? (m + ' 分 ' + String(s).padStart(2,'0') + ' 秒') : (s + ' 秒');
}
setInterval(updateSleepInfo, 1000);
async function setSleep(min){
  busy = true; msg('设置定时暂停…');
  try {
    const d = await post('/api/sleep', 'min=' + min);
    if(!d) return;
    if(!d.ok){ msg(d.error || '设置失败'); return; }
    sleepDue = d.due || 0;
    msg(min > 0 ? ('将在 ' + fmtTime(sleepDue * 1000) + ' 自动暂停') : '已取消定时暂停');
  } catch(e){ msg('设置失败'); }
  finally { busy = false; }
}
$('sleep').onchange = () => setSleep(Number($('sleep').value));
let wakeTimer = null;
$('wake').onclick = async () => {
  if(wakeTimer) return;
  msg('发送唤醒包…');
  try {
    const d = await post('/api/wake');
    if(!d) return;
    if(!d.ok){ msg(d.error || '唤醒不可用'); return; }
    msg('已发送，等待电脑启动…');
    let n = 0;
    wakeTimer = setInterval(async () => {
      n++;
      try {
        const r = await fetch('/api/pc', {cache:'no-store'});
        const p = await r.json();
        if(p.online){ clearInterval(wakeTimer); wakeTimer = null; msg('电脑已上线，正在加载…'); refresh(); }
        else if(n >= 30){ clearInterval(wakeTimer); wakeTimer = null; msg('唤醒超时：检查网卡/BIOS WoL 设置'); }
      } catch(e){}
    }, 3000);
  } catch(e){ msg('唤醒请求失败'); }
};
$('prev').onclick = () => queueStep('prev');
async function queueStep(dir){
  if(busy) return;
  busy = true;
  msg(dir === 'next' ? '下一首…' : '上一首…');
  try {
    const d = await post('/api/queue/' + dir);
    if(!d) return;
    if(!d.ok) msg(d.error || '没有队列');
    else { msg('队列 ' + (d.idx + 1) + '/' + d.n); setTimeout(refresh, 1500); }
  } catch(e) { msg('请求失败'); }
  finally { busy = false; }
}
$('next').onclick = () => queueStep('next');
$('mute').onclick = () => act('mute');
$('play').onclick = () => act('play');
$('pause').onclick = () => act('pause');
$('refresh').onclick = refresh;
// 轮询改为自适应：播放中 5s、暂停 15s、失败指数退避、页面隐藏完全停
let refreshTimer = 0, failCount = 0;
function refreshDelay(){
  if(failCount > 0) return Math.min(30000, 4000 * Math.pow(2, failCount));
  return playing ? 5000 : 15000;
}
function scheduleRefresh(ms){
  clearTimeout(refreshTimer);
  if(document.hidden) return;
  refreshTimer = setTimeout(tick, Math.max(500, ms | 0));
}
async function tick(){
  if(document.hidden) return;
  if(busy){ scheduleRefresh(1500); return; }
  await refresh();
  scheduleRefresh(refreshDelay());
}
document.addEventListener('visibilitychange', () => {
  if(document.hidden){ clearTimeout(refreshTimer); return; }
  tick();
});
tick();
</script>
</body>
</html>"""

VOICE_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>语音测试</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:#0b0f14;color:#e8eef5;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;padding:14px}
.card{max-width:760px;margin:0 auto;background:#121922;border:1px solid #223042;border-radius:16px;padding:18px}
.head{display:flex;align-items:center;gap:10px;margin-bottom:12px}
h1{font-size:17px;margin:0;flex:1}
a{color:#7fb0ff;text-decoration:none;font-size:13px}
.badge{font-size:12px;padding:4px 10px;border-radius:999px;background:#26313d}
.badge.ok{background:#12351f;color:#6ee7a0}
.badge.bad{background:#3a1717;color:#f28b82}
.rows{display:grid;grid-template-columns:76px 1fr;gap:9px 10px;font-size:13px;margin:12px 0}
.rows .k{color:#8fa1b3}
.rows .v{word-break:break-word;min-width:0}
.ph{margin:10px 0 4px}
.big{width:100%;padding:16px;font-size:16px;touch-action:none;user-select:none;-webkit-user-select:none}
.test{display:flex;gap:8px;margin:12px 0;flex-wrap:wrap}
.test input{flex:1 1 100%;background:#0d141c;border:1px solid #223042;border-radius:10px;color:#e8eef5;padding:10px 12px;font-size:14px;outline:none}
.test input:focus{border-color:#2563eb}
button{border:1px solid #2a3a4d;background:#1a2634;color:#e8eef5;border-radius:10px;padding:10px 12px;font-size:13px;cursor:pointer}
button.primary{background:#2563eb;border-color:#2563eb;color:#fff}
button:disabled{opacity:.4}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 6px}
.ev{border-top:1px solid #1d2a38;padding:7px 0;font-size:12px;color:#c7d2de;display:flex;gap:8px}
.ev .t{color:#64748b;flex:none;font-variant-numeric:tabular-nums}
.ev .e{color:#7fb0ff;flex:none;width:56px}
.ev .v{min-width:0;word-break:break-word}
.note{color:#64748b;font-size:11px;line-height:1.5;margin-top:12px}
@media (min-width:560px){.rows{grid-template-columns:86px 1fr}.test input{flex:1 1 auto}}
</style>
</head>
<body>
<main class="card">
  <div class="head">
    <h1>🎤 语音测试</h1>
    <span id="badge" class="badge">加载中</span>
    <a href="/">返回</a>
  </div>
  <div class="ph">
    <button id="talk" class="primary big">按住说话（手机麦克风）</button>
    <div id="phInfo" class="muted">按住按钮说话，松开后识别并执行</div>
  </div>
  <div class="rows">
    <span class="k">手机识别</span><span class="v" id="sPhone">—</span>
    <span class="k">监听</span><span class="v" id="sOn">—</span>
    <span class="k">模型</span><span class="v" id="sReady">—</span>
    <span class="k">最后识别</span><span class="v" id="sText">—</span>
    <span class="k">命中命令</span><span class="v" id="sCmd">—</span>
    <span class="k">最后动作</span><span class="v" id="sAct">—</span>
    <span class="k">回执</span><span class="v" id="sAck">—</span>
  </div>
  <div class="test">
    <input id="q" value="电脑帮我换周杰伦的歌" autocomplete="off">
    <button id="dry">只解析</button>
    <button id="run" class="primary">执行</button>
  </div>
  <div class="btns">
    <button id="ack">重放「我在呢」</button>
    <button id="toggle">关闭监听</button>
  </div>
  <div id="events"></div>
  <div class="note">识别 → 命令解析 → B站搜索第一条播放。点「执行」会切换电脑当前播放的歌曲。</div>
</main>
<script nonce="__NONCE__">
const CSRF = "__CSRF__";
const $ = id => document.getElementById(id);
let on = true;
function fmtTs(s){ if(!s) return ''; const d = new Date(s*1000); return d.toTimeString().slice(0,8); }
function show(d){
  const v = d.voice || {};
  on = !!v.on;
  $('badge').textContent = v.ready ? (v.on ? '监听中' : '已暂停') : (v.error ? '异常' : '加载中');
  $('badge').className = 'badge ' + (v.ready && v.on ? 'ok' : 'bad');
  $('sOn').textContent = v.on ? '开启' : '关闭';
  $('sReady').textContent = v.ready ? ('就绪 · 设备' + v.device) : (v.error || '未就绪');
  $('sText').textContent = v.last_text || '—';
  const c = v.last_cmd;
  $('sCmd').textContent = c ? ((c.kind === 'next' ? '下一首' : ('搜索 ' + c.q)) + ' · ' + (c.text || '')) : '—';
  const a = v.last_action;
  $('sAct').textContent = a ? (a.kind === 'search' ? ('搜索 ' + a.q + (a.ok ? ' → ' + (a.title || a.bv || '') : ' 失败')) : (a.kind === 'next' ? '下一首' : a.kind)) + (a.ok === false ? '（失败）' : '') : '—';
  $('sAck').textContent = v.last_ack ? fmtTs(v.last_ack) + ' 播放过' : '—';
  $('toggle').textContent = v.on ? '关闭监听' : '开启监听';
  const box = $('events');
  box.textContent = '';
  for(const e of (v.events || []).slice().reverse().slice(0, 15)){
    const row = document.createElement('div'); row.className = 'ev';
    const t = document.createElement('span'); t.className = 't'; t.textContent = fmtTs(e.ts);
    const k = document.createElement('span'); k.className = 'e'; k.textContent = e.ev;
    const x = document.createElement('span'); x.className = 'v';
    x.textContent = e.text || e.q || e.say || (e.ok !== undefined ? (e.ok ? 'ok' : 'fail') : '') || e.err || '';
    row.append(t, k, x); box.appendChild(row);
  }
}
async function post(url, body){
  const r = await fetch(url, {method:'POST', headers:{'X-CSRF-Token':CSRF, 'Content-Type':'application/x-www-form-urlencoded'}, body: body || ''});
  if(r.status === 403){ location.reload(); return null; }
  return r.json();
}
async function refresh(){
  try { const r = await fetch('/api/voice', {cache:'no-store'}); const d = await r.json(); if(d.ok) show(d); else { $('badge').textContent = '桥不可达'; $('badge').className = 'badge bad'; } }
  catch(e) { $('badge').textContent = '请求失败'; $('badge').className = 'badge bad'; }
}
$('dry').addEventListener('click', async () => {
  if(!$('q').value.trim()){ alert('先输入命令'); return; }
  const d = await post('/api/voice/test', 'q=' + encodeURIComponent($('q').value) + '&dry=1');
  if(d && d.cmd) alert('解析结果：' + (d.cmd.kind === 'next' ? '下一首' : d.cmd.q));
  else if(d) alert(d.error || '不是命令');
  refresh();
});
$('run').addEventListener('click', async () => {
  if(!$('q').value.trim()){ alert('先输入命令'); return; }
  if(!confirm('会切换当前播放，继续？')) return;
  const d = await post('/api/voice/test', 'q=' + encodeURIComponent($('q').value) + '&dry=0');
  if(d && !d.ok) alert(d.error || '失败');
  setTimeout(refresh, 1500);
});
$('ack').addEventListener('click', async () => { await post('/api/voice/ack'); setTimeout(refresh, 800); });
$('toggle').addEventListener('click', async () => { await post('/api/voice/on', 'on=' + (on ? '0' : '1')); setTimeout(refresh, 500); });
const media = {stream:null, ac:null, src:null, proc:null, chunks:[], on:false};
const PUB = "__PUBLIC_URL__";
const talk = $('talk');
if(!window.isSecureContext){
  talk.disabled = true;
  if(PUB && !location.href.startsWith(PUB)){ location.replace(PUB + 'voice'); }
  else if(PUB) $('phInfo').innerHTML = '局域网 HTTP 不能开麦克风，<a href="' + PUB + 'voice" style="color:#7fb0ff">点这里用 HTTPS 打开</a>';
  else $('phInfo').textContent = '手机麦克风需要 HTTPS 页面（请用公网地址打开）';
}
async function startTalk(e){
  e.preventDefault();
  if(media.on || talk.disabled) return;
  try {
    try {
      media.stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1, echoCancellation:true, noiseSuppression:true}});
    } catch(firstErr) {
      media.stream = await navigator.mediaDevices.getUserMedia({audio:true});
    }
    media.ac = new (window.AudioContext || window.webkitAudioContext)();
    media.src = media.ac.createMediaStreamSource(media.stream);
    media.proc = media.ac.createScriptProcessor(4096, 1, 1);
    media.chunks = [];
    media.proc.onaudioprocess = ev => {
      if(!media.on) return;
      const d = ev.inputBuffer.getChannelData(0);
      const a = new Int16Array(d.length);
      for(let i=0;i<d.length;i++){ let s = Math.max(-1, Math.min(1, d[i])); a[i] = s < 0 ? s*32768 : s*32767; }
      media.chunks.push(a);
    };
    media.src.connect(media.proc);
    media.proc.connect(media.ac.destination);
    media.on = true;
    talk.textContent = '松开发送…';
    $('phInfo').textContent = '录音中…';
  } catch(err) {
    $('phInfo').textContent = '麦克风不可用：' + err.message;
  }
}
async function stopTalk(){
  if(!media.on) return;
  media.on = false;
  talk.textContent = '按住说话';
  const rate = media.ac ? media.ac.sampleRate : 16000;
  try {
    media.proc.disconnect(); media.src.disconnect();
    media.stream.getTracks().forEach(t => t.stop());
    if(media.ac) media.ac.close();
  } catch(err) {}
  let total = 0;
  for(const c of media.chunks) total += c.length;
  if(total < 4000){ $('phInfo').textContent = '录音太短，再说一次'; return; }
  const pre = Math.floor(rate * 0.15);
  const post = Math.floor(rate * 0.20);
  const bytes = new Uint8Array((total + pre + post) * 2);
  const dv = new DataView(bytes.buffer);
  let off = pre * 2;
  for(const c of media.chunks){
    for(let i=0;i<c.length;i++){ dv.setInt16(off, c[i], true); off += 2; }
  }
  media.chunks = [];
  $('phInfo').textContent = '识别中…';
  try {
    const r = await fetch('/api/voice/audio?rate=' + rate, {
      method:'POST',
      headers:{'X-CSRF-Token':CSRF, 'Content-Type':'application/octet-stream'},
      body: bytes
    });
    const d = await r.json();
    $('sPhone').textContent = (d.text || '（无结果）') +
      (d.cmd ? ' → ' + (d.cmd.kind === 'next' ? '下一首' : ('搜索 ' + d.cmd.q)) : '（非命令）');
    $('phInfo').textContent = d.ok ? '完成' : (d.error || '失败');
    refresh();
  } catch(err) {
    $('phInfo').textContent = '上传失败：' + err.message;
  }
}
talk.addEventListener('pointerdown', startTalk);
addEventListener('pointerup', stopTalk);
setInterval(refresh, 2000);
refresh();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "media-web"
    sys_version = ""
    protocol_version = "HTTP/1.1"      # 复用连接，省掉每次请求的建连开销

    def _cookie_ok(self) -> bool:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME and v:
                exp_s, _, sig = v.partition(".")
                try:
                    exp = int(exp_s)
                except ValueError:
                    return False
                if exp < int(time.time()) or not H5_TOKEN:
                    return False
                want = hmac.new(H5_TOKEN.encode(), exp_s.encode(), hashlib.sha256).hexdigest()
                return hmac.compare_digest(sig, want)
        return False

    def _cookie_value(self) -> str:
        exp = str(int(time.time()) + COOKIE_TTL)
        sig = hmac.new(H5_TOKEN.encode(), exp.encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{sig}"

    def _auth(self) -> bool:
        if self._cookie_ok():
            return True
        if not AUTH_CONFIGURED:          # 没配密码就整体拒服务，不留后门
            return False
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(h[6:], validate=True).decode("utf-8")
            user, _, pw = raw.partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user, USER) and hmac.compare_digest(pw, PASSWORD)

    def _ip(self) -> str:
        """反代（cloudflared / nginx）后 client_address 恒为 127.0.0.1，此时信任 CF-Connecting-IP。"""
        ip = self.client_address[0]
        if TRUST_PROXY_HEADERS and ip in ("127.0.0.1", "::1"):
            cf = (self.headers.get("CF-Connecting-IP") or "").strip()
            if cf and re.fullmatch(r"[0-9A-Fa-f:.]{3,45}", cf):
                return _deny_ip(cf)
        return _deny_ip(ip)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None, nonce: str = "") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            f"script-src 'nonce-{nonce}'; connect-src 'self'; "
            "img-src 'self' data: https://*.hdslb.com https://*.bilibili.com; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'",
        )
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            self.send_header("Strict-Transport-Security", "max-age=15552000")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _deny(self) -> None:
        self._send(401, b"auth required", "text/plain; charset=utf-8",
                   {"WWW-Authenticate": 'Basic realm="media", charset="UTF-8"'})

    def _guard(self) -> bool:
        ip = self._ip()
        wait = _login_locked(ip)
        if wait > 0:
            self._send(429, b"too many failed logins", "text/plain; charset=utf-8",
                       {"Retry-After": str(int(wait))})
            return False
        if not _allow(ip):
            self._json({"ok": False, "error": "too many requests"}, 429)
            return False
        if not self._auth():
            # 只有「带了凭据但不对」才计入爆破计数；浏览器首次 401 挑战不算
            if self.headers.get("Authorization"):
                locked = _login_failed(ip)
                if locked:
                    self._send(429, b"too many failed logins", "text/plain; charset=utf-8",
                               {"Retry-After": str(int(locked))})
                    return False
            self._deny()
            return False
        _login_ok(ip)
        return True

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in ("/feishu", "/feishu/"):
            if not (FEISHU_APP_ID and FEISHU_APP_SECRET and H5_TOKEN):
                self._json({"ok": False, "error": "feishu oauth disabled"}, 503)
                return
            if not _allow(self._ip()):
                self._json({"ok": False, "error": "too many requests"}, 429)
                return
            if self._cookie_ok():
                nonce = secrets.token_urlsafe(16)
                html = PAGE.replace("__NONCE__", nonce).replace("__CSRF__", CSRF)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", nonce=nonce)
                return
            url = FEISHU_AUTH_URL + "?" + urlencode({
                "app_id": FEISHU_APP_ID,
                "redirect_uri": FEISHU_REDIRECT_URI,
                "state": _make_state(),
            })
            self._send(302, b"", "text/plain; charset=utf-8", {"Location": url})
            return
        if path == "/feishu/callback":
            if not _allow(self._ip()):
                self._json({"ok": False, "error": "too many requests"}, 429)
                return
            q = parse_qs(parsed.query)
            code = (q.get("code") or [""])[0]
            state = (q.get("state") or [""])[0]
            if not code or not _check_state(state):
                self._send(400, b"bad oauth state", "text/plain; charset=utf-8")
                return
            try:
                data = _feishu_exchange(code)
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
                return
            open_id = str(data.get("open_id") or "")
            if not open_id or not _allowed_openid(open_id):
                print(
                    "[feishu-oauth] denied open_id=%r name=%r"
                    % (open_id, data.get("name") or data.get("en_name") or ""),
                    flush=True,
                )
                self._send(403, b"not allowed", "text/plain; charset=utf-8")
                return
            self._send(302, b"", "text/plain; charset=utf-8", {
                "Location": "/feishu/",
                "Set-Cookie": (
                    f"{COOKIE_NAME}={self._cookie_value()}; Max-Age={COOKIE_TTL}; "
                    "Path=/; HttpOnly; Secure; SameSite=Lax"
                ),
            })
            return
        if path in ("/h5", "/h5/"):
            if not H5_TOKEN:
                self._json({"ok": False, "error": "h5 disabled"}, 403)
                return
            if not _allow(self._ip()):
                self._json({"ok": False, "error": "too many requests"}, 429)
                return
            if not self._cookie_ok():
                token = (parse_qs(parsed.query).get("t") or [""])[0]
                if not token or not hmac.compare_digest(token, H5_TOKEN):
                    self._json({"ok": False, "error": "invalid or missing token"}, 403)
                    return
                self._send(302, b"", "text/plain; charset=utf-8", {
                    "Location": "/h5/",
                    "Set-Cookie": (
                        f"{COOKIE_NAME}={self._cookie_value()}; Max-Age={COOKIE_TTL}; "
                        "Path=/; HttpOnly; Secure; SameSite=Lax"
                    ),
                })
                return
            nonce = secrets.token_urlsafe(16)
            html = PAGE.replace("__NONCE__", nonce).replace("__CSRF__", CSRF)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", nonce=nonce)
            return
        if path not in ("/", "/index.html", "/api/status", "/api/health", "/api/pc", "/api/climate",
                        "/api/tabs", "/api/videos", "/api/search", "/api/voice", "/voice",
                        "/api/queue/status", "/api/bili/watchlater", "/api/bili/history",
                        "/api/bili/favs", "/api/bili/fav"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if not self._guard():
            return
        if path in ("/", "/index.html"):
            nonce = secrets.token_urlsafe(16)
            html = PAGE.replace("__NONCE__", nonce).replace("__CSRF__", CSRF)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", nonce=nonce)
            return
        if path == "/api/health":
            self._json({"ok": True, "version": VERSION,
                        "uptime_s": int(time.time() - START_TS),
                        "auth": AUTH_CONFIGURED, "h5": bool(H5_TOKEN)})
            return
        if path == "/voice":
            nonce = secrets.token_urlsafe(16)
            html = VOICE_PAGE.replace("__NONCE__", nonce).replace("__CSRF__", CSRF).replace("__PUBLIC_URL__", PUBLIC_URL)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", nonce=nonce)
            return
        if path == "/api/voice":
            try:
                self._json(_bridge("voice/status"))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/pc":
            # 唤醒后前端每 2s 探一次，这里不能用 10s 缓存
            self._json({"ok": True, "online": _pc_online(ttl=2.0), "wol": bool(WIN_MAC)})
            return
        if path == "/api/climate":
            try:
                self._json(_climate())
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/tabs":
            force = (parse_qs(parsed.query).get("force") or [""])[0] == "1"
            try:
                self._json(_tabs(force))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/videos":
            force = (parse_qs(parsed.query).get("force") or [""])[0] == "1"
            try:
                self._json(_videos(force))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/search":
            qs = parse_qs(parsed.query)
            q = (qs.get("q") or [""])[0].strip()
            if not q:
                self._json({"ok": False, "error": "empty query"}, 400)
                return
            try:
                page = int((qs.get("page") or ["1"])[0])
            except ValueError:
                page = 1
            page = max(1, min(50, page))
            music = (qs.get("music") or [""])[0] in ("1", "true")
            order = (qs.get("order") or [""])[0]
            try:
                self._json(_search(q, page, music=music, order=order))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/queue/status":
            try:
                self._json(_bridge("queue/status"))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path in ("/api/bili/watchlater", "/api/bili/history"):
            try:
                self._json(_bridge("bili/" + path.rsplit("/", 1)[-1]))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/bili/favs":
            try:
                self._json(_bridge("bili/favs"))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        if path == "/api/bili/fav":
            qs = parse_qs(parsed.query)
            try:
                fid = int((qs.get("id") or ["0"])[0])
                page = int((qs.get("page") or ["1"])[0])
            except ValueError:
                fid, page = 0, 1
            if fid <= 0:
                self._json({"ok": False, "error": "bad id"}, 400)
                return
            try:
                self._json(_bridge("bili/fav?id=%d&page=%d" % (fid, max(1, page))))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 502)
            return
        try:
            self._json(_status())
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 502)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        action = path.rsplit("/", 1)[-1]
        if path not in ("/api/pause", "/api/play", "/api/next", "/api/prev", "/api/mute",
                        "/api/volume", "/api/seek", "/api/sleep", "/api/wake",
                        "/api/tab", "/api/open",
                        "/api/voice/test", "/api/voice/ack", "/api/voice/on",
                        "/api/voice/audio", "/api/queue/next", "/api/queue/prev",
                        "/api/queue/auto", "/api/queue/set", "/api/speed", "/api/vvol",
                        "/api/view", "/api/danmaku", "/api/eq", "/api/queue/add", "/api/queue/del",
                        "/api/queue/fill", "/api/queue/collection"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if action not in ("volume", "tab", "open", "seek", "sleep", "wake", "test", "ack", "on", "audio",
                          "next", "prev", "auto", "set", "speed", "vvol", "view", "danmaku",
                          "add", "del", "fill", "collection", "eq") and action not in ACTIONS:
            self._json({"ok": False, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        max_len = 3 * 1024 * 1024 if path == "/api/voice/audio" else (16 * 1024 if path in ("/api/queue/set", "/api/queue/add") else 1024)
        if length > max_len:
            self._json({"ok": False, "error": "body too large"}, 413)
            return
        # 先鉴权 / CSRF，再读请求体：未认证请求没法让服务端白读 3 MB
        if not self._guard():
            self.close_connection = True
            return
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), CSRF):
            self._json({"ok": False, "error": "bad csrf token"}, 403)
            self.close_connection = True
            return
        raw_bytes = self.rfile.read(length) if length else b""
        raw = raw_bytes.decode("utf-8", "replace")
        if not _allow(self._ip(), limit=60):
            self._json({"ok": False, "error": "too many actions"}, 429)
            return
        tid = ""
        w = t = None
        if action == "tab":
            q = parse_qs(raw)
            tid = (q.get("id") or [""])[0]
            if tid:
                if not re.fullmatch(r"[0-9A-Fa-f]{16,64}", tid):
                    self._json({"ok": False, "error": "bad tab id"}, 400)
                    return
            else:
                try:
                    w = int((q.get("w") or [""])[0])
                    t = int((q.get("t") or [""])[0])
                except ValueError:
                    w = t = None
                if w is None or t is None or not (0 <= w < 100 and 0 <= t < 200):
                    self._json({"ok": False, "error": "bad tab"}, 400)
                    return
        bv = ""
        if action == "open":
            bv = (parse_qs(raw).get("bv") or [""])[0].strip()
            if not re.fullmatch(r"BV[0-9A-Za-z]+", bv):
                self._json({"ok": False, "error": "bad bv"}, 400)
                return
        level = None
        if action == "volume":
            try:
                level = int((parse_qs(raw).get("level") or [""])[0])
            except ValueError:
                level = None
            if level is None or not 0 <= level <= 100:
                self._json({"ok": False, "error": "bad level"}, 400)
                return
        pos = None
        if action == "seek":
            try:
                pos = float((parse_qs(raw).get("pos") or [""])[0])
            except ValueError:
                pos = None
            if pos is None or not 0 <= pos <= 86400:
                self._json({"ok": False, "error": "bad pos"}, 400)
                return
        minutes = None
        if action == "sleep":
            try:
                minutes = int((parse_qs(raw).get("min") or [""])[0])
            except ValueError:
                minutes = None
            if minutes not in (0, 1, 15, 30, 45, 60, 90, 120):
                self._json({"ok": False, "error": "bad minutes"}, 400)
                return
        if not _ACTION_LOCK.acquire(blocking=False):
            self._json({"ok": False, "error": "busy"}, 409)
            return
        _status_invalidate()
        try:
            if action == "open":
                data = _open_bv(bv)
                self._json({
                    "ok": bool(data.get("ok")),
                    "action": "open",
                    "url": data.get("url"),
                    "error": data.get("error"),
                })
            elif action == "tab":
                data = _switch_tab(tid, w or -1, t or -1)
                self._json({
                    "ok": bool(data.get("ok")),
                    "action": "tab",
                    "tab": data.get("tab"),
                    "error": data.get("error"),
                })
            elif action == "audio":
                q = parse_qs(urlsplit(self.path).query)
                try:
                    rate = int((q.get("rate") or ["16000"])[0])
                except ValueError:
                    rate = 0
                if not 8000 <= rate <= 96000:
                    self._json({"ok": False, "error": "bad rate"}, 400)
                    return
                dry = (q.get("dry") or ["0"])[0] in ("1", "true")
                data = _bridge_post("voice/audio?rate=%d&dry=%s" % (rate, "1" if dry else "0"),
                                    raw_bytes, "application/octet-stream")
                data["action"] = "audio"
                self._json(data)
            elif action == "test":
                q = parse_qs(raw)
                text = (q.get("q") or [""])[0].strip()
                dry = (q.get("dry") or ["0"])[0] in ("1", "true")
                if not text:
                    self._json({"ok": False, "error": "empty q"}, 400)
                    return
                data = _bridge("voice/test?q=%s&dry=%s" % (quote(text), "1" if dry else "0"))
                data["action"] = "test"
                self._json(data)
            elif action == "ack":
                data = _bridge("voice/ack")
                data["action"] = "ack"
                self._json(data)
            elif action == "on":
                on = (parse_qs(raw).get("on") or ["1"])[0]
                data = _bridge("voice?on=%s" % ("0" if on in ("0", "false", "off") else "1"))
                data["action"] = "on"
                self._json(data)
            elif action in ("next", "prev"):
                data = _bridge("queue/" + action)
                data["action"] = action
                self._json(data)
            elif action == "auto":
                on = (parse_qs(raw).get("on") or ["1"])[0]
                data = _bridge("queue/auto?on=" + ("0" if on in ("0", "false", "off") else "1"))
                data["action"] = "auto"
                self._json(data)
            elif action == "set":
                q = parse_qs(raw)
                videos = (q.get("videos") or ["[]"])[0]
                idx = (q.get("idx") or ["0"])[0]
                data = _bridge("queue/set?videos=" + quote(videos) + "&idx=" + quote(idx))
                data["action"] = "set"
                self._json(data)
            elif action == "eq":
                on = (parse_qs(raw).get("on") or ["1"])[0]
                data = _bridge("cdp/eq?on=" + ("1" if on in ("1", "true", "on") else "0"))
                data["action"] = "eq"
                self._json(data)
            elif action == "add":
                q = parse_qs(raw)
                videos = (q.get("videos") or ["[]"])[0]
                data = _bridge("queue/add?videos=" + quote(videos))
                data["action"] = "add"
                self._json(data)
            elif action == "del":
                i = (parse_qs(raw).get("i") or ["-1"])[0]
                data = _bridge("queue/del?i=" + quote(i))
                data["action"] = "del"
                self._json(data)
            elif action == "fill":
                data = _bridge("queue/fill")
                data["action"] = "fill"
                self._json(data)
            elif action == "collection":
                data = _bridge("queue/collection")
                data["action"] = "collection"
                self._json(data)
            elif action == "speed":
                rate = (parse_qs(raw).get("rate") or ["1"])[0]
                data = _bridge("cdp/speed?rate=" + quote(rate))
                data["action"] = "speed"
                self._json(data)
            elif action == "vvol":
                level2 = (parse_qs(raw).get("level") or ["100"])[0]
                data = _bridge("cdp/volume?level=" + quote(level2))
                data["action"] = "vvol"
                self._json(data)
            elif action == "view":
                mode = (parse_qs(raw).get("mode") or ["web"])[0]
                data = _bridge("cdp/view?mode=" + quote(mode))
                data["action"] = "view"
                self._json(data)
            elif action == "danmaku":
                on = (parse_qs(raw).get("on") or ["1"])[0]
                data = _bridge("cdp/danmaku?on=" + ("1" if on in ("1", "true", "on") else "0"))
                data["action"] = "danmaku"
                self._json(data)
            elif action == "volume":
                data = _set_volume(level)
                self._json({
                    "ok": bool(data.get("ok")),
                    "action": "volume",
                    "volume": data.get("volume"),
                    "error": data.get("error"),
                })
            elif action == "seek":
                data = _bridge("cdp/seek?pos=%.1f" % pos)
                self._json({
                    "ok": bool(data.get("ok")),
                    "action": "seek",
                    "pos": data.get("pos"),
                    "error": data.get("error"),
                })
            elif action == "sleep":
                data = _set_sleep(minutes)
                data["action"] = "sleep"
                self._json(data)
            elif action == "wake":
                data = _wake()
                self._json({
                    "ok": bool(data.get("ok")),
                    "action": "wake",
                    "sent": data.get("sent"),
                    "error": data.get("error"),
                })
            else:
                data = _bridge(action)
                self._json({
                    "ok": bool(data.get("ok")),
                    "action": action,
                    "muted": data.get("muted"),
                    "error": data.get("error"),
                    "session": data.get("session"),
                })
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 502)
        finally:
            _ACTION_LOCK.release()

    def log_message(self, fmt: str, *args) -> None:
        # request line only; never log Authorization headers.
        print("%s %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    print(f"media-web on http://{HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
