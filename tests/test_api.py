import asyncio
import json
import struct

import pytest
from starlette.testclient import TestClient

from avatar.api import Hub, create_app
from avatar.config import Settings
from tests.fakes import FakeBrain, FakeMouth, FakeWorker


@pytest.fixture
def client(monkeypatch):
    from avatar import models
    monkeypatch.setattr(models, "missing", lambda include_optional=True: [models.ASR])  # 测试里不加载模型
    s = Settings(); s.gap = 0.02; s.silence_nudge = 0.0
    hub = Hub(settings=s, brain=FakeBrain(["先做登录吧。", "[静]"]), mouth=FakeMouth(),
              worker=FakeWorker(), save_settings=False)
    with TestClient(create_app(hub)) as c:
        c.hub = hub
        yield c


def wait_for(client, cond, tries=200):
    import time
    for _ in range(tries):
        st = client.get("/api/state").json()
        if cond(st):
            return st
        time.sleep(0.01)
    raise AssertionError("等待超时")


def test_state_before_meeting(client):
    st = client.get("/api/state").json()
    assert st["status"] == "off" and st["settings"]["name"] == "小克"
    assert "local" in st["voices"] and st["task_kinds"] == ["查资料", "查代码", "做设计"]
    assert st["ready"] == {"ears": False, "speakers": False, "local_voice": False}


def test_actions_require_meeting(client):
    for path, body in [("/api/utterance", {"speaker": "张三", "text": "你好"}), ("/api/say", {"text": "你好"}),
                       ("/api/hush", None), ("/api/tasks", {"kind": "查资料", "brief": "飞书接口"})]:
        r = client.post(path, json=body)
        assert r.status_code == 409 and r.json()["detail"] == "会议还没开始"


def test_full_text_meeting(client):
    assert client.post("/api/meeting/start").json()["status"] == "listening"
    line = client.post("/api/utterance", json={"speaker": "张三", "text": "小克先做什么"}).json()
    assert line["speaker"] == "张三" and line["who"] == "human"
    st = wait_for(client, lambda s: len(s["lines"]) == 2 and s["status"] == "listening")
    assert st["lines"][1]["who"] == "bot" and st["lines"][1]["text"] == "先做登录吧。"
    assert st["speakers"] == [{"id": 1, "name": "张三", "count": 1}]

    assert client.patch("/api/speakers/1", json={"name": "张总"}).json()["name"] == "张总"
    assert client.patch("/api/speakers/9", json={"name": "x"}).status_code == 404
    assert client.get("/api/state").json()["lines"][0]["speaker"] == "张总"

    client.post("/api/say", json={"text": "大家好。"})
    wait_for(client, lambda s: len(s["lines"]) == 3 and s["status"] == "listening")

    assert client.post("/api/mute", json={"muted": True}).json() == {"muted": True}
    assert client.post("/api/hush").json() == {"ok": True}
    client.post("/api/mute", json={"muted": False})

    t = client.post("/api/tasks", json={"kind": "查资料", "brief": "飞书机器人接口"}).json()
    assert t["status"] == "running"
    assert client.post("/api/tasks", json={"kind": "唱歌", "brief": "来一首"}).status_code == 400
    st = wait_for(client, lambda s: s["tasks"][0]["status"] == "done")
    assert st["board_task_id"] == t["id"]
    assert client.post("/api/board", json={"task_id": None}).json() == {"task_id": None}
    assert client.post("/api/board", json={"task_id": 99}).status_code == 404

    assert client.post("/api/meeting/stop").json()["status"] == "off"


def test_settings_patch_validates(client):
    r = client.patch("/api/settings", json={"name": "阿克", "speed": 1.2})
    assert r.json()["changed"] == ["name", "speed"]
    bad = client.patch("/api/settings", json={"voice": "不存在"})
    assert bad.status_code == 400 and bad.json()["detail"] == "没有这个嗓音"
    assert client.patch("/api/settings", json={"project_dir": "/no/such"}).status_code == 400


def test_events_stream_starts_with_snapshot_then_lines(client):
    client.post("/api/meeting/start")
    with client.websocket_connect("/ws/events") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and first["state"]["status"] == "listening"
        client.post("/api/utterance", json={"speaker": "李四", "text": "人手够"})
        seen = []
        for _ in range(10):
            ev = ws.receive_json()
            seen.append(ev["type"])
            if ev["type"] == "line":
                assert ev["line"]["text"] == "人手够"
                break
        assert "line" in seen


def test_stage_receives_clip_and_reports_playback(client):
    client.post("/api/meeting/start")
    with client.websocket_connect("/ws/audio") as stage:
        wait_for(client, lambda s: s["stage_online"])
        client.post("/api/utterance", json={"speaker": "张三", "text": "小克先做什么"})
        frame = stage.receive_bytes()
        n = struct.unpack(">I", frame[:4])[0]
        head = json.loads(frame[4:4 + n])
        assert head["text"] == "先做登录吧。" and frame[4 + n:] == "先做登录吧。".encode()
        assert client.get("/api/state").json()["status"] == "speaking"
        stage.send_text(json.dumps({"type": "clip_started", "id": head["id"]}))
        wait_for(client, lambda s: len(s["lines"]) == 2)
        client.post("/api/hush")
        assert stage.receive_json() == {"type": "stop"}
    wait_for(client, lambda s: not s["stage_online"])
