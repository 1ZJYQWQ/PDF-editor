

import { state, els, blankState } from '/static/core.js';
import { t } from '/static/i18n.js';
import { annState } from '/static/annot.js';
import {
  editState, snapshotHistory, restoreHistory, syncEditModeUI,
} from '/static/edit.js';

let host = null;                 
const tabs = [];                 
let activeId = null;
let seq = 0;
let homeTab = null;
let welcomePane = null;          
let panesEl = null;
let stripEl = null;

function blankEdit() {
  return {
    on: false,
    tool: null,
    objects: [],
    pages: { order: null, delete: [], rotate: {}, insert: [] },
    pageInfo: [],
    selectedId: null,
    dirty: false,
    tempShape: null,
  };
}

function blankAnn(docId = null) {
  return { docId, items: [], ext: [], activeId: null };
}

function resetAll() {
  Object.assign(state, blankState());
  Object.assign(annState, blankAnn());
  Object.assign(editState, blankEdit());
  restoreHistory(null);
}

export function initTabs() {
  panesEl = document.getElementById('panes');
  stripEl = document.getElementById('tabStrip');
  welcomePane = document.getElementById('pages');

  homeTab = {
    id: 'home',
    name: t('主页'),
    pane: welcomePane,
    home: true,
    snap: null,
  };
  tabs.push(homeTab);
  activeId = homeTab.id;
  els.pages = welcomePane;
  document.body.classList.add('on-home');
  refreshBar();
}

export function bindTabHost(inject) { host = inject; }

export function list() { return tabs.slice(); }
export function active() { return tabs.find(x => x.id === activeId) || null; }
export function homeId() { return homeTab ? homeTab.id : null; }
export function isHome() { return activeId === homeTab.id; }

export function findByPath(path) {
  if (!path) return null;
  return tabs.find(x => x.path && x.path === path) || null;
}

export function findByDoc(docId) {
  if (!docId) return null;
  return tabs.find(x => x.docId && x.docId === docId) || null;
}

function takeSnap() {
  return {
    state: { ...state },
    ann: { ...annState },
    edit: { ...editState },
    hist: snapshotHistory(),
    scroll: { top: els.viewer.scrollTop, left: els.viewer.scrollLeft },
    query: els.searchInput ? els.searchInput.value : '',
    count: els.searchCount ? els.searchCount.textContent : '',
  };
}

function applySnap(snap) {
  
  Object.assign(state, blankState(), snap.state);
  Object.assign(annState, snap.ann);
  Object.assign(editState, snap.edit);
  restoreHistory(snap.hist);
}

function showPane(tab, restoreScroll = true) {
  activeId = tab.id;
  for (const x of tabs) x.pane.hidden = x.id !== tab.id;
  
  
  els.pages = tab.pane;
  document.body.classList.toggle('on-home', !!tab.home);
  if (els.searchInput) els.searchInput.value = (restoreScroll && tab.snap && tab.snap.query) || '';
  if (els.searchCount) els.searchCount.textContent = (restoreScroll && tab.snap && tab.snap.count) || '';
  els.viewer.scrollTop = restoreScroll && tab.snap ? tab.snap.scroll.top : 0;
  els.viewer.scrollLeft = restoreScroll && tab.snap ? tab.snap.scroll.left : 0;
  syncEditModeUI();
  refreshBar();
  if (host && host.refreshChrome) host.refreshChrome();
}

export function activate(id) {
  if (!id || id === activeId) return;
  const target = tabs.find(x => x.id === id);
  if (!target) return;
  const cur = active();
  if (cur && !cur.home) cur.snap = takeSnap();
  if (target.home) {
    resetAll();
    showPane(target, false);
  } else {
    applySnap(target.snap);
    showPane(target, true);
  }
}

export function begin(meta = {}) {
  const cur = active();
  if (cur && !cur.home) cur.snap = takeSnap();

  const id = 'tab' + (++seq);
  const pane = document.createElement('div');
  pane.className = 'pages tab-pane';
  pane.dataset.tab = id;
  panesEl.appendChild(pane);

  const tab = {
    id,
    name: meta.name || t('文档'),
    path: meta.path || null,
    docId: meta.docId || null,
    pane,
    snap: null,
  };

  resetAll();
  document.body.classList.remove('on-home');
  tabs.push(tab);
  showPane(tab, false);
  return tab;
}

