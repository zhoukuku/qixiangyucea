# -*- coding: utf-8 -*-
"""环境气象参数源：环境温度 / 组件温度(估算) / 相对湿度 / 风速 / 风向 / 总辐射。

两个底层提供方合成一个源：
  Open-Meteo  免费无 key。逐小时 temperature_2m / relative_humidity_2m /
              wind_speed_10m / wind_direction_10m / shortwave_radiation，
              past_days(近7天实况) + forecast_days(未来预报) → 日粒度聚合
  心知天气    私钥直连 v3（now + hourly）。从接入当天起逐日积累进
              data/api_cache/weather_daily.json —— Open-Meteo 只回看 7 天，
              更早的日期靠这份积累补上，让参数趋势一天天变长

合并优先级：Open-Meteo（近7天实况+预报）> 心知积累（第 8 天起的历史）。
同一天两层都有时以 Open-Meteo 为准（口径统一，避免两条模型曲线互相打架）。

组件温度没有现成数据源（图霸监控才有实测传感器），按 NOCT 经验式估算：
  T组件 ≈ T环境 + (NOCT-20)/800 × G(W/m²)，NOCT 取 45 → 系数 0.03125
"""
import glob
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from statistics import mean

from .base import Source, SourceResult

_NOCT_K = 0.03125           # (45-20)/800
_CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "data", "api_cache", "weather_daily.json")

# 16 方位风向（deg → 名称）
_SECTORS = ["北", "北东北", "东北", "东东北", "东", "东东南", "东南", "南东南",
            "南", "南西南", "西南", "西西南", "西", "西西北", "西北", "北西北"]


