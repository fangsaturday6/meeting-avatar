"""慢脑：不真的启动 claude，只检查它为每种任务准备的工作目录、命令和提示。"""
import pytest

from avatar import knowledge, slow_brain
from avatar.config import Settings
from avatar.floor import WorkResult
from avatar.meeting import Task
from avatar.slow_brain import SlowBrain


@pytest.fixture
def brain(monkeypatch, tmp_path):
    knowledge.forget()
    monkeypatch.setattr(knowledge, "CLAUDE_HOME", tmp_path / "claude-home")
    monkeypatch.setattr(slow_brain, "MEETINGS_DIR", tmp_path / "meetings")
    b = SlowBrain(Settings())
    b.calls = []

    async def fake_attempt(cmd, cwd, on_progress, stdin_text=None):
        b.calls.append({"cmd": cmd, "cwd": cwd, "stdin": stdin_text})
        return WorkResult(True, "登录排期\n\n### 结论\n- 先做登录"), False

    monkeypatch.setattr(b, "_attempt", fake_attempt)
    return b


def arg(cmd, flag):
    return [cmd[i + 1] for i, x in enumerate(cmd) if x == flag]


async def test_code_lookup_runs_above_all_projects_with_the_index(brain, tmp_path):
    for name, doc in [("shop", "# 商城 {beta}\n\n卖东西的。"), ("erp", "# 进销存")]:
        (tmp_path / "work" / name).mkdir(parents=True)
        (tmp_path / "work" / name / "README.md").write_text(doc, encoding="utf-8")
    mem = knowledge.CLAUDE_HOME / "projects" / str(tmp_path / "work" / "erp").replace("/", "-").replace("_", "-").replace(".", "-") / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- 发版在周四", encoding="utf-8")
    brain.settings.project_dir = str(tmp_path / "work" / "shop")

    res = await brain.run(Task(1, "查代码", "供应商接口 {id} 上线了吗"), lambda p: None)
    assert res.ok
    call = brain.calls[0]
    prompt = call["cmd"][2]
    assert call["cwd"] == tmp_path / "work"                       # 在所有项目的上一级跑，它自己找对的那个
    assert "供应商接口 {id} 上线了吗" in prompt                    # 问题里的花括号原样保留
    assert f"- shop（{tmp_path / 'work' / 'shop'}）：商城 {{beta}}：卖东西的。" in prompt
    assert "默认查「shop」" in prompt and f"［记忆在 {mem}］" in prompt
    assert arg(call["cmd"], "--add-dir") == [str(mem)]            # 只放开有记忆的那个目录
    assert "{" not in prompt.replace("{id}", "").replace("{beta}", "")   # 模板里的占位都填上了
    assert arg(call["cmd"], "--tools") == ["Read,Grep,Glob"]      # 只读
    assert "--allowedTools" not in call["cmd"]                    # 不给全局许可：出了这些目录就读不到


async def test_code_lookup_without_any_project_says_so(brain):
    res = await brain.run(Task(1, "查代码", "登录在哪"), lambda p: None)
    assert not res.ok and res.text == "还没有登记项目目录，没法查代码" and brain.calls == []


async def test_past_meetings_are_searched_in_the_records_folder(brain, tmp_path):
    res = await brain.run(Task(1, "查会议", "上次怎么定的"), lambda p: None)
    assert not res.ok and res.text == "还没有以前的会议记录" and brain.calls == []
    (tmp_path / "meetings").mkdir()
    (tmp_path / "meetings" / "2026-11-10_100000.md").write_text("x", encoding="utf-8")
    assert (await brain.run(Task(2, "查会议", "上次怎么定的"), lambda p: None)).ok
    call = brain.calls[0]
    assert call["cwd"] == tmp_path / "meetings" and "上次怎么定的" in call["cmd"][2]
    assert arg(call["cmd"], "--tools") == ["Read,Grep,Glob"]


