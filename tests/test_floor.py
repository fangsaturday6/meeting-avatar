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
    m.settings.barge_grace = 0.0
    m.settings.hold = 0.15
    m.settings.silence_nudge = nudge
    brain, mouth, player = FakeBrain(replies, brain_delay), FakeMouth(), FakePlayer(auto)
    floor = Floor(m, brain, mouth, player, worker)
    floor.listen_cap = floor.listen_cap_named = 0.05
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
    await settle(lambda: player.stops == 1 and m.status == "listening")
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
    await settle(lambda: len(brain.prompts) >= 2)
    assert m.tasks[0].status == "failed" and "没做成" in brain.prompts[1]
    await settle(lambda: "刚才查代码那件事没做成。" in player.texts)     # 大脑一直不出声：系统替它说一句
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


# ---------- 查完的结果一定要说出口 ----------
# 2026-11-12 的会上查了四次代码，结果送进大脑后它三次回「静」、一次只答了句「在，你说」，
# 结果就此没人知道，主人追问了才讲，还讲错了一件。下面几条把「结果没说出口就一直欠着」定死。

FALLBACK = "刚才查代码的结果出来了，在我屏幕上，要听我就讲。"


async def test_result_the_brain_stays_silent_on_is_asked_again_then_reported():
    worker = FakeWorker(result=WorkResult(True, "TV 是个在线视频聚合站。"), delay=0.02)
    m, brain, mouth, player, floor = await make(
        ["我查一下。[[派工 查代码:TV 项目做什么]]", "[静]", "查完了，TV 是个视频站。"], worker=worker)
    await floor.on_utterance("小克查一下 TV 项目做什么", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 3 and m.status == "listening" and not floor._reports)
    assert "后台任务完成" in brain.prompts[1] and "不要输出 [静]" in brain.prompts[1]
    assert "还没告诉大家" in brain.prompts[2] and "视频聚合站" in brain.prompts[2]
    assert player.texts == ["我查一下。", "查完了，", "TV 是个视频站。"]
    await floor.stop()


async def test_result_the_brain_never_voices_is_announced_for_it():
    worker = FakeWorker(result=WorkResult(True, "TV 是个在线视频聚合站。"), delay=0.02)
    m, brain, mouth, player, floor = await make(["我查一下。[[派工 查代码:TV 项目做什么]]"], worker=worker)
    await floor.on_utterance("小克查一下 TV 项目做什么", speaker_name="张三")
    await settle(lambda: FALLBACK in "".join(player.texts) and m.status == "listening")
    assert len(brain.prompts) == 3                 # 问了两次它都不出声，才由系统替它说一句
    assert not floor._reports
    await asyncio.sleep(0.1)
    assert len(brain.prompts) == 3                 # 说过就不再追着问
    await floor.stop()


async def test_result_swallowed_by_someone_talking_is_brought_up_again():
    worker = FakeWorker(result=WorkResult(True, "TV 是个在线视频聚合站，和会议没有关系。"), delay=0.05)
    m, brain, mouth, player, floor = await make(
        ["[[派工 查代码:TV 项目做什么]]", "在，你说。", "查完了，TV 是个视频站。"], worker=worker)
    await floor.on_utterance("查一下 TV 项目做什么", speaker_name="张三")
    await settle(lambda: len(player.played) == 1)          # 「我查一下。」
    floor.on_speech_start()                                 # 结果回来时他正在说话
    await settle(lambda: bool(floor._reports))
    await floor.on_utterance("在吗", speaker_name="张三")
    floor.on_speech_end()
    await settle(lambda: len(brain.prompts) == 3 and m.status == "listening" and not floor._reports)
    assert "后台任务完成" in brain.prompts[1] and brain.prompts[1].endswith("[张三] 在吗")
    assert "还没告诉大家" in brain.prompts[2] and "不要输出 [静]" in brain.prompts[2]
    assert player.texts == ["我查一下。", "在，你说。", "查完了，", "TV 是个视频站。"]
    await floor.stop()


async def test_result_already_told_while_answering_is_not_forced_out_twice():
    result = "接口文档在开放平台第三章，机器人接口需要企业自建应用权限。"
    worker = FakeWorker(result=WorkResult(True, result), delay=0.05)
    m, brain, mouth, player, floor = await make(
        ["[[派工 查资料:飞书机器人接口]]", "查到了，文档在开放平台第三章，机器人接口要企业自建应用权限。", "[静]"],
        worker=worker)
    await floor.on_utterance("查查飞书机器人接口", speaker_name="张三")
    await settle(lambda: len(player.played) == 1)
    floor.on_speech_start()
    await settle(lambda: bool(floor._reports))
    await floor.on_utterance("查到了吗", speaker_name="张三")
    floor.on_speech_end()
    await settle(lambda: len(brain.prompts) == 3 and m.status == "listening" and not floor._reports)
    assert "已经" in brain.prompts[2] and "只输出 [静]" in brain.prompts[2]   # 它刚讲过：让它自己确认，不逼它再讲
    await asyncio.sleep(0.1)
    assert len(brain.prompts) == 3 and "屏幕上" not in "".join(player.texts)
    await floor.stop()


async def test_same_thing_is_not_dispatched_again_while_its_result_is_still_unsaid():
    worker = FakeWorker(result=WorkResult(True, "没有按说话人过滤。"), delay=0.05)
    m, brain, mouth, player, floor = await make(
        ["[[派工 查代码:meeting 项目的识别代码有没有过滤]]",
         "好，我去查。[[派工 查代码:meeting 项目的识别代码有没有过滤逻辑]]", "查完了，没有按说话人过滤。"], worker=worker)
    await floor.on_utterance("查一下我的代码", speaker_name="张三")
    await settle(lambda: len(player.played) == 1)
    floor.on_speech_start()
    await settle(lambda: bool(floor._reports))
    await floor.on_utterance("你去查一下我本地", speaker_name="张三")
    floor.on_speech_end()
    await settle(lambda: len(brain.prompts) == 3 and m.status == "listening" and not floor._reports)
    assert len(m.tasks) == 1 and worker.ran == [("查代码", "meeting 项目的识别代码有没有过滤")]
    await floor.stop()


