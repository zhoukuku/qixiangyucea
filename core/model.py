# -*- coding: utf-8 -*-
"""预测模型：E(kWh) = GHI(kWh/m²) × P0(MWp) × K × 1000

K 为综合效率系数（含组件效率、温升、逆变、线损、污渍、朝向等），
默认用最近 N 天的「实测辐照 → 实测电量」配对样本自动标定，
没有样本时回落到 0.95（行业经验值）并在接口里标记为经验值。

未来预测（未接入气象预报源时）采用「历史同期气候学 + 真实形态回映」：
  1. 目标月份有历史辐照样本 → 月典型值 = 该月历史日均；日起伏取该月真实日序列按日序回映
     （只保留 DAMP 比例的天气波动，避免把某一天的极端天气当成预测值）
  2. 目标月份没有历史样本 → 最近 N 天日均辐照 × 月季节因子（深圳约 22.6°N）
全部输入都来自已入库的历史记录，不引入外部臆造数值。
"""
import math
from datetime import datetime, timedelta

# 深圳（约 22.6°N）各月日照时段（自 00:00 起的分钟数）：用于日内曲线的时间范围
_SUN = {1: (400, 1035), 2: (390, 1050), 3: (370, 1060), 4: (350, 1072), 5: (335, 1082), 6: (330, 1090),
        7: (338, 1086), 8: (352, 1072), 9: (366, 1056), 10: (382, 1040), 11: (396, 1026), 12: (406, 1016)}
# 各月总辐射相对全年月均的季节因子（同纬度实测统计的经验比例）
_SEASON = {1: 0.72, 2: 0.62, 3: 0.70, 4: 0.84, 5: 1.00, 6: 1.04, 7: 1.22, 8: 1.14,
           9: 1.10, 10: 1.06, 11: 0.90, 12: 0.76}
_DAMP = 0.6          # 天气起伏保留比例（1.0 = 完全复刻历史某一天的天气）
_STEP_MIN = 15       # 日内曲线采样步长（分钟）


def pearson(xs, ys):
    """皮尔逊相关系数 r（成对剔除 None/非数），样本 <2 或分母为 0 时返回 None。"""
    pairs = [(float(x), float(y)) for x, y in zip(xs or [], ys or [])
             if x is not None and y is not None]
    n = len(pairs)
    if n < 2:
        return None
    mx = sum(x for x, _ in pairs) / n
    my = sum(y for _, y in pairs) / n
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = math.sqrt(sum((x - mx) ** 2 for x, _ in pairs))
    syy = math.sqrt(sum((y - my) ** 2 for _, y in pairs))
    if sxx <= 0 or syy <= 0:
        return None
    return round(sxy / (sxx * syy), 4)


def calibrate(daily_gen, daily_irr, p0, days=30, default=0.95):
    """用最近 days 天的配对样本标定 K = ΣE / Σ(GHI × P0)。"""
    pairs = [(d, daily_irr[d], daily_gen[d]) for d in sorted(daily_gen)
             if d in daily_irr and daily_irr[d] > 0.5 and daily_gen[d] > 0]
    pairs = pairs[-days:]
    num = sum(e for _, _, e in pairs)
    den = sum(g * p0 * 1000 for _, g, _ in pairs)      # 电量已改为 kWh，分母同步 ×1000
    if den <= 0 or not pairs:
        return None, {"k": default, "source": "default", "samples": 0}
    return num / den, {"k": num / den, "source": "calibrated", "samples": len(pairs)}


def forecast_day(irr_kwh_m2, p0, k):
    """单日预测电量 kWh。"""
    if irr_kwh_m2 is None or k is None:
        return None
    return irr_kwh_m2 * p0 * k * 1000


def accuracy(actual, pred):
    """单日准确率 %：1 - |实测-预测| / 预测（与偏差同基数，模型相对口径）。

    偏差 dev = (实测−预测)/预测，准确率 = max(0, 100 − |dev|)。
    两者同基数才能对得上：dev=−40% 时准确率必然是 60%，
    旧版分母用实测会算出 66.7%，两张卡片数字互相矛盾。
    """
    if actual is None or pred is None or pred <= 0:
        return None
    return max(0.0, 100 - abs(actual - pred) / pred * 100)


def bell_curve(sr_min, ss_min, step_min, daily_kwh):
    """把「日电量 kWh」摊成一条日内功率曲线（kW）。

    这是展示用的形态函数：包络取 sin^1.5，积分等于当日预测电量。
    真实日内形状应由功率预测服务给出，此处仅用于把日预测画成曲线。
    """
    if ss_min <= sr_min or daily_kwh is None:
        return []
    n = int((ss_min - sr_min) / step_min) + 1
    raw = [max(0.0, math.sin(math.pi * (i * step_min) / (ss_min - sr_min))) ** 1.5 for i in range(n)]
    integ = sum(raw) * step_min / 60                    # kW·h 口径下的积分
    if integ <= 0:
        return []
    scale = daily_kwh / integ
    return [(sr_min + i * step_min, raw[i] * scale) for i in range(n)]


