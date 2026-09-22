/* ==========================================================================
   编辑模块：叠加式内容编辑 + 页面管理

   设计原则（很重要，决定了它能稳定工作）：
     1. **不改原文**。所有编辑都是"往上盖"的新对象，原 PDF 字节不动。
     2. **数据与渲染分离**。editState.objects 是唯一真相，
        渲染层只是它的投影，随时可整层重画。
     3. **坐标即存即转**。屏幕上拖出来的框，立刻转成 PDF 坐标存起来；
        渲染时再从 PDF 坐标转回屏幕。这样缩放、旋转后都不会错位。
     4. **不做回流重排**。PDF 里没有重排所需的结构信息，硬做必崩。

   坐标转换：
      屏幕坐标 -> PDF 坐标：
        pdfX = (screenX - pageLeft) / scale
        pdfY = pageHeight - (screenY - pageTop) / scale      // y 轴翻转
      （这里是简化式，真实的 pageHeight 来自服务端的 MediaBox）
   ========================================================================== */

import { state, els, api, toast, uid } from '/static/core.js';
// 纯几何/数据变换放在 geom.js —— 那边不依赖 state/DOM/pdf.js，
// 所以可以在 node 里单测（见 webtest.mjs）。这里只做「读状态 + 转调」。
import {
  screenToNorm as gScreenToNorm,
  normToScreen as gNormToScreen,
  normToPdf as gNormToPdf,
  pdfToNorm as gPdfToNorm,
  hitTestNorm,
  payloadObjects,
  hasAnyEdit,
  makeTextBoxAuto,
} from '/static/geom.js';

/* --------------------------------------------------------------------------
   状态
   -------------------------------------------------------------------------- */

export const editState = {
  on: false,                 // 编辑模式是否开启
  tool: null,                // 当前工具：whiteout | text | rect | ellipse | line | select
  objects: [],               // 编辑对象列表（唯一真相）
  pages: {                   // 页面结构改动
    order: null,             // null 表示原序
    delete: [],              // 被删页下标
    rotate: {},              // 下标 -> 角度增量描述
    insert: [],              // { at, width, height }
  },
  pageInfo: [],              // 服务端返回的各页尺寸 [{width,height,rotate}]
  selectedId: null,          // 当前选中的对象
  dirty: false,              // 是否有未保存改动
  tempShape: null,           // 拖拽中的临时对象
};

let bound = false;
let saveTimer = null;

/* 外部注入的依赖（避免与 app.js 循环引用） */
let deps = {
  goToPage: () => {},
  renderPage: () => {},
  refreshPages: null,        // 重建页面（增删页后调用）
};

export function bindEditHelpers(inject) {
  deps = { ...deps, ...inject };
}

/* --------------------------------------------------------------------------
   坐标转换
   -------------------------------------------------------------------------- */

/* 取某页的 PDF 尺寸（来自服务端 MediaBox） */
export function pageSize(pageNum) {
  const info = editState.pageInfo[pageNum - 1];
  if (info && info.width > 0 && info.height > 0) {
    return { width: info.width, height: info.height };
  }
  // 兜底：A4
  return { width: 595, height: 842 };
}

/*
  屏幕坐标 -> 页面内归一化坐标（0~1）。

  用归一化而不是绝对 PDF 点，好处是**缩放后完全不用改数据** ——
  scale 变了，归一化值不变，渲染时乘新的尺寸即可。
  最终提交给服务端时再乘页面尺寸换成 PDF 点。

  纯逻辑在 geom.js（可单测），这里只负责量 DOM 再转调。
*/
export function screenToNorm(rec, clientX, clientY) {
  const r = rec.wrap.getBoundingClientRect();
  return gScreenToNorm(clientX, clientY, r.left, r.top, r.width, r.height);
}

/* 归一化 -> 屏幕像素（相对 viewer） */
export function normToScreen(rec, nx, ny, nw, nh) {
  const r = rec.wrap.getBoundingClientRect();
  return gNormToScreen(nx, ny, nw, nh, r.width, r.height);
}

