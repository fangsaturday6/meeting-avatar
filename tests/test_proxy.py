"""子进程（本机 claude、curl）用的代理：记下的那个不通了要自己重新找，大脑进程要跟着换。

背景：2026-11-14 本机代理从 1082 端口换到 7890，开了一天的后台还记着 1082，大脑连了半小时连不上。
"""
import asyncio
import socket

import pytest

from avatar import config
from avatar.config import Settings
from avatar.fast_brain import FastBrain


def listener() -> socket.socket:
    """本机一个真在听的端口，当作「活着的代理」。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def url(sock: socket.socket) -> str:
    return f"http://127.0.0.1:{sock.getsockname()[1]}"


def dead_url() -> str:
    """一个刚关掉的端口：没人听。"""
    s = listener()
    try:
        return url(s)
    finally:
        s.close()


@pytest.fixture
def lookups(monkeypatch):
    """环境里不带代理，去终端配置、系统设置里找的结果由测试给；记下各找了几次。"""
    for key in config._PROXY_KEYS:
        monkeypatch.delenv(key, raising=False)
        monkeypatch.delenv(key.lower(), raising=False)
    monkeypatch.setattr(config, "_child_env", None)
    monkeypatch.setattr(config, "_looked_at", 0.0)
    found = {"shell": {}, "system": {}, "calls": []}

    def source(name):
        def look():
            found["calls"].append(name)
            return dict(found[name])
        return look

    monkeypatch.setattr(config, "_proxy_from_shell", source("shell"))
    monkeypatch.setattr(config, "_proxy_from_system", source("system"))
    return found


def test_a_proxy_from_the_environment_that_answers_is_used_as_is(lookups, monkeypatch):
    with listener() as live:
        monkeypatch.setenv("HTTPS_PROXY", url(live))
        assert config.proxy_url() == url(live)
        assert config.proxy_url() == url(live)
    assert lookups["calls"] == []


def test_a_dead_proxy_from_the_environment_is_replaced(lookups, monkeypatch):
    """启动它的终端带着的代理已经没人听了：去系统设置里找现在的。"""
    old = dead_url()
    monkeypatch.setenv("HTTPS_PROXY", old)
    monkeypatch.setenv("http_proxy", old)
    monkeypatch.setenv("NO_PROXY", "localhost")
    with listener() as live:
        new = url(live)
        lookups["system"] = {"HTTPS_PROXY": new, "HTTP_PROXY": new}
        env = config.child_env()
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        assert env[key] == new
    assert env["NO_PROXY"] == "localhost"       # 找到的那份没说哪些不走代理：原来的留着


def test_a_proxy_that_moves_while_running_is_found_again(lookups):
    """开着不关的后台：代理中途换了端口，下一次要用时就换过去，不用重启。"""
    with listener() as first:
        lookups["shell"] = {"HTTPS_PROXY": url(first)}
        assert config.proxy_url() == url(first)
    with listener() as second:
        lookups["shell"] = {"HTTPS_PROXY": url(second)}
        assert config.proxy_url() == url(second)


def test_the_first_source_that_answers_wins(lookups):
    with listener() as live:
        lookups["shell"] = {"HTTPS_PROXY": dead_url()}
        lookups["system"] = {"HTTPS_PROXY": url(live)}
        assert config.proxy_url() == url(live)


def test_when_nothing_answers_it_keeps_what_it_had_and_does_not_look_on_every_call(lookups, monkeypatch):
    """代理整个没开：别每起一个子进程都去翻一遍终端配置（一次要一两秒）。"""
    old = dead_url()
    monkeypatch.setenv("HTTPS_PROXY", old)
    for _ in range(3):
        assert config.proxy_url() == old
    assert lookups["calls"] == ["shell", "system"]
    # 过了间隔再找；这回代理起来了
    monkeypatch.setattr(config, "_looked_at", config._looked_at - config.RECHECK - 1)
    with listener() as live:
        lookups["system"] = {"HTTPS_PROXY": url(live)}
        assert config.proxy_url() == url(live)


def test_a_machine_without_any_proxy_looks_only_once(lookups):
    for _ in range(3):
        assert config.proxy_url() is None
    assert lookups["calls"] == ["shell", "system"]


def test_a_proxy_on_another_machine_is_trusted_without_probing(lookups, monkeypatch):
    """不在本机的代理不去试连：一时不通多半是网络抖，不是换了地方。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.com:8080")
    assert config.proxy_url() == "http://proxy.example.com:8080"
    assert lookups["calls"] == []


# ---------- 大脑进程跟着换 ----------

class FakeProc:
    def __init__(self, env):
        self.env, self.returncode, self.wrote = env, None, []
        self.stdin = self

    def write(self, data):
        self.wrote.append(data)

    def close(self):
        pass

    def terminate(self):
        self.returncode = -15

    async def wait(self):
        return self.returncode


@pytest.fixture
def brain(monkeypatch):
    spawned = []

    async def spawn(*cmd, env=None, **kw):
        spawned.append(FakeProc(env))
        return spawned[-1]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    b = FastBrain(Settings(), recap=lambda: "老方：先做登录")
    b.login_email = lambda: ""
    b.spawned = spawned
    return b


async def test_the_brain_restarts_itself_when_the_proxy_moved(lookups, brain):
    """大脑是一个开着不关的进程，代理是它启动时带进去的：代理换了地方，它得重启才连得上。"""
    with listener() as first:
        lookups["shell"] = {"HTTPS_PROXY": url(first)}
        await brain._spawn()
        await brain.ask("第一句")
        assert len(brain.spawned) == 1
    with listener() as second:
        lookups["shell"] = {"HTTPS_PROXY": url(second)}
        await brain.ask("第二句")
        old, new = brain.spawned
        assert old.returncode is not None                 # 旧进程收掉了
        assert new.env["HTTPS_PROXY"] == url(second)
        sent = new.wrote[-1].decode()
        assert "先做登录" in sent and "第二句" in sent      # 之前的会议记录补给了新进程


async def test_the_brain_is_left_alone_while_the_proxy_stays(lookups, brain):
    with listener() as live:
        lookups["shell"] = {"HTTPS_PROXY": url(live)}
        await brain._spawn()
        await brain.ask("第一句")
        brain._inflight = False
        await brain.ask("第二句")
    assert len(brain.spawned) == 1
