// 图表生成器单元测试（无浏览器）：校验 SVG 结构、自闭合、null 断线、无 NaN
// 20261030：lineChart/hourChart 死代码已删除；multiLineChart 改为按量纲分面板（小倍数图）
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const here = dirname(fileURLToPath(import.meta.url));
const code = readFileSync(join(here, '../web/assets/charts.js'), 'utf-8');
eval(code + '\nglobalThis.Charts = Charts;');

const results = [];
const assert = (cond, msg) => { results.push([cond, msg]); console.log((cond ? '[PASS] ' : '[FAIL] ') + msg); };

function stub() {
  const el = {
    innerHTML: '', attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    removeAttribute(k) { delete this.attrs[k]; }
  };
  return el;
}

// 1) 当日对照：实测实线 vs 基线虚线，null 断线，当前时刻竖线
const el7 = stub();
Charts.compareChart(el7, {
  points: [
    { t: '00:00', actual_kw: null, forecast_kw: 0 },
    { t: '06:00', actual_kw: 13.7, forecast_kw: 55.2 },
    { t: '07:00', actual_kw: 122.0, forecast_kw: 210.4 },
    { t: '08:00', actual_kw: null, forecast_kw: 520.9 },
    { t: '09:00', actual_kw: null, forecast_kw: 900.1 }
  ],
  colors: { actual: '#35ED7E', forecast: '#6C73F5' },
  nowHour: 1, labelCount: 12
});
assert(el7.attrs.viewBox === '0 0 1280 380', 'compareChart viewBox 已设置（1280 基准宽）');
assert(el7.attrs.width === '100%', '宽度自适应 100%');
assert(el7.attrs.height === undefined, '不写死 height（高度由 viewBox 比例推导）');
// 预测线：已发生段必须是实线（与实测同权重才能直接比高低），未发生段才是淡化点线
const fPoly = (el7.innerHTML.match(/<polyline[^>]*stroke="#6C73F5"[^>]*>/g) || []);
assert(fPoly.some(t => t.includes('stroke-width="2.4"') && !t.includes('stroke-dasharray')),
  '预测线已发生段为实线（不是虚线）');
assert(el7.innerHTML.includes('stroke-dasharray="2 5"'), '未发生段用淡化点线');
assert(el7.innerHTML.includes('stroke="#6C73F5"'), '基线用蓝紫色');
assert((el7.innerHTML.match(/stroke="#35ED7E"/g) || []).length >= 1, '实测用绿色实线');
assert(el7.innerHTML.includes('stroke="#7C86FF"'), '标注当前时刻竖线');
assert(!/NaN/.test(el7.innerHTML), '当日对照无 NaN 坐标');
const c7s = (el7.innerHTML.match(/<line [^>]*\/>/g) || []).length;
const c7a = (el7.innerHTML.match(/<line /g) || []).length;
assert(c7s === c7a && c7a > 0, '当日对照 line 全部自闭合（' + c7s + '/' + c7a + '）');

// 1b) 当日只有 1 个实测小时（清晨刚出第一点）时也得看得见，不能被吞掉
const el7b = stub();
Charts.compareChart(el7b, {
  points: [
    { t: '00:00', actual_kw: null, forecast_kw: 0 },
    { t: '06:00', actual_kw: 88.4, forecast_kw: 55.2 },
    { t: '07:00', actual_kw: null, forecast_kw: 210.4 }
  ],
  colors: { actual: '#35ED7E', forecast: '#6C73F5' }, labelCount: 12
});
assert(/<circle[^>]*fill="#35ED7E"/.test(el7b.innerHTML), '单个实测点画圆点（不丢实测）');
assert(!/NaN/.test(el7b.innerHTML), '单点当日对照无 NaN 坐标');