/*
  归一化坐标 -> PDF 坐标（原点左下、y 向上）。

  PDF 的 y 轴方向和屏幕相反，所以：
      pdfY = pageHeight * (1 - (ny + nh))
  这是最容易写错的地方，务必保持「先翻转再乘尺寸」的顺序。
*/
export function normToPdf(pageNum, nx, ny, nw, nh) {
  const { width, height } = pageSize(pageNum);
  return gNormToPdf(nx, ny, nw, nh, width, height);
}

/* PDF 坐标 -> 归一化（回显用） */
export function pdfToNorm(pageNum, x, y, w, h) {
  const { width, height } = pageSize(pageNum);
  return gPdfToNorm(x, y, w, h, width, height);
}

/* --------------------------------------------------------------------------
   持久化
   -------------------------------------------------------------------------- */

export async function loadEdits(docId) {
  if (!docId) return;
  try {
    const data = await api(`/api/edits?doc=${encodeURIComponent(docId)}`);
    const p = data.pages || {};
    editState.objects = Array.isArray(data.objects) ? data.objects : [];
    editState.pages = {
      order: p.order || null,
      delete: Array.isArray(p.delete) ? p.delete : [],
      rotate: p.rotate || {},
      insert: Array.isArray(p.insert) ? p.insert : [],
    };
    editState.dirty = false;
  } catch (e) {
    console.warn('读取编辑数据失败：', e.message);
    editState.objects = [];
  }
}

export function scheduleSave() {
  editState.dirty = true;
  updateSaveHint();
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, 500);
}

export async function doSave() {
  if (!state.docId) return;
  if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
  try {
    await api('/api/edits', {
      method: 'POST',
      body: {
        docId: state.docId,
        doc: { name: state.fileName },
        pages: editState.pages,
        objects: editState.objects,
      },
    });
    editState.dirty = false;
    updateSaveHint();
  } catch (e) {
    toast('编辑数据保存失败：' + e.message, 2600);
  }
}

function updateSaveHint() {
  const el = els.editSaveHint;
  if (!el) return;
  if (editState.dirty) {
    el.textContent = '有未保存的改动…';
    el.className = 'edit-hint dirty';
  } else {
    el.textContent = '改动已保存';
    el.className = 'edit-hint';
  }
}

/* --------------------------------------------------------------------------
   对象的增删改
   -------------------------------------------------------------------------- */

export function addObject(obj) {
  const item = { id: uid(), ...obj };
  editState.objects.push(item);
  scheduleSave();
  repaintEdits();
  return item;
}

export function removeObject(id) {
  const i = editState.objects.findIndex(o => o.id === id);
  if (i < 0) return false;
  editState.objects.splice(i, 1);
  if (editState.selectedId === id) editState.selectedId = null;
  scheduleSave();
  repaintEdits();
  return true;
}

export function updateObject(id, patch) {
  const obj = editState.objects.find(o => o.id === id);
  if (!obj) return false;
  Object.assign(obj, patch);
  // ★ 渲染缓存必须作废。_norm 是「避免反复换算」的缓存，
  //   位置一变还留着它，渲染就会停在**旧位置**（拖动时踩过这个坑）。
  //   作废后 renderObjectEl 会从 x/y/w/h 重算，结果一致、不会漂。
  if (!('_norm' in patch)) obj._norm = null;
  scheduleSave();
  repaintEdits();
  return true;
}

export function clearAllObjects() {
  editState.objects = [];
  editState.selectedId = null;
  scheduleSave();
  repaintEdits();
}

/* --------------------------------------------------------------------------
   渲染：把编辑对象画到页面上的编辑层
   -------------------------------------------------------------------------- */

const EDIT_LAYER_CLASS = 'edit-layer';

function ensureLayer(rec) {
  let layer = rec.wrap.querySelector('.' + EDIT_LAYER_CLASS);
  if (!layer) {
    layer = document.createElement('div');
    layer.className = EDIT_LAYER_CLASS;
    rec.wrap.appendChild(layer);
  }
  return layer;
}

