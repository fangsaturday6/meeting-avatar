"""把会议记录登记给本机的 Claude Code / Codex：状态怎么认、什么时候不动别人的配置。"""
import pytest

from avatar import connect

OURS = "meetings:\n  Type: stdio\n  Command: /x/.venv/bin/python3\n  Args: /somewhere/else/avatar/mcp_server.py\n"
THEIRS = "meetings:\n  Type: stdio\n  Command: node\n  Args: /opt/other/meetings.js\n"
NONE = 'No MCP server named "meetings". Configured servers: pencil'


class Shell:
    """假的命令行：记下跑过什么，按事先给的结果回。"""

    def __init__(self, monkeypatch, get, add=(True, "Added"), installed=("claude", "codex")):
        self.calls, self.get, self.add = [], get, add
        monkeypatch.setattr(connect, "_run", self.run)
        monkeypatch.setattr(connect.shutil, "which", lambda b: f"/bin/{b}" if b in installed else None)

    def run(self, cmd):
        self.calls.append(cmd[2])                    # get / add / remove
        return {"get": self.get, "add": self.add, "remove": (True, "")}[cmd[2]]


@pytest.mark.parametrize("get, state", [((True, OURS), "on"), ((True, THEIRS), "taken"), ((False, NONE), "off"),
                                        ((False, "Command timed out"), "unknown")])
def test_status_is_read_from_the_client_itself(monkeypatch, get, state):
    Shell(monkeypatch, get)
    assert connect.status("claude") == state


def test_a_client_that_is_not_installed_is_never_asked(monkeypatch):
    sh = Shell(monkeypatch, (True, OURS), installed=("claude",))
    assert connect.status("codex") == "missing" and sh.calls == []
    with pytest.raises(ValueError, match="这台电脑上没装 Codex"):
        connect.set_connected("codex", True)
    assert sh.calls == []


def test_connecting_and_disconnecting(monkeypatch):
    sh = Shell(monkeypatch, (False, NONE))
    assert connect.set_connected("claude", True) == "on" and sh.calls == ["get", "add"]

    sh = Shell(monkeypatch, (True, OURS))            # 已经接着：先拿掉旧的再登记，路径才是新的
    assert connect.set_connected("claude", True) == "on" and sh.calls == ["get", "remove", "add"]
    sh = Shell(monkeypatch, (True, OURS))
    assert connect.set_connected("claude", False) == "off" and sh.calls == ["get", "remove"]
    sh = Shell(monkeypatch, (False, NONE))           # 本来就没接：取消什么也不用做
    assert connect.set_connected("claude", False) == "off" and sh.calls == ["get"]


@pytest.mark.parametrize("get, why", [((True, THEIRS), "已经有一个叫 meetings 的别的服务"), ((False, "timed out"), "没能确认")])
def test_someone_elses_config_is_left_alone(monkeypatch, get, why):
    sh = Shell(monkeypatch, get)
    for connected in (True, False):
        with pytest.raises(ValueError, match=why):
            connect.set_connected("claude", connected)
    assert set(sh.calls) == {"get"}


def test_a_failed_registration_is_reported_without_the_raw_error(monkeypatch):
    Shell(monkeypatch, (False, NONE), add=(False, "Error: EACCES /Users/x/.claude.json"))
    with pytest.raises(ValueError) as e:
        connect.set_connected("claude", True)
    assert str(e.value) == "没接上 Claude Code，过一会儿再试"


def test_command_line_connects_every_installed_client(monkeypatch):
    Shell(monkeypatch, (False, NONE), installed=("claude",))
    assert connect.connect() == {"Claude Code": "已接上", "Codex": "没装，跳过"}
    assert connect.connect(remove=True) == {"Claude Code": "本来就没登记", "Codex": "没装，跳过"}
    Shell(monkeypatch, (True, OURS))
    assert connect.connect(remove=True) == {"Claude Code": "已取消", "Codex": "已取消"}
    Shell(monkeypatch, (True, THEIRS))
    assert "没动它" in connect.connect()["Claude Code"]


def test_connector_reports_every_client(monkeypatch):
    Shell(monkeypatch, (True, OURS), installed=("claude",))
    assert connect.Connector().clients() == [{"id": "claude", "name": "Claude Code", "state": "on"},
                                             {"id": "codex", "name": "Codex", "state": "missing"}]


def test_the_guide_gives_an_agent_three_ways_in():
    text = connect.guide("http://127.0.0.1:8765/")
    assert "http://127.0.0.1:8765/mcp " in text and "http://127.0.0.1:8765/api/meetings/latest" in text
    assert "avatar/mcp_server.py" in text and "只读" in text
