



import { t } from '/static/i18n.js';
import { state, $, escapeHtml, toast , errText} from '/static/core.js';
import { exportPdf, downloadServerFile } from '/static/edit.js';

let hooks = {
  goToPage: () => {},
  flashBoxes: () => {},
  applyStagePreview: null,
};

export function initBatchUI(h) {
  Object.assign(hooks, h || {});
  bindFindReplace();
  bindTermRedact();
  bindRangeExport();
}




const keyOf = (h) =>
  `${h.page}:${h.para}:${h.cell ? h.cell[0] + '.' + h.cell[1] : ''}`;

const labelOf = (h) =>
  t('第 {page} 页 · 第 {v} 段', { page: h.page, v: h.para + 1 }) + (h.cell ? t('（表格第 {v} 行第 {v2} 列）', { v: h.cell[0] + 1, v2: h.cell[1] + 1 }) : '');

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}



function markPreview(text, term, ignoreCase) {
  if (!term) return escapeHtml(text);
  const re = new RegExp(escapeRe(term), ignoreCase ? 'gi' : 'g');
  let out = '';
  let last = 0;
  let m;
  let guard = 0;
  while ((m = re.exec(text)) && guard++ < 200) {
    if (!m[0]) { re.lastIndex++; continue; }
    out += escapeHtml(text.slice(last, m.index)) +
      '<mark>' + escapeHtml(m[0]) + '</mark>';
    last = m.index + m[0].length;
  }
  out += escapeHtml(text.slice(last));
  return out;
}


function placePop(pop, anchor) {
  pop.hidden = false;
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth || 300;
  const h = pop.offsetHeight || 200;
  let left = r.left;
  if (left + w > window.innerWidth - 12) left = window.innerWidth - w - 12;
  let top = r.bottom + 8;
  if (top + h > window.innerHeight - 12) top = Math.max(12, r.top - h - 8);
  pop.style.left = Math.max(12, left) + 'px';
  pop.style.top = top + 'px';
}




let hits = [];
let picked = new Set();
let lastTerm = '';
let lastIgnoreCase = false;

function bindFindReplace() {
  const btnFind = $('bpSearch');
  const btnAll = $('bpReplaceAll');
  const btnSel = $('bpReplaceSel');
  if (!btnFind) return;

  btnFind.addEventListener('click', doFind);
  $('bpFind').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doFind(); }
  });
  $('bpRepl').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); doReplace(false); }
  });
  btnAll.addEventListener('click', () => doReplace(false));
  btnSel.addEventListener('click', () => doReplace(true));

  $('bpList').addEventListener('click', (e) => {
    const row = e.target.closest('.bp-hit');
    if (!row) return;
    const h = hits[parseInt(row.dataset.i, 10)];
    if (!h) return;
    if (e.target.matches('input[type=checkbox]')) {
      const k = keyOf(h);
      if (e.target.checked) picked.add(k); else picked.delete(k);
      row.classList.toggle('off', !e.target.checked);
      syncPickButtons();
      return;
    }
    
    hooks.goToPage(h.page);
    hooks.flashBoxes(h.page, h.boxes);
  });
}

function syncPickButtons() {
  const n = picked.size;
  const btnSel = $('bpReplaceSel');
  if (btnSel) btnSel.disabled = n === 0;
  const btnAll = $('bpReplaceAll');
  if (btnAll) btnAll.disabled = hits.length === 0;
  const scope = document.querySelector('.bp-scope');
  if (scope) scope.textContent = n === hits.length ? t('整篇') : t('选中 {n}', { n });
}

