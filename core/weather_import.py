# -*- coding: utf-8 -*-
"""气象仪导出文件解析与入库（手动导入）。

数据来源：http://gl.tubanginfo.com/lvguang/public/admin/dashboard 后台按导出 xls
（一个月一个文件，如 2026-10.xls）。实测文件结构（2026-10.xls 共 7086 行）：

    0 站号(0444) | 1 报文时间(YYYY-MM-DD HH:MM:SS) | 2 环境温度℃ | 3 组件温度℃
    4 相对湿度% | 5 风速 m/s | 6 风向 ° | 7 瞬时总辐射 W/m² | 8 总辐射日累计 MJ/m²

行序是**按时间倒序**（最新在最上），采样间隔 5 分钟（秒位固定在 :51），
所以任何"取前 N 行"的写法都会拿到最新而非最早的记录。

口径（与既有 IrradianceExport 完全一致，不另立一套）：
  日辐照 kWh/m²   = 当日 max(第 8 列) / 3.6
                     —— 日累计列是仪器自己积分的，比逐点积分可靠：
                        2026-10-22/24/25 逐点积分比日累计低约 5%（首末采样缺失）
  逐时辐照 W/m²   = 该小时内第 7 列的算术平均；无样本的小时给 None（不补 0，
                     否则等于凭空造出"夜里没发电"之外的第二类假数据）

质量校验（只提示、不篡改数据）：
  rows<200            当日采样不足（理论 288 条 = 24h×12）
  night_nonzero       00:00–05:00 出现非零辐照 → 传感器零点漂移或时间错位
  integ_dev>15%       日累计与逐点积分偏差过大 → 导出被截断或重复
  dup_ts              同一时间戳重复出现
"""
import json
import os
from datetime import datetime

try:
    import xlrd
except ImportError:      # 缺依赖时给出明确原因，不返回假数据
    xlrd = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # site/
STORE_PATH = os.path.join(ROOT, "data", "weather_station.json")
MJ_PER_KWH = 3.6          # 1 kWh/m² = 3.6 MJ/m²
SAMPLE_MIN = 5            # 气象仪采样间隔（分钟）
EXPECTED_ROWS = 24 * 60 // SAMPLE_MIN      # 288

# 表头关键字 → 字段。命中即用，全部落空则回退到底部 POS_FALLBACK 的固定列序
HEADER_MAP = [
    ("dt", ("报文时间", "时间", "time", "date")),
    ("cum", ("日累计", "累计值", "cumulative")),
    ("ghi", ("总辐射", "辐射", "radiation", "ghi")),
    ("temp", ("环境温度", "temperature")),
    ("mod_temp", ("组件温度", "module")),
    ("hum", ("相对湿度", "湿度", "humidity")),
    ("wind", ("风速", "wind")),
    ("wdir", ("风向", "direction")),
]
POS_FALLBACK = {"dt": 1, "temp": 2, "mod_temp": 3, "hum": 4, "wind": 5,
                "wdir": 6, "ghi": 7, "cum": 8}


def _f(v):
    """任意单元格值 → float；空/非数 → None（绝不把 '' 当 0）。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ts(v):
    """单元格 → 'YYYY-MM-DD HH:MM:SS'；支持字符串与 Excel 日期序列/日期对象。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    s = str(v).strip()
    if len(s) >= 16 and s[4] == "-" and s[7] == "-":
        return s[:19] if len(s) >= 19 else s[:16]
    return None


def detect_columns(header):
    """按表头识别列。返回 {字段: 列号}，识别不到的字段按 POS_FALLBACK 补位。"""
    cols, notes = {}, []
    for idx, cell in enumerate(header or []):
        t = str(cell or "").lower()
        if not t:
            continue
        for field, keys in HEADER_MAP:
            if field in cols:
                continue
            if any(k.lower() in t for k in keys):
                cols[field] = idx
                break
    for field, idx in POS_FALLBACK.items():
        if field not in cols:
            cols[field] = idx
            notes.append("列「%s」未在表头命中，按固定列位 %d 取值" % (field, idx))
    return cols, notes


def _rows_from_xls(buf):
    if xlrd is None:
        raise RuntimeError("缺少 xlrd 依赖：pip install xlrd")
    wb = xlrd.open_workbook(file_contents=buf)
    sh = wb.sheet_by_index(0)
    return [[sh.cell_value(r, c) for c in range(sh.ncols)] for r in range(sh.nrows)]


def _rows_from_xlsx(buf):
    if openpyxl is None:
        raise RuntimeError("缺少 openpyxl 依赖：pip install openpyxl")
    import io
    wb = openpyxl.load_workbook(io.BytesIO(buf), data_only=True, read_only=True)
    sh = wb[wb.sheetnames[0]]
    return [list(r) for r in sh.iter_rows(values_only=True)]


