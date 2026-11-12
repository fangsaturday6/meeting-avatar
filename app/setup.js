// npm install 之后自动运行：把 Electron 的可执行程序放到位。
// Electron 自带的下载脚本不走代理，在这台机器上下不动。这里改成：先找本机缓存，没有再用 curl（走代理）
// 从国内镜像下载；两种来源都用 Electron 包里自带的官方校验值核对过才解包。
const { execFileSync } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const DIR = path.join(__dirname, 'node_modules/electron');
const { version } = require(path.join(DIR, 'package.json'));
const FILE = `electron-v${version}-${process.platform}-${process.arch}.zip`;
const WANT = require(path.join(DIR, 'checksums.json'))[FILE];
const PATH_TXT = { darwin: 'Electron.app/Contents/MacOS/Electron', linux: 'electron', win32: 'electron.exe' }[process.platform];
const CACHE = path.join(os.homedir(), process.platform === 'darwin' ? 'Library/Caches/electron' : '.cache/electron');

const installed = () => {
  try { return fs.readFileSync(path.join(DIR, 'dist/version'), 'utf8').replace(/^v/, '') === version && fs.existsSync(path.join(DIR, 'path.txt')); } catch { return false; }
};
const sha256 = (file) => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');

function findCached() {
  if (!fs.existsSync(CACHE)) return '';
  const hits = [path.join(CACHE, FILE), ...fs.readdirSync(CACHE).map((d) => path.join(CACHE, d, FILE))];
  return hits.find((f) => fs.existsSync(f) && sha256(f) === WANT) || '';
}

function download() {
  const to = path.join(CACHE, FILE);
  fs.mkdirSync(CACHE, { recursive: true });
  console.log(`正在下载 ${FILE}（约 120MB）…`);
  execFileSync('curl', ['-fL', '--retry', '8', '--retry-all-errors', '--retry-delay', '3', '-C', '-', '-o', to,
    `https://npmmirror.com/mirrors/electron/${version}/${FILE}`], { stdio: 'inherit' });
  if (sha256(to) !== WANT) { fs.rmSync(to); throw new Error('下载的 Electron 校验值不对，已删除，请重试'); }
  return to;
}

if (!WANT) throw new Error(`Electron 没有提供这个平台的程序：${FILE}`);
if (!installed()) {
  const zip = findCached() || download();
  const dist = path.join(DIR, 'dist');
  fs.rmSync(dist, { recursive: true, force: true });
  fs.mkdirSync(dist);
  if (process.platform === 'darwin') execFileSync('ditto', ['-x', '-k', zip, dist]);
  else execFileSync('unzip', ['-q', zip, '-d', dist]);
  fs.writeFileSync(path.join(DIR, 'path.txt'), PATH_TXT);
  console.log(`Electron ${version} 装好了`);
}
