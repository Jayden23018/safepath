# SafePath — 项目说明

Penn 暑期课程 final project。用 Flask 的 Python web app，串联三周课程：Pandas（数据）→ Graph（路由）→ Flask+HTML（Web）。单人 2 天 MVP，功能已实现完毕（2026-07-28）。

每条规则后面是原因。事故经过、实测数字和日期在 `GOTCHAS.md`，按需读。

## 一句话产品
费城步行安全/无障碍路由：在 Penn 周边 ~3.5km 步行图上叠加"安全数据层"（犯罪历史 + 用户上报的人行道/ADA 问题），找更安全的路线（不只是最短），并用可切换的风险图层（街道上色 + 犯罪热力图）让"哪里危险"肉眼可见。

## 怎么跑
```bash
python3 -m pip install -r requirements.txt
python3 prep_data.py            # 建图缓存 + 算风险 → data/safepath.db（3.5km 首次 ~30s）
python3 -m flask --app app run  # http://127.0.0.1:5000/
```
依赖以 `requirements.txt` 为准。本机 `pip` 装进 Python 3.9、`python3` 解析到 3.10，所以装依赖用 `python3 -m pip install`，不用裸 `pip install`。

只动了 POI tag / 路口逻辑时不用全跑 prep_data（会白算 386k 条犯罪），只重建 places 表：
```bash
python3 -c "import prep_data as p;from collections import Counter;G=p.fetch_walk_graph();a=[{'name':n,'lat':float(c[0]),'lng':float(c[1]),'kind':'landmark'} for n,c in p.PRESET_LOCATIONS.items()]+p.build_intersection_places(G)+p.fetch_poi_places();p.write_places_to_sqlite(a);print(dict(Counter(x['kind'] for x in a)))"
```
自检没有框架，用 `__main__` 里的 bare assert：
```bash
python3 prep_data.py   # H1 百分位排名归一化 / H4 H4b buffer+距离衰减分配
python3 routing.py     # H2 安全权重公式 / H3 路线分叉 / H6 hot_blocks 计数 / H7 avoided_hot_segments
python3 app.py --check # H5 分叉三态分类（按 diff_ratio）/ H8 点选 snap 距离守卫
```

## 改之前要知道的约束
- 改 `GRAPH_DIST_M` 后删 `data/walk.graphml`。`fetch_walk_graph` 命中缓存就短路，不会重抓新半径的图，静默保留旧图。
- `edge_risk` 加列或改列后重跑 `python3 prep_data.py`。`load_risk_weights` 显式 SELECT 这些列，旧库缺列会让每个请求都 `OperationalError`；这是一次性手动步骤，不加 try/except 兜底。
- α = 1.5 锁定（最险边 = 2.5× 真实长度，零犯罪边 = 原长度）。改它要同步 `routing.py` 的 H2 自检。
- `REPORT_BUMP_FACTOR = 5.0`。计划原值 0.3 太弱，密集路网里单条边的 bump 几乎看不见。改它先读 README 局限 #9。
- 犯罪 CSV：OpenDataPhilly 下的 `<year>_crime_data.csv` 复制成 `data/crime_<year>.csv`，近 3 年 2024–2026（列名一致，已核验）。列名 `point_x/point_y/text_general_code/dispatch_date/dc_key`。改路径要动 `CRIME_CSV_PATHS`。
- osmnx 用 2.0.7（计划写的 ≥2.1.1 不适用）：`nearest_nodes/nearest_edges` 在 `ox.distance`，不在 `ox.routing`。
- `prep_data.py` 重跑不能删用户上报。各 `write_*_to_sqlite` 只 `to_sql(replace)` 自己的表，`reported_incidents` 用 `CREATE TABLE IF NOT EXISTS` 保留，别加回 `os.remove(db_path)`。
- `BUFFER_M = 120` + 距离衰减：犯罪点按 `decay = 1 - dist/120` 加权分给 buffer 内所有边（NKDE 风格，覆盖相邻 2-3 条平行街）。原 30m 等权太稀疏。
- 归一化用百分位排名：`risk_normalized = risk_score.rank(pct=True)`，`risk_score` 不除边长。除以边长会让 risk 变成在测「边有多短」（`corr(risk, log length)=-0.719`），过街小段会污染 hot_blocks。代价是丢失量级，但 p90 阈值本身也是排名切分，语义一致。改它要同步 `prep_data` 的 H1 自检，经过见 `GOTCHAS.md`。
- 不要对 MultiDiGraph 用 `nx.shortest_simple_paths`（Yen / K-shortest），它不支持 multigraph，网格里还枚举不完。要做长度约束，用拉格朗日松弛，但实测不值得做。见 `docs/superpowers/specs/2026-07-29-detour-tolerance-design.md`。

