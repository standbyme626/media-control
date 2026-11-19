import asyncio
import collections
import ctypes
import json
import re
import threading
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, quote, urlsplit

LOG = r"C:\Users\kkk\dodaios-bridge.log"


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

VLOG = collections.deque(maxlen=40)
VST = {"on": True, "ready": False, "error": "", "device": None,
       "last_text": "", "last_cmd": None, "last_action": None,
       "last_ack": 0, "started": 0}
_VOICE_REC = None
_VLOCK = threading.Lock()
_VEXEC = threading.Lock()
_ACK_WAV = r"C:\Users\kkk\voice-ack-wozai.wav"
_VOICE_DIR = r"C:\Users\kkk\models\sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
_VAD_MODEL = r"C:\Users\kkk\models\silero_vad.onnx"

_STATE_PATH = r"C:\Users\kkk\media-state.json"
_STATE = {"rate": 1.0, "vvol": 100, "progress": {}, "queue": [], "idx": -1, "auto": True}


def _load_state():
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k in _STATE:
            if k in d:
                _STATE[k] = d[k]
    except Exception:
        pass


def _save_state():
    try:
        prog = _STATE.get("progress") or {}
        if len(prog) > 300:
            _STATE["progress"] = dict(sorted(prog.items(), key=lambda kv: kv[1])[-200:])
        with open(_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(_STATE, f, ensure_ascii=False)
    except Exception:
        pass


_load_state()


def vlog(ev, **kw):
    item = {"ts": int(time.time()), "ev": ev}
    item.update(kw)
    with _VLOCK:
        VLOG.append(item)
    return item


def _vnorm(t):
    t = re.sub(r"[\s，。！？、,.!?~～…]+", "", t or "")
    for a, b in (("周杰轮", "周杰伦"), ("一手", "一首"),
                 ("林俊节", "林俊杰"), ("陈亦迅", "陈奕迅")):
        t = t.replace(a, b)
    return t


_GENRE_TAIL = re.compile(r"([\u4e00-\u9fa5A-Za-z0-9·]{2,8}?)(?:的)?(?:歌曲|歌|音乐|曲)(?:吧|呢|啊|呗|$)")
_ACTION = re.compile(r"(?:换|播放|放|来一首|来|听|唱)(?:一首|首|点|一个|个)?(.+)")
_QFILL = ("播放", "帮我", "给我", "给我放", "来一首", "一首", "电脑", "小电脑",
          "我要", "我想", "你能", "能不能", "请", "麻烦", "帮我换", "帮我放",
          "换个", "放个", "点首", "点的", "不放", "不要放", "别放", "不要",
          "给我来", "你", "换", "放", "来", "听", "唱", "点", "首", "的",
          "歌曲", "歌", "音乐", "曲", "喂", "嗯")


def _clean_q(q):
    q = re.sub(r"[\s，。！？、,.!?~～…]+", "", q or "")
    changed = True
    while changed and q:
        changed = False
        for f in _QFILL:
            if q.startswith(f) and len(q) > len(f):
                q = q[len(f):]
                changed = True
    while q and q[-1] in "的了吧呢啊呗呀":
        q = q[:-1]
    return q


def voice_parse(text, relaxed=False):
    t = _vnorm(text)
    if not relaxed and "电脑" not in t:
        return None
    if re.search(r"(下一首|下一曲|换一首|切歌)", t):
        return {"kind": "next"}
    if not re.search(r"(歌|音乐|歌曲|曲|首)", t):
        return None
    q = ""
    m = _GENRE_TAIL.search(t)
    if m:
        q = _clean_q(m.group(1))
    if not q:
        m = _ACTION.search(t)
        if m:
            q = _clean_q(re.sub(r"(?:的)?(?:歌曲|歌|音乐|曲|吧|呢|啊|呗)$", "", m.group(1)))
    if not q:
        return None
    return {"kind": "search", "q": q}


def _sapi_say(text):
    try:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()
        v = win32com.client.Dispatch("SAPI.SpVoice")
        for x in v.GetVoices():
            if "Huihui" in x.GetDescription():
                v.Voice = x
                break
        v.Speak(text)
    except Exception as exc:
        log("sapi tts error: %r" % exc)


def voice_ack():
    def run():
        try:
            import wave
            import numpy as np
            import sounddevice as sd
            with wave.open(_ACK_WAV, "rb") as f:
                sr = f.getframerate()
                x = np.frombuffer(f.readframes(f.getnframes()),
                                  dtype=np.int16).astype("float32") / 32768.0
            sd.play(x, sr)
            sd.wait()
            VST["last_ack"] = int(time.time())
            vlog("ack", say="我在呢")
        except Exception as exc:
            vlog("ack_error", err=str(exc))
            _sapi_say("我在呢")
    threading.Thread(target=run, daemon=True).start()


def voice_exec_sync(cmd):
    if cmd["kind"] == "next":
        r = asyncio.run(act("next"))
        res = {"kind": "next", "ok": bool(r.get("ok")), "error": r.get("error")}
    else:
        q = cmd["q"]
        data = cdp_search(q, music=True, order="click")
        vids = data.get("videos") or []
        if not data.get("ok") or not vids:
            res = {"kind": "search", "q": q, "ok": False,
                   "error": data.get("error") or "no result"}
        else:
            queue_set(vids, 0)
            v = vids[0]
            r = cdp_open_bv(v.get("bv"))
            res = {"kind": "search", "q": q, "ok": bool(r.get("ok", True)),
                   "bv": v.get("bv"), "title": v.get("title")}
    VST["last_action"] = dict(res, ts=int(time.time()))
    vlog("action", **res)
    return res


def voice_exec(cmd, text):
    if not _VEXEC.acquire(blocking=False):
        vlog("busy", text=text)
        return
    try:
        voice_exec_sync(cmd)
    except Exception as exc:
        VST["last_action"] = {"kind": "error", "error": str(exc), "ts": int(time.time())}
        vlog("action_error", err=str(exc))
    finally:
        _VEXEC.release()


def voice_audio(raw, rate, dry=False):
    global _VOICE_REC
    import numpy as np
    if _VOICE_REC is None:
        return {"ok": False, "error": "asr not ready"}
    x = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
    if len(x) < 1600:
        return {"ok": False, "error": "too short"}
    if rate != 16000:
        n = max(1, int(len(x) * 16000.0 / rate))
        x = np.interp(np.linspace(0, len(x) - 1, n),
                      np.arange(len(x)), x).astype("float32")
    peak = float(np.max(np.abs(x)))
    if peak > 1e-4:
        x = x * (0.5 / peak)
    s = _VOICE_REC.create_stream()
    s.accept_waveform(16000, x)
    _VOICE_REC.decode_stream(s)
    text = (s.result.text or "").strip()
    VST["last_text"] = text
    cmd = voice_parse(text, relaxed=True)
    vlog("phone", text=text, cmd=bool(cmd), dur=round(len(x) / 16000, 2))
    action = None
    if cmd:
        VST["last_cmd"] = dict(cmd, text=text, ts=int(time.time()))
        if not dry:
            if _VEXEC.acquire(blocking=False):
                try:
                    action = voice_exec_sync(cmd)
                finally:
                    _VEXEC.release()
            else:
                action = {"ok": False, "error": "busy"}
    return {"ok": True, "text": text, "cmd": cmd, "action": action, "dry": dry}


def voice_handle(seg, rec):
    import numpy as np
    peak = float(np.max(np.abs(seg))) if len(seg) else 0.0
    if peak > 1e-4:
        seg = seg * (0.5 / peak)
    s = rec.create_stream()
    s.accept_waveform(16000, seg)
    rec.decode_stream(s)
    text = (s.result.text or "").strip()
    if not text:
        return
    VST["last_text"] = text
    vlog("heard", text=text, dur=round(len(seg) / 16000, 2), peak=round(peak, 4))
    if not VST["on"]:
        return
    plain = re.sub(r"[\s，。！？、,.!?]", "", text)
    if plain in ("电脑", "小电脑", "电脑在吗", "电脑你好", "电脑你在吗"):
        if time.time() - VST["last_ack"] > 5:
            voice_ack()
        return
    cmd = voice_parse(text)
    if cmd:
        VST["last_cmd"] = dict(cmd, text=text, ts=int(time.time()))
        vlog("cmd", text=text, kind=cmd["kind"], q=cmd.get("q"))
        threading.Thread(target=voice_exec, args=(cmd, text), daemon=True).start()


def voice_loop_vad():
    try:
        import numpy as np
        import sherpa_onnx
        import sounddevice as sd
    except Exception as exc:
        VST["error"] = "import: %s" % exc
        vlog("error", err=VST["error"])
        return
    global _VOICE_REC
    try:
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=_VOICE_DIR + r"\model.int8.onnx",
            tokens=_VOICE_DIR + r"\tokens.txt",
            num_threads=2, use_itn=True, debug=False)
        _VOICE_REC = rec
    except Exception as exc:
        VST["error"] = "model: %s" % exc
        vlog("error", err=VST["error"])
        return
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = _VAD_MODEL
    cfg.silero_vad.threshold = 0.5
    cfg.silero_vad.min_speech_duration = 0.35
    cfg.silero_vad.min_silence_duration = 0.6
    cfg.sample_rate = 16000
    dev = None
    try:
        for i, info in enumerate(sd.query_devices()):
            if "USB PnP" in info["name"] and info["hostapi"] == 0:
                dev = i
                break
    except Exception:
        pass
    VST.update({"ready": True, "device": dev, "started": int(time.time()), "error": ""})
    vlog("ready", device=dev)
    while True:
        try:
            vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
            with sd.RawInputStream(samplerate=16000, blocksize=1600, channels=1,
                                   dtype="int16", device=dev) as stream:
                vlog("mic_open", device=dev)
                while True:
                    data, _ = stream.read(1600)
                    x = np.frombuffer(data, dtype=np.int16).astype("float32") / 32768.0
                    vad.accept_waveform(x)
                    while not vad.empty():
                        seg = np.array(vad.front.samples, dtype="float32")
                        vad.pop()
                        try:
                            voice_handle(seg, rec)
                        except Exception as exc:
                            vlog("handle_error", err=str(exc))
        except Exception as exc:
            VST["error"] = str(exc)
            vlog("mic_error", err=str(exc))
            time.sleep(5)



