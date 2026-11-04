#!/usr/bin/env python3
"""Fixed-action media control page for the Windows bridge.

Security model:
- Only GET / , GET /api/status, GET /api/health, POST /api/pause, POST /api/play.
- No user-controlled path, file name, command or action string reaches a shell.
- SSH command is a fixed argv list; the bridge path is one of two constants.
- HTTP Basic auth (over the TLS reverse proxy) + per-process CSRF header.
- Per-IP rate limit; no CORS; no directory/file serving.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlsplit

HOST = os.environ.get("MEDIA_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEDIA_WEB_PORT", "8097"))
USER = os.environ.get("MEDIA_WEB_USER", "media")
PASSWORD = os.environ.get("MEDIA_WEB_PASSWORD", "")
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

FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_REDIRECT_URI = os.environ.get(
    "FEISHU_REDIRECT_URI", "http://127.0.0.1:8097/feishu/callback"
)
FEISHU_OPENID_FILE = os.environ.get("FEISHU_OPENID_FILE", "/etc/media-web-feishu-openid")
FEISHU_AUTH_URL = "https://open.feishu.cn/open-apis/authen/v1/index"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v1/access_token"
FEISHU_APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
_APP_TOKEN = {"token": "", "exp": 0.0}
_TABS = {"ts": 0.0, "data": None}
_TABS_LOCK = threading.Lock()
_VIDEOS = {"ts": 0.0, "data": None}
_VIDEOS_LOCK = threading.Lock()

WIN_HOST = os.environ.get("MEDIA_WIN_HOST", "windows-host")
BRIDGE = os.environ.get("MEDIA_BRIDGE_URL", "http://127.0.0.1:8765/")
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", WIN_HOST]
ACTIONS = {"pause", "play"}

_RATE: dict[str, deque] = defaultdict(deque)
_RATE_LOCK = threading.Lock()
_ACTION_LOCK = threading.Lock()


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


def _bridge(path: str) -> dict:
    cmd = SSH + ["curl", "-s", "--max-time", "8", '"' + BRIDGE + path + '"']
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError("bridge unreachable")
    data = json.loads(r.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("bad bridge response")
    return data


def _status() -> dict:
    try:
        cdp = _bridge("cdp/status")
        if cdp.get("ok"):
            return {
                "ok": True,
                "title": TITLE_SUFFIX_RE.sub("", cdp.get("title") or "").strip(),
                "app": "Microsoft Edge（受控）",
                "status": "paused" if cdp.get("paused") else "playing",
                "volume": cdp.get("volume"),
                "url": cdp.get("url") or "",
                "pos": cdp.get("t"),
                "dur": cdp.get("dur"),
                "backend": "cdp",
                "updated": int(time.time()),
            }
    except Exception:
        pass
    data = _bridge("list")
    chosen = data.get("chosen") or {}
    return {
        "ok": True,
        "title": chosen.get("title") or "",
        "app": chosen.get("app") or "",
        "status": chosen.get("status_name") or "unknown",
        "volume": data.get("volume"),
        "backend": "smtc",
        "updated": int(time.time()),
    }


def _set_volume(level: int) -> dict:
    return _bridge("volume?level=%d" % level)


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


def _search(q: str, page: int = 1) -> dict:
    return _bridge("cdp/search?q=" + quote(q) + "&page=%d" % page)


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
    p = pathlib.Path(FEISHU_OPENID_FILE)
    try:
        current = p.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        current = ""
    if not current:
        p.write_text(open_id + "\n", encoding="utf-8")
        p.chmod(0o600)
        return True
    return hmac.compare_digest(current, open_id)


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
.vol{display:flex;align-items:center;gap:12px;margin:0 2px 16px}
.vol input[type=range]{flex:1;accent-color:#2563eb;height:4px;cursor:pointer}
.vol #volVal{min-width:42px;text-align:right}
.search{display:flex;gap:8px;margin:0 0 12px}
.search input{flex:1;min-width:0;background:#0d141c;border:1px solid #223042;border-radius:10px;color:#e8eef5;padding:10px 12px;font-size:14px;outline:none}
.search input:focus{border-color:#2563eb}
.search button{border:1px solid #2563eb;background:#2563eb;color:#fff;border-radius:10px;padding:0 16px;font-size:14px;font-weight:600;cursor:pointer;white-space:nowrap}
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
.foot{display:flex;align-items:center;gap:12px}
.link{border:0;background:none;color:#7fb0ff;font-size:13px;cursor:pointer;padding:0}
.note{margin-top:16px;padding-top:12px;border-top:1px solid #1d2a38;color:#64748b;font-size:11px;line-height:1.5}
#msg{margin-left:auto}
</style>
</head>
<body>
<main class="card">
  <div class="head">
    <div class="logo">🎵</div>
    <div><h1>媒体控制</h1><p class="muted">Windows 媒体桥</p></div>
    <span id="badge" class="badge">加载中</span>
  </div>
  <div class="track">
    <div id="title" class="title">—</div>
    <div id="app" class="muted">—</div>
  </div>
  <div class="search">
    <input id="q" placeholder="搜 B站 歌单 / 歌曲" autocomplete="off">
    <button id="searchBtn">搜索</button>
  </div>
  <div class="refreshbar">
    <span id="refreshInfo">—</span>
    <button id="pageBtn" class="link">换一批</button>
    <button id="refreshBtn" class="link">刷新</button>
  </div>
  <div id="results"><div class="tab-empty">加载中…</div></div>
  <div class="vol">
    <span id="volIcon" title="系统音量">🔊</span>
    <input id="vol" type="range" min="0" max="100" step="1" value="30" aria-label="系统音量">
    <span id="volVal" class="muted">--%</span>
  </div>
  <div class="actions">
    <button id="play" class="btn">▶ 播放</button>
    <button id="pause" class="btn primary">⏸ 暂停</button>
  </div>
  <div class="foot">
    <button id="refresh" class="link">刷新</button>
    <span id="msg" class="muted"></span>
  </div>
  <div class="note">只允许暂停 / 播放 / 系统音量 / 搜索并播放 B站 视频。固定动作端点，无任意命令、无路径参数。</div>
</main>
<script nonce="__NONCE__">
const CSRF = "__CSRF__";
const $ = id => document.getElementById(id);
let busy = false;
function msg(t){ $('msg').textContent = t || ''; }
function render(d){
  if(!d.ok){
    $('badge').textContent = '离线'; $('badge').className = 'badge bad';
    $('title').textContent = d.error || '桥不可达'; $('app').textContent = '';
    return;
  }
  const st = d.status || 'unknown';
  const map = {playing:['▶ 播放中','ok'], paused:['⏸ 已暂停','warn'], stopped:['已停止','warn']};
  const [txt, cls] = map[st] || ['未知','bad'];
  $('badge').textContent = txt; $('badge').className = 'badge ' + cls;
  $('title').textContent = d.title || '未知';
  $('app').textContent = d.app || '';
  showVol(d.volume);
  $('pause').disabled = busy || st === 'paused';
  $('play').disabled = busy || st === 'playing';
}
async function refresh(){
  try { const r = await fetch('/api/status', {cache:'no-store'}); render(await r.json()); }
  catch(e){ render({ok:false, error:'请求失败'}); }
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
  return r.json();
}
async function act(a){
  if(busy) return;
  busy = true; msg('执行中…'); $('play').disabled = true; $('pause').disabled = true;
  try {
    const d = await post('/api/' + a);
    if(!d) return;
    if(!d.ok) msg(d.error || '操作失败');
    else msg(a === 'pause' ? '已暂停' : '已播放');
  } catch(e) { msg('请求失败'); }
  finally { busy = false; setTimeout(refresh, 1500); }
}
const vol = $('vol');
let volDragging = false;
let volTimer = null;
let volPending = null;
function volIcon(v){ return v <= 0 ? '🔇' : (v < 50 ? '🔉' : '🔊'); }
function showVol(v){
  if(v === null || v === undefined || volDragging) return;
  v = Math.max(0, Math.min(100, Math.round(Number(v))));
  vol.value = v;
  $('volVal').textContent = v + '%';
  $('volIcon').textContent = volIcon(v);
}
async function sendVol(){
  const v = Number(vol.value);
  volPending = v;
  try {
    const d = await post('/api/volume', 'level=' + v);
    if(!d) return;
    if(!d.ok) msg(d.error || '音量调整失败');
    else if(volPending === v){ $('volVal').textContent = d.volume + '%'; $('volIcon').textContent = volIcon(d.volume); }
  } catch(e){ msg('音量请求失败'); }
}
vol.addEventListener('pointerdown', () => { volDragging = true; });
addEventListener('pointerup', () => { volDragging = false; });
vol.addEventListener('input', () => {
  const v = Number(vol.value);
  $('volVal').textContent = v + '%';
  $('volIcon').textContent = volIcon(v);
  clearTimeout(volTimer);
  volTimer = setTimeout(sendVol, 250);
});
vol.addEventListener('change', () => { clearTimeout(volTimer); sendVol(); });
let videoBusy = false;
function renderVideos(list){
  const box = $('results');
  box.textContent = '';
  if(!list || !list.length){
    const e = document.createElement('div');
    e.className = 'tab-empty';
    e.textContent = '没有视频，换个关键词试试';
    box.appendChild(e);
    return;
  }
  for(const v of list){
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
    b.onclick = () => openVideo(v, b);
    box.appendChild(b);
  }
}
let lastQuery = '';
let searchPage = 1;
let lastRefresh = 0;
const AUTO_MS = 10 * 60 * 1000;
function fmtTime(ts){
  const d = new Date(ts);
  return String(d.getHours()).padStart(2,'0') + ':' + String(d.getMinutes()).padStart(2,'0');
}
function updateRefreshInfo(){
  const el = $('refreshInfo');
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
    renderVideos(d.videos || []);
    lastRefresh = Date.now();
    lastQuery = '';
    searchPage = 1;
    const u = (d.tab || {}).url || '';
    const m = u.match(/[?&]keyword=([^&]+)/);
    if(m){
      try { $('q').value = decodeURIComponent(m[1].replace(/\+/g, ' ')); } catch(e) {}
      lastQuery = $('q').value.trim();
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
    const r = await fetch('/api/search?q=' + encodeURIComponent(q) + '&page=' + searchPage, {cache:'no-store'});
    const d = await r.json();
    if(!d.ok){ msg(d.error || '搜索失败'); if(!opts.silent) renderVideos([]); updateRefreshInfo(); return; }
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
loadVideos(false, false);
setInterval(() => {
  if(document.hidden) return;
  if(lastRefresh && Date.now() - lastRefresh >= AUTO_MS) manualRefresh();
}, 30000);
document.addEventListener('visibilitychange', () => {
  if(!document.hidden && lastRefresh && Date.now() - lastRefresh >= AUTO_MS) manualRefresh();
});
$('play').onclick = () => act('play');
$('pause').onclick = () => act('pause');
$('refresh').onclick = refresh;
refresh();
setInterval(() => { if(!busy) refresh(); }, 5000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "media-web"
    sys_version = ""

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
        if not USER or not PASSWORD:
            return False
        if self._cookie_ok():
            return True
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
        return self.client_address[0]

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
        if not _allow(self._ip()):
            self._json({"ok": False, "error": "too many requests"}, 429)
            return False
        if not self._auth():
            self._deny()
            return False
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
        if path not in ("/", "/index.html", "/api/status", "/api/health",
                        "/api/tabs", "/api/videos", "/api/search"):
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
            self._json({"ok": True})
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
            q = (parse_qs(parsed.query).get("q") or [""])[0].strip()
            if not q:
                self._json({"ok": False, "error": "empty query"}, 400)
                return
            try:
                page = int((parse_qs(parsed.query).get("page") or ["1"])[0])
            except ValueError:
                page = 1
            page = max(1, min(50, page))
            try:
                self._json(_search(q, page))
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
        if path not in ("/api/pause", "/api/play", "/api/volume", "/api/tab", "/api/open"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if action not in ("volume", "tab", "open") and action not in ACTIONS:
            self._json({"ok": False, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length > 1024:
            self._json({"ok": False, "error": "body too large"}, 413)
            return
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        if not self._guard():
            return
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), CSRF):
            self._json({"ok": False, "error": "bad csrf token"}, 403)
            return
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
        if not _ACTION_LOCK.acquire(blocking=False):
            self._json({"ok": False, "error": "busy"}, 409)
            return
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
            elif action == "volume":
                data = _set_volume(level)
                self._json({
                    "ok": bool(data.get("ok")),
                    "action": "volume",
                    "volume": data.get("volume"),
                    "error": data.get("error"),
                })
            else:
                data = _bridge(action)
                self._json({
                    "ok": bool(data.get("ok")),
                    "action": action,
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
