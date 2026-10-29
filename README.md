# 光伏发电预测看板（site）

分布式光伏的发电监控与预测看板：实时功率 / 日前预测 / 水平面辐照 / 气象仪实测导入 / 模型准确率校验。
纯 Python 标准库后端（无 Web 框架）+ 无构建步骤的原生前端，`python server.py` 即可跑起来。

面向客户展示的光伏发电量看板。所有数值都来自数据源接口/导出文件，**没有任何写死的示例数据**；
每一块数据都带「来源 + 最新时间 + 新鲜度」，数据源未配置时就显示「未配置」，不会用假值填充。

---

## 1. 快速开始

```bash
git clone <repo-url> && cd site
cp config.example.json config.json   # 再填入自己的数据源凭据（字段说明见 §3）
pip install openpyxl xlrd            # 仅解析 Excel 导出文件时需要（Python 3.9+）
python server.py                     # 默认 http://127.0.0.1:8766/ ，自动打开浏览器
python server.py --port 8800         # 换端口
python server.py --no-browser        # 不自动开浏览器
```

> `config.json` 里有北向接口账号密码，已在 `.gitignore` 中排除，不会被提交。
> 仓库里只带 `config.example.json`（凭据与电站编码都是占位符）。

---

## 2. 项目结构

```
site/
├─ config.example.json       ← 配置模板（凭据/电站编码为占位符；复制成 config.json 再填）
├─ config.json               ← 你本地的配置（含账号密码，已在 .gitignore 中排除）
├─ server.py                 ← HTTP 服务入口：REST API + 静态站点 + 定时轮询
├─ core/
│  ├─ config.py              ← 配置加载 + 环境变量覆盖
│  ├─ model.py               ← 预测模型 E=辐照×装机×K、K 自动标定、准确率
│  ├─ actuals.py             ← 历史实际数据合并层（多源按日期对齐/去重/补齐，见 §5.2）
│  ├─ allocation.py          ← 时段分摊层（接口无明细时按历史分布摊当日累计量，见 §5.3）
│  ├─ weather_import.py      ← 气象仪 xls 解析/校验/入库（见 §4.2.1）
│  └─ store.py               ← 数据仓：调度拉取/缓存/新鲜度/失败降级/业务视图
├─ data/
│  ├─ weather_station.json   ← 导入库：按日「日辐照 + 逐时辐照」（有导入就有，可整目录删掉重来）
│  ├─ forecast_log.json      ← 日前预测快照（每天一份，用于事后真考核）
│  └─ api_cache/             ← 接口缓存（不入库，删掉只会重新拉一次）
├─ sources/
│  ├─ base.py                ← SourceResult（ok/empty/error/unavailable 四态）
│  ├─ fusionsolar_api.py     ← 华为 SmartPVMS 北向 API（thirdData，按 24.5.0 文档实现 + 内置限流节流）
│  ├─ local_export.py        ← 本地导出文件（FusionSolar xlsx / 气象仪 xls / 逆变器报表）
│  ├─ open_meteo.py          ← Open-Meteo 开放气象源（免费无 key：近7天实况辐照 + 未来16天预报辐照）
│  ├─ weather.py             ← 环境参数源（Open-Meteo 温/湿/风/辐射日线 + 心知天气逐日积累）
│  └─ http_json.py           ← 通用 HTTP JSON 源（气象实测、未来辐照预报）
├─ web/                      ← 前端 SPA（无构建步骤，原生 ES/CSS）
│  ├─ index.html
│  └─ assets/{style.css, api.js, banner.js, charts.js, app.js}
│                            ↑ 顶部提示文案（纯函数，随数据源模式自动显隐）
├─ tools/
│  └─ check_northbound.py    ← 北向 API 一键自检（登录/电站/设备/实时/日数据/历史）
└─ tests/
   ├─ api_smoke.py           ← 启动服务打全部接口 + 静态资源（63 项断言）
   ├─ fusionsolar_test.py    ← 北向适配器离线解析测试（20 项断言，不联网）
   ├─ actuals_test.py        ← 历史实际数据合并层（15 项断言，不联网）
   ├─ allocation_test.py     ← 时段分摊层（58 项断言，不联网）
   ├─ charts_test.mjs        ← 图表生成器单元测试（53 项断言）
   └─ banner_test.mjs        ← 顶部提示文案随数据源模式显隐（14 项断言）
```

