// 打包成可以双击的「小克.app」（只在这台 Mac 上用，不上架）。
// 做法：复制一份 Electron，放进外壳代码和项目位置，换名字和图标，在本机签名。
// 用法：npm run pack            → app/dist/小克.app
//      npm run pack -- 大锤     → 换个名字
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const NAME = process.argv[2] || require('./package.json').productName;
const SRC = path.join(__dirname, 'node_modules/electron/dist/Electron.app');
const OUT = path.join(__dirname, 'dist', `${NAME}.app`);
const RES = path.join(OUT, 'Contents/Resources');
const PLIST = path.join(OUT, 'Contents/Info.plist');
const run = (cmd, ...args) => execFileSync(cmd, args, { stdio: ['ignore', 'ignore', 'inherit'] });
const plist = (key, value) => run('plutil', '-replace', key, '-string', value, PLIST);

if (!fs.existsSync(SRC)) throw new Error('还没装 Electron，先在 app 目录运行 npm install');
fs.rmSync(OUT, { recursive: true, force: true });
fs.mkdirSync(path.dirname(OUT), { recursive: true });
run('ditto', SRC, OUT);

// 外壳代码 + 项目在哪（应用靠它找到后台）
fs.rmSync(path.join(RES, 'default_app.asar'), { force: true });
fs.mkdirSync(path.join(RES, 'app'));
for (const f of ['main.js', 'preload.js']) fs.copyFileSync(path.join(__dirname, f), path.join(RES, 'app', f));
fs.writeFileSync(path.join(RES, 'app/package.json'), JSON.stringify({ name: 'meeting-avatar-app', productName: NAME, version: require('./package.json').version, main: 'main.js' }));
fs.writeFileSync(path.join(RES, 'app/project.json'), JSON.stringify({ project: path.resolve(__dirname, '..') }));

// 名字、麦克风用途说明、图标
plist('CFBundleName', NAME);
plist('CFBundleDisplayName', NAME);
plist('CFBundleIdentifier', 'local.meeting-avatar');
plist('NSMicrophoneUsageDescription', '开会时用麦克风听大家说话。');
const icon = path.join(__dirname, 'icon.png');
if (fs.existsSync(icon)) {
  const set = path.join(__dirname, 'dist/icon.iconset');
  fs.rmSync(set, { recursive: true, force: true });
  fs.mkdirSync(set);
  for (const size of [16, 32, 128, 256, 512]) {
    run('sips', '-z', String(size), String(size), icon, '--out', path.join(set, `icon_${size}x${size}.png`));
    run('sips', '-z', String(size * 2), String(size * 2), icon, '--out', path.join(set, `icon_${size}x${size}@2x.png`));
  }
  run('iconutil', '-c', 'icns', set, '-o', path.join(RES, 'electron.icns'));
  fs.rmSync(set, { recursive: true, force: true });
}

run('codesign', '--force', '--deep', '--sign', '-', OUT);
console.log(`打包好了：${OUT}`);
