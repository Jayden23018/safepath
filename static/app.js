/* SafePath — 前端交互：图层 toggle + 图例联动 + report 定位按钮 + 上报后定位高亮。
 *
 * 依赖 folium 注入的 Leaflet 全局 (L) 和 window.SAFEPATH_CTX（folium 变量名，每次 build_map 变化，
 * 故用 get_name() 在后端动态注入，此处只读不硬编码）。
 *
 * 时序坑：本 script 在 </body> 执行，folium 的 var map_<hash> 声明在 folium 注入的内联 script 里，
 * 可能尚未求值 → 用 DOMContentLoaded + 轮询 window[ctx.map] 直到存在（重试上限避免死循环）。
 */

// 提到 IIFE 外：纯函数无闭包依赖，浏览器作全局工具 + node 可直接自检。
function parseReported(raw) {
  if (!raw) return null;
  var parts = raw.split(",").map(function (s) { return s.trim(); });
  if (parts.length !== 2) return null;
  // Number(" ")/Number("")→0 而非 NaN，故先排除空串再转。
  if (parts.some(function (s) { return !s; })) return null;
  var nums = parts.map(Number);
  if (nums.some(isNaN)) return null;
  return nums;
}

(function () {
  "use strict";

  // 无 DOM（node 自检）时直接返回，仅留 parseReported 给自检调用。
  if (typeof document === "undefined") return;

  var MAX_RETRIES = 40;        // ~2s（40 × 50ms）；图大时 folium script 求值慢，给足
  var POLL_MS = 50;

  document.addEventListener("DOMContentLoaded", boot);

  function boot() {
    // (A) report 定位按钮 —— 不依赖 MAP，纯父页面 geolocation，可立即绑定。
    bindLocate();

    var tries = 0;
    (function poll() {
      var ctx = window.SAFEPATH_CTX;
      if (!ctx || !window[ctx.map]) {
        if (tries++ < MAX_RETRIES) return setTimeout(poll, POLL_MS);
        return; // folium 没起来就算了，定位按钮仍可用
      }
      var MAP = window[ctx.map], RISK = window[ctx.risk], HEAT = window[ctx.heat];
      var AVOIDED = ctx.avoided ? window[ctx.avoided] : null;
      var SHORT = ctx.short ? window[ctx.short] : null;
      var SAFE = ctx.safe ? window[ctx.safe] : null;
      wireLayers(MAP, RISK, HEAT, AVOIDED, ctx.has_avoided);
      wireReported(MAP);
      wireAvoidedHover(MAP, AVOIDED);
      // C2: 每个 .field[data-search] 装一份 autocomplete（origin/dest 各一）。
      document.querySelectorAll(".field[data-search]").forEach(function (w) { wireAutocomplete(MAP, w); });
      wirePick(MAP);
      wirePanel();
      wireRouteHover(SHORT, SAFE);
      wireRouteLoading();
    })();
  }

  // (A) report 定位按钮：现场场景，手机定位比手敲坐标更准更快。拒绝/无 GPS 时静默留空，
  // 手输兜底（details.report-manual）仍在。navigator.geolocation 需 HTTPS 或 localhost。
  function bindLocate() {
    var btn = document.getElementById("btn-locate");
    if (!btn || !navigator.geolocation) {
      if (btn) btn.disabled = true; // 无 geolocation 能力的浏览器禁用，提示走手输
      return;
    }
    btn.addEventListener("click", function () {
      btn.classList.add("loading");
      navigator.geolocation.getCurrentPosition(
        function (pos) {
          btn.classList.remove("loading");
          var lat = pos.coords.latitude, lng = pos.coords.longitude;
          document.getElementById("id-lat").value = lat.toFixed(6);
          document.getElementById("id-lng").value = lng.toFixed(6);
          centerOnLocation(lat, lng);
        },
        function (err) {
          btn.classList.remove("loading");
          alert("Could not get your location: " + err.message +
                "\nYou can still enter coordinates manually below.");
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }
      );
    });
  }

  // 定位成功后把地图居中到用户位置 + 落 pin 提示，让用户知道定位到哪了。
  // MAP 可能尚未就绪（folium script 还没求值）→ 轮询 window[ctx.map] 直到出现。
  function centerOnLocation(lat, lng) {
    var tries = 0;
    (function poll() {
      var ctx = window.SAFEPATH_CTX;
      if (!ctx || !window[ctx.map]) {
        if (tries++ < MAX_RETRIES) return setTimeout(poll, POLL_MS);
        return; // 地图没起来也没关系，坐标已填进表单
      }
      var MAP = window[ctx.map];
      MAP.setView([lat, lng], 17);
      L.marker([lat, lng], { icon: pinIcon() }).addTo(MAP).bindTooltip("You are here").openTooltip();
    })();
  }

  // (B) 自建图层 toggle：删 folium 默认 LayerControl 后，这里建实色卡片按钮，addLayer/removeLayer。
  // 初始状态必须与 build_map 的 show= 一致：risk show=True→active，heat show=False→inactive，
  // avoided show=True→active（仅 /route 有，has_avoided 控制按钮显隐）。
  // 按钮由文字 + 内联 SVG 组合，静态无外部数据 → 用 DOM 构造避免 innerHTML（防御性）。
  function wireLayers(MAP, RISK, HEAT, AVOIDED, hasAvoided) {
    var box = document.createElement("div");
    box.className = "sp-layer-toggle";
    box.appendChild(layerBtn(STREET_ICON, "Risk streets", "risk", true, MAP, RISK, syncLegend));
    box.appendChild(layerBtn(HEAT_ICON, "Crime heat", "heat", false, MAP, HEAT));
    if (hasAvoided && AVOIDED) {
      box.appendChild(layerBtn(AVOID_ICON, "Avoided", "avoided", true, MAP, AVOIDED));
    }
    document.querySelector(".map-stage").appendChild(box);
  }

  // 内联 SVG（单色 stroke，跟 .sp-layer-toggle button svg 配套）。
  var STREET_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19l4-14M14 5l4 14"/><path d="M3 9h6M13 13h6"/></svg>';
  var HEAT_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3s5 6 5 10a5 5 0 0 1-10 0c0-4 5-10 5-10z"/></svg>';
  var AVOID_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>';

  function layerBtn(icon, label, name, startOn, MAP, layer, onChange) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.layer = name;
    btn.setAttribute("aria-pressed", startOn ? "true" : "false");
    btn.appendChild(svgIcon(icon)); // 静态 SVG 字面量 → DOM 节点，无 innerHTML
    btn.appendChild(document.createTextNode(label));
    if (startOn) btn.classList.add("active");
    btn.addEventListener("click", function () { toggle(btn, MAP, layer, onChange); });
    return btn;
  }

  // 把内联 SVG 字面量解析成单个节点（避免 innerHTML，纯静态无注入面）。
  function svgIcon(svgStr) {
    var node = new DOMParser().parseFromString(svgStr, "image/svg+xml").documentElement;
    return document.importNode(node, true);
  }

  function toggle(btn, MAP, layer, onChange) {
    var on = btn.classList.toggle("active");
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    if (on) MAP.addLayer(layer); else MAP.removeLayer(layer);
    if (onChange) onChange(on);
  }

  // 风险图例随 risk 图层开关联动：关图层时图例淡至 30%（仍在但弱化，避免误导）。
  function syncLegend(riskOn) {
    var legend = document.querySelector(".legend");
    if (legend) legend.classList.toggle("dim", !riskOn);
  }

  // (C) 上报后定位高亮：/report 重定向带 ?reported=lat,lng → 首页地图自动居中 + marker tooltip。
  // data-reported 由后端注入；坏格式 parseReported 返回 null（见文件顶部纯函数）。
  function wireReported(MAP) {
    var parts = parseReported(document.querySelector(".map-stage").getAttribute("data-reported"));
    if (!parts) return;
    MAP.setView([parts[0], parts[1]], 17);
    L.marker([parts[0], parts[1]], { icon: pinIcon() }).addTo(MAP).bindTooltip("Just reported here").openTooltip();
  }

  // (D) hover 面板"避开高危路段"区 → 地图红段脉冲（加粗+提亮）。
  // 纯 setStyle，无动画库。avoided 图层为空（首页）时该区不渲染，此处 no-op。
  function wireAvoidedHover(MAP, AVOIDED) {
    var trigger = document.querySelector("[data-avoided]");
    if (!trigger || !AVOIDED) return;
    trigger.addEventListener("mouseenter", function () {
      AVOIDED.eachLayer(function (lyr) { lyr.setStyle({ weight: 11, opacity: 1 }); });
    });
    trigger.addEventListener("mouseleave", function () {
      AVOIDED.eachLayer(function (lyr) { lyr.setStyle({ weight: 7, opacity: 0.95 }); });
    });
  }

  // (E) 地图点选 — 武装模式（根治硬伤 1：未武装时地图点击什么都不做）。
  // pickTarget 为 null 时 click handler 第一行 return；只有点 .pick-btn 把 pickTarget 设成
  // "origin"/"dest"/"report" 才武装，下一次地图点击写入对应字段并自动解除（pickTarget = null）。
  // Esc 取消武装。pin 缓存按 target 分桶，重落同 target 先移除旧 pin。
  var pickTarget = null;             // null | "origin" | "dest" | "report"
  var pickPins = { origin: null, dest: null, report: null };

  function wirePick(MAP) {
    var form = document.querySelector(".route-form");
    if (!form) return;

    // 武装按钮：route 表单的两个 .pick-btn + report 的 .report-pick，都带 data-target。
    document.querySelectorAll("[data-target]").forEach(function (btn) {
      btn.addEventListener("click", function () { armPick(MAP, btn.dataset.target, btn); });
    });

    // 核心验收点：未武装时点击地图什么都不做。
    MAP.on("click", function (e) {
      if (!pickTarget) return;
      var lat = e.latlng.lat, lng = e.latlng.lng;
      commitPick(MAP, pickTarget, lat, lng);
      disarmPick();
    });

    // Esc 取消武装（全局 keydown，避免聚焦在 input 时 Map 收不到）。
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && pickTarget) disarmPick();
    });

    // Clear pins：清 pin + hidden + 输入框。
    var clear = document.querySelector(".clear-pins");
    if (clear) clear.addEventListener("click", function () {
      ["origin", "dest"].forEach(function (t) { clearPick(MAP, t); });
      disarmPick();
      var hint = document.querySelector(".pick-hint");
      if (hint) hint.textContent = "Search above, or tap 📌 then click the map to drop a pin.";
    });
  }

  // 武装：设 pickTarget + .map-stage 加 .picking（CSS 十字光标）+ 按钮 active 态。
  function armPick(MAP, target, btn) {
    pickTarget = target;
    var stage = document.querySelector(".map-stage");
    if (stage) stage.classList.add("picking");
    document.querySelectorAll(".pick-btn.is-armed").forEach(function (b) { b.classList.remove("is-armed"); });
    if (btn) btn.classList.add("is-armed");
    var hint = document.querySelector(".pick-hint");
    if (hint) {
      var label = target === "report" ? "report location"
                : target === "origin" ? "origin" : "destination";
      hint.textContent = "Click the map to set the " + label + " (Esc to cancel).";
    }
  }

  // 解除武装：清 pickTarget + 移除 .picking + 按钮 active 态。
  function disarmPick() {
    pickTarget = null;
    var stage = document.querySelector(".map-stage");
    if (stage) stage.classList.remove("picking");
    document.querySelectorAll(".pick-btn.is-armed").forEach(function (b) { b.classList.remove("is-armed"); });
  }

  // 写入：落 pin + 填 hidden +（route 字段）填输入框显示名 + 居中。
  // 显示名用坐标本身（6 位小数），不接反向地理编码（刚排除掉的外部 API 不塞回来）。
  function commitPick(MAP, target, lat, lng) {
    if (pickPins[target]) MAP.removeLayer(pickPins[target]);
    var color = target === "origin" ? "#15803d"
              : target === "dest" ? "#dc2626" : "#2563eb";
    pickPins[target] = L.marker([lat, lng], { icon: pickPin(color) }).addTo(MAP);

    var label = pickLabel(lat, lng);
    if (target === "report") {
      var latEl = document.getElementById("id-lat"), lngEl = document.getElementById("id-lng");
      if (latEl) latEl.value = lat.toFixed(6);
      if (lngEl) lngEl.value = lng.toFixed(6);
    } else {
      var form = document.querySelector(".route-form");
      setHidden(form, target + "_lat", lat.toFixed(6));
      setHidden(form, target + "_lng", lng.toFixed(6));
      setHidden(form, target + "_name", label);
      var input = document.getElementById("q-" + target);
      if (input) input.value = label;
    }
    MAP.setView([lat, lng], 17);
  }

  // 清单个 target：移 pin + hidden + 输入框。
  function clearPick(MAP, target) {
    if (pickPins[target]) { MAP.removeLayer(pickPins[target]); pickPins[target] = null; }
    var form = document.querySelector(".route-form");
    if (form) {
      setHidden(form, target + "_lat", "");
      setHidden(form, target + "_lng", "");
      setHidden(form, target + "_name", "");
    }
    var input = document.getElementById("q-" + target);
    if (input) input.value = "";
  }

  // (E2) autocomplete combobox — debounce + AbortController 取消在途请求 + 键盘导航。
  // 候选 <li> 用 createElement（OSM 文本不可信，不用 innerHTML）。blur 延迟 150ms 让点击先落。
  function wireAutocomplete(MAP, wrapEl) {
    var input = wrapEl.querySelector(".place-input");
    var list = wrapEl.querySelector(".ac");
    var target = wrapEl.dataset.search;       // "origin" | "dest"
    if (!input || !list || !target) return;

    var timer = null, abort = null, hi = -1, items = [];

    input.addEventListener("input", function () {
      var q = input.value.trim();
      // 用户手改输入 → 之前选的坐标失效，清 hidden（保留 name 让用户看到自己打的字）。
      setHidden(document.querySelector(".route-form"), target + "_lat", "");
      setHidden(document.querySelector(".route-form"), target + "_lng", "");
      if (timer) clearTimeout(timer);
      if (abort) abort.abort();
      timer = setTimeout(function () { fetchSuggestions(MAP, list, input, q); }, 200);
    });

    input.addEventListener("keydown", function (e) {
      if (!items.length) return;
      if (e.key === "ArrowDown") { e.preventDefault(); moveHi(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveHi(-1); }
      else if (e.key === "Enter") {
        if (hi >= 0 && items[hi]) { e.preventDefault(); select(items[hi]); }
      } else if (e.key === "Escape") { close(); }
    });

    // blur 延迟关闭：鼠标点候选时列表先关会点不中。
    input.addEventListener("blur", function () { setTimeout(close, 150); });
    input.addEventListener("focus", function () {
      if (items.length) { open(); return; }
      // 空列表时拉推荐：后端对 q<2 字符回 12 个 landmark，前端不做分支判断。
      fetchSuggestions(MAP, list, input, input.value.trim());
    });

    function moveHi(delta) {
      hi = (hi + delta + items.length) % items.length;
      renderHi();
      var el = list.children[hi];
      if (el) input.setAttribute("aria-activedescendant", el.id);
    }

    function fetchSuggestions(MAP, list, input, q) {
      abort = new AbortController();
      var c = MAP.getCenter();
      var url = "/search?q=" + encodeURIComponent(q) + "&lat=" + c.lat + "&lng=" + c.lng;
      fetch(url, { signal: abort.signal })
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (data) { render(data); })
        // AbortError 是 debounce 正常路径（input 事件主动 abort 在途请求）→ 静默；
        // 其余错误必须可见：这个 bug 就是因为裸吞 catch 才能潜伏到用户手上。
        .catch(function (err) { if (err.name !== "AbortError") console.warn("search failed", err); });
    }

    function render(data) {
      items = searchResults(data).slice(0, 8);
      hi = -1;
      list.replaceChildren();   // 清空候选容器（不用 innerHTML = ""，防御性）
      input.removeAttribute("aria-activedescendant");
      items.forEach(function (it, i) {
        var li = document.createElement("li");
        li.id = list.id + "-" + i;
        li.setAttribute("role", "option");
        li.textContent = it.name + (it.addr ? " — " + it.addr : "");
        li.addEventListener("mousedown", function (ev) { ev.preventDefault(); select(it); });
        list.appendChild(li);
      });
      if (items.length) open(); else close();
    }

    function renderHi() {
      for (var i = 0; i < list.children.length; i++) {
        list.children[i].setAttribute("aria-selected", i === hi ? "true" : "false");
        list.children[i].classList.toggle("active", i === hi);
      }
    }

    function select(it) {
      input.value = it.name;
      var form = document.querySelector(".route-form");
      setHidden(form, target + "_lat", it.lat);
      setHidden(form, target + "_lng", it.lng);
      setHidden(form, target + "_name", it.name);
      // 选中后落 pin + 居中（与 pick 模式共享 pickPins 缓存，避免重叠 marker）。
      if (pickPins[target]) MAP.removeLayer(pickPins[target]);
      pickPins[target] = L.marker([it.lat, it.lng], { icon: pickPin(target === "origin" ? "#15803d" : "#dc2626") }).addTo(MAP);
      MAP.setView([it.lat, it.lng], 17);
      close();
    }

    function open() { list.hidden = false; input.setAttribute("aria-expanded", "true"); }
    function close() { list.hidden = true; input.setAttribute("aria-expanded", "false"); hi = -1; }
  }

  // (F) 面板折叠（桌面）+ 底部抽屉（移动）。
  // 桌面：折叠按钮切 .collapsed → 收成左侧窄条，还地图左 1/3 给用户。
  // 移动：点 panel-head 切 .expanded → 抽屉上滑展开（默认只露路线卡片摘要）。
  function wirePanel() {
    var panel = document.querySelector(".panel");
    var toggle = document.querySelector(".panel-toggle");
    if (!panel) return;
    if (toggle) {
      toggle.addEventListener("click", function (e) {
        if (window.matchMedia("(max-width: 560px)").matches) return; // 移动用抽屉，不用折叠
        e.stopPropagation();
        var collapsed = panel.classList.toggle("collapsed");
        toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      });
    }
    var head = panel.querySelector(".panel-head");
    if (head) {
      head.addEventListener("click", function () {
        if (window.matchMedia("(max-width: 560px)").matches) {
          panel.classList.toggle("expanded");
        }
      });
    }
  }

  // (G) hover 路线卡片 → 对应地图 polyline 加粗提亮（纯 setStyle，无动画库）。
  // 首页无路线（SHORT/SAFE 为 null）时 no-op。
  function wireRouteHover(SHORT, SAFE) {
    bindCard(".route-card.short", SHORT, 7, 11);
    bindCard(".route-card.safe", SAFE, 8, 12);
  }
  function bindCard(sel, layer, baseW, hoverW) {
    var card = document.querySelector(sel);
    if (!card || !layer) return;
    card.addEventListener("mouseenter", function () {
      layer.eachLayer(function (lyr) { if (lyr.setStyle) lyr.setStyle({ weight: hoverW, opacity: 1 }); });
    });
    card.addEventListener("mouseleave", function () {
      layer.eachLayer(function (lyr) { if (lyr.setStyle) lyr.setStyle({ weight: baseW, opacity: 0.95 }); });
    });
  }

  // (H) 路线表单 submit → loading 态：按钮禁用 + 文案换 Planning… + .loading 类跑 spinner。
  // 响应回来后整页重渲染，按钮自然恢复（无需手动复位）。
  function wireRouteLoading() {
    var form = document.querySelector(".route-form");
    if (!form) return;
    form.addEventListener("submit", function () {
      var btn = form.querySelector("button.primary");
      if (!btn) return;
      btn.disabled = true;
      btn.classList.add("loading");
      btn.textContent = "Planning…";
    });
  }

  function setHidden(form, name, val) {
    var el = form.querySelector('input[name="' + name + '"]');
    if (el) el.value = val;
  }

  // 点选起终点 pin：CSS 水滴圆点（颜色区分 origin 绿 / dest 红 / report 蓝）。
  // color 是本文件内固定 hex 字面量，非外部输入 → 拼接无注入面。
  function pickPin(color) {
    var html = '<span style="display:block;width:18px;height:18px;border-radius:50% 50% 50% 0;'
      + 'transform:rotate(-45deg);background:' + color + ';border:2px solid #fff;'
      + 'box-shadow:0 2px 6px rgba(15,23,42,.4);"></span>';
    return L.divIcon({ className: "sp-pick-pin", html: html, iconSize: [22, 22], iconAnchor: [2, 20] });
  }

  // 上报/定位 pin：CSS 水滴形圆点（.sp-report-pin），无 emoji。iconAnchor 对准水滴尖（左下）。
  function pinIcon() {
    return L.divIcon({ className: "sp-report-pin", html: "", iconSize: [18, 18], iconAnchor: [2, 16] });
  }
})();

