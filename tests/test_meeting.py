import json

import pytest

from avatar.config import Settings
from avatar.meeting import Meeting


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_lines_get_ids_and_emit_events():
    m = Meeting()
    q = m.subscribe()
    sid = m.speakers.by_name("张三")
    a = m.add_line("human", "先做登录", sid)
    b = m.add_line("bot", "同意")
    assert (a.id, b.id) == (1, 2)
    assert a.speaker == "张三" and b.speaker == m.settings.name
    evs = drain(q)
    assert [e["type"] for e in evs] == ["line", "line"]
    assert evs[0]["line"]["text"] == "先做登录"


async def test_status_change_emits_once():
    m = Meeting()
    q = m.subscribe()
    m.set_status("listening")
    m.set_status("listening")
    m.set_status("speaking")
    assert [e["status"] for e in drain(q)] == ["listening", "speaking"]
    assert m.active


async def test_snapshot_is_json_serializable():
    m = Meeting()
    m.add_line("human", "你好", m.speakers.by_name("张三"))
    t = m.add_task("查资料", "飞书接口")
    m.update_task(t, status="done", result="找到了")
    snap = json.loads(json.dumps(m.snapshot(), ensure_ascii=False))
    assert snap["tasks"][0]["status"] == "done" and snap["tasks"][0]["ended"] > 0
    assert snap["lines"][0]["speaker"] == "张三"
    assert not m.working


async def test_slow_subscriber_drops_oldest_instead_of_blocking():
    m = Meeting()
    q = m.subscribe(maxsize=3)
    for i in range(10):
        m.add_line("human", str(i))
    assert [e["line"]["text"] for e in drain(q)] == ["7", "8", "9"]


async def test_rename_speaker_rewrites_old_lines():
    m = Meeting()
    sid = m.speakers.assign(None)
    line = m.add_line("human", "大家好", sid)
    assert line.speaker == "说话人1"
    m.rename_speaker(sid, "王五")
    assert m.lines[0].speaker == "王五"


def test_settings_validate_and_roundtrip(tmp_path):
    s = Settings()
    assert s.update({"name": "阿克", "gap": 1, "unknown": 1}) == ["name", "gap"]
    assert s.gap == 1.0
    with pytest.raises(ValueError):
        s.update({"voice": "不存在"})
    with pytest.raises(ValueError):
        s.update({"project_dir": "/no/such/dir"})
    with pytest.raises(ValueError):
        s.update({"name": 3})
    f = tmp_path / "s.json"
    s.save(f)
    assert Settings.load(f).name == "阿克"


async def test_naming_someone_like_another_merges_them():
    m = Meeting()
    q = m.subscribe()
    a, b = m.speakers.by_name("张三"), m.speakers._new().id
    m.add_line("human", "我先说", a)
    m.add_line("human", "我补充", b)
    drain(q)
    sp = m.rename_speaker(b, " 张三 ")                                # 说话人2 其实也是张三
    assert sp["id"] == a and list(m.speakers.speakers) == [a]
    assert [(l.speaker_id, l.speaker) for l in m.lines] == [(a, "张三"), (a, "张三")]
    assert any(e["type"] == "snapshot" for e in drain(q))


async def test_one_line_can_be_given_to_someone_else_and_stays_there():
    m = Meeting()
    a, b = m.speakers.by_name("张三"), m.speakers.by_name("李四")
    l1 = m.add_line("human", "我先说", a)
    l2 = m.add_line("human", "这句其实是李四说的", a)
    m.reassign_line(l2.id, b)
    assert (l2.speaker_id, l2.speaker, l2.pinned) == (b, "李四", True) and l1.speaker_id == a
    with pytest.raises(KeyError):
        m.reassign_line(l1.id, 99)


async def test_recheck_results_are_applied_and_empty_speakers_disappear():
    m = Meeting()
    a, b = m.speakers._new().id, m.speakers._new().id
    lines = [m.add_line("human", t, sid) for t, sid in (("一", a), ("二", b), ("三", b))]
    assert m.apply_speakers({lines[1].id: a, lines[2].id: -1}) == 2
    new = lines[2].speaker_id
    assert lines[1].speaker_id == a and new not in (a, b)
    assert set(m.speakers.speakers) == {a, new}                       # 说话人2 一句都不剩了，去掉
    assert m.speakers.speakers[a].count == 2
