# -*- coding: utf-8 -*-
"""本地导出数据源：解析 Downloads 里的 FusionSolar / 气象仪导出文件。

数据来源是「用户手动导出」，因此 as_of 取文件修改时间，
页面会据此显示「数据延迟 X 分钟」，提醒这不是实时源。
"""
import glob
import json
import os
import time
from datetime import datetime

from .base import Source, SourceResult

try:
    import openpyxl
    import xlrd
except ImportError:      # 缺依赖时降级为 unavailable，不写假数据
    openpyxl = xlrd = None


def newest(pattern):
    fs = [f for f in glob.glob(pattern) if os.path.isfile(f)]
    return max(fs, key=os.path.getmtime) if fs else None


class GenerationExport(Source):
    name = "generation"
    label = "FusionSolar 导出文件（日电量 / 5min 功率）"

    def fetch(self):
        if openpyxl is None:
            return SourceResult("error", error="缺少 openpyxl/xlrd 依赖：pip install openpyxl xlrd")
        t0 = time.time()
        exp = self.cfg.get("export", {})
        files = glob.glob(exp.get("daily_glob", ""))
        if not files:
            return SourceResult("unavailable", error="未找到导出文件：%s" % exp.get("daily_glob"))
        daily, power5 = {}, []
        best_daily, best_power = None, None
        try:
            for f in files:
                ws = openpyxl.load_workbook(f, data_only=True)["能量趋势"]
                rows = [r for r in ws.iter_rows(min_row=3, values_only=True) if r[0] and r[1] not in (None, "")]
                dates = {str(r[0])[:10] for r in rows}
                # 日粒度文件：不同日期最多
                if best_daily is None or len(dates) > len(best_daily[1]):
                    best_daily = (f, dates)
                # 5min 文件：行数最多
                if best_power is None or len(rows) > len(best_power[1]):
                    best_power = (f, rows)
            if best_daily:
                ws = openpyxl.load_workbook(best_daily[0], data_only=True)["能量趋势"]
                for r in ws.iter_rows(min_row=3, values_only=True):
                    if r[0] and r[1] not in (None, ""):
                        daily[str(r[0])[:10]] = round(float(r[1]), 1)      # kWh
            if best_power:
                for r in best_power[1]:
                    power5.append([str(r[0]), float(r[1])])
        except Exception as e:
            return SourceResult("error", error="解析失败：%s" % e, latency_ms=int((time.time() - t0) * 1000))
        if not daily and not power5:
            return SourceResult("empty", error="导出文件无有效数据")
        as_of = datetime.fromtimestamp(max(os.path.getmtime(best_daily[0]), os.path.getmtime(best_power[0])))
        return SourceResult("ok", data={"daily": daily, "power5": power5}, as_of=as_of,
                            latency_ms=int((time.time() - t0) * 1000),
                            meta={"files": [os.path.basename(best_daily[0]), os.path.basename(best_power[0])],
                                  "mode": "export"})


class IrradianceExport(Source):
    name = "irradiance"
    label = "气象仪导出文件（总辐射）"

    def fetch(self):
        if xlrd is None:
            return SourceResult("error", error="缺少 xlrd 依赖：pip install xlrd")
        t0 = time.time()
        pattern = self.cfg.get("export", {}).get("glob", "")
        files = sorted(glob.glob(pattern))
        if not files:
            return SourceResult("unavailable", error="未找到气象导出文件：%s" % pattern)
        raw = {}
        try:
            for f in files:
                s = xlrd.open_workbook(f).sheet_by_index(0)
                for r in range(1, s.nrows):
                    t = str(s.cell_value(r, 1)).strip()
                    if not t:
                        continue
                    day = t[:10]
                    try:
                        cum = float(s.cell_value(r, 8))
                    except Exception:
                        continue
                    if cum > raw.get(day, 0):
                        raw[day] = cum
        except Exception as e:
            return SourceResult("error", error="解析失败：%s" % e)
        if not raw:
            return SourceResult("empty", error="气象文件无有效数据")
        data = {d: round(v / 3.6, 3) for d, v in raw.items()}     # MJ/m² -> kWh/m²
        as_of = datetime.fromtimestamp(max(os.path.getmtime(f) for f in files))
        return SourceResult("ok", data=data, as_of=as_of, latency_ms=int((time.time() - t0) * 1000),
                            meta={"files": [os.path.basename(f) for f in files], "mode": "export"})


class InverterExport(Source):
    name = "inverters"
    label = "逆变器报表导出文件"

    def fetch(self):
        if openpyxl is None:
            return SourceResult("error", error="缺少 openpyxl 依赖")
        import re
        t0 = time.time()
        f = newest(self.cfg.get("export", {}).get("glob", ""))
        if not f:
            return SourceResult("unavailable", error="未找到逆变器报表：%s" % self.cfg.get("export", {}).get("glob"))
        try:
            ws = openpyxl.load_workbook(f, data_only=True).active
            rows = []
            for r in ws.iter_rows(min_row=3, values_only=True):
                if r[1] and r[3] not in (None, ""):
                    rows.append({
                        "name": str(r[1]),
                        "kwh": round(float(r[3]), 2),
                        "peak_kw": round(float(r[6]), 2) if r[6] not in (None, "") else None,
                        "hours": round(float(r[7]), 2) if r[7] not in (None, "") else None,
                    })
        except Exception as e:
            return SourceResult("error", error="解析失败：%s" % e)
        if not rows:
            return SourceResult("empty", error="报表无有效行")
        m = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(f))
        data = {"date": m.group(1) if m else None, "rows": rows,
                "total_kwh": round(sum(x["kwh"] for x in rows), 2),
                "total_peak_kw": round(sum(x["peak_kw"] or 0 for x in rows), 2)}
        return SourceResult("ok", data=data, as_of=datetime.fromtimestamp(os.path.getmtime(f)),
                            latency_ms=int((time.time() - t0) * 1000),
                            meta={"file": os.path.basename(f), "mode": "export"})
