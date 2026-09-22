/* ==========================================================================
   纯几何与数据变换 —— 不依赖 state / DOM / pdf.js

   为什么单独成文件
     前端原来所有几何计算都和 state、DOM 缠在一起，于是：
       ① 没法在 node 里单测 —— core.js 顶部 import 了 pdf.js（浏览器包），
          一 import 就把整条链拖进来
       ② 坐标换算这种最容易写错的地方，只能靠肉眼验

     这里只放**纯函数**：不读全局、不碰 DOM、同样的输入必然同样的结果。
     所以可以用 node 直接跑测试（见 ../webtest.mjs）。
     `edit.js` 里保留同名函数，但只负责读 state/DOM 再转调这里
     —— **调用点一处都不用改，行为完全不变**。

   ★ 关键约定：PDF 坐标原点在**左下角、y 轴向上**，屏幕原点在左上、y 轴向下。
     所以转换必须「**先翻转再乘尺寸**」：
         pdfY = pageHeight * (1 - (ny + nh))
     这是整个前端最容易写错的一行。

   注：`static/package.json` 里声明了 "type":"module"，
   好让 node 把这里的 .js 当 ES Module 读（浏览器侧不受影响）。
   ========================================================================== */

/* 屏幕坐标 -> 页面内归一化坐标（0~1）
   用归一化而不是绝对 PDF 点，好处是**缩放后完全不用改数据**。 */
export function screenToNorm(clientX, clientY, left, top, rw, rh) {
  return {
    x: (clientX - left) / (rw || 1),
    y: (clientY - top) / (rh || 1),
    w: 0, h: 0,
  };
}

/* 归一化 -> 屏幕像素（相对页面左上角） */
export function normToScreen(nx, ny, nw, nh, rw, rh) {
  return {
    left: nx * rw,
    top: ny * rh,
    width: nw * rw,
    height: nh * rh,
  };
}

/* 归一化 -> PDF 点 */
export function normToPdf(nx, ny, nw, nh, pw, ph) {
  return {
    x: nx * pw,
    y: ph * (1 - ny - nh),        // ★ 先翻转，再乘尺寸
    w: nw * pw,
    h: nh * ph,
  };
}

/* PDF 点 -> 归一化 */
export function pdfToNorm(x, y, w, h, pw, ph) {
  if (!pw || !ph) return { x: 0, y: 0, w: 0, h: 0 };
  return {
    x: x / pw,
    y: 1 - (y + h) / ph,
    w: w / pw,
    h: h / ph,
  };
}

/* 点到线段的距离（命中直线用） */
export function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  if (len2 < 1e-9) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

/* 线的命中容差（归一化单位） */
export const HIT_LINE_TOL = 0.012;

/*
  命中测试：从后往前找（后画的在上面）。

  nx/ny 是**页面内归一化坐标**；getSize(pageNum) 返回该页的 PDF 尺寸。
  返回命中的对象，没有则 null。

  ★ 文字对象的数据里 w/h = 0 —— 它是「锚点」不是矩形（对应 PDF 的 Td）。
    所以它的可点范围由**渲染时量出来的盒子** `o._box`（归一化）给出，
    见 edit.js 的 paintPageEdits()。没有 _box 时文字点不中（与旧行为一致）。
*/
export function hitTestNorm(objects, pageNum, nx, ny, getSize) {
  const mine = objects.filter(o => o.page === pageNum);
  for (let i = mine.length - 1; i >= 0; i--) {
    const o = mine[i];
    const { width, height } = getSize(o.page);

    if (o.kind === 'line') {
      const a = pdfToNorm(o.x, o.y, 0, 0, width, height);
      const b = pdfToNorm(o.x2, o.y2, 0, 0, width, height);
      if (distToSegment(nx, ny, a.x, a.y, b.x, b.y) < HIT_LINE_TOL) return o;
      continue;
    }

    if (o.kind === 'text') {
      const b = o._box;
      if (b && nx >= b.x && nx <= b.x + b.w &&
          ny >= b.y && ny <= b.y + b.h) {
        return o;
      }
      continue;
    }

    const p = pdfToNorm(o.x, o.y, o.w, o.h, width, height);
    if (nx >= p.x && nx <= p.x + p.w && ny >= p.y && ny <= p.y + p.h) {
      return o;
    }
  }
  return null;
}

