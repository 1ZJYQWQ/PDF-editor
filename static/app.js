/* ==========================================================================
   PDF Viewer - 主逻辑
   基于 pdf.js，支持：连续滚动、缩放、旋转、缩略图、目录、全文搜索、标注
   ========================================================================== */

import {
  pdfjsLib, state, els, cacheEls, $, uid, escapeHtml, debounce,
  toast, showLoading, hideLoading, api,
} from '/static/core.js';

import {
  annState, loadAnnotations, scheduleSave, doSave,
  selectionToAnchors, addAnnotation, updateAnnotation, removeAnnotation,
  repaintAnnotations, paintPageAnnotations, renderAnnotList, focusAnnotation,
  updateProgressMarkers, bindNavHelpers,
} from '/static/annot.js';

import {
  editState, bindEditHelpers, bindEditUI, loadEdits, doSave as saveEdits,
  repaintEdits, paintPageEdits, setEditMode, setTool, currentColor,
  makeRectObject, makeLineObject, hitTest, removeObject, addObject,
  updateObject, pageSize, screenToNorm, refreshPagePanel,
  exportPdf, buildExportPayload, pdfToNorm,
} from '/static/edit.js';

// 纯几何（可单测）—— 拖动位移与坐标换算
import { dragDeltaToPdf, movedPosition } from '/static/geom.js';

const MAX_CANVAS_PIXELS = 16 * 1024 * 1024;
const BUFFER_PAGES = 1;
const LS_PREFIX = 'pdfviewer:';

/* --------------------------------------------------------------------------
   工具
   -------------------------------------------------------------------------- */

/* 计算页面在给定缩放下的显示尺寸 */
function pageSizeAt(page, scale) {
  const vp = page.getViewport({ scale, rotation: state.rotation });
  return { width: vp.width, height: vp.height };
}

/* 根据模式计算合适的缩放值 */
async function computeScale(mode) {
  if (!state.pdf) return 1;
  const page = await state.pdf.getPage(1);
  const base = page.getViewport({ scale: 1, rotation: state.rotation });

  const sideW = state.viewMode === 'double' ? els.viewer.clientWidth / 2 : els.viewer.clientWidth;
  const viewerW = sideW - 40;
  const viewerH = els.viewer.clientHeight - 40;

  if (mode === 'page') {
    return Math.min(viewerW / base.width, viewerH / base.height);
  }
  if (mode === 'width') {
    return viewerW / base.width;
  }
  if (mode === 'auto') {
    const fitPage = Math.min(viewerW / base.width, viewerH / base.height);
    if (fitPage >= 1) return 1;
    return Math.max(fitPage, viewerW / base.width > 1 ? 1 : viewerW / base.width);
  }
  return parseFloat(mode) || 1;
}

/* 阅读位置记忆 */
function posKey() {
  return LS_PREFIX + 'pos:' + (state.docId || state.fileName);
}

function savePosition() {
  if (!state.docId && !state.fileName) return;
  try {
    localStorage.setItem(posKey(), JSON.stringify({
      page: state.currentPage,
      scale: state.scale,
      scaleMode: state.scaleMode,
      rotation: state.rotation,
      viewMode: state.viewMode,
      t: Date.now(),
    }));
  } catch (e) { /* 隐私模式下可能失败，忽略 */ }
}

function readPosition() {
  try {
    const raw = localStorage.getItem(posKey());
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
}

/* 书签 */
function bookmarkKey() {
  return LS_PREFIX + 'bm:' + (state.docId || state.fileName);
}

function loadBookmarks() {
  try {
    const raw = localStorage.getItem(bookmarkKey());
    state.bookmarks = new Set(raw ? JSON.parse(raw) : []);
  } catch (e) { state.bookmarks = new Set(); }
}

function saveBookmarks() {
  try {
    localStorage.setItem(bookmarkKey(), JSON.stringify([...state.bookmarks]));
  } catch (e) { /* ignore */ }
}

function toggleBookmark() {
  if (!state.pdf) return;
  const p = state.currentPage;
  if (state.bookmarks.has(p)) { state.bookmarks.delete(p); toast('已取消书签'); }
  else { state.bookmarks.add(p); toast(`第 ${p} 页已加书签`); }
  saveBookmarks();
  updateProgressMarkers();
  updateBookmarkBtn();
}

function updateBookmarkBtn() {
  if (els.btnBookmark) {
    els.btnBookmark.classList.toggle('on', state.bookmarks.has(state.currentPage));
  }
}

/* --------------------------------------------------------------------------
   文档加载
   -------------------------------------------------------------------------- */

async function loadPdfFromPath(path, name, docId) {
  showLoading('正在打开文档…');
  try {
    const url = '/api/file?path=' + encodeURIComponent(path);
    const task = pdfjsLib.getDocument({
      url,
      rangeChunkSize: 262144,
      disableAutoFetch: false,
      disableStream: false,
    });
    await openDocument(task, name, docId || null, path);
  } catch (err) {
    hideLoading();
    console.error(err);
    if (String(err).includes('password') || err?.name === 'PasswordException') {
      return handlePassword(path, name, docId);
    }
    toast('打开失败：' + (err?.message || err));
  }
}

async function handlePassword(path, name, docId) {
  const pwd = prompt('该 PDF 已加密，请输入密码：');
  if (pwd === null) { hideLoading(); return; }
  try {
    const task = pdfjsLib.getDocument({
      url: '/api/file?path=' + encodeURIComponent(path),
      password: pwd,
    });
    await openDocument(task, name, docId || null, path);
  } catch (err) {
    hideLoading();
    toast('密码错误或文档无法打开');
  }
}

async function loadPdfFromFile(file) {
  showLoading('正在读取本地文件…');
  try {
    const buf = await file.arrayBuffer();
    const task = pdfjsLib.getDocument({ data: buf });
    // 本地文件用文件名+大小生成同样的指纹规则
    const id = await localDocId(file);
    await openDocument(task, file.name.replace(/\.pdf$/i, ''), id, null);
  } catch (err) {
    hideLoading();
    console.error(err);
    toast('打开失败：' + (err?.message || err));
  }
}

/* 本地文件的指纹：与服务端规则保持一致（文件名|大小） */
async function localDocId(file) {
  const raw = `${file.name}|${file.size}`;
  const buf = new TextEncoder().encode(raw);
  const digest = await crypto.subtle.digest('SHA-1', buf);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0'))
    .join('').slice(0, 16);
}

async function openDocument(task, name, docId, path) {
  const pdf = await task.promise;
  state.pdf = pdf;
  state.fileName = name || '未命名文档';
  state.docId = docId;
  state.filePath = path || null;
  state.pageCount = pdf.numPages;
  state.rotation = 0;
  state.searchHits = [];
  state.searchHitsByPage = new Map();
  state.hitIndex = -1;
  state.currentPage = 1;

  document.title = state.fileName + ' - PDF 查看器';

  els.pageTotal.textContent = '/ ' + pdf.numPages;
  els.pageInput.max = pdf.numPages;
  els.pageInput.value = 1;

  // 恢复上次阅读进度
  const saved = readPosition();
  state.rotation = (saved && saved.rotation) || 0;
  state.viewMode = (saved && saved.viewMode) || 'single';
  applyViewMode(state.viewMode, true);
  state.scaleMode = (saved && saved.scaleMode) || 'auto';
  state.scale = await computeScale(state.scaleMode);
  els.zoomSelect.value = state.scaleMode;

  // 显示需要文档的控件
  ['navGroup', 'navSep', 'zoomGroup', 'zoomSep', 'toolGroup', 'toolGroupSep',
   'annotGroup', 'annotSep', 'editGroup', 'editSep',
   'viewGroup', 'searchSep', 'fileGroup']
    .forEach(id => { els[id] && (els[id].hidden = false); });
  if (els.progressBar) els.progressBar.hidden = false;

  loadBookmarks();
  await buildPages();
  setupObserver();

  // 加载标注
  await loadAnnotations(state.docId);

  // 加载编辑数据与页面尺寸信息
  await loadEdits(state.docId);
  if (state.filePath) {
    try {
      const info = await api('/api/pdfinfo?path=' +
        encodeURIComponent(state.filePath));
      editState.pageInfo = info.pages || [];
    } catch (e) {
      console.warn('读取页面尺寸失败：', e.message);
      editState.pageInfo = [];
    }
  } else {
    // 本地文件没有服务端路径，用 pdf.js 自己的视口尺寸兜底
    editState.pageInfo = [];
  }
  repaintEdits();
  if (editState.on) refreshPagePanel();

  updateProgressMarkers();
  hideLoading();

  // 跳回上次位置
  if (saved && saved.page > 1 && saved.page <= pdf.numPages) {
    goToPage(saved.page);
    toast(`已回到第 ${saved.page} 页`, 1800);
  } else {
    els.viewer.focus();
    toast(`已打开：${state.fileName}（共 ${pdf.numPages} 页）`, 2200);
  }
  updateBookmarkBtn();
  updateProgress();
}

/* 关闭当前文档，回到欢迎页 */
function closeDocument() {
  if (state.observer) { state.observer.disconnect(); state.observer = null; }
  stopAutoScroll();
  if (state.pdf) { try { state.pdf.destroy(); } catch (e) { /* noop */ } }
  Object.assign(state, {
    pdf: null, filePath: null, fileName: '', docId: null, pageCount: 0,
    currentPage: 1, pages: [], searchHits: [], searchHitsByPage: new Map(),
    hitIndex: -1, outlinMap: new Map(), bookmarks: new Set(), annotMode: false,
  });
  annState.items = [];
  annState.docId = null;
  annState.activeId = null;
  document.title = 'PDF 查看器';
  els.pages.innerHTML = '';
  els.pages.classList.remove('empty');
  els.pages.classList.add('empty');
  els.thumbList.innerHTML = '';
  els.outlineList.innerHTML = '';
  if (els.annotList) els.annotList.innerHTML = '';
  els.searchInput.value = '';
  els.searchCount.textContent = '';
  if (els.progressBar) els.progressBar.hidden = true;
  ['navGroup', 'navSep', 'zoomGroup', 'zoomSep', 'toolGroup', 'toolGroupSep',
   'annotGroup', 'annotSep', 'viewGroup', 'searchSep', 'fileGroup', 'searchNav']
    .forEach(id => { els[id] && (els[id].hidden = true); });
  els.viewer.classList.remove('annot-mode');
  hideLoading();
  renderWelcome();
}

/* --------------------------------------------------------------------------
   页面构建与渲染
   -------------------------------------------------------------------------- */

async function buildPages() {
  const container = els.pages;
  container.innerHTML = '';
  container.classList.remove('empty');
  container.classList.toggle('double', state.viewMode === 'double');
  state.pages = [];

  const firstPage = await state.pdf.getPage(1);
  const baseVp = firstPage.getViewport({ scale: state.scale, rotation: state.rotation });

  const w = Math.floor(baseVp.width);
  const h = Math.floor(baseVp.height);

  const frag = document.createDocumentFragment();
  for (let i = 1; i <= state.pageCount; i++) {
    const wrap = document.createElement('div');
    wrap.className = 'page pending';
    wrap.dataset.page = i;
    wrap.dataset.ph = `第 ${i} 页`;
    wrap.style.width = w + 'px';
    wrap.style.height = h + 'px';

    const canvas = document.createElement('canvas');
    const textLayer = document.createElement('div');
    textLayer.className = 'text-layer';
    wrap.append(canvas, textLayer);
    frag.appendChild(wrap);

    state.pages.push({
      num: i, wrap, canvas, textLayer,
      rendered: false, rendering: false,
      viewport: null, textContent: null, task: null,
    });
  }
  container.appendChild(frag);
}

function setupObserver() {
  if (state.observer) state.observer.disconnect();

  state.observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const num = parseInt(entry.target.dataset.page, 10);
      const rec = state.pages[num - 1];
      if (!rec) continue;
      if (entry.isIntersecting) {
        renderPage(rec);
      }
    }
    updateCurrentPage();
  }, {
    root: els.viewer,
    rootMargin: `${BUFFER_PAGES * 100}% 0px ${BUFFER_PAGES * 100}% 0px`,
    threshold: 0,
  });

  for (const rec of state.pages) state.observer.observe(rec.wrap);
}

