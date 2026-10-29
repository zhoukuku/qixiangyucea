/* 顶部提示文案：随数据源【实际取数模式 + 数据新鲜度】自动显示或隐藏
   · api 模式且数据新鲜  → 不显示任何提示（真正实时）
   · api 模式但数据滞后  → 提示接口未更新
   · export 模式         → 提示导入/接入建议
   · 数据源异常/未配置    → 红色告警
   · 预测口径单独判断，与发电源模式解耦（仅在无气象预报源时提示）
   纯函数，无 DOM 依赖，可直接单测 */
const Banner = (() => {
  function ageText(sec) {
    if (sec == null) return '未知';
    if (sec < 90) return Math.round(sec) + ' 秒前';
    if (sec < 5400) return Math.round(sec / 60) + ' 分钟前';
    if (sec < 86400 * 2) return (sec / 3600).toFixed(1) + ' 小时前';
    return (sec / 86400).toFixed(1) + ' 天前';
  }
  /* 2026-10-27T11:30:00 → 10/27 11:30 */
  function atText(iso) {
    if (!iso) return '—';
    const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/.exec(String(iso));
    return m ? m[2] + '/' + m[3] + ' ' + m[4] + ':' + m[5] : String(iso).slice(0, 16);
  }

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  function build(st, routeId) {
    if (!st) return { level: 'err', msgs: ['无法连接数据服务，请确认 server.py 正在运行。'] };
    const s = st.sources || {};
    const gen = s.generation || {};
    const msgs = [];
    let level = 'info';

    if (gen.status === 'error') {
      msgs.push('发电数据源异常：' + esc(gen.error || '') + '（页面显示的是上一次成功数据）');
      level = 'err';
    } else if (gen.status === 'unavailable') {
      msgs.push('发电数据源未配置：请在 <b>site/config.json</b> 填写 sources.generation（type=api 或 export 路径）');
      level = 'err';
    } else if (gen.status === 'empty') {
      msgs.push('发电数据源返回空数据：请检查导出文件或 API 权限');
      level = 'err';
    } else if (gen.status === 'ok') {
      if (gen.mode === 'export') {
        msgs.push('当前为<b>导出文件</b>模式：数据截至 ' + atText(gen.as_of) + '（' +
          ageText(gen.age_seconds) + '）。导入最新报表或配置北向 API 后自动转为实时');
      } else if (gen.mode === 'api' && gen.freshness === 'stale') {
        msgs.push('<b>北向 API 已接入</b>，但最新数据仍是 ' + atText(gen.as_of) +
          '（' + ageText(gen.age_seconds) + '），请检查接口连通性');
      }
      // api 模式且数据新鲜时：不追加任何提示
    }

    if (routeId === 'forecast' && s.forecast && s.forecast.status !== 'ok') {
      msgs.push('未接入气象预报源：<b>未来辐照按历史同期气候学推算</b>，不是天气预报。接入后（config.json → sources.forecast）自动改用预报值');
    }

    return { level: level, msgs: msgs };
  }

  return { build: build, ageText: ageText, atText: atText };
})();
