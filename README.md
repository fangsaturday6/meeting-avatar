# 会议数字人

一个跑在本机的 AI 同事：听得懂多人讨论，会主动接话、提问、总结，别人一插话就停下；
能在后台联网查资料、读项目代码、出设计稿，查完口头汇报，详细结果显示在它的画面上。
当面开会和线上会议（腾讯会议、飞书等）共用同一套。

大脑用的是本机已登录的 `claude` 命令行，不需要任何密钥。语音识别、断句、声纹、语音合成全部在本地运行。

## 启动

它是一个桌面应用：打开就是它本人，没有别的。

```bash
uv run python -m avatar.models      # 首次：下载本地语音模型（约 440MB，逐个文件校验）
cd app && npm install && npm run pack   # 首次：打包出 app/dist/小克.app，拖到「应用程序」里
```

之后双击「小克」就行。应用会自己把后台起起来，退出时一起关掉；后台已经在跑的话就直接接上。

| 窗口 | 里面有什么 |
|---|---|
| 主窗口 | 只有它本人。状态看颜色和动作：闭眼=没开会，青=在听，紫=托下巴在想，琥珀=举手想发言，橙红=在说；头顶有光球在转=后台在干活；话筒翻上去=已静音 |
| 设置窗口 | 「记录」：对话、任务、打字发言、派任务；「设置」：会议背景、场景、名字、嗓音、麦克风和扬声器等 |

主窗口底部的控制条：会前是「开始」和设置；开会后是静音、让它停下、结束会议、会议记录、设置，鼠标动一下才出现。
同样的操作在菜单栏「会议」里也有，带快捷键。

不想装应用也可以：`uv run python -m avatar` 之后用 Chrome 打开 http://127.0.0.1:8765/ ，是同一个界面。

## 当面开会

1. 点设置，场景选「当面开会」，填好会议背景和项目目录。
2. 点「开始」，第一次会问你要麦克风权限。会议开始和接通声音是同一步。
3. 直接对它说话。叫它名字它一定回答；平时它自己判断该不该开口。
4. 在「记录」里点对话中的名字可以改名，之后它就知道谁是谁。

想给大家看就把主窗口投到电视或投影上。笔记本自带麦克风大约 2 米内可用；会议室大的话接一个会议全向麦，
在设置里选麦克风。

## 线上会议

数字人作为一个独立的与会者入会，所以这台电脑上的会议软件要登录数字人自己的账号，你本人用手机或另一台电脑入会。

1. 安装两个虚拟声卡和 OBS（需要管理员密码，只做一次）：

   ```bash
   brew install blackhole-2ch blackhole-16ch
   ```

   ```bash
   brew install --cask obs
   ```

2. 会议软件的音频设置：扬声器选 **BlackHole 2ch**，麦克风选 **BlackHole 16ch**。
3. 设置里：场景选「线上会议」，麦克风选 **BlackHole 2ch**（听会议里的人），扬声器选 **BlackHole 16ch**（把它的声音送进会议）。
4. OBS：添加「窗口采集」选小克的主窗口，点「启动虚拟摄像头」；会议软件的摄像头选 **OBS Virtual Camera**。
5. 建议把会议里的昵称写成「小克（AI）」这样，让其他人知道它不是真人。

## 它能做什么

| 你说 | 它做 |
|---|---|
| 「小克，你怎么看」 | 直接回答 |
| 别人之间在讨论 | 安静听；有实质补充时等空档再说 |
| 它说话时你插话 | 约 0.7 秒内停下，顺着你的话接 |
| 「查一下飞书有没有这个接口」 | 说「我查一下」，后台联网查，查完口头汇报 |
| 「登录逻辑在哪个文件」 | 后台只读方式查你登记的项目目录 |
| 「出一版登录页」 | 后台调用本机 Claude 写一个网页设计稿，显示在主窗口的资料板上 |
| 冷场 6 秒 | 主动推进或小结一次 |

## 接口