async function doFind() {
  const term = ($('bpFind').value || '').trim();
  if (!term) { toast(t('先输入要查找的内容'), 1600); return; }
  const src = state.contentPath || state.filePath;
  if (!src) { toast(t('请先打开一个文档'), 1600); return; }

  const btn = $('bpSearch');
  btn.disabled = true;
  $('bpCount').textContent = t('查找中…');
  try {
    const r = await fetch('/api/find', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path: src, term, ignore_case: !!$('bpIgnoreCase').checked,
      }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(errText(d) || t('查找失败 ({status})', { status: r.status }));

    lastTerm = term;
    lastIgnoreCase = !!$('bpIgnoreCase').checked;
    hits = d.hits || [];
    picked = new Set(hits.map(keyOf));

    const total = hits.reduce((a, h) => a + h.count, 0);
    $('bpCount').textContent = hits.length
      ? t('{length} 段 / {total} 处', { length: hits.length, total })
      : t('没有找到');
    paintHits();
    syncPickButtons();
    if (!hits.length) toast(t('「{term}」没有找到', { term }), 2200);
  } catch (e) {
    $('bpCount').textContent = t('查找失败');
    toast(e.message || t('查找失败'), 4000);
  } finally {
    btn.disabled = false;
  }
}

function paintHits() {
  const box = $('bpList');
  if (!box) return;
  if (!hits.length) {
    box.hidden = false;
    box.innerHTML = t('<div class="bp-empty">没有匹配的段落</div>');
    return;
  }
  box.hidden = false;
  box.innerHTML = hits.slice(0, 200).map((h, i) => `
    <div class="bp-hit" data-i="${i}" title="${escapeHtml(labelOf(h))}">
      <input type="checkbox" checked>
      <span class="bp-page">${h.page}</span>
      <span class="bp-text">${markPreview(h.preview, lastTerm, lastIgnoreCase)}</span>
    </div>`).join('');
}

