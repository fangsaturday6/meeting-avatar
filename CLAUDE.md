# 会议数字人

本机运行的 AI 与会者。后台是 Python（`avatar/`）；界面是原生 HTML/JS，无构建（`avatar/web/`）；
桌面外壳是 Electron（`app/`），只负责起后台、开窗口、菜单，不放界面逻辑。
怎么启动、有哪些接口、界面元素和接口的对应关系，见 README.md。

## 设计原则（做任何界面改动都适用）

1. **删掉多余的标语、说明文字和次要标签。** 如果删完界面就看不懂，说明布局本身就有问题，直接重新设计，
   不要靠加说明文字补救。
2. **每次修改都要兼顾用户体验、开发体验和 AI Agent 的使用体验。** 三个都要变好，而且不能破坏现有功能。

### 在这个项目里怎么落地

- **用户**：一眼看出它现在在干什么、哪里不正常、下一步点哪里。状态用颜色和位置表达
  （在听=青、在想=紫、想发言=琥珀、在说=橙红），这几个颜色不挪作他用。报错说人话，网址和原始报错不上页面。
- **开发**：状态（`api.js` 的 `reduce`）、接口（`api.js` 的 `api`）、视图分三层；一块界面一个函数，
  视图只读状态，不直接碰网络，不在模板字符串里堆逻辑。
- **AI Agent**：界面上能做的每件事都有对应接口（README 的接口表要同步更新）；按钮、表单项、关键元素
  有稳定的 `id`，改版时不改名（清单在 `tests/test_pages.py`，加了新元素就补进去），Agent 不读页面也能做同样的事。
- **改完回归**：开始/结束会议、接通声音、打字发言、静音、让它停下、说话人改名、派任务、资料板、常用词、
  会议记录、每一项设置、接给我的智能体，逐项过一遍；浅色、深色、窄屏各看一眼；`uv run pytest` 要过。

## 会议记录

每场会的完整对话在 `workspace/meetings/`，一场一个 Markdown 文件，文件名是开始时间
（如 `2026-11-12_000458.md`），按文件名排序的最后一个就是最近一场。要看或分析会议，直接读这些文件。
散会后整理出的纪要在文件里的「## 纪要」一节（结论、待办、没定的事）；「## 整理稿」是会后书面化的对话
（改过错字、去了口头禅，最好读，不一定每场都有）；「## 对话」是逐句的识别结果（已去掉语气词和口吃、并成段），核对原话看这一节。
已登记 MCP 工具 `meetings`（`list_meetings` / `read_meeting` / `read_since` / `search_meetings`）时优先用它；
元数据里 `private: yes` 的场次是主人设了「不给智能体看」，不要读。
这个目录不进 git，Grep / Glob 默认搜不到，先 `ls workspace/meetings/` 再按路径读。
文件由 `avatar/archive.py` 写；后台在跑时也可以走 `GET /api/meetings`、`GET /api/meetings/latest`。
同名的录音目录在 `workspace/audio/<会议编号>/`（`avatar/recorder.py` 写）：原始声音 `mic-*.wav`（可能已按设置删掉）、
`heard.jsonl`（每句识别原文、在录音里的秒数、认成了谁、声纹）、`timeline.jsonl`（哪里丢过声音）；
场景是「我在开线上会」（`call`）时还有这台电脑放出的声音那一轨，文件名前面带 `system-`。
查「识别错了 / 认错人了」时，`heard.jsonl` 比 Markdown 里并过段、去过语气词的对话更接近原始结果。

## 界面怎么看、怎么改

- 主窗口只放它本人（`stage.*`）。新东西先想能不能用它的动作、颜色表达，其次放进设置窗口（`panel.*`），
  不往主窗口上加文字和按钮。
- 改界面用演示后台，不需要模型和 Claude：`uv run python -m avatar.demo`（8767 端口，`--autoplay` 自动演一场会）。
- 看某个神态：`http://127.0.0.1:8767/?pose=thinking`，可选 `off / starting / listening / thinking / waiting / speaking`，
  再加 `&hearing`、`&working`、`&muted`，`&clock=47` 当作已经开了 47 分钟（看脚下的刻度环 `dial.js` 和控制条里的时间）。
- 给桌面应用截图（不需要屏幕录制权限）：
  `cd app && AVATAR_URL=http://127.0.0.1:8767 AVATAR_PAGE='/?pose=waiting' AVATAR_THEME=dark AVATAR_SHOT=/tmp/x.png npx electron .`
