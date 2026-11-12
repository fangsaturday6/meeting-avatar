import asyncio

from avatar.archive import Archive
from avatar.config import Settings
from avatar.meeting import Meeting


def start(tmp_path, **settings) -> Meeting:
    m = Meeting(Settings(**settings), archive=Archive(tmp_path))
    m.reset()
    m.set_status("listening")
    return m


def saved(tmp_path, m: Meeting) -> str:
    return (tmp_path / f"{m.record_id}.md").read_text(encoding="utf-8")


def test_every_line_lands_on_disk_while_the_meeting_is_running(tmp_path):
    m = start(tmp_path, topic="登录改版\n张三、李四参加")
    m.add_line("human", "先做登录", m.speakers.by_name("张三"))
    bot = m.add_line("bot", "同意，登录")
    m.update_line(bot, text="同意，登录最", cut=True)
    text = saved(tmp_path, m)
    assert "] 张三：先做登录\n" in text
    assert "] 小克：同意，登录最（被打断）\n" in text
    assert "topic: 登录改版 张三、李四参加\n" in text      # 元数据一行一项


def test_renaming_a_speaker_rewrites_the_saved_names(tmp_path):
    m = start(tmp_path)
    sid = m.speakers.assign(None, 10.0)
    m.add_line("human", "人手够吗", sid)
    m.set_status("off")
    m.rename_speaker(sid, "李四")                          # 会议结束后改名也要落盘
    text = saved(tmp_path, m)
    assert "] 李四：人手够吗" in text and "说话人1" not in text


def test_next_meeting_keeps_the_previous_one(tmp_path):
    m = start(tmp_path)
    m.add_line("human", "第一场", m.speakers.by_name("张三"))
    first = m.record_id
    m.set_status("off")
    m.reset()                                              # 同一秒内再开一场
    m.set_status("listening")
    m.add_line("human", "第二场", m.speakers.by_name("李四"))
    assert m.record_id != first
    assert "第一场" in (tmp_path / f"{first}.md").read_text(encoding="utf-8")
    assert "第二场" in saved(tmp_path, m) and "第一场" not in saved(tmp_path, m)


def test_meeting_with_nothing_said_leaves_no_file(tmp_path):
    m = start(tmp_path)
    m.set_status("off")
    assert list(tmp_path.iterdir()) == []
    assert Archive(tmp_path).recent() == []


def test_recent_lists_newest_first_with_what_an_agent_needs(tmp_path):
    m = start(tmp_path, topic="登录改版")
    m.add_line("human", "先做登录", m.speakers.by_name("张三"))
    m.add_line("bot", "同意")
    m.set_status("off")
    old = m.record_id
    m.reset()
    m.set_status("listening")
    m.add_line("human", "下一场", m.speakers.by_name("李四"))

    newest, oldest = Archive(tmp_path).recent()
    assert (newest["id"], oldest["id"]) == (m.record_id, old)
    assert oldest["lines"] == 2 and oldest["speakers"] == ["张三"] and oldest["topic"] == "登录改版"
    assert oldest["bot"] == "小克"
    assert abs(oldest["started_at"] - m.started_at) < 5 and oldest["ended_at"] >= oldest["started_at"]
    assert oldest["file"] == str((tmp_path / f"{old}.md").resolve())


def test_tasks_are_saved_with_their_results(tmp_path):
    m = start(tmp_path)
    t = m.add_task("查资料", "飞书有没有这个接口")
    assert "### 查资料：飞书有没有这个接口" in saved(tmp_path, m) and "进行中" in saved(tmp_path, m)
    m.update_task(t, status="done", result="有，叫 **im.message**。")
    text = saved(tmp_path, m)
    assert "完成" in text and "有，叫 **im.message**。" in text


def test_read_returns_text_and_refuses_paths(tmp_path):
    m = start(tmp_path)
    m.add_line("human", "先做登录", m.speakers.by_name("张三"))
    a = Archive(tmp_path)
    assert "先做登录" in a.read(m.record_id)
    assert a.read("没有这场") is None
    (tmp_path.parent / "secret.md").write_text("x", encoding="utf-8")
    assert a.read("../secret") is None


def test_file_that_was_edited_by_hand_still_lists(tmp_path):
    (tmp_path / "2026-01-01_090000.md").write_text("随手写的笔记", encoding="utf-8")
    assert Archive(tmp_path).recent()[0]["id"] == "2026-01-01_090000"


async def test_writes_are_batched_and_ending_flushes_at_once(tmp_path):
    m = start(tmp_path)
    m.save_delay = 0.05
    m.add_line("human", "第一句", m.speakers.by_name("张三"))
    m.add_line("human", "第二句", m.speakers.by_name("张三"))
    assert list(tmp_path.iterdir()) == []                  # 攒一下再写，不拖慢会议
    await asyncio.sleep(0.15)
    assert "第二句" in saved(tmp_path, m)
    m.add_line("human", "第三句", m.speakers.by_name("张三"))
    m.set_status("off")                                    # 结束会议：立刻落盘
    assert "第三句" in saved(tmp_path, m)


