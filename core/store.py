# -*- coding: utf-8 -*-
"""数据仓：按数据源统一调度拉取、缓存最近一次成功结果、计算新鲜度与状态。

- 每个源有独立状态（ok / empty / error / unavailable）、as_of、延迟、错误文案
- 定时轮询（poll.interval_seconds），也支持 force=True 立即刷新
- 缓存：失败时保留上一次成功数据并标记 stale，页面据此显示「数据可能过期」
- 电量单位统一为 kWh（功率 kW）
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta

from . import actuals, allocation, model, weather_import
from sources.base import SourceResult
from sources.fusionsolar_api import FusionSolarAPI, FusionSolarIrradiance, FusionSolarInverters
from sources.http_json import HttpJsonSeries
from sources.local_export import GenerationExport, IrradianceExport, InverterExport
from sources.open_meteo import OpenMeteoForecast, OpenMeteoIrradiance
from sources.weather import WeatherParams

FORECAST_HORIZON_DAYS = 30      # 日历可选范围：今天 ~ 今天+30 天
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # site/


def _api_source(kind, cfg, timeout_ms):
    """type=api 时按 driver 选择适配器：fusionsolar（华为北向）/ open-meteo / http_json（通用）。"""
    drv = ((cfg.get("api") or {}).get("driver") or "http_json").lower()
    if drv == "fusionsolar":
        return {"generation": FusionSolarAPI,
                "irradiance": FusionSolarIrradiance,
                "inverters": FusionSolarInverters}[kind](cfg, timeout_ms)
    if drv == "open-meteo":
        return {"irradiance": OpenMeteoIrradiance,
                "forecast": OpenMeteoForecast,
                "weather": WeatherParams}[kind](cfg, timeout_ms)
    return HttpJsonSeries(cfg, timeout_ms)


def _build_sources(cfg, timeout_ms):
    s = cfg["sources"]
    out = {}
    gen = s["generation"]
    out["generation"] = (_api_source("generation", gen, timeout_ms) if gen.get("type") == "api"
                         else GenerationExport(gen, timeout_ms))
    irr = s["irradiance"]
    out["irradiance"] = (_api_source("irradiance", irr, timeout_ms) if irr.get("type") == "api"
                         else IrradianceExport(irr, timeout_ms))
    inv = s["inverters"]
    out["inverters"] = (_api_source("inverters", inv, timeout_ms) if inv.get("type") == "api"
                        else InverterExport(inv, timeout_ms))
    fc = s["forecast"]
    out["forecast"] = (_api_source("forecast", fc, timeout_ms) if fc.get("type") == "api"
                       else _NoneSource(fc, timeout_ms))
    wth = s.get("weather") or {"type": "none"}
    out["weather"] = (_api_source("weather", wth, timeout_ms) if wth.get("type") == "api"
                      else _NoneSource(wth, timeout_ms))
    return out


class _NoneSource:
    """预报源未启用（未来预测改用历史气候学推算，见 model.future_irradiance）。"""

    name = "forecast"
    label = "未来辐照预报（未配置 · 用历史气候学推算）"

    def __init__(self, cfg, timeout_ms=8000):
        self.cfg = cfg

    def fetch(self):
        return SourceResult("unavailable", error="未配置预报数据源（config.json → sources.forecast）")


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        self.timeout_ms = cfg["http"]["timeout_ms"]
        self.sources = _build_sources(cfg, self.timeout_ms)
        self.cache = {}             # name -> {"result": SourceResult, "fetched_at": datetime}
        self.lock = threading.RLock()
        self.poll_thread = None
        self.stop_flag = False
        self.poll_count = 0
        self._merged = None          # 实测合并结果缓存（见 actual_merged）
        self._merged_at = 0
        self._archive = None         # 旧版归档缓存（文件没变就不重读）
        self._archive_sig = None
        self._prof_store = None      # 日内分布权重（见 hour_profile）
        self._prof_samples = None    # 学习样本缓存
        self._prof_sig = None
        self.refresh_one(self.sources.keys(), wait=False)

    # ---------- 拉取 ----------
    def refresh_one(self, names, wait=False):
        jobs = []
        for n in names:
            src = self.sources[n]
            if wait:
                self._fetch(n, src)
            else:
                t = threading.Thread(target=self._fetch, args=(n, src), daemon=True)
                t.start()
                jobs.append(t)
        for t in jobs:
            t.join(timeout=self.timeout_ms / 1000 + 5)

    def _fetch(self, name, src):
        try:
            res = src.fetch()
        except Exception as e:          # 兜底：任何异常都不能让服务挂掉
            res = SourceResult("error", error="未捕获异常：%s" % e)
        with self.lock:
            prev = self.cache.get(name)
            # 失败时保留上一次成功数据（标记 stale）
            if not res.ok and prev and prev["result"].ok:
                res.data = prev["result"].data
                res.as_of = prev["result"].as_of
                res.meta = dict(prev["result"].meta or {})
                res.meta["stale"] = True
            self.cache[name] = {"result": res, "fetched_at": datetime.now(), "label": src.label}

    def get(self, name):
        with self.lock:
            return self.cache.get(name)

    def start_poller(self):
        if self.poll_thread and self.poll_thread.is_alive():
            return
        self.stop_flag = False
        self.poll_thread = threading.Thread(target=self._loop, daemon=True)
        self.poll_thread.start()

    def stop(self):
        self.stop_flag = True

    def _loop(self):
        interval = int(self.cfg["poll"]["interval_seconds"])
        while not self.stop_flag:
            time.sleep(interval)
            if self.stop_flag:
                break
            self.poll_count += 1
            self.refresh_one(self.sources.keys(), wait=True)

    # ---------- 状态 ----------
    def status(self):
        now = datetime.now()
        stale_after = int(self.cfg["poll"]["stale_after_seconds"])
        items = {}
        for name, src in self.sources.items():
            c = self.get(name) or {}
            res = c.get("result")
            if res is None:
                items[name] = {"label": src.label, "status": "pending", "mode": self.cfg["sources"][name].get("type")}
                continue
            age = None
            if res.as_of:
                age = int((now - res.as_of).total_seconds())
            freshness = "unknown"
            if res.status == "ok":
                freshness = "live" if (age is not None and age <= stale_after) else "stale"
            items[name] = {
                "label": src.label,
                "mode": self.cfg["sources"][name].get("type"),
                "status": res.status,
                "freshness": freshness,
                "age_seconds": age,
                "as_of": res.as_of.isoformat(timespec="seconds") if res.as_of else None,
                "fetched_at": c.get("fetched_at").isoformat(timespec="seconds") if c.get("fetched_at") else None,
                "latency_ms": res.latency_ms,
                "error": res.error,
                "meta": res.meta,
            }
        return {
            "server_time": now.isoformat(timespec="seconds"),
            "poll_interval_seconds": int(self.cfg["poll"]["interval_seconds"]),
            "stale_after_seconds": stale_after,
            "poll_count": self.poll_count,
            "sources": items,
        }

    # ---------- 基础取数 ----------
    def _raw(self, name):
        c = self.get(name)
        return (c or {}).get("result")

    def _forecast_irr(self):
        """预报源给出的未来日辐照 {date: kWh/m²}（原始值，不做标量校正）。

        天气偏差不再用单一标量修：Open-Meteo 辐照晴天 +12~19%、阴雨天可达 +70%，
        一个比值顾此失彼（10-28 阴雨日漏修差 66%）。改为「按天气分档标定 K」——
        偏差由 model.calibrate_regimes 的分档 K 吸收，见 _om_ctx()。
        """
        fc = self._raw("forecast")
        data = dict(fc.data or {}) if fc and fc.ok and fc.data else {}
        return data

    def _forecast_hourly(self):
        """预报源自身的逐时辐照形状 {date: [24 个 W/m²]}（未校正，仅作相对权重）。"""
        fc = self._raw("forecast")
        return ((fc.meta or {}).get("hourly") or {}) if fc and fc.ok else {}

    def _weather_import_store(self):
        """手动导入的气象仪库（data/weather_station.json），按文件 mtime 缓存。

        导入接口写盘后 mtime 变化 → 下一次读取自动重读，不需要重启服务。
        """
        try:
            mt = os.path.getmtime(weather_import.STORE_PATH)
        except OSError:
            return {"version": 1, "days": {}, "files": {}, "updated_at": None}
        if getattr(self, "_wstore_sig", None) == mt:
            return self._wstore
        st = weather_import.load_store()
        self._wstore, self._wstore_sig = st, mt
        return st

    def _om_ctx(self):
        """天气分档模型上下文（缓存 60s）：OM 辐照、云量、分档 K、全局/地面 K。

        标定样本 = 实测电量 × OM 模型辐照配对（近 45 天），完全不依赖地面气象仪；
        云量来自 Open-Meteo（比辐照无偏），用于给每一天/预报日选分档 K。
        """
        now = time.time()
        ctx = getattr(self, "_om_ctx_cache", None)
        if ctx and now - ctx["at"] < 60:
            return ctx
        p0 = float(self.cfg["station"]["capacity_mwp"])
        irr = self._raw("irradiance")
        imeta = (getattr(irr, "meta", None) or {}) if irr else {}
        om = dict(imeta.get("om") or {})
        clouds = dict(imeta.get("om_clouds") or {})
        fc = self._raw("forecast")
        fmeta = (getattr(fc, "meta", None) or {}) if fc and fc.ok else {}
        for d, c in (fmeta.get("clouds") or {}).items():
            if c is not None:
                clouds.setdefault(d, c)
        daily = self.daily_gen()
        cal_days = int(self.cfg["model"].get("calibrate_days", 30))
        ks, reg_info = model.calibrate_regimes(daily, om, clouds, p0, days=cal_days)
        k_global = (reg_info or {}).get("k_global")
        if k_global is None:
            k_global = float(self.cfg["model"].get("k") or 0.95)
        # 地面口径 K：站内气象仪实测辐照与实测电量的配对——
        # 本地实测比任何气象模式都准（36 天重叠 r=0.94，隐含 K 波动仅 ±4%），
        # 有仪器数据的历史日一律优先用它回算，OM 只管预报与仪器空窗期。
        # 来源优先级：手动导入库（data/weather_station.json）> 旧目录扫描（Downloads/2026-*.xls）——
        # 导入库是显式入库、带导入时间与质量标记，比"扫目录碰运气"可靠。
        wstore = self._weather_import_store()
        imported_ground = weather_import.ground_days_of(wstore)
        ground = dict(imported_ground)
        for d, v in (imeta.get("ground") or {}).items():
            if v is not None and d not in ground:
                ground[d] = v
        gpairs = []
        for d in sorted(set(daily) & set(ground)):
            e, v = daily[d], ground[d]
            if e and v and e > 50 and v > 0.8:
                gpairs.append(e / (v * p0 * 1000.0))
        gpairs = gpairs[-cal_days:]
        k_ground = sorted(gpairs)[len(gpairs) // 2] if len(gpairs) >= 3 else None
        irr_map = self.irr_daily()
        info = {"regimes": reg_info, "k_global": round(k_global, 4),
                "k_ground": (round(k_ground, 4) if k_ground else None),
                "ground_samples": len(gpairs), "om_days": len(om), "cloud_days": len(clouds),
                "ground_days": len(ground), "imported_days": len(imported_ground),
                "imported_last": (max(imported_ground) if imported_ground else None),
                "imported_at": (wstore or {}).get("updated_at")}
        ctx = {"om": om, "clouds": clouds, "ks": ks, "k_global": k_global,
               "k_ground": k_ground, "info": info, "daily": daily,
               "irr_map": irr_map, "ground": ground, "p0": p0, "at": now,
               "wstore": wstore}
        self._om_ctx_cache = ctx
        return ctx

    def _model_k(self):
        """全局效率系数（分档样本的中位数）：供展示与兜底；分档 K 见 _om_ctx()。"""
        ctx = self._om_ctx()
        reg = (ctx["info"].get("regimes") or {})
        k = ctx["k_global"]
        info = {"k": k, "source": reg.get("source", "default"),
                "samples": reg.get("samples", 0), "model_info": ctx["info"]}
        return k, info

    def _hist_model(self, d, irr_map=None, ctx=None):
        """历史某天的模型回算电量（kWh）：气象仪覆盖日 > OM 分档 K > 其他辐照。"""
        ctx = ctx or self._om_ctx()
        if irr_map is None:
            irr_map = ctx["irr_map"]
        g = ctx["ground"].get(d)
        if g and ctx["k_ground"]:
            # 站内气象仪实测辐照：本地真值，优先级最高（K 波动仅 ±4%）
            return model.forecast_day(g, ctx["p0"], ctx["k_ground"])
        om = ctx["om"].get(d)
        if om:
            return model.forecast_day(om, ctx["p0"], model.k_for(ctx["ks"], ctx["clouds"].get(d)))
        irr = irr_map.get(d)
        if irr:
            return model.forecast_day(irr, ctx["p0"], ctx["k_ground"] or ctx["k_global"])
        return None

    def _dynamic_ratio(self, date, measured, prof, upto, min_hours=2, min_hour=10):
        """当日动态修正系数：已实现白天时段的 实测/预测 比值（夹取 0.40~1.15）。

        触发条件：当天 · 已过 10:00（原 11:00 会让上午大半天裸奔在未修正基线上）
        · 至少 2 个已实现白天时段。
        返回 None = 不修正（早晨样本太少，修正会放大噪声）。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if date != today or upto < min_hour or not prof:
            return None
        num = den = 0.0
        n = 0
        for h in range(upto):
            p = prof[h]["kwh"] if h < len(prof) else 0.0
            if p <= 0.05:                     # 只统计有预测量的白天时段
                continue
            m = (measured or {}).get(h)
            den += p
            num += (m["kwh"] if m else 0.0)
            if m:
                n += 1
        if den <= 0 or n < min_hours:
            return None
        ratio = max(0.40, min(1.15, num / den))
        return {"ratio": round(ratio, 3), "basis_hours": n,
                "basis_kwh": round(num, 1), "baseline_kwh": round(den, 1)}

    def price(self):
        fin = self.cfg.get("finance") or {}
        return float(fin.get("price_yuan_per_kwh") or 0)

    def daily_gen(self):
        """日发电量（kWh）。逆变器报表比能量趋势导出更新时，以报表合计为准。"""
        gen = self._raw("generation")
        daily = dict((gen.data or {}).get("daily", {}) if gen and gen.data else {})
        inv = self._raw("inverters")
        if daily and inv and inv.ok and inv.data.get("date"):
            d = inv.data["date"]
            if d in daily and inv.data.get("total_kwh"):
                v = round(inv.data["total_kwh"], 1)
                if v > daily[d]:
                    daily[d] = v
        return daily

    def irr_daily(self):
        """日辐照 kWh/m²：独立辐照源优先；发电源自带辐照（华为日数据接口）时回补。

        手动导入的气象仪日值最后叠加（地面真值 > 模型值）：导入库既是显式入库、
        又带质量标记，比 Open-Meteo 模型实况可靠——页面「当月辐照」因此能对上仪器。
        """
        irr = self._raw("irradiance")
        d = dict(irr.data or {}) if irr and irr.data else {}
        gen = self._raw("generation")
        gd = (gen.data or {}).get("irradiance") if gen and gen.data else None
        if gd:
            for k, v in (gd or {}).items():
                if v is not None and not d.get(k):
                    d[k] = v
        for k, v in weather_import.ground_days_of(self._weather_import_store()).items():
            if v:
                d[k] = v
        return d

    def overview(self):
        cfg = self.cfg
        p0 = float(cfg["station"]["capacity_mwp"])
        # 每天第一次打开总览时固化当日预测快照（内部有幂等判断，失败不影响主流程）
        try:
            self.record_forecast_snapshot()
        except Exception:
            pass
        gen = self._raw("generation")
        irr = self._raw("irradiance")
        inv = self._raw("inverters")
        daily = self.daily_gen()
        power5 = (gen.data or {}).get("power5", []) if gen and gen.data else []
        ctx = self._om_ctx()
        days = sorted(daily)
        # 数据里的“最新一天” vs 日历上的“真实今天”——数据源没更新到今天时绝不能把昨天当今天显示
        data_day = days[-1] if days else None
        cal_today = datetime.now().strftime("%Y-%m-%d")
        is_current = (data_day == cal_today)
        today = cal_today
        # 完整日（不含今天本身，今天未结束）
        full = [d for d in days if d < cal_today] if days else []

        k, k_info = self._model_k()

        # 只取“真实今天”的实测；数据源没更新到今天就是 None，不拿昨天的数据冒充实发
        actual_today = daily.get(cal_today)
        pred_today = None
        pred_today_is_estimate = False
        pred_today_adjusted = None
        prof_today = None
        dd_today = None
        if is_current:
            # 今日预测的唯一真源 = day_detail（含动态修正），
            # 保证「KPI 预测值 / 图上模型基线 / 偏差」三处是同一个数。
            try:
                dd_today = self.day_detail(cal_today)
            except Exception:
                dd_today = None
            if dd_today and dd_today.get("forecast_kwh"):
                pred_today = dd_today["forecast_kwh"]
                if dd_today.get("dynamic"):
                    pred_today_adjusted = dict(dd_today["dynamic"], adjusted_kwh=pred_today)
            else:
                # day_detail 不可用时才回落到单点计算（不改变口径，仅兜底）
                k_today = model.k_for(ctx["ks"], ctx["clouds"].get(cal_today))
                pred_today = model.forecast_day(self._forecast_irr().get(cal_today), p0, k_today)
                if pred_today is None and full:
                    recent = [daily[d] for d in full[-7:]]
                    pred_today = sum(recent) / len(recent)
                    pred_today_is_estimate = True

        # 当日峰值：优先用 5 分钟功率曲线与逐时段估算序列的较大者。
        # ⚠ 逆变器接口的 total_peak_kw 其实是「当前瞬时功率合计」，不是当日峰值，
        #   拿它当峰值会显示成刷新那一刻的功率（例如傍晚 16 kW），只在两者都缺时兜底。
        peak = None
        peak_src = None
        if is_current:
            alloc_peak = None
            try:
                vals = [h["value_kwh"] for h in ((dd_today or {}).get("hours") or []) if h.get("value_kwh")]
                if vals:
                    alloc_peak = max(vals)
            except Exception:
                alloc_peak = None
            p5 = max((v for _, v in power5), default=None) if power5 else None
            cands = [(v, s) for v, s in ((p5, "power5"), (alloc_peak, "allocated")) if v]
            if cands:
                peak, peak_src = max(cands)
        if peak is None and inv and inv.ok and inv.data.get("date") == cal_today:
            peak = inv.data.get("total_peak_kw")
            peak_src = "inverters_now"
        # 数据源未更新到今天时，仍展示“有数据的最新一天”的实测值（标签带日期，绝不冒充今日）
        latest_peak = None
        latest_peak_src = None
        if data_day:
            try:
                rec = (self.actual_merged().get("rows") or {}).get(data_day) or {}
                if rec.get("peak_kw"):
                    latest_peak, latest_peak_src = rec["peak_kw"], "curve"
            except Exception:
                pass
        if latest_peak is None and power5:
            latest_peak = max(v for _, v in power5)
            latest_peak_src = "power5"
        latest_forecast = self._hist_model(data_day) if data_day else None
        latest_forecast_est = False
        if latest_forecast is None and full:
            # 该日辐照缺测：用近期日均作参考值（前端标注为参考，不冒充模型输出）
            recent = [daily[d] for d in full[-7:] if daily.get(d)]
            if recent:
                latest_forecast = sum(recent) / len(recent)
                latest_forecast_est = True

        # 当日 / 最新数据日 的偏差与准确率（供 KPI 卡片直接展示）
        # 全站统一口径：dev = (实测−模型)/模型（正=超发），准确率 = max(0, 100−|dev|)。
        # 进行中的当天与「全天预测」比必然巨亏（早上只会越来越大），
        # 改与「截至当前的预期电量」比——这才是"模型今天准不准"的诚实回答。
        def _dev_acc(a, p):
            if not a or not p or p <= 0:
                return None, None
            dev = (a - p) / p * 100
            return (round(dev, 1), round(max(0.0, 100 - abs(dev)), 1))
        day_done = datetime.now().hour >= 19
        expected_now = dd_today.get("expected_now_kwh") if dd_today else None
        if dd_today and expected_now:
            today_dev = dd_today.get("dev_expected_pct")
            today_acc = (round(max(0.0, 100 - abs(today_dev)), 1)
                         if today_dev is not None else None)
        else:
            today_dev, today_acc = _dev_acc(actual_today, pred_today)
        latest_dev, latest_acc = _dev_acc(daily.get(data_day), latest_forecast)

        accs, devs = [], []
        for d in full:
            p = self._hist_model(d)
            a = daily.get(d)
            if p and a and a > 0:
                accs.append(model.accuracy(a, p))
                devs.append((a - p) / p * 100)
        pass_line = float(cfg["model"].get("pass_line_pct", 85))

        # 未来 30 天预测（用于首页概览条）
        fc_rows = self.forecast_days(FORECAST_HORIZON_DAYS + 1)[0]
        price = self.price()
        fc30 = sum((r["kwh"] or 0) for r in fc_rows[:30])

        return {
            "station": cfg["station"],
            "as_of": {
                "generation": gen.as_of.isoformat(timespec="seconds") if gen and gen.as_of else None,
                "irradiance": irr.as_of.isoformat(timespec="seconds") if irr and irr.as_of else None,
                "inverters": inv.as_of.isoformat(timespec="seconds") if inv and inv.as_of else None,
            },
            "today": today,
            "data_day": data_day,
            "is_current": is_current,
            "kpi": {
                "actual_today_kwh": actual_today,
                "latest_day_kwh": daily.get(data_day) if data_day else None,
                "actual_is_partial": bool(power5) and bool(full) and is_current,
                "forecast_today_kwh": pred_today,
                "forecast_today_is_estimate": pred_today_is_estimate,
                "forecast_today_adjusted": pred_today_adjusted,
                "peak_kw": peak,
                "peak_source": peak_src,
                "latest_peak_kw": latest_peak,
                "latest_peak_source": latest_peak_src,
                "latest_forecast_kwh": latest_forecast,
                "latest_forecast_is_estimate": latest_forecast_est,
                "today_dev_pct": today_dev,
                "today_accuracy_pct": today_acc,
                "today_dev_basis": ("expected_now" if expected_now else "full_day"),
                # 截至当前（当前小时按已过分钟折算）的预期电量：与「当前实发」同窗口，
                # KPI 卡片拿它当「当前预测」，这样两个数才可比、也才和图内结论一致
                "expected_now_kwh": (round(expected_now, 1) if expected_now else None),
                "forecast_day_end_kwh": (round(pred_today, 1) if pred_today else None),
                "latest_dev_pct": latest_dev,
                "latest_accuracy_pct": latest_acc,
                "forecast_30d_kwh": round(fc30, 1),
                "accuracy_pct": (sum(accs) / len(accs)) if accs else None,
                "accuracy_samples": len(accs),
                "accuracy_pass": sum(1 for a in accs if a >= pass_line),
                "pass_line_pct": pass_line,
                "max_dev_pct": max(devs) if devs else None,
                "min_dev_pct": min(devs) if devs else None,
            },
            "model": k_info,
            "finance": {"price_yuan_per_kwh": price,
                        "currency": (cfg.get("finance") or {}).get("currency", "CNY")},
        }

    # ---------- 未来预测 ----------
    def forecast_days(self, n=31):
        """从今天起 n 天的未来日预测：[{date, irradiance, kwh, revenue, basis}]

        辐照优先级：气象预报源（Open-Meteo，未来 ~16 天）> 历史气候学推算（更远日期兜底）。
        """
        p0 = float(self.cfg["station"]["capacity_mwp"])
        _, k_info = self._model_k()
        ctx = self._om_ctx()
        fdata = self._forecast_irr()
        today = datetime.now().strftime("%Y-%m-%d")
        rows = []
        for i in range(n):
            ds = (datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d")
            v = fdata.get(ds)
            if v:
                # 分档 K：按该日预报云量选晴/多云/阴雨档——阴雨日的 OM 辐照高估
                # 由该档 K 吸收，不再用单一标量校正
                k = model.k_for(ctx["ks"], ctx["clouds"].get(ds))
                g = model.regime_key(ctx["clouds"].get(ds))
                irr_v = v
                basis = "Open-Meteo 气象预报辐照 %.2f kWh/m² × 分档K %.3f（%s）" % (
                    v, k, model.REGIME_LABEL.get(g, "云量未知"))
            else:
                irr_v, basis = model.future_irradiance(ctx["irr_map"], ds)
                k = ctx["k_global"]
                if fdata:
                    basis += "（超出预报范围，气候学推算）"
            kwh = model.forecast_day(irr_v, p0, k)
            rows.append({"date": ds, "irradiance": irr_v, "kwh": (round(kwh, 1) if kwh else None),
                         "basis": basis})
        return rows, k_info

    def series(self, rng="mid"):
        """未来发电预测序列：short=今日 00:00–23:00 逐小时（kWh），mid=15 天，long=30 天。

        只给电量，不含收益换算（收益展示已在看板中移除）。
        """
        rows, k_info = self.forecast_days(FORECAST_HORIZON_DAYS + 1)
        gen = self._raw("generation")
        irr = self._raw("irradiance")
        out = {
            "range": rng, "scope": "future", "unit": "kWh",
            "k": k_info.get("k"), "k_source": k_info.get("source"),
            "as_of": gen.as_of.isoformat(timespec="seconds") if gen and gen.as_of else None,
            "irr_as_of": irr.as_of.isoformat(timespec="seconds") if irr and irr.as_of else None,
        }
        if rng == "short":
            # 短期按自然日 00:00 起画满 24 小时，X 轴从 00 时开始更易读
            start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            pred = {r["date"]: r["kwh"] for r in rows}
            pts = model.hourly_series(pred, start, 24)
            out.update({
                "granularity": "hour", "unit_note": "kWh / 小时",
                "start": start.strftime("%Y-%m-%d 00:00"),
                "end": start.strftime("%Y-%m-%d 23:00"),
                "points": pts,
                "total_kwh": round(sum(p["kwh"] or 0 for p in pts), 1),
            })
            return out
        n = 15 if rng == "mid" else 30
        sel = rows[:n]
        pts = [{"t": r["date"][5:], "date": r["date"], "kwh": r["kwh"],
                "irradiance": r["irradiance"]} for r in sel]
        out.update({
            "granularity": "day", "unit_note": "kWh / 日",
            "start": sel[0]["date"] if sel else None,
            "end": sel[-1]["date"] if sel else None,
            "points": pts,
            "total_kwh": round(sum(p["kwh"] or 0 for p in pts), 1),
        })
        return out

    def intraday(self, date=None, slot_min=60):
        """某一天逐时段对照（实测 vs 模型）。date 为空时取「最新有数据日」——
        数据源没更新到今天时绝不把昨天当今天显示。"""
        if not date:
            days = self._history_days()
            date = days[-1] if days else None
        if not date:
            return {"error": "暂无发电数据", "range": self.forecast_range()}
        return self.day_detail(date, slot_min=slot_min)

    # ---------- 历史 / 未来 统一口径 ----------
    def _history_days(self):
        """有实测发电的日期（升序）。并网前与停运日（发电 0）不进任何图表。"""
        daily = self.daily_gen()
        return [d for d in sorted(daily) if (daily.get(d) or 0) > 0]

    def _day_pair(self, d, daily, irr_map, p0, ctx=None):
        """历史某天的对账值：实测 kWh、模型回算 kWh（分档 K）、辐照、推算依据、状态。

        irr_map 为展示口径辐照（原始值）；OM 覆盖日按云量选分档 K，
        其余历史日按地面口径 K，模型回算与预测输入同源。
        """
        ctx = ctx or self._om_ctx()
        a = daily.get(d)
        irr = irr_map.get(d)
        p = self._hist_model(d, irr_map, ctx)
        est, basis, status = None, None, None
        if p is None and self._forecast_irr().get(d):
            # 预报源覆盖该日（典型是「今天」：OM 归档 drop_today 后无当日值，
            # 此前会错误回落气候学推算，导致总览与详情页基线打架）
            fv = self._forecast_irr()[d]
            k = model.k_for(ctx["ks"], ctx["clouds"].get(d))
            p = model.forecast_day(fv, p0, k)
            basis = "Open-Meteo 当日预报辐照 %.2f kWh/m² × 分档K %.3f（%s）" % (
                fv, k, model.REGIME_LABEL.get(model.regime_key(ctx["clouds"].get(d)), "云量未知"))
        if p is None and a:
            # 辐照缺测（气象仪未入库 / 华为不返回辐照）→ 用气候学推算补齐，
            # 否则模型线整段消失，看不出实测与模型的对比关系
            irr_est, b = model.future_irradiance(irr_map, d)
            if irr_est:
                est, basis = irr_est, b
                p = model.forecast_day(irr_est, p0, ctx["k_global"])
                status = "irr_estimated"
            else:
                status = "irr_missing"
        else:
            status = "ok" if (p is not None and a) else ("irr_missing" if a else "no_data")
        return {"actual_kwh": a, "forecast_kwh": (round(p, 1) if p is not None else None),
                "irradiance": irr, "irradiance_est": est, "basis": basis, "status": status}

    def _future_pair(self, d, irr_map, p0, ctx=None):
        """未来某天的预测值（无实测，只有模型输出）。预报源可用时优先用预报辐照。"""
        ctx = ctx or self._om_ctx()
        v = self._forecast_irr().get(d)
        if v:
            k = model.k_for(ctx["ks"], ctx["clouds"].get(d))
            g = model.regime_key(ctx["clouds"].get(d))
            irr, est = v, None
            basis = "Open-Meteo 气象预报辐照 %.2f kWh/m² × 分档K %.3f（%s）" % (
                v, k, model.REGIME_LABEL.get(g, "云量未知"))
        else:
            irr, basis = model.future_irradiance(irr_map, d)
            est, basis = irr, (basis + "（超出预报范围，气候学推算）" if self._forecast_irr() else basis)
            k = ctx["k_global"]
        p = model.forecast_day(irr, p0, k)
        return {"actual_kwh": None, "forecast_kwh": (round(p, 1) if p is not None else None),
                "irradiance": irr, "irradiance_est": est, "basis": basis, "status": "forecast"}

    def forecast_range(self):
        """日历可选范围：首个有实测发电的历史日 ~ 今天 + FORECAST_HORIZON_DAYS。"""
        today = datetime.now().strftime("%Y-%m-%d")
        hist = self._history_days()
        return {"min_date": (hist[0] if hist else today),
                "max_date": (datetime.now() + timedelta(days=FORECAST_HORIZON_DAYS)).strftime("%Y-%m-%d"),
                "today": today,
                "default_date": today,
                "horizon_days": FORECAST_HORIZON_DAYS,
                "history_days": len(hist)}

    def calendar(self):
        """日历上每一天：历史日给实测 + 模型回算，未来日给模型预测。"""
        rng = self.forecast_range()
        p0 = float(self.cfg["station"]["capacity_mwp"])
        _, k_info = self._model_k()
        daily = self.daily_gen()
        ctx = self._om_ctx()
        irr_map = ctx["irr_map"]
        days = {}
        for d in self._history_days():
            r = self._day_pair(d, daily, irr_map, p0, ctx)
            r.update({"date": d, "is_future": False, "is_today": d == rng["today"]})
            days[d] = r
        for row in self.forecast_days(FORECAST_HORIZON_DAYS + 1)[0]:
            d = row["date"]
            if d in days:
                continue
            r = self._future_pair(d, irr_map, p0, ctx)
            r.update({"date": d, "is_future": True, "is_today": d == rng["today"]})
            days[d] = r
        return {"days": [days[d] for d in sorted(days)], "range": rng,
                "k": k_info.get("k"), "today": rng["today"]}

    def day_detail(self, date, slot_min=60):
        """某一天的统一视图：历史日「实测 vs 模型回算」，未来日「模型预测」。

        slot_min 支持时段粒度（60=逐小时[默认]，15=15 分钟，兼容 5/10/15/20/30）。
        逐小时/逐时段实测只在有 5 分钟功率曲线的日子（通常只有当天）才有，
        其它历史日给出日电量级的实测与模型对账，绝不伪造逐时段曲线。
        分摊（allocation）块始终按小时口径输出（守恒计算以小时为单位）。
        """
        try:
            slot_min = int(slot_min)
        except Exception:
            slot_min = 60
        if slot_min not in (5, 10, 15, 20, 30, 60):
            slot_min = 60
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
        except Exception:
            return {"error": "日期格式应为 YYYY-MM-DD", "range": self.forecast_range()}
        rng = self.forecast_range()
        if date < rng["min_date"] or date > rng["max_date"]:
            return {"error": "日期超出可选范围（%s ~ %s）" % (rng["min_date"], rng["max_date"]),
                    "range": rng}
        p0 = float(self.cfg["station"]["capacity_mwp"])
        _, k_info = self._model_k()
        daily = self.daily_gen()
        ctx = self._om_ctx()
        today = rng["today"]
        is_future = date > today
        is_today = date == today

        if is_future:
            pair = self._future_pair(date, ctx["irr_map"], p0, ctx)
        else:
            pair = self._day_pair(date, daily, ctx["irr_map"], p0, ctx)
            if pair["status"] == "no_data":
                return {"error": "%s 无发电记录（并网前或停运日）" % date, "range": rng}
        fc_kwh = pair["forecast_kwh"]
        if fc_kwh is not None:
            fc_kwh = round(fc_kwh, 1)

        # 实测：5 分钟功率曲线按时段聚合 → 该时段平均功率 kW
        #（60 分钟桶时数值上 = 该小时 kWh；15 分钟桶时 kWh = 平均kW × 0.25）
        power5 = self._power5_for_date(date)          # 当天走实时源，历史日走落盘归档
        measured = allocation.bucketize(power5, slot_min=slot_min, day=date)
        measured60 = measured if slot_min == 60 else allocation.bucketize(power5, day=date)
        measured_from = measured_until = None
        for r in power5:
            ts = str(r[0])
            if measured_until is None or ts > measured_until:
                measured_until = ts
            if measured_from is None or ts < measured_from:
                measured_from = ts
        act_total = daily.get(date)

        # 分摊：接口只给得出「当日累计量」时，按同组历史日（工作日/周末）的日内分布
        # 把剩余量摊到未采集的**已结束**时段；未走完 / 未来时段一律留空，绝不铺满。
        # 分摊恒按小时粒度（权重以小时为单位），细分时段展示时取所属小时的分摊值。
        upto = 24 if date < today else datetime.now().hour
        alloc_slots, integrity = self.allocated_hours(date, measured60, act_total, upto)
        alloc_slots = alloc_slots or [{}] * 24

        # 日内形状：今天/未来日用预报自身的逐时辐照分布（保留云量时间信息），
        # 历史日无预报形状 → 钟形近似
        shape = self._forecast_hourly().get(date) if (is_future or is_today) else None
        prof = model.hourly_profile(date, fc_kwh, shape=shape)
        # 当日动态修正：已实现白天时段的 实测/预测 比值 → 缩放剩余时段的模型基线。
        # 基线是日前口径，天气偏离预报时（如突发阴天）不修正会全天偏高。
        dyn = self._dynamic_ratio(date, measured60, prof, upto) if is_today else None
        fc_baseline = round(sum(p["kwh"] for p in prof), 1) if prof else None
        # 日前基线留底（修正之前）：「承诺」与「最新判断」在图上是两条线，
        # 共用一个名字就会出现「同一个模型两个数」的口径混战。
        prof_base = [dict(p) for p in prof] if prof else None
        if dyn:
            for h in range(upto, 24):
                if h < len(prof):
                    prof[h]["kw"] = round(prof[h]["kw"] * dyn["ratio"], 1)
                    prof[h]["kwh"] = round(prof[h]["kwh"] * dyn["ratio"], 3)
        sph = 60 // slot_min                          # 每小时的时段桶数
        n_slots = 24 * sph
        exp_samples = allocation.expected_samples(slot_min)
        now_slot = (datetime.now().hour * 60 + datetime.now().minute) // slot_min if is_today else None

        def _slot_curve(profile):
            """小时电量 → 时段功率：以「小时中点」为采样点线性插值，再整体缩放守恒。

            旧实现把小时值当作 h 时刻的值、并在小时内各自归一，带来两个毛病：
              1) 相位偏 0.5 小时 → 日出后 07:45→08:00 出现 +453% 的台阶；
              2) 每小时单独缩放 → 小时边界必然不连续。
            改为中心对齐 + 全局缩放：曲线连续，日总量仍严格守恒（偏差口径不变）。
            """
            if not profile:
                return None
            base = [profile[h]["kw"] for h in range(min(24, len(profile)))]
            if not base:
                return None
            if sph <= 1:
                return list(base)
            last = len(base) - 1
            vals = []
            for i in range(n_slots):
                u = (i + 0.5) / sph - 0.5              # 小时中心坐标系：0.5 = 小时0中心
                if u <= 0:
                    vals.append(base[0])
                elif u >= last:
                    vals.append(base[last])
                else:
                    i0 = int(u)
                    fr = u - i0
                    vals.append(base[i0] * (1 - fr) + base[i0 + 1] * fr)
            got = sum(vals) * slot_min / 60.0
            want = sum(profile[h]["kwh"] for h in range(min(24, len(profile))))
            if got > 0 and want > 0:
                sc = want / got
                vals = [v * sc for v in vals]
            return vals

        slot_kw = _slot_curve(prof)
        # 只有真的发生过修正时两条线才不同，否则不多画一条重合的线
        slot_kw_base = _slot_curve(prof_base) if dyn else None
        # 逐时段辐照（W/m²）：优先站内气象仪实测逐时（手动导入库），空窗用预报逐时替补。
        # 两条来源混在一根线上时必须标出来源（ghi_src），否则"实测辐照"会被当成预报。
        ghi_by_hour = {h: shape[h] for h in range(24)
                       if shape and h < len(shape) and shape[h] is not None} if shape else {}
        ghi_meas = weather_import.ground_hourly_of(self._weather_import_store(), date)
        for h, v in (ghi_meas or {}).items():
            if 0 <= h < 24:
                ghi_by_hour[h] = v
        # 偏差只在「有实测的时段」内比较，不拿半天实测去比全天基线
        hours, act_sum, fc_sum, fc_sum_same = [], 0.0, 0.0, 0.0
        for i in range(n_slots):
            h = i // sph
            m = measured.get(i)
            mv = m["kwh"] if m else None
            a_kw = round(mv, 1) if m else None
            a_kwh = round(mv * slot_min / 60.0, 3) if m else None
            f_kw = (round(slot_kw[i], 1) if slot_kw else
                    (round(prof[h]["kw"], 1) if prof and h < len(prof) else 0.0))
            # 日前基线（未修正）的同口径功率序列：与 forecast_kw 并存，
            # 图上分「承诺」和「最新判断」两条线，避免一个模型两个数
            b_kw = None
            if dyn:
                b_kw = (round(slot_kw_base[i], 1) if slot_kw_base else
                        (round(prof_base[h]["kw"], 1)
                         if prof_base and h < len(prof_base) else None))
            ghi = ghi_by_hour.get(h)
            f_adj = bool(dyn and h >= upto and f_kw > 0)
            f_frac = f_kw * slot_min / 60.0           # 该时段的模型电量
            if a_kw is not None:
                act_sum += a_kwh
                fc_sum_same += f_frac
            fc_sum += f_frac
            sl = alloc_slots[h] if h < len(alloc_slots) else {}
            hours.append({"hour": i, "t": allocation.slot_label(i, slot_min),
                          # actual_kw 是功率显示值（1 位小数）；actual_kwh 是该时段电量（3 位小数）。
                          # 60 分钟桶时两者数值相等，保持既有口径。
                          "actual_kw": a_kw, "forecast_kw": f_kw, "baseline_kw": b_kw,
                          "actual_kwh": a_kwh, "forecast_kwh": round(f_frac, 3),
                          "ghi_w": (round(ghi, 0) if ghi is not None else None),
                          "ghi_src": ("实测" if h in ghi_meas else ("预报" if ghi is not None else None)),
                          "fc_adjusted": f_adj,
                          # 展示取值：有实测取实测，否则取所属小时的分摊估算值（未来时段为 None）
                          "value_kwh": (a_kwh if m else sl.get("value_kwh")),
                          "allocated_kwh": sl.get("allocated_kwh"),
                          "source": ("measured" if m else (sl.get("source") or allocation.SRC_NONE)),
                          "estimated": (bool(sl.get("estimated")) if not m else False),
                          "partial": (bool(m and m["samples"] < exp_samples)
                                      or bool(sl.get("partial") and not m)),
                          "weight": sl.get("weight"),
                          "samples": (m["samples"] if m else sl.get("samples")),
                          "samples_expected": (exp_samples if m else sl.get("samples_expected"))})

        if dyn:
            # 动态修正后：全天预测 = 曲线积分，KPI 与图上基线必须是同一个数
            fc_kwh = round(fc_sum, 1)
        gap = ((act_sum - fc_sum_same) / fc_sum_same * 100) if (fc_sum_same > 0 and act_sum) else None
        dev = ((act_total - fc_kwh) / fc_kwh * 100) if (act_total and fc_kwh) else None
        # 进行中的当天：与「截至当前预期电量」比（已结束时段全额 + 当前时段按分钟折算），
        # 这才是"模型今天准不准"的诚实回答；与全天基线比必然显示巨亏。
        expected_now = None
        if is_today and fc_kwh:
            now_dt = datetime.now()
            cur_h = now_dt.hour
            if cur_h < 19:
                expected_now = sum(h["forecast_kwh"] for h in hours
                                   if h["hour"] // sph < cur_h)
                if now_slot is not None and now_slot < len(hours):
                    expected_now += hours[now_slot]["forecast_kwh"] * (cur_h * 60 + now_dt.minute) % slot_min / slot_min
        dev_exp = ((act_total - expected_now) / expected_now * 100) if (act_total and expected_now) else None
        sr, ss = model.daylight(date[5:7])
        week = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.weekday()]
        return {
            "date": date, "weekday": week,
            "is_future": is_future, "is_today": is_today, "is_current": is_today,
            "actual_kwh": round(act_total, 1) if act_total is not None else None,
            "kwh": fc_kwh, "forecast_kwh": fc_kwh,
            "forecast_baseline_kwh": fc_baseline,
            "dynamic": dyn,
            "irradiance": pair["irradiance"], "irradiance_est": pair["irradiance_est"],
            "basis": pair["basis"], "status": pair["status"],
            "equivalent_hours": round((fc_kwh or 0) / (p0 * 1000), 2),   # kWh/kWp
            "peak_kw": round(max((h["forecast_kw"] or 0) for h in hours), 1) if hours else None,
            "sunrise": "%02d:%02d" % (sr // 60, sr % 60),
            "sunset": "%02d:%02d" % (ss // 60, ss % 60),
            "hours": hours,
            "slot_min": slot_min,
            "allocation": integrity,
            "now_hour": upto,                    # 已结束时段的边界（小时）：今天=当前小时，历史日=24
            "now_slot": now_slot,                # 已结束时段边界（按 slot_min 细分的桶序号）
            "actual_total_kwh": round(act_total, 1) if act_total is not None else None,
            "forecast_total_kwh": round(fc_sum, 1),
            "forecast_is_estimate": pair["status"] == "irr_estimated",
            "measured_sum_kwh": round(act_sum, 1),
            "forecast_measured_sum_kwh": round(fc_sum_same, 1),
            "measured_until": (measured_until or "")[:16] or None,
            "measured_from": (measured_from or "")[:16] or None,
            "measured_hours": sum(1 for h in range(24) if h in measured60),
            "gap_pct": round(gap, 1) if gap is not None else None,
            "dev_pct": round(dev, 1) if dev is not None else None,
            "expected_now_kwh": round(expected_now, 1) if expected_now else None,
            "dev_expected_pct": round(dev_exp, 1) if dev_exp is not None else None,
            "accuracy_pct": (round(model.accuracy(act_total, fc_kwh), 1)
                             if (act_total and fc_kwh) else None),
            "pass_line_pct": float(self.cfg["model"].get("pass_line_pct", 85)),
            "has_actual": any(h["actual_kw"] is not None for h in hours),
            "has_daily_actual": act_total is not None,
            "k": k_info.get("k"), "k_source": k_info.get("source"),
            "range": rng,
        }

    def trend(self, scope="all", limit=30):
        """逐日趋势（历史 + 未来同一条时间轴）：实测实线、模型/预测虚线。

        scope: recent=最近 limit 个历史日 / future=今天起 30 天 / all=全部历史 + 未来。

        历史段与「历史实际数据」明细表共用 `_merged_day_rows`，两者点数、实测值、状态
        严格一致：
          · 停运 / 并网前日实测为 0 → 画真实 0 点（配合模型线可直接看停机损失）；
          · 只有辐照、没有发电记录的日子实测为 None → 实测线断开，绝不画成假的 0 线；
          · 未来段无实测，实测线自然为空。
        """
        rng = self.forecast_range()
        p0 = float(self.cfg["station"]["capacity_mwp"])
        day_done = datetime.now().hour >= 19

        hist_src, _, _ = self._merged_day_rows()
        hist = []
        for r in hist_src:
            r = dict(r)
            r.update({"is_future": False, "is_today": r["date"] == rng["today"],
                      "partial": (r["date"] == rng["today"] and not day_done)})
            hist.append(r)

        # 未来预报的气候学参照：把合并集里的辐照也并进去，样本更全
        ctx = self._om_ctx()
        irr_full = dict(ctx["irr_map"])
        for r in hist:
            if r["irradiance"] is not None:
                irr_full[r["date"]] = r["irradiance"]

        fut = []
        have = {x["date"] for x in hist}
        for row in self.forecast_days(FORECAST_HORIZON_DAYS + 1)[0]:
            d = row["date"]
            if d in have:
                continue
            r = self._future_pair(d, irr_full, p0, ctx)
            r.update({"date": d, "is_future": True, "is_today": d == rng["today"], "partial": False})
            fut.append(r)

        if scope == "future":
            rows = [x for x in hist if x["is_today"]] + fut
        elif scope == "recent":
            rows = hist[-limit:] if limit else list(hist)
        else:
            rows = hist + fut
        # 环境参数（温度/湿度/风/辐射）按日挂到 trend 行上，供前端参数下拉框切换
        wth = self._raw("weather")
        wmap = dict(wth.data or {}) if wth and wth.ok and wth.data else {}
        for r in rows:
            w = wmap.get(r["date"])
            if w:
                r["weather"] = w
        # 「今天」的模型值必须与当日视图/KPI 是同一个数：
        # 那边是动态修正后的值，这里若仍用未修正的基线，同一个模型在两张图上会出现两个数
        for r in rows:
            if r.get("is_today"):
                try:
                    dd = self.day_detail(r["date"])
                    if dd.get("forecast_kwh"):
                        r["forecast_kwh"] = dd["forecast_kwh"]
                        r["forecast_baseline_kwh"] = dd.get("forecast_baseline_kwh")
                        r["dynamic"] = dd.get("dynamic")
                except Exception:
                    pass
        # 统计按当前 scope 的 rows 计算，否则「近 30 天」会显示全量数字，与图上点数对不上
        hs = [x for x in rows if not x["is_future"]]
        k = self._model_k()[0]
        return {"rows": rows, "scope": scope, "k": k, "today": rng["today"],
                "history_days": len(hs),
                "history_first": hs[0]["date"] if hs else None,
                "history_last": hs[-1]["date"] if hs else None,
                "counts": {
                    "with_actual": sum(1 for r in hs if r["actual_kwh"] is not None
                                       and r["actual_kwh"] > 0),
                    "no_generation": sum(1 for r in hs if r["status"] == "no_generation"),
                    "irr_only": sum(1 for r in hs if r["status"] == "irr_only"),
                    "future": sum(1 for r in rows if r["is_future"]),
                },
                "pass_line_pct": float(self.cfg["model"].get("pass_line_pct", 85))}

    def explain(self):
        """预测说明：用了哪些历史数据、什么依据、什么电价、什么不确定性。"""
        daily = self.daily_gen()
        irr = self.irr_daily()
        k, k_info = self._model_k()
        ctx = self._om_ctx()
        minfo = k_info.get("model_info") or {}
        reg = minfo.get("regimes") or {}
        gen = self._raw("generation")
        irr_res = self._raw("irradiance")
        fc = self._raw("forecast")
        fdata = self._forecast_irr()
        days = sorted(daily)
        reg_line = "、".join(
            "%s K=%.3f（%d 天）" % (model.REGIME_LABEL[g], (reg.get("k") or {}).get(g) or 0,
                                    ((reg.get("detail") or {}).get(g) or {}).get("n", 0))
            for g in model.REGIMES)
        if fdata:
            fw = {
                "method": "Open-Meteo 数值天气预报（未来 %d 天）+ 历史气候学推算兜底" % len(fdata),
                "detail": [
                    "预报范围内：直接取 Open-Meteo 的逐小时辐照预报按日求和（今天 ~ %s）" % max(fdata),
                    "按天气分档标定 K：预报云量分晴/多云/阴雨三档，各档用近 45 天「实测电量 × 模型辐照」配对样本取中位数——%s" % reg_line,
                    "分档的原因：OM 辐照晴天系统性高估 +12~19%、阴雨天可达 +70%，单一标量校正只能修一种天气（10-28 阴雨日曾差 66%）",
                    "超出预报范围：回落历史气候学推算（同月历史日均或近期日均 × 季节因子）",
                    "当日 10:00 后按已实现时段的 实测/预测 比值动态修正剩余时段（天气偏离预报时的兜底）",
                ],
                "forecast_source": (fc.meta or {}).get("provider") if fc else "open-meteo",
            }
        else:
            fw = {
                "method": "历史同期气候学推算（预报源不可用时的兜底）",
                "detail": [
                    "目标月份有历史样本：取该月历史日均辐照作月典型值，再按日序把该月的真实日起伏回映过来（保留 60% 波动）",
                    "目标月份无历史样本：取最近 30 天日均辐照 × 该月季节因子（深圳约 22.6°N）",
                    "预报源恢复后自动切回 Open-Meteo 预报辐照（config.json → sources.forecast）",
                ],
                "forecast_source": "未生效" + ("（%s）" % fc.error if fc and fc.error else ""),
            }
        with_irr = [d for d in days if (irr.get(d) or 0) > 0]
        months = model.month_typical(irr)
        # 回测准确率（模型对历史日的解释能力，分档 K 口径）
        accs = []
        for d in days:
            if d == datetime.now().strftime("%Y-%m-%d"):
                continue
            p = self._hist_model(d, ctx=ctx)
            if p and daily.get(d):
                accs.append(model.accuracy(daily[d], p))
        return {
            "formula": "日预测电量(kWh) = 水平面总辐照(kWh/m²) × 装机容量(MWp) × 综合效率系数 K × 1000",
            "k": {"value": round(k, 4), "source": k_info.get("source"), "samples": k_info.get("samples"),
                  "regimes": {"k_by_regime": reg.get("k"), "detail": reg.get("detail"),
                              "k_global": minfo.get("k_global"), "k_ground": minfo.get("k_ground"),
                              "ground_samples": minfo.get("ground_samples"),
                              "om_days": minfo.get("om_days"), "cloud_days": minfo.get("cloud_days")},
                  "meaning": "组件效率、温升、逆变、线损、污渍、朝向等综合折损；按天气分档各取一个 K"},
            "history": {
                "from": days[0] if days else None,
                "to": days[-1] if days else None,
                "days": len(days),
                "days_with_irradiance": len(with_irr),
                "total_kwh": round(sum(v for v in daily.values() if v), 1),
            },
            "irradiance_source": {
                "label": self.sources["irradiance"].label,
                "mode": self.cfg["sources"]["irradiance"].get("type"),
                "as_of": irr_res.as_of.isoformat(timespec="seconds") if irr_res and irr_res.as_of else None,
                "regime_calibration": reg_line or "样本不足，暂用全局 K",
                "regime_calibration_note": (
                    "Open-Meteo 辐照的系统性偏差随天气变化（晴天 +12~19%、阴雨天可达 +70%），"
                    "按预报云量分晴/多云/阴雨三档，各档用「实测电量 × 模型辐照」配对样本标定 K，"
                    "完全不依赖地面气象仪")
                    if reg.get("source") == "calibrated_regimes" else "OM/电量配对样本不足，暂用全局 K",
            },
            "future_weather": fw,
            "monthly_typical": months,
            "profile": "日内曲线优先用预报自身的逐时辐照分布归一化摊分（保留云量时间信息），无预报形状时按当月日出—日落 sin^1.5 包络近似；曲线下面积等于当日预测电量。当日基线在 10:00 后按已实现时段的 实测/预测 比值动态修正剩余时段",
            "accuracy": {"avg_pct": round(sum(accs) / len(accs), 1) if accs else None,
                         "samples": len(accs),
                         "note": "以历史日的模型辐照 × 分档K 回算、与实测电量比对得到，代表模型本身的解释能力"},
            "caveats": ([
                "预报范围（~16 天）内是真正的天气预报辐照，晴雨变化可预知；数值模型与本地实况存在系统差，由 K 标定吸收",
                "超出预报范围的天数为气候学推算，只反映季节趋势，不预知具体天气",
                "未计入限电、检修、计划停机、组件清洗等运营因素",
            ] if fdata else [
                "未来辐照不是天气预报：预报源不可用时退回气候学推算，晴雨变化无法预知，预测值代表「气候平均意义下的期望发电量」",
                "越靠近当下越准：短期（未来 24h）落在已知季节区间内，长期（30 天）只反映季节趋势",
                "未计入限电、检修、计划停机、组件清洗等运营因素",
            ]),
            "range": self.forecast_range(),
        }

    # ---------- 历史实际数据（多源合并）----------
    def _archive_dir(self):
        """旧版看板归档目录（config.json → archive.dir，相对 site/ 解析）。"""
        a = self.cfg.get("archive") or {}
        if not a.get("enabled", True):
            return None
        d = a.get("dir") or "../legacy/data"
        return d if os.path.isabs(d) else os.path.normpath(os.path.join(ROOT, d))

    def _archive_data(self):
        d = self._archive_dir()
        if not d or not os.path.isdir(d):
            return {"gen": {}, "irr": {}, "peak": {}, "power5": [], "files": []}
        try:
            sig = tuple((n, int(os.path.getmtime(os.path.join(d, n))))
                        for n in sorted(os.listdir(d)))
        except Exception:
            sig = None
        if self._archive is not None and sig == self._archive_sig:
            return self._archive
        self._archive = actuals.load_archive(d)
        self._archive_sig = sig
        return self._archive

    def _power5_archive(self):
        """历史日的 5 分钟曲线归档（data/api_cache/power5_<date>.json）。

        实时源只带「今天」的曲线，历史日的曲线只能靠当时落盘的文件。
        今天那份由实时源给出，这里跳过，避免同一时间戳被算两次。
        """
        d = self.adapter_cache_dir()
        if not d or not os.path.isdir(d):
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        out = []
        for n in sorted(os.listdir(d)):
            if not (n.startswith("power5_") and n.endswith(".json")):
                continue
            day = n[len("power5_"):-len(".json")]
            if day == today:
                continue
            try:
                pts = json.load(open(os.path.join(d, n), encoding="utf-8"))
                if isinstance(pts, list):
                    out.append(pts)
            except Exception:
                continue
        return out

    def adapter_cache_dir(self):
        """发电源的落盘目录（仅 fusionsolar 适配器有，其它源返回 None）。"""
        src = self.sources.get("generation")
        return getattr(src, "cache_dir", None)

    # ---------- 时段分摊（接口无明细时用历史分布摊当日累计量） ----------
    def _profile_store(self):
        """日内分布权重的持久化仓库（落盘在发电源缓存目录）。"""
        if self._prof_store is not None:
            return self._prof_store
        a = self.cfg.get("allocation") or {}
        d = self.adapter_cache_dir() or os.path.join(ROOT, "data", "api_cache")
        self._prof_store = allocation.ProfileStore(
            os.path.join(d, "hour_profile.json"),
            slot_min=int(a.get("slot_minutes", 60) or 60),
            mode=a.get("mode", "weekday_weekend") or "weekday_weekend",
            min_samples=int(a.get("min_samples", 3) or 3),
            half_life_days=float(a.get("half_life_days", 14) or 14))
        return self._prof_store

    def _profile_samples(self, force=False):
        """学习用的历史样本 {date: curve}。

        只取**已经过完**的日子：当天只跑到一半，归一化后会变成「半天形态」，
        拿它当先验会把下午的权重整体抬高。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            d = self.adapter_cache_dir()
            sig = (today, tuple(sorted(n for n in os.listdir(d)
                                       if n.startswith("power5_") and n.endswith(".json")))) \
                if d and os.path.isdir(d) else None
        except Exception:
            sig = None
        if not force and self._prof_samples is not None and sig == self._prof_sig:
            return self._prof_samples
        out = {}
        # 两个来源：① 适配器当天落盘的 power5_<date>.json（历史归档）
        #          ② 旧版看板归档 legacy/data/power_5min.json
        # 只取已经过完的日子：当天只跑一半，归一化后会变成「半天形态」，
        # 拿它当先验会把下午的权重整体抬高。
        curves = list(self._power5_archive())
        try:
            ap = (self._archive_data() or {}).get("power5") or []
            if ap:
                curves.append(ap)
        except Exception:
            pass
        for pts in curves:
            for p in pts or []:
                day = str(p[0])[:10]
                if day < today:
                    out.setdefault(day, []).append(p)
        self._prof_samples, self._prof_sig = out, sig
        return out

    def _power5_for_date(self, date):
        """某一天的 5 分钟功率曲线。

        当天：实时源带的是「今天」的采样累积；
        历史日：实时源不带，只能读落盘归档（power5_<date>.json + 旧版 power_5min.json）。
        """
        gen = self._raw("generation")
        live = (gen.data or {}).get("power5", []) if gen and gen.data else []
        out = [r for r in live if len(str(r[0])) >= 13 and str(r[0])[:10] == date]
        if date < datetime.now().strftime("%Y-%m-%d"):
            for pts in self._power5_archive():
                out.extend([p for p in pts or [] if str(p[0])[:10] == date])
            try:
                out.extend([p for p in ((self._archive_data() or {}).get("power5") or [])
                            if str(p[0])[:10] == date])
            except Exception:
                pass
        return out

    def hour_profile(self, force=False):
        """当前生效的日内分布权重（供接口展示/核对）。"""
        return self._profile_store().get(self._profile_samples(), force=force)

    def allocated_hours(self, date, measured, total_kwh, upto):
        """按历史日内分布把当日累计量摊到未采集的已结束时段。

        measured  {hour: {"kwh": 平均kW, "samples": n}}  已经采集到的时段（原样保留）
        total_kwh 接口给出的当日累计量
        upto      只处理 [0, upto) 的时段 —— 未来/未走完的时段不生成估算值
        返回 (slots, integrity)
        """
        a = self.cfg.get("allocation") or {}
        if not a.get("enabled", True):
            return None, None
        samples = self._profile_samples()
        w, meta = self._profile_store().weights_for(date, samples)
        return allocation.allocate_day(
            date, total_kwh, w, measured, upto,
            basis=meta.get("basis", ""), samples=meta.get("samples", 0),
            source=meta.get("source", ""))

    def actual_merged(self, force=False):
        """把四个来源的实测记录合并成一条时间轴（见 core/actuals.py 的合并规则）。"""
        now = time.time()
        if not force and self._merged and now - self._merged_at < 20:
            return self._merged
        gen = self._raw("generation")
        live = (gen.data or {}).get("power5", []) if gen and gen.data else []
        curves = [live]                      # 优先级：实时曲线 > 历史日归档曲线
        curves.extend(self._power5_archive())
        today = datetime.now().strftime("%Y-%m-%d")
        inv = self._raw("inverters")
        dev_today = None
        if inv and inv.ok and inv.data.get("date") == today and inv.data.get("total_kwh"):
            dev_today = (today, inv.data["total_kwh"])
        m = actuals.merge(self.daily_gen(), self.irr_daily(), curves,
                          self._archive_data(), dev_today=dev_today, today=today)
        m["stats"]["archive_dir"] = self._archive_dir()
        self._merged, self._merged_at = m, now
        return m

    # ---------- 历史 ----------
    def _merged_day_rows(self):
        """合并实测集 → 逐日对账行（升序），明细表与趋势图共用同一口径。

        返回 (rows, k, merge_stats)：
          · 覆盖「最早一期 ~ 最新一期」，并网前/停运日（发电 0）、只有辐照没有电量的
            记录**保留不剔除**，用 status 说明；
          · 缺字段用 None 补齐，不丢行；
          · 每行带 source / sources，可追溯到具体来源；
          · 电量 kWh、辐照 kWh/m²、功率 kW，字段名与其它接口完全一致。
        """
        p0 = float(self.cfg["station"]["capacity_mwp"])
        k, _ = self._model_k()
        ctx = self._om_ctx()
        m = self.actual_merged()
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        # 当天还没过完，实测电量必然低于全天预测，必须标注，否则看着像模型严重高估
        day_done = now.hour >= 19
        # 气候学补齐要用「整段」辐照序列做参照，只传当天那一条会让模型电量整段消失
        irr_map = dict(ctx["irr_map"])
        out = []
        for d in m["dates"]:
            rec = m["rows"][d]
            a, irr = rec["actual_kwh"], rec["irradiance"]
            if irr is not None:
                irr_map[d] = irr
            pair = self._day_pair(d,
                                  {d: a} if a is not None else {},
                                  irr_map, p0, ctx)
            p = pair["forecast_kwh"]
            est, basis = pair["irradiance_est"], pair["basis"]
            # 停运/并网前日（发电 0）拿不到实测辐照时，也给出模型回算，
            # 用来回答「那天本应发多少、停机损失多少」
            if p is None and a is not None:
                irr_est, b = model.future_irradiance(irr_map, d)
                if irr_est:
                    est, basis = irr_est, b
                    p = model.forecast_day(irr_est, p0, ctx["k_global"])
                    if p is not None:
                        p = round(p, 1)
            st = rec["status"]
            if st == "ok":
                st = pair["status"]                     # ok / irr_estimated / irr_missing
                if p is not None and irr is None:
                    st = "irr_estimated"                # 模型值来自气候学推算
            # 全站统一口径：dev = (实测−模型)/模型（正=超发红），准确率同基数
            dev = ((a - p) / p * 100) if (a and p and p > 0) else None
            gap = dev
            out.append({
                "date": d,
                "irradiance": irr,
                "irradiance_raw": rec["irradiance_raw"],
                "irradiance_est": est,
                "basis": basis,
                "actual_kwh": a,
                "forecast_kwh": p,
                "dev_pct": dev,
                "gap_pct": gap,
                "accuracy_pct": model.accuracy(a, p),
                "peak_kw": rec["peak_kw"],
                "measured_hours": rec["measured_hours"],
                "measured_from": (rec["measured_from"] or "")[:16] or None,
                "measured_until": (rec["measured_until"] or "")[:16] or None,
                "samples": rec["samples"],
                "source": rec["source"],
                "sources": rec["sources"],
                "status": st,
                "is_today": d == today,
                "partial": (d == today and not day_done),
            })
        return out, k, m["stats"]

    def daily_rows(self, limit=0):
        """历史实际数据：合并全部实测来源后的逐日明细，覆盖「最早一期 ~ 最新一期」。

        limit<=0 表示全部。口径与 /api/trend 完全一致（共用 _merged_day_rows）。
        """
        out, k, stats = self._merged_day_rows()
        rows = out[::-1]                                 # 最新在上
        if limit and limit > 0:
            rows = rows[:limit]
        c = {"days": len(out),
             "with_actual": sum(1 for r in out if r["status"] in ("ok", "irr_estimated", "irr_missing")),
             "no_generation": sum(1 for r in out if r["status"] == "no_generation"),
             "irr_only": sum(1 for r in out if r["status"] == "irr_only"),
             "no_actual": sum(1 for r in out if r["status"] == "no_actual")}
        return {"rows": rows, "k": k,
                "total_days": len(out),
                "first_day": out[0]["date"] if out else None,
                "last_day": out[-1]["date"] if out else None,
                "counts": c,
                "merge": stats,
                "excluded_no_data": stats["dropped"],    # 兼容旧字段：恒为 0
                "pass_line_pct": float(self.cfg["model"].get("pass_line_pct", 85))}

    def inverters(self):
        inv = self._raw("inverters")
        if not inv or not inv.ok:
            return {"rows": [], "as_of": None, "status": inv.status if inv else "unavailable",
                    "error": inv.error if inv else "未配置"}
        d = inv.data
        rows = sorted(d["rows"], key=lambda r: -(r["kwh"] or 0))
        return {"date": d.get("date"), "rows": rows, "total_kwh": d.get("total_kwh"),
                "total_peak_kw": d.get("total_peak_kw"), "count": len(rows),
                "as_of": inv.as_of.isoformat(timespec="seconds") if inv.as_of else None,
                "status": "ok"}

    # ---------- 日辐射度（当日逐时 / 当月逐日两个维度） ----------
    def radiation(self, month=None):
        """日辐射度数据：当日逐时辐照（W/m²）+ 当月逐日辐照（kWh/m²）。

        当月逐日来源优先级：气象仪实测 > Open-Meteo 实况 > 预报值（当天起，标注预报）；
        超出预报范围的未来日不给假值（None）。返回结构供总览页「当日/当月」切换。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        month = month or today[:7]
        ctx = self._om_ctx()
        ground = ctx.get("ground") or {}
        irr_map = ctx.get("irr_map") or {}
        fc_irr = self._forecast_irr()

        # 当日逐时（W/m²）：气象仪实测逐时优先（手动导入库），仪器空窗用预报逐时替补。
        # 逐时值是「该小时平均 W/m²」，所以 sum÷1000 就是当日累计 kWh/m²。
        shape = self._forecast_hourly().get(today) or []
        meas = weather_import.ground_hourly_of(self._weather_import_store(), today)
        today_hours = []
        for h in range(24):
            if h in meas:
                w, src = meas[h], "实测"
            elif shape and h < len(shape) and shape[h] is not None:
                w, src = shape[h], "预报"
            else:
                w, src = None, None
            today_hours.append({"t": "%02d:00" % h, "w": (round(w, 0) if w is not None else None),
                                "src": src})
        today_sum = (round(sum(x["w"] for x in today_hours if x["w"]) / 1000.0, 2)
                     if any(x["w"] is not None for x in today_hours) else None)
        # 来源标签必须与「实际覆盖了几小时」一致：气象仪只覆盖 2 小时、其余 22 小时是预报，
        # 却整日标「气象仪实测」，等于把"实测 2h + 预报 22h 的混合和"谎报成实测值。
        # 分档规则：24h 全实测 / 实测+预报混合（写清各几小时）/ 纯实测 / 纯预报 / 无。
        n_meas_h = sum(1 for x in today_hours if x["src"] == "实测")
        n_fc_h = sum(1 for x in today_hours if x["src"] == "预报")
        if n_meas_h >= 24:
            today_src = "气象仪实测"
        elif n_meas_h and n_fc_h:
            today_src = "实测 %dh + 预报 %dh" % (n_meas_h, n_fc_h)
        elif n_meas_h:
            today_src = "气象仪实测（%dh）" % n_meas_h
        elif n_fc_h:
            today_src = "预报"
        else:
            today_src = None

        # 当月逐日（kWh/m²）：实测（气象仪/OM）优先，未到日期用预报，预报外留空
        try:
            y, m = int(month[:4]), int(month[5:7])
            ndays = (datetime(y + (m == 12), (m % 12) + 1, 1) - datetime(y, m, 1)).days
        except Exception:
            return {"error": "月份格式应为 YYYY-MM"}
        days = []
        actual_vals = []
        for i in range(1, ndays + 1):
            d = "%04d-%02d-%02d" % (y, m, i)
            is_future = d > today
            v, src, est = None, None, False
            if not is_future and ground.get(d):
                v, src = ground[d], "气象仪"
            elif not is_future and irr_map.get(d):
                v, src = irr_map[d], "Open-Meteo 实况"
            elif fc_irr.get(d):
                v, src, est = fc_irr[d], "预报值", True
            if v is not None and not est:
                actual_vals.append(v)
            days.append({"date": d, "value": (round(v, 2) if v is not None else None),
                         "source": src, "is_forecast": est, "is_today": d == today})
        return {
            "month": month,
            "unit_day": "W/m²", "unit_month": "kWh/m²",
            "today": today,
            "today_hours": today_hours,
            "today_sum": today_sum,
            "today_sum_src": today_src,
            # 「当日逐时是否完全无实测」——混合日不算纯预报，也不该冒充纯实测
            "today_is_forecast": (n_meas_h == 0),
            "today_is_mixed": bool(n_meas_h and n_fc_h),
            "today_measured_hours": len(meas),
            "month_days": days,
            "month_total": round(sum(actual_vals), 2) if actual_vals else None,
            "month_avg": round(sum(actual_vals) / len(actual_vals), 2) if actual_vals else None,
            "actual_days": len(actual_vals),
            "sources_count": {
                "气象仪": sum(1 for x in days if x["source"] == "气象仪"),
                "Open-Meteo 实况": sum(1 for x in days if x["source"] == "Open-Meteo 实况"),
                "预报值": sum(1 for x in days if x["is_forecast"] and x["value"] is not None),
            },
        }

    # ---------- 日前预测快照（真实预测能力的唯一证据） ----------
    def _forecast_log_path(self):
        return os.path.join(ROOT, "data", "forecast_log.json")

    def _load_forecast_log(self):
        try:
            with open(self._forecast_log_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def record_forecast_snapshot(self):
        """把「今天做出的预测」按天存档一次。

        历史日画出来的模型值是用实测辐照回算的（posterior），拿它算准确率
        等于用答案算答案。要回答「模型到底准不准」，必须有真正的先验预测存档。
        每天只写第一次——那一版才是当日对外承诺的基线，事后不再覆盖。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        if getattr(self, "_snap_done", None) == today:
            return None
        log = self._load_forecast_log()
        if today in log:
            self._snap_done = today
            return log[today]
        p0 = float(self.cfg["station"]["capacity_mwp"])
        ctx = self._om_ctx()
        days = {}
        try:
            for row in self.forecast_days(FORECAST_HORIZON_DAYS)[0]:
                # forecast_days 的行用 kwh 字段（不是 forecast_kwh）
                v = row.get("forecast_kwh") or row.get("kwh")
                if v:
                    days[row["date"]] = round(float(v), 1)
        except Exception:
            return None
        if not days:
            return None
        rec = {"created": datetime.now().isoformat(timespec="seconds"),
               "k_global": round(ctx.get("k_global") or 0, 4),
               "days": days}
        log[today] = rec
        self._snap_done = today
        try:
            os.makedirs(os.path.dirname(self._forecast_log_path()), exist_ok=True)
            with open(self._forecast_log_path(), "w", encoding="utf-8") as f:
                json.dump(log, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        return rec

    def forecast_log(self, limit=30):
        """真实日前预测准确率：快照预测 vs 事后实测。

        与「回算」严格区分——只有这组数字能对外说「模型准不准」。
        同一天可能被多份快照预测过，考核取「最近一次预测」，避免重复计权。
        """
        log = self._load_forecast_log()
        daily = self.daily_gen()
        today = datetime.now().strftime("%Y-%m-%d")
        best = {}
        for made in sorted(log):
            rec = log.get(made) or {}
            try:
                created_h = int((rec.get("created") or "")[11:13])
            except Exception:
                created_h = 99
            for d, pred in (rec.get("days") or {}).items():
                if d < made:
                    continue             # 马后炮：预测的是已经发生的日子，无意义
                if d >= today:
                    # 当天还没结束，daily 里的实测是「截至当前」而非全天值，
                    # 拿来对账必然显示巨亏——必须等日子过完
                    continue
                if d == made and created_h > 9:
                    # 当天中午之后才存档的是「滚动修正」，不是日前承诺，不计入考核
                    continue
                act = daily.get(d)
                if not act or not pred:
                    continue
                dev = (act - pred) / pred * 100
                try:
                    lead = (datetime.strptime(d, "%Y-%m-%d")
                            - datetime.strptime(made, "%Y-%m-%d")).days
                except Exception:
                    lead = None
                r = {"made": made, "date": d, "pred_kwh": round(float(pred), 1),
                     "actual_kwh": round(act, 1), "dev_pct": round(dev, 1), "lead_days": lead}
                if d not in best or (r["made"] or "") > (best[d]["made"] or ""):
                    best[d] = r
        rows = [best[k] for k in sorted(best)][-limit:]
        devs = sorted(abs(r["dev_pct"]) for r in rows if r["dev_pct"] is not None)
        return {
            "snapshots": len(log),
            "first_made": min(log) if log else None,
            "last_made": max(log) if log else None,
            "days": len(rows),
            "rows": rows,
            "abs_dev_mean": round(sum(devs) / len(devs), 1) if devs else None,
            "abs_dev_max": round(devs[-1], 1) if devs else None,
            "within_10pct": sum(1 for v in devs if v <= 10.0),
            "pass_line_pct": float(self.cfg["model"].get("pass_line_pct", 85)),
        }

    # ---------- 气象仪数据手动导入（xls/xlsx → data/weather_station.json） ----------
    def reset_weather_cache(self):
        """导入写盘后清派生缓存：地面 K、分档、实测合并都要重算。"""
        self._om_ctx_cache = None
        self._merged = None
        self._wstore = None
        self._wstore_sig = None

    def weather_import(self, files, commit=False):
        """解析上传文件。commit=False 只出预览（新增/覆盖/异常），True 才落盘。"""
        parsed, infos, errors = weather_import.parse_uploads(files)
        prev = weather_import.preview(parsed, infos, errors)
        if not commit:
            return dict(prev, committed=False)
        if not parsed:
            return dict(prev, committed=False, error="没有解析出任何有效日期，未入库")
        saved = weather_import.commit(parsed, infos)
        self.reset_weather_cache()
        try:                       # 立刻让辐照链路用上新数据，不等 60s 缓存过期
            self.refresh_one(["irradiance"], wait=True)
        except Exception:
            pass
        return dict(prev, committed=True, store=saved)

    def weather_station(self):
        """导入库概况 + 当前生效的地面标定（页面用它说明"数据到哪天、什么时候导的"）。"""
        st = self._weather_import_store()
        s = weather_import.summary(st)
        ctx = self._om_ctx()
        info = ctx.get("info") or {}
        s["k_ground"] = info.get("k_ground")
        s["ground_samples"] = info.get("ground_samples")
        s["ground_days_used"] = info.get("ground_days")
        s["today_measured_hours"] = len(weather_import.ground_hourly_of(
            st, datetime.now().strftime("%Y-%m-%d")))
        s["today_hourly"] = bool(s["today_measured_hours"])   # 兼容旧字段
        s["path"] = weather_import.STORE_PATH
        return s

    # ---------- 数据质量校验（关联性 + 气象仪质量） ----------
    def quality(self):
        """数据校验报告：预测/实测/辐照关联性 + 今日逐时一致性 + 气象仪质量。

        全部为可复算的统计量（Pearson r、偏差分布），不写死任何数值。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        rows, _, _ = self._merged_day_rows()
        # 统计口径：只取「运营日」（有实测发电、状态 ok）近 30 天。
        # 未并网日（actual=0、模型仍会给 8000+ kWh）与并网首日（不足 24h）必须剔除，
        # 否则它们把方差吃光，r 会被算成负数——那是口径错，不是模型错。
        first_op = next((r["date"] for r in rows if (r["actual_kwh"] or 0) > 0), None)
        ok_days = [r for r in rows if r["date"] < today and r["status"] == "ok" and (r["actual_kwh"] or 0) > 0]
        excluded = [r["date"] for r in rows if r["date"] < today
                    and (r["status"] == "no_generation" or r["date"] == first_op)]
        hist = [r for r in ok_days if r["date"] != first_op][-30:]
        ctx = self._om_ctx()
        ground = ctx.get("ground") or {}
        om = ctx.get("om") or {}
        a = [r["actual_kwh"] for r in hist]
        p = [r["forecast_kwh"] for r in hist]
        # 「模型实际使用的辐照」= 气象仪优先、缺失才用 OM——相关性必须按这个口径算，
        # 否则拿 OM 原始值算会把气象仪的贡献算丢（10-18 这类 OM 垃圾日会拉低 r）
        irr = [ground.get(r["date"]) or r["irradiance"] for r in hist]
        devs = [abs((r["actual_kwh"] - r["forecast_kwh"]) / r["forecast_kwh"] * 100)
                for r in hist if r["forecast_kwh"] and r["forecast_kwh"] > 0]
        devs.sort()
        n_dev = len(devs)

        # 今日逐时段一致性（有实测的时段内）：实测功率 vs 模型基线功率
        id_pts, today_r = [], None
        try:
            dd = self.day_detail(today)
            id_pts = [h for h in (dd.get("hours") or [])
                      if h.get("actual_kw") is not None and h.get("forecast_kw") is not None]
        except Exception:
            dd = {}
        today_r = model.pearson([h["actual_kw"] for h in id_pts],
                                [h["forecast_kw"] for h in id_pts])

        # 气象仪质量：覆盖天数、断更、与 OM 重叠日的偏差与相关
        gdays = sorted(ground)
        overlap = [(d, ground[d], om[d]) for d in sorted(set(ground) & set(om))
                   if ground[d] and om[d] and om[d] > 0.8]
        ratios = sorted((g / o, d, g, o) for d, g, o in overlap)
        med = ratios[len(ratios) // 2] if ratios else None
        worst = sorted(overlap, key=lambda t: -abs(t[1] / t[2] - 1))[:3] if overlap else []
        sanity_bad = [(d, v) for d, v in sorted(ground.items()) if v < 0.2 or v > 8.5]
        by_date = {r["date"]: r for r in rows}
        gv = [(ground[d], (by_date.get(d) or {}).get("actual_kwh")) for d in gdays]
        gv = [(g, e) for g, e in gv if e]
        ws = {
            "days": len(gdays),
            "first_day": gdays[0] if gdays else None,
            "last_day": gdays[-1] if gdays else None,
            "missing_since": (None if not gdays or gdays[-1] >= today
                              else (datetime.now() - datetime.strptime(gdays[-1], "%Y-%m-%d")).days),
            "overlap_days": len(overlap),
            "overlap_r": model.pearson([g for _, g, _ in overlap], [o for _, _, o in overlap]),
            "r_ground_actual": model.pearson([g for g, _ in gv], [e for _, e in gv]),
            "ground_actual_days": len(gv),
            "ground_vs_om_median": (round(med[0], 3) if med else None),
            "ground_max": (round(max(ground.values()), 2) if ground else None),
            "sanity_bad": [{"date": d, "value": v} for d, v in sanity_bad],
            "worst_days": [{"date": d, "ground": round(g, 2), "om": round(o, 2),
                            "dev_pct": round((o - g) / g * 100, 1)} for d, g, o in worst],
            # 导入库来源（手动上传）：说清这批数据是"哪来的、什么时候导的"，
            # 断更判断也以导入库的最后一天为准
            "imported": {
                "days": len(weather_import.ground_days_of(self._weather_import_store())),
                "last_day": (ctx.get("info") or {}).get("imported_last"),
                "imported_at": (ctx.get("info") or {}).get("imported_at"),
                "k_ground": (ctx.get("info") or {}).get("k_ground"),
            },
        }
        return {
            "daily": {
                "days": len(hist),
                "window": (hist[0]["date"] + " ~ " + hist[-1]["date"]) if hist else None,
                "excluded_days": len(excluded),
                "excluded_reason": "未并网日（实测 0）与并网首日（不足 24h）不计入统计",
                "r_actual_forecast": model.pearson(a, p),
                "r_actual_irradiance": model.pearson(a, irr),
                "r_forecast_irradiance": model.pearson(p, irr),
                "abs_dev_mean": (round(sum(devs) / n_dev, 1) if n_dev else None),
                "abs_dev_median": (round(devs[n_dev // 2], 1) if n_dev else None),
                "abs_dev_max": (round(devs[-1], 1) if n_dev else None),
                "days_within_10pct": sum(1 for v in devs if v <= 10.0),
                "pass_line_pct": float(self.cfg["model"].get("pass_line_pct", 85)),
            },
            "intraday_today": {
                "slots": len(id_pts),
                "r_actual_forecast": today_r,
                "note": "今日 15 分钟时段平均功率：实测 vs 模型基线（仅统计已采样时段）",
            },
            "weather_station": ws,
            # 真实日前预测准确率：与「回算」严格区分，这组才是能对外说的数字
            "forecast_log": self.forecast_log(limit=45),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }


    def site(self):
        return {"station": self.cfg["station"],
                "model": self.cfg["model"], "poll": self.cfg["poll"],
                "finance": self.cfg.get("finance", {})}
