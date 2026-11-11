# 会议数字人 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 一个跑在本机的数字人，能听懂多人会议、主动接话、后台查资料 / 读代码 / 做设计，当面和线上会议通用。

**Architecture:** 浏览器「形象页」负责收音、播放和动画，经 WebSocket 与 FastAPI 后端交换 16k PCM 和语音片段。后端分层：ears（断句 + 识别 + 声纹）→ floor（发言决策）→ fast_brain（常驻 Claude Haiku）→ mouth（合成），slow_brain 按需启动本地 Claude Code 干活。所有操作走 REST，状态走事件流。

**Tech Stack:** Python 3.11 + uv、FastAPI、sherpa-onnx（silero VAD / SenseVoice / CAM++ 声纹）、edge-tts、本机 `claude` 命令行、原生 HTML/JS（无构建）。

设计文档：`docs/superpowers/specs/2026-11-11-meeting-avatar-design.md`

---

## 文件职责

| 文件 | 职责 | 依赖 |
|---|---|---|
| `avatar/models.py` | 模型路径与下载 | 无 |
| `avatar/config.py` | `Settings`：名字、嗓音、场景、项目目录；读写 `workspace/settings.json` | 无 |
| `avatar/textutil.py` | `SentenceSplitter` 流式分句；`parse_reply` 解析快脑输出；`similar` 回声判重 | 无 |
| `avatar/speakers.py` | `SpeakerBook`：声纹向量 → 说话人 id，改名 | numpy |
| `avatar/meeting.py` | `Meeting`：字幕、任务、数字人状态、事件总线 | speakers |
| `avatar/ears.py` | `Ears`：PCM → `on_speech_start` / `on_utterance(text, embedding)` | sherpa-onnx |
| `avatar/fast_brain.py` | `FastBrain`：常驻 `claude` 进程，`ask(text)` 流式产出文字，`cancel()` | claude CLI |
| `avatar/slow_brain.py` | `SlowBrain`：按类型启动一次性 `claude` 进程，回报进度和结果 | claude CLI |
| `avatar/mouth.py` | `Mouth.synth(text) -> bytes`：edge-tts，失败退回 `say` | edge-tts |
| `avatar/floor.py` | `Floor`：发言决策状态机，串起以上所有部件 | 以上全部（经接口注入） |
| `avatar/api.py` | FastAPI：REST、`/ws/events`、`/ws/audio`、静态页面 | floor, meeting |
| `avatar/web/stage.*` | 形象页：收音、播放、动画、资料板 | — |
| `avatar/web/console.*` | 控制台：字幕、说话人、任务、开关 | — |

`Floor` 只依赖四个可替换的接口（brain / mouth / worker / 播放通道），所以单元测试用假对象驱动，不需要模型和网络。

---

## Task 1：文字工具 `textutil.py`

**Files:** Create `avatar/textutil.py`，Test `tests/test_textutil.py`

- [ ] 先写测试：
  - `SentenceSplitter.feed("你好，我是小克。今天")` 只吐出 `["你好，我是小克。"]`，`flush()` 吐出 `["今天"]`
  - 首句为抢时间可在逗号处切：`feed("嗯，这个问题我觉得可以这样看，先做登录")` 首段在第一个逗号后切出，且长度 ≥ 4
  - 英文句号后跟空格才切，`3.5` 不切
  - `parse_reply("[静]")` → `Reply(silent=True)`；`parse_reply("好的我查一下。[[派工 查资料:飞书会议机器人接口]]")` → `speech="好的我查一下。"`, `dispatch=("查资料","飞书会议机器人接口")`
  - `ReplyStream`：逐字喂入时，开头是 `[静]` 就不产出任何语音；`[[派工 …]]` 不会被念出来
  - `similar("我觉得先做登录", "我觉得先做登陆")` > 0.8
- [ ] 跑测试确认失败 → 实现 → 跑测试确认通过 → 提交

## Task 2：说话人 `speakers.py`

**Files:** Create `avatar/speakers.py`，Test `tests/test_speakers.py`

- [ ] 测试：两簇随机单位向量（簇内加小噪声）交替喂入，得到恰好两个 id 且归属稳定；`embedding=None` 时沿用上一位；`rename(id, "张三")` 后 `name_of(id) == "张三"`；质心随新样本滑动更新
- [ ] 实现：余弦相似度阈值 0.45，质心为归一化滑动平均；提交

## Task 3：会议状态 `meeting.py` + `config.py`

**Files:** Create `avatar/meeting.py`、`avatar/config.py`，Test `tests/test_meeting.py`