界面上的每个操作都有对应接口，AI Agent 不读页面也能参会和控制。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/state` | 全量状态：字幕、说话人、任务、设置 |
| POST | `/api/meeting/start`、`/api/meeting/stop` | 开始、结束会议 |
| POST | `/api/utterance` `{speaker, text}` | 以文字方式说一句话（没有麦克风时、Agent 参会时用） |
| POST | `/api/say` `{text}` | 让数字人直接念一句话 |
| POST | `/api/hush` | 让它立刻停下 |
| POST | `/api/mute` `{muted}` | 静音：只听不说 |
| PATCH | `/api/speakers/{id}` `{name}` | 说话人改名 |
| POST | `/api/tasks` `{kind, brief}` | 直接派后台任务，kind 为 查资料 / 查代码 / 做设计 |
| POST | `/api/board` `{task_id}` | 选择资料板显示哪个任务，`null` 为收起 |
| PATCH | `/api/settings` | 改名字、嗓音、语速、场景、项目目录、会议背景等 |
| WS | `/ws/events` | 状态事件流，连接后先收到全量快照 |
| WS | `/ws/audio` | 主窗口的音频通道（同一时间只有一个窗口负责收音和播放） |

界面上的每个按钮和表单项都有固定的 `id`，改版不改名（`tests/test_pages.py` 里有清单并会检查）。它们和接口的对应关系：

| 界面元素 | 接口 |
|---|---|
| `#btn-start` 开始 | `POST /api/meeting/start`，同时在这个窗口接通声音 |
| `#btn-stop` 结束会议 | `POST /api/meeting/stop` |
| `#btn-mute` 静音 | `POST /api/mute` |
| `#btn-hush` 让它停下 | `POST /api/hush` |
| `#btn-board-close` 收起资料板、「记录」里的 `.task[data-task-id]` | `POST /api/board` |
| `#composer`（`#say-kind` 选「说话」） | `POST /api/utterance` |
| `#composer`（`#say-kind` 选任务类型） | `POST /api/tasks` |
| 对话里的 `button.who[data-speaker-id]` | `PATCH /api/speakers/{id}` |
| `#settings` 里带 `name` 的每一项 | `PATCH /api/settings` |

主窗口的网址参数：`?autostart=1` 会议一开始就免点击接通声音（桌面应用默认带）；
`?pose=thinking&hearing&working&muted` 强行指定神态，看效果和截图用。

## 代码结构

| 文件 | 职责 |
|---|---|
| `avatar/floor.py` | 发言决策：什么时候想、什么时候开口、什么时候让话 |
| `avatar/ears.py` | 断句、识别、声纹 |
| `avatar/fast_brain.py` | 快脑：常驻的 Claude Haiku，负责接话 |
| `avatar/slow_brain.py` | 慢脑：按需启动本机 Claude Code 干活 |
| `avatar/mouth.py` | 语音合成：本地优先，在线和系统语音兜底 |
| `avatar/meeting.py`、`speakers.py`、`config.py` | 会议状态、说话人、设置 |
| `avatar/api.py` | REST 和 WebSocket |
| `avatar/web/stage.*` | 主窗口：形象、资料板、字幕、提示、控制条 |
| `avatar/web/panel.*` | 设置窗口：记录和设置 |
| `avatar/web/character.js` | 3D 形象，全部用代码建模（three.js，放在 `vendor/` 里，不联网）；`avatar.js` 是不支持 3D 时的 2D 备用形象 |
| `avatar/web/api.js`、`audio.js` | 接口与状态、收音与播放（两个窗口共用） |
| `avatar/demo.py` | 演示后台：不需要模型、网络和 Claude，数据放临时目录 |
| `app/` | 桌面外壳（Electron）：起后台、开窗口、菜单；`pack.js` 打包，`setup.js` 安装 Electron |

## 测试

```bash
uv run python -m avatar.demo       # 演示后台（8767 端口），改界面时用；加 --autoplay 自动演一场会
cd app && npm run demo             # 桌面应用接到演示后台
uv run pytest                      # 单元测试，不需要模型和网络
uv run pytest -m live -s           # 真实模型和本机 Claude
uv run python -m tests.e2e_voice   # 语音端到端：两位模拟与会者说话、提问、插话
```

## 实测数据（M1 / 16GB）

- 问完到它出声：2 到 3 秒（等对方确实说完 0.5 秒，大脑 1 到 1.5 秒，合成 0.3 秒）
- 有人插话到它停下：约 0.65 秒
- 5 秒语音的识别用时：约 0.1 秒
- 查代码约 30 秒，查资料约 40 秒，做设计 1 到 1.5 分钟

## 已知限制

- 几个人同时说话时识别会变差。
- 一两秒的短句分不清是谁说的，会算到上一位说话人头上。
- 本机 `claude` 命令行版本较旧时，干活只能用它的默认模型；`claude update` 之后可以在设置里换。
- 在线嗓音更自然，但依赖网络；网络不稳时会自动退回本地嗓音。