/* 重画所有已渲染页面的编辑层 */
export function repaintEdits() {
  for (const rec of state.pages) {
    if (!rec.rendered) continue;
    paintPageEdits(rec);
  }
  // 所有对象变更都会经过这里，顺带刷新涂黑计数
  // （不在每个 add/remove 里各写一遍，避免漏掉某条路径）
  syncRedactCount();
}

export function paintPageEdits(rec) {
  if (!rec) return;
  const layer = ensureLayer(rec);

  // 清掉旧内容（保留拖拽中的预览由 CSS 层单独处理）
  layer.innerHTML = '';

  const mine = editState.objects.filter(o => o.page === rec.num);
  for (const obj of mine) {
    const el = renderObjectEl(obj, rec);
    if (!el) continue;
    layer.appendChild(el);
    // ★ 文字对象的命中范围必须在**插入 DOM 之后**量 ——
    // 元素不在文档里时 getBoundingClientRect 全是 0（踩过）。
    // 存成归一化值：缩放/旋转后每次重画都会刷新，不会失效。
    measureTextBox(obj, el, rec);
  }

  // 拖拽中的临时对象
  if (editState.tempShape && editState.tempShape.page === rec.num) {
    const t = renderObjectEl(editState.tempShape, rec, true);
    if (t) layer.appendChild(t);
  }
}

/*
  量文字对象**渲染出来的盒子**，写进 obj._box（归一化）。

  为什么需要：数据层里文字是「锚点」不是矩形（w/h = 0，对应 PDF 的
  Td + Tj），所以算不出可点范围。唯一正确的来源就是渲染结果 ——
  命中测试的定义本来就是「用户看得见的地方就该点得中」。

  没有这一步的后果：文字写得进去，但**选不中、删不掉、挪不了**。
*/
function measureTextBox(obj, el, rec) {
  if (obj.kind !== 'text') return;
  const r = rec.wrap.getBoundingClientRect();
  const b = el.getBoundingClientRect();
  if (!r.width || !r.height || !b.width) {
    obj._box = null;
    return;
  }
  obj._box = {
    x: (b.left - r.left) / r.width,
    y: (b.top - r.top) / r.height,
    w: b.width / r.width,
    h: b.height / r.height,
  };
}

function renderObjectEl(obj, rec, isTemp = false) {
  const el = document.createElement('div');
  el.className = 'ed ed-' + obj.kind;
  el.dataset.id = obj.id || '__temp__';
  if (isTemp) el.classList.add('temp');

  const p = obj._norm || pdfToNorm(obj.page, obj.x, obj.y, obj.w, obj.h);
  const s = normToScreen(rec, p.x, p.y, p.w, p.h);

  if (obj.kind === 'line') {
    // 线用 SVG 画，才能画出斜线
    return renderLineEl(obj, rec, isTemp);
  }

  el.style.left = s.left + 'px';
  el.style.top = s.top + 'px';
  el.style.width = Math.max(1, s.width) + 'px';
  el.style.height = Math.max(1, s.height) + 'px';
  el.style.setProperty('--c', obj.color || '#000000');

  if (obj.kind === 'whiteout') {
    el.style.background = '#ffffff';
  } else if (obj.kind === 'redact') {
    // 涂黑：视觉上画成**红色虚线框 + 半透明红底**，而不是实心黑。
    // 这是刻意的 —— 实心黑会让人以为"已经盖住了、安全了"，
    // 但涂黑尚未导出（还没真正删掉底层文字）。
    // 红色虚线框传达的是"这块待销毁"的语义，
    // 导出后的文件里才是实心黑。
    el.style.border = '1.5px dashed #dc2626';
    el.style.background = 'rgba(220, 38, 38, 0.14)';
    el.classList.add('ed-redact');
  } else if (obj.kind === 'rect') {
    el.style.border = `${Math.max(1, obj.lineWidth || 1)}px solid ${obj.color || '#000'}`;
    if (obj.fill) {
      el.style.background = hexWithAlpha(obj.color || '#000000', 0.18);
    }
  } else if (obj.kind === 'ellipse') {
    el.style.borderRadius = '50%';
    el.style.border = `${Math.max(1, obj.lineWidth || 1)}px solid ${obj.color || '#000'}`;
    if (obj.fill) {
      el.style.background = hexWithAlpha(obj.color || '#000000', 0.18);
    }
  } else if (obj.kind === 'text') {
    // 文字：按页面实际尺寸估算字号，保证视觉与导出一致
    const size = (obj.size || 12) * (rec.wrap.getBoundingClientRect().width /
      (pageSize(obj.page).width || 595));
    el.style.fontSize = Math.max(6, size) + 'px';
    el.style.color = obj.color || '#000000';
    el.style.lineHeight = '1.25';
    el.textContent = obj.text || '';
    // ★ 必须放在**最后**：上面按矩形算出来的宽高对文字是 1px，会把文字裁没。
    //   数据层里文字只有锚点（w/h = 0），盒子要按内容自适应。
    makeTextBoxAuto(el);
  }

  if (!isTemp && editState.selectedId === obj.id) {
    el.classList.add('on');
  }
  return el;
}

