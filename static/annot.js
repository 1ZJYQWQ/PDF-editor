



import { t } from '/static/i18n.js';
import { state, els, api, toast, uid, escapeHtml } from '/static/core.js';




export const annState = {
  docId: null,
  items: [],          
  
  
  
  ext: [],
  filter: 'all',
  activeId: null,
};

let saveTimer = null;




export async function loadAnnotations(docId) {
  annState.docId = docId;
  annState.items = [];
  annState.ext = [];        
  annState.activeId = null;
  if (!docId) { renderAnnotList(); return; }
  try {
    const data = await api(`/api/annotations?doc=${encodeURIComponent(docId)}`);
    if (data.ok) annState.items = data.items || [];
  } catch (err) {
    console.warn(t('读取标注失败'), err);
  }
  renderAnnotList();
  repaintAnnotations();
}


export function scheduleSave() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(doSave, 400);
}

export async function doSave() {
  if (!annState.docId) return;
  try {
    await api('/api/annotations', {
      method: 'POST',
      body: {
        docId: annState.docId,
        doc: { name: state.fileName },
        items: annState.items,
      },
    });
  } catch (err) {
    toast(t('标注保存失败：') + err.message, 2600);
  }
}






export function selectionToAnchors() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;

  const range = sel.getRangeAt(0);
  if (!range.toString().trim()) return null;

  
  let node = range.startContainer;
  let pageEl = null;
  while (node && node !== document.body) {
    if (node.classList && node.classList.contains('page')) { pageEl = node; break; }
    node = node.parentNode;
  }
  if (!pageEl) return null;

  const rec = state.pages[parseInt(pageEl.dataset.page, 10) - 1];
  if (!rec || !rec.textContent) return null;

  
  const byItem = buildSpanItemMap(rec);
  const spanItems = new Map();
  for (const [itemIndex, span] of byItem) spanItems.set(span, itemIndex);

  const parts = [];
  for (const [span, itemIndex] of spanItems) {
    if (!range.intersectsNode(span)) continue;
    if (Number.isNaN(itemIndex)) continue;

    const text = span.dataset.orig !== undefined ? span.dataset.orig : span.textContent;
    const len = text.length;
    let start = 0, end = len;

    if (span.contains(range.startContainer)) {
      start = offsetWithin(span, range.startContainer, range.startOffset);
    }
    if (span.contains(range.endContainer)) {
      end = offsetWithin(span, range.endContainer, range.endOffset);
    }
    start = Math.max(0, Math.min(start, len));
    end = Math.max(start, Math.min(end, len));
    if (start >= end) continue;

    parts.push({ itemIndex, charStart: start, charEnd: end });
  }
  if (!parts.length) return null;

  
  const text = parts.map(p => {
    const it = rec.textContent.items[p.itemIndex];
    return (it && it.str ? it.str : '').slice(p.charStart, p.charEnd);
  }).join('');

  return { page: rec.num, parts, text };
}



export function buildSpanItemMap(rec) {
  const map = new Map();                       
  if (!rec || !rec.textLayer) return map;

  const allSpans = [...rec.textLayer.querySelectorAll('span')];
  const indexed = new Set();

  for (const span of allSpans) {
    const k = span.dataset.itemIndex;
    if (k === undefined) continue;
    const idx = parseInt(k, 10);
    if (Number.isNaN(idx)) continue;
    map.set(idx, span);
    indexed.add(span);
  }

  
  const missing = allSpans.filter(s => !indexed.has(s));
  if (missing.length && rec.textContent && rec.textContent.items) {
    const used = new Set(map.keys());
    const freeItems = [];
    rec.textContent.items.forEach((it, i) => {
      if (it.str && !used.has(i)) freeItems.push(i);
    });
    const n = Math.min(missing.length, freeItems.length);
    for (let i = 0; i < n; i++) map.set(freeItems[i], missing[i]);
  }
  return map;
}