// 1c) 未采集时段：淡色底带（无文字标注）
const el7c = stub();
Charts.compareChart(el7c, {
  points: Array.from({ length: 24 }, (_, h) => ({
    t: String(h).padStart(2, '0') + ':00',
    actual_kw: h >= 16 ? 150 : null,
    forecast_kw: h >= 6 && h <= 18 ? 800 : 0
  })),
  colors: { actual: '#35ED7E', forecast: '#6C73F5' }, labelCount: 12,
  band: { from: 6, to: 16 }
});
assert(/<rect[^>]*fill="#8B90A8"[^>]*fill-opacity="0?\.07"/.test(el7c.innerHTML), '未采集时段画淡色底带');
assert(!el7c.innerHTML.includes('未采集'), '底带不再带文字标注');
assert(!/NaN/.test(el7c.innerHTML), '底带无 NaN 坐标');
// 没有 band 时不应凭空出现底带
const el7d = stub();
Charts.compareChart(el7d, {
  points: [{ t: '00:00', actual_kw: 10, forecast_kw: 0 }, { t: '01:00', actual_kw: 20, forecast_kw: 5 }],
  colors: { actual: '#35ED7E', forecast: '#6C73F5' }, labelCount: 12
});
assert(!/<rect[^>]*fill="#8B90A8"/.test(el7d.innerHTML), '未传 band 时不画底带');

// 2) 偏差回测：超发红/欠发绿（颜色由调用方传入）+ ±容差带 + 0 基准 + 跳空
const el8 = stub();
Charts.deviationChart(el8, {
  points: [
    { t: '10-01', gap_pct: 5.2 },
    { t: '10-02', gap_pct: -7.8 },
    { t: '10-03', gap_pct: 0.0 },
    { t: '10-04', gap_pct: null },
    { t: '10-05', gap_pct: 20.5 }
  ],
  tolerance_pct: 15,
  colors: { up: '#35ED7E', down: '#F2545B' }
});
assert(el8.innerHTML.includes('fill="#35ED7E"'), '正偏差（欠发·调用方配色）已渲染');
assert(el8.innerHTML.includes('fill="#F2545B"'), '负偏差（超发·调用方配色）已渲染');
assert(el8.innerHTML.includes('+15%') && el8.innerHTML.includes('\u221215%'), '标注 ±15% 考核容差带');
assert((el8.innerHTML.match(/<rect /g) || []).length >= 4, '偏差柱已渲染（含容差带底）');
assert(!/NaN/.test(el8.innerHTML), '偏差回测无 NaN 坐标');

// 2b) 默认配色下达标带透明度 0.1（过低会看不见带的存在）
const el8b = stub();
Charts.deviationChart(el8b, {
  points: [{ t: '10-01', gap_pct: 5 }, { t: '10-02', gap_pct: -20 }],
  tolerance_pct: 15
});
assert(/fill="#35ED7E"[^>]*fill-opacity="0?\.1"/.test(el8b.innerHTML), '达标带透明度 0.1（肉眼可见）');

// 3) 空数据：给出空状态（高度压缩到 160，不再占半屏）
const elE = stub();
Charts.compareChart(elE, { points: [], colors: { actual: '#fff', forecast: '#000' } });
assert(elE.innerHTML.includes('暂无数据'), '空数据有空状态文案');
assert(elE.attrs.viewBox === '0 0 1280 160', '空状态高度压缩为 160（旧版 380 占半屏）');

// 4) 多序列：同量纲共用一块面板、一条带刻度的 Y 轴
const el11 = stub();
Charts.multiLineChart(el11, {
  points: [
    { t: '10-27', vals: { a: 8000, f: 7800 }, today: false, note: '' },
    { t: '10-28', vals: { a: 6500, f: 7000 }, today: false, note: '' },
    { t: '10-29', vals: { a: 3200, f: 7800 }, today: true, note: '' },
  ],
  series: [
    { key: 'a', name: '实测发电', color: '#35ED7E', unit: ' kWh' },
    { key: 'f', name: '模型 / 预测', color: '#6C73F5', unit: ' kWh', dash: true }
  ], labelCount: 12
});
assert(el11.innerHTML.includes('polyline'), '多序列：同量纲画折线');
assert(/\d+k<\/text>/.test(el11.innerHTML), '同量纲共用带刻度 Y 轴');
assert(!/左轴|右轴/.test(el11.innerHTML), '单量纲不画左右轴标注（单位在图例中）');
assert(el11.innerHTML.includes('stroke-dasharray="7 5"'), '虚线序列生效');
assert(!/NaN/.test(el11.innerHTML), '多序列同量纲无 NaN');

