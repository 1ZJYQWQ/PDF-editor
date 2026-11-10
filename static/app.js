

import { t, initI18n, currentLang, nextLang, setLang, LANG_SHORT } from '/static/i18n.js';
import {
  pdfjsLib, state, els, cacheEls, $, uid, escapeHtml, debounce,
  toast, showLoading, hideLoading, api,
  errText,
} from '/static/core.js';

import {
  annState, loadAnnotations, scheduleSave, doSave,
  selectionToAnchors, addAnnotation, updateAnnotation, removeAnnotation,
  repaintAnnotations, paintPageAnnotations, renderAnnotList, focusAnnotation,
  updateProgressMarkers, bindNavHelpers, loadPagePdfAnnots,
} from '/static/annot.js';

import {
  editState, bindEditHelpers, bindEditUI, loadEdits, doSave as saveEdits,
  repaintEdits, paintPageEdits, setEditMode, setTool, currentColor,
  makeRectObject, makeLineObject, hitTest, removeObject, addObject,
  updateObject, pageSize, screenToNorm, refreshPagePanel, insertBlank,
  exportPdf, buildExportPayload, pdfToNorm, reflowParagraph, discardStage,
  undoEdit, redoEdit, copySelected, pasteObject, duplicateSelected,
  setStageUndoHandler, setStagePresent, redactCount, dropTableRow,
  insertTableRow, insertImageFile,
} from '/static/edit.js';

import { dragDeltaToPdf, movedPosition, resizedRect, wrapText, paginateText } from '/static/geom.js';

import { initBatchUI } from '/static/batch.js';

import * as tabs from '/static/tabs.js';

import { bindWelcomeUpdate } from '/static/update.js';

const MAX_CANVAS_PIXELS = 16 * 1024 * 1024;
const BUFFER_PAGES = 1;
const LS_PREFIX = 'pdfviewer:';

let welcomeReady = false;

function pageSizeAt(page, scale) {
  const vp = page.getViewport({ scale, rotation: state.rotation });
  return { width: vp.width, height: vp.height };
}

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
  } catch (e) {  }
}

function readPosition() {
  try {
    const raw = localStorage.getItem(posKey());
    return raw ? JSON.parse(raw) : null;
  } catch (e) { return null; }
}

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
  } catch (e) {  }
}

function toggleBookmark() {
  if (!state.pdf) return;
  const p = state.currentPage;
  if (state.bookmarks.has(p)) { state.bookmarks.delete(p); toast(t('已取消书签')); }
  else { state.bookmarks.add(p); toast(t('第 {p} 页已加书签', { p })); }
  saveBookmarks();
  updateProgressMarkers();
  updateBookmarkBtn();
  
  buildOutline();
}

function updateBookmarkBtn() {
  if (els.btnBookmark) {
    els.btnBookmark.classList.toggle('on', state.bookmarks.has(state.currentPage));
  }
}

async function loadPdfFromPath(path, name, docId) {
  
  const dup = tabs.findByPath(path);
  if (dup) { hideLoading(); tabs.activate(dup.id); return; }
  showLoading(t('正在打开文档…'));
  try {
    
    
    if (!docId) {
      try {
        const info = await api('/api/pdfinfo?path=' + encodeURIComponent(path));
        if (info && info.docId) docId = info.docId;
      } catch (e) {  }
    }
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
    toast(t('打开失败：') + (err?.message || err));
  }
}

async function handlePassword(path, name, docId) {
  const pwd = prompt(t('该 PDF 已加密，请输入密码：'));
  if (pwd === null) { hideLoading(); return; }
  try {
    const task = pdfjsLib.getDocument({
      url: '/api/file?path=' + encodeURIComponent(path),
      password: pwd,
    });
    await openDocument(task, name, docId || null, path);
  } catch (err) {
    hideLoading();
    toast(t('密码错误或文档无法打开'));
  }
}

async function loadPdfFromFile(file) {
  showLoading(t('正在读取本地文件…'));
  try {
    
    const id = await localDocId(file);
    const dup = tabs.findByDoc(id);
    if (dup) { hideLoading(); tabs.activate(dup.id); return; }
    const buf = await file.arrayBuffer();
    const task = pdfjsLib.getDocument({ data: buf });
    await openDocument(task, file.name.replace(/\.pdf$/i, ''), id, null);
  } catch (err) {
    hideLoading();
    console.error(err);
    toast(t('打开失败：') + (err?.message || err));
  }
}

function openNewPop(anchor) {
  const pop = els.newPop;
  if (!pop) return;
  if (els.newPages) els.newPages.value = '1';
  if (els.newLandscape) els.newLandscape.checked = false;
  if (els.newSize) els.newSize.value = 'a4';

  
  pop.hidden = false;
  placeNewPop(pop, anchor);
}

function placeNewPop(pop, anchor) {
  const pad = 12;
  const w = pop.offsetWidth || 320;
  const h = pop.offsetHeight || 240;
  let left, top;
  if (anchor && anchor.getBoundingClientRect) {
    const r = anchor.getBoundingClientRect();
    left = r.left;
    top = r.bottom + 8;
    if (top + h > window.innerHeight - pad) top = Math.max(pad, r.top - h - 8);
  } else {
    left = (window.innerWidth - w) / 2;
    top = Math.max(pad, (window.innerHeight - h) / 3);
  }
  if (left + w > window.innerWidth - pad) left = window.innerWidth - w - pad;
  pop.style.left = Math.max(pad, left) + 'px';
  pop.style.top = Math.max(pad, top) + 'px';
}

function closeNewPop() {
  if (els.newPop) els.newPop.hidden = true;
}

async function createNewPdf() {
  const size = (els.newSize && els.newSize.value) || 'a4';
  const pages = parseInt((els.newPages && els.newPages.value) || '1', 10) || 1;
  const landscape = !!(els.newLandscape && els.newLandscape.checked);
  const btn = els.newOk;
  if (btn) { btn.disabled = true; btn.textContent = t('正在生成…'); }
  try {
    const r = await fetch('/api/new', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ size, pages, landscape }),
    });
    if (!r.ok) {
      let msg = t('新建失败 ({status})', { status: r.status });
      try { const j = await r.json(); const m2 = errText(j); if (m2) msg = m2; } catch (e) {}
      throw new Error(msg);
    }
    const sid = r.headers.get('X-Stage-Id') || '';
    const raw = r.headers.get('X-Stage-Path') || '';
    let spath = raw;
    if (raw) { try { spath = decodeURIComponent(raw); } catch (e) { spath = raw; } }
    try { await r.arrayBuffer(); } catch (e) {  }
    if (!sid || !spath) throw new Error(t('服务端没有返回新文件位置'));
    closeNewPop();

    
    await loadPdfFromPath(spath, t('未命名'), null);
    
    state.stage = { id: sid, path: spath };
    state.contentPath = spath;
    state.savedPath = spath;
    updateStageBar();
    setEditMode(true);
    toast(t('已新建空白文档：用左侧工具加文字 / 图形，最后点「保存为 PDF」'),
      6000);
  } catch (e) {
    toast(e.message || t('新建失败'), 5000);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t('新建'); }
  }
}

async function localDocId(file) {
  const raw = `${file.name}|${file.size}`;
  const buf = new TextEncoder().encode(raw);
  const digest = await crypto.subtle.digest('SHA-1', buf);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0'))
    .join('').slice(0, 16);
}

async function openDocument(task, name, docId, path) {
  const pdf = await task.promise;
  
  
  const clean = String(name || '').replace(/\.pdf$/i, '') || t('未命名文档');
  const tab = tabs.begin({ name: clean, path, docId });
  try {
    await fillTab(pdf, clean, docId, path);
  } catch (err) {
    tabs.close(tab.id);
    throw err;
  }
}

async function fillTab(pdf, name, docId, path) {
  state.pdf = pdf;
  state.fileName = name || t('未命名文档');
  state.docId = docId;
  state.filePath = path || null;
  
  state.contentPath = path || null;
  state.savedPath = path || null;
  updateStageBar(false);
  
  
  paraCache.clear();
  closeParaPop();
  state.pageCount = pdf.numPages;
  state.rotation = 0;
  state.searchHits = [];
  state.searchHitsByPage = new Map();
  state.hitIndex = -1;
  state.currentPage = 1;

  document.title = state.fileName + t(' - PDF 查看器');

  els.pageTotal.textContent = '/ ' + pdf.numPages;
  els.pageInput.max = pdf.numPages;
  els.pageInput.value = 1;

  
  const saved = readPosition();
  state.rotation = (saved && saved.rotation) || 0;
  state.viewMode = (saved && saved.viewMode) || 'single';
  applyViewMode(state.viewMode, true);
  state.scaleMode = (saved && saved.scaleMode) || 'auto';
  state.scale = await computeScale(state.scaleMode);
  els.zoomSelect.value = state.scaleMode;

  
  ['navGroup', 'navSep', 'zoomGroup', 'zoomSep', 'toolGroup', 'toolGroupSep',
   'annotGroup', 'annotSep', 'editGroup', 'editSep',
   'viewGroup', 'searchSep', 'fileGroup']
    .forEach(id => { els[id] && (els[id].hidden = false); });
  if (els.progressBar) els.progressBar.hidden = false;

  
  if (editState.on) setEditMode(false);
  loadBookmarks();
  await buildPages();
  syncBlankPages();     
  setupObserver();

  
  
  
  buildThumbs();
  buildOutline();

  
  await loadAnnotations(state.docId);

  
  await loadEdits(state.docId);
  if (state.filePath) {
    try {
      const info = await api('/api/pdfinfo?path=' +
        encodeURIComponent(state.filePath));
      editState.pageInfo = info.pages || [];
    } catch (e) {
      console.warn(t('读取页面尺寸失败：'), e.message);
      editState.pageInfo = [];
    }
  } else {
    
    editState.pageInfo = [];
  }
  repaintEdits();
  if (editState.on) refreshPagePanel();

  updateProgressMarkers();
  hideLoading();

  
  if (saved && saved.page > 1 && saved.page <= pdf.numPages) {
    goToPage(saved.page);
    toast(t('已回到第 {page} 页', { page: saved.page }), 1800);
  } else {
    els.viewer.focus();
    toast(t('已打开：{fileName}（共 {numPages} 页）', { fileName: state.fileName, numPages: pdf.numPages }), 2200);
  }
  updateBookmarkBtn();
  updateProgress();
}

function goHome() {
  const hid = tabs.homeId();
  if (hid) tabs.activate(hid);
}

function closeDocument() {
  const cur = tabs.active();
  if (!cur || cur.home) return;
  tabs.close(cur.id);
}

function confirmCloseTab(tab) {
  const cur = tabs.active();
  const st = (cur && tab.id === cur.id)
    ? state
    : ((tab.snap && tab.snap.state) || {});
  if (st.stage && st.stage.id) {
    return confirm(t('这份文档还有未保存的预览，关闭标签页会丢弃它。仍要关闭？'));
  }
  return true;
}

function cycleTab(dir) {
  const list = tabs.list();
  if (!list.length) return;
  const cur = tabs.active();
  const i = cur ? list.findIndex(x => x.id === cur.id) : -1;
  const n = (i + dir + list.length) % list.length;
  tabs.activate(list[n].id);
}

function beforeCloseTab(tab, wasActive) {
  if (wasActive) {
    
    doSave();
    saveEdits();
    stopAutoScroll();
  }
  const st = wasActive ? state : ((tab.snap && tab.snap.state) || {});
  
  const sid = st.stage && st.stage.id;
  if (sid) {
    fetch('/api/stage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: sid }),
    }).catch(() => {});
  }
  if (st.observer) { try { st.observer.disconnect(); } catch (e) {  } }
  if (st.pdf) { try { st.pdf.destroy(); } catch (e) {  } }
}

