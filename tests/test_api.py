import asyncio
import json
import struct
import time

import pytest
from starlette.testclient import TestClient

from avatar.api import Hub, create_app
from avatar.archive import Archive
from avatar.config import Settings
from tests.fakes import FakeBrain, FakeMouth, FakeWorker


@pytest.fixture
def client(monkeypatch, tmp_path):
    from avatar import models
    monkeypatch.setattr(models, "missing", lambda include_optional=True: [models.ASR])  # 测试里不加载模型
    s = Settings(); s.gap = 0.02; s.silence_nudge = 0.0
    hub = Hub(settings=s, brain=FakeBrain(["先做登录吧。", "[静]"]), mouth=FakeMouth(),
              worker=FakeWorker(), save_settings=False, archive=Archive(tmp_path))
    hub.meeting.save_delay = 0.01
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
    assert "local" in st["voices"] and st["task_kinds"] == ["查资料", "查代码", "查会议", "做设计"]
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


def test_state_says_so_when_the_microphone_goes_quiet(client, monkeypatch):
    """耳机断开这类情况：窗口还连着，麦克风的声音却不来了。状态要如实说，不能照旧显示在听。"""
    from avatar import api
    monkeypatch.setattr(api, "MIC_SILENT_AFTER", 0.1)
    hub = client.hub
    client.post("/api/meeting/start")
    assert client.get("/api/state").json()["mic_online"] is False
    with client.websocket_connect("/ws/audio") as stage:
        stage.send_bytes(b"\0" * 1280)
        wait_for(client, lambda s: s["stage_online"] and s["mic_online"])
        hub.ready["ears"] = True
        hub.ears._speaking = True                 # 话说到一半麦克风断了
        hub.floor.on_speech_start()
        st = wait_for(client, lambda s: not s["mic_online"])
        assert st["stage_online"] and not st["humans_speaking"]      # 不能一直以为有人在说，那样它永远不开口
        stage.send_bytes(b"\0" * 1280)            # 声音回来了，马上恢复
        wait_for(client, lambda s: s["mic_online"])
    wait_for(client, lambda s: not s["mic_online"])


def test_meeting_records_survive_the_meeting(client):
    assert client.get("/api/meetings").json() == [] and client.get("/api/state").json()["meetings"] == []
    assert client.get("/api/meetings/latest").status_code == 404

    client.post("/api/meeting/start")
    client.post("/api/utterance", json={"speaker": "张三", "text": "小克先做什么"})
    st = wait_for(client, lambda s: len(s["lines"]) == 2 and s["status"] == "listening")
    live = client.get(f"/api/meetings/{st['record_id']}")          # 会议进行中读到的也是最新的
    assert live.headers["content-type"].startswith("text/plain")
    assert "张三：小克先做什么" in live.text and "小克：先做登录吧。" in live.text
    client.post("/api/meeting/stop")

    client.post("/api/meeting/start")                               # 下一场会开始，上一场还在
    client.post("/api/utterance", json={"speaker": "李四", "text": "人手够"})
    client.post("/api/meeting/stop")

    newest, oldest = client.get("/api/meetings").json()
    assert oldest["id"] == st["record_id"] and oldest["lines"] == 2 and oldest["speakers"] == ["张三"]
    assert newest["speakers"] == ["李四"]
    assert client.get("/api/state").json()["meetings"] == [newest, oldest]
    assert "张三：小克先做什么" in client.get(f"/api/meetings/{oldest['id']}").text
    assert "李四：人手够" in client.get("/api/meetings/latest").text
    r = client.get("/api/meetings/2020-01-01_000000")
    assert r.status_code == 404 and r.json()["detail"] == "没有这场会议的记录"


def test_identity_role_and_engagement_are_settings(client):
    st = client.get("/api/state").json()
    assert st["roles"] == {"companion": "陪我开会", "delegate": "替我开会"}
    assert list(st["engagements"]) == ["quiet", "normal", "active"]
    assert st["settings"]["engagement"] == "normal" and st["settings"]["role"] == "companion"
    r = client.patch("/api/settings", json={"owner": "老方", "role": "delegate", "engagement": "quiet"})
    assert r.json()["changed"] == ["owner", "role", "engagement"]
    assert client.patch("/api/settings", json={"engagement": "吵"}).status_code == 400
    assert client.patch("/api/settings", json={"role": "boss"}).json()["detail"] == "没有这种角色"


