



import { t } from '/static/i18n.js';
import { state, els, api, toast, uid , errText} from '/static/core.js';

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

import { annotationsToPdfItems } from '/static/annot.js';




export const editState = {
  on: false,                 
  tool: null,                
  objects: [],               
  pages: {                   
    order: null,             
    delete: [],              
    rotate: {},              
    insert: [],              
  },
  pageInfo: [],              
  selectedId: null,          
  dirty: false,              
  tempShape: null,           
};

let bound = false;
let saveTimer = null;


let deps = {
  goToPage: () => {},
  renderPage: () => {},
  refreshPages: null,        
};

export function bindEditHelpers(inject) {
  deps = { ...deps, ...inject };
}





export function pageSize(pageNum) {
  const info = editState.pageInfo[pageNum - 1];
  if (info && info.width > 0 && info.height > 0) {
    return { width: info.width, height: info.height };
  }
  
  
  const base = Array.isArray(editState.pageInfo) ? editState.pageInfo.length : 0;
  const ins = editState.pages && editState.pages.insert;
  if (ins && pageNum > base) {
    const spec = ins[pageNum - base - 1];
    if (spec && spec.width > 0 && spec.height > 0) {
      return { width: spec.width, height: spec.height };
    }
  }
  
  return { width: 595, height: 842 };
}



export function screenToNorm(rec, clientX, clientY) {
  const r = rec.wrap.getBoundingClientRect();
  return gScreenToNorm(clientX, clientY, r.left, r.top, r.width, r.height);
}


export function normToScreen(rec, nx, ny, nw, nh) {
  const r = rec.wrap.getBoundingClientRect();
  return gNormToScreen(nx, ny, nw, nh, r.width, r.height);
}



export function normToPdf(pageNum, nx, ny, nw, nh) {
  const { width, height } = pageSize(pageNum);
  return gNormToPdf(nx, ny, nw, nh, width, height);
}


export function pdfToNorm(pageNum, x, y, w, h) {
  const { width, height } = pageSize(pageNum);
  return gPdfToNorm(x, y, w, h, width, height);
}




export async function loadEdits(docId) {
  
  
  editState.objects = [];
  editState.pages = { order: null, delete: [], rotate: {}, insert: [] };
  editState.selectedId = null;
  resetHistory();                       
  if (!docId) { editState.dirty = false; return; }
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
    console.warn(t('读取编辑数据失败：'), e.message);
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
    toast(t('编辑数据保存失败：') + e.message, 2600);
  }
}

function updateSaveHint() {
  const el = els.editSaveHint;
  
  document.dispatchEvent(new CustomEvent('pdf:edit-dirty'));
  if (!el) return;
  if (editState.dirty) {
    el.textContent = t('有未保存的改动…');
    el.className = 'edit-hint dirty';
  } else {
    el.textContent = t('改动已保存');
    el.className = 'edit-hint';
  }
}




const MAX_HISTORY = 60;
const history = { past: [], future: [] };
let lastSnap = null;
let histTimer = null;



function takeSnapshot() {
  const objects = editState.objects.map(o => {
    const c = { ...o };
    delete c._norm;
    delete c._box;
    return c;
  });
  return JSON.stringify({ objects, pages: editState.pages });
}



function ensureHistoryBaseline() {
  if (lastSnap === null) lastSnap = takeSnapshot();
}



function resetHistory() {
  history.past = [];
  history.future = [];
  lastSnap = null;
  syncHistoryButtons();
}

function commitHistory() {
  if (histTimer) { clearTimeout(histTimer); histTimer = null; }
  const cur = takeSnapshot();
  if (lastSnap === null) { lastSnap = cur; return; }
  if (cur === lastSnap) return;
  history.past.push(lastSnap);
  if (history.past.length > MAX_HISTORY) history.past.shift();
  history.future.length = 0;
  lastSnap = cur;
  syncHistoryButtons();
}



export function scheduleHistory() {
  if (histTimer) clearTimeout(histTimer);
  histTimer = setTimeout(commitHistory, 350);
}

function restoreSnapshot(json) {
  const s = JSON.parse(json);
  editState.objects = Array.isArray(s.objects) ? s.objects : [];
  editState.pages = s.pages || { order: null, delete: [], rotate: {}, insert: [] };
  editState.selectedId = null;
  lastSnap = takeSnapshot();
  scheduleSave();
  repaintEdits();
  refreshPagePanel();
  syncHistoryButtons();
}



let stageUndoHandler = null;