def daylight(month):
    """该月的日出 / 日落时刻（分钟）。"""
    return _SUN.get(int(month), (375, 1065))


def om_bias_ratio(om, ground, days=7, min_samples=3):
    """（保留兼容）Open-Meteo 模型辐照 vs 地面实测的单一标量偏差校正系数。

    ⚠ 已被 calibrate_regimes() 取代：单一标量只能修一种天气的系统性偏差
    （晴天 +12~19% 与阴雨天可达 +70% 完全不是一个量级），阴雨天会漏修。
    """
    pairs = []
    for d in sorted(set(om or {}) & set(ground or {})):
        g, o = ground[d], om[d]
        if g and o and o > 0.5 and g > 0.5:      # 排除 0 占位与极低样本
            pairs.append((d, g / o))             # 校正系数 = 地面实测 / 模型值（<1 = 模型高估）
    pairs = pairs[-days:]
    if len(pairs) < min_samples:
        return 1.0, {"ratio": 1.0, "samples": len(pairs), "applied": False,
                     "detail": pairs, "reason": "重叠样本不足 %d 天" % min_samples}
    vals = sorted(r for _, r in pairs)
    ratio = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals)//2 - 1] + vals[len(vals)//2]) / 2
    ratio = min(max(ratio, 0.6), 1.1)            # 安全夹取：校正不越过合理区间
    return ratio, {"ratio": round(ratio, 4), "samples": len(pairs), "applied": True,
                   "detail": [(d, round(r, 3)) for d, r in pairs]}


# ---------- 天气分档标定（预测模型核心，2026-10-29 重构） ----------
REGIMES = ("clear", "partly", "overcast")
REGIME_LABEL = {"clear": "晴", "partly": "多云", "overcast": "阴雨"}


def regime_key(cloud_pct):
    """白天平均云量(%) → 天气分档。云量预报比辐照预报无偏得多，用它分档最稳。"""
    if cloud_pct is None:
        return None
    if cloud_pct < 35:
        return "clear"
    if cloud_pct <= 70:
        return "partly"
    return "overcast"


