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
  items: [],          // 我们自己加的标注（可编辑、可保存）
  // PDF 文件自带的注释（/Annots）：**只读**。
  // 单独放一份，避免混进 items 被当成自己的标注保存回 sidecar。
  // 结构 { eid, page, type, color, contents, quadPoints, rect }
  ext: [],
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
  annState.ext = [];        // 换文档必须清空：ext 是按当前文件解析出来的
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

/* --------------------------------------------------------------------------
   读取 PDF 自带的注释（/Annots）—— 只读显示
   --------------------------------------------------------------------------

   为什么借 pdf.js 的 getAnnotations()：各家写出 /Annots 的姿势差别很大
   （QuadPoints 顺序、坐标翻转、颜色空间、外观流），pdf.js 已经把这些抹平了，
   自己解析容易在某种文件上翻车。

   只读：这些注释属于原文件，我们不修改、也不参与保存；
   在层里的 div 加 pointer-events:none，避免抢走下面文字的选择与点击。
   -------------------------------------------------------------------------- */

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
    return;                     // 读不出来不影响正常阅读
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

  // ★ 外部注释也要进「标注」列表（只读）。
  //   只画线不进列表的话：用户看不到备注内容、不知道一共几条、
  //   也没法点它跳到对应页 —— 体感就是"这个文件我看不到注释"。
  out.forEach((o, i) => { o.eid = `ext-${rec.num}-${i}`; });
  annState.ext = (annState.ext || [])
    .filter(x => x.page !== rec.num)          // 同页替换，避免重复叠加
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
    toast(`本文档自带 ${annState.ext.length} 条 PDF 注释，已列入左侧「标注」（只读）`, 3200);
  }
}

/* PDF 坐标 -> 屏幕像素（走 viewport，自动处理缩放与旋转） */
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

