"""SafePath — Flask web app (Flask + HTML)。

GET  /       下拉选 + 上报表单 + (可选)默认地图
POST /route  compute_both_routes → folium 地图 → 重渲染 index.html
POST /report 校验 → INSERT reported_incidents → flash → redirect
GET  /about  归属 + 方法论 + 局限
"""
from __future__ import annotations

import os
import sqlite3
import functools
from typing import Optional

import folium
from folium.plugins import HeatMap
from flask import Flask, jsonify, flash, redirect, render_template, request, url_for

import routing as R

app = Flask(__name__)
# ponytail: 本地 demo 无 SECRET_KEY 环境变量时退回固定 key（flash 不涉及认证，风险低）；
# 部署到 Vercel 时通过项目环境变量设置 SECRET_KEY。
app.secret_key = os.environ.get("SECRET_KEY", "safepath-dev-key-change-in-prod")

ALLOWED_REPORT_TYPES = {"sidewalk_damage", "no_ada_ramp", "poor_lighting", "unsafe_crossing"}
# Philly 大致 bbox，校验上报坐标不跑偏到别的城市
PHILLY_BBOX = ((39.86, -75.32), (40.15, -74.90))  # (lat_min,lng_min)-(lat_max,lng_max)

# 默认地图中心（与 prep_data.PENN_CENTER 一致；扩到 3.5km 后不再硬编 Van Pelt）
PENN_CENTER = (39.9526, -75.1932)
# CartoDB 浅色底图（免费，demo 够用；备选 voyager 暖灰、OSM 标准 tiles）
LIGHT_TILES = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
TILES_ATTR = '&copy; <a href="https://openstreetmap.org">OpenStreetMap</a> &copy; <a href="https://carto.com/attribution">CARTO</a>'

# 路线对比配色（实色，亮色底图；与 style.css 的 --c-short / --c-safe / --c-danger 锁步同步）
COLOR_SHORT = "#2563eb"
COLOR_SAFE = "#15803d"
COLOR_DANGER = "#dc2626"  # 避开的高危段（比路线更醒目的红）

# minor 阈值：两条路线的「不同路段占比」< 此值才算"隔街级微绕"。
#
# 为什么不用绕路比：费城是曼哈顿网格，换一条平行街走 staircase 几何上天差地别、
# 总长度几乎不变。实测 Penn Park→Mill Creek 两条路只有 15% 节点重合（85% 不同），
# 绕路却仅 +2.2%；按旧的 5% 绕路比阈值会被判成 "minor" 弱化掉——把最强的证据扔了。
# 实测四对起终点的不同占比 48%/57%/71%/85%，0.25 把它们都收进 clear，且给
# 真·隔街微绕（不同占比通常 <15%）留出余量。
# ponytail: 0.25 是基于实测分布的经验值；升级路径=按路网密度自适应。
MINOR_DIFF_RATIO = 0.25

# 点选起终点 snap 距离上限（米）。点到 3.5km 图外时，nearest_nodes 仍会返回最近节点
# （可能几公里外），静默画出一条横穿城市的诡异路线。> 300m 直接拒，是地图点选的
# 唯一关键失败模式守卫。3.5km 图内任意点离最近路网节点 <300m（路网密集）。
SNAP_MAX_M = 300.0


def snap_point(G, lat: float, lng: float) -> tuple[int, float]:
    """(lat,lng) → (node_id, snap_dist_m)。纯函数，便于自检。

    ox.distance.nearest_nodes(return_dist=True) 单点返回 (node, dist_m)。
    调用方据 dist > SNAP_MAX_M 判定点是否落在步行图外。
    """
    import osmnx as ox
    node, dist = ox.distance.nearest_nodes(G, X=lng, Y=lat, return_dist=True)
    return int(node), float(dist)


# q 截断上限：防止超长输入撑 LIKE 模式或拖慢查询。60 字符盖住所有真实地名。
_SEARCH_Q_MAX = 60


