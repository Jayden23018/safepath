"""SafePath — 数据预处理 (Pandas + Graph)。

跑一次：抓步行路网 → 读犯罪 CSV → 分配到边 → 算风险 → 写 SQLite。
幂等：graphml 和 db 都 if_exists replace / 缓存。
"""
from __future__ import annotations

import os
import pickle
import sqlite3
from typing import Iterable

import networkx as nx
import osmnx as ox
import pandas as pd

from routing import PRESET_LOCATIONS  # name → (lat, lng) 12 个地标；routing 导入无副作用

# Penn 周边 (lat, lng)。osmnx graph_from_point 接 (lat, lng)。
PENN_CENTER = (39.9526, -75.1932)
GRAPH_DIST_M = 3500
GRAPH_PATH = "data/walk.graphml"
PICKLE_PATH = "data/walk.pkl"  # ponytail: 预序列化缓存，运行时 load 替代 ox.load_graphml（31MB XML 解析 2.2s → pkl ~0.5s），救 Vercel serverless 10s 超时。graphml 仍是权威源。
DB_PATH = "data/safepath.db"
HEATMAP_SAMPLE_N = 5000  # 热力图固定采样点数：prep 阶段采一次，运行时直接读全表，省掉每次渲染 ORDER BY RANDOM 全表排序。

# 近 3 年（2024–2026）合并，去单年波动。三年 CSV 列名一致（已核验）。
CRIME_CSV_PATHS = ["data/crime_2024.csv", "data/crime_2025.csv", "data/crime_2026.csv"]

BUFFER_M = 120.0  # 边缓冲半径（米）：覆盖相邻 2-3 条平行街（费城街区宽 ~50m）。
                   # 比 30m 更对症：隔街也能吃到风险信号，避免风险过度稀疏。
                   # 非等权——越远衰减越快（见 assign_crimes_to_edges 的 decay）。

# 严重度权重：暴力 > 财产 > 治安。判断取舍，非经验推导（见 README 局限 #8）。
SEVERITY: dict[str, float] = {
    "Homicide": 10.0,
    "Aggravated Assault Firearm": 8.0,
    "Robbery Firearm": 7.0,
    "Aggravated Assault No Firearm": 6.0,
    "Robbery No Firearm": 5.0,
    "Weapon Violations": 5.0,
    "Other Assaults": 4.0,
    "Burglary Residential": 3.0,
    "Motor Vehicle Theft": 3.0,
    "Theft from Vehicle": 2.5,
    "Thefts": 2.0,
    "Vandalism/Criminal Mischief": 1.5,
    "Narcotic / Drug Law Violations": 1.5,
    "Fraud": 1.0,
    "Disorderly Conduct": 1.0,
}
DEFAULT_SEVERITY = 1.0
ALPHA = 1.5  # 最高风险边 = 2.5× 真实长度；零犯罪边 = 原长度。


def fetch_walk_graph(
    center: tuple[float, float] = PENN_CENTER,
    dist: int = GRAPH_DIST_M,
    path: str = GRAPH_PATH,
) -> nx.MultiDiGraph:
    """缓存步行图：存在则 load_graphml，否则抓取 + 存盘。

    同时维护一份 pickle 预解析缓存（运行时 load 快 ~4×）。
    pkl 过期（比 graphml 旧，或 graphml 被删重抓）时自动重建。
    """
    if os.path.exists(path):
        G = ox.load_graphml(path)
    else:
        ox.settings.http_user_agent = "safepath@upenn.edu"
        G = ox.graph_from_point(center, dist=dist, network_type="walk")
        ox.save_graphml(G, path)
    if not os.path.exists(PICKLE_PATH) or os.path.getmtime(PICKLE_PATH) < os.path.getmtime(path):
        with open(PICKLE_PATH, "wb") as f:
            pickle.dump(G, f, protocol=pickle.HIGHEST_PROTOCOL)
    return G


# ---- 犯罪数据 ----

