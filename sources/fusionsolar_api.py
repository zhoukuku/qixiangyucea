# -*- coding: utf-8 -*-
"""华为 SmartPVMS 北向 API 数据源（实时）——按《SmartPVMS 24.5.0 北向接口参考》实现。

接口规范（API 账户接入方式）：
  1) POST {base_url}/thirdData/login          body {"userName": "...", "systemCode": "..."}
     → 登录成功后从【响应头】取 xsrf-token；有效期 30 分钟，每次调用自动续期
     → 注意：一个账号只有一个在线会话，重复登录会使先前 token 失效（故本模块做单例）
  2) POST {base_url}/thirdData/stations       body {"pageNo": 1}                   → plantCode / capacity / plantName
  3) POST {base_url}/thirdData/getDevList     body {"stationCodes": "..."}          → id / devName / esnCode
  4) POST {base_url}/thirdData/getStationRealKpi  body {"stationCodes": "..."}
     → dataItemMap: day_power(kWh) / month_power / total_power / real_health_state
  5) POST {base_url}/thirdData/getDevRealKpi  body {"devIds": "...", "devTypeId": 1}
     → dataItemMap: active_power(kW) / day_cap(kWh) / inverter_state / temperature
  6) POST {base_url}/thirdData/getKpiStationDay   body {"stationCodes": "...", "collectTime": ms}
     → 返回该月每天：inverterYield / radiation_intensity(kWh/m²) / installed_capacity(kW)
       / theory_power / performance_ratio / perpower_ratio
  7) POST {base_url}/thirdData/getDevHistoryKpi   body {"devIds": "...", "devTypeId": 1,
       "startTime": ms, "endTime": ms}            → 5 分钟维度（最多 10 设备 / 3 天）

限流（1 个电站 + 12 台逆变器场景，见文档第 4 章）：
  电站实时 / 设备实时   每 5 分钟 1 次          → 本模块节流 300s（正好合规）
  电站日数据            每天 25 次              → 节流 3600s（24 次/天）
  设备历史 5min         每天 Σceil(设备/10)+24  → 12 台每轮 2 次，节流 4h（12 次/天）
  电站列表 / 设备列表   每天 34 / 25 次         → 缓存 12 小时
  登录                  每 10 分钟 5 次         → token 缓存 25 分钟

日内 5 分钟曲线的做法：实时接口每 5 分钟采样一次并本地落盘累积成曲线，
避免高频调用被限流的 getDevHistoryKpi；服务重启时用一次历史接口回填当天已过去的点。
"""
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

from .base import Source, SourceResult

# 接口最小调用间隔（秒）——严格按文档限流配额设定
_MIN_INTERVAL = {
    "login": 1500,             # token 缓存 25 分钟（有效期 30 分钟）
    "stations": 43200,         # 12 小时
    "getDevList": 43200,       # 12 小时
    "getStationRealKpi": 300,  # 每 5 分钟 1 次
    "getDevRealKpi": 300,      # 每 5 分钟 1 次
    "getKpiStationDay": 3600,  # 每小时 1 次 → 24 次/天（上限 25）
    "getDevHistoryKpi": 14400,  # 每 4 小时 1 轮（12 台 = 2 次/轮 → 12 次/天，上限 26）
}

# 触发平台限流（407/429）后的冷却时长（秒）
_COOLDOWN = {
    "login": 600,
    "getStationRealKpi": 900,
    "getDevRealKpi": 900,
    "getKpiStationDay": 1800,
    "getDevHistoryKpi": 3600,
}

_FAIL_HINT = {
    305: "token 无效或已过期（305）",
    306: "token 已失效（306）",
    401: "API 账户无该接口权限（401）——请到「系统 > 公司管理 > 北向管理」开启“基础类”接口并绑定电站",
    402: "账户未授权（402）——需业主在 FusionSolar APP 授权管理里给该三方应用授权",
    407: "调用超过限流频度（407）——请调大 config.json 的 poll.interval_seconds",
    429: "请求过于频繁（429）",
    20001: "用户名或密码错误（20001）——注意密码字段是 systemCode，不是网页登录密码",
    20400: "用户名或 systemCode 无效（20400）——必须是「系统 > 公司管理 > 北向管理」里开通的账号，网页登录账号无效",
    20009: "电站编号无权限或不存在（20009）——检查 station_code 是否为本 API 账户下的 plantCode",
    20012: "collectTime 非法（20012）",
}