function refreshChrome() {
  const has = !!state.pdf;
  const total = state.pageCount + ((editState.pages && editState.pages.insert) || []).length;

  ['navGroup', 'navSep', 'zoomGroup', 'zoomSep', 'toolGroup', 'toolGroupSep',
   'annotGroup', 'annotSep', 'editGroup', 'editSep',
   'viewGroup', 'searchSep', 'fileGroup']
    .forEach(id => { if (els[id]) els[id].hidden = !has; });
  if (els.progressBar) els.progressBar.hidden = !has;

  if (els.pageInput) {
    els.pageInput.max = total || state.pageCount;
    els.pageInput.value = state.currentPage;
  }
  if (els.pageTotal) els.pageTotal.textContent = has ? '/ ' + total : '';
  if (els.zoomSelect) els.zoomSelect.value = state.scaleMode;
  if (els.searchNav) els.searchNav.hidden = !state.searchHits.length;
  if (els.searchCount) {
    els.searchCount.textContent = state.searchHits.length
      ? (state.hitIndex >= 0 ? (state.hitIndex + 1) + '/' + state.searchHits.length
                             : String(state.searchHits.length))
      : '';
  }

  els.viewer.classList.toggle('annot-mode', state.annotMode);
  if (els.btnAnnotMode) els.btnAnnotMode.classList.toggle('on', state.annotMode);

  
  
  document.body.classList.toggle('dark', state.invert);
  if (els.btnDark) els.btnDark.classList.toggle('on', state.invert);
  document.body.classList.toggle('view-double', state.viewMode === 'double');
  els.pages.classList.toggle('double', state.viewMode === 'double');
  if (els.btnViewSingle) els.btnViewSingle.classList.toggle('on', state.viewMode !== 'double');
  if (els.btnViewDouble) els.btnViewDouble.classList.toggle('on', state.viewMode === 'double');

  updateBookmarkBtn();
  updateStageBar(false);
  updateProgress();
  updateProgressMarkers();

  if (has) {
    buildThumbs();
    buildOutline();
    renderAnnotList();
  } else {
    
    
    
    if (els.stageBar) els.stageBar.hidden = true;
    els.thumbList.innerHTML = '';
    els.outlineList.innerHTML = '';
    if (els.annotList) els.annotList.innerHTML = '';
    if (els.annotCount) els.annotCount.textContent = '';
    if (els.annotActions) els.annotActions.hidden = true;
    if (els.pageTotal) els.pageTotal.textContent = '';
    if (els.pageInput) els.pageInput.value = '1';
    
    if (!welcomeReady) renderWelcome();
  }

  document.title = has ? state.fileName + t(' - PDF 查看器') : t('PDF 查看器');
}

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
    wrap.dataset.ph = t('第 {i} 页', { i });
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

function syncBlankPages() {
  if (!els.pages) return;
  const inserts = (editState.pages && editState.pages.insert) || [];
  const need = inserts.length;

  
  const blanks = state.pages.filter(r => r.blank);

  
  for (let i = blanks.length - 1; i >= need; i--) {
    const rec = blanks[i];
    rec.wrap.remove();
    const idx = state.pages.indexOf(rec);
    if (idx >= 0) state.pages.splice(idx, 1);
  }

  
  const first = els.pages.querySelector(':scope > .page:not(.blank)');
  const w = (first && first.offsetWidth) || 595;
  const h = (first && first.offsetHeight) || 842;

  
  
  
  
  for (let i = blanks.length; i < need; i++) {
    const num = state.pageCount + i + 1;
    const wrap = document.createElement('div');
    wrap.className = 'page blank';
    wrap.dataset.page = num;
    wrap.dataset.blank = '1';
    wrap.dataset.ph = t('空白页（导出时写入）');
    wrap.style.width = w + 'px';
    wrap.style.height = h + 'px';
    const canvas = document.createElement('canvas');
    const textLayer = document.createElement('div');
    textLayer.className = 'text-layer';
    wrap.append(canvas, textLayer);
    els.pages.appendChild(wrap);
    state.pages.push({
      num, wrap, canvas, textLayer,
      rendered: true, rendering: false,
      viewport: { width: w, height: h }, textContent: null, task: null,
      blank: true,
    });
  }

  
  const total = state.pageCount + need;
  if (els.pageInput) els.pageInput.max = total;
  if (els.pageTotal) els.pageTotal.textContent = '/ ' + total;

  
  if (editState.on) repaintEdits();
}

document.addEventListener('pdf:pages-changed', syncBlankPages);

function setupObserver() {
  if (state.observer) state.observer.disconnect();

  state.observer = new IntersectionObserver((entries) => {
    
    
    
    let mine = false;
    for (const entry of entries) {
      if (!els.pages || !els.pages.contains(entry.target)) continue;
      mine = true;
      const num = parseInt(entry.target.dataset.page, 10);
      const rec = state.pages[num - 1];
      if (!rec) continue;
      if (entry.isIntersecting) {
        renderPage(rec);
      }
    }
    if (mine) updateCurrentPage();
  }, {
    root: els.viewer,
    rootMargin: `${BUFFER_PAGES * 100}% 0px ${BUFFER_PAGES * 100}% 0px`,
    threshold: 0,
  });

  for (const rec of state.pages) state.observer.observe(rec.wrap);
}

async function renderPage(rec) {
  if (rec.rendered || rec.rendering) return;

  
  if (state.renderQueue.size >= 3) return;
  state.renderQueue.add(rec.num);
  rec.rendering = true;

  
  
  
  
  const pdf = state.pdf;
  const scale = state.scale;
  const rotation = state.rotation;
  const queue = state.renderQueue;
  const stale = () => state.pdf !== pdf;
  const abort = () => { rec.rendering = false; queue.delete(rec.num); };

  try {
    const page = await pdf.getPage(rec.num);
    if (stale()) { abort(); return; }
    const viewport = page.getViewport({ scale, rotation });

    
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
    if (stale()) { abort(); return; }

    
    const textContent = await page.getTextContent();
    if (stale()) { abort(); return; }
    rec.textContent = textContent;
    buildTextLayer(rec, textContent, viewport);

    
    loadPagePdfAnnots(rec);

    rec.wrap.classList.remove('pending');
    rec.wrap.removeAttribute('data-ph');
    rec.rendered = true;
    rec.rendering = false;
    queue.delete(rec.num);

    
    const hitOnPage = state.searchHitsByPage.get(rec.num - 1);
    if (hitOnPage) {
      for (const hit of hitOnPage) paintHitsOnPage(rec, hit);
      markCurrentHit();
    }

    
    paintPageAnnotations(rec);
    
    paintPageEdits(rec);
    
    
    if (objBarId) {
      const sel = editState.objects.find(o => o.id === objBarId);
      if (sel && sel.page === rec.num) showObjBar(rec, sel);
    }
  } catch (err) {
    rec.rendering = false;
    queue.delete(rec.num);
    
    if (err?.name !== 'RenderingCancelledException' && !String(err).includes('cancel')) {
      console.warn(t('第 {num} 页渲染失败：', { num: rec.num }), err);
      rec.wrap.classList.remove('pending');
      rec.wrap.setAttribute('data-ph', t('第 {num} 页渲染失败', { num: rec.num }));
    }
  }
}

