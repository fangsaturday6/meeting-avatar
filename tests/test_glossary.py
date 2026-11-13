"""常用词表：读文件、按读音找出记录里可能被认错的词、写成给整理稿和纪要看的一段提示。"""
from avatar import glossary


def test_file_is_one_term_per_line_with_an_optional_note(tmp_path):
    f = tmp_path / "glossary.txt"
    f.write_text("# 公司\n青禾科技\n\n回价：供应商回复的报价\nKestrel: 英文品牌名\n  企微  \n回价：重复的不算\n价\n", encoding="utf-8")
    assert glossary.load(f) == {"青禾科技": "", "回价": "供应商回复的报价", "Kestrel": "英文品牌名", "企微": ""}
    assert glossary.load(tmp_path / "没有这个文件.txt") == {}


def test_same_sounding_words_are_suspects_but_right_ones_are_not():
    said = "他这个数字人回家比我快，企微上发的。起微那边、齐微那边都没收到，清河科技的人在问。"
    found = glossary.suspects(said, ["回价", "企微", "青禾科技", "供应商"])
    assert found == {"回价": {"回家": 1}, "企微": {"起微": 1, "齐微": 1}, "青禾科技": {"清河科技": 1}}


def test_two_character_terms_must_sound_exactly_the_same():
    # 两个字的词只差平翘舌、前后鼻音就算的话，「纪总」会被当成「机种」，误伤太多；三个字以上才放宽
    assert glossary.suspects("周总和纪总提了个需求", ["机种"]) == {}
    assert glossary.suspects("先问一下工因商那边", ["供应商"]) == {"供应商": {"工因商": 1}}


def test_a_word_that_is_itself_in_the_glossary_is_left_alone():
    assert glossary.suspects("回价和汇价是两回事", ["回价", "汇价"]) == {}


def test_english_terms_match_by_spelling():
    found = glossary.suspects("那个 Kestral 新站和 Kestrol 的链接，EDA 工具，还有 meeting", ["Kestrel", "EDA", "SKU"])
    assert found == {"Kestrel": {"Kestral": 1, "Kestrol": 1}}
    assert glossary.suspects("接了 MCP 和 SPU", ["MPN", "SKU"]) == {}      # 短缩写差一个字母就是另一个词


def test_hint_lists_only_what_this_text_sounds_like(tmp_path):
    f = tmp_path / "glossary.txt"
    f.write_text("回价：供应商回复的报价\n企微\n青禾科技\n采购单\n", encoding="utf-8")
    hint = glossary.hint("数字人回家了，回家很快，起微上说的", f)
    assert "- 回价（记录里写成：回家）：供应商回复的报价" in hint and "- 企微（记录里写成：起微）" in hint
    assert "青禾科技" not in hint and "采购单" not in hint       # 这段话里没有像它们的，就不带出去
    assert "才改" in hint and "讲得通" in hint                   # 交给上下文判断，不是照着换
    assert glossary.hint("今天天气不错", f) == ""
    assert glossary.hint("数字人回家了", tmp_path / "没有.txt") == ""


def test_hint_is_capped_and_longer_terms_come_first(tmp_path):
    f = tmp_path / "glossary.txt"
    f.write_text("企微\n青禾科技\n回价\n", encoding="utf-8")
    hint = glossary.hint("起微 回家 清河科技", f, limit=2)
    rows = [r for r in hint.splitlines() if r.startswith("- ")]
    assert len(rows) == 2 and rows[0].startswith("- 青禾科技")   # 字多的对上了更可信，先留它


def test_saving_replaces_the_whole_file_and_keeps_comments(tmp_path):
    f = tmp_path / "ws" / "glossary.txt"                 # 目录还没有也能存
    assert glossary.save("# 人\r\n陈默  \n\n\n北辰：合作方\n\n", f) == "# 人\n陈默\n\n\n北辰：合作方\n"
    assert glossary.read(f) == "# 人\n陈默\n\n\n北辰：合作方\n" and glossary.load(f) == {"陈默": "", "北辰": "合作方"}
    assert glossary.save("  \n", f) == "" and glossary.load(f) == {}
    assert glossary.read(tmp_path / "没有.txt") == ""


