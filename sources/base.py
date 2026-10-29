# -*- coding: utf-8 -*-
"""数据源基类。

每个数据源 fetch() 返回 SourceResult，状态四选一：
  ok          取到数据
  empty       源可用但没数据
  error       请求失败/超时（带 error 文案）
  unavailable 未配置（缺 URL/账号/文件）
"""


class SourceResult:
    def __init__(self, status, data=None, as_of=None, error=None, latency_ms=0, meta=None):
        self.status = status
        self.data = data
        self.as_of = as_of          # 数据本身的时刻（datetime 或 None）
        self.error = error
        self.latency_ms = latency_ms
        self.meta = meta or {}

    def to_dict(self):
        return {
            "status": self.status,
            "as_of": self.as_of.isoformat(timespec="seconds") if self.as_of else None,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "meta": self.meta,
        }

    @property
    def ok(self):
        return self.status == "ok" and self.data


class Source:
    """子类实现 fetch()。"""

    name = "source"
    label = ""

    def __init__(self, cfg, timeout_ms=8000):
        self.cfg = cfg or {}
        self.timeout_ms = timeout_ms

    def fetch(self):  # pragma: no cover
        raise NotImplementedError