/*
  导出负载里的对象列表（纯部分：不读全局状态）。

  ★ **涂黑对象不参与普通导出。**
    普通导出是叠加式的，删不掉底层文字；让涂黑框走这条路，用户会拿到
    「看着是黑块、文字其实还在」的文件 —— 正是要避免的涂黑事故。
    涂黑必须走 /api/redact。
*/
export function payloadObjects(objects) {
  return objects
    .filter(o => o.kind !== 'redact')
    .map(o => {
      const out = { ...o };
      // 内部字段不下发：_norm/_box 是渲染缓存，id 是本地标识
      delete out._norm;
      delete out._box;
      delete out.id;
      if (o.kind === 'text') {
        out.text = o.text || '';
        out.size = o.size || 12;
        out.font = o.font || 'Helvetica';
      }
      return out;
    });
}

/* 有没有实质编辑内容（用于「还没有任何编辑内容」的提示） */
export function hasAnyEdit(payload) {
  if (payload.objects && payload.objects.length > 0) return true;
  const p = payload.pages || {};
  if (p.delete && p.delete.length) return true;
  if (p.insert && p.insert.length) return true;
  if (p.order) return true;
  if (Object.keys(p.rotate || {}).length) return true;
  return false;
}

/*
  拖动位移换算：**屏幕像素位移 → PDF 点的位移量**。

  ★ y 轴方向相反：屏幕往下拖（dyScreen > 0）在 PDF 里是 y **减小**。
    这是整个拖动逻辑里最容易写错的一个符号，所以独立成纯函数并单测。

  rw/rh = 页面在屏幕上的尺寸；pw/ph = 页面的 PDF 尺寸。
*/
export function dragDeltaToPdf(dxScreen, dyScreen, rw, rh, pw, ph) {
  return {
    dx: (dxScreen / (rw || 1)) * pw,
    dy: -(dyScreen / (rh || 1)) * ph,
  };
}

/*
  把位移应用到对象的原始坐标上，返回新坐标。

  传 orig 而不是就地累加：**每一帧都从原始位置算**，
  这样不会有累积误差，也不会因为中途一次事件丢失而漂移。
  线对象（有 x2/y2）的两个端点一起动。
*/
export function movedPosition(orig, d) {
  const out = { x: orig.x + d.dx, y: orig.y + d.dy };
  if (orig.x2 !== undefined && orig.x2 !== null) {
    out.x2 = orig.x2 + d.dx;
    out.y2 = orig.y2 + d.dy;
  }
  return out;
}

/*
  ★ 文字对象的盒子必须**按内容自适应**，绝不能当成矩形给固定宽高。

  为什么单独抽成一个函数（而不是就地写几行样式）：
    数据层里文字只有**锚点**（w/h = 0，对应 PDF 的 Td + Tj），不是矩形。
    渲染时先按矩形算宽高就会得到 1×1 的盒子，再叠加 `.ed-text` 的
    `overflow: hidden`，整段文字被裁得一干二净 ——
    表现是「对象建出来了（面板提示有未保存的改动），页面上却一片空白」。
    这个 bug 真实发生过，而且因为**没有报错**，查了很久。

    抽成函数是为了**可测**：这条不变量靠几何纯函数守不住，
    只能在测试里断言"没把宽高写成 1px"。谁改回去，测试立刻红。

  white-space 用 pre 而不是 pre-wrap：
    导出时换行是**拆成多个 Tj**（按显式 \n 拆），从不自动折行；
    预览若自动折行，长文本的观感就和导出对不上。
*/
export function makeTextBoxAuto(el) {
  el.style.width = 'auto';
  el.style.height = 'auto';
  el.style.overflow = 'visible';
  el.style.whiteSpace = 'pre';
  return el;
}