function renderLineEl(obj, rec, isTemp) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'ed ed-svg' + (isTemp ? ' temp' : ''));
  svg.dataset.id = obj.id || '__temp__';
  svg.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;' +
    'overflow:visible;pointer-events:none';

  const r = rec.wrap.getBoundingClientRect();
  const w = r.width || 1, h = r.height || 1;

  // 用归一化坐标换算到页面像素
  const n1 = obj._norm ? { x: obj._norm.x, y: obj._norm.y }
    : pdfToNorm(obj.page, obj.x, obj.y, 0, 0);
  const n2 = obj._norm
    ? { x: obj._norm.x2, y: obj._norm.y2 }
    : pdfToNorm(obj.page, obj.x2, obj.y2, 0, 0);

  const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  line.setAttribute('x1', n1.x * w);
  line.setAttribute('y1', n1.y * h);
  line.setAttribute('x2', n2.x * w);
  line.setAttribute('y2', n2.y * h);
  line.setAttribute('stroke', obj.color || '#000000');
  line.setAttribute('stroke-width', String(obj.lineWidth || 1.5));
  line.setAttribute('stroke-linecap', 'round');
  svg.appendChild(line);
  return svg;
}

function hexWithAlpha(hex, alpha) {
  const h = (hex || '#000').replace('#', '');
  const full = h.length === 3 ? h.split('').map(c => c + c).join('') : h;
  const r = parseInt(full.slice(0, 2), 16) || 0;
  const g = parseInt(full.slice(2, 4), 16) || 0;
  const b = parseInt(full.slice(4, 6), 16) || 0;
  return `rgba(${r},${g},${b},${alpha})`;
}

/* --------------------------------------------------------------------------
   工具：把屏幕拖出的框转成编辑对象
   -------------------------------------------------------------------------- */

/* 由两次屏幕坐标（拖拽起止点）生成一个矩形类对象 */
export function makeRectObject(rec, pageNum, start, end, kind, opts = {}) {
  const r = rec.wrap.getBoundingClientRect();
  const w = r.width || 1, h = r.height || 1;

  const left = Math.min(start.x, end.x);
  const top = Math.min(start.y, end.y);
  const width = Math.abs(end.x - start.x);
  const height = Math.abs(end.y - start.y);

  const nx = (left - r.left) / w;
  const ny = (top - r.top) / h;
  const nw = width / w;
  const nh = height / h;

  const pdf = normToPdf(pageNum, nx, ny, nw, nh);
  return {
    page: pageNum,
    kind,
    x: pdf.x, y: pdf.y, w: pdf.w, h: pdf.h,
    color: opts.color || '#000000',
    lineWidth: opts.lineWidth || 1,
    fill: !!opts.fill,
    stroke: opts.stroke !== false,
    text: opts.text || '',
    size: opts.size || 12,
    font: opts.font || 'Helvetica',
  };
}