def _like_escape(s: str) -> str:
    """转义 SQLite LIKE 的元字符 \\ % _，配合 `ESCAPE '\\'`。

    信任边界：q 直接进 LIKE，不转义的话查 "%" 匹配全表、"_" 当单字通配。
    H10 锁住这三个字符都被加反斜杠。
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_places(conn: sqlite3.Connection, q: str, lat: float, lng: float,
                  limit: int = 8) -> list[dict]:
    """在 places 表里按 q 搜索，返回最多 limit 条 [{name,lat,lng,kind}]。

    排序：前缀匹配 > 子串匹配；landmark > poi > intersection；同档离 (lat,lng) 近的优先。
    距离偏置是必需的：spruce/walnut 这类街名有几十条命中，没偏置候选全是远路口。
    """
    q = q[:_SEARCH_Q_MAX]
    esc = _like_escape(q)
    where_pat = f"%{esc}%"          # 子串匹配（WHERE 用）
    prefix_pat = f"{esc}%"          # 前缀匹配（排序用）
    rows = conn.execute(
        "SELECT name, lat, lng, kind FROM places "
        "WHERE name LIKE ? ESCAPE '\\' "
        "ORDER BY "
        "  CASE WHEN name LIKE ? ESCAPE '\\' THEN 0 ELSE 1 END, "
        "  CASE kind WHEN 'landmark' THEN 0 WHEN 'poi' THEN 1 ELSE 2 END, "
        "  (lat-?)*(lat-?) + (lng-?)*(lng-?) "
        "LIMIT ?",
        (where_pat, prefix_pat, lat, lat, lng, lng, limit),
    ).fetchall()
    return [{"name": r[0], "lat": r[1], "lng": r[2], "kind": r[3]} for r in rows]


def classify_divergence(short_nodes: list, safe_nodes: list,
                        short_len: float, safe_len: float) -> dict:
    """三态分叉语义。纯函数，不碰图/Flask。

    - "none"  节点序列完全相同 → 本区域风险差异小，推荐最短
    - "minor" 路线不同但不同路段占比 < MINOR_DIFF_RATIO → 隔街级微绕，弱化
    - "clear" 真分叉，高亮避开的高危路段

    diff_ratio = 1 - Jaccard(节点集)，即"这条安全路线有多少不走最短路的街"。
    detour_ratio 仍然返回（用来讲"代价多小"），但不再决定三态。
    """
    ns, nf = set(short_nodes), set(safe_nodes)
    union = ns | nf
    diff_ratio = 1 - len(ns & nf) / len(union) if union else 0.0
    detour_m = max(safe_len - short_len, 0.0)
    detour_ratio = detour_m / short_len if short_len else 0.0
    if short_nodes == safe_nodes:
        divergence = "none"
    elif diff_ratio < MINOR_DIFF_RATIO:
        divergence = "minor"
    else:
        divergence = "clear"
    return {
        "divergence": divergence,
        "divergent": divergence != "none",
        "diff_ratio": round(diff_ratio, 3),
        "detour_m": round(detour_m, 1),
        "detour_ratio": round(detour_ratio, 3),
    }


def build_map(
    routes: Optional[tuple[dict, dict]] = None,
    reported: Optional[tuple[float, float]] = None,
    avoided: Optional[list[dict]] = None,
) -> str:
    """folium 地图：浅色底图 + 两个可切换风险图层 + 路线。返回无 iframe 的完整 HTML。

    用 m.get_root().render() 替代 m._repr_html_() —— 前者返回顶层文档（无 iframe），
    注入的 JS 跑在父页面同文档，与 report 表单 / 图例 DOM 同源可联动。

    图层（右上自建 toggle 切换，见 _inject_frontend）：
    - Risk-colored streets（默认开）：边按 risk_normalized 上色 绿→黄→红。
    - Crime density heatmap（默认关）：原始犯罪点密度，~万级点故默认关保性能。
    - Avoided high-risk（默认开，仅 /route 有）：安全路线避开的高危段，红色粗线，
      画在路线之上（最后 add_to 压最上层），让"避开了哪里"肉眼可见。
    路线（蓝虚=最短、绿实=最安全）直接画在 m 上，不被图层 toggle 影响。
    reported: 上报点 (lat,lng)，由 app.js 读 window.SAFEPATH_REPORTED 定位高亮。
    """
    # zoom_control 挪右下：左上是搜索框/图例区，默认左上的 +/- 会压住。folium 0.20 接受位置串。
    m = folium.Map(location=PENN_CENTER, zoom_start=15, tiles=LIGHT_TILES, attr=TILES_ATTR,
                   zoom_control="bottomright")

    # 图层1：风险上色街道（只画 risk 较高的边——80k 条全画会卡，低风险边视觉本就绿色无信息）
    # 单个 GeoJson 装下所有边，不再逐条构造 folium.PolyLine（20000 个对象拖慢渲染）。
    risk_edges = folium.FeatureGroup(name="Risk-colored streets", show=True)
    risk_geojson = _load_edge_risk_colors()
    if risk_geojson["features"]:
        folium.GeoJson(
            risk_geojson,
            style_function=lambda f: {
                "color": f["properties"]["color"], "weight": 2.5, "opacity": 0.6,
            },
        ).add_to(risk_edges)
    risk_edges.add_to(m)

    # 图层2：犯罪密度热力图（默认关，点太多）
    heat = folium.FeatureGroup(name="Crime density heatmap", show=False)
    heat_pts = _load_crime_points()
    if heat_pts:
        HeatMap(heat_pts, radius=11, blur=16, max_zoom=16,
                min_opacity=0.25, max_val=10.0).add_to(heat)
    heat.add_to(m)

    # 路线（放进命名 FeatureGroup，show=True 永远可见；命名是为了 hover 联动拿变量名）。
    short_layer = safe_layer = None
    if routes:
        shortest, safest = routes
        short_layer = folium.FeatureGroup(name="Shortest route", show=True)
        safe_layer = folium.FeatureGroup(name="Safest route", show=True)
        if shortest["coords"]:
            folium.PolyLine(shortest["coords"], color=COLOR_SHORT, weight=7,
                            opacity=0.9, dash_array="8,8",
                            tooltip=f"Shortest ({shortest['length_m']} m)").add_to(short_layer)
        if safest["coords"]:
            folium.PolyLine(safest["coords"], color=COLOR_SAFE, weight=8,
                            opacity=0.95,
                            tooltip=f"Safest ({safest['length_m']} m)").add_to(safe_layer)
        short_layer.add_to(m)
        safe_layer.add_to(m)
        folium.Marker(safest["coords"][0], tooltip="Origin",
                      icon=folium.Icon(color="green", icon="play", prefix="fa")).add_to(m)
        folium.Marker(safest["coords"][-1], tooltip="Destination",
                      icon=folium.Icon(color="red", icon="flag", prefix="fa")).add_to(m)
        m.fit_bounds([safest["coords"][0], safest["coords"][-1]], padding=(24, 24))

    # 图层3：避开的高危段（红色粗线，最后 add_to 压在路线之上）。仅 /route 提供。
    avoided_layer = folium.FeatureGroup(name="Avoided high-risk", show=True)
    has_avoided = bool(avoided)
    if has_avoided:
        for seg in avoided:
            folium.PolyLine(seg["coords"], color=COLOR_DANGER, weight=7,
                            opacity=0.95,
                            tooltip=f"Avoided · {seg['length_m']} m").add_to(avoided_layer)
    avoided_layer.add_to(m)  # 最后加 → 最上层，不被路线盖住

    _inject_frontend(m, risk_edges, heat, avoided_layer, reported, has_avoided,
                     short_layer, safe_layer)
    return m.get_root().render()


def _inject_frontend(
    m: folium.Map,
    risk_edges: folium.FeatureGroup,
    heat: folium.FeatureGroup,
    avoided: folium.FeatureGroup,
    reported: Optional[tuple[float, float]],
    has_avoided: bool = False,
    short_layer: Optional[folium.FeatureGroup] = None,
    safe_layer: Optional[folium.FeatureGroup] = None,
) -> None:
    """把 folium 变量名 + 上报点交给外部 app.js，并注入自建图层 toggle 的 HTML/CSS。

    folium 的 LayerControl 与项目暗色玻璃 UI 格格不入，且无法让风险图例随图层开关联动 ——
    这里删掉默认控件，自建 toggle（app.js 绑定 addLayer/removeLayer）。
    变量名每次 build_map 变化（map_<hash>），必须用 get_name() 动态拿，绝不硬编码。
    ctx.avoided 给避开高危段图层名；ctx.has_avoided 控制第三个 toggle 按钮显隐（首页无路线=不显示）。
    ctx.short/safe 给两条路线图层名（hover route-card 联动加粗用，首页无路线时空串）。
    """
    root = m.get_root()
    short_name = short_layer.get_name() if short_layer else ""
    safe_name = safe_layer.get_name() if safe_layer else ""
    ctx = (f"window.SAFEPATH_CTX={{map:'{m.get_name()}',risk:'{risk_edges.get_name()}',"
           f"heat:'{heat.get_name()}',avoided:'{avoided.get_name()}',"
           f"has_avoided:{'true' if has_avoided else 'false'},"
           f"short:'{short_name}',safe:'{safe_name}'}};")
    snippet = ctx
    if reported is not None:
        snippet += f"window.SAFEPATH_REPORTED=[{reported[0]:.6f},{reported[1]:.6f}];"
    root.script.add_child(folium.Element(snippet))


# risk_normalized → 颜色（绿安全→黄→红危险）。端点与 style.css .legend-bar 锁步。
_RISK_GREEN = (0x15, 0x80, 0x3d)
_RISK_YELLOW = (0xca, 0x8a, 0x04)
_RISK_RED = (0xdc, 0x26, 0x26)


def _hex_blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(round(a[i] + (b[i] - a[i]) * t) for i in range(3)))


def risk_color(rn: float) -> str:
    """0=绿(安全) 0.5=黄 1=红(危险)。线性 hex 混合。"""
    rn = max(0.0, min(1.0, rn))
    if rn < 0.5:
        return _hex_blend(_RISK_GREEN, _RISK_YELLOW, rn / 0.5)
    return _hex_blend(_RISK_YELLOW, _RISK_RED, (rn - 0.5) / 0.5)


# 只画 risk_normalized 最高的 N 条边（跳过低风险街区——视觉绿色无信息且拖慢渲染）。
# ponytail: 曾用 risk_normalized > 0.4 阈值，但归一化换成百分位排名后 0.4 = 全图 60% 的边
# （浏览器卡死）。改 top-N：取最高的 20000 条，与分布形状无关，将来再改归一化也不失效。
_RISK_DRAW_LIMIT = 20000


def _load_edge_risk_colors() -> dict:
    """读 edge_risk → GeoJSON FeatureCollection 供风险街道图层。取 risk 最高的 top-N 条。

    改用单个 folium.GeoJson 而非 20000 个独立 folium.PolyLine：旧实现里 build_map 的
    for 循环要构造 20000 个 PolyLine 对象各自序列化进 HTML，是渲染耗时的主要来源；
    GeoJson 把同一批坐标打包成一个 FeatureCollection，一次性交给 leaflet 渲染。
    索引扫描 vs 全表排序、单对象 vs 多对象的具体收益待重新实测（旧的"only省0.1s"结论
    是针对 PolyLine 方案量的，GeoJson 换法后不再适用）。

    GeoJSON 坐标顺序是 [lng, lat]（与 PolyLine 用的 (lat,lng) 相反）。
    """
    empty = {"type": "FeatureCollection", "features": []}
    if not os.path.exists(R.DB_PATH):
        return empty
    try:
        conn = sqlite3.connect(R.DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT u,v,k,risk_normalized FROM edge_risk "
            "ORDER BY risk_normalized DESC LIMIT ?",
            (_RISK_DRAW_LIMIT,)).fetchall()
        conn.close()
    except sqlite3.OperationalError:  # 表不存在（prep 未跑）→ 空图层
        return empty
    if not rows:
        return empty
    G = R.load_weighted_graph()  # 进程内缓存的 GraphML，解析 u,v→坐标
    features = []
    for r in rows:
        try:
            d = G.edges[r["u"], r["v"], r["k"]]
            if "geometry" in d:
                xs, ys = d["geometry"].xy
                coords = list(zip(xs.tolist(), ys.tolist()))
            else:
                coords = [(G.nodes[r["u"]]["x"], G.nodes[r["u"]]["y"]),
                          (G.nodes[r["v"]]["x"], G.nodes[r["v"]]["y"])]
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {"color": risk_color(r["risk_normalized"])},
            })
        except KeyError:  # 边在风险表但不在图（缓存不一致）→ 跳过
            continue
    return {"type": "FeatureCollection", "features": features}


@functools.lru_cache(maxsize=1)
def _load_crime_points() -> tuple[tuple[float, float, float], ...]:
    """读 crime_points → [(lat,lng,severity)] 供热力图层。缓存。

    crime_points 表本身已在 prep_data 阶段采样到 ≤5000 行（HEATMAP_SAMPLE_N），
    这里直接读全表，不再 ORDER BY RANDOM() —— 那是对 10万+ 行做全表随机排序的
    经典反模式，每次冷启动都要重付。lru_cache 避免同进程内每次渲染重读。
    返回 tuple 供 lru_cache hash。
    """
    if not os.path.exists(R.DB_PATH):
        return ()
    try:
        conn = sqlite3.connect(R.DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT lat,lng,severity FROM crime_points").fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return ()
    return tuple((r["lat"], r["lng"], r["severity"]) for r in rows)


@app.get("/search")
def search():
    """搜索框的数据源。GET /search?q=&lat=&lng= → {results:[...]} 最多 8 条。

    q 短于 2 字符 → 返回全部 12 个 PRESET_LOCATIONS 当"推荐地点"。
    places 表不存在（老库/没跑 prep_data）→ 回落到 PRESET_LOCATIONS 上 Python 子串过滤。
    任何失败都返回 200 + 空结果或降级结果，绝不 5xx——搜索框不能把首页打挂。
    """
    q = (request.args.get("q") or "").strip()
    # lat/lng 解析失败回落地图中心，不报错（前端总会带，但防御性编程）。
    try:
        lat = float(request.args.get("lat", PENN_CENTER[0]))
        lng = float(request.args.get("lng", PENN_CENTER[1]))
    except (TypeError, ValueError):
        lat, lng = PENN_CENTER

    if len(q) < 2:
        # 空查询/单字符 → 推荐预设地标（搜索框刚聚焦时填候选）。
        return jsonify(results=[
            {"name": name, "lat": c[0], "lng": c[1], "kind": "landmark"}
            for name, c in R.PRESET_LOCATIONS.items()
        ])

    try:
        conn = sqlite3.connect(R.DB_PATH)
        try:
            results = search_places(conn, q, lat, lng)
        finally:
            conn.close()
        return jsonify(results=results)
    except sqlite3.OperationalError:
        # places 表不存在 → 在内存里过滤 PRESET_LOCATIONS。候选池缩到 12 个但功能不挂。
        ql = q.lower()
        results = [
            {"name": name, "lat": c[0], "lng": c[1], "kind": "landmark"}
            for name, c in R.PRESET_LOCATIONS.items()
            if ql in name.lower()
        ][:8]
        return jsonify(results=results)


@app.get("/")
def index():
    reported_raw = request.args.get("reported", "").strip()
    reported = None
    if reported_raw:
        try:
            la, lo = reported_raw.split(",")
            reported = (float(la), float(lo))
        except ValueError:
            reported_raw = ""  # 格式坏 → 不定位高亮，但首页照常出地图
    # 首页始终出地图：定位按钮有图、上报后有可视化反馈、首页观感提升。
    map_html = build_map(reported=reported)
    return render_template("index.html", map_html=map_html, stats=None,
                           reported_raw=reported_raw,
                           last_origin_label="", last_origin_lat="", last_origin_lng="",
                           last_dest_label="", last_dest_lat="", last_dest_lng="")


@app.post("/route")
def route():
    Gw = R.load_weighted_graph()

    # 唯一输入：搜索框/地图取点产出的 hidden 坐标。坐标缺失 → 提示回去选。
    o_lat = (request.form.get("origin_lat") or "").strip()
    o_lng = (request.form.get("origin_lng") or "").strip()
    d_lat = (request.form.get("dest_lat") or "").strip()
    d_lng = (request.form.get("dest_lng") or "").strip()
    # 空串是 falsy；不能 all(s)（all('')==True）。四字段都非空才算齐了。
    if not (o_lat and o_lng and d_lat and d_lng):
        flash("Search or pick both an origin and a destination.", "error")
        return redirect(url_for("index"))

    # 信任边界：先解析、再 bbox、再 snap 距离守卫。任何一步失败 → flash 回首页。
    try:
        op = (float(o_lat), float(o_lng))
        dp = (float(d_lat), float(d_lng))
    except ValueError:
        flash("Picked coordinates must be numbers.", "error")
        return redirect(url_for("index"))
    for la, lo in (op, dp):
        (lat_min, lng_min), (lat_max, lng_max) = PHILLY_BBOX
        if not (lat_min <= la <= lat_max and lng_min <= lo <= lng_max):
            flash("Picked point is outside the Philadelphia area.", "error")
            return redirect(url_for("index"))
    o_node, o_dist = snap_point(Gw, op[0], op[1])
    d_node, d_dist = snap_point(Gw, dp[0], dp[1])
    if o_dist > SNAP_MAX_M or d_dist > SNAP_MAX_M:
        flash("Picked point is too far from any walkable street (outside the map).", "error")
        return redirect(url_for("index"))
    if o_node == d_node:
        flash("Origin and destination are the same.", "error")
        return redirect(url_for("index"))
    o, d = o_node, d_node

    # 回显输入框：C4。name 截 120 字符防超长（Jinja 自动转义处理 HTML 注入）。
    last_origin_label = (request.form.get("origin_name") or "")[:120]
    last_dest_label = (request.form.get("dest_name") or "")[:120]

    shortest, safest = R.compute_both_routes(Gw, o, d)

    div = classify_divergence(shortest["nodes"], safest["nodes"],
                              shortest["length_m"], safest["length_m"])
    short_risk, safe_risk = shortest["total_risk"], safest["total_risk"]
    # 风险降幅：(最短路径风险 - 安全路径风险) / 最短路径风险，可能为负（safe 反而更险时兜底为 0）
    risk_reduction = (short_risk - safe_risk) / short_risk if short_risk else 0.0

    # 安全路线真正避开的高危段（最路上有、安全路上无、risk>p90）。聚合米数 + 地图红高亮。
    avoided = R.avoided_hot_segments(Gw, shortest["nodes"], safest["nodes"])
    # 反方向：安全路**新进入**的高危段。起点/终点本身是高犯罪锚点时（Market St Corridor），
    # 安全路离开 5 段又踩进另 5 段——只报 avoided 会说出 "Avoids 5 … 5 → 5"。
    entered = R.avoided_hot_segments(Gw, safest["nodes"], shortest["nodes"])
    # 绕不掉的高危段：safest 自己仍留在高危段里、且落在路径端点附近（前/后 10%）。
    # 起点或终点本身就是高犯罪锚点时，那段避不开——诚实标注而非假装绕开了。
    n_edges = max(len(safest["nodes"]) - 1, 1)
    edge_band = max(1, n_edges // 10)
    unavoidable = sum(1 for i, _ in R.hot_edge_indices(Gw, safest["nodes"])
                      if i < edge_band or i >= n_edges - edge_band)
    # 高危米数降幅：段数净差为 0 时（两端都在高危区）它是唯一站得住的数字。
    hot_m_cut = (shortest["hot_m"] - safest["hot_m"]) / shortest["hot_m"] if shortest["hot_m"] else 0.0

    stats = {
        "shortest": {"length_m": shortest["length_m"], "total_risk": shortest["total_risk"],
                     "steps": max(len(shortest["nodes"]) - 1, 0),
                     "hot_blocks": shortest["hot_blocks"], "hot_m": shortest["hot_m"]},
        "safest": {"length_m": safest["length_m"], "total_risk": safest["total_risk"],
                   "steps": max(len(safest["nodes"]) - 1, 0),
                   "hot_blocks": safest["hot_blocks"], "hot_m": safest["hot_m"]},
        "divergence": div["divergence"],
        "divergent": div["divergent"],
        "diff_ratio": div["diff_ratio"],
        "detour_m": div["detour_m"],
        "detour_ratio": div["detour_ratio"],
        # 主指标：避开了几个高危路段（risk>p90）。可能为负（安全路更长、多经过几段
        # 中危）→ 截到 0，模板据此决定说不说这句。
        "hot_avoided": max(shortest["hot_blocks"] - safest["hot_blocks"], 0),
        "risk_reduction": round(max(risk_reduction, 0.0), 3),
        # 实际避开的高危段明细（带坐标供地图红高亮）+ 总米数。
        "avoided": avoided,
        "avoided_count": len(avoided),
        "avoided_m": round(sum(a["length_m"] for a in avoided), 1),
        # 新进入的高危段：>0 时不能说 "Avoids N"（离开 N 段又踩进 M 段），改用米数口径。
        "entered_count": len(entered),
        "hot_m_cut": round(max(hot_m_cut, 0.0), 3),
        # 起终点附近绕不掉的高危段数（诚实标注）。
        "unavoidable": unavoidable,
    }
    map_html = build_map((shortest, safest), avoided=avoided)
    return render_template("index.html", map_html=map_html, stats=stats,
                           last_origin_label=last_origin_label,
                           last_origin_lat=op[0], last_origin_lng=op[1],
                           last_dest_label=last_dest_label,
                           last_dest_lat=dp[0], last_dest_lng=dp[1])


@app.post("/report")
def report():
    rtype = request.form.get("type", "")
    lat_raw = request.form.get("lat", "").strip()
    lng_raw = request.form.get("lng", "").strip()
    note = (request.form.get("note") or "").strip()

    if rtype not in ALLOWED_REPORT_TYPES:
        flash("Unknown report type.", "error")
        return redirect(url_for("index"))
    try:
        lat = float(lat_raw); lng = float(lng_raw)
    except ValueError:
        flash("Latitude/longitude must be numbers.", "error")
        return redirect(url_for("index"))
    (lat_min, lng_min), (lat_max, lng_max) = PHILLY_BBOX
    if not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max):
        flash("Coordinates outside the Philadelphia area.", "error")
        return redirect(url_for("index"))
    if len(note) > 500:
        flash("Note too long (max 500 chars).", "error")
        return redirect(url_for("index"))

    conn = sqlite3.connect(R.DB_PATH)
    conn.execute(
        "INSERT INTO reported_incidents (lat, lng, type, note) VALUES (?, ?, ?, ?)",
        (lat, lng, rtype, note or None),
    )
    conn.commit()
    conn.close()
    # 上报改变边权重，缓存的加权图作废——不清就是"上报了但路线不变"的静默失效。
    R.load_weighted_graph.cache_clear()
    flash(f"Reported {rtype.replace('_', ' ')} at ({lat}, {lng}). Future routes will avoid it.", "ok")
    return redirect(url_for("index", reported=f"{lat:.6f},{lng:.6f}"))


@app.get("/about")
def about():
    return render_template("about.html")


# ---- sanity check (assert, 无框架) ----

def _check_divergence_logic():
    """H5: classify_divergence 三态按「不同路段占比」而非绕路比。"""
    same = [1, 2, 3, 4, 5]
    assert classify_divergence(same, same, 1000.0, 1000.0)["divergence"] == "none"

    # 隔街微绕：换 1 个节点 → 并集 6、交集 4 → diff=1/3。改成换 0 个不现实，
    # 用 10 节点路换 1 个：并集 11、交集 9 → diff≈0.18 (<0.25) → minor
    a = list(range(10))
    b = list(range(9)) + [99]
    assert classify_divergence(a, b, 1000.0, 1010.0)["divergence"] == "minor"

    # 真分叉：只共享起终点 → diff 高，即便绕路仅 0.2%（网格的典型情形）
    c = [0, 101, 102, 103, 104, 105, 106, 107, 108, 9]
    d = classify_divergence(a, c, 1000.0, 1002.0)
    assert d["divergence"] == "clear", d
    assert d["detour_ratio"] == 0.002              # 绕路极小但仍判 clear —— 本次重构的重点
    assert 0.8 < d["diff_ratio"] <= 1.0, d

    # 绕路很大但路线几乎相同 → 仍是 minor（旧逻辑会误判 clear）
    e = classify_divergence(a, b, 1000.0, 1500.0)
    assert e["divergence"] == "minor" and e["detour_ratio"] == 0.5, e

    # safe 反而更短 → detour 截 0，不影响三态
    f = classify_divergence(a, c, 1000.0, 950.0)
    assert f["detour_m"] == 0.0 and f["detour_ratio"] == 0.0 and f["divergence"] == "clear"
    print("OK H5: classify_divergence 三态 (按 diff_ratio，非 detour_ratio)")


def _check_snap_guard():
    """H8: snap_point 距离守卫——图内点 snap 近、图外远点 snap 远（>SNAP_MAX_M）。

    锁住"点选起终点落在 3.5km 图外时被拒"的判定依据：snap 距离 > 300m。
    谁改坏了 snap_point 的 (node, dist) 返回顺序，或挪了 SNAP_MAX_M，这条就红。
    """
    Gw = R.load_weighted_graph()
    # Penn 中心：路网密集，snap 距离必然 < 300m
    _, near = snap_point(Gw, PENN_CENTER[0], PENN_CENTER[1])
    assert near < SNAP_MAX_M, near
    # 图外远点（费城东北角外，离 3.5km 图中心 >10km）
    _, far = snap_point(Gw, 40.0000, -75.0000)
    assert far > SNAP_MAX_M, far
    print(f"OK H8: snap 距离守卫 (图内 {near:.0f}m < {SNAP_MAX_M:.0f}m; 图外 {far:.0f}m > 上限)")


def _check_like_escape():
    """H10: _like_escape 把 LIKE 元字符 \\ % _ 都加反斜杠。

    不转义的话查 "%" 匹配全表、"_" 当单字通配、"\\\\" 提前结束转义序列。
    搭配 `ESCAPE '\\'`，被转义的字符当字面量。
    """
    assert _like_escape("%") == "\\%"
    assert _like_escape("_") == "\\_"
    assert _like_escape("\\") == "\\\\"       # 反斜杠自身先转义，否则 "\\%" 会被解读成字面 %
    # 普通字符不动
    assert _like_escape("Spruce St") == "Spruce St"
    # 组合：含 % 的真实输入
    assert _like_escape("50% off") == "50\\% off"

    # 端到端：内存库里塞一行名字带 %，LIKE '\\%' ESCAPE '\\' 只匹配字面 %
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE places (name TEXT, lat REAL, lng REAL, kind TEXT)")
    conn.execute("INSERT INTO places VALUES ('50% off sale', 0, 0, 'poi')")
    conn.execute("INSERT INTO places VALUES ('Spruce St', 0, 0, 'landmark')")
    # 转义后查 "%"：只命中字面含 % 的那行，不命中全表
    esc = _like_escape("%")
    hit = conn.execute("SELECT name FROM places WHERE name LIKE ? ESCAPE '\\'",
                       (f"%{esc}%",)).fetchall()
    assert len(hit) == 1 and hit[0][0] == "50% off sale", hit
    conn.close()
    print("OK H10: _like_escape 转义 % _ \\ (查 % 不匹配全表)")


def _check_search_places():
    """H11: search_places 排序契约——前缀胜子串、landmark 胜 intersection、同档近胜远。

    用内存库塞受控数据，断言三级排序都成立。
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE places (name TEXT, lat REAL, lng REAL, kind TEXT)")
    # 中心点 (0, 0)。所有候选都含 "spruce"（子串匹配通过 WHERE）。
    rows = [
        # (name, lat, lng, kind) —— 期望排名见下方注释
        ("Spruce Tower", 10.0, 0.0, "landmark"),       # 前缀 + landmark，但很远
        ("Spruce Hill", 0.1, 0.0, "landmark"),          # 前缀 + landmark + 近 —— 应排第1
        ("Hill at Spruce", 0.0, 0.1, "landmark"),       # 仅子串 + landmark + 近
        ("Spruce & 52nd", 0.2, 0.0, "intersection"),    # 前缀 + intersection（kind 劣于 landmark）
        ("Spruce & 53rd", 5.0, 0.0, "intersection"),    # 前缀 + intersection + 远
    ]
    conn.executemany("INSERT INTO places VALUES (?,?,?,?)", rows)

    res = search_places(conn, "spruce", 0.0, 0.0, limit=8)
    names = [r["name"] for r in res]
    # 1) 前缀优先于子串：Spruce Hill（前缀）排在 Hill at Spruce（子串）前
    assert names.index("Spruce Hill") < names.index("Hill at Spruce"), names
    # 2) 同为前缀时 landmark 胜 intersection：Spruce Hill/Tower 排在 Spruce & xx 前
    assert names.index("Spruce Hill") < names.index("Spruce & 52nd"), names
    # 3) 同档（前缀+landmark，或前缀+intersection）近的胜远：
    #    landmark 组里 Spruce Hill(0.1) 近于 Spruce Tower(10.0)
    assert names.index("Spruce Hill") < names.index("Spruce Tower"), names
    #    intersection 组里 Spruce & 52nd(0.2) 近于 Spruce & 53rd(5.0)
    assert names.index("Spruce & 52nd") < names.index("Spruce & 53rd"), names
    conn.close()
    print("OK H11: search_places 排序 (前缀>子串, landmark>intersection, 近>远)")


