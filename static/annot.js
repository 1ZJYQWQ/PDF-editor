/* ==========================================================================
   标注模块
   --------------------------------------------------------------------------
   核心设计：用「内容锚点」而不是屏幕坐标来记录标注位置。

   锚点结构：
     {
       page: 1,              // 页码，从 1 开始
       itemIndex: 12,        // 该页 textContent.items 的下标
       charStart: 3,         // item 内的字符起始偏移
       charEnd: 18           // item 内的字符结束偏移（不含）
     }

   这样无论缩放、旋转、换设备，标注都能精确贴回原文。
   渲染时再用当前视口矩阵把锚点换算成屏幕矩形。
   ========================================================================== */

import { state, els, api, toast, uid, escapeHtml } from '/static/core.js';

/* --------------------------------------------------------------------------
   状态
   -------------------------------------------------------------------------- */

export const annState = {
  docId: null,
  items: [],          // 全部标注
  filter: 'all',
  activeId: null,
};

let saveTimer = null;

/* --------------------------------------------------------------------------
   持久化
   -------------------------------------------------------------------------- */

export async function loadAnnotations(docId) {
  annState.docId = docId;
  annState.items = [];
  annState.activeId = null;
  if (!docId) { renderAnnotList(); return; }
  try {
    const data = await api(`/api/annotations?doc=${encodeURIComponent(docId)}`);
    if (data.ok) annState.items = data.items || [];
  } catch (err) {
    console.warn('读取标注失败', err);
  }
  renderAnnotList();
  repaintAnnotations();
}

/* 保存（防抖，避免频繁写盘） */
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
    toast('标注保存失败：' + err.message, 2600);
  }
}

/* --------------------------------------------------------------------------
   创建标注：从选区提取锚点
   -------------------------------------------------------------------------- */

/**
 * 把当前 DOM 选区转换成若干锚点。
 * 选区可能跨多个 span（也就跨多个 textContent item），所以返回数组。
 */
export function selectionToAnchors() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;

  const range = sel.getRangeAt(0);
  if (!range.toString().trim()) return null;

  // 找到选区所属的页面
  let node = range.startContainer;
  let pageEl = null;
  while (node && node !== document.body) {
    if (node.classList && node.classList.contains('page')) { pageEl = node; break; }
    node = node.parentNode;
  }
  if (!pageEl) return null;

  const rec = state.pages[parseInt(pageEl.dataset.page, 10) - 1];
  if (!rec || !rec.textContent) return null;

  // 反查：span -> item 下标（兼容新旧文字层结构）
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

  // 取原文快照
  const text = parts.map(p => {
    const it = rec.textContent.items[p.itemIndex];
    return (it && it.str ? it.str : '').slice(p.charStart, p.charEnd);
  }).join('');

  return { page: rec.num, parts, text };
}

