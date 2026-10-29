# -*- coding: utf-8 -*-
"""气象仪导入解析测试：列识别 / 日累计口径 / 逐时均值 / 质量标记 / 幂等入库。

不上传真文件也能验证：用 openpyxl 在内存里造一份与图邦后台导出**同结构**的表
（9 列、报文时间倒序、5 分钟一条），跑完整链路。真文件的端到端验证见
`python -c` 里的 2026-10.xls 冒烟（tests/api_smoke.py 里另有 HTTP 层用例）。
"""
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import weather_import as wi

ok = fail = 0


def check(cond, name):
    global ok, fail
    if cond:
        ok += 1
        print("[PASS] " + name)
    else:
        fail += 1
        print("[FAIL] " + name)


HEADER = ["站号", "报文时间", "环境温度", "组件温度", "相对湿度", "风速", "风向",
          "总辐射", "总辐射日累计值"]


def make_rows(day="2026-10-20", n=288, night_bad=False, cum_scale=1.0, dup=0):
    """造一天 5 分钟报文（与实测导出同序：时间倒序）。"""
    t0 = datetime.strptime(day + " 00:00:00", "%Y-%m-%d %H:%M:%S")
    rows = [HEADER]
    out = []
    cum = 0.0
    for i in range(n):
        t = t0 + timedelta(minutes=5 * i)
        h = t.hour + t.minute / 60.0
        # 简单钟形：08:00–18:00 才有辐照，正午峰 800 W/m²
        ghi = max(0.0, 800.0 * max(0.0, 1 - ((h - 13) / 5.0) ** 2)) if 8 <= h <= 18 else 0.0
        if night_bad and 1 <= h <= 2:
            ghi = 45.0
        cum += ghi * 5 * 60 / 1e6          # W/m² × 5min → MJ/m²
        out.append(["0444", t.strftime("%Y-%m-%d %H:%M:%S"), "27.0", "0", "70.0",
                    "1.5", "180", round(ghi, 1), round(cum * cum_scale, 3)])
    rows.extend(reversed(out))              # 源文件是按时间倒序
    rows.extend(rows[1:1 + dup])            # 重复时间戳
    return rows


def make_xlsx(rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main():
    # 1) 列识别：表头命中，无需回退
    cols, notes = wi.detect_columns(HEADER)
    check(cols == {"dt": 1, "temp": 2, "mod_temp": 3, "hum": 4, "wind": 5,
                   "wdir": 6, "ghi": 7, "cum": 8}, "按表头识别 8 个字段")
    check(notes == [], "表头齐全时不做列位回退")

    # 2) 表头全空 → 固定列位回退，并说明回退
    cols2, notes2 = wi.detect_columns(["", "", "", "", "", "", "", "", ""])
    check(cols2["dt"] == 1 and cols2["cum"] == 8, "无表头时回退到固定列位")
    check(len(notes2) == 8, "回退时逐字段给说明")

    # 3) 解析：日辐照 = 日累计/3.6，逐时辐照 = 小时均值
    buf = make_xlsx(make_rows())
    days, info = wi.parse_file(buf, "2026-10.xlsx")
    check("2026-10-20" in days, "解析出 2026-10-20")
    d = days["2026-10-20"]
    check(d["rows"] == 288, "当日 288 条采样")
    check(3.0 < d["kwh_m2"] < 6.0, "日辐照落在 3~6 kWh/m² 合理区间（实测 %.2f）" % d["kwh_m2"])
    check(len(d["ghi_hourly"]) == 24, "逐时辐照按小时落 24 个桶（夜间为真实的 0）")
    lit = sorted(int(k) for k, v in d["ghi_hourly"].items() if v > 0)
    check(lit == list(range(8, 18)), "有辐照的小时为 08~17（造数用钟形曲线，18 时归零）")
    check(d["ghi_hourly"]["13"] > d["ghi_hourly"]["9"], "正午 13 时辐照高于上午 9 时")
    check(d["ghi_hourly"].get("3") == 0.0 or "3" not in d["ghi_hourly"],
          "夜间小时不造假值（0 或缺失）")
    check(info["dup_ts"] == 0, "无重复时间戳")

    # 4) 质量标记：采样不足 / 夜间非零 / 日累计与积分不符
    small = wi.parse_file(make_xlsx(make_rows(n=120)), "x.xlsx")[0]["2026-10-20"]
    check("rows_lt_200" in small["flags"], "采样不足触发 rows_lt_200")
    night = wi.parse_file(make_xlsx(make_rows(night_bad=True)), "x.xlsx")[0]["2026-10-20"]
    check("night_nonzero" in night["flags"], "夜间非零触发 night_nonzero")
    off = wi.parse_file(make_xlsx(make_rows(cum_scale=1.4)), "x.xlsx")[0]["2026-10-20"]
    check(any(f.startswith("integ_dev") for f in off["flags"]), "日累计与积分偏差触发标记")

    # 5) 重复时间戳被计数（不丢行，但要如实报出来）
    _, infod = wi.parse_file(make_xlsx(make_rows(dup=5)), "x.xlsx")
    check(infod["dup_ts"] == 5, "重复时间戳如实计数（%d 条）" % infod["dup_ts"])

    # 6) 入库：新增 → 覆盖 → 幂等
    tmp = tempfile.mkdtemp(prefix="wx_")
    old = wi.STORE_PATH
    wi.STORE_PATH = os.path.join(tmp, "weather_station.json")
    try:
        days1, info1 = wi.parse_file(buf, "2026-10.xlsx")
        prev1 = wi.preview(days1, [info1], [])
        check(prev1["new_days"] == 1 and prev1["overwrite_days"] == 0, "首次预览：新增 1 天")
        st = wi.commit(days1, [info1])
        check(st["days"] == 1 and st["first"] == "2026-10-20", "入库后 1 天")
        prev2 = wi.preview(days1, [info1], [])
        check(prev2["new_days"] == 0 and prev2["overwrite_days"] == 1, "重复导入：覆盖而非重复计数")
        wi.commit(days1, [info1])
        check(wi.summary()["days"] == 1, "重复导入后仍是 1 天（幂等）")
        check(wi.summary()["files"][0]["imports"] == 2, "同一文件导入次数累加")
        # ground 视图
        g = wi.ground_days()
        check("2026-10-20" in g and g["2026-10-20"] == round(days1["2026-10-20"]["kwh_m2"], 3),
              "ground_days 摊平出日辐照")
        h = wi.ground_hourly("2026-10-20")
        check(isinstance(list(h)[0], int), "ground_hourly 的键转成 int（供按小时索引）")
        check(wi.ground_hourly("1999-01-01") == {}, "无该日返回空 dict，不抛错")
    finally:
        wi.STORE_PATH = old

    print("\n=== 气象仪导入测试：%d 通过 / %d 失败 ===" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
