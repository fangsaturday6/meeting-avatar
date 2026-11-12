from pathlib import Path

from avatar import knowledge
from avatar.config import Settings


def make_tree(root: Path) -> None:
    def proj(rel, files):
        d = root / rel
        d.mkdir(parents=True)
        for name, text in files.items():
            (d / name).write_text(text, encoding="utf-8")
    proj("shop", {"CLAUDE.md": "# CLAUDE.md\n\nThis file provides guidance to Claude Code.\n\n商城后台，Go 写的。\n"})
    proj("erp", {"README.md": "# 进销存\n\n![logo](x.png)\n\n给仓库用的进销存系统。\n", "package.json": "{}"})
    proj("crm/api", {"go.mod": "module x"})
    proj("crm/web", {"package.json": "{}"})
    (root / "照片").mkdir()
    (root / ".cache" / "x").mkdir(parents=True)
    for i in range(6):
        proj(f"mono/svc{i}", {"go.mod": "module x"})


def test_scan_finds_projects_one_and_two_levels_down(tmp_path):
    knowledge.forget()
    make_tree(tmp_path)
    ps = {p.name: p for p in knowledge.scan(tmp_path)}
    assert {"shop", "erp", "crm/api", "crm/web", "mono/svc0"} <= set(ps) and "照片" not in ps
    assert ps["shop"].about == "商城后台，Go 写的。"           # 文件名当标题、模板套话都不要
    assert ps["erp"].about == "进销存：给仓库用的进销存系统。"
    assert ps["crm/api"].about == "" and ps["crm/api"].path == str(tmp_path / "crm" / "api")


def test_brain_gets_names_without_paths_and_big_folders_collapsed(tmp_path):
    knowledge.forget()
    make_tree(tmp_path)
    text = knowledge.for_brain(knowledge.scan(tmp_path))
    assert str(tmp_path) not in text
    assert "- mono：下面有 6 个子项目" in text and "svc5" not in text.replace("如 ", "").split("mono：")[0]
    assert text.count("mono") == 1 + text.count("mono/")
    assert "- crm/api" in text
    worker = knowledge.for_worker(knowledge.scan(tmp_path))
    assert f"- shop（{tmp_path / 'shop'}）：商城后台，Go 写的。" in worker and "mono/svc5" in worker


def test_root_defaults_to_the_folder_holding_the_main_project(tmp_path):
    knowledge.forget()
    make_tree(tmp_path)
    s = Settings()
    assert knowledge.root_of(s) is None and knowledge.scan(None) == []
    s.project_dir = str(tmp_path / "shop")
    assert knowledge.root_of(s) == tmp_path
    assert knowledge.main_project(s, knowledge.scan(tmp_path)) == "shop"
    s.projects_root = str(tmp_path / "crm")
    assert [p.name for p in knowledge.scan(knowledge.root_of(s))] in (["api", "web"], ["web", "api"])


def test_it_reads_what_the_owners_claude_remembers(tmp_path, monkeypatch):
    knowledge.forget()
    home = tmp_path / "claude-home"
    monkeypatch.setattr(knowledge, "CLAUDE_HOME", home)
    proj = tmp_path / "work" / "shop.v2"
    proj.mkdir(parents=True)
    (proj / "README.md").write_text("# 商城\n\n卖东西的。", encoding="utf-8")
    assert knowledge.memory_dir(proj) is None and knowledge.memory_index(proj) == "" and knowledge.owner_rules() == ""

    mem = home / "projects" / str(proj).replace("/", "-").replace(".", "-").replace("_", "-") / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- [发布流程](release.md) — 周四发版，先灰度\n", encoding="utf-8")
    (home / "CLAUDE.md").write_text("# 全局偏好\n\n- 始终用中文回复\n" + "x" * 3000, encoding="utf-8")
    assert knowledge.memory_dir(proj) == mem
    assert knowledge.memory_index(proj) == "- [发布流程](release.md) — 周四发版，先灰度"
    rules = knowledge.owner_rules()
    assert rules.startswith("# 全局偏好") and "始终用中文回复" in rules and len(rules) < 1600 and rules.endswith("……")
    p = knowledge.scan(tmp_path / "work")[0]
    assert p.memory == str(mem) and f"［记忆在 {mem}］" in knowledge.for_worker([p])
    assert str(mem) not in knowledge.for_brain([p])


def _session(home, cwd, title, name="s1"):
    import json
    d = home / "projects" / str(cwd).replace("/", "-").replace(".", "-").replace("_", "-")
    d.mkdir(parents=True, exist_ok=True)
    lines = [{"type": "user", "cwd": str(cwd), "message": {"role": "user", "content": "这是对话内容，不该被读出来"}}]
    if title:
        lines.append({"type": "custom-title", "customTitle": title, "sessionId": name})
    (d / f"{name}.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines), encoding="utf-8")


def test_recent_claude_sessions_tell_it_what_the_owner_has_been_doing(tmp_path, monkeypatch):
    knowledge.forget()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / ".claude"
    monkeypatch.setattr(knowledge, "CLAUDE_HOME", home)
    work = tmp_path / "Documents"
    for name in ("meeting", "shop", "old"):
        (work / name).mkdir(parents=True)
    (work / "meeting" / "README.md").write_text("# 会议数字人", encoding="utf-8")
    (work / "old" / "README.md").write_text("# 老项目", encoding="utf-8")
    (tmp_path / "code" / "achat" / "web").mkdir(parents=True)
    _session(home, work / "shop", "订单导出接口权限")                 # 没有说明文件的目录，但在里面干过活
    _session(home, work / "shop" / ".claude" / "worktrees" / "x1", "退款流程", "s2")   # 工作树算到原项目头上
    _session(home, tmp_path / "code" / "achat", "回复和问题修复")            # 总目录之外的项目
    _session(home, tmp_path / "code" / "achat" / "web", "前端样式", "s3")    # 项目里的子目录不单列
    _session(home, work / "meeting", "")                                      # 没标题的会话不算
    _session(home, tmp_path / ".cache" / "x", "临时目录里的")                # 隐藏目录不算项目

    s = Settings()
    s.project_dir = str(work / "meeting")
    projects = knowledge.everything(s)
    names = [p.name for p in projects]
    assert set(names) == {"meeting", "old", "shop", "code/achat"}
    assert names.index("shop") < names.index("old")                # 最近用 Claude 干过活的排前面
    text = knowledge.sessions_text(knowledge.recent_sessions(projects))
    assert "shop：订单导出接口权限" in text and "shop：退款流程" in text
    assert "code/achat：回复和问题修复" in text and "code/achat：前端样式" in text
    assert "对话内容" not in text                                    # 只有标题


def test_a_folder_it_cannot_enter_does_not_wipe_the_whole_list(tmp_path):
    import os
    knowledge.forget()
    make_tree(tmp_path)
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    os.chmod(locked, 0)
    try:
        names = {p.name for p in knowledge.scan(tmp_path)}
    finally:
        os.chmod(locked, 0o755)
    assert {"shop", "erp"} <= names and "locked" not in names


def test_main_project_brief_is_the_readme_lead(tmp_path):
    d = tmp_path / "shop"
    d.mkdir()
    assert knowledge.project_brief(d) == ""
    (d / "README.md").write_text(
        "# 商城\n\n![logo](x.png)\n\n卖电子元器件的网站，面向采购员。支持按型号搜索、批量询价和在线下单，后台用 Go 写。\n\n## 启动\n\nmake run\n", encoding="utf-8")
    brief = knowledge.project_brief(d)
    assert brief.startswith("# 商城") and "面向采购员" in brief and "make run" not in brief and "logo" not in brief
