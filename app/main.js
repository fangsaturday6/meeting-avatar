// 桌面外壳：接上后台（没在跑就自己起一个）→ 开主窗口 → 系统菜单。
// 界面本身是后台提供的页面（avatar/web/），这里不放任何界面逻辑。
//
// 环境变量：AVATAR_URL      接到指定的后台，不自己起（比如演示后台 http://127.0.0.1:8767）
//          AVATAR_PORT     后台端口（默认 8765）；这个端口上没有后台就自己起一个
//          AVATAR_PROJECT  项目目录（默认是本文件的上一级；打包后读 project.json）
//          AVATAR_PAGE     主窗口打开哪个地址（默认 /?autostart=1），AVATAR_SIZE 窗口大小（如 440x640）
//          AVATAR_THEME    开发用：强制 light 或 dark
//          AVATAR_SHOT     开发用：载入几秒后把主窗口截图存成这个 PNG 文件，然后退出；
//                          再加 AVATAR_ROUND=1 会裁成圆角方形（生成应用图标用）
const { app, BrowserWindow, Menu, dialog, ipcMain, nativeTheme, session, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const BASE = (process.env.AVATAR_URL || `http://127.0.0.1:${process.env.AVATAR_PORT || 8765}`).replace(/\/$/, '');
const PROJECT = process.env.AVATAR_PROJECT || packedProject() || path.resolve(__dirname, '..');
const ALLOWED = ['media', 'speaker-selection'];     // 页面只需要麦克风和选择扬声器
const PAGE = process.env.AVATAR_PAGE || '/?autostart=1';
const [WIDTH, HEIGHT] = (process.env.AVATAR_SIZE || '440x640').split('x').map(Number);
const SHOT = process.env.AVATAR_SHOT || '';
if (process.env.AVATAR_THEME) nativeTheme.themeSource = process.env.AVATAR_THEME;   // 开发用：light / dark

let win = null, panel = null, backend = null, widthBeforeBoard = 0;

function packedProject() {
  try { return JSON.parse(fs.readFileSync(path.join(__dirname, 'project.json'), 'utf8')).project; } catch { return ''; }
}

// ---------- 后台 ----------
async function ping() {
  try { return (await fetch(`${BASE}/api/state`, { signal: AbortSignal.timeout(1500) })).ok; } catch { return false; }
}

async function ensureBackend() {
  if (await ping()) return;                        // 已经有后台在跑（比如终端里起的），直接用
  if (process.env.AVATAR_URL) throw new Error(`没有连上 ${BASE}`);
  const port = new URL(BASE).port || '80';
  // 从访达启动时 PATH 里没有 uv，用登录 shell 并补上常见的安装位置
  const cmd = `export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"; cd ${JSON.stringify(PROJECT)} && exec uv run python -m avatar --port ${port}`;
  let log = '';
  backend = spawn('/bin/zsh', ['-lc', cmd], { stdio: ['ignore', 'pipe', 'pipe'] });
  backend.stdout.on('data', (d) => { log = (log + d).slice(-2000); });
  backend.stderr.on('data', (d) => { log = (log + d).slice(-2000); });
  let exited = false;
  backend.on('exit', () => { exited = true; backend = null; });
  for (let i = 0; i < 120; i++) {                  // 最多等一分钟
    if (await ping()) return;
    if (exited) break;
    await new Promise((r) => setTimeout(r, 500));
  }
  console.error(log);
  throw new Error('后台没能启动');
}

function stopBackend() {
  if (backend) { backend.kill('SIGTERM'); backend = null; }
}

// ---------- 窗口 ----------
const ours = (url) => url === BASE || url.startsWith(`${BASE}/`);
const bg = () => (nativeTheme.shouldUseDarkColors ? '#0f1012' : '#f4f3f1');

function openMain() {
  win = new BrowserWindow({
    width: WIDTH, height: HEIGHT, minWidth: 320, minHeight: 440, show: false,
    title: '小克', titleBarStyle: 'hiddenInset', backgroundColor: bg(),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      backgroundThrottling: false,                 // 窗口被挡住时也要照常收音、播放
      autoplayPolicy: 'no-user-gesture-required',  // 会议一开始就能出声，不用再点一下
    },
  });
  win.loadURL(BASE + PAGE);
  if (SHOT) win.webContents.once('did-finish-load', () => setTimeout(shoot, 4000));
  win.once('ready-to-show', () => win.show());
  win.on('closed', () => { win = null; app.quit(); });
  guard(win);
}