/* 一条外部注释 -> 若干屏幕矩形 */
function extBoxes(ex, vp) {
  const out = [];
  if (typeof vp.convertToViewportPoint !== 'function') return out;

  const qp = ex.quadPoints;
  if (qp && qp.length >= 8) {
    // 每 8 个数是一个四边形（左上/右上/左下/右下），转成包围盒
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
  // 清掉旧层
  const old = rec.wrap.querySelector('.annot-layer');
  if (old) old.remove();

  const mine = annState.items.filter(a => a.page === rec.num);
  const ext = rec.pdfAnnots || [];
  if (!mine.length && !ext.length) return;

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
    const rects = [];
    for (const part of (an.parts || [])) {
      const span = spanByItem.get(part.itemIndex);
      if (!span) { dbg.noSpan += 1; continue; }

      const rs = charRects(span, part.charStart, part.charEnd);
      if (!rs.length) { dbg.noRect += 1; continue; }
      for (const r of rs) rects.push(r);
    }

    // ★ 先把同一行里首尾相接的矩形合并成一条，再逐个画。
    //   文字层是「一个 item 一个 span」，一条标注常常横跨几十个 span；
    //   逐段画会得到一串有缝、高低不齐的小线段（下划线看着像拼的），
    //   相邻段重叠处还会叠成双线。合并后每行只有一条连续段。
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

  // ---- PDF 自带的注释：只读显示 ----
  // 这些属于原文件，我们只画出来、不参与编辑/保存。
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
        el.dataset.id = ex.eid;          // 供列表点击后定位/高亮
        if (ex.contents) el.title = ex.contents;
        if (ex.eid === annState.activeId) el.classList.add('active');
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

/**
 * 把标注矩形按行合并成连续段。
 *
 * 为什么需要：文字层是「一个 item 一个 span」（中文一行常被切成十几个），
 * 一条标注会拿到很多首尾相接的小矩形。逐个画出来的下划线/删除线是
 * 一串有缝、高低不齐的小线段，重叠处还会叠成双线（用户反馈「不精致」）。
 *
 * 规则：
 *  1. top 相近的算同一行（容差按字高比例，避免把两行并成一行）；
 *  2. 同一行内按 left 排序，间隔 ≤1.6px 就并成一条（吃掉亚像素缝）；
 *  3. 同一行统一 top/bottom —— 底线取该行最低的那条，线才不会高低跳。
 */
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

/* --------------------------------------------------------------------------
   导出：把批注换算成 PDF 用户空间（左下原点、y 向上）的矩形
   --------------------------------------------------------------------------

   为什么不复用屏幕矩形：屏幕矩形依赖当前缩放和窗口，而且只对"已渲染"的页
   有效。这里直接从 pdf.js 的 textContent 里算 —— 每条 part 记的是
   (itemIndex, charStart, charEnd)，item.transform 里就带着 PDF 坐标，
   所以不需要页面被渲染，也不需要知道缩放比例。

   产出结构交给后端 pdfannot.py 写 /Annots：
     [{ page, type, rects: [[x0,y0,x1,y1], ...], color, note }]
   -------------------------------------------------------------------------- */

const _tcCache = new Map();

/* 取某页的 textContent（优先用已缓存的，未渲染的页才现取） */
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

/* item 内的字符区间 -> PDF 矩形 */
function itemRectPdf(item, start, end) {
  const t = item.transform || [1, 0, 0, 1, 0, 0];
  const h = Math.abs(Math.hypot(t[2], t[3])) || 10;   // 字号（PDF 点）
  const len = (item.str || '').length || 1;
  const w = Math.abs(item.width || 0);
  const s = Math.max(0, Math.min(start, len));
  const e = Math.max(s, Math.min(end, len));
  const x = t[4];
  const y = t[5];
  // 基线在 y，字身向上一个字号
  return [x + (s / len) * w, y, x + (e / len) * w, y + h];
}

/* 页面尺寸（PDF 用户空间） */
async function pageGeom(pageNum) {
  if (!state.pdf) return null;
  try {
    const page = await state.pdf.getPage(pageNum);
    const v = page.view;                     // [x0, y0, x1, y1]
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

    // 区域框：存的是相对页面的百分比（左上原点），换回 PDF 坐标
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

/* --------------------------------------------------------------------------
   侧栏列表
   -------------------------------------------------------------------------- */

export function renderAnnotList() {
  const box = els.annotList;
  if (!box) return;

  const mine = annState.items || [];
  const ext = annState.ext || [];
  const filt = annState.filter;

  // 统一成同一种"卡片视图模型"，两类只在来源标记上不同
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

  // 计数：只统计"自己的"会让人以为 PDF 自带的没读到，所以显示总数
  els.annotCount.textContent = nAll ? `${list.length}/${nAll}` : '';

  if (!list.length) {
    box.innerHTML = `<div class="annot-empty">${
      nAll
        ? '当前筛选下没有标注'
        : '还没有标注<br><span style="font-size:11.5px">选中文字即可添加高亮或笔记</span>'
    }</div>`;
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
        ${a.isExt ? '<span class="ac-src">PDF 自带</span>' : ''}
        <span class="ac-page">第 ${a.page} 页</span>
      </div>
      ${a.text ? `<div class="ac-text">${escapeHtml(a.text)}</div>` : ''}
      ${a.note
        ? `<div class="ac-note">${escapeHtml(a.note)}</div>`
        : (a.isExt ? '<div class="ac-note ac-none">无备注文字</div>' : '')}
    </div>`).join('');

  box.querySelectorAll('.annot-card').forEach(card => {
    card.addEventListener('click', () => {
      if (card.dataset.ext === '1') focusExt(card.dataset.id);
      else focusAnnotation(card.dataset.id);
    });
  });
}

/* 由 bindNavHelpers 注入：翻页 / 渲染某页。
   用"注入变量"而不是直接 import app.js —— 那边已经 import 了本模块，
   反向引用会形成循环依赖（表现为某个导出变 undefined）。 */
let goToPageForAnnot = () => {};
let renderPageForAnnot = () => {};

/* 翻到某页并把某个标注元素滚到视野中间（自己的标注与 PDF 自带的共用） */
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

/** 定位到 PDF 自带的注释（只读）：只做「翻页 + 滚到可见 + 高亮」 */
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

/* --------------------------------------------------------------------------
   进度条上的标注标记
   -------------------------------------------------------------------------- */

export function updateProgressMarkers() {
  const box = els.progressMarkers;
  if (!box) return;
  box.innerHTML = '';
  if (!state.pageCount) return;

  const seen = new Set();
  // 进度条上也要标出 PDF 自带的注释，否则用户不知道哪几页有东西
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