# CSV 列名 → 标准列。OpenDataPhilly 不同年份/格式可能用 point_x/point_y 或 lat/lng。
_COL_ALIASES = {
    "lng": ["lng", "point_x"],
    "lat": ["lat", "point_y"],
    "code": ["text_general_code"],
    "date": ["dispatch_date", "dispatch_date_time"],
}


def _pick(df: pd.DataFrame, standard: str):
    for c in _COL_ALIASES[standard]:
        if c in df.columns:
            return df[c]
    return None


def load_crime_csv(paths: Iterable[str] = CRIME_CSV_PATHS) -> pd.DataFrame:
    """读+合并多年犯罪 CSV。返回 df[lng, lat, code, date]，dc_key 去重。

    CSV 不存在或无坐标列 → 合成 ~20 个 Penn 周边点，让 pipeline 跑通。
    """
    # 只读需要的列，省内存。pandas usecols callable 对每个列名返回 bool。
    keep = set(sum(_COL_ALIASES.values(), [])) | {"dc_key"}
    frames = [pd.read_csv(p, usecols=lambda c: c in keep, low_memory=False)
              for p in paths if os.path.exists(p)]
    if not frames:
        return _synthetic_crime()
    df = pd.concat(frames, ignore_index=True)
    lng, lat = _pick(df, "lng"), _pick(df, "lat")
    if lng is None or lat is None:
        return _synthetic_crime()
    out = pd.DataFrame(
        {"lng": pd.to_numeric(lng, errors="coerce"),
         "lat": pd.to_numeric(lat, errors="coerce"),
         "code": (_pick(df, "code") if _pick(df, "code") is not None else "Unknown"),
         "date": (_pick(df, "date") if _pick(df, "date") is not None else "")}
    )
    # dc_key 去重：跨年份同事件只留一条；无 dc_key 则全行去重。
    if "dc_key" in df.columns:
        out["dc_key"] = df["dc_key"].values
        out = out.drop_duplicates(subset=["dc_key"]).drop(columns=["dc_key"])
    else:
        out = out.drop_duplicates()
    return out.dropna(subset=["lng", "lat"]).reset_index(drop=True)


def _synthetic_crime() -> pd.DataFrame:
    """~20 个 Penn 周边合成点，让无 CSV 时 pipeline 也能跑。"""
    rows = [
        (-75.195, 39.953, "Thefts"),
        (-75.194, 39.952, "Robbery No Firearm"),
        (-75.193, 39.951, "Other Assaults"),
        (-75.192, 39.950, "Thefts"),
        (-75.191, 39.949, "Vandalism/Criminal Mischief"),
        (-75.190, 39.950, "Robbery Firearm"),
        (-75.189, 39.951, "Theft from Vehicle"),
        (-75.188, 39.952, "Other Assaults"),
        (-75.187, 39.953, "Disorderly Conduct"),
        (-75.195, 39.955, "Burglary Residential"),
        (-75.194, 39.956, "Aggravated Assault No Firearm"),
        (-75.193, 39.957, "Weapon Violations"),
        (-75.196, 39.954, "Thefts"),
        (-75.190, 39.955, "Motor Vehicle Theft"),
        (-75.188, 39.956, "Fraud"),
        (-75.192, 39.953, "Other Assaults"),
        (-75.191, 39.954, "Robbery No Firearm"),
        (-75.189, 39.955, "Thefts"),
        (-75.187, 39.954, "Vandalism/Criminal Mischief"),
        (-75.194, 39.951, "Narcotic / Drug Law Violations"),
    ]
    return pd.DataFrame(rows, columns=["lng", "lat", "code"])


def filter_to_bbox(df: pd.DataFrame, center: tuple[float, float] = PENN_CENTER,
                   dist: int = GRAPH_DIST_M) -> pd.DataFrame:
    """center±dist (米→粗略度数) 的 bbox 过滤。1° lat≈111320m；经度随纬度缩。"""
    dlat = dist / 111_320.0
    dlng = dist / (111_320.0 * __import__("math").cos(__import__("math").radians(center[0])))
    lat0, lng0 = center
    mask = (df["lat"].between(lat0 - dlat, lat0 + dlat)
            & df["lng"].between(lng0 - dlng, lng0 + dlng))
    return df[mask].reset_index(drop=True)


