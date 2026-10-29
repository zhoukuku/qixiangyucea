# -*- coding: utf-8 -*-
"""Open-Meteo 开放气象数据源（api.open-meteo.com）。

特点：免费、无需注册 key；逐小时 GHI 由气象模型（ICON 等）给出，
深圳点位经与实测电量对照（2026-10-27：模型 8213 kWh vs 实发 8063 kWh，+1.9%）质量可用。

提供两个适配器：
  OpenMeteoIrradiance  近 N 天逐小时实况 → 日辐照 kWh/m²（今天不算实测，避免半日值混入对账）
                       可选 export 兜底（气象仪导出文件）：Open-Meteo 没覆盖的更早日期用导出补齐
  OpenMeteoForecast    未来 N 天预报 → 日辐照 kWh/m²（供未来预测替代气候学推算）

接口（无 key）：
  https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..
      &hourly=shortwave_radiation&past_days=7&forecast_days=1&timezone=Asia/Shanghai
单位：shortwave_radiation 为 W/m²（= Wh/m²/h），按本地日求和 ÷1000 → kWh/m²。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from .base import Source, SourceResult
from .local_export import IrradianceExport

API_URL = "https://api.open-meteo.com/v1/forecast"


def daily_from_hourly(times, values):
    """逐小时辐照序列 → {本地日: kWh/m²}。shortwave_radiation 单位 W/m²，1 小时积分 = Wh/m²。"""
    day = {}
    for t, v in zip(times or [], values or []):
        if v is None:
            continue
        d = str(t)[:10]
        day[d] = day.get(d, 0.0) + float(v)
    return {d: round(v / 1000.0, 3) for d, v in day.items()}


def drop_today(obs, today=None):
    """剔除「今天」：只跑到一半的日累计辐照会低估全天值，绝不能进历史对账。"""
    today = today or datetime.now().strftime("%Y-%m-%d")
    return {d: v for d, v in (obs or {}).items() if d < today}


class _OpenMeteoBase(Source):
    """公共请求逻辑：拼参数 → 拉 hourly 序列 → 聚合成日辐照。"""

    def _cfg(self):
        api = self.cfg.get("api", {})
        return {
            "url": api.get("url") or API_URL,
            "lat": api.get("latitude"),
            "lon": api.get("longitude"),
            "past_days": int(api.get("past_days") or 0),
            "forecast_days": int(api.get("forecast_days") or 1),
            "timezone": api.get("timezone") or "Asia/Shanghai",
        }

    def _fetch_raw(self, forecast_days=None, retries=1):
        """拉取 hourly 序列，返回 ({日: kWh/m²}, {日: [24 个 W/m²]}, {日: 白天平均云量%})。

        云量（cloud_cover）与辐照一起拉：云量预报比辐照无偏得多，
        作为预测模型「按天气分档标定」的分类输入（model.regime_key）。
        """
        c = self._cfg()
        if c["lat"] is None or c["lon"] is None:
            raise ValueError("未配置经纬度（config.json → sources.%s.api.latitude/longitude）" % self.name)
        qs = urllib.parse.urlencode({
            "latitude": c["lat"], "longitude": c["lon"],
            "hourly": "shortwave_radiation,cloud_cover",
            "past_days": c["past_days"],
            "forecast_days": forecast_days if forecast_days is not None else c["forecast_days"],
            "timezone": c["timezone"],
        })
        req = urllib.request.Request(c["url"] + "?" + qs, method="GET",
                                     headers={"User-Agent": "pv-board/1.0"})
        # 网络抖动偶发超时：HTTPError（4xx/5xx）不重试，超时/断连重试 1 次
        last = None
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_ms / 1000) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                h = payload.get("hourly") or {}
                times = h.get("time") or []
                vals = h.get("shortwave_radiation") or []
                clouds_raw = h.get("cloud_cover") or []
                hourly, day_v, day_c = {}, {}, {}
                for t, v, cc in zip(times, vals, clouds_raw):
                    d = str(t)[:10]
                    hourly.setdefault(d, []).append(v)
                    day_v[d] = day_v.get(d, 0.0) + float(v or 0)
                    if v is not None and float(v) > 20 and cc is not None:
                        day_c.setdefault(d, []).append(float(cc))
                daily = {d: round(v / 1000.0, 3) for d, v in day_v.items()}
                # 白天平均云量：只统计辐照>20W/m² 的时段（夜间云量无意义）
                clouds = {d: round(sum(vs) / len(vs), 1) for d, vs in day_c.items() if vs}
                return daily, hourly, clouds
            except urllib.error.HTTPError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last = e
                if attempt < retries:
                    time.sleep(1.0)
        raise last

    def _fetch_daily(self, forecast_days=None, retries=1):
        return self._fetch_raw(forecast_days, retries)[0]


class OpenMeteoIrradiance(_OpenMeteoBase):
    """历史日辐照：Open-Meteo 近 N 天实况为主，气象仪导出文件兜底补更早日期。"""

    name = "irradiance"
    label = "Open-Meteo 气象模型（近 7 天实况 + 气象仪导出兜底）"

    def fetch(self):
        t0 = time.time()
        lat_ms = int((time.time() - t0) * 1000)
        obs, clouds, om_err = {}, {}, None
        try:
            raw_d, _h, raw_c = self._fetch_raw(forecast_days=1)
            obs, clouds = drop_today(raw_d), drop_today(raw_c)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            om_err = "HTTP %s" % getattr(e, "code", "") or str(e.reason)
        except Exception as e:
            om_err = str(e)

        # 气象仪导出兜底：只补 Open-Meteo 没有的日期（更早的历史），已覆盖日期不回退
        exp_res = None
        if self.cfg.get("export", {}).get("glob"):
            exp_res = IrradianceExport(self.cfg, self.timeout_ms).fetch()
        merged = dict(exp_res.data) if (exp_res and exp_res.ok and exp_res.data) else {}
        merged.update(obs)

        if not merged:
            if om_err and exp_res and exp_res.status == "unavailable":
                return SourceResult("error", error="Open-Meteo 请求失败（%s）且无导出文件兜底" % om_err,
                                    latency_ms=lat_ms)
            return SourceResult("empty", error=om_err or (exp_res.error if exp_res else "无数据"),
                                latency_ms=lat_ms)

        meta = {"provider": "open-meteo", "mode": "api",
                "openmeteo_days": len(obs), "export_days": len(merged) - len(obs),
                # 双轨原始值：供 store 计算本地偏差校正 / 天气分档标定
                "om": obs, "om_clouds": clouds,
                "ground": (dict(exp_res.data) if (exp_res and exp_res.ok and exp_res.data) else {})}
        as_of = datetime.now()
        if obs:
            if om_err:                      # 拿到了导出兜底但 Open-Meteo 失败：数据可用但标注异常
                meta["warning"] = "Open-Meteo 请求失败（%s），仅气象仪导出兜底" % om_err
            return SourceResult("ok", data=merged, as_of=as_of, latency_ms=lat_ms, meta=meta)
        # Open-Meteo 失败，纯靠导出：标 stale 语义（meta.warning），数据本身照常可用
        meta["warning"] = "Open-Meteo 请求失败（%s），仅气象仪导出兜底" % om_err
        meta["mode"] = "export"
        return SourceResult("ok", data=merged, as_of=(exp_res.as_of if exp_res else None),
                            latency_ms=lat_ms, meta=meta)


class OpenMeteoForecast(_OpenMeteoBase):
    """未来辐照预报：只保留今天起未来的日期（历史对账不用预报值）。"""

    name = "forecast"
    label = "Open-Meteo 未来辐照预报"

    def fetch(self):
        t0 = time.time()
        try:
            daily, hourly, clouds = self._fetch_raw()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            return SourceResult("error", error="HTTP %s @ open-meteo" % getattr(e, "code", ""),
                                latency_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            return SourceResult("error", error=str(e), latency_ms=int((time.time() - t0) * 1000))
        today = datetime.now().strftime("%Y-%m-%d")
        fut = {d: v for d, v in daily.items() if d >= today}
        if not fut:
            return SourceResult("empty", error="预报序列为空", latency_ms=int((time.time() - t0) * 1000))
        # 保留预报自身的逐时形状（W/m²）：日内基线用它归一化摊电能量，
        # 替代钟形近似，保住预报里的云量时间信息；云量日线供分档标定选 K
        return SourceResult("ok", data=fut, as_of=datetime.now(),
                            latency_ms=int((time.time() - t0) * 1000),
                            meta={"provider": "open-meteo", "mode": "api",
                                  "days": len(fut), "last_day": max(fut),
                                  "clouds": {d: clouds.get(d) for d in sorted(fut)},
                                  "hourly": {d: hourly.get(d) for d in sorted(fut)}})
