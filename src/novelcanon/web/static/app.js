"use strict";

const $ = (id) => document.getElementById(id);

const bookSelect = $("book-select");
const question = $("question");
const cutoff = $("cutoff");
const world = $("world");
const askBtn = $("ask-btn");
const statusEl = $("status");
const resultEl = $("result");

const books = new Map();

async function init() {
  try {
    const res = await fetch("/books");
    if (!res.ok) throw new Error(`books 加载失败：HTTP ${res.status}`);
    const list = await res.json();
    bookSelect.innerHTML = "";
    for (const b of list) {
      books.set(b.book_id, b);
      const opt = document.createElement("option");
      opt.value = b.book_id;
      const idx = b.active_index ? "✓" : "✗";
      opt.textContent = `${b.title}（${b.chapter_count} 章，索引${idx}）`;
      bookSelect.appendChild(opt);
    }
    askBtn.disabled = list.length === 0;
    if (list.length > 0) {
      bookSelect.value = list[0].book_id;
      question.focus();
    }
  } catch (err) {
    showStatus(`图书列表加载失败：${err.message}`, "error");
  }
}

function showStatus(msg, kind) {
  statusEl.textContent = msg;
  statusEl.className = `status ${kind}`;
}

function hideStatus() {
  statusEl.className = "status hidden";
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function renderAnswer(d) {
  resultEl.classList.remove("hidden");
  const route = $("route-badge");
  route.textContent = d.route || "—";
  route.style.borderColor = d.route === "structured" ? "var(--ok)" : "var(--accent-dim)";
  $("confidence").textContent = d.confidence != null ? `置信度 ${(d.confidence * 100).toFixed(0)}%` : "";
  $("versions").textContent = [
    d.run_version ? `run ${d.run_version.slice(0, 8)}` : "",
    d.index_version ? `idx ${d.index_version.slice(0, 8)}` : "",
    d.profile || "",
  ].filter(Boolean).join(" · ");

  const cannot = $("cannot");
  if (d.cannot_answer) {
    cannot.classList.remove("hidden");
    cannot.textContent = "无法回答";
  } else {
    cannot.classList.add("hidden");
  }

  const caveats = $("caveats");
  if (d.caveats && d.caveats.length) {
    caveats.classList.remove("hidden");
    caveats.textContent = "⚠ " + d.caveats.join("；");
  } else {
    caveats.classList.add("hidden");
  }

  $("answer").textContent = d.answer || "（无答案）";
  $("src-count").textContent = d.total_sources != null ? `共 ${d.total_sources} 条` : "";
  renderSources(d.sources || []);
}

function renderSources(sources) {
  const ul = $("sources");
  ul.innerHTML = "";
  if (!sources.length) {
    const li = document.createElement("li");
    li.className = "source";
    li.textContent = "（无证据来源）";
    ul.appendChild(li);
    return;
  }
  for (const s of sources) {
    const li = document.createElement("li");
    li.className = "source";

    const head = document.createElement("div");
    head.className = "head";

    const ord = document.createElement("span");
    ord.className = "ord";
    ord.textContent = `第 ${s.observed_ordinal ?? "?"} 章`;
    head.appendChild(ord);

    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = s.kind || "evidence";
    head.appendChild(kind);

    const stance = document.createElement("span");
    stance.className = `stance ${s.stance || ""}`;
    stance.textContent = s.stance || "";
    head.appendChild(stance);

    const pos = document.createElement("span");
    pos.className = "pos";
    pos.textContent = s.char_start != null ? `[${s.char_start}, ${s.char_end})` : "";
    head.appendChild(pos);

    const hint = document.createElement("span");
    hint.className = "toggle-hint";
    hint.textContent = s.span_text ? "点击展开原文 ▾" : "";
    head.appendChild(hint);
    li.appendChild(head);

    if (s.span_text) {
      const snippet = document.createElement("div");
      snippet.className = "snippet";
      snippet.textContent = s.span_text.slice(0, 60) + (s.span_text.length > 60 ? "…" : "");
      li.appendChild(snippet);

      const span = document.createElement("div");
      span.className = "span";
      span.textContent = s.span_text;
      li.appendChild(span);

      head.addEventListener("click", () => {
        li.classList.toggle("open");
        hint.textContent = li.classList.contains("open") ? "收起 ▴" : "点击展开原文 ▾";
      });
    }
    ul.appendChild(li);
  }
}

const ERR_TEXT = {
  missing_book: "缺少 book_id",
  book_not_found: "图书不存在",
  rate_limited: "请求频率超限，请稍后重试",
  invalid_params: "参数无效",
  timeout: "查询超时，请重试",
  overloaded: "服务过载，请稍后重试",
  backend_not_configured: "embedding 后端未配置（服务端配置错误）",
  internal: "内部错误",
};

async function ask() {
  const q = question.value.trim();
  const bookId = bookSelect.value;
  if (!q) { showStatus("请输入问题", "error"); return; }
  if (!bookId) { showStatus("请先选择图书", "error"); return; }

  hideStatus();
  askBtn.disabled = true;
  resultEl.classList.add("hidden");

  const body = { question: q, book_id: bookId };
  const kc = cutoff.value.trim();
  if (kc !== "") body.knowledge_cutoff = parseInt(kc, 10);
  const wa = world.value.trim();
  if (wa !== "") body.world_at = parseInt(wa, 10);

  try {
    showStatus("查询中…", "loading");
    const res = await fetch("/query", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    hideStatus();
    if (!res.ok) {
      let msg = `查询失败（HTTP ${res.status}）`;
      try {
        const err = await res.json();
        const code = err.detail && err.detail.code;
        msg = ERR_TEXT[code] || msg;
        if (err.detail && err.detail.message && code !== msg) msg += `：${err.detail.message}`;
      } catch { /* 非 JSON 错误体 */ }
      showStatus(msg, "error");
      return;
    }
    renderAnswer(await res.json());
  } catch (err) {
    hideStatus();
    showStatus(`网络错误：${err.message}`, "error");
  } finally {
    askBtn.disabled = false;
  }
}

askBtn.addEventListener("click", ask);
question.addEventListener("keydown", (e) => {
  if (e.key === "Enter") ask();
});

// ── 阶段二 03：tab 导航 ────────────────────────────────────

const tabs = $("tabs");
tabs.addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === btn));
  document.querySelectorAll(".tabpane").forEach((p) => {
    p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`);
  });
  if (btn.dataset.tab === "entities") loadEntities("");
  if (btn.dataset.tab === "graph") loadGraph();
});

// ── 实体浏览 ───────────────────────────────────────────────

const entityQ = $("entity-q");
const entitySearchBtn = $("entity-search-btn");
const entityList = $("entity-list");
const entityDetail = $("entity-detail");
const entityStatusEl = $("entity-status");

function showEntityStatus(msg, kind) {
  entityStatusEl.textContent = msg;
  entityStatusEl.className = `status ${kind || "hidden"}`;
}

async function loadEntities(q) {
  const bookId = bookSelect.value;
  if (!bookId) { showEntityStatus("请先选择图书", "error"); return; }
  showEntityStatus("加载中…", "loading");
  try {
    const params = new URLSearchParams({ book_id: bookId });
    if (q) params.set("q", q);
    const res = await fetch(`/entities?${params}`);
    if (!res.ok) {
      const err = await res.json().catch(() => null);
      throw new Error(err && err.detail ? err.detail.message : `HTTP ${res.status}`);
    }
    const body = await res.json();
    renderEntityList(body.items);
    showEntityStatus(`共 ${body.total} 个实体`, "");
  } catch (err) {
    showEntityStatus(`加载失败：${err.message}`, "error");
  }
}

function renderEntityList(items) {
  entityList.innerHTML = "";
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "（无匹配实体）";
    entityList.appendChild(li);
    return;
  }
  for (const it of items) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "e-name";
    name.textContent = it.display_name || it.canonical_name;
    const tier = document.createElement("span");
    tier.className = "tier";
    tier.textContent = it.tier || "";
    name.appendChild(tier);
    li.appendChild(name);
    const meta = document.createElement("span");
    meta.className = "e-meta";
    meta.textContent = `提及 ${it.mention_count} · 别名 ${it.alias_count}`;
    li.appendChild(meta);
    li.addEventListener("click", () => {
      document.querySelectorAll(".entity-list li").forEach((x) => x.classList.remove("active"));
      li.classList.add("active");
      loadEntityDetail(it.canonical_id);
    });
    entityList.appendChild(li);
  }
}

async function loadEntityDetail(cid) {
  const bookId = bookSelect.value;
  entityDetail.innerHTML = '<div class="empty-hint">加载中…</div>';
  try {
    const res = await fetch(
      `/entities/${encodeURIComponent(cid)}?book_id=${encodeURIComponent(bookId)}`
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderEntityDetail(await res.json());
  } catch (err) {
    entityDetail.innerHTML = `<div class="empty-hint">加载失败：${esc(err.message)}</div>`;
  }
}

function sectionTitle(text) {
  const div = document.createElement("div");
  div.className = "d-section";
  const h4 = document.createElement("h4");
  h4.textContent = text;
  div.appendChild(h4);
  return div;
}

function emptyHint(text) {
  const div = document.createElement("div");
  div.className = "empty-hint";
  div.textContent = text;
  return div;
}

function factRow(main, sub, evidence) {
  const div = document.createElement("div");
  div.className = "fact-row";
  const m = document.createElement("div");
  m.className = "f-main";
  m.textContent = main;
  div.appendChild(m);
  if (sub) {
    const s = document.createElement("div");
    s.className = "f-sub";
    s.textContent = sub;
    div.appendChild(s);
  }
  if (evidence && evidence.length) {
    const evBox = document.createElement("div");
    evBox.className = "f-evidence";
    for (const ev of evidence) {
      const line = document.createElement("div");
      line.textContent = `[${ev.evidence_stance || "evidence"}] 披露于第 ${ev.observed_ordinal ?? "?"} 章`;
      evBox.appendChild(line);
      if (ev.span_text) {
        const span = document.createElement("div");
        span.textContent = ev.span_text;
        evBox.appendChild(span);
      }
    }
    div.appendChild(evBox);
    div.addEventListener("click", () => div.classList.toggle("open"));
  }
  return div;
}

function renderEntityDetail(d) {
  const box = document.createElement("div");

  const head = document.createElement("div");
  head.className = "d-head";
  const h3 = document.createElement("h3");
  h3.textContent = d.display_name || d.canonical_name;
  head.appendChild(h3);
  const tier = document.createElement("span");
  tier.className = "badge";
  tier.textContent = `tier: ${d.tier || "—"}`;
  head.appendChild(tier);
  const imp = document.createElement("span");
  imp.className = "muted small";
  imp.textContent = `importance ${Number(d.importance_score || 0).toFixed(2)}`;
  head.appendChild(imp);
  box.appendChild(head);

  if (d.aliases && d.aliases.length) {
    box.appendChild(sectionTitle("表面名"));
    const wrap = document.createElement("div");
    wrap.className = "aliases";
    for (const a of d.aliases) {
      const chip = document.createElement("span");
      chip.className = "alias-chip";
      chip.textContent = a;
      wrap.appendChild(chip);
    }
    box.appendChild(wrap);
  }

  box.appendChild(sectionTitle("属性（当前版本）"));
  if (d.states && d.states.length) {
    for (const s of d.states) {
      const sub = s.observed_ordinal != null ? `披露于第 ${s.observed_ordinal} 章` : "";
      box.appendChild(factRow(`${s.field} = ${s.value}`, sub, s.evidence));
    }
  } else {
    box.appendChild(emptyHint("（无属性）"));
  }

  box.appendChild(sectionTitle("关系"));
  if (d.relations && d.relations.length) {
    for (const r of d.relations) {
      const sub = [
        r.observed_ordinal != null ? `第 ${r.observed_ordinal} 章` : "",
        r.relation_raw || "",
      ].filter(Boolean).join(" · ");
      box.appendChild(factRow(`${r.from_name} —[${r.relation_type}]→ ${r.to_name}`, sub, r.evidence));
    }
  } else {
    box.appendChild(emptyHint("（无关系）"));
  }

  box.appendChild(sectionTitle("参与事件"));
  if (d.events && d.events.length) {
    for (const ev of d.events) {
      const sub = ev.observed_ordinal != null ? `第 ${ev.observed_ordinal} 章` : "";
      box.appendChild(factRow(`[${ev.event_type}] ${ev.summary}`, sub, ev.evidence || []));
    }
  } else {
    box.appendChild(emptyHint("（无事件）"));
  }

  entityDetail.innerHTML = "";
  entityDetail.appendChild(box);
}

entitySearchBtn.addEventListener("click", () => loadEntities(entityQ.value.trim()));
entityQ.addEventListener("keydown", (e) => {
  if (e.key === "Enter") loadEntities(entityQ.value.trim());
});

// ── 图谱 ───────────────────────────────────────────────────

const graphLimit = $("graph-limit");
const graphLoadBtn = $("graph-load-btn");
const graphWrap = $("graph-wrap");
const graphMeta = $("graph-meta");
const graphStatusEl = $("graph-status");

function showGraphStatus(msg, kind) {
  graphStatusEl.textContent = msg;
  graphStatusEl.className = `status ${kind || "hidden"}`;
}

async function loadGraph() {
  const bookId = bookSelect.value;
  if (!bookId) { showGraphStatus("请先选择图书", "error"); return; }
  const limit = parseInt(graphLimit.value, 10) || 60;
  showGraphStatus("加载中…", "loading");
  try {
    const res = await fetch(`/graph?book_id=${encodeURIComponent(bookId)}&limit=${limit}`);
    if (!res.ok) {
      const err = await res.json().catch(() => null);
      throw new Error(err && err.detail ? err.detail.message : `HTTP ${res.status}`);
    }
    const body = await res.json();
    graphMeta.textContent = `${body.nodes.length} 节点 / ${body.edges.length} 边（全书 ${body.total_nodes} 实体）`;
    renderGraph(body);
    showGraphStatus("", "");
  } catch (err) {
    showGraphStatus(`加载失败：${err.message}`, "error");
  }
}

function renderGraph(g) {
  graphWrap.innerHTML = "";
  if (!g.nodes.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "（无实体数据——请先完成抽取并激活 run）";
    graphWrap.appendChild(d);
    return;
  }
  const W = 820;
  const H = 560;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const hint = document.createElement("div");
  hint.className = "hint";
  hint.textContent = "点击节点查看详情";
  graphWrap.appendChild(hint);
  graphWrap.appendChild(svg);

  const nodes = g.nodes.map((n) => ({
    ...n, x: Math.random() * W, y: Math.random() * H, vx: 0, vy: 0,
  }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const edges = g.edges
    .map((e) => ({ ...e, a: byId.get(e.source), b: byId.get(e.target) }))
    .filter((e) => e.a && e.b);

  // 力导向模拟：斥力 + 弹簧 + 向心 + 碰撞分离，600 迭代带冷却收敛
  const k = 100;
  const rep = 1400;
  const center = 0.0015;
  const collideGap = 26;
  for (let iter = 0; iter < 600; iter++) {
    const damp = 0.9 - 0.35 * (iter / 600);
    for (const n of nodes) { n.vx = 0; n.vy = 0; }
    for (const e of edges) {
      const dx = e.b.x - e.a.x;
      const dy = e.b.y - e.a.y;
      const dist = Math.max(Math.hypot(dx, dy), 1);
      const f = (dist - k) * 0.02;
      const fx = (dx / dist) * f;
      const fy = (dy / dist) * f;
      e.a.vx += fx; e.a.vy += fy;
      e.b.vx -= fx; e.b.vy -= fy;
    }
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i];
        const b = nodes[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d2 = Math.max(dx * dx + dy * dy, 1);
        const f = rep / d2;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx -= fx; a.vy -= fy;
        b.vx += fx; b.vy += fy;
      }
    }
    for (const n of nodes) {
      n.vx += (W / 2 - n.x) * center;
      n.vy += (H / 2 - n.y) * center;
    }
    // 碰撞分离：硬性最小间距约束（解决中心坍缩重叠）
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i];
        const b = nodes[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.hypot(dx, dy);
        const minD = a.r + b.r + collideGap;
        if (d < minD && d > 0.0001) {
          const push = ((minD - d) / 2) * damp;
          const nx = dx / d;
          const ny = dy / d;
          a.x -= nx * push;
          a.y -= ny * push;
          b.x += nx * push;
          b.y += ny * push;
        }
      }
    }
    for (const n of nodes) {
      n.x += n.vx * damp;
      n.y += n.vy * damp;
    }
  }

  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
  marker.setAttribute("id", "arrow");
  marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "9");
  marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "6");
  marker.setAttribute("markerHeight", "6");
  marker.setAttribute("orient", "auto-start-reverse");
  const arrowPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
  arrowPath.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
  arrowPath.setAttribute("fill", "var(--border)");
  marker.appendChild(arrowPath);
  defs.appendChild(marker);
  svg.appendChild(defs);

  const tierColor = { core: "var(--ok)", major: "var(--accent)", minor: "var(--muted)" };
  const maxMention = Math.max(1, ...nodes.map((n) => n.mention_count));
  for (const n of nodes) {
    n.r = 6 + 10 * (n.mention_count / maxMention);
  }
  // 标签分级：节点多时只标注主要实体（避免标签糊成一团）
  const showAllLabels = nodes.length <= 40;
  const labelThreshold = Math.max(1, Math.round(maxMention * 0.25));

  const edgeGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
  for (const e of edges) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", e.a.x);
    line.setAttribute("y1", e.a.y);
    line.setAttribute("x2", e.b.x);
    line.setAttribute("y2", e.b.y);
    line.setAttribute("stroke", "#3a4a66");
    line.setAttribute("stroke-width", "1.2");
    if (e.direction === "directed") line.setAttribute("marker-end", "url(#arrow)");
    line.title = `${e.relation_type}（第 ${e.observed_ordinal} 章）`;
    edgeGroup.appendChild(line);
  }
  svg.appendChild(edgeGroup);

  const nodeGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
  for (const n of nodes) {
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.style.cursor = "pointer";
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("cx", n.x);
    circle.setAttribute("cy", n.y);
    circle.setAttribute("r", n.r);
    circle.setAttribute("fill", tierColor[n.tier] || "var(--accent-dim)");
    circle.setAttribute("stroke", "var(--bg)");
    circle.setAttribute("stroke-width", "1.5");
    g.appendChild(circle);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    const showLabel = showAllLabels || n.mention_count >= labelThreshold || n.tier === "core" || n.tier === "major";
    if (showLabel) {
      label.setAttribute("x", n.x + n.r + 4);
      label.setAttribute("y", n.y + 4);
      label.setAttribute("class", "node-label");
      label.textContent = n.name.length > 14 ? `${n.name.slice(0, 14)}…` : n.name;
    } else {
      label.setAttribute("x", n.x);
      label.setAttribute("y", n.y);
      label.setAttribute("class", "node-label");
      label.textContent = n.name.length > 8 ? `${n.name.slice(0, 8)}…` : n.name;
      label.setAttribute("opacity", "0");
    }
    g.appendChild(label);
    g.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => {
        t.classList.toggle("active", t.dataset.tab === "entities");
      });
      document.querySelectorAll(".tabpane").forEach((p) => {
        p.classList.toggle("active", p.id === "tab-entities");
      });
      loadEntityDetail(n.id);
    });
    nodeGroup.appendChild(g);
  }
  svg.appendChild(nodeGroup);
}

graphLoadBtn.addEventListener("click", loadGraph);

init();