function buildTextLayer(rec, textContent, viewport) {
  const layer = rec.textLayer;
  layer.innerHTML = '';

  
  textContent.items.forEach((it, i) => { it.__idx = i; });

  
  const measureCtx = (rec._measureCanvas ||= document.createElement('canvas')).getContext('2d');
  const frag = document.createDocumentFragment();

  for (const item of textContent.items) {
    if (!item.str) continue;

    const tx = pdfjsLib.Util.transform(viewport.transform, item.transform);
    const fontHeight = Math.hypot(tx[2], tx[3]);
    const angle = Math.atan2(tx[1], tx[0]);

    const span = document.createElement('span');
    span.textContent = item.str;
    
    
    span.dataset.itemIndex = String(item.__idx);
    span.style.left = tx[4] + 'px';
    span.style.top = (tx[5] - fontHeight) + 'px';
    span.style.fontSize = fontHeight + 'px';
    span.style.fontFamily = 'sans-serif';

    
    const targetW = item.width * viewport.scale;

    
    
    
    
    
    
    
    const isBlank = !item.str.trim();
    if (fontHeight > 0 && targetW > 0 && !isBlank) {
      measureCtx.font = `${fontHeight}px sans-serif`;
      const naturalW = measureCtx.measureText(item.str).width;
      if (naturalW > 0.01) {
        const ratio = targetW / naturalW;
        
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

function updateProgress() {
  if (!els.progressFill || !state.pageCount) return;
  const v = els.viewer;
  const max = v.scrollHeight - v.clientHeight;
  const ratio = max > 0 ? Math.min(1, v.scrollTop / max) : 0;
  els.progressFill.style.width = (ratio * 100) + '%';
}

async function applyScale(mode, anchor) {
  if (!state.pdf) return;
  const oldScale = state.scale;
  state.scaleMode = mode;
  state.scale = await computeScale(mode);

  
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
  
  for (const rec of state.pages) {
    if (rec.task) { try { rec.task.cancel(); } catch (e) {  } rec.task = null; }
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
      rec.wrap.dataset.ph = t('第 {num} 页', { num: rec.num });
    }
  }

  
  if (anchor) {
    requestAnimationFrame(() => {
      const rec = state.pages[anchor.page - 1];
      if (!rec) return;
      const target = rec.wrap.offsetTop +
        rec.wrap.offsetHeight * Math.min(Math.max(anchor.ratio, 0), 1) -
        els.viewer.clientHeight / 2;
      els.viewer.scrollTop = Math.max(0, target);
      
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
  
  buildThumbs();
  toast(t('旋转 {rotation}°', { rotation: state.rotation }), 1200);
}

function toggleInvert() {
  state.invert = !state.invert;
  document.body.classList.toggle('dark', state.invert);
  els.btnDark.classList.toggle('on', state.invert);
}

async function applyViewMode(mode, silent = false) {
  state.viewMode = mode;
  document.body.classList.toggle('view-double', mode === 'double');
  if (els.btnViewSingle) els.btnViewSingle.classList.toggle('on', mode === 'single');
  if (els.btnViewDouble) els.btnViewDouble.classList.toggle('on', mode === 'double');
  els.pages.classList.toggle('double', mode === 'double');

  if (state.pdf && !silent) {
    
    state.scale = await computeScale(state.scaleMode);
    await buildPages();
    setupObserver();
    goToPage(state.currentPage);
    repaintAnnotations();
    savePosition();
  }
}

let autoScrollRAF = null;
let autoScrollSpeed = 0.55;   

function startAutoScroll() {
  if (autoScrollRAF) { stopAutoScroll(); return; }
  if (!state.pdf) return;
  els.btnScrollPlay.classList.add('scrolling');
  toast(t('自动滚动已开启，按空格暂停'), 1600);

  const step = () => {
    if (!autoScrollRAF) return;
    const v = els.viewer;
    if (v.scrollTop + v.clientHeight >= v.scrollHeight - 2) {
      stopAutoScroll();
      toast(t('已滚动到文档末尾'));
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

let thumbObserver = null;

async function buildThumbs() {
  const box = els.thumbList;
  box.innerHTML = '';
  
  
  
  thumbCache.clear();
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

  
  thumbObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      const n = parseInt(e.target.dataset.page, 10);
      
      
      
      
      renderThumb(n)
        .then(ok => { if (ok) thumbObserver.unobserve(e.target); })
        .catch(() => {  });
    }
  }, { root: els.sideBody, rootMargin: '200px 0px' });

  for (const b of box.children) thumbObserver.observe(b);
}

const thumbCache = new Map();

async function renderThumb(num) {
  
  if (!state.pdf || thumbCache.has(num)) return true;
  const btn = els.thumbList.querySelector(`.thumb[data-page="${num}"]`);
  if (!btn) return false;
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
    if (!btn.isConnected) return true;
    const ph = holder.querySelector('.thumb-ph');
    if (ph) ph.remove();
    holder.appendChild(canvas);
    return true;
  } catch (err) {
    
    if (err && err.name !== 'RenderingCancelledException') {
      console.warn(t('缩略图渲染失败（下次可见时重试）'), num, err);
    }
    return false;
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

async function buildOutline() {
  const box = els.outlineList;
  box.innerHTML = '';
  state.outlinMap = new Map();
  if (!state.pdf) return;

  
  
  
  renderMyBookmarks(box);

  let outline = null;
  try { outline = await state.pdf.getOutline(); } catch (e) { outline = null; }

  if (!outline || !outline.length) {
    
    if (!state.bookmarks.size) {
      const div = document.createElement('div');
      div.className = 'outline-empty';
      div.innerHTML = t('此文档没有目录<br>') +
        t('<span style="font-size:12px">有些 PDF 未嵌入书签结构</span>');
      box.appendChild(div);
    }
    return;
  }

  const frag = document.createDocumentFragment();
  const rendered = await Promise.all(outline.map(item => buildOutlineNode(item, 0)));
  rendered.forEach(n => n && frag.appendChild(n));
  box.appendChild(frag);
}

function renderMyBookmarks(box) {
  const pages = [...state.bookmarks].sort((a, b) => a - b);
  if (!pages.length) return;
  const head = document.createElement('div');
  head.className = 'outline-heading';
  head.textContent = t('我的书签');
  box.appendChild(head);
  for (const p of pages) {
    const btn = document.createElement('button');
    btn.className = 'outline-item';
    btn.innerHTML = t('<span>第 {p} 页</span>', { p }) +
      `<span class="outline-page">${p}</span>`;
    btn.style.paddingLeft = '8px';
    btn.addEventListener('click', () => {
      goToPage(p, true);
      els.outlineList.querySelectorAll('.outline-item.on')
        .forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
    });
    box.appendChild(btn);
  }
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
  els.searchCount.textContent = t('搜索中…');
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

  els.searchCount.textContent = total ? t('{total} 处', { total }) : t('无结果');
  els.searchInput.classList.toggle('no-result', total === 0);
  els.searchNav.hidden = total === 0;

  if (total > 0) {
    applySearchHighlight();
    gotoHit(0);
  }
}

function restoreSpan(span) {
  if (span.dataset.orig !== undefined) {
    span.textContent = span.dataset.orig;
    delete span.dataset.orig;
  }
}

function clearSearchHighlights() {
  for (const rec of state.pages) {
    rec.textLayer.querySelectorAll('span[data-orig]').forEach(restoreSpan);
    
    rec.textLayer.querySelectorAll('mark.hl').forEach(m => {
      const sp = m.closest('span');
      if (sp) restoreSpan(sp);
    });
  }
  state.searchHits = [];
  state.searchHitsByPage = new Map();
  state.hitIndex = -1;
}

function findSpanForItem(rec, itemIndex) {
  if (!rec || !rec.textLayer) return null;
  return rec.textLayer.querySelector(`span[data-item-index="${itemIndex}"]`);
}

function paintHitsOnPage(rec, hit) {
  if (!rec || !rec.rendered) return;

  
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

function applySearchHighlight() {
  for (const rec of state.pages) {
    rec.textLayer.querySelectorAll('span[data-orig]').forEach(restoreSpan);
  }
  for (const hit of state.searchHits) {
    paintHitsOnPage(state.pages[hit.pageIndex], hit);
  }
  markCurrentHit();
}

function markCurrentHit() {
  document.querySelectorAll('.text-layer mark.cur').forEach(m => m.classList.remove('cur'));
  if (state.hitIndex < 0 || !state.searchHits.length) return;
  const hit = state.searchHits[state.hitIndex];
  const rec = state.pages[hit.pageIndex];
  if (!rec || !rec.rendered) return;
  const mark = rec.textLayer.querySelector(`mark.hl[data-seq="${hit.seq}"]`);
  if (mark) mark.classList.add('cur');
}

function gotoHit(index) {
  const n = state.searchHits.length;
  if (!n) return;
  state.hitIndex = ((index % n) + n) % n;
  const hit = state.searchHits[state.hitIndex];

  els.searchCount.textContent = `${state.hitIndex + 1} / ${n}`;

  const rec = state.pages[hit.pageIndex];
  if (!rec) return;

  if (!rec.rendered) renderPage(rec);

  
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
      
      goToPage(hit.pageNum, true);
      markCurrentHit();
    }
  };
  locate();
}

let pendingSel = null;   
let popAnnotId = null;   
let currentAnnotColor = 'yellow';
let currentAnnotColorHex = '#fbbf24';
let menuBusy = false;    

let selMenuTimer = null;

function hideSelMenu() {
  if (selMenuTimer) { clearTimeout(selMenuTimer); selMenuTimer = null; }
  if (els.selMenu) els.selMenu.hidden = true;
}

function showSelMenu(rect) {
  const menu = els.selMenu;
  if (!menu) return;
  menu.hidden = false;

  
  
  
  const box = menu.getBoundingClientRect();
  const mw = box.width || menu.offsetWidth || 300;
  const mh = box.height || menu.offsetHeight || 34;

  const vr = els.viewer.getBoundingClientRect();

  
  
  
  const sx = els.viewer.scrollLeft;
  const sy = els.viewer.scrollTop;

  
  const selLeft = rect.left - vr.left + sx;
  const selTop = rect.top - vr.top + sy;
  const selBottom = rect.bottom - vr.top + sy;

  
  const viewTop = sy + 6;
  const viewBottom = sy + vr.height - 6;

  const GAP = 8;      
  const EDGE = 6;     

  
  const minLeft = sx + EDGE;
  const maxLeft = sx + Math.max(EDGE, vr.width - mw - EDGE);
  let left = selLeft + rect.width / 2 - mw / 2;
  left = Math.min(Math.max(left, minLeft), maxLeft);

  
  
  const above = selTop - mh - GAP;
  const below = selBottom + GAP;

  let top;
  if (above >= viewTop) {
    top = above;                                   
  } else if (below + mh <= viewBottom) {
    top = below;                                   
  } else {
    top = Math.min(Math.max(above, viewTop), viewBottom - mh);  
  }
  top = Math.min(Math.max(top, viewTop), viewBottom - mh);

  menu.style.left = left + 'px';
  menu.style.top = top + 'px';

  
  if (selMenuTimer) clearTimeout(selMenuTimer);
  selMenuTimer = setTimeout(() => {
    if (menuBusy) return;
    hideSelMenu();
    pendingSel = null;
  }, 6000);
}

function onSelectionChange() {
  
  if (menuBusy) return;

  
  if (editState.on) return;

  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) {
    
    if (!pendingSel) hideSelMenu();
    return;
  }
  const text = sel.toString().trim();
  if (!text) { hideSelMenu(); return; }

  const range = sel.getRangeAt(0);
  if (!els.viewer.contains(range.commonAncestorContainer)) { hideSelMenu(); return; }

  
  const anchors = selectionToAnchors();
  if (!anchors) { hideSelMenu(); return; }
  pendingSel = anchors;

  
  
  let anchorRect = null;
  try {
    const rects = range.getClientRects();
    if (rects && rects.length) {
      const first = rects[0];
      
      const whole = range.getBoundingClientRect();
      anchorRect = {
        left: whole.left, width: whole.width,
        top: first.top, bottom: first.bottom,
      };
    }
  } catch (e) {  }
  showSelMenu(anchorRect || range.getBoundingClientRect());
}

function finishMenuAction() {
  hideSelMenu();
  pendingSel = null;
  try { window.getSelection()?.removeAllRanges(); } catch (e) {  }
}

let dragState = null;      
let moveState = null;      
let resizeState = null;    
let textPopInfo = null;    
let objBarId = null;       

const MOVE_THRESHOLD = 3;

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

function showObjBar(rec, obj) {
  const bar = els.objBar;
  if (!bar) return;

  if (!obj) { hideObjBar(); return; }

  const names = { whiteout: t('涂白块'), text: t('文字'), rect: t('矩形'),
                  ellipse: t('椭圆'), line: t('直线'), replace: t('替换文字'),
                  image: t('图片') };
  if (els.objBarName) els.objBarName.textContent = names[obj.kind] || t('对象');
  objBarId = obj.id;

  
  
  
  
  
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

function onEditDoubleClick(e) {
  if (!editState.on || editState.tool !== 'select') return;
  if (!e.target.closest('.page')) return;
  const rec = pageAt(e.clientX, e.clientY);
  if (!rec) return;
  const obj = hitTest(rec, e.clientX, e.clientY);
  if (!obj) return;
  if (obj.kind === 'text') {
    e.preventDefault();
    openTextPop(rec, e.clientX, e.clientY, obj);
  } else if (obj.kind === 'replace') {
    
    e.preventDefault();
    openReplacePop(obj, e.clientX, e.clientY);
  }
}

function setAnnotMode(on, silent = false) {
  const next = !!on;
  if (state.annotMode === next) return;
  state.annotMode = next;
  if (els.btnAnnotMode) els.btnAnnotMode.classList.toggle('on', next);
  if (els.viewer) els.viewer.classList.toggle('annot-mode', next);

  if (next && editState.on) setEditMode(false);   

  if (!silent) {
    toast(next ? t('标注模式：拖拽可框选区域（已退出编辑）') : t('已退出标注模式'), 1600);
  }
}

function onEditMouseDown(e) {
  if (!editState.on || !editState.tool) return;
  if (e.button !== 0) return;

  
  
  
  const handle = e.target.closest('.ed-handle');
  if (handle) {
    if (editState.tool !== 'select') return;
    const obj = editState.objects.find(o => o.id === handle.dataset.id);
    const hrec = pageAt(e.clientX, e.clientY);
    if (!obj || !hrec) return;
    e.preventDefault();
    const hr = hrec.wrap.getBoundingClientRect();
    const hps = pageSize(hrec.num);
    resizeState = {
      id: obj.id,
      rec: hrec,
      corner: handle.dataset.corner,
      startX: e.clientX,
      startY: e.clientY,
      orig: { x: obj.x, y: obj.y, w: obj.w, h: obj.h },
      rw: hr.width,
      rh: hr.height,
      pw: hps.width,
      ph: hps.height,
    };
    hideObjBar();
    return;
  }

  
  
  
  
  
  
  
  
  
  if (!e.target.closest('.page')) return;

  
  closeTextPop();

  const rec = pageAt(e.clientX, e.clientY);
  if (!rec) return;

  const tool = editState.tool;

  
  if (tool === 'para') {
    e.preventDefault();
    openParaPop(rec, e.clientX, e.clientY);
    return;
  }

  
  
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

  
  hideObjBar();

  
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

function dragMoveObject(e) {
  const st = moveState;
  const dxs = e.clientX - st.startX;
  const dys = e.clientY - st.startY;

  if (!st.moved) {
    if (Math.abs(dxs) < MOVE_THRESHOLD && Math.abs(dys) < MOVE_THRESHOLD) {
      return;                    
    }
    st.moved = true;
    hideObjBar();                
  }

  const d = dragDeltaToPdf(dxs, dys, st.rw, st.rh, st.pw, st.ph);
  updateObject(st.id, movedPosition(st.orig, d));
}

function dragResizeObject(e) {
  const st = resizeState;
  const dxs = e.clientX - st.startX;
  const dys = e.clientY - st.startY;
  const d = dragDeltaToPdf(dxs, dys, st.rw, st.rh, st.pw, st.ph);
  
  const lock = editState.keepRatio || e.shiftKey;
  updateObject(st.id, resizedRect(st.orig, st.corner, d, lock));
}

function onEditMouseMove(e) {
  if (resizeState) { dragResizeObject(e); return; }
  if (moveState) { dragMoveObject(e); return; }
  if (!dragState) return;
  const { rec, page, start, tool, color } = dragState;

  const opts = { color, lineWidth: tool === 'line' ? 1.5 : 1, fill: false };

  const temp = tool === 'line'
    ? makeLineObject(rec, page, start, { x: e.clientX, y: e.clientY }, opts)
    : makeRectObject(rec, page, start, { x: e.clientX, y: e.clientY },
                     tool, opts);

  
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

function onEditMouseUp(e) {
  
  if (resizeState) {
    const st = resizeState;
    resizeState = null;
    const obj = editState.objects.find(o => o.id === st.id);
    if (obj) showObjBar(st.rec, obj);   
    toast(t('已缩放'), 900);
    return;
  }
  
  if (moveState) {
    const st = moveState;
    moveState = null;
    if (st.moved) {
      const obj = editState.objects.find(o => o.id === st.id);
      if (obj) showObjBar(st.rec, obj);   
      toast(t('已移动'), 900);
    }
    return;
  }
  if (!dragState) return;
  const { rec, page, start, tool, color } = dragState;
  dragState = null;
  document.body.classList.remove('dragging-edit');

  const dx = Math.abs(e.clientX - start.x);
  const dy = Math.abs(e.clientY - start.y);

  
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
  const created = addObject(obj);
  
  
  setTool('select');
  editState.selectedId = created.id;
  showObjBar(rec, created);
  repaintEdits();
  
  if (tool === 'replace') {
    toast(t('已框选替换区域，请输入要替换成的新文字'), 2600);
    openReplacePop(created, e.clientX, e.clientY);
    return;
  }
  const label = { whiteout: t('涂白'), rect: t('矩形'), ellipse: t('椭圆'),
                  line: t('直线'), redact: t('涂黑区域') }[tool] || t('对象');
  
  
  if (tool === 'redact') {
    toast(t('已标记涂黑区域，点「涂黑并另存为…」才会真正删除文字'), 3200);
  } else {
    toast(t('已添加{label}', { label }), 1000);
  }
}

let paraPopInfo = null;             
const paraCache = new Map();        

async function fetchParagraphs(page) {
  
  
  
  
  const src = state.contentPath || state.filePath;
  const key = `${src}|${page}`;
  if (paraCache.has(key)) return paraCache.get(key);
  const q = new URLSearchParams({ path: src, page: String(page) });
  const r = await fetch('/api/paragraphs?' + q.toString());
  if (!r.ok) {
    let msg = t('取段落失败 ({status})', { status: r.status });
    try { const j = await r.json(); const m2 = errText(j); if (m2) msg = m2; } catch (e) {  }
    throw new Error(msg);
  }
  const data = await r.json();
  const list = data.paragraphs || [];
  paraCache.set(key, list);
  return list;
}

function itemRectPdf(item) {
  const t = item.transform || [1, 0, 0, 1, 0, 0];
  const h = Math.abs(Math.hypot(t[2], t[3])) || 10;
  const w = Math.abs(item.width || 0);
  return [t[4], t[5], t[4] + w, t[5] + h];
}

function joinItemsByLines(picked) {
  if (!picked.length) return '';
  picked.sort((a, b) => (b.y - a.y) || (a.x - b.x));
  const lines = [];
  for (const it of picked) {
    const cur = lines[lines.length - 1];
    if (cur && Math.abs(it.y - cur.y) <= 3) {
      cur.items.push(it);
    } else {
      lines.push({ y: it.y, items: [it] });
    }
  }
  return lines
    .map(l => l.items.sort((a, b) => a.x - b.x).map(i => i.str).join(''))
    .join('\n');
}

function pageTextFromDom(rec) {
  const tc = rec && rec.textContent;
  if (!tc || !tc.items) return '';
  const picked = [];
  for (const it of tc.items) {
    if (!it.str) continue;
    const r = itemRectPdf(it);
    picked.push({ y: r[1], x: r[0], str: it.str });
  }
  return joinItemsByLines(picked);
}

function paragraphTextFromDom(rec, box) {
  const tc = rec && rec.textContent;
  if (!tc || !tc.items) return '';
  const picked = [];
  for (const it of tc.items) {
    if (!it.str) continue;
    const r = itemRectPdf(it);
    const cx = (r[0] + r[2]) / 2;
    const cy = (r[1] + r[3]) / 2;
    if (cx >= box.x0 - 2 && cx <= box.x1 + 2 &&
        cy >= box.y0 - 2 && cy <= box.y1 + 2) {
      picked.push({ y: r[1], x: r[0], str: it.str });
    }
  }
  if (!picked.length) return '';
  
  return joinItemsByLines(picked);
}

function flashParaRange(rec, p, label) {
  const { width: pw, height: ph } = pageSize(rec.num);
  if (!pw || !ph || !rec.wrap) return;
  const topPct = (1 - p.y1 / ph) * 100;
  const el = document.createElement('div');
  el.className = 'para-flash';
  el.style.left = (p.x0 / pw * 100) + '%';
  el.style.width = (Math.max(0, p.x1 - p.x0) / pw * 100) + '%';
  el.style.top = topPct + '%';
  el.style.height = (Math.max(0, p.y1 - p.y0) / ph * 100) + '%';
  if (label) {
    const tag = document.createElement('span');
    tag.className = 'para-flash-tag' + (topPct < 6 ? ' inside' : '');
    tag.textContent = label;
    el.appendChild(tag);
  }
  rec.wrap.appendChild(el);
  setTimeout(() => el.remove(), 2300);
}

function flashBoxes(pageNum, boxes) {
  const rec = state.pages[pageNum - 1];
  if (!rec || !boxes || !boxes.length) return;
  boxes.slice(0, 40).forEach(b => flashParaRange(rec, b, null));
}

async function openParaPop(rec, clientX, clientY) {
  if (!state.filePath) { toast(t('请先打开一个文档'), 1600); return; }

  const r = rec.wrap.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const nx = (clientX - r.left) / r.width;
  const ny = (clientY - r.top) / r.height;
  const { width: pw, height: ph } = pageSize(rec.num);
  const px = nx * pw;
  const py = ph * (1 - ny);          

  let list;
  try {
    list = await fetchParagraphs(rec.num);
  } catch (e) {
    toast(e.message || t('取段落失败'), 3200);
    return;
  }

  
  const hit = list.find(p => px >= p.x0 - 2 && px <= p.x1 + 2 &&
                             py >= p.y0 - 2 && py <= p.y1 + 2);
  if (!hit) {
    toast(t('这里没有可编辑的段落（点正文里的任意一行试试）'), 2800);
    return;
  }

  
  
  
  
  
  
  if (hit.table_like) {
    const cell = (hit.cells || []).find(c =>
      px >= c.x0 - 2 && px <= c.x1 + 2 && py >= c.y0 - 2 && py <= c.y1 + 2);
    if (!cell) {
      flashParaRange(rec, hit, t('表格区域（{cols} 列）· 请点具体的格子', { cols: hit.cols }));
      return;
    }
    flashParaRange(rec, cell,
                   t('第 {v} 行 第 {v2} 列', { v: cell.row + 1, v2: cell.col + 1 }));
    paraPopInfo = {
      page: rec.num,
      para: hit.index,
      cell: [cell.row, cell.col],
      
      gid: (cell.gid != null ? cell.gid : cell.row),
      
      orig: cell.text || null,
      pageText: null,
    };
    if (els.paraDropRow) {
      els.paraDropRow.hidden = false;
      els.paraDropRow.textContent = t('删掉第 {v} 行', { v: cell.row + 1 });
    }
    if (els.paraInsertBelow) els.paraInsertBelow.hidden = false;
    if (els.paraInsertAbove) {
      els.paraInsertAbove.hidden = false;
      
      els.paraInsertAbove.textContent = cell.row === 0 ? t('加表头') : t('上插一行');
    }
    if (els.paraPage) els.paraPage.textContent = t('第 {num} 页', { num: rec.num });
    if (els.paraTip) {
      els.paraTip.textContent =
        t('表格 第 {v} 行 · 第 {v2} 列 · 字号 {size}', { v: cell.row + 1, v2: cell.col + 1, size: hit.size });
    }
    if (els.paraText) els.paraText.value = cell.text || '';
    updateParaCount();
    const cbox = els.paraEdit;
    if (!cbox) return;
    cbox.hidden = false;
    cbox.scrollIntoView({ block: 'nearest' });
    els.paraText && els.paraText.focus();
    return;
  }

  
  
  let paraText = hit.text || '';
  let usedDom = false;
  if (!paraText && rec.textContent) {
    paraText = paragraphTextFromDom(rec, hit);
    usedDom = !!paraText;
  }
  if (!paraText) {
    toast(t('这篇文档的字体没有 ToUnicode，且没能解出这一段文字，暂时无法编辑'), 4200);
    return;
  }

  paraPopInfo = {
    page: rec.num,
    para: hit.index,
    
    gid: null,
    
    orig: usedDom ? paraText : null,
    
    
    pageText: usedDom ? pageTextFromDom(rec) : null,
  };
  if (els.paraDropRow) els.paraDropRow.hidden = true;
  if (els.paraInsertBelow) els.paraInsertBelow.hidden = true;
  if (els.paraInsertAbove) els.paraInsertAbove.hidden = true;
  if (els.paraPage) els.paraPage.textContent = t('第 {num} 页', { num: rec.num });
  if (els.paraTip) {
    els.paraTip.textContent =
      t('第 {v} 段 · {lines} 行 · 字号 {size}', { v: hit.index + 1, lines: hit.lines, size: hit.size }) +
      (usedDom ? t('　原文由页面文字补全') : '');
  }
  if (els.paraText) els.paraText.value = paraText;
  updateParaCount();

  const box = els.paraEdit;
  if (!box) return;
  box.hidden = false;

  
  
  
  box.scrollIntoView({ block: 'nearest' });
  els.paraText && els.paraText.focus();
}

function updateParaCount() {
  if (!els.paraCount) return;
  const t = (els.paraText && els.paraText.value) || '';
  
  const n = [...t.replace(/\s/g, '')].length;
  els.paraCount.textContent = t('{n} 字', { n });
}

async function reloadContentFrom(path) {
  const keepPage = state.currentPage;
  const keepScroll = els.viewer.scrollTop;

  const task = pdfjsLib.getDocument({
    url: '/api/file?path=' + encodeURIComponent(path),
    rangeChunkSize: 262144,
  });
  const pdf = await task.promise;

  if (state.observer) { state.observer.disconnect(); state.observer = null; }
  state.pdf = pdf;
  state.pageCount = pdf.numPages;
  els.pageTotal.textContent = '/ ' + pdf.numPages;
  els.pageInput.max = pdf.numPages;
  
  
  paraCache.clear();
  state.searchHits = [];
  state.searchHitsByPage = new Map();
  state.hitIndex = -1;

  await buildPages();
  syncBlankPages();
  setupObserver();
  repaintEdits();
  if (editState.on) refreshPagePanel();
  buildThumbs();
  buildOutline();

  await goToPage(Math.min(keepPage, pdf.numPages));
  els.viewer.scrollTop = keepScroll;

  
  
  if (state.contentPath) {
    try {
      const info = await api('/api/pdfinfo?path=' +
        encodeURIComponent(state.contentPath));
      editState.pageInfo = info.pages || [];
    } catch (e) {
      console.warn(t('刷新页面尺寸失败：'), e.message);
    }
  }
  updateProgressMarkers();
  return pdf;
}

async function applyStagePreview(res) {
  showLoading(t('正在刷新预览…'));
  try {
    await reloadContentFrom(res.stagePath);
    updateStageBar();
  } catch (e) {
    toast(t('刷新预览失败：') + ((e && e.message) || e), 6000);
    hideLoading();
    return;
  }
  hideLoading();

  let msg = t('已更新预览');
  if (res.linesBefore != null && res.linesAfter != null &&
      res.linesBefore !== res.linesAfter) {
    msg += t('（{linesBefore} 行 → {linesAfter} 行）', { linesBefore: res.linesBefore, linesAfter: res.linesAfter });
  }
  if (res.pages > 1) msg += t('，跨 {pages} 页续排', { pages: res.pages });
  if (res.newPages > 0) msg += t('，新增 {newPages} 页', { newPages: res.newPages });

  
  
  const staleRedact = redactCount();
  if (staleRedact > 0) {
    msg += t('　⚠ 版面已变，之前画的 {staleRedact} 处涂黑框请重画', { staleRedact });
  }

  if (res.fallback) {
    
    msg += t('　⚠ 这些字用了替代字体，字形会与原文不同：{fallback}', { fallback: res.fallback });
    toast(msg, 9000);
  } else {
    toast(msg + t('。满意就点顶部「保存为 PDF」'), 3600);
  }
}

async function undoStagePreview() {
  if (!state.stage || !state.stage.id) return false;
  let j = {};
  let r = null;
  try {
    r = await fetch('/api/stage/undo', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: state.stage.id }),
    });
    j = await r.json().catch(() => ({}));
  } catch (e) {
    toast(t('撤销失败：') + ((e && e.message) || e), 4000);
    return false;
  }
  if (!r.ok) {
    
    if (r.status === 400 && !errText(j)) return false;
    if (r.status === 400) { toast(errText(j), 2200); return true; }
    toast(errText(j) || t('撤销失败'), 4000);
    return false;
  }
  showLoading(t('正在退回上一版…'));
  try {
    state.stage.path = j.stagePath;
    await reloadContentFrom(j.stagePath);
    updateStageBar();
    toast(t('已退回上一版预览') + (j.remaining > 0 ? t('（还能退 {remaining} 步）', { remaining: j.remaining }) : ''), 2200);
  } catch (e) {
    toast(t('刷新预览失败：') + ((e && e.message) || e), 5000);
  } finally {
    hideLoading();
  }
  return true;
}

setStageUndoHandler(undoStagePreview);

function updateStageBar(saved = false) {
  const bar = els.stageBar;
  if (!bar) return;
  const has = !!state.stage.id;
  
  
  setStagePresent(has);
  bar.hidden = !has && !saved;
  bar.classList.toggle('is-saved', !!saved);
  if (els.stageText) {
    els.stageText.textContent = saved ? t('已保存') : t('预览中 · 改动尚未保存');
  }
  if (els.stageSave) els.stageSave.hidden = !!saved;
  if (els.stageDrop) els.stageDrop.hidden = !!saved;
  if (saved) {
    setTimeout(() => { if (!state.stage.id) bar.hidden = true; }, 4200);
  }
}

async function saveStagePreview() {
  const res = await exportPdf();
  if (res && res.ok) updateStageBar(true);
}

async function exitStagePreview() {
  await discardStage();
  
  
  const back = state.savedPath || state.filePath;
  state.contentPath = back;
  updateStageBar(false);
  showLoading(t('正在回到上次保存的版本…'));
  try {
    await reloadContentFrom(back);
    toast(t('已退出预览，画面回到上次保存的版本'), 2800);
  } catch (e) {
    toast(t('回到原文件失败：') + ((e && e.message) || e), 6000);
  } finally {
    hideLoading();
  }
}

function closeParaPop() {
  if (els.paraEdit) els.paraEdit.hidden = true;
  
  if (els.paraDropRow) els.paraDropRow.hidden = true;
  if (els.paraInsertBelow) els.paraInsertBelow.hidden = true;
  if (els.paraInsertAbove) els.paraInsertAbove.hidden = true;
  paraPopInfo = null;
}

async function confirmParaPop() {
  if (!paraPopInfo) return;
  const txt = (els.paraText && els.paraText.value) || '';
  if (!txt.trim()) {
    toast(t('文字不能为空。要整段删掉，请用涂黑工具'), 2600);
    return;
  }
  const info = paraPopInfo;
  const res = await reflowParagraph(info.page, info.para, txt,
                                    info.orig, info.pageText, info.cell);
  if (!res) return;
  
  closeParaPop();
  await applyStagePreview(res);
}

async function confirmInsertRow(where) {
  if (!paraPopInfo || paraPopInfo.gid == null) return;
  const info = paraPopInfo;
  const above = where === 'above';
  const ok = confirm(
    (above ? t('在上面插入一行？\\n\\n') : t('在下面插入一行？\\n\\n')) +
    t('· 新行是**这一行的副本**，插完直接点它、改掉里面的文字即可；\\n') +
    t('· {v}整体下移一行，表格补上边框；\n', { v: above ? t('这一行') : t('下面的内容') }) +
    (above ? t('· 在第一行上用它，就等于给表格加了一行表头；\\n') : '') +
    t('· 页底空间不够时会拒绝，不会把内容挤出页面。'));
  if (!ok) return;

  const res = await insertTableRow(info.page, info.para, info.gid, where);
  if (!res) return;
  closeParaPop();
  await applyStagePreview(res);
}

async function confirmDropRow() {
  if (!paraPopInfo || paraPopInfo.gid == null) return;
  const info = paraPopInfo;
  const preview = (els.paraText && els.paraText.value || '').trim();
  const shown = preview.length > 40 ? preview.slice(0, 40) + '…' : preview;
  const ok = confirm(
    t('删掉表格里的这一整行？\\n\\n') +
    t('这一行现在有：{v}\n', { v: shown ? t('「{text}」', { text: shown }) : t('（空行）') }) +
    t('· 整行的文字和它的下边框线一起消失（不只是这一格）；\\n') +
    t('· 下面的内容整体往上走，表格不留空洞；\\n') +
    t('· 想反悔请点预览条上的「撤销」。\\n\\n继续吗？'));
  if (!ok) return;

  const res = await dropTableRow(info.page, info.para, info.gid);
  if (!res) return;
  closeParaPop();
  await applyStagePreview(res);
}

function openTextPop(rec, clientX, clientY, editing = null) {
  const pop = els.textPop;
  if (!pop) return;

  const vr = els.viewer.getBoundingClientRect();
  const r = rec.wrap.getBoundingClientRect();

  
  
  
  textPopInfo = {
    rec,
    page: rec.num,
    nx: (clientX - r.left) / (r.width || 1),
    ny: (clientY - r.top) / (r.height || 1),
    editingId: editing ? editing.id : null,
  };

  pop.hidden = false;
  
  if (els.tipText) els.tipText.value = editing ? (editing.text || '') : '';
  if (els.tipSize) {
    els.tipSize.value = String(editing ? (editing.size || 14) : 14);
  }

  
  
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = Math.max(8, Math.min(clientX, vw - 250));
  let top = clientY;
  if (top + 180 > vh) top = Math.max(8, vh - 190);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';

  setTimeout(() => els.tipText && els.tipText.focus(), 30);
}

function closeTextPop() {
  if (els.textPop) els.textPop.hidden = true;
  textPopInfo = null;
  
  
  
  if (els.tipText && document.activeElement === els.tipText) els.tipText.blur();
}

function textMargins(pageW, pageH) {
  return {
    mX: Math.max(12, pageW * 0.025),
    mTop: Math.max(18, pageH * 0.04),
    mBot: Math.max(30, pageH * 0.08),
  };
}

function pageElScale(pageNum) {
  const W = pageSize(pageNum).width || 595;
  const rec = (state.pages || []).find(r => r.num === pageNum);
  const px = rec && rec.wrap ? rec.wrap.getBoundingClientRect().width : 0;
  return (px || W) / W;
}

let _textMeasureCtx = null;
function makeMeasurer(sizePdf, scale) {
  try {
    if (!_textMeasureCtx) _textMeasureCtx = document.createElement('canvas').getContext('2d');
    _textMeasureCtx.font =
      `${Math.max(6, sizePdf * scale)}px Helvetica, Arial, "NotoSansSC", sans-serif`;
    return (s) => _textMeasureCtx.measureText(s).width;
  } catch (e) { return () => 0; }
}

function layoutText(text, page, xPdf, firstBaseline, size) {
  const { width: W, height: H } = pageSize(page);
  const { mX, mTop, mBot } = textMargins(W, H);
  const scale = pageElScale(page);
  const availPdf = Math.max(size, (W - mX) - xPdf);
  const wrapped = wrapText(text, makeMeasurer(size, scale), availPdf * scale);
  return paginateText(wrapped.split('\n'), {
    page, firstBaseline, pageW: W, pageH: H, size,
    marginX: mX, marginTop: mTop, marginBottom: mBot,
  });
}

function ensurePageExists(num) {
  let guard = 0;
  while (state.pages.length < num && guard++ < 500) {
    insertBlank(state.pages.length - 1);
  }
}

function placeTextPlacements(placements, common) {
  for (const pl of placements) {
    ensurePageExists(pl.page);
    addObject({
      page: pl.page,
      kind: 'text',
      x: common.x,
      y: pl.y,
      w: 0, h: 0,
      text: pl.lines.join('\n'),
      size: common.size,
      font: common.font,
      color: common.color,
    });
  }
}

function confirmTextPop() {
  if (!textPopInfo) return;
  const txt = (els.tipText && els.tipText.value || '').trim();
  const size = parseFloat(els.tipSize && els.tipSize.value) || 14;
  const { page, nx, ny, editingId } = textPopInfo;

  
  if (editingId) {
    if (!txt) {
      toast(t('文字不能为空。要删掉这段文字，请用选择工具删掉对象'), 2600);
      return;
    }
    const cur = (editState.objects || []).find(o => o.id === editingId);
    if (!cur) { updateObject(editingId, { text: txt, size }); closeTextPop(); return; }

    const { width: W, height: H } = pageSize(cur.page);
    const { mX } = textMargins(W, H);
    const minWrap = Math.max(size * 4, W * 0.12);
    const xPdf = Math.min(Math.max(cur.x || mX, mX), Math.max(mX, (W - mX) - minWrap));
    const placements = layoutText(txt, cur.page, xPdf, cur.y || 0, size);

    if (placements.length === 1 && placements[0].page === cur.page) {
      updateObject(editingId, {
        text: placements[0].lines.join('\n'), size, x: xPdf, y: placements[0].y,
      });
    } else {
      removeObject(editingId);
      placeTextPlacements(placements, {
        x: xPdf, size, font: cur.font || 'Helvetica', color: cur.color || currentColor(),
      });
    }
    closeTextPop();
    toast(t('已更新文字'), 1100);
    return;
  }

  if (!txt) { closeTextPop(); return; }

  const { width: W, height: H } = pageSize(page);
  const { mX } = textMargins(W, H);

  const lineH = size * 1.25;
  const firstBaseline = H * (1 - ny) - lineH;

  const minWrap = Math.max(size * 4, W * 0.12);
  let xPdf = nx * W;
  xPdf = Math.min(Math.max(xPdf, mX), Math.max(mX, (W - mX) - minWrap));

  const placements = layoutText(txt, page, xPdf, firstBaseline, size);
  placeTextPlacements(placements, {
    x: xPdf, size, font: 'Helvetica', color: currentColor(),
  });

  closeTextPop();
  const pages = placements.length ? placements[placements.length - 1].page - page + 1 : 1;
  toast(pages > 1 ? t('已添加文字（跨 {pages} 页）', { pages }) : t('已添加文字'), 1100);
}

let replacePopInfo = null;   

function openReplacePop(obj, clientX, clientY) {
  const pop = els.replaceTextPop;
  if (!pop) return;

  replacePopInfo = { id: obj.id };

  pop.hidden = false;
  if (els.replaceText) els.replaceText.value = obj.text || '';

  
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = Math.max(8, Math.min(clientX, vw - 250));
  let top = clientY;
  if (top + 180 > vh) top = Math.max(8, vh - 190);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';

  setTimeout(() => els.replaceText && els.replaceText.focus(), 30);
}

function closeReplacePop() {
  if (els.replaceTextPop) els.replaceTextPop.hidden = true;
  replacePopInfo = null;
  
  
  if (els.replaceText && document.activeElement === els.replaceText) els.replaceText.blur();
}

function confirmReplacePop() {
  if (!replacePopInfo) return;
  const txt = (els.replaceText && els.replaceText.value || '').trim();
  const { id } = replacePopInfo;
  if (!txt) {
    toast(t('替换文字不能为空'), 1800);
    return;
  }
  updateObject(id, { text: txt });
  closeReplacePop();
  toast(t('已设置替换文字'), 1100);
}

function applyAnnotation(sel, type, color) {
  if (!sel || !sel.parts || !sel.parts.length) {
    toast(t('选区已失效，请重新选择文字'), 1800);
    return;
  }

  if (type === 'note') {
    
    const an = addAnnotation(sel, 'highlight', color || currentAnnotColorHex, '');
    setTimeout(() => openAnnotPop(an.id), 60);
    return;
  }

  addAnnotation(sel, type, color, '');
  const label = { highlight: t('高亮'), underline: t('下划线'), wavy: t('波浪线'),
                  strike: t('删除线') }[type] || t('标注');
  toast(t('已添加{label}', { label }), 1100);
}

function openAnnotPop(id) {
  const an = annState.items.find(a => a.id === id);
  if (!an) return;
  popAnnotId = id;

  els.apTag.textContent = { highlight: t('高亮'), underline: t('下划线'), wavy: t('波浪线'),
                            strike: t('删除线'), note: t('笔记'), box: t('区域') }[an.type] || an.type;
  els.apText.textContent = an.text || t('（区域标注）');
  els.apNote.value = an.note || '';

  
  const rec = state.pages[an.page - 1];
  const el = rec && rec.wrap.querySelector(`.an[data-id="${id}"]`);
  const vr = els.viewer.getBoundingClientRect();
  const pop = els.annotPop;
  pop.hidden = false;

  
  
  
  const pw = pop.offsetWidth || 306;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left, top;
  if (el) {
    const r = el.getBoundingClientRect();
    left = r.left;
    top = r.bottom + 10;
  } else {
    left = vw / 2 - pw / 2;
    top = 80;
  }
  left = Math.max(8, Math.min(left, vw - pw - 8));
  if (top + 240 > vh) {
    top = Math.max(8, (el ? el.getBoundingClientRect().top : 200) - 250);
  }
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';

  els.apNote.focus();
}

function closeAnnotPop() {
  els.annotPop && (els.annotPop.hidden = true);
  popAnnotId = null;
}

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

let serverFiles = [];

let mergeSel = [];
let mergeMode = false;

function setMergeMode(on) {
  mergeMode = on;
  document.body.classList.toggle('merge-mode', on);
  const bar = $('mergeBar');
  if (bar) bar.hidden = !on;
  if (!on) { mergeSel = []; paintFileList(); }
  paintMergeBar();
}

function paintMergeBar() {
  const n = $('mergeN');
  if (n) n.textContent = mergeSel.length;
  const go = $('mergeGo');
  if (go) go.disabled = mergeSel.length < 2;
}

async function mergeSelected() {
  if (mergeSel.length < 2) { toast(t('至少勾两份文档'), 1600); return; }
  const go = $('mergeGo');
  if (go) { go.disabled = true; go.classList.add('busy'); }
  try {
    const r = await fetch('/api/merge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ paths: mergeSel }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(errText(d) || t('合并失败 ({status})', { status: r.status }));
    toast(t('已合并 {pages} 页：{name}', { pages: d.pages, name: d.name }), 5000);
    
    window.location.href = '/api/file?download=1&path=' +
      encodeURIComponent(d.path);
    setMergeMode(false);
  } catch (e) {
    toast(e.message || t('合并失败'), 4000);
  } finally {
    if (go) { go.disabled = mergeSel.length < 2; go.classList.remove('busy'); }
  }
}

async function renderWelcome() {
  const container = els.pages;
  welcomeReady = true;
  container.classList.add('empty');
  container.innerHTML = `
  <div class="welcome">
    <h1>${t('PDF 查看器')}</h1>
    <p class="sub">${t('选择服务端目录中的 PDF，或直接把文件拖进来。所有解析都在本机完成，文件不会上传到任何地方。')}</p>

    <div class="drop-zone" id="dropZone">
      <div class="big">${t('拖拽 PDF 文件到这里')}</div>
      <div>${t('或点击选择本地文件')}</div>
    </div>

    <div class="welcome-head">
      <h2>${t('服务端文档')}</h2>
      <input class="file-search" id="fileSearch" placeholder="${t('按名称筛选…')}">
      <button class="btn primary" id="btnNewHere" title="${t('从零新建一个空白 PDF')}">
        <svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><polyline points="14 3 14 8 19 8"/><line x1="12" y1="12" x2="12" y2="17"/><line x1="9.5" y1="14.5" x2="14.5" y2="14.5"/></svg>
        <span class="lbl">${t('新建 PDF')}</span>
      </button>
      <button class="btn" id="btnPickDir" title="${t('添加其他目录')}">
        <svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="9.5" y1="13.5" x2="14.5" y2="13.5"/></svg>
        <span class="lbl">${t('添加目录')}</span>
      </button>
      <button class="btn" id="btnMerge" title="${t('勾选多份文档，按顺序合并成一份')}">
        <svg viewBox="0 0 24 24"><path d="M7 4h7l4 4v12H7z"/><path d="M14 4v4h4"/><path d="M4 8v12h9"/></svg>
        <span class="lbl">${t('合并…')}</span>
      </button>
    </div>

    <div class="merge-bar" id="mergeBar" hidden>
      <span>${t('已选 {n} 份（按点击顺序合并）', { n: '<b id="mergeN">0</b>' })}</span>
      <span class="ep-gap"></span>
      <button class="mini-btn" id="mergeCancel">${t('取消')}</button>
      <button class="mini-btn primary" id="mergeGo" disabled>${t('合并成一份')}</button>
    </div>

    <div class="file-list" id="fileList">
      <div class="empty-tip"><div class="spinner" style="margin:0 auto 10px"></div>${t('正在扫描文档…')}</div>
    </div>

    <div class="welcome-foot">
      <span id="scanInfo"></span>
      <button class="link-btn" id="btnRescan">${t('重新扫描')}</button>
    </div>

    <div class="welcome-foot update-foot" id="updateBox">
      <span class="ver-label" id="verLabel"></span>
      <span class="uf-right">
        <span class="update-msg" id="updateMsg"></span>
        <button class="link-btn" id="btnCheckUpdate">${t('检查更新')}</button>
        <label class="uf-auto">
          <input type="checkbox" id="chkAutoUpdate">
          <span>${t('启动时检查')}</span>
        </label>
      </span>
    </div>
  </div>`;

  bindWelcome();
  bindWelcomeUpdate();
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
  $('btnMerge').addEventListener('click', () => setMergeMode(!mergeMode));
  $('btnNewHere').addEventListener('click', (ev) => openNewPop(ev.currentTarget));
  $('mergeCancel').addEventListener('click', () => setMergeMode(false));
  $('mergeGo').addEventListener('click', mergeSelected);
  
  mergeMode = false;
  document.body.classList.remove('merge-mode');
  paintMergeBar();
}

async function loadServerFiles() {
  const list = $('fileList');
  if (!list) return;
  try {
    const r = await fetch('/api/files');
    const data = await r.json();
    if (!data.ok) throw new Error(errText(data) || t('扫描失败'));
    serverFiles = data.files || [];
    paintFileList();
    const info = $('scanInfo');
    if (info) {
      info.textContent = t('共 {count} 个文档 · {v} 个扫描目录', { count: data.count, v: (data.dirs || []).length });
      info.title = (data.dirs || []).join('\n');
    }
  } catch (err) {
    list.innerHTML = t('<div class="empty-tip">扫描失败：{v}<br>\n      请确认服务已启动，或点击「重新扫描」</div>', { v: escapeHtml(err.message) });
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
      ? t('<div class="empty-tip">没有匹配「{v}」的文档</div>', { v: escapeHtml(kw) })
      : t('<div class="empty-tip">未找到 PDF 文件<br><br>\n         把 PDF 放进扫描目录，或启动时指定：<br>\n         <code>python server.py --dir D:\\\\你的PDF目录</code></div>');
    return;
  }

  list.innerHTML = items.slice(0, 300).map((f, i) => `
    <div class="file-item" data-i="${serverFiles.indexOf(f)}">
      <label class="fi-check" title="${t('选中参与合并')}">
        <input type="checkbox" data-path="${escapeHtml(f.path)}"
          ${mergeSel.includes(f.path) ? 'checked' : ''}>
      </label>
      <div class="fi-icon">PDF</div>
      <div class="fi-text">
        <div class="fi-name" title="${escapeHtml(f.path)}">${escapeHtml(f.name)}</div>
        <div class="fi-meta">${escapeHtml(f.folder)} · ${f.sizeText}</div>
      </div>
      <div class="fi-open" title="${t('用系统默认程序打开')}">
        <svg viewBox="0 0 24 24"><path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h6"/></svg>
      </div>
    </div>`).join('');

  list.querySelectorAll('.file-item').forEach(el => {
    el.addEventListener('click', (e) => {
      const f = serverFiles[parseInt(el.dataset.i, 10)];
      
      if (e.target.closest('.fi-check')) {
        e.preventDefault();
        const at = mergeSel.indexOf(f.path);
        if (at >= 0) mergeSel.splice(at, 1);
        else mergeSel.push(f.path);          
        paintMergeBar();
        el.querySelector('.fi-check input').checked = at < 0;
        return;
      }
      if (e.target.closest('.fi-open')) {
        fetch('/api/open?path=' + encodeURIComponent(f.path))
          .then(r => r.json())
          .then(d => toast(d.ok ? t('已用系统默认程序打开') : (t('打开失败：') + errText(d))))
          .catch(() => toast(t('打开失败')));
        return;
      }
      
      
      
      loadPdfFromPath(f.path, f.name, f.docId);
    });
  });
}

async function pickServerDir() {
  
  const dir = prompt(t('请输入要扫描的文件夹完整路径：\\n例如 D:\\\\我的PDF'), '');
  if (!dir) return;
  showLoading(t('正在扫描目录…'));
  try {
    const r = await fetch('/api/files?dir=' + encodeURIComponent(dir));
    const data = await r.json();
    hideLoading();
    if (!data.ok) { toast(t('扫描失败：') + errText(data)); return; }
    if (!data.files.length) { toast(t('该目录下没有找到 PDF 文件')); return; }
    serverFiles = data.files;
    paintFileList();
    const info = $('scanInfo');
    if (info) info.textContent = t('共 {count} 个文档（含临时目录 {dir}）', { count: data.count, dir });
    toast(t('找到 {count} 个文档', { count: data.count }), 1600);
  } catch (err) {
    hideLoading();
    toast(t('扫描失败：') + err.message);
  }
}

async function handleLocalFile(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== 'application/pdf') {
    toast(t('请选择 PDF 文件'));
    return;
  }
  await loadPdfFromFile(file);
}

function bindToolbar() {
  
  els.stageSave && els.stageSave.addEventListener('click', saveStagePreview);
  els.stageDrop && els.stageDrop.addEventListener('click', exitStagePreview);

  
  els.colorPicker.querySelectorAll('.color-dot').forEach(dot => {
    dot.addEventListener('click', () => {
      els.colorPicker.querySelectorAll('.color-dot').forEach(d => d.classList.remove('on'));
      dot.classList.add('on');
      currentAnnotColor = dot.dataset.color;
      currentAnnotColorHex = dot.style.getPropertyValue('--c').trim() || '#fbbf24';
    });
  });

  
  els.btnAnnotMode.addEventListener('click', () => {
    setAnnotMode(!state.annotMode);
  });

  
  document.addEventListener('pdf:edit-on', () => setAnnotMode(false, true));

  
  els.btnViewSingle.addEventListener('click', () => applyViewMode('single'));
  els.btnViewDouble.addEventListener('click', () => applyViewMode('double'));
  els.btnScrollPlay.addEventListener('click', startAutoScroll);

  
  els.btnBookmark.addEventListener('click', toggleBookmark);

  
  
  
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
            () => toast(t('已复制到剪贴板'), 1200),
            () => toast(t('复制失败'))
          );
        }
        finishMenuAction();
        return;
      }

      if (!pendingSel) {
        toast(t('选区已失效，请重新选择文字'), 1600);
        finishMenuAction();
        return;
      }

      const color = act === 'highlight' ? currentAnnotColorHex : btn.dataset.color;
      const sel = pendingSel;   
      finishMenuAction();       
      applyAnnotation(sel, act, color);
    });
  });

  
  els.selMenu.addEventListener('mousedown', () => {
    menuBusy = true;
    setTimeout(() => { menuBusy = false; }, 300);
  }, true);

  
  els.annotPop.addEventListener('mousedown', guardMenu, true);
  els.annotPop.addEventListener('mouseup', (e) => e.stopPropagation(), true);
  els.annotPop.addEventListener('mousedown', () => {
    menuBusy = true;
    setTimeout(() => { menuBusy = false; }, 300);
  }, true);
  els.apSave.addEventListener('click', () => {
    if (popAnnotId) {
      updateAnnotation(popAnnotId, { note: els.apNote.value.trim() });
      toast(t('笔记已保存'), 1200);
    }
    closeAnnotPop();
  });
  els.apDelete.addEventListener('click', () => {
    if (popAnnotId) { removeAnnotation(popAnnotId); toast(t('已删除标注'), 1200); }
    closeAnnotPop();
  });
  els.apCancel.addEventListener('click', () => {
    
    const an = annState.items.find(a => a.id === popAnnotId);
    if (an && !an.note && !an.text) removeAnnotation(popAnnotId);
    closeAnnotPop();
  });
  els.apNote.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { e.preventDefault(); els.apCancel.click(); }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') { e.preventDefault(); els.apSave.click(); }
  });

  
  els.annotFilter.querySelectorAll('.af-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      els.annotFilter.querySelectorAll('.af-chip').forEach(c => c.classList.remove('on'));
      chip.classList.add('on');
      annState.filter = chip.dataset.filter;
      renderAnnotList();
    });
  });

  
  els.pages.addEventListener('click', (e) => {
    const an = e.target.closest('.an');
    if (an) {
      e.stopPropagation();
      openAnnotPop(an.dataset.id);
      return;
    }
  });

  
  els.viewer.addEventListener('mousedown', onBoxDown);

  
  els.annotActions.hidden = false;
  els.btnExportMd.addEventListener('click', () => {
    if (!state.docId) { toast(t('请先打开文档')); return; }
    if (!annState.items.length) { toast(t('还没有标注可导出')); return; }
    window.open('/api/notes?doc=' + encodeURIComponent(state.docId) +
                '&name=' + encodeURIComponent(state.fileName), '_blank');
  });
  els.btnExportJson.addEventListener('click', () => {
    if (!annState.items.length) { toast(t('还没有标注可导出')); return; }
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

  
  document.addEventListener('mousedown', (e) => {
    if (menuBusy) return;

    
    if (!els.selMenu.hidden && els.selMenu.contains(e.target)) return;

    
    if (!els.annotPop.hidden && els.annotPop.contains(e.target)) return;

    
    if (!els.selMenu.hidden) {
      hideSelMenu();
      pendingSel = null;
    }

    
    const onText = !!e.target.closest('.text-layer');
    const onAnnot = !!e.target.closest('.an');

    if (!els.annotPop.hidden && !onText && !onAnnot) {
      
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

  
  document.addEventListener('selectionchange', debounce(onSelectionChange, 90));

  
  els.progressBar.addEventListener('click', (e) => {
    const r = els.progressBar.getBoundingClientRect();
    const ratio = (e.clientX - r.left) / r.width;
    const v = els.viewer;
    v.scrollTop = ratio * (v.scrollHeight - v.clientHeight);
  });

  
  window.addEventListener('blur', () => {
    if (!els.selMenu.hidden) hideSelMenu();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && !els.selMenu.hidden) hideSelMenu();
  });

  
  els.viewer.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !els.selMenu.hidden) {
      hideSelMenu();
      pendingSel = null;
    }
  });

  
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (els.redactConfirm && !els.redactConfirm.hidden) {
      e.preventDefault();
      els.redactConfirm.hidden = true;
    }
  });

  
  ['zoomSelect', 'btnZoomIn', 'btnZoomOut', 'btnRotate', 'btnFitWidth']
    .forEach(id => {
      const el = els[id];
      if (el) el.addEventListener('click', () => hideSelMenu());
    });

  
  bindEditUI();

  
  els.viewer.addEventListener('mousedown', onEditMouseDown);
  
  els.viewer.addEventListener('dblclick', onEditDoubleClick);
  window.addEventListener('mousemove', onEditMouseMove);
  window.addEventListener('mouseup', onEditMouseUp);

  
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
  
  document.addEventListener('mousedown', (e) => {
    if (els.objBar && !els.objBar.hidden && !els.objBar.contains(e.target)) {
      hideObjBar();
    }
    if (!els.textPop || els.textPop.hidden) return;
    if (els.textPop.contains(e.target)) return;
    closeTextPop();
  }, true);

  
  if (els.replaceOk) els.replaceOk.addEventListener('click', confirmReplacePop);
  if (els.replaceCancel) els.replaceCancel.addEventListener('click', closeReplacePop);
  if (els.replaceText) {
    els.replaceText.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); closeReplacePop(); }
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault(); confirmReplacePop();
      }
    });
  }
  
  document.addEventListener('mousedown', (e) => {
    if (!els.replaceTextPop || els.replaceTextPop.hidden) return;
    if (els.replaceTextPop.contains(e.target)) return;
    closeReplacePop();
  }, true);

  
  if (els.paraOk) els.paraOk.addEventListener('click', confirmParaPop);
  if (els.paraCancel) els.paraCancel.addEventListener('click', closeParaPop);
  if (els.paraDropRow) els.paraDropRow.addEventListener('click', confirmDropRow);
  if (els.paraInsertBelow) {
    els.paraInsertBelow.addEventListener('click', () => confirmInsertRow('below'));
  }
  if (els.paraInsertAbove) {
    els.paraInsertAbove.addEventListener('click', () => confirmInsertRow('above'));
  }
  if (els.btnNewPdf) {
    els.btnNewPdf.addEventListener('click', (ev) => openNewPop(ev.currentTarget));
  }
  if (els.newOk) els.newOk.addEventListener('click', createNewPdf);
  if (els.newCancel) els.newCancel.addEventListener('click', closeNewPop);
  if (els.paraText) {
    els.paraText.addEventListener('input', updateParaCount);
    els.paraText.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { e.preventDefault(); closeParaPop(); }
      
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault(); confirmParaPop();
      }
    });
  }

  
  if (els.sideClose) {
    els.sideClose.addEventListener('click', () => setEditMode(false));
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;

    
    
    
    const overlays = [
      els.paraEdit, els.textPop, els.replaceTextPop,
      els.annotPop, els.selMenu, els.redactConfirm,
    ];
    if (overlays.some(p => p && !p.hidden)) return;

    
    
    
    const tgt = e.target;
    if (tgt && tgt.closest &&
        tgt.closest('.text-input-pop, .annot-pop, input, textarea')) return;

    if (els.sidePanel && !els.sidePanel.hidden) {
      e.preventDefault();
      setEditMode(false);
    }
  });

  
  if (els.paraFold) {
    els.paraFold.addEventListener('click', closeParaPop);
  }

  
  const DW_KEY = LS_PREFIX + 'drawerW';
  const setDrawerW = (px, persist = true) => {
    const w = Math.max(240, Math.min(620, Math.round(px)));
    document.documentElement.style.setProperty('--drawer-w', w + 'px');
    if (persist) { try { localStorage.setItem(DW_KEY, String(w)); } catch (e) {  } }
  };
  try {
    const saved = parseInt(localStorage.getItem(DW_KEY) || '', 10);
    if (saved >= 240 && saved <= 620) setDrawerW(saved, false);
  } catch (e) {  }

  if (els.spResizer) {
    let dragging = false;
    els.spResizer.addEventListener('pointerdown', (e) => {
      dragging = true;
      els.spResizer.classList.add('dragging');
      try { els.spResizer.setPointerCapture(e.pointerId); } catch (err) {  }
      e.preventDefault();
    });
    
    window.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      setDrawerW(window.innerWidth - e.clientX);
    });
    window.addEventListener('pointerup', () => {
      if (!dragging) return;
      dragging = false;
      els.spResizer.classList.remove('dragging');
    });
    els.spResizer.addEventListener('dblclick', () => setDrawerW(300));
  }

  
  if (els.objBarDel) {
    els.objBarDel.addEventListener('click', () => {
      if (objBarId == null) return;
      removeObject(objBarId);
      hideObjBar();
      toast(t('已删除对象'), 1100);
    });
  }
  if (els.objBarFront) {
    els.objBarFront.addEventListener('click', () => {
      if (objBarId == null) return;
      
      const i = editState.objects.findIndex(o => o.id === objBarId);
      if (i >= 0 && i < editState.objects.length - 1) {
        const [o] = editState.objects.splice(i, 1);
        editState.objects.push(o);
        repaintEdits();
        toast(t('已置顶'), 900);
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
        toast(t('已置底'), 900);
      }
    });
  }

  
  els.btnSidebar.addEventListener('click', () => {
    els.sidebar.classList.toggle('collapsed');
    
    setTimeout(() => {
      if (state.pdf && state.scaleMode === 'auto') applyScale('auto');
    }, 220);
  });

  els.btnHome.addEventListener('click', () => {
    if (!state.pdf) return;
    goHome();
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

  
  
  
  
  if (els.btnExportTop) {
    els.btnExportTop.addEventListener('click', () => {
      if (!state.pdf) { toast(t('请先打开一个文档'), 1600); return; }
      exportPdf();
    });
  }

  els.btnPrint.addEventListener('click', () => {
    if (!state.pdf) return;
    state.pdf.getData().then(d => {
      const blob = new Blob([d], { type: 'application/pdf' });
      const url = URL.createObjectURL(blob);
      const w = window.open(url);
      if (w) setTimeout(() => { try { w.print(); } catch (e) {} }, 800);
      else toast(t('打印窗口被浏览器拦截，请允许弹出窗口'));
    });
  });

  

  if (els.langLabel) {
    els.langLabel.textContent = LANG_SHORT[currentLang()] || currentLang().toUpperCase();
  }
  if (els.btnLang) {
    els.btnLang.addEventListener('click', () => {
      const busy = document.body.classList.contains('edit-mode')
        || !!(state.stage && state.stage.id);
      if (busy && !confirm(t('切换语言会重新载入页面，未保存的改动会丢失。继续吗？'))) return;
      
      snapshotOpenTabs();
      setLang(nextLang());
    });
  }

  
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

  
  document.querySelectorAll('.side-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.side-tab').forEach(t => t.classList.toggle('on', t === tab));
      const key = tab.dataset.tab;
      
      
      
      $('panelThumbs').classList.toggle('on', key === 'thumbs');
      $('panelOutline').classList.toggle('on', key === 'outline');
      $('panelAnnots').classList.toggle('on', key === 'annots');
    });
  });

  
  els.viewer.addEventListener('scroll', () => {
    if (!els.selMenu.hidden) hideSelMenu();
  }, { passive: true });

  
  
  ['zoomSelect', 'btnZoomIn', 'btnZoomOut', 'btnRotate', 'btnFitWidth']
    .forEach(id => {
      const el = els[id];
      if (el) el.addEventListener('click', () => hideObjBar());
    });

  els.viewer.addEventListener('scroll', debounce(() => {
    updateCurrentPage();
    updateProgress();
  }, 60), { passive: true });

  
  els.viewer.addEventListener('wheel', (e) => {
    if (!state.pdf) return;
    if (e.ctrlKey || e.metaKey) {
      e.preventDefault();
      stepZoom(e.deltaY < 0 ? 1 : -1);
    }
  }, { passive: false });

  
  els.viewer.addEventListener('click', (e) => {
    if (!state.pdf) return;
    const rec = e.target.closest('.page');
    if (rec) {
      const n = parseInt(rec.dataset.page, 10);
      state.pages[n - 1] && renderPage(state.pages[n - 1]);
    }
  });

  
  ['dragover', 'drop'].forEach(ev => {
    document.addEventListener(ev, (e) => {
      if (ev === 'dragover') { e.preventDefault(); return; }
      e.preventDefault();
      const f = e.dataTransfer?.files?.[0];
      if (!f) return;
      
      if (editState.on && /^image\//.test(f.type || '')) {
        const rec = pageAt(e.clientX, e.clientY);
        if (rec) {
          const r = rec.wrap.getBoundingClientRect();
          const ps = pageSize(rec.num);
          const nx = (e.clientX - r.left) / (r.width || 1);
          const ny = (e.clientY - r.top) / (r.height || 1);
          insertImageFile(f, {
            page: rec.num,
            pdfX: nx * ps.width,
            pdfY: ps.height * (1 - ny),
          });
          return;
        }
      }
      handleLocalFile(f);
    });
  });

  
  
  
  
  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) || String(e.key).toLowerCase() !== 's') return;
    e.preventDefault();
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || e.target.isContentEditable;
    if (typing) {
      
      toast(t('请先完成当前输入（确定 / 取消）再导出'), 2600);
      return;
    }
    if (!state.pdf) return;
    exportPdf();
  });

  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea' || e.target.isContentEditable;

    if ((e.ctrlKey || e.metaKey) && !typing) {
      
      if (editState.on) {
        const k = e.key.toLowerCase();
        if (k === 'z') { e.preventDefault(); e.shiftKey ? redoEdit() : undoEdit(); return; }
        if (k === 'y') { e.preventDefault(); redoEdit(); return; }
        if (k === 'c') { e.preventDefault(); copySelected(); return; }
        if (k === 'v') { e.preventDefault(); pasteObject(); return; }
        if (k === 'd') { e.preventDefault(); duplicateSelected(); return; }
      }
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
        if (state.annotMode) setAnnotMode(false, true);
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
        goHome();
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
    
    const o = new Option(Math.round(target * 100) + '%', String(target));
    els.zoomSelect.add(o);
    els.zoomSelect.value = String(target);
  }
  applyScale(String(target));
}