页面（hash 路由，刷新后停留在当前页）：

| 路由 | 页面 | 内容 |
|---|---|---|
| `#/overview` | 运行总览 | **只讲当天**：KPI（当前实发 / 当前预测 / 偏差 / 准确率 / 今日峰值）、「发电对比 · 实测 vs 预测」双线图（未来段淡化、两线间差值带、右轴辐照、勾选控制显隐）、辐照度「当日 / 当月」双维度、数据校验卡、逆变器 TOP5 |
| `#/forecast` | 发电分析 | 原「发电预测」+「历史统计」合并：**选一个日期即可同时看该日实测与预测**。逐日趋势（近 30 天 / 未来 30 天 / 全部，X 轴按日历连续）、日历选日（历史日 + 未来日）、该日逐小时对照、历史回测偏差、**历史实际数据**（多源实测合并表 + 月度汇总，可搜索）、预测说明 |
| `#/history` | — | 已并入 `#/forecast`（旧链接自动跳转） |
| `#/inverters` | 逆变器监控 | 设备列表、发电量/峰值/并网时长/占比、可搜索 |
| `#/site` | 电站档案 | 电站信息、运行配置 |
| `#/status` | 数据源 | 每个源的状态/模式/最新时间/耗时/错误，一键刷新；**导入气象仪数据**（见 §4.2.1） |

---

## 3. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 数据源状态、新鲜度、轮询次数 |
| GET | `/api/overview` | KPI 汇总 + 模型信息 + 电价 |
| GET | `/api/trend?scope=all\|recent\|future&limit=30` | 逐日趋势：历史实测 + 模型回算 + 未来预测，同一条时间轴（`is_future` 区分）。历史段与 `/api/daily` **共用 `_merged_day_rows`**，点数/实测值/状态严格一致 |
| GET | `/api/series?range=short\|mid\|long` | 短期=当日 00:00–23:00 逐小时；中期=未来 15 天；长期=未来 30 天（仅 kWh） |
| GET | `/api/intraday?date=&slot_min=` | 指定日逐时段「实测 vs 模型基线」（`slot_min=60` 逐小时[默认] / `15`=15 分钟粒度）+ **分摊估算**（见 §5.3），含每时段 `source`/`estimated`/`partial`、日级 `allocation` 守恒校验 |
| GET | `/api/hour-profile?refresh=1` | 当前生效的日内分布权重：按工作日/周末分组、样本天数、来源（observed/blended/fallback）、依据说明 |
| GET | `/api/forecast/calendar` | 日历范围（首个有发电记录日 ~ 今天+30）+ 每日实测 / 模型 / 预测值 |
| GET | `/api/forecast/day?date=&slot_min=` | 指定日**统一视图**（`slot_min` 同上）：历史日=实测 vs 模型回算；未来日=模型预测（仅 kWh） |
| GET | `/api/forecast/explain` | 预测说明（依据/方法/不确定性） |
| GET | `/api/daily?limit=0` | **历史实际数据**（多源实测合并后的逐日明细，`limit=0`=全部）：实测/模型/偏差/准确率/辐照/峰值/来源/状态 |
| GET | `/api/radiation?month=YYYY-MM` | 辐照度：当日 24 小时逐时（W/m²，每小时带 `src=实测\|预报`）+ 当月逐日（kWh/m²，带 `source`/`is_forecast`/`is_today`）+ 月累计与有效日均 |
| GET | `/api/quality` | 数据校验：回算关联性（`r_actual_forecast` 等）、日前预测准确率（来自快照日志）、今日逐时段一致性、**气象仪质量块**（覆盖天数/断更/与 Open-Meteo 的重叠相关与偏差/异常日） |
| GET | `/api/weather-station` | 已入库气象仪数据概况：天数区间、导入时间、地面标定 `k_ground`、质量标记天数、今日实测覆盖小时数、按月汇总 |
| GET | `/api/forecast-log?limit=45` | 日前预测快照日志与事后对账（**真考核**，区别于用实测辐照反推的回算） |
| POST | `/api/import/weather` | 导入气象仪导出 xls/xlsx：`{"files":[{"name":"2026-10.xls","data_b64":"…"}],"commit":false}`；`commit=false` 只预览（新增/覆盖/异常），`true` 才落盘 |
| GET | `/api/inverters` | 分设备数据 |
| GET | `/api/site` | 电站档案 + 轮询/模型配置 |
| GET | `/api/config` | 当前配置（密码/Token 脱敏为 `***`） |
| POST | `/api/refresh` | 立即拉取，body 可指定 `{"sources":["generation"]}` |