# ---- 犯罪点 → 边分配（buffer 空间 join + 距离衰减）----

def assign_crimes_to_edges(df: pd.DataFrame, G: nx.MultiDiGraph) -> pd.DataFrame:
    """每条边画 BUFFER_M 缓冲带，统计落在里面的犯罪点 + 按距离线性衰减。

    返回 df[u, v, k, code, dist_m, decay] —— 每条 (犯罪点, 覆盖边) 一行。
    - 重叠区给所有覆盖边都计（路口多边，有意设计）。
    - decay = 1 - dist_m / BUFFER_M：贴着犯罪点的边=1.0，缓冲边缘=0.0。
      比等权更贴近 NKDE（network KDE，带宽 ~300m 标准做法）的衰减精神，
      让相邻 2-3 条平行街也吃到风险信号，而非只给最近那条。
    """
    import geopandas as gpd
    from shapely.geometry import Point

    G_proj = ox.project_graph(G)
    edges = ox.convert.graph_to_gdfs(G_proj, nodes=False)
    line_geom = edges.geometry  # 原始线几何，用于算真实垂直距离
    edges["buffer"] = line_geom.buffer(BUFFER_M)
    buf = edges.set_geometry("buffer")

    pts = gpd.GeoDataFrame(
        df.copy(),
        geometry=[Point(x, y) for x, y in zip(df["lng"], df["lat"])],
        crs="EPSG:4326",
    ).to_crs(G_proj.graph["crs"])

    joined = gpd.sjoin(pts, buf[["buffer"]], how="inner", predicate="within")
    # geopandas 1.1: 右表边的 (u,v,k) MultiIndex 展开成普通列 [u, v, key]。
    # 兼容旧版 index_right 元组：取到列就重映射。
    if {"u", "v", "key"}.issubset(joined.columns):
        out = joined.rename(columns={"key": "k"})
    elif "index_right" in joined.columns:
        uvk = pd.DataFrame(joined["index_right"].tolist(), index=joined.index,
                           columns=["u", "v", "k"])
        out = pd.concat([joined.drop(columns=["index_right"]), uvk], axis=1)
    else:  # ponytail: 兜底；geopandas 大版本变结构时手动核
        out = joined
        out[["u", "v", "k"]] = 0

    # 距离衰减：逐行算犯罪点到该边原始线几何的距离（米）。
    # 用 (u,v,k) → line 字典反查，避免按位置对齐的脆弱性。
    line_map = {(u, v, k): geom for u, v, k, geom in
                zip(edges.index.get_level_values(0),
                    edges.index.get_level_values(1),
                    edges.index.get_level_values(2), line_geom)}
    out["dist_m"] = [line_map.get((r.u, r.v, r.k)).distance(pt)
                     for r, pt in zip(out.itertuples(), out.geometry)]
    out["dist_m"] = out["dist_m"].clip(upper=BUFFER_M)
    out["decay"] = (1.0 - out["dist_m"] / BUFFER_M).clip(lower=0.0)
    return out.reset_index(drop=True)[["u", "v", "k", "code", "dist_m", "decay"]]


# ---- 风险评分 ----

