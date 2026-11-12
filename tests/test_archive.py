import asyncio

from avatar import archive
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
    assert split_minutes("会议标题：登录排期\n\n### 结论\n- x") == ("登录排期", "### 结论\n- x")


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


# ---- 记录是整理过的：一个人连着说的并成一段，语气词和口吃不进记录 ----
def said(m: Meeting, who: str, text: str, at: float, seconds: float = 1.0):
    """at 是这句话说完的时刻（相对开会多少秒），seconds 是这句话说了多久。"""
    line = m.add_line("human", text, m.speakers.by_name(who), seconds=seconds)
    line.t = m.started_at + at
    return line


def dialogue(tmp_path, m: Meeting) -> list[str]:
    m.save()
    body = saved(tmp_path, m).partition("\n## 对话\n")[2].partition("\n## 后台任务\n")[0]
    return [l.partition("] ")[2] for l in body.splitlines() if l.strip()]


def test_one_person_talking_on_becomes_one_paragraph(tmp_path):
    m = start(tmp_path)
    said(m, "张三", "我们现在是两个模式。", at=2.0, seconds=2.0)
    said(m, "张三", "一个是问答。", at=3.8, seconds=1.1)       # 只停了 0.7 秒：是一句话被切开了
    said(m, "张三", "一个是干活。", at=5.7, seconds=1.2)
    said(m, "张三", "之前没选干活，所以它只回答。", at=10.0, seconds=2.6)   # 停了 1.7 秒：另一句，但还是这一段
    assert dialogue(tmp_path, m) == ["张三：我们现在是两个模式，一个是问答，一个是干活。之前没选干活，所以它只回答。"]


def test_a_long_pause_or_another_speaker_starts_a_new_paragraph(tmp_path):
    m = start(tmp_path)
    said(m, "张三", "先做登录。", at=2.0)
    said(m, "张三", "再做支付。", at=9.0)            # 停了 6 秒
    said(m, "李四", "人手够吗？", at=10.5)
    said(m, "张三", "够。", at=12.0)
    assert dialogue(tmp_path, m) == ["张三：先做登录。", "张三：再做支付。", "李四：人手够吗？", "张三：够。"]


def test_fillers_and_stutters_stay_out_of_the_record(tmp_path):
    m = start(tmp_path)
    said(m, "张三", "呃，那那那个就是就是他他就回问题了。", at=3.0, seconds=2.5)
    said(m, "李四", "嗯。", at=4.0, seconds=0.4)        # 只是附和了一声：不进记录，也不把张三的话切成两段
    said(m, "张三", "他就。", at=5.2, seconds=0.6)
    said(m, "张三", "他就没干活。", at=6.9, seconds=1.2)
    m.add_line("bot", "嗯，我看了一下，是模式没选对。")       # 数字人自己说的话原样留着
    text = dialogue(tmp_path, m)
    assert text == ["张三：那个就是他就回问题了。他就没干活。", "小克：嗯，我看了一下，是模式没选对。"]
    assert "lines: 4\n" in saved(tmp_path, m)             # 句数只算进了记录的


def test_a_meeting_of_nothing_but_noise_has_an_empty_dialogue(tmp_path):
    m = start(tmp_path)
    said(m, "张三", "嗯。", at=1.0)
    said(m, "张三", "啊。", at=5.0)
    assert dialogue(tmp_path, m) == []
    assert "lines: 0\n" in saved(tmp_path, m)


def test_paragraphs_do_not_grow_forever(tmp_path):
    m = start(tmp_path)
    for i in range(30):
        said(m, "张三", f"第{i}点是这个接口要先定下来。", at=2.0 * (i + 1), seconds=1.8)
    paras = dialogue(tmp_path, m)
    assert len(paras) > 1 and all(len(p) < 260 for p in paras)


# ---- 整理稿：会后由 Claude 书面化的对话，放在纪要和逐句对话之间 ----
def test_polished_transcript_sits_between_minutes_and_dialogue_and_survives_resaves(tmp_path):
    m = start(tmp_path)
    sid = m.speakers.assign(None, 10.0)
    line = m.add_line("human", "那个就是先做登录对吧", sid)
    stamp = saved(tmp_path, m).partition("\n## 对话\n")[2].strip().split("]")[0] + "]"
    m.set_status("off")
    m.set_minutes("登录排期", "### 结论\n- 先做登录", f"{stamp} 说话人1：先做登录。")
    text = saved(tmp_path, m)
    assert text.index("## 纪要") < text.index("## 整理稿") < text.index("## 对话")
    assert archive.polished_of(text) == f"{stamp} 说话人1：先做登录。"
    assert "那个就是先做登录对吧" in archive.dialogue_of(text)         # 逐句原话还在，可以对照
    assert "整理稿" not in archive.minutes_of(text)

    m.rename_speaker(sid, "张三")                                      # 会后改名：整理稿里的名字跟着改
    text = saved(tmp_path, m)
    assert archive.polished_of(text) == f"{stamp} 张三：先做登录。" and "说话人1" not in text

    m.set_minutes("登录排期", "### 结论\n- 先做登录")                    # 重新整理纪要但这次没有整理稿：旧的不留
    assert "## 整理稿" not in saved(tmp_path, m)


def test_polished_transcript_can_be_added_to_an_old_record(tmp_path):
    m = start(tmp_path)
    m.add_line("human", "先做登录", m.speakers.by_name("张三"))
    m.set_status("off")
    a = Archive(tmp_path)
    assert a.set_minutes(m.record_id, "登录排期", "- 先做登录", "[10:00:00] 张三：先做登录。")
    text = a.read(m.record_id)
    assert archive.polished_of(text) == "[10:00:00] 张三：先做登录。" and archive.minutes_of(text) == "- 先做登录"
    assert a.set_minutes(m.record_id, "登录排期", "- 先做登录")          # 再整理一次、这次没有整理稿
    assert "## 整理稿" not in a.read(m.record_id) and archive.minutes_of(a.read(m.record_id)) == "- 先做登录"