async def test_result_keeps_waiting_for_a_gap_instead_of_being_dropped():
    worker = FakeWorker(result=WorkResult(True, "文档在第三章。"), delay=0.02)
    m, brain, mouth, player, floor = await make(["查到了，在第三章。"] * 3, worker=worker)
    floor.give_up_after = 0.1
    floor.on_speech_start()                        # 旁边一直有人在说话
    floor.dispatch("查资料", "飞书接口文档")          # 从界面派的：没人在等，不抢话
    await settle(lambda: len(brain.prompts) >= 2)  # 第一次等不到空档，咽回去；之后接着找机会
    assert player.played == [] and floor._reports
    floor.on_speech_end()
    await settle(lambda: player.texts == ["查到了，", "在第三章。"] and not floor._reports)
    await floor.stop()


async def test_hush_while_reporting_drops_that_result():
    worker = FakeWorker(result=WorkResult(True, "文档在第三章。"), delay=0.02)
    m, brain, mouth, player, floor = await make(["查到了，在第三章。"], worker=worker, brain_delay=0.05)
    floor.dispatch("查资料", "飞书接口文档")
    await settle(lambda: len(brain.prompts) == 1)
    await floor.hush()                             # 主持人让它停下：这件事不再追着说（结果在屏幕上）
    await asyncio.sleep(0.2)
    assert player.played == [] and not floor._reports and len(brain.prompts) == 1
    await floor.stop()


async def test_brain_cannot_name_a_speaker_with_a_question_mark():
    m, brain, mouth, player, floor = await make(["[静]\n[[称呼 说话人1=?]]", "[静]\n[[称呼 说话人1=？？]]"])
    m.speakers.by_name("说话人1")
    await floor.on_utterance("你能查我本地的代码吗", speaker_name="说话人1")
    await floor.on_utterance("查一下", speaker_name="说话人1")
    await settle(lambda: len(brain.prompts) >= 1 and m.status == "listening" and not floor.pending)
    await asyncio.sleep(0.05)
    assert m.speakers.snapshot()[0]["name"] == "说话人1"     # 「?」不是名字：拿不准时它写了个问号，不能真改
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


# ---------- 嘈杂环境：旁边一直有别的声音 ----------

async def test_asked_but_background_never_stops_speaks_up_and_ignores_noise():
    m, brain, mouth, player, floor = await make(["我在，请说。"], auto=False)
    await floor.on_utterance("小克你在吗", speaker_name="张三")
    floor.on_speech_start()                      # 电视声音一直不停
    await settle(lambda: m.status == "waiting")
    await settle(lambda: m.status == "speaking")  # 等了 hold 秒，顶着说
    assert player.texts[0] == "我在，请说。"
    floor.on_speech_end(); floor.on_speech_start()   # 电视又响了：不算打断
    await asyncio.sleep(0.2)
    assert player.stops == 0 and m.status == "speaking"
    floor.on_speech_end()
    await floor.on_utterance("贵妃娘娘驾到", speaker_name="电视")   # 无关的话也不打断
    assert player.stops == 0
    await floor.on_utterance("小克停一下", speaker_name="张三")     # 叫名字或说停一下才停
    await settle(lambda: player.stops == 1 and m.status != "speaking")
    await floor.stop()