/* 由两点生成一条线 */
export function makeLineObject(rec, pageNum, start, end, opts = {}) {
  const r = rec.wrap.getBoundingClientRect();
  const w = r.width || 1, h = r.height || 1;

  const n1 = { x: (start.x - r.left) / w, y: (start.y - r.top) / h };
  const n2 = { x: (end.x - r.left) / w, y: (end.y - r.top) / h };

  const { width, height } = pageSize(pageNum);
  return {
    page: pageNum,
    kind: 'line',
    x: n1.x * width,
    y: height * (1 - n1.y),
    x2: n2.x * width,
    y2: height * (1 - n2.y),
    w: 0, h: 0,
    color: opts.color || '#000000',
    lineWidth: opts.lineWidth || 1.5,
  };
}

/* 命中测试：点在哪个编辑对象上（用于选中/删除） */
/* 命中测试：把屏幕点换成归一化坐标，再交给 geom.js 的纯逻辑 */
export function hitTest(rec, clientX, clientY) {
  const r = rec.wrap.getBoundingClientRect();
  const nx = (clientX - r.left) / (r.width || 1);
  const ny = (clientY - r.top) / (r.height || 1);
  return hitTestNorm(editState.objects, rec.num, nx, ny, pageSize);
}

/* --------------------------------------------------------------------------
   页面管理
   -------------------------------------------------------------------------- */

/* 当前有效页序（考虑删除后重排） */
export function effectiveOrder() {
  const total = state.pageCount;
  const del = new Set(editState.pages.delete);
  let order = editState.pages.order
    ? [...editState.pages.order]
    : Array.from({ length: total }, (_, i) => i);
  order = order.filter(i => !del.has(i));
  return order;
}

export function markPageDeleted(index) {
  const del = new Set(editState.pages.delete);
  if (del.has(index)) del.delete(index);
  else del.add(index);
  editState.pages.delete = [...del].sort((a, b) => a - b);
  scheduleSave();
  refreshPagePanel();
}

export function rotatePage(index, delta) {
  const cur = parseInt(editState.pages.rotate[index] || 0, 10);
  editState.pages.rotate[index] = ((cur + delta) % 360 + 360) % 360;
  scheduleSave();
  refreshPagePanel();
}

export function movePage(from, to) {
  const order = editState.pages.order
    ? [...editState.pages.order]
    : Array.from({ length: state.pageCount }, (_, i) => i);
  if (from < 0 || from >= order.length || to < 0 || to >= order.length) return;
  const [x] = order.splice(from, 1);
  order.splice(to, 0, x);
  editState.pages.order = order;
  scheduleSave();
  refreshPagePanel();
}

export function insertBlank(afterIndex) {
  const info = editState.pageInfo[0] || { width: 595, height: 842 };
  editState.pages.insert.push({
    at: afterIndex + 1,
    width: info.width || 595,
    height: info.height || 842,
  });
  scheduleSave();
  refreshPagePanel();
}