数据源状态语义：`ok` 正常 / `empty` 空数据 / `error` 请求失败或超时 / `unavailable` 未配置。
失败时**保留上一次成功数据**并在页面顶部标注延迟，不会清空成 0。

---

## 4. 需要你填写的配置（config.json）

### 4.1 走实时 API（推荐，开通后全自动）—— 按《SmartPVMS 24.5.0 北向接口参考》实现

**开通步骤（文档 3.2.1，公司管理员操作）**
FusionSolar → 主菜单 **系统 > 公司管理 > 北向管理** → 新增 → 填「关联账号 / 用户名 / 密码」→
API 能力开启**基础类**接口 → 绑定公司或电站 → 保存。拿到的三个值填下面。

```jsonc
"sources": {
  "generation": {
    "type": "api",                       // ← 从 export 改成 api
    "api": {
      "driver": "fusionsolar",
      "base_url": "https://cn.fusionsolar.huawei.com/thirdData",
      "username": "北向用户名(userName)",
      "password": "北向密码(systemCode)",
      "station_code": "",                // 留空则自动取账户下第一个电站
      "dev_type_id": 1,                  // 1=组串式逆变器
      "verify_ssl": true
    }
  },
  "irradiance": { "type": "api", "api": { "driver": "fusionsolar", …同上账号 } },
  "inverters":  { "type": "api", "api": { "driver": "fusionsolar", …同上账号 } }
}
```

**实际调用的接口**（全部 POST，token 为响应头 `xsrf-token`，30 分钟有效、每次调用自动续期）

| 接口 | 用途 | 关键字段 |
|---|---|---|
| `/thirdData/login` | 取 xsrf-token | body `{userName, systemCode}` |
| `/thirdData/stations` | 电站列表（自动发现编码） | `plantCode` / `capacity` |
| `/thirdData/getDevList` | 设备列表 | `id` / `devName` / `esnCode` / `devTypeId` |
| `/thirdData/getStationRealKpi` | 电站实时 | `day_power` 当日电量 kWh、`month_power`、`real_health_state` |
| `/thirdData/getDevRealKpi` | 逆变器实时 | `active_power` kW、`day_cap` kWh |
| `/thirdData/getKpiStationDay` | 整月日数据 | `inverterYield`、`radiation_intensity`(kWh/m²)、`installed_capacity`(kW)、`performance_ratio` |
| `/thirdData/getDevHistoryKpi` | 5 分钟维度曲线 | `collectTime` + `active_power` |

**限流预算（1 电站 + 12 台逆变器，见文档第 4 章）——程序已内置节流，不会超额**

