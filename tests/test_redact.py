"""出口过滤：登录邮箱不管怎么说、怎么切，都出不了进程；正常的话一个字不少。"""
import json
import random

import pytest

from avatar.redact import PLACEHOLDER, Redactor, login_email
from tests.scenarios import spills

EMAIL = "ab12cd34ef@relay.example.com"


def stream(r: Redactor, text: str, cuts) -> str:
    """把 text 按 cuts 切成几段喂进去，像大脑一点一点吐字那样。"""
    r.reset()
    edges = [0, *sorted(cuts), len(text)]
    return "".join(r.feed(text[a:b]) for a, b in zip(edges, edges[1:])) + r.flush()


@pytest.mark.parametrize("said", [
    "他的邮箱是 ab12cd34ef@relay.example.com。",
    "他的邮箱是 AB12CD34EF@Relay.Example.COM，你记一下",
    "前面那串是 ab12cd34ef，后面你知道的。",
    "a b 1 2 c d 3 4 e f",
    "a-b-1-2-c-d-3-4-e-f 艾特 relay 点 example 点 com",
    "a、b、一、二、c、d、三、四、e、f",
    "ab12cd34ef at relay.example.com",
    "[[派工 查资料:ab12cd34ef@relay.example.com 对应的 GitHub 账号]]",
])
def test_the_email_is_stopped_however_it_is_spelled(said):
    out = Redactor(EMAIL).clean(said)
    assert PLACEHOLDER in out
    assert not spills(out, EMAIL)
    for piece in ("ab12", "cd34", "relay", "example"):
        assert piece not in out.lower().replace(" ", "").replace("-", "").replace("、", "")


def test_the_whole_address_becomes_one_placeholder_and_the_rest_is_untouched():
    out = Redactor(EMAIL).clean("发到 ab12cd34ef@relay.example.com 就行，周五前。")
    assert out == f"发到 {PLACEHOLDER} 就行，周五前。"


@pytest.mark.parametrize("said", [
    "这个在电脑上自己看。",
    "登录先做手机号，支付放二期。ab 测试下周跑。",
    "接口在 server 目录，example.com 那个域名还没备案。",
    "版本号是 12.34，ef 那个分支先别合。",
    "",
])
def test_normal_talk_passes_through_unchanged(said):
    r = Redactor(EMAIL)
    assert r.clean(said) == said
    assert stream(r, said, range(1, len(said))) == said
    assert r.hits == 0


def test_no_email_known_means_nothing_is_touched():
    r = Redactor("")
    assert r.feed("ab12cd34ef@relay.example.com") == "ab12cd34ef@relay.example.com"
    assert r.flush() == "" and r.clean("abc") == "abc"


def test_a_short_name_alone_is_not_swallowed_only_the_full_address():
    r = Redactor("bob@example.com")
    assert r.clean("bob 说下周提测") == "bob 说下周提测"
    assert r.clean("发给 bob@example.com") == f"发给 {PLACEHOLDER}"


@pytest.mark.parametrize("said", [
    "他的邮箱是 ab12cd34ef@relay.example.com，你记一下。",
    "只说前面：ab12cd34ef。后面不说。",
    "拼一下 a b 1 2 c d 3 4 e f 就这些",
    "ab12cd34ef@relay.example.com",
    "两遍：ab12cd34ef 和 ab12cd34ef@relay.example.com 完",
])
def test_streaming_never_lets_it_out_wherever_the_chunks_are_cut(said):
    r = Redactor(EMAIL)
    whole = r.clean(said)
    for cut in range(1, len(said)):                      # 切一刀，每个位置都试
        assert stream(r, said, [cut]) == whole
    assert stream(r, said, range(1, len(said))) == whole  # 一个字一个字吐
    rng = random.Random(7)
    for _ in range(200):                                  # 随便切几刀
        cuts = rng.sample(range(1, len(said)), k=rng.randint(1, min(6, len(said) - 1)))
        assert stream(r, said, cuts) == whole


def test_streaming_holds_back_only_a_possible_start_and_releases_it_when_it_is_not():
    r = Redactor(EMAIL)
    assert r.feed("这个接口叫 ab") == "这个接口叫 "      # 「ab」可能是邮箱开头：先扣着
    assert r.feed("out，是关于页面") == "about，是关于页面"   # 不是：原样放出去
    assert r.feed("版本 ab12") == "版本 "
    assert r.flush() == "ab12"                            # 话说完了也没凑成邮箱：放出去


def test_an_email_that_overlaps_itself_cannot_slip_through_a_cut():
    """邮箱里有重复的片段时，扣着的那截不能把已经凑齐的邮箱放走。"""
    r = Redactor("abcab@x.io")
    for said in ("abcabc 之后", "ababcab 之后", "abcabcab@x.io"):
        whole = r.clean(said)
        assert "abcab" not in whole
        for cut in range(1, len(said)):
            assert stream(r, said, [cut]) == whole
        assert stream(r, said, range(1, len(said))) == whole


def test_login_email_is_read_from_the_cli_config_and_missing_is_empty(tmp_path):
    f = tmp_path / ".claude.json"
    assert login_email(f) == ""                           # 没登录
    f.write_text(json.dumps({"oauthAccount": {"emailAddress": EMAIL}}), encoding="utf-8")
    assert login_email(f) == EMAIL
    f.write_text("{坏的", encoding="utf-8")
    assert login_email(f) == ""


def test_tests_never_read_the_real_account():
    assert login_email() == ""                            # conftest 把 Claude 的家目录指到了临时目录


async def test_worker_results_and_progress_are_filtered_before_they_reach_the_screen(monkeypatch, tmp_path):
    """干活的进程（查资料、查代码、写纪要）同样知道登录邮箱：结果和进度离开进程前都要过滤。"""
    import asyncio

    from avatar import slow_brain
    from avatar.config import Settings

    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebSearch", "input": {"query": f"{EMAIL} github"}}]}},
        {"type": "result", "result": f"查到了：登录账号是 {EMAIL}，对应的仓库有三个。"},
    ]

    class Out:
        def __init__(self):
            self.rows = [json.dumps(x, ensure_ascii=False).encode() + b"\n" for x in lines]

        async def readline(self):
            return self.rows.pop(0) if self.rows else b""

    class Proc:
        returncode = 0
        stdout = Out()

    async def spawn(*cmd, **kw):
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(slow_brain, "login_email", lambda: EMAIL)
    progress: list[str] = []
    result, stalled = await slow_brain.SlowBrain(Settings())._attempt(["claude"], tmp_path, progress.append)
    assert result.ok and not stalled
    assert result.text == f"查到了：登录账号是 {PLACEHOLDER}，对应的仓库有三个。"
    assert progress and not any(spills(p, EMAIL) for p in progress)
