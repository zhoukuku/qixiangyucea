# -*- coding: utf-8 -*-
"""时段分摊：接口没有明细、只有每日累计值时，按历史分布规律摊成近似时段序列。

---------------- 为什么要这一层 ----------------
按时间段展示的数据有两条来源，必须分开标注、不能混着看：

  1) measured   接口直接给出各时段明细（本项目是 5 分钟总功率曲线按小时聚合）
  2) allocated  接口只给得出「当日最终累计值」，用**同组历史日的日内分布**
                作权重，把累计量摊到各个时段，得到近似明细

华为北向的 `getDevHistoryKpi`（日内历史明细）对该账户返回 401，
所以除当天外**所有历史日都只有第 2 条路**。没有这一层，历史日点进去就是空图。

---------------- 三条硬约束 ----------------
  A. 守恒      Σ 分摊值 ≡ 当日累计量（内部全部走「瓦时整数」，零残差）
  B. 不铺未来  只有「已结束」的时段才产生估算值，未完成/未来时段恒为 None
  C. 可溯源    每个时段带 source / weight / basis；日级给 mode / profile / integrity

---------------- 分摊算法 ----------------
 权重 w[h] 由「同分组（工作日/周末）历史日的日内曲线」学到：
   单日归一化 → 按距今天数做指数衰减加权（半衰期 half_life_days）→ 组内求和
 样本不足时向「默认形态」收缩（经验贝叶斯 shrinkage）：
   混合权重 = (n·观测均值 + k·先验) / (n + k)，k = min_samples
   n = 0 → 纯先验（日出日落钟形包络）；0 < n < k → 混合；n ≥ k → 纯观测

 分配用**最大余数法（largest remainder / Hare quota）**：
   先按下取整分配整数瓦时，再把余下的按小数部分从大到小逐个补 1，
   保证 Σ 与累计量**严格相等**，而不是「约等于」。

---------------- 边界情况 ----------------
 · 跨天     所有时段按本地日界切成 24 桶，分摊只在单个 date 内进行，绝不跨日
 · 补采     接口后来补出明细 → 该时段改用实测；剩余量重算后其余时段重新分摊
 · 修正     当日累计量被修正 → 缓存签名含 total_kwh，自动失效重算
 · 打架     实测之和 > 累计量（接口滞后）→ 停止分摊并把差值记入 residual
 · 缺数据   无累计量 / 权重全零 → mode 降级并给出 reason，不编造数值
"""
import json
import math
import os
from datetime import datetime

DEFAULT_SLOT_MIN = 60          # 时段粒度（分钟）：60=逐小时，30=半小时，15=一刻钟
DEFAULT_MIN_SAMPLES = 3        # 低于此样本数时向默认形态收缩
DEFAULT_HALF_LIFE_DAYS = 14    # 近期样本的指数衰减半衰期（天）
SAMPLE_MIN = 5                 # 采集端采样步长（分钟），用于判断时段采样是否完整
PARTIAL_RATIO = 0.6            # 采样点数低于「应有点数」的该比例 → 该时段标为采样不全
ALGO_VERSION = "2026-10-28.2"  # 权重算法版本（进缓存签名，改了算法旧缓存自动失效）

OBSERVED = "observed"          # 权重来源：历史观测
BLENDED = "blended"            # 权重来源：观测 + 默认形态混合
FALLBACK = "fallback"          # 权重来源：纯默认形态

SRC_MEASURED = "measured"      # 时段值来源：接口采集
SRC_ALLOCATED = "allocated"    # 时段值来源：分摊估算
SRC_NONE = "none"              # 时段值来源：无（未来 / 未采集且无法估算）

