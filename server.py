# -*- coding: utf-8 -*-
"""光伏发电预测看板 —— 本地服务入口。

启动：python server.py [--port 8765] [--once]
接口：
  GET  /api/status             数据源状态（类型/最新时间/延迟/错误）
  GET  /api/overview           KPI 汇总
  GET  /api/trend?scope=all|recent|future&limit=30   逐日趋势（历史实测 vs 模型回算 + 未来预测）
  GET  /api/series?range=short|mid|long    未来预测序列（24h / 15天 / 30天，kWh）
  GET  /api/intraday?date=            指定日逐小时「实测 vs 预测基线」对照（kWh）
  GET  /api/forecast/calendar  日历可选范围（历史日 + 未来日）+ 每日实测/预测
  GET  /api/forecast/day?date= 指定日统一视图（历史=实测 vs 模型回算；未来=模型预测）
  GET  /api/forecast/explain   预测说明（历史依据 / 方法 / 不确定性）
  GET  /api/daily?limit=0      历史实际数据（多源实测合并后的逐日明细，limit=0 为全部）
  GET  /api/hour-profile       当前生效的日内分布权重（工作日/周末分组、样本数、依据）
  GET  /api/inverters          分逆变器
  GET  /api/radiation?month=   日辐射度（当日逐时 W/m² + 当月逐日 kWh/m²）
  GET  /api/quality            数据校验（关联性 r / 偏差分布 / 气象仪质量）
  GET  /api/forecast-log       真实日前预测准确率（快照预测 vs 事后实测）
  GET  /api/weather-station    气象仪导入库概况（覆盖日期/导入时间/质量标记）
  GET  /api/site               电站档案 + 模型配置
  POST /api/refresh            立即刷新（可带 {"sources":["generation"]}）
  POST /api/import/weather     导入气象仪 xls/xlsx（{"files":[{name,data_b64}],"commit":false}
                                commit=false 只出预览：新增/覆盖天数、异常日、列识别结果）
  GET  /api/config             当前配置（脱敏）
静态：web/ 目录（SPA）
"""
import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import load_config          # noqa: E402
from core.store import Store                 # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")

CFG = load_config()
STORE = Store(CFG)

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json; charset=utf-8"}


def json_response(handler, payload, code=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def mask(cfg):
    c = json.loads(json.dumps(cfg, ensure_ascii=False))
    for sec in c.get("sources", {}).values():
        api = sec.get("api") or {}
        for k in ("password", "token"):
            if api.get(k):
                api[k] = "***"
    return c


class Handler(BaseHTTPRequestHandler):
    server_version = "PVBoard/1.0"

    def log_message(self, fmt, *args):
        if "/api/" in (self.path or ""):
            sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), self.path))

    # ---------- GET ----------
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p == "/api/status":
                return json_response(self, STORE.status())
            if p == "/api/overview":
                return json_response(self, STORE.overview())
            if p == "/api/series":
                return json_response(self, STORE.series(q.get("range", ["mid"])[0]))
            if p == "/api/intraday":
                return json_response(self, STORE.intraday(q.get("date", [None])[0],
                                                          slot_min=int(q.get("slot_min", ["60"])[0])))
            if p == "/api/forecast/calendar":
                return json_response(self, STORE.calendar())
            if p == "/api/forecast/day":
                return json_response(self, STORE.day_detail((q.get("date", [""])[0] or "").strip(),
                                                            slot_min=int(q.get("slot_min", ["60"])[0])))
            if p == "/api/forecast/explain":
                return json_response(self, STORE.explain())
            if p == "/api/daily":
                return json_response(self, STORE.daily_rows(int(q.get("limit", ["0"])[0])))
            if p == "/api/trend":
                return json_response(self, STORE.trend(q.get("scope", ["all"])[0],
                                                       int(q.get("limit", ["30"])[0])))
            if p == "/api/hour-profile":
                return json_response(self, STORE.hour_profile(
                    force=(q.get("refresh", ["0"])[0] in ("1", "true", "yes"))))
            if p == "/api/inverters":
                return json_response(self, STORE.inverters())
            if p == "/api/radiation":
                return json_response(self, STORE.radiation((q.get("month", [""])[0] or "").strip() or None))
            if p == "/api/quality":
                return json_response(self, STORE.quality())
            if p == "/api/forecast-log":
                return json_response(self, STORE.forecast_log())
            if p == "/api/weather-station":
                return json_response(self, STORE.weather_station())
            if p == "/api/site":
                return json_response(self, STORE.site())
            if p == "/api/config":
                return json_response(self, mask(CFG))
            return self.serve_static(p)
        except Exception as e:
            return json_response(self, {"error": str(e)}, 500)

    # ---------- POST ----------
    def do_POST(self):
        u = urlparse(self.path)
        try:
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}
        except Exception:
            body = {}
        # 气象仪数据导入：前端把文件读成 base64 走 JSON（3.13 已移除 cgi，
        # 手写 multipart 解析得不偿失；一个月的 xls 约 1.2MB，base64 后 1.6MB 完全够用）
        if u.path == "/api/import/weather":
            files = body.get("files")
            if not isinstance(files, list) or not files:
                return json_response(self, {"error": "未收到文件（files 为空）"}, 400)
            if len(files) > 24:
                return json_response(self, {"error": "一次最多导入 24 个文件"}, 400)
            try:
                return json_response(self, STORE.weather_import(files, commit=bool(body.get("commit"))))
            except Exception as e:
                return json_response(self, {"error": "导入失败：%s" % e}, 500)
        if u.path != "/api/refresh":
            return json_response(self, {"error": "not found"}, 404)
        names = body.get("sources") or list(STORE.sources.keys())
        names = [n for n in names if n in STORE.sources]
        STORE.refresh_one(names, wait=True)
        return json_response(self, {"refreshed": names, "status": STORE.status()})

    # ---------- 静态 ----------
    def serve_static(self, path):
        rel = path.lstrip("/")
        if rel in ("", "index.html"):
            rel = "index.html"
        if rel.startswith("api/"):
            return json_response(self, {"error": "unknown api"}, 404)
        f = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not f.startswith(WEB_DIR) or not os.path.isfile(f):
            f = os.path.join(WEB_DIR, "index.html")     # SPA 回退
        ext = os.path.splitext(f)[1].lower()
        body = open(f, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", STATIC_TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # index.html 禁缓存；其余静态资源全部带 ?v= 版本号，可放心长缓存
        self.send_header("Cache-Control",
                         "no-store" if ext == ".html" else "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(CFG["http"]["port"]))
    ap.add_argument("--host", default=CFG["http"].get("host", "127.0.0.1"))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    STORE.start_poller()
    port = args.port
    for _ in range(20):                      # 端口占用自动顺延
        try:
            httpd = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            port += 1
    else:
        raise SystemExit("无可用端口")
    url = "http://127.0.0.1:%d/" % port
    print("== 光伏发电预测看板 ==")
    print("站点: %s" % url)
    print("轮询: %ss   数据源: %s" % (CFG["poll"]["interval_seconds"],
                                    ", ".join("%s=%s" % (k, v.get("type")) for k, v in CFG["sources"].items())))
    if CFG["http"].get("open_browser") and not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        STORE.stop()


if __name__ == "__main__":
    main()
