import asyncio

import pytest

from avatar.floor import Floor, WorkResult
from avatar.meeting import Meeting
from tests.fakes import FakeBrain, FakeMouth, FakePlayer, FakeWorker


async def settle(cond, timeout=2.0):
    """等到条件成立；超时则失败。"""
    end = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > end:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.005)


async def make(replies=(), auto=True, brain_delay=0.0, worker=None, nudge=0.0):
    m = Meeting()
    m.settings.gap = 0.05
    m.settings.barge_delay = 0.05
    m.settings.silence_nudge = nudge
    brain, mouth, player = FakeBrain(replies, brain_delay), FakeMouth(), FakePlayer(auto)
    floor = Floor(m, brain, mouth, player, worker)
    player.floor = floor
    await floor.start()
    return m, brain, mouth, player, floor


def statuses(q):
    out = []
    while not q.empty():
        ev = q.get_nowait()
        if ev["type"] == "status":
            out.append(ev["status"])
    return out


async def test_addressed_question_gets_spoken_answer():
    m, brain, mouth, player, floor = await make(["先做登录吧。支付依赖它。"])
    q = m.subscribe()
    await floor.on_utterance("小克你觉得先做什么", speaker_name="张三")
    await settle(lambda: m.status == "listening" and player.played)
    assert brain.prompts == ["[张三] 小克你觉得先做什么"]
    assert player.texts == ["先做登录吧。", "支付依赖它。"]
    assert statuses(q) == ["thinking", "speaking", "listening"]
    assert [(l.who, l.text) for l in m.lines] == [
        ("human", "小克你觉得先做什么"), ("bot", "先做登录吧。支付依赖它。")]
    await floor.stop()
    assert m.status == "off"


async def test_silent_reply_plays_nothing():
    m, brain, mouth, player, floor = await make(["[静]"])
    await floor.on_utterance("李四你那边人手够吗", speaker_name="张三")
    await settle(lambda: brain.prompts and m.status == "listening")
    await asyncio.sleep(0.05)
    assert player.played == [] and mouth.said == []
    assert [l.who for l in m.lines] == ["human"]
    await floor.stop()


async def test_holds_while_someone_is_talking_then_speaks_after_gap():
    m, brain, mouth, player, floor = await make(["我补充一点。"])
    await floor.on_utterance("这个方案", speaker_name="张三")
    floor.on_speech_start()                      # 张三又接着说了
    await settle(lambda: m.status == "waiting")
    assert player.played == []
    floor.on_speech_end()
    await asyncio.sleep(0.02)
    assert player.played == []                   # 还没到 0.05 秒的空档
    await settle(lambda: player.played)
    assert player.texts == ["我补充一点。"]
    await floor.stop()


