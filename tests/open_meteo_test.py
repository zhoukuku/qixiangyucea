# -*- coding: utf-8 -*-
"""Open-Meteo 适配器单测（纯函数部分，不发真实请求）。"""
import sys
import os
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.open_meteo import daily_from_hourly, drop_today


class TestDailyFromHourly(unittest.TestCase):
    def test_sum_and_unit(self):
        # W/m² × 1h = Wh/m²；4800 Wh/m² = 4.8 kWh/m²
        times = ["2026-10-27T10:00", "2026-10-27T11:00"]
        vals = [200.0, 400.0]
        self.assertEqual(daily_from_hourly(times, vals), {"2026-10-27": 0.6})

    def test_cross_day_split(self):
        times = ["2026-10-26T23:00", "2026-10-27T00:00"]
        vals = [100.0, 300.0]
        d = daily_from_hourly(times, vals)
        self.assertEqual(d, {"2026-10-26": 0.1, "2026-10-27": 0.3})

    def test_none_values_skipped(self):
        times = ["2026-10-27T10:00", "2026-10-27T11:00"]
        vals = [None, 500.0]
        self.assertEqual(daily_from_hourly(times, vals), {"2026-10-27": 0.5})

    def test_empty(self):
        self.assertEqual(daily_from_hourly([], []), {})
        self.assertEqual(daily_from_hourly(None, None), {})

    def test_full_day_shape(self):
        # 24 小时晴天曲线 ≈ 6 kWh/m²：量级防呆
        times = ["2026-10-27T%02d:00" % h for h in range(24)]
        vals = [max(0.0, 800 - abs(h - 12.5) * 100) for h in range(24)]
        d = daily_from_hourly(times, vals)
        self.assertTrue(5.0 < d["2026-10-27"] < 7.0)


class TestDropToday(unittest.TestCase):
    def test_today_removed(self):
        obs = {"2026-10-26": 4.5, "2026-10-27": 2.1, "2026-10-28": 1.3}
        out = drop_today(obs, today="2026-10-28")
        self.assertEqual(out, {"2026-10-26": 4.5, "2026-10-27": 2.1})

    def test_keeps_all_when_no_today(self):
        obs = {"2026-10-26": 4.5}
        self.assertEqual(drop_today(obs, today="2026-10-28"), obs)

    def test_empty(self):
        self.assertEqual(drop_today({}, today="2026-10-28"), {})
        self.assertEqual(drop_today(None, today="2026-10-28"), {})

    def test_default_today_is_now(self):
        today = datetime.now().strftime("%Y-%m-%d")
        out = drop_today({today: 1.0, "2000-01-01": 2.0})
        self.assertEqual(out, {"2000-01-01": 2.0})




# 14) Open-Meteo 本地偏差校正 om_bias_ratio
from core.model import om_bias_ratio
om = {'2026-10-22': 5.326, '2026-10-23': 5.639, '2026-10-24': 5.621, '2026-10-25': 4.792, '2026-10-26': 4.313}
gd = {'2026-10-22': 4.735, '2026-10-23': 4.776, '2026-10-24': 4.709, '2026-10-25': 4.641, '2026-10-26': 3.965}
r, info = om_bias_ratio(om, gd)
assert 0.8 < r < 0.95, '晴日高估场景校正系数应约 0.88，得到 %s' % r
assert info['applied'] and info['samples'] == 5, '样本数应为 5'
r2, info2 = om_bias_ratio(om, {'2026-10-22': 4.735})
assert r2 == 1.0 and not info2['applied'], '样本不足应返回 1.0 不校正'
r3, _ = om_bias_ratio({k: v * 1.0 for k, v in om.items()}, {k: v * 1.0 for k, v in om.items()})
assert abs(r3 - 1.0) < 1e-9, '模型与地面完全吻合时校正系数应为 1'
r4, _ = om_bias_ratio({k: v * 2 for k, v in om.items()}, gd)
assert r4 >= 0.6, '极端高估应被夹取在 0.6 下限'

# 15) hourly_profile 支持预报形状
from core.model import hourly_profile
shape = [0]*6 + [50, 200, 400, 600, 500, 300, 100, 20] + [0]*10
prof = hourly_profile('2026-10-29', 8000.0, shape=shape)
s = sum(p['kwh'] for p in prof)
assert abs(s - 8000) / 8000 < 0.02, '形状归一化后总量应等于日电量（%.1f）' % s
assert prof[6]['kwh'] > 0 and prof[23]['kwh'] == 0, '形状只在有辐照的小时分布'
assert abs(prof[9]['kwh'] / prof[6]['kwh'] - 600/50) < 0.2, '形状比例应保留（云量时间信息）'
prof2 = hourly_profile('2026-10-29', 8000.0, shape=None)
assert abs(sum(p['kwh'] for p in prof2) - 8000) / 8000 < 0.02, '无形状回落钟形近似总量守恒'


if __name__ == "__main__":
    unittest.main(verbosity=2)