const onResize = debounce(() => {
  if (state.pdf && state.scaleMode === 'auto') applyScale('auto');
}, 220);

function cacheAllEls() {
  cacheEls([
    'viewer', 'pages', 'loading', 'loadingText', 'toast', 'sidebar',
    'thumbList', 'outlineList', 'panelThumbs', 'panelOutline',
    'btnSidebar', 'btnHome', 'navGroup', 'navSep', 'btnFirst', 'btnPrev',
    'pageInput', 'pageTotal', 'btnNext', 'btnLast',
    'zoomGroup', 'zoomSep', 'btnZoomOut', 'zoomSelect', 'btnZoomIn',
    'toolGroup', 'toolGroupSep', 'btnRotate', 'btnDark', 'btnFitWidth',
    'searchWrap', 'searchInput', 'searchClear', 'searchCount', 'searchNav',
    'btnPrevHit', 'btnNextHit', 'searchSep', 'fileGroup', 'btnExportTop',
    'btnLang', 'langLabel',
    'btnPrint', 'filePicker',
    
    'annotGroup', 'annotSep', 'btnAnnotMode', 'colorPicker',
    'viewGroup', 'btnViewSingle', 'btnViewDouble', 'btnScrollPlay',
    'btnBookmark',
    'panelAnnots', 'annotList', 'annotFilter', 'annotCount',
    'annotActions', 'btnExportMd', 'btnExportJson',
    'progressBar', 'progressFill', 'progressMarkers',
    'stageBar', 'stageText', 'stageSave', 'stageDrop',
    'selMenu', 'annotPop', 'apTag', 'apText', 'apNote', 'apSave', 'apCancel', 'apDelete',
    
    'editGroup', 'editSep', 'btnEditMode', 'editPalette', 'editColor',
    'sidePanel', 'sideClose', 'sideTitle',
    'editColorValue', 'btnEditUndo', 'btnEditRedo', 'btnEditClear', 'btnExportPdf',
    'chkKeepRatio',
    'editSaveHint', 'pagePanel', 'pageListBody',
    'textPop', 'tipText', 'tipSize', 'tipCancel', 'tipOk',
    'objBar', 'objBarName', 'objBarFront', 'objBarBack', 'objBarDel',
    
    'btnRedactPdf', 'redactCount', 'redactConfirm', 'redactConfirmStats',
    'redactCancel', 'redactOk',
    
    'btnReplacePdf', 'replaceCount', 'replaceTextPop', 'replaceText',
    'replaceCancel', 'replaceOk',
    
    'paraEdit', 'paraText', 'paraTip', 'paraPage', 'paraCount',
    'paraFold', 'spResizer',
    'paraCancel', 'paraOk', 'paraDropRow', 'paraInsertAbove', 'paraInsertBelow',
    'btnNewPdf', 'newPop', 'newSize', 'newPages', 'newLandscape',
    'newOk', 'newCancel',
    
    'btnInsertImage', 'imagePicker',
  ]);
  els.sideBody = document.querySelector('.side-body');
}

