# SafePath 技术细节文档

> 供对外材料（推荐信 / 个人陈述 / 项目答辩）引用。
> 所有数字均为 **2026-08-11 在本机实测**（重跑 `prep_data.py` 产出的 `data/safepath.db` + 现场跑路由），
> 不是设计文档里的估计值。凡是我没实测的，都会写明"未实测"。

---

## 0. 速查：你问的五个问题

| 问题 | 事实 |
|---|---|
| **犯罪数据来源 / 时间范围 / 记录数** | 费城市政开放数据 **OpenDataPhilly — Crime Incidents**（底层是 PHL Carto SQL API，表 `incidents_part1_part2`，数据属主为费城警察局 PPD）。下载为三份年度 CSV（2024 / 2025 / 2026）。原始 **397,464** 行，`dispatch_date` 跨度 **2023-12-31 → 2026-07-27**；按 `dc_key` 跨年去重 + 丢弃无坐标行后 **386,801** 行；再按 Penn 中心 ±3.5 km 的 bbox 过滤，进入模型的是 **106,881** 个犯罪点。 |
| **最短路用什么算法** | **Dijkstra**，不是 A\*。调用 `networkx.shortest_path(G, o, d, weight=...)`；NetworkX 3.4.2 在同时给定 source 和 target 且 `method="dijkstra"`（默认）时，内部分派到 **`nx.bidirectional_dijkstra`**（双向 Dijkstra）。同一张图跑两次，只换边权：`weight="length"` 得最短路，`weight="safety_weight"` 得最安全路。 |
| **危险指数有没有具体权重** | 有，而且是**两阶段**而非"三因子加权求和"。① 边级风险：`Σ(severity × decay)`，`severity` 是 15 类犯罪的手工权重表（Homicide 10.0 … Fraud 1.0，缺省 1.0），`decay = 1 − 距离/120 m` 的线性距离衰减；② 混进路径代价：`safety_weight = length × (1 + α × risk_normalized)`，**α = 1.5 锁定**（最险边=2.5× 真实长度，零犯罪边=原长度）。用户上报再叠一层乘性惩罚 `× (1 + 5.0 × severity_type)`。 |
| **后端数据库是什么** | **SQLite**（单文件 `data/safepath.db`，9.3 MB），Python 标准库 `sqlite3` 直连，无 ORM。四张表：`edge_risk`(80,550) / `crime_points`(5,000) / `places`(10,039) / `reported_incidents`。 |
| **定位是浏览器 Geolocation API 吗** | **是，但只用在一个地方**：上报表单的 "Use my location" 按钮调 `navigator.geolocation.getCurrentPosition`（`enableHighAccuracy: true, timeout: 10s, maximumAge: 30s`），把当前经纬度填进上报表单并把地图居中。**路由的起终点不走 Geolocation**——起终点来自本地地点搜索（`GET /search`）或"武装式地图点选"。浏览器不支持或用户拒绝授权时按钮禁用/静默降级到手输坐标，功能不挂。 |

---

## 1. 系统总览

一句话：在 Penn 周边 3.5 km 的 OSM 步行图上叠一层"安全数据层"（历史犯罪 + 用户上报的人行道/无障碍问题），
用**两套边权跑两次 Dijkstra**，把"最短"和"最安全"同时画出来，并用可切换的风险图层让"哪里危险"肉眼可见。

```
                    ┌─── 离线（跑一次，~30 s）────────────────────────────┐
OpenDataPhilly CSV ─┤ prep_data.py                                      │
OpenStreetMap ──────┤  抓步行图 → 读 CSV → 去重 → bbox → 120m buffer 空间 │
                    │  join + 距离衰减 → groupby 边 → 严重度加权求和 →     │
                    │  百分位排名归一化 → to_sql                          │
                    └───────────────┬───────────────────────────────────┘
                                    ↓
                        data/safepath.db (SQLite)
                        data/walk.graphml + walk.pkl
                                    ↓
                    ┌─── 在线（每请求）──────────────────────────────────┐
                    │ routing.py  读 SQLite → 设 safety_weight → Dijkstra│
                    │ app.py      Flask 路由 + folium 出图 + Jinja 渲染  │
                    │ static/app.js 图层 toggle / 搜索 / 地图取点 / 定位  │
                    └───────────────────────────────────────────────────┘
```

**关键的架构边界（有意维持的三条）：**

1. `routing.py` **运行时不碰 pandas**——pandas 只活在离线的 `prep_data.py` 里，在线只读 SQLite。
   这让 web 进程的内存和冷启动时间与犯罪数据规模脱钩（38 万行 CSV 不进 web 进程）。
2. `app.py` **不直接碰图算法**，只调 `routing` 的函数；图论细节（平行边聚合、p90 阈值、路径差集）全在 `routing.py`。
3. `hot_edge_indices()` 是"路径上哪些段是高危"的**唯一实现**，`hot_blocks` 计数、高危米数、
   "绕不掉的段在不在端点附近"三处共用它——避免同一把尺子写三遍后悄悄漂移。

代码规模：`app.py` 715 行 / `prep_data.py` 515 行 / `routing.py` 353 行 / `static/app.js` 538 行 /
模板 398 行。单人完成，14 次提交。

---

## 2. 技术栈与实测版本

| 层 | 选型 | 本机实测版本 |
|---|---|---|
| 语言 | Python | 3.10.9（注意：本机 `pip` 指向 3.9，装依赖必须 `python3 -m pip install`） |
| Web | Flask | 3.1.3 |
| 图论 | NetworkX | 3.4.2 |
| 路网获取 | OSMnx | 2.0.7 |
| 数据处理 | pandas | 2.3.3 |
| 空间计算 | GeoPandas / Shapely | 1.1.4 / 2.1.2 |
| 地图渲染 | folium（内含 Leaflet） | 0.20.0 |
| 持久化 | SQLite（`sqlite3` 标准库） | 随 Python |
| 底图 | CARTO light_all 瓦片 | — |
| 部署 | Vercel Serverless（`vercel.json`，`maxDuration: 60`） | — |

