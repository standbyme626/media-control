import asyncio
import ctypes
import json
import os
import re
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlsplit

LOG = os.environ.get("BRIDGE_LOG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dodaios-bridge.log")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


STATUS = {0: "closed", 1: "opened", 2: "changing", 3: "stopped", 4: "playing", 5: "paused"}


def _endpoint_volume():
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def volume(level=None):
    ep = _endpoint_volume()
    if level is not None:
        ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, level / 100.0)), None)
    return int(round(ep.GetMasterVolumeLevelScalar() * 100))


def muted():
    try:
        return bool(_endpoint_volume().GetMute())
    except Exception:
        return None


def set_mute(toggle=True):
    try:
        ep = _endpoint_volume()
        state = (not bool(ep.GetMute())) if toggle else False
        ep.SetMute(state, None)
        return {"ok": True, "muted": state, "volume": volume()}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


CDP = "http://127.0.0.1:9222"
_LAST = {"id": ""}
_PLAYER = {"id": ""}
_LIST = {"id": ""}
SEARCH_URL = "https://search.bilibili.com/all?keyword="
VIDEO_INFO = ("(()=>{const v=document.querySelector('video');"
              "return {title:document.title,url:location.href,"
              "paused:v?v.paused:null,t:v?Math.round(v.currentTime):null,"
              "dur:v?Math.round(v.duration):null}})()")


def cdp_targets():
    try:
        with urllib.request.urlopen(CDP + "/json/list", timeout=3) as r:
            data = json.load(r)
    except Exception:
        return []
    out = []
    for t in data:
        if t.get("type") != "page":
            continue
        url = t.get("url") or ""
        if url.startswith(("edge://", "about:", "devtools://")):
            continue
        out.append(t)
    return out


def cdp_eval(target, expr, gesture=False):
    import websocket

    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=4)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expr, "userGesture": gesture, "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return (msg.get("result") or {}).get("result", {}).get("value")
    finally:
        ws.close()


VIDEO_JS = r"""(()=>{const out=[],seen=new Set();
for(const c of document.querySelectorAll('.bili-video-card')){
  const a=c.querySelector('a[href*="/video/BV"]');
  if(!a)continue;
  const m=(a.href||'').match(/\/video\/(BV[0-9A-Za-z]+)/);
  if(!m||seen.has(m[1]))continue;
  seen.add(m[1]);
  const g=s=>{const e=c.querySelector(s);return e?((e.innerText||e.textContent||'').trim()):''};
  const titleEl=c.querySelector('.bili-video-card__info--tit')||c.querySelector('.bili-video-card__title');
  let stats=[...c.querySelectorAll('.bili-video-card__stats--item')].map(e=>(e.innerText||'').trim()).filter(Boolean);
  let dur=g('.bili-video-card__stats__duration');
  if(!stats.length){
    const spans=[...c.querySelectorAll('.bili-cover-card__stat span')].map(e=>(e.innerText||'').trim()).filter(Boolean);
    if(spans.length>=2)stats=[spans[0],spans[1]];
    if(!dur&&spans.length>=3)dur=spans[2];
  }
  const img=c.querySelector('img');
  out.push({bv:m[1],
    title:(titleEl?(titleEl.innerText||titleEl.textContent||'').trim():'')||(a.title||'').trim(),
    author:g('.bili-video-card__info--author'),
    date:g('.bili-video-card__info--date')||g('.bili-video-card__subtitle'),
    play:stats[0]||'',danmaku:stats[1]||'',duration:dur,
    cover:img?img.src:''});
}
return out.slice(0,60)})()"""


def cdp_call(target, method, params):
    import websocket

    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=4)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg
    finally:
        ws.close()


def cdp_new_tab(url):
    req = urllib.request.Request(
        CDP + "/json/new?" + quote(url, safe=":/?=&%"), method="PUT")
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.load(r)


def cdp_close(tid):
    if not tid:
        return
    try:
        urllib.request.urlopen(CDP + "/json/close/" + tid, timeout=3).read()
    except Exception:
        pass


def cdp_videos():
    targets = [t for t in cdp_targets() if "bilibili.com" in (t.get("url") or "")]
    if not targets:
        return {"ok": False, "error": "no bilibili page"}
    targets.sort(key=lambda t: 0 if "search.bilibili.com" in (t.get("url") or "") else 1)
    best = None
    for t in targets:
        try:
            videos = cdp_eval(t, VIDEO_JS) or []
        except Exception:
            continue
        if videos and (best is None or len(videos) > len(best[1])):
            best = (t, videos)
    if best is None:
        return {"ok": False, "error": "no video cards"}
    t, videos = best
    return {"ok": True,
            "tab": {"id": t["id"], "title": t.get("title") or "", "url": t.get("url") or ""},
            "videos": videos}