_VOSK_DIR = r"C:\Users\kkk\models\vosk-model-small-cn-0.22"


def _duck(delta=-10):
    try:
        v = volume()
        volume(max(3, min(100, v + delta)))
        return v
    except Exception:
        return None


def voice_loop_kws():
    global _VOICE_REC
    import numpy as np
    import sherpa_onnx
    import sounddevice as sd
    import vosk
    try:
        rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=_VOICE_DIR + r"\model.int8.onnx",
            tokens=_VOICE_DIR + r"\tokens.txt",
            num_threads=2, use_itn=True, debug=False)
        _VOICE_REC = rec
        vmodel = vosk.Model(_VOSK_DIR)
        vrec = vosk.KaldiRecognizer(vmodel, 16000)
    except Exception as exc:
        VST["error"] = "kws model: %s" % exc
        vlog("kws_error", err=VST["error"])
        return
    dev = None
    try:
        for i, info in enumerate(sd.query_devices()):
            if "USB PnP" in info["name"] and info["hostapi"] == 0:
                dev = i
                break
    except Exception:
        pass
    VST.update({"ready": True, "device": dev, "started": int(time.time()),
                "error": "", "mode": "kws"})
    vlog("ready", device=dev, mode="kws")
    state = "wake"
    buf = []
    listen_until = 0.0
    last_wake = 0.0
    duck_prev = None
    while True:
        try:
            with sd.RawInputStream(samplerate=16000, blocksize=1600, channels=1,
                                   dtype="int16", device=dev) as stream:
                vlog("mic_open", device=dev, mode="kws")
                while True:
                    data, _ = stream.read(1600)
                    vrec.AcceptWaveform(bytes(data))
                    now = time.time()
                    if state == "wake":
                        part = (json.loads(vrec.PartialResult()).get("partial") or "").replace(" ", "")
                        if ("电脑" in part or "小电脑" in part) and now - last_wake > 8:
                            last_wake = now
                            state = "listen"
                            buf = []
                            listen_until = now + 6.0
                            vrec.Reset()
                            vlog("wake", text=part[-24:])
                            if time.time() - VST["last_ack"] > 3:
                                voice_ack()
                            duck_prev = _duck(-10)
                    else:
                        buf.append(np.frombuffer(data, dtype=np.int16).astype("float32") / 32768.0)
                        if now >= listen_until:
                            x = np.concatenate(buf) if buf else np.zeros(1, dtype="float32")
                            skip = int(16000 * 0.9)
                            if len(x) > skip + 6400:
                                x = x[skip:]
                            peak = float(np.max(np.abs(x))) if len(x) else 0.0
                            if peak > 1e-4:
                                x = x * (0.5 / peak)
                            try:
                                st = rec.create_stream()
                                st.accept_waveform(16000, x)
                                rec.decode_stream(st)
                                text = (st.result.text or "").strip()
                            except Exception as exc:
                                text = ""
                                vlog("asr_error", err=str(exc))
                            if text:
                                VST["last_text"] = text
                                vlog("heard", text=text, dur=round(len(x) / 16000, 2),
                                     peak=round(peak, 4), mode="kws")
                                cmd = voice_parse(text, relaxed=True)
                                if cmd:
                                    VST["last_cmd"] = dict(cmd, text=text, ts=int(time.time()))
                                    vlog("cmd", text=text, kind=cmd["kind"], q=cmd.get("q"))
                                    if VST["on"]:
                                        threading.Thread(target=voice_exec, args=(cmd, text), daemon=True).start()
                            if duck_prev is not None:
                                try:
                                    volume(duck_prev)
                                except Exception:
                                    pass
                                duck_prev = None
                            state = "wake"
                            vrec.Reset()
        except Exception as exc:
            VST["error"] = str(exc)
            vlog("mic_error", err=str(exc))
            time.sleep(5)