export function close(id) {
  const idx = tabs.findIndex(x => x.id === id);
  if (idx < 0) return false;
  const tab = tabs[idx];
  if (tab.home) return false;                       
  if (host && host.confirmClose && host.confirmClose(tab) === false) return false;

  const wasActive = tab.id === activeId;

  if (host && host.beforeClose) host.beforeClose(tab, wasActive);

  tab.pane.remove();
  tabs.splice(idx, 1);

  if (wasActive) {
    
    const next = tabs[Math.min(idx, tabs.length - 1)];
    if (next.home) {
      resetAll();
      showPane(next, false);
    } else {
      applySnap(next.snap);
      showPane(next, true);
    }
  } else {
    refreshBar();
  }
  return true;
}

export function rename(id, name) {
  const tab = tabs.find(x => x.id === id);
  if (!tab) return;
  tab.name = name;
  refreshBar();
}

const DOC_ICON =
  '<svg viewBox="0 0 24 24" aria-hidden="true">' +
  '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>' +
  '<polyline points="14 3 14 8 19 8"/></svg>';

const HOME_ICON =
  '<svg viewBox="0 0 24 24" aria-hidden="true">' +
  '<path d="M4 11l8-7 8 7"/><path d="M6 10v10h12V10"/></svg>';

function isDirty(tab) {
  if (tab.id === activeId) return !!editState.dirty;
  return !!(tab.snap && tab.snap.edit && tab.snap.edit.dirty);
}

export function refreshBar() {
  if (!stripEl) return;
  stripEl.hidden = false;      
  const frag = document.createDocumentFragment();

  for (const tab of tabs) {
    const item = document.createElement('div');
    item.className = 'tab-item' + (tab.id === activeId ? ' on' : '') +
      (tab.home ? ' home' : '');
    item.dataset.id = tab.id;
    item.title = tab.home ? t('主页') : (tab.path || tab.name);

    const ico = document.createElement('span');
    ico.className = 'tab-ico';
    ico.innerHTML = tab.home ? HOME_ICON : DOC_ICON;

    const name = document.createElement('span');
    name.className = 'tab-name';
    name.textContent = tab.name;

    item.append(ico, name);

    if (!tab.home && isDirty(tab)) {
      const dot = document.createElement('span');
      dot.className = 'tab-dirty';
      dot.title = t('有未保存的改动');
      item.appendChild(dot);
    }

    
    if (!tab.home) {
      const x = document.createElement('button');
      x.className = 'tab-close';
      x.title = t('关闭标签页');
      x.setAttribute('aria-label', t('关闭标签页'));
      x.textContent = '×';
      item.appendChild(x);
    }

    frag.appendChild(item);
  }

  const add = document.createElement('button');
  add.className = 'tab-add';
  add.id = 'btnTabAdd';
  add.title = t('打开文件（新标签页）');
  add.setAttribute('aria-label', t('打开文件（新标签页）'));
  add.textContent = '+';
  frag.appendChild(add);

  stripEl.innerHTML = '';
  stripEl.appendChild(frag);

  
  const on = stripEl.querySelector('.tab-item.on');
  if (on && on.scrollIntoView) {
    try { on.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch (e) {  }
  }
}

export function bindStrip() {
  if (!stripEl) return;
  stripEl.addEventListener('click', (e) => {
    const add = e.target.closest('#btnTabAdd');
    if (add) {
      if (host && host.pickFile) host.pickFile();
      return;
    }
    const item = e.target.closest('.tab-item');
    if (!item) return;
    if (e.target.closest('.tab-close')) { close(item.dataset.id); return; }
    activate(item.dataset.id);
  });
  
  stripEl.addEventListener('auxclick', (e) => {
    if (e.button !== 1) return;
    const item = e.target.closest('.tab-item');
    if (item && !item.classList.contains('home')) {
      e.preventDefault();
      close(item.dataset.id);
    }
  });
  
  stripEl.addEventListener('wheel', (e) => {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    if (stripEl.scrollWidth <= stripEl.clientWidth) return;
    stripEl.scrollLeft += e.deltaY;
    e.preventDefault();
  }, { passive: false });
}