let stagePresent = false;

export function setStageUndoHandler(fn) {
  stageUndoHandler = fn;
}

export function setStagePresent(v) {
  stagePresent = !!v;
  syncHistoryButtons();
}

export async function undoEdit() {
  commitHistory();                                   
  if (!history.past.length) {
    
    if (stageUndoHandler) {
      let done = false;
      try { done = await stageUndoHandler(); } catch (e) { done = false; }
      if (done) { syncHistoryButtons(); return; }
    }
    toast(t('没有可撤销的操作'), 1200);
    return;
  }
  history.future.push(takeSnapshot());
  restoreSnapshot(history.past.pop());
  toast(t('已撤销'), 900);
}

export function redoEdit() {
  commitHistory();
  if (!history.future.length) { toast(t('没有可重做的操作'), 1200); return; }
  history.past.push(takeSnapshot());
  restoreSnapshot(history.future.pop());
  toast(t('已重做'), 900);
}

function syncHistoryButtons() {
  if (els.btnEditUndo) els.btnEditUndo.disabled = !history.past.length && !stagePresent;
  if (els.btnEditRedo) els.btnEditRedo.disabled = !history.future.length;
}



export function snapshotHistory() {
  return { past: history.past.slice(), future: history.future.slice(), lastSnap };
}

export function restoreHistory(h) {
  history.past = h && Array.isArray(h.past) ? h.past.slice() : [];
  history.future = h && Array.isArray(h.future) ? h.future.slice() : [];
  lastSnap = h ? h.lastSnap : null;
  syncHistoryButtons();
}



export function syncEditModeUI() {
  document.body.classList.toggle('edit-mode', editState.on);
  if (els.btnEditMode) els.btnEditMode.classList.toggle('on', editState.on);
  if (els.sidePanel) els.sidePanel.hidden = !editState.on;
  syncToolButtons();
  repaintEdits();
  syncHistoryButtons();
  if (editState.on) refreshPagePanel();
}




let clipboard = null;

function cloneObj(o) {
  const c = JSON.parse(JSON.stringify(o));
  delete c._norm;
  delete c._box;
  return c;
}

export function copySelected() {
  const o = editState.objects.find(x => x.id === editState.selectedId);
  if (!o) { toast(t('先选中一个对象再复制'), 1500); return; }
  clipboard = cloneObj(o);
  toast(t('已复制对象（Ctrl+V 粘贴）'), 1400);
}

export function duplicateSelected() {
  const o = editState.objects.find(x => x.id === editState.selectedId);
  if (!o) { toast(t('先选中一个对象'), 1500); return; }
  pasteObj(cloneObj(o));
}

export function pasteObject() {
  if (!clipboard) { toast(t('剪贴板为空（先选中对象按 Ctrl+C）'), 1800); return; }
  pasteObj(cloneObj(clipboard));
}


function pasteObj(base) {
  const D = 14;
  const o = { ...base, id: uid(), page: state.currentPage };
  o.x += D;
  o.y -= D;                                        
  if (o.x2 !== undefined && o.x2 !== null) { o.x2 += D; o.y2 -= D; }
  editState.objects.push(o);
  editState.selectedId = o.id;
  scheduleHistory();
  scheduleSave();
  repaintEdits();
  toast(t('已粘贴对象'), 900);
}




export function addObject(obj) {
  ensureHistoryBaseline();
  const item = { id: uid(), ...obj };
  editState.objects.push(item);
  scheduleHistory();
  scheduleSave();
  repaintEdits();
  return item;
}

export function removeObject(id) {
  const i = editState.objects.findIndex(o => o.id === id);
  if (i < 0) return false;
  ensureHistoryBaseline();
  editState.objects.splice(i, 1);
  if (editState.selectedId === id) editState.selectedId = null;
  scheduleHistory();
  scheduleSave();
  repaintEdits();
  return true;
}

export function updateObject(id, patch) {
  const obj = editState.objects.find(o => o.id === id);
  if (!obj) return false;
  ensureHistoryBaseline();
  Object.assign(obj, patch);
  
  
  
  if (!('_norm' in patch)) obj._norm = null;
  scheduleHistory();
  scheduleSave();
  repaintEdits();
  return true;
}

export function clearAllObjects() {
  editState.objects = [];
  editState.selectedId = null;
  scheduleHistory();
  scheduleSave();
  repaintEdits();
}




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


export function repaintEdits() {
  for (const rec of state.pages) {
    if (!rec.rendered) continue;
    paintPageEdits(rec);
  }
  
  
  syncRedactCount();
  syncReplaceCount();
}