/* 渲染单页：位图 + 文字层 */
async function renderPage(rec) {
  if (rec.rendered || rec.rendering) return;

  // 限制并发：同时最多 3 页在渲染
  if (state.renderQueue.size >= 3) return;
  state.renderQueue.add(rec.num);
  rec.rendering = true;

  try {
    const page = await state.pdf.getPage(rec.num);
    const viewport = page.getViewport({ scale: state.scale, rotation: state.rotation });

    // 计算画布分辨率，受像素上限约束
    const outputScale = window.devicePixelRatio || 1;
    let cssW = viewport.width, cssH = viewport.height;
    let pxW = Math.floor(cssW * outputScale);
    let pxH = Math.floor(cssH * outputScale);
    let effScale = outputScale;

    if (pxW * pxH > MAX_CANVAS_PIXELS) {
      const ratio = Math.sqrt(MAX_CANVAS_PIXELS / (pxW * pxH));
      pxW = Math.floor(pxW * ratio);
      pxH = Math.floor(pxH * ratio);
      effScale = outputScale * ratio;
    }

    // 尺寸同步到容器
    rec.wrap.style.width = Math.floor(cssW) + 'px';
    rec.wrap.style.height = Math.floor(cssH) + 'px';

    const canvas = rec.canvas;
    const ctx = canvas.getContext('2d', { alpha: false });
    canvas.width = pxW;
    canvas.height = pxH;
    canvas.style.width = Math.floor(cssW) + 'px';
    canvas.style.height = Math.floor(cssH) + 'px';

    rec.viewport = viewport;

    const task = page.render({
      canvasContext: ctx,
      viewport,
      transform: effScale !== 1 ? [effScale, 0, 0, effScale, 0, 0] : null,
    });
    rec.task = task;
    await task.promise;
    rec.task = null;

    // ---- 文字层 ----
    const textContent = await page.getTextContent();
    rec.textContent = textContent;
    buildTextLayer(rec, textContent, viewport);

    rec.wrap.classList.remove('pending');
    rec.wrap.removeAttribute('data-ph');
    rec.rendered = true;
    rec.rendering = false;
    state.renderQueue.delete(rec.num);

    // 若该页存在搜索命中，渲染完成后即时补上高亮
    const hitOnPage = state.searchHitsByPage.get(rec.num - 1);
    if (hitOnPage) {
      for (const hit of hitOnPage) paintHitsOnPage(rec, hit);
      markCurrentHit();
    }

    // 标注层
    paintPageAnnotations(rec);
    // 编辑层
    paintPageEdits(rec);
    // 页面重渲染后对象位置会变，操作条要跟着重定位
    // （只在本页有选中对象时才需要）
    if (objBarId) {
      const sel = editState.objects.find(o => o.id === objBarId);
      if (sel && sel.page === rec.num) showObjBar(rec, sel);
    }
  } catch (err) {
    rec.rendering = false;
    state.renderQueue.delete(rec.num);
    // 取消渲染不算错误
    if (err?.name !== 'RenderingCancelledException' && !String(err).includes('cancel')) {
      console.warn(`第 ${rec.num} 页渲染失败：`, err);
      rec.wrap.classList.remove('pending');
      rec.wrap.setAttribute('data-ph', `第 ${rec.num} 页渲染失败`);
    }
  }
}

/* 构建可选中 / 可搜索的文字层 */
function buildTextLayer(rec, textContent, viewport) {
  const layer = rec.textLayer;
  layer.innerHTML = '';

  // 给每个 item 打上原始下标，span 会记住它
  textContent.items.forEach((it, i) => { it.__idx = i; });

  // 用一个隐藏画布测量文本宽度，避免依赖 DOM 布局（未插入时测量恒为 0）
  const measureCtx = (rec._measureCanvas ||= document.createElement('canvas')).getContext('2d');
  const frag = document.createDocumentFragment();

  for (const item of textContent.items) {
    if (!item.str) continue;

    const tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
    const fontHeight = Math.hypot(tx[2], tx[3]);
    const angle = Math.atan2(tx[1], tx[0]);

    const span = document.createElement('span');
    span.textContent = item.str;
    // 显式记录所属的 item 下标 —— 后续取锚点、画标注都靠它，
    // 不再依赖 DOM 顺序推断，避免被搜索高亮等操作打乱。
    span.dataset.itemIndex = String(item.__idx);
    span.style.left = tx[4] + 'px';
    span.style.top = (tx[5] - fontHeight) + 'px';
    span.style.fontSize = fontHeight + 'px';
    span.style.fontFamily = 'sans-serif';

    // 原文字形宽度（PDF 坐标 -> 屏幕像素）
    const targetW = item.width * viewport.scale;
    if (fontHeight > 0 && targetW > 0) {
      measureCtx.font = `${fontHeight}px sans-serif`;
      const naturalW = measureCtx.measureText(item.str).width;
      if (naturalW > 0.01) {
        const ratio = targetW / naturalW;
        // 只在明显偏离时才缩放，避免亚像素级抖动
        if (Math.abs(ratio - 1) > 0.01) {
          span.style.transformOrigin = '0 0';
          span.style.transform = `scaleX(${ratio.toFixed(4)})`;
        }
      }
    }
    if (angle !== 0) {
      const base = span.style.transform ? span.style.transform + ' ' : '';
      span.style.transformOrigin = '0 0';
      span.style.transform = `${base}rotate(${angle}rad)`;
    }
    frag.appendChild(span);
  }
  layer.appendChild(frag);
}

/* 更新当前页码显示（取视口中心最近的页） */
function updateCurrentPage() {
  if (!state.pages.length) return;
  const viewerRect = els.viewer.getBoundingClientRect();
  const center = viewerRect.top + viewerRect.height / 2;

  let best = 1, bestDist = Infinity;
  for (const rec of state.pages) {
    const r = rec.wrap.getBoundingClientRect();
    if (r.bottom < viewerRect.top - 200 || r.top > viewerRect.bottom + 200) continue;
    const dist = Math.abs((r.top + r.height / 2) - center);
    if (dist < bestDist) { bestDist = dist; best = rec.num; }
  }
  if (best !== state.currentPage) {
    state.currentPage = best;
    els.pageInput.value = best;
    updateThumbActive();
    updateBookmarkBtn();
    updateProgress();
    savePosition();
  }
}

/* 更新顶部进度条 */
function updateProgress() {
  if (!els.progressFill || !state.pageCount) return;
  const v = els.viewer;
  const max = v.scrollHeight - v.clientHeight;
  const ratio = max > 0 ? Math.min(1, v.scrollTop / max) : 0;
  els.progressFill.style.width = (ratio * 100) + '%';
}

/* --------------------------------------------------------------------------
   缩放 / 旋转
   -------------------------------------------------------------------------- */

async function applyScale(mode, anchor) {
  if (!state.pdf) return;
  const oldScale = state.scale;
  state.scaleMode = mode;
  state.scale = await computeScale(mode);

  // 记录锚点，缩放后回到同一位置
  if (!anchor) {
    const viewerRect = els.viewer.getBoundingClientRect();
    anchor = {
      page: state.currentPage,
      ratio: (viewerRect.top + viewerRect.height / 2 -
              state.pages[state.currentPage - 1].wrap.getBoundingClientRect().top) /
             state.pages[state.currentPage - 1].wrap.getBoundingClientRect().height,
    };
  }

  await rebuildAtNewScale(anchor);
}

async function rebuildAtNewScale(anchor) {
  // 停止所有进行中的渲染
  for (const rec of state.pages) {
    if (rec.task) { try { rec.task.cancel(); } catch (e) { /* noop */ } rec.task = null; }
  }
  state.renderQueue.clear();

  const firstPage = await state.pdf.getPage(1);
  const baseVp = firstPage.getViewport({ scale: state.scale, rotation: state.rotation });

  for (const rec of state.pages) {
    rec.rendered = false;
    rec.rendering = false;
    rec.wrap.style.width = Math.floor(baseVp.width) + 'px';
    rec.wrap.style.height = Math.floor(baseVp.height) + 'px';
    rec.canvas.width = 0;
    rec.canvas.height = 0;
    rec.textLayer.innerHTML = '';
    if (!rec.wrap.classList.contains('pending')) {
      rec.wrap.classList.add('pending');
      rec.wrap.dataset.ph = `第 ${rec.num} 页`;
    }
  }

  // 恢复锚点位置
  if (anchor) {
    requestAnimationFrame(() => {
      const rec = state.pages[anchor.page - 1];
      if (!rec) return;
      const target = rec.wrap.offsetTop +
        rec.wrap.offsetHeight * Math.min(Math.max(anchor.ratio, 0), 1) -
        els.viewer.clientHeight / 2;
      els.viewer.scrollTop = Math.max(0, target);
      // 立即渲染锚点页及邻近页
      for (let d = -1; d <= 1; d++) {
        const r = state.pages[anchor.page - 1 + d];
        if (r) renderPage(r);
      }
      updateCurrentPage();
    });
  }
}

function rotate() {
  if (!state.pdf) return;
  state.rotation = (state.rotation + 90) % 360;
  applyScale(state.scaleMode === 'auto' ? 'auto' : state.scaleMode);
  toast(`旋转 ${state.rotation}°`, 1200);
}

function toggleInvert() {
  state.invert = !state.invert;
  document.body.classList.toggle('dark', state.invert);
  els.btnDark.classList.toggle('on', state.invert);
}

/* --------------------------------------------------------------------------
   视图模式：单页 / 双栏
   -------------------------------------------------------------------------- */

