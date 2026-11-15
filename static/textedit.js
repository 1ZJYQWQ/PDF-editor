import { t } from '/static/i18n.js';

const ASCENT_FALLBACK = 0.88;

function measureFont(fontPx, family) {
  try {
    const ctx = document.createElement('canvas').getContext('2d');
    if (!ctx) return null;
    ctx.font = `${fontPx}px ${family}`;
    const m = ctx.measureText('国');
    const asc = m.fontBoundingBoxAscent;
    const desc = m.fontBoundingBoxDescent;
    if (asc > 0 && desc > 0) return { asc, desc };
  } catch (e) {  }
  return null;
}

const FONT_STACK =
  '"Microsoft YaHei", "PingFang SC", "Hiragino Sans GB", ' +
  '"Source Han Sans SC", "Noto Sans CJK SC", sans-serif';

let active = null;

export function isInlineEditing() {
  return !!active;
}

export function closeInlineEdit(commit) {
  const cur = active;
  if (!cur) return;
  active = null;
  cur.finish(commit);
}

export function beginInlineEdit(host, para, opts = {}) {
  if (!host || !para) return false;

  if (active) closeInlineEdit(true);

  const pw = Number(opts.pw) || Number(host.dataset.pdfW) || 0;
  const ph = Number(opts.ph) || Number(host.dataset.pdfH) || 0;
  if (!pw || !ph) return false;

  const cw = host.clientWidth;
  const ch = host.clientHeight;
  if (!cw || !ch) return false;

  const sx = cw / pw;
  const sy = ch / ph;

  const size = Number(para.size) || 12;
  const fontPx = Math.max(6, size * sy);

  const rows = Array.isArray(para.rows) ? para.rows : [];

  let gapPt = size * 1.5;
  if (rows.length > 1) {
    const d = Math.abs(rows[0].y - rows[1].y);
    if (d > size * 0.8 && d < size * 4) gapPt = d;
  }
  const linePx = gapPt * sy;

  const x0 = rows.length ? Math.min(...rows.map(r => r.x)) : para.x0;
  const x1 = rows.length ? Math.max(...rows.map(r => r.x1)) : para.x1;
  const firstBaseY = rows.length ? rows[0].y : para.y1;

  const left = x0 * sx;
  const width = Math.max(fontPx * 2, (x1 - x0) * sx);

  const baseScreenY = (ph - firstBaseY) * sy;
  const fm = measureFont(fontPx, FONT_STACK);
  const ascPx = fm ? fm.asc : ASCENT_FALLBACK * fontPx;
  const descPx = fm ? fm.desc : (1 - ASCENT_FALLBACK) * fontPx;
  const top = baseScreenY - ((linePx - (ascPx + descPx)) / 2 + ascPx);

  const box = document.createElement('div');
  box.className = 'inline-edit';
  box.setAttribute('contenteditable', 'plaintext-only');
  box.setAttribute('spellcheck', 'false');
  box.setAttribute('role', 'textbox');
  box.setAttribute('aria-multiline', 'true');

  box.style.left = left + 'px';
  box.style.top = top + 'px';
  box.style.width = width + 'px';
  box.style.fontSize = fontPx + 'px';
  box.style.lineHeight = linePx + 'px';
  box.style.fontFamily = FONT_STACK;

  const initial = rows.length
    ? rows.map(r => r.text).join('\n')
    : String(para.text || '');
  box.textContent = initial;

  host.appendChild(box);

  if (!box.isConnected) return false;

  let changed = false;
  let done = false;

  const instance = {
    box,
    finish(commit) {
      if (done) return;
      done = true;
      const value = readText(box);
      box.remove();
      if (commit) {

        if (value !== initial) opts.onCommit && opts.onCommit(value);
      } else if (changed) {
        opts.onCancel && opts.onCancel();
      }

      try { host.focus({ preventScroll: true }); } catch (e) {  }
    },
  };
  active = instance;

  box.addEventListener('input', () => { changed = true; });

  box.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closeInlineEdit(false);
      return;
    }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      closeInlineEdit(true);
      return;
    }

    e.stopPropagation();
  });

  box.addEventListener('keyup', (e) => e.stopPropagation());
  box.addEventListener('keypress', (e) => e.stopPropagation());

  box.addEventListener('blur', () => {

    setTimeout(() => { if (active === instance) closeInlineEdit(true); }, 0);
  });

  try {
    box.focus({ preventScroll: false });
    const range = document.createRange();
    range.selectNodeContents(box);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  } catch (e) {  }

  return true;
}

function readText(box) {
  let s = box.innerText;
  if (typeof s !== 'string') s = box.textContent || '';
  return s
    .replace(/\r\n?/g, '\n')
    .replace(/\u00a0/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .replace(/\s+$/, '');
}