- [ ] 测试：`add_line` 递增 id 并发事件 `line`；`set_status("speaking")` 发事件 `status`；`snapshot()` 可 JSON 序列化；订阅者队列满时丢弃最旧事件而不是阻塞；任务 `add_task/update_task`
- [ ] 实现并提交

## Task 4：发言决策 `floor.py`

**Files:** Create `avatar/floor.py`，Test `tests/test_floor.py`

用假 brain（按脚本吐字）、假 mouth（立即返回字节）、假播放通道（记录收到的指令，可手动回报播放完毕）测试：

- [ ] 被点名 → 快脑产出语音 → 播放通道收到按句的片段，状态 `listening → thinking → speaking → listening`
- [ ] 快脑回 `[静]` → 不产生任何播放，状态回到 `listening`
- [ ] 有人在说话时产出的语音不立即播放（状态 `waiting`），`on_speech_end` 后 0.4 秒才播放
- [ ] 等待期间来了新的一句 → 丢弃旧发言，快脑被再次调用，且输入里带「没说出口」的提示
- [ ] 说话过程中 `on_speech_start` 持续 0.3 秒 → 播放通道收到 `stop`，下一次快脑输入带「被打断」提示和已说出的部分
- [ ] 数字人说话时识别到与自己台词高度相似的文字 → 判为回声，丢弃，不进字幕
- [ ] 安静 6 秒只提示一次；`muted` 时不调用快脑；`hush()` 立即停播
- [ ] 快脑输出带派工 → worker 被调用；worker 完成后快脑收到结果摘要
- [ ] 实现并提交

## Task 5：耳朵 `ears.py`

**Files:** Create `avatar/ears.py`，Test `tests/test_ears.py`（需要模型，缺模型则跳过）

- [ ] 测试：用 `say -v Tingting` 合成「大家好，我们今天讨论登录功能」转 16k wav，分块喂入，断言触发一次 `on_speech_start`、一次 `on_utterance`，文字含「登录」，embedding 维度 > 0
- [ ] 实现：识别放线程池，不阻塞事件循环；提交

## Task 6：快脑 `fast_brain.py`、嘴 `mouth.py`

**Files:** Create `avatar/fast_brain.py`、`avatar/mouth.py`，Test `tests/test_live.py`（标记 `live`，默认不跑）

- [ ] 快脑：`start()` 预热；`ask(text)` 异步迭代文字增量；`cancel()` 发中断；进程退出自动重启并补喂最近字幕
- [ ] 嘴：`synth(text) -> (bytes, mime)`；edge-tts 失败时用 `say` + `ffmpeg` 兜底
- [ ] live 测试：快脑问候能在 3 秒内出首字；合成「你好」得到非空音频
- [ ] 提交

## Task 7：接口 `api.py`

**Files:** Create `avatar/api.py`、`avatar/__main__.py`，Test `tests/test_api.py`

- [ ] 测试（httpx + 假部件）：设计文档接口表里每个接口各一条；`/ws/events` 连接后先收到全量快照；`POST /api/utterance` 后事件流里出现 `line`
- [ ] 实现并提交

## Task 8：形象页 `web/stage.*`

- [ ] 收音：`getUserMedia`（回声消除开关随场景）→ AudioWorklet 降采样 16k → `/ws/audio`
- [ ] 播放：按句排队、可立即停止、回报 `started / ended`；AnalyserNode 驱动嘴型
- [ ] 形象：聆听、思考、举手、说话、干活五种神态，用颜色和动作区分
- [ ] 资料板：有任务或结果时滑出；设计稿用 iframe 展示
- [ ] 元素有稳定 id；浅色、深色、窄屏检查

## Task 9：控制台 `web/console.*`

- [ ] 字幕流、说话人改名、任务列表、场景和设备选择、开始 / 结束、静音、闭嘴、文字发言框
- [ ] 每个按钮对应一个接口，元素有稳定 id；浅色、深色、窄屏检查

## Task 10：慢脑 `slow_brain.py`

- [ ] 三类任务的工具白名单与工作目录；流式回报「正在搜索…/正在读 xx 文件」；超时 5 分钟
- [ ] live 测试：查资料任务能返回带来源的结果；查代码任务能在本仓库里找到 `SentenceSplitter` 定义位置
- [ ] 提交

## Task 11：线上会议接入

- [ ] 场景切到「线上」时关闭回声消除，输入输出设备可选（`setSinkId`）
- [ ] `README.md` 写清 BlackHole、OBS、会议软件三处设置

## Task 12：端到端验证

- [ ] `tests/e2e_voice.py`：三种嗓音合成三位与会者的对话，经 `/ws/audio` 送入真实后端，断言：三位说话人被区分、点名后数字人回应、插话后数字人停下
- [ ] 浏览器里实际打开两个页面，浅色、深色、窄屏各截图检查
