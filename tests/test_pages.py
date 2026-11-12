"""页面和演示后台：地址都能打开，按钮和表单项的 id 不被改版改丢，演示后台能演完一轮。"""
import re
import time

import pytest
from starlette.testclient import TestClient

from avatar import demo
from avatar.api import create_app

# 界面元素的稳定名字。AI Agent 和桌面应用的菜单靠这些 id 找按钮，改版时不能改名。
STAGE_IDS = ["stage", "avatar", "avatar-grip", "status-text", "ai-tag", "board", "board-kind", "board-brief", "board-body", "board-open",
             "btn-board-close", "caption", "alerts", "dock", "btn-start", "btn-mute", "btn-hush", "btn-stop",
             "btn-notes", "btn-settings"]
PANEL_IDS = ["panel", "tab-notes", "tab-settings", "alerts", "btn-meetings", "tasks", "transcript", "composer", "say-kind",
             "say-text", "btn-send", "composer-error", "meetings", "reader", "reader-title", "reader-sub", "btn-reader-close",
             "btn-minutes", "btn-copy-path", "reader-body", "settings", "engagement", "set-topic", "set-role", "open-book-row",
             "set-open-book", "set-scene", "set-project", "set-projects-root", "projects-count", "set-name", "set-owner",
             "set-voice", "set-speed", "set-skills", "mic-select", "speaker-select", "settings-error",
             "agents", "agent-claude", "agent-codex", "btn-copy-agent-guide", "agents-error"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "DESIGN_DIR", tmp_path)
    monkeypatch.setattr(demo, "PACE", 0.01)
    with TestClient(create_app(demo.DemoHub())) as c:
        yield c


def wait_for(client, cond, tries=300):
    for _ in range(tries):
        st = client.get("/api/state").json()
        if cond(st):
            return st
        time.sleep(0.01)
    raise AssertionError("等待超时")


def ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


@pytest.mark.parametrize("path, expected", [("/", STAGE_IDS), ("/stage", STAGE_IDS), ("/panel", PANEL_IDS)])
def test_pages_keep_their_ids(client, path, expected):
    r = client.get(path)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert not set(expected) - ids(r.text)


def test_page_assets_are_served(client):
    for name in ["stage.js", "panel.js", "character.js", "model.js", "avatar.js", "audio.js", "api.js", "icons.js", "common.css",
                 "vendor/three/three.module.js", "vendor/three/three.core.js", "vendor/three/RoomEnvironment.js",
                 "vendor/three/GLTFLoader.js", "vendor/three/BufferGeometryUtils.js", "vendor/three/SkeletonUtils.js"]:
        assert client.get(f"/static/{name}").status_code == 200, name


def test_figures_lists_models_with_their_motions(client, tmp_path, monkeypatch):
    from avatar import api
    monkeypatch.setattr(api, "FIGURES", tmp_path / "none")
    assert client.get("/api/figures").json() == {"figures": []}          # 没有目录也不报错

    monkeypatch.setattr(api, "FIGURES", tmp_path)
    for name in ["butcher.glb", "butcher.thinking.glb", "butcher.dance.glb", "orphan.waiting.glb", "notes.txt"]:
        (tmp_path / name).write_bytes(b"")
    assert client.get("/api/figures").json() == {"figures": [
        {"name": "butcher", "url": "/static/models/butcher.glb", "motions": {"thinking": "/static/models/butcher.thinking.glb"}}]}


def test_every_page_action_has_an_api():
    """api.js 里的每个操作都对应一个后台接口。"""
    from pathlib import Path
    src = (Path(demo.__file__).parent / "web" / "api.js").read_text(encoding="utf-8")
    called = set(re.findall(r"call\('(GET|POST|PATCH|PUT)', [`']([^`'$]+)", src))
    routes = {(m, r.path) for r in create_app(demo.DemoHub()).routes for m in getattr(r, "methods", None) or []}
    # 带参数的地址（/api/meetings/{id}/minutes）在 api.js 里只写到参数之前，所以按开头比
    missing = {(m, p) for m, p in called if not any(rm == m and rp.split("{")[0] == p or rm == m and rp == p for rm, rp in routes)}
    assert called and not missing, missing


def test_demo_meeting_runs_without_models(client, tmp_path):
    assert client.get("/api/state").json()["ready"] == {"ears": True, "speakers": True, "local_voice": True}
    assert client.post("/api/meeting/start").json()["status"] == "listening"
    client.post("/api/utterance", json={"speaker": "李工", "text": "小克你怎么看"})
    st = wait_for(client, lambda s: any(l["who"] == "bot" for l in s["lines"]) and s["status"] == "listening")
    assert st["lines"][0]["speaker"] == "李工"

    client.post("/api/utterance", json={"speaker": "张总", "text": "那出一版登录页看看"})
    st = wait_for(client, lambda s: s["tasks"] and s["tasks"][0]["status"] == "done")
    task = st["tasks"][0]
    assert task["kind"] == "做设计" and st["board_task_id"] == task["id"]
    assert task["artifact"] == f"/designs/demo-{task['id']}/index.html"
    assert (tmp_path / f"demo-{task['id']}" / "index.html").exists()
    assert client.post("/api/meeting/stop").json()["status"] == "off"


def test_every_connectable_agent_has_its_own_switch(client):
    """能一键登记的智能体，设置窗口里各有一个开关，名字是「agent-编号」；演示后台里拨它不碰真实配置。"""
    from avatar import connect
    html = client.get("/panel").text
    assert {f"agent-{cid}" for cid in connect.CLIENTS} <= ids(html)
    assert set(re.findall(r'data-agent="([^"]+)"', html)) == set(connect.CLIENTS)
    assert [c["state"] for c in client.get("/api/agents").json()["clients"]] == ["off", "missing"]
    assert client.put("/api/agents/claude", json={"connected": True}).json()["clients"][0]["state"] == "on"
