# -*- coding: utf-8 -*-
"""北向 API 适配器离线测试：用《SmartPVMS 24.5.0 北向接口参考》里的官方报文样例打桩，
验证 登录取 token / 电站列表 / 设备列表 / 实时 / 日数据（含辐照）/ 5 分钟历史 的解析与聚合。

不联网，可随时重跑：python tests/fusionsolar_test.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from sources.fusionsolar_api import FusionSolarClient, FusionSolarAPI  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK] %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  [X] %s %s" % (name, extra))


# ---------- 官方样例报文 ----------
R_LOGIN = {"success": True, "data": None, "failCode": 0, "params": None, "message": None}
R_STATIONS = {"success": True, "failCode": 0, "message": "get plant list success", "data": {
    "list": [{"plantCode": "NE=000000001", "plantName": "演示光伏电站", "capacity": 2105.0,
              "gridConnectionDate": "2026-09-01T10:00:00+08:00"}],
    "pageCount": 1, "pageNo": 1, "pageSize": 100, "total": 1}}
R_DEVLIST = {"success": True, "failCode": 0, "data": [
    {"id": 1001, "devName": "1NB01", "esnCode": "ES000000000", "devTypeId": 1, "stationCode": "NE=000000001"},
    {"id": 1002, "devName": "1NB02", "esnCode": "ES000000009", "devTypeId": 1, "stationCode": "NE=000000001"},
]}
R_STATION_RT = {"success": True, "failCode": 0, "data": [{
    "stationCode": "NE=000000001",
    "dataItemMap": {"real_health_state": "3", "day_power": "7868.00", "month_power": "225500.00",
                    "total_power": "900000.00", "day_income": "0.00", "total_income": "2088.00"}}]}
R_DEV_RT = {"success": True, "failCode": 0, "data": [
    {"devId": 1001, "sn": "ES000000000",
     "dataItemMap": {"active_power": "727.5", "day_cap": "727.16", "inverter_state": "1", "temperature": "42"}},
    {"devId": 1002, "sn": "ES000000009",
     "dataItemMap": {"active_power": "735.5", "day_cap": "735.53", "inverter_state": "1", "temperature": "43"}}]}

DAY_MS = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
R_DAY = {"success": True, "failCode": 0, "data": [
    {"stationCode": "NE=000000001", "collectTime": DAY_MS,
     "dataItemMap": {"installed_capacity": 2105.0, "radiation_intensity": 4.62,
                     "theory_power": 9725.1, "inverterYield": 7868.0,
                     "performance_ratio": 89, "perpower_ratio": 3.74}},
    {"stationCode": "NE=000000001", "collectTime": DAY_MS - 86400000,
     "dataItemMap": {"installed_capacity": 2105.0, "radiation_intensity": 5.10,
                     "theory_power": 10735.5, "inverterYield": 9190.0,
                     "performance_ratio": 90, "perpower_ratio": 4.37}}]}
R_HIST = {"success": True, "failCode": 0, "data": [
    {"devId": 1001, "collectTime": DAY_MS + 6 * 3600 * 1000, "dataItemMap": {"active_power": "300.0"}},
    {"devId": 1002, "collectTime": DAY_MS + 6 * 3600 * 1000, "dataItemMap": {"active_power": "310.0"}},
    {"devId": 1001, "collectTime": DAY_MS + 6 * 3600 * 1000 + 300000, "dataItemMap": {"active_power": "320.0"}},
    {"devId": 1002, "collectTime": DAY_MS + 6 * 3600 * 1000 + 300000, "dataItemMap": {"active_power": "330.0"}}]}

ROUTES = {
    "login": R_LOGIN, "stations": R_STATIONS, "getDevList": R_DEVLIST,
    "getStationRealKpi": R_STATION_RT, "getDevRealKpi": R_DEV_RT,
    "getKpiStationDay": R_DAY, "getDevHistoryKpi": R_HIST,
}


class FakeClient(FusionSolarClient):
    """把 HTTP 换成本地样例；token 从「响应头」取（与真实接口一致）。"""
    calls = []

    def _post(self, path, payload):
        op = path.strip("/").split("/")[-1]
        FakeClient.calls.append(op)
        if op == "login":
            return R_LOGIN, "fake-xsrf-token-abc"
        return ROUTES[op], "fake-xsrf-token-%d" % len(FakeClient.calls)


def _reset(c):
    """清掉构造时从真实 data/api_cache 加载的会话与日数据（离线桩必须隔离）。"""
    c._token, c._token_at, c._cookies = None, 0, ""
    c._op_at, c._cool = {}, {}
    c._cache = {"daily": {}, "irr": {}, "meta": {}, "daily_month": {}}


def main():
    tmp = tempfile.mkdtemp(prefix="pv_nb_")
    api_cfg = {"base_url": "https://uni01cn.fusionsolar.huawei.com/thirdData",
               "username": "u", "password": "p", "station_code": "", "dev_type_id": 1}
    c = FakeClient(api_cfg, timeout_ms=8000)
    c.cache_dir = tmp
    _reset(c)   # 切到临时目录后清掉从真实磁盘加载的会话/日数据，保证桩数据独立

    print("1) 登录与 token")
    c.login()
    check("login 后从响应头取得 xsrf-token", c._token == "fake-xsrf-token-abc", c._token)

    print("2) 电站列表 / 设备列表")
    check("未配置 station_code 时自动发现", c.station_code() == "NE=000000001", c.station_code())
    devs = c.inverter_ids()
    check("设备列表解析出 2 台逆变器", len(devs) == 2 and devs[0]["name"] == "1NB01", str(devs[:1]))

    print("3) 实时接口")
    FakeClient.calls.clear()
    rt = c.station_realtime()
    check("电站实时 day_power=7868 kWh", rt and rt["day_kwh"] == 7868.0, str(rt))
    dr = c.dev_realtime()
    tot_kw = sum(d["kw"] for d in dr)
    check("设备实时 总有功功率 1463 kW", abs(tot_kw - 1463.0) < 0.01, "%.1f" % tot_kw)
    check("设备实时 名称回填（devName）", [d["name"] for d in dr] == ["1NB01", "1NB02"], str([d["name"] for d in dr]))

    print("4) 日数据（含辐照 / 装机 / PR）")
    FakeClient.calls.clear()
    snap = c.snapshot()
    today = datetime.now().strftime("%Y-%m-%d")
    yday = (datetime.now() - __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")
    # 口径：电站实时接口 day_power 优先（7868），设备 day_cap 合计（1463）仅作备用
    check("今日电量 = 电站实时 day_power 7868 kWh（非设备合计）",
          abs(snap["daily"].get(today, 0) - 7868.0) < 2,
          str(snap["daily"].get(today)))
    check("昨日电量 = 日接口 inverterYield 9190 kWh", abs(snap["daily"].get(yday, 0) - 9190.0) < 2,
          str(snap["daily"].get(yday)))
    check("辐照解析（今日 4.62 kWh/m²）", abs(snap["irradiance"].get(today, 0) - 4.62) < 0.001,
          str(snap["irradiance"].get(today)))
    check("装机容量解析 2105 kW", snap["installed_kw"] == 2105.0, str(snap["installed_kw"]))
    check("实时功率合计写入日内曲线", any(abs(p[1] - 1463.0) < 0.01 for p in snap["power5"]),
          str(snap["power5"][-1:]))
    check("逆变器聚合输出 2 行 / 合计 1462.69 kWh",
          snap["inverters"] and len(snap["inverters"]["rows"]) == 2
          and abs(snap["inverters"]["total_kwh"] - 1462.69) < 0.01,
          str(snap["inverters"]["total_kwh"]) if snap["inverters"] else "None")

    print("4b) 整月日数据缓存（历史不能退化成只剩 1 天）")
    c3 = FakeClient(api_cfg, timeout_ms=8000)
    c3.cache_dir = tempfile.mkdtemp(prefix="pv_nb3_")
    _reset(c3)
    rows = c3.station_day(int(datetime(2026, 10, 1, 12).timestamp() * 1000))
    check("整月日数据解析出本月多天", len(rows) >= 2, "%d 天" % len(rows))
    check("整月结果按月落缓存", bool((c3._cache.get("daily_month") or {}).get("2026-10")))
    # 推进节流窗口后再取：必须回退到缓存，而不是返回空
    c3._op_at["getKpiStationDay"] = time.time()
    again = c3.station_day(int(datetime(2026, 10, 1, 12).timestamp() * 1000))
    check("节流期回退到已缓存的整月数据", len(again) == len(rows) and len(again) > 0,
          "%d 天" % len(again))
    # 空月必须被标记，否则轮询会一直往并网前探测、耗尽 25 次/天配额
    # （桩对所有月份返回同一份报文，这里临时换成空响应模拟「该月无数据」）
    c3._op_at.pop("getKpiStationDay", None)
    _bak = ROUTES["getKpiStationDay"]
    ROUTES["getKpiStationDay"] = {"success": True, "failCode": 0, "data": []}
    try:
        c3.station_day(int(datetime(2026, 8, 1, 12).timestamp() * 1000))
    finally:
        ROUTES["getKpiStationDay"] = _bak
    check("无数据月份标记为 _empty", (c3._cache["daily_month"].get("2026-08") or {}).get("_empty") == 1,
          str(c3._cache["daily_month"].get("2026-08")))
    check("空月记入历史下界，避免继续往前探测",
          (c3._cache.get("meta") or {}).get("history_floor") == "2026-08",
          str((c3._cache.get("meta") or {}).get("history_floor")))

    print("5) 限流节流")
    FakeClient.calls.clear()
    c.snapshot()
    check("节流期内不重复调用受限接口",
          "getKpiStationDay" not in FakeClient.calls and "getStationRealKpi" not in FakeClient.calls,
          str(sorted(set(FakeClient.calls))))

    print("6) 5 分钟历史回填")
    c2 = FakeClient(api_cfg, timeout_ms=8000)
    c2.cache_dir = tempfile.mkdtemp(prefix="pv_nb2_")
    _reset(c2)
    hist = c2.dev_history(today)
    check("历史接口按时间戳合并两台设备",
          len(hist) == 2 and abs(hist[0][1] - 610.0) < 0.01 and abs(hist[1][1] - 650.0) < 0.01, str(hist))

    print("7) 未配置时的降级")
    empty = FusionSolarAPI({"api": {}}, 8000).fetch()
    check("无账号 → unavailable（不造假数据）", empty.status == "unavailable", empty.error[:40])

    shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 60)
    print("通过 %d / 失败 %d" % (PASS, FAIL))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