def parse_file(buf, filename=""):
    """解析单个文件 → (days, info)。days: {date: {kwh_m2, ghi_hourly, rows, ...}}"""
    name = (filename or "").lower()
    if name.endswith(".xlsx") or name.endswith(".xlsm"):
        rows = _rows_from_xlsx(buf)
    else:
        rows = _rows_from_xls(buf)
    if not rows:
        raise RuntimeError("文件无内容")
    cols, notes = detect_columns(rows[0])

    samples = {}                 # date -> [(ts, ghi, cum)]
    dup = 0
    seen = set()
    for r in rows[1:]:
        if not r:
            continue
        ts = _ts(r[cols["dt"]] if cols["dt"] < len(r) else None)
        if not ts:
            continue
        key = ts[:16]
        if key in seen:
            dup += 1
        seen.add(key)
        samples.setdefault(ts[:10], []).append(
            (ts, _f(r[cols["ghi"]]) if cols["ghi"] < len(r) else None,
             _f(r[cols["cum"]]) if cols["cum"] < len(r) else None))

    days = {}
    for d, ss in samples.items():
        ss.sort(key=lambda x: x[0])                      # 源文件是倒序，必须显式排序
        cums = [c for _, _, c in ss if c is not None]
        gh = [(t, g) for t, g, _ in ss if g is not None]
        kwh = (round(max(cums) / MJ_PER_KWH, 3) if cums else None)
        integ = round(sum(g for _, g in gh) * SAMPLE_MIN / 60.0 / 1000.0, 3) if gh else None
        # 逐时：只给有样本的小时，保持 None（不插 0）
        buckets = {}
        for t, g in gh:
            h = int(t[11:13])
            buckets.setdefault(h, []).append(g)
        hourly = {str(h): round(sum(v) / len(v), 1) for h, v in sorted(buckets.items())}
        flags = []
        if len(ss) < 200:
            flags.append("rows_lt_200")
        night = [g for t, g in gh if int(t[11:13]) < 5 and g > 20]
        if night:
            flags.append("night_nonzero")
        if kwh and integ and kwh > 0:
            dev = abs(integ - kwh) / kwh
            if dev > 0.15:
                flags.append("integ_dev_%.0f%%" % (dev * 100))
        days[d] = {"kwh_m2": kwh, "ghi_hourly": hourly, "rows": len(ss),
                   "integ_kwh_m2": integ, "flags": flags,
                   "day_rows_expected": EXPECTED_ROWS}

    info = {"name": filename, "rows": max(0, len(rows) - 1), "days": len(days),
            "first": (min(days) if days else None), "last": (max(days) if days else None),
            "dup_ts": dup, "columns": cols, "notes": notes}
    return days, info


def parse_uploads(files):
    """files: [{"name": str, "data_b64": str}] → (days, infos, errors)"""
    import base64
    days, infos, errors = {}, [], []
    mod_temp_zero = []
    for item in files or []:
        name = item.get("name") or "未命名"
        try:
            buf = base64.b64decode(item.get("data_b64") or "")
        except Exception as e:
            errors.append({"file": name, "error": "base64 解码失败：%s" % e})
            continue
        if not buf:
            errors.append({"file": name, "error": "文件内容为空"})
            continue
        try:
            d, info = parse_file(buf, name)
        except Exception as e:
            errors.append({"file": name, "error": "解析失败：%s" % e})
            continue
        info["mod_temp_all_zero"] = _mod_temp_zero(buf, name)
        if info.get("mod_temp_all_zero"):
            mod_temp_zero.append(name)
        # 同一日期出现在多个文件里时，后导入的覆盖（你补导出时通常是新文件更全）
        for dd, rec in d.items():
            rec["file"] = name
            days[dd] = rec
        infos.append(info)
    return days, infos, errors


def _mod_temp_zero(buf, name):
    try:
        rows = (_rows_from_xlsx(buf) if (name or "").lower().endswith(("xlsx", "xlsm"))
                else _rows_from_xls(buf))
    except Exception:
        return None
    if not rows:
        return None
    cols, _ = detect_columns(rows[0])
    i = cols.get("mod_temp")
    if i is None:
        return None
    seen = False
    for r in rows[1:]:
        if i < len(r):
            v = _f(r[i])
            if v is not None:
                seen = True
                if v != 0:
                    return False
    return True if seen else None


# ---------- 入库（data/weather_station.json） ----------

def load_store():
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("days"), dict):
            return d
    except Exception:
        pass
    return {"version": 1, "days": {}, "files": {}, "updated_at": None}