# 降级原因（前端直接展示）
REASON_NO_TOTAL = "接口未提供当日累计量，无法分摊"
REASON_NO_TARGET = "当日尚无任何已结束时段，无需分摊"
REASON_NEGATIVE = "实测采样之和已超过接口累计量（数据滞后），本日不做分摊"
REASON_NO_WEIGHT = "历史样本权重全为零，无法分配"


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def slot_count(slot_min=DEFAULT_SLOT_MIN):
    """一天的时段桶数量。"""
    try:
        s = int(slot_min)
    except Exception:
        s = DEFAULT_SLOT_MIN
    return max(1, int(round(24 * 60 / float(max(1, s)))))


def group_of(date_str, mode="weekday_weekend"):
    """日期 → 分组键。mode: weekday_weekend（2 组）| per_weekday（7 组）。"""
    try:
        dt = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    except Exception:
        return "weekday"
    if mode == "per_weekday":
        return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][dt.weekday()]
    return "weekend" if dt.weekday() >= 5 else "weekday"


def expected_samples(slot_min=DEFAULT_SLOT_MIN):
    """一个完整时段应有的采样点数（60 分钟 / 5 分钟 = 12）。"""
    return max(1, int(round(max(1, int(slot_min)) / float(SAMPLE_MIN))))