def voice_loop():
    import os
    if os.path.isdir(_VOSK_DIR):
        try:
            voice_loop_kws()
            return
        except Exception as exc:
            vlog("kws_error", err=str(exc))
    voice_loop_vad()


def _pick_video_target():
    targets = [t for t in cdp_targets() if "bilibili.com" in (t.get("url") or "")]
    if not targets:
        return None
    vids = [t for t in targets if "/video/" in (t.get("url") or "")]
    if vids:
        return next((t for t in vids if t["id"] == _LAST["id"]), vids[0])
    return next((t for t in targets if t["id"] == _LAST["id"]), targets[0])


def video_volume(level=None):
    t = _pick_video_target()
    if not t:
        return {"ok": False, "error": "no bilibili tab"}
    lv = None
    if level is not None:
        lv = max(0, min(100, int(level)))
        _STATE["vvol"] = lv
        _save_state()
    js = "(()=>{const v=document.querySelector('video');if(!v)return null;"
    if lv is not None:
        js += "v.volume=%s;" % (lv / 100.0)
    js += "return Math.round((v.volume||0)*100)})()"
    val = cdp_eval(t, js)
    return {"ok": val is not None, "vvol": val}


def set_rate(rate):
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad rate"}
    r = max(0.5, min(2.0, r))
    t = _pick_video_target()
    if not t:
        return {"ok": False, "error": "no bilibili tab"}
    val = cdp_eval(t, "(()=>{const v=document.querySelector('video');if(!v)return null;"
                      "v.playbackRate=%s;v.preservesPitch=true;return v.playbackRate})()" % r)
    if val is not None:
        _STATE["rate"] = r
        _save_state()
    return {"ok": val is not None, "rate": val}