> **踩过的版本坑**：OSMnx 2.0.7 里 `nearest_nodes` / `nearest_edges` 在 `ox.distance` 下，
> **不在** `ox.routing`（计划书按 ≥2.1.1 写的路径是错的）；`ox.features.features_from_point` 在 `ox.features` 下。
> GeoPandas 1.1 的 `sjoin` 把右表 MultiIndex 展开成 `u/v/key` 普通列，旧版是 `index_right` 元组——代码里两种都兼容。

---

## 3. 数据层

### 3.1 犯罪数据

- **来源**：OpenDataPhilly 的 Crime Incidents 数据集（费城警察局 PPD 出数，市政开放数据平台分发；
  底层查询接口是 `phl.carto.com` 的 Carto SQL API，表名 `incidents_part1_part2`）。
- **获取方式**：按年下载 CSV 静态快照，落到 `data/crime_<year>.csv`。**不接实时 API**——
  这是有意的：demo 断网也能跑，且不给评委看一个"依赖第三方 SLA"的演示。
- **规模（实测）**：

  | 阶段 | 行数 |
  |---|---|
  | 三份原始 CSV（2024 / 2025 / 2026） | 160,388 + 152,555 + 84,521 = **397,464** |
  | `dc_key` 去重 + 丢弃缺经纬度行 | **386,801** |
  | Penn 中心 ±3.5 km bbox 内 | **106,881** |
  | 成功分配到某条街道边上 | 覆盖 **80,550 / 81,324 条边（99.0%）** |
  | 热力图预采样（固定 seed） | **5,000** |

- **时间跨度（实测 `dispatch_date`）**：2023-12-31 → 2026-07-27（≈ 2 年 7 个月）。
  三份年度文件在年末/年初各有一天重叠，这正是要按 `dc_key`（案件唯一号）去重的原因。
- **用到的列**：`lat` / `lng`（若缺则回退 `point_y` / `point_x`）、`text_general_code`（案件类型）、
  `dispatch_date`、`dc_key`。`pd.read_csv(usecols=callable)` 只读这几列，不把 42 MB × 3 的全宽表读进内存。
- **区域内案件类型分布（bbox 内 top 8，实测）**：Thefts 30,649 / Other Assaults 18,760 /
  All Other Offenses 10,513 / Vandalism 9,019 / Motor Vehicle Theft 8,024 / Theft from Vehicle 6,302 /
  Fraud 6,120 / Aggravated Assault No Firearm 3,835。
- **降级路径**：CSV 全部缺失或没有坐标列时，`_synthetic_crime()` 生成 ~20 个 Penn 周边合成点，
  让整条 pipeline 仍能跑通（评审环境没有 100 MB CSV 时不至于直接崩）。

### 3.2 步行路网图

- `osmnx.graph_from_point(center=(39.9526, -75.1932), dist=3500, network_type="walk")`。
- **实测规模：26,429 节点 / 81,324 边**（`MultiDiGraph`，含平行边）。
- **双缓存**：`data/walk.graphml`（31 MB，权威源）+ `data/walk.pkl`（11.7 MB，预解析缓存）。
  运行时优先读 pkl（XML 解析 2.2 s → 反序列化 ~0.5 s），pkl 比 graphml 旧时自动重建。
  这是为 Vercel serverless 的 10 s 冷启动上限做的——`pickle` 只反序列化本地产出的派生缓存，
  从不接收外部输入，所以"pickle 任意代码执行"的风险面在这里不成立（这一点在代码注释里写死了，
  免得后人看到 `pickle.load` 就误判成漏洞）。
- **一次性手动步骤（写进 CLAUDE.md 的坑）**：改了 `GRAPH_DIST_M` 必须**先删 `walk.graphml`**，
  否则 `fetch_walk_graph` 命中缓存直接短路，静默用旧半径的图——这是那种"看起来跑通了其实全错"的第一故障点。

### 3.3 地点索引 `places`（起终点搜索的数据源）

**实测 10,039 行**，三种 kind：

| kind | 条数 | 来源 |
|---|---|---|
| `landmark` | 12 | `routing.PRESET_LOCATIONS`（唯一真源，坐标不复制第二份） |
| `poi` | 5,435 | `ox.features.features_from_point`，tag 集 `amenity/shop/leisure/tourism/office/**building**` |
| `intersection` | 4,592 | 图节点中相邻边 name 数 ≥2 的路口，label = `" & ".join(sorted(names))` 去重 |

两个非显然的决策：

- **`building` 这个 tag 必须收**。实测 3.5 km 内 1,752 个有名字的 building 里，有 **976 个**不带
  `amenity/shop/leisure/tourism/office` 任何一个——1920 Commons（OSM 名 `Class of 1920 Commons`）、
  Houston Hall、Harnwell、Du Bois、Levine、Meyerson 全在这 976 个里。去掉 `building`，校园搜索直接废掉。
- **`public_transport` / `railway` 不收**。那 586 个"名字"长这样：`16844`、`15186 Broad St & Walnut St`——
  是站点 ID，进库只会污染候选列表。

---

## 4. 风险模型（项目的技术核心）

### 4.1 空间分配：120 m 缓冲带 + 线性距离衰减

