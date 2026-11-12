// 线条图标。按钮的名字写在 aria-label 里，图标只是给人看的。
const svg = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;

export const ICONS = {
  mute: svg('<path d="M11 5 6.5 9H3v6h3.5L11 19z"/><path d="m16 9.5 5 5m0-5-5 5"/>'),
  hush: svg('<rect x="5" y="5" width="14" height="14" rx="3"/>'),
  stop: svg('<path d="M2 15.2c5.6-5.6 14.4-5.6 20 0l-2.7 2.7-3.9-1.7v-3.1a12.5 12.5 0 0 0-6.8 0v3.1l-3.9 1.7z"/>'),
  notes: svg('<path d="M5 5h14a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-7l-4.5 3.5V17H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2z"/><path d="M8 9.5h8M8 12.8h5"/>'),
  settings: svg('<path d="M4 7h9m4 0h3M4 12h3m4 0h9M4 17h11m4 0h1"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17" r="2"/>'),
  open: svg('<path d="M13 5h6v6m0-6-8 8"/><path d="M10 6H7a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-3"/>'),
  back: svg('<path d="m14.5 5-7 7 7 7"/>'),
  close: svg('<path d="m6 6 12 12M18 6 6 18"/>'),
  send: svg('<path d="M12 19V5m-6 6 6-6 6 6"/>'),
};

// 把页面里所有 <button data-icon="名字"> 填上图标
export function fillIcons(root = document) {
  for (const el of root.querySelectorAll('[data-icon]')) el.innerHTML = ICONS[el.dataset.icon] || '';
}
