"""SafePath — 路由 (Graph)。

最短 vs 最安全：同一图，两种边权重下跑 Dijkstra。
运行时不碰 pandas，只读 SQLite 把 safety_weight 设到图边属性。
"""
from __future__ import annotations

import functools
import math
import os
import sqlite3
from typing import Optional

import networkx as nx
import osmnx as ox

GRAPH_PATH = "data/walk.graphml"
DB_PATH = "data/safepath.db"
ALPHA = 1.5

# 预设地标（下拉起终点）。坐标为 Penn 周边真实地标近似。
# 含一个高犯罪热区点（Market St 走廊）——demo 选它↔西校园点可看到最短 vs 最安全分叉。
PRESET_LOCATIONS: dict[str, tuple[float, float]] = {
    "Van Pelt Library": (39.9524, -75.1946),
    "30th Street Station": (39.9566, -75.1822),
    "Huntsman Hall": (39.9528, -75.1932),
    "Franklin Field": (39.9581, -75.1896),
    "Drexel Main": (39.9570, -75.1889),
    "Penn Park": (39.9476, -75.1869),
    "Locust Walk": (39.9509, -75.1974),
    "Market St Corridor": (39.9550, -75.1833),
    # 3.5km 扩区新增高犯罪社区锚点（均 ≤3.5km，haversine 核验过）
    "Mantua (35th & Haverford)": (39.9640, -75.1960),
    "Mill Creek (44th & Lancaster)": (39.9635, -75.2095),
    "Point Breeze (20th & Washington)": (39.9340, -75.1810),
    "Brewerytown (31st & Girard)": (39.9760, -75.1860),
}

# 用户上报类型的乘性 bump（POST /report → load_risk_weights 时应用）
REPORT_SEVERITY = {
    "sidewalk_damage": 1.5,
    "no_ada_ramp": 1.2,
    "poor_lighting": 1.0,
    "unsafe_crossing": 1.3,
}
REPORT_BUMP_FACTOR = 5.0  # safety_weight *= (1 + 5.0 * severity[type])
# ponytail: 计划原值 0.3 在密集路网上 bump 单条边几乎不可见（替代路径等长）。
# 5.0 是让单条/几条上报能撬动 safest 路线节点序列的经验值；升级路径=学到的经验权重。


def load_graph(path: str = GRAPH_PATH) -> nx.MultiDiGraph:
    return ox.load_graphml(path)


@functools.lru_cache(maxsize=1)
def load_weighted_graph() -> nx.MultiDiGraph:
    """带 safety_weight/risk 的图，进程内缓存。

    31MB GraphML 解析 + 80550 条 edge_risk 赋权 + 上报 nearest_edges 建 KDTree
    实测 ~3s，之前每个 GET / 和 POST /route 都重付。上报会改变权重，
    故 POST /report 必须 `load_weighted_graph.cache_clear()`。
    """
    return load_risk_weights(load_graph())


def load_risk_weights(G: nx.MultiDiGraph, db_path: str = DB_PATH,
                      alpha: float = ALPHA) -> nx.MultiDiGraph:
    """读 edge_risk + reported_incidents，就地设边 safety_weight。

    - 有 edge_risk 记录的边：length*(1+alpha*risk_normalized)，并把 risk_normalized
      存到边属性 `risk` 供高危路段计数（compute_route 的 hot_blocks）。
    - 用户上报：nearest_edges 找最近边，safety_weight *= (1+0.3*sev)。
    - 其余边：safety_weight = length（零犯罪）、risk = 0。

    同时把 risk 的 p90 存到 `G.graph["risk_p90"]` —— 高危路段的判定阈值随数据走，
    不硬编码常数（重跑 prep_data 换了年份/区域，阈值自动跟着变）。
    """
    # 默认：safety_weight = length（米）。GraphML 读回的 length 是 float。
    for u, v, k, d in G.edges(keys=True, data=True):
        d["safety_weight"] = d.get("length", 1.0)
        d["risk"] = 0.0

    G.graph["risk_p90"] = 1.0  # 无 DB 时没有边算高危（risk 恒 0，不可能 > 1.0）
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        risks: list[float] = []
        for r in conn.execute("SELECT u,v,k,risk_normalized FROM edge_risk"):
            d = G.edges[r["u"], r["v"], r["k"]]
            L = d.get("length", 1.0)
            d["safety_weight"] = L * (1 + alpha * r["risk_normalized"])
            d["risk"] = r["risk_normalized"]
            risks.append(r["risk_normalized"])
        if risks:
            risks.sort()
            G.graph["risk_p90"] = risks[int(0.9 * len(risks))]
        # 用户上报叠加
        rows = list(conn.execute("SELECT lat,lng,type FROM reported_incidents"))
        conn.close()
        if rows:
            for r in rows:
                try:
                    ue = ox.distance.nearest_edges(G, X=r["lng"], Y=r["lat"])
                    u, v, k = ue[0], ue[1], (ue[2] if len(ue) > 2 else 0)
                    sev = REPORT_SEVERITY.get(r["type"], 1.0)
                    if (u, v, k) in G.edges:
                        G.edges[u, v, k]["safety_weight"] *= (1 + REPORT_BUMP_FACTOR * sev)
                except Exception:
                    continue  # ponytail: 上报坐标完全在图外时跳过；升级路径=投影后更稳健的 snap
    return G