对每条街道边画 **120 m 缓冲带**，用 GeoPandas `sjoin(predicate="within")` 找出落在带内的所有犯罪点，
再逐点算它到该边**原始线几何**的真实垂直距离，按

```
decay = clip(1 − dist_m / 120, 0, 1)
```

线性衰减加权。贴着这条街发生的案件算满权重，缓冲带边缘衰减到 0。

**为什么是 120 m 而不是 30 m**：费城街区宽约 50 m。30 m 等权分配只覆盖最近的那一条街，
风险场极其稀疏、边界锯齿状；120 m + 衰减能让相邻 2–3 条平行街也吃到信号，
更接近网络核密度估计（NKDE，文献里常用带宽 ~300 m）的做法，同时保留"越近越重"的物理直觉。

**重叠区有意重复计入**：路口附近一个点会同时落进多条边的缓冲带，每条都记一次。
这不是 bug 而是设计——路口本来就是风险更高的地方，这个模型让路口自然地被加权更高（`/about` 页写明了这条）。

### 4.2 严重度权重表（15 类，判断取舍非经验推导）

| 类型 | 权重 | 类型 | 权重 |
|---|---|---|---|
| Homicide | 10.0 | Burglary Residential | 3.0 |
| Aggravated Assault Firearm | 8.0 | Motor Vehicle Theft | 3.0 |
| Robbery Firearm | 7.0 | Theft from Vehicle | 2.5 |
| Aggravated Assault No Firearm | 6.0 | Thefts | 2.0 |
| Robbery No Firearm | 5.0 | Vandalism/Criminal Mischief | 1.5 |
| Weapon Violations | 5.0 | Narcotic / Drug Law Violations | 1.5 |
| Other Assaults | 4.0 | Fraud / Disorderly Conduct | 1.0 |
| | | **未列出的类型（缺省）** | **1.0** |

设计原则：暴力 > 财产 > 治安。这是**明确标注为主观取舍**的一环，在 README 和 `/about` 的局限清单里点名了
（"severity 是判断取舍，不是从伤害数据反推的"）——比假装它有客观依据更诚实。

### 4.3 边风险分与归一化（踩过两个坑，第三版才对）

```python
weighted_crime_sum = Σ(severity × decay)          # groupby(u, v, k)
risk_score         = weighted_crime_sum           # ← 不除以边长
risk_normalized    = risk_score.rank(pct=True)    # 百分位排名，落在 (0, 1]
```

这行"不除以边长"和"用排名而非 min-max"是整个模型里最贵的两个教训：

**坑一：除以边长（risk density）把模型变成了"边有多短"的探测器。**
原实现是 `risk_score = weighted_crime_sum / length_m`，直觉上是"单位长度风险密度"。
但 120 m 缓冲带决定了一条边收到多少犯罪量**与它自身长度几乎无关**
（实测 `corr(weighted_crime_sum, length) = 0.181`），于是除以长度 ≈ 乘以 `1/length`，
`risk_normalized` 主要在测量长度的倒数——实测 `corr(risk, log length) = −0.719`。
后果是几米长的人行道过街小段被推成"高危"，p90 高危边里 **81% 是 <10 m 的过街段**，
污染 `hot_blocks` 计数和整个风险上色图层。去掉除法后：高危边中位长度 **44.3 m**，
`corr(risk, log length)` 从 −0.719 转为 **+0.258**。

**坑二：只去掉除法、保留 log + min-max 归一化，判别力被压死。**
犯罪量是长尾分布，log 压缩后所有边挤在 0.55–0.68 的窄带里，
惩罚上限只有 **1.227×**——比修复前那个有 bug 的版本实际达到的 1.249× 还低，调 α 也救不回来，
三对 demo 起终点全部不再分叉。

**第三版：百分位排名归一化。** `rank(pct=True)` 与分布形状完全无关，把 (0,1] 的动态范围原样交给 Dijkstra。
实测归一化后 min 1.86e-5 / p50 0.500 / **p90 0.900** / p99 0.990 / max 0.99999（均匀分布，符合排名的定义）。
**代价**：丢失量级信息——"最险的街区"和"比较险的街区"被压成相邻名次。
但项目里高危阈值本身就是 p90（一个排名切分），两者语义一致，所以这个代价是自洽的，
且写进了局限清单。

### 4.4 边权重公式

```
safety_weight = length_m × (1 + α × risk_normalized),   α = 1.5（锁定）
```

- `risk_normalized → 0`：`safety_weight = length`（零犯罪街道按真实长度计）
- `risk_normalized → 1`：`safety_weight = 2.5 × length`（最险的街走 100 m 记作 250 m）

α 的含义就是"你愿意为绕开最险的街多走多少倍路"。锁定为 1.5 的理由：
2.5× 的上限足以在密集网格里撬动路径切换（实测四对起终点全部分叉），
再高就会开始产生用户直觉上"绕得莫名其妙"的路线。这个常数有对应的自检（`routing.py` 的 H2），
改 α 会让自检变红，逼迫改动者同步更新文档。

### 4.5 用户上报的乘性惩罚

```
safety_weight *= (1 + REPORT_BUMP_FACTOR × severity[type]),  REPORT_BUMP_FACTOR = 5.0
```

| 上报类型 | severity | 实际倍数 |
|---|---|---|
| `sidewalk_damage` | 1.5 | ×8.5 |
| `unsafe_crossing` | 1.3 | ×7.5 |
| `no_ada_ramp` | 1.2 | ×7.0 |
| `poor_lighting` | 1.0 | ×6.0 |

**为什么是 5.0 而不是计划书里的 0.3**：密集网格里替代路径几乎等长，
`×1.3` 的惩罚根本撬不动 Dijkstra 的选择——上报了但路线不变，用户看到的就是"这个功能是假的"。
5.0 是让单条上报能真实改变安全路径节点序列的经验值。它是一个**调参常数**，在局限清单里明说了。

