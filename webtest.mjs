/* ==========================================================================
   前端纯逻辑测试 —— 用 node 直接跑，不需要浏览器

   为什么需要它
     前端原来没有任何自动验证手段：`check.py` 只做静态检查
     （语法/标识符/import 匹配/DOM id），跑不了逻辑。
     于是「坐标换算对不对」「涂黑对象有没有混进普通导出」这类
     真正会出错的地方，只能靠肉眼点。

     `geom.js` 里是**纯函数**（不依赖 state/DOM/pdf.js），所以能在 node 里测。

   用法
     node webtest.mjs
     或 python run_tests.py（会一并跑）
   ========================================================================== */
import {
  screenToNorm, normToScreen, normToPdf, pdfToNorm,
  distToSegment, hitTestNorm, HIT_LINE_TOL,
  payloadObjects, hasAnyEdit, dragDeltaToPdf, movedPosition,
  makeTextBoxAuto,
} from './static/geom.js';

let PASS = 0;
let FAIL = 0;
const FAILED = [];

function check(name, cond, extra = '') {
  if (cond) {
    PASS++;
    console.log(`  PASS ${name}${extra ? `  [${extra}]` : ''}`);
  } else {
    FAIL++;
    FAILED.push(name);
    console.log(`  FAIL ${name}${extra ? `  [${extra}]` : ''}`);
  }
}

const near = (a, b, eps = 1e-9) => Math.abs(a - b) < eps;
const W = 595;      // A4
const H = 842;
const getSize = () => ({ width: W, height: H });

console.log('='.repeat(62));
console.log('  前端纯逻辑测试（geom.js）');
console.log('='.repeat(62));

/* --------------------------------------------------------------------- */
console.log('\n=== 1. 坐标换算：y 轴翻转 ===');
/* --------------------------------------------------------------------- */

// PDF 点 -> 归一化（手算对照，不拿函数自己校验自己）
const p1 = pdfToNorm(100, 700, 200, 50, W, H);
check('pdfToNorm x = x/W', near(p1.x, 100 / W), String(p1.x));
check('pdfToNorm y = 1 - (y+h)/H', near(p1.y, 1 - 750 / H), String(p1.y));
check('pdfToNorm w = w/W', near(p1.w, 200 / W));
check('pdfToNorm h = h/H', near(p1.h, 50 / H));

// ★ 语义护栏：PDF 原点在左下、屏幕原点在左上
check('PDF 左下角 (0,0) → 归一化 y=1（屏幕底部）',
  near(pdfToNorm(0, 0, 0, 0, W, H).y, 1));
check('PDF 顶端 (0,H) → 归一化 y=0（屏幕顶部）',
  near(pdfToNorm(0, H, 0, 0, W, H).y, 0));
check('屏幕靠上的框，PDF y 更大（轴方向没搞反）',
  normToPdf(0.5, 0.05, 0.1, 0.1, W, H).y >
  normToPdf(0.5, 0.80, 0.1, 0.1, W, H).y);

const q1 = normToPdf(0.5, 0.25, 0.25, 0.5, W, H);
check('normToPdf x = nx*W', near(q1.x, 0.5 * W));
check('normToPdf y = H*(1-ny-nh)', near(q1.y, H * 0.25));
check('normToPdf w/h', near(q1.w, 0.25 * W) && near(q1.h, 0.5 * H));

/* --------------------------------------------------------------------- */
console.log('\n=== 2. 往返无损与缩放不变性 ===');
/* --------------------------------------------------------------------- */
let bad = [];
for (const [nx, ny, nw, nh] of [
  [0, 0, 1, 1], [0.1, 0.2, 0.3, 0.4], [0.999, 0.001, 0.001, 0.999],
  [0.5, 0.5, 0, 0], [0.25, 0.75, 0.5, 0.25],
]) {
  const r = pdfToNorm(...Object.values(normToPdf(nx, ny, nw, nh, W, H)), W, H);
  if (![r.x, r.y, r.w, r.h].every((v, i) => near(v, [nx, ny, nw, nh][i], 1e-12))) {
    bad.push(`${nx},${ny},${nw},${nh} -> ${r.x},${r.y},${r.w},${r.h}`);
  }
}
check('normToPdf → pdfToNorm 往返无损（5 组）', bad.length === 0, bad.join('; '));

