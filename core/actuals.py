# -*- coding: utf-8 -*-
"""历史实际数据（实测）统一数据集。

「已有历史数据」散在四个地方（北向 API 日电量 / 5 分钟功率曲线 / 气象仪辐照 /
旧版看板归档），字段名、单位、覆盖区间都不一样。本模块把它们按日期对齐合并成
**一条**以 date 为主键的实测时间轴，供 /api/daily（历史实际数据表）使用。

只做三件事：对齐、去重、补齐。不参与任何模型计算（模型回算仍由 store 用
model.forecast_day 算），也不改各来源自身的判定口径。

---------------- 数据来源（按字段优先级从高到低）----------------
  api      sources.generation 北向 API 日电量（含 data/api_cache/daily_month.json
           逐月回填缓存）—— 实测电量的首选，单位 kWh
  curve    sources.generation 5 分钟总功率曲线（当日实时采样累积 +
           data/api_cache/power5_<date>.json 历史日归档）—— 只提供峰值/覆盖时段，
           不反推日电量（采样只覆盖部分时段，反推会凭空造数）
  irr      气象仪日辐照（sources.irradiance，export）+ 北向 API 自带辐照 —— kWh/m²
  archive  旧版看板归档 legacy/data/*（gen_daily.csv 单位 MWh、irradiance_daily.csv、
           power_5min.json、inverters.json）—— 最低优先级，只在前面全缺时补位

---------------- 合并规则 ----------------
 1. 主键 = date（YYYY-MM-DD，本地日界）；同一天无论来自几个来源只保留**一条**记录。
 2. 字段级优先级：actual_kwh = api > archive；irradiance = 气象仪/API > archive；
    peak_kw / measured_* = curve > archive。低优先级值不覆盖高优先级，但来源会记进
    sources[]，便于追溯（不静默丢）。
 3. 去重：5 分钟曲线按时间戳（YYYY-MM-DD HH:MM）去重；日记录按 date 去重。
 4. 缺字段不丢行：值置 None（空值补齐），原值能留的都留在 *_raw 字段里。
    辐照 <= 0.005 kWh/m² 视为「气象仪空记录占位」（导出文件里写 0.001），
    指标置空但原值保留在 irradiance_raw，不当作真实辐照参与模型。
 5. 并网前/停运日（发电 0）与「仅辐照无电量」的记录一律保留，由 status 说明原因。
"""
import json
import os

MISSING_IRR = 0.005        # kWh/m²；低于此视为缺测占位（气象仪空行写 0.001）
_LIVE_SRC = "api"

# 来源中文名（返回给前端做溯源展示）
SRC_LABEL = {
    "api": "北向API日电量",
    "dev": "逆变器合计",
    "curve": "5分钟功率曲线",
    "irr": "气象仪/接口辐照",
    "archive": "旧版归档",
}


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