def test_changing_engagement_mid_meeting_reaches_the_brain(client):
    client.post("/api/meeting/start")
    client.patch("/api/settings", json={"engagement": "quiet"})
    client.post("/api/utterance", json={"speaker": "张三", "text": "小克先做什么"})
    wait_for(client, lambda s: len(s["lines"]) == 2 and s["status"] == "listening")
    assert "参与度调成了：少说" in client.hub.brain.prompts[-1]


def test_it_knows_the_projects_next_to_the_main_one(client, tmp_path):
    from avatar import knowledge
    knowledge.forget()
    for name, doc in [("shop", "# 商城\n\n卖东西的网站。"), ("erp", "# erp\n\n进销存。")]:
        (tmp_path / "work" / name).mkdir(parents=True)
        (tmp_path / "work" / name / "README.md").write_text(doc, encoding="utf-8")
    (tmp_path / "work" / "照片").mkdir()
    assert client.get("/api/state").json()["projects"] == {"root": "", "names": []}
    with client.websocket_connect("/ws/events") as ws:
        ws.receive_json()
        client.patch("/api/settings", json={"project_dir": str(tmp_path / "work" / "shop")})
        ev = ws.receive_json()
        while ev["type"] != "projects":           # 认项目要读盘，在后台做，做完发事件
            ev = ws.receive_json()
        assert sorted(ev["projects"]["names"]) == ["erp", "shop"]
    st = client.get("/api/state").json()
    assert st["projects"]["root"] == str(tmp_path / "work")
    briefing = client.hub.briefing()
    assert "这场会主要讨论「shop」" in briefing and "- shop：商城：卖东西的网站。" in briefing and "- erp：进销存。" in briefing
    assert "照片" not in briefing


def test_minutes_are_written_after_a_real_meeting_and_shown_to_the_next_one(client):
    client.hub.brain.replies = ["[静]"] * 20
    client.post("/api/meeting/start")
    for i in range(5):
        client.post("/api/utterance", json={"speaker": "张三", "text": f"第{i}点我们先做登录"})
    st = wait_for(client, lambda s: len(s["lines"]) == 5)
    client.post("/api/meeting/stop")
    row = wait_for(client, lambda s: s["meetings"] and s["meetings"][0]["minutes"] == "yes")["meetings"][0]
    assert row["id"] == st["record_id"] and row["title"] == "登录排期"
    assert "张三：第0点我们先做登录" in client.hub.worker.summarized[0]
    text = client.get("/api/meetings/latest").text
    assert text.index("## 纪要") < text.index("### 结论") < text.index("## 对话")
    client.patch("/api/speakers/1", json={"name": "张总"})        # 会后改名重存，纪要还在
    wait_for(client, lambda s: "张总" in s["meetings"][0]["speakers"])
    assert "- 先做登录" in client.get("/api/meetings/latest").text
    assert "「登录排期」" in client.hub.briefing() and "- 先做登录" in client.hub.briefing()


def test_the_polished_transcript_arrives_after_the_minutes(client):
    client.hub.brain.replies = ["[静]"] * 20
    client.hub.worker.polished = "[10:00:00] 张三：我们先做登录。"
    client.hub.worker.polish_delay = 0.3            # 整理稿比纪要慢：纪要先出来，不等它
    client.post("/api/meeting/start")
    for i in range(5):
        client.post("/api/utterance", json={"speaker": "张三", "text": f"那个第{i}点就是我们先做登录"})
    wait_for(client, lambda s: len(s["lines"]) == 5)
    client.post("/api/meeting/stop")
    wait_for(client, lambda s: s["meetings"] and s["meetings"][0]["minutes"] == "yes")
    assert "## 整理稿" not in client.get("/api/meetings/latest").text
    deadline = time.time() + 3
    while "## 整理稿" not in client.get("/api/meetings/latest").text and time.time() < deadline:
        time.sleep(0.05)
    text = client.get("/api/meetings/latest").text
    assert text.index("## 纪要") < text.index("## 整理稿") < text.index("张三：我们先做登录。") < text.index("## 对话")
    assert "- 先做登录" in text and not client.hub.minuting and not client.hub.polishing