def eq_set(on=True):
    t = _pick_video_target()
    if not t:
        return {"ok": False, "error": "no bilibili tab"}
    if on:
        js = ("(()=>{const v=document.querySelector('video');if(!v)return 'no video';"
              "if(!window.__mcCtx)window.__mcCtx=new (window.AudioContext||window.webkitAudioContext)();"
              "const ctx=window.__mcCtx;"
              "if(window.__mcEl!==v){if(window.__mcSrc){try{window.__mcSrc.disconnect()}catch(e){}}"
              "try{window.__mcSrc=ctx.createMediaElementSource(v)}catch(e){return 'src:'+e.message}"
              "const c=ctx.createDynamicsCompressor();"
              "c.threshold.value=-24;c.knee.value=24;c.ratio.value=4;c.attack.value=0.005;c.release.value=0.25;"
              "const g=ctx.createGain();g.gain.value=1.0;"
              "window.__mcSrc.connect(c);c.connect(g);g.connect(ctx.destination);"
              "window.__mcComp=c;window.__mcEl=v;}"
              "if(ctx.state==='suspended')ctx.resume();"
              "return ctx.state;})()")
    else:
        js = ("(()=>{if(window.__mcComp){window.__mcComp.threshold.value=0;"
              "window.__mcComp.ratio.value=1;}return 'off'})()")
    val = cdp_eval(t, js, gesture=True)
    ok = not (isinstance(val, str) and val.startswith(("src:", "no video")))
    return {"ok": ok, "eq": bool(on), "result": val}

def view_mode(mode):
    t = _pick_video_target()
    if not t:
        return {"ok": False, "error": "no bilibili tab"}
    if mode == "web":
        js = ("(()=>{const c=document.querySelector('.bpx-player-container');"
              "const on=c&&String(c.className).indexOf('bpx-state-web-fullscreen')>=0;"
              "const b=document.querySelector('.bpx-player-ctrl-web');"
              "if(b){b.click();return on?'off':'on';}return 'no'})()")
    else:
        js = ("(()=>{const c=document.querySelector('.bpx-player-container');"
              "const on=c&&String(c.className).indexOf('bpx-state-web-fullscreen')>=0;"
              "if(on){const b=document.querySelector('.bpx-player-ctrl-web');if(b){b.click();return 'off';}}"
              "return 'normal'})()")
    return {"ok": True, "mode": mode, "result": cdp_eval(t, js, gesture=True)}


def danmaku_set(on=None):
    t = _pick_video_target()
    if not t:
        return {"ok": False, "error": "no bilibili tab"}
    js = "(()=>{const i=document.querySelector('.bpx-player-dm-switch input');"
    if on is not None:
        js += "if(i){const want=%s;if(i.checked!==want){i.click();}return i.checked;}" % ("true" if int(on) else "false")
        js += "const b=document.querySelector('.bpx-player-dm-switch');if(b){b.click();return null;}"
    else:
        js += "return i?i.checked:null;"
    js += "return null})()"
    val = cdp_eval(t, js, gesture=True)
    return {"ok": True, "danmaku": val}


def queue_set(videos, idx=0):
    q = []
    seen = set()
    for v in videos or []:
        bv = (v or {}).get("bv")
        try:
            p = int((v or {}).get("p") or 1)
        except (TypeError, ValueError):
            p = 1
        if bv and re.fullmatch(r"BV[0-9A-Za-z]+", bv) and (bv, p) not in seen:
            seen.add((bv, p))
            q.append({"bv": bv, "p": p, "title": ((v.get("title") or "")[:120])})
    _STATE["queue"] = q
    _STATE["idx"] = max(0, min(int(idx or 0), len(q) - 1)) if q else -1
    _save_state()
    return {"ok": True, "n": len(q), "idx": _STATE["idx"]}