def cdp_scroll_videos(tab):
    videos = cdp_eval(tab, VIDEO_JS) or []
    seen = {v.get("bv") for v in videos}
    for _ in range(3):
        try:
            cdp_eval(tab, "window.scrollTo(0, document.body.scrollHeight); 1")
        except Exception:
            break
        time.sleep(1.2)
        try:
            more = cdp_eval(tab, VIDEO_JS) or []
        except Exception:
            break
        fresh = [v for v in more if v.get("bv") not in seen]
        if not fresh:
            break
        seen.update(v.get("bv") for v in fresh)
        videos.extend(fresh)
    return videos


def cdp_playlist(url):
    if not re.match(r"https://(space|www)\.bilibili\.com/", url or ""):
        return {"ok": False, "error": "bad playlist url"}
    tab = None
    if _LIST["id"]:
        tab = next((t for t in cdp_targets() if t.get("id") == _LIST["id"]), None)
    if tab is None:
        try:
            tab = cdp_new_tab(url)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        _LIST["id"] = tab.get("id") or ""
    else:
        try:
            cdp_call(tab, "Page.navigate", {"url": url})
        except Exception:
            pass
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            videos = cdp_scroll_videos(tab)
        except Exception:
            videos = []
        if videos:
            return {"ok": True, "url": url, "title": tab.get("title") or "", "videos": videos}
        time.sleep(0.8)
    return {"ok": False, "error": "playlist timeout"}


def cdp_search(q, page=1):
    if not q:
        return {"ok": False, "error": "empty query"}
    url = SEARCH_URL + quote(q)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    if page > 1:
        url += "&page=%d" % page
    tab = next((t for t in cdp_targets() if "search.bilibili.com" in (t.get("url") or "")), None)
    if tab is not None:
        try:
            cdp_call(tab, "Page.navigate", {"url": url})
        except Exception:
            cdp_close(tab["id"])
            tab = None
    if tab is None:
        try:
            tab = cdp_new_tab(url)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            videos = cdp_eval(tab, VIDEO_JS) or []
        except Exception:
            videos = []
        if videos:
            return {"ok": True,
                    "tab": {"id": tab.get("id"), "title": tab.get("title") or "", "url": url},
                    "videos": videos}
        time.sleep(0.8)
    return {"ok": False, "error": "search timeout"}


def cdp_open_bv(bv):
    if not re.fullmatch(r"BV[0-9A-Za-z]+", bv or ""):
        return {"ok": False, "error": "bad bv"}
    url = "https://www.bilibili.com/video/%s/" % bv
    victim = _PLAYER["id"]
    if not victim:
        try:
            st = cdp_status()
            if st.get("ok") and "/video/" in (st.get("url") or ""):
                victim = st.get("id")
        except Exception:
            pass
    cdp_close(victim)
    try:
        tab = cdp_new_tab(url)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    _PLAYER["id"] = tab.get("id") or ""
    _LAST["id"] = _PLAYER["id"]
    time.sleep(1.5)
    try:
        cdp_eval(tab, "(()=>{const v=document.querySelector('video');if(v)v.play();return v?1:0})()",
                 gesture=True)
    except Exception:
        pass
    return {"ok": True, "id": _PLAYER["id"], "url": url}


def cdp_list():
    rows = [{"id": t.get("id"), "title": t.get("title") or "", "url": t.get("url") or ""}
            for t in cdp_targets()]
    return {"ok": True, "tabs": rows}


def cdp_status():
    infos = []
    for t in cdp_targets():
        try:
            info = cdp_eval(t, VIDEO_INFO)
        except Exception:
            info = None
        if info:
            info["id"] = t["id"]
            infos.append(info)
    if not infos:
        return {"ok": False, "error": "no cdp video"}
    chosen = next((i for i in infos if i.get("paused") is False), None)
    if chosen is None:
        chosen = next((i for i in infos if i.get("id") == _LAST["id"]), None) or infos[0]
    _LAST["id"] = chosen["id"]
    return {"ok": True, "id": chosen["id"], "title": chosen.get("title") or "",
            "url": chosen.get("url") or "", "paused": chosen.get("paused"),
            "t": chosen.get("t"), "dur": chosen.get("dur"), "volume": volume(),
            "muted": muted()}


def cdp_activate(tid):
    targets = cdp_targets()
    target = next((t for t in targets if t.get("id") == tid), None)
    if target is None:
        return {"ok": False, "error": "bad cdp id"}
    try:
        urllib.request.urlopen(CDP + "/json/activate/" + target["id"], timeout=3).read()
    except Exception:
        pass
    _LAST["id"] = target["id"]
    return {"ok": True, "id": target["id"], "title": target.get("title") or ""}