上报落到具体某条边的方式：`ox.distance.nearest_edges(G, X=lng, Y=lat)`。
坐标完全在图外时捕获异常跳过，不让一条脏上报打挂整张图的赋权。

---

## 5. 路由算法

### 5.1 两次 Dijkstra

```python
weight = "safety_weight" if mode == "safest" else "length"
nodes  = nx.shortest_path(G_w, origin_id, dest_id, weight=weight)
```

- NetworkX 3.4.2 的 `shortest_path` 在 `method="dijkstra"`（默认）且 source/target 都给定时，
  内部走 **`nx.bidirectional_dijkstra`**（源码实测确认，非记忆）。
- **为什么不用 A\***：A\* 需要一个可采纳（admissible）的启发式。对 `length` 权重可以用大圆距离，
  但对 `safety_weight` 这个"长度 × 风险惩罚"的权重，任何基于地理距离的启发式都可能高估真实代价
  （因为路径代价 ≥ 距离，但风险乘子是各边独立的，构造可采纳启发式要额外做下界估计），
  会破坏最优性保证。**规模也用不上**：81,324 条边、单机毫秒级，双向 Dijkstra 完全够。
  为了不可测的常数级加速引入正确性风险，是典型的负收益。
- **为什么不用 Yen / K-shortest paths 做"绕路预算"约束**（这是被明确否决的方案，
  审计记录在 `docs/superpowers/specs/2026-07-29-detour-tolerance-design.md`）：
  ① `nx.shortest_simple_paths` 带 `@not_implemented_for("multigraph")` 装饰器，
  对 OSMnx 的 `MultiDiGraph` 直接抛异常；
  ② 网格图里等长路径的数量是组合爆炸的 `C(m+n, n)`——10×10 的偏移就有 184,756 条 detour = 0% 的路径，
  预算剪枝的 `break` 永远触发不了。20×20 玩具网格实测枚举 15 秒、3,014 条候选，
  **仍未离开"等长层"**。
  真要做"长度约束下的最小风险"（这是 NP-hard 的 CSP 问题），正解是拉格朗日松弛：
  `weight = risk + λ·length`，二分 λ，每轮一次 Dijkstra。但实测最优解就在绕路 2–3% 附近，
  预算根本花不出去——**做了也没有收益，所以不做**。

### 5.2 MultiDiGraph 平行边的聚合约定

OSM 里两个节点之间可能有多条平行边（不同 key）。全项目统一约定：

- **length 取 `min`**（与 Dijkstra 实际会选的那条一致）
- **risk 取 `max`**（保守，宁可高估风险也不漏判高危段）
- `avoided_hot_segments` 里的犯罪类型标签，**从产出 max risk 的那条边上取**，
  而不是各字段独立聚合——否则会报出"风险来自 A 边、案件类型来自 B 边"的自相矛盾结果。
- `top_crime_count` **不跨平行边求和**：120 m 缓冲带会让同一个犯罪点被两条平行边各计一次，求和等于重复计数。

这条约定有专门的自检（H6：平行边 key=0 低危 / key=1 高危时必须判为高危，写 `key=0` 会漏判）。

### 5.3 高危阈值 p90：跟着数据走，不硬编码

`load_risk_weights` 在赋权时顺手把全库 `risk_normalized` 的 90 分位存进 `G.graph["risk_p90"]`
（实测 **0.9000062**）。这样重跑 `prep_data.py` 换了年份或区域，阈值自动跟着变。
数据库缺失时 p90 默认 1.0——`risk ≤ 1` 恒成立，于是"零高危段"，不会误报（H6 锁死这个兜底）。

---

## 6. 指标体系：怎么证明"更安全"不是嘴上说说

这部分是项目里被返工最多的地方，因为**最直觉的两个指标都是错的**。

**错误指标一：总风险降幅。** 总风险是全路径求和，风险场又平滑，两条路的总风险只差 1–2%——
数字太弱，当卖点站不住。

**错误指标二：绕路比。** 一开始用"绕路 < 1%"得出结论"几乎没有分叉"。**这是量错了指标**：
曼哈顿网格里换一条平行街走 staircase，几何上天差地别，总长度却几乎不变。分叉一直都在，
是旧的分类阈值把它判成 "minor" 弱化掉了。

**现在的主指标：**

| 指标 | 定义 | 为什么可信 |
|---|---|---|
| **`hot_blocks`** | 路径上 `risk > p90` 的路段数 | 差距显著（实测 10→0、4→0），不像总风险被摊薄 |
| **`hot_m`** | 那些高危路段的总米数 | 段数净差为 0 时（起终点本身在高危区）唯一站得住的数字 |
| **`diff_ratio`** | `1 − Jaccard(节点集)`，即"安全路有多少段不走最短路" | 直接量路线重合度，不受网格等长性质干扰 |
| `detour_m` / `detour_ratio` | 绝对米数 + 百分比 | 只用来讲"代价多小"，**不再参与分叉判定** |
| `avoided` / `entered` | 最短路独有的高危段 / 安全路新进入的高危段 | 见下方"诚实性"一节 |
| `remaining` | 安全路自己仍经过的所有高危段 | "safest" ≠ "零风险" |
| `unavoidable` | 落在路径前/后 10% 的高危段数 | 起终点本身是高犯罪锚点时，那段谁也绕不掉 |
| `incident_summary` | `avoided + remaining` 按犯罪类型聚合取 top 3 | 告诉用户"绕开的是什么类型的案件" |

