/* 纯 SVG 图表：折线/面积/柱状，数据全部来自接口（支持 null 断线）
   统一约定：
   · viewBox 宽 1280（全宽卡）/ 620（半宽卡），宽度 100% 自适应，高度由 viewBox 比例推导
   · 网格线最多 4 条、极暗，0 线略亮；轴文字 11px；X 轴标签最多 10 个
   · 配色：实测=绿 #35ED7E，模型/预测=蓝紫 #6C73F5，超发=红 / 欠发=绿（收益轴已移除）
   · 多序列图（multiLineChart）按量纲分面板（小倍数图）：同量纲共轴，混量纲各占
     一块面板、各自标刻度——不同单位的数值不做无意义的同轴归一化
   · 所有图表共用同一套悬停十字线 + HTML 提示气泡 */
const Charts = (() => {
  const ns = (tag, attrs, text) => {
    const a = Object.entries(attrs)
      .filter(([, v]) => v !== null && v !== undefined)
      .map(([k, v]) => k + '="' + v + '"').join(' ');
    if (text === undefined) return '<' + tag + ' ' + a + '/>';   // SVG 必须自闭合
    return '<' + tag + ' ' + a + '>' + text + '</' + tag + '>';
  };
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  const C = {
    grid: '#232852', axis: '#39407F', label: '#8B90A8',
    actual: '#35ED7E', model: '#6C73F5', rev: '#F5C542',
    up: '#35ED7E', down: '#F0655A', cross: '#7C86FF'
  };
  const FS = 11;

  /* Y 轴取整刻度：从常用数列挑最细的一档，保证刻度线 ≤ 4 条 */
  function niceTicks(maxVal, target = 4) {
    const steps = [0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 250, 500,
                   1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000];
    const raw = Math.max(maxVal, 0.05);
    let step = steps[steps.length - 1];
    for (const s of steps) {
      if (Math.ceil(raw / s - 1e-9) <= target) { step = s; break; }
    }
    const ymax = step * Math.ceil(raw / step - 1e-9);
    const ticks = [];
    for (let v = 0; v <= ymax + 1e-9; v += step) ticks.push(+v.toFixed(4));
    return { ymax: +ymax.toFixed(4), ticks };
  }

  /* 轴刻度文字：量级大时用 k 简写，避免长串数字挤占画布 */
  const tickFmt = (v, ymax) => (v === 0 ? '0'
    : (ymax >= 10000 ? (+ (v / 1000).toFixed(1)) + 'k' : String(+v.toFixed(2))));
  const numFmt = v => (Math.abs(v) >= 1000 ? Math.round(v).toLocaleString('zh-CN')
    : (Math.abs(v) >= 100 ? String(Math.round(v)) : String(+v.toFixed(2))));

  /* X 轴标签抽样：最多 10 个，末点必显 */
  function xIdx(n, count) {
    const step = Math.max(1, Math.ceil(n / (count || 10)));
    const out = [];
    for (let i = 0; i < n; i += step) out.push(i);
    if (out[out.length - 1] !== n - 1) out.push(n - 1);
    return out;
  }

  /* 网格 + 两侧轴文字（noX=true 时不画 X 标签，用于多面板布局的上层） */
  function drawAxis(L, m, ticks, ymax, pts, xs, px, noX) {
    for (const v of ticks) {
      const y = m.Y1 - v * (m.Y1 - m.Y0) / ymax;
      const zero = v === 0;
      L.push(ns('line', { x1: m.X0, y1: y.toFixed(1), x2: m.X1, y2: y.toFixed(1),
        stroke: zero ? C.axis : C.grid, 'stroke-width': zero ? 1.2 : 1,
        'stroke-dasharray': zero ? null : '4 6', 'stroke-opacity': zero ? .85 : .5 }));
      L.push(ns('text', { x: m.X0 - 10, y: (y + 4).toFixed(1), fill: C.label,
        'font-size': FS, 'text-anchor': 'end' }, tickFmt(v, ymax)));
    }
    if (noX) return;
    for (const i of xs) {
      const p = pts[i] || {};
      L.push(ns('text', { x: px(i).toFixed(1), y: m.Y1 + 22, fill: p.today ? C.actual : C.label,
        'font-size': FS, 'text-anchor': 'middle', 'font-weight': p.today ? 600 : 400 },
        String(p.t) + (p.today ? ' 今' : '')));
    }
  }

  /* ---------- 悬停：十字线 + 气泡 ---------- */
  function buildHits(pts, px, series) {
    const n = pts.length;
    const half = n > 1 ? Math.abs(px(1) - px(0)) / 2 : 60;
    return pts.map((p, i) => {
      const cx = px(i);
      return {
        cx: cx, x0: cx - half, x1: cx + half,
        title: String(p.t == null ? '' : p.t) + (p.today ? '（今天）' : ''),
        note: String(p.note || ''),
        ys: series.map(s => {
          const v = s.get(p);
          const nm = (typeof s.name === 'function') ? s.name(p) : (s.name || '');
          return { y: v == null ? null : s.py(v), color: s.color,
                   text: v == null ? '' : (nm ? nm + ' ' : '') + numFmt(v) + (s.unit || '') };
        })
      };
    });
  }

  function tipLayer(m, hits) {
    const L = [];
    hits.forEach((h, i) => {
      L.push(ns('rect', { x: h.x0.toFixed(1), y: m.Y0, width: Math.max(1, h.x1 - h.x0).toFixed(1),
        height: (m.Y1 - m.Y0).toFixed(1), fill: 'transparent', 'data-i': String(i) }));
    });
    const dots = ((hits[0] && hits[0].ys) || []).map(() =>
      ns('circle', { class: 'cxdot', cx: -99, cy: -99, r: 4, fill: '#fff',
        stroke: '#0A0D3A', 'stroke-width': 2, opacity: 0 })).join('');
    L.push('<g class="cxg" opacity="0">' +
      ns('line', { class: 'cxline', x1: -99, y1: m.Y0, x2: -99, y2: m.Y1,
        stroke: C.cross, 'stroke-width': 1, 'stroke-dasharray': '4 4' }) + dots + '</g>');
    return L.join('');
  }

  function toLocal(el, ev) {
    try {
      const ctm = el.getScreenCTM && el.getScreenCTM();
      if (!ctm) return null;
      const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(ctm.inverse());
      return { x: p.x, y: p.y };
    } catch (e) { return null; }
  }

  function showTip(el, i) {
    const hits = el.__hits || [];
    const h = hits[i];
    if (!h) return;
    const g = el.querySelector && el.querySelector('.cxg');
    if (g) {
      g.setAttribute('opacity', '1');
      const ln = g.querySelector('.cxline');
      if (ln) { ln.setAttribute('x1', h.cx.toFixed(1)); ln.setAttribute('x2', h.cx.toFixed(1)); }
      const dots = g.querySelectorAll ? g.querySelectorAll('.cxdot') : [];
      for (let k = 0; k < dots.length; k++) {
        const s = h.ys[k], c = dots[k];
        if (!s || s.y == null) { c.setAttribute('opacity', '0'); continue; }
        c.setAttribute('cx', h.cx.toFixed(1));
        c.setAttribute('cy', s.y.toFixed(1));
        c.setAttribute('fill', s.color);
        c.setAttribute('opacity', '1');
      }
    }
    const host = el.parentNode;
    if (!host || !host.querySelector) return;
    let box = host.querySelector('.tip');
    if (!box) {
      if (typeof document === 'undefined' || !document.createElement) return;
      box = document.createElement('div');
      box.className = 'tip';
      host.appendChild(box);
    }
    box.innerHTML = (h.title ? '<b>' + esc(h.title) + '</b>' : '') +
      h.ys.filter(s => s.text).map(s => '<span><i style="background:' + s.color + '"></i>' + esc(s.text) + '</span>').join('') +
      (h.note ? '<em>' + esc(h.note) + '</em>' : '');
    box.style.display = 'block';
    const r = el.getBoundingClientRect ? el.getBoundingClientRect() : { width: 0, height: 0 };
    const sc = r.width / (el.__W || 1);
    let left = h.cx * sc + 18;
    if (left + 170 > r.width) left = h.cx * sc - 178;
    if (left < 2) left = 2;
    const ys = h.ys.filter(s => s.y != null).map(s => s.y);
    const top = (ys.length ? Math.min.apply(null, ys) * sc : 20);
    box.style.left = left + 'px';
    box.style.top = Math.max(2, top - 6) + 'px';
  }

  function hideTip(el) {
    const g = el.querySelector && el.querySelector('.cxg');
    if (g) g.setAttribute('opacity', '0');
    const host = el.parentNode;
    const box = host && host.querySelector && host.querySelector('.tip');
    if (box) box.style.display = 'none';
  }

  function bindTip(el, W, H, hits) {
    el.__W = W; el.__H = H; el.__hits = hits;
    if (el.__tipBound || typeof el.addEventListener !== 'function') return;
    el.__tipBound = 1;
    el.addEventListener('mousemove', ev => {
      const p = toLocal(el, ev);
      if (!p) return;
      let bi = -1, bd = Infinity;
      (el.__hits || []).forEach((h, i) => { const d = Math.abs(p.x - h.cx); if (d < bd) { bd = d; bi = i; } });
      if (bi >= 0) showTip(el, bi);
    });
    el.addEventListener('mouseleave', () => hideTip(el));
  }

  function finish(el, W, H, m, L, hits) {
    if (hits && hits.length) L.push(tipLayer(m, hits));
    el.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    el.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    el.setAttribute('width', '100%');
    if (typeof el.removeAttribute === 'function') el.removeAttribute('height');
    el.innerHTML = L.join('');
    if (hits && hits.length) bindTip(el, W, H, hits);
  }

  const empty = (el, msg) => {
    el.setAttribute('viewBox', '0 0 1280 160');
    el.setAttribute('width', '100%');
    if (typeof el.removeAttribute === 'function') el.removeAttribute('height');
    el.innerHTML = '<text x="640" y="85" fill="#8B90A8" font-size="14" text-anchor="middle">' + esc(msg) + '</text>';
  };

  /* 多序列折线（同图追加式）：复选框勾选 = 在当前图里追加一条线，取消 = 移除。
     全部线条共用同一绘图区、同一条 X 时间轴与同一套图例（复选框色点即图例）；
     第 1 种量纲走左轴（带刻度），第 2 种量纲走右轴（带刻度、琥珀色区分），
     更多量纲沿用右轴刻度、悬停提示显示真实值——绝不为单条线新开独立图表。
     不同颜色区分线条；dash 序列表示模型/预报。 */
  function multiLineChart(el, opt) {
    const pts = opt.points || [];
    const srs = (opt.series || []).filter(s =>
      s && pts.some(p => p.vals && p.vals[s.key] != null));
    if (!pts.length || !srs.length) { empty(el, opt.emptyMsg || '暂无数据'); return; }
    const n = pts.length;
    const W = opt.w || 1280;
    const m = { X0: 66, X1: W - 64 };
    const px = i => m.X0 + (n === 1 ? (m.X1 - m.X0) / 2 : i * (m.X1 - m.X0) / (n - 1));

    // 量纲 → 轴：第 1 种左轴、第 2 种右轴、其余沿用右轴（不做无刻度归一化）
    const units = [];
    srs.forEach(s => { const u = (s.unit || '').trim(); if (units.indexOf(u) < 0) units.push(u); });
    const uL = units[0];
    const uR = units.length > 1 ? units[1] : null;
    const isRight = s => !!uR && (s.unit || '').trim() !== uL;
    const srsR = srs.filter(isRight);

    const XLAB = 26;
    const ph = opt.h ? opt.h - 78 : 328;
    const H = ph + XLAB + 16;
    const Y0 = 16, Y1 = Y0 + ph;
    m.Y0 = Y0; m.Y1 = Y1;

    const valsOf = list => {
      const v = [];
      list.forEach(s => pts.forEach(pt => { const x = pt.vals[s.key]; if (x != null) v.push(x); }));
      return v;
    };
    const ntL = niceTicks(Math.max.apply(null, valsOf(srs.filter(s => !isRight(s))).concat([1])) * 1.08, 4);
    const pyL = v => Y1 - v * (Y1 - Y0) / ntL.ymax;
    let ntR = null, pyR = null;
    if (srsR.length) {
      ntR = niceTicks(Math.max.apply(null, valsOf(srsR).concat([1])) * 1.08, 3);
      pyR = v => Y1 - v * (Y1 - Y0) / ntR.ymax;
    }

    const L = [];
    // 左轴网格 + 刻度 + 底部 X 标签；右轴只放刻度文字（琥珀色），不加网格避免打架
    drawAxis(L, m, ntL.ticks, ntL.ymax, pts, xIdx(n, opt.labelCount || 12), px);
    if (ntR) {
      ntR.ticks.forEach(v => {
        const y = Y1 - v * (Y1 - Y0) / ntR.ymax;
        L.push(ns('text', { x: m.X1 + 10, y: (y + 4).toFixed(1), fill: '#D9A93F',
          'font-size': FS, 'text-anchor': 'start' }, tickFmt(v, ntR.ymax)));
      });
      // 顶部量纲标注：一眼分清左右轴各是什么单位（单量纲时不标，单位在图例里）
      L.push(ns('text', { x: m.X0 + 4, y: (Y0 - 4).toFixed(1), fill: C.label,
        'font-size': 10, 'font-style': 'italic' }, '左轴 ' + (uL || '')));
      L.push(ns('text', { x: m.X1 - 4, y: (Y0 - 4).toFixed(1), fill: '#D9A93F',
        'font-size': 10, 'font-style': 'italic', 'text-anchor': 'end' },
        '右轴 ' + uR + (units.length > 2 ? ' 等' : '')));
    }

    // 各序列：null 断线，单点画圆；dash 序列画虚线（模型/预报）——全部追加进同一绘图区
    srs.forEach(s => {
      const py = isRight(s) ? pyR : pyL;
      let seg = [];
      const draw = () => {
        if (seg.length > 1) {
          const at = { points: seg.join(' '), fill: 'none', stroke: s.color || C.model,
            'stroke-width': s.width || 2.2, 'stroke-linejoin': 'round' };
          if (s.dash) { at['stroke-dasharray'] = '7 5'; at['stroke-opacity'] = .9; }
          L.push(ns('polyline', at));
        } else if (seg.length === 1) {
          const c = seg[0].split(',');
          L.push(ns('circle', { cx: c[0], cy: c[1], r: 3.5, fill: s.color || C.model,
            'fill-opacity': s.dash ? .7 : 1 }));
        }
        seg = [];
      };
      pts.forEach((p, i) => {
        const v = p.vals[s.key];
        if (v == null) { draw(); return; }
        seg.push(px(i).toFixed(1) + ',' + py(v).toFixed(1));
      });
      draw();
    });

    // 「今天」竖线：贯穿整个绘图区，历史/未来的分界
    pts.forEach((p, i) => {
      if (!p.today) return;
      const x = px(i).toFixed(1);
      L.push(ns('line', { x1: x, y1: Y0.toFixed(1), x2: x, y2: Y1.toFixed(1),
        stroke: C.cross, 'stroke-width': 1, 'stroke-dasharray': '3 5', 'stroke-opacity': .5 }));
    });

    const hits = buildHits(pts, px, srs.map(s => ({
      get: p => p.vals[s.key], py: (isRight(s) ? pyR : pyL), color: s.color || C.model,
      name: (typeof s.name === 'function') ? s.name : (s.name || s.key), unit: s.unit || ''
    })));
    finish(el, W, H, m, L, hits);
  }

  /* 当日对照：实测（实线） vs 模型基线（虚线+面积），主轴 kW；
     可选辐照度——同图追加一条黄色细线，走右侧副轴（W/m²，琥珀色刻度），
     与功率共用同一绘图区与 X 轴，不新开独立图表。
     opt.visible = { actual, forecast, ghi } 控制各曲线显隐（总览页复选框）。 */
  function compareChart(el, opt) {
    const pts = opt.points || [];
    if (!pts.length) { empty(el, '暂无数据'); return; }
    const vis = opt.visible || {};
    const showA = vis.actual !== false, showF = vis.forecast !== false;
    const hasB = pts.some(p => p.baseline_kw != null);
    const showB = !!vis.baseline && hasB;
    const hasG = pts.some(p => p.ghi_w != null);
    const showG = !!vis.ghi && hasG;
    if (!showA && !showF && !showB && !showG) { empty(el, '请至少勾选一条曲线'); return; }

    const ca = opt.colors.actual || C.actual, cf = opt.colors.forecast || C.model;
    const cb = opt.colors.baseline || '#A5A9C4';
    const cg = '#F5C542';
    const vals = [];
    if (showA || showF || showB) pts.forEach(p => {
      if (showA && p.actual_kw != null) vals.push(p.actual_kw);
      if (showF && p.forecast_kw != null) vals.push(p.forecast_kw);
      if (showB && p.baseline_kw != null) vals.push(p.baseline_kw);
    });
    const nt = niceTicks(Math.max.apply(null, vals.concat([1])) * 1.08, 4);
    const ymax = nt.ymax, n = pts.length;
    const W = 1280, H = 380, m = { X0: 66, X1: 1216, Y0: 20, Y1: 330 };
    // 辐照副轴（右）：与功率同绘图区，独立刻度——不同量纲绝不共轴
    const gvals = pts.map(p => p.ghi_w).filter(v => v != null);
    const ng = niceTicks(Math.max.apply(null, gvals.concat([1])) * 1.1, 2);
    const px = i => m.X0 + (n === 1 ? (m.X1 - m.X0) / 2 : i * (m.X1 - m.X0) / (n - 1));
    const py = v => m.Y1 - v * (m.Y1 - m.Y0) / ymax;
    const pyg = v => m.Y1 - v * (m.Y1 - m.Y0) / ng.ymax;
    const L = [];
    drawAxis(L, m, nt.ticks, ymax, pts, xIdx(n, opt.labelCount || 12), px);
    // 轴标题：分轴同图时没有轴标题，等于让人猜"哪条线是哪个量纲"
    L.push(ns('text', { x: (m.X0 - 56), y: (m.Y0 - 6).toFixed(1), fill: C.label,
      'font-size': 10, 'font-style': 'italic' }, '功率 kW'));
    if (showG) {
      ng.ticks.forEach(v => {
        const y = m.Y1 - v * (m.Y1 - m.Y0) / ng.ymax;
        L.push(ns('text', { x: m.X1 + 10, y: (y + 4).toFixed(1), fill: '#D9A93F',
          'font-size': FS, 'text-anchor': 'start' }, tickFmt(v, ng.ymax)));
      });
      L.push(ns('text', { x: m.X1 - 4, y: (m.Y0 - 6).toFixed(1), fill: cg,
        'font-size': 10, 'font-style': 'italic', 'text-anchor': 'end' }, '右轴 W/m²'));
    }

    // 未采集时段：淡色底带（「没采到」≠「发电为 0」）
    const bd = opt.band;
    if (bd && bd.to > bd.from && n > 1) {
      const b0 = px(Math.max(0, bd.from)), b1 = px(Math.min(n - 1, bd.to));
      if (b1 - b0 > 6) {
        L.push(ns('rect', { x: b0.toFixed(1), y: m.Y0, width: (b1 - b0).toFixed(1),
          height: (m.Y1 - m.Y0).toFixed(1), fill: '#8B90A8', 'fill-opacity': .07 }));
      }
    }

    // 当日未结束时的「现在」边界：之前是已发生区（可对账），之后是预测区
    const nowI = (opt.nowHour != null && opt.nowHour >= 0 && opt.nowHour < n) ? opt.nowHour : null;

    // 预测区底纹：模型线画满全天、实测只到当前就断，不分区的话
    // 两条线的面积比会被直接读成「模型高估 X 倍」——那是画法造成的误判，不是模型错
    if (nowI != null && nowI < n - 1) {
      L.push(ns('rect', { x: px(nowI).toFixed(1), y: m.Y0,
        width: (m.X1 - px(nowI)).toFixed(1), height: (m.Y1 - m.Y0).toFixed(1),
        fill: cf, 'fill-opacity': .045 }));
      L.push(ns('text', { x: (m.X1 - 6), y: (m.Y0 + 14), fill: C.label, 'font-size': 10,
        'text-anchor': 'end', 'fill-opacity': .9 }, '未发生 · 仅预测'));
    }

    // 已发生段的「差值带」：实测与预测之间填一层淡色——一眼看出谁高、高多少，
    // 不再靠"估两条线的面积比"（那是上一版最容易读成「模型高估一倍」的地方）
    if (showA && showF) {
      const cut = (nowI != null) ? Math.min(n - 1, nowI) : n - 1;
      const top = [], bot = [];
      for (let i = 0; i <= cut; i++) {
        const a = (pts[i].value_kw != null ? pts[i].value_kw : pts[i].actual_kw);
        const f = pts[i].forecast_kw;
        if (a == null || f == null) continue;
        top.push(px(i).toFixed(1) + ',' + py(a).toFixed(1));
        bot.push(px(i).toFixed(1) + ',' + py(f).toFixed(1));
      }
      if (top.length > 1) {
        L.push(ns('polygon', { points: top.join(' ') + ' ' + bot.reverse().join(' '),
          fill: cf, 'fill-opacity': .14 }));
      }
    }

    // 预测线：已发生段实线（与实测同权重、可直接对比），未来段淡化点线（不参与对账）
    if (showF) {
      const segF = pts.map((p, i) => px(i).toFixed(1) + ',' + py(p.forecast_kw || 0).toFixed(1));
      if (segF.length > 1) {
        const cut = (nowI != null) ? Math.min(segF.length - 1, Math.max(1, nowI)) : segF.length - 1;
        const past = segF.slice(0, cut + 1);
        if (past.length > 1) {
          L.push(ns('polyline', { points: past.join(' '), fill: 'none', stroke: cf,
            'stroke-width': 2.4, 'stroke-linejoin': 'round' }));
        }
        if (cut < segF.length - 1) {
          L.push(ns('polyline', { points: segF.slice(cut).join(' '), fill: 'none', stroke: cf,
            'stroke-width': 1.5, 'stroke-dasharray': '2 5', 'stroke-opacity': .4 }));
        }
      }
    }
    // 日前基线（未修正的「承诺」）：细灰虚线，与修正后的判断线并存，绝不再共用一个名字
    if (showB) {
      let segB = [];
      const flushB = () => {
        if (segB.length > 1) L.push(ns('polyline', { points: segB.join(' '), fill: 'none',
          stroke: cb, 'stroke-width': 1.4, 'stroke-dasharray': '3 3', 'stroke-opacity': .8 }));
        segB = [];
      };
      pts.forEach((p, i) => {
        if (p.baseline_kw == null) { flushB(); return; }
        segB.push(px(i).toFixed(1) + ',' + py(p.baseline_kw).toFixed(1));
      });
      flushB();
    }
    // 实测：实线；估算：虚线 + 半透明；null 断开；单点画圆
    if (showA) {
      const segs = [];
      let cur = null;
      pts.forEach((p, i) => {
        const v = (p.value_kw != null) ? p.value_kw : p.actual_kw;
        if (v == null) { cur = null; return; }
        if (!cur || cur.est !== !!p.estimated) { cur = { est: !!p.estimated, pts: [] }; segs.push(cur); }
        cur.pts.push(px(i).toFixed(1) + ',' + py(v).toFixed(1));
      });
      segs.forEach(s => {
        const x = s.pts[0].split(',')[0];
        if (s.pts.length > 1) {
          const at = { points: s.pts.join(' '), fill: 'none', stroke: ca,
            'stroke-width': 2.8, 'stroke-linejoin': 'round' };
          if (s.est) { at['stroke-dasharray'] = '6 4'; at['stroke-opacity'] = .5; }
          L.push(ns('polyline', at));
        } else {
          L.push(ns('circle', { cx: x, cy: s.pts[0].split(',')[1], r: 4,
            fill: ca, 'fill-opacity': s.est ? .5 : 1 }));
        }
      });
    }
    // 辐照度：同图细折线（黄色，右轴刻度）
    if (showG) {
      let seg = [];
      const draw = () => {
        if (seg.length > 1) L.push(ns('polyline', { points: seg.join(' '), fill: 'none',
          stroke: cg, 'stroke-width': 1.8, 'stroke-opacity': .95 }));
        seg = [];
      };
      pts.forEach((p, i) => {
        if (p.ghi_w == null) { draw(); return; }
        seg.push(px(i).toFixed(1) + ',' + pyg(p.ghi_w).toFixed(1));
      });
      draw();
    }

    // 当前时刻竖线
    if (nowI != null) {
      const nx = px(nowI);
      L.push(ns('line', { x1: nx.toFixed(1), y1: m.Y0, x2: nx.toFixed(1), y2: m.Y1,
        stroke: C.cross, 'stroke-width': 1.2, 'stroke-dasharray': '4 4', 'stroke-opacity': .7 }));
      L.push(ns('text', { x: (nx + 6).toFixed(1), y: (m.Y0 + 14), fill: C.cross, 'font-size': 10 }, '现在'));
    }

    // 图内直接给结论：不让人用眼睛去估两条线的面积差
    if (opt.callout && opt.callout.label) {
      const c = opt.callout;
      L.push(ns('text', { x: (m.X0 + 8), y: (m.Y0 + 18), fill: c.color || ca,
        'font-size': 13, 'font-weight': 500 }, c.label));
      if (c.sub) L.push(ns('text', { x: (m.X0 + 8), y: (m.Y0 + 34), fill: C.label,
        'font-size': 10 }, c.sub));
    }

    const series = [];
    if (showA) series.push({ get: p => (p.value_kw != null ? p.value_kw : p.actual_kw), py: py,
      color: ca, name: p => (p.estimated ? '实测·估算' : (p.partial ? '实测·采样中' : '实测')), unit: ' kW' });
    if (showF) series.push({ get: p => p.forecast_kw, py: py, color: cf, name: '预测', unit: ' kW' });
    if (showB) series.push({ get: p => p.baseline_kw, py: py, color: cb, name: '日前基线', unit: ' kW' });
    if (showG) series.push({ get: p => p.ghi_w, py: pyg, color: cg,
      name: p => '辐照' + (p.ghi_src ? '·' + p.ghi_src : ''), unit: ' W/m²' });
    const hits = buildHits(pts, px, series);
    finish(el, W, H, m, L, hits);
  }

  /* 偏差回测：柱状（实测 − 预测，%），正绿负红，0 居中 + 考核容差带 */
  function deviationChart(el, opt) {
    const W = 1280, H = 270, m = { X0: 66, X1: 1216, Y0: 16, Y1: 222 };
    const pts = opt.points || [];
    if (!pts.length) { empty(el, '暂无回测数据'); return; }
    const tol = opt.tolerance_pct || 15;
    const co = opt.colors || {};
    const cu = co.up || C.up, cd = co.down || C.down;
    const nt = niceTicks(Math.max(Math.max.apply(null, pts.map(p => Math.abs(p.gap_pct || 0))), tol) * 1.15, 2);
    const half = Math.max(nt.ymax, tol), n = pts.length;
    const px = i => m.X0 + (n === 1 ? (m.X1 - m.X0) / 2 : i * (m.X1 - m.X0) / (n - 1));
    const py = v => m.Y1 - (v + half) * (m.Y1 - m.Y0) / (2 * half);
    const y0 = py(0);
    const L = [];

    // 容差带（达标带要肉眼可见，透明度过低会"看不见带的存在"）
    L.push(ns('rect', { x: m.X0, y: py(tol).toFixed(1), width: (m.X1 - m.X0).toFixed(1),
      height: Math.max(1, py(-tol) - py(tol)).toFixed(1), fill: C.up, 'fill-opacity': .1 }));
    [tol, -tol].forEach(v => {
      L.push(ns('line', { x1: m.X0, y1: py(v).toFixed(1), x2: m.X1, y2: py(v).toFixed(1),
        stroke: C.up, 'stroke-width': 1, 'stroke-dasharray': '4 4', 'stroke-opacity': .5 }));
      L.push(ns('text', { x: m.X1 - 4, y: (py(v) - 6).toFixed(1), fill: C.up,
        'font-size': 10, 'text-anchor': 'end', 'fill-opacity': .75 },
        (v > 0 ? '+' : '−') + tol + '%'));
    });
    // 刻度
    [-half, -half / 2, 0, half / 2, half].forEach(v => {
      const y = py(v), zero = v === 0;
      L.push(ns('line', { x1: m.X0, y1: y.toFixed(1), x2: m.X1, y2: y.toFixed(1),
        stroke: zero ? C.axis : C.grid, 'stroke-width': zero ? 1.2 : 1,
        'stroke-dasharray': zero ? null : '4 6', 'stroke-opacity': zero ? .85 : .5 }));
      L.push(ns('text', { x: m.X0 - 10, y: (y + 4).toFixed(1), fill: C.label,
        'font-size': FS, 'text-anchor': 'end' }, (v > 0 ? '+' : '') + (+v.toFixed(1)) + '%'));
    });
    // 柱
    const bw = Math.max(4, Math.min(20, (m.X1 - m.X0) / n * 0.55));
    pts.forEach((p, i) => {
      const v = p.gap_pct;
      if (v == null) return;
      const y = py(v);
      L.push(ns('rect', { x: (px(i) - bw / 2).toFixed(1), y: Math.min(y, y0).toFixed(1),
        width: bw.toFixed(1), height: Math.max(1.5, Math.abs(y0 - y)).toFixed(1), rx: 2,
        fill: v >= 0 ? cu : cd, 'fill-opacity': v >= 0 ? .8 : .85 }));
    });
    for (const i of xIdx(n, opt.labelCount || 12)) {
      L.push(ns('text', { x: px(i).toFixed(1), y: m.Y1 + 20, fill: C.label,
        'font-size': FS, 'text-anchor': 'middle' }, String((pts[i] || {}).t)));
    }

    const hits = buildHits(pts, px, [
      { get: p => p.gap_pct, py: py, color: '', name: '偏差' }
    ]);
    hits.forEach((h, i) => {
      const v = (pts[i] || {}).gap_pct;
      h.ys[0].color = v == null ? C.label : (v >= 0 ? cu : cd);
      h.ys[0].text = v == null ? '' : (v >= 0 ? '+' : '') + v.toFixed(1) + '%' + (v >= 0 ? ' 超发' : ' 欠发');
    });
    finish(el, W, H, m, L, hits);
  }

  return { compareChart, deviationChart, multiLineChart };
})();