## 数据契约（SQLite 表）
`routing.py` 和 `app.py` 运行时只读 SQLite，不碰 pandas，所以数据都在 prep 阶段落表。
- `places(name, lat, lng, kind)`，kind ∈ {landmark, poi, intersection}：地点搜索数据。landmark 从 `routing.PRESET_LOCATIONS` 来，它是唯一真源，别复制坐标。poi 来自 `ox.features.features_from_point`，整体 try/except 降级 `[]`，Overpass 挂了不阻断。intersection 取 name 数 ≥2 的路口，label 为 `" & ".join(sorted(names))` 并去重。
- POI tag 集必须含 `building`，否则校园楼（多数只有 `building=*`）搜不到。不收 `public_transport` / `railway`，那些名字是站点 ID，只会污染候选。
- 取边的 `name` 时它可能是 list，取 `nm[0]`，否则放进 set 会 unhashable。POI 取点用 `geometry.representative_point()`，不用 `.centroid`。
- `crime_points(lat, lng, severity)`：热力图点。prep 阶段用 `HEATMAP_SAMPLE_N=5000` + 固定 `random_state=0` 采样一次，`app.py:_load_crime_points` 直接 `SELECT` 全表。运行时 `ORDER BY RANDOM()` 要对 10 万+ 行排序，每次冷启动重付。
- `edge_risk`：`risk_normalized DESC` 建了索引，服务 `_load_edge_risk_colors` 的 `ORDER BY ... LIMIT`。它还有 `top_crime_category` / `top_crime_count`，供路线说明弹窗标案件类型。

## 指标口径（对外怎么报数）
- 分叉量的是路线重合度，不是绕路比。网格里换条平行街，路线差异很大但总长几乎不变。`classify_divergence` 按 `diff_ratio = 1 - Jaccard(节点集)` 分类，阈值 `MINOR_DIFF_RATIO = 0.25`。
- 主指标是高危路段数（`hot_blocks`，`risk > p90`）。总风险是全路径求和、风险场平滑，两条路只差 1–2%，不能当卖点。p90 由 `load_risk_weights` 从 edge_risk 实算存进 `G.graph["risk_p90"]`，不硬编码。
- 四对 demo 的实测：`diff_ratio` 0.544 / 0.615 / 0.922 / 0.927（54%–93% 的路段不同），绕路只有 +40m~+107m（2026-08-11 复测）。Penn Park→Mill Creek 高危段 10→0（+106m），Van Pelt→30th St 4→0（+40m）。
- 对外一律先报绝对米数，百分比放括号。短程的百分比会失真：Van Pelt→Huntsman 是 177m 基线上的 +63m，写成 +35.8% 听着吓人。旧的 "+0.2%~+2.3%" 是算错的，不再引用。
- 短程（<500m）没有高危段可避，改报总风险降幅（Van Pelt→Huntsman 降 57%）。
- 起点本身在高危区时，高危段数净差可以是 0，要报米数：Market St→Mantua 5→5（+303m），高危米数 115.1→55.6 = −52%。这种 pair 在 UI 里加「绕不掉」说明。
- `incident_summary` 是 `list[str]`（类型名 top 3，不带数量）。`top_crime_count` 不跨平行边求和，也不沿路径求和：120m buffer 让一起案件平均落在 112 条边上，求和会得到 "Fraud (1850)" 这种虚高数字。H13（`_check_incident_types`）锁住它。要报真实案件数，回 `crime_points` 或原始 CSV 重算。
- `/report` 是分叉的另一来源：上报一条边后它的 safety_weight ×7.5（`REPORT_BUMP_FACTOR` × severity），安全路径会绕开，幅度仍温和。
- demo 重心是可视化 + 上报。新加的 4 个校外高犯罪地标（Mantua / Mill Creek / Point Breeze / Brewerytown）让分叉有真实的高/低风险对比。数据规模和分布的实测值见 `GOTCHAS.md`，以 `prep_data.py` 输出为准。

## 评分叙事映射（改功能时别破坏）
- Pandas：`prep_data.py`（CSV→DataFrame→groupby 边→severity×距离衰减加权→百分位排名归一化→to_sql）
- Graph：`prep_data.py`（建图 + buffer 距离衰减分配）+ `routing.py`（两种权重下 `nx.shortest_path` 跑 Dijkstra 分叉）
- Flask+HTML：`app.py` + templates（GET 渲染；POST /route 计算并重渲染；POST /report 持久化 SQLite，改变后续行为；folium 双风险图层 + LayerControl 经 Jinja `|safe` 嵌入）
- 三个 point：持久化 = SQLite / 有意义的 POST = /report 改状态 + /route 触发计算 / 部署 = 未来工作

