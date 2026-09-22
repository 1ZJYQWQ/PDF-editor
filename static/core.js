/* ==========================================================================
   共享核心：状态、DOM 引用、通用工具
   被 app.js 与 annot.js 共同引用，避免循环依赖
   ========================================================================== */

import * as pdfjsLib from '/static/vendor/pdf.min.mjs';

pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/vendor/pdf.worker.min.mjs';

export { pdfjsLib };

/* 调试开关：控制台执行 __PDFVIEW_DEBUG__ = true 即可打印标注/渲染日志 */
if (typeof window !== 'undefined' && window.__PDFVIEW_DEBUG__ === undefined) {
  window.__PDFVIEW_DEBUG__ = false;
}

/* 全局状态 */
export const state = {
  pdf: null,
  filePath: null,
  fileName: '',
  docId: null,
  pageCount: 0,
  scale: 1,
  scaleMode: 'auto',
  rotation: 0,
  invert: false,
  currentPage: 1,
  pages: [],
  observer: null,
  renderQueue: new Set(),
  searchHits: [],
  searchHitsByPage: new Map(),
  hitIndex: -1,
  outlinMap: new Map(),
  viewMode: 'single',   // single | double
  annotMode: false,
  bookmarks: new Set(), // 页码书签
};

export const els = {};

export function cacheEls(ids) {
  ids.forEach(id => { els[id] = document.getElementById(id); });
}

export function $(id) { return document.getElementById(id); }

export function uid() {
  return 'a' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export function debounce(fn, ms) {
  let tid;
  return (...args) => {
    clearTimeout(tid);
    tid = setTimeout(() => fn(...args), ms);
  };
}

export function toast(msg, ms = 1800) {
  const t = els.toast;
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._tid);
  t._tid = setTimeout(() => t.classList.remove('show'), ms);
}

export function showLoading(text) {
  if (!els.loading) return;
  els.loadingText.textContent = text || '正在加载…';
  els.loading.classList.remove('hide');
}

export function hideLoading() {
  if (els.loading) els.loading.classList.add('hide');
}

/* 统一的 fetch 封装 */
export async function api(url, opts = {}) {
  const init = { method: opts.method || 'GET' };
  if (opts.body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' };
    init.body = JSON.stringify(opts.body);
  }
  const r = await fetch(url, init);
  let data;
  try {
    data = await r.json();
  } catch (e) {
    throw new Error(`服务返回异常 (${r.status})`);
  }
  if (!r.ok || data.ok === false) {
    throw new Error(data.error || `请求失败 (${r.status})`);
  }
  return data;
}