def queue_open(i):
    q = _STATE.get("queue") or []
    if not q:
        return {"ok": False, "error": "empty queue"}
    i = max(0, min(int(i), len(q) - 1))
    _STATE["idx"] = i
    _save_state()
    v = q[i]
    r = cdp_open_bv(v["bv"], v.get("p", 1))
    return {"ok": bool(r.get("ok", True)), "idx": i, "n": len(q),
            "bv": v["bv"], "p": v.get("p", 1), "title": v.get("title")}


def queue_next():
    q = _STATE.get("queue") or []
    if not q:
        return {"ok": False, "error": "empty queue"}
    i = int(_STATE.get("idx", -1)) + 1
    if i >= len(q):
        if _STATE.get("auto"):
            try:
                queue_fill()
            except Exception:
                pass
            q = _STATE.get("queue") or []
        if i >= len(q):
            return {"ok": False, "error": "queue end", "idx": _STATE.get("idx"), "n": len(q)}
    return queue_open(i)


def queue_prev():
    q = _STATE.get("queue") or []
    if not q:
        return {"ok": False, "error": "empty queue"}
    i = int(_STATE.get("idx", 0)) - 1
    if i < 0:
        return {"ok": False, "error": "queue head", "idx": 0, "n": len(q)}
    return queue_open(i)


def bili_fetch(url):
    t = next((x for x in cdp_targets() if "bilibili.com" in (x.get("url") or "")), None)
    if not t:
        return {"ok": False, "error": "no bilibili tab"}
    expr = ("(async()=>{const r=await fetch(%s,{credentials:'include'});"
            "return await r.json()})()" % json.dumps(url))
    data = cdp_eval(t, expr, await_promise=True)
    return data if isinstance(data, dict) else {"ok": False, "error": "bad response"}


def bili_watchlater():
    d = bili_fetch("https://api.bilibili.com/x/v2/history/toview")
    items = ((d.get("data") or {}).get("list") or []) if d.get("code") == 0 else []
    vids = [{"bv": it.get("bvid"), "title": it.get("title") or "",
             "duration": it.get("duration"), "author": (it.get("owner") or {}).get("name") or ""}
            for it in items if it.get("bvid")]
    return {"ok": d.get("code") == 0, "videos": vids,
            "error": d.get("message") if d.get("code") != 0 else None}


def bili_history():
    d = bili_fetch("https://api.bilibili.com/x/web-interface/history/cursor?ps=20&type=archive")
    items = ((d.get("data") or {}).get("list") or []) if d.get("code") == 0 else []
    vids = []
    for it in items:
        bv = ((it.get("history") or {}).get("bvid")) or ""
        if bv:
            vids.append({"bv": bv, "title": it.get("title") or "",
                         "duration": it.get("duration"), "author": it.get("author_name") or ""})
    return {"ok": d.get("code") == 0, "videos": vids,
            "error": d.get("message") if d.get("code") != 0 else None}


def bili_favs():
    nav = bili_fetch("https://api.bilibili.com/x/web-interface/nav")
    mid = ((nav.get("data") or {}).get("mid")) if nav.get("code") == 0 else None
    if not mid:
        return {"ok": False, "error": "not logged in"}
    d = bili_fetch("https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid=%s" % mid)
    items = ((d.get("data") or {}).get("list") or []) if d.get("code") == 0 else []
    return {"ok": d.get("code") == 0,
            "folders": [{"id": x.get("id"), "title": x.get("title") or "",
                         "count": x.get("media_count")} for x in items],
            "error": d.get("message") if d.get("code") != 0 else None}


def bili_fav(media_id, page=1):
    d = bili_fetch("https://api.bilibili.com/x/v3/fav/resource/list?media_id=%s&pn=%d&ps=20&platform=web"
                   % (media_id, max(1, int(page))))
    items = ((d.get("data") or {}).get("medias") or []) if d.get("code") == 0 else []
    vids = [{"bv": it.get("bvid"), "title": it.get("title") or "",
             "duration": it.get("duration"), "author": (it.get("upper") or {}).get("name") or ""}
            for it in items if it.get("bvid")]
    return {"ok": d.get("code") == 0, "videos": vids,
            "error": d.get("message") if d.get("code") != 0 else None}


def queue_add(videos):
    q = _STATE.setdefault("queue", [])
    seen = {(v.get("bv"), int(v.get("p") or 1)) for v in q}
    added = 0
    for v in videos or []:
        bv = (v or {}).get("bv")
        try:
            p = int((v or {}).get("p") or 1)
        except (TypeError, ValueError):
            p = 1
        if not bv or not re.fullmatch(r"BV[0-9A-Za-z]+", bv) or (bv, p) in seen:
            continue
        seen.add((bv, p))
        q.append({"bv": bv, "p": p, "title": ((v.get("title") or "")[:120])})
        added += 1
    _save_state()
    return {"ok": True, "added": added, "n": len(q)}


