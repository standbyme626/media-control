"""Windows 桥的纯函数测试（不依赖 Windows / 声卡 / CDP）。"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("BRIDGE_LOG", str(tmp_path / "bridge.log"))
    spec = importlib.util.spec_from_file_location("bridge_under_test", ROOT / "bridge" / "dodaios-bridge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_voice_parse_next(bridge):
    assert bridge.voice_parse("电脑下一首") == {"kind": "next"}
    assert bridge.voice_parse("换下一曲吧") is None        # 必须有「电脑」在前
    assert bridge.voice_parse("下一首") is None


def test_voice_parse_search(bridge):
    cmd = bridge.voice_parse("电脑放一首周杰伦的歌")
    assert cmd["kind"] == "search" and "周杰伦" in cmd["q"]
    cmd = bridge.voice_parse("电脑来一首陈奕迅")
    assert cmd["kind"] == "search" and cmd["q"] == "陈奕迅"
    assert bridge.voice_parse("电脑今天天气怎么样") is None   # 不含歌/音乐类词


def test_voice_parse_relaxed(bridge):
    cmd = bridge.voice_parse("周杰伦的歌", relaxed=True)
    assert cmd and cmd["kind"] == "search"


def test_clean_q_strips_fillers(bridge):
    # 尾部的「歌」由 _GENRE_TAIL 负责，_clean_q 只去前缀填充词
    assert bridge._clean_q("帮我播放周杰伦的歌") == "周杰伦的歌"
    assert bridge._clean_q("来一首张学友") == "张学友"


def test_vnorm_fixes_homophones(bridge):
    assert "周杰伦" in bridge._vnorm("周杰轮")
    assert bridge._vnorm("放，一 首 歌！") == "放一首歌"


def test_queue_set_dedup_and_clamp(bridge):
    r = bridge.queue_set([
        {"bv": "BV1aa411c7mD", "title": "a"},
        {"bv": "BV1aa411c7mD", "title": "a"},
        {"bv": "bad-bv", "title": "x"},
        {"bv": "BV1bb411c7mE", "title": "b", "p": 2},
    ], idx=99)
    assert r["n"] == 2 and r["idx"] == 1        # 去重 + idx 夹到末位
    st = bridge._STATE
    assert [v["bv"] for v in st["queue"]] == ["BV1aa411c7mD", "BV1bb411c7mE"]
    assert st["queue"][1]["p"] == 2


def test_state_saved_atomically(bridge, tmp_path):
    bridge.queue_set([{"bv": "BV1aa411c7mD", "title": "a", "p": 1}], idx=0)
    path = pathlib.Path(bridge._STATE_PATH)
    assert path.exists()
    assert not pathlib.Path(str(path) + ".tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["queue"][0]["bv"] == "BV1aa411c7mD"


def test_status_cache_ttl(bridge, monkeypatch):
    calls = {"n": 0}

    def fake_status():
        calls["n"] += 1
        return {"ok": True, "n": calls["n"]}

    monkeypatch.setattr(bridge, "cdp_status", fake_status)
    a = bridge.cdp_status_cached()
    b = bridge.cdp_status_cached()
    assert a is b and calls["n"] == 1
    bridge._status_dirty()
    c = bridge.cdp_status_cached()
    assert c["n"] == 2 and calls["n"] == 2


def test_queue_open_busy_guard(bridge, monkeypatch):
    bridge.queue_set([{"bv": "BV1aa411c7mD", "title": "a", "p": 1}], idx=0)
    monkeypatch.setattr(bridge, "cdp_open_bv", lambda bv, p=1: {"ok": True, "bv": bv})

    class BusyLock:
        def acquire(self, timeout=None):
            return False

        def release(self):
            pass

    monkeypatch.setattr(bridge, "_QLOCK", BusyLock())
    r = bridge.queue_open(0)
    assert r["ok"] is False and r["error"] == "queue busy"