// 缩放不变性：同一个 PDF 框，在不同页面尺寸下归一化值必然不同，
// 但「屏幕上量到的归一化值」与 PDF 尺寸无关 —— 这是归一化的意义
const pdfRect = normToPdf(0.2, 0.3, 0.4, 0.2, W, H);
const back = pdfToNorm(pdfRect.x, pdfRect.y, pdfRect.w, pdfRect.h, W, H);
check('归一化值与页面尺寸无关（0.2/0.3 原样回来）',
  near(back.x, 0.2) && near(back.y, 0.3), `${back.x},${back.y}`);

// 屏幕换算
const s = normToScreen(0.2, 0.3, 0.4, 0.2, 800, 1000);
check('normToScreen 按屏幕尺寸放大',
  near(s.left, 160) && near(s.top, 300) && near(s.width, 320) &&
  near(s.height, 200), JSON.stringify(s));
const sn = screenToNorm(260, 400, 100, 100, 800, 1000);
check('screenToNorm 反算正确',
  near(sn.x, 0.2) && near(sn.y, 0.3), JSON.stringify(sn));
check('screenToNorm 不会除零', Number.isFinite(
  screenToNorm(10, 10, 0, 0, 0, 0).x));

/* --------------------------------------------------------------------- */
console.log('\n=== 3. 点到线段距离 ===');
/* --------------------------------------------------------------------- */
check('垂足落在段内', near(distToSegment(5, 3, 0, 0, 10, 0), 3, 1e-9));
check('垂足落在段外（取端点距离）',
  near(distToSegment(-4, 3, 0, 0, 10, 0), 5, 1e-9));
check('退化线段（两端点重合）',
  near(distToSegment(0, 3, 0, 0, 0, 0), 3, 1e-9));

/* --------------------------------------------------------------------- */
console.log('\n=== 4. 命中测试 ===');
/* --------------------------------------------------------------------- */
const rect = { id: 'r1', kind: 'rect', page: 1, x: 100, y: 700, w: 200, h: 50 };
const rp = pdfToNorm(rect.x, rect.y, rect.w, rect.h, W, H);
const mid = { x: rp.x + rp.w / 2, y: rp.y + rp.h / 2 };

check('点在框内 → 命中', hitTestNorm([rect], 1, mid.x, mid.y, getSize) === rect);
check('点在框外 → 不命中',
  hitTestNorm([rect], 1, rp.x - 0.05, mid.y, getSize) === null);
check('别的页上的对象不受影响',
  hitTestNorm([rect], 2, mid.x, mid.y, getSize) === null);
check('边界上（左边缘）能命中',
  hitTestNorm([rect], 1, rp.x, mid.y, getSize) === rect);

// 后画的在上面 —— 探测点必须**同时落在两个框里**，
// 否则测的其实是"哪个框包含这个点"，不是层级顺序
const under = { id: 'u', kind: 'rect', page: 1, x: 100, y: 700, w: 200, h: 50 };
const over = { id: 'o', kind: 'rect', page: 1, x: 120, y: 710, w: 50, h: 20 };
const op = pdfToNorm(over.x, over.y, over.w, over.h, W, H);
const both = { x: op.x + op.w / 2, y: op.y + op.h / 2 };
check('探测点确实同时在两个框内（前提）', (() => {
  const up = pdfToNorm(under.x, under.y, under.w, under.h, W, H);
  return both.x >= up.x && both.x <= up.x + up.w &&
         both.y >= up.y && both.y <= up.y + up.h;
})());
check('重叠时命中后画的（数组靠后）',
  hitTestNorm([under, over], 1, both.x, both.y, getSize) === over);
check('顺序反过来则命中先画的（层级由数组顺序决定）',
  hitTestNorm([over, under], 1, both.x, both.y, getSize) === under);

// 直线：按点到线段距离判定
const line = { id: 'l', kind: 'line', page: 1, x: 100, y: 100, x2: 300, y2: 100 };
const lp = pdfToNorm(100, 100, 0, 0, W, H);
const lp2 = pdfToNorm(300, 100, 0, 0, W, H);
check('靠近直线 → 命中', hitTestNorm([line], 1,
  (lp.x + lp2.x) / 2, lp.y + HIT_LINE_TOL / 2, getSize) === line);