def queue_del(i):
    q = _STATE.get("queue") or []
    try:
        i = int(i)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad index"}
    if i < 0 or i >= len(q):
        return {"ok": False, "error": "bad index", "n": len(q)}
    q.pop(i)
    idx = int(_STATE.get("idx", -1))
    if i < idx:
        idx -= 1
    elif i == idx:
        idx = min(idx, len(q) - 1)
    _STATE["idx"] = idx
    _save_state()
    return {"ok": True, "n": len(q), "idx": idx}


def queue_fill():
    t = _pick_video_target()
    vids = []
    if t:
        try:
            vids = cdp_eval(t, "(()=>{const d=window.__INITIAL_STATE__;const r=d&&d.related;"
                              "if(!r)return [];return r.map(x=>({bv:x.bvid,title:x.title||''})).filter(x=>x.bv).slice(0,30)})()") or []
        except Exception:
            vids = []
    if not vids:
        q = _STATE.get("queue") or []
        idx = int(_STATE.get("idx", -1))
        cur = q[idx].get("bv") if 0 <= idx < len(q) else ""
        if not cur and t:
            m = re.search(r"/video/(BV[0-9A-Za-z]+)", t.get("url") or "")
            cur = m.group(1) if m else ""
        if cur:
            d = bili_fetch("https://api.bilibili.com/x/web-interface/archive/related?bvid=" + cur)
            vids = [{"bv": x.get("bvid"), "title": x.get("title") or ""}
                    for x in (d.get("data") or []) if x.get("bvid")]
    return queue_add(vids)


def queue_collection():
    t = _pick_video_target()
    if not t:
        return {"ok": False, "error": "no bilibili tab"}
    js = ("(()=>{const d=window.__INITIAL_STATE__;const vd=d&&d.videoData;if(!vd)return null;"
          "const out=[];"
          "if(vd.ugc_season&&vd.ugc_season.sections){for(const s of vd.ugc_season.sections){"
          "for(const e of (s.episodes||[])){out.push({bv:e.bvid,title:e.title||'',p:1});}}}"
          "if(out.length<2&&vd.pages&&vd.pages.length>1){out.length=0;"
          "for(const pg of vd.pages){out.push({bv:vd.bvid,title:(vd.title||'')+' P'+pg.page,p:pg.page});}}"
          "if(!out.length)out.push({bv:vd.bvid,title:vd.title||'',p:1});"
          "return {videos:out,bv:vd.bvid,p:d.p||1};})()")
    data = cdp_eval(t, js)
    if not isinstance(data, dict) or not data.get("videos"):
        return {"ok": False, "error": "no collection"}
    vids = data["videos"]
    cur_bv, cur_p = data.get("bv"), int(data.get("p") or 1)
    idx = 0
    for i, v in enumerate(vids):
        if v.get("bv") == cur_bv and int(v.get("p") or 1) == cur_p:
            idx = i
            break
    queue_set(vids, idx)
    return {"ok": True, "n": len(vids), "idx": idx, "title": vids[idx].get("title")}


_AUTO = {"near_ts": 0.0, "cooldown": 0.0}


def queue_monitor():
    last_save = 0
    while True:
        time.sleep(2)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8765/cdp/status", timeout=6) as r:
                st = json.loads(r.read().decode("utf-8"))
            if not st.get("ok"):
                continue
            url = st.get("url") or ""
            m = re.search(r"/video/(BV[0-9A-Za-z]+)", url)
            pm = re.search(r"[?&]p=(\d+)", url)
            page = int(pm.group(1)) if pm else 1
            pos = st.get("t") or 0
            dur = st.get("dur") or 0
            if m and pos > 5:
                key = m.group(1) if page <= 1 else "%s#%d" % (m.group(1), page)
                _STATE.setdefault("progress", {})[key] = int(pos)
                if time.time() - last_save > 15:
                    _save_state()
                    last_save = time.time()
            q = _STATE.get("queue") or []
            idx = int(_STATE.get("idx", -1))
            if q and _STATE.get("auto") and 0 <= idx < len(q) and m:
                cur = m.group(1)
                on_cur = (cur == q[idx].get("bv") and page == int(q[idx].get("p") or 1))
                if time.time() < _AUTO.get("cooldown", 0):
                    continue
                if on_cur and dur and pos:
                    if dur - pos <= max(10, dur * 0.02):
                        _AUTO["near_ts"] = time.time()
                    if dur - pos <= 2:
                        time.sleep(1)
                        _AUTO["cooldown"] = time.time() + 10
                        _AUTO["near_ts"] = 0.0
                        urllib.request.urlopen("http://127.0.0.1:8765/queue/next", timeout=25).read()
                elif (not on_cur) and time.time() - _AUTO.get("near_ts", 0) < 30:
                    _AUTO["near_ts"] = 0.0
                    _AUTO["cooldown"] = time.time() + 10
                    time.sleep(1)
                    urllib.request.urlopen("http://127.0.0.1:8765/queue/next", timeout=25).read()
        except Exception:
            pass