// 5) 多序列：混量纲（kWh + ℃）→ 分成两块面板，各自带刻度与单位标注
const el12 = stub();
Charts.multiLineChart(el12, {
  points: [
    { t: '10-27', vals: { a: 8000, temp: 25.4 }, note: '' },
    { t: '10-28', vals: { a: 6500, temp: 22.1 }, note: '' },
    { t: '10-29', vals: { a: 3200, temp: 26.8 }, today: true, note: '' },
  ],
  series: [
    { key: 'a', name: '实测发电', color: '#35ED7E', unit: ' kWh' },
    { key: 'temp', name: '环境温度', color: '#FF8A5C', unit: ' ℃' }
  ], labelCount: 12
});
assert((el12.innerHTML.match(/<polyline/g) || []).length === 2, '混量纲：两条折线同图追加（不另开图表）');
assert(el12.innerHTML.includes('左轴 kWh') && el12.innerHTML.includes('右轴 ℃'), '左右轴各自标注量纲');
assert(el12.innerHTML.includes('D9A93F'), '右轴刻度用琥珀色区分');
assert((el12.innerHTML.match(/text-anchor="end"/g) || []).length >= 4, '左轴带刻度（≥4 个）');
assert((el12.innerHTML.match(/text-anchor="start"/g) || []).length >= 2, '右轴带刻度（≥2 个）');
assert(!/NaN/.test(el12.innerHTML), '混量纲无 NaN');
assert(el12.attrs.viewBox.startsWith('0 0 1280 '), '混量纲 viewBox 宽度不变（高度不随量纲数暴涨）');

// 5b) 三种量纲 → 三块面板；「今天」竖线贯穿全部面板
const el12b = stub();
Charts.multiLineChart(el12b, {
  points: [
    { t: '10-28', vals: { a: 8000, peak: 2100, temp: 25.0 }, note: '' },
    { t: '10-29', vals: { a: 3200, peak: 1800, temp: 26.8 }, today: true, note: '' },
  ],
  series: [
    { key: 'a', name: '实测发电', color: '#35ED7E', unit: ' kWh' },
    { key: 'peak', name: '峰值功率', color: '#F5C542', unit: ' kW' },
    { key: 'temp', name: '环境温度', color: '#FF8A5C', unit: ' ℃' }
  ], labelCount: 12
});
assert((el12b.innerHTML.match(/<polyline/g) || []).length === 3, '三种量纲：三条线全部追加进同一张图');
assert(el12b.innerHTML.includes('右轴 kW 等'), '第三种量纲共用右轴并标注「等」');
// 今天竖线是 3 5 虚线 + 十字色；悬停十字线（4 4 虚线）不算
const todayLines = (el12b.innerHTML.match(/stroke="#7C86FF"[^>]*stroke-dasharray="3 5"/g) || []).length;
assert(todayLines === 1, '今天竖线整条贯穿（跨面板只画一条）');

// 6) 悬停交互层：命中区覆盖每个数据点 + 十字线组 + 全部 line 自闭合
const el9 = stub();
Charts.multiLineChart(el9, {
  points: [
    { t: '10-28', vals: { a: 8371.7, f: 8200 } },
    { t: '10-29', vals: { a: 7402.7, f: 7600 } },
    { t: '10-30', vals: { a: 9434.3, f: 9300 } }
  ],
  series: [
    { key: 'a', name: '实测发电', color: '#35ED7E', unit: ' kWh' },
    { key: 'f', name: '模型 / 预测', color: '#6C73F5', unit: ' kWh', dash: true }
  ], labelCount: 12
});
assert((el9.innerHTML.match(/fill="transparent"/g) || []).length === 3, '3 个点各有一块命中区');
assert(el9.innerHTML.includes('class="cxg"'), '含十字线分组');
assert((el9.innerHTML.match(/class="cxdot"/g) || []).length === 2, '十字线为双序列各备一个点');
const s9 = (el9.innerHTML.match(/<line [^>]*\/>/g) || []).length;
const a9 = (el9.innerHTML.match(/<line /g) || []).length;
assert(s9 === a9 && a9 > 0, '多序列图 line 全部自闭合（' + s9 + '/' + a9 + '）');
const r9 = (el9.innerHTML.match(/text-anchor="end"/g) || []).length;
assert(r9 > 0 && r9 <= 5, '单面板 Y 轴刻度精简到 ≤5 个（实为 ' + r9 + '）');