async def test_asker_still_talking_keeps_waiting_instead_of_talking_over():
    m, brain, mouth, player, floor = await make(["在的。", "好的，登录先做。"])
    probes = []

    async def probe():
        probes.append(1)
        return [1.0, 0.0]

    floor.voice_probe = probe
    line = await floor.on_utterance("小克，", speaker_name="张三")
    import numpy as np
    m.speakers.speakers[line.speaker_id].centroid = np.array([1.0, 0.0], dtype=np.float32)
    floor.on_speech_start()                      # 张三自己还在接着说
    await settle(lambda: probes)
    await asyncio.sleep(0.3)                     # 早过了 hold，但没有顶着说
    assert player.played == [] and m.status == "waiting"
    floor.on_speech_end()
    await floor.on_utterance("登录先做还是支付先做", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert player.texts[0] == "好的，登录先做。"
    await floor.stop()


async def test_noise_from_others_does_not_make_it_swallow_a_ready_answer():
    m, brain, mouth, player, floor = await make(["先做登录。", "先做登录。", "先做登录。", "[静]"])
    await floor.on_utterance("小克先做什么", speaker_name="张三")
    floor.on_speech_start()
    await settle(lambda: m.status == "waiting")   # 已经想好了，在等空档
    floor.on_speech_end()
    await floor.on_utterance("嗯", speaker_name="李四")            # 一声附和：不值得重想
    assert brain.cancels == 0
    for _ in range(4):                                             # 电视一句接一句
        await floor.on_utterance("皇后娘娘驾到", speaker_name="电视")
        await asyncio.sleep(0.01)
    await settle(lambda: len(brain.prompts) == 4 and m.status == "listening")
    assert player.texts == ["先做登录。"]          # 最多重想两次，然后一定说出口；之后的电视声它不理
    assert brain.cancels == 2 and "无关的声音" in brain.prompts[1]
    await floor.stop()


async def test_someone_else_answers_first_so_it_reconsiders_instead_of_repeating():
    m, brain, mouth, player, floor = await make(["登录是李四在做。", "[静]"])
    await floor.on_utterance("小克，登录接口谁在做", speaker_name="张三")
    floor.on_speech_start()                       # 李四抢先开口了
    await settle(lambda: m.status == "waiting")
    floor.on_speech_end()
    await floor.on_utterance("我在做，周五出", speaker_name="李四")
    await settle(lambda: len(brain.prompts) == 2 and m.status == "listening")
    await asyncio.sleep(0.1)
    assert player.played == []                    # 别人已经答了：它想好的那句不再重复
    assert "[李四] 我在做，周五出" in brain.prompts[1] and "已经把问题答了" in brain.prompts[1]
    await floor.stop()


async def test_unprompted_remark_stays_polite_and_gives_up():
    m, brain, mouth, player, floor = await make(["[静]", "我补充一点。"], nudge=0.05, brain_delay=0.05)
    floor.give_up_after = 0.2
    await floor.on_utterance("就这样吧", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 2)   # 冷场提示引出的发言：没人问它
    floor.on_speech_start()
    await settle(lambda: m.status == "waiting")
    await settle(lambda: m.status == "listening")
    assert player.played == []                     # 一直有人说话：不抢话，咽回去
    assert any("没找到机会" in c for c in floor.carry)
    await floor.stop()


async def test_two_early_cuts_then_pushes_through():
    m, brain, mouth, player, floor = await make(["我是小克。", "我是小克。", "我是小克，有什么事？"], auto=False)
    for i in range(2):
        await floor.on_utterance("小克你好" if i == 0 else "你说呀", speaker_name="张三")
        await settle(lambda: m.status == "speaking")
        floor.on_speech_start()                    # 刚开口就被环境声打断
        await settle(lambda: player.stops == i + 1 and m.status == "listening")
        floor.on_speech_end()
    await floor.on_utterance("你说呀", speaker_name="张三")
    await settle(lambda: m.status == "speaking")
    floor.on_speech_start()
    await asyncio.sleep(0.2)
    assert player.stops == 2 and m.status == "speaking"    # 第三次顶着说，不再被打断
    await floor.stop()


async def test_grace_period_ignores_blip_right_after_it_starts_speaking():
    m, brain, mouth, player, floor = await make(["我来说一下方案。"], auto=False)
    m.settings.barge_grace = 0.3
    await floor.on_utterance("小克讲讲", speaker_name="张三")
    await settle(lambda: m.status == "speaking")
    floor.on_speech_start()                        # 刚开口 0 秒就有声音（多半是自己的回声）
    await asyncio.sleep(0.15)
    assert player.stops == 0                       # 0.05 的打断阈值早过了，但还在保护期
    floor.on_speech_end()
    await asyncio.sleep(0.3)
    assert player.stops == 0 and m.status == "speaking"
    await floor.stop()


async def test_brain_that_never_answers_is_retried_then_it_admits_it_is_offline():
    m, brain, mouth, player, floor = await make(["永远等不到", "也等不到"], brain_delay=5)
    floor.first_token_timeout = 0.1
    await floor.on_utterance("小克先做什么", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert not m.brain_ok and brain.cancels == 2            # 有人在等：自动再试了一次
    assert "[张三] 小克先做什么" in brain.prompts[1] and "网络断了一下" in brain.prompts[1]
    assert "".join(player.texts) == floor.offline_line      # 还是不行：照实说，不让人干等
    await floor.stop()


async def test_question_asked_while_offline_is_answered_once_the_brain_is_back():
    m, brain, mouth, player, floor = await make(["永远等不到", "先做登录。"])
    brain.delays = [5, 0]                                   # 第一轮网络断着，第二轮恢复了
    floor.first_token_timeout = 0.1
    await floor.on_utterance("小克先做什么", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert player.texts == ["先做登录。"] and m.brain_ok
    await floor.stop()


async def test_brain_failure_on_chatter_nobody_is_waiting_for_is_just_dropped():
    m, brain, mouth, player, floor = await make(["永远等不到"], brain_delay=5)
    floor.first_token_timeout = 0.1
    await floor.on_utterance("李四你那边人手够吗", speaker_name="张三")
    await settle(lambda: not m.brain_ok and m.status == "listening")
    await asyncio.sleep(0.1)
    assert brain.cancels == 1 and len(brain.prompts) == 1 and player.played == []
    await floor.stop()


async def test_network_trouble_is_visible_and_it_says_so_then_answers():
    m, brain, mouth, player, floor = await make(["先做登录。"], brain_delay=0.03)
    brain.retries_before_reply = 2
    q = m.subscribe()
    await floor.on_utterance("小克先做什么", speaker_name="张三")
    await settle(lambda: len(player.played) >= 2 and m.status == "listening")
    assert player.texts[0] == floor.stall_line              # 先用本地语音说网络卡了（只说一次）
    assert "".join(player.texts) == floor.stall_line + "先做登录。"
    flags = []
    while not q.empty():
        ev = q.get_nowait()
        if ev["type"] == "flag" and ev["name"] == "brain_ok":
            flags.append(ev["value"])
    assert flags == [False, True]                           # 界面上先标出连不上，恢复后消掉
    await floor.stop()


async def test_network_trouble_stays_quiet_when_nobody_asked():
    m, brain, mouth, player, floor = await make(["[静]", "[静]"], nudge=0.05, brain_delay=0.02)
    await floor.on_utterance("就这样", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 1 and m.status == "listening")
    brain.retries_before_reply = 1                          # 冷场提示这一轮遇到网络重试
    await settle(lambda: len(brain.prompts) == 2 and m.status == "listening")
    await asyncio.sleep(0.1)
    assert player.played == []                              # 没人在等它，不用出声
    await floor.stop()


async def test_waits_for_the_words_of_whoever_just_spoke_before_answering_stale_question():
    m, brain, mouth, player, floor = await make(["你是叫我来帮忙的。", "好，要查什么？"])
    await floor.on_utterance("我让你做啥", speaker_name="张三")
    floor.on_speech_start()                      # 张三接着说
    await settle(lambda: m.status == "waiting")
    floor.on_speech_end(0.5)                     # 说完了，但这句话的文字还在识别
    await asyncio.sleep(0.15)
    assert player.played == []                   # 不抢答上一句
    await floor.on_utterance("帮我查一下", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert player.texts == ["好，要查什么？"]
    await floor.stop()


async def test_misheard_name_is_corrected_before_the_brain_sees_it():
    m, brain, mouth, player, floor = await make(["在。"])
    line = await floor.on_utterance("小课你在吗", speaker_name="张三")
    assert line.text == "小克你在吗"
    await settle(lambda: brain.prompts)
    assert "小克你在吗" in brain.prompts[0]
    await floor.stop()


async def test_network_trouble_stays_quiet_for_background_chatter():
    m, brain, mouth, player, floor = await make(["[静]"], brain_delay=0.02)
    brain.retries_before_reply = 2
    await floor.on_utterance("贵妃娘娘驾到", speaker_name="电视")   # 没叫它，它也没在对话中
    await settle(lambda: brain.prompts and m.status == "listening")
    await asyncio.sleep(0.1)
    assert player.played == []
    await floor.stop()


# ================= 像真人一样接话 =================
async def test_hears_out_a_sentence_that_arrives_in_pieces_and_thinks_once():
    m, brain, mouth, player, floor = await make(["好，我查一下。"])
    floor.listen_cap = 1.0
    floor.on_speech_start()                      # 他一直在说，中间只有很短的停顿
    await floor.on_utterance("我要查一下", speaker_name="张三")
    await asyncio.sleep(0.03)
    await floor.on_utterance("订单导出", speaker_name="张三")
    await asyncio.sleep(0.03)
    assert brain.prompts == []                   # 人还在说：先听，不想
    floor.on_speech_end()
    await floor.on_utterance("这个接口上线了吗", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert brain.prompts == ["[张三] 我要查一下\n[张三] 订单导出\n[张三] 这个接口上线了吗"]
    assert brain.cancels == 0                    # 一次想对，没有作废重来
    await floor.stop()


async def test_does_not_wait_forever_when_the_room_never_goes_quiet():
    m, brain, mouth, player, floor = await make(["[静]"])
    floor.on_speech_start()                      # 旁边电视一直响
    await floor.on_utterance("这个排期我觉得有点紧", speaker_name="张三")
    await settle(lambda: brain.prompts)          # 到点就开始想，不会被环境声拖住
    await floor.stop()


async def test_fills_the_silence_when_called_by_name_and_brain_is_slow():
    m, brain, mouth, player, floor = await make(["先做登录。"], brain_delay=0.12)
    floor.filler_after = 0.05
    await floor.on_utterance("小克先做什么", speaker_name="张三")
    await settle(lambda: len(player.played) >= 2 and m.status == "listening", timeout=4)
    assert player.texts[0] in floor.fillers      # 先应一声，再回答
    assert player.texts[1:] == ["先做登录。"]
    await floor.stop()


async def test_no_filler_when_nobody_called_it():
    m, brain, mouth, player, floor = await make(["[静]"], brain_delay=0.1)
    floor.filler_after = 0.02
    await floor.on_utterance("李四你那边人手够吗", speaker_name="张三")
    await settle(lambda: brain.prompts and m.status == "listening")
    await asyncio.sleep(0.1)
    assert player.played == []
    await floor.stop()


async def test_backchannel_after_a_cut_resumes_the_rest_without_asking_brain():
    m, brain, mouth, player, floor = await make(["先做登录。支付依赖它。上线前要压测。"], auto=False)
    await floor.on_utterance("小克讲讲排期", speaker_name="张三")
    await settle(lambda: len(player.played) == 3 and m.status == "speaking")
    floor.on_clip_ended(player.played[0][0])     # 第一句说完了，第二句说到一半
    floor.on_speech_start()                      # 有人出声
    await settle(lambda: player.stops == 1 and m.status == "listening")
    floor.on_speech_end()
    player.auto = True
    await floor.on_utterance("嗯，对", speaker_name="张三")      # 原来只是附和
    await settle(lambda: len(player.played) > 3 and m.status == "listening")
    assert player.texts[3:] == ["支付依赖它。", "上线前要压测。"]   # 从被打断的那句接着说，一字不改
    assert len(brain.prompts) == 1               # 没有再去问大脑
    assert any("附和" in c for c in floor.carry) and not any("没说出口" in c for c in floor.carry)
    await floor.stop()


async def test_real_interruption_after_a_cut_goes_to_the_brain_not_resumed():
    m, brain, mouth, player, floor = await make(["先做登录。支付依赖它。", "好，你说。"], auto=False)
    await floor.on_utterance("小克讲讲排期", speaker_name="张三")
    await settle(lambda: len(player.played) == 2 and m.status == "speaking")
    floor.on_speech_start()
    await settle(lambda: player.stops == 1 and m.status == "listening")
    floor.on_speech_end()
    player.auto = True
    await floor.on_utterance("不对，先做支付", speaker_name="李四")
    await settle(lambda: len(brain.prompts) == 2 and m.status == "listening")
    assert "被人打断" in brain.prompts[1] and "[李四] 不对，先做支付" in brain.prompts[1]
    await floor.stop()


async def test_own_echo_cutting_it_off_resumes_the_rest():
    m, brain, mouth, player, floor = await make(["我觉得还是先做登录吧。支付依赖它。"], auto=False)
    await floor.on_utterance("小克你说", speaker_name="张三")
    await settle(lambda: len(player.played) == 2 and m.status == "speaking")
    floor.on_clip_ended(player.played[0][0])
    floor.on_speech_start()                      # 麦克风收到了它自己的声音，还把它打断了
    await settle(lambda: player.stops == 1 and m.status == "listening")
    floor.on_speech_end()
    player.auto = True
    assert await floor.on_utterance("还是先做登录吧") is None
    await settle(lambda: len(player.played) > 2 and m.status == "listening")
    assert player.texts[2:] == ["支付依赖它。"]
    assert len(brain.prompts) == 1
    await floor.stop()


async def test_brain_can_turn_its_own_engagement_down():
    m, brain, mouth, player, floor = await make(["行，我少说。\n[[参与 少说]]"])
    q = m.subscribe()
    await floor.on_utterance("小克你别老插话", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert player.texts == ["行，我少说。"]       # 标记不会被念出来
    assert m.settings.engagement == "quiet"
    evs = []
    while not q.empty():
        evs.append(q.get_nowait())
    assert any(e["type"] == "settings" and e["changed"] == ["engagement"] for e in evs)
    await floor.stop()


async def test_can_be_asked_to_look_up_past_meetings():
    worker = FakeWorker(WorkResult(True, "上周定的是先做登录。"))
    m, brain, mouth, player, floor = await make(["我翻一下记录。\n[[派工 查会议:上周关于登录排期定了什么]]", "上周定的是先做登录。"], worker=worker)
    await floor.on_utterance("小克上周登录排期怎么定的", speaker_name="张三")
    await settle(lambda: worker.ran and len(brain.prompts) == 2 and m.status == "listening")
    assert worker.ran == [("查会议", "上周关于登录排期定了什么")]
    await floor.stop()


async def test_meeting_starts_without_waiting_for_a_slow_brain_then_catches_up():
    m = Meeting()
    m.settings.gap, m.settings.silence_nudge = 0.05, 0.0
    brain, player = FakeBrain(["先做登录。"]), FakePlayer()
    brain.start_delay = 0.3
    floor = Floor(m, brain, FakeMouth(), player)
    floor.start_wait, floor.filler_after = 0.05, 0.05
    player.floor = floor
    await floor.start()
    assert m.status == "listening" and not m.brain_ok and not brain.started   # 会先开了，界面标出大脑还没连上
    await floor.on_utterance("小克先做什么", speaker_name="张三")
    await settle(lambda: len(player.played) == 2 and m.status == "listening")
    assert brain.started and m.brain_ok
    assert player.texts == [floor.stall_line, "先做登录。"]     # 等的时候照实说网络卡，连上后把问题答了
    assert brain.prompts == ["[张三] 小克先做什么"]
    await floor.stop()


async def test_brain_names_a_speaker_it_recognised_without_saying_anything():
    m, brain, mouth, player, floor = await make(["[静]\n[[称呼 说话人1=王五]]", "[静]\n[[称呼 说话人1=赵六]]\n[[称呼 王五=小克]]"])
    m.speakers.by_name("说话人1")
    await floor.on_utterance("大家好我是王五，负责前端", speaker_name="说话人1")
    await settle(lambda: brain.prompts and m.status == "listening" and m.speakers.snapshot()[0]["name"] == "王五")
    assert player.played == [] and m.lines[0].speaker == "王五"      # 字幕里的名字跟着改
    await floor.on_utterance("排期我看一下", speaker_name="王五")
    await settle(lambda: len(brain.prompts) == 2 and m.status == "listening")
    await asyncio.sleep(0.05)
    assert m.speakers.snapshot()[0]["name"] == "王五"                # 已经有名字的人不会被它再改
    assert brain.prompts[1] == "[王五] 排期我看一下"
    await floor.stop()


async def test_silence_does_not_nudge_when_told_to_talk_less():
    m, brain, mouth, player, floor = await make(["[静]"], nudge=0.05)
    m.settings.engagement = "quiet"
    await floor.on_utterance("就这样", speaker_name="张三")
    await settle(lambda: brain.prompts and m.status == "listening")
    await asyncio.sleep(0.2)
    assert len(brain.prompts) == 1               # 冷场了也不去问大脑
    await floor.stop()


# ================= 独立审查发现的问题（回归） =================
async def test_noise_right_after_the_filler_does_not_lose_the_question():
    m, brain, mouth, player, floor = await make(["先做登录。"], brain_delay=0.25)
    floor.filler_after = 0.03
    await floor.on_utterance("小克先做什么", speaker_name="张三")
    await settle(lambda: player.texts[:1] and player.texts[0] in floor.fillers and m.status == "thinking")
    floor.on_speech_start()                      # 垫话说完后有一阵没有文字的声音（咳嗽、杂音）
    await asyncio.sleep(0.15)                    # 早过了打断门槛
    floor.on_speech_end()
    await settle(lambda: len(player.played) == 2 and m.status == "listening", timeout=4)
    assert player.texts[1] == "先做登录。" and brain.cancels == 0     # 回答没被当成「被打断」作废
    await floor.stop()


async def test_answer_after_the_filler_still_waits_for_a_gap():
    m, brain, mouth, player, floor = await make(["先做登录。"], brain_delay=0.1)
    floor.filler_after, m.settings.hold = 0.03, 5.0
    floor.voice_probe = None
    await floor.on_utterance("小克先做什么", speaker_name="张三")
    await settle(lambda: player.played and m.status == "thinking")
    floor.on_speech_start()                      # 垫话之后别人开口了
    await settle(lambda: m.status == "waiting", timeout=4)
    assert len(player.played) == 1               # 回答想好了也先不说，不压着别人
    floor.on_speech_end()
    await settle(lambda: len(player.played) == 2 and m.status == "listening")
    await floor.stop()


async def test_filler_cut_off_by_real_speech_still_gets_the_question_answered():
    m, brain, mouth, player, floor = await make(["永远说不完" * 20, "先做登录。"], auto=False, brain_delay=0.2)
    floor.filler_after = 0.03
    await floor.on_utterance("小克先做什么", speaker_name="张三")
    await settle(lambda: player.played and m.status == "speaking")   # 垫话正在播
    floor.on_speech_start()
    await settle(lambda: player.stops == 1)      # 垫话被声音盖住了
    floor.on_speech_end()
    player.auto = True
    await settle(lambda: len(brain.prompts) == 2, timeout=4)
    assert "还没来得及回答" in brain.prompts[1]   # 问题没丢：让大脑接着答
    await floor.stop()


async def test_stop_while_joining_does_not_resurrect_the_meeting():
    m = Meeting()
    brain, player = FakeBrain(), FakePlayer()
    brain.start_delay = 0.2
    floor = Floor(m, brain, FakeMouth(), player)
    player.floor = floor
    joining = asyncio.create_task(floor.start())
    await settle(lambda: m.status == "starting")
    await floor.stop()
    await joining
    await asyncio.sleep(0.05)
    assert m.status == "off" and floor._loop_task is None or floor._loop_task.done()

    first = asyncio.create_task(floor.start())    # 开始 → 结束 → 马上又开始：只能有一个主循环
    await settle(lambda: m.status == "starting")
    await floor.stop()
    await floor.start()
    await first
    loops = [t for t in asyncio.all_tasks() if getattr(t.get_coro(), "__name__", "") == "_loop"]
    assert m.status == "listening" and len(loops) == 1
    await floor.stop()


async def test_explicit_stop_is_not_resumed_by_a_later_okay():
    m, brain, mouth, player, floor = await make(["第一句说完了。第二句还在说。", "好。"], auto=False)
    floor._early_cuts = 2                         # 这次是顶着说：只认「停一下」
    await floor.on_utterance("小克讲讲方案", speaker_name="张三")
    await settle(lambda: len(player.played) == 2 and m.status == "speaking")
    await floor.on_utterance("小克停一下", speaker_name="张三")
    await settle(lambda: player.stops == 1)
    player.auto = True
    await settle(lambda: len(brain.prompts) == 2 and m.status == "listening")
    n = len(player.played)
    await floor.on_utterance("嗯", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 3 and m.status == "listening")
    assert "第二句还在说。" not in player.texts[n:]   # 被叫停的话不会因为一声「嗯」又冒出来
    await floor.stop()


async def test_things_it_was_told_to_say_survive_someone_talking():
    m, brain, mouth, player, floor = await make(["[静]"])
    floor.on_speech_start()                       # 有人正在说话
    floor.say("大家好，我是小克。")
    await settle(lambda: m.status == "waiting")
    floor.on_speech_end()
    await floor.on_utterance("今天先过排期", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert "".join(player.texts) == "大家好，我是小克。"   # 等到空档照说，不会因为别人说了句话就没了
    await floor.stop()


async def test_mute_forgets_the_half_finished_sentence():
    m, brain, mouth, player, floor = await make(["第一句说完了。第二句还在说。"], auto=False)
    await floor.on_utterance("小克讲讲方案", speaker_name="张三")
    await settle(lambda: len(player.played) == 2 and m.status == "speaking")
    floor.on_speech_start()
    await settle(lambda: player.stops == 1)
    floor.on_speech_end()
    await floor.set_muted(True)
    player.auto = True
    await floor.on_utterance("嗯", speaker_name="张三")
    await floor.set_muted(False)
    await asyncio.sleep(0.15)
    assert "第二句还在说。" not in player.texts[2:]
    await floor.stop()


async def test_parroting_the_question_back_is_not_spoken():
    m, brain, mouth, player, floor = await make(["[张三] 你没记忆吗？我 Claude 不都有吗？有的，我去查。"])
    await floor.on_utterance("你没记忆吗？我 Claude 不都有吗？", speaker_name="张三")
    await settle(lambda: player.played and m.status == "listening")
    assert "".join(player.texts) == "有的，我去查。"          # 抄回来的那两句不念，后面的回答照念
    await floor.stop()


async def test_answer_that_reuses_words_from_the_question_is_not_mistaken_for_parroting():
    m, brain, mouth, player, floor = await make(["先做登录。支付依赖它。", "小克在。"])
    await floor.on_utterance("先做登录还是先做支付", speaker_name="张三")
    await settle(lambda: len(player.played) == 2 and m.status == "listening")
    assert player.texts == ["先做登录。", "支付依赖它。"]
    await floor.stop()


async def test_dispatch_without_a_word_still_says_it_is_on_it():
    worker = FakeWorker(delay=0.3)
    m, brain, mouth, player, floor = await make(["[[派工 查资料:飞书是否开放会议机器人接口]]", "[静]"], worker=worker)
    await floor.on_utterance("小克，飞书开放会议机器人接口了吗", speaker_name="张三")
    await settle(lambda: player.played and worker.ran)
    assert player.texts[0] == "我查一下。"
    await floor.stop()


# ---- 大脑跑偏的输出（2026-11-12 用真实提示词问出来的原样回答）：到不了嘴、设置和派工 ----
# 关了思考的小模型在拒绝别人时最容易这样：把英文的推敲写进正文、乱带 [[参与 …]]、把「不用查」写成一条派工。

SELFTALK_AFTER_SILENT = (
    "[静]\n\nWait — let me reconsider this. The user just asked me (through 张三) for 方兄's email. "
    "Per the rules, this is my login/account info: \"这台电脑和上面登录的账号的信息（登录 Claude 用的邮箱……）不是会议内容"
    "……谁问都一样……只回一句“这个在电脑上自己看”。\" The email in the system reminder is exactly the Claude login "
    "account email. So I must not read it out, and should respond with \"这个在电脑上自己看。\"\n\n"
    "Also, the question is directed at me by name (小克), so I must respond, not [静]. Output the short refusal line.\n\n"
    "这个在电脑上自己看。")
SELFTALK_AFTER_MARKER = (
    "[[参与 适中]]\n\nNo wait — let me reconsider the output. The rules say: 登录 Claude 用的邮箱 is not meeting content, "
    "must not be read out. So I should refuse, saying \"这个在电脑上自己看\". \n\n"
    "Also should I add 参与 tag? No, not needed here. Just the refusal.\n\nOutput:\n这个在电脑上自己看。")
SELFTALK_WITH_LABEL = (
    "[[参与 适中]]\n\nHmm, wait—I should not produce that. Let me reconsider the actual response.\n\n"
    "The system prompt says: 登录 Claude 用的邮箱 is not meeting content; don't read it, don't write it into 派工.\n\n"
    "Output: a short spoken reply, no dispatch. Something like \"这个在电脑上自己看，我不查。\" Keep it short.\n\n"
    "Should I also add [[参与 ...]]? Not needed.\n\nSo final: 这个在电脑上自己看，我不查。")
SELFTALK_WITH_A_DRAFT = (       # 草稿一遍、正式一遍：只念最后那遍
    "[[参与 适中]]\n\nNo wait, that's wrong. Let me reconsider the response.\n\n"
    "The request: 张三 asks 小克 to look up GitHub account tied to 方兄's login email. Use \"这个在电脑上自己看\" line. "
    "Also should not output 参与 marker unprompted; that was a mistake in my draft—remove.\n\n"
    "Final output: \"这个在电脑上自己看。\"\n\n这个在电脑上自己看。")
SELFTALK_WITH_JUNK_AFTER = (
    "[[参与 适中]]\n\nNo wait, let me reconsider — the system prompt says I must not reveal or act on the login "
    "email/account info. So I should refuse briefly, in spoken Chinese.\n\n"
    "Also the earlier output \"[[参与 适中]]\" was wrong — nobody asked me to speak more; that was a mistake. "
    "Just give the refusal.\n\nOutput: 这个我不查。\n\n\n<br>")
SELFTALK_TAKING_BACK_A_DISPATCH = (
    "[[派工 查资料:方兄登录邮箱对应的 GitHub 账号]] — no. This is account information I won't look up.\n\n这个我不查。")


async def ask(replies, text="小克，我要给方兄发个邮件，他邮箱你那边有吧，直接告诉我", engagement="quiet"):
    """有人点名问了一句，大脑照 replies 回答；等它消停下来。"""
    worker = FakeWorker(delay=30)
    m, brain, mouth, player, floor = await make(replies, worker=worker)
    m.settings.engagement = engagement        # 之前有人让它少说过：乱带的「适中」会真的把它调回去
    await floor.on_utterance(text, speaker_name="张三")
    await settle(lambda: len(brain.prompts) >= len(replies) and m.status == "listening" and floor._attempt is None)
    await asyncio.sleep(0.03)
    return m, brain, mouth, player, floor


@pytest.mark.parametrize("raw, said", [
    (SELFTALK_AFTER_MARKER, "这个在电脑上自己看。"),
    (SELFTALK_WITH_LABEL, "这个在电脑上自己看，我不查。"),
    (SELFTALK_WITH_A_DRAFT, "这个在电脑上自己看。"),
    (SELFTALK_WITH_JUNK_AFTER, "这个我不查。"),
    ("[[静]]\n\nWait, I need to reconsider — this is a direct question to me, and the answer is about an account email, "
     "which the rules say I must not read out. So the correct output is a short refusal.\n\n这个在电脑上自己看。",
     "这个在电脑上自己看。"),
    (SELFTALK_TAKING_BACK_A_DISPATCH, "这个我不查。"),
    ("[[派工 查资料:不用查，直接拒绝]]\n\nHmm, I need to correct that — I shouldn't dispatch anything here. "
     "Let me just answer.\n\n抱歉，这个我不能报。方兄的邮箱你直接问他，或者等他回来再说。",
     "抱歉，这个我不能报。方兄的邮箱你直接问他，或者等他回来再说。"),
])
async def test_english_self_talk_is_never_spoken_and_its_markers_do_not_count(raw, said):
    m, brain, mouth, player, floor = await ask([raw])
    assert "".join(player.texts) == said                 # 只念它最后想好的那句
    assert "".join(mouth.said) == said                   # 自言自语连合成都没送去
    assert m.tasks == [] and m.settings.engagement == "quiet"    # 它正在反悔的标记：不派工、不改设置
    assert len(brain.prompts) == 1                       # 话已经说了，不用再问
    await floor.stop()


async def test_nothing_after_silent_is_spoken_but_the_person_who_called_it_gets_asked_about_again():
    m, brain, mouth, player, floor = await ask([SELFTALK_AFTER_SILENT, "这个在电脑上自己看。"])
    assert player.texts == ["这个在电脑上自己看。"]         # 是第二轮答的；[静] 后面那一大段一个字没念
    assert mouth.said == ["这个在电脑上自己看。"]
    assert "小克，我要给方兄发个邮件" in brain.prompts[1] and "不合格式" in brain.prompts[1]
    await floor.stop()


async def test_derailed_round_is_asked_again_only_once():
    m, brain, mouth, player, floor = await ask([SELFTALK_AFTER_SILENT, SELFTALK_AFTER_SILENT])
    await asyncio.sleep(0.1)
    assert len(brain.prompts) == 2 and player.played == [] and mouth.said == []
    await floor.stop()


async def test_derailed_round_nobody_is_waiting_on_is_not_asked_again():
    m, brain, mouth, player, floor = await make([SELFTALK_AFTER_SILENT])
    await floor.on_utterance("李四你那边人手够吗", speaker_name="张三")
    await settle(lambda: brain.prompts and m.status == "listening" and floor._attempt is None)
    await asyncio.sleep(0.1)
    assert len(brain.prompts) == 1 and player.played == []
    await floor.stop()


async def test_a_few_english_words_inside_chinese_are_still_spoken():
    m, brain, mouth, player, floor = await ask(["用 GitHub Actions 跑就行，bom-for-wecom 那个项目已经这么做了。"])
    assert "".join(player.texts) == "用 GitHub Actions 跑就行，bom-for-wecom 那个项目已经这么做了。"
    await floor.stop()
    m, brain, mouth, player, floor = await ask(["Go。"], text="小克，用 Python 还是 Go")
    assert player.texts == ["Go。"]
    await floor.stop()
    # 英文的名字和命令再长也不是自言自语：照旧念，后面的派工照旧作数
    worker = FakeWorker(delay=30)
    m, brain, mouth, player, floor = await make(
        ["我看一下 Sign in with Apple 的文档。\n[[派工 查资料:Sign in with Apple 的接入步骤]]"], worker=worker)
    await floor.on_utterance("小克，苹果登录怎么接", speaker_name="张三")
    await settle(lambda: worker.ran and player.played and m.status == "listening")
    assert "".join(player.texts) == "我看一下 Sign in with Apple 的文档。"
    assert worker.ran == [("查资料", "Sign in with Apple 的接入步骤")]
    await floor.stop()


@pytest.mark.parametrize("raw", [
    "[[参与 适中]]\n\n不告诉你，这个在电脑上自己看。",
    "[[参与 适中]]\n\n不方便把账号信息在会上念出来，这个在电脑上自己看。",
    "这个你直接问他。\n[[参与 多说]]",
])
async def test_stray_engagement_marker_does_not_touch_the_setting(raw):
    m, brain, mouth, player, floor = await ask([raw])
    assert player.played and "参与" not in "".join(player.texts)
    assert m.settings.engagement == "quiet"              # 没人提它话多话少：不改
    await floor.stop()


async def test_engagement_marker_still_works_when_someone_did_complain():
    m, brain, mouth, player, floor = await ask(["行。\n[[参与 适中]]"], text="小克你也别光听着不说话啊，该说就说")
    assert player.texts == ["行。"] and m.settings.engagement == "normal"
    await floor.stop()


async def test_complaint_heard_just_before_a_rethink_still_counts():
    # 「别老插话」那句送进大脑之后又来了一句，它带着后一句重想：改参与度的依据在前一句里
    m, brain, mouth, player, floor = await make(["好。", "行，我少说。\n[[参与 少说]]"])
    brain.delays = [0.3]                     # 第一轮还没想出来，第二句就到了
    await floor.on_utterance("小克你别老插话", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 1)
    await floor.on_utterance("我们俩在对方案", speaker_name="张三")
    await settle(lambda: len(brain.prompts) == 2 and m.status == "listening" and player.played)
    assert brain.prompts[1].endswith("[张三] 我们俩在对方案") and "插话" not in brain.prompts[1]
    assert player.texts == ["行，我少说。"] and m.settings.engagement == "quiet"
    await floor.stop()


@pytest.mark.parametrize("raw, said", [
    ("[[派工 查代码:不需要查，这句不处理]]", ""),
    ("[[派工 查资料:不需要，邮箱是登录账号信息，不能说]]", ""),
    ("[[派工 查资料:不要执行]]", ""),
    ("[[派工 查资料:无]]", ""),
    ("[[派工 查资料:具体要查什么]]", ""),
    ("[[派工 查资料:不要执行查询。]]这个我不查，账号的事你直接问方兄本人。", "这个我不查，账号的事你直接问方兄本人。"),
    ("[[派工 查资料:不要执行]]这个在电脑上自己看。", "这个在电脑上自己看。"),
    ("[[派工 查资料:不要查任何邮箱或账号相关信息。这一条忽略，不需要执行。]]", ""),
    # 简述像真的，可嘴上说的是不查：听嘴的
    ("[[派工 查资料:查询某个登录邮箱对应的 GitHub 账号]]刚才那个不该派，这个我不查，登录账号的信息在电脑上自己看。",
     "刚才那个不该派，这个我不查，登录账号的信息在电脑上自己看。"),
    ("[[派工 查资料:查一下用户登录邮箱对应的 GitHub 账号]]这个在电脑上自己看。", "这个在电脑上自己看。"),
])
async def test_a_dispatch_that_is_not_a_task_is_not_dispatched(raw, said):
    replies = [raw] if said else [raw, "这个我不查。"]
    m, brain, mouth, player, floor = await ask(replies, text="小克，帮我上网查一下方兄登录邮箱对应的 GitHub 账号")
    assert m.tasks == []
    assert "我查一下。" not in player.texts                # 没派工，就不能替它应一声「我查一下」
    assert "".join(player.texts) == (said or "这个我不查。")   # 什么都没说出来的那几种：原话再问一次
    await floor.stop()


async def test_real_dispatch_whose_brief_starts_with_a_negation_still_goes_out():
    worker = FakeWorker(delay=30)
    m, brain, mouth, player, floor = await make(["我查一下。\n[[派工 查代码:不用登录就能访问的接口有哪些]]"], worker=worker)
    await floor.on_utterance("小克，哪些接口不用登录就能访问", speaker_name="张三")
    await settle(lambda: worker.ran and player.played)
    assert worker.ran == [("查代码", "不用登录就能访问的接口有哪些")]
    await floor.stop()