async function applyViewMode(mode, silent = false) {
  state.viewMode = mode;
  document.body.classList.toggle('view-double', mode === 'double');
  if (els.btnViewSingle) els.btnViewSingle.classList.toggle('on', mode === 'single');
  if (els.btnViewDouble) els.btnViewDouble.classList.toggle('on', mode === 'double');
  els.pages.classList.toggle('double', mode === 'double');

  if (state.pdf && !silent) {
    // 切换视图后重新计算缩放并重建页面
    state.scale = await computeScale(state.scaleMode);
    await buildPages();
    setupObserver();
    goToPage(state.currentPage);
    repaintAnnotations();
    savePosition();
  }
}

/* --------------------------------------------------------------------------
   自动滚动
   -------------------------------------------------------------------------- */

let autoScrollRAF = null;
let autoScrollSpeed = 0.55;   // 像素 / 帧

function startAutoScroll() {
  if (autoScrollRAF) { stopAutoScroll(); return; }
  if (!state.pdf) return;
  els.btnScrollPlay.classList.add('scrolling');
  toast('自动滚动已开启，按空格暂停', 1600);

  const step = () => {
    if (!autoScrollRAF) return;
    const v = els.viewer;
    if (v.scrollTop + v.clientHeight >= v.scrollHeight - 2) {
      stopAutoScroll();
      toast('已滚动到文档末尾');
      return;
    }
    v.scrollTop += autoScrollSpeed;
    autoScrollRAF = requestAnimationFrame(step);
  };
  autoScrollRAF = requestAnimationFrame(step);
}

function stopAutoScroll() {
  if (autoScrollRAF) {
    cancelAnimationFrame(autoScrollRAF);
    autoScrollRAF = null;
  }
  els.btnScrollPlay && els.btnScrollPlay.classList.remove('scrolling');
}

/* --------------------------------------------------------------------------
   跳页
   -------------------------------------------------------------------------- */

function goToPage(num, smooth = false) {
  if (!state.pdf) return;
  num = Math.min(Math.max(1, parseInt(num, 10) || 1), state.pageCount);
  const rec = state.pages[num - 1];
  if (!rec) return;

  const top = rec.wrap.offsetTop - 20;
  if (smooth) els.viewer.scrollTo({ top, behavior: 'smooth' });
  else els.viewer.scrollTop = top;

  state.currentPage = num;
  els.pageInput.value = num;
  renderPage(rec);
  if (state.pages[num]) renderPage(state.pages[num]);
  updateThumbActive();
}

/* --------------------------------------------------------------------------
   缩略图
   -------------------------------------------------------------------------- */

let thumbObserver = null;

async function buildThumbs() {
  const box = els.thumbList;
  box.innerHTML = '';
  if (!state.pdf) return;
  if (thumbObserver) thumbObserver.disconnect();

  const frag = document.createDocumentFragment();
  for (let i = 1; i <= state.pageCount; i++) {
    const btn = document.createElement('button');
    btn.className = 'thumb';
    btn.dataset.page = i;
    btn.innerHTML = `<div class="thumb-img"><div class="thumb-ph"></div></div>
                     <div class="thumb-num">${i}</div>`;
    btn.addEventListener('click', () => goToPage(i));
    frag.appendChild(btn);
  }
  box.appendChild(frag);

  // 缩略图本身也懒加载
  thumbObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) {
        renderThumb(parseInt(e.target.dataset.page, 10));
        thumbObserver.unobserve(e.target);
      }
    }
  }, { root: els.sideBody, rootMargin: '200px 0px' });

  for (const b of box.children) thumbObserver.observe(b);
}

const thumbCache = new Map();

async function renderThumb(num) {
  if (!state.pdf || thumbCache.has(num)) return;
  const btn = els.thumbList.querySelector(`.thumb[data-page="${num}"]`);
  if (!btn) return;
  const holder = btn.querySelector('.thumb-img');

  try {
    const page = await state.pdf.getPage(num);
    const targetW = 170;
    const base = page.getViewport({ scale: 1, rotation: state.rotation });
    const scale = targetW / base.width;
    const vp = page.getViewport({ scale, rotation: state.rotation });

    const canvas = document.createElement('canvas');
    const os = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(vp.width * os);
    canvas.height = Math.floor(vp.height * os);
    canvas.style.width = '100%';
    canvas.style.height = 'auto';

    await page.render({
      canvasContext: canvas.getContext('2d', { alpha: false }),
      viewport: vp,
      transform: os !== 1 ? [os, 0, 0, os, 0, 0] : null,
    }).promise;

    thumbCache.set(num, true);
    if (!btn.isConnected) return;
    const ph = holder.querySelector('.thumb-ph');
    if (ph) ph.remove();
    holder.appendChild(canvas);
  } catch (err) {
    if (err?.name !== 'RenderingCancelledException') console.warn('缩略图失败', num, err);
  }
}