| 接口 | 配额 | 本程序节流 |
|---|---|---|
| 电站/设备实时 | 每 5 分钟 1 次 | 300s |
| 电站日数据 | 25 次/天 | 3600s（24 次/天） |
| 设备 5min 历史 | 26 次/天（12 台每轮 2 次） | 4 小时/轮（12 次/天） |
| 电站/设备列表 | 34 / 25 次/天 | 12 小时 |
| login | 每 10 分钟 5 次 | 25 分钟 |

日内 5 分钟曲线的做法：**实时接口每 5 分钟采样一次并本地累积**（完全合规），
落在 `data/api_cache/power5_<date>.json`，重启不会丢。

> ⚠️ **当日曲线只能从「服务启动那一刻」开始累积**。原设计是服务重启时用一次
> `getDevHistoryKpi` 回填当天已过去的点，但该接口对本账户返回 **401**（未开通），
> 回填必然失败。所以服务早上没开 / 中途关过，那段时间的逐小时实测就是空的
> （例如 16:09 才启动 → 全天只有 16:00 一个小时有实测）。
> 「运行总览 → 当日发电对照」图会把这段标成 **未采集 · 采样自 HH:MM 起** 的淡色底带，
> 与「发电为 0」严格区分；KPI 卡片上的当日实发电量来自北向日电量接口，不受影响。
> 想拿到全天曲线：让服务常驻（开机自启），或开通 `getDevHistoryKpi` 权限。

### 4.1.1 实测结论（2026-10-28 已接通，务必先看）

用真实 API 账户打通后的实测结果，与文档/直觉有出入的三条：

| 项 | 结论 |
|---|---|
| 接入域名 | `uni01cn / uniportal` 是**网页门户**域名，打 `/thirdData` 会 302 到网页登录页；北向 API 中国区域名是 **`cn.fusionsolar.huawei.com`** |
| 电站编码 | 门户 URL 里 `#/view/station/NE=xxx` 是**电站编码**，`#/company/NE=xxx` 是**公司编码**。北向接口要填**电站编码**，填成公司编码会报 20009 |
| **辐照** | ⚠️ `getKpiStationDay` **不返回辐照**。实测原始字段只有 `inverter_power / inverterYield / PVYield / power_profit / reduction_total_co2 / coal / tree`，**没有 `radiation_intensity`**，`installed_capacity` 也是 0。该电站未配辐照计（或字段未授权）→ **辐照必须另找气象源** |

其余接口实测正常：`getStationRealKpi`（当日/当月/累计电量）、`getDevRealKpi`（12 台逆变器）、
`getDevList`（24 台设备：12 逆变器 + 12 Dongle）。
`getDevHistoryKpi` 返回 **401**（账户未开该接口权限）——不影响运行，日内曲线靠实时接口每 5 分钟采样自建。

### 4.2 气象辐照（华为给不了，必须另接）

⚠️ 见 4.1.1：华为 `getKpiStationDay` **不返回** `radiation_intensity`，所以 `irradiance.type` 设成
`api + driver: fusionsolar` 会变成 `empty`。当前 `irradiance` 走气象仪导出文件。

```jsonc
// ⚠️ 不要这样配 —— 华为不返回辐照字段，配了 irradiance 会变 empty：
// "irradiance": { "type": "api", "api": { "driver": "fusionsolar", … } }
```

当前实际配置就是气象仪导出文件：

```jsonc
"irradiance": { "type": "export", "export": { "glob": "~/Downloads/2026-*.xls" } }
```

若以后拿到第三方气象/辐照接口（非华为），用通用 HTTP 适配器（`driver` 不填即为 `http_json`）：

```jsonc
"irradiance": {
  "type": "api",
  "api": {
    "driver": "http_json",
    "url": "https://…/irradiance",
    "token": "…",
    "method": "GET",                      // 或 POST
    "params": { "station": "0444" },
    "field_map": { "date": "date", "value": "ghi_kwh_m2" }
  }
}
```

返回体支持 `[{date, value}]`、`{"data":[…]}`、`{"list":[…]}` 等常见形态。