VIDEO_INFO = ("(()=>{const v=document.querySelector('video');"
              "const i=document.querySelector('.bpx-player-dm-switch input');"
              "return {title:document.title,url:location.href,"
              "paused:v?v.paused:null,t:v?Math.round(v.currentTime):null,"
              "dur:v?Math.round(v.duration):null,dm:i?i.checked:null,"
              "vvol:v?Math.round((v.volume||0)*100):null,rate:v?v.playbackRate:null,"
              "eq:!!(window.__mcComp&&window.__mcComp.ratio&&window.__mcComp.ratio.value>1)}})()")


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


def cdp_eval(target, expr, gesture=False, await_promise=False):
    import websocket

    params = {"expression": expr, "userGesture": gesture, "returnByValue": True}
    if await_promise:
        params["awaitPromise"] = True
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=6)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": params}))
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


def cdp_search(q, page=1, music=False, order=""):
    if not q:
        return {"ok": False, "error": "empty query"}
    url = SEARCH_URL + quote(q)
    try:
        page = max(1, int(page))
    except (TypeError, ValueError):
        page = 1
    if music:
        url += "&tids_1=3"
    if order in ("click", "pubdate", "dm", "stow"):
        url += "&order=" + order
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


def cdp_open_bv(bv, p=1):
    if not re.fullmatch(r"BV[0-9A-Za-z]+", bv or ""):
        return {"ok": False, "error": "bad bv"}
    try:
        p = int(p)
    except (TypeError, ValueError):
        p = 1
    url = "https://www.bilibili.com/video/%s/" % bv + ("?p=%d" % p if p > 1 else "")
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
        key = bv if p <= 1 else "%s#%d" % (bv, p)
        pos = (_STATE.get("progress") or {}).get(key) or 0
        rate = float(_STATE.get("rate") or 1.0)
        vv = max(0, min(100, int(_STATE.get("vvol") or 100))) / 100.0
        js = ("(()=>{const v=document.querySelector('video');if(!v)return 0;v.play();"
              "v.playbackRate=%s;v.preservesPitch=true;v.volume=%s;" % (rate, vv))
        if pos > 5:
            js += "v.currentTime=%s;" % float(pos)
        js += "return 1})()"
        cdp_eval(tab, js, gesture=True)
    except Exception:
        pass
    return {"ok": True, "id": _PLAYER["id"], "url": url, "p": p}


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
            "muted": muted(), "dm": chosen.get("dm"), "eq": chosen.get("eq"),
            "vvol": int(chosen.get("vvol") if chosen.get("vvol") is not None else (_STATE.get("vvol") or 100)),
            "rate": float(chosen.get("rate") if chosen.get("rate") is not None else (_STATE.get("rate") or 1.0)),
            "queue": {"n": len(_STATE.get("queue") or []),
                      "idx": int(_STATE.get("idx", -1)),
                      "next": ((_STATE.get("queue") or [])[int(_STATE.get("idx", -1)) + 1].get("title")
                               if 0 <= int(_STATE.get("idx", -1)) + 1 < len(_STATE.get("queue") or []) else ""),
                      "auto": bool(_STATE.get("auto"))}}


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
                qs = parse_qs(urlsplit(self.path).query)
                q = (qs.get("q") or [""])[0]
                page = (qs.get("page") or ["1"])[0]
                music = (qs.get("music") or ["0"])[0] in ("1", "true")
                order = (qs.get("order") or [""])[0]
                body = cdp_search(q, page, music=music, order=order)
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
            elif path == "cdp/volume":
                q = parse_qs(urlsplit(self.path).query)
                raw = (q.get("level") or [None])[0]
                body = video_volume(None if raw is None else max(0, min(100, int(raw))))
            elif path == "cdp/speed":
                q = parse_qs(urlsplit(self.path).query)
                body = set_rate((q.get("rate") or ["1"])[0])
            elif path == "cdp/view":
                q = parse_qs(urlsplit(self.path).query)
                body = view_mode((q.get("mode") or ["web"])[0])
            elif path == "cdp/eq":
                q = parse_qs(urlsplit(self.path).query)
                body = eq_set((q.get("on") or ["1"])[0] not in ("0", "false", "off"))
            elif path == "cdp/danmaku":
                q = parse_qs(urlsplit(self.path).query)
                raw = (q.get("on") or [None])[0]
                body = danmaku_set(None if raw is None else (raw not in ("0", "false", "off")))
            elif path == "queue/status":
                body = {"ok": True, "n": len(_STATE.get("queue") or []),
                        "idx": int(_STATE.get("idx", -1)),
                        "auto": bool(_STATE.get("auto")),
                        "queue": [{"bv": v.get("bv"), "title": v.get("title")}
                                  for v in (_STATE.get("queue") or [])][:60]}
            elif path == "queue/set":
                q = parse_qs(urlsplit(self.path).query)
                try:
                    videos = json.loads((q.get("videos") or ["[]"])[0])
                except Exception:
                    videos = []
                try:
                    idx = int((q.get("idx") or ["0"])[0])
                except ValueError:
                    idx = 0
                body = queue_set(videos, idx)
            elif path == "queue/add":
                q = parse_qs(urlsplit(self.path).query)
                try:
                    videos = json.loads((q.get("videos") or ["[]"])[0])
                except Exception:
                    videos = []
                body = queue_add(videos)
            elif path == "queue/del":
                q = parse_qs(urlsplit(self.path).query)
                body = queue_del((q.get("i") or ["-1"])[0])
            elif path == "queue/fill":
                body = queue_fill()
            elif path == "queue/collection":
                body = queue_collection()
            elif path == "queue/next":
                body = queue_next()
            elif path == "queue/prev":
                body = queue_prev()
            elif path == "queue/auto":
                q = parse_qs(urlsplit(self.path).query)
                _STATE["auto"] = (q.get("on") or ["1"])[0] not in ("0", "false", "off")
                _save_state()
                body = {"ok": True, "auto": _STATE["auto"]}
            elif path == "bili/watchlater":
                body = bili_watchlater()
            elif path == "bili/history":
                body = bili_history()
            elif path == "bili/favs":
                body = bili_favs()
            elif path == "bili/fav":
                q = parse_qs(urlsplit(self.path).query)
                try:
                    media_id = int((q.get("id") or ["0"])[0])
                    page = int((q.get("page") or ["1"])[0])
                except ValueError:
                    media_id, page = 0, 1
                body = {"ok": False, "error": "bad id"} if media_id <= 0 else bili_fav(media_id, page)
            elif path == "voice/status":
                body = {"ok": True, "voice": dict(VST, events=list(VLOG))}
            elif path == "voice":
                q = parse_qs(urlsplit(self.path).query)
                on = (q.get("on") or ["1"])[0]
                VST["on"] = on not in ("0", "false", "off")
                body = {"ok": True, "on": VST["on"]}
            elif path == "voice/ack":
                voice_ack()
                body = {"ok": True}
            elif path == "voice/test":
                q = parse_qs(urlsplit(self.path).query)
                text = (q.get("q") or [""])[0].strip()
                dry = (q.get("dry") or ["0"])[0] in ("1", "true")
                relaxed = (q.get("relaxed") or ["0"])[0] in ("1", "true")
                cmd = voice_parse(text, relaxed=relaxed)
                VST["last_text"] = text
                vlog("test", text=text, cmd=bool(cmd), dry=dry)
                if not cmd:
                    body = {"ok": False, "error": "not a command", "text": text}
                elif dry:
                    body = {"ok": True, "cmd": cmd, "dry": True}
                else:
                    VST["last_cmd"] = dict(cmd, text=text, ts=int(time.time()))
                    threading.Thread(target=voice_exec, args=(cmd, text), daemon=True).start()
                    body = {"ok": True, "cmd": cmd}
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
                                  "/voice/status", "/voice?on=0|1", "/voice/ack", "/voice/test?q=&dry=",
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

    def _json(self, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200 if body.get("ok") else 500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = self.path.split("?", 1)[0].strip("/").lower()
        if path != "voice/audio":
            self._json({"ok": False, "error": "unknown endpoint"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > 3 * 1024 * 1024:
            self._json({"ok": False, "error": "bad body"})
            return
        raw = self.rfile.read(length)
        q = parse_qs(urlsplit(self.path).query)
        try:
            rate = int((q.get("rate") or ["16000"])[0])
        except ValueError:
            rate = 16000
        if rate < 8000 or rate > 96000:
            self._json({"ok": False, "error": "bad rate"})
            return
        dry = (q.get("dry") or ["0"])[0] in ("1", "true")
        try:
            body = voice_audio(raw, rate, dry)
        except Exception as exc:
            log(traceback.format_exc())
            body = {"ok": False, "error": str(exc)}
        self._json(body)

    def log_message(self, *args):
        return


if __name__ == "__main__":
    log("bridge starting on 127.0.0.1:8765")
    try:
        threading.Thread(target=voice_loop, daemon=True).start()
    except Exception:
        log("voice start error: " + traceback.format_exc())
    try:
        threading.Thread(target=queue_monitor, daemon=True).start()
    except Exception:
        log("queue monitor error: " + traceback.format_exc())
    HTTPServer(("127.0.0.1", 8765), Handler).serve_forever()