const LANG_REOPEN_KEY = 'pdfviewer:reopen';

function snapshotOpenTabs() {
  const cur = tabs.active();
  const open = [];
  for (const tab of tabs.list()) {
    if (tab.home || !tab.path) continue;   
    const page = (cur && tab.id === cur.id)
      ? state.currentPage
      : (tab.snap && tab.snap.state ? tab.snap.state.currentPage : 1);
    open.push({
      path: tab.path,
      name: tab.name,
      docId: tab.docId || null,
      page: page || 1,
    });
  }
  try {
    sessionStorage.setItem(LANG_REOPEN_KEY, JSON.stringify({
      open,
      activePath: cur && !cur.home ? cur.path : null,
    }));
  } catch (e) {  }
}

async function restoreOpenTabs() {
  let data = null;
  try {
    const raw = sessionStorage.getItem(LANG_REOPEN_KEY);
    sessionStorage.removeItem(LANG_REOPEN_KEY);
    data = raw ? JSON.parse(raw) : null;
  } catch (e) { return; }
  if (!data || !Array.isArray(data.open) || !data.open.length) return;

  for (const item of data.open) {
    if (!item || !item.path) continue;
    try {
      await loadPdfFromPath(item.path, item.name, item.docId);
      const page = Math.max(1, Number(item.page) || 1);
      if (page > 1) goToPage(page);
    } catch (e) {
      
    }
  }
  const back = data.activePath && tabs.findByPath(data.activePath);
  if (back) tabs.activate(back.id);
}

