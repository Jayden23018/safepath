# SafePath 踩坑记录

`CLAUDE.md` 里每条规则只留一行加一句原因，事故经过、实测数字和日期放在这里。每条按「现象、根因、规则」三段写。规则本身以 `CLAUDE.md` 为准，这里是它的来历。

## 1. 改 GRAPH_DIST_M 后图没变

- 现象：改了半径，路网还是旧的，没有任何报错。
- 根因：`fetch_walk_graph` 命中缓存就短路，不会重抓新半径的图。
- 规则：改 `GRAPH_DIST_M` 后删 `data/walk.graphml`。静默保留旧图是第一故障点。

## 2. 为什么要重算 risk：除以边长的归一化（百分位排名归一化）

- 现象：原来 `risk_normalized` 是 risk density（除以边长）再 log-minmax。`corr(risk, log length) = -0.719`，p90 边里 81% 是 <10m 的过街小段，hot_blocks 被 3-6m 人行道过街段污染。修复前 mean 0.252 / p50 0.226 / p90 0.448。
- 根因：除法让 risk 主要在测「边有多短」而不是「有多危险」。去掉除法后单做 log-minmax 也不行：判别力被压到 α 救不回（天花板 1.227 < 旧 bug 实际 1.249）。
- 规则：`risk_normalized = risk_score.rank(pct=True)`，`risk_score` 不除长度，就是加权犯罪和本身。rank 归一化给 Dijkstra 满动态范围，实测修复后 p50≈0.5 / p90≈0.9，三对 demo 全恢复分叉；hot 边中位长度 44.3m，`corr(risk, log length)` 由 -0.719 转 +0.258。代价是丢失量级，但 p90 阈值本身也是排名切分，语义一致。改它要同步动 `prep_data` 的 H1 自检。
- 连带：固定阈值在百分位排名下失效（`risk>0.4` = 全图 60% 边），所以风险上色层改用 top-N。

## 3. places 表（地点搜索数据契约）

- 契约：`places(name, lat, lng, kind)`，kind ∈ {landmark, poi, intersection}。prep_data 产出：landmark 从 `routing.PRESET_LOCATIONS`（唯一真源，别复制坐标）；poi 用 `ox.features.features_from_point`（整体 try/except 降级 `[]`，Overpass 挂了不阻断）；intersection 按 name 数 ≥2 的路口，label=`" & ".join(sorted(names))` 去重。2026-07-29 实测 10039 行（landmark 12 / poi 5435 / intersection 4592）。
- 现象与根因（POI tag 集）：校园楼多数只有 `building=*` 没有 `amenity`。3.5km 内 1752 个有名 building 里 976 个不带 amenity/shop/leisure/tourism/office，1920 Commons（OSM 名 `Class of 1920 Commons`）、Houston Hall、Harnwell、Du Bois、Levine、Meyerson 全在这批里。
- 规则：POI tag 集必须含 `building`，去掉它校园搜索直接废掉。
- 不收 `public_transport` / `railway`：那 586 个名字是 `16844`、`15186 Broad St & Walnut St` 这种站点 ID，只会污染候选。
- 边的 `name` 可能是 list（OSM 多名路），取 `nm[0]`，否则 set 塞 unhashable。
- POI 取点用 `geometry.representative_point()`，不用 `.centroid`（地理 CRS 抛 UserWarning）。

## 4. crime_points 热力图采样

- 现象：热力图原先每次冷启动 `ORDER BY RANDOM() LIMIT`，是对 10 万+ 行全表随机排序，每次重付。
- 规则：采样在 prep 阶段做一次，`write_crime_points_to_sqlite` 用 `HEATMAP_SAMPLE_N=5000` + 固定 `random_state=0`，表里只有 ≤5000 行，`app.py:_load_crime_points` 直接 `SELECT` 全表。app.py 不碰 pandas，热力图点从 SQLite `crime_points(lat,lng,severity)` 读（与 edge_risk 同模式）。
- 代价：热力图从「每次渲染换一批随机点」变成「固定同一批点」，视觉密度分布不变，展示场景可接受。

## 5. edge_risk 索引与新列