check('离直线远 → 不命中', hitTestNorm([line], 1,
  (lp.x + lp2.x) / 2, lp.y + HIT_LINE_TOL * 3, getSize) === null);

// 涂黑区域也要能被选中（选择工具要能删掉它）
const red = { id: 'rd', kind: 'redact', page: 1, x: 100, y: 600, w: 100, h: 20 };
const dp = pdfToNorm(red.x, red.y, red.w, red.h, W, H);
check('涂黑区域可被命中（能选中、能删）',
  hitTestNorm([red], 1, dp.x + 0.01, dp.y + 0.001, getSize) === red);

// ★ 文字对象：数据里 w/h=0，靠渲染时量出来的 _box 才能命中。
//   这条锁死「文字写得进去但点不中」那个老问题。
const txt = { id: 't', kind: 'text', page: 1, x: 100, y: 700, w: 0, h: 0 };
check('文字没有 _box 时命中不了（旧行为，避免误判）',
  hitTestNorm([txt], 1, 0.2, 0.15, getSize) === null);
const txtBoxed = { ...txt, _box: { x: 0.16, y: 0.10, w: 0.2, h: 0.04 } };
check('文字有 _box 时能命中',
  hitTestNorm([txtBoxed], 1, 0.2, 0.12, getSize) === txtBoxed);
check('文字 _box 之外不命中',
  hitTestNorm([txtBoxed], 1, 0.5, 0.12, getSize) === null);

// 空列表
check('空对象列表返回 null',
  hitTestNorm([], 1, 0.5, 0.5, getSize) === null);

/* --------------------------------------------------------------------- */
console.log('\n=== 5. 导出负载（含安全不变量）===');
/* --------------------------------------------------------------------- */
const objs = [
  { id: 'a', kind: 'rect', page: 1, x: 1, y: 2, w: 3, h: 4, _norm: { x: 0 } },
  { id: 'b', kind: 'text', page: 1, x: 5, y: 6, w: 0, h: 0, text: 'hi',
    _box: { x: 0 } },
  { id: 'c', kind: 'redact', page: 1, x: 7, y: 8, w: 9, h: 10 },
  { id: 'd', kind: 'text', page: 2, x: 1, y: 1, w: 0, h: 0 },
];
const pl = payloadObjects(objs);
check('★ 涂黑对象被排除（不能混进普通导出）',
  pl.every(o => o.kind !== 'redact') && pl.length === 3,
  `${pl.length} 个`);
check('内部缓存字段被清掉（_norm/_box）',
  pl.every(o => o._norm === undefined && o._box === undefined));
check('本地 id 被清掉', pl.every(o => o.id === undefined));
const t = pl.find(o => o.kind === 'text' && o.text === 'hi');
check('文字对象保留 text/size/font', !!t && t.size === 12 &&
  t.font === 'Helvetica', JSON.stringify(t));
const t2 = pl.find(o => o.kind === 'text' && o.page === 2);
check('文字缺省值补齐（空文本 → ""）', !!t2 && t2.text === '');
check('非文字对象不带 text 字段',
  pl.find(o => o.kind === 'rect').text === undefined);

/* --------------------------------------------------------------------- */
console.log('\n=== 6. 有没有编辑内容 ===');
/* --------------------------------------------------------------------- */
check('无对象无页面操作 → false', hasAnyEdit({ objects: [], pages: {} }) === false);
check('有对象 → true',
  hasAnyEdit({ objects: [{}], pages: {} }) === true);
check('删页 → true', hasAnyEdit({ objects: [], pages: { delete: [0] } }) === true);
check('插页 → true',
  hasAnyEdit({ objects: [], pages: { insert: [{ at: 0 }] } }) === true);
check('重排 → true',
  hasAnyEdit({ objects: [], pages: { order: [1, 0] } }) === true);
check('旋转 → true',
  hasAnyEdit({ objects: [], pages: { rotate: { 0: 90 } } }) === true);