### 4.2.1 手动导入气象仪数据（推荐，页面里就能做）

不配任何接口也能拿到**真·地面实测辐照**：数据源页 `#/status` → 「导入气象仪数据」卡片，
上传台站后台按月导出的 `.xls`（如 `2026-10.xls`）→「解析预览」看识别结果 →「确认导入」。

- **落点**：`data/weather_station.json`，按日保存「日辐照 kWh/m² + 逐时辐照 W/m²」，同日重复导入**以后一次为准**（幂等），原子写入
- **口径**：日辐照取「日累计 MJ/m² ÷ 3.6」；逐时取「瞬时总辐射 W/m²」的小时均值
- **质量标记**：`rows_lt_200`（采样不足，理论 288 条/天）、`night_nonzero`（夜间非零辐照）、`integ_dev_N%`（日累计与逐点积分差 N%）、`dup_ts`（重复时间戳）
- **生效优先级**：导入库 > 旧目录扫描（`~/Downloads/2026-*.xls`）> Open-Meteo。凡有实测的日子，
  历史回算、当月辐照、当日图右轴一律改用实测；仪器空窗的小时仍标 `预报`，不会冒充实测
- **副作用**：地面标定 `k_ground` 会用「实测辐照 × 实测发电」重新配对计算（页面上能看到配对日数），
  导入后当日预测基线会随之调整——这是期望行为

> 半天残日（例如当天上午导出、日累计还是 0）可以放心导入：它会被打上 `rows_lt_200` 标记，
> 且不进日辐照统计（分母只数有效日），只贡献已有的逐时实测。


### 4.3 未来发电量预测（需要辐照预报源，否则页面明确显示「未配置」）

```jsonc
"forecast": { "type": "api", "api": { "url": "https://…/forecast", "token": "…",
              "horizon_days": 15, "field_map": { "date": "date", "value": "ghi_kwh_m2" } } }
```

### 4.4 其它

| 配置项 | 作用 | 默认 |
|---|---|---|
| `poll.interval_seconds` | 服务端拉取间隔（秒） | 300 |
| `poll.stale_after_seconds` | 超过多久算「过期」并提示 | 1800 |
| `http.timeout_ms` | 数据源请求超时 | 8000 |
| `model.k` | 固定 K；null=自动标定 | null |
| `model.pass_line_pct` | 准确率考核线 | 85 |
| `finance.price_yuan_per_kwh` | 综合电价（元/kWh），仅在「实时状态 / 电站档案」展示配置值 | 0.5487 |
| `archive.dir` / `archive.enabled` | 旧版看板归档目录（相对 `site/`），作为「历史实际数据」的最低优先级补充源 | `../legacy/data` / true |
| `allocation.enabled` | 是否启用时段分摊（关掉后历史日逐小时全空，只显示日电量级实测） | true |
| `allocation.slot_minutes` | 时段粒度：60=逐小时，30=半小时，15=一刻钟 | 60 |
| `allocation.mode` | 权重分组：`weekday_weekend`（工作日/周末）或 `per_weekday`（周一到周日各自一组） | `weekday_weekend` |
| `allocation.min_samples` | 少于多少天样本时向默认形态收缩 | 3 |
| `allocation.half_life_days` | 历史样本的指数衰减半衰期（天），越小越只听近期 | 14 |

### 4.5 环境变量（优先级高于 config.json，便于部署）

| 变量 | 含义 |
|---|---|
| `PV_FS_BASE_URL` / `PV_FS_USERNAME` / `PV_FS_PASSWORD` | 华为北向 API 地址与账号 |
| `PV_STATION_CODE` | 电站编号（形如 `NE=<数字>`） |
| `PV_IRR_URL` / `PV_IRR_TOKEN` | 气象辐照接口与令牌 |
| `PV_FC_URL` / `PV_FC_TOKEN` | 辐照预报接口与令牌 |
| `PV_PORT` / `PV_POLL_SECONDS` | 服务端口 / 轮询间隔 |