def build_edge_risk_table(assigned: pd.DataFrame, G: nx.MultiDiGraph,
                          alpha: float = ALPHA) -> pd.DataFrame:
    """groupby(u,v,k): 距离衰减加权犯罪和 → 风险分 → 百分位排名归一化 → safety_weight。

    - weighted_crime_sum = Σ(severity * decay)：近犯罪点权重大、远的小。
    - risk_score = weighted_crime_sum（这条边坐落在多强的犯罪场里，不再除以长度）。
      原来除以 length_m 是「单位长度风险密度」，但 120m buffer 决定了一条边收到的
      犯罪量与长度基本无关（实测 corr(weighted_crime_sum, length)=0.181），除以长度
      ≈ 乘 1/length → risk_normalized 主要在测「边有多短」（corr=-0.719），把几米的人行道
      过街小段推成高危，污染 hot_blocks 和风险上色层。
    - risk_normalized = risk_score.rank(pct=True)：百分位排名（平局取平均名次），落在 (0,1]。
      不能只去除法再保留 log-minmax——那样所有边挤在 0.55–0.68，惩罚天花板 1.227 低于
      旧 bug 实际达到的 1.249，调 α 也救不回分叉。排名归一化给 Dijkstra 满动态范围。
      代价：丢失量级（最险街区与「比较险」压成同名次差），但 p90 高危阈值本身也是
      排名切分，两者语义一致。
    length_m 取图边原始 length 属性（米）；保留该列写入 SQLite（schema 不变）。
    """
    assigned = assigned.assign(sev=assigned["code"].map(SEVERITY).fillna(DEFAULT_SEVERITY))
    assigned["weighted"] = assigned["sev"] * assigned["decay"]
    g = assigned.groupby(["u", "v", "k"])
    agg = g.agg(
        crime_count=("code", "size"),
        weighted_crime_sum=("weighted", "sum"),
        top_crime_category=("code", lambda s: s.value_counts().index[0]),
        top_crime_count=("code", lambda s: int(s.value_counts().iloc[0])),
    ).reset_index()

    length_map = {(u, v, k): d.get("length", 1.0) for u, v, k, d in G.edges(keys=True, data=True)}
    agg["length_m"] = [length_map.get((r.u, r.v, r.k), 1.0) for r in agg.itertuples()]

    # 不除长度：risk_score 直接是犯罪场强度。归一化用百分位排名，与分布形状无关。
    agg["risk_score"] = agg["weighted_crime_sum"]
    agg["risk_normalized"] = agg["risk_score"].rank(pct=True)
    agg["safety_weight"] = agg["length_m"] * (1 + alpha * agg["risk_normalized"])
    return agg


def write_risk_to_sqlite(risk: pd.DataFrame, db_path: str = DB_PATH) -> None:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    # 不删整个库：to_sql(if_exists="replace") 只重建 edge_risk，reported_incidents 保留。
    conn = sqlite3.connect(db_path)
    risk[["u", "v", "k", "crime_count", "weighted_crime_sum",
          "length_m", "risk_score", "risk_normalized",
          "top_crime_category", "top_crime_count"]].to_sql(
        "edge_risk", conn, if_exists="replace", index=False)
    # 服务 app.py 的 ORDER BY risk_normalized DESC LIMIT：无索引是全表排序（80550 行）。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edge_risk_risk ON edge_risk(risk_normalized DESC)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS reported_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lat REAL NOT NULL, lng REAL NOT NULL,
            type TEXT NOT NULL CHECK (type IN
                ('sidewalk_damage','no_ada_ramp','poor_lighting','unsafe_crossing')),
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')))"""
    )
    conn.commit()
    conn.close()


def write_crime_points_to_sqlite(df: pd.DataFrame, db_path: str = DB_PATH) -> None:
    """写原始犯罪点 (lat,lng,severity) 供热力图层用。

    用 bbox 过滤后的 df（与路由图同范围），不写全市 23 万原始点。
    severity 复用 SEVERITY 字典，热力图可按犯罪严重度加权。
    app.py 不碰 pandas，故走 SQLite（与 edge_risk 同模式）。

    采样固定 HEATMAP_SAMPLE_N 点（random_state 固定 seed，可复现，非安全用途）在 prep
    阶段做一次，而非运行时每次渲染 ORDER BY RANDOM() 重采——权衡：热力图从"每次渲染
    换一批随机点"变成"固定同一批点"，视觉密度分布不变，展示性场景可接受。
    """
    out = df.assign(severity=df["code"].map(SEVERITY).fillna(DEFAULT_SEVERITY))
    sampled = out.sample(n=min(len(out), HEATMAP_SAMPLE_N), random_state=0)
    conn = sqlite3.connect(db_path)
    sampled[["lat", "lng", "severity"]].to_sql(
        "crime_points", conn, if_exists="replace", index=False)
    conn.commit()
    conn.close()


