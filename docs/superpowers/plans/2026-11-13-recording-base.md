# 阶段一：录音底座 实施计划

> 路线图见 [2026-11-13-recording-roadmap.md](2026-11-13-recording-roadmap.md)。按 TDD 做，每个任务先写失败的测试。

**目标**：每场会的原始声音、每句话在录音里的位置和声纹都留下来，崩溃不丢；麦克风送来的是数字静音时它知道自己听不到；能用一条命令把一场会原样重放。

**架构**：形象页送来的每个声音帧带序号。后台新增 `AudioIn` 作为唯一入口：解帧、计音频时钟、看健康、交给 `Recorder` 落盘、交给 `Ears` 识别。`Ears` 给每句话带上它在音频时钟上的起点 `at`，`Hub._heard` 把识别结果（文字、时长、声纹、归给了谁）记进 `heard.jsonl`。`avatar.replay` 读存档喂给真实的 `Ears`。

## 已核实的前提

- sherpa-onnx VAD 的 `front.start` 从创建或 `reset()` 起累计采样数，`reset()` 后归零（2026-11-13 实测）。所以全局位置 = `Ears` 每次重置后第一帧的音频时钟 + `front.start`。
- VAD 有 `flush()`，重放结束时用它交出最后一句。

## 文件布局

```
workspace/audio/<会议编号>/          目录权限 700
  mic-0000.wav, mic-0001.wav …       每块 60 秒，16k 单声道 16 位
  mic-0005.wav.part                  正在写的一块（头部大小待补；崩溃后可读、可补封）
  heard.jsonl                        每句识别结果：at, seconds, text, line_id, speaker_id, echo, emb(base64)
  timeline.jsonl                     音频时钟与墙钟的对应：start / gap / restart / end
```

## 和路线图相比的调整

- **丢帧不补零**：丢了多少记进 `timeline.jsonl`，录音保持连续，重放才和现场喂给 `Ears` 的完全一致。
- **健康只判「数字静音」一种**：形象页已有「听不到」的表达（`stage.js` 的 `deaf()`），静音并进去，不新做动作。只在当面开会时判：线上会议走虚拟声卡，没人说话时本来就是全零。
- **声纹不单独存文件**，并进 `heard.jsonl`。
- **记录里的段落锚点**挪到主线 A4（纪要回链）再做，这一阶段不改 Markdown 格式。

## 任务

### 任务 1：Recorder 和 AudioStore（`avatar/recorder.py`，`tests/test_recorder.py`）

- `AudioStore(root)`：`folder(id)`、`pcm(id) -> bytes`（按块序读，含 `.part`）、`seal(id)`（补封遗留的 `.part`）、`seal_all()`、`drop_audio(id)`（删声音、留 jsonl）、`rows(id, name)`（读 jsonl）。
- `Recorder(folder, keep)`：`write(pcm)`（攒满 1 秒交给写盘线程；跨块边界时切开），`event(**row)`、`heard(row)`，`close()`（写完、补封、等线程结束）。`keep()` 返回 `never` 时不写声音。
- 测试：块边界切得对、读回来和写进去一样、补封后是合法 WAV、`never` 不产生声音文件、`drop_audio` 留下 jsonl、遗留 `.part` 能读能封。

### 任务 2：AudioIn（`avatar/audio_in.py`，`tests/test_audio_in.py`）

- 帧格式：`b"PCM1"` + 4 字节小端序号 + PCM；没有头的帧照旧当纯 PCM（旧页面、现有测试）。
- 按会议编号懒开 `Recorder`，编号变了先收掉上一场；`finish()` 收尾并按 `keep_audio == "never"` 删声音。
- 序号跳号记 `gap`（丢了几帧），序号变小记 `restart`（页面刷新）。
- 当面开会时连续 5 秒全零：`mic_health = "silent"`；来了非零帧马上回 `ok`。
- 把音频时钟传给 `Ears.feed(pcm, clock=...)`。
- 测试：序号与时钟、跳号与重启、静音判定只在当面开会生效、换会议时上一场收尾。

### 任务 3：Ears 带位置、可冲刷（`avatar/ears.py`，`tests/test_ears.py`）

- `feed(pcm, clock=None)`：重置后第一帧记下基准；每段 `at = (基准 + front.start) / 16000`；`on_utterance(text, emb, seconds=, at=)`。
- 断句参数提成类属性（`vad_threshold`、`min_speech`、`max_speech`、`threads`），重放可以改。
- `flush()` 交出最后一句，`drain()` 等所有识别做完。
- 测试用假 VAD 和假识别：位置换算、重置后重新锚定、冲刷后能等到结果。

### 任务 4：接线（`avatar/meeting.py`、`floor.py`、`api.py`、`config.py`）

- `Line.at`；`Meeting.mic_health`（进快照）；`Floor.on_utterance(..., at=None)` 透传。
- `Hub`：`audio_store`、`audio`；`Ears` 回调改成 `Hub._heard`（转给 `floor`，再记 `heard.jsonl`）；`ws_audio` 改喂 `hub.audio.feed`；散会和关闭时 `audio.finish()`；启动时 `seal_all()`。
- 设置 `keep_audio`：`always`（默认）/ `until_polished` / `never`，在 `Settings.update` 里校验；`until_polished` 在整理稿写进记录后（或纪要判定为空时）删声音。
- 测试（`test_api.py`）：带头的帧落盘、散会后有 WAV、`heard.jsonl` 有这句话、`keep_audio` 校验、`until_polished` 整理后删声音留文字、状态里有 `mic_health`。

### 任务 5：界面（`audio.js`、`stage.js`、`api.js`、`panel.html/js`、`tests/test_pages.py`）

- `audio.js`：帧加头，序号在发送前就递增（发不出去的帧也占号，后台才看得出丢了）；`stop()` 时挂起播放用的 `AudioContext`（不挂起会让电脑一直不休眠）。
- `stage.js`：`deaf()` 包含 `mic_health === 'silent'`，提示改成说人话的「麦克风一直是静音」。
- 设置窗口：麦克风、扬声器下面加「录音」下拉框 `set-keep-audio`；`test_pages.py` 补 id。

### 任务 6：重放评测台（`avatar/replay.py`，`tests/test_replay.py`）

- `uv run python -m avatar.replay [编号|latest] [--set 参数=值 …] [--threshold 0.55] [--save 名字] [--diff heard|名字]`。
- 核心函数 `replay(pcm, ears, book)` 可注入假耳朵测试；命令行部分只做参数和打印。
- 输出逐句结果、句数、说话人数、识别耗时与录音时长之比；`--diff` 逐句比对。

### 任务 7：文档与回归

- README：接口表（状态里的 `mic_health`、设置 `keep_audio`、`/ws/audio` 帧格式）、录音存放位置、重放命令。
- CLAUDE.md：「会议记录」一节补录音目录和 `heard.jsonl`；「约定」里调参用 `avatar.replay`。
- `uv run pytest`；演示后台看设置窗口浅色、深色、窄屏；桌面应用里开一场真会验证落盘和静音判定（不碰 8765）。
