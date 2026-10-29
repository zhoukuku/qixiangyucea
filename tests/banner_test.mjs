// 顶部提示文案单元测试：验证文案随【实际数据模式】自动显示/隐藏
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const here = dirname(fileURLToPath(import.meta.url));
eval(readFileSync(join(here, '../web/assets/banner.js'), 'utf-8') + '\nglobalThis.Banner = Banner;');

const results = [];
const assert = (cond, msg) => { results.push([cond, msg]); console.log((cond ? '[PASS] ' : '[FAIL] ') + msg); };

const st = gen => ({ sources: { generation: gen, forecast: { status: 'ok', mode: 'none' } } });

// 1) export 模式：显示导出提示
const e1 = Banner.build(st({ status: 'ok', mode: 'export', as_of: '2026-10-27T11:30:00', age_seconds: 87200 }), 'overview');
assert(e1.msgs.length === 1, 'export 模式给出 1 条提示');
assert(e1.level === 'info', 'export 模式为 info 级（非红色告警）');
assert(e1.msgs[0].includes('导出文件') && e1.msgs[0].includes('10/27 11:30'), 'export 文案含可读时间戳: ' + e1.msgs[0]);
assert(!e1.msgs[0].includes('2026-10-27T'), '时间戳已格式化，不再是原始 ISO 串');

// 2) api 模式 + 数据新鲜：不显示任何提示
const e2 = Banner.build(st({ status: 'ok', mode: 'api', freshness: 'live', as_of: '2026-10-28T11:30:00', age_seconds: 120 }), 'overview');
assert(e2.msgs.length === 0, 'api 模式且数据新鲜 → 无任何提示（自动隐藏）');

// 3) api 模式但数据滞后：提示接口未更新
const e3 = Banner.build(st({ status: 'ok', mode: 'api', freshness: 'stale', as_of: '2026-10-27T11:30:00', age_seconds: 87200 }), 'overview');
assert(e3.msgs.length === 1, 'api 模式但数据滞后 → 给出 1 条提示');
assert(e3.msgs[0].includes('北向 API 已接入'), '滞后文案说明已接入但未更新: ' + e3.msgs[0]);

// 4) 数据源异常 / 未配置：红色告警
const e4 = Banner.build(st({ status: 'error', mode: 'api', error: 'timeout' }), 'overview');
assert(e4.level === 'err' && e4.msgs.length === 1, '数据源异常 → err 级告警');
const e5 = Banner.build(st({ status: 'unavailable', mode: 'api' }), 'overview');
assert(e5.level === 'err' && e5.msgs[0].includes('未配置'), '数据源未配置 → err 级告警');

// 5) 预测口径与发电源解耦：仅无气象预报源时才追加
const noFc = { sources: { generation: { status: 'ok', mode: 'api', freshness: 'live' }, forecast: { status: 'unavailable' } } };
const a = Banner.build(noFc, 'overview');
const b = Banner.build(noFc, 'forecast');
assert(a.msgs.length === 0, 'api 实时时，总览页不显示任何提示');
assert(b.msgs.length === 1 && b.msgs[0].includes('气象预报'), '仅预测页提示气象预报源缺失');
assert(!b.msgs[0].includes('导出文件'), '预测页不再夹带与发电源模式无关的导出文案');

// 6) 服务不可达
const e6 = Banner.build(null, 'overview');
assert(e6.level === 'err' && e6.msgs[0].includes('server.py'), '服务不可达时给出红色提示');

// 7) XSS：数据源异常文案中的标签被转义
const e7 = Banner.build(st({ status: 'error', mode: 'api', error: '<script>x</script>' }), 'overview');
assert(e7.msgs[0].includes('&lt;script&gt;'), '错误信息已转义，不会注入 HTML');

const bad = results.filter(r => !r[0]).length;
console.log('\n=== 横幅文案测试：' + (results.length - bad) + ' 通过 / ' + bad + ' 失败 ===');
process.exit(bad ? 1 : 0);