def sector(deg):
    """风向角度 → 16 方位名称。"""
    if deg is None:
        return None
    return _SECTORS[int(((float(deg) % 360) + 11.25) // 22.5) % 16]


def dominant_wind_dir(degs):
    """一天的风向取「出现最多的 16 方位」的中心角（平均角在环绕方向上无意义）。"""
    vals = [int(round(float(d))) % 360 for d in degs if d is not None]
    if not vals:
        return None
    counts = {}
    for d in vals:
        s = int((d + 11.25) // 22.5) % 16
        counts[s] = counts.get(s, 0) + 1
    best = max(counts, key=lambda k: counts[k])
    return best * 22.5


def daily_weather_from_hourly(times, temp, rh, wind, wdir, rad):
    """逐小时参数序列 → {date: 聚合参数}。

    temp: 环境温度 ℃；rh: 相对湿度 %；wind: m/s；wdir: deg；rad: W/m²。
    ghi_avg 取「辐照 > 0 的小时平均」（白天平均辐照度），ghi_sum 为日累计 kWh/m²。
    """
    by_day = {}
    for i, t in enumerate(times or []):
        d = str(t)[:10]
        rec = by_day.setdefault(d, {"temp": [], "rh": [], "wind": [], "wdir": [], "rad": []})
        if temp[i] is not None:
            rec["temp"].append(float(temp[i]))
        if rh[i] is not None:
            rec["rh"].append(float(rh[i]))
        if wind[i] is not None:
            rec["wind"].append(float(wind[i]))
        if wdir[i] is not None:
            rec["wdir"].append(float(wdir[i]))
        if rad[i] is not None:
            rec["rad"].append(float(rad[i]))
    out = {}
    for d, rec in by_day.items():
        day_hrs = [h for h in rec["rad"] if h > 0]
        ghi_sum = sum(rec["rad"]) / 1000.0
        ghi_avg = mean(day_hrs) if day_hrs else 0.0
        t = mean(rec["temp"]) if rec["temp"] else None
        out[d] = {
            "temp": (round(t, 1) if t is not None else None),
            "temp_min": (round(min(rec["temp"]), 1) if rec["temp"] else None),
            "temp_max": (round(max(rec["temp"]), 1) if rec["temp"] else None),
            "rh": (round(mean(rec["rh"])) if rec["rh"] else None),
            "wind": (round(mean(rec["wind"]), 1) if rec["wind"] else None),
            "wind_max": (round(max(rec["wind"]), 1) if rec["wind"] else None),
            "wind_dir": dominant_wind_dir(rec["wdir"]),
            "ghi_avg": round(ghi_avg),
            "ghi_sum": round(ghi_sum, 2),
            "src": None,           # 合并阶段填 open-meteo / seniverse / forecast
        }
        if t is not None:
            out[d]["tmod"] = round(t + _NOCT_K * ghi_avg, 1)      # 组件温度估算
    return out


def load_accumulated(path=_CACHE):
    """读取心知积累的历史参数 {date: rec}（文件不存在/损坏返回空）。"""
    try:
        with open(path, encoding="utf-8") as f:
            return dict(json.load(f))
    except Exception:
        return {}


def save_accumulated(wmap, path=_CACHE):
    """积累历史落盘（最多保留 400 天）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        keep = dict(sorted(wmap.items())[-400:])
        json.dump(keep, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def seniverse_today(cfg, timeout_ms=8000):
    """心知天气 v3：now + 逐小时 → 今天的环境参数（参考值）。

    返回 {date: rec}（仅今天）；请求失败返回 {}。
    """
    sk = ((cfg.get("seniverse") or {}).get("private_key") or "")
    loc = ((cfg.get("seniverse") or {}).get("location") or "22.55:114.05")
    if not sk:
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    rec = {"temp": [], "rh": [], "wind": [], "wdir": []}
    for url, pick in (
        ("https://api.seniverse.com/v3/weather/now.json?key=%s&location=%s&language=zh-Hans" % (sk, loc),
         lambda n: (n.get("temperature"), n.get("humidity"), n.get("wind_speed"),
                    n.get("wind_direction_degree"))),
        ("https://api.seniverse.com/v3/weather/hourly.json?key=%s&location=%s&language=zh-Hans" % (sk, loc),
         lambda h: (h.get("temperature"), h.get("humidity"), h.get("wind_speed"), None)),
    ):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "pv-board/1.0"})
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000) as r:
                payload = json.loads(r.read().decode("utf-8"))
            results = payload.get("results") or []
            items = results[0].get("now") if (results and "now" in results[0]) else \
                (results[0].get("hourly") if results else [])
            for it in items or []:
                if "time" in (it or {}) and str(it["time"])[:10] != today:
                    continue                      # 逐小时预报里跨到明天的部分不要
                t, h, w, wd = pick(it)
                if t not in (None, ""):
                    rec["temp"].append(float(t))
                if h not in (None, ""):
                    rec["rh"].append(float(h))
                if w not in (None, ""):
                    rec["wind"].append(float(w))
                if wd not in (None, ""):
                    rec["wdir"].append(float(wd))
        except Exception:
            continue
    if not rec["temp"] and not rec["wind"]:
        return {}
    t = mean(rec["temp"]) if rec["temp"] else None
    day = {
        "temp": (round(t, 1) if t is not None else None),
        "temp_min": (round(min(rec["temp"]), 1) if rec["temp"] else None),
        "temp_max": (round(max(rec["temp"]), 1) if rec["temp"] else None),
        "rh": (round(mean(rec["rh"])) if rec["rh"] else None),
        "wind": (round(mean(rec["wind"]), 1) if rec["wind"] else None),
        "wind_max": (round(max(rec["wind"]), 1) if rec["wind"] else None),
        "wind_dir": dominant_wind_dir(rec["wdir"]),
        "ghi_avg": None, "ghi_sum": None,                 # 心知免费档无总辐射
        "src": "seniverse",
    }
    if t is not None:
        day["tmod"] = None                                # 无辐照输入，不估组件温度
    return {today: day}


class WeatherParams(Source):
    """环境参数合成源：Open-Meteo 参数日线 + 心知天气历史积累。"""

    name = "weather"
    label = "环境气象参数（Open-Meteo 实况/预报 + 心知天气积累）"

    def fetch(self):
        t0 = time.time()
        api = self.cfg.get("api", {})
        om = {}
        om_err = None
        if (api.get("driver") or "").lower() == "open-meteo" and api.get("latitude") is not None:
            qs = urllib.parse.urlencode({
                "latitude": api["latitude"], "longitude": api["longitude"],
                "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,shortwave_radiation",
                "past_days": int(api.get("past_days") or 7),
                "forecast_days": int(api.get("forecast_days") or 7),
                "timezone": api.get("timezone") or "Asia/Shanghai",
            })
            req = urllib.request.Request(
                (api.get("url") or "https://api.open-meteo.com/v1/forecast") + "?" + qs,
                headers={"User-Agent": "pv-board/1.0"})
            last = None
            for attempt in range(2):
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout_ms / 1000) as r:
                        payload = json.loads(r.read().decode("utf-8"))
                    h = payload.get("hourly") or {}
                    om = daily_weather_from_hourly(
                        h.get("time"), h.get("temperature_2m"), h.get("relative_humidity_2m"),
                        h.get("wind_speed_10m"), h.get("wind_direction_10m"), h.get("shortwave_radiation"))
                    today = datetime.now().strftime("%Y-%m-%d")
                    for d, rec in om.items():
                        rec["src"] = "forecast" if d >= today else "open-meteo"
                    break
                except Exception as e:
                    last = e
                    if attempt < 1:
                        time.sleep(1.0)
            else:
                om_err = str(last)

        sv = seniverse_today(self.cfg, self.timeout_ms)
        acc = load_accumulated()
        acc.update(sv)                       # 心知今天的积累就地更新
        save_accumulated(acc)

        merged = dict(acc)
        merged.update(om)                    # Open-Meteo 优先（近7天+预报）
        if not merged:
            return SourceResult("error", error="Open-Meteo 请求失败（%s）且心知天气未返回数据" % om_err,
                                latency_ms=int((time.time() - t0) * 1000))
        meta = {"provider": "open-meteo+seniverse", "mode": "api",
                "openmeteo_days": len(om), "accumulated_days": len(acc),
                "seniverse_today": bool(sv)}
        if om_err:
            meta["warning"] = "Open-Meteo 参数请求失败（%s），仅心知积累兜底" % om_err
        return SourceResult("ok", data=merged, as_of=datetime.now(),
                            latency_ms=int((time.time() - t0) * 1000), meta=meta)