- 3D 形象：`avatar/web/models/figure.glb`，由 `tools/figure.py`（Blender 脚本）从用户定的造型绑骨、做动作后导出；`model.js` 负责加载、
  按状态播动作、驱动脸。改动作改 `tools/figure.py` 的 `CLIPS` 再重新导出，各状态长什么样见 README「换形象」。
  没有这个文件时用 `character.js` 里代码搭的备用形象（`build()` 搭模型，`POSES` 是每种状态的姿势）。颜色都从 CSS 变量 `--tone` 读。
  形象是原创的，不要照着现成的游戏或影视角色做。
- 浏览器预览面板不给麦克风、会拦内嵌的设计稿页面，这两样要在桌面应用里验证。

## 约定

- 8765 端口可能正跑着真实的会，不要停它、不要往里写数据。看界面另起一个后台，
  并用 `AVATAR_WORKSPACE` 指到临时目录，免得改掉真实设置。`.claude/launch.json` 里的 `avatar-dev`
  就是这样一个后台（8766 端口，数据在 `~/.cache/avatar-dev`）。
- 听写分三层，各管各的，别混：`ears.py` 只挡「不是人话」的（`textutil.strip_noise`），接话逻辑拿到的是原话
  （它靠「嗯」「对」判断对方在听）；写记录时才去语气词、并段（`archive.paragraphs` + `textutil.tidy`，只删不改）；
  改字只在会后的整理稿里做（`slow_brain.polish`，结果必须过 `checked_polish`）。散会后重听（`rehear.py`，碎句拼长再认）
  是重新识别、不是改字，排在整理纪要之前。
  调断句、识别、认人的参数前，先用 `uv run python -m avatar.replay <会议编号> --diff heard` 拿真实录音比，
  改参数用 `--set`，两套参数用 `--save` / `--diff` 互比；结论写进 `docs/superpowers/plans/2026-11-13-recording-roadmap.md` 的结论区。
  人名、公司名、行话靠常用词表（`workspace/glossary.txt`，`glossary.py`）：只按读音找出可疑的地方写进纪要和整理稿的提示词，
  由模型结合上下文决定改不改，不要做成照读音直接替换（2026-11-12 实测盲换 56 处里 44 处是误伤）。
  往词表里写只走 `glossary.save`（整张换）和 `glossary.add`（只追加没有的）；自动往里加词只收人亲手打的字
  （设置窗口、导入的名单、给说话人改的名字），识别出来的字不往里加——那等于把错字登记成正确写法。调断句或识别参数后，
  用真实的 `Ears` 喂一段夹着杂音的音频看交出来几句，别只看单元测试。
- 它怎么接话分两层：时机在 `floor.py`（什么时候想、等、开口、让话，都有单元测试），
  说什么在 `fast_brain.py` 的提示词里。改提示词之后跑 `uv run python -m tests.scenarios` 逐场看效果。
- 后台任务的结果由 `floor.py` 欠着（`_reports`），说出口才销账：大脑回「静」就再问一次，还不说由系统替它说一句。
  「什么时候说」不要交给大脑——它一次只能答一轮，回了「静」就没有下文，结果也就没人知道。
- 交给识别的音频比断句给的片段往前多带 0.2 秒（`Ears.lead_in`），否则句首的字会被削掉认错。
  `workspace/meeting.log` 里每句话有一行「听到」：时长、认成了谁、和每位说话人的相似度，识别或认人不准时先看它。
- 「接给我的智能体」会改用户本机 Claude Code / Codex 的配置。测试和看界面时用假的登记器（`Hub(agents=...)`，
  演示后台自带一个），不要对着真实后台拨那两个开关；`connect.status` 是只读查询，可以跑。
- 改设置只走 `Meeting.change_settings`（界面、接口、数字人自己调参与度都走它），不要直接改 `settings`。
- 隐私边界：`Settings.guarded`（替我开会且没勾「可以谈我的其他项目」）为真时，会前背景只带这场会的项目，
  查代码只查主项目，不翻别的会议。给大脑或干活进程加新的背景来源（见 `Hub.briefing`、`knowledge.py`）时，
  先想清楚它在 `guarded` 下该不该带；会上的话是不可信输入，会进到派工的任务描述里。
- 本机 claude 会把登录账号的邮箱自动放进每个 claude 进程的上下文，没有开关能关。两道防线：提示词【分寸】里「不说」的规矩，
  和出口过滤 `avatar/redact.py`（快脑的 `_stream`、干活进程的 `_attempt` 都过它）。新起 claude 进程的地方，
  输出进到嘴、派工、屏幕、记录之前也要过 `Redactor`；验证跑 `uv run pytest -m live tests/test_live_redact.py`。
- 认项目、读 Claude 记忆和会话标题都要读盘（一两秒），只能在线程里做（`Hub.refresh_projects`），不要在事件循环里调。