def cdp_play(tid=None):
    targets = cdp_targets()
    if not targets:
        return {"ok": False, "error": "no cdp targets"}
    target = next((t for t in targets if t.get("id") == tid), None)
    if target is None:
        target = next((t for t in targets if t.get("id") == _LAST["id"]), None) or targets[0]
    try:
        urllib.request.urlopen(CDP + "/json/activate/" + target["id"], timeout=3).read()
    except Exception:
        pass
    try:
        val = cdp_eval(target,
                       "(()=>{const v=document.querySelector('video');if(!v)return null;"
                       "v.play();return {paused:v.paused,title:document.title}})()",
                       gesture=True)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    _LAST["id"] = target["id"]
    return {"ok": True, "id": target["id"],
            "tab": (val or {}).get("title") or target.get("title") or "",
            "paused": (val or {}).get("paused")}


def cdp_pause():
    targets = cdp_targets()
    if not targets:
        return {"ok": False, "error": "no cdp targets"}
    n = 0
    for t in targets:
        try:
            cdp_eval(t, "(()=>{const v=document.querySelector('video');"
                        "if(v)v.pause();return v?1:0})()")
            n += 1
        except Exception:
            pass
    return {"ok": True, "paused_tabs": n}


def cdp_seek(pos):
    targets = cdp_targets()
    if not targets:
        return {"ok": False, "error": "no cdp targets"}
    target = next((t for t in targets if t.get("id") == _LAST["id"]), None)
    if target is None:
        for t in targets:
            try:
                info = cdp_eval(t, VIDEO_INFO)
            except Exception:
                continue
            if info and info.get("paused") is False:
                target = t
                break
            if target is None and info:
                target = t
    if target is None:
        target = targets[0]
    try:
        val = cdp_eval(target,
                       "(()=>{const v=document.querySelector('video');if(!v)return null;"
                       "v.currentTime=%s;return Math.round(v.currentTime)})()" % float(pos))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if val is None:
        return {"ok": False, "error": "no video"}
    _LAST["id"] = target["id"]
    return {"ok": True, "pos": val}


def _manager_cls():
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )
    return MediaManager


async def describe(session):
    info = session.get_playback_info()
    st = int(info.playback_status)
    d = {
        "app": session.source_app_user_model_id,
        "status": st,
        "status_name": STATUS.get(st, "unknown"),
    }
    try:
        props = await session.try_get_media_properties_async()
        d["title"] = props.title or ""
        d["artist"] = props.artist or ""
    except Exception as exc:
        d["title"] = ""
        d["artist"] = ""
        d["props_error"] = str(exc)
    return d


async def snapshot():
    manager = await _manager_cls().request_async()
    sessions = list(manager.get_sessions())
    out = []
    for session in sessions:
        out.append(await describe(session))
    current = manager.get_current_session()
    current_app = current.source_app_user_model_id if current else None
    playing = [s for s in sessions if int(s.get_playback_info().playback_status) == 4]
    chosen = playing[0] if playing else (current or (sessions[0] if sessions else None))
    chosen_d = await describe(chosen) if chosen else None
    return out, chosen_d, current_app


def media_key(name):
    vk = {"pause": 0xB3, "play": 0xB3, "toggle": 0xB3, "next": 0xB0, "prev": 0xB1}.get(name, 0xB3)
    try:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)
        return True
    except Exception as exc:
        log("media_key failed: %r" % (exc,))
        return False


async def act(name):
    manager = await _manager_cls().request_async()
    sessions = list(manager.get_sessions())
    playing = [s for s in sessions if int(s.get_playback_info().playback_status) == 4]
    chosen = playing[0] if playing else (manager.get_current_session() or (sessions[0] if sessions else None))
    if chosen is None:
        return {"ok": media_key(name), "action": name, "fallback": "media-key", "note": "no media session"}
    fn = {
        "pause": chosen.try_pause_async,
        "play": chosen.try_play_async,
        "toggle": chosen.try_toggle_play_pause_async,
        "next": chosen.try_skip_next_async,
        "prev": chosen.try_skip_previous_async,
    }.get(name)
    if fn is None:
        return {"ok": False, "error": "unknown action"}
    ok = bool(await fn())
    return {"ok": ok, "action": name, "session": await describe(chosen)}


BROWSERS = ("chrome.exe", "msedge.exe", "brave.exe", "firefox.exe")


def _browser_windows(desktop):
    import psutil

    rows = []
    for win in desktop.windows():
        try:
            name = psutil.Process(win.process_id()).name().lower()
        except Exception:
            continue
        if name in BROWSERS:
            rows.append((name, win))
    return rows