def _check_search_places_fallback():
    """H12: places 表缺失 → search_places 抛 OperationalError，/search 回落 PRESET_LOCATIONS。

    锁住"老库没 places 表时搜索框不 500"。用不存在的 DB 路径触发。
    """
    # 构造一个空库（只有 edge_risk 没有 places）→ search_places 必须 raise OperationalError
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE edge_risk (u INT, v INT, k INT, risk REAL)")
    raised = False
    try:
        search_places(conn, "spruce", 0.0, 0.0)
    except sqlite3.OperationalError:
        raised = True
    conn.close()
    assert raised, "search_places 在 places 表缺失时应抛 OperationalError"

    # /search 的回落逻辑：表缺失时过滤 PRESET_LOCATIONS。模拟前端调 "Van"。
    ql = "van"
    fallback = [
        name for name in R.PRESET_LOCATIONS if ql in name.lower()
    ]
    assert "Van Pelt Library" in fallback, fallback   # 子串命中
    assert len(fallback) >= 1
    print("OK H12: places 表缺失 → /search 回落 PRESET_LOCATIONS 过滤 (不 500)")


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        _check_divergence_logic()
        _check_snap_guard()
        _check_like_escape()
        _check_search_places()
        _check_search_places_fallback()
        print("app self-checks passed")
    else:
        app.run(debug=True)