async function init() {
  
  
  await initI18n();
  cacheAllEls();

  
  tabs.initTabs();
  tabs.bindTabHost({
    refreshChrome,
    beforeClose: beforeCloseTab,
    confirmClose: confirmCloseTab,
    pickFile: () => els.filePicker.click(),
  });
  tabs.bindStrip();

  bindNavHelpers(goToPage, renderPage);
  bindEditHelpers({ goToPage, renderPage });
  
  
  initBatchUI({
    goToPage: (p) => goToPage(p, true),
    flashBoxes,
    applyStagePreview,
  });
  bindToolbar();
  window.addEventListener('resize', onResize);

  

  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
    const k = String(e.key).toLowerCase();
    if (k === 'w') {
      
      const cur = tabs.active();
      if (cur && !cur.home) { e.preventDefault(); closeDocument(); }
      return;
    }
    if (e.key === 'Tab') {
      e.preventDefault();
      cycleTab(e.shiftKey ? -1 : 1);
      return;
    }
    
    if (/^[1-9]$/.test(e.key)) {
      const target = tabs.list()[Number(e.key) - 1];
      if (target) { e.preventDefault(); tabs.activate(target.id); }
    }
  });

  
  window.addEventListener('beforeunload', savePosition);
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { savePosition(); doSave(); saveEdits(); }
  });

  const params = new URLSearchParams(location.search);
  const direct = params.get('path');
  if (direct) {
    

    try {
      await api('/api/allow', { method: 'POST', body: { path: direct } });
    } catch (e) {  }
    await loadPdfFromPath(direct, params.get('name') || t('文档'),
                          params.get('doc') || null);
  } else {
    renderWelcome();
  }
}

window.__openPdfTab = (path, name) => {
  if (!path) return;
  loadPdfFromPath(path, name || t('文档'), null);
};

init();
