# -*- coding: utf-8 -*-
"""通用 HTTP JSON 数据源：气象辐照实测 / 未来辐照预报。

配置格式（config.json → sources.irradiance.api / sources.forecast.api）：
  url, method(GET|POST), headers, params, token, field_map{date,value}, horizon_days
返回体支持两种：
  1) [{"date":"2026-10-27","ghi_kwh_m2":4.7}, ...]
  2) {"data":[...]} 或 {"list":[...]}（自动找第一个数组）
未配置 url 时返回 unavailable。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .base import Source, SourceResult


def _pick_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "list", "result", "items", "rows"):
            if isinstance(payload.get(k), list):
                return payload[k]
        for v in payload.values():
            if isinstance(v, list):
                return v
    return []


class HttpJsonSeries(Source):
    """按日期索引的时序数据源（实测辐照 or 预报辐照）。"""

    def fetch(self):
        api = self.cfg.get("api", {})
        url = api.get("url")
        if not url:
            return SourceResult("unavailable", error="未配置接口地址（config.json → sources.%s.api.url）" % self.name)
        t0 = time.time()
        try:
            headers = dict(api.get("headers") or {})
            if api.get("token"):
                headers["Authorization"] = "Bearer " + api["token"]
            params = dict(api.get("params") or {})
            method = (api.get("method") or "GET").upper()
            if method == "GET":
                full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params) if params else url
                req = urllib.request.Request(full, headers=headers, method="GET")
            else:
                req = urllib.request.Request(url, data=json.dumps(params).encode("utf-8"),
                                             headers={"Content-Type": "application/json", **headers}, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout_ms / 1000) as r:
                payload = json.loads(r.read().decode("utf-8"))
            fmap = api.get("field_map") or {"date": "date", "value": "ghi_kwh_m2"}
            out = {}
            for it in _pick_list(payload):
                if not isinstance(it, dict):
                    continue
                d = it.get(fmap.get("date", "date"))
                v = it.get(fmap.get("value", "ghi_kwh_m2"))
                if d is None or v is None:
                    continue
                out[str(d)[:10]] = round(float(v), 3)
            if not out:
                return SourceResult("empty", error="接口返回为空或字段映射不匹配（field_map=%s）" % fmap,
                                    latency_ms=int((time.time() - t0) * 1000))
            horizon = int(api.get("horizon_days") or 0)
            if horizon:
                end = datetime.now().date() + timedelta(days=horizon)
                out = {d: v for d, v in out.items() if d <= end.isoformat()}
            return SourceResult("ok", data=out, as_of=datetime.now(),
                                latency_ms=int((time.time() - t0) * 1000), meta={"url": url, "mode": "api"})
        except urllib.error.HTTPError as e:
            return SourceResult("error", error="HTTP %s @ %s" % (e.code, url), latency_ms=int((time.time() - t0) * 1000))
        except urllib.error.URLError as e:
            return SourceResult("error", error="网络不可达（%s）" % e.reason, latency_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return SourceResult("error", error=str(e), latency_ms=int((time.time() - t0) * 1000))
