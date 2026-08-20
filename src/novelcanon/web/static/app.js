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

init();