def save_store(store):
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    store["updated_at"] = datetime.now().isoformat(timespec="seconds")
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE_PATH)          # 原子替换：写到一半断电也不会留下坏档
    return store


def preview(days, infos, errors):
    """解析结果 × 现有库 → 给人看的预览（新增/覆盖/异常），不写盘。"""
    store = load_store()
    have = store.get("days") or {}
    new, over = [], []
    for d in sorted(days):
        (over if d in have else new).append(d)
    flagged = {d: rec["flags"] for d, rec in days.items() if rec.get("flags")}
    return {
        "files": infos,
        "errors": errors,
        "days_parsed": len(days),
        "first": (min(days) if days else None),
        "last": (max(days) if days else None),
        "new_days": len(new), "new_list": new[:40],
        "overwrite_days": len(over), "overwrite_list": over[:40],
        "flagged": flagged,
        "rows_total": sum(i.get("rows") or 0 for i in infos),
        "existing": {"days": len(have),
                     "first": (min(have) if have else None),
                     "last": (max(have) if have else None),
                     "updated_at": store.get("updated_at")},
        "sample": [{"date": d, "kwh_m2": days[d]["kwh_m2"], "rows": days[d]["rows"],
                    "flags": days[d]["flags"]} for d in sorted(days)][-10:],
    }


def commit(days, infos):
    """把解析结果并入库（同日覆盖），返回入库后的概况。"""
    store = load_store()
    now = datetime.now().isoformat(timespec="seconds")
    for d, rec in days.items():
        store["days"][d] = {"kwh_m2": rec["kwh_m2"], "ghi_hourly": rec["ghi_hourly"],
                            "rows": rec["rows"], "flags": rec["flags"],
                            "file": rec.get("file"), "imported_at": now}
    for info in infos or []:
        name = info.get("name")
        if not name:
            continue
        prev = store["files"].get(name) or {}
        store["files"][name] = {"imported_at": now, "days": info.get("days"),
                                 "rows": info.get("rows"), "first": info.get("first"),
                                 "last": info.get("last"),
                                 "imports": int(prev.get("imports") or 0) + 1}
    save_store(store)
    return summary(store)


def summary(store=None):
    st = store or load_store()
    days = st.get("days") or {}
    files = st.get("files") or {}
    return {
        "days": len(days),
        "first": (min(days) if days else None),
        "last": (max(days) if days else None),
        "updated_at": st.get("updated_at"),
        "files": [dict(v, name=k) for k, v in sorted(files.items(),
                                                      key=lambda kv: (kv[1] or {}).get("imported_at") or "")],
        "flagged_days": sum(1 for v in days.values() if v.get("flags")),
        "monthly": _monthly(days),
    }


def _monthly(days):
    """按月汇总：有效天数、月累计辐照（kWh/m²）——直接和页面「当月」视图对得上。

    分母只数「有日辐照值（kwh_m2>0）」的有效日。导入当天常常只有半天报文
    （rows_lt_200、日累计还是 0），把它算进天数会把月日均拉低，
    与 /api/radiation 的 month_avg（只数有效日）打架。原始天数另给 raw_days。
    """
    out = {}
    for d, rec in days.items():
        m = d[:7]
        e = out.setdefault(m, {"month": m, "days": 0, "raw_days": 0, "total": 0.0})
        e["raw_days"] += 1
        if rec.get("kwh_m2"):
            e["days"] += 1
            e["total"] += rec["kwh_m2"]
    for e in out.values():
        e["total"] = round(e["total"], 2)
        e["avg"] = round(e["total"] / e["days"], 2) if e["days"] else None
    return [out[m] for m in sorted(out)]


def ground_days_of(store):
    """{date: kWh/m²}：把已入库的日辐照摊平，供 irradiance 链路当「地面真值」。"""
    return {d: v["kwh_m2"] for d, v in ((store or {}).get("days") or {}).items()
            if v.get("kwh_m2")}


def ground_hourly_of(store, date):
    """某日逐时实测辐照 {hour:int → W/m²}；没有该日返回 {}（绝不补 0）。"""
    rec = ((store or {}).get("days") or {}).get(date) or {}
    out = {}
    for k, v in (rec.get("ghi_hourly") or {}).items():
        if v is None:
            continue
        try:
            out[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def ground_days():
    """{date: kWh/m²}，供 irradiance 链路当"地面真值"用。"""
    return ground_days_of(load_store())


def ground_hourly(date):
    """某日逐时实测辐照 {hour:int → W/m²}；没有该日返回 {}。"""
    return ground_hourly_of(load_store(), date)