def hot_edge_indices(G_w: nx.MultiDiGraph, nodes: list) -> list[tuple[int, float]]:
    """路径上 risk > p90 的边：[(下标, 长度 m), ...]。

    hot_blocks 计数、高危米数、"绕不掉的段在不在端点附近" 三处共用同一把尺子。
    ponytail: 平行边取 max(risk) 偏保守 / min(length) 与 Dijkstra 选边一致。
    """
    p90 = G_w.graph.get("risk_p90", 1.0)
    out: list[tuple[int, float]] = []
    for i in range(len(nodes) - 1):
        edata = G_w.get_edge_data(nodes[i], nodes[i + 1])
        if not edata:
            continue
        if max(d.get("risk", 0.0) for d in edata.values()) > p90:
            out.append((i, min(d.get("length", 1.0) for d in edata.values())))
    return out


def compute_route(G_w: nx.MultiDiGraph, origin_id: int, dest_id: int,
                  mode: str = "safest") -> dict:
    """weight='length'（最短）或 'safety_weight'（最安全）。nx.shortest_path 跑 Dijkstra。

    除长度/总风险外还数 `hot_blocks` —— 路径上 risk > p90 的路段数。总风险是全路径
    求和、风险场平滑，两条路差距被摊薄到 1-2%（实测）；高危路段数才看得出差别
    （实测 Penn Park→Mill Creek: 13 → 6）。这是对比展示的主指标。
    """
    weight = "safety_weight" if mode == "safest" else "length"
    nodes = nx.shortest_path(G_w, origin_id, dest_id, weight=weight)
    coords = [(G_w.nodes[n]["y"], G_w.nodes[n]["x"]) for n in nodes]  # (lat,lng)
    # MultiDiGraph 可能有平行边；Dijkstra 按 weight 最小那条选，这里取同 weight 聚合保持一致。
    def _edge_agg(i: int, attr: str) -> float:
        u, v = nodes[i], nodes[i + 1]
        return min(d.get(attr, 0.0) for d in G_w.get_edge_data(u, v).values())
    length_m = sum(_edge_agg(i, "length") for i in range(len(nodes) - 1))
    total_risk = sum(_edge_agg(i, "safety_weight") for i in range(len(nodes) - 1))
    hot = hot_edge_indices(G_w, nodes)
    return {"mode": mode, "nodes": nodes, "coords": coords,
            "length_m": round(length_m, 1), "total_risk": round(total_risk, 1),
            "hot_blocks": len(hot),
            # 高危米数：段数在起点即高危区时两路可能同为 5（净差 0），米数仍有真实差距
            # （实测 Market St→Mantua 115.1 → 55.6 m），是那种场景下唯一站得住的数字。
            "hot_m": round(sum(m for _, m in hot), 1)}


def compute_both_routes(G_w: nx.MultiDiGraph, origin_id: int,
                        dest_id: int) -> tuple[dict, dict]:
    return compute_route(G_w, origin_id, dest_id, "shortest"), \
        compute_route(G_w, origin_id, dest_id, "safest")


