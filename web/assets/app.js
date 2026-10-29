/* 站点主逻辑：路由 + 页面渲染（数据全部来自 /api/*，无任何写死数值）
   电量单位统一 kWh，功率 kW，收益 元 */
(function () {
  const fmt = (v, d = 2) => (v == null || isNaN(v)) ? '—' : Number(v).toFixed(d);
  const fmtK = v => (v == null || isNaN(v)) ? '—' : Math.round(Number(v)).toLocaleString('zh-CN');
  /* 电价：最多 4 位小数且去掉末尾 0 —— 0.5487 显示 0.5487，0.65 显示 0.65 */
  const fmtP = v => (v == null || isNaN(v)) ? '—' : String(+Number(v).toFixed(4));
  // ISO 时间戳 → "10-29 13:10"（导入时间、快照时间这类"什么时候"的表达用）
  const shortTs = s => {
    if (!s) return '—';
    const t = String(s);
    return t.length >= 16 ? t.slice(5, 16).replace('T', ' ') : t;
  };
  // 当前时刻 "HH:MM"（KPI 与图内结论共用同一种表述，避免"截至当前"含糊）
  const hmNow = () => {
    const d = new Date();
    return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2);
  };
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const ROUTES = [
    { id: 'overview', name: '运行总览', hash: '#/overview' },
    // 原「发电预测」与「历史统计」已合并：选日期即可同时看到该日实测与预测
    { id: 'forecast', name: '发电分析', hash: '#/forecast' },
    { id: 'inverters', name: '逆变器监控', hash: '#/inverters' },
    { id: 'site', name: '电站档案', hash: '#/site' },
    { id: 'status', name: '数据源', hash: '#/status' },
  ];
  const SCOPE_BTN = { recent: '近 30 天', future: '未来 30 天', all: '全部' };

  /* 趋势图复选框（多选）：逆变器数据 / 气象数据 两组，勾选即画线 */
  const WDIR_TXT = ['北', '北东北', '东北', '东东北', '东', '东东南', '东南', '南东南',
    '南', '南西南', '西南', '西西南', '西', '西西北', '西北', '北西北'];
  const wdirName = d => (d == null ? '' :
    WDIR_TXT[Math.floor(((d % 360) + 11.25) / 22.5) % 16] + '风');
  /* 单参数取值：历史日取实况源（Open-Meteo / 心知积累），未来日取预报源 */
  function wv(r, field) {
    const w = r && r.weather;
    if (!w) return null;
    return r.is_future ? (w.src === 'forecast' ? w[field] : null)
                       : (w.src === 'forecast' ? null : w[field]);
  }
  const TREND_GROUPS = [
    { name: '逆变器数据', items: [
      // 发电量：实测（绿实线）与模型/预测（蓝紫虚线）合并为一项、画两条线
      { key: 'gen', label: '发电量', dual: ['#35ED7E', '#6C73F5'] },
      { key: 'peak', label: '峰值功率', unit: ' kW', color: '#F5C542', get: r => r.peak_kw },
    ] },
    { name: '气象数据', items: [
      { key: 'temp',    label: '环境温度', unit: ' ℃',    color: '#FF8A5C', get: r => wv(r, 'temp') },
      { key: 'tmod',    label: '组件温度', unit: ' ℃',    color: '#F0655A', get: r => wv(r, 'tmod') },
      { key: 'rh',      label: '相对湿度', unit: ' %',    color: '#4FC3F7', get: r => wv(r, 'rh') },
      { key: 'wind',    label: '风速',     unit: ' m/s',  color: '#4DD0E1', get: r => wv(r, 'wind') },
      { key: 'wdir',    label: '风向（悬停显示）', noLine: true,
        get: r => wv(r, 'wind_dir'),
        extra: w => (w && w.wind_dir != null) ? wdirName(w.wind_dir) : '' },
      { key: 'ghi_avg', label: '总辐射',   unit: ' W/m²', color: '#FFD54F', get: r => wv(r, 'ghi_avg') },
      { key: 'ghi_sum', label: '总辐射日累计值', unit: ' kWh/m²', color: '#FFB74D', get: r => wv(r, 'ghi_sum') },
    ] },
  ];
  const TREND_ITEMS = {};
  TREND_GROUPS.forEach(g => g.items.forEach(it => { TREND_ITEMS[it.key] = it; }));

  /* 发电分析 KPI 统计范围：当日 / 近 7 天 / 近 30 天 */
  const KPI_RANGES = { today: '当日', d7: '近 7 天', d30: '近 30 天' };

  const $view = document.getElementById('view');
  const $nav = document.getElementById('nav');
  const $title = document.getElementById('pageTitle');
  const $banner = document.getElementById('banner');
  const $chipDate = document.getElementById('chipDate');

  let STATUS = null;
  let timer = null;
  let currentPage = null;
  // 发电分析页状态跨刷新保留（自动刷新时不丢选中日期、范围与勾选参数）
  const FC = { scope: 'all', date: null, month: null, params: new Set(['gen']), kpiRange: 'd7' };
  // 总览图默认只画「实测 + 预测」两条：一张图回答一个问题（今天准不准），
  // 日前基线与辐照度按需勾选——默认全开会让"哪条能对比"变成猜谜
  const OV = { params: new Set(['actual', 'forecast']), view: 'day', cap: 2.105 };

  /* ---------------- 通用 UI 片段 ---------------- */
  function skeleton(cols) {
    return '<div class="kpis" style="grid-template-columns:repeat(' + cols + ',1fr)">' +
      Array.from({ length: cols }).map(() => '<div class="kpi"><div class="skel" style="height:12px;width:60%"></div>' +
        '<div class="skel" style="height:26px;width:70%;margin:10px 0"></div>' +
        '<div class="skel" style="height:10px;width:50%"></div></div>').join('') + '</div>';
  }
  function emptyBox(title, desc, action) {
    return '<div class="empty"><b>' + esc(title) + '</b>' + esc(desc || '') +
      (action ? '<div style="margin-top:12px">' + action + '</div>' : '') + '</div>';
  }
  function errorBox(msg, retryId) {
    return '<div class="empty"><b>数据加载失败</b>' + esc(msg) +
      '<div style="margin-top:12px"><button class="btn" id="' + retryId + '">重试</button></div></div>';
  }
  const ageText = Banner.ageText;
  const atText = Banner.atText;
  function dotClass(st) {
    return { live: 'live', stale: 'stale', ok: 'live', error: 'err', empty: 'stale', unavailable: 'off' }[st] || 'off';
  }

  function showBanner(kind, html) {
    $banner.hidden = false;
    $banner.className = 'banner' + (kind ? ' ' + kind : '');
    $banner.innerHTML = html;
  }
  function hideBanner() { $banner.hidden = true; }

  /* ---------------- 状态头 ---------------- */
  async function refreshStatus(force) {
    try { STATUS = await API.status(); } catch (e) { STATUS = null; }
    document.getElementById('srcLine').textContent =
      STATUS ? Object.values(STATUS.sources).filter(s => s.status === 'ok').length + '/' +
        Object.keys(STATUS.sources).length + ' 个源在线' : '状态不可用';
    return STATUS;
  }

  /* 顶部提示：文案由 Banner.build 按「实际取数模式 + 数据新鲜度」给出，
     api 模式且数据新鲜时返回空数组 → 自动隐藏 */
  function bannerByStatus(routeId) {
    const r = Banner.build(STATUS, routeId);
    if (r.msgs.length) showBanner(r.level, r.msgs.join('<br>'));
    else hideBanner();
  }

  /* ---------------- 逐日趋势：历史实测 + 模型回算/未来预测，同一条时间轴
     参数复选框多选：逆变器类（发电量/峰值功率）+ 气象类（温/湿/风/辐照） ---------------- */
  async function drawTrendChart(svg) {
    if (!svg) return;
    svg.innerHTML = '<text x="640" y="190" fill="#8B90A8" font-size="13" text-anchor="middle">加载中…</text>';
    let d;
    try { d = await API.trend(FC.scope, 30); }
    catch (e) {
      svg.innerHTML = '<text x="640" y="190" fill="#F0655A" font-size="13" text-anchor="middle">' + esc(e.message) + '</text>';
      return;
    }
    const rows = d.rows || [];
    if (!rows.length) { Charts.multiLineChart(svg, { points: [], series: [] }); return; }
    // X 轴按真实日历连续：历史段里「没有记录」的日期补成空点（各序列均为 null），
    // 否则 08-12 与 09-07 之间隔着 26 天却只占一个身位，时间轴会被读成连续运行
    const all = padCalendarGaps(rows);

    // 按勾选参数组装序列：发电量 = 实测 + 模型/预测 两条线；其余参数各一条线。
    // 风向是 0–360° 方位量，画折线会在 0°/360° 产生假跳变、且角度没有"大小"语义
    // → 不画线，勾选后只在悬停提示里给方位文字（extra）。
    const checked = Array.from(FC.params).filter(k => TREND_ITEMS[k]);
    if (!checked.length) checked.push('gen');
    const srs = [];
    if (checked.indexOf('gen') >= 0) {
      srs.push({ key: 'a', name: '实测发电', color: '#35ED7E', unit: ' kWh', get: r => r.actual_kwh });
      srs.push({ key: 'f', name: '模型 / 预测', color: '#6C73F5', unit: ' kWh', dash: true, get: r => r.forecast_kwh });
    }
    checked.filter(k => k !== 'gen' && !TREND_ITEMS[k].noLine).forEach(k => {
      const it = TREND_ITEMS[k];
      srs.push({ key: k, name: it.label, color: it.color, unit: it.unit || '', get: it.get });
    });
    if (!srs.length) {          // 只勾了纯文字项（如风向）时回落到发电量，图不至于全空
      srs.push({ key: 'a', name: '实测发电', color: '#35ED7E', unit: ' kWh', get: r => r.actual_kwh });
      srs.push({ key: 'f', name: '模型 / 预测', color: '#6C73F5', unit: ' kWh', dash: true, get: r => r.forecast_kwh });
    }

    const wSrcName = { 'open-meteo': 'Open-Meteo 实况', seniverse: '心知天气积累', forecast: '气象预报' };
    const pts = all.map(r => {
      const vals = {};
      srs.forEach(s => { vals[s.key] = s.get(r); });
      let note = r.is_future ? '未来预测' : (r.gap ? '无记录' : '历史实测');
      if (r.status === 'no_generation') note += ' · 停运 / 并网前，实测 0';
      else if (r.status === 'irr_only') note += ' · 只有辐照，无发电记录';
      if (r.status === 'irr_estimated') note += ' · 辐照缺测，按气候学推算';
      if (r.partial) note += ' · 今日尚未结束，实测仍会上升';
      const w = r.weather;
      if (w) {
        note += ' · ' + (wSrcName[w.src] || w.src);
        if (w.temp_min != null && w.temp_max != null) note += ' · ' + w.temp_min + '~' + w.temp_max + '℃';
        checked.filter(k => k !== 'gen').forEach(k => {
          const it = TREND_ITEMS[k];
          if (it.extra) { const ex = it.extra(w); if (ex) note += ' · ' + ex; }
          if (k === 'tmod') note += ' · NOCT 经验式估算';
        });
      }
      return { t: (r.date || '').slice(5), vals: vals, today: !!r.is_today, note: note };
    });
    Charts.multiLineChart(svg, {
      points: pts, series: srs, labelCount: 12,
      emptyMsg: '暂无数据（勾选参数后显示曲线）'
    });
  }

  /* 历史段按日历补齐空位：返回仍按日期升序，补出来的行 gap=true、两个值都 null */
  const _ymd = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') +
    '-' + String(d.getDate()).padStart(2, '0');
  function padCalendarGaps(rows) {
    const out = [];
    for (let i = 0; i < rows.length; i++) {
      out.push(rows[i]);
      const nx = rows[i + 1];
      if (!nx || nx.is_future) continue;
      const a = new Date(rows[i].date + 'T00:00:00');
      const b = new Date(nx.date + 'T00:00:00');
      const gap = Math.round((b - a) / 86400000) - 1;
      if (gap > 0 && gap <= 90) {          // 超过 90 天的空档不补，避免整张图被拉空
        for (let g = 1; g <= gap; g++) {
          out.push({ date: _ymd(new Date(a.getTime() + g * 86400000)),
            actual_kwh: null, forecast_kwh: null, status: 'gap', gap: true, is_future: false });
        }
      }
    }
    return out;
  }

  /* 趋势图复选框：逆变器数据 / 气象数据 两组，横排；勾选即画线 */
  function trendCheckboxes() {
    return TREND_GROUPS.map(g =>
      '<span class="ckgrp">' + g.name + '</span>' + g.items.map(it => {
        let dots = '';
        if (it.dual) dots = it.dual.map(c => '<i style="background:' + c + '"></i>').join('');
        else if (it.color) dots = '<i style="background:' + it.color + '"></i>';
        return '<label class="ck"><input type="checkbox" data-tp="' + it.key + '"' +
          (FC.params.has(it.key) ? ' checked' : '') + '>' + dots + it.label + '</label>';
      }).join('')).join('');
  }
  function bindTrendChecks() {
    document.querySelectorAll('[data-tp]').forEach(cb => {
      cb.onchange = () => {
        if (cb.checked) FC.params.add(cb.dataset.tp);
        else FC.params.delete(cb.dataset.tp);
        if (!FC.params.size) FC.params.add('gen');     // 至少保留一项
        document.querySelectorAll('[data-tp]').forEach(x => {
          x.checked = FC.params.has(x.dataset.tp);
        });
        syncHash();
        drawTrendChart(document.getElementById('trendChart'));
      };
    });
  }

  /* 范围切换：近 30 天 / 未来 30 天 / 全部 */
  function scopeButtons(active) {
    return '<div class="rgs">' + ['recent', 'future', 'all'].map(s =>
      '<button class="rg' + (s === active ? ' on' : '') + '" data-sc="' + s + '">' + SCOPE_BTN[s] + '</button>').join('') + '</div>';
  }
  function bindScopeSwitch() {
    document.querySelectorAll('[data-sc]').forEach(b => {
      b.onclick = () => {
        document.querySelectorAll('[data-sc]').forEach(x => x.classList.toggle('on', x === b));
        FC.scope = b.dataset.sc;
        syncHash();
        drawTrendChart(document.getElementById('trendChart'));
      };
    });
  }

  /* ---------------- 视图状态 ↔ URL（M2）：F5 / 分享链接 / 后退都能还原现场 ----------------
     发电分析页的选中日期、日历月份、趋势范围、勾选参数、KPI 范围全部编进 hash query，
     用 replaceState 写回（不产生历史记录），路由时解析恢复。 */
  function syncHash() {
    if (currentPage !== 'forecast') return;
    const q = new URLSearchParams();
    if (FC.date) q.set('date', FC.date);
    if (FC.month) q.set('m', FC.month);
    q.set('scope', FC.scope);
    q.set('params', Array.from(FC.params).join(','));
    q.set('kr', FC.kpiRange);
    history.replaceState(null, '', '#/forecast?' + q.toString());
  }
  function applyHashState() {
    const h = location.hash || '';
    const qi = h.indexOf('?');
    if (qi < 0) return;
    const q = new URLSearchParams(h.slice(qi + 1));
    if (SCOPE_BTN[q.get('scope')]) FC.scope = q.get('scope');
    if (/^\d{4}-\d{2}-\d{2}$/.test(q.get('date') || '')) FC.date = q.get('date');
    if (/^\d{4}-\d{2}$/.test(q.get('m') || '')) FC.month = q.get('m');
    if (KPI_RANGES[q.get('kr')]) FC.kpiRange = q.get('kr');
    if (q.get('params')) {
      const ps = q.get('params').split(',').filter(k => TREND_ITEMS[k]);
      if (ps.length) FC.params = new Set(ps);
    }
  }

  /* ---------------- 页面：运行总览（只讲当天） ---------------- */
  async function pageOverview(silent) {
    if (!silent) {
      $view.innerHTML = skeleton(3) +
        '<div class="card"><div class="skel" style="height:300px"></div></div>' +
        '<div class="card"><div class="skel" style="height:240px"></div></div>';
    }
    let ov;
    try { ov = await API.overview(); }
    catch (e) { $view.innerHTML = errorBox(e.message, 'retryOv'); bindRetry(pageOverview); return; }

    const k = ov.kpi;
    const cur = !!ov.is_current;
    const dday = ov.data_day || '';
    const short = (s) => s ? s.slice(5).replace('-', '/') : '';
    const staleTip = '数据源最新 ' + short(dday) + ' · 今日(' + short(ov.today) + ') 尚无数据';
    const act = cur ? k.actual_today_kwh : k.latest_day_kwh;
    const fc = cur ? k.forecast_today_kwh : k.latest_forecast_kwh;
    const dev = cur ? k.today_dev_pct : k.latest_dev_pct;
    const acc = cur ? k.today_accuracy_pct : k.latest_accuracy_pct;
    // 「今日」表述不准：一天只过了一半，拿"当前累计实发"去比"全天预测"必然巨亏。
    // 改成「当前」+ 同窗口口径：当前实发 = 截至现在累计；当前预测 = 截至现在预期。
    const nowHM = hmNow();
    const dayLbl = cur ? '当前' : (short(dday) + ' ');
    // 偏差颜色全站统一口径：超发（实测>模型）红、欠发绿 —— 与偏差图、历史数据表一致
    const accV = acc == null ? null : Math.max(0, acc);   // |偏差|>100% 时准确率夹到 0，不出负数
    const fcNow = (cur && k.expected_now_kwh != null) ? k.expected_now_kwh : fc;
    const cards = [
      { cls: cur ? 'g' : 'm', label: dayLbl + '实发', v: fmtK(act), u: 'kWh',
        note: cur ? ('截至 ' + nowHM + ' 累计') : staleTip },
      { cls: cur ? 'p' : 'm', label: dayLbl + '预测', v: fmtK(fcNow), u: 'kWh', idV: 'kpiFcVal',
        note: cur ? (k.forecast_today_is_estimate ? '辐照缺测 · 按近期日均参考'
                  : (k.expected_now_kwh != null
                  ? ('截至 ' + nowHM + ' 预期 · 全天 ' + fmtK(fc) + ' kWh' +
                     (k.forecast_today_adjusted ? '（已按实况 ×' + k.forecast_today_adjusted.ratio + '）' : ''))
                  : (k.forecast_today_adjusted
                  ? ('全天 · 已按实况 ×' + k.forecast_today_adjusted.ratio)
                  : (fc == null ? '暂无辐照输入' : '模型预测值'))))
                  : ((k.latest_forecast_is_estimate ? '参考值 · 该日辐照缺测' : '模型对账值') + ' · ' + staleTip) },
      { cls: dev == null ? 'm' : (dev > 0 ? 'r' : (dev < 0 ? 'g' : 'm')), label: cur ? '偏差（当前）' : '偏差（当日）',
        v: dev == null ? '—' : (dev >= 0 ? '+' : '') + fmt(dev, 1) + '%', u: '', idV: 'kpiDevVal', idN: 'kpiDevNote',
        note: dev == null ? '缺实测或模型值' : (dev > 0 ? '超发 · 实测高于预测' : (dev < 0 ? '欠发 · 实测低于预测' : '实测与预测一致'))
          + (cur && k.today_dev_basis === 'expected_now' ? ' · 对比截至现在预期' : '') },
      { cls: accV == null ? 'm' : (accV >= 85 ? 'g' : 'b'), label: '准确率',
        v: accV == null ? '—' : fmt(accV, 1) + '%', u: '', idV: 'kpiAccVal',
        note: accV == null ? '缺实测或预测值' : '1 − |实测 − 预测| ÷ 预测（下限 0）' },
    ];
    $view.innerHTML =
      '<div class="kpis">' + cards.map(c =>
        '<div class="kpi ' + c.cls + '"><div class="label">' + c.label + '</div>' +
        '<div class="value"' + (c.idV ? ' id="' + c.idV + '"' : '') + '>' + c.v + '<small>' + c.u + '</small></div>' +
        '<div class="note"' + (c.idN ? ' id="' + c.idN + '"' : '') + '>' + esc(c.note) + '</div></div>').join('') + '</div>' +

      '<div class="card">' +
        '<div class="chead">' +
          '<div class="cL"><h2>发电对比 · 实测 vs 预测' +
          '<span class="u" id="ovTodayUnit">15 分钟平均功率 kW</span></h2></div>' +
          '<div class="cR">' +
            '<div class="rgs" style="margin-right:12px">' +
              '<button class="rg on" data-radview="day">当日</button>' +
              '<button class="rg" data-radview="month">当月</button>' +
            '</div>' +
            '<div class="cks">' +
              '<label class="ck"><input type="checkbox" data-ov="actual"' + (OV.params.has('actual') ? ' checked' : '') + '>' +
              '<i style="background:var(--green)"></i>实测</label>' +
              '<label class="ck"><input type="checkbox" data-ov="forecast"' + (OV.params.has('forecast') ? ' checked' : '') + '>' +
              '<i style="background:var(--blurple)"></i>预测</label>' +
              '<label class="ck"><input type="checkbox" data-ov="baseline"' + (OV.params.has('baseline') ? ' checked' : '') + '>' +
              '<i class="dash" style="border-color:#A5A9C4"></i>日前基线</label>' +
              '<label class="ck"><input type="checkbox" data-ov="ghi"' + (OV.params.has('ghi') ? ' checked' : '') + '>' +
              '<i style="background:var(--yellow)"></i>辐照度</label>' +
            '</div>' +
          '</div>' +
        '</div>' +
        '<div class="ctip" id="ovTodayTip"></div>' +
        '<div class="note" style="color:var(--t4);font-size:11px;margin-top:8px;line-height:1.7" id="ovOvNote">' +
        '<b style="color:var(--t3)">怎么读这张图</b>：左轴<b style="color:var(--t3)">功率 kW</b>（两条线同一个轴、同一时间轴，' +
        '可直接比高低）——「实测」= 15 分钟时段平均功率（华为 5 分钟曲线聚合，<b>不是发电量</b>）；' +
        '「预测」= 模型给出的同时段平均功率（已按今日实况动态修正）。' +
        '两线之间的<span style="color:var(--blurple)">淡色填充＝差值</span>。' +
        '当前时刻以右为<span style="color:var(--blurple)">未发生区（淡化点线）</span>，只做参考、不参与对账；' +
        '「日前基线」是当日原始预测（承诺值，默认不画）。右轴 <b style="color:var(--t3)">辐照 W/m²</b> 勾选后出现。</div>' +
        '<div class="chart-wrap"><svg id="ovTodayChart"></svg></div>' +
      '</div>' +

      '<div class="grid2">' +
        '<div class="card"><div class="chead"><h2>逆变器发电排行<span class="u">kWh · TOP 5</span></h2></div>' +
        '<div id="ovInv"></div></div>' +
        '<div class="card"><div class="chead"><h2>实时心跳</h2></div><div id="ovSide"></div>' +
        '<div class="note" style="color:var(--t4);font-size:11px;margin-top:10px">' +
        '装机、并网、电价、模型系数等静态信息见「电站档案」页</div></div>' +
      '</div>' +

      '<div class="card"><div class="chead"><h2>数据校验<span class="u">关联性 · 偏差分布 · 气象仪质量</span></h2></div>' +
      '<div id="ovQuality"><div class="note" style="color:var(--t4);font-size:12px">计算中…</div></div></div>';

    drawTodayChart();
    // 总览图复选框：勾选即画线、取消即隐藏（样式/交互与发电分析页一致）
    document.querySelectorAll('[data-ov]').forEach(cb => {
      cb.onchange = () => {
        if (cb.checked) OV.params.add(cb.dataset.ov);
        else OV.params.delete(cb.dataset.ov);
        if (!OV.params.size) {                 // 至少保留一条曲线
          OV.params.add('actual');
          const first = document.querySelector('[data-ov="actual"]');
          if (first) first.checked = true;
        }
        drawTodayChart();
      };
    });
    renderOvSide(ov);
    renderOvInv();
    OV.cap = (ov.station || {}).capacity_mwp || 2.105;
    // 当日 / 当月 维度切换（同一张图，不另开图表）
    document.querySelectorAll('[data-radview]').forEach(b => {
      b.onclick = () => {
        OV.view = b.dataset.radview;
        document.querySelectorAll('[data-radview]').forEach(x => x.classList.toggle('on', x === b));
        drawTodayChart();
      };
    });
    drawTodayChart();
    renderQuality();
  }

  /* 当月视图：逐日辐照（kWh/m²，右轴）× 逐日发电（kWh，左轴）——同一张图、同一时间轴 */
  async function drawMonthChart(svg) {
    const [tr, rad] = await Promise.all([
      API.trend('all', 60).catch(() => null),
      API.radiation().catch(() => null),
    ]);
    const days = (rad && rad.month_days) || [];
    if (!days.length) {
      svg.innerHTML = '<text x="640" y="190" fill="#8B90A8" font-size="13" text-anchor="middle">本月暂无辐照数据</text>';
      return;
    }
    const map = {};
    ((tr && tr.rows) || []).forEach(r => { map[r.date] = r; });
    const show = { a: OV.params.has('actual'), f: OV.params.has('forecast'), g: OV.params.has('ghi') };
    const pts = days.map(x => {
      const r = map[x.date] || {};
      const isHist = !r.status || r.status !== 'forecast';
      return {
        t: x.date.slice(5),
        vals: {
          a: show.a && isHist ? (r.actual_kwh != null ? r.actual_kwh : null) : null,
          // 历史段的「模型」是用实测辐照回算出来的，画在历史段等于用答案算答案；
          // 只有未来日才是真正的预测，所以模型线只出现在未来日（右侧）——
          // 否则「历史几乎贴线」会被误读成「模型很准」，那不是预测能力
          f: (show.f && r.is_future && r.forecast_kwh != null) ? r.forecast_kwh : null,
          g: show.g && !x.is_forecast ? x.value : null,
        },
        today: !!x.is_today,
        note: x.date + ' · 辐照 ' + (x.value == null ? '—' : fmt(x.value, 2)) + ' kWh/m²（' + (x.source || '无数据') + '）' +
          (r.actual_kwh != null ? ' · 实发 ' + fmtK(r.actual_kwh) + ' kWh' : '') +
          (r.is_future && r.forecast_kwh != null ? ' · 预测 ' + fmtK(r.forecast_kwh) + ' kWh'
            : (r.forecast_kwh != null ? ' · 回算 ' + fmtK(r.forecast_kwh) + ' kWh（事后，非预测）' : '')),
      };
    });
    Charts.multiLineChart(svg, {
      points: pts,
      series: [
        { key: 'a', name: '实测发电', color: '#35ED7E', unit: ' kWh' },
        { key: 'f', name: '预测（未来）', color: '#6C73F5', unit: ' kWh', dash: true },
        { key: 'g', name: '总辐照', color: '#F5C542', unit: ' kWh/m²' },
      ],
      labelCount: 12, emptyMsg: '本月暂无数据（勾选曲线后显示）',
    });
    const tip = document.getElementById('ovTodayTip');
    const unit = document.getElementById('ovTodayUnit');
    if (unit) unit.textContent = '当月（' + (rad.month || '') + '）· 电量 kWh / 辐照 kWh/m²';
    if (tip) {
      const actE = days.reduce((s, x) => s + ((map[x.date] || {}).actual_kwh || 0), 0);
      // 只累加未来日的预测：历史回算值加进来毫无意义，还会把「预测」说成很准
      const fut = days.filter(x => (map[x.date] || {}).is_future);
      const fcE = fut.reduce((s, x) => s + ((map[x.date] || {}).forecast_kwh || 0), 0);
      // PR = 实发 / （辐照 × 装机）：回答「辐照和发电到底什么关系」
      const pr = (rad.month_total && OV.cap) ? actE / (rad.month_total * OV.cap * 1000) : null;
      tip.innerHTML =
        '当月累计 <b>实发 ' + fmtK(actE) + ' kWh</b>' +
        (fut.length ? ' · <b>未来 ' + fut.length + ' 天预测 ' + fmtK(fcE) + ' kWh</b>' : '') +
        ' &nbsp;·&nbsp; <b>辐照 ' + fmt(rad.month_total, 1) + ' kWh/m²</b>（' + (rad.actual_days || 0) + ' 天实测，日均 ' +
        fmt(rad.month_avg, 2) + '）' +
        (pr != null ? ' &nbsp;·&nbsp; <b>系统效率 PR ' + fmt(pr * 100, 1) + '%</b>（实发 ÷ 理论 = 辐照 × ' +
          fmt(OV.cap, 3) + 'MWp × 1000）' : '') +
        ' &nbsp;·&nbsp; 来源：气象仪 ' + (((rad.sources_count || {})['气象仪']) || 0) + ' 天 / Open-Meteo ' +
        (((rad.sources_count || {})['Open-Meteo 实况']) || 0) + ' 天';
    }
  }

  /* 数据校验卡：预测/实测/辐照关联性 + 今日逐时一致性 + 气象仪质量（全部来自 /api/quality） */
  async function renderQuality() {
    const el = document.getElementById('ovQuality');
    if (!el) return;
    let q;
    try { q = await API.quality(); }
    catch (e) {
      el.innerHTML = '<div class="note" style="color:var(--red);font-size:12px">校验失败：' + esc(e.message) + '</div>';
      return;
    }
    const d = q.daily || {}, it = q.intraday_today || {}, ws = q.weather_station || {};
    const fl = q.forecast_log || {};
    const rTxt = r => (r == null ? '—' : fmt(r, 3));
    const col = r => (r == null ? 'var(--t2)' : (r >= 0.9 ? 'var(--green)' : (r >= 0.75 ? 'var(--yellow)' : 'var(--red)')));
    const rSpan = r => '<span style="color:' + col(r) + '">' + rTxt(r) + '</span>';
    const rows = [
      // 「回算」和「预测」必须分开说：回算用的是当天实测辐照，几乎必然贴实测，
      // 拿它当预测准确率是循环论证（10-16 曾出现 A=F=8827.2 完全相等）
      ['回算关联性（事后）',
       '实测×回算 r=' + rSpan(d.r_actual_forecast) + ' · 实测×辐照 r=' + rSpan(d.r_actual_irradiance) +
       ' · 回算×辐照 r=' + rSpan(d.r_forecast_irradiance) +
       '（' + (d.days || 0) + ' 个运营日' + (d.window ? ' · ' + d.window : '') + '，用当天实测辐照反推，不能当预测准确率）', true],
      ['日前预测准确率',
       (fl.days ? ('平均|偏差| ' + fmt(fl.abs_dev_mean, 1) + '% · ≤10% ' + (fl.within_10pct || 0) + '/' + fl.days +
         ' 天 · 最大 ' + fmt(fl.abs_dev_max, 1) + '% · 存档 ' + (fl.snapshots || 0) + ' 份')
         : '尚未积累（每天存一份当日预测快照，「预测→事后对账」才是真考核，明日开始出数）'), true],
      ['回算偏差分布',
       '平均|偏差| ' + (d.abs_dev_mean == null ? '—' : fmt(d.abs_dev_mean, 1) + '%') +
       ' · 中位 ' + (d.abs_dev_median == null ? '—' : fmt(d.abs_dev_median, 1) + '%') +
       ' · 最大 ' + (d.abs_dev_max == null ? '—' : fmt(d.abs_dev_max, 1) + '%') +
       ' · ≤10% ' + (d.days_within_10pct || 0) + '/' + (d.days || 0) + ' 天', true],
      ['今日逐时段一致性',
       '实测功率×模型 r=' + rSpan(it.r_actual_forecast) + '（' + (it.slots || 0) + ' 个已采样时段 · 15 分钟平均功率）', true],
      ['气象仪覆盖',
       (ws.days ? (ws.first_day + ' ~ ' + ws.last_day + ' 共 ' + ws.days + ' 天' +
         (ws.imported && ws.imported.imported_at
           ? ' · 手动导入 ' + shortTs(ws.imported.imported_at) + '（' + (ws.imported.days || 0) + ' 天）'
           : ' · 来自旧目录扫描') +
         (ws.missing_since ? '，<span style="color:var(--red)">已断更 ' + ws.missing_since + ' 天</span>' : ''))
         : '无数据'), true],
      ['气象仪可信度',
       '辐照×实发 r=' + rSpan(ws.r_ground_actual) + '（' + (ws.ground_actual_days || 0) + ' 天配对）' +
       ' · 与 Open-Meteo r=' + rSpan(ws.overlap_r) + '（' + (ws.overlap_days || 0) + ' 天重叠）' +
       ' · 地面/OM 中位比值 ' + (ws.ground_vs_om_median == null ? '—' : fmt(ws.ground_vs_om_median, 3)) +
       ' · 异常值 ' + ((ws.sanity_bad || []).length) + ' 天', true],
    ];
    let html = '<div class="kv">' + rows.map(([a, b, raw]) =>
      '<div class="k">' + esc(a) + '</div><div class="v">' + (raw ? b : esc(b)) + '</div>').join('') + '</div>';
    const notes = [];
    if (ws.missing_since) notes.push('气象仪已断更 ' + ws.missing_since + ' 天（最后数据 ' + ws.last_day +
      '），断更日的辐照由 Open-Meteo 兜底、模型回算精度略降，建议导出最新 xls');
    (ws.worst_days || []).forEach(w => {
      if (Math.abs(w.dev_pct) > 30) notes.push('气象仪 ' + w.date + ' 与 OM 偏差 ' + w.dev_pct +
        '%（地面 ' + w.ground + ' vs OM ' + w.om + ' kWh/m²），该日模型回算以气象仪优先');
    });
    if ((ws.sanity_bad || []).length) {
      notes.push('气象仪异常值：' + ws.sanity_bad.map(x => x.date + '(' + x.value + ')').join('、') + '，建议核对仪器');
    }
    if (notes.length) {
      html += '<div class="note" style="color:var(--yellow);font-size:11px;margin-top:10px;line-height:1.7">' +
        notes.map(esc).join('<br>') + '</div>';
    }
    el.innerHTML = html;
  }

  /* 当日逐时段：实测功率 vs 模型基线（kW 左轴），辐照度（W/m² 右轴）同图追加 */
  async function drawTodayChart() {
    const svg = document.getElementById('ovTodayChart');
    if (!svg) return;
    if (OV.view === 'month') {
      svg.innerHTML = '<text x="640" y="190" fill="#8B90A8" font-size="13" text-anchor="middle">加载中…</text>';
      const note = document.getElementById('ovOvNote');
      if (note) note.innerHTML = '<b style="color:var(--t3)">口径说明</b>：左轴＝逐日<b>发电量（kWh）</b>——' +
        '「实测」为该日累计电量，「模型」为该日模型预测/回算电量（同为 kWh，可直接对比）；' +
        '右轴＝逐日<b>总辐照（kWh/m²）</b>。辐照与发电是「投入—产出」关系，不是同一量纲，' +
        '所以必须分轴呈现；PR（系统效率）＝实发 ÷（辐照 × 装机），把两者关系压成一个可比较的数。';
      return drawMonthChart(svg);
    }
    let id;
    try { id = await API.intraday(null, 15); }   // 15 分钟粒度
    catch (e) {
      svg.innerHTML = '<text x="640" y="190" fill="#F0655A" font-size="13" text-anchor="middle">' + esc(e.message) + '</text>';
      return;
    }
    if (!id || id.error || !(id.hours || []).length) {
      svg.innerHTML = '<text x="640" y="190" fill="#8B90A8" font-size="14" text-anchor="middle">' +
        esc((id && id.error) || '暂无数据') + '</text>';
      return;
    }
    const hs = id.hours || [];
    const sph = 60 / (id.slot_min || 60);          // 每小时时段桶数（15 分钟粒度 = 4）
    // 日出之后、第一个采样点之前的那段是「没采到」，不是「发电为 0」，图上必须标出来
    let band = null;
    if (id.measured_from && id.measured_hours) {
      const sunIdx = hs.findIndex(h => (h.forecast_kw || 0) > 0);
      const mh = parseInt(String(id.measured_from).slice(11, 13), 10) * sph;
      if (sunIdx >= 0 && mh > sunIdx) {
        band = { from: sunIdx, to: mh };
      }
    }
    const nowI = id.is_current ? (id.now_slot != null ? id.now_slot : new Date().getHours() * sph) : null;
    // 图内直接给结论：只对「已发生」的部分下判断。今日未结束时全天口径不给结论——
    // 那必然显示巨亏，是本轮「看起来模型不准」的主因。
    let callout = null;
    const dv = (id.dev_expected_pct != null) ? id.dev_expected_pct
             : (id.is_current ? null : id.dev_pct);
    if (dv != null) {
      const ok = Math.abs(dv) <= 10;
      callout = {
        label: (id.is_current ? '截至 ' + hmNow() + ' 偏差 ' : '日累计偏差 ') +
               (dv >= 0 ? '+' : '') + fmt(dv, 1) + '%',
        sub: '实测 ' + fmtK(id.actual_total_kwh) + ' kWh · ' +
             (id.is_current ? '预期 ' + fmtK(id.expected_now_kwh) : '预测 ' + fmtK(id.forecast_total_kwh)) +
             ' kWh（同一截止时刻，可直接对账）',
        color: ok ? '#35ED7E' : '#F0655A',
      };
    }
    Charts.compareChart(svg, {
      // value_kwh 是展示取值：有采集值取采集值，没有则取「按历史日内分布分摊」的估算值。
      // actual_kw 保持只放真实采集值，偏差计算不受估算值污染。
      points: hs.map(h => ({ t: h.t, actual_kw: h.actual_kw, forecast_kw: h.forecast_kw,
        baseline_kw: h.baseline_kw,
        value_kw: (h.value_kwh != null ? h.value_kwh : h.actual_kw),
        ghi_w: h.ghi_w, ghi_src: h.ghi_src,
        estimated: !!h.estimated, partial: !!h.partial,
        samples: h.samples, samples_expected: h.samples_expected })),
      colors: { actual: '#35ED7E', forecast: '#6C73F5', baseline: '#A5A9C4' },
      visible: { actual: OV.params.has('actual'), forecast: OV.params.has('forecast'),
                 baseline: OV.params.has('baseline'), ghi: OV.params.has('ghi') },
      labelCount: 24, band: band, callout: callout,
      nowHour: nowI
    });
    // KPI 卡与图同源回填：overview 与 intraday 是两次请求，中间实时数据会涨，
    // 不回填的话同一页面会出现「KPI +12.5% / 图 +13.1%」两个数打架
    if (id.is_current && dv != null) {
      const dvEl = document.getElementById('kpiDevVal');
      if (dvEl && dvEl.firstChild) dvEl.firstChild.nodeValue = (dv >= 0 ? '+' : '') + fmt(dv, 1) + '%';
      const ntEl = document.getElementById('kpiDevNote');
      if (ntEl) ntEl.textContent = (dv > 0 ? '超发 · 实测高于预测' : (dv < 0 ? '欠发 · 实测低于预测' : '实测与预测一致')) +
        ' · 对比截至现在预期（与图同源）';
      const acEl = document.getElementById('kpiAccVal');
      if (acEl && acEl.firstChild) acEl.firstChild.nodeValue = fmt(Math.max(0, 100 - Math.abs(dv)), 1) + '%';
      // 「当前预测」也回填：overview 与 intraday 是两次请求，预期电量会随时间涨，
      // 不回填会出现 KPI 4,404 / 图 4,331 两个"预期"打架
      const fcEl = document.getElementById('kpiFcVal');
      if (fcEl && fcEl.firstChild && id.expected_now_kwh != null) {
        fcEl.firstChild.nodeValue = fmtK(id.expected_now_kwh);
      }
    }
    const u = document.getElementById('ovTodayUnit');
    if (u) u.textContent = (id.date || '') + ' · 15 分钟平均功率 kW';
    const note = document.getElementById('ovOvNote');
    if (note) note.innerHTML = '<b style="color:var(--t3)">怎么读这张图</b>：左轴<b style="color:var(--t3)">功率 kW</b>' +
      '（实测与预测同一个轴、同一时间轴，可直接比高低）——「实测」= 15 分钟时段平均功率' +
      '（华为 5 分钟曲线聚合，<b>不是发电量</b>）；「预测」= 模型给出的同时段平均功率（已按今日实况动态修正）。' +
      '两线之间的<span style="color:var(--blurple)">淡色填充＝差值</span>；当前时刻以右为' +
      '<span style="color:var(--blurple)">未发生区（淡化点线）</span>，只做参考、不参与对账。' +
      '「日前基线」是当日原始预测（承诺值，默认不画）；右轴 <b style="color:var(--t3)">辐照 W/m²</b> 勾选后出现。';
    const tip = document.getElementById('ovTodayTip');
    if (tip) {
      const parts = [];
      parts.push('<b>实测</b> ' + fmtK(id.actual_total_kwh) + ' kWh');
      parts.push('<b>预测全天</b> ' + fmtK(id.forecast_total_kwh) + ' kWh' +
        (id.dynamic ? '（已按今日实况 ×' + fmt(id.dynamic.ratio, 3) + '）' : '') +
        (id.forecast_is_estimate ? '（辐照缺测·参考值）' : ''));
      // 偏差口径只能有一个：与上方 KPI 卡一致用「截至当前预期」，全天口径另注仅供参考
      if (id.dev_expected_pct != null) {
        parts.push('<b>偏差（截至当前）</b> ' + (id.dev_expected_pct >= 0 ? '+' : '') +
          fmt(id.dev_expected_pct, 1) + '% · 预期 ' + fmtK(id.expected_now_kwh) + ' kWh');
      }
      if (id.is_today && id.dev_pct != null) {
        parts.push('全天口径 ' + (id.dev_pct >= 0 ? '+' : '') + fmt(id.dev_pct, 1) +
          '%（今日未结束，仅参考）');
      } else if (!id.is_today && id.dev_pct != null) {
        parts.push('<b>日累计偏差</b> ' + (id.dev_pct >= 0 ? '+' : '') + fmt(id.dev_pct, 1) + '%');
      }
      // 两种来源必须写在图上：哪些是采到的、哪些是摊出来的，一眼能分清
      const al = id.allocation || {};
      if (al.measured_slots) {
        parts.push('<b>采集 ' + al.measured_slots + ' 个时段</b> ' + fmtK(al.measured_sum_kwh) + ' kWh');
      }
      if (al.allocated_slots) {
        parts.push('<b>估算 ' + al.allocated_slots + ' 个时段</b> ' + fmtK(al.allocated_sum_kwh) +
          ' kWh（按' + (al.weight_samples ? '历史 ' + al.weight_samples + ' 天' : '默认') +
          '日内分布分摊，合计与当日累计量严格相等）');
      }
      if (al.partial_slots) {
        parts.push(al.partial_slots + ' 个实测时段采样不全（值仅代表已采样部分，非整时段电量）');
      }
      if (al.pending_slots) parts.push(al.pending_slots + ' 个时段未到，不估算');
      if (al.mode && al.mode !== 'allocated' && al.reason) parts.push('<b>降级</b> ' + esc(al.reason));
      tip.innerHTML = parts.join(' &nbsp;·&nbsp; ');
    }
    // 回填「实时心跳」卡：当前功率 = 最近一个有实测的时段；今日电量 = 实测累计 + 模型全天
    const lastM = hs.slice().reverse().find(h => h.actual_kw != null);
    const $p = document.getElementById('livePower');
    if ($p) $p.textContent = lastM ? fmt(lastM.actual_kw, 1) + ' kW · ' + String(lastM.t).slice(0, 5) : '暂无采样';
    const $e = document.getElementById('liveEnergy');
    if ($e) {
      const ep = [];
      if (id.actual_total_kwh != null) ep.push('实测 ' + fmtK(id.actual_total_kwh) + ' kWh');
      if (id.forecast_total_kwh != null) ep.push('预测全天 ' + fmtK(id.forecast_total_kwh) + ' kWh');
      if (id.actual_total_kwh != null && id.forecast_total_kwh != null) {
        ep.push('预计剩余 ' + fmtK(Math.max(0, id.forecast_total_kwh - (id.measured_sum_kwh || 0))) + ' kWh');
      }
      $e.textContent = ep.length ? ep.join(' · ') : '暂无数据';
    }
  }

  /* 总览页：逆变器 TOP5 排行条 */
  async function renderOvInv() {
    const el = document.getElementById('ovInv');
    if (!el) return;
    try {
      const d = await API.inverters();
      const rows = (d.rows || []).slice(0, 5);
      if (!rows.length) {
        // 说明原因，而不是只写「暂无数据」——接口节流、未取到、未配置是三回事
        const why = d.status === 'empty'
          ? '本轮未取到逆变器数据（接口 5 分钟节流窗口内），稍后自动刷新'
          : (d.error || '暂无逆变器数据');
        el.innerHTML = '<div class="note" style="color:var(--t4);font-size:12px">' + esc(why) + '</div>';
        return;
      }
      const max = Math.max(...rows.map(r => r.kwh || 0), 1);
      el.innerHTML = '<div class="rk">' + rows.map(r =>
        '<div class="row"><span class="n">' + esc(r.name || '—') + '</span>' +
        '<span class="bar"><i style="width:' + Math.max(2, Math.round((r.kwh || 0) / max * 100)) + '%"></i></span>' +
        '<span class="v">' + fmtK(r.kwh) + '</span></div>').join('') + '</div>' +
        '<div class="note" style="color:var(--t4);font-size:11px;margin-top:10px">' +
        esc(d.date || '') + ' · 共 ' + (d.count || 0) + ' 台 · 合计 ' + fmtK(d.total_kwh) + ' kWh</div>';
    } catch (e) {
      el.innerHTML = '<div class="note" style="color:var(--red);font-size:12px">加载失败</div>';
    }
  }

  /* 总览「实时心跳」：只放随时间变化的信息（当前功率/今日电量/源状态）。
     电站名、容量、并网、电价、模型 K 全是静态档案 → 移到「电站档案」页，
     放在总览里会稀释"此刻系统状态"的焦点，且与档案页重复。 */
  function renderOvSide(ov) {
    const el = document.getElementById('ovSide');
    const s = (STATUS && STATUS.sources) || {};
    const rows = [
      ['当前功率', '<span id="livePower">采样中…</span>', true],
      ['今日电量', '<span id="liveEnergy">采样中…</span>', true],
      ['数据源最新日', (ov.data_day || '—') + (ov.is_current ? '（即今天）' : '（今日数据未入库）'), false],
      ['发电数据源', statusLine(s.generation), false],
      ['气象数据源', statusLine(s.irradiance), false],
      ['逆变器数据源', statusLine(s.inverters), false],
    ];
    el.innerHTML = '<div class="kv">' + rows.map(([a, b, raw]) =>
      '<div class="k">' + esc(a) + '</div><div class="v">' + (raw ? b : esc(b)) + '</div>').join('') + '</div>';
  }
  function statusLine(s) {
    if (!s) return '—';
    const map = { ok: '正常', error: '异常', empty: '空数据', unavailable: '未配置' };
    const src = (s.meta && (s.meta.provider || s.meta.mode)) || s.mode || '-';
    return (map[s.status] || s.status) + ' · ' + src + (s.as_of ? ' · ' + ageText(s.age_seconds) : '');
  }

  /* 逐日趋势（历史 + 未来）统一由 drawTrendChart 渲染，见「发电分析」页 */

  /* ---------------- 页面：发电分析（历史实测 + 未来预测，同一处选日期对比） ---------------- */
  async function pageAnalysis() {
    $view.innerHTML = skeleton(4) + '<div class="card"><div class="skel" style="height:300px"></div></div>' +
      '<div class="grid2 even"><div class="card"><div class="skel" style="height:340px"></div></div>' +
      '<div class="card"><div class="skel" style="height:340px"></div></div></div>';
    let cal, ex;
    try { [cal, ex] = await Promise.all([API.calendar(), API.forecastExplain()]); }
    catch (e) { $view.innerHTML = errorBox(e.message, 'retryFc'); bindRetry(pageAnalysis); return; }
    const rng = cal.range || {};
    if (!FC.date) FC.date = rng.default_date || rng.today;
    // 历史回填后最早可选日会前移；选中日期掉出范围时拉回默认日，不让页面卡在空数据上
    if ((rng.min_date && FC.date < rng.min_date) || (rng.max_date && FC.date > rng.max_date)) {
      FC.date = rng.default_date || rng.today;
    }
    if (!FC.month) FC.month = FC.date.slice(0, 7);

    $view.innerHTML =
      '<div class="rgs" style="justify-content:flex-end;margin-bottom:10px">' +
        '<select class="sel" id="kpiRange">' +
          Object.keys(KPI_RANGES).map(k =>
            '<option value="' + k + '"' + (FC.kpiRange === k ? ' selected' : '') + '>' +
            KPI_RANGES[k] + '</option>').join('') +
        '</select></div>' +
      '<div class="kpis" style="grid-template-columns:repeat(4,1fr)" id="fcKpis">' +
      Array.from({ length: 4 }).map(() =>
        '<div class="kpi"><div class="skel" style="height:12px;width:60%"></div>' +
        '<div class="skel" style="height:26px;width:70%;margin:10px 0"></div>' +
        '<div class="skel" style="height:10px;width:50%"></div></div>').join('') + '</div>' +

      '<div class="card">' +
        '<div class="chead">' +
          '<div class="cL"><h2>逐日趋势</h2></div>' +
          '<div class="cR">' + scopeButtons(FC.scope) + '</div>' +
        '</div>' +
        '<div class="cks">' + trendCheckboxes() + '</div>' +
        '<div class="chart-wrap"><svg id="trendChart"></svg></div>' +
      '</div>' +

      '<div class="grid2 even">' +
        '<div class="card"><div class="chead"><h2>选择日期</h2>' +
          '<div class="rgs"><button class="rg" id="calPrev">‹ 上月</button>' +
          '<span class="u" id="calMonth" style="align-self:center"></span>' +
          '<button class="rg" id="calNext">下月 ›</button></div></div>' +
          '<div class="cal-head">' + ['一', '二', '三', '四', '五', '六', '日'].map(w => '<span>' + w + '</span>').join('') + '</div>' +
          '<div class="cal" id="calGrid"></div>' +
          '<div class="note" style="color:var(--t4);font-size:11px;margin-top:8px">可选范围 ' +
            esc((cal.range || {}).min_date || '') + ' ~ ' + esc((cal.range || {}).max_date || '') +
            '（历史日 + 未来一个月）· 历史日格内为实测 kWh，未来日为预测 kWh</div></div>' +
        '<div class="card"><div class="chead">' +
          '<div class="cL"><h2 id="dayTitle">当日对照<span class="u">05:00–19:00 · 15 分钟 · kW</span></h2></div>' +
          '<div class="cR"><span class="lg">' +
            '<span><i style="border-color:var(--green)"></i>实测 / 估算 kW</span>' +
            '<span><i class="dash" style="border-color:var(--blurple)"></i>模型 / 预测 kW</span></span></div>' +
          '</div>' +
          '<div class="chart-wrap"><svg id="dayChart"></svg></div></div>' +
      '</div>' +

      '<div class="card"><div class="chead">' +
        '<div class="cL"><h2>历史回测偏差<span class="u">实测 − 预测，%</span>' +
        '<span class="u" id="devTip"></span></h2></div>' +
        '<div class="cR"><span class="lg">' +
          '<span><i class="box" style="background:rgba(242,84,91,.75)"></i>超发</span>' +
          '<span><i class="box" style="background:rgba(53,237,126,.75)"></i>欠发</span>' +
          '<span class="u">浅绿带内 |偏差| ≤ 15% 为达标</span></span></div>' +
        '</div>' +
        '<div class="chart-wrap"><svg id="devChart"></svg></div></div>' +

      '<div class="grid2">' +
        '<div class="card"><div class="chead"><h2>历史实际数据<span class="u">实测 / 模型回算 / 偏差</span></h2>' +
        '<input id="hiSearch" class="rg" style="background:var(--inner);color:var(--t2);border:none;outline:none" placeholder="搜索日期…"></div>' +
        '<div id="hiMeta" class="note" style="color:var(--t4);font-size:11px;line-height:1.7;margin:-2px 0 8px"></div>' +
        '<div class="tbl-wrap" style="max-height:420px"><table><thead id="hiHead"></thead><tbody id="hiBody"></tbody></table></div></div>' +
        '<div class="card"><div class="chead"><h2>月度汇总</h2></div><div id="hiMonths"></div></div>' +
      '</div>' +

      '<div class="card"><div class="chead"><h2>预测说明</h2><span class="u">依据 · 方法 · 不确定性</span></div>' +
        '<div id="expBox"></div></div>';

    drawTrendChart(document.getElementById('trendChart'));
    bindScopeSwitch();
    bindTrendChecks();
    const ksel = document.getElementById('kpiRange');
    if (ksel) ksel.onchange = () => {
      FC.kpiRange = ksel.value;
      syncHash();
      renderRangeKpi();
      drawDeviation();     // 偏差回测窗口跟随 KPI 范围联动
    };
    drawDeviation();
    renderExplain(ex);
    mountCalendar(cal);
    loadDay(FC.date);
    renderRangeKpi();
    renderDailyTable();
    syncHash();          // 首次进入也把默认视图写进 URL，此后随时可分享/刷新还原
  }

  /* KPI 卡片：按统计范围（当日 / 近 7 天 / 近 30 天）汇总实测、模型、偏差、准确率，
     口径注明统计起止日期；「当日」档跟随日历选中日期 */
  async function renderRangeKpi() {
    const box = document.getElementById('fcKpis');
    if (!box) return;
    let d;
    try { d = await API.daily(0); }
    catch (e) { box.innerHTML = '<div class="note" style="color:var(--red)">加载失败：' + esc(e.message) + '</div>'; return; }
    const rows = d.rows || [];
    const short = s => (s || '').slice(5).replace('-', '/');
    const todayD = (rows.find(r => r.is_today) || rows[0] || {}).date;
    const cards = [];

    if (FC.kpiRange === 'today') {
      const r = rows.find(x => x.date === FC.date) || rows.find(x => x.is_today);
      if (!r) { box.innerHTML = ''; return; }
      const has = r.actual_kwh != null;
      if (has) {
        cards.push(
          { cls: 'g', label: r.date + ' 实测发电', v: fmtK(r.actual_kwh), u: 'kWh',
            note: r.is_today && r.measured_until ? '统计 ' + short(r.date) + ' · 截至 ' + r.measured_until.slice(11, 16)
                                                 : '统计 ' + short(r.date) },
          { cls: 'p', label: short(r.date) + ' 模型电量', v: fmtK(r.forecast_kwh), u: 'kWh',
            note: '统计 ' + short(r.date) },
          { cls: r.dev_pct == null ? 'm' : (r.dev_pct > 0 ? 'r' : (r.dev_pct < 0 ? 'g' : 'm')), label: '偏差（当日）',
            v: r.dev_pct == null ? '—' : (r.dev_pct >= 0 ? '+' : '') + fmt(r.dev_pct, 1) + '%', u: '',
            note: r.dev_pct == null ? '缺实测或模型值' :
              (r.dev_pct > 0 ? '超发 · 实测高于模型' : (r.dev_pct < 0 ? '欠发 · 实测低于模型' : '实测与模型一致')) +
              ' · 统计 ' + short(r.date) },
          { cls: r.accuracy_pct == null ? 'm' : (Math.max(0, r.accuracy_pct) >= 85 ? 'g' : 'b'), label: '准确率',
            v: r.accuracy_pct == null ? '—' : fmt(Math.max(0, r.accuracy_pct), 1) + '%', u: '',
            note: '1 − |实测 − 模型| ÷ 模型（下限 0） · 统计 ' + short(r.date) });
      } else {
        cards.push(
          { cls: 'p', label: r.date + ' 预测电量', v: fmtK(r.forecast_kwh), u: 'kWh', note: '未来日期 · 尚无实测 · 统计 ' + short(r.date) },
          { cls: 'b', label: '预测辐照', v: fmt(r.irradiance), u: 'kWh/m²', note: '统计 ' + short(r.date) },
          { cls: 'm', label: '偏差 / 准确率', v: '—', u: '', note: '待该日产生实测后自动对账' },
          { cls: 'm', label: '预测依据', v: '—', u: '', note: r.basis || '—' });
      }
    } else {
      const n = FC.kpiRange === 'd7' ? 7 : 30;
      const end = todayD;
      const startD = end ? _ymd(new Date(new Date(end + 'T00:00:00').getTime() - (n - 1) * 86400000)) : null;
      const sel = rows.filter(r => r.date >= startD && r.date <= end &&
        r.actual_kwh != null && r.forecast_kwh != null && r.status !== 'no_generation');
      const sumA = sel.reduce((s, r) => s + (r.actual_kwh || 0), 0);
      const sumF = sel.reduce((s, r) => s + (r.forecast_kwh || 0), 0);
      const dev = sumF > 0 ? (sumA - sumF) / sumF * 100 : null;
      const acc = dev == null ? null : Math.max(0, 100 - Math.abs(dev));   // 夹取 [0,100]
      const span = sel.length ? short(sel[sel.length - 1].date) + ' ~ ' + short(sel[0].date)
                              : (startD ? short(startD) + ' ~ ' + short(end) : '—');
      cards.push(
        { cls: 'g', label: KPI_RANGES[FC.kpiRange] + '实测发电', v: fmtK(sumA), u: 'kWh',
          note: '统计 ' + span + ' · ' + sel.length + ' 天有效数据' },
        { cls: 'p', label: KPI_RANGES[FC.kpiRange] + '模型电量', v: fmtK(sumF), u: 'kWh',
          note: '同期模型回算合计 · 统计 ' + span },
        { cls: dev == null ? 'm' : (dev > 0 ? 'r' : (dev < 0 ? 'g' : 'm')), label: '偏差',
          v: dev == null ? '—' : (dev >= 0 ? '+' : '') + fmt(dev, 1) + '%', u: '',
          note: dev == null ? '统计区间内缺实测/模型' :
            (dev > 0 ? '超发 · 实测高于模型' : (dev < 0 ? '欠发 · 实测低于模型' : '实测与模型一致')) + ' · 统计 ' + span },
        { cls: acc == null ? 'm' : (acc >= 85 ? 'g' : 'b'), label: '准确率',
          v: acc == null ? '—' : fmt(acc, 1) + '%', u: '',
          note: acc == null ? '统计区间内缺实测/模型' : '1 − |实测 − 模型| ÷ 模型 · 统计 ' + span });
    }
    box.innerHTML = cards.map(c =>
      '<div class="kpi ' + c.cls + '"><div class="label">' + esc(c.label) + '</div>' +
      '<div class="value">' + (c.v == null || c.v === '' ? '—' : c.v) + '<small>' + (c.u || '') + '</small></div>' +
      '<div class="note">' + esc(c.note) + '</div></div>').join('');
  }

  /* 日历：历史日（实测）+ 今天 + 未来 30 天（预测）都可点 */
  function mountCalendar(cal) {
    const byDate = new Map((cal.days || []).map(d => [d.date, d]));
    const rng = cal.range || {};
    const grid = document.getElementById('calGrid');
    const $m = document.getElementById('calMonth');
    const minM = (rng.min_date || '').slice(0, 7), maxM = (rng.max_date || '').slice(0, 7);

    function draw() {
      $m.textContent = FC.month.replace('-', ' 年 ') + ' 月';
      document.getElementById('calPrev').disabled = FC.month <= minM;
      document.getElementById('calNext').disabled = FC.month >= maxM;
      const [yy, mm] = FC.month.split('-').map(Number);
      const first = new Date(yy, mm - 1, 1);
      const lead = (first.getDay() + 6) % 7;               // 周一开头
      const total = new Date(yy, mm, 0).getDate();
      let html = '';
      for (let i = 0; i < lead; i++) html += '<div class="cd blank"></div>';
      for (let d = 1; d <= total; d++) {
        const ds = FC.month + '-' + String(d).padStart(2, '0');
        const rec = byDate.get(ds);
        const ok = rec && ds >= (rng.min_date || '') && ds <= (rng.max_date || '');
        // 历史日显示实测，未来日显示预测；今天两者都有，优先实测
        const val = ok ? (rec.actual_kwh != null ? rec.actual_kwh : rec.forecast_kwh) : null;
        html += '<div class="cd' + (ok ? '' : ' off') + (rec && rec.is_future ? ' fut' : '') +
          (ds === FC.date ? ' on' : '') + '"' + (ok ? ' data-d="' + ds + '"' : '') + '>' +
          '<b>' + d + '</b><span>' + (ok ? (val == null ? '—' : Math.round(val)) : '—') + '</span></div>';
      }
      grid.innerHTML = html;
      grid.querySelectorAll('.cd[data-d]').forEach(el => {
        el.onclick = () => {
          FC.date = el.dataset.d;
          grid.querySelectorAll('.cd').forEach(x => x.classList.toggle('on', x === el));
          syncHash();
          loadDay(FC.date);
          if (FC.kpiRange === 'today') renderRangeKpi();   // 「当日」档跟随选中日期
        };
      });
    }
    const ym = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
    document.getElementById('calPrev').onclick = () => {
      const [yy, mm] = FC.month.split('-').map(Number);
      FC.month = ym(new Date(yy, mm - 2, 1));
      syncHash();
      draw();
    };
    document.getElementById('calNext').onclick = () => {
      const [yy, mm] = FC.month.split('-').map(Number);
      FC.month = ym(new Date(yy, mm, 1));
      syncHash();
      draw();
    };
    draw();
  }

  /* 历史回测偏差：实测 − 预测（%），超发红/欠发绿，浅绿带 = 考核容差。
     回测窗口跟随 KPI 统计范围（当日/近7天 → 近7天，近30天 → 近30天），
     避免上方 KPI 换了范围、下方图还停在 30 天的认知错位。 */
  const DEV_WINDOW = { today: 7, d7: 7, d30: 30 };
  async function drawDeviation() {
    const el = document.getElementById('devChart');
    if (!el) return;
    const nd = DEV_WINDOW[FC.kpiRange] || 30;
    try {
      const d = await API.daily(nd);
      const rows = (d.rows || []).slice().reverse()
        .filter(r => r.gap_pct != null)
        .map(r => ({ t: (r.date || '').slice(5), gap_pct: r.gap_pct }));
      if (!rows.length) {
        el.innerHTML = '<text x="640" y="140" fill="#8B90A8" font-size="13" text-anchor="middle">暂无回测数据</text>';
        return;
      }
      const tol = d.pass_line_pct != null ? Math.round(100 - d.pass_line_pct) : 15;
      // 颜色规则：超发（实测 > 模型）红、欠发绿 —— 与「历史实际数据」表口径一致
      Charts.deviationChart(el, { points: rows, tolerance_pct: tol, labelCount: 15,
        colors: { up: '#F2545B', down: '#35ED7E' } });
      const tip = document.getElementById('devTip');
      if (tip) {
        const pass = rows.filter(r => Math.abs(r.gap_pct) <= tol).length;
        tip.textContent = '统计 ' + rows[0].t + ' ~ ' + rows[rows.length - 1].t +
          ' · 跟随统计范围「' + KPI_RANGES[FC.kpiRange] + '」' + (FC.kpiRange === 'today' ? '（当日档按近 7 天展示）' : '') +
          ' · ' + rows.length + ' 天回测 · 达标 ' + pass + ' 天（' +
          Math.round(pass / rows.length * 100) + '%）';
      }
    } catch (e) {
      el.innerHTML = '<text x="640" y="140" fill="#F0655A" font-size="13" text-anchor="middle">加载失败：' +
      esc(e.message) + '</text>';
    }
  }

  async function loadDay(date) {
    const title = document.getElementById('dayTitle');
    const svg = document.getElementById('dayChart');
    if (svg) svg.innerHTML = '<text x="310" y="150" fill="#8B90A8" font-size="13" text-anchor="middle">加载中…</text>';
    let d;
    try { d = await API.forecastDay(date, 15); }
    catch (e) { if (svg) svg.innerHTML = '<text x="310" y="150" fill="#F0655A" font-size="13" text-anchor="middle">' + esc(e.message) + '</text>'; return; }
    if (d.error) {
      if (svg) svg.innerHTML = '<text x="310" y="150" fill="#F0655A" font-size="13" text-anchor="middle">' + esc(d.error) + '</text>';
      const box = document.getElementById('fcKpis');
      if (box) box.innerHTML = '';
      return;
    }
    if (title) {
    title.textContent = d.date.slice(5) + ' ' + d.weekday + ' · ' +
      (d.is_future ? '未来预测' : (d.has_daily_actual ? '实测 vs 模型' : '无实测记录')) +
      (d.dynamic ? ' · 动态修正 ×' + d.dynamic.ratio : '');
    }
    // 双曲线：实测/估算（绿）vs 模型/预测（蓝紫虚线），X 轴固定 05:00–19:00
    const pts = (d.hours || [])
      .filter(h => h.t >= '05:00' && h.t <= '19:00')
      .map(h => ({ t: h.t,
        vals: { a: (h.value_kwh != null ? h.value_kwh : h.actual_kwh), f: h.forecast_kw },
        today: false,
        note: (h.partial ? '采样中 · ' : (h.estimated ? '估算（按历史日内分布分摊） · ' : '')) +
              (h.samples ? '采样 ' + h.samples + ' 点' : '') }));
    const anyMeasured = (d.hours || []).some(h => h.actual_kwh != null);
    Charts.multiLineChart(svg, {
      points: pts, w: 620, h: 300, labelCount: 8,
      series: [
        // 历史日无日内实测曲线时，实测线全部来自分摊估算 → 画虚线以示区分
        { key: 'a', name: anyMeasured ? '实测' : '估算', color: '#35ED7E', unit: ' kW',
          dash: !anyMeasured, width: 2.4 },
        { key: 'f', name: '模型 / 预测', color: '#6C73F5', unit: ' kW', dash: true }
      ],
      emptyMsg: '暂无数据'
    });
  }

  /* 历史实际数据：多源实测合并后的逐日明细（最早一期 ~ 最新一期，一条不丢）+ 月度汇总。
     表头可点排序（M1）：th 原本就有 pointer 样式却没绑事件，属于"看起来能点"的欺骗交互。
     数值列缺测值恒排末尾，不参与大小比较。 */
  const HI_COLS = [
    { k: 'date', label: '日期' },
    { k: 'irradiance', label: '辐照 kWh/m²' },
    { k: 'actual_kwh', label: '实测 kWh' },
    { k: 'forecast_kwh', label: '模型 kWh' },
    { k: 'dev_pct', label: '偏差' },
    { k: 'accuracy_pct', label: '准确率' },
    { k: 'status', label: '状态' },
  ];
  const HI_STATUS_ORD = { ok: 0, irr_estimated: 1, irr_missing: 2, no_generation: 3, irr_only: 4, no_actual: 5 };
  async function renderDailyTable() {
    const head = document.getElementById('hiHead');
    const body = document.getElementById('hiBody');
    if (!body) return;
    let d;
    try { d = await API.daily(0); }                      // 0 = 全部，不再按条数截断
    catch (e) { body.innerHTML = '<tr><td colspan="7">' + esc(e.message) + '</td></tr>'; return; }
    const rows = d.rows || [];
    let sort = { key: 'date', dir: -1 };                 // 默认日期倒序（最新在上）
    const SRC = { api: '北向API', dev: '逆变器合计', curve: '功率曲线', irr: '气象仪', archive: '旧版归档' };
    const STATUS = {
      ok: ['ok', '正常'],
      irr_estimated: ['warn', '辐照推算'],
      irr_missing: ['warn', '辐照缺测'],
      no_generation: ['mute', '停运/并网前'],
      irr_only: ['mute', '仅辐照'],
      no_actual: ['mute', '无电量记录'],
    };
    const srcText = r => (r.sources || []).map(k => SRC[k] || k).join(' + ');
    // 一行里塞不下的信息（来源 / 实测峰值 / 实测曲线覆盖时段）放进悬停提示
    const rowTip = r => {
      const parts = ['来源：' + (srcText(r) || '—')];
      if (r.peak_kw != null) parts.push('实测峰值 ' + fmt(r.peak_kw, 1) + ' kW');
      if (r.measured_hours) parts.push('实测曲线 ' + (r.measured_from || '').slice(11, 16) +
        '–' + (r.measured_until || '').slice(11, 16) + '（' + r.measured_hours + ' 小时）');
      if (r.irradiance_raw != null && r.irradiance == null) parts.push('辐照原始值 ' + fmt(r.irradiance_raw, 3) + '（缺测占位）');
      return parts.join(' · ');
    };
    // 合并口径：数据从哪来、怎么去重、有没有丢记录，都写在表头上
    const c = d.counts || {}, mg = d.merge || {};
    const meta = document.getElementById('hiMeta');
    if (meta) {
      const parts = [
        (d.first_day || '—') + ' ~ ' + (d.last_day || '—'),
        '共 ' + (d.total_days || 0) + ' 期（实测 ' + (c.with_actual || 0) +
          ' · 停运/并网前 ' + (c.no_generation || 0) + ' · 仅辐照 ' + (c.irr_only || 0) + '）',
        '来源 ' + ((mg.sources || []).map(s => esc(s.label)).join(' / ') || '—'),
        '合并 ' + (mg.ingested || 0) + ' 条 · 去重 ' + (mg.duplicates || 0) +
          ' 条 · 保留 ' + (d.total_days || 0) + ' 期 · 丢弃 0 条',
      ];
      meta.innerHTML = parts.join(' · ');
    }
    const months = {};
    rows.forEach(r => {
      const m = (r.date || '').slice(0, 7);
      const o = months[m] = months[m] || { actual: 0, pred: 0, days: 0, stall: 0 };
      if (r.status === 'no_generation') { o.stall++; return; }
      if (r.actual_kwh != null) { o.actual += r.actual_kwh; o.days++; }
      if (r.forecast_kwh != null) o.pred += r.forecast_kwh;
    });
    let curList = rows;
    // 缺测值（null）恒排末尾，不参与大小比较，避免空值混在有效数据中间
    const sortList = (list) => list.slice().sort((a, b) => {
      const k = sort.key;
      if (k === 'date') {
        const va = a.date || '', vb = b.date || '';
        return (va < vb ? -1 : va > vb ? 1 : 0) * sort.dir;
      }
      if (k === 'status') {
        const va = HI_STATUS_ORD[a.status] != null ? HI_STATUS_ORD[a.status] : 9;
        const vb = HI_STATUS_ORD[b.status] != null ? HI_STATUS_ORD[b.status] : 9;
        return (va - vb) * sort.dir;
      }
      const an = a[k] == null, bn = b[k] == null;
      if (an && bn) return 0;
      if (an) return 1;
      if (bn) return -1;
      return (a[k] - b[k]) * sort.dir;
    });
    const renderHead = () => {
      if (!head) return;
      head.innerHTML = '<tr>' + HI_COLS.map(c =>
        '<th data-k="' + c.k + '"' + (sort.key === c.k ? ' style="color:var(--t2)"' : '') + '>' +
        c.label + (sort.key === c.k ? (sort.dir > 0 ? ' ▲' : ' ▼') : '') + '</th>').join('') + '</tr>';
      head.querySelectorAll('th').forEach(th => {
        th.onclick = () => {
          if (sort.key === th.dataset.k) sort.dir = -sort.dir;
          else { sort.key = th.dataset.k; sort.dir = -1; }   // 换列默认倒序（最大/最新在前）
          renderHead();
          renderRows(curList);
        };
      });
    };
    const renderRows = (list) => {
      curList = list;
      const sorted = sortList(list);
      body.innerHTML = sorted.length ? sorted.map(r => {
        const st = STATUS[r.status] || ['mute', r.status || '—'];
        const label = st[1] + (r.is_today && r.partial ? ' · 进行中' : '');
        return '<tr><td title="' + esc(rowTip(r)) + '">' + esc(r.date) + '</td>' +
          '<td>' + fmt(r.irradiance) + '</td>' +
          '<td>' + fmtK(r.actual_kwh) + '</td>' +
          '<td>' + fmtK(r.forecast_kwh) + '</td>' +
          '<td>' + (r.dev_pct == null ? '—' : '<span class="tag ' +
            (r.dev_pct > 0 ? 'err' : (r.dev_pct < 0 ? 'ok' : 'mute')) + '">' +
            (r.dev_pct > 0 ? '+' : '') + fmt(r.dev_pct, 1) + '%</span>') + '</td>' +
          '<td>' + (r.accuracy_pct == null ? '—' : fmt(r.accuracy_pct, 1) + '%') + '</td>' +
          '<td><span class="tag ' + st[0] + '">' + esc(label) + '</span></td></tr>';
      }).join('')
        : '<tr><td colspan="7">' + emptyBox('无匹配数据', '换个关键词试试') + '</td></tr>';
    };
    renderHead();
    renderRows(rows);
    const s = document.getElementById('hiSearch');
    if (s) s.oninput = e => {
      const q = e.target.value.trim();
      renderRows(q ? rows.filter(r => (r.date || '').indexOf(q) >= 0) : rows);
    };
    const mb = document.getElementById('hiMonths');
    if (mb) mb.innerHTML = '<div class="kv">' +
      Object.keys(months).sort().reverse().map(m =>
        '<div class="k">' + esc(m) + '</div><div class="v">' + fmtK(months[m].actual) + ' kWh · ' +
        months[m].days + ' 天 · 模型 ' + fmtK(months[m].pred) + ' kWh' +
        (months[m].stall ? ' · 停运 ' + months[m].stall + ' 天' : '') + '</div>').join('') + '</div>';
  }

  /* 预测说明 */
  function renderExplain(ex) {
    const box = document.getElementById('expBox');
    if (!box) return;
    const h = ex.history || {}, irr = ex.irradiance_source || {}, k = ex.k || {}, fw = ex.future_weather || {};
    const months = Object.entries(ex.monthly_typical || {}).map(([m, v]) =>
      '<span class="tag mute" style="margin:0 6px 6px 0">' + esc(m) + ' · ' + fmt(v) + ' kWh/m²</span>').join('') || '—';
    box.innerHTML =
      '<div class="kv">' + [
        ['预测公式', ex.formula],
        ['综合效率 K', fmt(k.value, 3) + '（' + (k.source === 'calibrated' ? '由 ' + k.samples + ' 天实测样本自动标定' : '经验默认值') + '）· ' + esc(k.meaning || '')],
        ['历史样本', (h.from || '—') + ' ~ ' + (h.to || '—') + ' · 共 ' + h.days + ' 天电量记录，其中 ' + h.days_with_irradiance + ' 天带实测辐照，累计 ' + fmtK(h.total_kwh) + ' kWh'],
        ['辐照来源', esc(irr.label || '') + '（' + esc(irr.mode || '') + '）' + (irr.as_of ? ' · 最新 ' + esc(irr.as_of) : '')],
        ['未来天气依据', esc(fw.method || '') + '：' + ((fw.detail || []).map(x => esc(x)).join('；'))],
        ['季节因素', '按月给出月典型辐照（历史同月日均），无历史月份用近 30 天日均 × 季节因子外推：' ],
        ['日内曲线', esc(ex.profile || '')],
        ['模型准确率', (ex.accuracy && ex.accuracy.avg_pct != null) ? fmt(ex.accuracy.avg_pct, 1) + '%（' + ex.accuracy.samples + ' 天）· ' + esc(ex.accuracy.note || '') : '样本不足'],
      ].map(([a, b]) => '<div class="k">' + esc(a) + '</div><div class="v">' + b + '</div>').join('') + '</div>' +
      '<div style="margin-top:10px">' + months + '</div>' +
      '<div style="margin-top:12px;color:var(--yellow);font-size:12px;line-height:1.9">' +
        (ex.caveats || []).map(c => '· ' + esc(c)).join('<br>') + '</div>';
  }

  /* ---------------- 页面：逆变器监控 ---------------- */
  const INV_COLS = [
    { k: 'idx', label: '#' }, { k: 'name', label: '设备名称' },
    { k: 'kwh', label: '发电量(kWh)' }, { k: 'peak_kw', label: '峰值功率(kW)' },
    { k: 'hours', label: '并网时长(h)' }, { k: 'share', label: '占比' },
  ];
  async function pageInverters() {
    $view.innerHTML = '<div class="card"><div class="skel" style="height:340px"></div></div>';
    let d;
    try { d = await API.inverters(); }
    catch (e) { $view.innerHTML = errorBox(e.message, 'retryInv'); bindRetry(pageInverters); return; }
    if (d.status !== 'ok' || !d.rows.length) {
      $view.innerHTML = '<div class="card">' + emptyBox('逆变器数据不可用',
        (d.error || '数据源未返回数据') + '。请在 config.json → sources.inverters 配置 api 或导出文件路径。') + '</div>';
      return;
    }
    // 「峰值合计（各机相加）」没有业务意义：各机峰值不在同一时刻到达，相加会显著高估
    // 电站峰值 → 改为「单机最高峰值」。列表上方补一条发电量排行条（M8：光有表没有可视化）。
    const maxPeak = Math.max.apply(null, d.rows.map(r => r.peak_kw || 0).concat([0]));
    const top = d.rows.slice(0, 8);
    const maxKwh = Math.max.apply(null, top.map(r => r.kwh || 0).concat([1]));
    $view.innerHTML =
      '<div class="kpis" style="grid-template-columns:repeat(3,1fr)">' +
      [['设备数', d.count, '台', d.date || ''], ['当日合计', fmtK(d.total_kwh), 'kWh', ''],
       ['单机最高峰值', fmt(maxPeak, 1), 'kW', '当日各机峰值最大值（峰值不同时到达，不宜相加）']]
      .map(([l, v, u, n]) => '<div class="kpi"><div class="label">' + l + '</div><div class="value">' + v +
        '<small>' + u + '</small></div><div class="note">' + esc(n) + '</div></div>').join('') + '</div>' +
      '<div class="card"><div class="chead"><h2>逆变器列表<span class="u">' + esc(d.date || '') +
      ' · 表头点击排序</span></h2>' +
      '<input id="invSearch" class="rg" style="background:var(--inner);color:var(--t2);border:none;outline:none" placeholder="搜索设备名…"></div>' +
      '<div class="rk" style="margin-bottom:14px">' + top.map(r =>
        '<div class="row"><span class="n">' + esc(r.name || '—') + '</span>' +
        '<span class="bar"><i style="width:' + Math.max(2, Math.round((r.kwh || 0) / maxKwh * 100)) + '%"></i></span>' +
        '<span class="v">' + fmtK(r.kwh) + '</span></div>').join('') + '</div>' +
      '<div class="tbl-wrap" style="max-height:460px"><table><thead id="invHead"></thead><tbody id="invBody"></tbody></table></div></div>';
    const head = document.getElementById('invHead');
    const body = document.getElementById('invBody');
    let sort = { key: 'kwh', dir: -1 };
    let curList = d.rows;
    const sortList = (list) => list.slice().sort((a, b) => {
      const k = sort.key;
      if (k === 'name') {
        const va = a.name || '', vb = b.name || '';
        return (va < vb ? -1 : va > vb ? 1 : 0) * sort.dir;
      }
      if (k === 'idx') return (a._i - b._i) * sort.dir;
      const va = k === 'share' ? (d.total_kwh ? a.kwh / d.total_kwh : null) : a[k];
      const vb = k === 'share' ? (d.total_kwh ? b.kwh / d.total_kwh : null) : b[k];
      const an = va == null, bn = vb == null;
      if (an && bn) return 0;
      if (an) return 1;
      if (bn) return -1;
      return (va - vb) * sort.dir;
    });
    const renderHead = () => {
      head.innerHTML = '<tr>' + INV_COLS.map((c, i) =>
        '<th data-k="' + c.k + '"' + (sort.key === c.k ? ' style="color:var(--t2)"' : '') + '>' +
        c.label + (sort.key === c.k ? (sort.dir > 0 ? ' ▲' : ' ▼') : '') + '</th>').join('') + '</tr>';
      head.querySelectorAll('th').forEach(th => {
        th.onclick = () => {
          if (sort.key === th.dataset.k) sort.dir = -sort.dir;
          else { sort.key = th.dataset.k; sort.dir = -1; }
          renderHead();
          draw(curList);
        };
      });
    };
    const draw = (list) => {
      curList = list;
      const sorted = sortList(list);
      body.innerHTML = sorted.length ? sorted.map((r, i) =>
        '<tr><td>' + (i + 1) + '</td><td>' + esc(r.name) + '</td><td>' + fmtK(r.kwh) + '</td>' +
        '<td>' + fmt(r.peak_kw, 1) + '</td><td>' + fmt(r.hours, 1) + '</td>' +
        '<td>' + (d.total_kwh ? fmt(r.kwh / d.total_kwh * 100, 1) + '%' : '—') + '</td></tr>').join('')
        : '<tr><td colspan="6">' + emptyBox('无匹配设备', '') + '</td></tr>';
    };
    d.rows.forEach((r, i) => { r._i = i; });
    renderHead();
    draw(d.rows);
    document.getElementById('invSearch').oninput = e => {
      const q = e.target.value.trim().toLowerCase();
      draw(q ? d.rows.filter(r => (r.name || '').toLowerCase().indexOf(q) >= 0) : d.rows);
    };
  }

  /* ---------------- 页面：电站档案 ---------------- */
  async function pageSite() {
    let s;
    try { s = await API.site(); } catch (e) { $view.innerHTML = errorBox(e.message, 'retrySite'); bindRetry(pageSite); return; }
    const st = s.station || {}, fin = s.finance || {};
    $view.innerHTML =
      '<div class="grid2 even">' +
      '<div class="card"><div class="chead"><h2>电站信息</h2></div><div class="kv">' +
      [['电站名称', st.name], ['电站编号', st.station_code], ['装机容量', fmt(st.capacity_mwp, 3) + ' MWp'],
       ['并网时间', st.grid_date], ['所在地', st.location], ['业主', st.owner]]
      .map(([a, b]) => '<div class="k">' + esc(a) + '</div><div class="v">' + esc(b || '—') + '</div>').join('') + '</div></div>' +
      '<div class="card"><div class="chead"><h2>运行配置</h2></div><div class="kv">' +
      [['轮询间隔', s.poll.interval_seconds + ' 秒'], ['过期阈值', s.poll.stale_after_seconds + ' 秒'],
       ['模型 K', (s.model.k == null ? '自动标定' : s.model.k) + (s.model.auto_calibrate ? '（自动）' : '')],
       ['考核线', s.model.pass_line_pct + '%'],
       ['综合电价', fmtP(fin.price_yuan_per_kwh) + ' 元/kWh' + (fin.note ? '（' + esc(fin.note) + '）' : '')],
       ['分析范围', '首个有发电记录日 ~ 今天 + 30 天']]
      .map(([a, b]) => '<div class="k">' + esc(a) + '</div><div class="v">' + esc(b) + '</div>').join('') + '</div></div>';
  }

  /* ---------------- 页面：数据源状态 ---------------- */
  async function pageStatus() {
    const st = await refreshStatus(true);
    if (!st) { $view.innerHTML = errorBox('无法获取数据服务状态', 'retrySt'); bindRetry(pageStatus); return; }
    const items = Object.entries(st.sources).map(([key, s]) => {
      const map = { ok: '正常', error: '异常', empty: '空数据', unavailable: '未配置' };
      const cls = s.status === 'ok' ? dotClass(s.freshness) : dotClass(s.status);
      return '<div class="src"><div class="n">' + esc(s.label || key) +
        '<small>' + esc(s.mode || '') + ' · 最新数据 ' + (s.as_of ? esc(s.as_of) + '（' + ageText(s.age_seconds) + '）' : '无') +
        (s.latency_ms ? ' · 耗时 ' + s.latency_ms + 'ms' : '') +
        (s.error ? ' · ' + esc(s.error) : '') + '</small></div>' +
        '<div class="s"><i class="dot ' + cls + '"></i>' + esc(map[s.status] || s.status) + '</div></div>';
    }).join('');
    $view.innerHTML =
      '<div class="card"><div class="chead"><h2>数据源状态</h2>' +
      '<div class="rgs"><button class="btn sm" id="btnAll">全部刷新</button></div></div>' + items + '</div>' +
      '<div class="card"><div class="chead"><h2>导入气象仪数据' +
        '<span class="u">手动上传 · .xls / .xlsx · 一个月一个文件</span></h2></div>' +
        '<div class="note" style="color:var(--t4);font-size:11.5px;line-height:1.75">' +
        '从 <b style="color:var(--t3)">gl.tubanginfo.com</b> 后台按月导出后在这里上传：' +
        '解析 → 预览（识别到的列、覆盖日期、新增/覆盖天数、异常日）→ 确认才入库。' +
        '入库落到 <b style="color:var(--t3)">data/weather_station.json</b>，' +
        '按日保存「日辐照 kWh/m² + 逐时辐照 W/m²」，同日重复导入以后一次为准（幂等）。' +
        '入库后地面实测辐照优先于 Open-Meteo：历史回算、当月辐照、当日图右轴都会改用它。</div>' +
        '<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0">' +
          '<input type="file" id="wxFile" accept=".xls,.xlsx" multiple ' +
            'style="font-size:12px;color:var(--t3);background:var(--bg2);border:1px solid var(--line);' +
            'border-radius:8px;padding:7px 10px;max-width:340px">' +
          '<button class="btn sm" id="wxPrev">解析预览</button>' +
          '<button class="btn sm" id="wxCommit" disabled>确认导入</button>' +
          '<span id="wxMsg" style="font-size:12px;color:var(--t4)"></span>' +
        '</div>' +
        '<div id="wxInfo" class="note" style="color:var(--t4);font-size:11.5px"></div>' +
        '<div id="wxBox" style="margin-top:10px"></div>' +
      '</div>' +
      '<div class="card"><div class="chead"><h2>服务信息</h2></div><div class="kv">' +
      [['服务时间', st.server_time], ['轮询间隔', st.poll_interval_seconds + ' 秒'],
       ['过期阈值', st.stale_after_seconds + ' 秒'], ['已轮询次数', st.poll_count]]
      .map(([a, b]) => '<div class="k">' + esc(a) + '</div><div class="v">' + esc(b) + '</div>').join('') + '</div></div>';
    document.getElementById('btnAll').onclick = async () => {
      document.getElementById('btnAll').textContent = '刷新中…';
      try { await API.refresh(); } catch (e) { }
      pageStatus();
    };
    bindWeatherImport();
  }

  /* ---------------- 气象仪导入卡（预览 → 确认） ---------------- */
  const WX_FLAG = [
    [/^rows_lt_200$/, '采样不足 200 条（理论 288）'],
    [/^night_nonzero$/, '夜间出现非零辐照'],
    [/^integ_dev_(\d+)%$/, (m) => '日累计与逐点积分相差 ' + m[1] + '%'],
  ];
  function wxFlagText(f) {
    for (const [re, txt] of WX_FLAG) {
      const m = String(f).match(re);
      if (m) return typeof txt === 'function' ? txt(m) : txt;
    }
    return f;
  }

  let WX_FILES = null, WX_PREVIEW = null;
  async function bindWeatherImport() {
    const inp = document.getElementById('wxFile');
    const btnPrev = document.getElementById('wxPrev');
    const btnOk = document.getElementById('wxCommit');
    const msg = document.getElementById('wxMsg');
    const box = document.getElementById('wxBox');
    const info = document.getElementById('wxInfo');
    if (!inp || !btnPrev) return;
    const setMsg = (t, c) => { if (msg) { msg.textContent = t || ''; msg.style.color = c || 'var(--t4)'; } };

    // 现有库概况：说清"数据到哪天、什么时候导的、地面标定 K 多少"
    async function loadInfo() {
      try {
        const w = await API.weatherStation();
        if (!info) return;
        info.innerHTML = w.days
          ? '已入库 <b style="color:var(--t3)">' + w.days + ' 天</b>（' + w.first + ' ~ ' + w.last + '）' +
            ' · 最近导入 ' + esc(shortTs(w.updated_at)) +
            ' · 地面标定 K=' + (w.k_ground == null ? '—' : fmt(w.k_ground, 4)) +
            '（' + (w.ground_samples || 0) + ' 个配对日）' +
            (w.flagged_days ? ' · <span style="color:var(--yellow)">' + w.flagged_days + ' 天有质量标记</span>' : '') +
            '<div style="margin-top:4px">今日实测覆盖 <b style="color:var(--t3)">' +
              (w.today_measured_hours || 0) + '/24</b> 小时' +
              ((w.today_measured_hours || 0) < 24 ? '（其余小时仍是预报，图上按小时标「实测/预报」）' : '（全天实测）') +
            '</div>' +
            '<div style="margin-top:4px">生效位置：总览<b>当月辐照</b>（有实测的日期改用实测）· ' +
              '总览<b>当日图右轴辐照</b>（今日已覆盖的小时）· <b>历史回算 K</b> 与当日基线</div>'
          : '尚未导入任何气象仪数据（当前辐照来自 Open-Meteo 与旧目录扫描）';
      } catch (e) { if (info) info.textContent = '导入库状态读取失败：' + e.message; }
    }
    loadInfo();

    inp.onchange = () => {
      WX_FILES = Array.from(inp.files || []);
      WX_PREVIEW = null;
      btnOk.disabled = true;
      if (box) box.innerHTML = '';
      setMsg(WX_FILES.length ? '已选择 ' + WX_FILES.length + ' 个文件，点「解析预览」' +
        (WX_FILES.some(f => f.size > 6 * 1024 * 1024) ? '（文件较大，解析需要几秒）' : '') : '');
    };

    btnPrev.onclick = async () => {
      if (!WX_FILES || !WX_FILES.length) { setMsg('请先选择文件', 'var(--yellow)'); return; }
      btnPrev.disabled = true; setMsg('解析中…');
      try {
        WX_PREVIEW = await API.importWeather(WX_FILES, false);
        renderWxPreview(box, WX_PREVIEW);
        const ok = (WX_PREVIEW.days_parsed || 0) > 0;
        btnOk.disabled = !ok;
        setMsg(ok ? ('解析出 ' + WX_PREVIEW.days_parsed + ' 天，确认后入库')
                  : '未解析出有效日期，检查文件是否为气象仪导出格式', ok ? 'var(--green)' : 'var(--red)');
      } catch (e) { setMsg(e.message, 'var(--red)'); }
      finally { btnPrev.disabled = false; }
    };

    btnOk.onclick = async () => {
      if (!WX_FILES || !WX_FILES.length) return;
      btnOk.disabled = true; setMsg('正在入库…');
      try {
        const r = await API.importWeather(WX_FILES, true);
        if (r.error) { setMsg(r.error, 'var(--red)'); btnOk.disabled = false; return; }
        renderWxPreview(box, r, true);
        setMsg('已入库 ' + (r.days_parsed || 0) + ' 天（新增 ' + (r.new_days || 0) +
               ' / 覆盖 ' + (r.overwrite_days || 0) + '），页面数据已刷新', 'var(--green)');
        await loadInfo();
        // 导入改变了辐照与回算 → 让状态卡与页面数据一起重取
        try { await API.refresh(['irradiance']); } catch (e) { }
      } catch (e) { setMsg(e.message, 'var(--red)'); btnOk.disabled = false; }
    };
  }

  function renderWxPreview(box, r, done) {
    if (!box) return;
    const fl = r.files || [];
    const bad = Object.keys(r.flagged || {});
    const rows = fl.map(f =>
      '<tr><td>' + esc(f.name) + '</td>' +
      '<td>' + (f.days || 0) + '</td>' +
      '<td>' + (f.first ? esc(f.first) + ' ~ ' + esc(f.last) : '—') + '</td>' +
      '<td>' + (f.rows || 0) + '</td>' +
      '<td>' + (f.dup_ts ? '<span style="color:var(--yellow)">' + f.dup_ts + '</span>' : '0') + '</td>' +
      '<td style="color:var(--t4)">' + esc((f.notes || []).join('；') || '—') + '</td></tr>').join('');
    const errs = (r.errors || []).map(e => '<div style="color:var(--red)">' + esc(e.file) + '：' + esc(e.error) + '</div>').join('');
    const samp = (r.sample || []).map(s =>
      '<tr><td>' + esc(s.date) + '</td><td>' + fmt(s.kwh_m2, 2) + '</td><td>' + s.rows + '</td>' +
      '<td style="color:' + (s.flags && s.flags.length ? 'var(--yellow)' : 'var(--t4)') + '">' +
      esc((s.flags || []).map(wxFlagText).join('；') || '正常') + '</td></tr>').join('');
    const badList = bad.slice(0, 6).map(d =>
      '<span style="color:var(--yellow)">' + esc(d) + '</span>（' +
      (r.flagged[d] || []).map(wxFlagText).join('；') + '）').join(' · ');
    box.innerHTML =
      '<div style="font-size:12px;line-height:1.9;margin-bottom:8px">' +
        (done ? '<b style="color:var(--green)">入库结果</b> · ' : '<b style="color:var(--t3)">解析预览</b> · ') +
        '共 <b>' + (r.days_parsed || 0) + '</b> 天' +
        (r.first ? '（' + esc(r.first) + ' ~ ' + esc(r.last) + '）' : '') +
        ' · 新增 <b style="color:var(--green)">' + (r.new_days || 0) + '</b> 天' +
        ' · 覆盖 <b style="color:var(--yellow)">' + (r.overwrite_days || 0) + '</b> 天' +
        ' · 总行数 ' + (r.rows_total || 0) +
      '</div>' + (errs ? '<div style="font-size:12px;margin-bottom:8px">' + errs + '</div>' : '') +
      '<div class="tbl-wrap"><table><thead><tr><th>文件</th><th>天数</th><th>日期范围</th><th>行数</th><th>重复时间戳</th><th>列识别说明</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table></div>' +
      (bad.length ? '<div style="font-size:11.5px;margin-top:8px;line-height:1.8">' +
        '<b style="color:var(--yellow)">质量提示 ' + bad.length + ' 天</b>：' + badList +
        (bad.length > 6 ? ' 等' : '') +
        '<div style="color:var(--t4)">提示只做标注、不改数据：采样不足多为当天导出未满 24h，' +
        '夜间非零多为传感器零点漂移。</div></div>' : '') +
      (samp ? '<div style="font-size:11.5px;margin-top:10px;color:var(--t4)">最近 10 天（日辐照 kWh/m²）</div>' +
        '<div class="tbl-wrap"><table><thead><tr><th>日期</th><th>日辐照 kWh/m²</th><th>采样条数</th><th>质量</th></tr></thead>' +
        '<tbody>' + samp + '</tbody></table></div>' : '');
  }

  /* ---------------- 路由 ---------------- */
  const PAGES = { overview: pageOverview, forecast: pageAnalysis,
                  inverters: pageInverters, site: pageSite, status: pageStatus };

  function currentRoute() {
    const h = (location.hash || '').split('?')[0].replace('#/', '').replace('#', '');
    if (h === 'history') return 'forecast';      // 旧链接兼容：历史统计已并入发电分析
    return ROUTES.some(r => r.id === h) ? h : 'overview';
  }

  function renderNav(id) {
    $nav.innerHTML = ROUTES.map(r =>
      '<a href="' + r.hash + '" class="' + (r.id === id ? 'on' : '') + '"><i></i>' + r.name + '</a>').join('');
    $title.textContent = (ROUTES.find(r => r.id === id) || {}).name || '';
  }

  async function route() {
    const id = currentRoute();
    if (id === 'forecast') applyHashState();     // 从 URL 恢复视图状态（分享/刷新可用）
    currentPage = id;
    renderNav(id);
    hideBanner();
    document.getElementById('side').classList.remove('open');
    await refreshStatus();
    bannerByStatus(id);
    $chipDate.textContent = new Date().toLocaleDateString('zh-CN');
    try { await PAGES[id](); }
    catch (e) { $view.innerHTML = errorBox(e.message || '未知错误', 'retryRt'); bindRetry(route); }
  }

  function bindRetry(fn) {
    const b = document.getElementById('retryOv') || document.getElementById('retryFc') ||
      document.getElementById('retryInv') ||
      document.getElementById('retrySite') || document.getElementById('retrySt') ||
      document.getElementById('retryRt');
    if (b) b.onclick = () => fn();
  }

  /* ---------------- 启动 ---------------- */
  document.getElementById('burger').onclick = () =>
    document.getElementById('side').classList.toggle('open');

  document.getElementById('btnRefresh').onclick = async () => {
    const btn = document.getElementById('btnRefresh');
    btn.textContent = '刷新中…'; btn.disabled = true;
    try { await API.refresh(); } catch (e) { }
    btn.textContent = '立即刷新'; btn.disabled = false;
    route();
  };

  window.addEventListener('hashchange', route);

  (async function boot() {
    try {
      const ov = await API.overview().catch(() => null);
      if (ov && ov.station) {
        document.getElementById('stName').textContent = ov.station.short_name || ov.station.name;
        document.getElementById('stCap').textContent = fmt(ov.station.capacity_mwp, 3) + ' MWp · 分布式';
      }
    } catch (e) { }
    await route();
    // 自动刷新（静默）：只重渲「无输入状态」的页面（总览 / 数据源），保留滚动位置、
    // 跳过骨架屏——整页 innerHTML 重建会闪骨架屏、重置滚动、清空搜索框与悬停态。
    // 发电分析 / 逆变器 / 电站档案含搜索框与选中态，保持现场不被打断（切页时刷新）。
    if (timer) clearInterval(timer);
    const iv = Math.max(60, (STATUS && STATUS.poll_interval_seconds) || 300);
    timer = setInterval(async () => {
      await refreshStatus();
      if (currentPage) bannerByStatus(currentPage);
      if (currentPage !== 'overview' && currentPage !== 'status') return;
      const sc = $view.scrollTop;
      try { await PAGES[currentPage](true); } catch (e) { }
      $view.scrollTop = sc;
    }, iv * 1000);
  })();
})();