def test_no_polished_transcript_when_there_is_nothing_worth_minutes(client):
    client.hub.brain.replies = ["[静]"] * 20
    client.hub.worker.minutes = "无实质内容"
    client.hub.worker.polished = "[10:00:00] 张三：电视里的声音。"
    client.post("/api/meeting/start")
    for i in range(5):
        client.post("/api/utterance", json={"speaker": "张三", "text": f"电视里的声音{i}"})
    wait_for(client, lambda s: len(s["lines"]) == 5)
    client.post("/api/meeting/stop")
    wait_for(client, lambda s: s["meetings"] and s["meetings"][0]["minutes"] == "none")
    wait_for(client, lambda s: not client.hub.minuting and not client.hub.polishing)
    assert "## 整理稿" not in client.get("/api/meetings/latest").text


def test_short_meetings_get_minutes_only_on_request(client):
    client.post("/api/meeting/start")
    client.post("/api/utterance", json={"speaker": "张三", "text": "小克先做什么"})
    st = wait_for(client, lambda s: len(s["lines"]) == 2 and s["status"] == "listening")
    assert client.post(f"/api/meetings/{st['record_id']}/minutes").status_code == 409     # 还没结束
    client.post("/api/meeting/stop")
    assert client.hub.worker.summarized == [] and client.get("/api/meetings").json()[0]["minutes"] == ""
    r = client.post("/api/meetings/latest/minutes")
    assert r.status_code == 202 and r.json() == {"id": st["record_id"], "state": "running"}
    wait_for(client, lambda s: s["meetings"][0]["minutes"] == "yes" and not s["minuting"])
    assert client.post("/api/meetings/2020-01-01_000000/minutes").status_code == 404


def test_meeting_with_nothing_in_it_is_marked_empty(client):
    client.hub.worker.minutes = "无实质内容"
    client.post("/api/meeting/start")
    client.post("/api/utterance", json={"speaker": "电视", "text": "贵妃娘娘驾到"})
    client.post("/api/meeting/stop")
    client.post("/api/meetings/latest/minutes")
    row = wait_for(client, lambda s: s["meetings"][0]["minutes"] == "none")["meetings"][0]
    assert row["title"] == "无实质内容" and "## 纪要" not in client.get("/api/meetings/latest").text