# ---------------------------------------------------------------- 归档读取
def load_archive(root):
    """读旧版看板归档。每个文件都可选，缺了不算错（返回空字典）。

    返回 {"gen": {date: kWh}, "irr": {date: kWh/m²}, "peak": {date: kW},
          "power5": [[ts, kw], ...], "files": [...]}
    """
    out = {"gen": {}, "irr": {}, "peak": {}, "power5": [], "files": []}
    if not root or not os.path.isdir(root):
        return out

    p = os.path.join(root, "gen_daily.csv")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8-sig") as f:
                for i, ln in enumerate(f):
                    c = [x.strip() for x in ln.split(",")]
                    if i == 0 or len(c) < 2 or not c[0][:4].isdigit():
                        continue
                    v = _f(c[1])
                    if v is not None:
                        out["gen"][c[0][:10]] = round(v * 1000, 1)      # MWh → kWh
            out["files"].append("gen_daily.csv")
        except Exception:
            pass

    p = os.path.join(root, "irradiance_daily.csv")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8-sig") as f:
                for i, ln in enumerate(f):
                    c = [x.strip() for x in ln.split(",")]
                    if i == 0 or len(c) < 2 or not c[0][:4].isdigit():
                        continue
                    v = _f(c[1])
                    if v is not None:
                        out["irr"][c[0][:10]] = round(v, 3)
            out["files"].append("irradiance_daily.csv")
        except Exception:
            pass

    p = os.path.join(root, "power_5min.json")
    if os.path.isfile(p):
        try:
            pts = json.load(open(p, encoding="utf-8"))
            if isinstance(pts, list):
                out["power5"].extend(pts)
                out["files"].append("power_5min.json")
        except Exception:
            pass

    # 逆变器报表只有「分机峰值」，相加≠电站同时峰值，仅作最低优先级补位
    p = os.path.join(root, "inverters.json")
    if os.path.isfile(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            day, rows = d.get("date"), d.get("rows") or []
            pk = sum((_f(r.get("peak_kw")) or 0) for r in rows if isinstance(r, dict))
            if day and pk > 0:
                out["peak"][day] = round(pk, 1)
                out["files"].append("inverters.json")
        except Exception:
            pass

    return out


# ---------------------------------------------------------------- 曲线聚合
def curve_daily(curves):
    """把多条 5 分钟功率曲线按时间戳去重后按日聚合。

    curves 是若干条 [[ "YYYY-MM-DD HH:MM", kw ], ...]，优先级按传入顺序。
    返回 ({date: {peak_kw, measured_hours, measured_from, measured_until,
                  samples, covered_kwh}}, 重复点数)
    """
    seen = set()
    pts = []
    dup = 0
    for c in curves or []:
        for p in c or []:
            if not isinstance(p, (list, tuple)) or len(p) < 2:
                continue
            ts = str(p[0])[:16]
            kw = _f(p[1])
            if len(ts) < 16 or kw is None:
                continue
            if ts in seen:                    # 同一时间戳只留第一条（优先级高者）
                dup += 1
                continue
            seen.add(ts)
            pts.append((ts, kw))
    pts.sort()

    days = {}
    for ts, kw in pts:
        d = ts[:10]
        r = days.setdefault(d, {"vals": [], "peak": None, "hours": set(), "frm": ts, "to": ts})
        r["vals"].append(kw)
        r["peak"] = kw if r["peak"] is None else max(r["peak"], kw)
        r["hours"].add(ts[11:13])
        if ts < r["frm"]:
            r["frm"] = ts
        if ts > r["to"]:
            r["to"] = ts
    out = {}
    for d, r in days.items():
        out[d] = {
            "peak_kw": round(r["peak"], 1),
            "measured_hours": len(r["hours"]),
            "measured_from": r["frm"],
            "measured_until": r["to"],
            "samples": len(r["vals"]),
            # 仅「采样覆盖时段」内的电量，不等于日电量，只用于覆盖度说明
            "covered_kwh": round(sum(r["vals"]) * 5 / 60, 1),
        }
    return out, dup


# ---------------------------------------------------------------- 合并
def _blank(d):
    return {
        "date": d,
        "actual_kwh": None,          # 实测日发电量 kWh
        "irradiance": None,          # 实测日水平面总辐照 kWh/m²
        "irradiance_raw": None,      # 辐照原始值（缺测占位也留着）
        "peak_kw": None,             # 实测峰值功率 kW（仅曲线覆盖日）
        "measured_hours": 0,         # 实测逐小时覆盖小时数
        "measured_from": None,       # 实测曲线起点 YYYY-MM-DD HH:MM
        "measured_until": None,      # 实测曲线终点
        "samples": 0,                # 5 分钟采样点数
        "source": None,              # 电量主来源
        "sources": [],               # 参与合并的全部来源（去重依据）
        "status": None,              # ok / no_generation / no_actual / irr_only
    }


def merge(api_daily, api_irr, curves, archive, dev_today=None, today=None):
    """把各来源的实测记录合并成一条以 date 为主键的时间轴。

    api_daily  {date: kWh}          北向 API 日电量
    api_irr    {date: kWh/m²}       接口/气象仪日辐照
    curves     [[[ts, kw], ...]]    5 分钟功率曲线（可多条，按优先级排列）
    archive    load_archive() 的结果
    dev_today  (date, kWh)          分逆变器当日合计，只在 API 缺当天时补位
    """
    rows = {}
    pairs = 0                    # 参与合并的「日期×来源」条数，用于算去重条数
    src_days = {}

    def touch(d, src):
        nonlocal pairs
        r = rows.get(d)
        if r is None:
            r = rows[d] = _blank(d)
        if src not in r["sources"]:
            r["sources"].append(src)
            pairs += 1
            src_days.setdefault(src, set()).add(d)
        return r

    # 1) 北向 API 日电量（最高优先级）
    for d, v in (api_daily or {}).items():
        v = _f(v)
        if v is None:
            continue
        r = touch(str(d)[:10], _LIVE_SRC)
        if r["actual_kwh"] is None:
            r["actual_kwh"] = round(v, 1)
            r["source"] = _LIVE_SRC

    # 2) 分逆变器当日合计（只在 API 完全没有该日时补位，不做覆盖）
    if dev_today:
        d, v = dev_today
        v = _f(v)
        if d and v is not None:
            r = touch(str(d)[:10], "dev")
            if r["actual_kwh"] is None:
                r["actual_kwh"] = round(v, 1)
                r["source"] = "dev"

    # 3) 5 分钟功率曲线 → 峰值 / 覆盖时段（不反推日电量）
    by_day, curve_dup = curve_daily(list(curves or []) + [archive.get("power5") or []])
    for d, c in by_day.items():
        r = touch(d, "curve")
        r["peak_kw"] = c["peak_kw"]
        r["measured_hours"] = c["measured_hours"]
        r["measured_from"] = c["measured_from"]
        r["measured_until"] = c["measured_until"]
        r["samples"] = c["samples"]

    # 4) 辐照：接口/气象仪优先，归档兜底；缺测占位只保留原值
    def put_irr(d, v, src):
        raw = _f(v)
        if raw is None:
            return
        r = touch(str(d)[:10], src)
        if r["irradiance_raw"] is None:
            r["irradiance_raw"] = round(raw, 3)
        if raw <= MISSING_IRR:
            return                              # 占位值：不参与模型，但原值已留
        if r["irradiance"] is None:
            r["irradiance"] = round(raw, 3)

    for d, v in (api_irr or {}).items():
        put_irr(d, v, "irr")
    for d, v in (archive.get("irr") or {}).items():
        put_irr(d, v, "archive")

    # 5) 归档日电量（最低优先级：只补 API 完全没有的日期）
    for d, v in (archive.get("gen") or {}).items():
        v = _f(v)
        if v is None:
            continue
        r = touch(d, "archive")
        if r["actual_kwh"] is None:
            r["actual_kwh"] = round(v, 1)
            r["source"] = "archive"

    # 6) 归档峰值（只在没有曲线覆盖时补位）
    for d, v in (archive.get("peak") or {}).items():
        r = touch(d, "archive")
        if r["peak_kw"] is None:
            r["peak_kw"] = round(_f(v) or 0, 1) or None

    # 7) 状态：一行都不删，用状态说明为什么没有实测
    for r in rows.values():
        a = r["actual_kwh"]
        if a is None:
            r["status"] = "irr_only" if r["irradiance"] is not None else "no_actual"
        elif a <= 0:
            r["status"] = "no_generation"          # 并网前 / 停运：有功电记录但为 0
        else:
            r["status"] = "ok"                     # 辐照缺失与否由 store 再细分

    dates = sorted(rows)
    stats = {
        "ingested": pairs,                          # 日期×来源 条数
        "dates": len(dates),
        "duplicates": max(0, pairs - len(dates)),   # 合并去重掉的重复期数
        "curve_duplicates": curve_dup,              # 5 分钟曲线同时间戳去重数
        "dropped": 0,                               # 丢记录数：恒为 0（本模块不删行）
        "sources": [{"key": k, "label": SRC_LABEL.get(k, k),
                     "days": len(v), "first": min(v), "last": max(v)}
                    for k, v in sorted(src_days.items())],
        "archive_files": list((archive or {}).get("files") or []),
    }
    return {"dates": dates, "rows": rows, "stats": stats}