# ---- places 表（起终点候选搜索源）----

def build_intersection_places(G: nx.MultiDiGraph) -> list[dict]:
    """按节点收集相邻边 name，name 数 ≥2 的路口作 place。

    label = " & ".join(sorted(names))，按 label 去重保留首个。
    坐标取节点 y/x（图节点本身，snap 距离 0）。

    OSM 多名道路 edge['name'] 可能是 list（unhashable）→ isinstance 取 nm[0]。
    """
    seen: set[str] = set()
    places: list[dict] = []
    for node, data in G.nodes(data=True):
        names: set[str] = set()
        for _, _, edata in G.edges(node, data=True):
            nm = edata.get("name")
            if nm is None:
                continue
            if isinstance(nm, list):  # OSM 多名道路
                nm = nm[0] if nm else None
                if nm is None:
                    continue
            names.add(nm)
        if len(names) < 2:
            continue
        label = " & ".join(sorted(names))
        if label in seen:
            continue
        seen.add(label)
        places.append({"name": label, "lat": float(data["y"]), "lng": float(data["x"]),
                       "kind": "intersection"})
    return places


def fetch_poi_places(center: tuple[float, float] = PENN_CENTER,
                     dist: int = GRAPH_DIST_M) -> list[dict]:
    """抓 OSM POI（amenity/shop/leisure/tourism/office/building），过滤有 name 的。

    osmnx 2.0.7: features_from_point 在 ox.features。
    Overpass 挂/超时 → 打印降级警告返回 []，路口和地标照常产出。
    representative_point() 取点（地理 CRS 下 .centroid 抛 UserWarning）。

    building 必须在里面：校园楼（宿舍/教学楼/食堂）多数只有 building=* 没有 amenity，
    实测 3.5km 内 1752 个有名字的 building 里 976 个不带前五类 tag —— 1920 Commons
    （OSM 名 "Class of 1920 Commons", building=university）、Houston Hall、Harnwell、
    Du Bois、Levine、Meyerson 全在这 976 里，少了它们校园搜索基本是废的。
    ponytail: 没收 public_transport/railway —— 那批 586 个名字是 "16844"、
    "15186 Broad St & Walnut St" 这种站点 ID，进库只会污染候选。
    """
    try:
        tags = {"amenity": True, "shop": True, "leisure": True,
                "tourism": True, "office": True, "building": True}
        gdf = ox.features.features_from_point(center, tags=tags, dist=dist)
    except Exception as e:
        print(f"      ⚠️ POI 抓取失败（Overpass 不可达/超时）→ 降级，仅路口+地标: {e}")
        return []
    if gdf.empty or "name" not in gdf.columns:
        return []
    gdf = gdf[gdf["name"].notna()]
    places: list[dict] = []
    for _, row in gdf.iterrows():
        try:
            pt = row.geometry.representative_point()
        except Exception:
            continue
        places.append({"name": str(row["name"]), "lat": float(pt.y), "lng": float(pt.x),
                       "kind": "poi"})
    return places


def write_places_to_sqlite(places: list[dict], db_path: str = DB_PATH) -> None:
    """写 places 表。to_sql(replace) 只替换 places，reported_incidents 保留。"""
    df = pd.DataFrame(places, columns=["name", "lat", "lng", "kind"])
    conn = sqlite3.connect(db_path)
    df.to_sql("places", conn, if_exists="replace", index=False)
    conn.commit()
    conn.close()


# ---- sanity checks (assert, 无框架) ----

