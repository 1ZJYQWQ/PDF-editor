

import { t } from '/static/i18n.js';

const CACHE_KEY = 'pdfviewer:update-cache';   
const AUTO_KEY = 'pdfviewer:update-auto';     
const CACHE_MS = 6 * 3600 * 1000;             
const REPO_URL = 'https://github.com/1ZJYQWQ/PDF-editor/releases';

let current = '';
let busy = false;

function readCache() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    const d = raw ? JSON.parse(raw) : null;
    return d && d.at ? d : null;
  } catch (e) { return null; }
}

function writeCache(data) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({ at: Date.now(), data }));
  } catch (e) {  }
}

export function autoEnabled() {
  try { return localStorage.getItem(AUTO_KEY) !== '0'; } catch (e) { return true; }
}

function setAuto(on) {
  try { localStorage.setItem(AUTO_KEY, on ? '1' : '0'); } catch (e) {  }
}

async function fetchUpdate(force) {
  const r = await fetch('/api/update' + (force ? '?force=1' : ''));
  const d = await r.json();
  if (d && d.current) current = d.current;
  return d;
}

function el(id) { return document.getElementById(id); }

function paintVer() {
  const v = el('verLabel');
  if (v) v.textContent = current ? 'v' + current : '';
}

function showMsg(text, kind) {
  const m = el('updateMsg');
  if (!m) return;
  m.textContent = text || '';
  m.className = 'update-msg' + (kind ? ' ' + kind : '');
}

function render(data) {
  if (!data) return;
  if (!data.ok) { showMsg(t('检查更新失败'), 'warn'); return; }
  if (data.hasUpdate && data.latest) {
    const link = el('updateMsg');
    showMsg(t('有新版本 {v}', { v: data.latest }), 'new');
    if (link) {
      link.dataset.url = data.url || REPO_URL;
      
      
      link.title = t('去下载') + (data.tag ? ' · ' + data.tag : '');
    }
    return;
  }
  showMsg(t('已是最新（{v}）', { v: data.current }), 'ok');
}

async function doCheck(force) {
  if (busy) return;
  busy = true;
  const btn = el('btnCheckUpdate');
  if (btn) { btn.disabled = true; btn.textContent = t('检查中…'); }
  try {
    const cached = readCache();
    let data;
    if (!force && cached && Date.now() - cached.at < CACHE_MS) {
      data = cached.data;
    } else {
      data = await fetchUpdate(!!force);
      if (data && data.ok) writeCache(data);
    }
    paintVer();
    render(data);
  } catch (e) {
    showMsg(t('检查更新失败'), 'warn');
  } finally {
    busy = false;
    if (btn) { btn.disabled = false; btn.textContent = t('检查更新'); }
  }
}

export function bindWelcomeUpdate() {
  const box = el('updateBox');
  if (!box) return;

  if (!current) {
    
    fetch('/api/ping').then(r => r.json()).then(d => {
      if (d && d.version) { current = d.version; paintVer(); }
    }).catch(() => {});
  } else {
    paintVer();
  }

  const btn = el('btnCheckUpdate');
  if (btn && !btn.dataset.bound) {
    btn.dataset.bound = '1';
    btn.addEventListener('click', () => doCheck(true));
  }

  const msg = el('updateMsg');
  if (msg && !msg.dataset.bound) {
    msg.dataset.bound = '1';
    msg.addEventListener('click', () => {
      const url = msg.dataset.url;
      if (url) window.open(url, '_blank', 'noopener');
    });
  }

  const chk = el('chkAutoUpdate');
  if (chk) {
    chk.checked = autoEnabled();
    chk.addEventListener('change', () => {
      setAuto(chk.checked);
      if (chk.checked) doCheck(false);
    });
  }

  
  if (autoEnabled()) {
    const cached = readCache();
    if (cached && Date.now() - cached.at < CACHE_MS) {
      paintVer();
      render(cached.data);
    } else {
      doCheck(false).catch(() => {});
    }
  }
}