async function shoot() {
  let png = (await win.webContents.capturePage()).toPNG();
  if (process.env.AVATAR_ROUND) {
    // 按 macOS 图标的规格：四周留一成边距，圆角约为边长的 22%
    const url = await win.webContents.executeJavaScript(`new Promise((done) => {
      const img = new Image();
      img.onload = () => {
        const c = Object.assign(document.createElement('canvas'), { width: 1024, height: 1024 }), g = c.getContext('2d');
        g.beginPath(); g.roundRect(100, 100, 824, 824, 185); g.clip();
        g.drawImage(img, 100, 100, 824, 824);
        done(c.toDataURL('image/png'));
      };
      img.src = ${JSON.stringify('data:image/png;base64,' + png.toString('base64'))};
    })`);
    png = Buffer.from(url.split(',')[1], 'base64');
  }
  fs.writeFileSync(SHOT, png);
  app.quit();
}

// 设置窗口只开一个；再点就切到对应那一页并拿到前面
function openPanel(url) {
  if (panel && !panel.isDestroyed()) {
    panel.webContents.executeJavaScript(`location.hash = ${JSON.stringify(new URL(url).hash)}`);
    panel.show(); panel.focus();
    return;
  }
  const [x, y] = win.getPosition(), [w] = win.getSize();
  panel = new BrowserWindow({ width: 420, height: 700, minWidth: 340, minHeight: 420, x: x + w + 12, y, title: '设置', backgroundColor: bg() });
  panel.loadURL(url);
  panel.on('closed', () => { panel = null; });
  guard(panel);
}

// 本机页面留在应用里；别的网址交给系统浏览器
function guard(w) {
  w.webContents.setWindowOpenHandler(({ url }) => {
    if (!ours(url)) { if (/^https?:/.test(url)) shell.openExternal(url); return { action: 'deny' }; }
    if (new URL(url).pathname === '/panel') { openPanel(url); return { action: 'deny' }; }
    return { action: 'allow', overrideBrowserWindowOptions: { width: 1100, height: 800, backgroundColor: '#ffffff' } };   // 设计稿
  });
  w.webContents.on('will-navigate', (e, url) => {
    if (!ours(url)) { e.preventDefault(); if (/^https?:/.test(url)) shell.openExternal(url); }
  });
}

// ---------- 菜单：和页面上的按钮是同一组操作 ----------
function buildMenu() {
  const act = (name) => () => win && win.webContents.send('action', name);
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    { label: app.name, submenu: [
      { role: 'about', label: `关于${app.name}` },
      { type: 'separator' },
      { label: '设置…', accelerator: 'Cmd+,', click: act('settings') },
      { type: 'separator' },
      { role: 'hide', label: `隐藏${app.name}` }, { role: 'hideOthers', label: '隐藏其他' }, { role: 'unhide', label: '全部显示' },
      { type: 'separator' },
      { role: 'quit', label: `退出${app.name}` },
    ] },
    { label: '会议', submenu: [
      { label: '开始会议', accelerator: 'Cmd+Return', click: act('start') },
      { label: '结束会议', accelerator: 'Cmd+Shift+E', click: act('stop') },
      { type: 'separator' },
      { label: '让它停下', accelerator: 'Cmd+.', click: act('hush') },
      { label: '静音 / 取消静音', accelerator: 'Cmd+Shift+M', click: act('mute') },
      { type: 'separator' },
      { label: '会议记录', accelerator: 'Cmd+L', click: act('notes') },
    ] },
    { role: 'editMenu', label: '编辑' },
    { label: '窗口', submenu: [
      { label: '总在最前', type: 'checkbox', click: (item) => win && win.setAlwaysOnTop(item.checked, 'floating') },
      { role: 'minimize', label: '最小化' }, { role: 'zoom', label: '缩放' },
      { type: 'separator' },
      { role: 'reload', label: '重新载入' }, { role: 'toggleDevTools', label: '开发者工具' },
    ] },
  ]));
}

// ---------- 启动 ----------
if (!app.requestSingleInstanceLock()) app.quit();
app.on('second-instance', () => { if (win) { win.show(); win.focus(); } });
app.on('before-quit', stopBackend);
ipcMain.on('board', (_e, on) => {
  if (!win) return;
  const [w, h] = win.getSize();
  if (on && w < 960) { widthBeforeBoard = w; win.setSize(1080, Math.max(h, 640), true); }
  else if (!on && widthBeforeBoard) { win.setSize(widthBeforeBoard, h, true); widthBeforeBoard = 0; }
});

app.whenReady().then(async () => {
  buildMenu();
  session.defaultSession.setPermissionRequestHandler((wc, permission, done, details) => done(ALLOWED.includes(permission) && ours(details.requestingUrl || wc.getURL())));
  session.defaultSession.setPermissionCheckHandler((_wc, permission, origin) => ALLOWED.includes(permission) && ours(origin.replace(/\/$/, '')));
  try {
    await ensureBackend();
  } catch (e) {
    dialog.showErrorBox('小克没能启动', `${e.message}。\n请在终端里进入项目目录运行 uv run python -m avatar 看看报了什么。\n项目目录：${PROJECT}`);
    return app.quit();
  }
  openMain();
});