def test_adding_only_appends_what_is_new(tmp_path):
    f = tmp_path / "glossary.txt"
    f.write_text("# 手写\n陈默：研发\n", encoding="utf-8")
    added = glossary.add({"陈默": "别的说明", "北辰": "合作\n方", "x": "", "12345": "", "#井号": "", " 蓝湾 ": ""}, "从 客户.csv 导入", f)
    assert added == ["北辰", "蓝湾"]                    # 已有的不动，太短的、纯数字的、会变成注释的不要
    assert f.read_text(encoding="utf-8") == "# 手写\n陈默：研发\n\n# 从 客户.csv 导入\n北辰：合作 方\n蓝湾\n"
    assert glossary.add({"北辰": "", "陈默": ""}, "从 客户.csv 导入", f) == []
    assert f.read_text(encoding="utf-8").count("# 从 客户.csv 导入") == 1

    # 同一个来历接着加，不再写一遍标题（会上改一次名字加一个）
    people = tmp_path / "people.txt"
    assert glossary.add({"张总": ""}, "会上的人", people) == ["张总"]
    assert glossary.add({"李工": ""}, "会上的人", people) == ["李工"]
    assert people.read_text(encoding="utf-8") == "# 会上的人\n张总\n李工\n"


def test_lists_from_elsewhere_are_read_by_their_file_type():
    # 一行一个（文件开头可能带着记事本加的标记）
    assert glossary.from_file("客户.txt", "\ufeff北辰\n蓝湾：做 EDA 的\n# 注释\n") == {"北辰": "", "蓝湾": "做 EDA 的"}
    assert glossary.from_file("", "北辰\n") == {"北辰": ""}
    # 表格：没表头就是第一列是词、第二列是说明
    assert glossary.from_file("a.csv", "陈默,研发部,138\n\n\"北辰, 上海\",合作方\n7,\n") == {"陈默": "研发部", "北辰, 上海": "合作方"}
    assert glossary.from_file("a.tsv", "陈默\t研发部\n") == {"陈默": "研发部"}
    # 有表头按表头找：通讯录第一列常常是工号
    book = "工号,姓名,部门,备注\n1001,陈默,研发部,负责 EDA\n1002,周航,销售部,\n"
    assert glossary.from_file("通讯录.CSV", book) == {"陈默": "负责 EDA", "周航": ""}
    assert glossary.from_file("客户.csv", "客户名称,城市\n北辰,上海\n") == {"北辰": ""}


def test_company_ontology_gives_its_names_not_its_rule_text():
    doc = """
- object: 报价单
  description: 一次对客户的正式报价。后面的话不要
  properties:
    状态: {values: [待回价, 已回价]}
    SKU_ID: {}
  logic:
    - name: 回价超时
      when: 三天没回
  actions:
    - name: 发起询价
      input: [询价任务ID]
      returns: [报价明细行]
  vocabulary:
    回价: {别名: [回报价]}
  disambiguation:
    - {词: 汇价, 含义: 外汇的价格；和回价不是一回事}
  param_order: [币种]
"""
    terms = glossary.from_file("quote.yaml", doc)
    assert terms["报价单"] == "一次对客户的正式报价" and terms["回报价"] == "就是回价" and terms["汇价"] == "外汇的价格"
    assert {"状态", "待回价", "已回价", "SKU", "回价超时", "发起询价", "询价任务", "报价明细行", "回价", "币种"} <= set(terms)
    assert "三天没回" not in terms
    assert glossary.from_file("ontology.yaml", "name: 青禾科技\nversion: 1\n") == {"青禾科技": "公司名"}
    assert glossary.from_file("别的.yml", "just: a config\n") == {}

    import pytest
    with pytest.raises(ValueError, match="读不懂"):
        glossary.from_file("坏的.yaml", "a: [1, 2\n")