// pick 模式落点后的显示名：坐标本身，6 位小数。纯函数（无 DOM），node 可测。
// 不接反向地理编码 —— 那等于把刚排除掉的外部 API 塞回来。
function pickLabel(lat, lng) {
  return Number(lat).toFixed(6) + ", " + Number(lng).toFixed(6);
}

// /search 返回 {"results": [...]}（契约 C2）。历史上前端曾按裸数组解析导致静默失败，
// 这个函数是唯一解包点，H13 锁住它。
function searchResults(data) {
  if (Array.isArray(data)) return data;                 // 容忍裸数组
  if (data && Array.isArray(data.results)) return data.results;
  return [];
}

// ---- sanity check（无框架）：node static/app.js ----
if (typeof module !== "undefined" && require.main === module) {
  var cases = {
    "39.9,-75.1": [39.9, -75.1],
    "": null,
    "39.9": null,            // 单值
    "a,b": null,             // 非数字
    "39.9,-75.1,1": null,    // 三值
    " , ": null,             // 空格逗号 → trim 后空，不假通过为 [0,0]
  };
  var ok = 0;
  for (var k in cases) {
    var got = JSON.stringify(parseReported(k));
    if (got === JSON.stringify(cases[k])) ok++;
    else console.log("FAIL " + JSON.stringify(k) + " => " + got + " (expected " + JSON.stringify(cases[k]) + ")");
  }
  console.log("OK parseReported: " + ok + "/" + Object.keys(cases).length + " cases");

  // pickLabel：6 位小数 + 逗号空格分隔（commitPick 落点显示名契约）。
  var labelCases = [
    [39.9526, -75.1932, "39.952600, -75.193200"],
    [40, 75, "40.000000, 75.000000"],
    [39.9525999, -75.1932001, "39.952600, -75.193200"],   // 第 7 位四舍五入
  ];
  var lok = 0;
  labelCases.forEach(function (c) {
    if (pickLabel(c[0], c[1]) === c[2]) lok++;
    else console.log("FAIL pickLabel(" + c[0] + "," + c[1] + ") => " + pickLabel(c[0], c[1]) + " (expected " + c[2] + ")");
  });
  console.log("OK pickLabel: " + lok + "/" + labelCases.length + " cases");

  // H13: /search 响应解包契约。这个 bug 真实发生过（后端 {"results":[…]}，前端按裸数组 .slice
  // → TypeError 被 .catch 裸吞，打字永远不弹候选）。
  var srCases = [
    [{ results: [{ name: "a" }] }, 1],
    [{ results: [] }, 0],
    [[{ name: "a" }, { name: "b" }], 2],   // 裸数组兼容
    [{}, 0],
    [null, 0],
    [undefined, 0],
  ];
  var sok = 0;
  srCases.forEach(function (c) {
    var got = searchResults(c[0]).length;
    if (got === c[1]) sok++;
    else console.log("FAIL searchResults(" + JSON.stringify(c[0]) + ") => " + got + " (expected " + c[1] + ")");
  });
  console.log("OK searchResults: " + sok + "/" + srCases.length + " cases");
}