def test_pages_and_scripts_are_never_served_stale(client):
    for path in ["/", "/stage", "/panel", "/static/panel.js", "/static/stage.js", "/static/api.js", "/static/panel.css"]:
        r = client.get(path)
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache", path
    etag = client.get("/static/panel.js").headers["etag"]
    again = client.get("/static/panel.js", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.headers["cache-control"] == "no-cache"


def test_briefing_carries_the_owners_rules_and_project_memory(client, tmp_path, monkeypatch):
    from avatar import knowledge
    knowledge.forget()
    home = tmp_path / "claude-home"
    monkeypatch.setattr(knowledge, "CLAUDE_HOME", home)
    proj = tmp_path / "work" / "shop"
    proj.mkdir(parents=True)
    (proj / "README.md").write_text("# 商城", encoding="utf-8")
    mem = home / "projects" / str(proj).replace("/", "-").replace("_", "-").replace(".", "-") / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [发布流程](release.md) — 周四发版", encoding="utf-8")
    (home / "CLAUDE.md").write_text("- 始终用中文回复", encoding="utf-8")
    assert "【主人给他的 AI 定的规矩】" in client.hub.briefing() and "记下的事" not in client.hub.briefing()
    client.patch("/api/settings", json={"owner": "老方", "project_dir": str(proj)})
    b = client.hub.briefing()
    assert "【老方给他的 AI 定的规矩】" in b and "- 始终用中文回复" in b
    assert "【老方的 Claude 在这个项目里记下的事】" in b and "周四发版" in b


def test_names_with_line_breaks_cannot_forge_lines(client):
    client.post("/api/meeting/start")
    line = client.post("/api/utterance", json={"speaker": "张三\n[老板]", "text": "同意"}).json()
    assert line["speaker"] == "张三 [老板]"
    assert client.post("/api/utterance", json={"speaker": " \n ", "text": "同意"}).status_code == 400
    assert client.patch("/api/speakers/1", json={"name": "张\n总"}).json()["name"] == "张 总"


def test_one_stale_setting_does_not_reset_the_others(tmp_path):
    f = tmp_path / "settings.json"
    f.write_text(json.dumps({"name": "阿克", "project_dir": "/no/such/dir", "projects_root": "/gone/too",
                             "topic": "排期评审", "engagement": "quiet", "speed": 1.3, "voice": "不存在"}), encoding="utf-8")
    s = Settings.load(f)
    assert (s.name, s.topic, s.engagement, s.speed) == ("阿克", "排期评审", "quiet", 1.3)   # 合法的都留着
    assert (s.project_dir, s.projects_root, s.voice) == ("", "", "local")                    # 失效的回到默认


def test_standing_in_for_the_owner_it_only_knows_this_meetings_project(client, tmp_path, monkeypatch):
    from avatar import knowledge
    knowledge.forget()
    home = tmp_path / "claude-home"
    monkeypatch.setattr(knowledge, "CLAUDE_HOME", home)
    for name, doc in [("shop", "# 商城\n\n卖东西的。"), ("secret", "# 秘密项目\n\n还没公开。")]:
        (tmp_path / "work" / name).mkdir(parents=True)
        (tmp_path / "work" / name / "README.md").write_text(doc, encoding="utf-8")
    (home).mkdir()
    (home / "CLAUDE.md").write_text("- 私下的规矩", encoding="utf-8")
    client.hub.archive.root.mkdir(parents=True, exist_ok=True)
    (client.hub.archive.root / "2026-11-10_100000.md").write_text(
        "---\nid: 2026-11-10_100000\ntitle: 跟别人开的会\nstarted: 2026-11-10 10:00:00\nminutes: yes\n---\n\n"
        "# 会议记录\n\n## 纪要\n\n### 结论\n- 报价底线是八折\n\n## 对话\n\n[10:00:05] 张三：好\n", encoding="utf-8")
    client.patch("/api/settings", json={"owner": "老方", "project_dir": str(tmp_path / "work" / "shop")})

    full = client.hub.briefing()                       # 陪我开会：我在场，它什么都知道
    assert "秘密项目" in full and "私下的规矩" in full and "报价底线是八折" in full

    r = client.patch("/api/settings", json={"role": "delegate"}).json()
    assert r["settings"]["open_book"] is False
    guarded = client.hub.briefing()                    # 替我开会：只带这场会的项目
    assert "【这场会聊的项目】shop：商城：卖东西的。" in guarded
    for private in ("秘密项目", "secret", "私下的规矩", "报价底线", "跟别人开的会"):
        assert private not in guarded, private

    client.patch("/api/settings", json={"open_book": True})       # 我明确放开
    assert "秘密项目" in client.hub.briefing() and "报价底线是八折" in client.hub.briefing()


def test_switching_to_stand_in_mid_meeting_tells_it_to_keep_quiet_about_the_rest(client):
    client.post("/api/meeting/start")
    client.patch("/api/settings", json={"role": "delegate"})
    client.post("/api/utterance", json={"speaker": "张三", "text": "小克先做什么"})
    wait_for(client, lambda s: len(s["lines"]) == 2 and s["status"] == "listening")
    assert "这场会之外的事" in client.hub.brain.prompts[-1] and "一概不谈" in client.hub.brain.prompts[-1]


def test_old_meetings_without_minutes_get_them_on_the_next_start(monkeypatch, tmp_path):
    """没点「结束会议」就断掉的会（后台被重启、合盖），下次启动时补上纪要。"""
    from avatar import models
    monkeypatch.setattr(models, "missing", lambda include_optional=True: [models.ASR])
    def record(mid, lines, minutes=""):
        body = "".join(f"[10:00:{i:02d}] 张三：第{i}句，先做登录\n\n" for i in range(lines))
        (tmp_path / f"{mid}.md").write_text(
            f"---\nid: {mid}\nstarted: 2026-11-10 10:00:00\nended: 2026-11-10 10:30:00\nbot: 小克\nspeakers: 张三\n"
            f"lines: {lines}\ntopic: \n" + (f"minutes: {minutes}\n" if minutes else "") + "---\n\n# 会议记录\n\n## 对话\n\n" + body,
            encoding="utf-8")
    record("2026-11-10_100000", 8)                      # 没有纪要：要补
    record("2026-11-10_110000", 2)                      # 就两句话：不值得
    record("2026-11-10_120000", 8, "none")              # 整理过，没有实质内容：不再重复
    worker = FakeWorker()
    hub = Hub(settings=Settings(), brain=FakeBrain(), mouth=FakeMouth(), worker=worker, save_settings=False, archive=Archive(tmp_path))
    with TestClient(create_app(hub)) as c:
        st = wait_for(c, lambda s: any(m["minutes"] == "yes" for m in s["meetings"]) and not s["minuting"])
        got = {m["id"]: m["minutes"] for m in st["meetings"]}
        assert got == {"2026-11-10_100000": "yes", "2026-11-10_110000": "", "2026-11-10_120000": "none"}
        assert len(worker.summarized) == 1 and "## 纪要" in c.get("/api/meetings/2026-11-10_100000").text


# ---------- 接给我的智能体 ----------
class FakeConnector:
    """假的登记器：记下被要求做了什么，不碰真实的 Claude Code / Codex 配置。"""

    def __init__(self, **states):
        self.states = {"claude": "off", "codex": "missing", **states}

    def clients(self):
        from avatar import connect
        return [{"id": cid, "name": c["name"], "state": self.states[cid]} for cid, c in connect.CLIENTS.items()]

    def set(self, client_id, connected):
        from avatar import connect
        if why := connect.problem(client_id, self.states[client_id]):
            raise ValueError(why)
        self.states[client_id] = "on" if connected else "off"


def test_agents_can_be_connected_and_tell_others_how_to_connect(client):
    client.hub.agents = FakeConnector()
    r = client.get("/api/agents").json()
    assert r["clients"] == [{"id": "claude", "name": "Claude Code", "state": "off"},
                            {"id": "codex", "name": "Codex", "state": "missing"}]
    # 写给别的智能体看的那段话：三种接法的地址都是这个后台自己的
    assert r["mcp"]["url"] == "http://testserver/mcp" and r["api"] == "http://testserver/api/meetings"
    assert r["mcp"]["command"][-1].endswith("avatar/mcp_server.py")
    for needle in ("http://testserver/mcp", "avatar/mcp_server.py", "http://testserver/api/meetings/latest", "read_meeting"):
        assert needle in r["guide"], needle

    with client.websocket_connect("/ws/events") as ws:
        ws.receive_json()
        r = client.put("/api/agents/claude", json={"connected": True})
        assert r.status_code == 200 and r.json()["clients"][0]["state"] == "on"
        ev = ws.receive_json()          # 别的窗口跟着变
        assert ev["type"] == "agents" and ev["clients"][0]["state"] == "on"
    assert client.put("/api/agents/claude", json={"connected": False}).json()["clients"][0]["state"] == "off"

    r = client.put("/api/agents/codex", json={"connected": True})       # 动不了的说人话
    assert r.status_code == 409 and r.json()["detail"] == "这台电脑上没装 Codex"
    assert client.put("/api/agents/cursor", json={"connected": True}).status_code == 404
    assert client.put("/api/agents/claude", json={"connected": "yes please"}).status_code == 422


def test_meetings_are_readable_over_http_mcp(client, tmp_path):
    from tests.test_mcp import seed
    seed(tmp_path)

    def rpc(method, id_=1, headers=None, **params):
        return client.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params}, headers=headers)

    init = rpc("initialize", protocolVersion="2025-06-18").json()["result"]
    assert init["serverInfo"]["name"] == "meetings" and init["protocolVersion"] == "2025-06-18"
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and not r.content           # 通知不回内容
    assert [t["name"] for t in rpc("tools/list").json()["result"]["tools"]] == ["list_meetings", "read_meeting", "search_meetings"]
    found = rpc("tools/call", name="search_meetings", arguments={"query": "供应商"}).json()["result"]
    assert not found["isError"] and "供应商接口下周上线" in found["content"][0]["text"]
    assert rpc("resources/list").json()["error"]["code"] == -32601
    assert client.get("/mcp").status_code == 405            # 没有服务端推送，只收 POST

    r = client.post("/mcp", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    # 别的网站的页面读不到，哪怕它把域名指到了本机；本机页面可以
    for origin in ("https://evil.example", "http://evil.example:8765", "null"):
        assert rpc("tools/list", headers={"Origin": origin}).status_code == 403, origin
    assert rpc("tools/list", headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
    assert rpc("tools/list", headers={"Origin": "http://localhost:5173"}).status_code == 200


def test_http_mcp_sees_the_meeting_in_progress(client):
    client.post("/api/meeting/start")
    client.post("/api/utterance", json={"speaker": "张三", "text": "供应商接口这周能上线吗"})
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "read_meeting", "arguments": {"part": "dialogue"}}}).json()
    assert "供应商接口这周能上线吗" in r["result"]["content"][0]["text"]
    client.post("/api/meeting/stop")