def slot_label(idx, slot_min=DEFAULT_SLOT_MIN):
    """时段桶起点标签，如 06:00。"""
    s = max(1, int(slot_min))
    m = idx * s
    return "%02d:%02d" % ((m // 60) % 24, m % 60)


# ---------------------------------------------------------------- 观测聚合
def bucketize(curve, slot_min=DEFAULT_SLOT_MIN, day=None):
    """把 5 分钟功率点 [ts, kw] 聚合到时段桶。

    口径与既有逐小时视图一致：**1 小时内的 kWh 数值 = 该小时平均 kW**，
    所以桶值 = 桶内所有采样点的平均功率（kW）。

    但平均必须是**时间加权**平均，不能是简单算术平均，原因有二（实测数据）：
      · 源端会用前值填充 —— 11:06~11:10 出现 5 个完全相同的 641.46，
        把它们当独立样本会把桶值硬拽向该值；
      · 采样间隔并不规整 —— 实测 1/4/5/6 分钟混杂，点数多寡与覆盖时长不成比例，
        简单平均会让相邻桶不可比，曲线出现假的锯齿与跳变
        （11:00 桶 634 kW → 11:15 桶 1276 kW，两个桶覆盖同样的 15 分钟）。

    做法：按时间戳去重 → 每个点按「分段常数」保持到下一个点（末点延伸一个间隔）
    → 与桶边界求交，得到每个桶的加权均值。规则采样时与简单平均完全等价。

    day 不为空时只统计该日的点 —— 分摊是**按天**做的，
    跨天的点混进来会把次日凌晨的功率算到当天头上。
    返回 {slot_idx: {"kwh": 加权平均kW, "samples": 该桶直接采样点数}}
    """
    n = slot_count(slot_min)
    span = max(1, int(slot_min))
    d0 = str(day)[:10] if day else None
    # 1) 去重（同一时间戳保留最后一次）并按时间排序
    seen = {}
    for p in curve or []:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            continue
        ts = str(p[0])
        kw = _f(p[1])
        if kw is None or len(ts) < 16:
            continue
        if d0 and ts[:10] != d0:
            continue
        try:
            hh, mm = int(ts[11:13]), int(ts[14:16])
        except Exception:
            continue
        t = hh * 60 + mm
        if 0 <= t < 24 * 60:
            seen[ts[:16]] = (t, kw)
    if not seen:
        return {}
    pts = [seen[k] for k in sorted(seen)]

    # 2) 分段常数积分：点 i 覆盖 [t_i, t_{i+1})，末点延伸一个采样间隔。
    #    间隔必须设上限（30 分钟）：数据断档时若一路「保持」到下一个点，
    #    相隔半天甚至跨时段的两点会被摊成一条假曲线。
    MAX_HOLD = 30
    acc = {}
    for i, (t, kw) in enumerate(pts):
        gap = (pts[i + 1][0] - t) if i + 1 < len(pts) else 5
        if gap <= 0:
            gap = 1
        t_end = t + (gap if gap <= MAX_HOLD else 5)
        t_end = min(t_end, 24 * 60)
        cur = t
        while cur < t_end:
            b = cur // span
            seg = min(t_end, (b + 1) * span)
            w = seg - cur
            if 0 <= b < n and w > 0:
                a = acc.setdefault(b, [0.0, 0.0])
                a[0] += kw * w
                a[1] += w
            cur = seg
    # samples 仍按「该桶直接采到的点数」计，供上层判断采样是否完整
    cnt = {}
    for t, _ in pts:
        b = t // span
        if 0 <= b < n:
            cnt[b] = cnt.get(b, 0) + 1
    out = {}
    for b, (s, w) in acc.items():
        out[b] = {"kwh": round(s / w, 3), "samples": cnt.get(b, 0)}
    return out


def _age_days(date_str, today=None):
    try:
        t = datetime.strptime(today, "%Y-%m-%d") if today else datetime.now()
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
        return max(0, (t - d).days)
    except Exception:
        return 0


# ---------------------------------------------------------------- 默认形态
def fallback_weights(date_str, slot_min=DEFAULT_SLOT_MIN):
    """默认形态：由日出/日落驱动的钟形包络（model.hourly_profile），归一化到和为 1。

    历史样本为 0 时用；它只提供「白天有、夜里无」的形态，不承诺精度。
    """
    from . import model
    n = slot_count(slot_min)
    # 用 1000 kWh 而不是 1 kWh 生成形态：hourly_profile 把每小时功率 round 到 0.1，
    # 日电量取 1 时每小时只有 0.1~0.2，会被抹成台阶（13/14/15 点权重变成同值）。
    # 形态只用到比值，归一化会消掉这个因子。
    prof = model.hourly_profile(str(date_str)[:10], 1000.0) or []
    w = [0.0] * n
    if prof:
        per = max(1, int(round(60 / float(slot_min))))       # 每小时含几个桶
        for i in range(n):
            h = (i * int(slot_min)) // 60
            if h < len(prof):
                w[i] = float(prof[h]["kw"] or 0.0) / per      # 均分到该小时的各桶
    s = sum(w)
    if s <= 0:                                                # 极端兜底：白天均分
        for i in range(n):
            w[i] = 1.0 / n
        s = 1.0
    return [x / s for x in w]


# ---------------------------------------------------------------- 权重学习
def learn(day_curves, slot_min=DEFAULT_SLOT_MIN, mode="weekday_weekend",
          min_samples=DEFAULT_MIN_SAMPLES, half_life_days=DEFAULT_HALF_LIFE_DAYS,
          today=None, sample_dates=None):
    """从「有真实日内曲线的历史日」学习各分组的日内分布权重。

    day_curves   {date: [[ "YYYY-MM-DD HH:MM", kw ], ...]}
    sample_dates 需要产出权重的日期列表（决定要给哪些分组算）
    返回 {"groups": {g: {"weights":[n], "samples":int, "source":str, "basis":str, "dates":[...]}}}
    """
    n = slot_count(slot_min)
    k = max(0, int(min_samples))
    acc = {}

    for d, curve in (day_curves or {}).items():
        d = str(d)[:10]
        b = bucketize(curve, slot_min)
        tot = sum(v["kwh"] for v in b.values())
        if tot <= 0:
            continue                                    # 全天 0 的日子不构成形态
        g = group_of(d, mode)
        a = acc.setdefault(g, {"w": [0.0] * n, "cov": [0.0] * n, "days": 0, "dates": []})
        wt = 0.5 ** (_age_days(d, today) / float(half_life_days)) if half_life_days > 0 else 1.0
        for idx, v in b.items():
            a["w"][idx] += (v["kwh"] / tot) * wt
            # 覆盖度：这条样本在这个小时确实有采样。样本可能只覆盖半个白天
            # （例如服务中午才启动），没覆盖的小时不能当成「权重 0」来拉低观测
            a["cov"][idx] += wt
        a["days"] += 1
        a["dates"].append(d)

    # 需要覆盖的分组：显式指定 → 否则补上两个标准组
    need = set(sample_dates and [group_of(x, mode) for x in sample_dates] or [])
    if not need:
        need = {"weekday", "weekend"}

    groups = {}
    for g in sorted(need):
        a = acc.get(g)
        prior = fallback_weights(_sample_date_for(g), slot_min)
        if not a or a["days"] == 0:
            groups[g] = {"weights": prior, "samples": 0, "source": FALLBACK,
                         "basis": "无同组历史样本，采用日出日落钟形包络（默认形态，未经本站数据标定）",
                         "dates": []}
            continue
        # 逐小时取「该小时的观测均值 / 覆盖度」，再按覆盖度向先验收缩：
        #   该小时有 n 天采样 → 权重≈观测；没有采样 → 完全用先验。
        # 这样一条只覆盖半天的样本不会把下午的观测值压成 0。
        obs, cov = [0.0] * n, [0.0] * n
        for i in range(n):
            if a["cov"][i] > 0:
                obs[i] = a["w"][i] / a["cov"][i]
                cov[i] = min(1.0, a["cov"][i])
        s = sum(obs)
        obs = [(x / s if s > 0 else 0.0) for x in obs]
        raw = [((cov[i] * obs[i] + k * prior[i]) / (cov[i] + k) if (cov[i] + k) > 0 else prior[i])
               for i in range(n)]
        tot = sum(raw)
        w = [(x / tot if tot > 0 else 1.0 / n) for x in raw]

        partial_cover = sum(1 for i in range(n) if 0 < cov[i] < 0.999)
        if a["days"] < k:
            groups[g] = {"weights": w, "samples": a["days"], "source": BLENDED,
                         "basis": "同组历史 %d 天（少于 %d 天阈值，已向默认形态收缩）· 近期样本半衰期 %d 天%s"
                                  % (a["days"], k, half_life_days,
                                     ("· %d 个小时无观测覆盖，该时段用默认形态" % partial_cover)
                                     if partial_cover else ""),
                         "dates": sorted(a["dates"]), "partial_hours": partial_cover}
        else:
            src = OBSERVED if partial_cover == 0 else BLENDED
            groups[g] = {"weights": w, "samples": a["days"], "source": src,
                         "basis": "同组历史 %d 天 · 近期样本半衰期 %d 天%s"
                                  % (a["days"], half_life_days,
                                     ("· %d 个小时无观测覆盖，该时段用默认形态" % partial_cover)
                                     if partial_cover else ""),
                         "dates": sorted(a["dates"]), "partial_hours": partial_cover}
    return {"groups": groups, "slot_min": max(1, int(slot_min)), "mode": mode}


def _sample_date_for(group):
    """给 fallback 形态挑一个代表日期（钟形只跟月份有关）。"""
    now = datetime.now()
    return now.strftime("%Y-%m-%d")


# ---------------------------------------------------------------- 最大余数法
def _distribute(total, ws):
    """把整数 total 按权重 ws 分配，返回与 ws 等长的整数列表，Σ 严格等于 total。"""
    m = len(ws)
    if m <= 0 or total <= 0:
        return [0] * m
    s = float(sum(ws))
    if s <= 0:
        base_total = int(total)
        q, r = divmod(base_total, m)
        out = [q] * m
        for i in range(r):
            out[i] += 1
        return out
    raw = [total * w / s for w in ws]
    base = [int(math.floor(x)) for x in raw]
    rest = int(total) - sum(base)
    if rest > 0:
        # 小数部分大的优先补 1；并列时权重大的优先；再并列按序号（保证结果可复现）
        order = sorted(range(m), key=lambda i: (-(raw[i] - base[i]), -ws[i], i))
        for j in range(rest):
            base[order[j % m]] += 1
    elif rest < 0:
        order = sorted(range(m), key=lambda i: ((raw[i] - base[i]), -ws[i], i))
        for j in range(-rest):
            if base[order[j % m]] > 0:
                base[order[j % m]] -= 1
    return base


# ---------------------------------------------------------------- 分摊主流程
def allocate_day(date_str, total_kwh, weights, measured, upto, basis="", samples=0,
                 source="", slot_min=DEFAULT_SLOT_MIN):
    """把当日累计量 total_kwh 摊到各时段。

    date_str   日期
    total_kwh  接口给出的当日累计量（kWh）；None / <=0 直接降级
    weights    [n] 日内分布权重（无需预先归一化）
    measured   {slot_idx: kWh} 已采集的时段值 —— 这些时段**原样保留，不参与重摊**
    upto       只处理 [0, upto) 的时段；upto 之后的时段恒为 None（不铺未来）
    basis      权重依据说明
    samples    权重用到的历史样本天数
    source     权重来源 observed / blended / fallback

    返回 (slots, integrity)
      slots: [{index, t, actual_kwh, value_kwh, source, weight, samples, estimated}]
      integrity: {daily_total_kwh, measured_sum_kwh, allocated_sum_kwh, sum_kwh,
                  residual_kwh, sum_equal, allocated_slots, pending_slots, mode, reason}
    """
    n = slot_count(slot_min)
    ws = [max(0.0, float(w or 0.0)) for w in (weights or [])]
    if len(ws) != n:
        ws = (ws + [0.0] * n)[:n]

    # measured 支持两种写法：{slot: kWh} 或 {slot: {"kwh": kWh, "samples": n}}
    meas = {}
    for k, v in (measured or {}).items():
        try:
            i = int(k)
        except Exception:
            continue
        if isinstance(v, dict):
            kwh, smp = _f(v.get("kwh")), v.get("samples")
        else:
            kwh, smp = _f(v), None
        if 0 <= i < n and kwh is not None and kwh >= 0:
            meas[i] = {"kwh": kwh, "samples": smp}

    upto = max(0, min(n, int(upto if upto is not None else n)))
    targets = [i for i in range(upto) if i not in meas]

    mode, reason = SRC_ALLOCATED, ""
    total_wh = int(round(float(total_kwh) * 1000)) if _f(total_kwh) else 0
    meas_wh = {i: int(round(v["kwh"] * 1000)) for i, v in meas.items()}
    measured_sum_wh = sum(meas_wh.values())
    rem_wh = total_wh - measured_sum_wh

    alloc_wh = {}
    if total_wh <= 0:
        mode, reason = "no_total", REASON_NO_TOTAL
    elif not targets:
        mode, reason = "no_target", REASON_NO_TARGET
    elif rem_wh < 0:
        mode, reason = "conflict", REASON_NEGATIVE
    elif sum(ws[i] for i in targets) <= 0:
        mode, reason = "no_weight", REASON_NO_WEIGHT
    else:
        vals = _distribute(rem_wh, [ws[i] for i in targets])
        alloc_wh = dict(zip(targets, vals))

    slots = []
    wsum = float(sum(ws)) or 1.0
    exp = expected_samples(slot_min)
    for i in range(n):
        smp = meas[i].get("samples") if i in meas else None
        # 采样不全（例如服务刚启动、只采到当前小时的一两个点）：值仍是真实采集值，
        # 但只是「已采样部分的平均」，不能当成整小时电量读
        partial = bool(smp is not None and exp and smp < exp * PARTIAL_RATIO)
        if i in meas:
            v, src, est = round(meas[i]["kwh"], 3), SRC_MEASURED, False
        elif i in alloc_wh:
            v, src, est = round(alloc_wh[i] / 1000.0, 3), SRC_ALLOCATED, True
        else:
            v, src, est = None, SRC_NONE, False
        slots.append({
            "index": i, "t": slot_label(i, slot_min),
            # actual_kwh 只放**真实采集值**，语义不变，偏差计算不受分摊影响
            "actual_kwh": (round(meas[i]["kwh"], 3) if i in meas else None),
            "allocated_kwh": (round(alloc_wh[i] / 1000.0, 3) if i in alloc_wh else None),
            "value_kwh": v,             # 展示取值：有实测用实测，否则用分摊
            "source": src,
            "estimated": est,           # True = 估算值（前端用虚线/半透明区分）
            "partial": partial,         # True = 采样不全（值只代表已采样部分）
            "weight": round(ws[i] / wsum, 6),
            "samples": smp,
            "samples_expected": exp,
        })

    alloc_sum_wh = sum(alloc_wh.values())
    sum_wh = measured_sum_wh + alloc_sum_wh
    integrity = {
        "daily_total_kwh": round(total_kwh, 3) if _f(total_kwh) else None,
        "measured_sum_kwh": round(measured_sum_wh / 1000.0, 3),
        "allocated_sum_kwh": round(alloc_sum_wh / 1000.0, 3),
        "sum_kwh": round(sum_wh / 1000.0, 3),
        "residual_kwh": round((total_wh - sum_wh) / 1000.0, 3),
        "sum_equal": (total_wh == sum_wh),
        "allocated_slots": len(alloc_wh),
        "measured_slots": len(meas),
        "partial_slots": sum(1 for s in slots if s["partial"]),
        "pending_slots": n - len(meas) - len(alloc_wh),
        "mode": mode,
        "reason": reason,
        "basis": basis,
        "weight_source": source,
        "weight_samples": samples,
        "slot_min": max(1, int(slot_min)),
    }
    return slots, integrity


# ---------------------------------------------------------------- 权重持久化
class ProfileStore(object):
    """日内分布权重的落盘与增量更新。

    签名 = (各样本日期, 各样本点数, slot_min, mode)。签名不变就读缓存，
    变了才重算 —— 每天第一次有新的历史曲线落盘时自动刷新一次。
    """

    def __init__(self, path, slot_min=DEFAULT_SLOT_MIN, mode="weekday_weekend",
                 min_samples=DEFAULT_MIN_SAMPLES, half_life_days=DEFAULT_HALF_LIFE_DAYS):
        self.path = path
        self.slot_min = max(1, int(slot_min))
        self.mode = mode
        self.min_samples = max(0, int(min_samples))
        self.half_life_days = half_life_days
        self._sig = None
        self._data = None

    def _signature(self, day_curves):
        # 算法版本也进签名：形态 / 收缩逻辑一改，旧缓存必须自动失效，
        # 否则会读到用旧算法算出的权重，改半天没效果。
        return (ALGO_VERSION, self.slot_min, self.mode, self.min_samples, self.half_life_days,
                tuple(sorted((str(d)[:10], len(c or [])) for d, c in (day_curves or {}).items())))

    def get(self, day_curves, force=False):
        sig = self._signature(day_curves)
        if not force and self._data is not None and sig == self._sig:
            return self._data
        data = learn(day_curves, slot_min=self.slot_min, mode=self.mode,
                     min_samples=self.min_samples, half_life_days=self.half_life_days)
        data["signature"] = list(sig)
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._data, self._sig = data, sig
        self._save(data)
        return data

    def _save(self, data):
        try:
            d = os.path.dirname(self.path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def weights_for(self, date_str, day_curves=None, force=False):
        """取某天的权重向量，返回 (weights[24], meta)。"""
        data = self.get(day_curves or {}, force=force)
        g = group_of(date_str, self.mode)
        info = (data.get("groups") or {}).get(g)
        if not info:
            w = fallback_weights(date_str, self.slot_min)
            return w, {"group": g, "samples": 0, "source": FALLBACK,
                       "basis": "该分组无历史样本，采用默认形态", "dates": []}
        w = list(info.get("weights") or [])
        if len(w) != slot_count(self.slot_min):
            w = fallback_weights(date_str, self.slot_min)
        return w, {"group": g, "samples": info.get("samples", 0),
                   "source": info.get("source", FALLBACK),
                       "basis": info.get("basis", ""), "dates": info.get("dates", [])}