function updateThumbActive() {
  const prev = els.thumbList.querySelector('.thumb.on');
  if (prev) prev.classList.remove('on');
  const cur = els.thumbList.querySelector(`.thumb[data-page="${state.currentPage}"]`);
  if (cur) {
    cur.classList.add('on');
    const sb = els.sideBody;
    const r = cur.getBoundingClientRect();
    const sr = sb.getBoundingClientRect();
    if (r.top < sr.top || r.bottom > sr.bottom) {
      cur.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }
}

/* --------------------------------------------------------------------------
   目录
   -------------------------------------------------------------------------- */

async function buildOutline() {
  const box = els.outlineList;
  box.innerHTML = '';
  state.outlinMap = new Map();
  if (!state.pdf) return;

  let outline = null;
  try { outline = await state.pdf.getOutline(); } catch (e) { outline = null; }

  if (!outline || !outline.length) {
    box.innerHTML = `<div class="outline-empty">此文档没有目录<br><span style="font-size:12px">有些 PDF 未嵌入书签结构</span></div>`;
    return;
  }

  const frag = document.createDocumentFragment();
  const rendered = await Promise.all(outline.map(item => buildOutlineNode(item, 0)));
  rendered.forEach(n => n && frag.appendChild(n));
  box.appendChild(frag);
}

async function resolveDestPage(dest) {
  try {
    let ref;
    if (typeof dest === 'string') {
      ref = await state.pdf.getDestination(dest);
    } else {
      ref = dest;
    }
    if (!ref) return null;
    if (ref[0] && typeof ref[0] === 'object') {
      // 显式页面引用
      const idx = await state.pdf.getPageIndex(ref[0]);
      return idx + 1;
    }
    const idx = await state.pdf.getPageIndex(ref[0]);
    return idx + 1;
  } catch (e) {
    return null;
  }
}

async function buildOutlineNode(item, depth) {
  const pageNum = await resolveDestPage(item.dest);
  const btn = document.createElement('button');
  btn.className = 'outline-item';
  btn.innerHTML = `<span>${escapeHtml(item.title)}</span>` +
    (pageNum ? `<span class="outline-page">${pageNum}</span>` : '');
  btn.style.paddingLeft = (8 + depth * 6) + 'px';
  if (pageNum) {
    btn.addEventListener('click', () => {
      goToPage(pageNum, true);
      els.outlineList.querySelectorAll('.outline-item.on').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
    });
  } else {
    btn.style.opacity = '0.65';
    btn.disabled = true;
  }

  if (item.items && item.items.length) {
    const wrap = document.createElement('div');
    wrap.appendChild(btn);
    const kids = document.createElement('div');
    kids.className = 'outline-kids';
    const sub = await Promise.all(item.items.map(c => buildOutlineNode(c, depth + 1)));
    sub.forEach(n => n && kids.appendChild(n));
    wrap.appendChild(kids);
    return wrap;
  }
  return btn;
}

/* --------------------------------------------------------------------------
   搜索
   -------------------------------------------------------------------------- */

/*
  搜索模型说明
  ------------
  遍历每页的 textContent.items，拼成完整字符串（并记录每个字符所属的 item
  与 item 内偏移）。匹配后为每个命中生成一条记录：

    { pageIndex, pageNum, seq, parts: [{ itemIndex, start, end, isFirst }] }

  seq 是全局序号，与界面上的「第 N / M 处」以及 mark 元素一一对应，
  从而保证计数、跳转索引、高亮三者严格一致。
*/

async function runSearch(keyword) {
  clearSearchHighlights();
  state.searchHits = [];
  state.searchHitsByPage = new Map();
  state.hitIndex = -1;

  if (!state.pdf || !keyword.trim()) {
    els.searchCount.textContent = '';
    els.searchInput.classList.remove('no-result');
    els.searchNav.hidden = true;
    return;
  }

  const q = keyword.trim().toLowerCase();
  els.searchCount.textContent = '搜索中…';
  els.searchNav.hidden = true;

  let seq = 0;
  for (let i = 1; i <= state.pageCount; i++) {
    const rec = state.pages[i - 1];
    if (!rec) continue;

    let textContent = rec.textContent;
    if (!textContent) {
      try {
        const page = await state.pdf.getPage(i);
        textContent = await page.getTextContent();
        rec.textContent = textContent;
      } catch (e) { continue; }
    }
    if (!textContent || !textContent.items.length) continue;

    // 拼接全文，同时记录字符 -> (itemIndex, charInItem)
    const pieces = [];
    const charMap = [];
    for (let ii = 0; ii < textContent.items.length; ii++) {
      const s = textContent.items[ii].str;
      if (!s) continue;
      const start = pieces.reduce((a, x) => a + x.length, 0);
      pieces.push(s);
      for (let k = 0; k < s.length; k++) charMap.push([ii, k]);
    }
    const pageText = pieces.join('');
    const lower = pageText.toLowerCase();

    let at = lower.indexOf(q);
    while (at !== -1) {
      // 把 [at, at+q.length) 的字符区间按 item 切成若干段
      const parts = [];
      let cur = null;
      for (let c = at; c < at + q.length; c++) {
        const m = charMap[c];
        if (!m) continue;
        const [itemIndex, charInItem] = m;
        if (cur && cur.itemIndex === itemIndex && cur.end === charInItem) {
          cur.end = charInItem + 1;
        } else {
          cur = { itemIndex, start: charInItem, end: charInItem + 1 };
          parts.push(cur);
        }
      }
      if (parts.length) {
        seq += 1;
        state.searchHits.push({
          pageIndex: i - 1,
          pageNum: i,
          seq,
          parts,
        });
      }
      at = lower.indexOf(q, at + q.length);
    }
  }

  const total = state.searchHits.length;
  state.searchHitsByPage = new Map();
  for (const hit of state.searchHits) {
    if (!state.searchHitsByPage.has(hit.pageIndex)) {
      state.searchHitsByPage.set(hit.pageIndex, []);
    }
    state.searchHitsByPage.get(hit.pageIndex).push(hit);
  }

  els.searchCount.textContent = total ? `${total} 处` : '无结果';
  els.searchInput.classList.toggle('no-result', total === 0);
  els.searchNav.hidden = total === 0;

  if (total > 0) {
    applySearchHighlight();
    gotoHit(0);
  }
}

/* 恢复某个 span 的原始文本（去掉其中所有 mark） */
function restoreSpan(span) {
  if (span.dataset.orig !== undefined) {
    span.textContent = span.dataset.orig;
    delete span.dataset.orig;
  }
}

/* 清除所有页面的搜索高亮 */
function clearSearchHighlights() {
  for (const rec of state.pages) {
    rec.textLayer.querySelectorAll('span[data-orig]').forEach(restoreSpan);
    // 兜底：清掉可能残留的 mark
    rec.textLayer.querySelectorAll('mark.hl').forEach(m => {
      const sp = m.closest('span');
      if (sp) restoreSpan(sp);
    });
  }
  state.searchHits = [];
  state.searchHitsByPage = new Map();
  state.hitIndex = -1;
}

/* 文字层 span 顺序与「非空 item」顺序严格对应 */
function findSpanForItem(rec, itemIndex) {
  if (!rec || !rec.textLayer) return null;
  return rec.textLayer.querySelector(`span[data-item-index="${itemIndex}"]`);
}

/* 在单个页面上绘制一条命中的高亮 */
function paintHitsOnPage(rec, hit) {
  if (!rec || !rec.rendered) return;

  // 同一 item 内的多段合并处理：按 itemIndex 分组
  const byItem = new Map();
  hit.parts.forEach((p, idx) => {
    if (!byItem.has(p.itemIndex)) byItem.set(p.itemIndex, []);
    byItem.get(p.itemIndex).push({ ...p, isFirst: idx === 0 });
  });

  for (const [itemIndex, segs] of byItem) {
    const span = findSpanForItem(rec, itemIndex);
    if (!span) continue;
    const orig = span.dataset.orig !== undefined ? span.dataset.orig : span.textContent;
    span.dataset.orig = orig;

    // 从后往前替换，避免前面的插入影响后面的偏移
    let html = escapeHtml(orig);
    [...segs].sort((a, b) => b.start - a.start).forEach(seg => {
      const inner = escapeHtml(orig.slice(seg.start, seg.end));
      const cls = seg.isFirst ? 'hl start' : 'hl';
      html = html.slice(0, seg.start) +
             `<mark class="${cls}" data-seq="${hit.seq}">${inner}</mark>` +
             html.slice(seg.end);
    });
    span.innerHTML = html;
  }
}

/* 把高亮写入所有已渲染页面 */
function applySearchHighlight() {
  for (const rec of state.pages) {
    rec.textLayer.querySelectorAll('span[data-orig]').forEach(restoreSpan);
  }
  for (const hit of state.searchHits) {
    paintHitsOnPage(state.pages[hit.pageIndex], hit);
  }
  markCurrentHit();
}

/* 高亮当前命中项 */
function markCurrentHit() {
  document.querySelectorAll('.text-layer mark.cur').forEach(m => m.classList.remove('cur'));
  if (state.hitIndex < 0 || !state.searchHits.length) return;
  const hit = state.searchHits[state.hitIndex];
  const rec = state.pages[hit.pageIndex];
  if (!rec || !rec.rendered) return;
  const mark = rec.textLayer.querySelector(`mark.hl[data-seq="${hit.seq}"]`);
  if (mark) mark.classList.add('cur');
}

/* 跳转到第 index 个命中 */
function gotoHit(index) {
  const n = state.searchHits.length;
  if (!n) return;
  state.hitIndex = ((index % n) + n) % n;
  const hit = state.searchHits[state.hitIndex];

  els.searchCount.textContent = `${state.hitIndex + 1} / ${n}`;

  const rec = state.pages[hit.pageIndex];
  if (!rec) return;

  if (!rec.rendered) renderPage(rec);

  // 等页面渲染完再定位（最多轮询 2 秒）
  let tries = 0;
  const locate = () => {
    const mark = rec.textLayer.querySelector(`mark.hl[data-seq="${hit.seq}"]`);
    if (mark) {
      markCurrentHit();
      const mr = mark.getBoundingClientRect();
      const vr = els.viewer.getBoundingClientRect();
      const outside = mr.top < vr.top + 50 || mr.bottom > vr.bottom - 50;
      if (outside) {
        els.viewer.scrollBy({
          top: mr.top - vr.top - vr.height / 2,
          behavior: 'smooth',
        });
      }
      return;
    }
    tries += 1;
    if (tries < 20) {
      setTimeout(locate, 100);
    } else {
      // 兜底：直接滚到该页
      goToPage(hit.pageNum, true);
      markCurrentHit();
    }
  };
  locate();
}

/* --------------------------------------------------------------------------
   划词菜单与标注交互
   -------------------------------------------------------------------------- */

let pendingSel = null;   // 暂存选区锚点（已固化的快照）
let popAnnotId = null;   // 气泡正在编辑的标注
let currentAnnotColor = 'yellow';
let currentAnnotColorHex = '#fbbf24';
let menuBusy = false;    // 正在操作浮动菜单，期间的 selectionchange 一律忽略

let selMenuTimer = null;

function hideSelMenu() {
  if (selMenuTimer) { clearTimeout(selMenuTimer); selMenuTimer = null; }
  if (els.selMenu) els.selMenu.hidden = true;
}

/*
  定位划词菜单。

  难点：rect 是「整个选区的外接矩形」。若选区跨行、跨页，或首行滚出视口，
  外接矩形的 top / bottom 会离实际想贴的位置很远，直接用它会把菜单
  推出可视区（表现为「飞到页面顶端、点不到」）。

  所以这里：
    1. 优先用选区「第一行」的矩形作为锚点（getClientRects 的第一个）；
    2. 上、下两个候选位置都算出来，选一个真正能放下的；
    3. 最后无条件把结果钳制进 viewer 可视区，保证永远点得到。
*/
function showSelMenu(rect) {
  const menu = els.selMenu;
  if (!menu) return;
  menu.hidden = false;

  // 先量尺寸。此刻虽已取消隐藏，但布局可能尚未刷新，
  // offsetWidth 会返回 0 —— 用 getBoundingClientRect 更可靠，
  // 仍为 0 时（极罕见）退回估算值。
  const box = menu.getBoundingClientRect();
  const mw = box.width || menu.offsetWidth || 300;
  const mh = box.height || menu.offsetHeight || 34;

  const vr = els.viewer.getBoundingClientRect();

  // 菜单是 .viewer 内的 absolute 元素，坐标基准是 viewer 的「内容区原点」，
  // 而 rect / vr 都是视口坐标。两者相差一个滚动量，必须补上，
  // 否则页面滚得越远、菜单偏得越离谱（表现为往上飞出可视区）。
  const sx = els.viewer.scrollLeft;
  const sy = els.viewer.scrollTop;

  // 选区相对 viewer 内容区的坐标
  const selLeft = rect.left - vr.left + sx;
  const selTop = rect.top - vr.top + sy;
  const selBottom = rect.bottom - vr.top + sy;

  // 可视窗口（相对内容区）的上下边界
  const viewTop = sy + 6;
  const viewBottom = sy + vr.height - 6;

  const GAP = 8;      // 与选区的间距
  const EDGE = 6;     // 与 viewer 边缘的最小留白

  // 水平：以选区中心对齐，再钳制在可视区内（左右边界都用内容区坐标）
  const minLeft = sx + EDGE;
  const maxLeft = sx + Math.max(EDGE, vr.width - mw - EDGE);
  let left = selLeft + rect.width / 2 - mw / 2;
  left = Math.min(Math.max(left, minLeft), maxLeft);

  // 垂直：优先放选区上方；上方放不下则放下方。
  // 两个位置都不理想时，钳制进可视窗口（宁可压住文字，也不能点不到）。
  const above = selTop - mh - GAP;
  const below = selBottom + GAP;

  let top;
  if (above >= viewTop) {
    top = above;                                   // 上方放得下，首选上方
  } else if (below + mh <= viewBottom) {
    top = below;                                   // 上方不够，下方放得下
  } else {
    top = Math.min(Math.max(above, viewTop), viewBottom - mh);  // 强行钳制
  }
  top = Math.min(Math.max(top, viewTop), viewBottom - mh);

  menu.style.left = left + 'px';
  menu.style.top = top + 'px';

  // 兜底：菜单弹出后一段时间内无操作，自动收起，避免遮挡正文
  if (selMenuTimer) clearTimeout(selMenuTimer);
  selMenuTimer = setTimeout(() => {
    if (menuBusy) return;
    hideSelMenu();
    pendingSel = null;
  }, 6000);
}

/* 监听选区变化 */
function onSelectionChange() {
  // 正在点浮动菜单/气泡时不处理，否则会把菜单误关掉
  if (menuBusy) return;

  // 编辑模式下不弹划词菜单 —— 此时鼠标是"画笔"，不是"选择文字"
  if (editState.on) return;

  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) {
    // 选区消失：菜单如果还开着，只有在没有待处理锚点时才关
    if (!pendingSel) hideSelMenu();
    return;
  }
  const text = sel.toString().trim();
  if (!text) { hideSelMenu(); return; }

  const range = sel.getRangeAt(0);
  if (!els.viewer.contains(range.commonAncestorContainer)) { hideSelMenu(); return; }

  // 固化锚点快照 —— 后面即使选区被清空，这份数据依然有效
  const anchors = selectionToAnchors();
  if (!anchors) { hideSelMenu(); return; }
  pendingSel = anchors;

  // 菜单定位用「选区第一行」的矩形，而不是整个选区的外接矩形。
  // 跨行/跨页选区的外接矩形会把底部拉到很远，导致菜单飞出可视区。
  let anchorRect = null;
  try {
    const rects = range.getClientRects();
    if (rects && rects.length) {
      const first = rects[0];
      // 取首行矩形，但水平范围用整段选区，横向居中更符合直觉
      const whole = range.getBoundingClientRect();
      anchorRect = {
        left: whole.left, width: whole.width,
        top: first.top, bottom: first.bottom,
      };
    }
  } catch (e) { /* 忽略，回退到下面的兜底 */ }
  showSelMenu(anchorRect || range.getBoundingClientRect());
}

/* 收尾：隐藏菜单、清空待处理锚点、清除浏览器选区 */
function finishMenuAction() {
  hideSelMenu();
  pendingSel = null;
  try { window.getSelection()?.removeAllRanges(); } catch (e) { /* ignore */ }
}

/* --------------------------------------------------------------------------
   编辑模式：拖拽绘制
   -------------------------------------------------------------------------- */

let dragState = null;      // { rec, page, start, tool, color }  绘制新对象
let moveState = null;      // 拖动已有对象：{ id, startX/Y, orig, rec, rw/rh, pw/ph }
let textPopInfo = null;    // 文字输入浮层的上下文
let objBarId = null;       // 操作条当前绑定的对象 id

/* 拖动的最小位移阈值（屏幕像素）—— 小于它就当"点击"，不当"拖动"。
   没有阈值的话，手抖一个像素也会把对象挪走。 */
const MOVE_THRESHOLD = 3;

/* 找出坐标落在哪一页上 */
function pageAt(clientX, clientY) {
  for (const rec of state.pages) {
    const r = rec.wrap.getBoundingClientRect();
    if (clientX >= r.left && clientX <= r.right &&
        clientY >= r.top && clientY <= r.bottom) {
      return rec;
    }
  }
  return null;
}

