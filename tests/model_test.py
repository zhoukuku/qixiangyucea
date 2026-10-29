# -*- coding: utf-8 -*-
"""预测模型单测：天气分档标定（calibrate_regimes）+ 统一偏差/准确率口径。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import model

ok = fail = 0
def chk(name, cond, extra=""):
    global ok, fail
    if cond: ok += 1
    else:
        fail += 1
        print("[FAIL]", name, extra)

# ---------- regime_key：云量分档边界 ----------
chk("0% 云量=晴", model.regime_key(0) == "clear")
chk("34.9% =晴", model.regime_key(34.9) == "clear")
chk("35% =多云", model.regime_key(35) == "partly")
chk("70% =多云", model.regime_key(70) == "partly")
chk("70.1% =阴雨", model.regime_key(70.1) == "overcast")
chk("None → None（不猜）", model.regime_key(None) is None)

# ---------- calibrate_regimes：分档中位数 ----------
p0 = 1.0
# 构造 12 天：晴天 k=0.9，阴天 k=0.5（OM 辐照故意相同，考验分档区分）
daily_gen, om, clouds = {}, {}, {}
for i in range(6):                                   # 6 个晴天
    d = "2026-09-%02d" % (10 + i)
    om[d], clouds[d], daily_gen[d] = 5.0, 10.0, 5.0 * p0 * 1000 * 0.90
for i in range(6):                                   # 6 个阴天
    d = "2026-10-%02d" % (10 + i)
    om[d], clouds[d], daily_gen[d] = 5.0, 90.0, 5.0 * p0 * 1000 * 0.50
ks, info = model.calibrate_regimes(daily_gen, om, clouds, p0)
chk("分档标定生效", info.get("source") == "calibrated_regimes", info.get("source"))
chk("晴天 K=0.90", abs(ks["clear"] - 0.90) < 1e-6, ks["clear"])
chk("阴天 K=0.50", abs(ks["overcast"] - 0.50) < 1e-6, ks["overcast"])
chk("多云缺样本 → 向下借档", abs(ks["partly"] - 0.90) < 1e-6, ks["partly"])
chk("样本计数 12", info["samples"] == 12, info["samples"])
chk("档内样本数 6/0/6", (info["detail"]["clear"]["n"], info["detail"]["partly"]["n"],
                          info["detail"]["overcast"]["n"]) == (6, 0, 6))

# 缺档回落次序：overcast→partly→clear→全局（绝不把阴雨借成晴天）
ks2, _ = model.calibrate_regimes(
    {"2026-09-10": 5.0 * p0 * 1000 * 0.8}, {"2026-09-10": 5.0}, {"2026-09-10": 10.0}, p0)
# 单样本：每档都只有 1 个 < min_samples=2 → 全部回落到全局中位
chk("样本不足回落全局", abs(ks2["overcast"] - 0.8) < 1e-6, ks2)

# 空/坏样本
ks3, info3 = model.calibrate_regimes({}, {}, {}, p0)
chk("无样本 → ks=None", ks3 is None and info3["source"] == "no_samples")
# 半日值/极低值剔除
ks4, info4 = model.calibrate_regimes(
    {"2026-09-10": 30.0}, {"2026-09-10": 5.0}, {"2026-09-10": 5.0}, p0,
    min_samples=1)
chk("极低电量(30kWh)剔除", info4["source"] == "no_samples", info4["source"])

# ---------- k_for ----------
ks5 = {"clear": 0.9, "partly": 0.8, "overcast": 0.5}
chk("k_for 晴", model.k_for(ks5, 5) == 0.9)
chk("k_for 阴", model.k_for(ks5, 85) == 0.5)
chk("k_for 云量未知 → partly", model.k_for(ks5, None) == 0.8)
chk("k_for 无表 → default", abs(model.k_for(None, 5) - 0.85) < 1e-9)

# ---------- accuracy：与偏差同基数（模型相对） ----------
chk("准确率 = 100-|dev| 同基数", abs(model.accuracy(600.0, 1000.0) - 60.0) < 1e-9,
    model.accuracy(600.0, 1000.0))
chk("偏差>100% → 准确率夹 0", model.accuracy(0.0, 1500.0) == 0.0)
chk("完全命中 → 100", abs(model.accuracy(1000.0, 1000.0) - 100.0) < 1e-9)
chk("分母为模型（预测1200实测1000→83.3）", abs(model.accuracy(1000.0, 1200.0) - (100 - 200/12)) < 1e-9)
chk("pred 缺 → None", model.accuracy(100.0, None) is None)
chk("pred=0 → None", model.accuracy(100.0, 0) is None)

# ---------- pearson：关联性校验用 ----------
chk("pearson 完全正相关 = 1", abs(model.pearson([1, 2, 3], [2, 4, 6]) - 1.0) < 1e-6)
chk("pearson 完全负相关 = -1", abs(model.pearson([1, 2, 3], [6, 4, 2]) + 1.0) < 1e-6)
chk("pearson 剔除 None 成对", abs(model.pearson([1, None, 3], [2, 9, 6]) - 1.0) < 1e-6)
chk("pearson 样本<2 → None", model.pearson([1], [2]) is None)
chk("pearson 常量序列 → None", model.pearson([1, 1, 1], [2, 4, 6]) is None)
chk("pearson 空输入 → None", model.pearson([], []) is None)

print("=== 模型测试：%d 通过 / %d 失败 ===" % (ok, fail))
sys.exit(1 if fail else 0)