export function paintPageEdits(rec) {
  if (!rec) return;
  const layer = ensureLayer(rec);

  
  layer.innerHTML = '';

  const mine = editState.objects.filter(o => o.page === rec.num);
  for (const obj of mine) {
    const el = renderObjectEl(obj, rec);
    if (!el) continue;
    layer.appendChild(el);
    
    
    
    measureTextBox(obj, el, rec);
  }

  
  const sel = mine.find(o => o.id === editState.selectedId);
  if (sel && editState.tool === 'select' && RESIZABLE_KINDS.has(sel.kind)) {
    renderResizeHandles(layer, sel, rec);
  }

  
  if (editState.tempShape && editState.tempShape.page === rec.num) {
    const t = renderObjectEl(editState.tempShape, rec, true);
    if (t) layer.appendChild(t);
  }
}



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



function textBaselineOffset(fontSize, lineHeight) {
  try {
    const ctx = document.createElement('canvas').getContext('2d');
    ctx.font = `${fontSize}px Helvetica, Arial, "NotoSansSC", sans-serif`;
    const m = ctx.measureText('Hg中');
    const asc = m.fontBoundingBoxAscent || m.actualBoundingBoxAscent || fontSize * 0.8;
    const desc = m.fontBoundingBoxDescent || m.actualBoundingBoxDescent || fontSize * 0.2;
    const content = asc + desc;
    return (fontSize * lineHeight - content) / 2 + asc;
  } catch (e) {
    
    return fontSize * 0.925;
  }
}

function renderObjectEl(obj, rec, isTemp = false) {
  const el = document.createElement('div');
  el.className = 'ed ed-' + obj.kind;
  el.dataset.id = obj.id || '__temp__';
  if (isTemp) el.classList.add('temp');

  const p = obj._norm || pdfToNorm(obj.page, obj.x, obj.y, obj.w, obj.h);
  const s = normToScreen(rec, p.x, p.y, p.w, p.h);

  if (obj.kind === 'line') {
    
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
    
    const size = (obj.size || 12) * (rec.wrap.getBoundingClientRect().width /
      (pageSize(obj.page).width || 595));
    const fs = Math.max(6, size);
    el.style.fontSize = fs + 'px';
    el.style.color = obj.color || '#000000';
    el.style.lineHeight = '1.25';
    el.textContent = obj.text || '';
    
    
    
    
    
    el.style.top = (s.top - textBaselineOffset(fs, 1.25)) + 'px';
    
    
    makeTextBoxAuto(el);
  } else if (obj.kind === 'replace') {
    
    
    
    el.style.border = '1.5px dashed #d97706';
    el.style.background = 'rgba(217, 119, 6, 0.12)';
    el.classList.add('ed-replace');
    const t = document.createElement('span');
    t.className = 'ed-replace-text';
    t.textContent = obj.text || '';
    el.appendChild(t);
  }

  if (!isTemp && editState.selectedId === obj.id) {
    el.classList.add('on');
  }
  return el;
}



const RESIZABLE_KINDS = new Set(['rect', 'ellipse', 'whiteout', 'redact', 'replace']);



function renderResizeHandles(layer, obj, rec) {
  const p = obj._norm || pdfToNorm(obj.page, obj.x, obj.y, obj.w, obj.h);
  const s = normToScreen(rec, p.x, p.y, p.w, p.h);
  const corners = [
    ['nw', s.left, s.top],
    ['ne', s.left + s.width, s.top],
    ['sw', s.left, s.top + s.height],
    ['se', s.left + s.width, s.top + s.height],
  ];
  for (const [name, cx, cy] of corners) {
    const hd = document.createElement('div');
    hd.className = 'ed-handle ed-handle-' + name;
    hd.dataset.id = obj.id;
    hd.dataset.corner = name;
    hd.style.left = cx + 'px';
    hd.style.top = cy + 'px';
    layer.appendChild(hd);
  }
}

function renderLineEl(obj, rec, isTemp) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'ed ed-svg' + (isTemp ? ' temp' : ''));
  svg.dataset.id = obj.id || '__temp__';
  svg.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;' +
    'overflow:visible;pointer-events:none';

  const r = rec.wrap.getBoundingClientRect();
  const w = r.width || 1, h = r.height || 1;

  
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