/* 刷新页面管理面板 */
export function refreshPagePanel() {
  const box = els.pageListBody;
  if (!box) return;
  box.innerHTML = '';

  const total = state.pageCount;
  const del = new Set(editState.pages.delete);
  const order = editState.pages.order;

  // 面板按「当前展示顺序」列出：先重排，再标删除
  const seq = order ? [...order] : Array.from({ length: total }, (_, i) => i);

  const head = document.createElement('div');
  head.className = 'pm-head';
  const liveCount = seq.filter(i => !del.has(i)).length +
    editState.pages.insert.length;
  head.innerHTML = `<span>共 <b>${total}</b> 页，保留 <b>${liveCount}</b> 页` +
    `</span>`;
  box.appendChild(head);

  seq.forEach((origIdx, pos) => {
    const row = document.createElement('div');
    row.className = 'pm-row' + (del.has(origIdx) ? ' deleted' : '');
    const rot = editState.pages.rotate[origIdx] || 0;

    row.innerHTML = `
      <span class="pm-pos">${pos + 1}</span>
      <span class="pm-name">原第 ${origIdx + 1} 页${
        rot ? ` <em>旋转 ${rot}°</em>` : ''}</span>
      <span class="pm-ops">
        <button class="pm-btn" data-act="up" title="上移">↑</button>
        <button class="pm-btn" data-act="down" title="下移">↓</button>
        <button class="pm-btn" data-act="rot" title="旋转 90°">⟳</button>
        <button class="pm-btn danger" data-act="del" title="${
          del.has(origIdx) ? '恢复' : '删除'}">${
          del.has(origIdx) ? '↺' : '×'}</button>
      </span>`;

    row.querySelectorAll('.pm-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const act = btn.dataset.act;
        if (act === 'del') markPageDeleted(origIdx);
        else if (act === 'rot') rotatePage(origIdx, 90);
        else if (act === 'up') movePage(pos, pos - 1);
        else if (act === 'down') movePage(pos, pos + 1);
      });
    });
    box.appendChild(row);
  });

  // 插入空白页的行
  if (editState.pages.insert.length) {
    const ins = document.createElement('div');
    ins.className = 'pm-insert-note';
    ins.textContent = `另有 ${editState.pages.insert.length} 张空白页待插入`;
    box.appendChild(ins);
  }

  const actions = document.createElement('div');
  actions.className = 'pm-actions';
  actions.innerHTML = `
    <button class="mini-btn" id="pmInsert">末尾插入空白页</button>
    <button class="mini-btn" id="pmReset">重置页面改动</button>`;
  box.appendChild(actions);

  actions.querySelector('#pmInsert').addEventListener('click', () => {
    insertBlank(seq.length - 1);
  });
  actions.querySelector('#pmReset').addEventListener('click', () => {
    editState.pages = { order: null, delete: [], rotate: {}, insert: [] };
    scheduleSave();
    refreshPagePanel();
    toast('已重置页面改动', 1400);
  });

  const hint = document.createElement('div');
  hint.className = 'pm-tip';
  hint.textContent = '页面改动会在导出时生效，原文件不受影响。';
  box.appendChild(hint);
}

/* --------------------------------------------------------------------------
   导出
   -------------------------------------------------------------------------- */

export function buildExportPayload() {
  // 对象部分的变换在 geom.js —— 那里是纯函数、可以单测。
  // 其中一条关键约束写在那边：**涂黑对象不参与普通导出**
  // （普通导出是叠加式的，删不掉底层文字；让它走这条路会产出
  //  「看着是黑块、文字其实还在」的文件。涂黑必须走 /api/redact）。
  return {
    path: state.filePath,
    pages: editState.pages,
    objects: payloadObjects(editState.objects),
  };
}

export async function exportPdf() {
  if (!state.filePath) {
    toast('请先打开一个文档', 1600);
    return;
  }

  const payload = buildExportPayload();
  if (!hasAnyEdit(payload)) {
    toast('还没有任何编辑内容', 1800);
    return;
  }

  const btn = els.btnExportPdf;
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }

  try {
    const r = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!r.ok) {
      let msg = `导出失败 (${r.status})`;
      try {
        const j = await r.json();
        if (j.error) msg = j.error;
      } catch (e) { /* 忽略 */ }
      throw new Error(msg);
    }

    const blob = await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    let name = `${state.fileName}-已编辑.pdf`;
    const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
    if (m) {
      try { name = decodeURIComponent(m[1]); } catch (e) { /* 忽略 */ }
    }

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);

    const pages = r.headers.get('X-Export-Pages');
    const dropped = r.headers.get('X-Export-Dropped');

    // 中文字体里没有的字符写不进去 —— **必须让用户看到**。
    // 静默丢字是最糟的结果：用户以为写进去了，实际没有。
    const missRaw = r.headers.get('X-Export-CJK-Missing') || '';
    let miss = '';
    if (missRaw) {
      try { miss = decodeURIComponent(missRaw); } catch (e) { miss = missRaw; }
    }

    let msg = `已导出：${name}` + (pages ? `（${pages} 页）` : '');
    if (dropped && dropped !== '0') msg += `，${dropped} 项被忽略`;
    if (miss) {
      msg += `　⚠ 有 ${[...miss].length} 个字没能写入（字体里没有）：${miss}`;
      toast(msg, 8000);
    } else {
      toast(msg, 3200);
    }
  } catch (e) {
    toast(e.message || '导出失败', 3000);
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}

