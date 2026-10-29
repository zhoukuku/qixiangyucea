# -*- coding: utf-8 -*-
"""时段分摊层单测（core/allocation.py）—— 纯离线，不打接口。

覆盖六条要求：
  1) 分摊后各时段之和**严格等于**当日累计量（整数瓦时 + 最大余数法，零残差）
  2) 权重可按工作日 / 周末区分，并支持动态更新
  3) 当日未走完时只对已结束时段生成估算值，未来时段不铺
  4) 结果标明来源（measured / allocated / none）
  5) 接口缺数据、历史样本不足时给出明确降级策略与默认值
  6) 跨天不串桶、补采优先、数据修正自动重算
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import allocation as A  # noqa: E402

BAD = []


def ok(cond, msg, extra=""):
    if not cond:
        BAD.append(msg)
    print(("[PASS] " if cond else "[FAIL] ") + msg + (" | " + str(extra) if extra else ""))


def mk_curve(day, peak, hours=range(24), step=5):
    """造一条 5 分钟曲线：日出 06:00 ~ 日落 18:00 的钟形。"""
    import math
    pts = []
    for h in hours:
        for m in range(0, 60, step):
            x = (h * 60 + m - 360) / 720.0
            v = peak * max(0.0, math.sin(math.pi * x)) if 0 <= x <= 1 else 0.0
            pts.append(["%s %02d:%02d" % (day, h, m), round(v, 2)])
    return pts


def main():
    print("=== 时段分摊层 ===")

    # ---- 1) 分组：工作日 / 周末 / 按周几 ----
    ok(A.group_of("2026-10-26") == "weekday", "周一识别为 weekday")
    ok(A.group_of("2026-10-31") == "weekend", "周六识别为 weekend")
    ok(A.group_of("2026-11-01") == "weekend", "周日识别为 weekend")
    ok(A.group_of("2026-10-28", "per_weekday") == "wed", "按周几分组成 wed")
    ok(A.group_of("bad-date") == "weekday", "非法日期回落到 weekday")

    # ---- 2) 桶聚合：同小时归并 + 跨天不串 ----
    b = A.bucketize([["2026-10-28 16:00", 100.0], ["2026-10-28 16:05", 200.0],
                     ["2026-10-29 16:10", 999.0]])
    ok(set(b.keys()) == {16}, "同一小时的点归并到一个桶", sorted(b.keys()))
    # 传 day 时必须按日过滤，否则次日 16:10 的 999 会串到当天
    b1d = A.bucketize([["2026-10-28 16:00", 100.0], ["2026-10-28 16:05", 200.0],
                       ["2026-10-29 16:10", 999.0]], day="2026-10-28")
    ok(abs(b1d[16]["kwh"] - 150.0) < 1e-9 and b1d[16]["samples"] == 2,
       "指定 day 时跨天点被排除（不串日）", b1d[16])

    b2 = A.bucketize(mk_curve("2026-10-27", 1386.0))
    ok(b2[11]["kwh"] > b2[8]["kwh"] > 0, "正午权重高于上午", (b2[8]["kwh"], b2[11]["kwh"]))
    ok(b2[0]["kwh"] == 0 and b2[23]["kwh"] == 0, "夜间桶为 0")

    # 默认形态必须是一条平滑的钟形，不能是台阶
    # （早先用 daily_kwh=1 生成，每小时功率只有 0.1~0.2，被 round 抹平过）
    fw = A.fallback_weights("2026-10-28")
    ok(all(fw[i] < fw[i + 1] for i in range(8, 11)), "默认形态上午段单调递增",
       [round(fw[i], 4) for i in range(8, 12)])
    ok(all(fw[i] > fw[i + 1] for i in range(12, 17)), "默认形态下午段单调递减",
       [round(fw[i], 4) for i in range(12, 17)])
    ok(len(set(round(fw[i], 4) for i in range(13, 16))) == 3, "默认形态不再出现同值台阶",
       [round(fw[i], 4) for i in range(13, 16)])

    # ---- 3) 权重学习：归一化 + 样本不足时的收缩 ----
    prof = A.learn({d: mk_curve(d, 1400.0) for d in
                    ("2026-10-26", "2026-10-27", "2026-10-23")},
                   today="2026-10-28")
    gw = prof["groups"]["weekday"]
    ok(abs(sum(gw["weights"]) - 1.0) < 1e-9, "权重归一化：和为 1", round(sum(gw["weights"]), 9))
    ok(gw["samples"] == 3 and gw["source"] == A.OBSERVED, "样本充足 → 纯观测", gw["source"])
    pk_h = max(range(24), key=lambda i: gw["weights"][i])
    ok(10 <= pk_h <= 13, "峰值出现在正午前后", pk_h)

    prof1 = A.learn({"2026-10-27": mk_curve("2026-10-27", 1400.0)}, today="2026-10-28")
    g1 = prof1["groups"]["weekday"]
    ok(g1["samples"] == 1 and g1["source"] == A.BLENDED, "样本不足 → 向默认形态收缩", g1["source"])

    prof0 = A.learn({}, today="2026-10-28")
    g0 = prof0["groups"]["weekday"]
    ok(g0["samples"] == 0 and g0["source"] == A.FALLBACK, "无样本 → 纯默认形态", g0["source"])
    ok(abs(sum(g0["weights"]) - 1.0) < 1e-9, "默认形态同样归一化", round(sum(g0["weights"]), 9))

    # 近期权重高于远期：近=上午峰、远=下午峰，混合后峰位应偏向近期（上午）
    def skew(day, peak_h):
        pts = []
        for h in range(24):
            for m in range(0, 60, 5):
                v = 1000 * max(0.0, 1 - abs(h - peak_h) / 5.0)
                pts.append(["%s %02d:%02d" % (day, h, m), round(v, 2)])
        return pts

    near = A.learn({"2026-10-27": skew("2026-10-27", 9)}, today="2026-10-28")
    far = A.learn({"2026-01-27": skew("2026-01-27", 15)}, today="2026-10-28")
    mix = A.learn({"2026-10-27": skew("2026-10-27", 9),
                   "2026-01-27": skew("2026-01-27", 15)}, today="2026-10-28")
    pk = lambda w: max(range(24), key=lambda i: w[i])
    ok(pk(mix["groups"]["weekday"]["weights"]) <= 11,
       "近期样本权重更大（混合峰位偏向近期）",
       (pk(near["groups"]["weekday"]["weights"]),
        pk(mix["groups"]["weekday"]["weights"]),
        pk(far["groups"]["weekday"]["weights"])))
    # 半衰期为 0 → 等权。两个样本形状关于 12 点对称，纯观测下 9 点与 15 点权重应相等
    # （min_samples=0 关掉向先验收缩，否则先验本身不对称会掩盖结论）
    flat = A.learn({"2026-10-27": skew("2026-10-27", 9),
                    "2026-10-26": skew("2026-10-26", 15)},
                   half_life_days=0, min_samples=0, today="2026-10-28")
    wf = flat["groups"]["weekday"]["weights"]
    ok(abs(wf[9] - wf[15]) < 1e-9, "half_life_days=0 → 样本等权", (wf[9], wf[15]))
    # 半衰期很长（近似等权）时衰减几乎不起作用；很短时应完全偏向近期样本
    decay = A.learn({"2026-10-27": skew("2026-10-27", 9),
                     "2026-06-01": skew("2026-06-01", 15)},
                    half_life_days=1, min_samples=0, today="2026-10-28")
    wd = decay["groups"]["weekday"]["weights"]
    ok(wd[9] > wd[15] * 5, "half_life 很短 → 几乎只听近期样本", (round(wd[9], 4), round(wd[15], 5)))

    # ---- 4) 守恒：各种量级、各种权重分布 ----
    cases = [
        (6429.0, [1] * 24, {}, 24),
        (8063.2, [1] * 24, {12: 500.0}, 24),
        (0.003, [1, 1, 1], {}, 3),
        (1.0, [7] * 24, {}, 24),
        (123456.789, [0.5, 2, 0, 9, 1], {}, 5),
        (100.0, [0] * 20 + [1, 1, 1, 1], {}, 24),
        (9999.999, [1] * 24, {i: 1.0 for i in range(0, 24, 3)}, 24),
    ]
    for total, w, meas, upto in cases:
        slots, itg = A.allocate_day("2026-10-28", total, w, meas, upto)
        shown = round(sum(s["value_kwh"] or 0 for s in slots), 3)
        ok(itg["sum_equal"] and abs(shown - round(total, 3)) < 1e-6,
           "守恒：Σ各时段 = 累计量 %.3f" % total, "Σ=%.3f residual=%s" % (shown, itg["residual_kwh"]))
        ok(itg["allocated_slots"] + itg["measured_slots"] + itg["pending_slots"] == 24,
           "时段分类不重不漏（%.3f）" % total, itg["allocated_slots"])

    # 实测时段**原样保留**，不被重摊
    slots, itg = A.allocate_day("2026-10-28", 1000.0, [1] * 24, {9: 250.0}, 24)
    ok(slots[9]["value_kwh"] == 250.0 and slots[9]["source"] == A.SRC_MEASURED,
       "已采集时段保留原值，不参与重摊", slots[9]["value_kwh"])
    ok(slots[9]["actual_kwh"] == 250.0 and slots[9]["allocated_kwh"] is None,
       "实测时段不应再带分摊值", slots[9])

    # 采样不全标记：每小时应 12 个 5 分钟点，只采到 2 个 → partial
    exp = A.expected_samples(60)
    ok(exp == 12, "完整时段应有 12 个 5 分钟采样点", exp)
    slots, itg = A.allocate_day("2026-10-28", 1000.0, [1] * 24,
                                {9: {"kwh": 25.0, "samples": 2},
                                 10: {"kwh": 300.0, "samples": 12}}, 24)
    ok(slots[9]["partial"] is True and slots[9]["samples"] == 2,
       "采样点数不足 → 标 partial（值只代表已采样部分）", slots[9]["samples"])
    ok(slots[10]["partial"] is False and slots[10]["samples"] == 12,
       "采样完整 → 不标 partial", slots[10]["samples"])
    ok(itg["partial_slots"] == 1, "采样不全时段计数正确", itg["partial_slots"])
    ok(round(sum(s["value_kwh"] or 0 for s in slots), 3) == 1000.0,
       "含采样不全时段时守恒依然成立")

    # ---- 5) 不铺未来 ----
    slots, itg = A.allocate_day("2026-10-28", 1000.0, [1] * 24, {}, 8)
    ok(all(s["value_kwh"] is None for s in slots[8:]),
       "未走完 / 未来时段不生成估算值", [s["value_kwh"] for s in slots[8:12]])
    ok(all(s["source"] == A.SRC_NONE for s in slots[8:]), "未来时段来源标记为 none")
    ok(round(sum(s["value_kwh"] or 0 for s in slots), 3) == 1000.0,
       "只对已结束时段分摊时，守恒依然成立",
       round(sum(s["value_kwh"] or 0 for s in slots), 3))
    ok(itg["pending_slots"] == 16, "待定时段计数正确", itg["pending_slots"])

    # ---- 6) 降级策略 ----
    _, itg = A.allocate_day("2026-10-28", None, [1] * 24, {}, 24)
    ok(itg["mode"] == "no_total" and itg["reason"] == A.REASON_NO_TOTAL,
       "无当日累计量 → 降级 no_total", itg["reason"])

    _, itg = A.allocate_day("2026-10-28", 100.0, [0] * 24, {}, 24)
    ok(itg["mode"] == "no_weight" and itg["reason"] == A.REASON_NO_WEIGHT,
       "权重全零 → 降级 no_weight", itg["reason"])

    _, itg = A.allocate_day("2026-10-28", 100.0, [1] * 24, {0: 80.0, 1: 40.0}, 24)
    ok(itg["mode"] == "conflict" and itg["reason"] == A.REASON_NEGATIVE,
       "实测之和 > 累计量 → 降级 conflict（不倒填负数）", itg["residual_kwh"])

    slots0, itg0 = A.allocate_day("2026-10-28", 100.0, [1] * 24,
                                  {i: 0.0 for i in range(24)}, 24)
    ok(itg0["mode"] == "no_target" and itg0["reason"] == A.REASON_NO_TARGET,
       "全部时段已采集 → 无需分摊（no_target）", itg0["reason"])
    ok(all(s["value_kwh"] == 0 and s["source"] == A.SRC_MEASURED for s in slots0),
       "no_target 时保留全部实测值，不因无法分摊而清空")

    # 无权重可分的「已结束但权重为 0」的时段：值为 0 且标为估算
    slots, _ = A.allocate_day("2026-10-28", 100.0, [0] * 12 + [1] * 12, {}, 24)
    ok(slots[0]["value_kwh"] == 0.0 and slots[0]["estimated"] is True,
       "权重为 0 的已结束时段：给 0 并标为估算", slots[0]["value_kwh"])

    # ---- 7) 跨天：bucketize 只按 ts 分桶，日期过滤由调用方做；这里验证 23:55 不会落到次日 ----
    b3 = A.bucketize([["2026-10-28 23:55", 10.0], ["2026-10-28 00:05", 20.0]])
    ok(b3[23]["kwh"] == 10.0 and b3[0]["kwh"] == 20.0, "跨天点各归各桶，不串日",
       (b3[0]["kwh"], b3[23]["kwh"]))

    # ---- 8) ProfileStore：动态更新（签名变化才重算） ----
    p = os.path.join(ROOT, "data", "api_cache", "_test_hour_profile.json")
    try:
        st = A.ProfileStore(p, slot_min=60, mode="weekday_weekend", min_samples=3)
        s1 = A.learn({"2026-10-27": mk_curve("2026-10-27", 1400.0)}, today="2026-10-28")
        st._save(dict(s1, updated_at="t1", signature=[]))
        st2 = A.ProfileStore(p, slot_min=60, mode="weekday_weekend", min_samples=3)
        w_a, m_a = st2.weights_for("2026-10-28", {"2026-10-27": mk_curve("2026-10-27", 1400.0)})
        ok(m_a["samples"] == 1, "落盘后重新加载可用", m_a["samples"])
        w_b, m_b = st2.weights_for("2026-10-28",
                                  {"2026-10-27": mk_curve("2026-10-27", 1400.0),
                                   "2026-10-26": mk_curve("2026-10-26", 1200.0)})
        ok(m_b["samples"] == 2, "样本增加后动态重算", m_b["samples"])
        ok(w_a != w_b, "样本变化后权重随之更新")
        w_c, m_c = st2.weights_for("2026-10-31", {})
        ok(m_c["samples"] == 0 and m_c["source"] == A.FALLBACK,
           "周末分组独立取默认形态", m_c["source"])
    finally:
        try:
            os.remove(p)
        except Exception:
            pass

    print()
    if BAD:
        print("[FAIL] %d 项未通过" % len(BAD))
        sys.exit(1)
    print("[PASS] 时段分摊层全部通过")


if __name__ == "__main__":
    main()