def avoided_hot_segments(G_w: nx.MultiDiGraph, on_nodes: list,
                         off_nodes: list) -> list[dict]:
    """在 on_nodes 路径上、不在 off_nodes 路径上、且 risk > p90 的边。方向可交换：

    - `(shortest, safest)` → 安全路**避开**的高危段。
    - `(safest, shortest)` → 安全路**新进入**的高危段。两侧都要看：起点/终点本身
      是高犯罪锚点时（实测 Market St Corridor），安全路离开 5 段又踩进另 5 段，
      只报"避开"侧会说出 "Avoids 5 … 5 → 5" 这种自相矛盾的话。

    每条返回 {coords, length_m, risk, name}：
    - coords/length/risk 复用 compute_route 的 _edge_agg 约定：平行边 risk 取 max、
      length 取 min（与 Dijkstra 选边 + hot_blocks 计数同一把尺子）。
    - name 取边的 OSM 'name'，取不到给 None（风险 top 边里只有 ~15% 有 name，
      故调用方应按聚合数字 + 地图高亮呈现，不要做逐条街名列表）。

    "两路共有"判定：on 路的相邻节点对 (u,v) 若在 off 路里也以 u→v 出现，则不计入。
    """
    p90 = G_w.graph.get("risk_p90", 1.0)
    off_edges: set[tuple] = set()
    for i in range(len(off_nodes) - 1):
        off_edges.add((off_nodes[i], off_nodes[i + 1]))

    out: list[dict] = []
    for i in range(len(on_nodes) - 1):
        u, v = on_nodes[i], on_nodes[i + 1]
        if (u, v) in off_edges:
            continue  # 两路共有，不算差异
        edata = G_w.get_edge_data(u, v)
        if not edata:
            continue
        risk = max(d.get("risk", 0.0) for d in edata.values())
        if risk <= p90:
            continue
        length = min(d.get("length", 1.0) for d in edata.values())
        name = next((d.get("name") for d in edata.values() if d.get("name")), None)
        # 坐标优先用边几何（curved），否则退回两端点直线
        geom = next((d.get("geometry") for d in edata.values() if d.get("geometry")), None)
        if geom is not None:
            xs, ys = geom.xy
            coords = list(zip(ys.tolist(), xs.tolist()))
        else:
            coords = [(G_w.nodes[u]["y"], G_w.nodes[u]["x"]),
                      (G_w.nodes[v]["y"], G_w.nodes[v]["x"])]
        out.append({"coords": coords, "length_m": round(length, 1),
                    "risk": round(risk, 3), "name": name})
    return out


# ---- sanity checks (assert, 无框架) ----

def _check_safety_weight_formula():
    """H2: risk_norm=0 → sw==length; risk_norm=1 → sw==2.5×length（α=1.5 锁定）。"""
    length = 100.0
    assert math.isclose(length * (1 + ALPHA * 0.0), length)
    assert math.isclose(length * (1 + ALPHA * 1.0), length * 2.5)
    print("OK H2: safety_weight 公式 (0→1×, 1→2.5×)")


def _check_route_divergence():
    """H3: 合成"短而险"+"长而安"图，两种 weight 下路径不同。"""
    G = nx.MultiDiGraph()
    G.add_node("A", x=0.0, y=0.0)
    G.add_node("B", x=0.001, y=0.0)
    G.add_node("C", x=0.0, y=0.001)
    # 直边：短(length=100)但险(safety=400)；
    # 绕路：长(length 合 300)但安(safety 合 300 < 400)。
    G.add_edge("A", "B", 0, length=100.0, safety_weight=400.0)
    G.add_edge("A", "C", 0, length=200.0, safety_weight=200.0)
    G.add_edge("C", "B", 0, length=100.0, safety_weight=100.0)
    short = compute_route(G, "A", "B", "shortest")
    safe = compute_route(G, "A", "B", "safest")
    assert short["nodes"] == ["A", "B"], short["nodes"]      # 直边最短(100<300)
    assert safe["nodes"] == ["A", "C", "B"], safe["nodes"]   # 绕路总 safety 更低(300<400)
    print("OK H3: shortest≠safest 路径分叉")