---

## 5. 数据口径（必须知道的三条）

1. **预测 = 辐照 × 装机 × K**。K 由最近 30 天「实测辐照 → 实测电量」自动标定（当前 0.947，26 天样本）。
   预测值只对**已发生日期**用实测辐照回算，用于对账；**未来日期必须有预报源才出数**，否则留空。
2. **今日实发**优先取逆变器报表合计（比能量趋势导出更新），两者都在时取大值。
3. **今日辐照缺测**时，今日预测按最近 7 个完整日均值给「参考值」，页面显式标注「参考 · 近期日均」。
4. **趋势图的「推测」线不许断**：个别日期辐照缺测（气象仪未入库）时，用气候学推算补齐并标记
   `status=irr_estimated`，悬停提示会写明；只有「发电 0 且无辐照」的停运日（并网前）才剔除，
   否则图左会出现一段假的零发电低谷。
5. **今天必须单独标出来**：当天只发了半天，实测点必然低于模型全天预测，不标注会被误读成发电暴跌。
   图上以空心点 + 竖虚线 + 「今日」字样标出，X 轴标签带「今」，悬停提示「今日尚未结束，实测仍会上升」。

### 5.1 历史回填（换电站 / 首次接入必做）

`getKpiStationDay` 是**按月**取数的，配额 25 次/天，在线轮询只能每小时补一个月，
首次接入时趋势图会长时间只有几天。先跑一次回填，历史一次性补齐并落盘：

```bash
python tools/backfill_history.py      # 默认近 6 个月
python tools/backfill_history.py 12   # 近 12 个月
```

结果写在 `data/api_cache/daily_month.json`；无数据的月份会标记 `_empty` 并写入
`meta.history_floor`，之后轮询不再往并网前探测（否则配额全浪费在空月份上）。

### 5.2 历史实际数据（「发电分析 → 历史实际数据」表）怎么合并的

实测数据散在四个地方，字段名、单位、覆盖区间都不一样，`core/actuals.py` 把它们合并成
**一条以 `date` 为主键的时间轴**，`/api/daily` 直接输出合并结果。

| 来源 | 取什么 | 单位 | 优先级 |
|---|---|---|---|
| `generation`（北向 API 日电量，含 `daily_month.json` 逐月回填） | 日发电量 | kWh | 电量第 1 |
| `inverters`（分逆变器实时合计） | 当日总电量（仅当天补位） | kWh | 电量第 2 |
| `generation` 5 分钟功率曲线（实时采样 + `data/api_cache/power5_<date>.json`） | 峰值 / 覆盖时段 / 采样点数 | kW | 峰值第 1 |
| `irradiance`（气象仪导出 + 接口辐照） | 日水平面总辐照 | kWh/m² | 辐照第 1 |
| `archive`（`config.json → archive.dir`，默认 `../legacy/data`） | 日电量(MWh→kWh) / 辐照 / 5min 曲线 / 分机峰值 | 换算后同上 | 全部第末 |

**合并规则（四条，对应实际代码）**

1. **按时间对齐**：以 `date`（`YYYY-MM-DD`，本地日界）为主键，字段名/单位与其它接口完全一致
   （`actual_kwh` kWh、`irradiance` kWh/m²、`peak_kw` kW）。
2. **按时间戳去重**：5 分钟曲线按 `YYYY-MM-DD HH:MM` 去重（同一时刻保留优先级高的第一条）；
   日记录按 `date` 去重，同一天无论来自几个来源只留一条，被合并进来的来源记进 `sources[]`。
3. **覆盖最早一期 ~ 最新一期**：`limit=0` 全量返回，不按条数截断；表头写明
   `最早日 ~ 最新日 · 共 N 期 · 合并 X 条 · 去重 Y 条 · 丢弃 0 条`。
