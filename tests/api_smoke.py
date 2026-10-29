# -*- coding: utf-8 -*-
"""接口冒烟测试：启动服务 → 逐个打接口 → 断言字段与状态。"""
import json
import subprocess
import sys
import time
import urllib.request

ROOT = __file__.rsplit("\\", 2)[0] if "\\" in __file__ else __file__.rsplit("/", 2)[0]
PORT = 8899


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    proc = subprocess.Popen([sys.executable, "server.py", "--port", str(PORT), "--no-browser"],
                            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ok = []
    try:
        for _ in range(40):
            try:
                get("/api/status")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise SystemExit("服务未启动")

        def check(name, cond, extra=""):
            ok.append(cond)
            print(("[PASS] " if cond else "[FAIL] ") + name + (" | " + str(extra) if extra else ""))
            if not cond:
                print(json.dumps(last, ensure_ascii=False)[:800])

        last = get("/api/status")
        check("status 含 4 个数据源", set(last["sources"]) >= {"generation", "irradiance", "inverters", "forecast"},
              list(last["sources"]))
        check("generation 有状态", last["sources"]["generation"]["status"] in
              ("ok", "empty", "error", "unavailable"), last["sources"]["generation"]["status"])

        last = get("/api/overview")
        check("overview 含 KPI", "kpi" in last and "station" in last)
        # 口径：数据源更新到今天才有"今日实发"；否则必须回退到"最新数据日"且带日期，
        # 绝不能把昨天的数据标成今天（跨天正确性）
        k = last["kpi"]
        if last["is_current"]:
            check("今日实发为真实值", k["actual_today_kwh"] is not None, k["actual_today_kwh"])
        else:
            check("跨天：今日实发为空、回退到最新数据日",
                  k["actual_today_kwh"] is None and k["latest_day_kwh"] is not None
                  and last["data_day"] != last["today"],
                  {"data_day": last["data_day"], "today": last["today"],
                   "latest_day_kwh": k["latest_day_kwh"]})
        check("模型 K 已标定", last["model"]["k"], last["model"])
        # 清晨/上午数据源只更新到当天半天时，latest 即今日半日值（量级小属正常）
        check("电量单位为 kWh（完整日 > 1000，或当日进行中）",
              (k.get("latest_day_kwh") or 0) > 1000 or last["is_current"], k.get("latest_day_kwh"))
        check("含未来 30 天预测电量（不含收益换算）",
              (k.get("forecast_30d_kwh") or 0) > 0 and "forecast_30d_revenue" not in k,
              k.get("forecast_30d_kwh"))
        print("   KPI:", {k: v for k, v in last["kpi"].items()})

        # 预测序列：三档全部指向未来
        last = get("/api/series?range=short")
        check("短期 = 当日逐小时（24 点）", len(last["points"]) == 24 and last["granularity"] == "hour",
              len(last["points"]))
        check("短期 X 轴从 00:00 起", last["start"].endswith("00:00") and last["points"][0]["t"] == "00:00",
              (last["start"], last["points"][0]["t"]))
        check("短期每点含电量且不含收益", all("kwh" in p and "revenue" not in p for p in last["points"]))

        # 当日对照：实测 vs 模型基线（预测页短期档）
        last = get("/api/intraday")
        hrs = last.get("hours") or []
        check("当日对照含 24 小时", len(hrs) == 24, len(hrs))
        check("当日对照小时从 00:00 到 23:00",
              len(hrs) == 24 and hrs[0]["t"] == "00:00" and hrs[23]["t"] == "23:00",
              (hrs[0]["t"], hrs[23]["t"]) if hrs else None)
        check("当日对照含实测与基线两条序列",
              any(h["actual_kw"] is not None for h in hrs) and any((h["forecast_kw"] or 0) > 0 for h in hrs))
        check("当日对照偏差按同时段计算",
              last.get("gap_pct") is not None and last.get("forecast_measured_sum_kwh") is not None,
              (last.get("gap_pct"), last.get("forecast_measured_sum_kwh")))
        check("当日对照标注实测截止时刻", bool(last.get("measured_until")), last.get("measured_until"))

        # 时段分摊：守恒 + 不铺未来 + 来源可溯
        al = last.get("allocation") or {}
        # 清晨实时功率曲线积分可能领先华为日累计量 → 设计内 conflict 降级（不倒填实测，
        # 如实标 sum_equal=False + reason）。只有非 conflict 时才要求严格守恒。
        # 另外「当前小时未走完」时段（no_weight/no_target + 正残差）：累计量里属于
        # 当前小时的电量无处安放（设计上不铺未走完时段），同属如实降级。
        in_conflict = ((al.get("mode") == "conflict" and (al.get("residual_kwh") or 0) < 0)
                       or (al.get("mode") in ("no_weight", "no_target")
                           and (al.get("residual_kwh") or 0) > 0))
        check("分摊块存在且含守恒校验（或如实降级 conflict）",
              al.get("sum_equal") is True or in_conflict,
              (al.get("sum_kwh"), al.get("daily_total_kwh"), al.get("residual_kwh"), al.get("mode")))
        check("分摊：各时段之和严格等于当日累计量（conflict 时注明原因）",
              al.get("sum_kwh") == al.get("daily_total_kwh") or in_conflict,
              "Σ=%s vs %s mode=%s" % (al.get("sum_kwh"), al.get("daily_total_kwh"), al.get("mode")))
        nh = last.get("now_hour") or 24
        check("分摊：未来 / 未走完时段不生成估算值",
              all(h.get("value_kwh") is None for h in hrs
                  if h["hour"] >= nh and h["actual_kwh"] is None),
              nh)
        check("分摊：估算只落在已结束时段（index < now_hour）",
              all(h["hour"] < nh for h in hrs if h["source"] == "allocated"),
              nh)
        check("分摊：实测时段保留原值且标为 measured",
              all(h["source"] == "measured" and h["value_kwh"] == h["actual_kwh"]
                  for h in hrs if h["actual_kwh"] is not None))
        check("分摊：估算时段标记 estimated",
              all(h.get("estimated") is True for h in hrs
                  if h["source"] == "allocated"))
        check("分摊：权重来源可追溯",
              al.get("weight_source") in ("observed", "blended", "fallback") and bool(al.get("basis")),
              (al.get("weight_source"), al.get("weight_samples")))
        check("分摊：时段分类不重不漏",
              (al.get("measured_slots", 0) + al.get("allocated_slots", 0)
               + al.get("pending_slots", 0)) == 24,
              (al.get("measured_slots"), al.get("allocated_slots"), al.get("pending_slots")))

        # 权重画像接口
        pr = get("/api/hour-profile")
        check("权重画像含工作日/周末两组",
              "weekday" in (pr.get("groups") or {}) and "weekend" in pr["groups"],
              sorted((pr.get("groups") or {}).keys()))
        check("权重画像每组权重和为 1",
              all(abs(sum(v["weights"]) - 1.0) < 1e-6
                  for v in pr["groups"].values()))
        check("权重画像标注样本数与依据",
              all("samples" in v and "basis" in v for v in pr["groups"].values()))
        last = get("/api/series?range=mid")
        check("中期 = 未来 15 天", len(last["points"]) == 15 and last["scope"] == "future", len(last["points"]))
        last = get("/api/series?range=long")
        check("长期 = 未来 30 天", len(last["points"]) == 30 and last["scope"] == "future", len(last["points"]))
        check("预测序列单位统一 kWh", last["unit"] == "kWh", last["unit"])

        # 日历与单日预测
        last = get("/api/forecast/calendar")
        rng = last["range"]
        check("日历范围 = 首个有发电记录日 ~ 今天+30 天（历史 + 未来都可点）",
              rng["min_date"] <= rng["today"] and rng["max_date"] > rng["today"] and rng["horizon_days"] == 30,
              (rng["min_date"], rng["today"], rng["max_date"], len(last["days"])))
        check("日历历史日带实测、未来日为预测",
              any((d.get("actual_kwh") or 0) > 0 for d in last["days"]) and
              any(d.get("is_future") and d.get("actual_kwh") is None for d in last["days"]))
        check("日历每天都有模型/预测电量", all((d.get("forecast_kwh") or 0) > 0 for d in last["days"]))
        d0 = rng["default_date"]
        last = get("/api/forecast/day?date=" + d0)
        check("单日含 24 小时曲线与模型电量", len(last["hours"]) == 24 and (last.get("forecast_kwh") or 0) > 0,
              last.get("forecast_kwh"))
        last = get("/api/forecast/day?date=" + rng["min_date"])
        check("历史日可查：给出实测与模型回算", (last.get("actual_kwh") or 0) > 0 and (last.get("forecast_kwh") or 0) > 0,
              {"actual": last.get("actual_kwh"), "model": last.get("forecast_kwh")})
        last = get("/api/forecast/day?date=" + rng["max_date"])
        check("未来日可查：给出预测且无实测",
              (last.get("forecast_kwh") or 0) > 0 and last.get("actual_kwh") is None and last.get("is_future"),
              {"model": last.get("forecast_kwh"), "actual": last.get("actual_kwh")})
        check("单日视图不含收益字段", "revenue" not in last and "price" not in last)
        last = get("/api/forecast/day?date=2099-01-01")
        check("超出范围的日期明确报错（不给假数据）", "error" in last, last.get("error"))

        last = get("/api/forecast/explain")
        check("预测说明含历史样本与 K", last["history"]["days"] > 0 and last["k"]["value"] > 0)
        check("预测说明含未来天气依据与不确定性", bool(last["future_weather"]["method"]) and len(last["caveats"]) > 0)

        last = get("/api/daily?limit=60")
        check("逐日明细非空", len(last["rows"]) > 0, len(last["rows"]))
        check("逐日明细字段为 kWh 且不含收益",
              all("actual_kwh" in r and "revenue" not in r for r in last["rows"]))

        # 历史实际数据：多源合并后必须覆盖最早一期 ~ 最新一期，一条不丢
        last = get("/api/daily?limit=0")
        rows = last["rows"]
        dates = [r["date"] for r in rows]
        check("历史实际数据全量返回（limit=0 不按条数截断）",
              len(rows) == last["total_days"] and len(rows) >= 40, (len(rows), last["total_days"]))
        check("覆盖最早一期 ~ 最新一期（升序区间、最新在上）",
              last["first_day"] <= "2026-09-07" and dates == sorted(dates, reverse=True),
              (last["first_day"], last["last_day"]))
        check("按日期去重：同一期不重复出现", len(set(dates)) == len(dates), len(dates))
        check("停运/并网前日与仅辐照日保留，丢弃数为 0",
              last["merge"]["dropped"] == 0 and any(r["status"] == "no_generation" for r in rows),
              (last["merge"]["dropped"], last["counts"]))
        check("每行可溯源（source/sources）、缺字段以空值补齐",
              all(r["sources"] for r in rows)
              and all(r["source"] or r["actual_kwh"] is None for r in rows)
              and all(r["actual_kwh"] is None or r["actual_kwh"] >= 0 for r in rows),
              [r["sources"] for r in rows[:2]])

        # 逐日趋势：历史 + 未来同一条时间轴
        last = get("/api/trend?scope=all")
        check("逐日趋势覆盖历史与未来",
              any(r["is_future"] for r in last["rows"]) and any(not r["is_future"] for r in last["rows"]),
              len(last["rows"]))
        last = get("/api/trend?scope=recent&limit=30")
        check("近 30 天档只含历史日", len(last["rows"]) <= 30 and all(not r["is_future"] for r in last["rows"]),
              len(last["rows"]))
        last = get("/api/trend?scope=future")
        check("未来档 = 今天起 31 天（含今天）", len(last["rows"]) == 31, len(last["rows"]))

        # 趋势图与「历史实际数据」必须同源同口径（2026-10-28：趋势图曾只覆盖 09-20 起的 39 天）
        tr_all = get("/api/trend?scope=all")
        th = [r for r in tr_all["rows"] if not r["is_future"]]
        dy = get("/api/daily?limit=0")
        dm = {r["date"]: r for r in dy["rows"]}
        check("趋势图历史点数 = 明细表期数", len(th) == len(dm), (len(th), len(dm)))
        check("趋势图覆盖最早一期（不再从 09-20 起）",
              th and th[0]["date"] == dy["first_day"], (th[0]["date"] if th else None, dy["first_day"]))
        check("趋势图逐日实测/模型值与明细表完全一致",
              all(dm[r["date"]]["actual_kwh"] == r["actual_kwh"]
                  and dm[r["date"]]["forecast_kwh"] == r["forecast_kwh"] for r in th))
        check("停运日实测为 0（真实记录，不是缺测）",
              all(r["actual_kwh"] == 0 for r in th if r["status"] == "no_generation")
              and any(r["status"] == "no_generation" for r in th),
              sum(1 for r in th if r["status"] == "no_generation"))
        check("只有辐照的日子实测为空（趋势图断线，不画假零线）",
              all(r["actual_kwh"] is None for r in th if r["status"] == "irr_only"))
        check("趋势图 counts 按 scope 自洽",
              tr_all["counts"]["future"] == sum(1 for r in tr_all["rows"] if r["is_future"])
              and tr_all["counts"]["with_actual"] == sum(
                  1 for r in th if (r["actual_kwh"] or 0) > 0))

        last = get("/api/inverters")
        check("逆变器数据可用", last["status"] == "ok" and len(last["rows"]) == 12, last.get("count"))

        last = get("/api/site")
        check("站点档案含装机容量", last["station"]["capacity_mwp"] == 2.105, last["station"]["capacity_mwp"])

        last = get("/api/config")
        check("配置脱敏", last["sources"]["generation"]["api"]["password"] in ("", "***"))

        # 强制刷新
        req = urllib.request.Request("http://127.0.0.1:%d/api/refresh" % PORT,
                                     data=b'{"sources":["generation"]}',
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            last = json.loads(r.read().decode("utf-8"))
        check("手动刷新返回 refreshed", last.get("refreshed") == ["generation"], last.get("refreshed"))

        # 静态资源
        for p in ("/", "/assets/app.js", "/assets/charts.js", "/assets/banner.js", "/assets/style.css"):
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, p), timeout=10) as r:
                body = r.read()
            check("静态资源可访问 " + p, len(body) > 100, len(body))
    finally:
        proc.terminate()

    bad = ok.count(False)
    print("\n=== 冒烟测试：%d 通过 / %d 失败 ===" % (ok.count(True), bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
