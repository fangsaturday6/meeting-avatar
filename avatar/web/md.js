// 极简 Markdown 渲染：先整体转义，再只放行标题、列表、粗体、行内代码、代码块和 http(s) 链接。
import { esc } from './api.js';

function inline(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/(^|[\s（(：:])(https?:\/\/[^\s<）)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
}

export function renderMarkdown(src) {
  const lines = esc(src || '').split('\n');
  const out = [];
  let list = null, para = [], code = null;
  const flushPara = () => { if (para.length) { out.push(`<p>${inline(para.join('<br>'))}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of lines) {
    if (code !== null) {
      if (line.trim().startsWith('```')) { out.push(`<pre><code>${code.join('\n')}</code></pre>`); code = null; }
      else code.push(line);
      continue;
    }
    if (line.trim().startsWith('```')) { flushPara(); flushList(); code = []; continue; }
    let m;
    if ((m = line.match(/^\s*(#{1,4})\s+(.*)$/))) {
      flushPara(); flushList(); out.push(`<h3>${inline(m[2])}</h3>`);
    } else if ((m = line.match(/^\s*(?:[-*•]|(\d+)[.、)])\s+(.*)$/))) {
      flushPara();
      const kind = m[1] ? 'ol' : 'ul';
      if (list !== kind) { flushList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[2])}</li>`);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList(); para.push(line.trim());
    }
  }
  if (code !== null) out.push(`<pre><code>${code.join('\n')}</code></pre>`);
  flushPara(); flushList();
  return out.join('');
}