def _check_hot_blocks():
    """H6: hot_blocks 只数 risk>p90 的路段，且平行边取 max 不退化成 key=0。"""
    G = nx.MultiDiGraph()
    G.graph["risk_p90"] = 0.5
    for n, (x, y) in {"A": (0.0, 0.0), "B": (0.001, 0.0), "C": (0.0, 0.001)}.items():
        G.add_node(n, x=x, y=y)
    # A→B 直边：短(100)但两段高危；A→C→B 绕路：长(300)但只有 1 段高危。
    G.add_edge("A", "B", 0, length=100.0, safety_weight=400.0, risk=0.9)
    G.add_edge("A", "C", 0, length=200.0, safety_weight=200.0, risk=0.6)
    G.add_edge("C", "B", 0, length=100.0, safety_weight=100.0, risk=0.1)
    assert compute_route(G, "A", "B", "shortest")["hot_blocks"] == 1
    assert compute_route(G, "A", "B", "safest")["hot_blocks"] == 1

    # 平行边：key=0 低危 / key=1 高危。取 max → 仍计为高危（key=0 会漏判）。
    H = nx.MultiDiGraph()
    H.graph["risk_p90"] = 0.5
    H.add_node("A", x=0.0, y=0.0); H.add_node("B", x=0.001, y=0.0)
    H.add_edge("A", "B", 0, length=100.0, safety_weight=100.0, risk=0.1)
    H.add_edge("A", "B", 1, length=100.0, safety_weight=100.0, risk=0.9)
    assert compute_route(H, "A", "B", "shortest")["hot_blocks"] == 1

    # p90 缺省（无 DB）：阈值 1.0，risk 恒 ≤1 → 零高危，不误报。
    K = nx.MultiDiGraph()
    K.add_node("A", x=0.0, y=0.0); K.add_node("B", x=0.001, y=0.0)
    K.add_edge("A", "B", 0, length=100.0, safety_weight=100.0, risk=0.0)
    assert compute_route(K, "A", "B", "shortest")["hot_blocks"] == 0
    print("OK H6: hot_blocks 计数 (p90 阈值 / 平行边 max / 无 DB 兜底)")


def _check_avoided_hot_segments():
    """H7: avoided_hot_segments 只返回"最短路独有且高危"的边。

    合成图（p90=0.5）：
      A→B(直,高危)  A→C→B(绕,安全)  B→D(两路共有,高危)
    shortest = A,B,D ；safest = A,C,B,D
    - A→B：最路独有 + 高危(0.9>0.5) → 计入
    - B→D：两路共有 → 不计入（即使高危）
    - A→C / C→B：不在最路上 → 不出现
    交换方向 (safe, short) 得"新进入"侧：这里 safest 独有的 A→C / C→B 都是低危，
    故为空 —— 锁住"方向交换即得另一侧"这个契约（app.py 的 entered 计数依赖它）。
    """
    G = nx.MultiDiGraph()
    G.graph["risk_p90"] = 0.5
    for n, (x, y) in {"A": (0.0, 0.0), "B": (0.001, 0.0),
                      "C": (0.0, 0.001), "D": (0.002, 0.0)}.items():
        G.add_node(n, x=x, y=y)
    G.add_edge("A", "B", 0, length=100.0, safety_weight=400.0, risk=0.9)   # 直边高危
    G.add_edge("A", "C", 0, length=200.0, safety_weight=200.0, risk=0.1)
    G.add_edge("C", "B", 0, length=100.0, safety_weight=100.0, risk=0.1)
    G.add_edge("B", "D", 0, length=100.0, safety_weight=400.0, risk=0.9)   # 共有高危
    short = compute_route(G, "A", "D", "shortest")   # A,B,D
    safe = compute_route(G, "A", "D", "safest")      # A,C,B,D
    av = avoided_hot_segments(G, short["nodes"], safe["nodes"])
    # 只剩 A→B 一条（B→D 共有被排除）
    assert len(av) == 1, av
    assert av[0]["length_m"] == 100.0 and av[0]["risk"] == 0.9, av
    # 方向交换 = "新进入"侧：safest 独有的 A→C / C→B 都是低危 → 空
    av2 = avoided_hot_segments(G, safe["nodes"], short["nodes"])
    assert av2 == [], av2
    # 再造一条高危的 safest 独有边，确认交换方向真的能抓到它
    G2 = G.copy()
    G2["C"]["B"][0]["risk"] = 0.9
    assert len(avoided_hot_segments(G2, safe["nodes"], short["nodes"])) == 1
    print("OK H7: avoided_hot_segments 只取单侧独有+高危；方向交换得另一侧")


if __name__ == "__main__":
    _check_safety_weight_formula()
    _check_route_divergence()
    _check_hot_blocks()
    _check_avoided_hot_segments()
    print("routing self-checks passed")