function offsetWithin(span, container, offset) {
  
  if (container === span) {
    let count = 0;
    let idx = 0;
    const walker = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      
      let top = n;
      while (top.parentNode && top.parentNode !== span) top = top.parentNode;
      const topIndex = [...span.childNodes].indexOf(top);
      if (topIndex >= offset) return count;
      count += n.textContent.length;
    }
    return count;
  }

  
  let count = 0;
  const walker = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if (n === container) {
      
      return count + offset;
    }
    count += n.textContent.length;
  }

  
  if (container.nodeType === 1 && span.contains(container)) {
    let acc = 0;
    const w2 = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
    let n2;
    while ((n2 = w2.nextNode())) {
      if (container.contains(n2)) {
        
        let inner = 0;
        for (let i = 0; i < offset && i < container.childNodes.length; i++) {
          inner += container.childNodes[i].textContent.length;
        }
        return acc + inner;
      }
      acc += n2.textContent.length;
    }
  }

  return count;
}




export function addAnnotation(sel, type, color, note = '') {
  const item = {
    id: uid(),
    type,
    color: color || '#fbbf24',
    page: sel.page,
    parts: sel.parts,
    text: sel.text,
    note,
    created: Date.now(),
  };
  annState.items.push(item);
  scheduleSave();
  renderAnnotList();
  
  repaintAnnotations();
  
  requestAnimationFrame(() => {
    const rec = state.pages[item.page - 1];
    if (rec && rec.rendered) {
      const has = rec.wrap.querySelector(`.annot-layer .an[data-id="${item.id}"]`);
      if (!has) paintPageAnnotations(rec);
    }
  });
  updateProgressMarkers();
  return item;
}

export function updateAnnotation(id, patch) {
  const it = annState.items.find(a => a.id === id);
  if (!it) return;
  Object.assign(it, patch);
  scheduleSave();
  renderAnnotList();
  repaintAnnotations();
}

export function removeAnnotation(id) {
  const i = annState.items.findIndex(a => a.id === id);
  if (i < 0) return;
  annState.items.splice(i, 1);
  if (annState.activeId === id) annState.activeId = null;
  scheduleSave();
  renderAnnotList();
  repaintAnnotations();
  updateProgressMarkers();
}




const TYPE_LABEL = {
  highlight: t('高亮'), underline: t('下划线'), wavy: t('波浪线'),
  strike: t('删除线'), note: t('笔记'), box: t('区域'),
};

export function repaintAnnotations() {
  let painted = 0;
  for (const rec of state.pages) {
    if (!rec.rendered) continue;
    paintPageAnnotations(rec);
    painted += 1;
  }
  if (window.__PDFVIEW_DEBUG__) {
    console.log(t('[标注] 重绘 {painted} 个已渲染页面，共 {length} 条标注', { painted, length: annState.items.length }));
  }
  return painted;
}




const _EXT_TYPES = {
  Highlight: 'highlight',
  Underline: 'underline',
  Squiggly: 'wavy',
  StrikeOut: 'strike',
  Square: 'box',
  Text: 'note',
};

export async function loadPagePdfAnnots(rec) {
  if (!state.pdf || !rec || rec.pdfAnnotsLoaded) return;
  rec.pdfAnnotsLoaded = true;

  let list;
  try {
    const page = await state.pdf.getPage(rec.num);
    list = await page.getAnnotations({ intent: 'display' });
  } catch (e) {
    return;                     
  }

  const out = [];
  for (const a of list || []) {
    const type = _EXT_TYPES[a.subtype];
    if (!type) continue;
    const c = Array.isArray(a.color) && a.color.length >= 3 ? a.color : null;
    out.push({
      type,
      color: c ? `rgb(${c[0]}, ${c[1]}, ${c[2]})` : '#f59e0b',
      quadPoints: Array.isArray(a.quadPoints) ? a.quadPoints : null,
      rect: Array.isArray(a.rect) ? a.rect : null,
      contents: (a.contentsObj && a.contentsObj.str) || '',
    });
  }

  
  
  
  out.forEach((o, i) => { o.eid = `ext-${rec.num}-${i}`; });
  annState.ext = (annState.ext || [])
    .filter(x => x.page !== rec.num)          
    .concat(out);
  rec.pdfAnnots = out;

  renderAnnotList();
  updateProgressMarkers();

  if (!out.length) {
    paintPageAnnotations(rec);
    return;
  }

  paintPageAnnotations(rec);
  if (!state.pdfAnnotHintShown) {
    state.pdfAnnotHintShown = true;
    toast(t('本文档自带 {length} 条 PDF 注释，已列入左侧「标注」（只读）', { length: annState.ext.length }), 3200);
  }
}