/*
  建立「item 下标 -> span」映射。

  新版文字层在 span 上写了 data-item-index，直接读即可；
  但如果页面是旧版构建的（没有该属性），或属性只覆盖了一部分，
  则对缺失的部分按「span 顺序对应非空 item」推断补全。
*/
export function buildSpanItemMap(rec) {
  const map = new Map();                       // itemIndex -> span
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

  // 补全：没有 itemIndex 的 span 按顺序对应到「还没被占用的非空 item」
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

/*
  计算「某个边界点」在 span 内对应的字符偏移。

  边界点可能落在三种位置：
    1. container 本身就是 span（offset 是子节点下标）
    2. container 是某个文本节点（offset 是节点内字符下标）
    3. container 是 span 内的 mark 元素

  span 内部结构可能被搜索高亮的 mark 切碎，
  所以统一用 TreeWalker 遍历文本节点，边累加长度边判断。
*/
function offsetWithin(span, container, offset) {
  // 情况 1：container 就是 span，offset 是「第几个子节点」
  if (container === span) {
    let count = 0;
    let idx = 0;
    const walker = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      // 找出这个文本节点是其父链上第几个「顶层子节点」
      let top = n;
      while (top.parentNode && top.parentNode !== span) top = top.parentNode;
      const topIndex = [...span.childNodes].indexOf(top);
      if (topIndex >= offset) return count;
      count += n.textContent.length;
    }
    return count;
  }

  // 情况 2 / 3：container 是文本节点或 mark 元素
  let count = 0;
  const walker = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if (n === container) {
      // 文本节点：offset 是节点内字符下标
      return count + offset;
    }
    count += n.textContent.length;
  }

  // container 是元素（mark），用 offset 作为子节点下标近似处理
  if (container.nodeType === 1 && span.contains(container)) {
    let acc = 0;
    const w2 = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
    let n2;
    while ((n2 = w2.nextNode())) {
      if (container.contains(n2)) {
        // 该元素内的第 offset 个子节点之前的长度
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

/* --------------------------------------------------------------------------
   添加 / 编辑 / 删除
   -------------------------------------------------------------------------- */

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
  // 立即把标注画到页面上（不等防抖，也不依赖滚动等外部触发）
  repaintAnnotations();
  // 双保险：若目标页存在但本轮没画出来，下一帧再补一次
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

/* --------------------------------------------------------------------------
   渲染：把锚点换算成屏幕矩形并绘制
   -------------------------------------------------------------------------- */

const TYPE_LABEL = {
  highlight: '高亮', underline: '下划线', wavy: '波浪线',
  strike: '删除线', note: '笔记', box: '区域',
};

export function repaintAnnotations() {
  let painted = 0;
  for (const rec of state.pages) {
    if (!rec.rendered) continue;
    paintPageAnnotations(rec);
    painted += 1;
  }
  if (window.__PDFVIEW_DEBUG__) {
    console.log(`[标注] 重绘 ${painted} 个已渲染页面，共 ${annState.items.length} 条标注`);
  }
  return painted;
}

export function paintPageAnnotations(rec) {
  // 清掉旧层
  const old = rec.wrap.querySelector('.annot-layer');
  if (old) old.remove();

  const mine = annState.items.filter(a => a.page === rec.num);
  if (!mine.length) return;

  const layer = document.createElement('div');
  layer.className = 'annot-layer';

  // item 下标 -> span（兼容新旧两种文字层结构）
  const spanByItem = buildSpanItemMap(rec);

  const dbg = { total: mine.length, noSpan: 0, noRect: 0, drawn: 0 };

  for (const an of mine) {
    // 区域框选：直接用相对坐标渲染
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

    // 文本类标注：逐 part 换算矩形
    for (const part of (an.parts || [])) {
      const span = spanByItem.get(part.itemIndex);
      if (!span) { dbg.noSpan += 1; continue; }

      const rects = charRects(span, part.charStart, part.charEnd);
      if (!rects.length) { dbg.noRect += 1; continue; }

      for (const r of rects) {
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
  }

  // 只要能画出东西就挂上去
  if (dbg.drawn > 0 || layer.childElementCount) {
    rec.wrap.appendChild(layer);
  }

  if (window.__PDFVIEW_DEBUG__) {
    console.log(`[标注] 第 ${rec.num} 页 已有${dbg.total}条 绘制${dbg.drawn}个矩形 ` +
                `找不到span:${dbg.noSpan} 无矩形:${dbg.noRect}`);
    if (dbg.noSpan) {
      console.log('  可用 itemIndex:', [...spanByItem.keys()].slice(0, 30).join(','),
                  ' 需要:', mine.flatMap(a => (a.parts || []).map(p => p.itemIndex)).join(','));
    }
  }
}

/**
 * 求某个字符区间在 span 内对应的矩形列表。
 *
 * 优先用 Range.getClientRects() —— 它能正确处理跨行文本。
 * 但它有个坑：span 上如果有 transform（我们给文字层加了 scaleX 对齐），
 * 返回的矩形可能为空或不准。所以这里加了兜底：
 * 取不到就用 span 自身的矩形 + 字符比例推算。
 */
function charRects(span, start, end) {
  const textLen = (span.dataset.orig !== undefined
    ? span.dataset.orig : span.textContent).length;
  start = Math.max(0, Math.min(start, textLen));
  end = Math.max(start, Math.min(end, textLen));
  if (start === end || textLen === 0) return [];

  const pageEl = span.closest('.page');
  if (!pageEl) return [];
  const pageRect = pageEl.getBoundingClientRect();

  // ---- 方案 A：Range 精确取矩形（支持跨行） ----
  const rects = rectsViaRange(span, start, end, pageRect);
  if (rects.length) return rects;

  // ---- 方案 B：兜底，按比例切分 span 的矩形 ----
  return rectsViaRatio(span, start, end, textLen, pageRect);
}

function rectsViaRange(span, start, end, pageRect) {
  // 建立「字符下标 -> (文本节点, 节点内偏移)」映射
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

/* --------------------------------------------------------------------------
   侧栏列表
   -------------------------------------------------------------------------- */

export function renderAnnotList() {
  const box = els.annotList;
  if (!box) return;

  const all = annState.items;
  const list = annState.filter === 'all'
    ? all
    : all.filter(a => a.type === annState.filter);

  els.annotCount.textContent = all.length ? `${list.length}/${all.length}` : '';

  if (!list.length) {
    box.innerHTML = `<div class="annot-empty">${
      all.length
        ? '当前筛选下没有标注'
        : '还没有标注<br><span style="font-size:11.5px">选中文字即可添加高亮或笔记</span>'
    }</div>`;
    return;
  }

  const sorted = [...list].sort((a, b) => a.page - b.page || (a.created || 0) - (b.created || 0));
  box.innerHTML = sorted.map(a => `
    <div class="annot-card${a.id === annState.activeId ? ' active' : ''}"
         data-id="${a.id}" style="--acard:${a.color || '#a1a1aa'}">
      <div class="ac-head">
        <span class="ac-type">${TYPE_LABEL[a.type] || a.type}</span>
        <span class="ac-page">第 ${a.page} 页</span>
      </div>
      <div class="ac-text">${escapeHtml(a.text || '')}</div>
      ${a.note ? `<div class="ac-note">${escapeHtml(a.note)}</div>` : ''}
    </div>`).join('');

  box.querySelectorAll('.annot-card').forEach(card => {
    card.addEventListener('click', () => focusAnnotation(card.dataset.id));
  });
}

export function focusAnnotation(id) {
  const an = annState.items.find(a => a.id === id);
  if (!an) return;
  annState.activeId = id;

  if (an.page !== state.currentPage) goToPageForAnnot(an.page);

  const rec = state.pages[an.page - 1];
  if (rec && !rec.rendered) renderPageForAnnot(rec);

  setTimeout(() => {
    repaintAnnotations();
    renderAnnotList();
    // 滚动到标注位置
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

/* 这两个由 app.js 注入，避免循环依赖 */
let goToPageForAnnot = () => {};
let renderPageForAnnot = () => {};

export function bindNavHelpers(goToPage, renderPage) {
  goToPageForAnnot = goToPage;
  renderPageForAnnot = renderPage;
}

/* --------------------------------------------------------------------------
   进度条上的标注标记
   -------------------------------------------------------------------------- */

export function updateProgressMarkers() {
  const box = els.progressMarkers;
  if (!box) return;
  box.innerHTML = '';
  if (!state.pageCount) return;

  const seen = new Set();
  for (const an of annState.items) {
    const key = `${an.page}-${an.type}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const m = document.createElement('div');
    m.className = 'pm pm-annot';
    m.style.left = ((an.page - 1) / state.pageCount * 100) + '%';
    m.title = `第 ${an.page} 页有标注`;
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

