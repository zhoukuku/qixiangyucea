# -*- coding: utf-8 -*-
"""华为 SmartPVMS 北向 API（thirdData）连通性自检 —— 依据《SmartPVMS 24.5.0 北向接口参考》。

用法：
  python tools/check_northbound.py                                  # 读 config.json
  python tools/check_northbound.py --user xxx --password yyy        # 命令行传参
  PV_FS_USERNAME=xxx PV_FS_PASSWORD=yyy python tools/check_northbound.py

自检链路（全部只读）：
  POST /thirdData/login             -> 响应头取 xsrf-token
  POST /thirdData/stations          -> plantCode（电站编码）
  POST /thirdData/getDevList        -> 设备 id / devTypeId
  POST /thirdData/getStationRealKpi -> day_power 当日电量
  POST /thirdData/getDevRealKpi     -> active_power 实时功率 / day_cap 当日电量
  POST /thirdData/getKpiStationDay  -> 整月日数据（含 radiation_intensity 辐照）
  POST /thirdData/getDevHistoryKpi  -> 5 分钟维度曲线
最后打印建议写入 config.json 的配置片段。
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 实测结论（2026-10-28）：uni01cn / uniportal 是 SmartPVMS 网页门户域名，
# 打到 /thirdData 会被 302 到 cn.fusionsolar.huawei.com/unisso/login.action（网页登录页）。
# 真正的北向 API 域名是 cn（中国区）/ intl（海外），login 返回 JSON。
CANDIDATES = [
    "https://cn.fusionsolar.huawei.com/thirdData",
    "https://intl.fusionsolar.huawei.com/thirdData",
    "https://uni01cn.fusionsolar.huawei.com/thirdData",
]


def load_cfg():
    p = os.path.join(ROOT, "config.json")
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    return (cfg.get("sources", {}).get("generation", {}) or {}).get("api", {}) or {}


class Client:
    def __init__(self, base, timeout=15, verify=True):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.token = None
        self.cookies = ""
        self.ctx = ssl.create_default_context()
        if not verify:
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def post(self, op, payload=None):
        headers = {
            "Content-Type": "application/json;charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "pv-dashboard/1.0 (northbound checker)",
        }
        if self.token:
            headers["XSRF-TOKEN"] = self.token
        if self.cookies:
            headers["Cookie"] = self.cookies
        data = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(self.base + "/" + op, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                raw = r.read().decode("utf-8", "replace")
                setc = r.headers.get_all("Set-Cookie") or []
                if setc:
                    self.cookies = "; ".join(c.split(";")[0] for c in setc)
                tok = r.headers.get("xsrf-token") or r.headers.get("XSRF-TOKEN")
                if tok:
                    self.token = tok
                return r.status, raw, tok
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), None
        except Exception as e:  # noqa: BLE001
            return None, "%s: %s" % (type(e).__name__, e), None


def parse(raw):
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def snippet(raw, n=200):
    return " ".join((raw or "").split())[:n]


def jerr(js):
    if isinstance(js, dict) and js.get("success") is False:
        return "failCode=%s %s" % (js.get("failCode"), js.get("message") or "")
    return None


def diagnose(status, raw, js):
    if status is None:
        if "timed out" in raw or "Timeout" in raw:
            return "连接超时（网络不通）", "确认本机 443 出站放行；公司网络需网管加白名单"
        if "SSL" in raw or "CERTIFICATE" in raw:
            return "SSL 证书校验失败", "加 --no-verify，或在 config.json 里 verify_ssl=false"
        if "getaddrinfo" in raw or "Name or service" in raw:
            return "域名解析失败", "检查 DNS / 是否内网限制"
        return "请求失败：" + raw, "先确认网络可达"
    if status in (301, 302, 303, 307):
        return "被重定向（%s）：该地址不是北向入口" % status, \
               "base_url 必须填「北向管理」页面给出的接入地址，形如 https://xxx.fusionsolar.huawei.com"
    if status == 404:
        return "404：路径不存在（base_url 多了或少了 /thirdData）", \
               "本工具的 base_url 结尾应为 /thirdData"
    if js is None:
        low = (raw or "").lower()
        if "unisso" in low or "<!doctype html" in low or "<html" in low:
            return "返回的是网页登录页，不是北向接口", \
                   "uni01cn / uniportal 是门户域名；北向 API 在中国区是 https://cn.fusionsolar.huawei.com/thirdData"
        return "返回不是 JSON（HTTP %s）：%s" % (status, snippet(raw)), \
               "多半打到了网页端，请核对接入地址"
    e = jerr(js)
    if e:
        code = js.get("failCode")
        hint = {305: "token 失效", 401: "账户无该接口权限（北向管理里要开“基础类”并绑定电站）",
                402: "未授权（需业主在 APP 授权）", 407: "超过调用限流频度",
                20001: "用户名或密码错误（密码字段是 systemCode，不是网页密码）",
                20400: "用户名或 systemCode 无效——必须是「北向管理」开通的 API 账号，网页登录账号无效",
                20009: "电站编号无权限或不存在"}.get(code, "")
        return e + (" —— " + hint if hint else ""), \
               "对照《SmartPVMS 24.5.0 北向接口参考》8.1 节排查"
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None)
    ap.add_argument("--user", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--station", default=None)
    ap.add_argument("--no-verify", action="store_true")
    a = ap.parse_args()

    cfg = load_cfg()
    base = a.base or os.environ.get("PV_FS_BASE_URL") or cfg.get("base_url") or ""
    user = a.user or os.environ.get("PV_FS_USERNAME") or cfg.get("username") or ""
    pwd = a.password or os.environ.get("PV_FS_PASSWORD") or cfg.get("password") or ""
    station = a.station or os.environ.get("PV_FS_STATION_CODE") or cfg.get("station_code") or ""
    verify = not (a.no_verify or cfg.get("verify_ssl") is False)

    print("=" * 64)
    print("华为 SmartPVMS 北向 API 自检（thirdData）")
    print("=" * 64)
    print("接入地址 :", base or "(未填)")
    print("用户名   :", user or "(未填)")
    print("密码     :", ("*" * len(pwd)) if pwd else "(未填)")
    print("电站编码 :", station or "(未填，可自动发现)")
    print("-" * 64)

    if not user or not pwd:
        print("[X] 缺用户名/密码 —— 还没有北向账号。")
        print()
        print("开通步骤（文档 3.2.1）：")
        print("  1. FusionSolar → 主菜单 系统 > 公司管理 > 北向管理 → 新增")
        print("  2. 设置 关联账号 / 用户名 / 密码")
        print("  3. API 能力里开启「基础类」接口")
        print("  4. 绑定公司或电站权限")
        print("  5. 把 接入地址 + 用户名 + 密码 填进 config.json → sources.*.api，再跑本脚本")
        return 2

    bases = [base] if base else []
    bases += [b for b in CANDIDATES if b not in bases]

    client = None
    for b in bases:
        print("尝试接入:", b)
        c = Client(b, verify=verify)
        st, raw, tok = c.post("login", {"userName": user, "systemCode": pwd})
        js = parse(raw)
        if st == 200 and js is not None and js.get("success") is not False and tok:
            print("  [OK] login 成功，已取得 xsrf-token")
            client = c
            break
        if st == 200 and js is not None and js.get("success") is not False and not tok:
            print("  [!] login 返回 success，但响应头没有 xsrf-token；后续接口会被 305")
            client = c
            break
        why, todo = diagnose(st, raw, js)
        print("  [X] %s" % (why or ("HTTP %s" % st)))
        if todo:
            print("      → %s" % todo)
        print("      原始返回:", snippet(raw, 150) or "(空)")
        print()

    if not client:
        print("-" * 64)
        print("结论：北向接口未接通。上面「→」就是下一步。")
        return 1

    c = client
    print("-" * 64)

    # 1) 电站列表
    st, raw, _ = c.post("stations", {"pageNo": 1})
    js = parse(raw)
    codes = []
    if isinstance(js, dict) and (js.get("data") or {}).get("list"):
        for it in js["data"]["list"][:20]:
            codes.append((it.get("plantCode"), it.get("plantName"), it.get("capacity")))
    if codes:
        print("电站列表（%s 个）：" % ((js.get("data") or {}).get("total") or len(codes)))
        for code, name, cap in codes:
            flag = "   <== 当前配置" if code == station else ""
            print("   %-24s %-28s %skWp%s" % (code or "?", name or "", cap or "?", flag))
        if not station:
            station = codes[0][0]
            print("   → 未配置 station_code，自动取第一个：%s" % station)
    else:
        why, todo = diagnose(st, raw, js)
        print("[X] stations 电站列表：%s" % (why or snippet(raw, 160)))
        if todo:
            print("    → %s" % todo)

    dev_ids = []
    if station:
        # 2) 设备列表
        st, raw, _ = c.post("getDevList", {"stationCodes": station})
        js = parse(raw)
        rows = (js or {}).get("data") or []
        if rows:
            print("-" * 64)
            print("设备列表（%d 台）：" % len(rows))
            for r in rows[:15]:
                print("   id=%-12s type=%-6s %s" % (r.get("id"), r.get("devTypeId"), r.get("devName") or ""))
            dev_ids = [r.get("id") for r in rows if str(r.get("devTypeId")) in ("1", "38", "39")] \
                      or [r.get("id") for r in rows]
            print("   逆变器 %d 台（历史接口每次最多 10 台 → 分 %d 批）"
                  % (len(dev_ids), (len(dev_ids) + 9) // 10))

        # 3) 电站实时
        st, raw, _ = c.post("getStationRealKpi", {"stationCodes": station})
        js = parse(raw)
        print("-" * 64)
        if isinstance(js, dict) and js.get("success") and js.get("data"):
            m = js["data"][0].get("dataItemMap", {})
            print("[OK] getStationRealKpi：当日发电 %s kWh | 当月 %s kWh | 累计 %s kWh | 健康状态 %s"
                  % (m.get("day_power"), m.get("month_power"), m.get("total_power"),
                     {1: "断连", 2: "故障", 3: "健康"}.get(m.get("real_health_state"), m.get("real_health_state"))))
        else:
            why, todo = diagnose(st, raw, js)
            print("[X] getStationRealKpi：%s" % (why or snippet(raw, 160)))
            if todo:
                print("    → %s" % todo)

        # 4) 设备实时
        if dev_ids:
            st, raw, _ = c.post("getDevRealKpi", {"devIds": ",".join(str(x) for x in dev_ids[:10]),
                                                  "devTypeId": 1})
            js = parse(raw)
            if isinstance(js, dict) and js.get("success") and js.get("data"):
                tot_kw = sum(float((d.get("dataItemMap") or {}).get("active_power") or 0) for d in js["data"])
                tot_kwh = sum(float((d.get("dataItemMap") or {}).get("day_cap") or 0) for d in js["data"])
                print("[OK] getDevRealKpi：%d 台 · 当前总有功功率 %.1f kW · 当日合计 %.1f kWh"
                      % (len(js["data"]), tot_kw, tot_kwh))
            else:
                why, todo = diagnose(st, raw, js)
                print("[X] getDevRealKpi：%s" % (why or snippet(raw, 160)))
                if todo:
                    print("    → %s" % todo)

        # 5) 电站日数据（含辐照）
        now = datetime.now()
        st, raw, _ = c.post("getKpiStationDay", {"stationCodes": station,
                                                 "collectTime": int(now.replace(hour=12).timestamp() * 1000)})
        js = parse(raw)
        print("-" * 64)
        if isinstance(js, dict) and js.get("success") and js.get("data"):
            rows = js["data"]
            print("[OK] getKpiStationDay：本月 %d 天" % len(rows))
            for it in rows[-5:]:
                m = it.get("dataItemMap", {})
                d = datetime.fromtimestamp(int(it["collectTime"]) / 1000).strftime("%Y-%m-%d")
                print("   %s 发电 %8.2f kWh | 辐照 %s kWh/m² | 装机 %s kW | PR %s%%"
                      % (d, float(m.get("inverterYield") or 0), m.get("radiation_intensity"),
                         m.get("installed_capacity"), m.get("performance_ratio")))
        else:
            why, todo = diagnose(st, raw, js)
            print("[X] getKpiStationDay：%s" % (why or snippet(raw, 160)))
            if todo:
                print("    → %s" % todo)

        # 6) 5 分钟历史
        if dev_ids:
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            st, raw, _ = c.post("getDevHistoryKpi", {
                "devIds": ",".join(str(x) for x in dev_ids[:10]), "devTypeId": 1,
                "startTime": int(start.timestamp() * 1000), "endTime": int(now.timestamp() * 1000)})
            js = parse(raw)
            if isinstance(js, dict) and js.get("success") and js.get("data"):
                print("[OK] getDevHistoryKpi：今日 5 分钟点 %d 条（示例 %s）"
                      % (len(js["data"]), datetime.fromtimestamp(
                          int(js["data"][-1]["collectTime"]) / 1000).strftime("%H:%M")))
            else:
                why, todo = diagnose(st, raw, js)
                print("[!] getDevHistoryKpi：%s" % (why or snippet(raw, 160)))
                print("    （不影响运行：服务会用实时接口每 5 分钟采样自建曲线）")

    print("=" * 64)
    print("建议写入 config.json → sources：")
    frag = {
        "generation": {"type": "api", "api": {"driver": "fusionsolar", "base_url": c.base,
                                              "username": user, "password": "***",
                                              "station_code": station, "dev_type_id": 1}},
        "irradiance": {"type": "api", "api": {"driver": "fusionsolar"}},
        "inverters": {"type": "api", "api": {"driver": "fusionsolar"}},
    }
    print(json.dumps(frag, ensure_ascii=False, indent=2))
    print("-" * 64)
    print("改完重启：python server.py --port 8765")
    return 0


if __name__ == "__main__":
    sys.exit(main())