export function hitTest(rec, clientX, clientY) {
  const r = rec.wrap.getBoundingClientRect();
  const nx = (clientX - r.left) / (r.width || 1);
  const ny = (clientY - r.top) / (r.height || 1);
  return hitTestNorm(editState.objects, rec.num, nx, ny, pageSize);
}





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
  notifyPagesChanged();
  toast(t('已在末尾插入空白页（滚动到最下方可见）'), 2000);
}


function notifyPagesChanged() {
  document.dispatchEvent(new CustomEvent('pdf:pages-changed'));
}


export function refreshPagePanel() {
  const box = els.pageListBody;
  if (!box) return;
  box.innerHTML = '';

  const total = state.pageCount;
  const del = new Set(editState.pages.delete);
  const order = editState.pages.order;

  
  const seq = order ? [...order] : Array.from({ length: total }, (_, i) => i);

  const head = document.createElement('div');
  head.className = 'pm-head';
  const liveCount = seq.filter(i => !del.has(i)).length +
    editState.pages.insert.length;
  head.innerHTML = t('<span>共 <b>{total}</b> 页，保留 <b>{liveCount}</b> 页', { total, liveCount }) +
    `</span>`;
  box.appendChild(head);

  seq.forEach((origIdx, pos) => {
    const row = document.createElement('div');
    row.className = 'pm-row' + (del.has(origIdx) ? ' deleted' : '');
    const rot = editState.pages.rotate[origIdx] || 0;

    row.innerHTML = `
      <span class="pm-pos">${pos + 1}</span>
      <span class="pm-name">${t('原第 {n} 页', { n: origIdx + 1 })}${
        rot ? ` <em>${t('旋转 {deg}°', { deg: rot })}</em>` : ''}</span>
      <span class="pm-ops">
        <button class="pm-btn" data-act="up" title="${t('上移')}">↑</button>
        <button class="pm-btn" data-act="down" title="${t('下移')}">↓</button>
        <button class="pm-btn" data-act="rot" title="${t('旋转 90°')}">⟳</button>
        <button class="pm-btn danger" data-act="del" title="${
          del.has(origIdx) ? t('恢复') : t('删除')}">${
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

  
  if (editState.pages.insert.length) {
    const ins = document.createElement('div');
    ins.className = 'pm-insert-note';
    ins.textContent = t('另有 {length} 张空白页待插入', { length: editState.pages.insert.length });
    box.appendChild(ins);
  }

  const actions = document.createElement('div');
  actions.className = 'pm-actions';
  actions.innerHTML = t('\n    <button class="mini-btn" id="pmInsert">末尾插入空白页</button>\n    <button class="mini-btn" id="pmReset">重置页面改动</button>');
  box.appendChild(actions);

  actions.querySelector('#pmInsert').addEventListener('click', () => {
    insertBlank(seq.length - 1);
  });
  actions.querySelector('#pmReset').addEventListener('click', () => {
    editState.pages = { order: null, delete: [], rotate: {}, insert: [] };
    scheduleSave();
    refreshPagePanel();
    notifyPagesChanged();
    toast(t('已重置页面改动'), 1400);
  });

  const hint = document.createElement('div');
  hint.className = 'pm-tip';
  hint.textContent = t('页面改动会在导出时生效，原文件不受影响。');
  box.appendChild(hint);
}









export function downloadServerFile(path, name) {
  const q = new URLSearchParams({ path, download: '1' });
  if (name) q.set('name', name);
  const a = document.createElement('a');
  a.href = '/api/file?' + q.toString();
  a.download = name || '';
  a.rel = 'noopener';
  a.style.display = 'none';
  
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 0);
}


function downloadBlob(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

export function buildExportPayload() {
  
  
  
  
  return {
    
    
    
    path: state.contentPath || state.filePath,
    
    name: state.fileName,
    pages: editState.pages,
    objects: payloadObjects(editState.objects),
  };
}

export async function exportPdf(opts = {}) {
  if (!state.filePath) {
    toast(t('请先打开一个文档'), 1600);
    return;
  }

  const payload = buildExportPayload();
  
  
  if (opts.range) payload.range = String(opts.range);
  const nRedact = redactCount();
  const nReplace = replaceCount();
  const hasSpecial = nRedact > 0 || nReplace > 0;

  
  
  
  let annots = [];
  try {
    annots = await annotationsToPdfItems();
  } catch (e) {
    console.warn(t('批注换算失败，导出将不含批注：'), e);
  }
  if (annots.length) payload.annotations = annots;

  
  
  
  const noChange = !hasAnyEdit(payload) && !annots.length;
  if (noChange && hasSpecial) {
    toast(t('「导出 PDF」不含涂黑 / 替换。要保存它们请点对应的「并另存为…」') +
      t('（会连当前编辑一起带上）'), 4200);
    return;
  }

  
  
  
  
  if (hasSpecial) {
    const parts = [];
    if (nRedact > 0) parts.push(t('涂黑 {nRedact} 处', { nRedact }));
    if (nReplace > 0) parts.push(t('替换 {nReplace} 处', { nReplace }));
    const ok = confirm(
      t('你画了 {v}，但「导出 PDF」不包含这些操作。\n\n', { v: parts.join(t('、')) }) +
      t('· 要连它们一起保存：先取消，点对应的「并另存为…」。\\n') +
      t('  它作用在**当前版本**上，你刚改的段落会一起带进去。\\n') +
      t('· 继续导出：只导出其它编辑，这些区域不会被处理。\\n\\n确定继续导出吗？')
    );
    if (!ok) {
      toast(t('已取消。要执行请点对应的「并另存为…」'), 2800);
      return;
    }
  }

  
  
  
  const btns = [els.btnExportTop, els.btnExportPdf].filter(Boolean);
  if (btns.some(b => b.disabled)) return;
  const setBusy = (on) => btns.forEach(b => {
    b.disabled = on;
    b.classList.toggle('busy', on);
  });
  setBusy(true);

  try {
    const r = await fetch('/api/export', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (!r.ok) {
      let msg = t('导出失败 ({status})', { status: r.status });
      try {
        const j = await r.json();
        const m2 = errText(j); if (m2) msg = m2;
      } catch (e) {  }
      throw new Error(msg);
    }

    const cd = r.headers.get('Content-Disposition') || '';
    let name = t('{fileName}-已编辑.pdf', { fileName: state.fileName });
    const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
    if (m) {
      try { name = decodeURIComponent(m[1]); } catch (e) {  }
    }

    
    const rawOut = r.headers.get('X-Export-Path') || '';
    let outPath = rawOut;
    if (rawOut) {
      try { outPath = decodeURIComponent(rawOut); } catch (e) { outPath = rawOut; }
    }

    if (outPath) {
      
      
      downloadServerFile(outPath, name);
    } else {
      downloadBlob(await r.blob(), name);
    }

    const pages = r.headers.get('X-Export-Pages');
    const dropped = r.headers.get('X-Export-Dropped');

    
    
    const missRaw = r.headers.get('X-Export-CJK-Missing') || '';
    let miss = '';
    if (missRaw) {
      try { miss = decodeURIComponent(missRaw); } catch (e) { miss = missRaw; }
    }

    
    let msg = (noChange ? t('已导出原样副本：{name}', { name }) : t('已导出：{name}', { name })) +
              (pages ? t('（{pages} 页）', { pages }) : '');
    const nAnn = r.headers.get('X-Export-Annots');
    if (nAnn && nAnn !== '0') msg += t('，含 {nAnn} 条批注', { nAnn });
    if (dropped && dropped !== '0') msg += t('，{dropped} 项被忽略', { dropped });
    if (miss) {
      msg += t('　⚠ 有 {v} 个字没能写入（字体里没有）：{miss}', { v: [...miss].length, miss });
      toast(msg, 8000);
    } else {
      toast(msg, 3200);
    }

    
    
    if (outPath) {
      state.contentPath = outPath;
      state.savedPath = outPath;
    }
    await discardStage();
    return { ok: true, name, outPath };
  } catch (e) {
    toast(e.message || t('导出失败'), 3000);
    return { ok: false, error: e.message || String(e) };
  } finally {
    setBusy(false);
  }
}





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



export async function redactPdf() {
  if (!state.filePath) {
    toast(t('请先打开一个文档'), 1600);
    return;
  }

  const regions = redactRegions();
  if (!regions.length) {
    toast(t('还没有涂黑区域，先用「涂黑」工具框住要销毁的内容'), 2600);
    return;
  }

  const btn = els.btnRedactPdf;
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }

  try {
    const r = await fetch('/api/redact', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        
        
        
        
        path: state.contentPath || state.filePath,
        
        name: state.fileName,
        regions,
        
        overwrite: false,
      }),
    });

    if (!r.ok) {
      let msg = t('涂黑失败 ({status})', { status: r.status });
      try {
        const j = await r.json();
        const m2 = errText(j); if (m2) msg = m2;
      } catch (e) {  }
      throw new Error(msg);
    }

    
    const rawOut = r.headers.get('X-Out-Path') || '';
    let outPath = rawOut;
    if (rawOut) {
      try { outPath = decodeURIComponent(rawOut); } catch (e) { outPath = rawOut; }
    }
    const blob = outPath ? null : await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    let name = t('{fileName}-已涂黑.pdf', { fileName: state.fileName });
    const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
    if (m) {
      try { name = decodeURIComponent(m[1]); } catch (e) {  }
    }

    if (outPath) {
      downloadServerFile(outPath, name);
    } else {
      downloadBlob(blob, name);
    }

    const glyphs = r.headers.get('X-Redact-Glyphs') || '0';
    const pages = r.headers.get('X-Redact-Pages') || '0';
    const outline = r.headers.get('X-Redact-Outline') || '0';
    
    
    const merged = (state.stage && state.stage.id)
      ? t('（已包含尚未保存的段落编辑）') : '';
    toast(t('涂黑完成{merged}：{pages} 页，移除 {glyphs} 个字形', { merged, pages, glyphs }) +
      (outline !== '0' ? t('，清理 {outline} 条书签标题', { outline }) : ''), 4800);
  } catch (e) {
    toast(e.message || t('涂黑失败'), 4000);
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}





export function replaceRegions() {
  return editState.objects
    .filter(o => o.kind === 'replace')
    .map(o => {
      const x = Number(o.x) || 0;
      const y = Number(o.y) || 0;
      const w = Number(o.w) || 0;
      const h = Number(o.h) || 0;
      return { page: o.page, x, y, w, h, text: (o.text || '').toString() };
    })
    .filter(r => r.w > 0 && r.h > 0 && r.text.trim());
}

export function replaceCount() {
  return replaceRegions().length;
}

export async function replacePdf() {
  if (!state.filePath) {
    toast(t('请先打开一个文档'), 1600);
    return;
  }

  const regions = replaceRegions();
  if (!regions.length) {
    toast(t('还没有替换区域，先用「替换」工具框住要改的文字并输入新文字'), 3000);
    return;
  }

  const btn = els.btnReplacePdf;
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }

  try {
    const r = await fetch('/api/replace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: state.filePath,
        replacements: regions,
        
        overwrite: false,
      }),
    });

    if (!r.ok) {
      let msg = t('替换失败 ({status})', { status: r.status });
      try {
        const j = await r.json();
        const m2 = errText(j); if (m2) msg = m2;
      } catch (e) {  }
      throw new Error(msg);
    }

    
    const rawOut = r.headers.get('X-Out-Path') || '';
    let outPath = rawOut;
    if (rawOut) {
      try { outPath = decodeURIComponent(rawOut); } catch (e) { outPath = rawOut; }
    }
    const blob = outPath ? null : await r.blob();
    const cd = r.headers.get('Content-Disposition') || '';
    let name = t('{fileName}-已替换.pdf', { fileName: state.fileName });
    const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
    if (m) {
      try { name = decodeURIComponent(m[1]); } catch (e) {  }
    }

    if (outPath) {
      downloadServerFile(outPath, name);
    } else {
      downloadBlob(blob, name);
    }

    const glyphs = r.headers.get('X-Replace-Glyphs') || '0';
    const pages = r.headers.get('X-Replace-Pages') || '0';
    toast(t('替换完成：{pages} 页，改写 {glyphs} 个字形', { pages, glyphs }), 4200);
  } catch (e) {
    toast(e.message || t('替换失败'), 4000);
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}


export function syncReplaceCount() {
  const el = els.replaceCount;
  if (!el) return;
  const n = replaceCount();
  el.textContent = t('{n} 处', { n });
  const btn = els.btnReplacePdf;
  if (btn) btn.disabled = n === 0;
}




export function setEditMode(on) {
  editState.on = !!on;
  if (!editState.on) {
    editState.tool = null;
    editState.selectedId = null;
    editState.tempShape = null;
  }
  document.body.classList.toggle('edit-mode', editState.on);
  if (els.btnEditMode) els.btnEditMode.classList.toggle('on', editState.on);
  
  if (els.sidePanel) els.sidePanel.hidden = !editState.on;

  
  
  
  if (editState.on) {
    document.dispatchEvent(new CustomEvent('pdf:edit-on'));
  }

  syncToolButtons();
  repaintEdits();
  if (editState.on) {
    
    
    
    
    
    
    
    
    if (lastSnap === null) lastSnap = takeSnapshot();
    syncHistoryButtons();
    refreshPagePanel();
    toast(t('编辑模式：拖拽绘制，点选可删除'), 2200);
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




export function bindEditUI() {
  if (bound) return;
  bound = true;

  
  
  
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => { if (editState.on) repaintEdits(); });
  }

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
    if (!editState.objects.length) { toast(t('没有编辑对象'), 1400); return; }
    if (confirm(t('确定清空全部 {length} 个编辑对象？', { length: editState.objects.length }))) {
      clearAllObjects();
      toast(t('已清空编辑对象'), 1400);
    }
  });

  els.btnEditUndo && els.btnEditUndo.addEventListener('click', undoEdit);
  els.btnEditRedo && els.btnEditRedo.addEventListener('click', redoEdit);

  els.btnExportPdf && els.btnExportPdf.addEventListener('click', exportPdf);

  
  els.btnRedactPdf && els.btnRedactPdf.addEventListener('click',
    openRedactConfirm);
  els.redactCancel && els.redactCancel.addEventListener('click',
    closeRedactConfirm);
  els.redactOk && els.redactOk.addEventListener('click', () => {
    closeRedactConfirm();
    redactPdf();
  });
  
  els.redactConfirm && els.redactConfirm.addEventListener('click', (e) => {
    if (e.target === els.redactConfirm) closeRedactConfirm();
  });

  
  els.btnReplacePdf && els.btnReplacePdf.addEventListener('click', replacePdf);

  syncRedactCount();
  syncReplaceCount();
}


export function syncRedactCount() {
  const el = els.redactCount;
  if (!el) return;
  const n = redactCount();
  el.textContent = t('{n} 处', { n });
  const btn = els.btnRedactPdf;
  if (btn) btn.disabled = n === 0;
}



function openRedactConfirm() {
  const regions = redactRegions();
  if (!regions.length) {
    toast(t('还没有涂黑区域，先用「涂黑」工具框住要销毁的内容'), 2600);
    return;
  }
  if (!els.redactConfirm) {
    
    if (confirm(t('确定永久删除 {length} 处涂黑区域内的文字？', { length: regions.length }))) {
      redactPdf();
    }
    return;
  }

  
  const byPage = {};
  regions.forEach(r => {
    byPage[r.page] = (byPage[r.page] || 0) + 1;
  });
  const lines = Object.keys(byPage)
    .sort((a, b) => Number(a) - Number(b))
    .map(p => t('<li>第 {p} 页：<b>{v}</b> 处</li>', { p, v: byPage[p] }));

  if (els.redactConfirmStats) {
    els.redactConfirmStats.innerHTML =
      lines.join('') + t('<li>合计：<b>{length}</b> 处区域</li>', { length: regions.length });
  }
  els.redactConfirm.hidden = false;
}

function closeRedactConfirm() {
  if (els.redactConfirm) els.redactConfirm.hidden = true;
}



export async function reflowParagraph(page, para, text, orig, pageText, cell) {
  if (!state.filePath) {
    toast(t('请先打开一个文档'), 1600);
    return null;
  }
  if (!text || !text.trim()) {
    toast(t('文字不能为空。要整段删掉，请用涂黑工具'), 2600);
    return null;
  }

  const btn = els.paraOk;
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }
  try {
    const r = await fetch('/api/reflow', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      
      
      
      
      
      
      body: JSON.stringify({ path: state.contentPath || state.filePath,
                             name: state.fileName,
                             page, para, text,
                             orig: orig || null,
                             page_text: pageText || null,
                             
                             cell: cell || null,
                             stage: state.stage.id || null }),
    });

    if (!r.ok) {
      let msg = t('重排失败 ({status})', { status: r.status });
      try {
        const j = await r.json();
        const m2 = errText(j); if (m2) msg = m2;
      } catch (e) {  }
      toast(msg, 8000);
      return null;
    }

    const sid = r.headers.get('X-Stage-Id') || '';
    const rawPath = r.headers.get('X-Stage-Path') || '';
    let spath = rawPath;
    if (rawPath) {
      try { spath = decodeURIComponent(rawPath); } catch (e) { spath = rawPath; }
    }
    if (!sid || !spath) {
      toast(t('服务端没有返回预览位置，无法显示结果'), 6000);
      return null;
    }
    state.stage = { id: sid, path: spath };
    state.contentPath = spath;

    
    
    try { await r.arrayBuffer(); } catch (e) {  }

    const lb = r.headers.get('X-Reflow-Lines-Before');
    const la = r.headers.get('X-Reflow-Lines-After');
    const fbRaw = r.headers.get('X-Reflow-Fallback') || '';
    let fb = '';
    if (fbRaw) {
      try { fb = decodeURIComponent(fbRaw); } catch (e) { fb = fbRaw; }
    }
    return {
      stageId: sid,
      stagePath: spath,
      linesBefore: lb ? parseInt(lb, 10) : null,
      linesAfter: la ? parseInt(la, 10) : null,
      pages: parseInt(r.headers.get('X-Reflow-Pages') || '1', 10) || 1,
      newPages: parseInt(r.headers.get('X-Reflow-New-Pages') || '0', 10) || 0,
      fallback: fb,
    };
  } catch (e) {
    toast(t('重排失败：{v}', { v: e.message || e }), 5000);
    return null;
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}



export async function dropTableRow(page, para, gid) {
  if (!state.filePath) {
    toast(t('请先打开一个文档'), 1600);
    return null;
  }
  const btn = els.paraDropRow;
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }
  try {
    const r = await fetch('/api/table/drop_row', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: state.contentPath || state.filePath,
                             name: state.fileName,
                             page, para, gid,
                             stage: state.stage.id || null }),
    });
    if (!r.ok) {
      let msg = t('删行失败 ({status})', { status: r.status });
      try {
        const j = await r.json();
        const m2 = errText(j); if (m2) msg = m2;
      } catch (e) {  }
      toast(msg, 8000);
      return null;
    }
    const sid = r.headers.get('X-Stage-Id') || '';
    const rawPath = r.headers.get('X-Stage-Path') || '';
    let spath = rawPath;
    if (rawPath) {
      try { spath = decodeURIComponent(rawPath); } catch (e) { spath = rawPath; }
    }
    if (!sid || !spath) {
      toast(t('服务端没有返回预览位置，无法显示结果'), 6000);
      return null;
    }
    state.stage = { id: sid, path: spath };
    state.contentPath = spath;
    try { await r.arrayBuffer(); } catch (e) {  }

    let dropped = r.headers.get('X-Drop-Text') || '';
    if (dropped) { try { dropped = decodeURIComponent(dropped); } catch (e) {} }
    return {
      stageId: sid,
      stagePath: spath,
      dropped,
      height: parseFloat(r.headers.get('X-Drop-Height') || '0') || 0,
      movedRows: parseInt(r.headers.get('X-Drop-Rows') || '0', 10) || 0,
    };
  } catch (e) {
    toast(t('删行失败：{v}', { v: e.message || e }), 5000);
    return null;
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}



export async function insertTableRow(page, para, gid, where = 'below') {
  if (!state.filePath) {
    toast(t('请先打开一个文档'), 1600);
    return null;
  }
  const btn = where === 'above' ? els.paraInsertAbove : els.paraInsertBelow;
  if (btn) { btn.disabled = true; btn.classList.add('busy'); }
  try {
    const r = await fetch('/api/table/insert_row', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: state.contentPath || state.filePath,
                             name: state.fileName,
                             page, para, gid, where,
                             stage: state.stage.id || null }),
    });
    if (!r.ok) {
      let msg = t('插入行失败 ({status})', { status: r.status });
      try {
        const j = await r.json();
        const m2 = errText(j); if (m2) msg = m2;
      } catch (e) {  }
      toast(msg, 8000);
      return null;
    }
    const sid = r.headers.get('X-Stage-Id') || '';
    const rawPath = r.headers.get('X-Stage-Path') || '';
    let spath = rawPath;
    if (rawPath) {
      try { spath = decodeURIComponent(rawPath); } catch (e) { spath = rawPath; }
    }
    if (!sid || !spath) {
      toast(t('服务端没有返回预览位置，无法显示结果'), 6000);
      return null;
    }
    state.stage = { id: sid, path: spath };
    state.contentPath = spath;
    try { await r.arrayBuffer(); } catch (e) {  }

    return {
      stageId: sid,
      stagePath: spath,
      height: parseFloat(r.headers.get('X-Insert-Height') || '0') || 0,
      movedRows: parseInt(r.headers.get('X-Insert-Rows') || '0', 10) || 0,
    };
  } catch (e) {
    toast(t('插入行失败：{v}', { v: e.message || e }), 5000);
    return null;
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('busy'); }
  }
}



export async function discardStage() {
  const sid = state.stage.id;
  state.stage = { id: null, path: null };
  if (!sid) return;
  try {
    await fetch('/api/stage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: sid }),
    });
  } catch (e) {  }
}