- 索引：`write_risk_to_sqlite` 里 `CREATE INDEX IF NOT EXISTS idx_edge_risk_risk`，服务 `_load_edge_risk_colors` 的 `ORDER BY risk_normalized DESC LIMIT`，全表排序变索引扫描取前 N 条。`load_risk_weights` 的全表 `SELECT`（无 WHERE/ORDER）本来就读全部行，索引帮不上。
- 现象：`edge_risk` 新增 `top_crime_category` / `top_crime_count` 列后，旧库（重跑 prep 前）缺列，每个 `GET /` / `POST /route` 请求都 `sqlite3.OperationalError`。
- 根因：`build_edge_risk_table` 的 groupby 顺手多算「该边最常见案件类型 + 次数」（`value_counts().index[0]` / `.iloc[0]`），供路线说明弹窗标案件类型，不走运行时查询；`load_risk_weights` 的 SELECT 显式列出这两列。
- 规则：升级后重跑 `python3 prep_data.py`。这和「改 GRAPH_DIST_M 后删 walk.graphml」同类，是一次性手动步骤，不加 try/except 静默兜底。
- `avoided_hot_segments` 平行边取值：从产出 risk 的同一条边（`max(..., key=risk)`）取 category/count，不再各字段独立聚合，避免 risk 和 category 报告不一致。
- `app.py:route()` 把 `avoided + remaining`（互斥，不重复计入同一条边）交给 `app.py:incident_types()` 聚合进 `stats.incident_summary`，模板 `templates/index.html` 的 `<dialog id="route-explainer">` 读它渲染类型列表。

## 6. top_crime_count 不能求和（平行边、路径上的连续边都不行）

- 现象：旧代码 `cat_counts[cat] += seg["top_crime_count"]` 沿路段求和，在 10 段路上渲染出 "Fraud (1850)"，且被单边巨大 count（实测上限 1543）带偏排序（2026-08-29 修）。
- 根因：120m buffer 把一个犯罪点分给带内所有边。全库 `sum(crime_count)=11,980,172`，而 bbox 内只有 106,881 个点，一起案件平均落在 112 条边上。跨平行边求和（同一犯罪点被两条平行边重复计数）和沿路径求和是同一个问题，只是方向不同。
- 规则：`incident_summary` 是 `list[str]`（类型名 top 3，无数量）。现按「有多少段以该类型为首位」排名，`app.py:_check_incident_types`（H13）锁死「返回值里没有数量、排名不看 count」。去重后的真实案件数在边级表里导不出来（没有 dc_key），要报数就回 `crime_points` 或原始 CSV 重算，不在 UI 里凑一个。

## 7. Vercel 部署排除 walk.graphml

- 现象：31M 的 graphml 随部署包上传，拖慢冷启动解压。
- 根因：它只在 `data/walk.pkl` 缺失/过期时才会被读到，正常路径从不读。
- 规则：`vercel.json` 的 `functions."app.py".excludeFiles` 排除它。`load_graph` 的 fallback 判断 `not os.path.exists(path) or ...`，graphml 在 Vercel 上不存在时安全走 pickle 路径。只影响 Vercel 构建产物，不影响本地仓库、`prep_data.py`、`flask run`。

## 8. 底图：CARTO 失效，换 OpenFreeMap

- 现象：2026-09-25 起 CARTO 栅格瓦片无 key 只回「API KEY REQUIRED」图，而且是 HTTP 200，服务端和浏览器都不报错（2026-10-14，issue #1）。
- 规则：底图用 OpenFreeMap Positron 矢量瓦片，`app.py` 的 `VectorBasemap` 用 maplibre-gl-leaflet 挂到 Leaflet，`folium.Map(tiles=None)`。H14 锁死 `cartocdn` 不出现。
- maplibre-gl 钉 5.x：6.x 只发 ES 模块，没有插件要的全局 `maplibregl`。换版本前先 curl 一下 `dist/maplibre-gl.js` 在不在。
- 署名要显式传 `customAttribution`，Positron 数据源不带 attribution。

## 9. Fixed area 与地点搜索不接外部 geocoder

- 地点搜索用纯本地 SQLite places 表（路口+POI+地标，全在图内，`SNAP_MAX_M` 守卫不可能触发，断网也能 demo）。
- 不接外部 geocoder：OSM Nominatim 政策禁 autocomplete，Photon 是 demo 服务器无 SLA，且都破坏「不接实时 API」。代价：搜不到门牌号地址。
- 热力图已实现（原「不做热力图」约束已解除，2026-07-28）。

## 10. 数据现状快照（2026-07-28 实测，3.5km + 2024–2026 三年）

这些数字会随数据和参数变，以 `prep_data.py` 的输出为准。

- 路网：26429 节点 / 81324 边。
- 真犯罪：386801 行（dc_key 跨年去重）→ bbox 内 106881 点 → 分到 80550 边（99% 覆盖）。
- crime_points 表：106881 原始点供热力图（后来改为 prep 阶段采样 5000，见第 4 条）。
- risk_normalized（百分位排名后）：mean 0.5，p50 0.5，p90 0.9，p99 0.99，[0,1] 均匀分布（不再 log 长尾）。热点集中在 Mantua / Market 走廊 / Point Breeze。