/* 选中对象后弹出的操作条：删除 / 置顶 / 置底。
   放在这里而不是像原来那样弹 confirm() —— 原生对话框会打断操作流，
   而且它在 Firefox 里会冻结渲染，既丑又慢。 */
function showObjBar(rec, obj) {
  const bar = els.objBar;
  if (!bar) return;

  if (!obj) { hideObjBar(); return; }

  const names = { whiteout: '涂白块', text: '文字', rect: '矩形',
                  ellipse: '椭圆', line: '直线' };
  if (els.objBarName) els.objBarName.textContent = names[obj.kind] || '对象';
  objBarId = obj.id;

  // 贴在对象上方；对象太靠上就翻到下方。
  // ★ 锚点必须从**对象自己的数据**算，不要依赖 obj._norm ——
  //   那只是「避免反复换算」的渲染缓存，位置一变就被作废
  //   （见 edit.js 的 updateObject）。依赖它的话，拖动过的对象
  //   会让操作条跑回页面角落。
  const r = rec.wrap.getBoundingClientRect();
  const ps = pageSize(obj.page);
  const p = pdfToNorm(obj.x, obj.y, obj.w || 0, obj.h || 0,
                      ps.width, ps.height);
  const anchorLeft = r.left + p.x * r.width;
  const anchorTop = r.top + p.y * r.height;

  const vr = els.viewer.getBoundingClientRect();
  const sx = els.viewer.scrollLeft;
  const sy = els.viewer.scrollTop;

  bar.hidden = false;
  const bw = bar.offsetWidth || 180;
  const bh = bar.offsetHeight || 30;

  let left = anchorLeft - vr.left + sx;
  left = Math.min(Math.max(left, sx + 6),
                  sx + Math.max(6, vr.width - bw - 6));

  let top = anchorTop - vr.top + sy - bh - 8;
  const viewTop = sy + 6;
  if (top < viewTop) top = anchorTop - vr.top + sy + 8;
  top = Math.min(Math.max(top, viewTop),
                 sy + Math.max(6, vr.height - bh - 6));

  bar.style.left = left + 'px';
  bar.style.top = top + 'px';
}

function hideObjBar() {
  objBarId = null;
  if (els.objBar) els.objBar.hidden = true;
}

/*
  双击对象 → 编辑内容。

  目前只支持**文字对象**（改文字 + 字号）—— 文字是唯一「内容可变」的对象；
  图形对象要改只能删了重画，那是另一套交互，先不做。

  为什么限定「选择工具」：文字工具单击就会弹出输入框，
  再叠一层双击语义会互相打架。

  没有这一步的后果：**打错一个字就得删掉重打**，位置也白定了。
*/
function onEditDoubleClick(e) {
  if (!editState.on || editState.tool !== 'select') return;
  if (!e.target.closest('.page')) return;
  const rec = pageAt(e.clientX, e.clientY);
  if (!rec) return;
  const obj = hitTest(rec, e.clientX, e.clientY);
  if (!obj || obj.kind !== 'text') return;
  e.preventDefault();
  openTextPop(rec, e.clientX, e.clientY, obj);
}

/* 编辑层的鼠标按下 */
function onEditMouseDown(e) {
  if (!editState.on || !editState.tool) return;
  if (e.button !== 0) return;

  // 落笔必须发生在**页面内容**上。
  // 编辑 UI（工具面板 / 文字输入框 / 对象操作条 / 页面管理 / 涂黑确认框）
  // 全都是 #viewer 的后代，它们的 mousedown 会一路冒泡到这里。
  // 不排除的话，点「确定」会被当成一次新的落笔，而落点恰好在按钮处：
  // 于是原地又弹出一个空输入框、已输入的文字被静默清空；同时 click 因
  // 按钮已隐藏而根本不会派发 —— "确定"就永远不生效。
  // 对象操作条（置顶/置底/删除）和工具面板的按钮同理会被吃掉。
  // 用 closest('.page') 一次排除全部浮层，比逐个列举容器类名更不容易漏
  // （onBoxDown 用的就是同一条判据）。
  if (!e.target.closest('.page')) return;

  // 文字输入浮层开着时，先关掉它
  closeTextPop();

  const rec = pageAt(e.clientX, e.clientY);
  if (!rec) return;

  const tool = editState.tool;

  // 选择工具：点中对象则选中并弹出操作条，点空白则取消选中。
  // 命中对象时同时进入「可拖动」状态 —— 移动超过阈值才算拖动。
  if (tool === 'select') {
    const obj = hitTest(rec, e.clientX, e.clientY);
    editState.selectedId = obj ? obj.id : null;
    repaintEdits();
    showObjBar(rec, obj);
    if (obj) {
      const r = rec.wrap.getBoundingClientRect();
      const ps = pageSize(rec.num);
      moveState = {
        id: obj.id,
        rec,
        startX: e.clientX,
        startY: e.clientY,
        // 记**原始坐标**：每一帧都从它算位移，不做累加 ——
        // 累加会累积浮点误差，中途丢一帧事件还会永久漂移
        orig: { x: obj.x, y: obj.y, x2: obj.x2, y2: obj.y2 },
        rw: r.width,
        rh: r.height,
        pw: ps.width,
        ph: ps.height,
        moved: false,
      };
    }
    return;
  }

  e.preventDefault();

  // 选中别的对象时，开始绘制前先收起操作条
  hideObjBar();

  // 文字工具：单击即弹出输入框（不需要拖）
  if (tool === 'text') {
    openTextPop(rec, e.clientX, e.clientY);
    return;
  }

  dragState = {
    rec,
    page: rec.num,
    start: { x: e.clientX, y: e.clientY },
    tool,
    color: currentColor(),
  };
  document.body.classList.add('dragging-edit');
}

/*
  拖动已有对象。

  位移换算（含 y 轴翻转）交给 geom.js 的纯函数，这里只管阈值与副作用。
  ★ 位置每帧都从**原始坐标**重算，不在上一帧的结果上累加 ——
    累加会累积误差，中途丢一帧事件还会永久漂移。
*/
function dragMoveObject(e) {
  const st = moveState;
  const dxs = e.clientX - st.startX;
  const dys = e.clientY - st.startY;

  if (!st.moved) {
    if (Math.abs(dxs) < MOVE_THRESHOLD && Math.abs(dys) < MOVE_THRESHOLD) {
      return;                    // 还没超过阈值：仍算「点击」，不算拖动
    }
    st.moved = true;
    hideObjBar();                // 拖动时收起操作条，免得挡视线
  }

  const d = dragDeltaToPdf(dxs, dys, st.rw, st.rh, st.pw, st.ph);
  updateObject(st.id, movedPosition(st.orig, d));
}

/* 拖拽中：实时预览 */
function onEditMouseMove(e) {
  if (moveState) { dragMoveObject(e); return; }
  if (!dragState) return;
  const { rec, page, start, tool, color } = dragState;

  const opts = { color, lineWidth: tool === 'line' ? 1.5 : 1, fill: false };

  const temp = tool === 'line'
    ? makeLineObject(rec, page, start, { x: e.clientX, y: e.clientY }, opts)
    : makeRectObject(rec, page, start, { x: e.clientX, y: e.clientY },
                     tool, opts);

  // 转成归一化坐标供渲染（避免反复换算导致抖动）
  if (tool === 'line') {
    temp._norm = {
      x: temp.x / pageSize(page).width,
      y: 1 - temp.y / pageSize(page).height,
      x2: temp.x2 / pageSize(page).width,
      y2: 1 - temp.y2 / pageSize(page).height,
    };
  } else {
    const { width, height } = pageSize(page);
    temp._norm = {
      x: temp.x / width,
      y: 1 - (temp.y + temp.h) / height,
      w: temp.w / width,
      h: temp.h / height,
    };
  }

  editState.tempShape = temp;
  paintPageEdits(rec);
}

/* 拖拽结束：落成一个对象 */
function onEditMouseUp(e) {
  // 拖动已有对象：坐标在拖动过程中已经写回了，这里只收尾
  if (moveState) {
    const st = moveState;
    moveState = null;
    if (st.moved) {
      const obj = editState.objects.find(o => o.id === st.id);
      if (obj) showObjBar(st.rec, obj);   // 把操作条贴回对象旁边
      toast('已移动', 900);
    }
    return;
  }
  if (!dragState) return;
  const { rec, page, start, tool, color } = dragState;
  dragState = null;
  document.body.classList.remove('dragging-edit');

  const dx = Math.abs(e.clientX - start.x);
  const dy = Math.abs(e.clientY - start.y);

  // 太小的一拖当作误操作，忽略（避免点一下就留个碎点）
  if (tool !== 'line' && dx < 4 && dy < 4) {
    editState.tempShape = null;
    paintPageEdits(rec);
    return;
  }

  const opts = { color, lineWidth: tool === 'line' ? 1.5 : 1, fill: false };
  let obj;
  if (tool === 'line') {
    obj = makeLineObject(rec, page, start, { x: e.clientX, y: e.clientY }, opts);
    if (Math.hypot(e.clientX - start.x, e.clientY - start.y) < 5) {
      editState.tempShape = null;
      paintPageEdits(rec);
      return;
    }
  } else {
    obj = makeRectObject(rec, page, start, { x: e.clientX, y: e.clientY },
                         tool, opts);
  }

  editState.tempShape = null;
  addObject(obj);
  const label = { whiteout: '涂白', rect: '矩形', ellipse: '椭圆',
                  line: '直线', redact: '涂黑区域' }[tool] || '对象';
  // 涂黑只标记区域，真正的销毁在点「涂黑并另存为」时发生 ——
  // 这里提示一下，免得用户以为已经删掉了
  if (tool === 'redact') {
    toast('已标记涂黑区域，点「涂黑并另存为…」才会真正删除文字', 3200);
  } else {
    toast(`已添加${label}`, 1000);
  }
}

/* ---- 文字输入浮层 ---- */

function openTextPop(rec, clientX, clientY, editing = null) {
  const pop = els.textPop;
  if (!pop) return;

  const vr = els.viewer.getBoundingClientRect();
  const r = rec.wrap.getBoundingClientRect();

  // 记住落点（归一化），确定时用它定位。
  // editing 非空 = 在**修改已有的文字对象**（双击进入），
  // 此时只改内容与字号，位置保持原样。
  textPopInfo = {
    rec,
    page: rec.num,
    nx: (clientX - r.left) / (r.width || 1),
    ny: (clientY - r.top) / (r.height || 1),
    editingId: editing ? editing.id : null,
  };

  pop.hidden = false;
  // 新建时用默认值；修改时**带出原值** —— 否则一打开就把原文清空了
  if (els.tipText) els.tipText.value = editing ? (editing.text || '') : '';
  if (els.tipSize) {
    els.tipSize.value = String(editing ? (editing.size || 14) : 14);
  }

  let left = clientX - vr.left;
  let top = clientY - vr.top;
  left = Math.max(8, Math.min(left, vr.width - 250));
  if (top + 150 > vr.height) top = Math.max(8, vr.height - 170);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';

  setTimeout(() => els.tipText && els.tipText.focus(), 30);
}

