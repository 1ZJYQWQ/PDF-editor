





export function screenToNorm(clientX, clientY, left, top, rw, rh) {
  return {
    x: (clientX - left) / (rw || 1),
    y: (clientY - top) / (rh || 1),
    w: 0, h: 0,
  };
}


export function normToScreen(nx, ny, nw, nh, rw, rh) {
  return {
    left: nx * rw,
    top: ny * rh,
    width: nw * rw,
    height: nh * rh,
  };
}


export function normToPdf(nx, ny, nw, nh, pw, ph) {
  return {
    x: nx * pw,
    y: ph * (1 - ny - nh),        
    w: nw * pw,
    h: nh * ph,
  };
}


export function pdfToNorm(x, y, w, h, pw, ph) {
  if (!pw || !ph) return { x: 0, y: 0, w: 0, h: 0 };
  return {
    x: x / pw,
    y: 1 - (y + h) / ph,
    w: w / pw,
    h: h / ph,
  };
}


export function distToSegment(px, py, ax, ay, bx, by) {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  if (len2 < 1e-9) return Math.hypot(px - ax, py - ay);
  let t = ((px - ax) * dx + (py - ay) * dy) / len2;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}


export const HIT_LINE_TOL = 0.012;



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



export function payloadObjects(objects) {
  return objects
    .filter(o => o.kind !== 'redact' && o.kind !== 'replace')
    .map(o => {
      const out = { ...o };
      
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


export function hasAnyEdit(payload) {
  if (payload.objects && payload.objects.length > 0) return true;
  const p = payload.pages || {};
  if (p.delete && p.delete.length) return true;
  if (p.insert && p.insert.length) return true;
  if (p.order) return true;
  if (Object.keys(p.rotate || {}).length) return true;
  return false;
}



export function dragDeltaToPdf(dxScreen, dyScreen, rw, rh, pw, ph) {
  return {
    dx: (dxScreen / (rw || 1)) * pw,
    dy: -(dyScreen / (rh || 1)) * ph,
  };
}



export function movedPosition(orig, d) {
  const out = { x: orig.x + d.dx, y: orig.y + d.dy };
  if (orig.x2 !== undefined && orig.x2 !== null) {
    out.x2 = orig.x2 + d.dx;
    out.y2 = orig.y2 + d.dy;
  }
  return out;
}



export function resizedRect(orig, corner, d) {
  const MIN = 6;
  const east = corner.indexOf('e') >= 0;    
  const north = corner.indexOf('n') >= 0;   

  let x = orig.x, y = orig.y, w = orig.w, h = orig.h;

  if (east) {
    w = w + d.dx;                 
  } else {
    x = x + d.dx;                 
    w = w - d.dx;
  }
  if (north) {
    h = h + d.dy;                 
  } else {
    y = y + d.dy;                 
    h = h - d.dy;
  }

  
  if (w < MIN) {
    w = MIN;
    if (!east) x = orig.x + orig.w - MIN;    
  }
  if (h < MIN) {
    h = MIN;
    if (!north) y = orig.y + orig.h - MIN;   
  }
  return { x, y, w, h };
}



export function makeTextBoxAuto(el) {
  el.style.width = 'auto';
  el.style.height = 'auto';
  el.style.overflow = 'visible';
  el.style.whiteSpace = 'pre';
  return el;
}



export function wrapText(text, measure, maxWidth) {
  const src = String(text == null ? '' : text);
  if (!(maxWidth > 0) || typeof measure !== 'function') return src;
  const out = [];
  for (const para of src.split('\n')) {
    if (para === '') { out.push(''); continue; }
    let line = '';
    for (const ch of para) {
      if (line === '' || measure(line + ch) <= maxWidth) {
        line += ch;
        continue;
      }
      const sp = line.lastIndexOf(' ');
      if (sp > 0) {
        out.push(line.slice(0, sp));
        line = line.slice(sp + 1) + ch;
      } else {
        out.push(line);
        line = ch;
      }
    }
    out.push(line);
  }
  return out.join('\n');
}



export function paginateText(lines, opts) {
  const { page, firstBaseline, pageW, pageH, size } = opts;
  const marginTop = opts.marginTop != null ? opts.marginTop : Math.max(18, pageH * 0.04);
  const marginBot = opts.marginBottom != null ? opts.marginBottom : Math.max(30, pageH * 0.08);
  const lineH = size * 1.25;
  const asc = size * 0.80;
  const desc = size * 0.20;
  const topMost = (pageH - marginTop) - asc;   
  const botMost = marginBot + desc;            
  const perPage = Math.max(1, Math.floor((topMost - botMost) / lineH) + 1);

  const arr = (lines && lines.length) ? lines : [''];
  const out = [];
  let i = 0, pageNo = page, firstPage = true;
  while (i < arr.length) {
    const remaining = arr.length - i;
    let fb = firstPage ? firstBaseline : topMost;
    if (!(fb <= topMost)) fb = topMost;
    if (fb < botMost) fb = botMost;
    let take = Math.floor((fb - botMost) / lineH) + 1;
    if (take < 1) take = 1;
    if (take > perPage) take = perPage;
    if (take > remaining) take = remaining;
    out.push({ page: pageNo, y: fb, lines: arr.slice(i, i + take) });
    i += take;
    firstPage = false;
    pageNo += 1;
  }
  return out;
}