// 7) 大量级刻度过长时用 k 简写（30 点趋势）
const el10 = stub();
Charts.multiLineChart(el10, {
  points: Array.from({ length: 30 }, (_, i) => ({ t: 'd' + i, vals: { a: 8000 + i * 60, f: 7900 + i * 60 }, note: '' })),
  series: [
    { key: 'a', name: '实测发电', color: '#35ED7E', unit: ' kWh' },
    { key: 'f', name: '模型 / 预测', color: '#6C73F5', unit: ' kWh', dash: true }
  ], labelCount: 12
});
assert(/\d+k<\/text>/.test(el10.innerHTML), '大数量级刻度用 k 简写');
assert(!/NaN/.test(el10.innerHTML), '30 点趋势图无 NaN');

// 8) 多序列：空序列 / 全空数据 → 空提示
const el13 = stub();
Charts.multiLineChart(el13, { points: [{ t: '10-29', vals: {}, note: '' }], series: [], emptyMsg: '暂无数据' });
assert(el13.innerHTML.includes('暂无数据'), '空数据给提示文案');

const bad = results.filter(r => !r[0]).length;
// ---------- compareChart：曲线显隐 + 辐照副面板（总览页复选框） ----------
{
  const el6 = stub(); const pts6 = [];
  for (let i = 0; i < 8; i++) pts6.push({ t: String(i).padStart(2, '0') + ':00',
    actual_kw: 100 + i * 10, forecast_kw: 110 + i * 10, ghi_w: 200 + i * 50 });
  Charts.compareChart(el6, { points: pts6, colors: { actual: '#35ED7E', forecast: '#6C73F5' },
    visible: { actual: true, forecast: true, ghi: true } });
  assert(el6.innerHTML.includes('右轴 W/m²'), '辐照度右轴标注（同图追加）');
  assert(el6.innerHTML.includes('F5C542'), '辐照度黄色细线在');
  assert((el6.innerHTML.match(/35ED7E/g) || []).length > 0, '实测线在');
  assert((el6.innerHTML.match(/6C73F5/g) || []).length > 0, '基线在');
  assert(el6.attrs.viewBox === '0 0 1280 380', '同图追加不改变 viewBox');
  const el6b = stub();
  Charts.compareChart(el6b, { points: pts6, colors: { actual: '#35ED7E', forecast: '#6C73F5' },
    visible: { actual: true, forecast: false, ghi: true } });
  assert(!(el6b.innerHTML.includes('6C73F5')), '隐藏后基线消失');
  assert(el6b.innerHTML.includes('右轴 W/m²'), '辐照度线仍在');
  const el6c = stub();
  Charts.compareChart(el6c, { points: pts6, colors: { actual: '#35ED7E', forecast: '#6C73F5' },
    visible: { actual: false, forecast: false, ghi: false } });
  assert(el6c.innerHTML.includes('至少勾选'), '全隐藏显示空态提示');
  const el6d = stub();
  Charts.compareChart(el6d, { points: pts6, colors: { actual: '#35ED7E', forecast: '#6C73F5' } });
  assert(el6d.attrs.viewBox === '0 0 1280 380', '旧调用保持 380 高');
  assert((el6d.innerHTML.match(/6C73F5/g) || []).length > 0, '旧调用基线在');
}

console.log('\n=== 图表测试：' + (results.length - bad) + ' 通过 / ' + bad + ' 失败 ===');
process.exit(bad ? 1 : 0);
