// 页面和外壳之间只有两件事：菜单触发页面上的操作；资料板出现时让窗口变宽。
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('shell', {
  onAction: (cb) => ipcRenderer.on('action', (_e, name) => cb(name)),
  setBoard: (on) => ipcRenderer.send('board', !!on),
});
