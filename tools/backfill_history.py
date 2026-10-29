# -*- coding: utf-8 -*-
"""回填华为北向「电站日数据」历史（getKpiStationDay 按月取数）。

用法：
    python tools/backfill_history.py            # 默认回填近 6 个月
    python tools/backfill_history.py 12         # 回填近 12 个月

为什么要单独跑：
  接口配额是 25 次/天，在线轮询只能每小时补一个月，首次接入或换电站时
  历史趋势图会长时间只有几天数据。本脚本一次性把历史补齐并落盘
  （data/api_cache/daily_month.json），之后常规轮询命中节流也能读到完整历史。

注意：无数据的月份会被标记为空月并记入 history_floor，之后不再重复探测，
避免把每天的配额浪费在电站并网前的月份上。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.fusionsolar_api import FusionSolarClient  # noqa: E402


def main():
    months = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 6
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = json.load(open(os.path.join(root, "config.json"), encoding="utf-8"))
    api = (cfg.get("sources", {}).get("generation", {}).get("api") or {})
    if not (api.get("username") and api.get("password")):
        print("未配置北向账号（config.json → sources.generation.api），无法回填")
        return 1
    print("回填近 %d 个月的日数据…" % months)
    c = FusionSolarClient.get(api, 15000)
    got = c.backfill_months(months, pause_s=2.0)
    store = c._cache.get("daily_month") or {}
    all_days = {}
    for k in sorted(store):
        v = store[k]
        if isinstance(v, dict) and not v.get("_empty"):
            all_days.update(v)
    ds = sorted(all_days)
    print("\n各月结果：", {k: ("空" if (store[k] or {}).get("_empty") else "%d 天" % len(store[k]))
                       for k in sorted(store)})
    print("历史下界 history_floor：", c._cache.get("meta", {}).get("history_floor"))
    print("累计 %d 天" % len(ds), ("（%s ~ %s）" % (ds[0], ds[-1])) if ds else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