async def test_minutes_prompt_goes_through_stdin_and_knows_who_it_is_for(brain):
    res = await brain.summarize("[10:00:05] 张三：先做登录 {x}")
    assert res.ok
    call = brain.calls[0]
    assert "[10:00:05] 张三：先做登录 {x}" in call["stdin"] and "### 待办" in call["stdin"]
    assert "需要" not in call["stdin"].split("=== 会议记录 ===")[0].replace("需要他", "")   # 没填名字就没有「需要某某确认」
    assert arg(call["cmd"], "--tools") == [""] and "-p" in call["cmd"]
    assert not any("张三" in x for x in call["cmd"])               # 记录不放在命令行参数里

    brain.settings.owner, brain.settings.role = "老方", "delegate"
    await brain.summarize("…")
    head = brain.calls[1]["stdin"].split("=== 会议记录 ===")[0]
    assert "### 需要老方确认" in head and "「老方」没有到场" in head


async def test_standing_in_it_only_looks_at_this_meetings_project(brain, tmp_path):
    for name in ("shop", "secret"):
        (tmp_path / "work" / name).mkdir(parents=True)
        (tmp_path / "work" / name / "README.md").write_text(f"# {name}", encoding="utf-8")
    (tmp_path / "meetings").mkdir()
    (tmp_path / "meetings" / "2026-11-10_100000.md").write_text("x", encoding="utf-8")
    brain.settings.project_dir = str(tmp_path / "work" / "shop")
    brain.settings.role = "delegate"

    assert (await brain.run(Task(1, "查代码", "登录在哪"), lambda p: None)).ok
    call = brain.calls[0]
    assert call["cwd"] == tmp_path / "work" / "shop"               # 只在主项目里查
    assert "secret" not in call["cmd"][2] and arg(call["cmd"], "--add-dir") == []
    assert "不要读密钥" in call["cmd"][2]
    res = await brain.run(Task(2, "查会议", "上次报价多少"), lambda p: None)
    assert not res.ok and "问他本人" in res.text and len(brain.calls) == 1   # 别的会不翻

    brain.settings.open_book = True
    assert (await brain.run(Task(3, "查代码", "登录在哪"), lambda p: None)).ok
    assert brain.calls[1]["cwd"] == tmp_path / "work" and "secret" in brain.calls[1]["cmd"][2]


async def test_design_task_cannot_touch_the_project_and_research_has_no_file_tools(brain, tmp_path, monkeypatch):
    monkeypatch.setattr(slow_brain, "DESIGN_DIR", tmp_path / "designs")
    proj = tmp_path / "work" / "shop"
    proj.mkdir(parents=True)
    (proj / "README.md").write_text("# 商城\n\n卖电子元器件的网站，面向采购员，支持按型号搜索、批量询价和在线下单。\n\n## 启动\n", encoding="utf-8")
    brain.settings.project_dir = str(proj)

    async def attempt(cmd, cwd, on_progress, stdin_text=None):
        brain.calls.append({"cmd": cmd, "cwd": cwd})
        (cwd / "index.html").write_text("<html></html>", encoding="utf-8")
        return WorkResult(True, "做了一版登录页。"), False

    monkeypatch.setattr(brain, "_attempt", attempt)
    res = await brain.run(Task(1, "做设计", "出一版登录页"), lambda p: None)
    cmd = brain.calls[0]["cmd"]
    assert res.ok and res.artifact.startswith("/designs/task-1-")
    assert "--allowedTools" not in cmd and "--add-dir" not in cmd          # 只能写在自己的设计稿目录里
    assert "面向采购员" in cmd[2]                                          # 项目是什么，用简介告诉它

    await brain.run(Task(2, "查资料", "飞书接口"), lambda p: None)
    cmd = brain.calls[1]["cmd"]
    assert arg(cmd, "--tools") == ["WebSearch,WebFetch"] and arg(cmd, "--allowedTools") == ["WebSearch,WebFetch"]