function bboxPx(vp, x0, y0, x1, y1) {
  const a = vp.convertToViewportPoint([x0, y0]);
  const b = vp.convertToViewportPoint([x1, y1]);
  return {
    left: Math.min(a[0], b[0]),
    top: Math.min(a[1], b[1]),
    width: Math.abs(b[0] - a[0]),
    height: Math.abs(b[1] - a[1]),
  };
}


function extBoxes(ex, vp) {
  const out = [];
  if (typeof vp.convertToViewportPoint !== 'function') return out;

  const qp = ex.quadPoints;
  if (qp && qp.length >= 8) {
    
    for (let i = 0; i + 7 < qp.length; i += 8) {
      const xs = [qp[i], qp[i + 2], qp[i + 4], qp[i + 6]];
      const ys = [qp[i + 1], qp[i + 3], qp[i + 5], qp[i + 7]];
      out.push(bboxPx(vp, Math.min(...xs), Math.min(...ys),
                      Math.max(...xs), Math.max(...ys)));
    }
    return out;
  }

  const r = ex.rect;
  if (r && r.length === 4) {
    out.push(bboxPx(vp, Math.min(r[0], r[2]), Math.min(r[1], r[3]),
                    Math.max(r[0], r[2]), Math.max(r[1], r[3])));
  }
  return out;
}

export function paintPageAnnotations(rec) {
  
  const old = rec.wrap.querySelector('.annot-layer');
  if (old) old.remove();

  const mine = annState.items.filter(a => a.page === rec.num);
  const ext = rec.pdfAnnots || [];
  if (!mine.length && !ext.length) return;

  const layer = document.createElement('div');
  layer.className = 'annot-layer';

  
  const spanByItem = buildSpanItemMap(rec);

  const dbg = { total: mine.length, noSpan: 0, noRect: 0, drawn: 0 };

  for (const an of mine) {
    
    if (an.type === 'box' && an.rect) {
      const el = document.createElement('div');
      el.className = 'an an-box';
      el.style.left = an.rect.x + '%';
      el.style.top = an.rect.y + '%';
      el.style.width = an.rect.w + '%';
      el.style.height = an.rect.h + '%';
      el.style.color = an.color || '#2563eb';
      el.dataset.id = an.id;
      if (an.note) el.classList.add('has-note');
      if (an.id === annState.activeId) el.classList.add('active');
      layer.appendChild(el);
      dbg.drawn += 1;
      continue;
    }

    
    const rects = [];
    for (const part of (an.parts || [])) {
      const span = spanByItem.get(part.itemIndex);
      if (!span) { dbg.noSpan += 1; continue; }

      const rs = charRects(span, part.charStart, part.charEnd);
      if (!rs.length) { dbg.noRect += 1; continue; }
      for (const r of rs) rects.push(r);
    }

    
    
    
    
    for (const r of mergeLineRuns(rects)) {
      const el = document.createElement('div');
      el.className = 'an an-' + an.type;
      el.style.left = r.left + 'px';
      el.style.top = r.top + 'px';
      el.style.width = r.width + 'px';
      el.style.height = r.height + 'px';
      el.dataset.id = an.id;
      el.dataset.page = rec.num;

      if (an.type === 'highlight') {
        el.style.background = hexToRgba(an.color, 0.42);
      } else {
        el.style.color = an.color;
      }
      if (an.note) el.classList.add('has-note');
      if (an.id === annState.activeId) el.classList.add('active');
      layer.appendChild(el);
      dbg.drawn += 1;
    }
  }

  
  
  if (ext.length && rec.viewport) {
    for (const ex of ext) {
      const boxes = mergeLineRuns(extBoxes(ex, rec.viewport));
      for (const r of boxes) {
        const el = document.createElement('div');
        el.className = 'an an-ext an-' + ex.type;
        el.style.left = r.left + 'px';
        el.style.top = r.top + 'px';
        el.style.width = r.width + 'px';
        el.style.height = r.height + 'px';
        el.style.color = ex.color;
        el.dataset.id = ex.eid;          
        if (ex.contents) el.title = ex.contents;
        if (ex.eid === annState.activeId) el.classList.add('active');
        layer.appendChild(el);
        dbg.drawn += 1;
      }
    }
  }

  
  if (dbg.drawn > 0 || layer.childElementCount) {
    rec.wrap.appendChild(layer);
  }

  if (window.__PDFVIEW_DEBUG__) {
    console.log(t('[标注] 第 {num} 页 已有{total}条 绘制{drawn}个矩形 ', { num: rec.num, total: dbg.total, drawn: dbg.drawn }) +
                t('找不到span:{noSpan} 无矩形:{noRect}', { noSpan: dbg.noSpan, noRect: dbg.noRect }));
    if (dbg.noSpan) {
      console.log(t('  可用 itemIndex:'), [...spanByItem.keys()].slice(0, 30).join(','),
                  t(' 需要:'), mine.flatMap(a => (a.parts || []).map(p => p.itemIndex)).join(','));
    }
  }
}