async function doReplace(onlyPicked) {
  const term = ($('bpFind').value || '').trim();
  const repl = $('bpRepl').value || '';
  if (!term) { toast(t('先输入要查找的内容'), 1600); return; }
  if (!hits.length) { toast(t('先点「查找」'), 1600); return; }

  if (onlyPicked && !picked.size) { toast(t('没有勾选任何一处'), 1600); return; }

  const targets = onlyPicked
    ? hits.filter(h => picked.has(keyOf(h)))
        .map(h => [h.page, h.para, h.cell])
    : null;
  const n = onlyPicked ? targets.length : hits.length;

  const btnAll = $('bpReplaceAll');
  const btnSel = $('bpReplaceSel');
  btnAll.disabled = true;
  btnSel.disabled = true;
  const before = $('bpCount').textContent;
  $('bpCount').textContent = t('替换中…');

  try {
    const body = {
      path: state.contentPath || state.filePath,
      term, repl,
      ignore_case: !!$('bpIgnoreCase').checked,
      name: state.fileName,
    };
    if (targets) body.targets = targets;
    
    if (state.stage && state.stage.id) body.stage = state.stage.id;

    const r = await fetch('/api/replace_all', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    if (!r.ok) {
      let msg = t('替换失败 ({status})', { status: r.status });
      try { const j = await r.json(); const m2 = errText(j); if (m2) msg = m2; } catch (e) {}
      throw new Error(msg);
    }

    const sid = r.headers.get('X-Stage-Id') || '';
    const rawPath = r.headers.get('X-Stage-Path') || '';
    let spath = rawPath;
    if (rawPath) {
      try { spath = decodeURIComponent(rawPath); } catch (e) { spath = rawPath; }
    }
    try { await r.arrayBuffer(); } catch (e) {  }

    if (!sid || !spath) throw new Error(t('服务端没有返回预览位置'));
    state.stage = { id: sid, path: spath };
    state.contentPath = spath;

    const edits = r.headers.get('X-Replace-Edits') || '0';
    const occ = r.headers.get('X-Replace-Occurrences') || '0';
    const skipped = r.headers.get('X-Replace-Skipped') || '';

    if (hooks.applyStagePreview) {
      await hooks.applyStagePreview({ stagePath: spath });
    }
    toast(t('已替换 {edits} 段（{occ} 处）', { edits, occ }) +
      (skipped ? t('；跳过：{v}', { v: decodeURIComponent(skipped) }) : ''),
      skipped ? 6000 : 3800);

    
    await doFind();
  } catch (e) {
    $('bpCount').textContent = before;
    toast(e.message || t('替换失败'), 5000);
  } finally {
    btnAll.disabled = false;
    btnSel.disabled = false;
    syncPickButtons();
  }
}




function bindTermRedact() {
  const btn = $('btnTermRedact');
  const pop = $('termPop');
  if (!btn || !pop) return;

  btn.addEventListener('click', () => {
    if (pop.hidden) placePop(pop, btn);
    else pop.hidden = true;
    if (!pop.hidden) $('termText').focus();
  });
  $('termCancel').addEventListener('click', () => { pop.hidden = true; });
  $('termOk').addEventListener('click', runTermRedact);

  
  
  const sel = $('termMode');
  if (sel) {
    const paint = () => {
      const del = sel.value === 'delete';
      const hint = $('termHint');
      if (hint) {
        hint.textContent = del
          ? t('删掉：文字消失、段落重新折行，不留黑块')
          : t('涂黑：版面不动，盖一块黑（字形同样会被删）');
      }
      const ok = $('termOk');
      if (ok) ok.textContent = del ? t('删除并另存为…') : t('涂黑并另存为…');
    };
    sel.addEventListener('change', paint);
    paint();
  }
}

async function runTermRedact() {
  const raw = $('termText').value || '';
  const terms = raw.split('\n').map(s => s.trim()).filter(Boolean);
  if (!terms.length) { toast(t('先输入要删除的词（一行一个）'), 2000); return; }
  const src = state.contentPath || state.filePath;
  if (!src) { toast(t('请先打开一个文档'), 1600); return; }

  
  
  const sel = ($('termMode') && $('termMode').value) || 'delete';
  const style = sel.startsWith('redact') ? 'redact' : 'delete';
  const mode = style === 'redact' ? sel.slice(7) : null;

  const scopeText = style === 'delete'
    ? t('删掉这些词，该段落重新折行，后面的文字往前补位，不留黑块')
    : {
      term: t('涂黑命中的那几个字（版面不动，盖一块黑）'),
      line: t('连命中的整行一起涂黑（版面不动）'),
      para: t('连命中的整段一起涂黑（版面不动）'),
    }[mode];

  const ok = confirm(
    t('将从文档里永久删除这些词命中的每一处：\n\n{v}\n\n', { v: terms.join(t('、')) }) +
    t('处理方式：{scopeText}\n', { scopeText }) +
    t('· 文字会被真正删掉，另存为新文件；\\n') +
    t('· 图片/扫描件里的文字删不掉。\\n\\n继续吗？'));
  if (!ok) return;

  const btn = $('termOk');
  btn.disabled = true;
  btn.textContent = t('处理中…');
  try {
    const r = await fetch('/api/redact_terms', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: src, terms, ignore_case: true, style, mode }),
    });
    if (!r.ok) {
      let msg = t('删除失败 ({status})', { status: r.status });
      try { const j = await r.json(); const m2 = errText(j); if (m2) msg = m2; } catch (e) {}
      throw new Error(msg);
    }

    const rawOut = r.headers.get('X-Out-Path') || '';
    let outPath = '';
    if (rawOut) {
      try { outPath = decodeURIComponent(rawOut); } catch (e) { outPath = rawOut; }
    }
    const cd = r.headers.get('Content-Disposition') || '';
    let name = t('{fileName}-{v}.pdf', { fileName: state.fileName, v: style === 'delete' ? t('已删除') : t('已涂黑') });
    const m = /filename\*=UTF-8''([^;]+)/.exec(cd);
    if (m) { try { name = decodeURIComponent(m[1]); } catch (e) {} }

    if (outPath) {
      downloadServerFile(outPath, name);
    } else {
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = name; a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    }

    $('termPop').hidden = true;

    
    let msg;
    let bad;
    if (style === 'delete') {
      const edits = r.headers.get('X-Delete-Edits') || '0';
      const removed = r.headers.get('X-Delete-Removed') || '0';
      const remaining = parseInt(r.headers.get('X-Delete-Remaining') || '0', 10);
      const residue = r.headers.get('X-Delete-Residue');
      const missing = r.headers.get('X-Delete-Missing') || '';
      const skipped = r.headers.get('X-Delete-Skipped') || '';
      const check = residue == null ? '' :
        (residue === '0' ? t('，复查残留 0 处') : t('，⚠ 复查仍有 {residue} 处残留', { residue }));
      
      
      const more = remaining > 0 ? t('；还有 {remaining} 段没处理，请再点一次', { remaining }) : '';
      msg = t('已删掉 {removed} 处（{edits} 段）{check}', { removed, edits, check }) +
        (missing ? t('；没找到：{v}', { v: decodeURIComponent(missing) }) : '') +
        (skipped ? t('；跳过：{v}', { v: decodeURIComponent(skipped) }) : '') + more;
      bad = !!missing || (residue && residue !== '0') || !!skipped || remaining > 0;
    } else {
      const regions = r.headers.get('X-Redact-Regions') || '0';
      const glyphs = r.headers.get('X-Redact-Glyphs') || '0';
      const missing = r.headers.get('X-Redact-Missing') || '';
      const residue = r.headers.get('X-Redact-Residue');
      const check = residue == null ? '' :
        (residue === '0' ? t('；复查残留 0 处') : t('；⚠ 复查仍有 {residue} 处残留', { residue }));
      msg = t('已涂黑 {regions} 处 / {glyphs} 个字形{check}', { regions, glyphs, check }) +
        (missing ? t('；没找到：{v}', { v: decodeURIComponent(missing) }) : '');
      bad = !!missing || (residue && residue !== '0');
    }
    toast(msg, bad ? 7000 : 4200);
  } catch (e) {
    toast(e.message || t('删除失败'), 5000);
  } finally {
    btn.disabled = false;
    btn.textContent = t('删除并另存为…');
  }
}