async def test_new_utterance_while_waiting_abandons_and_rethinks():
    m, brain, mouth, player, floor = await make(["在的。", "登录接口用 OAuth 比较稳。"])
    await floor.on_utterance("小克，", speaker_name="张三")
    floor.on_speech_start()
    await settle(lambda: m.status == "waiting")
    floor.on_speech_end()
    await floor.on_utterance("登录接口你建议怎么做", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert player.texts == ["登录接口用 OAuth 比较稳。"]      # 「在的」没有说出口
    assert len(brain.prompts) == 2
    assert "还没出口" in brain.prompts[1] and "登录接口你建议怎么做" in brain.prompts[1]
    assert brain.cancels == 1
    await floor.stop()


async def test_new_utterance_while_thinking_abandons_and_rethinks():
    m, brain, mouth, player, floor = await make(["嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯嗯。", "好的。"], brain_delay=0.03)
    await floor.on_utterance("我觉得", speaker_name="张三")
    await settle(lambda: brain.prompts)
    await floor.on_utterance("应该先做支付", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert player.texts == ["好的。"]
    await floor.stop()


async def test_barge_in_stops_speech_and_tells_brain():
    m, brain, mouth, player, floor = await make(["第一句说完了。第二句还在说。", "好，你说。"], auto=False)
    await floor.on_utterance("小克讲讲方案", speaker_name="张三")
    await settle(lambda: len(player.played) == 2 and m.status == "speaking")
    floor.on_speech_start()                      # 有人插话
    await settle(lambda: player.stops == 1)
    assert m.status == "listening"
    bot_line = [l for l in m.lines if l.who == "bot"][0]
    assert bot_line.cut
    floor.on_speech_end()
    player.auto = True
    await floor.on_utterance("等一下我先说", speaker_name="李四")
    await settle(lambda: len(brain.prompts) == 2 and m.status == "listening")
    assert "被人打断" in brain.prompts[1] and "[李四] 等一下我先说" in brain.prompts[1]
    await floor.stop()


async def test_short_noise_does_not_interrupt():
    m, brain, mouth, player, floor = await make(["我来说一下方案。"], auto=False)
    await floor.on_utterance("小克讲讲", speaker_name="张三")
    await settle(lambda: m.status == "speaking")
    floor.on_speech_start()
    await asyncio.sleep(0.01)
    floor.on_speech_end()                        # 只响了 0.01 秒，不到打断阈值
    await asyncio.sleep(0.1)
    assert player.stops == 0 and m.status == "speaking"
    player.finish_all()
    await settle(lambda: m.status == "listening")
    await floor.stop()


async def test_own_echo_is_dropped():
    m, brain, mouth, player, floor = await make(["我觉得还是先做登录吧，支付依赖它。"], auto=False)
    await floor.on_utterance("小克你说", speaker_name="张三")
    await settle(lambda: m.status == "speaking")
    floor.on_speech_start()                      # 麦克风收到了数字人自己的声音
    floor.on_speech_end()
    line = await floor.on_utterance("还是先做登录吧")
    assert line is None
    assert [l.who for l in m.lines] == ["human", "bot"]
    player.finish_all()
    await floor.stop()


async def test_similar_words_from_a_person_after_bot_finished_are_kept():
    m, brain, mouth, player, floor = await make(["先做登录吧。"])
    await floor.on_utterance("小克你说", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    floor.on_speech_start()                      # 数字人已经说完，这是真人在说
    floor.on_speech_end()
    line = await floor.on_utterance("对，先做登录吧")
    assert line is not None
    await floor.stop()


async def test_silence_nudges_brain_once():
    m, brain, mouth, player, floor = await make(["[静]", "那我总结一下。", "[静]"], nudge=0.08)
    await floor.on_utterance("大概就这些", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 2, timeout=1)
    assert "安静" in brain.prompts[1]
    await settle(lambda: player.texts == ["那我总结一下。"])
    await asyncio.sleep(0.25)
    assert len(brain.prompts) == 2               # 没人再说话就不再提示
    await floor.stop()


async def test_muted_listens_but_never_asks_brain_until_unmuted():
    m, brain, mouth, player, floor = await make(["好的。"])
    await floor.set_muted(True)
    await floor.on_utterance("小克在吗", speaker_name="张三")
    await asyncio.sleep(0.05)
    assert brain.prompts == [] and len(m.lines) == 1
    await floor.set_muted(False)
    await settle(lambda: player.played)
    assert "小克在吗" in brain.prompts[0]
    await floor.stop()


async def test_hush_stops_immediately():
    m, brain, mouth, player, floor = await make(["这是一段很长的话。还有第二句。"], auto=False)
    await floor.on_utterance("小克讲讲", speaker_name="张三")
    await settle(lambda: m.status == "speaking")
    await floor.hush()
    await settle(lambda: m.status == "listening")
    assert player.stops == 1
    assert any("主持人让你先停下" in c for c in floor.carry)
    await floor.stop()


async def test_say_speaks_without_brain():
    m, brain, mouth, player, floor = await make()
    floor.say("大家好，我是小克。")
    await settle(lambda: player.played and m.status == "listening")
    assert brain.prompts == [] and player.texts == ["大家好，", "我是小克。"]
    await floor.stop()


async def test_dispatch_runs_worker_and_reports_back():
    worker = FakeWorker(delay=0.02)
    m, brain, mouth, player, floor = await make(
        ["我查一下。[[派工 查资料:飞书会议机器人接口]]", "查到了，文档在开放平台第三章。"], worker=worker)
    await floor.on_utterance("小克查查飞书有没有机器人接口", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 2 and m.status == "listening" and len(player.played) == 3)
    assert player.texts == ["我查一下。", "查到了，", "文档在开放平台第三章。"]
    assert worker.ran == [("查资料", "飞书会议机器人接口")]
    assert m.tasks[0].status == "done" and m.board_task_id == m.tasks[0].id
    assert "后台任务完成" in brain.prompts[1] and "开放平台第三章" in brain.prompts[1]
    await floor.stop()


async def test_failed_task_is_reported():
    worker = FakeWorker(result=WorkResult(False, "搜不到"))
    m, brain, mouth, player, floor = await make(["[[派工 查代码:登录在哪]]"], worker=worker)
    await floor.on_utterance("查下登录在哪", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 2)
    assert m.tasks[0].status == "failed" and "没做成" in brain.prompts[1]
    await floor.stop()


async def test_duplicate_dispatch_is_ignored_while_running():
    worker = FakeWorker(delay=0.3)
    m, brain, mouth, player, floor = await make(
        ["[[派工 查资料:飞书机器人接口]]", "[[派工 查资料:飞书机器人接口文档]]"], worker=worker)
    await floor.on_utterance("查一下飞书接口", speaker_name="张三")
    await settle(lambda: len(m.tasks) == 1)
    await floor.on_utterance("快查", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 2 and m.status == "listening")
    assert len(m.tasks) == 1
    await floor.stop()


async def test_unknown_task_kind_rejected():
    m, brain, mouth, player, floor = await make()
    with pytest.raises(ValueError):
        floor.dispatch("唱歌", "来一首")
    await floor.stop()


async def test_stop_fails_running_tasks():
    worker = FakeWorker(delay=5)
    m, brain, mouth, player, floor = await make(worker=worker)
    floor.dispatch("查资料", "很慢的事")
    await asyncio.sleep(0.01)
    await floor.stop()
    assert m.tasks[0].status == "failed" and not brain.started