def list_tabs():
    from pywinauto import Desktop
    import psutil

    desktop = Desktop(backend="uia")
    rows = []
    for win in desktop.windows():
        try:
            name = psutil.Process(win.process_id()).name().lower()
        except Exception:
            continue
        if name not in BROWSERS:
            continue
        title = win.window_text() or ""
        try:
            tabs = [t.window_text() for t in win.descendants(control_type="TabItem") if t.window_text()]
        except Exception as exc:
            rows.append({"id": len(rows), "browser": name, "window": title,
                         "tabs": [], "tabs_error": str(exc)})
            continue
        rows.append({"id": len(rows), "browser": name, "window": title, "tabs": tabs})
    return rows


def select_tab(w, t):
    from pywinauto import Desktop

    desktop = Desktop(backend="uia")
    wins = _browser_windows(desktop)
    if not 0 <= w < len(wins):
        return {"ok": False, "error": "bad window index"}
    name, win = wins[w]
    tabs = win.descendants(control_type="TabItem")
    if not 0 <= t < len(tabs):
        return {"ok": False, "error": "bad tab index"}
    tab = tabs[t]
    try:
        tab.select()
        how = "select"
    except Exception:
        tab.click_input()
        how = "click"
    time.sleep(0.6)
    return {"ok": True, "how": how, "browser": name,
            "window": win.window_text(), "tab": tab.window_text()}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0].strip("/").lower()
        try:
            if path in ("", "health"):
                body = {"ok": True, "bridge": "dodaios-bridge", "time": int(time.time())}
            elif path in ("list", "now"):
                sessions, chosen, current_app = asyncio.run(snapshot())
                body = {"ok": True, "current_app": current_app, "chosen": chosen,
                        "sessions": sessions, "volume": volume(), "muted": muted()}
            elif path == "tabs":
                body = {"ok": True, "windows": list_tabs()}
            elif path == "volume":
                q = parse_qs(urlsplit(self.path).query)
                raw = (q.get("level") or [None])[0]
                level = None if raw is None else max(0, min(100, int(raw)))
                body = {"ok": True, "volume": volume(level)}
            elif path == "tab":
                q = parse_qs(urlsplit(self.path).query)
                w = int((q.get("w") or ["-1"])[0])
                t = int((q.get("t") or ["-1"])[0])
                body = select_tab(w, t)
            elif path == "cdp/list":
                body = cdp_list()
            elif path == "cdp/videos":
                body = cdp_videos()
            elif path == "cdp/search":
                q = (parse_qs(urlsplit(self.path).query).get("q") or [""])[0]
                page = (parse_qs(urlsplit(self.path).query).get("page") or ["1"])[0]
                body = cdp_search(q, page)
            elif path == "cdp/playlist":
                url = (parse_qs(urlsplit(self.path).query).get("url") or [""])[0]
                body = cdp_playlist(url)
            elif path == "cdp/open":
                bv = (parse_qs(urlsplit(self.path).query).get("bv") or [""])[0]
                body = cdp_open_bv(bv)
            elif path == "cdp/status":
                body = cdp_status()
            elif path == "cdp/seek":
                q = parse_qs(urlsplit(self.path).query)
                try:
                    pos = float((q.get("pos") or ["-1"])[0])
                except ValueError:
                    pos = -1
                if pos < 0 or pos > 86400:
                    body = {"ok": False, "error": "bad pos"}
                else:
                    body = cdp_seek(pos)
            elif path == "mute":
                body = set_mute(True)
            elif path == "cdp/pause":
                body = cdp_pause()
            elif path == "cdp/activate":
                q = parse_qs(urlsplit(self.path).query)
                body = cdp_activate((q.get("id") or [""])[0])
            elif path == "cdp/play":
                q = parse_qs(urlsplit(self.path).query)
                body = cdp_play((q.get("id") or [None])[0])
            elif path in ("pause", "play"):
                if cdp_targets():
                    body = cdp_pause() if path == "pause" else cdp_play(None)
                    body["backend"] = "cdp"
                else:
                    body = asyncio.run(act(path))
            elif path in ("toggle", "next", "prev"):
                body = asyncio.run(act(path))
            else:
                body = {
                    "ok": False,
                    "error": "unknown endpoint",
                    "endpoints": ["/health", "/list", "/now", "/tabs", "/tab?w=&t=",
                                  "/cdp/list", "/cdp/status", "/cdp/play?id=",
                                  "/cdp/pause", "/cdp/activate?id=",
                                  "/cdp/seek?pos=",
                                  "/cdp/videos", "/cdp/search?q=&page=",
                                  "/cdp/playlist?url=", "/cdp/open?bv=",
                                  "/volume?level=0-100", "/mute",
                                  "/pause", "/play", "/toggle", "/next", "/prev"],
                }
        except Exception as exc:
            log(traceback.format_exc())
            body = {"ok": False, "error": str(exc)}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200 if body.get("ok") else 500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        return


if __name__ == "__main__":
    log("bridge starting on 127.0.0.1:8765")
    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
