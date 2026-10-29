# -*- coding: utf-8 -*-
"""环境参数源单测：聚合/风向主方向/组件温度估算/积累合并（不发真实请求）。"""
import sys
import os
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.weather import (daily_weather_from_hourly, dominant_wind_dir, sector,
                             load_accumulated, save_accumulated)


def _hours(n=24, day="2026-10-28"):
    return ["%sT%02d:00" % (day, h) for h in range(n)]


class TestDailyWeather(unittest.TestCase):
    def test_basic_aggregation(self):
        times = _hours(24)
        temp = [20.0 + h * 0.5 for h in range(24)]        # 20 → 31.5
        rh = [80.0 - h for h in range(24)]
        wind = [2.0] * 24
        wdir = [90.0] * 24
        rad = [0.0] * 6 + [600.0] * 12 + [0.0] * 6        # 12 小时白天
        d = daily_weather_from_hourly(times, temp, rh, wind, wdir, rad)["2026-10-28"]
        self.assertEqual(d["temp"], 25.8)                 # mean(20..31.5)
        self.assertEqual(d["temp_min"], 20.0)
        self.assertEqual(d["temp_max"], 31.5)
        self.assertEqual(d["wind"], 2.0)
        self.assertEqual(d["wind_dir"], 90.0)
        self.assertEqual(d["ghi_avg"], 600)               # 白天平均
        self.assertEqual(d["ghi_sum"], 7.2)               # 600×12/1000
        # 组件温度估算：25.75 + 0.03125×600 ≈ 44.5
        self.assertAlmostEqual(d["tmod"], round(25.75 + 0.03125 * 600, 1), places=1)

    def test_night_only_ghi(self):
        rad = [0.0] * 24
        d = daily_weather_from_hourly(_hours(24), [25.0] * 24, [60] * 24, [3.0] * 24, [180] * 24, rad)
        self.assertEqual(d["2026-10-28"]["ghi_avg"], 0)
        self.assertEqual(d["2026-10-28"]["ghi_sum"], 0)
        self.assertEqual(d["2026-10-28"]["tmod"], 25.0)   # 无辐照抬升

    def test_none_values_skipped(self):
        times = _hours(4)
        d = daily_weather_from_hourly(times, [None, 20, None, 22], [None] * 4,
                                      [None] * 4, [None] * 4, [None] * 4)["2026-10-28"]
        self.assertEqual(d["temp"], 21.0)
        self.assertIsNone(d["rh"])
        self.assertIsNone(d["wind_dir"])

    def test_multi_day_split(self):
        times = ["2026-10-27T23:00", "2026-10-28T00:00"]
        d = daily_weather_from_hourly(times, [10.0, 20.0], [50, 60], [1, 2], [0, 90], [100, 200])
        self.assertIn("2026-10-27", d)
        self.assertIn("2026-10-28", d)


class TestWindDir(unittest.TestCase):
    def test_sector_names(self):
        self.assertEqual(sector(0), "北")
        self.assertEqual(sector(90), "东")
        self.assertEqual(sector(115), "东东南")
        self.assertEqual(sector(315), "西北")
        self.assertIsNone(sector(None))

    def test_dominant_ignores_outliers(self):
        # 3 个东风 vs 1 个西风 → 主体东风
        self.assertEqual(dominant_wind_dir([90, 95, 85, 270]), 90.0)

    def test_dominant_empty(self):
        self.assertIsNone(dominant_wind_dir([]))
        self.assertIsNone(dominant_wind_dir([None, None]))


class TestAccumulate(unittest.TestCase):
    def test_roundtrip(self):
        tmp = os.path.join(tempfile.mkdtemp(), "w.json")
        self.assertEqual(load_accumulated(tmp), {})
        save_accumulated({"2026-10-28": {"temp": 25.0}}, tmp)
        self.assertEqual(load_accumulated(tmp)["2026-10-28"]["temp"], 25.0)

    def test_cap_400_days(self):
        tmp = os.path.join(tempfile.mkdtemp(), "w.json")
        wmap = {"%04d-01-01" % (2000 + i): {"temp": i} for i in range(500)}
        save_accumulated(wmap, tmp)
        self.assertEqual(len(load_accumulated(tmp)), 400)

    def test_corrupt_file(self):
        tmp = os.path.join(tempfile.mkdtemp(), "w.json")
        open(tmp, "w").write("{broken")
        self.assertEqual(load_accumulated(tmp), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