**三态分叉分类**（`classify_divergence`，纯函数，有自检 H5）：

- `none`：节点序列完全相同 → 本区域风险差异小，直接推荐最短路
- `minor`：`diff_ratio < 0.25` → 隔街级微绕，UI 弱化
- `clear`：真分叉，高亮避开的高危路段

阈值 0.25 的依据：实测四对起终点的不同路段占比是 54%/61%/92%/93%，全部落进 `clear`；
真·隔街微绕的 `diff_ratio` 通常 <15%，0.25 给两边都留了余量。

**诚实性设计（我认为这是项目里最值得说的一点）：**
`avoided_hot_segments(G, on, off)` 的参数是**可交换**的——`(short, safe)` 得到"避开的段"，
`(safe, short)` 得到"新踩进去的段"。**两侧都必须看**：当起点本身就在高犯罪锚点上时
（实测 Market St Corridor → Mantua），安全路离开 5 段高危、又踩进另外 5 段，
段数净差是 0。只报 avoided 一侧，UI 就会渲染出 "Avoids 5 … 5 → 5" 这种自相矛盾的话。
所以代码里规定：**`entered_count > 0` 时 UI 不许说 "Avoids N"**，改报高危米数降幅
（实测 115.1 → 55.6 m，−52%）。这条契约由自检 H7 锁死。

---

## 7. 持久化：SQLite

选 SQLite 而不是 MySQL/Postgres 的理由：单文件、零运维、随仓库走、能塞进 serverless 部署包，
而且这个项目的写入是"偶尔一条用户上报"，读是"启动时全表扫一遍 + 少量索引查询"——
并发写从来不是瓶颈。**用标准库 `sqlite3` 直连，不上 ORM**：四张表、七八条 SQL，
引入 SQLAlchemy 只会增加一层要读的东西。

| 表 | 行数（实测） | 内容 | 谁写 |
|---|---|---|---|
| `edge_risk` | 80,550 | `u,v,k, crime_count, weighted_crime_sum, length_m, risk_score, risk_normalized, top_crime_category, top_crime_count` | `prep_data.py` |
| `crime_points` | 5,000 | `lat, lng, severity`，热力图数据源（prep 阶段固定 seed 采样） | `prep_data.py` |
| `places` | 10,039 | `name, lat, lng, kind` | `prep_data.py` |
| `reported_incidents` | 动态（当前 2） | `id, lat, lng, type, note, created_at` | **`POST /report`（运行时）** |

**幂等性设计**：`prep_data.py` 重跑时对前三张表用 `to_sql(if_exists="replace")` 各自替换，
`reported_incidents` 用 `CREATE TABLE IF NOT EXISTS` 保留——**重跑数据管道不会清掉用户上报**。
（早期版本是 `os.remove(db_path)` 整库删除，这是明确移除掉的反模式。）

**表级约束不下放到应用层**：`type` 列带 `CHECK (type IN (...))`，
即使有人绕过 Flask 直接写库也塞不进非法类型。

**索引**：`CREATE INDEX idx_edge_risk_risk ON edge_risk(risk_normalized DESC)`，
服务风险上色图层的 `ORDER BY risk_normalized DESC LIMIT 20000`——把 80,550 行的全表排序变成索引扫描。
（注意：`load_risk_weights` 的全表 `SELECT`（无 WHERE / ORDER）本来就要读所有行，索引在那里帮不上忙，
所以没有为它加更多索引。）

**Serverless 的只读文件系统**：Vercel 的部署包只读，`INSERT` 会 500。
`_resolve_db_path()` 检测 `VERCEL=1` 环境变量，首次把库复制到 `/tmp/safepath.db` 再用。
代价明说：`/tmp` 每次冷启动清空，**线上的上报撑不过冷启动**——要真持久化得换外部数据库
（Postgres / Turso），demo 场景不值得。

---

## 8. Web 层：Flask

| 路由 | 方法 | 作用 | 备注 |
|---|---|---|---|
| `/` | GET | 首页：搜索框 + 上报表单 + 默认地图（含风险图层） | 支持 `?reported=lat,lng` 回显定位高亮 |
| `/route` | **POST** | 校验坐标 → snap 到节点 → 两次 Dijkstra → 算全部对比指标 → 重建地图 → 重渲染页面 | **有意义的计算型 POST** |
| `/report` | **POST** | 校验 → `INSERT reported_incidents` → 清缓存 → flash → 302 重定向 | **改变服务端状态的 POST**（PRG 模式） |
| `/search` | GET(JSON) | `?q=&lat=&lng=` → `{"results":[{name,lat,lng,kind}]}` 最多 8 条 | 唯一的 JSON 端点，喂给前端 combobox |
| `/about` | GET | 方法论、数据归属、完整局限清单 | — |

**`POST /report` 的缓存失效**：加权图缓存在 `functools.lru_cache(maxsize=1)` 里，
上报改变了边权重，所以 `/report` 必须调 `load_weighted_graph.cache_clear()`。
不清就是最阴险的那类 bug——"上报成功了，但后续路线一点没变"，**静默失效，没有任何报错**。

**folium 地图的层级顺序 = `add_to` 顺序**，代码里注释锁死：

```
risk（风险街道上色）→ heat（犯罪热力图）→ 路线 short/safe → avoided（红）→ remaining（琥珀虚线）
```

`avoided` 必须最后加，否则红色的"避开段"会被路线本身盖住。

**前后端契约 `window.SAFEPATH_CTX`**：folium 生成的 JS 变量名带哈希（`map_<hash>`），每次渲染都变。
后端用 `get_name()` **动态**取名注入，前端只读不硬编码。
契约字段：`{map, risk, heat, avoided, has_avoided, short, safe, remaining, has_remaining}`。
另外用 `m.get_root().render()` 而非 `m._repr_html_()`——后者会包一层 iframe，
注入的 JS 就跨文档了，没法和侧栏表单、图例 DOM 联动。