4. **缺字段不丢记录**：值缺失一律置 `null`（前端显示 `—`）而不是删行；
   气象仪导出里的 `0.001` 是**空记录占位**，`irradiance` 置空、原值保留在 `irradiance_raw`。
   并网前/停运日（发电 0）标 `no_generation`，只有辐照没有电量的日子标 `irr_only`。

**状态取值**：`ok` 正常 / `irr_estimated` 辐照推算 / `irr_missing` 辐照缺测 /
`no_generation` 停运·并网前 / `irr_only` 仅辐照 / `no_actual` 无电量记录。
停运日仍给「模型 kWh」，用来回答「那天本应发多少、停机损失多少」。

> **趋势图与本表同源**（2026-10-28 起）：`/api/trend` 的历史段同样走 `_merged_day_rows`，
> 点数、实测值、状态与本表逐行一致，覆盖最早一期 ~ 最新一期。画法三条：
> ① 停运/并网前日实测为 **0**，如实画出（配模型线即可读出停机损失），不是缺测；
> ② 只有辐照、没有发电记录的日子实测为 `null` → 实测线**断开**，绝不画成假零线；
> ③ 前端把日历上空缺的日期补成空点（`padCalendarGaps`），X 轴按真实日历连续，
> 否则 08-12 与 09-07 之间隔着 26 天却只占一个身位，会被读成连续运行。
> 这些天数在标题行注明：`实测 N 天 · 停运 N 天 · 仅辐照 N 天 · 无记录 N 天`。

---

### 5.3 时段（逐小时）数据怎么来：采集值 + 分摊估算

按时间段展示的数据有**两条来源**，必须分开标注、不能混着看（`core/allocation.py`）：

| 来源 | 条件 | 标记 |
|---|---|---|
| **采集值** | 接口给出各时段明细。本项目是 5 分钟总功率曲线按小时聚合（当天=实时采样累积，历史日=落盘的 `power5_<date>.json` / `legacy/data/power_5min.json`） | `source="measured"` |
| **分摊估算** | 接口只给得出「当日最终累计值」（华为 `getDevHistoryKpi` 对该账户 401，除当天外所有历史日都走这条） | `source="allocated"`、`estimated=true` |

#### 数据结构

`/api/intraday`、`/api/forecast/day` 的 `hours[]` 每个时段：

```jsonc
{ "hour": 12, "t": "12:00",
  "actual_kwh": null,          // 只放**真实采集值**，没采到就是 null（偏差计算不受估算污染）
  "allocated_kwh": 921.676,    // 分摊估算值
  "value_kwh": 921.676,        // 展示取值：有采集用采集，否则用估算
  "source": "allocated",       // measured | allocated | none
  "estimated": true,           // 前端据此画半透明 + 虚线描边
  "partial": false,            // 采样不全（值只代表已采样部分，不是整时段电量）
  "weight": 0.1422, "samples": null, "samples_expected": 12 }
```

日级 `allocation` 块（守恒校验，直接可断定分摊是否可信）：

```jsonc
{ "daily_total_kwh": 6488.9, "measured_sum_kwh": 21.6, "allocated_sum_kwh": 6467.3,
  "sum_kwh": 6488.9, "residual_kwh": 0.0, "sum_equal": true,
  "measured_slots": 1, "allocated_slots": 17, "partial_slots": 1, "pending_slots": 6,
  "mode": "allocated", "reason": "", "weight_source": "blended", "weight_samples": 1, "basis": "…" }
```

#### 分摊算法

1. **学权重**：拿「同分组（工作日 / 周末，可切 `per_weekday` 按周一到周日 7 组）历史日的日内曲线」，
   单日归一化 → 按距今天数做指数衰减加权（`half_life_days`，默认 14 天）→ 组内求和。
2. **样本不足时收缩**（经验贝叶斯）：`w = (cov·观测 + k·先验) / (cov + k)`，
   `k = min_samples`（默认 3），先验是日出日落钟形包络。
   ⚠ 收缩是**逐小时**按覆盖度做的：一条只覆盖半天的样本不会把下午的观测值压成 0。