/* --------------------------------------------------------------------------
   涂黑（redaction）—— 与普通编辑完全分开的一条路径

   为什么分开：普通编辑是「叠加」，可逆、不破坏原文；
   涂黑是**信息销毁**，不可逆。两者风险等级差太远，
   混在同一个导出流程里会让人放松警惕。

   所以涂黑有自己的一套：
     - 独立工具（redact），视觉上是红色虚线框
     - 独立导出按钮与独立的二次确认
     - 只走 /api/redact，**永远另存为**，不写回原文件
   -------------------------------------------------------------------------- */

/* 取当前所有涂黑区域（转成 PDF 坐标） */
export function redactRegions() {
  return editState.objects
    .filter(o => o.kind === 'redact')
    .map(o => {
      const x = Number(o.x) || 0;
      const y = Number(o.y) || 0;
      const w = Number(o.w) || 0;
      const h = Number(o.h) || 0;
      return { page: o.page, x, y, w, h };
    })
    .filter(r => r.w > 0 && r.h > 0);
}

export function redactCount() {
  return redactRegions().length;
}

/*
  执行涂黑导出。

  这里会做一次**本地预检**：如果某个涂黑框在页面上没盖到任何
  可选中的文字，就提前警告 —— 服务端也会查（零命中直接报错），
  但本地先查能给出更好的提示。

  注意：本地预检用的是 pdf.js 的文字层，可能和引擎的字形判定
  有细微差异，所以**不阻断**，只提示。
*/
export async function redactPdf() {
  if (!state.filePath) {
    toast('请先打开一个文档', 1600);
    return;
  }

  const regions = redactRegions();
  if (!regions.length) {
    toast('还没有涂黑区域，先用「涂黑」工具框住要销毁的内容', 2600);
    return;
  }

  const btn = els.btnRedactPdf;
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }

  try {
    const r = await fetch('/api/redact', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: state.filePath,
        regions,
        // 明确声明不覆盖原文件（服务端也会强制拒绝覆盖请求）
        overwrite: false,
      }),
    });

    if (!r.ok) {
      let msg = `涂黑失败 (${r.status})`;
      try {
        const j = await r.json();
        if (j.error) msg = j.error;
      } catch (e) { /* 忽略 */ }
      throw new Error(msg);
    }

    const blob = await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    let name = `${state.fileName}-已涂黑.pdf`;
    const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
    if (m) {
      try { name = decodeURIComponent(m[1]); } catch (e) { /* 忽略 */ }
    }

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 4000);

    const glyphs = r.headers.get('X-Redact-Glyphs') || '0';
    const pages = r.headers.get('X-Redact-Pages') || '0';
    const outline = r.headers.get('X-Redact-Outline') || '0';
    toast(`涂黑完成：${pages} 页，移除 ${glyphs} 个字形` +
      (outline !== '0' ? `，清理 ${outline} 条书签标题` : ''), 4200);
  } catch (e) {
    toast(e.message || '涂黑失败', 4000);
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}

/* --------------------------------------------------------------------------
   模式切换
   -------------------------------------------------------------------------- */

export function setEditMode(on) {
  editState.on = !!on;
  if (!editState.on) {
    editState.tool = null;
    editState.selectedId = null;
    editState.tempShape = null;
  }
  document.body.classList.toggle('edit-mode', editState.on);
  if (els.btnEditMode) els.btnEditMode.classList.toggle('on', editState.on);
  if (els.editPalette) els.editPalette.hidden = !editState.on;
  if (els.pagePanel) els.pagePanel.hidden = !editState.on;

  syncToolButtons();
  repaintEdits();
  if (editState.on) {
    refreshPagePanel();
    toast('编辑模式：拖拽绘制，点选可删除', 2200);
  }
}

export function setTool(tool) {
  editState.tool = tool;
  editState.selectedId = null;
  syncToolButtons();
  repaintEdits();
}