def _hint(code):
    return _FAIL_HINT.get(code, "错误码 %s" % code)


class FusionSolarClient:
    """单例客户端：共享登录会话与限流节流（一个账号只能有一个在线会话）。"""

    _instances = {}
    _lock = threading.Lock()

    @classmethod
    def get(cls, api_cfg, timeout_ms):
        key = (api_cfg.get("base_url") or "", api_cfg.get("username") or "", api_cfg.get("password") or "")
        with cls._lock:
            if key not in cls._instances:
                cls._instances[key] = cls(api_cfg, timeout_ms)
            return cls._instances[key]

    def __init__(self, api_cfg, timeout_ms=8000):
        self.api = api_cfg or {}
        self.timeout_ms = timeout_ms
        self._token = None
        self._token_at = 0
        self._cookies = ""
        self._op_at = {}                 # op -> 上次成功调用时间戳
        self._cool = {}                  # op -> 限流冷却截止时刻
        self._cache = {}                 # 业务级缓存（电站、设备、日数据、曲线）
        self._lock2 = threading.RLock()
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.cache_dir = os.path.join(root, "data", "api_cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._load_disk()

    # ---------- 本地持久化 ----------
    def _path(self, name):
        return os.path.join(self.cache_dir, name)

    def _load_disk(self):
        for name in ("daily.json", "irr.json", "meta.json", "daily_month.json",
                     "dev_rt.json", "station_rt.json", "devs.json", "station.json"):
            p = self._path(name)
            if os.path.exists(p):
                try:
                    self._cache[name[:-5]] = json.load(open(p, encoding="utf-8"))
                except Exception:
                    pass
        self._cache.setdefault("daily", {})
        self._cache.setdefault("irr", {})
        self._cache.setdefault("meta", {})
        self._cache.setdefault("daily_month", {})
        self._load_session()

    # ---------- 会话持久化 ----------
    # 华为登录限流是「10 分钟 5 次」，服务一重启就重新登录会迅速打满配额（407），
    # 之后所有接口都会失败。token 有效期 30 分钟，落盘复用即可跨重启续命。
    def _load_session(self):
        p = self._path("session.json")
        if not os.path.exists(p):
            return
        try:
            s = json.load(open(p, encoding="utf-8"))
            self._token = s.get("token") or None
            self._token_at = float(s.get("token_at") or 0)
            self._cookies = s.get("cookies") or ""
            self._op_at = dict(s.get("op_at") or {})
            self._cool = {k: v for k, v in (s.get("cool") or {}).items() if v > time.time()}
        except Exception:
            pass

    def _save_session(self):
        try:
            json.dump({"token": self._token, "token_at": self._token_at,
                       "cookies": self._cookies, "op_at": self._op_at, "cool": self._cool},
                      open(self._path("session.json"), "w", encoding="utf-8"))
        except Exception:
            pass

    def _save_disk(self, name):
        try:
            json.dump(self._cache.get(name, {}), open(self._path(name + ".json"), "w", encoding="utf-8"),
                      ensure_ascii=False)
        except Exception:
            pass

    def _load_day_curve(self, day):
        p = self._path("power5_%s.json" % day)
        if os.path.exists(p):
            try:
                return json.load(open(p, encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save_day_curve(self, day, pts):
        try:
            json.dump(pts, open(self._path("power5_%s.json" % day), "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass

    # ---------- HTTP ----------
    def _configured(self):
        a = self.api
        return bool(a.get("base_url") and a.get("username") and a.get("password"))

    def _post(self, path, payload):
        base = self.api["base_url"].rstrip("/")
        op = path.strip("/").split("/")[-1]
        # base_url 可写「域名」或「域名/thirdData」，两种都兼容
        url = (base + "/" + op) if base.lower().endswith("/thirddata") else (base + "/thirdData/" + op)
        headers = {"Content-Type": "application/json;charset=utf-8", "Accept": "application/json"}
        if self._token:
            headers["XSRF-TOKEN"] = self._token
        if self._cookies:
            headers["Cookie"] = self._cookies
        ctx = ssl.create_default_context()
        if self.api.get("verify_ssl") is False:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, data=json.dumps(payload or {}).encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_ms / 1000, context=ctx) as r:
            raw = r.read().decode("utf-8", "replace")
            # 响应头里的 xsrf-token（HTTP 头大小写不敏感）
            tok = r.headers.get("xsrf-token") or r.headers.get("XSRF-TOKEN") or ""
            setc = r.headers.get_all("Set-Cookie") or []
            if setc:
                self._cookies = "; ".join(c.split(";")[0] for c in setc)
            try:
                body = json.loads(raw)
            except Exception:
                raise RuntimeError("响应不是 JSON（可能被网关拦截/跳转）：%s" % raw[:120])
            return body, tok

    def login(self, force=False):
        with self._lock2:
            now = time.time()
            if not force and self._token and now - self._token_at < _MIN_INTERVAL["login"]:
                return
            body, tok = self._post("/thirdData/login", {
                "userName": self.api.get("username"),
                "systemCode": self.api.get("password"),
            })
            self._op_at["login"] = now
            if body.get("success") is False:
                raise RuntimeError("登录失败：%s" % _hint(body.get("failCode")))
            if not tok:
                raise RuntimeError("登录成功但未取到 xsrf-token（failCode=%s）" % body.get("failCode"))
            self._token = tok
            self._token_at = now
            self._save_session()

    def call(self, path, payload, op=None, retry=True):
        """带节流与一次重试的调用；返回 (body, from_cache)。"""
        op = op or path.strip("/").split("/")[-1]
        min_iv = _MIN_INTERVAL.get(op, 0)
        with self._lock2:
            if time.time() < self._cool.get(op, 0):
                return None, True                      # 限流退避期内，直接走缓存
            last = self._op_at.get(op, 0)
            if min_iv and time.time() - last < min_iv:
                return None, True
            try:
                self.login()
                body, tok = self._post(path, payload)
                if tok:
                    self._token = tok          # 每次调用都会下发新 token
                    self._token_at = time.time()
                self._op_at[op] = time.time()
                self._save_session()
                if body.get("success") is False:
                    code = body.get("failCode")
                    if code in (305, 306, 401) and retry:
                        self.login(force=True)
                        return self.call(path, payload, op=op, retry=False)
                    if code in (407, 429):
                        # 触发平台限流：进入冷却，否则 5 分钟一轮的轮询会持续撞墙
                        self._cool[op] = time.time() + _COOLDOWN.get(op, 900)
                        self._cool["login"] = time.time() + 300
                        self._save_session()
                    raise RuntimeError("%s：%s" % (op, _hint(code)))
                return body, False
            except urllib.error.HTTPError as e:
                raise RuntimeError("%s HTTP %s" % (op, e.code))
            except urllib.error.URLError as e:
                raise RuntimeError("%s 网络不可达（%s）" % (op, e.reason))
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError("%s 调用异常：%s" % (op, e))

    # ---------- 业务接口 ----------
    def station_code(self):
        code = self.api.get("station_code") or ""
        if code:
            return code
        c = self._cache.get("station")
        if c and time.time() - c.get("_at", 0) < 43200:
            return c["plantCode"]
        body, cached = self.call("/thirdData/stations", {"pageNo": 1}, op="stations")
        if cached:
            raise RuntimeError("电站列表处于节流期，请稍后再试或手动配置 station_code")
        lst = ((body or {}).get("data") or {}).get("list") or []
        if not lst:
            raise RuntimeError("电站列表为空——API 账户未绑定任何电站（北向管理 → 绑定公司/电站）")
        self._cache["station"] = {"plantCode": lst[0]["plantCode"], "plantName": lst[0].get("plantName"),
                                  "capacity_kw": lst[0].get("capacity"), "_at": time.time()}
        self._save_disk("station")
        return lst[0]["plantCode"]

    def inverter_ids(self):
        c = self._cache.get("devs")
        if c and time.time() - c.get("_at", 0) < 43200:
            return c["devs"]
        code = self.station_code()
        body, cached = self.call("/thirdData/getDevList", {"stationCodes": code}, op="getDevList")
        if cached:
            return (c or {}).get("devs", [])
        rows = (body or {}).get("data") or []
        devs = [{"id": r.get("id"), "name": r.get("devName"), "sn": r.get("esnCode"),
                 "devTypeId": r.get("devTypeId")}
                for r in rows if str(r.get("devTypeId")) in ("1", "38", "39") or r.get("devTypeId") is None]
        if not devs:
            devs = [{"id": r.get("id"), "name": r.get("devName"), "sn": r.get("esnCode"),
                     "devTypeId": r.get("devTypeId")} for r in rows]
        self._cache["devs"] = {"devs": devs, "_at": time.time()}
        # 落盘：设备列表接口配额是 25 次/天且缓存 12 小时，重启后内存一空就会卡在
        # 节流窗口里拿不到设备 → 逆变器整页空。落盘后重启即可复用。
        self._save_disk("devs")
        return devs

    def _chunks(self, lst, n=10):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    def dev_realtime(self):
        """逆变器实时：返回每台 {id,name,kwh(当日发电量),kw(当前有功功率)}。

        节流期内（<5 分钟）复用上一次结果，避免数据出现空档。
        """
        devs = self.inverter_ids()
        if not devs:
            return self._cache.get("dev_rt", {}).get("rows", [])
        out = []
        # 实时接口一次就能查完全部设备（10 台上限只存在于历史接口 getDevHistoryKpi）。
        # 早期按 10 台分批时，第二批会被本地节流吃掉 → 12 台只取到 10 台，当日电量偏小
        # （实测 4765.0 vs 电站接口 5417.93）。故改为单次请求。
        body, cached = self.call("/thirdData/getDevRealKpi", {
            "devIds": ",".join(str(d["id"]) for d in devs),
            "devTypeId": int(self.api.get("dev_type_id", 1)),
        }, op="getDevRealKpi")
        if not cached and body:
            for item in body.get("data") or []:
                m = item.get("dataItemMap") or {}
                out.append({
                    "devId": item.get("devId"),
                    "sn": item.get("sn"),
                    "kw": _f(m.get("active_power")),
                    "kwh": _f(m.get("day_cap")),
                    "state": m.get("inverter_state"),
                    "temp": _f(m.get("temperature")),
                })
        by_id = {str(d["id"]): d for d in devs}
        for r in out:
            d = by_id.get(str(r["devId"]), {})
            r["name"] = d.get("name") or d.get("sn") or ("逆变器 %s" % r["devId"])
        if out:
            self._cache["dev_rt"] = {"rows": out, "_at": time.time()}
            self._save_disk("dev_rt")     # 落盘：重启后即使还在节流窗口内也有数据可显示
        return self._cache.get("dev_rt", {}).get("rows", [])

    def station_realtime(self):
        body, cached = self.call("/thirdData/getStationRealKpi",
                                 {"stationCodes": self.station_code()}, op="getStationRealKpi")
        if cached or not body:
            return self._cache.get("station_rt")
        for item in body.get("data") or []:
            if str(item.get("stationCode")) == str(self.station_code()) or len(body.get("data") or []) == 1:
                m = item.get("dataItemMap") or {}
                rt = {
                    "day_kwh": _f(m.get("day_power")),
                    "month_kwh": _f(m.get("month_power")),
                    "total_kwh": _f(m.get("total_power")),
                    "health": m.get("real_health_state"),
                }
                self._cache["station_rt"] = rt
                self._save_disk("station_rt")
                return rt
        return self._cache.get("station_rt")

    def station_day(self, month_ms, force=False):
        """整月日数据：返回 {date: {"gen_kwh":.., "irr":.., "pr":..}}（电量统一 kWh）。

        按「月份」持久化缓存：接口每天只有 25 次配额，节流期必须用已缓存的整月数据回退，
        否则日序列会退化成只剩当天 1 个点（历史趋势图就画不出来）。
        force=True 时忽略本地节流强取一次（仅批量回填历史时使用，调用方负责控制频度）。
        """
        key = datetime.fromtimestamp(int(month_ms) / 1000).strftime("%Y-%m")
        store = self._cache.setdefault("daily_month", {})
        if force:
            self._op_at.pop("getKpiStationDay", None)
        body, cached = self.call("/thirdData/getKpiStationDay",
                                 {"stationCodes": self.station_code(), "collectTime": int(month_ms)},
                                 op="getKpiStationDay")
        if not body:
            return store.get(key, {})          # 节流期：回退到已缓存的该月数据
        out = {}
        for item in body.get("data") or []:
            m = item.get("dataItemMap") or {}
            ct = item.get("collectTime")
            if not ct:
                continue
            day = datetime.fromtimestamp(int(ct) / 1000).strftime("%Y-%m-%d")
            gen = _f(m.get("inverterYield")) or _f(m.get("inverter_power")) or _f(m.get("PVYield"))
            if gen is None:
                continue
            out[day] = {
                "gen_kwh": round(gen, 1),
                "irr": _f(m.get("radiation_intensity")),
                "pr": _f(m.get("performance_ratio")),
                "theory_kwh": round(_f(m.get("theory_power")) or 0, 1),
                "installed_kw": _f(m.get("installed_capacity")),
            }
        if out:
            store[key] = out
        else:
            # 该月确实没有数据（电站尚未并网）：记成空月并下移历史下界，
            # 否则轮询会一直往更早的月份探测，白吃掉 25 次/天的接口配额
            store[key] = {"_empty": 1}
            meta = self._cache.setdefault("meta", {})
            cur = meta.get("history_floor")
            if not cur or key < cur:
                meta["history_floor"] = key
                self._save_disk("meta")
        self._save_disk("daily_month")
        return out or {}

    def backfill_months(self, months=6, pause_s=2.0, log=print):
        """批量回填近 months 个月的日数据（含当月），已缓存的月份跳过。

        独立调用（不受 3600s 节流约束，但严格遵守接口 25 次/天配额：默认只补 6 次），
        结果全部落盘，之后常规轮询命中节流也能从磁盘回退到完整历史。
        """
        now = datetime.now()
        got = {}
        for i in range(months):
            # 逐月往前推：当月、上月、上上月 …
            y, mo = now.year, now.month - i
            while mo <= 0:
                mo += 12
                y -= 1
            anchor = datetime(y, mo, 1, 12)
            key = anchor.strftime("%Y-%m")
            store = self._cache.setdefault("daily_month", {})
            if store.get(key):
                got[key] = len(store[key])
                log("  %s 已缓存 %d 天，跳过" % (key, len(store[key])))
                continue
            try:
                r = self.station_day(int(anchor.timestamp() * 1000), force=True)
            except Exception as e:
                log("  %s 失败：%s" % (key, e))
                continue
            got[key] = len(r)
            log("  %s 取到 %d 天" % (key, len(r)))
            if pause_s and len(r) >= 0:
                time.sleep(pause_s)
        return got

    def dev_history(self, day, devs=None):
        """回填某天的 5 分钟功率曲线（12 台需 2 批）。"""
        devs = devs or self.inverter_ids()
        if not devs:
            return []
        start = datetime.strptime(day, "%Y-%m-%d")
        end = min(start + timedelta(days=1), datetime.now())
        series = {}
        for chunk in self._chunks([d["id"] for d in devs], 10):
            body, cached = self.call("/thirdData/getDevHistoryKpi", {
                "devIds": ",".join(str(x) for x in chunk),
                "devTypeId": int(self.api.get("dev_type_id", 1)),
                "startTime": int(start.timestamp() * 1000),
                "endTime": int(end.timestamp() * 1000),
            }, op="getDevHistoryKpi")
            if cached or not body:
                continue
            for item in body.get("data") or []:
                ct = item.get("collectTime")
                if not ct:
                    continue
                ts = datetime.fromtimestamp(int(ct) / 1000).strftime("%Y-%m-%d %H:%M")
                kw = _f((item.get("dataItemMap") or {}).get("active_power"))
                if kw is None:
                    continue
                series[ts] = series.get(ts, 0.0) + kw
        return sorted([[ts, round(v, 2)] for ts, v in series.items()])

    # ---------- 快照 ----------
    def snapshot(self):
        """组合各接口产出一次快照；任一接口失败不影响其余数据。"""
        t0 = time.time()
        errs = {}
        now = datetime.now()
        day = now.strftime("%Y-%m-%d")

        # 1) 实时（每 5 分钟）
        rt = None
        try:
            rt = self.station_realtime()
        except Exception as e:
            errs["getStationRealKpi"] = str(e)
        devs_rt = []
        try:
            devs_rt = self.dev_realtime()
        except Exception as e:
            errs["getDevRealKpi"] = str(e)

        # 1) 当日曲线为空（服务刚启动）时，先用历史接口回填已过去的点
        curve = self._load_day_curve(day)
        if len(curve) < 2:
            try:
                back = self.dev_history(day)
                if back:
                    curve = back
                    self._save_day_curve(day, curve)
            except Exception as e:
                errs["getDevHistoryKpi"] = str(e)

        # 2) 再把本次采样到的实时总有功功率追加进去（每 5 分钟一个点）
        total_kw = sum(d["kw"] for d in devs_rt if d.get("kw") is not None)
        if total_kw > 0:
            ts = now.strftime("%Y-%m-%d %H:%M")
            curve = [p for p in curve if p[0] != ts]
            curve.append([ts, round(total_kw, 2)])
            curve.sort()
            self._save_day_curve(day, curve)

        # 3) 日数据（含辐照）：先把所有已缓存月份合并进来（历史永不丢），
        #    再按「每月轮流一次」的节奏补一个还没取过的月份（接口 25 次/天，不能每轮都打）
        daily = dict(self._cache.get("daily") or {})
        irr = dict(self._cache.get("irr") or {})
        pr_day = {}
        cap_kw = (self._cache.get("meta") or {}).get("installed_kw")
        months = self._cache.setdefault("daily_month", {})
        for _key in sorted(months):
            for d, v in months[_key].items():
                if not isinstance(v, dict) or v.get("_empty"):
                    continue
                if v.get("gen_kwh") is not None:
                    daily[d] = v["gen_kwh"]
                if v.get("irr") is not None:
                    irr[d] = round(v["irr"], 3)
                if v.get("pr") is not None:
                    pr_day[d] = v["pr"]
                if v.get("installed_kw"):
                    cap_kw = v["installed_kw"]
        # 待补月份：近 12 个月里还没取过的（空月与早于历史下界的月份跳过）
        floor = (self._cache.get("meta") or {}).get("history_floor")
        pending = []
        for i in range(12):
            y, mo = now.year, now.month - i
            while mo <= 0:
                mo += 12
                y -= 1
            key = "%04d-%02d" % (y, mo)
            if floor and key < floor:
                continue
            if key in months:
                continue
            pending.append((key, datetime(y, mo, 1, 12)))
        if pending:
            # 由近及远补：先保证最近月份完整，再逐月往前推进历史
            anchor = pending[0][1]
            try:
                rows = self.station_day(int(anchor.timestamp() * 1000))
            except Exception as e:
                errs["getKpiStationDay"] = str(e)
                rows = {}
            for d, v in rows.items():
                if v.get("gen_kwh") is not None:
                    daily[d] = v["gen_kwh"]
                if v.get("irr") is not None:
                    irr[d] = round(v["irr"], 3)
                if v.get("pr") is not None:
                    pr_day[d] = v["pr"]
                if v.get("installed_kw"):
                    cap_kw = v["installed_kw"]
        self._cache["daily"], self._cache["irr"] = daily, irr
        self._save_disk("daily")
        self._save_disk("irr")
        if cap_kw:
            self._cache.setdefault("meta", {})["installed_kw"] = cap_kw
            self._save_disk("meta")

        # 4) 当日累计电量：优先设备 day_cap 合计，其次电站实时 day_power
        today_kwh = None
        # 电站实时接口（getStationRealKpi.day_power）是官方当日发电量，权威且不受
        # 设备台数/分批影响；设备 day_cap 合计只作备用（限流时可能只取到部分设备）。
        if rt and rt.get("day_kwh") is not None:
            today_kwh = rt["day_kwh"]
        elif devs_rt and any(d.get("kwh") is not None for d in devs_rt):
            today_kwh = sum(d["kwh"] for d in devs_rt if d.get("kwh") is not None)
        if today_kwh is not None and today_kwh > 0:
            daily[day] = round(today_kwh, 1)

        inverters = None
        if devs_rt:
            rows = [{"name": d.get("name"), "kwh": round(d["kwh"], 2) if d.get("kwh") is not None else None,
                     "peak_kw": None, "hours": None, "kw": d.get("kw"), "state": d.get("state")}
                    for d in devs_rt]
            rows = [r for r in rows if r["kwh"] is not None]
            if rows:
                inverters = {"date": day, "rows": rows,
                             "total_kwh": round(sum(r["kwh"] for r in rows), 2),
                             "total_peak_kw": round(sum(r["kw"] for r in rows if r.get("kw")), 2)}

        # 数据时刻取「实时接口的真实采样时刻」，节流期复用缓存时不会假装是刚取到的
        rt_at = (self._cache.get("dev_rt") or {}).get("_at")
        as_of = datetime.fromtimestamp(rt_at) if rt_at else now

        return {
            "client": self,
            "daily": daily,
            "power5": curve,
            "irradiance": irr,
            "inverters": inverters,
            "realtime_kw": round(total_kw, 2) if total_kw else None,
            "station_realtime": rt,
            "installed_kw": cap_kw,
            "pr_by_day": pr_day,
            "errors": errs,
            "as_of": as_of,
            "latency_ms": int((time.time() - t0) * 1000),
        }


def _f(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


class FusionSolarAPI(Source):
    """发电数据（日电量 + 日内 5 分钟功率曲线）。"""

    name = "generation"
    label = "华为 SmartPVMS 北向 API（实时）"

    def fetch(self):
        c = FusionSolarClient.get(self.cfg.get("api", {}), self.timeout_ms)
        if not c._configured():
            return SourceResult("unavailable", error="未配置北向 API（config.json → sources.generation.api：base_url / username / password）")
        try:
            s = c.snapshot()
        except Exception as e:
            return SourceResult("error", error=str(e))
        if not s["daily"] and not s["power5"]:
            return SourceResult("empty", error="接口连通但无数据；%s" % (s["errors"] or "检查账户是否绑定该电站"),
                                latency_ms=s["latency_ms"])
        return SourceResult("ok", data={"daily": s["daily"], "power5": s["power5"],
                                        "realtime_kw": s["realtime_kw"],
                                        "installed_kw": s["installed_kw"],
                                        "irradiance": s["irradiance"]},
                            as_of=s["as_of"], latency_ms=s["latency_ms"],
                            meta={"mode": "api", "errors": s["errors"],
                                  "station_code": (c.api.get("station_code") or
                                                   (c._cache.get("station") or {}).get("plantCode")),
                                  "realtime_kw": s["realtime_kw"]})


class FusionSolarIrradiance(Source):
    """日辐照（来自电站日数据接口的 radiation_intensity，kWh/m²）。"""

    name = "irradiance"
    label = "华为 SmartPVMS 日辐照（实时）"

    def fetch(self):
        c = FusionSolarClient.get(self.cfg.get("api", {}), self.timeout_ms)
        if not c._configured():
            return SourceResult("unavailable", error="未配置北向 API（config.json → sources.irradiance.api）")
        try:
            s = c.snapshot()
        except Exception as e:
            return SourceResult("error", error=str(e))
        if not s["irradiance"]:
            return SourceResult("empty", error="日数据接口未返回辐照；%s" % (s["errors"] or "该电站可能未配置辐照计"),
                                latency_ms=s["latency_ms"])
        return SourceResult("ok", data=s["irradiance"], as_of=s["as_of"],
                            latency_ms=s["latency_ms"], meta={"mode": "api", "errors": s["errors"]})


class FusionSolarInverters(Source):
    """分逆变器数据（当日发电量 + 当前有功功率）。"""

    name = "inverters"
    label = "华为 SmartPVMS 逆变器（实时）"

    def fetch(self):
        c = FusionSolarClient.get(self.cfg.get("api", {}), self.timeout_ms)
        if not c._configured():
            return SourceResult("unavailable", error="未配置北向 API（config.json → sources.inverters.api）")
        try:
            s = c.snapshot()
        except Exception as e:
            return SourceResult("error", error=str(e))
        if not s["inverters"]:
            return SourceResult("empty", error="未取到逆变器数据；%s" % (s["errors"] or "检查设备列表权限"),
                                latency_ms=s["latency_ms"])
        return SourceResult("ok", data=s["inverters"], as_of=s["as_of"],
                            latency_ms=s["latency_ms"], meta={"mode": "api", "errors": s["errors"]})