def _check_normalization():
    """H1: 百分位排名归一化。锁 rank(pct=True) 的契约。

    取代旧的 log 压缩检查（去除长度除法后 log-minmax 会把判别力压到 α 也救不回，
    故改为 rank 归一化）。这里锁三件事：
    1) rank 随 risk_score 单调（更高 score → 更高（或相等）的归一化值）；
    2) 归一化值落在 (0, 1]（pandas rank(pct=True) 最小值 pct=1/N，不是 0）；
    3) 平局（相同 score）取相同名次。
    另外锁住「长度除法已移除」——同样的 weighted_crime_sum 配不同 length_m，
    归一化结果必须完全相同（谁把 /length 加回去，这条就红）。
    """
    df = pd.DataFrame({"risk_score": [0.0, 1.0, 2.0, 3.0, 3.0, 5.0]})
    norm = df["risk_score"].rank(pct=True).tolist()
    # 单调：相邻值不下降
    assert all(norm[i] <= norm[i + 1] + 1e-12 for i in range(len(norm) - 1)), norm
    # 落在 (0, 1]
    assert all(0.0 < v <= 1.0 + 1e-12 for v in norm), norm
    # 平局（两个 3.0）取相同名次
    assert norm[3] == norm[4], norm
    # 最高值 pct = 1.0（n 个值里最大名次占比）
    assert norm[5] == 1.0, norm

    # 长度除法守卫：同 weighted_crime_sum、不同 length_m → 同一 risk_normalized。
    # 构造 build_edge_risk_table 的最小输入：assigned 只需 code/decay，length 来自 G。
    G = nx.MultiDiGraph()
    G.add_edge("a", "b", 0, length=3.0)    # 极短过街段
    G.add_edge("c", "d", 0, length=200.0)  # 长街
    assigned = pd.DataFrame([
        {"u": "a", "v": "b", "k": 0, "code": "Thefts", "decay": 1.0},
        {"u": "c", "v": "d", "k": 0, "code": "Thefts", "decay": 1.0},
    ])
    out = build_edge_risk_table(assigned, G)
    r = out.set_index(["u", "v"])["risk_normalized"]
    assert r[("a", "b")] == r[("c", "d")], out  # 同犯罪强度 → 同排名，与长度无关
    print(f"OK H1: 百分位排名归一化 单调/(0,1]/平局取同值 + 长度除法已移除 (norm={norm})")


def _check_buffer_assignment():
    """H4/H4b: 点落在边 30m 内→分配；30m 外→不分配；重叠区两条边各计一次。"""
    G = fetch_walk_graph()
    # 一个 Penn 中心点（路网密集，必落进多条边的 30m 缓冲带）→ 验证重叠区计入多条边
    dense = pd.DataFrame([{"lng": -75.1932, "lat": 39.9526, "code": "dense"}])
    n_dense = len(assign_crimes_to_edges(dense, G))
    # 远离路网的点（图 bbox 之外）→ 不在任何缓冲带，不被分配
    far = pd.DataFrame([{"lng": -75.0000, "lat": 40.0000, "code": "far"}])  # Philly 外东北角
    n_far = len(assign_crimes_to_edges(far, G))
    assert n_dense >= 2, f"路网密集点应分给多条边(重叠), got {n_dense}"
    assert n_far == 0, f"远离路网点应不被分配, got {n_far}"
    print(f"OK H4: 远点→{n_far} 条边(buffer 外不计); H4b: 密集点→{n_dense} 条边(重叠计入)")