function syncToolButtons() {
  if (!els.editPalette) return;
  els.editPalette.querySelectorAll('[data-tool]').forEach(b => {
    b.classList.toggle('on', b.dataset.tool === editState.tool);
  });
  const cv = els.editColorValue;
  if (cv) cv.textContent = currentColor();
}

export function currentColor() {
  const el = els.editColor;
  return el && el.value ? el.value : '#111111';
}

/* --------------------------------------------------------------------------
   初始化
   -------------------------------------------------------------------------- */

export function bindEditUI() {
  if (bound) return;
  bound = true;

  els.btnEditMode && els.btnEditMode.addEventListener('click', () => {
    setEditMode(!editState.on);
  });

  if (els.editPalette) {
    els.editPalette.querySelectorAll('[data-tool]').forEach(btn => {
      btn.addEventListener('click', () => {
        const t = btn.dataset.tool;
        setTool(editState.tool === t ? null : t);
      });
    });
  }

  els.editColor && els.editColor.addEventListener('input', () => {
    const cv = els.editColorValue;
    if (cv) cv.textContent = currentColor();
  });

  els.btnEditClear && els.btnEditClear.addEventListener('click', () => {
    if (!editState.objects.length) { toast('没有编辑对象', 1400); return; }
    if (confirm(`确定清空全部 ${editState.objects.length} 个编辑对象？`)) {
      clearAllObjects();
      toast('已清空编辑对象', 1400);
    }
  });

  els.btnEditUndo && els.btnEditUndo.addEventListener('click', () => {
    const last = editState.objects.pop();
    if (!last) { toast('没有可撤销的对象', 1400); return; }
    if (editState.selectedId === last.id) editState.selectedId = null;
    scheduleSave();
    repaintEdits();
    toast('已撤销上一个对象', 1200);
  });

  els.btnExportPdf && els.btnExportPdf.addEventListener('click', exportPdf);

  // ---- 涂黑 ----
  els.btnRedactPdf && els.btnRedactPdf.addEventListener('click',
    openRedactConfirm);
  els.redactCancel && els.redactCancel.addEventListener('click',
    closeRedactConfirm);
  els.redactOk && els.redactOk.addEventListener('click', () => {
    closeRedactConfirm();
    redactPdf();
  });
  // 点遮罩空白处也可取消（但绝不默认确认）
  els.redactConfirm && els.redactConfirm.addEventListener('click', (e) => {
    if (e.target === els.redactConfirm) closeRedactConfirm();
  });

  syncRedactCount();
}

/* 更新涂黑区域计数显示 */
export function syncRedactCount() {
  const el = els.redactCount;
  if (!el) return;
  const n = redactCount();
  el.textContent = `${n} 处`;
  const btn = els.btnRedactPdf;
  if (btn) btn.disabled = n === 0;
}

/*
  打开二次确认框。

  确认框里把要删的区域摊开列清楚（哪几页、几处），
  让人有机会发现"我框错页了"。这是不可逆操作，
  宁可多一步确认，也不要让人事后才发现删错了。
*/
function openRedactConfirm() {
  const regions = redactRegions();
  if (!regions.length) {
    toast('还没有涂黑区域，先用「涂黑」工具框住要销毁的内容', 2600);
    return;
  }
  if (!els.redactConfirm) {
    // 兜底：万一对话框元素缺失，退回原生确认
    if (confirm(`确定永久删除 ${regions.length} 处涂黑区域内的文字？`)) {
      redactPdf();
    }
    return;
  }

  // 按页归组统计
  const byPage = {};
  regions.forEach(r => {
    byPage[r.page] = (byPage[r.page] || 0) + 1;
  });
  const lines = Object.keys(byPage)
    .sort((a, b) => Number(a) - Number(b))
    .map(p => `<li>第 ${p} 页：<b>${byPage[p]}</b> 处</li>`);

  if (els.redactConfirmStats) {
    els.redactConfirmStats.innerHTML =
      lines.join('') + `<li>合计：<b>${regions.length}</b> 处区域</li>`;
  }
  els.redactConfirm.hidden = false;
}

function closeRedactConfirm() {
  if (els.redactConfirm) els.redactConfirm.hidden = true;
}