function charRects(span, start, end) {
  const textLen = (span.dataset.orig !== undefined
    ? span.dataset.orig : span.textContent).length;
  start = Math.max(0, Math.min(start, textLen));
  end = Math.max(start, Math.min(end, textLen));
  if (start === end || textLen === 0) return [];

  const pageEl = span.closest('.page');
  if (!pageEl) return [];
  const pageRect = pageEl.getBoundingClientRect();

  
  const rects = rectsViaRange(span, start, end, pageRect);
  if (rects.length) return rects;

  
  return rectsViaRatio(span, start, end, textLen, pageRect);
}

function rectsViaRange(span, start, end, pageRect) {
  
  const map = [];
  const walker = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    for (let i = 0; i < n.textContent.length; i++) map.push([n, i]);
  }
  if (map.length < end) return [];

  const s = map[start];
  const e = map[end - 1];
  if (!s || !e) return [];

  const range = document.createRange();
  try {
    range.setStart(s[0], s[1]);
    range.setEnd(e[0], e[1] + 1);
  } catch (err) {
    return [];
  }

  const out = [];
  const list = range.getClientRects();
  for (let i = 0; i < list.length; i++) {
    const cr = list[i];
    if (cr.width < 0.5 || cr.height < 0.5) continue;
    out.push({
      left: cr.left - pageRect.left,
      top: cr.top - pageRect.top,
      width: cr.width,
      height: cr.height,
    });
  }
  return out;
}

function rectsViaRatio(span, start, end, textLen, pageRect) {
  const r = span.getBoundingClientRect();
  if (r.width < 0.5 || r.height < 0.5) return [];
  const ratio = r.width / textLen;
  return [{
    left: r.left - pageRect.left + start * ratio,
    top: r.top - pageRect.top,
    width: (end - start) * ratio,
    height: r.height,
  }];
}



function mergeLineRuns(rects) {
  if (!rects || !rects.length) return [];
  const sorted = [...rects].sort((a, b) => a.top - b.top || a.left - b.left);

  const rows = [];
  for (const r of sorted) {
    const row = rows.find(g => Math.abs(g.top - r.top) <= Math.max(2.5, r.height * 0.25));
    if (row) row.items.push(r);
    else rows.push({ top: r.top, items: [r] });
  }

  const out = [];
  for (const row of rows) {
    row.items.sort((a, b) => a.left - b.left);
    let top = Infinity;
    let bottom = 0;
    for (const r of row.items) {
      top = Math.min(top, r.top);
      bottom = Math.max(bottom, r.top + r.height);
    }

    let cur = null;
    const segs = [];
    for (const r of row.items) {
      const right = r.left + r.width;
      if (cur && r.left <= cur.right + 1.6) {
        cur.right = Math.max(cur.right, right);
      } else {
        cur = { left: r.left, right };
        segs.push(cur);
      }
    }
    for (const s of segs) {
      out.push({
        left: s.left,
        top,
        width: Math.max(1, s.right - s.left),
        height: Math.max(1, bottom - top),
      });
    }
  }
  return out;
}