---

## 9. 前端交互（原生 JS，无框架）

538 行原生 JS，零构建步骤、零 npm 依赖。四块主要逻辑：

**① 起终点选取有且只有两条路径**（都产出坐标，后端只认坐标）：

- **搜索**：`GET /search` 驱动的 combobox。200 ms debounce + `AbortController` 取消在途请求 +
  方向键导航 + `role="listbox"/"option"` + `aria-activedescendant`。候选 `<li>` 用
  `document.createElement` + `textContent` 构造，**不用 `innerHTML`**（OSM 的地名是外部数据，不可信）。
- **地图取点（"武装模式"）**：点 📌 按钮**武装**某个字段 → 下一次地图点击写入该字段并**自动解除武装**，
  Esc 取消。未武装时 `MAP.on("click")` 第一行就 `return`。
  这是为了根治早期的硬伤——旧实现里每次点地图都交替落 origin/dest 的 pin，
  用户想拖地图看看都会误改起点。

**② Geolocation（回答你的问题，作用域很窄）**：只绑在上报表单的 "Use my location" 按钮上，
`navigator.geolocation.getCurrentPosition(success, error, {enableHighAccuracy:true, timeout:10000, maximumAge:30000})`，
成功后填 `#id-lat`/`#id-lng` 并把地图居中到该点、落一个 "You are here" 的 pin。
浏览器不支持 → 按钮直接 `disabled`；用户拒绝授权 → 弹一句人话提示，手输坐标的 `<details>` 兜底一直在。
另外 `navigator.geolocation` 只在 HTTPS 或 localhost 下可用，这条在 `/about` 页里对用户讲明了。

**③ 图层 toggle**：删掉 folium 默认的 `LayerControl`（与项目的深色玻璃 UI 格格不入，
且无法让风险图例随图层开关联动），自建按钮绑 `MAP.addLayer/removeLayer`；
关掉风险图层时图例淡到 30%——弱化但不移除，避免用户误以为图例失效。

**④ 联动**：hover 侧栏的路线卡片 / "避开的高危段"区块 → 地图上对应的 polyline 加粗提亮
（纯 `setStyle`，无动画库）；提交表单进 loading 态。

**时序坑**：本脚本在 `</body>` 执行，而 folium 的 `var map_<hash>` 在它自己注入的内联 script 里，
可能尚未求值。解法是 `DOMContentLoaded` + 轮询 `window[ctx.map]`，40 次 × 50 ms 上限（~2 s），
超时就放弃地图相关绑定但保留定位按钮——不写死循环。

---

## 10. 安全与信任边界

| 边界 | 处理 |
|---|---|
| `/search` 的 `q` 进 SQL `LIKE` | `_like_escape()` 转义 `\` `%` `_` 三个元字符，配合 `ESCAPE '\'`。**不转义的话搜 `%` 会匹配 `places` 全表（当前 10,039 行）**。截断到 60 字符防超长输入。有端到端自检（H10）。 |
| 所有 SQL | 一律参数化（`?` 占位符），无字符串拼接 |
| `/route`、`/report` 的坐标 | 三道关：① `float()` 解析失败 → flash 报错；② 费城 bbox 校验（`39.86–40.15, −75.32–−74.90`），防止别的城市的坐标；③ **snap 距离守卫 `SNAP_MAX_M = 300 m`** |
| `/report` 的 `type` | Python 白名单 `ALLOWED_REPORT_TYPES` + SQLite `CHECK` 约束，双层 |
| `/report` 的 `note` | 长度 ≤ 500，超长拒绝 |
| 回显的地名 | 截断 120 字符；Jinja2 自动转义处理 HTML 注入 |
| 前端插入外部文本 | `textContent` / `createElement`，不用 `innerHTML`；静态 SVG 图标走 `DOMParser` |
| `/search` 的失败模式 | `places` 表不存在（老库）时捕获 `OperationalError` **回落**到 12 个预设地标做子串过滤，**绝不 5xx**——搜索框不能把首页打挂（自检 H12） |
| `SECRET_KEY` | 读环境变量，本地 demo 才回落固定值（只用于 flash 消息，不涉及认证） |

**`SNAP_MAX_M = 300` 这道守卫的必要性**：`nearest_nodes` 对图外的点**仍然会返回**一个最近节点
（可能在几公里外），于是应用会静默画出一条横穿城市的诡异路线。这是地图点选唯一的关键失败模式，
所以设了硬上限并配自检 H8（图内点 snap 距离 < 300 m，图外点 > 300 m）。

另一个踩过的坑写在代码注释里：判定四个坐标字段是否齐全**不能用 `all(strings)`**，
因为 `all('') == True`（空字符串对 `all` 而言是"空可迭代对象"，返回 True）——
必须写 `bool(a and b and c and d)`。

---

## 11. 性能工程（都是实测驱动的）