## 11. 分叉量的是路线重合度，不是绕路比

- 现象：旧结论「绕路 <1% 所以几乎无分叉」。分叉一直都在，是 `classify_divergence` 的旧阈值把它判成 "minor" 弱化掉了（2026-07-29 修正）。
- 根因：量错了指标。曼哈顿网格里换一条平行街走 staircase，几何上天差地别、总长度几乎不变。实测四对 demo 的 `diff_ratio`（=1-Jaccard）是 0.544 / 0.615 / 0.922 / 0.927（54%–93% 的路段不同），绕路只有 +40m~+107m（2026-08-11 复测，绝对米数与旧记录一致）。
- 规则：现按 `diff_ratio = 1 - Jaccard(节点集)` 分类，阈值 `MINOR_DIFF_RATIO = 0.25`。百分比别引旧的「+0.2%~+2.3%」，那是算错的，正确值：106.6/3153.9=3.4%、39.9/1314.7=3.0%、303.7/1711.5=17.7%、65.8/176.8=37.2%。绝对米数才是对外口径。

## 12. 主指标是高危路段数，不是总风险

- 现象：总风险是全路径求和、风险场平滑，两条路只差 1–2%，数字太弱不能当卖点。
- 规则：主指标 = 高危路段数（`hot_blocks`，`risk > p90`，p90=0.9 由 `load_risk_weights` 从 edge_risk 实算存进 `G.graph["risk_p90"]`，不硬编码）。
- 实测：Penn Park→Mill Creek 10→0（+106m）；Van Pelt→30th St 4→0（+40m）；Market St→Mantua 5→5（+303m；段数净差 0，但高危米数 115.1→55.6 = −52%）。
- Market St→Mantua 的原因：起点 Market St Corridor 本身是高犯罪锚点，两路的 5 段高危都在路径前 0-4 段即起点脚下，安全路避开 5 段又踩进另 5 段，任何算法都避不开你正站着的街区。UI 对这种 pair 走米数口径 + 「绕不掉」说明。
- 短程（<500m）没有高危段可避，改用总风险降幅（Van Pelt→Huntsman 降 57%）。

## 13. 绕路比在短程会失真

- 现象：Van Pelt→Huntsman +35.8% 听着吓人，实际是 177m 基线上的 +63m。
- 规则：UI 一律先报绝对米数，百分比放括号。

## 14. 不要用 Yen / K-shortest 做 budget 约束（审计结论）

详见 `docs/superpowers/specs/2026-07-29-detour-tolerance-design.md`。

- `nx.shortest_simple_paths` 带 `@not_implemented_for("multigraph")`，对 osmnx 的 MultiDiGraph 直接抛异常。
- 网格里等长路径数是 C(m+n,n)（10×10 偏移 = 184756 条，detour 0%），budget 剪枝的 break 永不触发。20×20 玩具网格实测 15 秒枚举 3014 条候选仍未离开等长层。
- 真要做长度约束下最小风险（CSP，NP-hard），用拉格朗日松弛：`weight = risk + λ·length` 二分 λ，每轮一次 Dijkstra。但实测最优解就在 2% 附近，budget 花不出去，不值得做。

## 15. 分叉的另一来源是 /report，demo 重心

- 上报一条边后该边 safety_weight ×7.5（`REPORT_BUMP_FACTOR`×severity），安全路径确实绕开它（已验证）。但密集网格下绕一条边成本也低，绕路幅度仍温和。
- demo 重心 = 可视化 + 上报：风险街道上色层 + 热力图层展示「哪里危险」（核心价值，肉眼可见）；/report 演示「上报改变后续路径」。新加 4 个校外高犯罪地标（Mantua / Mill Creek / Point Breeze / Brewerytown）让分叉叙事有真实高/低风险对比。
- `REPORT_BUMP_FACTOR = 5.0`：计划原值 0.3 太弱，密集路网 bump 单条边几乎不可见。

## 16. avoided_hot_segments 方向可交换