function bindRangeExport() {
  const btn = $('btnRange');
  const pop = $('rangePop');
  if (!btn || !pop) return;

  btn.addEventListener('click', () => {
    if (pop.hidden) {
      placePop(pop, btn);
      $('rangeHint').textContent = state.pageCount
        ? t('本文档 {pageCount} 页', { pageCount: state.pageCount }) : '';
      $('rangeSpec').focus();
    } else {
      pop.hidden = true;
    }
  });
  $('rangeCancel').addEventListener('click', () => { pop.hidden = true; });

  $('rangeOk').addEventListener('click', async () => {
    const spec = ($('rangeSpec').value || '').trim();
    pop.hidden = true;
    await exportPdf(spec ? { range: spec } : {});
  });

  $('splitOk').addEventListener('click', runSplit);
}

async function runSplit() {
  const src = state.contentPath || state.filePath;
  if (!src) { toast(t('请先打开一个文档'), 1600); return; }
  const per = parseInt($('splitPer').value || '0', 10);
  if (!per || per < 1) { toast(t('先填每份几页'), 1800); return; }

  const btn = $('splitOk');
  btn.disabled = true;
  btn.textContent = t('拆分中…');
  try {
    const r = await fetch('/api/split', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: src, per, name: state.fileName }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.ok) throw new Error(errText(d) || t('拆分失败 ({status})', { status: r.status }));

    
    const pop = $('rangePop');
    let box = pop.querySelector('.split-list');
    if (!box) {
      box = document.createElement('div');
      box.className = 'split-list';
      pop.appendChild(box);
    }
    box.innerHTML = d.files.map(f => `
      <a class="split-item" href="/api/file?download=1&path=${
        encodeURIComponent(f.path)}" title="${escapeHtml(f.path)}">
        ${escapeHtml(f.name)}
      </a>`).join('');
    $('rangeHint').textContent = t('已拆成 {count} 份，点名字下载', { count: d.count });
    toast(t('已拆分 {count} 份（每份 {per} 页）', { count: d.count, per }), 3200);
  } catch (e) {
    toast(e.message || t('拆分失败'), 5000);
  } finally {
    btn.disabled = false;
    btn.textContent = t('拆分整篇');
  }
}
