"""页面和演示后台：地址都能打开，按钮和表单项的 id 不被改版改丢，演示后台能演完一轮。"""
import re
import time

import pytest
from starlette.testclient import TestClient

from avatar import demo
from avatar.api import create_app

# 界面元素的稳定名字。AI Agent 和桌面应用的菜单靠这些 id 找按钮，改版时不能改名。
STAGE_IDS = ["stage", "avatar", "status-text", "board", "board-kind", "board-brief", "board-body", "board-open",
             "btn-board-close", "caption", "alerts", "dock", "btn-start", "btn-mute", "btn-hush", "btn-stop",
             "btn-notes", "btn-settings"]
PANEL_IDS = ["panel", "tab-notes", "tab-settings", "alerts", "tasks", "transcript", "composer", "say-kind", "say-text",
             "btn-send", "composer-error", "settings", "set-topic", "set-scene", "set-project", "set-name", "set-voice",
             "set-speed", "set-skills", "mic-select", "speaker-select", "say-as", "settings-error"]


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


def test_pages_and_assets_are_never_stale(client):
    """更新界面后不能出现新页面配旧脚本：页面和静态文件都要求浏览器每次确认。"""
    for path in ["/", "/panel", "/static/stage.js", "/static/stage.css"]:
        r = client.get(path)
        assert r.headers["cache-control"] == "no-cache", path
    etag = client.get("/static/stage.js").headers["etag"]
    assert client.get("/static/stage.js", headers={"If-None-Match": etag}).status_code == 304


def test_page_assets_are_served(client):
    for name in ["stage.js", "panel.js", "character.js", "avatar.js", "audio.js", "api.js", "icons.js", "common.css",
                 "vendor/three/three.module.js", "vendor/three/three.core.js", "vendor/three/RoomEnvironment.js"]:
        assert client.get(f"/static/{name}").status_code == 200, name


def test_every_page_action_has_an_api():
    """api.js 里的每个操作都对应一个后台接口。"""
    from pathlib import Path
    src = (Path(demo.__file__).parent / "web" / "api.js").read_text(encoding="utf-8")
    called = set(re.findall(r"call\('(GET|POST|PATCH)', [`']([^`'$]+)", src))
    routes = {(m, re.sub(r"\{[^}]+\}", "", r.path)) for r in create_app(demo.DemoHub()).routes
              for m in getattr(r, "methods", None) or []}
    assert called and called <= routes, called - routes


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
