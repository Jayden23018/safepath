# Detour Tolerance（绕路容忍度）设计 — ❌ 已否决，未实施

> **状态：2026-07-29 审计后否决。保留作决策记录，勿照此实施。**
>
> 否决理由（三条，前两条是硬阻断）：
> 1. **跑不起来**：`nx.shortest_simple_paths` 带 `@not_implemented_for("multigraph")`（networkx 3.4.2），对 osmnx 的 MultiDiGraph 直接抛 `NetworkXNotImplemented`。第 49 行还专门讨论了平行边聚合，说明知道是多重图但没查这个约束。
> 2. **剪枝永不触发**：第 48 行假设 budget 内候选有限。曼哈顿网格里等长路径数是 C(m+n,n)——10×10 偏移即 184756 条、detour 0%。20×20 玩具网格实测：15 秒枚举 3014 条候选，长度全部 = 1800.0m，连等长层都没走完。真图 26429 节点，`break` 不可达。第 4 行引用的"紧密方格网、相邻平行街长度相等"正是这个爆炸的成立条件——spec 拿它当动机，却没意识到它同时否决了自己的算法。
> 3. **前提是错的**：spec 假设两条路"几何重合"。实测节点重合度只有 15%–52%，Penn Park→Mill Creek 两条路 85% 的路段不同。小的是长度差不是路线差。验收标准 #1（绕路 >5%）既做不到、也量错了东西——绕路大是代价不是卖点。
>
> **实际采纳方案**：不加 budget、不动 Dijkstra，改度量口径——`classify_divergence` 从绕路比换成路线重合度（`diff_ratio = 1 - Jaccard`），并新增高危路段数（`risk > p90`）作为对比主指标。详见 CLAUDE.md「关键约束」。
>
> 若将来仍需长度约束下的最小风险路径（CSP，NP-hard）：用拉格朗日松弛（`weight = risk + λ·length`，二分 λ，每轮一次 Dijkstra），不要用 Yen。

**日期**：2026-07-29
**背景**：当前 safest 路径来自单目标 Dijkstra（weight=safety_weight），费城密集网格下与 shortest 几何重合，绕路幅度典型 <1%（CLAUDE.md 第 39 行穷举验证）。这是几何约束非 bug，但产品上缺少"明显分叉"的 demo 价值。

**目标**：把绕路决定权交给用户，在"用户允许的绕路幅度（budget）内"找总风险最低的路径，让安全路径主动绕开整片高犯罪区。学术依据：Galbrun & Pelechrinis, *Urban Navigation Beyond Shortest Route: The Case of Safe Paths*, Information Systems 57 (2016)——SafePaths 问题即"在 detour tolerance 内最小化犯罪暴露"。

**不做**：不调 α（仍锁 1.5）、不动 prep_data、不伪造热点、不缓存 K-shortest 结果、不加滑块（下拉够用）。

---

## 1. 核心算法（routing.py）

新增函数 `compute_safest_with_budget()`。**不改动现有 `compute_route` / `compute_both_routes`**——shortest 路径仍走原 `compute_route(mode="shortest")`。

### 签名
```python
def compute_safest_with_budget(G, origin_id, dest_id, budget_ratio=0.2):
    """在 budget 内找总 safety_weight 最低的路径。
    返回 (nodes, total_length, total_safety_weight, fallback_flag)。
    """
```

### 算法（Yen K-shortest + budget 剪枝）
```
1. short_nodes = nx.shortest_path(G, origin_id, dest_id, weight="length")
   short_len   = sum(length of edges in short_nodes)
   budget      = short_len * (1 + budget_ratio)

2. best_safe_nodes = short_nodes        # 兜底：最坏返回最短路
   best_safe_sw    = sum(safety_weight of edges in short_nodes)

3. for candidate in nx.shortest_simple_paths(G, origin_id, dest_id, weight="length"):
       cand_len = sum(length of edges)
       if cand_len > budget:
           break                          # shortest_simple_paths 按 length 递增，后续只会更长，安全剪枝
       cand_sw = sum(safety_weight of edges)
       if cand_sw < best_safe_sw:
           best_safe_sw    = cand_sw
           best_safe_nodes = candidate

4. fallback = (best_safe_nodes == short_nodes)   # budget 内没找到比 shortest 风险更低的
   return (best_safe_nodes, len(best_safe_nodes), best_safe_sw, fallback)
```

