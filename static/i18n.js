



export const SUPPORTED = ['zh', 'en'];


export const LANG_LABEL = { zh: '简体中文', en: 'English' };


export const LANG_SHORT = { zh: '中', en: 'EN' };


const LANG_TAG = { zh: 'zh-CN', en: 'en' };

const STORE_KEY = 'pdfviewer.lang';

let lang = 'zh';
let dict = Object.create(null);

export function currentLang() {
  return lang;
}



export function t(msgid, vars) {
  if (msgid === undefined || msgid === null) return '';
  const s = Object.prototype.hasOwnProperty.call(dict, msgid) ? dict[msgid] : msgid;
  if (!vars) return String(s);
  return String(s).replace(/\{(\w+)\}/g, (m, k) => (
    Object.prototype.hasOwnProperty.call(vars, k) ? String(vars[k]) : m
  ));
}


export function has(msgid) {
  return Object.prototype.hasOwnProperty.call(dict, msgid);
}

function detectLang() {
  const params = new URLSearchParams(location.search);
  const forced = params.get('lang');
  if (forced && SUPPORTED.includes(forced)) {
    try { localStorage.setItem(STORE_KEY, forced); } catch (e) {  }
    return forced;
  }
  let saved = null;
  try { saved = localStorage.getItem(STORE_KEY); } catch (e) {  }
  if (saved && SUPPORTED.includes(saved)) return saved;

  

  const nav = String(navigator.language || 'zh').toLowerCase();
  return nav.startsWith('zh') ? 'zh' : 'en';
}

async function loadDict(code) {
  if (code === 'zh') return Object.create(null);
  const mod = await import(`./lang/${code}.js`);
  return mod.default || Object.create(null);
}



export async function initI18n() {
  lang = detectLang();
  if (lang !== 'zh') {
    try {
      dict = await loadDict(lang);
    } catch (e) {
      
      console.warn('[i18n] 语言包加载失败，回退中文：', lang, e);
      lang = 'zh';
      dict = Object.create(null);
    }
  }
  document.documentElement.setAttribute('lang', LANG_TAG[lang] || lang);
  applyDom();
  return lang;
}



export function applyDom(root) {
  const scope = root || document;

  


  scope.querySelectorAll('[data-i18n]').forEach((el) => {
    const key = el.getAttribute('data-i18n');
    if (key) el.textContent = t(key);
  });
  scope.querySelectorAll('[data-i18n-title]').forEach((el) => {
    const key = el.getAttribute('data-i18n-title');
    if (key) el.setAttribute('title', t(key));
  });
  scope.querySelectorAll('[data-i18n-ph]').forEach((el) => {
    const key = el.getAttribute('data-i18n-ph');
    if (key) el.setAttribute('placeholder', t(key));
  });
  scope.querySelectorAll('[data-i18n-aria]').forEach((el) => {
    const key = el.getAttribute('data-i18n-aria');
    if (key) el.setAttribute('aria-label', t(key));
  });

  


  scope.querySelectorAll('[data-i18n-html]').forEach((el) => {
    const key = el.getAttribute('data-i18n-html');
    if (key) el.innerHTML = t(key);
  });
}



export function setLang(next) {
  if (!SUPPORTED.includes(next) || next === lang) return;
  try { localStorage.setItem(STORE_KEY, next); } catch (e) {  }
  location.reload();
}


export function nextLang() {
  const i = SUPPORTED.indexOf(lang);
  return SUPPORTED[(i + 1) % SUPPORTED.length];
}
