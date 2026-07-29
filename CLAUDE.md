# SafePath — 项目说明

Penn 暑期课程 final project。用 Flask 的 Python web app，串联三周课程：Pandas（数据）→ Graph（路由）→ Flask+HTML（Web）。单人 2 天 MVP。**功能已实现完毕**（2026-07-28）。

## 一句话产品
费城步行安全/无障碍路由：在 Penn 周边 ~3.5km 步行图上叠加"安全数据层"（犯罪历史 + 用户上报的人行道/ADA 问题），找**更安全**的路线（不只是最短），并用可切换的风险图层（街道上色 + 犯罪热力图）让"哪里危险"肉眼可见。

## 怎么跑（这是最常用的）
```bash
python3 -m pip install -r requirements.txt
python3 prep_data.py            # 建图缓存 + 算风险 → data/safepath.db（3.5km 首次 ~30s）
python3 -m flask --app app run  # http://127.0.0.1:5000/
```
只动了 POI tag / 路口逻辑时不用全跑 prep_data（会白算 386k 条犯罪）：
```bash
python3 -c "import prep_data as p;from collections import Counter;G=p.fetch_walk_graph();a=[{'name':n,'lat':float(c[0]),'lng':float(c[1]),'kind':'landmark'} for n,c in p.PRESET_LOCATIONS.items()]+p.build_intersection_places(G)+p.fetch_poi_places();p.write_places_to_sqlite(a);print(dict(Counter(x['kind'] for x in a)))"
```
**改 GRAPH_DIST_M 后必须删 `data/walk.graphml`**：`fetch_walk_graph` 命中缓存就短路，不会重抓新半径的图（静默保留旧图 = 第一故障点）。
自检（无框架，bare assert 在 `__main__`）：
```bash
python3 prep_data.py   # H1 百分位排名归一化 / H4 H4b buffer+距离衰减分配
python3 routing.py     # H2 安全权重公式 / H3 路线分叉 / H6 hot_blocks 计数 / H7 avoided_hot_segments
python3 app.py --check # H5 分叉三态分类（按 diff_ratio）/ H8 点选 snap 距离守卫
```
**注意 pyenv 环境**：本机 `pip` 装进 Python 3.9，但 `python3` 解析到 3.10。装依赖必须用 `python3 -m pip install`，不能裸 `pip install`。