### 关键设计点
- **枚举键 = length，选优键 = safety_weight**：`shortest_simple_paths` 按 length 递增产出候选（Yen 算法），但最终选总 safety_weight 最低的。这正是 Galbrun 的"detour budget 内最小化风险暴露"。
- **剪枝正确性**：Yen 按 length 单调递增产出，故 `cand_len > budget` 后 break 安全，不会漏掉 budget 内候选。
- **平行边聚合**：候选路径求 length / safety_weight 时复用现有 `_edge_agg`（`min(weight)`）逻辑，与 Dijkstra 选边一致（CLAUDE.md 第 52 行约束）。
- **性能**：81324 边图上，流式产出 + 超 budget 即 break，一般枚举几十条候选即停。不缓存，每次算路实时跑。最坏情况（大 budget + 极密集局部图）枚举量上升，但 `shortest_simple_paths` 是生成器，内存恒定，仅时间增长；MVP 可接受，未来若慢可加 K 条上限（`itertools.islice`）并 log 截断（见第 8 节未做项）。

### 边界
- `fallback=True`：budget 内所有候选风险都不低于 shortest（含 shortest 自身）→ 返回 short_nodes。语义："在 X% 内没有更安全的路"。
- `shortest_simple_paths` 抛 `NetworkXNoPath`：由调用方（app.py）捕获，与现有无路径处理一致。

## 2. 三态分类（不变）

`classify_divergence(none/minor/clear)` 逻辑与阈值（5%）均不变。它喂入的 safest 路径从"单 Dijkstra 结果"变为"budget 内风险最低路径"——

- **之前**：网格下绕路 <1% → 多数 minor/none
- **之后**：20% budget 下主动绕开高犯罪区 → 更易落入 clear（绕路 5-20%）

`detour_ratio` 从新 safest 路径自然算出，三态分类自动适配，无需改 `classify_divergence`。

## 3. Fallback UI 标注

当 `safest_fallback=True`，UI 在 verdict 框诚实标注"在 20% 绕路容忍内没有找到更安全的路，已显示最短路径"。本质仍走 minor/none 文案分支，但点明用户选择生效且数据结论如此。不伪造分叉。

## 4. UI（templates/index.html）

算路表单（origin/dest 下拉旁）加 `<select name="detour">`：
- 选项：`10%` / `20%`（默认 selected）/ `30%`，value 为数字 10/20/30
- 随 `/route` POST 提交
- 结果页 stats 新增一行显示"绕路容忍度：20%"，让用户看到自己的选择生效

## 5. app.py 改动

`/route`（`app.py:233-270`）：
- 读并校验 detour 参数（信任边界输入）：
  ```python
  try:
      pct = int(request.form.get("detour", 20))
  except (TypeError, ValueError):
      pct = 20
  budget_ratio = max(0, min(pct, 100)) / 100   # clip 到 [0, 1]，非法/越界回退默认 20%
  ```
  合法选项固定为 10/20/30；clip 兜底防止直接 POST 越界值（如 -5 / 999）。
- shortest 路径仍 `compute_route(G, origin, dest, mode="shortest")`
- safest 路径改调 `compute_safest_with_budget(G, origin, dest, budget_ratio)`
- `stats` 字典新增 `detour_budget`（百分比整数）和 `safest_fallback`（bool）传给模板

## 6. 评分叙事映射

- **Graph**：从"两条独立 Dijkstra"升级为"带预算约束的多路径优化（Yen K-shortest + budget 剪枝，按 length 枚举按 safety_weight 选优）"——算法复杂度叙事更强。
- **Flask+HTML**：新增 POST 参数 `detour`，是第二个"用户输入改变计算结果"的 POST（第一个 /report 改状态）。

## 7. 自检（routing.py `__main__`，bare assert）

新增 H6 自检，构造最小测试图验证：
1. budget 足够时，选了风险更低但更长的路（非最短路）
2. budget 不够时，回退最短路（fallback=True）
3. 平行边聚合正确（不退化成 key=0）

最小测试图：3-4 节点，手动设 length 和 safety_weight，造一条短而险 + 一条长而安全的对照路径。

## 8. 不改动清单（YAGNI）

- α（1.5 锁定）、REPORT_BUMP_FACTOR（5.0）、prep_data 全部不动
- `compute_both_routes` 的 shortest 调用不动
- 不加滑块（下拉够用，少写 JS / 状态同步）
- 不缓存 K-shortest（图已在内存，实时算）
- 不动 `classify_divergence` 阈值

## 9. 受影响文件

| 文件 | 改动 |
|---|---|
| `routing.py` | 新增 `compute_safest_with_budget()` + H6 自检；不动现有函数 |
| `app.py` | `/route` 读 detour 参数、改调 safest 函数、stats 加两字段 |
| `templates/index.html` | 表单加 detour 下拉；结果页显示 budget + fallback 文案 |

## 10. 验收标准

- [ ] 选 20% budget 时，高/低风险起终点对（如 Market St Corridor → 校外地标）的 safest 路径绕路幅度 >5%（落入 clear），肉眼可见避开高犯罪区
- [ ] 选 10% budget 时分叉更小，30% 时更大，随参数单调变化
- [ ] fallback 场景（低风险起终点对）返回 shortest 并诚实标注，不报错
- [ ] H6 自检通过；现有 H1-H5 自检不回归
- [ ] shortest 路径行为与改动前完全一致
