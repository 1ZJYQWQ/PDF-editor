export function isTypingTarget(el) {
  if (!el || !el.tagName) return false;
  const tag = el.tagName.toLowerCase();
  if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
  if (el.isContentEditable) return true;

  return !!(el.closest && el.closest('[contenteditable="true"], .inline-edit'));
}

const MODAL_IDS = ['encMask', 'pwMask', 'redactConfirm'];

export function topModal(doc = document) {
  for (let i = MODAL_IDS.length - 1; i >= 0; i -= 1) {
    const el = doc.getElementById(MODAL_IDS[i]);
    if (el && !el.hidden) return el;
  }
  return null;
}

const POPUP_IDS = ['textPop', 'replaceTextPop', 'annotPop', 'selMenu'];

export function topPopup(doc = document) {
  for (let i = POPUP_IDS.length - 1; i >= 0; i -= 1) {
    const el = doc.getElementById(POPUP_IDS[i]);
    if (el && !el.hidden) return el;
  }
  return null;
}

export function dblclickMode(editOn, tool) {
  if (editOn && tool && tool !== 'select') return 'draw';
  return 'text';
}