## 架构边界
- `routing.py` 运行时不碰 pandas，只读 SQLite。`app.py` 不直接碰图算法细节，调 routing。
- MultiDiGraph 平行边求和用 `min(weight)`（与 Dijkstra 选边一致），别改回 `key=0`。
- 无 tests/ 目录，自检全放 `__main__`。非平凡逻辑要有 bare assert。
- `SAFEPATH_CTX` 是前后端契约（`_inject_frontend` 注入），字段在 `app.py` 里。加字段要同步 `app.js`。folium 变量名用 `get_name()` 动态取，不硬编码 `map_<hash>`。
- 图层 `add_to` 顺序就是渲染层级：risk → heat → 路线（short/safe FeatureGroup）→ avoided。avoided 最后加，红段才不被路线盖住。路线保持命名 FeatureGroup，hover 联动要用它的变量名。
- `avoided_hot_segments(G, on_nodes, off_nodes)` 方向可交换：`(short, safe)` 是避开的，`(safe, short)` 是新进入的，两侧都要看。只报避开的，会在起点本身高危时出现「Avoids 5 … 5 段绕不掉」的自相矛盾。H7 锁住它。
- `entered_count > 0` 时 UI 不说 "Avoids N"，改报 `hot_m` 降幅（`stats.hot_m_cut`）。这时段数净差为 0，米数才站得住。
- `hot_edge_indices(G, nodes)` 是 `hot_blocks` / `hot_m` / `unavoidable` 三处的唯一来源，别再各写一遍遍历。
- 起终点有两条取点路径，都产出坐标：combobox 搜索 `/search`，或显式武装的地图 pick（点 `.pick-btn` 武装，下次地图点击写入并自动解除，未武装时点击什么都不做）。旧 select 下拉和 `routing.snap_to_node` 已删。表单字段契约：前端 `origin_lat/lng/name`、`dest_lat/lng/name`，后端回显 `last_origin_*`、`last_dest_*`。
- 点选起终点有 snap 守卫 `SNAP_MAX_M=300`：点到图外时 `nearest_nodes` 仍会返回远节点，画出怪路线，所以超过 300m 直接拒。改它要同步 H8。判断「四个坐标都有」用 `bool(a and b and c and d)`，不用 `all(s)`（`all('')` 为 True）。
- 风险上色层用 top-N（`_RISK_DRAW_LIMIT`），不用固定阈值，因为百分位排名下固定阈值会命中大半张图。渲染走单个 `folium.GeoJson`，不在循环里造上万个 PolyLine（逐个过模板序列化太慢）。GeoJSON 坐标是 `[lng, lat]`，与 PolyLine 的 `(lat, lng)` 相反。
- `/search` 是信任边界：q 直接进 LIKE，必须经 `_like_escape` 转义 `\ % _`（H10 锁住）。排序是前缀匹配 → landmark>poi>intersection → 离 `(lat,lng)` 近；距离偏置不能去，否则常见街名的候选全是远处路口。places 表缺失（老库）捕获 `OperationalError` 回落 `PRESET_LOCATIONS`，不许 5xx（H11/H12）。纯本地索引，不上 FTS5。

## 部署与外部依赖
- Vercel 部署排除 `data/walk.graphml`（`vercel.json` 的 `excludeFiles`）：它只在 `data/walk.pkl` 缺失或过期时才会被读，上传只会拖慢冷启动。`load_graph` 在它不存在时走 pickle。
- 底图是 OpenFreeMap Positron 矢量瓦片（`app.py` 的 `VectorBasemap`，maplibre-gl-leaflet，`folium.Map(tiles=None)`）。不换回 CARTO：它从 2026-09-25 起无 key 只回「API KEY REQUIRED」图，而且是 HTTP 200，不会报错。H14 锁住 `cartocdn` 不出现。
- maplibre-gl 钉 5.x，6.x 只发 ES 模块，没有插件要的全局 `maplibregl`。换版本前先确认新版的 dist 目录里还有全局版的 maplibre-gl.js。署名要显式传 `customAttribution`。
- 固定区域是 Penn 周边 3.5km（`PENN_CENTER`）。地点搜索只用本地 places 表，不接外部 geocoder：它们的政策或 SLA 不适合 autocomplete，也会破坏断网 demo。代价是搜不到门牌号地址。

## 局限（完整条目在 README.md 和 /about 页，改模型前读一遍）
- 数据和范围：历史犯罪≠未来风险 / 快照非实时 / 固定区域（3.5km）/ 人行道数据轶事性。
- 模型：距离衰减加权模型（单 α）/ 高危路段阈值取 p90 是判断取舍 / 排名归一化丢失量级（最险与较险压成同名次差，但与 p90 阈值同为排名语义）/ 120m buffer 路口重复计入（有意设计）/ 无转弯惩罚 / severity 是判断取舍 / bump 是调参常数 / α 锁定。
- 绕路幅度小（中长程 +40~107m，3% 量级）但路线差异大（54%–93% 路段不同），这是网格几何使然，不是"没分叉"。短程绕路比失真。
- 地点搜索纯本地 places 表：搜不到门牌号地址，要外部 geocoder，而它会破坏断网 demo。
- combobox 候选列表走正常文档流（`position:absolute` 会被 `.panel` 的 `overflow-y:auto` 裁掉），展开时把下面字段推下略有跳动。升级路径 `position:fixed`+`getBoundingClientRect`。
- 地图取点只显示坐标本身，不做反向地理编码。

## 主动建议哪些 skill
只主动建议这组：python-reviewer、code-review、context7。