function closeTextPop() {
  if (els.textPop) els.textPop.hidden = true;
  textPopInfo = null;
}

function confirmTextPop() {
  if (!textPopInfo) return;
  const txt = (els.tipText && els.tipText.value || '').trim();
  const size = parseFloat(els.tipSize && els.tipSize.value) || 14;
  const { rec, page, nx, ny, editingId } = textPopInfo;

  // ★ 修改**已有的**文字对象（双击进入）：只改内容与字号，**不动位置** ——
  //   位置是用户当初落笔定的，改文字不该把它挪走。
  //   行数变了也没关系：锚点是首行基线，多出来的行自然往下长。
  if (editingId) {
    if (!txt) {
      // 这里不能静默关掉 —— 用户会以为改动生效了，其实没有
      toast('文字不能为空。要删掉这段文字，请用选择工具删掉对象', 2600);
      return;
    }
    updateObject(editingId, { text: txt, size });
    closeTextPop();
    toast('已更新文字', 1100);
    return;
  }

  if (!txt) { closeTextPop(); return; }

  const { width, height } = pageSize(page);

  // 文字锚点：(nx, ny) 是落点（相对页面左上角的归一化坐标）。
  // PDF 的 Td 定位的是**基线**，且 y 轴向上，
  // 所以要把落点沿 y 向下偏移一行高度，再整体翻转。
  const lineH = size * 1.25;

  addObject({
    page,
    kind: 'text',
    x: nx * width,
    y: height * (1 - ny) - lineH,
    w: 0,
    h: 0,
    text: txt,
    size,
    font: 'Helvetica',
    color: currentColor(),
  });

  closeTextPop();
  toast('已添加文字', 1100);
}

/* 应用标注：sel 为已固化的选区快照 */
function applyAnnotation(sel, type, color) {
  if (!sel || !sel.parts || !sel.parts.length) {
    toast('选区已失效，请重新选择文字', 1800);
    return;
  }

  if (type === 'note') {
    // 笔记：先建一条高亮，再打开气泡写内容
    const an = addAnnotation(sel, 'highlight', color || currentAnnotColorHex, '');
    setTimeout(() => openAnnotPop(an.id), 60);
    return;
  }

  addAnnotation(sel, type, color, '');
  const label = { highlight: '高亮', underline: '下划线', wavy: '波浪线',
                  strike: '删除线' }[type] || '标注';
  toast(`已添加${label}`, 1100);
}

/* 打开标注气泡 */
function openAnnotPop(id) {
  const an = annState.items.find(a => a.id === id);
  if (!an) return;
  popAnnotId = id;

  els.apTag.textContent = { highlight: '高亮', underline: '下划线', wavy: '波浪线',
                            strike: '删除线', note: '笔记', box: '区域' }[an.type] || an.type;
  els.apText.textContent = an.text || '（区域标注）';
  els.apNote.value = an.note || '';

  // 定位到标注附近
  const rec = state.pages[an.page - 1];
  const el = rec && rec.wrap.querySelector(`.an[data-id="${id}"]`);
  const vr = els.viewer.getBoundingClientRect();
  const pop = els.annotPop;
  pop.hidden = false;

  const pw = pop.offsetWidth || 306;
  let left, top;
  if (el) {
    const r = el.getBoundingClientRect();
    left = r.left - vr.left;
    top = r.bottom - vr.top + 10;
  } else {
    left = vr.width / 2 - pw / 2;
    top = 80;
  }
  left = Math.max(8, Math.min(left, vr.width - pw - 8));
  if (top + 200 > vr.height) top = Math.max(8, (el ? el.getBoundingClientRect().top - vr.top : 100) - 210);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';

  els.apNote.focus();
}

function closeAnnotPop() {
  els.annotPop && (els.annotPop.hidden = true);
  popAnnotId = null;
}

/* --------------------------------------------------------------------------
   框选区域标注
   -------------------------------------------------------------------------- */

let boxStart = null;
let boxEl = null;

function onBoxDown(e) {
  if (!state.annotMode || !state.pdf) return;
  if (e.button !== 0) return;
  const page = e.target.closest('.page');
  if (!page) return;

  e.preventDefault();
  const pageRect = page.getBoundingClientRect();
  boxStart = {
    page: parseInt(page.dataset.page, 10),
    x: e.clientX - pageRect.left,
    y: e.clientY - pageRect.top,
    rect: pageRect,
    el: page,
  };
  boxEl = document.createElement('div');
  boxEl.className = 'box-rubber';
  page.appendChild(boxEl);

  const move = (ev) => {
    if (!boxStart) return;
    const x = Math.max(0, Math.min(ev.clientX - boxStart.rect.left, boxStart.rect.width));
    const y = Math.max(0, Math.min(ev.clientY - boxStart.rect.top, boxStart.rect.height));
    const left = Math.min(boxStart.x, x);
    const top = Math.min(boxStart.y, y);
    boxEl.style.left = left + 'px';
    boxEl.style.top = top + 'px';
    boxEl.style.width = Math.abs(x - boxStart.x) + 'px';
    boxEl.style.height = Math.abs(y - boxStart.y) + 'px';
  };

  const up = (ev) => {
    document.removeEventListener('mousemove', move);
    document.removeEventListener('mouseup', up);
    if (!boxStart) return;

    const x = Math.max(0, Math.min(ev.clientX - boxStart.rect.left, boxStart.rect.width));
    const y = Math.max(0, Math.min(ev.clientY - boxStart.rect.top, boxStart.rect.height));
    const left = Math.min(boxStart.x, x);
    const top = Math.min(boxStart.y, y);
    const w = Math.abs(x - boxStart.x);
    const h = Math.abs(y - boxStart.y);

    if (boxEl) boxEl.remove();
    const info = boxStart;
    boxStart = null;
    boxEl = null;

    if (w < 12 || h < 12) return;

    const an = {
      id: uid(),
      type: 'box',
      color: currentAnnotColor,
      page: info.page,
      text: '',
      note: '',
      created: Date.now(),
      rect: {
        x: (left / info.rect.width) * 100,
        y: (top / info.rect.height) * 100,
        w: (w / info.rect.width) * 100,
        h: (h / info.rect.height) * 100,
      },
    };
    annState.items.push(an);
    scheduleSave();
    renderAnnotList();
    repaintAnnotations();
    updateProgressMarkers();
    openAnnotPop(an.id);
  };

  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
}

/* --------------------------------------------------------------------------
   欢迎页 / 文件选择
   -------------------------------------------------------------------------- */
let serverFiles = [];

async function renderWelcome() {
  const container = els.pages;
  container.classList.add('empty');
  container.innerHTML = `
  <div class="welcome">
    <h1>PDF 查看器</h1>
    <p class="sub">选择服务端目录中的 PDF，或直接把文件拖进来。所有解析都在本机完成，文件不会上传到任何地方。</p>

    <div class="drop-zone" id="dropZone">
      <div class="big">拖拽 PDF 文件到这里</div>
      <div>或点击选择本地文件</div>
    </div>

    <div class="welcome-head">
      <h2>服务端文档</h2>
      <input class="file-search" id="fileSearch" placeholder="按名称筛选…">
      <button class="btn" id="btnPickDir" title="添加其他目录">
        <svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="9.5" y1="13.5" x2="14.5" y2="13.5"/></svg>
        <span class="lbl">添加目录</span>
      </button>
    </div>

    <div class="file-list" id="fileList">
      <div class="empty-tip"><div class="spinner" style="margin:0 auto 10px"></div>正在扫描文档…</div>
    </div>

    <div class="welcome-foot">
      <span id="scanInfo"></span>
      <button class="link-btn" id="btnRescan">重新扫描</button>
    </div>
  </div>`;

  bindWelcome();
  await loadServerFiles();
}