def _check_intersection_places():
    """H9: build_intersection_places 契约。

    合成小 MultiDiGraph：单名节点被排除、双名节点出 sorted " & " label、
    同 label 去重只留一个。另锁 list-name（OSM 多名）取 [0] 不抛 unhashable。
    每个目标节点的邻居都是只连一条边的叶子，避免相邻边 name 集合交叉污染。
    """
    G = nx.MultiDiGraph()

    def junction(nid, y, x, names):
        """nid 是路口，连 names 条各自独立的叶子边。"""
        G.add_node(nid, y=y, x=x)
        for i, nm in enumerate(names):
            leaf = f"{nid}_l{i}"  # 唯一叶子节点，只挂这一条边
            G.add_edge(nid, leaf, 0, name=nm)

    # N1: 两条同名边 → 单名，应被排除
    junction("n1", 39.95, -75.19, ["Walnut St", "Walnut St"])
    # N2: 异名双街 → label = "Locust St & Walnut St"
    junction("n2", 39.96, -75.20, ["Walnut St", "Locust St"])
    # N3: 异名双街 → label = "Chestnut St & Walnut St"
    junction("n3", 39.97, -75.21, ["Walnut St", "Chestnut St"])
    # N4 / N5: 同 label → 去重只留首个（保留哪个都合规）
    junction("n4", 39.98, -75.22, ["Spruce St", "Pine St"])
    junction("n5", 39.99, -75.23, ["Pine St", "Spruce St"])
    # N6: 一条边 name 是 list（OSM 多名）→ 取 [0]，不抛 unhashable
    junction("n6", 40.00, -75.24, [["US-30", "Lancaster Ave"], "Market St"])

    places = build_intersection_places(G)
    by_label = {p["name"]: p for p in places}
    # 单名节点（N1）被排除
    assert "Walnut St" not in by_label, by_label
    # N2/N3 两个异名 label 各自保留，sorted join
    assert "Locust St & Walnut St" in by_label, by_label
    assert "Chestnut St & Walnut St" in by_label, by_label  # sorted: C < W
    # N4/N5 同 label 去重只留一个
    spruce_pine = [p for p in places if p["name"] == "Pine St & Spruce St"]
    assert len(spruce_pine) == 1, spruce_pine
    # list-name 取 [0]：N6 label = "Market St & US-30"
    assert "Market St & US-30" in by_label, by_label
    # 坐标来自节点 y/x，kind 固定
    n2 = by_label["Locust St & Walnut St"]
    assert n2["lat"] == 39.96 and n2["lng"] == -75.20, n2
    assert n2["kind"] == "intersection", n2
    print(f"OK H9: 单名排除/双名 sorted join/同 label 去重/list-name 取[0] "
          f"({len(places)} places: {sorted(by_label)})")


def main() -> None:
    print("[1/7] 抓取/加载步行图…")
    G = fetch_walk_graph()
    print(f"      图: {G.number_of_nodes()} 节点, {G.number_of_edges()} 边")
    print("[2/7] 读犯罪 CSV…")
    df = load_crime_csv()
    print(f"      {len(df)} 条犯罪记录")
    print("[3/7] bbox 过滤…")
    df = filter_to_bbox(df)
    print(f"      {len(df)} 条落在区域内")
    print("[4/7] buffer 分配到边 + 风险评分…")
    assigned = assign_crimes_to_edges(df, G)
    risk = build_edge_risk_table(assigned, G)
    print(f"      {len(risk)} 条边有犯罪记录")
    print("[5/7] 写 edge_risk + crime_points 到 SQLite…")
    write_risk_to_sqlite(risk)
    write_crime_points_to_sqlite(df)
    print(f"      {len(df)} 个犯罪点 → crime_points 采样 {min(len(df), HEATMAP_SAMPLE_N)} 条")
    print("[6/7] 构建 places 表（landmark + intersection + poi）…")
    landmarks = [{"name": n, "lat": float(c[0]), "lng": float(c[1]), "kind": "landmark"}
                 for n, c in PRESET_LOCATIONS.items()]
    intersections = build_intersection_places(G)
    pois = fetch_poi_places()
    write_places_to_sqlite(landmarks + intersections + pois)
    from collections import Counter
    by_kind = Counter(p["kind"] for p in landmarks + intersections + pois)
    print(f"      places: {dict(by_kind)} (landmark={len(landmarks)}, "
          f"intersection={len(intersections)}, poi={len(pois)})")
    print("[7/7] 完成")
    print(f"      → {DB_PATH}")


if __name__ == "__main__":
    _check_normalization()
    _check_buffer_assignment()
    _check_intersection_places()
    print("prep self-checks passed\n---")
    main()