3. **分配**：先扣掉已采集时段，剩余量 = `当日累计量 − Σ已采集`，按剩余权重用
   **最大余数法（Hare quota）** 摊到未采集的已结束时段 —— 内部走**整数瓦时**，
   下取整后把余下的逐个补 1，所以 `Σ` 与累计量**严格相等**（不是约等于）。
4. **只摊已结束时段**：`upto = 今天 ? 当前小时 : 24`，`upto` 之后一律 `null`，绝不铺满。
5. 权重落盘 `data/api_cache/hour_profile.json`，签名含样本日期/点数/**算法版本**，
   样本或算法一变自动重算（`/api/hour-profile?refresh=1` 可强制）。

#### 降级策略（`mode` / `reason`）

| 情况 | `mode` | 行为 |
|---|---|---|
| 接口没给当日累计量 | `no_total` | 时段全 `null`，前端显示原因，不编数 |
| 当天还没走到任何已结束时段 | `no_target` | 保留已采集值，不摊 |
| 已采集之和 > 累计量（接口滞后） | `conflict` | 停止分摊，差值记进 `residual_kwh`，不倒填负数 |
| 权重全为零 | `no_weight` | 时段全 `null` |
| 无同组历史样本 | `weight_source=fallback` | 用钟形默认形态，`basis` 明确写「未经本站数据标定」 |
| 样本 < `min_samples` | `weight_source=blended` | 观测 + 默认形态收缩混合 |

#### 边界情况

- **跨天**：时段按本地日界切 24 桶，分摊只在单个 date 内做；`bucketize(day=...)` 强制按日过滤，
  次日 00:05 的点不会串到当天 23 点。
- **补采**：接口后补出明细 → 该时段改标 `measured`，剩余量自动扣除后其余时段重摊，守恒仍成立。
- **数据修正**：当日累计量被修正 → 缓存签名含 `total_kwh`（在 `daily_month.json` 侧），自动失效重算。
- **采样不全**：某时段采样点数 < 应有点数 × 60%（每小时应 12 个 5 分钟点）→ `partial=true`，
  值仍是真实采集值，但只是「已采样部分的平均」，不能当整时段电量读；前端 tooltip 标「实测·采样中」。
- **当日峰值**：优先用 5 分钟曲线的历史最大值；采样不足一小时时用逐小时估算最大值兜底。
  ⚠ 逆变器接口的 `total_peak_kw` 实为**当前瞬时功率合计**，只在完全没有曲线时兜底，且标注「当前瞬时功率合计」。

---

## 6. 测试

```bash
cd site
python tests/api_smoke.py         # 63 项：接口 + 状态 + 刷新 + 静态资源 + 历史实际数据合并 + 分摊守恒
python tests/fusionsolar_test.py  # 20 项：北向适配器离线解析（官方报文打桩，不联网，含整月缓存/空月标记）
python tests/actuals_test.py      # 15 项：合并层对齐/去重/补齐/不丢记录（不联网）
python tests/allocation_test.py   # 58 项：分摊守恒/权重分组与收缩/不铺未来/降级/采样不全（不联网）
cd tests && node charts_test.mjs  # 53 项：SVG 结构/自闭合/null 断线/无 NaN/刻度精简/悬停层
cd tests && node banner_test.mjs  # 14 项：顶部提示随数据源模式（export/api）与新鲜度自动显隐
```

北向账号开通后先跑自检（10 秒出结论，不通会告诉你是哪一步、该做什么）：

```bash
python tools/check_northbound.py                                 # 读 config.json
python tools/check_northbound.py --user 北向用户名 --password 系统码
```

---

## 7. 旧版说明

`../legacy/` 里是之前的「单次导入快照」版本（dashboard.html + ingest.py），页面不再使用；
但它的 `legacy/data/*` 已作为**历史实际数据的归档来源**参与合并（见 §5.2），**不要删**。