const _tcCache = new Map();


async function pageTextItems(pageNum) {
  if (_tcCache.has(pageNum)) return _tcCache.get(pageNum);
  const rec = state.pages[pageNum - 1];
  if (rec && rec.textContent) {
    _tcCache.set(pageNum, rec.textContent);
    return rec.textContent;
  }
  if (!state.pdf) return null;
  try {
    const page = await state.pdf.getPage(pageNum);
    const tc = await page.getTextContent();
    _tcCache.set(pageNum, tc);
    return tc;
  } catch (e) {
    return null;
  }
}


function itemRectPdf(item, start, end) {
  const t = item.transform || [1, 0, 0, 1, 0, 0];
  const h = Math.abs(Math.hypot(t[2], t[3])) || 10;   
  const len = (item.str || '').length || 1;
  const w = Math.abs(item.width || 0);
  const s = Math.max(0, Math.min(start, len));
  const e = Math.max(s, Math.min(end, len));
  const x = t[4];
  const y = t[5];
  
  return [x + (s / len) * w, y, x + (e / len) * w, y + h];
}


async function pageGeom(pageNum) {
  if (!state.pdf) return null;
  try {
    const page = await state.pdf.getPage(pageNum);
    const v = page.view;                     
    return { x0: v[0], y0: v[1], x1: v[2], y1: v[3],
             W: v[2] - v[0], H: v[3] - v[1] };
  } catch (e) {
    return null;
  }
}

export async function annotationsToPdfItems() {
  const list = annState.items || [];
  const out = [];

  for (const an of list) {
    const color = an.color || '#f59e0b';
    const note = an.note || '';

    
    if (an.type === 'box' && an.rect) {
      const g = await pageGeom(an.page);
      if (!g) continue;
      const x0 = g.x0 + (an.rect.x / 100) * g.W;
      const x1 = x0 + (an.rect.w / 100) * g.W;
      const y1 = g.y1 - (an.rect.y / 100) * g.H;
      const y0 = y1 - (an.rect.h / 100) * g.H;
      out.push({ page: an.page, type: 'box', rects: [[x0, y0, x1, y1]],
                 color, note });
      continue;
    }

    const tc = await pageTextItems(an.page);
    if (!tc || !tc.items) continue;

    const rects = [];
    for (const part of (an.parts || [])) {
      const item = tc.items[part.itemIndex];
      if (!item || !item.str) continue;
      const r = itemRectPdf(item, part.charStart, part.charEnd);
      if (r[2] - r[0] > 0.1 && r[3] - r[1] > 0.1) rects.push(r);
    }
    if (!rects.length) continue;

    out.push({ page: an.page, type: an.type, rects, color, note });
  }

  return out;
}