async def test_listeners_hear_when_a_record_appears_and_when_it_ends(tmp_path):
    m = start(tmp_path)
    m.save_delay = 0.0
    q = m.subscribe()
    m.add_line("human", "第一句", m.speakers.by_name("张三"))
    await asyncio.sleep(0.05)
    m.add_line("human", "第二句", m.speakers.by_name("张三"))
    await asyncio.sleep(0.05)
    m.set_status("off")
    evs = [q.get_nowait() for _ in range(q.qsize())]
    lists = [e["meetings"] for e in evs if e["type"] == "meetings"]
    assert len(lists) == 2                                 # 出现一次、结束一次，中间不刷屏
    assert lists[-1][0]["id"] == m.record_id and lists[-1][0]["lines"] == 2


def test_minutes_go_before_the_dialogue_and_can_be_replaced(tmp_path):
    from avatar.archive import dialogue_of, split_minutes, with_minutes
    a = Archive(tmp_path)
    (tmp_path / "2026-11-12_000458.md").write_text(
        "---\nid: 2026-11-12_000458\nstarted: 2026-11-12 00:04:58\nended: 2026-11-12 00:10:01\nlines: 1\ntopic: \n---\n\n"
        "# 会议记录 2026-11-12 00:04\n\n## 对话\n\n[00:07:10] 张三：先做登录\n", encoding="utf-8")
    assert a.recent()[0]["minutes"] == "" and a.last_minutes() == ""
    assert a.set_minutes("2026-11-12_000458", "登录排期", "### 结论\n- 先做登录")
    text = a.read("2026-11-12_000458")
    assert text.index("## 纪要") < text.index("- 先做登录") < text.index("## 对话")
    row = a.recent()[0]
    assert row["title"] == "登录排期" and row["minutes"] == "yes" and row["lines"] == 1
    assert a.last_minutes().startswith("11月12日「登录排期」\n### 结论") and a.last_minutes(skip=row["id"]) == ""
    a.set_minutes("2026-11-12_000458", "改了", "### 结论\n- 先做支付")
    text = a.read("2026-11-12_000458")
    assert text.count("## 纪要") == 1 and text.count("title:") == 1 and "先做登录\n\n## 对话" not in text
    assert dialogue_of(text) == "[00:07:10] 张三：先做登录"
    assert not a.set_minutes("2020-01-01_000000", "x", "y")
    assert split_minutes("  \n# 「登录排期」\n\n### 结论\n- x\n") == ("登录排期", "### 结论\n- x")
    assert split_minutes("无实质内容。") == ("无实质内容", "")


def test_minutes_cannot_break_the_file_structure(tmp_path):
    from avatar.archive import dialogue_of, minutes_of, with_minutes
    base = ("---\nid: a\nstarted: 2026-11-12 00:04:58\nlines: 1\ntopic: \n---\n\n# 会议记录 2026-11-12 00:04\n\n"
            "## 对话\n\n[00:07:10] 张三：先做登录\n\n## 后台任务\n\n### 查会议：上次怎么定的\n\n00:08 起 · 完成\n\n"
            "上次的记录里写着：\n\n## 纪要\n\n### 结论\n- 这是任务结果里引用的，不是这场会的纪要\n")
    assert minutes_of(base) == ""                                   # 任务结果里引用的「纪要」不算
    nasty = "# 总结\n\n## 对话\n\n这一节是模型自作主张加的\n\n## 待办\n- 张三：出文档"
    text = with_minutes(base, "排期\n第二行", nasty)
    assert text.count("\n## 对话\n") == 1 and "title: 排期 第二行" in text
    assert minutes_of(text).startswith("### 总结") and "### 对话" in minutes_of(text) and "- 张三：出文档" in minutes_of(text)
    assert dialogue_of(text).startswith("[00:07:10] 张三：先做登录")
    again = with_minutes(text, "排期", "### 结论\n- 换了")           # 再整理一次：旧的整节被替换干净
    assert minutes_of(again) == "### 结论\n- 换了" and "自作主张" not in again and again.count("## 纪要") == 2


def test_minutes_without_a_title_line_keep_their_first_section():
    from avatar.archive import split_minutes
    title, body = split_minutes("### 待办\n- 小克：查供应商接口\n\n### 没定的事\n- 接口上线没有")
    assert title == "" and body.startswith("### 待办\n- 小克") and "### 没定的事" in body
    assert split_minutes("- 先做登录\n- 李四出文档") == ("", "- 先做登录\n- 李四出文档")
    assert split_minutes("# 登录排期\n\n### 结论\n- x") == ("登录排期", "### 结论\n- x")