| 优化 | 手段 | 收益 |
|---|---|---|
| 图解析 | GraphML → pickle 预解析缓存 | 2.2 s → ~0.5 s |
| 加权图 | `functools.lru_cache(maxsize=1)` 缓存"图 + 赋权"结果 | 每个 `GET /` 和 `POST /route` 原本都重付 ~3 s |
| 风险图层查询 | `edge_risk(risk_normalized DESC)` 索引 | 80,550 行全表排序 → 索引扫描取 top 20,000 |
| 风险图层渲染 | 20,000 个独立 `folium.PolyLine` → **单个 `folium.GeoJson`** FeatureCollection | 旧方案每个 PolyLine 各过一遍 Jinja 模板序列化，是渲染耗时大头 |
| 热力图采样 | 运行时 `ORDER BY RANDOM() LIMIT 5000` → **prep 阶段采样一次**（固定 seed）+ `lru_cache` | 消掉"对 10 万行全表随机排序"这个每次冷启动都要重付的经典反模式 |
| 部署包 | `vercel.json` 的 `excludeFiles: "data/walk.graphml"` | 31 MB 的 XML 只在 pkl 缺失时才读（正常路径从不读），排除它减少冷启动解压时间 |
| CSV 读取 | `read_csv(usecols=callable)` 只读 5 列 | 不把 3 × 42 MB 的全宽表读进内存 |

风险上色图层还有一个**取 top-N 而非阈值**的决策：早期用 `risk_normalized > 0.4` 筛选，
换成百分位排名归一化后 0.4 = **全图 60% 的边**，浏览器直接卡死。
改成"取最高的 20,000 条"后，这个上限与分布形状无关，将来再换归一化方式也不会失效。

---

## 12. 验证：13 项可运行自检

无测试框架、无 `tests/` 目录、无 fixture——**裸 `assert` 写在各模块的 `__main__` 里**，
三条命令跑完全部：

```bash
python3 prep_data.py     # H1  百分位排名归一化（单调 / (0,1] / 平局同名次 / 长度除法已移除的守卫）
                         # H4  buffer 分配：图外远点不分配
                         # H4b 重叠区：路网密集点分给多条边
                         # H9  路口 place：单名排除 / 双名 sorted join / 同 label 去重 / OSM list-name 取 [0]
python3 routing.py       # H2  safety_weight 公式（0→1×，1→2.5×，锁 α=1.5）
                         # H3  路线分叉：合成"短而险 vs 长而安"图，两权重下路径必须不同
                         # H6  hot_blocks（p90 阈值 / 平行边取 max 不退化成 key=0 / 无 DB 兜底）
                         # H7  avoided_hot_segments 只取单侧独有+高危；**方向交换即得另一侧**
python3 app.py --check   # H5  classify_divergence 三态（按 diff_ratio 而非 detour_ratio）
                         # H8  snap 距离守卫（图内 <300 m，图外 >300 m）
                         # H10 _like_escape 转义 % _ \（端到端：搜 "%" 不匹配全表）
                         # H11 search_places 三级排序（前缀>子串，landmark>intersection，近>远）
                         # H12 places 表缺失 → 回落预设地标，不 500
node static/app.js       # parseReported / pickLabel / searchResults 的解包契约（H13）
```

每条自检都对应一个**真实踩过的坑**，不是为覆盖率写的。举两个例子：

- **H13** 锁的是一个真实发生过的静默 bug：后端返回 `{"results": [...]}`，前端按裸数组 `.slice()`，
  抛出的 `TypeError` 被 `.catch` 裸吞——打字永远不弹候选，控制台一片安静。
  现在 `searchResults()` 是唯一解包点，且 `.catch` 里只静默 `AbortError`，其余一律 `console.warn`。
- **H1** 里有一条"长度除法守卫"：同样的 `weighted_crime_sum` 配不同的 `length_m`，
  归一化结果必须完全相同——谁把 `/length` 加回去，这条自检立刻变红。

---

## 13. 部署

Vercel Serverless（`vercel.json`：`functions."app.py"`，`maxDuration: 60`，
`excludeFiles: "data/walk.graphml"`），线上地址 `safepath-black.vercel.app`。
Serverless 环境的三处针对性处理：pickle 预解析缓存（冷启动）、`/tmp` 数据库复制（只读文件系统）、
排除 31 MB graphml（部署包体积）。

---

## 14. 实测结果（可直接引用的数字）

**2026-08-11 现场跑出的四对起终点**（`risk_p90 = 0.900`）：

| 起 → 终 | 最短 | 最安全 | 代价 | **高危路段 (risk>p90)** | 高危米数 | 路线差异 (`diff_ratio`) |
|---|---|---|---|---|---|---|
| Penn Park → Mill Creek (44th & Lancaster) | 3,153.9 m | 3,260.5 m | **+106.6 m (+3.4%)** | **10 → 0** | 480.0 → 0 m | 0.922（92% 路段不同） |
| Van Pelt Library → 30th Street Station | 1,314.7 m | 1,354.6 m | **+39.9 m (+3.0%)** | **4 → 0** | 68.6 → 0 m | 0.544 |
| Market St Corridor → Mantua (35th & Haverford) | 1,711.5 m | 2,015.2 m | +303.7 m (+17.7%) | 5 → 5（净差 0） | **115.1 → 55.6 m（−52%）** | 0.927 |
| Van Pelt Library → Huntsman Hall（短程 177 m） | 176.8 m | 242.6 m | +65.8 m (+37.2%) | 0 → 0（本就没有） | — | 0.615（总风险 **−60.7%**） |

**这张表怎么读（三个诚实的注脚）：**

1. **前两行是最强的证据**：多走 40–107 米，把路径上的高危路段从 10 段 / 4 段清到 **0**。
2. **第三行是"绕不掉"的真实场景**：起点 Market St Corridor 本身就是高犯罪锚点，
   两条路都有 5 段高危（段数净差 0），因为**你正站着的那个街区谁也绕不开**。
   这时唯一站得住的数字是高危**米数**降了 52%，UI 也切换到米数口径 + 明确的"绕不掉"说明。
3. **第四行说明短程的百分比会失真**：+37.2% 听着吓人，绝对值只是 177 米基线上的 +66 米。
   所以 UI 一律**先报绝对米数，百分比放括号**。短程没有高危段可避，改用总风险降幅（−60.7%）来讲。

