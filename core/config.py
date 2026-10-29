# -*- coding: utf-8 -*-
"""配置加载：config.json（用户填写）+ 环境变量覆盖。

环境变量优先级高于 config.json，方便部署时不改动文件：
  PV_STATION_CODE / PV_FS_BASE_URL / PV_FS_USERNAME / PV_FS_PASSWORD
  PV_IRR_URL / PV_IRR_TOKEN
  PV_FC_URL / PV_FC_TOKEN
  PV_PORT / PV_POLL_SECONDS
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULTS = {
    "http": {"host": "127.0.0.1", "port": 8765, "timeout_ms": 8000, "open_browser": True},
    "poll": {"interval_seconds": 300, "stale_after_seconds": 1800, "retry_backoff_seconds": 30},
    "model": {"k": None, "auto_calibrate": True, "calibrate_days": 30, "pass_line_pct": 85},
    "finance": {"price_yuan_per_kwh": 0.5487, "currency": "CNY", "note": ""},
}


def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=CONFIG_PATH):
    cfg = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
    cfg = deep_merge(DEFAULTS, cfg)
    # 环境变量覆盖
    env = os.environ
    st = cfg.setdefault("station", {})
    if env.get("PV_STATION_CODE"):
        st["station_code"] = env["PV_STATION_CODE"]
    g = cfg["sources"]["generation"]["api"]
    g["base_url"] = env.get("PV_FS_BASE_URL", g.get("base_url") or "")
    g["username"] = env.get("PV_FS_USERNAME", g.get("username") or "")
    g["password"] = env.get("PV_FS_PASSWORD", g.get("password") or "")
    if env.get("PV_STATION_CODE"):
        g["station_code"] = env["PV_STATION_CODE"]
    irr = cfg["sources"]["irradiance"]["api"]
    irr["url"] = env.get("PV_IRR_URL", irr.get("url") or "")
    irr["token"] = env.get("PV_IRR_TOKEN", irr.get("token") or "")
    fc = cfg["sources"]["forecast"]["api"]
    fc["url"] = env.get("PV_FC_URL", fc.get("url") or "")
    fc["token"] = env.get("PV_FC_TOKEN", fc.get("token") or "")
    if env.get("PV_PORT"):
        cfg["http"]["port"] = int(env["PV_PORT"])
    if env.get("PV_POLL_SECONDS"):
        cfg["poll"]["interval_seconds"] = int(env["PV_POLL_SECONDS"])
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