- 现象：起终点本身在高危区时，安全路离开 N 段又踩进 M 段（Market St→Mantua 5 进 5 出，段数净差 0）。只报 avoided 会渲染出「Avoids 5 … 5 → 5 … 5 段绕不掉」的自相矛盾。
- 规则：`avoided_hot_segments(G, on_nodes, off_nodes)` 取 on 路独有 + risk>p90 的边。`(short, safe)` 得「避开的」，`(safe, short)` 得「新进入的」，两侧都要看，H7 锁住这个交换契约。平行边约定 risk 取 max、length 取 min（与 `compute_route` 一致）；name 取不到给 None（top 边约 15% 有 name，所以 UI 做聚合+地图高亮，不做街名列表）。
- `entered_count > 0` 时 UI 不说 "Avoids N"，改报 `hot_m` 降幅（`stats.hot_m_cut`，实测 115.1→55.6 m = −52%）。
- `hot_edge_indices(G, nodes)` 返回路径上 risk>p90 的 `[(下标, 长度)]`，是 `hot_blocks` / `hot_m` / app.py 的 `unavoidable`（下标落在前后 10%）三处的唯一来源。

## 17. 起终点取点

- 现象：旧的地图取点「每次点击都交替落 pin」。
- 规则：两条路径都产出坐标：①combobox 搜索 `/search` 选中填 hidden；②显式武装的地图 pick（点 `.pick-btn` 武装 → 下次地图点击写入该字段并自动解除；未武装时 `MAP.on("click")` 第一行 `if (!pickTarget) return` 什么都不做）。`/route` 的旧 select 下拉分支和 `routing.snap_to_node` 已删除（app.py 是唯一调用方），坐标路径是唯一路径（2026-07-29）。
- 表单字段契约：前端填 `origin_lat/origin_lng/origin_name` + `dest_lat/dest_lng/dest_name`，后端回显 `last_origin_label/lat/lng` + `last_dest_label/lat/lng`。
- snap 守卫：`SNAP_MAX_M=300`（app.py）。点到 3.5km 图外时 nearest_nodes 仍返回远节点会画诡异路线，>300m 直接拒。改它要同步 H8 自检。
- `picked` 判定用 `bool(a and b and c and d)`，不能 `all(s)`，因为 `all('')==True`，这是踩过的坑。

## 18. 风险上色层渲染

- 现象：旧方案在 `build_map` 里 for 循环构造 20000 个独立 `folium.PolyLine`，每个各自过一遍 Jinja 模板序列化，是渲染耗时大头。
- 规则：用 top-N（`_RISK_DRAW_LIMIT=20000`，`ORDER BY risk_normalized DESC LIMIT`），渲染走单个 `folium.GeoJson`（`_load_edge_risk_colors` 返回 FeatureCollection，每边一个 LineString feature，颜色存 `properties.color`）。GeoJSON 坐标顺序 `[lng, lat]`，与 PolyLine 的 `(lat, lng)` 相反，别搞反。

## 19. /search 的实现细节

- 契约：`GET /search?q=&lat=&lng=` → `{"results":[{name,lat,lng,kind}]}` 最多 8 条，`q<2` 字符返 12 个 landmark。
- q 直接进 LIKE 必须转义：`_like_escape` 转义 `\ % _`。不转义查 `%` 匹配全表 8937 行（H10 自检锁死）。
- 三级排序：前缀匹配优先 → landmark>poi>intersection → 离 `(lat,lng)` 近的优先。距离偏置是必需的：`spruce` 命中 47 条、`walnut` 38 条，不偏置候选全是 52nd/53rd 街远路口。
- places 表缺失（老库）捕获 `OperationalError` 回落 `PRESET_LOCATIONS` 子串过滤，不许 5xx（H11/H12 自检）。
- `search_places(conn,...)` 是收 connection 的纯函数，便于内存库自检。纯本地索引不上 FTS5：5–8k 行全表扫 <1ms。

## 20. 图层顺序与 SAFEPATH_CTX

- 图层 `add_to` 顺序 = 渲染层级：risk → heat → 路线（short/safe FeatureGroup）→ avoided。avoided 最后 add_to 压最上层，红段才不被路线盖住。
- 路线包进命名 FeatureGroup（short/safe）是为了 hover 联动拿变量名，别改回匿名 PolyLine。
- `SAFEPATH_CTX`（`_inject_frontend` 注入）字段 `{map,risk,heat,avoided,has_avoided,short,safe}`，app.js 据此建 toggle + hover 联动。所有 folium 变量名用 `get_name()` 动态拿，不硬编码 `map_<hash>`。

## 21. 依赖清单

原先 `CLAUDE.md` 抄了一份依赖清单（flask, osmnx>=2.0, networkx, folium, pandas, geopandas, shapely, requests），缺 `scikit-learn>=1.0`。现已删，以 `requirements.txt` 为准。2026-11-16 核对：`requirements.txt` 列了 `scikit-learn>=1.0`，但仓库里没有任何 `sklearn` 引用，疑似未使用的依赖（另开 issue 跟踪）。