## 关键约束（改之前必须知道）
- **α = 1.5 锁定**。最险边 = 2.5× 真实长度，零犯罪边 = 原长度。改它要同步动 routing.py 的 H2 自检。
- **REPORT_BUMP_FACTOR = 5.0**（计划原值 0.3 太弱，密集路网 bump 单条边几乎不可见）。改它要看 README 局限 #9。
- **CSV 文件名**：OpenDataPhilly 下的是 `<year>_crime_data.csv`，复制成 `data/crime_<year>.csv`。近 3 年 2024–2026（列名一致，已核验）。列名 `point_x/point_y/text_general_code/dispatch_date/dc_key`。改路径要动 `CRIME_CSV_PATHS`。
- **osmnx 2.0.7**（计划写 ≥2.1.1）：`nearest_nodes/nearest_edges` 在 `ox.distance`，**不在** `ox.routing`。
- **prep_data.py 重跑不删用户上报**：`write_risk_to_sqlite` / `write_crime_points_to_sqlite` / `write_places_to_sqlite` 只 `to_sql(replace)` 替换各自表，reported_incidents 用 `CREATE TABLE IF NOT EXISTS` 保留。别加回 `os.remove(db_path)`。
- **`places` 表是地点搜索数据契约**：`places(name, lat, lng, kind)`，kind ∈ {landmark, poi, intersection}。prep_data 产出：landmark 从 `routing.PRESET_LOCATIONS`（**唯一真源，别复制坐标**）、poi 用 `ox.features.features_from_point`（整体 try/except 降级 `[]`，Overpass 挂了不阻断）、intersection 按 name 数 ≥2 的路口 label=`" & ".join(sorted(names))` 去重。2026-07-29 实测 10039 行（landmark 12 / poi 5435 / intersection 4592）。**POI tag 集必须含 `building`**：校园楼多数只有 `building=*` 没有 `amenity`，3.5km 内 1752 个有名 building 里 976 个不带 amenity/shop/leisure/tourism/office —— 1920 Commons（OSM 名 `Class of 1920 Commons`）、Houston Hall、Harnwell、Du Bois、Levine、Meyerson 全在这批里，去掉 building 校园搜索直接废掉。**不收 `public_transport`/`railway`**：那 586 个名字是 `16844`、`15186 Broad St & Walnut St` 这种站点 ID，只会污染候选。**边的 `name` 可能是 list（OSM 多名路），取 `nm[0]` 否则 set 塞 unhashable**。POI 取点用 `geometry.representative_point()` 不用 `.centroid`（地理 CRS 抛 UserWarning）。
- **BUFFER_M = 120 + 距离衰减**：犯罪点按 `decay = 1 - dist/120` 加权分给 buffer 内所有边（NKDE 风格，覆盖相邻 2-3 条平行街）。原 30m 等权太稀疏。
- **百分位排名归一化**：`risk_normalized = risk_score.rank(pct=True)`（risk_score 不除长度，就是加权犯罪和本身）。原来除以边长（risk density）+ log-minmax，除法让 `corr(risk, log length)=-0.719`——risk 主要在测「边有多短」不是「有多危险」，把 3-6m 人行道过街段推成高危污染 hot_blocks；去掉除法后单做 log-minmax 会把判别力压到 α 也救不回（天花板 1.227 < 旧 bug 实际 1.249）。rank 归一化给 Dijkstra 满动态范围（实测修复后 p50≈0.5/p90≈0.9，三对 demo 全恢复分叉）。代价：丢失量级，但 p90 阈值本身也是排名切分，语义一致。改它要同步动 prep_data H1 自检。
- **crime_points 表是热力图层数据契约**：app.py 不碰 pandas，热力图原始犯罪点从 SQLite `crime_points(lat,lng,severity)` 读（与 edge_risk 同模式）。
- **Fixed area**：Penn 周边 3.5km（`PENN_CENTER`）。地点搜索用**纯本地 SQLite places 表**（路口+POI+地标，全在图内、`SNAP_MAX_M` 守卫不可能触发、断网也能 demo）——**不接外部 geocoder**（OSM Nominatim 政策禁 autocomplete、Photon 是 demo 服务器无 SLA，且都破坏"不接实时 API"）。代价：搜不到门牌号地址。热力图**已实现**（原"不做热力图"约束已解除，2026-07-28）。

## 数据现状（2026-07-28 实测，3.5km + 2024–2026 三年）
- 路网：26429 节点 / 81324 边
- 真犯罪：386801 行（dc_key 跨年去重）→ bbox 内 106881 点 → 分到 80550 边（99% 覆盖）
- crime_points 表：106881 原始点供热力图（渲染时 SQL 随机采样 5000 进 HTML，省体量）
- risk_normalized（百分位排名后）：mean 0.5，p50 0.5，p90 0.9，p99 0.99。rank 归一化后 [0,1] 均匀分布（不再 log 长尾）。热点集中在 Mantua / Market 走廊 / Point Breeze。修复前（除长度+log-minmax）是 mean 0.252/p50 0.226/p90 0.448，但 p90 边里 81% 是 <10m 的过街小段——测的是"边有多短"。修复后 hot 边中位长度 44.3m、corr(risk,log length) 由 -0.719 转 +0.258。
- **分叉量的是路线重合度，不是绕路比**（2026-07-29 修正）。旧结论"绕路 <1% 所以几乎无分叉"是**量错了指标**：曼哈顿网格里换一条平行街走 staircase，几何上天差地别、总长度几乎不变。实测两条路的节点重合度只有 15%–52%（即 48%–85% 的路段不同），绕路却仅 +0.2%~+2.3%。分叉一直都在，是 `classify_divergence` 的旧阈值把它判成 "minor" 弱化掉了。现按 `diff_ratio = 1 - Jaccard(节点集)` 分类，阈值 `MINOR_DIFF_RATIO = 0.25`。
- **主指标 = 高危路段数（hot_blocks）**，不是总风险。总风险是全路径求和、风险场平滑，两条路只差 1–2%（数字太弱不能当卖点）；高危路段数（`risk > p90`，p90=0.9 由 `load_risk_weights` 从 edge_risk 实算存进 `G.graph["risk_p90"]`，不硬编码）差距显著：Penn Park→Mill Creek **10→0**（+106m）、Van Pelt→30th St **4→0**（+40m）、Market St→Mantua 5→5（+303m；段数净差 0 但**高危米数 115.1→55.6 = −52%**——起点 Market St Corridor 本身是高犯罪锚点，两路的 5 段高危都在路径前 0-4 段即起点脚下，安全路避开 5 段又踩进另 5 段，任何算法都避不开你正站着的街区。UI 对这种 pair 走米数口径 + "绕不掉"说明）。短程（<500m）没有高危段可避，改用总风险降幅（Van Pelt→Huntsman 降 57%）。
- **绕路比在短程会失真**：Van Pelt→Huntsman +35.8% 听着吓人，实际是 177m 基线上的 +63m。UI 一律先报绝对米数，百分比放括号。
- **不要用 Yen / K-shortest 做 budget 约束**（审计结论，见 `docs/superpowers/specs/2026-07-29-detour-tolerance-design.md`）：① `nx.shortest_simple_paths` 带 `@not_implemented_for("multigraph")`，对 osmnx 的 MultiDiGraph 直接抛异常；② 网格里等长路径数是 C(m+n,n)（10×10 偏移=184756 条，detour 0%），budget 剪枝的 break 永不触发——20×20 玩具网格实测 15 秒枚举 3014 条候选仍未离开等长层。真要做长度约束下最小风险（CSP，NP-hard），用拉格朗日松弛：`weight = risk + λ·length` 二分 λ，每轮一次 Dijkstra。但实测最优解就在 2% 附近，budget 花不出去，**不值得做**。
- **分叉的另一来源是 /report**：上报一条边后该边 safety_weight ×7.5（REPORT_BUMP_FACTOR×severity），安全路径确实绕开它（已验证）。但密集网格下绕一条边成本也低，绕路幅度仍温和。
- **demo 重心 = 可视化 + 上报**：风险街道上色层 + 热力图层展示"哪里危险"（核心价值，肉眼可见）；/report 演示"上报改变后续路径"。新加 4 个校外高犯罪地标（Mantua / Mill Creek / Point Breeze / Brewerytown）让分叉叙事有真实高/低风险对比。

