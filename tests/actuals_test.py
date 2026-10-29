# -*- coding: utf-8 -*-
"""历史实际数据合并层单测（core/actuals.py）—— 纯离线，不打接口。

覆盖四条需求：
  1) 多源按时间对齐，字段名/单位统一（kWh / kWh/m² / kW）
  2) 按时间戳去重（日记录按 date、5 分钟曲线按 ts）
  3) 覆盖「最早一期 ~ 最新一期」，区间没有被截断
  4) 缺字段不丢记录：空值补齐，占位原值保留在 *_raw
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import actuals  # noqa: E402

BAD = []


def ok(cond, msg, extra=""):
    if not cond:
        BAD.append(msg)
    print(("[PASS] " if cond else "[FAIL] ") + msg + (" | " + str(extra) if extra else ""))


def main():
    api_daily = {"2026-10-01": 8000.0, "2026-10-02": 0.0, "2026-10-03": None}
    api_irr = {"2026-10-01": 4.2, "2026-10-02": 0.001}
    # 10:05 两条曲线都有 → 必须去重，且保留优先级高的第一条（120）
    curves = [[["2026-10-01 10:00", 100.0], ["2026-10-01 10:05", 120.0]],
              [["2026-10-01 10:05", 999.0], ["2026-10-03 11:00", 60.0]]]
    archive = {"gen": {"2026-10-01": 9000.0, "2026-10-05": 5000.0},
               "irr": {"2026-10-01": 9.9, "2026-10-04": 3.3, "2026-10-06": 0.001},
               "peak": {"2026-10-05": 333.3},
               "power5": [["2026-10-05 12:00", 70.0]],
               "files": ["gen_daily.csv"]}

    m = actuals.merge(api_daily, api_irr, curves, archive, dev_today=("2026-10-02", 77.0))
    r = m["rows"]

    ok(sorted(r) == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"],
       "区间覆盖最早一期 ~ 最新一期（一条不丢）", sorted(r))
    ok(m["dates"][0] == "2026-10-01" and m["dates"][-1] == "2026-10-06", "dates 升序且首尾正确", m["dates"])

    # 1) 对齐 + 单位
    ok(r["2026-10-01"]["actual_kwh"] == 8000.0, "同一日多来源时电量取 API（kWh）", r["2026-10-01"]["actual_kwh"])
    ok(r["2026-10-01"]["irradiance"] == 4.2, "辐照取接口/气象仪（kWh/m²），不被归档 9.9 覆盖",
       r["2026-10-01"]["irradiance"])
    ok(set(r["2026-10-01"]["sources"]) == {"api", "curve", "irr", "archive"}, "来源全部记入 sources 可溯源",
       r["2026-10-01"]["sources"])

    # 2) 去重
    ok(r["2026-10-01"]["peak_kw"] == 120.0, "5 分钟曲线同时间戳去重（保留首条 120，不取 999）",
       r["2026-10-01"]["peak_kw"])
    ok(m["stats"]["curve_duplicates"] == 1, "曲线重复点数被统计", m["stats"]["curve_duplicates"])
    ok(len(r) == len(set(r)), "日记录按 date 唯一，同一期不重复出现", len(r))

    # 3) 缺字段补齐，不丢记录
    ok(r["2026-10-02"]["actual_kwh"] == 0.0 and r["2026-10-02"]["status"] == "no_generation",
       "发电 0（停运/并网前）保留记录并标状态", r["2026-10-02"]["status"])
    ok(r["2026-10-02"]["irradiance"] is None and r["2026-10-02"]["irradiance_raw"] == 0.001,
       "辐照 0.001 占位：指标置空、原值留在 irradiance_raw")
    ok(r["2026-10-03"]["actual_kwh"] is None and r["2026-10-03"]["peak_kw"] == 60.0,
       "只有曲线没有日电量时仍出记录（空值补齐）", r["2026-10-03"]["status"])
    ok(r["2026-10-04"]["status"] == "irr_only", "只有辐照没有电量 → 标 irr_only 而非丢弃",
       r["2026-10-04"]["status"])
    ok(r["2026-10-05"]["actual_kwh"] == 5000.0 and r["2026-10-05"]["source"] == "archive",
       "API 完全缺该日时用归档 MWh→kWh 补位", r["2026-10-05"]["actual_kwh"])
    ok(r["2026-10-06"]["irradiance"] is None and r["2026-10-06"]["irradiance_raw"] == 0.001,
       "归档辐照占位同样只留原值", r["2026-10-06"]["irradiance_raw"])
    ok(m["stats"]["dropped"] == 0, "丢记录数恒为 0", m["stats"]["dropped"])

    # 归档读取：文件缺失不报错
    empty = actuals.load_archive(None)
    ok(empty["gen"] == {} and empty["files"] == [], "归档目录未配置时安全降级")

    print()
    if BAD:
        print("[FAIL] %d 项未通过" % len(BAD))
        sys.exit(1)
    print("[PASS] 历史实际数据合并层 15 项全部通过")


if __name__ == "__main__":
    main()