function bindWelcome() {
  const dz = $('dropZone');
  const picker = $('filePicker');

  dz.addEventListener('click', () => picker.click());
  dz.addEventListener('dragover', (e) => { e.preventDefault(); dz.classList.add('over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('over'));
  dz.addEventListener('drop', (e) => {
    e.preventDefault();
    dz.classList.remove('over');
    const f = e.dataTransfer.files[0];
    if (f) handleLocalFile(f);
  });

  $('fileSearch').addEventListener('input', debounce(() => paintFileList(), 120));
  $('btnRescan').addEventListener('click', () => loadServerFiles());
  $('btnPickDir').addEventListener('click', pickServerDir);
}

async function loadServerFiles() {
  const list = $('fileList');
  if (!list) return;
  try {
    const r = await fetch('/api/files');
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || '扫描失败');
    serverFiles = data.files || [];
    paintFileList();
    const info = $('scanInfo');
    if (info) {
      info.textContent = `共 ${data.count} 个文档 · ${(data.dirs || []).length} 个扫描目录`;
      info.title = (data.dirs || []).join('\n');
    }
  } catch (err) {
    list.innerHTML = `<div class="empty-tip">扫描失败：${escapeHtml(err.message)}<br>
      请确认服务已启动，或点击「重新扫描」</div>`;
  }
}

function paintFileList() {
  const list = $('fileList');
  if (!list) return;
  const kw = ($('fileSearch')?.value || '').trim().toLowerCase();
  const items = kw
    ? serverFiles.filter(f => f.name.toLowerCase().includes(kw) ||
                              f.path.toLowerCase().includes(kw))
    : serverFiles;

  if (!items.length) {
    list.innerHTML = serverFiles.length
      ? `<div class="empty-tip">没有匹配「${escapeHtml(kw)}」的文档</div>`
      : `<div class="empty-tip">未找到 PDF 文件<br><br>
         把 PDF 放进扫描目录，或启动时指定：<br>
         <code>python server.py --dir D:\\你的PDF目录</code></div>`;
    return;
  }

  list.innerHTML = items.slice(0, 300).map((f, i) => `
    <div class="file-item" data-i="${serverFiles.indexOf(f)}">
      <div class="fi-icon">PDF</div>
      <div class="fi-text">
        <div class="fi-name" title="${escapeHtml(f.path)}">${escapeHtml(f.name)}</div>
        <div class="fi-meta">${escapeHtml(f.folder)} · ${f.sizeText}</div>
      </div>
      <div class="fi-open" title="用系统默认程序打开">
        <svg viewBox="0 0 24 24"><path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6"/></svg>
      </div>
    </div>`).join('');

  list.querySelectorAll('.file-item').forEach(el => {
    el.addEventListener('click', (e) => {
      const f = serverFiles[parseInt(el.dataset.i, 10)];
      if (e.target.closest('.fi-open')) {
        fetch('/api/open?path=' + encodeURIComponent(f.path))
          .then(r => r.json())
          .then(d => toast(d.ok ? '已用系统默认程序打开' : ('打开失败：' + d.error)))
          .catch(() => toast('打开失败'));
        return;
      }
      loadPdfFromPath(f.path, f.name);
    });
  });
}

async function pickServerDir() {
  // 浏览器拿不到真实路径，让用户手输；服务端会校验存在性
  const dir = prompt('请输入要扫描的文件夹完整路径：\n例如 D:\\我的PDF', '');
  if (!dir) return;
  showLoading('正在扫描目录…');
  try {
    const r = await fetch('/api/files?dir=' + encodeURIComponent(dir));
    const data = await r.json();
    hideLoading();
    if (!data.ok) { toast('扫描失败：' + data.error); return; }
    if (!data.files.length) { toast('该目录下没有找到 PDF 文件'); return; }
    serverFiles = data.files;
    paintFileList();
    const info = $('scanInfo');
    if (info) info.textContent = `共 ${data.count} 个文档（含临时目录 ${dir}）`;
    toast(`找到 ${data.count} 个文档`, 1600);
  } catch (err) {
    hideLoading();
    toast('扫描失败：' + err.message);
  }
}

async function handleLocalFile(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') {
    toast('请选择 PDF 文件');
    return;
  }
  await loadPdfFromFile(file);
}

/* --------------------------------------------------------------------------
   事件绑定
   -------------------------------------------------------------------------- */

function bindToolbar() {
  // ---- 标注：颜色选择 ----
  els.colorPicker.querySelectorAll('.color-dot').forEach(dot => {
    dot.addEventListener('click', () => {
      els.colorPicker.querySelectorAll('.color-dot').forEach(d => d.classList.remove('on'));
      dot.classList.add('on');
      currentAnnotColor = dot.dataset.color;
      currentAnnotColorHex = dot.style.getPropertyValue('--c').trim() || '#fbbf24';
    });
  });

  // ---- 标注模式开关 ----
  els.btnAnnotMode.addEventListener('click', () => {
    state.annotMode = !state.annotMode;
    els.btnAnnotMode.classList.toggle('on', state.annotMode);
    els.viewer.classList.toggle('annot-mode', state.annotMode);
    toast(state.annotMode ? '标注模式：拖拽可框选区域' : '已退出标注模式', 1500);
  });

  // ---- 视图模式 ----
  els.btnViewSingle.addEventListener('click', () => applyViewMode('single'));
  els.btnViewDouble.addEventListener('click', () => applyViewMode('double'));
  els.btnScrollPlay.addEventListener('click', startAutoScroll);

  // ---- 书签 ----
  els.btnBookmark.addEventListener('click', toggleBookmark);

  // ---- 划词菜单 ----
  // 关键：在容器上用捕获阶段拦住 mousedown 的默认行为，
  // 否则浏览器会先清空选区，pendingSel 之外的现场全部失效。
  const guardMenu = (e) => {
    e.preventDefault();
    e.stopPropagation();
  };
  els.selMenu.addEventListener('mousedown', guardMenu, true);
  els.selMenu.addEventListener('mouseup', (e) => e.stopPropagation(), true);
  els.selMenu.addEventListener('click', (e) => e.stopPropagation());

  els.selMenu.querySelectorAll('.sm-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const act = btn.dataset.act;

      if (act === 'copy') {
        const t = pendingSel ? pendingSel.text : window.getSelection()?.toString();
        if (t) {
          navigator.clipboard.writeText(t).then(
            () => toast('已复制到剪贴板', 1200),
            () => toast('复制失败')
          );
        }
        finishMenuAction();
        return;
      }

      if (!pendingSel) {
        toast('选区已失效，请重新选择文字', 1600);
        finishMenuAction();
        return;
      }

      const color = act === 'highlight' ? currentAnnotColorHex : btn.dataset.color;
      const sel = pendingSel;   // 先把快照取出来
      finishMenuAction();       // 立刻收起菜单、解除选区
      applyAnnotation(sel, act, color);
    });
  });

  // 菜单内的 mousedown 视为「正在操作菜单」
  els.selMenu.addEventListener('mousedown', () => {
    menuBusy = true;
    setTimeout(() => { menuBusy = false; }, 300);
  }, true);

  // ---- 标注气泡 ----
  els.annotPop.addEventListener('mousedown', guardMenu, true);
  els.annotPop.addEventListener('mouseup', (e) => e.stopPropagation(), true);
  els.annotPop.addEventListener('mousedown', () => {
    menuBusy = true;
    setTimeout(() => { menuBusy = false; }, 300);
  }, true);
  els.apSave.addEventListener('click', () => {
    if (popAnnotId) {
      updateAnnotation(popAnnotId, { note: els.apNote.value.trim() });
      toast('笔记已保存', 1200);
    }
    closeAnnotPop();
  });
  els.apDelete.addEventListener('click', () => {
    if (popAnnotId) { removeAnnotation(popAnnotId); toast('已删除标注', 1200); }
    closeAnnotPop();
  });
  els.apCancel.addEventListener('click', () => {
    // 若笔记是空的（新建的笔记标注），取消时一并删除
    const an = annState.items.find(a => a.id === popAnnotId);
    if (an && !an.note && !an.text) removeAnnotation(popAnnotId);
    closeAnnotPop();
  });
  els.apNote.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); els.apCancel.click(); }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); els.apSave.click(); }
  });

  // ---- 标注筛选 ----
  els.annotFilter.querySelectorAll('.af-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      els.annotFilter.querySelectorAll('.af-chip').forEach(c => c.classList.remove('on'));
      chip.classList.add('on');
      annState.filter = chip.dataset.filter;
      renderAnnotList();
    });
  });

  // ---- 点击标注元素 ----
  els.pages.addEventListener('click', (e) => {
    const an = e.target.closest('.an');
    if (an) {
      e.stopPropagation();
      openAnnotPop(an.dataset.id);
      return;
    }
  });

  // ---- 框选 ----
  els.viewer.addEventListener('mousedown', onBoxDown);

  // ---- 导出笔记 ----
  els.annotActions.hidden = false;
  els.btnExportMd.addEventListener('click', () => {
    if (!state.docId) { toast('请先打开文档'); return; }
    if (!annState.items.length) { toast('还没有标注可导出'); return; }
    window.open('/api/notes?doc=' + encodeURIComponent(state.docId) +
                '&name=' + encodeURIComponent(state.fileName), '_blank');
  });
  els.btnExportJson.addEventListener('click', () => {
    if (!annState.items.length) { toast('还没有标注可导出'); return; }
    const blob = new Blob(
      [JSON.stringify({ docId: state.docId, doc: state.fileName, items: annState.items }, null, 2)],
      { type: 'application/json' }
    );
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${state.fileName}-annotations.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
  });

  // ---- 点击空白处收起菜单 ----
  document.addEventListener('mousedown', (e) => {
    if (menuBusy) return;

    // 点在浮动菜单上：交给菜单自己的处理
    if (!els.selMenu.hidden && els.selMenu.contains(e.target)) return;

    // 点在标注气泡上：同理
    if (!els.annotPop.hidden && els.annotPop.contains(e.target)) return;

    // 点其他位置：立即收起划词菜单并作废锚点
    if (!els.selMenu.hidden) {
      hideSelMenu();
      pendingSel = null;
    }

    // 点在文本层上准备重新选择，不关气泡（可能想接着编辑）
    const onText = !!e.target.closest('.text-layer');
    const onAnnot = !!e.target.closest('.an');

    if (!els.annotPop.hidden && !onText && !onAnnot) {
      // 点空白处等同保存
      if (popAnnotId) {
        const an = annState.items.find(a => a.id === popAnnotId);
        if (an) {
          const v = els.apNote.value.trim();
          if (v !== (an.note || '')) {
            if (v || an.text) updateAnnotation(popAnnotId, { note: v });
            else removeAnnotation(popAnnotId);
          }
        }
      }
      closeAnnotPop();
    }
  });

  // ---- 选区监听 ----
  document.addEventListener('selectionchange', debounce(onSelectionChange, 90));

  // ---- 进度条点击跳转 ----
  els.progressBar.addEventListener('click', (e) => {
    const r = els.progressBar.getBoundingClientRect();
    const ratio = (e.clientX - r.left) / r.width;
    const v = els.viewer;
    v.scrollTop = ratio * (v.scrollHeight - v.clientHeight);
  });

  // 窗口失焦、切走标签页时收起菜单
  window.addEventListener('blur', () => {
    if (!els.selMenu.hidden) hideSelMenu();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && !els.selMenu.hidden) hideSelMenu();
  });

  // 按 Esc 也可以收起（已有全局快捷键处理，这里补一个直接绑定）
  els.viewer.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !els.selMenu.hidden) {
      hideSelMenu();
      pendingSel = null;
    }
  });

  // 涂黑确认框：Esc 一律视为取消（绝不默认确认不可逆操作）
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (els.redactConfirm && !els.redactConfirm.hidden) {
      e.preventDefault();
      els.redactConfirm.hidden = true;
    }
  });

  // 缩放/旋转等会改变布局的操作后，菜单位置就失效了，直接收起
  ['zoomSelect', 'btnZoomIn', 'btnZoomOut', 'btnRotate', 'btnFitWidth']
    .forEach(id => {
      const el = els[id];
      if (el) el.addEventListener('click', () => hideSelMenu());
    });

  // ---- 编辑模式 ----
  bindEditUI();

  // 编辑层的事件：都挂在 viewer 上，用 pageAt 判断落在哪页
  els.viewer.addEventListener('mousedown', onEditMouseDown);
  // 双击文字对象 → 修改内容。没有它，打错一个字只能删掉重打。
  els.viewer.addEventListener('dblclick', onEditDoubleClick);
  window.addEventListener('mousemove', onEditMouseMove);
  window.addEventListener('mouseup', onEditMouseUp);

  // 文字浮层
  if (els.tipOk) els.tipOk.addEventListener('click', confirmTextPop);
  if (els.tipCancel) els.tipCancel.addEventListener('click', closeTextPop);
  if (els.tipText) {
    els.tipText.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); closeTextPop(); }
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault(); confirmTextPop();
      }
    });
  }
  // 点浮层外区域关闭（文字浮层 + 对象操作条）
  document.addEventListener('mousedown', (e) => {
    if (els.objBar && !els.objBar.hidden && !els.objBar.contains(e.target)) {
      hideObjBar();
    }
    if (!els.textPop || els.textPop.hidden) return;
    if (els.textPop.contains(e.target)) return;
    closeTextPop();
  }, true);

  // 编辑对象操作条
  if (els.objBarDel) {
    els.objBarDel.addEventListener('click', () => {
      if (objBarId == null) return;
      removeObject(objBarId);
      hideObjBar();
      toast('已删除对象', 1100);
    });
  }
  if (els.objBarFront) {
    els.objBarFront.addEventListener('click', () => {
      if (objBarId == null) return;
      // 数组末尾即绘制在上层
      const i = editState.objects.findIndex(o => o.id === objBarId);
      if (i >= 0 && i < editState.objects.length - 1) {
        const [o] = editState.objects.splice(i, 1);
        editState.objects.push(o);
        repaintEdits();
        toast('已置顶', 900);
      }
    });
  }
  if (els.objBarBack) {
    els.objBarBack.addEventListener('click', () => {
      if (objBarId == null) return;
      const i = editState.objects.findIndex(o => o.id === objBarId);
      if (i > 0) {
        const [o] = editState.objects.splice(i, 1);
        editState.objects.unshift(o);
        repaintEdits();
        toast('已置底', 900);
      }
    });
  }

  // ---- 页码/缩放等原有按钮 ----
  els.btnSidebar.addEventListener('click', () => {
    els.sidebar.classList.toggle('collapsed');
    // 侧边栏开合后重新计算缩放
    setTimeout(() => {
      if (state.pdf && state.scaleMode === 'auto') applyScale('auto');
    }, 220);
  });

  els.btnHome.addEventListener('click', () => {
    if (!state.pdf) return;
    if (confirm('关闭当前文档并返回列表？')) closeDocument();
  });

  els.btnFirst.addEventListener('click', () => goToPage(1));
  els.btnLast.addEventListener('click', () => goToPage(state.pageCount));
  els.btnPrev.addEventListener('click', () => goToPage(state.currentPage - 1));
  els.btnNext.addEventListener('click', () => goToPage(state.currentPage + 1));

  els.pageInput.addEventListener('change', () => goToPage(els.pageInput.value));
  els.pageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { goToPage(els.pageInput.value); els.viewer.focus(); }
  });

  els.btnZoomIn.addEventListener('click', () => stepZoom(1));
  els.btnZoomOut.addEventListener('click', () => stepZoom(-1));
  els.zoomSelect.addEventListener('change', () => applyScale(els.zoomSelect.value));

  els.btnRotate.addEventListener('click', rotate);
  els.btnDark.addEventListener('click', toggleInvert);
  els.btnFitWidth.addEventListener('click', () => {
    applyScale('width');
    els.zoomSelect.value = 'width';
  });

  els.btnPrint.addEventListener('click', () => {
    if (!state.pdf) return;
    state.pdf.getData().then(d => {
      const blob = new Blob([d], { type: 'application/pdf' });
      const url = URL.createObjectURL(blob);
      const w = window.open(url);
      if (w) setTimeout(() => { try { w.print(); } catch (e) {} }, 800);
      else toast('打印窗口被浏览器拦截，请允许弹出窗口');
    });
  });

  // 搜索
  const si = els.searchInput;
  si.addEventListener('input', debounce(() => {
    els.searchWrap.classList.toggle('has-text', si.value.length > 0);
    runSearch(si.value);
  }, 320));
  si.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      if (state.searchHits.length) gotoHit(state.hitIndex + (e.shiftKey ? -1 : 1));
      else runSearch(si.value);
    }
    if (e.key === 'Escape') { si.value = ''; els.searchWrap.classList.remove('has-text'); runSearch(''); si.blur(); }
  });
  els.searchClear.addEventListener('click', () => {
    si.value = '';
    els.searchWrap.classList.remove('has-text');
    runSearch('');
    si.focus();
  });
  els.btnPrevHit.addEventListener('click', () => gotoHit(state.hitIndex - 1));
  els.btnNextHit.addEventListener('click', () => gotoHit(state.hitIndex + 1));

  // 侧栏切换
  document.querySelectorAll('.side-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.side-tab').forEach(t => t.classList.toggle('on', t === tab));
      const key = tab.dataset.tab;
      $('panelThumbs').classList.toggle('on', key === 'thumbs');
      $('panelOutline').classList.toggle('on', key === 'outline');
    });
  });

  // 滚动时更新页码与进度，并收起划词菜单
  els.viewer.addEventListener('scroll', () => {
    if (!els.selMenu.hidden) hideSelMenu();
  }, { passive: true });

  // 缩放会改变页面尺寸，操作条位置随之失效 —— 直接收起
  // （滚动不收起：对象还在原地，只是视口移动，用户可能正要点按钮）
  ['zoomSelect', 'btnZoomIn', 'btnZoomOut', 'btnRotate', 'btnFitWidth']
    .forEach(id => {
      const el = els[id];
      if (el) el.addEventListener('click', () => hideObjBar());
    });

  els.viewer.addEventListener('scroll', debounce(() => {
    updateCurrentPage();
    updateProgress();
  }, 60), { passive: true });

  // Ctrl + 滚轮缩放
  els.viewer.addEventListener('wheel', (e) => {
    if (!state.pdf) return;
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      stepZoom(e.deltaY < 0 ? 1 : -1);
    }
  }, { passive: false });

  // 点击空白区域渲染邻近页
  els.viewer.addEventListener('click', (e) => {
    if (!state.pdf) return;
    const rec = e.target.closest('.page');
    if (rec) {
      const n = parseInt(rec.dataset.page, 10);
      state.pages[n - 1] && renderPage(state.pages[n - 1]);
    }
  });

  // 拖拽到阅读区也能打开
  ['dragover', 'drop'].forEach(ev => {
    document.addEventListener(ev, (e) => {
      if (ev === 'dragover') { e.preventDefault(); return; }
      e.preventDefault();
      const f = e.dataTransfer?.files?.[0];
      if (f) handleLocalFile(f);
    });
  });

  // 全局快捷键
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || e.target.isContentEditable;

    if ((e.ctrlKey || e.metaKey) && !typing) {
      if (e.key === 'o') { e.preventDefault(); els.filePicker.click(); return; }
      if (e.key === '=' || e.key === '+') { e.preventDefault(); stepZoom(1); return; }
      if (e.key === '-') { e.preventDefault(); stepZoom(-1); return; }
      if (e.key === '0') { e.preventDefault(); applyScale('auto'); els.zoomSelect.value = 'auto'; return; }
      if (e.key === 'f') { e.preventDefault(); els.searchInput.focus(); els.searchInput.select(); return; }
      if (e.key === 'p') { e.preventDefault(); els.btnPrint.click(); return; }
    }

    if (typing) return;
    if (!state.pdf) return;

    switch (e.key) {
      case 'PageDown': case 'j': case 'J':
        e.preventDefault(); goToPage(state.currentPage + 1, true); break;
      case 'PageUp': case 'k': case 'K':
        e.preventDefault(); goToPage(state.currentPage - 1, true); break;
      case 'Home':
        e.preventDefault(); goToPage(1); break;
      case 'End':
        e.preventDefault(); goToPage(state.pageCount); break;
      case 'ArrowDown': case 'ArrowUp': case 'ArrowLeft': case 'ArrowRight':
        break;
      case ' ':
        e.preventDefault(); startAutoScroll(); break;
      case 'Escape':
        hideSelMenu(); closeAnnotPop();
        if (state.annotMode) {
          state.annotMode = false;
          els.btnAnnotMode.classList.remove('on');
          els.viewer.classList.remove('annot-mode');
        }
        break;
      case 'r': case 'R': rotate(); break;
      case 'd': case 'D': toggleInvert(); break;
      case 'w': case 'W': applyScale('width'); els.zoomSelect.value = 'width'; break;
      case 'a': case 'A': els.btnAnnotMode.click(); break;
      case 'e': case 'E': els.btnEditMode && els.btnEditMode.click(); break;
      case 'b': case 'B': toggleBookmark(); break;
      case '2': applyViewMode(state.viewMode === 'double' ? 'single' : 'double'); break;
      case 's': case 'S': els.btnSidebar.click(); break;
      case 'h': case 'H':
        if (confirm('关闭当前文档并返回列表？')) closeDocument();
        break;
    }
  });

  els.filePicker.addEventListener('change', () => {
    const f = els.filePicker.files[0];
    if (f) handleLocalFile(f);
    els.filePicker.value = '';
  });
}