function hexToRgba(hex, alpha) {
  if (!hex) return `rgba(251,191,36,${alpha})`;
  const h = hex.replace('#', '');
  const v = h.length === 3
    ? h.split('').map(c => c + c).join('')
    : h;
  const r = parseInt(v.slice(0, 2), 16);
  const g = parseInt(v.slice(2, 4), 16);
  const b = parseInt(v.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}




export function renderAnnotList() {
  const box = els.annotList;
  if (!box) return;

  const mine = annState.items || [];
  const ext = annState.ext || [];
  const filt = annState.filter;

  
  const view = [
    ...mine.map(a => ({
      id: a.id, page: a.page, type: a.type, color: a.color,
      text: a.text || '', note: a.note || '',
      created: a.created || 0, isExt: false,
    })),
    ...ext.map(x => ({
      id: x.eid, page: x.page, type: x.type, color: x.color,
      text: '', note: x.contents || '',
      created: 0, isExt: true,
    })),
  ];

  const list = filt === 'all' ? view : view.filter(a => a.type === filt);
  const nAll = view.length;

  
  els.annotCount.textContent = nAll ? `${list.length}/${nAll}` : '';

  if (!list.length) {
    box.innerHTML = t('<div class="annot-empty">{v}</div>', { v: nAll
        ? t('当前筛选下没有标注')
        : t('还没有标注<br><span style="font-size:11.5px">选中文字即可添加高亮或笔记</span>') });
    return;
  }

  const sorted = [...list].sort((a, b) =>
    a.page - b.page || (a.created || 0) - (b.created || 0));

  box.innerHTML = sorted.map(a => `
    <div class="annot-card${a.id === annState.activeId ? ' active' : ''}${a.isExt ? ' is-ext' : ''}"
         data-id="${a.id}" data-ext="${a.isExt ? '1' : '0'}"
         style="--acard:${a.color || '#a1a1aa'}">
      <div class="ac-head">
        <span class="ac-type">${TYPE_LABEL[a.type] || a.type}</span>
        ${a.isExt ? `<span class="ac-src">${t('PDF 自带')}</span>` : ''}
        <span class="ac-page">${t('第 {n} 页', { n: a.page })}</span>
      </div>
      ${a.text ? `<div class="ac-text">${escapeHtml(a.text)}</div>` : ''}
      ${a.note
        ? `<div class="ac-note">${escapeHtml(a.note)}</div>`
        : (a.isExt ? `<div class="ac-note ac-none">${t('无备注文字')}</div>` : '')}
    </div>`).join('');

  box.querySelectorAll('.annot-card').forEach(card => {
    card.addEventListener('click', () => {
      if (card.dataset.ext === '1') focusExt(card.dataset.id);
      else focusAnnotation(card.dataset.id);
    });
  });
}



let goToPageForAnnot = () => {};
let renderPageForAnnot = () => {};


function revealAnnot(page, id) {
  if (page !== state.currentPage) goToPageForAnnot(page);

  const rec = state.pages[page - 1];
  if (rec && !rec.rendered) renderPageForAnnot(rec);

  setTimeout(() => {
    repaintAnnotations();
    renderAnnotList();
    const el = rec && rec.wrap.querySelector(`.an[data-id="${id}"]`);
    if (el) {
      const r = el.getBoundingClientRect();
      const vr = els.viewer.getBoundingClientRect();
      if (r.top < vr.top + 80 || r.bottom > vr.bottom - 80) {
        els.viewer.scrollBy({
          top: r.top - vr.top - vr.height / 2.4,
          behavior: 'smooth',
        });
      }
    }
  }, 120);
}

export function focusAnnotation(id) {
  const an = annState.items.find(a => a.id === id);
  if (!an) return;
  annState.activeId = id;
  revealAnnot(an.page, id);
}


export function focusExt(eid) {
  const ex = (annState.ext || []).find(x => x.eid === eid);
  if (!ex) return;
  annState.activeId = eid;
  revealAnnot(ex.page, eid);
}

export function bindNavHelpers(goToPage, renderPage) {
  goToPageForAnnot = goToPage;
  renderPageForAnnot = renderPage;
}




export function updateProgressMarkers() {
  const box = els.progressMarkers;
  if (!box) return;
  box.innerHTML = '';
  if (!state.pageCount) return;

  const seen = new Set();
  
  const allMark = [
    ...(annState.items || []),
    ...(annState.ext || []).map(x => ({ page: x.page, type: x.type, color: x.color })),
  ];
  for (const an of allMark) {
    const key = `${an.page}-${an.type}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const m = document.createElement('div');
    m.className = 'pm pm-annot';
    m.style.left = ((an.page - 1) / state.pageCount * 100) + '%';
    m.title = t('第 {page} 页有标注', { page: an.page });
    m.style.background = an.color || '#fbbf24';
    m.addEventListener('click', (e) => {
      e.stopPropagation();
      focusAnnotation(an.id);
    });
    box.appendChild(m);
  }
}

function escapeHtmlLocal(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

