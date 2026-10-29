/* 统一数据访问层：所有页面只读接口，不内置任何写死数据
   并发去重：同一瞬间对同一接口的重复 GET 共享同一个 promise
   （发电分析页此前会对 /api/daily?limit=0 发两次，数据量大时白白翻倍） */
const API = (() => {
  const TIMEOUT_MS = 8000;
  const inflight = new Map();

  async function get(path, params = {}) {
    const qs = Object.keys(params).length
      ? '?' + new URLSearchParams(params).toString() : '';
    const key = path + qs;
    if (inflight.has(key)) return inflight.get(key);
    const p = (async () => {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
      try {
        const r = await fetch('/api/' + path + qs, { signal: ctl.signal, cache: 'no-store' });
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return await r.json();
      } catch (e) {
        if (e.name === 'AbortError') throw new Error('请求超时（>' + TIMEOUT_MS / 1000 + 's）');
        throw new Error(e.message || '网络错误');
      } finally {
        clearTimeout(timer);
      }
    })();
    inflight.set(key, p);
    try { return await p; } finally { inflight.delete(key); }
  }

  async function post(path, body = {}, timeoutMs = TIMEOUT_MS) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const r = await fetch('/api/' + path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: ctl.signal
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    } catch (e) {
      if (e.name === 'AbortError') throw new Error('请求超时（>' + Math.round(timeoutMs / 1000) + 's）');
      throw new Error(e.message || '网络错误');
    } finally {
      clearTimeout(timer);
    }
  }

  // 文件 → base64（去掉 data:xxx;base64, 前缀）：一个月的气象仪 xls 约 1.2MB，
  // base64 后 1.6MB，走 JSON 比手写 multipart 稳；解析 7000 行要几秒，超时放宽到 120s
  function readAsBase64(file) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onerror = () => reject(new Error('读取文件失败：' + file.name));
      fr.onload = () => {
        const s = String(fr.result || '');
        const i = s.indexOf(',');
        resolve(i >= 0 ? s.slice(i + 1) : s);
      };
      fr.readAsDataURL(file);
    });
  }

  async function importWeather(files, commit) {
    const payload = [];
    for (const f of files) payload.push({ name: f.name, data_b64: await readAsBase64(f) });
    return post('import/weather', { files: payload, commit: !!commit }, 180000);
  }

  return {
    status: () => get('status'),
    overview: () => get('overview'),
    series: (range) => get('series', { range }),
    intraday: (date, slotMin) => get('intraday',
      Object.assign(date ? { date } : {}, slotMin ? { slot_min: slotMin } : {})),
    calendar: () => get('forecast/calendar'),
    forecastDay: (date, slotMin) => get('forecast/day',
      Object.assign({ date }, slotMin ? { slot_min: slotMin } : {})),
    forecastExplain: () => get('forecast/explain'),
    daily: (limit) => get('daily', { limit }),
    trend: (scope, limit) => get('trend', { scope, limit }),
    inverters: () => get('inverters'),
    radiation: (month) => get('radiation', month ? { month } : {}),
    quality: () => get('quality'),
    weatherStation: () => get('weather-station'),
    site: () => get('site'),
    refresh: (sources) => post('refresh', { sources }),
    importWeather: importWeather,
  };
})();