function stepZoom(dir) {
  const steps = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4, 5];
  const cur = state.scale;
  let target;
  if (dir > 0) target = steps.find(s => s > cur + 0.001) ?? steps[steps.length - 1];
  else target = [...steps].reverse().find(s => s < cur - 0.001) ?? steps[0];

  els.zoomSelect.value = String(target);
  const opt = [...els.zoomSelect.options].find(o => o.value === String(target));
  if (!opt) {
    // 不在预设里则动态插入
    const o = new Option(Math.round(target * 100) + '%', String(target));
    els.zoomSelect.add(o);
    els.zoomSelect.value = String(target);
  }
  applyScale(String(target));
}

/* 窗口尺寸变化时，若处于自适应模式则重算 */
const onResize = debounce(() => {
  if (state.pdf && state.scaleMode === 'auto') applyScale('auto');
}, 220);

/* --------------------------------------------------------------------------
   初始化
   -------------------------------------------------------------------------- */

function cacheAllEls() {
  cacheEls([
    'viewer', 'pages', 'loading', 'loadingText', 'toast', 'sidebar',
    'thumbList', 'outlineList', 'panelThumbs', 'panelOutline',
    'btnSidebar', 'btnHome', 'navGroup', 'navSep', 'btnFirst', 'btnPrev',
    'pageInput', 'pageTotal', 'btnNext', 'btnLast',
    'zoomGroup', 'zoomSep', 'btnZoomOut', 'zoomSelect', 'btnZoomIn',
    'toolGroup', 'toolGroupSep', 'btnRotate', 'btnDark', 'btnFitWidth',
    'searchWrap', 'searchInput', 'searchClear', 'searchCount', 'searchNav',
    'btnPrevHit', 'btnNextHit', 'searchSep', 'fileGroup', 'btnPrint',
    'filePicker',
    // 新增
    'annotGroup', 'annotSep', 'btnAnnotMode', 'colorPicker',
    'viewGroup', 'btnViewSingle', 'btnViewDouble', 'btnScrollPlay',
    'btnBookmark',
    'panelAnnots', 'annotList', 'annotFilter', 'annotCount',
    'annotActions', 'btnExportMd', 'btnExportJson',
    'progressBar', 'progressFill', 'progressMarkers',
    'selMenu', 'annotPop', 'apTag', 'apText', 'apNote', 'apSave', 'apCancel', 'apDelete',
    // 编辑模式
    'editGroup', 'editSep', 'btnEditMode', 'editPalette', 'editColor',
    'editColorValue', 'btnEditUndo', 'btnEditClear', 'btnExportPdf',
    'editSaveHint', 'pagePanel', 'pageListBody',
    'textPop', 'tipText', 'tipSize', 'tipCancel', 'tipOk',
    'objBar', 'objBarName', 'objBarFront', 'objBarBack', 'objBarDel',
    // 涂黑
    'btnRedactPdf', 'redactCount', 'redactConfirm', 'redactConfirmStats',
    'redactCancel', 'redactOk',
  ]);
  els.sideBody = document.querySelector('.side-body');
}

async function init() {
  cacheAllEls();
  bindNavHelpers(goToPage, renderPage);
  bindEditHelpers({ goToPage, renderPage });
  bindToolbar();
  window.addEventListener('resize', onResize);

  // 离开页面前保存进度
  window.addEventListener('beforeunload', savePosition);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { savePosition(); doSave(); saveEdits(); }
  });

  const params = new URLSearchParams(location.search);
  const direct = params.get('path');
  if (direct) {
    await loadPdfFromPath(direct, params.get('name') || '文档',
                          params.get('doc') || null);
  } else {
    renderWelcome();
  }
}

init();