> 补一句一致性说明：项目早期笔记里记的绕路幅度是 "<2.5%"，本次实测是 3.0%–3.4%（前两行）。
> 对外引用请用本表的实测值。

**其他实测数字：** 路网 26,429 节点 / 81,324 边；犯罪点覆盖 80,550 条边（99.0%）；
`risk_normalized` 分布 p50 = 0.500 / p90 = 0.900 / p99 = 0.990；
风险热点集中在 Mantua、Market St 走廊、Point Breeze 三处；
修复"除以长度"的 bug 后高危边中位长度从 <10 m 升到 **44.3 m**，
`corr(risk, log length)` 从 **−0.719** 转为 **+0.258**。

---

## 15. 已知局限（全部写在 README 和 `/about` 页里）

1. 历史犯罪 ≠ 未来风险；数据反映的是**报案**分布，含报案率偏差
2. 静态快照，非实时（有意为之：断网可 demo，不依赖第三方 SLA）
3. 固定区域：Penn 周边 3.5 km
4. 人行道 / ADA 数据是轶事性的——只有用户上报，费城没有可用的开放数据集
5. 距离衰减模型只有单一 α，且是线性衰减而非核函数
6. **绕路幅度小（3% 量级）但路线差异大（54%–93% 路段不同）**——网格几何使然，不是"没分叉"
7. 高危阈值取 p90 是判断取舍
8. **排名归一化丢失量级**（最险与较险被压成相邻名次；但与 p90 阈值同为排名语义，自洽）
9. 短程绕路百分比失真
10. 120 m 缓冲带让路口重复计入（有意设计，但会系统性抬高路口风险）
11. 无转弯惩罚（不区分"直行"和"频繁拐弯"的路线）
12. severity 权重表是判断取舍，非从伤害数据反推
13. `REPORT_BUMP_FACTOR = 5.0` 是调参常数
14. α = 1.5 锁定
15. 地点搜索纯本地，**搜不到门牌号地址**（接外部 geocoder 会破坏断网 demo，见下）
16. 地图取点的显示名就是坐标本身，不做反向地理编码
17. 线上（Vercel）的用户上报撑不过冷启动（`/tmp` 会被清空）

---

## 16. 关键决策速查表

| 决策 | 选了什么 | 为什么 |
|---|---|---|
| 归一化方式 | 百分位排名 `rank(pct=True)` | min-max/log 都被长尾压死判别力；排名与分布形状无关，把满动态范围交给 Dijkstra |
| 风险分是否除以长度 | **不除** | 除法让 `corr(risk, log length) = −0.719`，模型退化成"测边有多短" |
| 缓冲半径 | 120 m + 线性衰减 | 30 m 等权太稀疏；费城街区宽 ~50 m，120 m 才能覆盖相邻 2–3 条平行街 |
| 最短路算法 | 双向 Dijkstra | A\* 在 `safety_weight` 下的可采纳启发式不平凡，且 8 万边规模用不上 |
| 绕路预算约束 | **不做** | Yen 对 MultiDiGraph 直接抛异常；网格里等长路径组合爆炸，预算剪枝永不触发；实测最优解就在 2–3%，预算花不出去 |
| 主指标 | 高危路段数 / 高危米数 | 总风险差被平滑摊薄到 1–2%；绕路比在网格里量的是错的东西 |
| 分叉判定 | `1 − Jaccard(节点集)` | 网格里两条几何差异巨大的路可以等长，长度量不出分叉 |
| 数据库 | SQLite，无 ORM | 单文件、零运维、能进部署包；四张表七八条 SQL，ORM 是净负担 |
| 地点搜索 | **纯本地 `places` 表**，不接 geocoder | OSM Nominatim 的使用政策禁止 autocomplete；Photon 是无 SLA 的 demo 服务器；两者都破坏"不依赖实时 API"的前提。代价（搜不到门牌号）明确接受 |
| 前端框架 | **无**（538 行原生 JS） | 一个页面、四块交互逻辑，构建工具链的成本高于收益 |
| 测试 | 裸 `assert` 写进 `__main__` | 13 项自检全部对应真实踩过的坑，不为覆盖率写空断言 |
| 数据管道幂等性 | 分表 `replace` + 上报表 `IF NOT EXISTS` | 重跑数据管道不能清掉用户上报（早期整库删除是明确移除的反模式） |

---

## 17. 课程知识点映射

| 课程模块 | 项目中的体现 |
|---|---|
| **Pandas** | `prep_data.py`：多年 CSV 合并 → `dc_key` 去重 → bbox 过滤 → GeoPandas 空间 join → `groupby(u,v,k)` 多列聚合（含 `value_counts` 取众数的自定义 lambda）→ `rank(pct=True)` 归一化 → `to_sql` |
| **Graph** | OSMnx 建图（26,429 节点 / 81,324 边）→ 缓冲带把点数据分配到边 → 自定义边权 → 两次 Dijkstra → 路径差集 / Jaccard / 平行边聚合 |
| **Flask + HTML** | 5 个路由（含 2 个 POST + 1 个 JSON 端点）、Jinja2 模板与自动转义、flash + PRG 重定向模式、folium 地图经 `\|safe` 嵌入、原生 JS 前端 |
| **持久化** | SQLite 四张表，含 `CHECK` 约束、索引、幂等重建策略 |
| **有意义的 POST** | `/report` 真正改变服务端状态并**改变后续路由结果**（不是写进库就没下文的表单）；`/route` 触发实际计算 |
| **部署** | Vercel Serverless，含三处针对无状态/只读环境的适配 |