check('旋转空对象 → false',
  hasAnyEdit({ objects: [], pages: { rotate: {} } }) === false);

/* --------------------------------------------------------------------- */
console.log('\n=== 7. 拖动位移（y 轴符号是重点）===');
/* --------------------------------------------------------------------- */
// ★ 屏幕往下拖 100px，PDF 里 y 应该**减小** —— 轴方向相反
const d1 = dragDeltaToPdf(0, 100, 800, 1000, 595, 842);
check('★ 屏幕向下拖 → PDF y 减小（轴方向相反）', d1.dy < 0, String(d1.dy));
check('dy 大小 = (100/1000)×842', near(d1.dy, -84.2, 1e-9), String(d1.dy));
check('屏幕向上拖 → PDF y 增大',
  dragDeltaToPdf(0, -100, 800, 1000, 595, 842).dy > 0);
check('水平方向不变（屏幕右 = PDF x 增大）',
  near(dragDeltaToPdf(50, 0, 800, 1000, 595, 842).dx, (50 / 800) * 595));

// 每帧都从原始位置算 → 位移不会累积
const orig = { x: 100, y: 200 };
const mv1 = movedPosition(orig, dragDeltaToPdf(10, 10, 800, 1000, 595, 842));
const mv2 = movedPosition(orig, dragDeltaToPdf(20, 20, 800, 1000, 595, 842));
check('从原始位置算位移 → 正好 2 倍（不是累积）',
  near(mv2.x - orig.x, 2 * (mv1.x - orig.x), 1e-9),
  `${mv2.x - orig.x} vs ${2 * (mv1.x - orig.x)}`);
check('原始对象没有被就地修改（纯函数）',
  orig.x === 100 && orig.y === 200);
check('零位移等于原位置',
  (() => {
    const m = movedPosition(orig, { dx: 0, dy: 0 });
    return m.x === orig.x && m.y === orig.y;
  })());

// 线对象：两个端点一起动
const line0 = { x: 10, y: 20, x2: 30, y2: 40 };
const lm = movedPosition(line0, { dx: 5, dy: -7 });
check('线对象两端点一起移动',
  lm.x === 15 && lm.y === 13 && lm.x2 === 35 && lm.y2 === 33,
  JSON.stringify(lm));

// 矩形/文字没有 x2 → 不该凭空多出端点字段
const rm = movedPosition({ x: 1, y: 2 }, { dx: 3, dy: 4 });
check('没有 x2 的对象不产生 x2 字段', rm.x2 === undefined,
  JSON.stringify(rm));

/* --------------------------------------------------------------------- */
console.log('\n=== 8. 文字盒必须按内容自适应（不是 1px）===');
/* --------------------------------------------------------------------- */
// 这是本项目真实踩过的坑：数据层里文字只有**锚点**（w/h = 0，对应 PDF 的
// Td + Tj）。渲染时若按矩形给固定宽高，会得到 1×1 的盒子，再被
// `.ed-text` 的 overflow 一裁 —— **插入了却看不见**，而且不报错。
//
// 这条不变量几何纯函数守不住，只能断言"我们没把宽高写成 1px"。
const fakeEl = { style: {} };
const ret = makeTextBoxAuto(fakeEl);
check('★ 宽度是 auto（不能是 1px）', fakeEl.style.width === 'auto',
  String(fakeEl.style.width));
check('高度是 auto（不能是 1px）', fakeEl.style.height === 'auto',
  String(fakeEl.style.height));
check('★ overflow 不能是 hidden（否则整段被裁）',
  fakeEl.style.overflow === 'visible', String(fakeEl.style.overflow));
check('不自动折行（导出从不折行，预览要对齐）',
  fakeEl.style.whiteSpace === 'pre', String(fakeEl.style.whiteSpace));
check('返回同一个元素（便于就地调用）', ret === fakeEl);

/* --------------------------------------------------------------------- */
console.log('\n' + '='.repeat(62));
console.log(`  通过 ${PASS}，失败 ${FAIL}`);
if (FAILED.length) {
  console.log('  失败项：');
  FAILED.forEach(n => console.log('    - ' + n));
}
console.log('='.repeat(62));
process.exit(FAIL ? 1 : 0);