## 评分叙事映射（改功能时别破坏）
- **Pandas**：prep_data.py（CSV→DataFrame→groupby 边→severity×距离衰减加权→百分位排名归一化→to_sql）
- **Graph**：prep_data.py(建图+buffer 距离衰减分配) + routing.py(两权重下 nx.shortest_path 跑 Dijkstra 分叉)
- **Flask+HTML**：app.py + templates（GET 渲染；POST /route 计算+重渲染；POST /report 持久化 SQLite 改变后续行为；folium 双风险图层+LayerControl 经 Jinja `|safe` 嵌入）
- **三个 point**：持久化=SQLite / 有意义 POST=/report 改状态 + /route 触发计算 / 部署=未来工作

## 架构边界
- `routing.py` 运行时**不碰 pandas**，只读 SQLite。
- `app.py` 不直接碰图算法细节，调 routing。
- MultiDiGraph 平行边求和用 `min(weight)`（与 Dijkstra 选边一致），别改回 key=0。
- 无 tests/ 目录，自检全放 `__main__`。非平凡逻辑必须有 bare assert。
- **`SAFEPATH_CTX` 是前后端契约**（`_inject_frontend` 注入）：字段 `{map,risk,heat,avoided,has_avoided,short,safe}`，app.js 据此建 toggle + hover 联动。加字段要同步 app.js；所有 folium 变量名用 `get_name()` 动态拿，绝不硬编码 `map_<hash>`。
- **图层 add_to 顺序=渲染层级**：risk → heat → 路线(short/safe FeatureGroup) → avoided。avoided 必须**最后 add_to** 压最上层（红段不被路线盖住）。路线现已包进命名 FeatureGroup（short/safe），是为 hover 联动拿变量名——别改回匿名 PolyLine。
- **`avoided_hot_segments(G, on_nodes, off_nodes)`**（routing.py）：取 on 路独有 + risk>p90 的边。**方向可交换**——`(short, safe)` 得"避开的"，`(safe, short)` 得"新进入的"，两侧都必须看：起终点本身在高危区时安全路离开 N 段又踩进 M 段（Market St→Mantua 5 进 5 出，段数净差 0），只报 avoided 会渲染出 "Avoids 5 … 5 → 5 … 5 段绕不掉" 的自相矛盾。H7 锁住这个交换契约。平行边约定 risk 取 max、length 取 min（与 `compute_route` 一致）；name 取不到给 None（top 边 ~15% 有 name，故 UI 做聚合+地图高亮，不做街名列表）。
- **`hot_edge_indices(G, nodes)`**（routing.py）：路径上 risk>p90 的 `[(下标, 长度)]`，是 `hot_blocks` / `hot_m` / app.py 的 `unavoidable`（下标落在前后 10%）三处的**唯一**来源，别再各写一遍遍历。
- **`entered_count > 0` 时 UI 不许说 "Avoids N"**，改报 `hot_m` 降幅（`stats.hot_m_cut`，实测 115.1→55.6 m = −52%）。段数在那种场景净差为 0，米数才站得住。
- **起终点取点有两条路径，都产出坐标**（2026-07-29）：①combobox 搜索 `/search` 选中填 hidden；②显式武装的地图 pick（点 `.pick-btn` 武装 → 下次地图点击写入该字段并自动解除，未武装时 `MAP.on("click")` 第一行 `if (!pickTarget) return` **什么都不做**——根治旧"每次点击都交替落 pin"的硬伤）。`/route` 的旧 select 下拉分支 + `routing.snap_to_node` **已删除**（app.py 是唯一调用方），坐标路径成唯一路径。表单字段契约：前端填 `origin_lat/origin_lng/origin_name` + `dest_lat/dest_lng/dest_name`，后端回显 `last_origin_label/lat/lng` + `last_dest_label/lat/lng`。
- **点选起终点 snap 守卫**：`SNAP_MAX_M=300`（app.py）。点到 3.5km 图外时 nearest_nodes 仍返回远节点会画诡异路线，>300m 直接拒。改它要同步 H8 自检。`picked` 判定用 `bool(a and b and c and d)`，**不能 `all(s)`——`all('')==True`** 是踩过的坑。
- **风险上色层用 top-N**（`_RISK_DRAW_LIMIT=20000`，`ORDER BY risk_normalized DESC LIMIT`），不再用固定阈值——百分位排名下固定阈值会失效（`risk>0.4` = 全图 60% 边）。
- **`/search` 是信任边界**（2026-07-29）：`GET /search?q=&lat=&lng=` → `{"results":[{name,lat,lng,kind}]}` 最多 8 条，`q<2` 字符返 12 个 landmark。**q 直接进 LIKE 必须转义** `_like_escape` 转义 `\ % _`（不转义查 `%` 匹配全表 8937 行，H10 自检锁死）。三级排序：前缀匹配优先 → landmark>poi>intersection → 离 `(lat,lng)` 近的优先。**距离偏置是必需的**：`spruce` 命中 47 条、`walnut` 38 条，不偏置候选全是 52nd/53rd 街远路口。places 表缺失（老库）捕获 `OperationalError` 回落 `PRESET_LOCATIONS` 子串过滤，**不许 5xx**（H11/H12 自检）。`search_places(conn,...)` 是收 connection 的纯函数便于内存库自检。纯本地索引不上 FTS5：5–8k 行全表扫 <1ms。

## 依赖（已固定下限）
flask, osmnx>=2.0, networkx, folium, pandas, geopandas, shapely, requests。geopandas/shapely 随 osmnx 装上。

## 局限（完整条目在 README.md + /about 页，改模型前读一遍）
历史犯罪≠未来风险 / 快照非实时 / 固定区域（3.5km）/ 人行道数据轶事性 / 距离衰减加权模型（单 α）/ **绕路幅度小（<2.5%）但路线差异大（48%–85% 路段不同）——网格几何使然，不是"没分叉"** / 高危路段阈值取 p90 是判断取舍 / **排名归一化丢失量级（最险与较险压成同名次差，但与 p90 阈值同为排名语义）** / 短程绕路比失真 / 120m buffer 路口重复计入（有意设计）/ 无转弯惩罚 / severity 是判断取舍 / bump 是调参常数 / α 锁定 / **地点搜索纯本地 places 表（搜不到门牌号地址，需外部 geocoder，破坏断网 demo）** / **combobox 候选列表走正常文档流（`position:absolute` 会被 `.panel` 的 `overflow-y:auto` 裁掉），展开时把下面字段推下略有跳动，升级路径 `position:fixed`+`getBoundingClientRect`** / **地图取点显示坐标本身不做反向地理编码**。