def calibrate_regimes(daily_gen, om_daily, clouds, p0, days=45, min_samples=2):
    """按天气分档标定效率系数：K_g = median(E / (OM辐照 × P0))，每档取中位数。

    原理：OM 辐照的系统性偏差随天气分档变化（晴天 +12~19%、阴雨更高），
    把「辐照偏差 × 全链路效率」按档吸收进各自的 K_g，阴雨天不再漏修；
    样本 = 实测电量 × OM 模型辐照配对，完全不依赖地面气象仪（ xls 断更不再影响标定）。

    返回 (ks 或 None, info)；缺档保守回落：overcast→partly→clear→全局中位
    （只往下借档，绝不把阴雨档借成晴天档）。
    """
    pairs = []
    for d in sorted(set(daily_gen or {}) & set(om_daily or {})):
        e, o = (daily_gen or {}).get(d), (om_daily or {}).get(d)
        if e and o and e > 50 and o > 0.8:       # 排除半日值与极低样本
            pairs.append((d, e / (o * p0 * 1000.0), regime_key((clouds or {}).get(d))))
    pairs = pairs[-days:]
    if not pairs:
        return None, {"source": "no_samples", "samples": 0, "detail": {}}
    all_k = sorted(kk for _, kk, _ in pairs)
    glob = all_k[len(all_k) // 2]
    buckets = {}
    for d, kk, g in pairs:
        if g:
            buckets.setdefault(g, []).append(kk)
    ks, detail = {}, {}
    for g in REGIMES:
        vals = sorted(buckets.get(g, []))
        if len(vals) >= min_samples:
            ks[g] = vals[len(vals) // 2]
        detail[g] = {"n": len(vals), "k": (round(ks[g], 4) if g in ks else None),
                     "label": REGIME_LABEL[g]}
    ks.setdefault("clear", glob)
    ks.setdefault("partly", ks.get("clear", glob))
    ks.setdefault("overcast", ks.get("partly", glob))
    ks = {g: round(ks[g], 4) for g in REGIMES}
    return ks, {"source": "calibrated_regimes", "samples": len(pairs),
                "k_global": round(glob, 4), "k": ks, "detail": detail,
                "pairs": [(d, round(kk, 3), g) for d, kk, g in pairs]}


def k_for(ks, cloud_pct, default=0.85):
    """按当天云量选档取 K；无分档表或云量未知时回落 partly/默认。"""
    if not ks:
        return default
    g = regime_key(cloud_pct)
    if g and g in ks:
        return ks[g]
    return ks.get("partly") or default


def hourly_profile(date_str, daily_kwh, shape=None):
    """某一天逐小时的功率(kW)与电量(kWh)：1 小时内的 kWh 数值等于该小时平均 kW。

    shape: 可选的 24 小时相对权重（如气象预报自身的逐时辐照分布）。
    给定且总和 > 0 时按 shape 归一化摊到 daily_kwh（保留预报的云量时间信息），
    否则回落到钟形包络近似。
    """
    if daily_kwh is None:
        return []
    out = []
    if shape and len(shape) == 24 and sum(v or 0 for v in shape) > 0:
        total = float(sum(v or 0 for v in shape))
        out = [{"hour": h, "kw": round(float(shape[h] or 0) / total * daily_kwh, 1),
                "kwh": round(float(shape[h] or 0) / total * daily_kwh, 1)} for h in range(24)]
        return out
    sr, ss = daylight(date_str[5:7])
    buckets = {}
    for minute, kw in bell_curve(sr, ss, _STEP_MIN, daily_kwh):
        buckets.setdefault((minute // 60) % 24, []).append(kw)
    for h in range(24):
        vals = buckets.get(h)
        kw = (sum(vals) / len(vals)) if vals else 0.0
        out.append({"hour": h, "kw": round(kw, 1), "kwh": round(kw, 1)})
    return out


def hourly_series(daily_pred, start_dt, hours=24):
    """未来 hours 小时的逐小时序列（跨天自动切换日预测）。daily_pred: {date: kWh}"""
    cache, out = {}, []
    for i in range(hours):
        t = start_dt + timedelta(hours=i)
        d = t.strftime("%Y-%m-%d")
        if d not in cache:
            cache[d] = hourly_profile(d, daily_pred.get(d))
        prof = cache[d]
        h = prof[t.hour] if prof else {"hour": t.hour, "kw": 0.0, "kwh": 0.0}
        out.append({"t": t.strftime("%H:00"), "date": d, "hour": t.hour,
                    "kwh": h["kwh"], "kw": h["kw"]})
    return out


def month_typical(irr_daily):
    """按月汇总历史日辐照 → {月: 日均 kWh/m²}。"""
    agg = {}
    for d, v in (irr_daily or {}).items():
        if not v or v <= 0:
            continue
        agg.setdefault(d[:7], []).append(v)
    return {m: round(sum(vs) / len(vs), 3) for m, vs in sorted(agg.items())}


def future_irradiance(irr_daily, date_str, recent_days=30):
    """估计目标日的水平面总辐照（kWh/m²），返回 (值, 依据说明)。"""
    hist = {d: v for d, v in (irr_daily or {}).items() if v and v > 0}
    if not hist:
        return None, "无历史辐照样本，无法推算"
    m = int(date_str[5:7])
    same = [v for d, v in sorted(hist.items()) if int(d[5:7]) == m]
    if same:
        typ = sum(same) / len(same)
        sample = same[(int(date_str[8:10]) - 1) % len(same)]
        v = typ + (sample - typ) * _DAMP
        return round(max(v, 0.2), 3), \
            "同月历史 %d 天 · 月典型 %.2f kWh/m² · 叠加 %.0f%% 天气波动" % (len(same), typ, _DAMP * 100)
    recent = sorted(hist)[-recent_days:]
    vals = [hist[d] for d in recent]
    base = sum(vals) / len(vals)
    fr = sum(_SEASON[int(d[5:7])] for d in recent) / len(recent)
    ratio = _SEASON[m] / fr
    # 同样回映真实天气起伏（否则整月会是一条直线）
    sample = vals[(int(date_str[8:10]) - 1) % len(vals)]
    v = (base + (sample - base) * _DAMP) * ratio
    return round(max(v, 0.2), 3), \
        "%d 月无历史样本 · 近 %d 天日均 %.2f kWh/m² × 季节因子 %.2f · 叠加 %.0f%% 天气波动" % (
            m, len(recent), base, ratio, _DAMP * 100)


def future_daily_pred(irr_daily, p0, k, start_date, days):
    """从 start_date 起 days 天的未来日预测：[{date, irradiance, kwh, basis}]"""
    out = []
    d0 = datetime.strptime(start_date, "%Y-%m-%d")
    for i in range(days):
        ds = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        irr, basis = future_irradiance(irr_daily, ds)
        kwh = forecast_day(irr, p0, k)
        out.append({"date": ds, "irradiance": irr, "kwh": (round(kwh, 1) if kwh else None),
                    "basis": basis})
    return out
