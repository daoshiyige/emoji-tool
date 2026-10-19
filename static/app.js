"use strict";

/* ================= 状态 ================= */
const state = {
  view: "all", // all | untagged | inbox | trash
  filters: { academies: [], characters: [], artists: [], specials: [], folders: [], exts: [], name: "", untagged: false, q: "", no_char: false },
  tags: null,
  facets: null, // 当前筛选下各标签的文件数（/api/facets）；没筛选时为 null，侧栏用 tags 的全库计数
  files: [], total: 0, offset: 0, pageSize: 120, loading: false, done: false, sort: "mtime", sortDir: "desc",
  selected: new Set(),
  loadSeq: 0, // 请求序号：防止旧搜索响应覆盖新结果
  viewerIdx: -1,
  current: null, // 属性面板/查看器当前文件
  lastFile: null,
  modalMode: "single", // single | batch | place
  inboxFiles: [], trashFiles: [],
  pendingTags: {}, // 未处理文件待打标：rel -> {characters, specials, artist}
  placingInbox: new Set(),
  pendingMove: new Set(), // 库视图已改标签、待取消选中后归位的文件
  libOverlay: new Map(), // 库视图批量打标本地覆盖：rel -> {characters,specials,artist,academy?,acadLock}
  libSaveTimer: null,
  libSaveBusy: false,
  libSaveAgain: false,
  orgSel: new Set(), // 整理预览勾选：建议 id
  orgItemMap: {}, // 建议 id -> 建议项
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const IC = (p) => `<svg viewBox="0 0 24 24" class="ic">${p}</svg>`;
const ICONS = {
  eye: IC('<path d="M2 12s3.5-6.5 10-6.5S22 12 22 12s-3.5 6.5-10 6.5S2 12 2 12Z"/><circle cx="12" cy="12" r="2.8"/>'),
  tag: IC('<path d="M3 12V5a2 2 0 0 1 2-2h7l9 9-7 7-9-9Z"/><circle cx="8" cy="8" r="1.5"/>'),
  pen: IC('<path d="m4 20 4.5-1L19 8.5a2.1 2.1 0 0 0-3-3L5.5 16 4 20Z"/><path d="m14 7 3 3"/>'),
  trash: IC('<path d="M4 7h16"/><path d="M9 7V5a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/><path d="M6 7v12a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7"/><path d="M10 11v6m4-6v6"/>'),
  check: IC('<path d="m5 12 4.5 4.5L19 7"/>'),
  restore: IC('<path d="M4 12a8 8 0 1 1 2.34 5.66"/><path d="M4 20v-4h4"/>'),
  place: IC('<path d="M12 3v10m0 0 4-4m-4 4-4-4"/><path d="M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>'),
};

async function api(path, opts = {}) {
  const isForm = typeof FormData !== "undefined" && opts.body instanceof FormData;
  const res = await fetch(path, {
    headers: isForm ? {} : { "Content-Type": "application/json" },
    ...opts,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { data = {}; }
  if (!res.ok) throw new Error(data.error || ("请求失败 " + res.status));
  return data;
}

function toast(msg, type = "info") {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast " + type;
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, 2600);
}
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
const debouncedSearch = debounce(() => { if (state.view === "all" || state.view === "untagged") loadFiles(true); }, 350);
const fmtSize = (n) => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
const fmtCount = (n) => n >= 10000 ? (n / 10000).toFixed(1).replace(/\.0$/, "") + "w" : String(n);
const typeOf = (name) => {
  const e = (name.split(".").pop() || "").toLowerCase();
  return e === "png" ? "PNG" : e === "gif" ? "GIF" : e === "webp" ? "WEBP" : e === "jpg" || e === "jpeg" ? "JPG" : e.toUpperCase();
};
const typeCls = (name) => {
  const e = (name.split(".").pop() || "").toLowerCase();
  return e === "png" ? "t-png" : e === "gif" ? "t-gif" : e === "webp" ? "t-webp" : "t-jpg";
};

/* ================= 标签 ================= */
async function loadTags() {
  state.tags = await api("/api/tags");
  renderGroups();
  renderFilterToolbar();
  updateNavCounts();
  scheduleFacets(200); // 全库计数变了，筛选下的计数跟着重算
}

// 打标/归位等高频操作后延迟合并刷新，避免每次点击都全量重拉 /api/tags（约 0.5s）
let tagsRefreshTimer = null;
function scheduleTagsRefresh(delay = 300) {
  clearTimeout(tagsRefreshTimer);
  tagsRefreshTimer = setTimeout(() => {
    tagsRefreshTimer = null;
    loadTags().catch(() => {});
  }, delay);
}

function updateNavCounts() {
  if (!state.tags) return;
  const f = state.facets; // 有筛选：用筛选后的计数，「全部文件」跟网格的「共 N 个文件」对齐
  const total = f ? f.total : Object.values(state.tags.academies).reduce((a, b) => a + b, 0);
  $("nav-all").textContent = fmtCount(total);
  const unt = f ? f.untagged : state.tags.untagged;
  $("nav-untagged").textContent = unt ? fmtCount(unt) : "";
}

function renderGroups() {
  if (!state.tags) return;
  renderGroup("academy", "academies", groupMap("academies"));
  renderGroup("character", "characters", groupMap("characters"));
  renderGroup("artist", "artists", groupMap("artists"));
  renderGroup("special", "specials", groupMap("specials"));
}

// 有筛选时用 /api/facets 的计数；已选但当前筛选下没有的标签补 0，保证还能点掉它
function groupMap(key) {
  const base = (state.facets && state.facets[key]) || state.tags[key] || {};
  if (!state.facets) return base;
  const sel = state.filters[key] || [];
  if (!sel.length) return base;
  const m = Object.assign({}, base);
  for (const v of sel) if (!(v in m)) m[v] = 0;
  return m;
}

const grpUI = { artistShowAll: false };

function renderGroup(grp, key, map) {
  const box = $("chips-" + grp);
  const search = $("search-" + grp);
  box.innerHTML = "";
  const sel = new Set(state.filters[key]);
  const aliases = (grp === "artist" && state.tags.artist_aliases) || {};
  const entries = Object.entries(map).sort((a, b) => b[1] - a[1]);
  $("cnt-" + grp).textContent = entries.length;
  let shown = entries;
  if (grp === "artist" && !grpUI.artistShowAll && entries.length > 30) {
    shown = entries.slice(0, 30);
  }
  if (grp === "character") {
    const nc = document.createElement("button");
    nc.className = "chip chip-notag" + (state.filters.no_char ? " on" : "");
    nc.dataset.name = "__no_char";
    nc.innerHTML = `<span>无角色标签</span>`;
    nc.title = "筛选没有任何角色标签的文件";
    nc.addEventListener("click", () => toggleNoChar());
    box.appendChild(nc);
  }
  for (const [name, count] of shown) {
    const chip = document.createElement("button");
    chip.className = "chip" + (sel.has(name) ? " on" : "");
    chip.dataset.name = name;
    chip.innerHTML = `<span>${esc(name)}</span><span class="chip-cnt">${fmtCount(count)}</span>`;
    if (grp === "character") {
      const ok = chipAllowed(name);
      const zero = !!state.facets && !count;
      chip.classList.toggle("dim", !ok || zero);
      chip.title = !ok ? `角色「${name}」不属于当前所选学院，强行选择结果将为空`
        : zero ? "当前筛选下没有这个角色的文件" : "";
    } else if (state.facets && !count) {
      chip.classList.add("dim");
      chip.title = "当前筛选下没有该标签的文件";
    }
    if (grp === "artist" && aliases[name]) {
      chip.title = `别名：${aliases[name].join("、")}`;
    }
    chip.addEventListener("click", () => toggleFilter(key, name));
    box.appendChild(chip);
  }
  if (grp === "artist" && entries.length > 30) {
    const more = document.createElement("button");
    more.className = "grp-more";
    more.textContent = grpUI.artistShowAll ? "收起（显示前 30）" : `显示全部 ${entries.length} 个画师`;
    more.addEventListener("click", () => {
      grpUI.artistShowAll = !grpUI.artistShowAll;
      renderGroup(grp, key, map);
    });
    box.appendChild(more);
  }
  search.oninput = () => {
    const q = search.value.trim();
    if (grp === "artist" && q && !grpUI.artistShowAll) {
      grpUI.artistShowAll = true;
      renderGroup(grp, key, map);
    }
    if (grp === "character") renderRecentChars(map, q);
    box.querySelectorAll(".chip").forEach((c) => {
      if (c.classList.contains("chip-notag")) { c.style.display = ""; return; }
      const name = c.dataset.name;
      let hit = !q || name.includes(q);
      if (!hit && grp === "artist") hit = (aliases[name] || []).some((a) => a.includes(q));
      if (!hit) hit = tagMatch(name, q);
      c.style.display = hit ? "" : "none";
    });
    const moreBtn = box.querySelector(".grp-more");
    if (moreBtn) moreBtn.style.display = q ? "none" : "";
  };
  if (grp === "character") renderRecentChars(map, search.value.trim());
}

function chipAllowed(charName) {
  const acads = state.filters.academies;
  if (!acads.length || !state.tags) return true;
  return acads.some((a) => (state.tags.relations[a] || []).includes(charName));
}

function pinyinOf(name) {
  return (state.tags && state.tags.pinyin && state.tags.pinyin[name]) || null;
}

// 与后端 norm() 对齐：NFKC 归一化（数学粗体/全角等 Unicode 变体 → 普通字符）+ 小写 + 去空格下划线。
// 这样输入 kiroshi 也能命中画师「𝗞𝗶𝗿𝗼𝘀𝗵𝗶」。
function normKey(s) {
  return (s == null ? "" : String(s)).normalize("NFKC").trim().toLowerCase().replace(/[\s_]+/g, "");
}

function tagMatchScore(name, q) {
  if (!q) return 1;
  const ql = normKey(q);
  if (!ql) return 1;
  const nl = normKey(name);
  if (nl === ql) return 1000;
  if (nl.startsWith(ql)) return 900;
  if (nl.includes(ql)) return 400;
  // 画师别名：主名命中不了时，用任一别名做匹配（如输入 OMO 找到 Oo12271547）；
  // normKey 会把数学粗体等 Unicode 变体归一化（𝗞𝗶𝗿𝗼𝘀𝗵𝗶 → kiroshi），普通写法也能搜到
  const al = (state.tags && state.tags.artist_aliases && state.tags.artist_aliases[name]) || [];
  if (al.length) {
    const hit = al.filter((a) => normKey(a).includes(ql));
    if (hit.length) {
      const best = normKey(hit.sort((a, b) => b.length - a.length)[0]);
      if (best === ql) return 860;
      if (best.startsWith(ql)) return 700;
      return 380;
    }
  }
  const p = pinyinOf(name);
  if (!p) return 0;
  if (p.py === ql) return 800;
  if (p.ab === ql) return 790;
  if (p.ab.startsWith(ql)) return 690;
  if (ql.length >= 2) {
    if (p.py.includes(ql)) return 500;
    if (p.ab.includes(ql)) return 490;
  }
  return 0;
}

function tagMatch(name, q) {
  return tagMatchScore(name, q) > 0;
}

function tagSort(a, b, q) {
  const sa = tagMatchScore(a[0], q), sb = tagMatchScore(b[0], q);
  if (sa !== sb) return sb - sa;
  return b[1] - a[1];
}

/* ================= 近期标签（侧栏角色组推荐） ================= */
// 记录最近用过的角色标签（侧栏筛选、打标都算），按最近使用排序；只存在本机 localStorage。
const RECENT_KEY = "emojiTool.recentTags.v1";
const RECENT_SHOW = 10;   // 推荐区最多显示几个
const RECENT_KEEP = 40;   // 本地最多记几个
const recentTags = { characters: [] };

function loadRecentTags() {
  try {
    const raw = JSON.parse(localStorage.getItem(RECENT_KEY) || "null");
    const arr = Array.isArray(raw) ? raw : (raw && raw.characters);
    if (Array.isArray(arr)) recentTags.characters = arr.filter((n) => typeof n === "string" && n);
  } catch (e) { recentTags.characters = []; }
}

function saveRecentTags() {
  try { localStorage.setItem(RECENT_KEY, JSON.stringify({ characters: recentTags.characters })); } catch (e) { /* 隐私模式等存不了就算了 */ }
}

function touchRecentTag(name) {
  if (!name) return;
  const arr = recentTags.characters;
  const i = arr.indexOf(name);
  if (i >= 0) arr.splice(i, 1);
  arr.unshift(name);
  if (arr.length > RECENT_KEEP) arr.length = RECENT_KEEP;
  saveRecentTags();
}

// 按当前搜索词筛近期标签：无输入 = 全部（最多 10 个）；有输入 = 与主列表同一套匹配（含拼音/别名）
function recentCharNames(map, q) {
  const out = [];
  for (const n of recentTags.characters) {
    if (map[n] === undefined) continue;          // 库里已经没有这个标签了，跳过
    if (q && !tagMatch(n, q)) continue;
    out.push(n);
  }
  if (q) out.sort((a, b) => tagSort([a, map[a] || 0], [b, map[b] || 0], q));
  return out.slice(0, RECENT_SHOW);
}

// 模板被 Flask 缓存（改 index.html 需重启）时兜底：DOM 里没有推荐区就自己建一个，放在角色 chip 列表上面
function ensureRecentHost() {
  let wrap = $("recent-character");
  if (wrap) return wrap;
  const chips = $("chips-character");
  const body = chips && chips.parentElement;
  if (!body) return null;
  wrap = document.createElement("div");
  wrap.className = "grp-recent";
  wrap.id = "recent-character";
  wrap.hidden = true;
  wrap.innerHTML = `<div class="grp-recent-head">近期<span class="grp-recent-tip">最近用过的角色</span></div><div class="chips" id="chips-recent-character"></div>`;
  body.insertBefore(wrap, chips);
  return wrap;
}

function renderRecentChars(map, q) {
  const wrap = ensureRecentHost();
  const box = $("chips-recent-character");
  if (!wrap || !box) return;
  const m = map || groupMap("characters");
  const search = $("search-character");
  const query = q == null ? ((search && search.value.trim()) || "") : q;
  const names = recentCharNames(m, query);
  box.innerHTML = "";
  if (!names.length) { wrap.hidden = true; return; }
  const sel = new Set(state.filters.characters);
  for (const name of names) {
    const cnt = m[name] || 0;
    const chip = document.createElement("button");
    chip.className = "chip" + (sel.has(name) ? " on" : "") + (state.facets && !cnt ? " dim" : "");
    chip.dataset.name = name;
    chip.innerHTML = `<span>${esc(name)}</span><span class="chip-cnt">${fmtCount(cnt)}</span>`;
    chip.title = "近期使用";
    chip.addEventListener("click", () => toggleFilter("characters", name));
    box.appendChild(chip);
  }
  wrap.hidden = false;
}

function toggleFilter(key, value) {
  if (key === "characters" && !chipAllowed(value) && !state.filters.characters.includes(value)) {
    toast(`强制筛选：角色「${value}」不属于所选学院，结果将为空`, "warn");
  }
  const set = state.filters[key];
  const i = set.indexOf(value);
  if (i >= 0) set.splice(i, 1);
  else {
    set.push(value);
    if (key === "characters") touchRecentTag(value);   // 只在"用上"时记，取消选择不算
  }
  if (key === "characters") renderRecentChars(groupMap("characters"));
  refreshFilterToolbar();
  renderFilterBar();
  if (state.view === "all" || state.view === "untagged") loadFiles(true);
}

function toggleNoChar() {
  state.filters.no_char = !state.filters.no_char;
  renderGroups();
  refreshFilterToolbar();
  renderFilterBar();
  if (state.view === "all" || state.view === "untagged") loadFiles(true);
}

/* ================= 顶部搜索建议（支持 ↑↓ 选择 + 回车确认） ================= */
const qSug = { items: [], hi: -1 };

function qSugSetHi(i) {
  const box = $("q-suggest");
  const n = qSug.items.length;
  qSug.hi = n ? ((i % n) + n) % n : -1;
  box.querySelectorAll(".sg-item").forEach((b) => b.classList.toggle("hi", +b.dataset.idx === qSug.hi));
  const act = qSug.hi >= 0 ? box.querySelector(`.sg-item[data-idx="${qSug.hi}"]`) : null;
  if (act && act.scrollIntoView) act.scrollIntoView({ block: "nearest" });
}

function qSugChoose(i) {
  const it = qSug.items[i];
  if (!it) return;
  const qEl = $("q");
  const box = $("q-suggest");
  qEl.value = "";
  state.filters.q = "";
  qSug.items = []; qSug.hi = -1;
  box.innerHTML = "";
  box.hidden = true;
  toggleFilter(it.key, it.name);   // 点/回车选中建议 = 用这个标签筛选，同时丢掉已输入的关键词
  qEl.focus();
}

function qSuggestGroups() {
  const q = $("q").value.trim();
  const box = $("q-suggest");
  qSug.items = []; qSug.hi = -1;
  if (!q || !state.tags) { box.hidden = true; return; }
  const groups = [["academies", "学院"], ["characters", "角色"], ["artists", "画师"], ["specials", "特殊"]];
  const hits = [];
  for (const [key, label] of groups) {
    const matched = Object.entries(state.tags[key] || {}).filter(([name]) => tagMatch(name, q)).sort((a, b) => tagSort(a, b, q)).slice(0, 8);
    if (matched.length) hits.push({ label, key, matched });
  }
  if (!hits.length) { box.hidden = true; return; }
  const items = [];
  const html = hits.map((h) =>
    `<div class="sg-group">${h.label}</div>` + h.matched.map(([name, cnt]) => {
      const idx = items.length;
      items.push({ key: h.key, name });
      return `<button class="sg-item" data-idx="${idx}"><span class="ty">${h.label}</span><span>${esc(name)}</span><span class="cnt">${fmtCount(cnt)}</span></button>`;
    }).join("")
  ).join("");
  box.innerHTML = html;
  qSug.items = items;
  box.querySelectorAll(".sg-item").forEach((b) => {
    b.addEventListener("mousemove", () => { if (qSug.hi !== +b.dataset.idx) qSugSetHi(+b.dataset.idx); });
    b.addEventListener("click", () => qSugChoose(+b.dataset.idx));
  });
  box.hidden = false;
}

function renderFilterBar() {
  const bar = $("filterbar");
  bar.innerHTML = "";
  const f = state.filters;
  const chips = [];
  if (f.name) chips.push(["name", "文件名", f.name]);
  if (f.no_char) chips.push(["no_char", "角色", "无角色标签"]);
  for (const [key, label] of [["academies", "学院"], ["characters", "角色"], ["artists", "画师"], ["specials", "特殊"], ["folders", "文件夹"], ["exts", "类型"]]) {
    for (const v of f[key]) chips.push([key, label, v]);
  }
  for (const [key, label, v] of chips) {
    const c = document.createElement("span");
    c.className = "fchip";
    c.innerHTML = `${label}·${esc(v)} <button title="移除">×</button>`;
    c.querySelector("button").addEventListener("click", () => {
      if (key === "name") applyNameFilter("");
      else if (key === "no_char") toggleNoChar();
      else toggleFilter(key, v);
    });
    bar.appendChild(c);
  }
}

/* ================= 筛选下拉工具栏 ================= */
const FTR_DEFS = [["name", "文件名"], ["folder", "文件夹"], ["academy", "学院"], ["character", "角色"], ["artist", "画师"], ["ext", "文件类型"]];
const FTR_KEY = { name: "name", folder: "folders", academy: "academies", character: "characters", artist: "artists", ext: "exts" };
const ftrState = { open: null, search: {} };
const debouncedNameFilter = debounce((v) => applyNameFilter(v), 350);

function ftrHasVal(key) {
  if (key === "name") return !!state.filters.name;
  return state.filters[FTR_KEY[key]].length > 0;
}

function renderFilterToolbar() {
  const row = $("ftr-row");
  row.innerHTML = "";
  for (const [key, label] of FTR_DEFS) {
    const wrap = document.createElement("div");
    wrap.className = "ftr" + (ftrState.open === key ? " open" : "");
    wrap.dataset.key = key;
    const btn = document.createElement("button");
    btn.className = "ftr-btn" + (ftrHasVal(key) ? " has-val" : "");
    btn.innerHTML = `${label} <svg class="chev" viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg>` + (ftrHasVal(key) ? `<span class="fdot"></span>` : "");
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      ftrState.open = ftrState.open === key ? null : key;
      renderFilterToolbar();
    });
    const menu = document.createElement("div");
    menu.className = "ftr-menu";
    menu.dataset.menu = key;
    menu.hidden = ftrState.open !== key;
    wrap.appendChild(btn);
    wrap.appendChild(menu);
    row.appendChild(wrap);
    if (ftrState.open === key) fillFtrMenu(menu, key);
  }
}

function refreshFilterToolbar() {
  // 仅更新选中态与已开菜单列表，不重建整个工具栏，避免下拉框重置到开头
  document.querySelectorAll(".ftr").forEach((wrap) => {
    const key = wrap.dataset.key;
    const btn = wrap.querySelector(".ftr-btn");
    if (btn) btn.classList.toggle("has-val", ftrHasVal(key));
  });
  if (ftrState.open && ftrState.open !== "name") {
    const menu = document.querySelector(`.ftr-menu[data-menu="${ftrState.open}"]`);
    if (menu) renderFtrList(menu, ftrState.open, FTR_KEY[ftrState.open]);
  }
}

function applyNameFilter(v) {
  if (state.filters.name === v) return;
  state.filters.name = v;
  renderFilterToolbar();
  renderFilterBar();
  if (state.view === "all" || state.view === "untagged") loadFiles(true);
}

function fillFtrMenu(menu, key) {
  const f = state.filters;
  const s = ftrState.search[key] || "";
  if (key === "name") {
    menu.innerHTML = `<div class="ftr-name-box"><input class="ftr-name-input" placeholder="输入文件名关键字…" value="${esc(f.name)}" autocomplete="off">${f.name ? `<button class="ftr-clear" title="清除">清除</button>` : ""}</div>`;
    const inp = menu.querySelector(".ftr-name-input");
    inp.focus();
    const len = inp.value.length;
    inp.setSelectionRange(len, len);
    inp.addEventListener("keydown", (e) => { if (e.key === "Enter") applyNameFilter(inp.value.trim()); });
    inp.addEventListener("input", () => debouncedNameFilter(inp.value.trim()));
    const clr = menu.querySelector(".ftr-clear");
    if (clr) clr.addEventListener("click", () => applyNameFilter(""));
    return;
  }
  const mapKey = FTR_KEY[key];
  if (!menu.dataset.built) {
    // 输入框只创建一次，避免每次输入都重建导致光标回到开头、字符顺序倒置
    menu.dataset.built = "1";
    menu.innerHTML = `<input class="ftr-search" placeholder="筛选…" value="${esc(s)}" autocomplete="off"><div class="ftr-list"></div>`;
    const si = menu.querySelector(".ftr-search");
    si.addEventListener("input", () => {
      ftrState.search[key] = si.value.trim();
      renderFtrList(menu, key, mapKey);
    });
  }
  const si = menu.querySelector(".ftr-search");
  if (document.activeElement !== si) si.focus();
  si.setSelectionRange(si.value.length, si.value.length);
  renderFtrList(menu, key, mapKey);
}

function renderFtrList(menu, key, mapKey) {
  const f = state.filters;
  const s = ftrState.search[key] || "";
  const sel = new Set(f[mapKey]);
  // 有筛选时用 /api/facets 的计数（与左侧栏、与网格实际结果一致）：否则这里显示的是
  // 全库数量，选完才发现网格是空的
  let entries = Object.entries(groupMap(mapKey));
  entries.sort((a, b) => (s ? tagSort(a, b, s) : a[0].localeCompare(b[0], "zh")));
  if (state.facets) { // 当前筛选下没有文件的标签不再列出；已选中的留着，否则没法取消
    entries = entries.filter(([n, c]) => c > 0 || sel.has(n));
  }
  if (key === "character" && state.filters.academies.length) {
    // 已选学院时只保留匹配的角色标签
    entries = entries.filter(([n]) => chipAllowed(n));
  }
  if (s) {
    const sl = s.toLowerCase();
    entries = key === "folder" || key === "ext"
      ? entries.filter(([n]) => n.toLowerCase().includes(sl))
      : entries.filter(([n]) => tagMatch(n, s));
  }
  let html = "";
  if (key === "character") {
    html += `<button class="ftr-item${state.filters.no_char ? " on" : ""}" data-nc="1"><span class="cb">${state.filters.no_char ? "✓" : ""}</span><span class="nm">无角色标签</span></button>`;
  }
  if (!entries.length) html += `<div class="ftr-empty">无匹配</div>`;
  for (const [n, c] of entries) {
    const on = sel.has(n);
    html += `<button class="ftr-item${on ? " on" : ""}${c ? "" : " dim"}" data-val="${esc(n)}"><span class="cb">${on ? "✓" : ""}</span><span class="nm">${esc(n)}</span><span class="cnt">${fmtCount(c)}</span></button>`;
  }
  const list = menu.querySelector(".ftr-list");
  const st = menu.scrollTop;
  if (list) list.innerHTML = html;
  menu.scrollTop = st;
  const ncIt = menu.querySelector('[data-nc="1"]');
  if (ncIt) ncIt.addEventListener("click", (e) => { e.stopPropagation(); toggleNoChar(); });
  menu.querySelectorAll(".ftr-item").forEach((it) => it.addEventListener("click", (e) => { e.stopPropagation(); toggleFilter(mapKey, it.dataset.val); }));
}

/* ================= 视图切换 ================= */
function setView(view) {
  state.view = view;
  clearSelection(true);
  closeInspector();
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.nav === view));
  const isLibrary = view === "all" || view === "untagged";
  $("sort-box").hidden = !isLibrary;
  $("btn-trash-clear").hidden = view !== "trash";
  $("trash-hint").hidden = view !== "trash";
  $("f-untagged").hidden = !isLibrary;
  $("btn-organize").hidden = !isLibrary;
  $("btn-select-all").hidden = false;
  $("btn-batch-tags").hidden = !isLibrary;
  $("btn-batch-trash").hidden = view === "trash";
  $("btn-batch-restore").hidden = view !== "trash";
  $("ftr-row").hidden = !isLibrary;
  const titles = { all: "全部文件", untagged: "未标记", inbox: "未处理", trash: "废纸篓" };
  $("view-title").firstChild.textContent = titles[view];
  if (!isLibrary) { // 未处理/废纸篓不走筛选，侧栏回到全库计数
    facetsSeq++;
    state.facets = null;
    renderGroups();
    updateNavCounts();
  }
  if (view === "inbox") { $("view-sub").textContent = "侧栏打标（可多选批量），取消选中/点击其他文件时自动归位"; renderInbox(); }
  else if (view === "trash") { $("view-sub").textContent = ""; renderTrash(); }
  else {
    state.filters.untagged = view === "untagged";
    $("f-untagged").checked = state.filters.untagged;
    $("view-sub").textContent = "";
    loadFiles(true);
  }
}

/* ================= 文件加载（库视图） ================= */
function filterParams() {
  const f = state.filters;
  const p = new URLSearchParams();
  f.academies.forEach((v) => p.append("academy", v));
  f.characters.forEach((v) => p.append("character", v));
  f.artists.forEach((v) => p.append("artist", v));
  f.specials.forEach((v) => p.append("special", v));
  f.folders.forEach((v) => p.append("folder", v));
  f.exts.forEach((v) => p.append("ext", v));
  if (f.name) p.set("name", f.name);
  if (f.untagged) p.set("untagged", "1");
  if (f.no_char) p.set("no_char", "1");
  if (f.q) p.set("q", f.q);
  return p;
}

function buildQuery() {
  const p = filterParams();
  p.set("sort", state.sort);
  p.set("dir", state.sortDir);
  p.set("limit", state.pageSize);
  p.set("offset", state.offset);
  return p.toString();
}

function hasActiveFilter() {
  const f = state.filters;
  return !!(f.academies.length || f.characters.length || f.artists.length || f.specials.length ||
    f.folders.length || f.exts.length || f.name || f.untagged || f.no_char || f.q);
}

// 侧栏计数随筛选条件变化：筛选变了就去 /api/facets 取「当前筛选下每个标签还剩多少文件」
let facetsTimer = null, facetsSeq = 0;
function scheduleFacets(delay = 150) { clearTimeout(facetsTimer); facetsTimer = setTimeout(loadFacets, delay); }
async function loadFacets() {
  if (!hasActiveFilter()) { // 没筛选：侧栏直接用 /api/tags 的全库计数，不必多跑一次查询
    if (state.facets) {
      facetsSeq++; // 丢掉在途的响应，免得它又把 facets 塞回来
      state.facets = null;
      renderGroups();
      updateNavCounts();
      refreshFilterToolbar(); // 下拉里开着的那组要从「筛选后的列表」回到全量列表
    }
    return;
  }
  const seq = ++facetsSeq;
  try {
    const data = await api("/api/facets?" + filterParams().toString());
    if (seq !== facetsSeq) return; // 过期响应
    state.facets = data;
  } catch (e) {
    if (seq !== facetsSeq) return;
    state.facets = null;
  }
  renderGroups();
  updateNavCounts();
  refreshFilterToolbar(); // 顶部下拉里开着的那一组计数也要跟着换
}

async function loadFiles(reset) {
  if (!reset && state.loading) return;
  const seq = ++state.loadSeq;
  state.loading = true;
  if (reset) {
    state.offset = 0; state.done = false; state.files = [];
    clearSelection(true); closeInspector();
    $("grid").innerHTML = ""; $("grid").style.gridTemplateColumns = "";
    scheduleFacets();
  }
  const reqOffset = state.offset;
  try {
    const data = await api("/api/files?" + buildQuery());
    if (seq !== state.loadSeq) return; // 过期响应：期间又发起了新搜索/筛选，忽略旧结果
    state.files = state.files.concat(data.files);
    state.total = data.total;
    state.done = state.files.length >= data.total;
    renderNote(data.note);
    appendCards(data.files);
    $("result-count").textContent = `共 ${fmtCount(state.total)} 个文件`;
    $("empty").hidden = data.files.length > 0 || reqOffset > 0;
    $("sentinel").hidden = state.done;
    state.offset = reqOffset + data.files.length;
  } catch (e) {
    if (seq === state.loadSeq) toast(e.message, "error");
  } finally {
    if (seq === state.loadSeq) state.loading = false;
  }
}

function renderNote(note) {
  const el = $("note");
  el.hidden = !note;
  el.textContent = note ? "⚠ " + note : "";
}

function thumbUrl(rel, mtime) { return "/api/thumb?rel=" + encodeURIComponent(rel) + (mtime ? "&v=" + mtime : ""); }
function fileUrl(rel, mtime) { return "/api/file?rel=" + encodeURIComponent(rel) + (mtime ? "&v=" + mtime : ""); }

function cardHTML(f) {
  const badges = [];
  if ((f.specials || []).length) badges.push(`<i class="bd bd-sp">${esc(f.specials.join("/"))}</i>`);
  if (f.artist) badges.push(`<i class="bd bd-ar">${esc(f.artist)}</i>`);
  return `
  <div class="card" data-rel="${esc(f.rel)}">
    <div class="thumb">
      <img loading="lazy" alt="" src="${thumbUrl(f.rel, f.mtime)}">
      <span class="bd-type ${typeCls(f.name)}">${typeOf(f.name)}</span>
      <div class="check">${ICONS.check}</div>
      <div class="badges">${badges.join("")}</div>
      <div class="hover-actions">
        <button class="hact" data-act="view" title="全屏预览">${ICONS.eye}</button>
        <button class="hact" data-act="tags" title="编辑标签">${ICONS.tag}</button>
        <button class="hact" data-act="rename" title="重命名">${ICONS.pen}</button>
        <button class="hact danger" data-act="trash" title="移入废纸篓">${ICONS.trash}</button>
      </div>
    </div>
    <div class="card-info">
      <div class="card-name" title="${esc(f.rel)}">${esc(f.name)}</div>
      <div class="card-path" title="${esc(f.folder || "未处理")}">${esc(f.folder || "未处理")}</div>
      <div class="card-meta">${fmtSize(f.size)} · ${typeOf(f.name)}</div>
    </div>
  </div>`;
}

function appendCards(list) {
  const grid = $("grid");
  const frag = document.createDocumentFragment();
  for (const f of list) {
    const div = document.createElement("div");
    div.innerHTML = cardHTML(f);
    const card = div.firstElementChild;
    bindCard(card, f);
    if (state.selected.has(f.rel)) card.classList.add("selected");
    frag.appendChild(card);
  }
  grid.appendChild(frag);
}

function bindCard(card, f) {
  card.addEventListener("click", (e) => {
    if (e.target.closest(".hact")) return;
    if (e.target.closest(".check")) { toggleSelect(f.rel, card); return; }
    if (e.ctrlKey || e.metaKey) { toggleSelect(f.rel, card); return; }
    selectOne(f.rel, card);
  });
  card.addEventListener("dblclick", (e) => { if (!e.target.closest(".card-name")) openViewer(f); });
  const cardName = card.querySelector(".card-name");
  if (cardName) cardName.addEventListener("dblclick", (e) => { e.stopPropagation(); inlineRename(cardName, f.rel); });
  card.querySelector('[data-act="view"]').addEventListener("click", (e) => { e.stopPropagation(); openViewer(f); });
  card.querySelector('[data-act="tags"]').addEventListener("click", (e) => { e.stopPropagation(); selectOne(f.rel, card, true); });
  card.querySelector('[data-act="rename"]').addEventListener("click", (e) => { e.stopPropagation(); inlineRename(cardName, f.rel); });
  card.querySelector('[data-act="trash"]').addEventListener("click", (e) => { e.stopPropagation(); doTrash(f); });
}

/* ================= 选择 ================= */
function findFile(rel) {
  return state.files.find((x) => x.rel === rel) || state.inboxFiles.find((x) => x.rel === rel) || null;
}

function selectOne(rel, card, keep) {
  const prev = new Set(state.selected);
  const sameRel = prev.size === 1 && prev.has(rel);
  if (sameRel && !keep) {
    // 再次单击已选中的唯一文件 → 取消选中（触发自动归位）
    state.selected.clear();
    syncCardClasses();
    updateBatchBar();
    closeInspector();
    flushPendingAll(prev);
    return;
  }
  if (!prev.has(rel)) {
    // 点击新的文件 → 单选替换；此前已选中/待归位的文件此刻取消选中并归位
    state.selected.clear();
    state.selected.add(rel);
    flushPendingAll(prev);
  }
  // 若 rel 已在选择集中（含多选）：保持选择集不变，仅把右侧栏聚焦到该文件
  syncCardClasses();
  updateBatchBar();
  const f = findFile(rel);
  if (f) openInspector(f);
}

function toggleSelect(rel, card) {
  const removing = state.selected.has(rel);
  if (removing) { state.selected.delete(rel); card.classList.remove("selected"); }
  else { state.selected.add(rel); card.classList.add("selected"); }
  updateBatchBar();
  if (removing) flushPendingAll(new Set([rel]));
  if (state.selected.size === 0) {
    closeInspector();
  } else if (!removing && state.selected.size >= 2) {
    const f = findFile(rel);
    if (f) openInspector(f);
  }
}

function syncCardClasses() {
  document.querySelectorAll(".card").forEach((c) => {
    c.classList.toggle("selected", state.selected.has(c.dataset.rel));
  });
}

function syncTrashRowClasses() {
  document.querySelectorAll(".trash-row").forEach((c) => {
    c.classList.toggle("selected", state.selected.has(c.dataset.rel));
  });
}

function toggleTrashSelect(rel, row) {
  const removing = state.selected.has(rel);
  if (removing) { state.selected.delete(rel); row.classList.remove("selected"); }
  else { state.selected.add(rel); row.classList.add("selected"); }
  updateBatchBar();
  if (state.selected.size === 0) closeInspector();
}

function clearSelection(keepSelectionSet = false) {
  const prev = new Set(state.selected);
  state.selected.clear();
  syncCardClasses();
  updateBatchBar();
  flushPendingAll(prev);
  if (!keepSelectionSet) closeInspector();
}

function updateBatchBar() {
  const n = state.selected.size;
  $("batchbar").hidden = n === 0;
  $("batch-count").textContent = n;
}

/* ================= 鼠标拖拽框选 ================= */
// 在网格空白处按下并拖动 → 拉出选框，框到的卡片进入选择集（配合 Ctrl/Shift 为追加）
function initMarquee() {
  const grid = $("grid");
  const SKIP = "button, input, select, textarea, a, label, .rename-inline, .hact, .check, .restore-btn";
  let box = null, startX = 0, startY = 0, additive = false, active = false, dragged = false, scrollTimer = null;
  let baseSel = new Set(), autoScroll = 0, swallowClick = false;

  const hitRels = (x1, y1, x2, y2) => {
    const hit = [];
    grid.querySelectorAll(".card, .trash-row").forEach((el) => {
      if (!el.dataset.rel) return;
      const r = el.getBoundingClientRect();
      if (r.width && r.height && r.left < x2 && r.right > x1 && r.top < y2 && r.bottom > y1) hit.push(el.dataset.rel);
    });
    return hit;
  };

  const applySelection = (rels) => {
    const next = additive ? new Set([...baseSel, ...rels]) : new Set(rels);
    if (next.size === state.selected.size && [...next].every((r) => state.selected.has(r))) return;
    state.selected.clear();
    next.forEach((r) => state.selected.add(r));
    syncCardClasses();
    syncTrashRowClasses();
    updateBatchBar();
  };

  const stopAutoScroll = () => {
    autoScroll = 0;
    if (scrollTimer) { clearInterval(scrollTimer); scrollTimer = null; }
  };

  grid.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.detail > 1) return;
    if (e.target.closest(".card, .trash-row")) return;   // 卡片上的点击交给原有逻辑
    if (e.target.closest(SKIP)) return;
    active = true;
    dragged = false;
    startX = e.clientX;
    startY = e.clientY;
    baseSel = new Set(state.selected);
    additive = e.ctrlKey || e.metaKey || e.shiftKey;
    e.preventDefault();                                   // 拖动时不选中文字
  });

  document.addEventListener("mousemove", (e) => {
    if (!active) return;
    if (!dragged) {
      if (Math.abs(e.clientX - startX) + Math.abs(e.clientY - startY) < 5) return;
      dragged = true;
      box = document.createElement("div");
      box.className = "marquee";
      document.body.appendChild(box);
    }
    const x1 = Math.min(startX, e.clientX), y1 = Math.min(startY, e.clientY);
    const x2 = Math.max(startX, e.clientX), y2 = Math.max(startY, e.clientY);
    box.style.left = x1 + "px";
    box.style.top = y1 + "px";
    box.style.width = (x2 - x1) + "px";
    box.style.height = (y2 - y1) + "px";
    applySelection(hitRels(x1, y1, x2, y2));
    // 拖到窗口上下边缘时自动滚动，方便跨屏框选
    const edge = 56;
    autoScroll = e.clientY < edge ? -20 : (e.clientY > window.innerHeight - edge ? 20 : 0);
    if (autoScroll && !scrollTimer) {
      scrollTimer = setInterval(() => { if (active && autoScroll) window.scrollBy(0, autoScroll); }, 40);
    } else if (!autoScroll) stopAutoScroll();
  });

  document.addEventListener("mouseup", () => {
    if (!active) return;
    active = false;
    stopAutoScroll();
    if (!dragged) return;
    dragged = false;
    if (box) { box.remove(); box = null; }
    swallowClick = true;
    setTimeout(() => { swallowClick = false; }, 60);
    // 被取消选中的文件：按原有规则归位/清未处理暂存
    const removed = [...baseSel].filter((r) => !state.selected.has(r));
    if (removed.length) flushPendingAll(new Set(removed));
    if (state.selected.size) {
      const f = findFile([...state.selected].pop());
      if (f) openInspector(f);
    } else {
      closeInspector();
    }
  }, true);

  // 框选结束时松手会落在卡片上 → 吞掉随之而来的那次 click，避免选择被重置为单个
  document.addEventListener("click", (e) => {
    if (!swallowClick) return;
    swallowClick = false;
    e.stopPropagation();
    e.preventDefault();
  }, true);
}

/* ================= 右侧属性面板 ================= */
function openInspector(f) {
  state.current = f;
  state.lastFile = f;
  $("insp-name").textContent = f.name;
  const img = $("insp-img");
  img.onload = () => { $("insp-dim").textContent = img.naturalWidth ? `${img.naturalWidth} × ${img.naturalHeight}` : ""; };
  img.src = fileUrl(f.rel, f.mtime);
  refreshTagEditors();
  loadSuggestions(f.rel, "insp");
  let meta =
    `<b>路径</b><span>${esc(f.folder || "未处理")}</span>` +
    `<b>类型</b><span>${typeOf(f.name)}</span>` +
    `<b>大小</b><span>${fmtSize(f.size)}</span>` +
    `<b>创建时间</b><span>${f.ctime ? new Date(f.ctime * 1000).toLocaleString() : "-"}</span>` +
    `<b>修改时间</b><span>${f.mtime ? new Date(f.mtime * 1000).toLocaleString() : "-"}</span>` +
    `<b>画师原值</b><span>${esc(f.artist_raw || "-")}</span>`;
  if (f.artist && state.tags.artist_aliases && state.tags.artist_aliases[f.artist]) {
    meta += `<b>画师别名</b><span>${esc(state.tags.artist_aliases[f.artist].join("、"))}</span>`;
  }
  $("insp-meta").innerHTML = meta;
  const placeBtn = $("btn-place-file");
  if (placeBtn) placeBtn.hidden = !isInboxRel(f.rel);
  $("inspector").hidden = false;
  document.querySelector(".layout").classList.add("insp-open");
  $("btn-insp-expand").hidden = true;
}

function closeInspector() {
  if (state.current && state.pendingMove.has(state.current.rel)) flushPendingMoves([state.current.rel]);
  $("inspector").hidden = true;
  document.querySelector(".layout").classList.remove("insp-open");
  state.current = null;
  $("btn-insp-expand").hidden = !state.lastFile;
}

/* ================= 内联打标（pixcall 式，侧边栏直接编辑） ================= */
function mountTagEditor(containerId) {
  const box = $(containerId);
  if (!box) return;
  box.innerHTML = `
    <div class="te-row"><label>学院 <span class="auto-tip">随角色自动关联</span></label><div class="tag-edit-chips" data-kind="academy"></div></div>
    <div class="te-row"><label>角色（可多选，支持拼音）</label><div class="tag-edit-chips" data-kind="character"></div>
      <div class="tag-edit-ip"><input class="tag-edit-input" data-pool="character" placeholder="输入角色或拼音缩写…" autocomplete="off"><div class="tag-edit-sg" hidden></div></div></div>
    <div class="te-row"><label>画师</label><div class="tag-edit-chips" data-kind="artist"></div>
      <div class="tag-edit-ip"><input class="tag-edit-input" data-pool="artist" placeholder="输入画师…" autocomplete="off"><div class="tag-edit-sg" hidden></div></div>
      <div class="alias-row"><span class="alias-label">别名</span><span class="alias-chips"></span><button class="alias-add" title="添加别名">＋</button><span class="alias-ip"><input class="alias-input" placeholder="输入别名（可搜已有画师）后回车" autocomplete="off" hidden><div class="alias-sg" hidden></div></span></div></div>
    <div class="te-row"><label>特殊类型</label><div class="tag-edit-chips" data-kind="special"></div></div>`;
  box.querySelectorAll(".tag-edit-input").forEach((input) => {
    const pool = input.dataset.pool;
    const sg = input.parentElement.querySelector(".tag-edit-sg");
    const groups = pool === "character" ? [["characters", "角色", "character"]] : [["artists", "画师", "artist"]];
    mountSugInput(input, sg, groups);
  });
}

function mountTagEditors() {
  mountTagEditor("insp-tag-edit");
  mountTagEditor("vw-tag-edit");
}

/* ================= 角色候选（只推荐，不自动打标） ================= */
const sugSeq = { insp: 0, vw: 0 };

function ensureSuggestHost(where) {
  let el = $(where + "-suggest");
  if (el) return el;
  const anchor = $(where + "-tag-edit");
  if (!anchor || !anchor.parentElement) return null;
  el = document.createElement("div");
  el.className = "sug-row";
  el.id = where + "-suggest";
  el.hidden = true;
  el.innerHTML = '<span class="sug-label">推荐</span><span class="sug-chips"></span>';
  anchor.parentElement.insertBefore(el, anchor);
  return el;
}

async function loadSuggestions(rel, where) {
  const host = ensureSuggestHost(where);
  if (!host) return;
  const seq = ++sugSeq[where];
  const box = host.querySelector(".sug-chips");
  host.hidden = true;
  if (box) box.innerHTML = "";
  if (!rel) return;
  try {
    const r = await api("/api/suggest?rel=" + encodeURIComponent(rel) + "&n=3");
    if (seq !== sugSeq[where]) return;
    const items = (r && r.items) || [];
    if (!items.length) return;
    for (const it of items) {
      const b = document.createElement("button");
      b.className = "sug-chip";
      b.textContent = it.character;
      b.title = `相似度 ${Number(it.score).toFixed(3)}（只是候选，点了才打标）`;
      b.addEventListener("click", () => {
        applyTag("character", it.character, rel);
        b.classList.add("on");
      });
      box.appendChild(b);
    }
    host.hidden = false;
  } catch (e) { /* 没有索引/接口不可用时静默 */ }
}

function mountSugInput(inputEl, sgEl, groups) {
  let items = [];
  let hi = -1;
  const pool = groups.length ? groups[0][0] : "";
  const isChar = pool === "characters";
  function setHi(i) {
    hi = i;
    sgEl.querySelectorAll(".sg-item").forEach((b, k) => b.classList.toggle("hi", k === hi));
  }
  function choose(i) {
    const it = items[i];
    if (!it) return;
    inputEl.value = "";
    sgEl.hidden = true; items = []; hi = -1;
    if (it.kind === "new-character") { openNewTag("character", it.name); return; }
    if (it.kind === "new-artist") { openNewTag("artist", it.name); return; }
    if (it.kind === "artist-alias") { addArtistAlias(it.name); return; }
    applyTag(it.kind, it.name);
  }
  function show() {
    const q = inputEl.value.trim();
    if (!q || !state.tags) { sgEl.hidden = true; items = []; hi = -1; return; }
    const hits = [];
    for (const [key, label, kind] of groups) {
      const matched = Object.entries(state.tags[key] || {}).filter(([name]) => tagMatch(name, q)).sort((a, b) => tagSort(a, b, q)).slice(0, 6);
      if (matched.length) hits.push({ label, kind, matched });
    }
    items = [];
    let html = "";
    for (const h of hits) {
      html += `<div class="sg-group">${h.label}</div>`;
      for (const [name, cnt] of h.matched) {
        const idx = items.length;
        items.push({ kind: h.kind, name });
        html += `<button class="sg-item" data-idx="${idx}"><span class="ty">${h.label}</span><span>${esc(name)}</span><span class="cnt">${fmtCount(cnt)}</span></button>`;
      }
    }
    // 画师：输入别名时给出主名
    if (!isChar && state.tags.artist_alias_map) {
      const seen = new Set(items.filter((x) => x.kind === "artist").map((x) => x.name));
      for (const [alias, prim] of Object.entries(state.tags.artist_alias_map)) {
        if (tagMatch(alias, q) && !seen.has(prim)) {
          const idx = items.length;
          items.push({ kind: "artist", name: prim });
          html += `<button class="sg-item" data-idx="${idx}"><span class="ty">画师</span><span>${esc(prim)}</span><span class="cnt">别名 ${esc(alias)}</span></button>`;
          seen.add(prim);
        }
      }
    }
    const ql = normKey(q);
    const exact = items.some((it) => normKey(it.name) === ql);
    if (!exact) {
      if (isChar) {
        const idx = items.length;
        items.push({ kind: "new-character", name: q });
        html += `<button class="sg-item sg-new" data-idx="${idx}"><span class="ty">新建</span><span>新建角色「${esc(q)}」</span></button>`;
      } else {
        const idx = items.length;
        items.push({ kind: "new-artist", name: q });
        html += `<button class="sg-item sg-new" data-idx="${idx}"><span class="ty">新建</span><span>新建画师「${esc(q)}」</span></button>`;
        const cur = state.current && state.current.artist;
        if (cur && normKey(cur) !== ql) {
          const idx2 = items.length;
          items.push({ kind: "artist-alias", name: q });
          html += `<button class="sg-item sg-new" data-idx="${idx2}"><span class="ty">别名</span><span>关联「${esc(q)}」到 ${esc(cur)}</span></button>`;
        }
      }
    }
    if (!items.length) { sgEl.hidden = true; items = []; hi = -1; return; }
    sgEl.innerHTML = html;
    sgEl._items = items;
    hi = -1;
    sgEl.querySelectorAll(".sg-item").forEach((b) => {
      b.addEventListener("mousemove", () => setHi(+b.dataset.idx));
      b.addEventListener("click", () => choose(+b.dataset.idx));
    });
    sgEl.hidden = false;
  }
  inputEl.addEventListener("input", show);
  inputEl.addEventListener("keydown", (e) => {
    const n = items.length;
    if (e.key === "ArrowDown") { e.preventDefault(); setHi(n ? (hi + 1) % n : -1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setHi(n ? (hi - 1 + n) % n : -1); }
    else if (e.key === "Enter") {
      if (n) {
        e.preventDefault();
        let idx = hi;
        if (idx < 0) {
          const q = inputEl.value.trim().toLowerCase();
          const qi = items.findIndex((it) => it.name && it.name.toLowerCase() === q);
          idx = qi >= 0 ? qi : 0;
        }
        choose(idx);
      }
    }
    else if (e.key === "Escape") { e.stopPropagation(); sgEl.hidden = true; items = []; hi = -1; }
  });
}

/* ================= 新建标签 / 画师别名 ================= */
let newTagPending = null;

function openNewTag(kind, name) {
  newTagPending = { kind, name };
  $("newtag-title").textContent = kind === "character" ? `新建角色「${name}」` : `新建画师「${name}」`;
  $("newtag-name").value = name;
  const acadBlock = $("newtag-acad-block");
  acadBlock.hidden = kind !== "character";
  if (kind === "character") {
    const sel = $("newtag-acad");
    sel.innerHTML = '<option value="">选择学院…</option><option value="__new">＋ 新建学院…</option>';
    for (const ac of Object.keys(state.tags.academies || {})) {
      const o = document.createElement("option");
      o.value = ac; o.textContent = ac;
      sel.appendChild(o);
    }
    sel.value = "";
    $("newtag-acad-new").hidden = true;
    $("newtag-acad-new").value = "";
  }
  $("modal-newtag").hidden = false;
  setTimeout(() => { if (kind === "character") $("newtag-acad").focus(); }, 60);
}

async function confirmNewTag() {
  const p = newTagPending;
  if (!p) return;
  let academy = "";
  if (p.kind === "character") {
    const sel = $("newtag-acad");
    academy = sel.value === "__new" ? $("newtag-acad-new").value.trim() : sel.value;
    if (!academy) { toast("请选择学院或输入新学院", "warn"); return; }
  }
  const targetRel = state.current ? state.current.rel : null;
  try {
    await api("/api/new_tag", { method: "POST", body: JSON.stringify({ type: p.kind, name: p.name, academy }) });
    $("modal-newtag").hidden = true;
    toast(`已创建${p.kind === "character" ? "角色" : "画师"}「${p.name}」`);
    await loadTags();
    if (targetRel) applyTag(p.kind, p.name, targetRel);
  } catch (e) { toast(e.message, "error"); }
}

// 加/删别名后服务端会重算库里的 files.artist（主名可能换掉），并把真正改到的
// [artist_raw, 旧名, 新名] 一起返回。按这个三元组就地改本地列表里对得上的行即可：
// 不用重拉列表，滚动位置/选中状态都不动，画师名立刻就是新的。
function applyArtistRetag(pairs) {
  if (pairs && pairs.length) {
    const byRaw = new Map(pairs.map(([raw, oldName, newName]) => [raw, [oldName, newName]]));
    const seen = new Set();
    const patch = (fl) => {
      if (!fl || seen.has(fl)) return;
      seen.add(fl);
      const hit = fl.artist_raw ? byRaw.get(fl.artist_raw) : null;
      if (hit && (fl.artist || "") === hit[0]) fl.artist = hit[1];
    };
    state.files.forEach(patch);
    (state.inboxFiles || []).forEach(patch);
    patch(state.current);
    state.files.forEach((fl) => { if (byRaw.has(fl.artist_raw)) refreshCardBadges(fl.rel); });
    // 正按旧画师名筛着的话，筛选值跟到新主名：否则筛选条还停在旧名上，
    // 一重新加载（换排序/翻页）就变成「没有匹配的文件」
    const renamed = new Map(pairs.map(([raw, oldName, newName]) => [oldName, newName]));
    const sel = state.filters.artists;
    let followed = false;
    for (let i = 0; i < sel.length; i++) {
      const nn = renamed.get(sel[i]);
      if (nn && nn !== sel[i]) { sel[i] = nn; followed = true; }
    }
    if (followed) {
      const uniq = [...new Set(sel)];
      sel.length = 0; sel.push(...uniq);
      refreshFilterToolbar(); // 顶部下拉里的勾选与计数
      renderFilterBar();      // 筛选条上那个「画师:旧名」标签
      renderGroups();         // 左侧栏的选中态
      scheduleFacets();       // 计数按新筛选值重取
    }
  }
  // 别名列表本身也变了：把当前打开的面板整块重画（画师名、别名那一行、右侧元信息）
  if (state.current && !$("viewer").hidden) openViewer(state.current);
  else if (state.current && !$("inspector").hidden) openInspector(state.current);
  else refreshTagEditors();
}

async function afterAliasChange(res) {
  await loadTags();
  applyArtistRetag(res && res.retag);
}

async function addArtistAlias(alias) {
  const f = state.current;
  if (!f || !f.artist) { toast("请先给文件设置画师", "warn"); return; }
  if (alias === f.artist) return;
  const before = f.artist;
  try {
    const r = await api("/api/artist_alias", { method: "POST", body: JSON.stringify({ names: [before, alias] }) });
    await afterAliasChange(r);
    toast(`已将「${alias}」关联为「${before}」的别名`);
  } catch (e) { toast(e.message, "error"); }
}

async function removeArtistAlias(name) {
  try {
    const r = await api("/api/artist_alias", { method: "POST", body: JSON.stringify({ remove: name }) });
    await afterAliasChange(r);
    toast(`已移除别名「${name}」`);
  } catch (e) { toast(e.message, "error"); }
}

// 别名输入框：输入关键字时提示「已存在的画师名」（画师名/别名都按 normKey 匹配，
// 所以 𝗞𝗶𝗿𝗼𝘀𝗵𝗶 用 kiroshi 也能搜到），点一下就把那个画师名关联成当前画师的别名。
function wireAliasInput(box, artist) {
  const aliasRow = box.querySelector(".alias-row");
  const aliasAdd = box.querySelector(".alias-add");
  const aliasInput = box.querySelector(".alias-input");
  const aliasSg = box.querySelector(".alias-sg");
  if (!aliasRow || !aliasAdd || !aliasInput || !aliasSg) return;
  let items = [], hi = -1;
  const curKey = normKey(artist);
  function setHi(i) {
    hi = i;
    aliasSg.querySelectorAll(".sg-item").forEach((b, k) => b.classList.toggle("hi", k === hi));
  }
  function close() { aliasSg.hidden = true; items = []; hi = -1; }
  function choose(i) {
    const it = items[i];
    if (!it) return;
    aliasInput.value = ""; aliasInput.hidden = true;
    close();
    addArtistAlias(it.name);
  }
  function show() {
    const q = normKey(aliasInput.value);
    if (!q || !state.tags) { close(); return; }
    const hits = [];
    for (const [name, cnt] of Object.entries(state.tags.artists || {})) {
      const nk = normKey(name);
      if (nk === curKey) continue;
      let score = 0;
      if (nk === q) score = 1000;
      else if (nk.startsWith(q)) score = 900;
      else if (nk.includes(q)) score = 400;
      else {
        const al = (state.tags.artist_aliases && state.tags.artist_aliases[name]) || [];
        if (al.some((a) => normKey(a).includes(q))) score = 350;
        else continue;
      }
      hits.push([score, cnt || 0, name]);
    }
    hits.sort((a, b) => (b[0] - a[0]) || (b[1] - a[1]));
    const top = hits.slice(0, 8);
    if (!top.length) { close(); return; }
    items = top.map(([, , name]) => ({ name }));
    aliasSg.innerHTML = `<div class="sg-group">已有画师（点击关联为别名）</div>` +
      top.map(([, cnt, name], i) =>
        `<button class="sg-item" data-idx="${i}"><span class="ty">画师</span><span>${esc(name)}</span><span class="cnt">${fmtCount(cnt)}</span></button>`).join("");
    aliasSg.querySelectorAll(".sg-item").forEach((b, k) => {
      b.addEventListener("mousedown", (e) => e.preventDefault());
      b.addEventListener("mousemove", () => setHi(k));
      b.addEventListener("click", () => choose(k));
    });
    hi = -1;
    aliasSg.hidden = false;
  }
  aliasAdd.onclick = () => { aliasInput.hidden = false; aliasInput.focus(); show(); };
  aliasInput.oninput = show;
  aliasInput.onkeydown = (e) => {
    const n = items.length;
    if (e.key === "ArrowDown") { if (n) { e.preventDefault(); setHi((hi + 1) % n); } }
    else if (e.key === "ArrowUp") { if (n) { e.preventDefault(); setHi((hi - 1 + n) % n); } }
    else if (e.key === "Enter") {
      if (hi >= 0 && items[hi]) { e.preventDefault(); choose(hi); return; }
      const v = aliasInput.value.trim();
      aliasInput.value = ""; aliasInput.hidden = true;
      close();
      if (v) addArtistAlias(v);
    } else if (e.key === "Escape") {
      e.stopPropagation(); aliasInput.value = ""; close();
    }
  };
  aliasInput.onblur = () => { if (!aliasInput.value.trim()) close(); };
}

function tagChipHTML(kind, cls, name) {
  return `<span class="tag tag-${cls}"><i class="dot"></i>${esc(name)}<button class="tag-x" data-kind="${kind}" data-name="${esc(name)}" title="移除">×</button></span>`;
}

function renderTagChips(box, file) {
  if (!box) return;
  const empty = (s) => `<span class="tag-edit-empty">${s}</span>`;
  const ac = box.querySelector('[data-kind="academy"]');
  const ch = box.querySelector('[data-kind="character"]');
  const ar = box.querySelector('[data-kind="artist"]');
  const sp = box.querySelector('[data-kind="special"]');
  if (!file) {
    if (ac) ac.innerHTML = ""; if (ch) ch.innerHTML = ""; if (ar) ar.innerHTML = ""; if (sp) sp.innerHTML = "";
    return;
  }
  if (ac) ac.innerHTML = file.academy ? tagChipHTML("academy", "ac", file.academy) : empty("未设置");
  if (ch) ch.innerHTML = (file.characters || []).length ? (file.characters || []).map((c) => tagChipHTML("character", "ch", c)).join("") : empty("暂无");
  if (ar) ar.innerHTML = file.artist ? tagChipHTML("artist", "ar", file.artist) : empty("未设置");
  if (sp) {
    const has = (s) => (file.specials || []).includes(s);
    sp.innerHTML = ["差分", "铸币大头", "圣诞特供", "AI"].map((s) =>
      `<button class="sp-chip${has(s) ? " on" : ""}" data-name="${esc(s)}">${esc(s)}</button>`).join("");
    sp.querySelectorAll(".sp-chip").forEach((b) => b.addEventListener("click", () => {
      mutateTags("special", b.dataset.name, !has(b.dataset.name));
    }));
  }
  box.querySelectorAll(".tag-x").forEach((b) => b.addEventListener("click", () => {
    mutateTags(b.dataset.kind, b.dataset.name, false);
  }));
  const aliasRow = box.querySelector(".alias-row");
  if (aliasRow) {
    const artist = file ? file.artist : "";
    const aliasChips = box.querySelector(".alias-chips");
    const aliasAdd = box.querySelector(".alias-add");
    const aliasInput = box.querySelector(".alias-input");
    aliasRow.hidden = !artist;
    if (aliasChips) {
      const aliases = (artist && state.tags && state.tags.artist_aliases && state.tags.artist_aliases[artist]) || [];
      const extraAliases = (artist && state.tags && state.tags.extra_aliases && state.tags.extra_aliases[artist]) || [];
      aliasChips.innerHTML = artist
        ? `<span class="alias-chip"><b>${esc(artist)}</b></span>` + aliases.map((a) =>
            `<span class="alias-chip">${esc(a)}${extraAliases.includes(a) ? `<button data-rm="${esc(a)}" title="移除">×</button>` : ""}</span>`).join("")
        : "";
      aliasChips.querySelectorAll("button[data-rm]").forEach((b) => b.addEventListener("click", () => removeArtistAlias(b.dataset.rm)));
    }
    if (aliasAdd && aliasInput) wireAliasInput(box, artist);
  }
}

function refreshTagEditors() {
  const ef = effectiveFile(state.current);
  ["insp-tag-edit", "vw-tag-edit"].forEach((id) => {
    const box = $(id);
    if (box) renderTagChips(box, ef);
  });
}

function effectiveFile(f) {
  if (!f) return f;
  const p = (state.libOverlay && state.libOverlay.get(f.rel)) || state.pendingTags[f.rel];
  return {
    ...f,
    characters: p ? p.characters : (f.characters || []),
    specials: p ? p.specials : (f.specials || []),
    artist: p ? p.artist : (f.artist || ""),
  };
}

function isInboxRel(rel) { return (rel || "").split("/")[0] === "未处理"; }

function updateInboxCardBadges(rel) {
  const card = [...document.querySelectorAll(".inbox-card")].find((c) => c.dataset.rel === rel);
  const f = card && state.inboxFiles.find((x) => x.rel === rel);
  if (!card || !f) return;
  const ef = effectiveFile(f);
  const badges = [];
  for (const c of ef.characters) badges.push(`<i class="bd bd-ch">${esc(c)}</i>`);
  for (const s of ef.specials) badges.push(`<i class="bd bd-sp">${esc(s)}</i>`);
  if (ef.artist) badges.push(`<i class="bd bd-ar">${esc(ef.artist)}</i>`);
  const box = card.querySelector(".badges");
  if (box) box.innerHTML = badges.join("");
}

function mutateInboxTags(kind, name, add) {
  const rels = state.selected.size ? [...state.selected] : (state.current ? [state.current.rel] : []);
  let changed = false;
  for (const rel of rels) {
    const base = state.inboxFiles.find((x) => x.rel === rel);
    if (!base) continue;
    const p = state.pendingTags[rel] || { characters: [], specials: [], artist: base.artist || "" };
    if (kind === "character") {
      if (add) { if (!p.characters.includes(name)) p.characters.push(name); }
      else p.characters = p.characters.filter((c) => c !== name);
    } else if (kind === "special") {
      if (add) { if (!p.specials.includes(name)) p.specials.push(name); }
      else p.specials = p.specials.filter((s) => s !== name);
    } else if (kind === "artist") {
      p.artist = add ? name : "";
    }
    state.pendingTags[rel] = p;
    changed = true;
    updateInboxCardBadges(rel);
  }
  if (changed) refreshTagEditors();
}

async function flushPendingInbox(rels) {
  const targets = [...(rels || [])].filter((rel) => {
    if (!isInboxRel(rel) || state.placingInbox.has(rel)) return false;
    const p = state.pendingTags[rel];
    if (!p || !state.inboxFiles.some((f) => f.rel === rel)) return false;
    // 有角色 → 归位；仅画师（无角色、无特殊）→ 补 _@画师 后缀并留在未处理
    return p.characters.length > 0 || (p.artist && !p.specials.length);
  });
  if (!targets.length) return;
  const groups = new Map();
  for (const rel of targets) {
    const p = state.pendingTags[rel];
    const key = JSON.stringify([p.characters, p.specials, p.artist]);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(rel);
  }
  for (const group of groups.values()) {
    const p = state.pendingTags[group[0]];
    group.forEach((r) => state.placingInbox.add(r));
    try {
      if (p.characters.length) {
        const r = await api("/api/place", { method: "POST", body: JSON.stringify({ rels: group, characters: p.characters, specials: p.specials, artist: p.artist }) });
        group.forEach((r2) => { delete state.pendingTags[r2]; state.placingInbox.delete(r2); });
        if (r.placed && r.placed.length) toast(`已自动归位 ${r.placed.length} 个文件`);
        if (state.current && group.includes(state.current.rel)) {
          state.selected.delete(state.current.rel);
          closeInspector();
          updateBatchBar();
        }
      } else {
        // 仅打了画师、未选角色：把画师主名写入文件名后缀，文件留在未处理
        const r = await api("/api/inbox_artist", { method: "POST", body: JSON.stringify({ rels: group, artist: p.artist }) });
        const failedRels = new Set((r.failed || []).map((x) => x.rel));
        group.forEach((r2) => {
          state.placingInbox.delete(r2);
          if (!failedRels.has(r2)) delete state.pendingTags[r2];
        });
        const renamedN = (r.renamed || []).length;
        if (renamedN) toast(`已为 ${renamedN} 个文件补画师后缀`);
        if (r.failed && r.failed.length) toast(`画师后缀失败 ${r.failed.length} 个：${esc(r.failed[0].error || "")}`, "error");
        // 重命名的正是当前未处理文件时同步 rel，避免侧栏/缩略图错位
        const byOld = new Map((r.renamed || []).map((x) => [x.old_rel, x]));
        if (state.current && byOld.has(state.current.rel)) {
          const nf = byOld.get(state.current.rel);
          state.current.rel = nf.rel;
          state.current.name = nf.name;
          $("insp-name").textContent = nf.name;
          $("insp-img").src = fileUrl(nf.rel, Date.now());
        }
      }
    } catch (e) {
      group.forEach((r2) => state.placingInbox.delete(r2));
      toast(e.message, "error");
    }
  }
  scheduleTagsRefresh();
  if (state.view === "inbox") refreshInbox();
  else if (state.view === "all" || state.view === "untagged") debouncedSearch();
}

function applyTag(kind, name, rel) { mutateTags(kind, name, true, rel); }

function mutateTags(kind, name, add, rel) {
  if (kind === "character" && add) touchRecentTag(name);   // 打标也算"用过"，进近期推荐
  if (state.view === "inbox") { mutateInboxTags(kind, name, add); return; }
  const targets = libTagTargets(rel);
  let changed = false;
  for (const r of targets) {
    const st = libTagState(r);
    if (!st) continue;
    if (kind === "character") {
      if (add) { if (!st.characters.includes(name)) st.characters.push(name); }
      else { const i = st.characters.indexOf(name); if (i >= 0) st.characters.splice(i, 1); }
      st.acadLock = false; // 角色变更后学院交由后端按角色重新推断
    } else if (kind === "special") {
      if (add) { if (!st.specials.includes(name)) st.specials.push(name); }
      else { const i = st.specials.indexOf(name); if (i >= 0) st.specials.splice(i, 1); }
      st.acadLock = false;
    } else if (kind === "artist") {
      st.artist = add ? name : "";
      st.acadLock = false;
    } else if (kind === "academy") {
      st.acadLock = true;
      st.academy = add ? name : "";
    } else {
      continue;
    }
    state.libOverlay.set(r, st);
    changed = true;
  }
  if (!changed) return;
  if (kind === "character" && state.tags) renderRecentChars(groupMap("characters"));
  refreshTagEditors();
  scheduleLibTagSave();
}

function libTagTargets(rel) {
  // 卡片“标签”图标 / 新建标签后回填：只编辑该文件
  if (rel) return [rel];
  // 全屏查看器是单文件语境：只编辑当前预览文件
  if (!$("viewer").hidden && state.current) return [state.current.rel];
  const sels = [...state.selected];
  if (sels.length) return sels;
  return state.current ? [state.current.rel] : [];
}

function libBaseFile(rel) {
  return state.files.find((x) => x.rel === rel) || (state.current && state.current.rel === rel ? state.current : null);
}

function libTagState(rel) {
  const ov = state.libOverlay.get(rel);
  if (ov) return ov;
  const base = libBaseFile(rel);
  if (!base) return null;
  return {
    characters: [...(base.characters || [])],
    specials: [...(base.specials || [])],
    artist: base.artist || "",
    academy: base.academy || "",
    acadLock: false,
  };
}

function scheduleLibTagSave() {
  clearTimeout(state.libSaveTimer);
  state.libSaveTimer = setTimeout(runLibTagSave, 150);
}

async function runLibTagSave() {
  if (state.libSaveBusy) { state.libSaveAgain = true; return; }
  state.libSaveBusy = true;
  try {
    do {
      state.libSaveAgain = false;
      // 清理已被移动/删除的旧覆盖
      for (const rel of [...state.libOverlay.keys()]) {
        if (!libBaseFile(rel)) state.libOverlay.delete(rel);
      }
      const rows = [];
      for (const [rel, st] of state.libOverlay) {
        const row = { rel, characters: st.characters, specials: st.specials, artist: st.artist };
        if (st.acadLock) row.academy = st.academy;
        rows.push(row);
      }
      if (!rows.length) break;
      const snapKeys = rows.map((r) => JSON.stringify(r));
      try {
        const res = await api("/api/tags_batch", { method: "POST", body: JSON.stringify({ rows, no_move: true }) });
        const byRel = new Map((res.files || []).map((x) => [x.rel, x]));
        for (let k = 0; k < rows.length; k++) {
          const row = rows[k];
          const rf = byRel.get(row.rel);
          const idx = state.files.findIndex((x) => x.rel === row.rel);
          if (rf && idx >= 0) state.files[idx] = rf;
          if (rf) refreshCardBadges(rf.rel);
          state.pendingMove.add(rf ? rf.rel : row.rel);
          const cur = state.libOverlay.get(row.rel);
          if (cur) {
            const curKey = JSON.stringify({ rel: row.rel, characters: cur.characters, specials: cur.specials, artist: cur.artist, ...(cur.acadLock ? { academy: cur.academy } : {}) });
            if (curKey === snapKeys[k]) state.libOverlay.delete(row.rel);
          }
        }
        if (state.current) {
          const ci = state.files.findIndex((x) => x.rel === state.current.rel);
          const rf = byRel.get(state.current.rel);
          if (ci >= 0) state.current = state.files[ci];
          else if (rf) state.current = rf; // 查看器等可能不在当前加载列表
          refreshTagEditors();
        }
        // 保存期间已被取消选中的文件：立即归位（全屏查看器中仍预览的文件除外，关闭时再归位）
        const inViewer = !$("viewer").hidden && state.current && state.current.rel;
        const toMove = [...state.pendingMove].filter((x) => !state.selected.has(x) && !state.libOverlay.has(x) && x !== inViewer);
        if (toMove.length) { toMove.forEach((x) => state.pendingMove.delete(x)); flushPendingMoves(toMove); }
        scheduleTagsRefresh();
      } catch (e) {
        toast(e.message, "error");
        break;
      }
    } while (state.libSaveAgain);
  } finally {
    state.libSaveBusy = false;
    clearTimeout(state.libSaveTimer);
  }
}

function applyMovedCard(oldRel, f) {
  // 文件被移动后就地更新卡片：不整页重载，避免打标/归位时网格跳动、侧栏关闭
  if (!f || f.rel === oldRel) return;
  const idx = state.files.findIndex((x) => x.rel === oldRel);
  if (idx >= 0) state.files[idx] = f;
  const card = document.querySelector(`.card[data-rel="${CSS.escape(oldRel)}"]`);
  if (card) {
    card.dataset.rel = f.rel;
    const img = card.querySelector(".thumb img");
    if (img) img.src = thumbUrl(f.rel, f.mtime || Date.now());
    const nameEl = card.querySelector(".card-name");
    if (nameEl) { nameEl.textContent = f.name; nameEl.title = f.rel; }
    const pathEl = card.querySelector(".card-path");
    if (pathEl) { pathEl.textContent = f.folder || ""; pathEl.title = f.folder || ""; }
    refreshCardBadges(f.rel);
  }
  if (state.selected.has(oldRel)) { state.selected.delete(oldRel); state.selected.add(f.rel); }
  syncCardClasses();
}

async function flushPendingMoves(rels) {
  const targets = [...(rels || [])].filter((rel) => state.pendingMove.has(rel) && !state.libOverlay.has(rel));
  if (!targets.length) return;
  targets.forEach((rel) => state.pendingMove.delete(rel));
  try {
    const r = await api("/api/reposition_batch", { method: "POST", body: JSON.stringify({ rels: targets }) });
    let movedCount = 0;
    for (const res of (r.results || [])) {
      if (!res.file) continue;
      if (res.file.rel === res.rel) {
        const idx = state.files.findIndex((x) => x.rel === res.rel);
        if (idx >= 0) state.files[idx] = res.file;
        refreshCardBadges(res.rel);
      } else if (res.moved) {
        applyMovedCard(res.rel, res.file);
        movedCount++;
      }
    }
    if (movedCount) toast(`已自动归位 ${movedCount} 个文件`);
    scheduleTagsRefresh();
  } catch (e) {
    // 失败放回待归位列表，下次取消选中/打标保存时自动重试
    targets.forEach((rel) => state.pendingMove.add(rel));
    toast("自动归位失败：" + e.message, "error");
  }
}

function flushPendingAll(rels) {
  flushPendingInbox(rels);
  flushPendingMoves(rels);
}

function inlineRename(el, rel) {
  const f = state.files.find((x) => x.rel === rel) || state.inboxFiles.find((x) => x.rel === rel);
  if (!f || el.dataset.editing) return;
  const oldRel = f.rel;
  const tag = el.tagName, cls = el.className, id = el.id || "";
  const input = document.createElement("input");
  input.type = "text";
  input.className = "rename-inline";
  input.value = f.name;
  el.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    const name = input.value.trim();
    let savedRel = null, savedName = null;
    if (save && name && name !== f.name) {
      try {
        const r = await api("/api/rename", { method: "POST", body: JSON.stringify({ rel: f.rel, name }) });
        toast("已重命名");
        savedRel = r.rel; savedName = r.name;
        const i = state.files.findIndex((x) => x.rel === f.rel);
        if (i >= 0) { state.files[i].rel = r.rel; state.files[i].name = r.name; }
        const j = state.inboxFiles.findIndex((x) => x.rel === f.rel);
        if (j >= 0) { state.inboxFiles[j].rel = r.rel; state.inboxFiles[j].name = r.name; }
        if (state.pendingTags[f.rel]) { state.pendingTags[r.rel] = state.pendingTags[f.rel]; delete state.pendingTags[f.rel]; }
        if (state.libOverlay.has(f.rel)) { state.libOverlay.set(r.rel, state.libOverlay.get(f.rel)); state.libOverlay.delete(f.rel); }
        if (state.pendingMove.has(f.rel)) { state.pendingMove.delete(f.rel); state.pendingMove.add(r.rel); }
        if (state.selected.has(f.rel)) { state.selected.delete(f.rel); state.selected.add(r.rel); }
        if (state.current && state.current.rel === f.rel) { state.current.rel = r.rel; state.current.name = r.name; }
      } catch (e) { toast(e.message, "error"); }
    }
    const relNow = savedRel || f.rel;
    const nameNow = savedName || f.name;
    {
      const card = [...document.querySelectorAll(".card[data-rel]")].find((c) => c.dataset.rel === oldRel);
      if (card) {
        const nf = effectiveFile(state.files.find((x) => x.rel === relNow) || { ...f, rel: relNow, name: nameNow });
        const div = document.createElement("div");
        div.innerHTML = cardHTML(nf);
        const nc = div.firstElementChild;
        bindCard(nc, nf);
        if (state.selected.has(nf.rel)) nc.classList.add("selected");
        card.replaceWith(nc);
      }
    }
    if (cls !== "card-name") {
      const parent = input.parentElement;
      if (parent) {
        const back = document.createElement(tag);
        if (id) back.id = id;
        if (cls) back.className = cls;
        back.textContent = nameNow;
        parent.replaceChild(back, input);
      }
    }
    if (state.current && state.current.rel === relNow) {
      if (id !== "insp-name") $("insp-name").textContent = nameNow;
      if (id !== "vw-name") $("vw-name").textContent = nameNow;
      $("insp-img").src = fileUrl(relNow, (state.current && state.current.mtime) || Date.now());
      $("vw-img").src = fileUrl(relNow, (state.current && state.current.mtime) || Date.now());
    }
    if (state.view === "inbox") refreshInbox();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    else if (e.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

function refreshCardBadges(rel) {
  const base = state.files.find((x) => x.rel === rel);
  const f = effectiveFile(base);
  if (!f) return;
  const badges = [];
  if ((f.specials || []).length) badges.push(`<i class="bd bd-sp">${esc(f.specials.join("/"))}</i>`);
  if (f.artist) badges.push(`<i class="bd bd-ar">${esc(f.artist)}</i>`);
  document.querySelectorAll(".card[data-rel]").forEach((card) => {
    if (card.dataset.rel === rel) {
      const box = card.querySelector(".badges");
      if (box) box.innerHTML = badges.join("");
    }
  });
}

/* ================= 全屏查看器 ================= */
function openViewer(f) {
  state.current = f;
  state.lastFile = f;
  state.viewerIdx = state.files.findIndex((x) => x.rel === f.rel);
  $("vw-img").src = fileUrl(f.rel, f.mtime);
  $("vw-name").textContent = f.name;
  $("vw-path").textContent = f.folder || "未处理";
  refreshTagEditors();
  loadSuggestions(f.rel, "vw");
  let vmeta =
    `<b>类型</b><span>${typeOf(f.name)}</span>` +
    `<b>大小</b><span>${fmtSize(f.size)}</span>` +
    `<b>创建</b><span>${f.ctime ? new Date(f.ctime * 1000).toLocaleString() : "-"}</span>` +
    `<b>修改</b><span>${f.mtime ? new Date(f.mtime * 1000).toLocaleString() : "-"}</span>` +
    `<b>画师原值</b><span>${esc(f.artist_raw || "-")}</span>`;
  if (f.artist && state.tags.artist_aliases && state.tags.artist_aliases[f.artist]) {
    vmeta += `<b>画师别名</b><span>${esc(state.tags.artist_aliases[f.artist].join("、"))}</span>`;
  }
  $("vw-meta").innerHTML = vmeta;
  $("viewer").hidden = false;
}

function closeViewer() {
  if (state.current && state.pendingMove.has(state.current.rel)) flushPendingMoves([state.current.rel]);
  $("viewer").hidden = true; state.current = null;
}

function vwNav(dir) {
  if (state.viewerIdx < 0) return;
  const idx = state.viewerIdx + dir;
  if (idx < 0 || idx >= state.files.length) {
    if (!state.done && !state.loading) loadFiles(false);
    return;
  }
  openViewer(state.files[idx]);
}

/* ================= 重命名 ================= */
function openRename(f) {
  state.current = f;
  $("rename-path").textContent = f.rel;
  $("rename-name").value = f.name;
  $("modal-rename").hidden = false;
  setTimeout(() => $("rename-name").select(), 30);
}

async function doRename() {
  const f = state.current;
  const name = $("rename-name").value.trim();
  if (!name) return toast("文件名不能为空", "error");
  try {
    const r = await api("/api/rename", { method: "POST", body: JSON.stringify({ rel: f.rel, name }) });
    $("modal-rename").hidden = true;
    toast("已重命名");
    if (state.view === "inbox" || state.view === "trash") { refreshInbox(); refreshTrash(); return; }
    loadFiles(true); scheduleTagsRefresh();
    if (!$("viewer").hidden || !$("inspector").hidden) {
      f.rel = r.rel; f.name = r.name;
      if (!$("viewer").hidden) openViewer(f); else openInspector(f);
    }
  } catch (e) { toast(e.message, "error"); }
}

/* ================= 废纸篓 ================= */
async function doReveal(f) {
  if (!f) return;
  try {
    const r = await api("/api/reveal", { method: "POST", body: JSON.stringify({ rel: f.rel }) });
    if (r.mode === "trash") toast("该文件已进废纸篓，已在资源管理器中定位废纸篓里的那份");
    else if (r.mode === "folder") toast("文件已不存在，已打开它原来的文件夹");
    else toast("已在资源管理器中定位该文件");
  } catch (e) { toast(e.message, "error"); }
}

async function doTrash(f) {
  if (!confirm(`确定将「${f.name}」移入废纸篓？`)) return;
  try {
    await api("/api/trash", { method: "POST", body: JSON.stringify({ rel: f.rel }) });
    toast("已移入废纸篓");
    state.selected.delete(f.rel);
    delete state.pendingTags[f.rel];
    state.placingInbox.delete(f.rel);
    state.libOverlay.delete(f.rel);
    state.pendingMove.delete(f.rel);
    closeViewer(); closeInspector();
    updateBatchBar();
    scheduleTagsRefresh();
    if (state.view === "inbox") refreshInbox(); else loadFiles(true);
  } catch (e) { toast(e.message, "error"); }
}

async function renderTrash() {
  $("grid").innerHTML = "";
  $("sentinel").hidden = true;
  try {
    const data = await api("/api/trash");
    state.trashFiles = data.files;
    $("result-count").textContent = `废纸篓 · ${data.files.length} 项 · 超过 30 天自动移入系统回收站`;
    $("nav-trash").textContent = data.files.length ? fmtCount(data.files.length) : "";
    $("btn-trash-clear").disabled = !data.files.length;
    const grid = $("grid");
    grid.style.gridTemplateColumns = "1fr";
    grid.innerHTML = data.files.length ? data.files.map((f) => `
      <div class="trash-row" data-rel="${esc(f.rel)}">
        <span class="trash-check">${ICONS.check}</span>
        <img loading="lazy" src="${thumbUrl(f.rel, f.mtime)}" alt="">
        <div class="trash-info">
          <div class="trash-name">${esc(f.name)}</div>
          <div class="trash-path">${esc(f.rel)}</div>
        </div>
        <div class="trash-time">${f.trashed_at ? new Date(f.trashed_at * 1000).toLocaleString() : "-"}</div>
        <button class="btn ghost small restore-btn" data-rel="${esc(f.rel)}">${ICONS.restore}恢复</button>
      </div>`).join("") : `<div class="empty" style="grid-column:1/-1">废纸篓是空的</div>`;
    grid.querySelectorAll(".trash-row").forEach((row) => {
      if (state.selected.has(row.dataset.rel)) row.classList.add("selected");
      row.addEventListener("click", (e) => {
        if (e.target.closest(".restore-btn")) return;
        toggleTrashSelect(row.dataset.rel, row);
      });
    });
    grid.querySelectorAll(".restore-btn").forEach((b) => b.addEventListener("click", async () => {
      try {
        await api("/api/restore", { method: "POST", body: JSON.stringify({ rel: b.dataset.rel }) });
        toast("已恢复");
        state.selected.delete(b.dataset.rel);
        updateBatchBar();
        renderTrash(); scheduleTagsRefresh();
      } catch (e) { toast(e.message, "error"); }
    }));
    $("empty").hidden = true;
  } catch (e) { toast(e.message, "error"); }
}

function refreshTrash() { if (state.view === "trash") renderTrash(); }

async function trashBatch() {
  const rels = [...state.selected];
  if (!rels.length || !confirm(`确定将选中的 ${rels.length} 个文件移入废纸篓？`)) return;
  try {
    const r = await api("/api/trash_batch", { method: "POST", body: JSON.stringify({ rels }) });
    toast(`已移入 ${r.moved} 个文件`);
    rels.forEach((rel) => { delete state.pendingTags[rel]; state.placingInbox.delete(rel); state.libOverlay.delete(rel); state.pendingMove.delete(rel); });
    clearSelection(true); updateBatchBar();
    scheduleTagsRefresh();
    if (state.view === "inbox") refreshInbox(); else loadFiles(true);
  } catch (e) { toast(e.message, "error"); }
}

async function restoreBatch() {
  const rels = [...state.selected];
  if (!rels.length || !confirm(`确定恢复选中的 ${rels.length} 个文件？`)) return;
  try {
    const r = await api("/api/restore_batch", { method: "POST", body: JSON.stringify({ rels }) });
    toast(`已恢复 ${r.restored} 个文件${r.failed ? `（${r.failed} 个失败）` : ""}`);
    clearSelection(true); updateBatchBar();
    renderTrash(); scheduleTagsRefresh();
  } catch (e) { toast(e.message, "error"); }
}

/* ================= 标签弹窗 ================= */
function openTagsModal(mode, file) {
  state.modalMode = mode;
  state.current = file || state.current;
  const title = mode === "batch" ? `批量打标（${state.selected.size} 项）` : mode === "place" ? "打标并归位" : "编辑标签";
  $("tags-title").textContent = title;
  const hint = $("tags-hint");
  if (mode === "batch") { hint.hidden = false; hint.textContent = "以下标签将追加到所有选中文件；未处理文件请在「未处理」里逐个归位。"; }
  else if (mode === "place") { hint.hidden = false; hint.textContent = "选择角色后将移动/复制到对应学院/角色文件夹，并从「未处理」移除。可多选角色（将复制多份）。"; }
  else hint.hidden = true;
  const f = state.current;
  $("tags-file").textContent = f ? f.rel : "";
  $("tags-char-q").value = "";

  const charBox = $("tags-chars");
  charBox.innerHTML = "";
  const cur = mode === "single" && f ? (f.characters || []) : [];
  const entries = Object.entries(state.tags.characters).sort((a, b) => b[1] - a[1]);
  for (const [name] of entries) {
    const b = document.createElement("button");
    b.className = "tchip" + (cur.includes(name) ? " on" : "");
    b.textContent = name;
    b.dataset.name = name;
    b.addEventListener("click", () => b.classList.toggle("on"));
    charBox.appendChild(b);
  }
  filterCharPicker();

  $("tags-artist").value = mode === "single" && f ? (f.artist || "") : "";
  const dl = $("artist-list");
  dl.innerHTML = Object.keys(state.tags.artists).map((a) => `<option value="${esc(a)}">`).join("");

  const spBox = $("tags-specials");
  spBox.innerHTML = "";
  const known = new Set(Object.keys(state.tags.specials));
  ["差分", "铸币大头", "圣诞特供", "AI"].forEach((s) => {
    const b = document.createElement("button");
    b.className = "tchip" + (mode === "single" && f && (f.specials || []).includes(s) ? " on" : "");
    b.textContent = known.has(s) ? s : s + "（新建）";
    b.dataset.name = s;
    b.addEventListener("click", () => b.classList.toggle("on"));
    spBox.appendChild(b);
  });
  $("modal-tags").hidden = false;
}

function filterCharPicker() {
  const q = $("tags-char-q").value.trim();
  $("tags-chars").querySelectorAll(".tchip").forEach((b) => {
    b.style.display = !q || tagMatch(b.dataset.name, q) ? "" : "none";
  });
}

function collectTagModal() {
  const chars = [...$("tags-chars").querySelectorAll(".tchip.on")].map((b) => b.dataset.name);
  const specials = [...$("tags-specials").querySelectorAll(".tchip.on")].map((b) => b.dataset.name);
  const artist = $("tags-artist").value.trim();
  return { chars, specials, artist };
}

async function doSaveTags() {
  const { chars, specials, artist } = collectTagModal();
  try {
    if (state.modalMode === "batch") {
      const rels = [...state.selected].filter((r) => !isInboxRel(r));
      if (!rels.length) { toast("请先勾选要打标的库内文件", "warn"); return; }
      const r = await api("/api/tags_batch", { method: "POST", body: JSON.stringify({ rels, characters: chars, specials, artist, no_move: true }) });
      toast(`批量打标完成（${r.updated || rels.length} 个）`);
      const byRel = new Map((r.files || []).map((x) => [x.rel, x]));
      rels.forEach((rel) => {
        const rf = byRel.get(rel);
        if (!rf) return;
        const idx = state.files.findIndex((x) => x.rel === rel);
        if (idx >= 0) state.files[idx] = rf;
        refreshCardBadges(rf.rel);
        state.pendingMove.add(rf.rel);
      });
      if (state.current) {
        const ci = state.files.findIndex((x) => x.rel === state.current.rel);
        if (ci >= 0) { state.current = state.files[ci]; refreshTagEditors(); }
      }
      $("modal-tags").hidden = true;
      clearSelection(false); // 触发取消选中 → 统一批量归位
      scheduleTagsRefresh();
      debouncedSearch(); // 让结果列表反映最新标签（不再整页重置）
      return;
    } else if (state.modalMode === "place") {
      if (!chars.length) return toast("请至少选择一个角色", "warn");
      const r = await api("/api/place", { method: "POST", body: JSON.stringify({ rel: state.current.rel, characters: chars, specials, artist }) });
      toast(`已归位 ${r.placed.length} 个文件`);
      state.selected.delete(state.current.rel);
      delete state.pendingTags[state.current.rel];
      state.placingInbox.delete(state.current.rel);
      updateBatchBar();
    } else {
      const r = await api("/api/tags", { method: "POST", body: JSON.stringify({ rel: state.current.rel, characters: chars, specials, artist }) });
      toast("标签已保存");
      if (r.file) {
        state.current = r.file;
        if (!$("viewer").hidden) openViewer(r.file); else if (!$("inspector").hidden) openInspector(r.file);
      }
    }
    $("modal-tags").hidden = true;
    scheduleTagsRefresh();
    if (state.view === "inbox") refreshInbox(); else debouncedSearch();
  } catch (e) { toast(e.message, "error"); }
}

/* ================= 未处理 ================= */
async function refreshInbox() {
  try {
    const data = await api("/api/inbox");
    state.inboxFiles = data.files;
    $("nav-inbox").textContent = data.files.length ? fmtCount(data.files.length) : "";
    if (state.view === "inbox") renderInbox();
  } catch (e) { /* 静默 */ }
}

function renderInbox() {
  $("grid").innerHTML = "";
  $("sentinel").hidden = true;
  $("result-count").textContent = "未处理";
  $("grid").style.gridTemplateColumns = "";
  const grid = $("grid");
  const list = state.inboxFiles;
  if (!list.length) {
    grid.innerHTML = `<div class="empty" style="grid-column:1/-1">「未处理」是空的。<br>把新图片拖进页面、或点右上角「上传到未处理」，再在这里侧栏打标，取消选中即自动归位。</div>`;
    $("empty").hidden = true;
    return;
  }
  const frag = document.createDocumentFragment();
  for (const f of list) {
    const ef = effectiveFile(f);
    const badges = [];
    for (const c of ef.characters) badges.push(`<i class="bd bd-ch">${esc(c)}</i>`);
    for (const s of ef.specials) badges.push(`<i class="bd bd-sp">${esc(s)}</i>`);
    if (ef.artist) badges.push(`<i class="bd bd-ar">${esc(ef.artist)}</i>`);
    const div = document.createElement("div");
    div.innerHTML = `
    <div class="card inbox-card" data-rel="${esc(f.rel)}">
      <div class="thumb">
        <img loading="lazy" src="${thumbUrl(f.rel, f.mtime)}" alt="">
        <span class="bd-type ${typeCls(f.name)}">${typeOf(f.name)}</span>
        <div class="check">${ICONS.check}</div>
        <div class="badges">${badges.join("")}</div>
        <div class="hover-actions">
          <button class="hact" data-act="view" title="全屏预览">${ICONS.eye}</button>
          <button class="hact danger" data-act="trash" title="移入废纸篓">${ICONS.trash}</button>
        </div>
      </div>
      <div class="card-info">
        <div class="card-name" title="${esc(f.rel)}">${esc(f.name)}</div>
        <div class="card-path" title="未处理">未处理</div>
        <div class="card-meta">${fmtSize(f.size)} · ${typeOf(f.name)}</div>
      </div>
    </div>`;
    const card = div.firstElementChild;
    bindInboxCard(card, f);
    if (state.selected.has(f.rel)) card.classList.add("selected");
    frag.appendChild(card);
  }
  grid.appendChild(frag);
  $("empty").hidden = true;
}

function bindInboxCard(card, f) {
  card.addEventListener("click", (e) => {
    if (e.target.closest(".hact")) return;
    if (e.target.closest(".check")) { toggleSelect(f.rel, card); return; }
    if (e.ctrlKey || e.metaKey) { toggleSelect(f.rel, card); return; }
    selectOne(f.rel, card);
  });
  card.addEventListener("dblclick", (e) => { if (!e.target.closest(".card-name")) openViewer(f); });
  const cardName = card.querySelector(".card-name");
  if (cardName) cardName.addEventListener("dblclick", (e) => { e.stopPropagation(); inlineRename(cardName, f.rel); });
  card.querySelector('[data-act="view"]').addEventListener("click", (e) => { e.stopPropagation(); openViewer(f); });
  card.querySelector('[data-act="trash"]').addEventListener("click", (e) => { e.stopPropagation(); doTrash(f); });
}

/* ================= 上传 ================= */
const IMG_DROP_RE = /\.(jpe?g|png|gif|webp)$/i;
function fileFromEntry(entry) {
  return new Promise((resolve) => {
    try {
      if (entry.isFile) entry.file(resolve, () => resolve(null));
      else resolve(null);
    } catch (e) { resolve(null); }
  });
}
function walkDirEntry(entry) {
  return new Promise((resolve) => {
    const out = [];
    let reader = null;
    try { reader = entry.createReader(); } catch (e) { resolve(out); return; }
    const readBatch = () => {
      try {
        reader.readEntries(async (entries) => {
          if (!entries || !entries.length) { resolve(out); return; }
          const parts = await Promise.all(entries.map((e) => e.isDirectory ? walkDirEntry(e) : (IMG_DROP_RE.test(e.name) ? fileFromEntry(e) : Promise.resolve(null))));
          for (const p of parts.flat(Infinity)) if (p) out.push(p);
          readBatch();
        }, () => resolve(out));
      } catch (e) { resolve(out); }
    };
    readBatch();
  });
}
async function collectDropFiles(dt) {
  const out = [];
  const items = dt && dt.items ? Array.from(dt.items) : [];
  if (items.length) {
    // DataTransferItem 在 drop 事件结束后即失效，须在首个 await 前同步取出全部 entry/文件
    const entries = [];
    for (const it of items) {
      let entry = null;
      try { entry = it.webkitGetAsEntry ? it.webkitGetAsEntry() : null; } catch (e) { entry = null; }
      if (!entry) {
        try {
          const f = it.getAsFile ? it.getAsFile() : null;
          if (f && IMG_DROP_RE.test(f.name)) out.push(f);
        } catch (e) { /* 单项失败不影响其他 */ }
      } else {
        entries.push(entry);
      }
    }
    for (const entry of entries) {
      try {
        if (entry.isDirectory) out.push(...(await walkDirEntry(entry)));
        else if (entry.isFile && IMG_DROP_RE.test(entry.name)) {
          const f = await fileFromEntry(entry);
          if (f) out.push(f);
        }
      } catch (e) { /* 单项失败不影响其他 */ }
    }
  } else {
    for (const f of (dt && dt.files ? Array.from(dt.files) : [])) {
      if (IMG_DROP_RE.test(f.name)) out.push(f);
    }
  }
  return out;
}
async function handleDrop(dt) {
  try {
    const files = await collectDropFiles(dt);
    if (!files.length) { toast("未找到可上传的图片（支持拖入文件夹，自动递归搜索）", "warn"); return; }
    await doUpload(files);
  } catch (e) { toast("上传失败：" + (e.message || e), "error"); }
}

function openUpload() { $("modal-upload").hidden = false; $("upload-list").textContent = ""; }

async function doUpload(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  try {
    const r = await api("/api/upload", { method: "POST", body: fd });
    $("upload-list").textContent = `已上传 ${r.saved.length} 个文件 → 未处理`;
    toast(`已上传 ${r.saved.length} 个文件`);
    refreshInbox();
    if (state.view !== "inbox") setView("inbox");
  } catch (e) { toast(e.message, "error"); }
}

/* ================= 整理预览 ================= */
async function openOrganize() {
  $("drawer-title").textContent = "整理预览";
  let cfg0 = {};
  try { cfg0 = await api("/api/config"); } catch (e) { /* 忽略 */ }
  const gv = (k, d) => (cfg0[k] != null ? cfg0[k] : d);
  const mb = gv("artist_weight_gif_large_mb", "?");
  const w1 = gv("artist_weight_gif", "?");
  const w2 = gv("artist_weight_gif_large", "?");
  $("drawer-body").innerHTML = `<div class="drawer-hint">规则：按文件加权数判定（普通 ×1、gif ≤${mb}M ×${w1}、gif &gt;${mb}M ×${w2}）≥ 阈值时创建/并入画师文件夹，低于阈值则解散；解散时文件名不含画师名/别名的补成「画师主名-原文件名」，不解散不改名；未知/杂烩文件夹低于阈值时解散且不改名；桃绿/光望成员池按「自身 + 组合池」合并计算阈值与画师权重，解散判定取文件夹权重与合并权重的较大值；组合池只有当该画师与任一成员池的累计加权都低于阈值时才解散；画师文件夹中画师标签与文件夹不符的文件会在打标时自动移出，不再进入本预览；池根目录中带画师标签且已有该画师文件夹的文件会在扫描/导入后自动归位。</div><div class="muted">计算中…</div>`;
  $("drawer").hidden = false;
  state.orgSel.clear();
  try {
    const data = await api("/api/organize");
    const s = data.suggestions;
    const cfg = data.config;
    state.orgItemMap = {};
    s.create.forEach((it) => state.orgItemMap[it.id] = Object.assign({}, it, { type: "create" }));
    s.remove.forEach((it) => state.orgItemMap[it.id] = Object.assign({}, it, { type: "remove" }));
    let html = `<div class="drawer-hint">阈值：加权 &lt; ${cfg.artist_threshold_split1} → ${cfg.artist_threshold_min}；${cfg.artist_threshold_split1}–${cfg.artist_threshold_split2} → ${cfg.artist_threshold_mid1}；${cfg.artist_threshold_split2}–${cfg.artist_threshold_split3} → ${cfg.artist_threshold_mid}；≥ ${cfg.artist_threshold_split3} → ${cfg.artist_threshold_mid2}。权重：gif ≤${cfg.artist_weight_gif_large_mb}M ×${cfg.artist_weight_gif}，gif &gt;${cfg.artist_weight_gif_large_mb}M ×${cfg.artist_weight_gif_large}。角色文件夹加权总数 = 零散文件实际权重 + 每个画师文件夹按 min(0.4×权重, ${cfg.artist_folder_weight_cap}) 计。</div>`;
    html += `<div class="drawer-actions"><button id="btn-org-apply" class="btn primary small">执行选中整理</button><button id="btn-org-all" class="btn small">全选</button><button id="btn-org-none" class="btn small">清空</button><span class="muted" id="org-sel-count">已选 0 项</span></div>`;
    const sNew = s.create.filter(i => !i.existing);
    const sMerge = s.create.filter(i => i.existing);
    html += `<h4>新建画师文件夹（${sNew.length}）</h4>` + orgTable(sNew, [["角色", "character"], ["画师", "artist"], ["池", "pool"], ["加权", "weight"], ["阈值", "threshold"], ["文件", "count"], ["需改名", "rename_n"]]);
    html += `<h4>并入已有画师文件夹（${sMerge.length}）</h4>` + orgTable(sMerge, [["角色", "character"], ["画师", "artist"], ["目标文件夹", "folder"], ["加权", "weight"], ["阈值", "threshold"], ["文件", "count"], ["需改名", "rename_n"]]);
    html += `<h4>解散画师文件夹（${s.remove.length}）</h4>` + orgTable(s.remove, [["角色", "character"], ["文件夹", "folder"], ["加权", "weight"], ["阈值", "threshold"], ["需改名", "rename_n"], ["原因", "reason"]]);

    $("drawer-body").innerHTML = html;
    bindOrgChecks();
    const updateCount = () => { $("org-sel-count").textContent = `已选 ${state.orgSel.size} 项`; };
    $("btn-org-all").addEventListener("click", () => {
      document.querySelectorAll("#drawer-body input[data-id]").forEach(cb => { cb.checked = true; state.orgSel.add(cb.dataset.id); });
      updateCount();
    });
    $("btn-org-none").addEventListener("click", () => {
      document.querySelectorAll("#drawer-body input[data-id]").forEach(cb => { cb.checked = false; state.orgSel.clear(); });
      updateCount();
    });
    $("btn-org-apply").addEventListener("click", async () => {
      if (!state.orgSel.size) { toast("请先勾选要执行的项（可用全选）", "error"); return; }
      if (!confirm(`确定执行选中的 ${state.orgSel.size} 项整理？`)) return;
      try {
        const items = [...state.orgSel].map((id) => state.orgItemMap[id]).filter(Boolean);
        const r = await api("/api/organize/apply", { method: "POST", body: JSON.stringify({ items }) });
        const btn = $("btn-org-apply");
        btn.disabled = true;
        btn.textContent = "整理执行中…";
        toast("整理已在后台执行…");
        const timer = setInterval(async () => {
          let st;
          try { st = await api("/api/organize/apply/status"); }
          catch (e2) { clearInterval(timer); btn.disabled = false; btn.textContent = "执行选中整理"; toast(e2.message, "error"); return; }
          const prog = st.progress || {};
          const cnt = $("org-sel-count");
          if (cnt) cnt.textContent = `${prog.stage || "整理中"} ${prog.done || 0}/${prog.total || "?"}`;
          if (st.running) return;
          clearInterval(timer);
          btn.disabled = false;
          btn.textContent = "执行选中整理";
          if (st.error) { toast("整理出错：" + st.error, "error"); return; }
          const r2 = st.result || { stats: {} };
          toast(`整理完成：创建 ${r2.stats.create} 个文件夹（移入 ${r2.stats.move_in} 个文件），解散 ${r2.stats.remove} 个（移出 ${r2.stats.dissolve_out} 个文件），重命名 ${r2.stats.rename} 个，重排序号 ${r2.stats.renumber || 0} 个文件夹` + (r2.stats.renumber_failed ? `；${r2.stats.renumber_failed} 个角色池序号重排失败（文件夹被占用），稍后再跑一次整理即可` : ""));
          scheduleTagsRefresh();
          if (state.view === "all" || state.view === "untagged") loadFiles(true);
          openOrganize();
        }, 800);
      } catch (e) { toast(e.message, "error"); }
    });
  } catch (e) { toast(e.message, "error"); }
}

function bindOrgChecks() {
  if (window.__orgChecksBound) return;
  window.__orgChecksBound = true;
  document.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t.matches("#drawer-body input[data-id]")) {
      if (t.checked) state.orgSel.add(t.dataset.id); else state.orgSel.delete(t.dataset.id);
      const table = t.closest("table");
      const cbs = [...table.querySelectorAll("input[data-id]")];
      const all = cbs.length && cbs.every(cb => cb.checked);
      const head = table.querySelector("input[data-selall]");
      if (head) head.checked = all;
      const c = $("org-sel-count"); if (c) c.textContent = `已选 ${state.orgSel.size} 项`;
    } else if (t.matches("#drawer-body input[data-selall]")) {
      const table = t.closest("table");
      table.querySelectorAll("input[data-id]").forEach(cb => { cb.checked = t.checked; if (t.checked) state.orgSel.add(cb.dataset.id); else state.orgSel.delete(cb.dataset.id); });
      const c = $("org-sel-count"); if (c) c.textContent = `已选 ${state.orgSel.size} 项`;
    }
  });
}

function orgTable(list, cols) {
  if (!list.length) return `<div class="empty">无</div>`;
  const rows = list.slice(0, 200).map((r) => `<tr><td class="org-check"><input type="checkbox" data-id="${esc(r.id)}" ${state.orgSel.has(r.id) ? "checked" : ""}></td>${cols.map(([, k]) => `<td>${esc(String(r[k]))}</td>`).join("")}</tr>`).join("");
  return `<table class="org-table"><thead><tr><th class="org-check"><input type="checkbox" data-selall=""></th>${cols.map(([h]) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table>`;
}

/* ================= 扫描 ================= */
async function doScan() {
  try {
    await api("/api/scan", { method: "POST" });
    $("scanbar").hidden = false;
    pollScan();
  } catch (e) { toast(e.message, "error"); }
}

const SCAN_PHASE_LABEL = { count: "统计文件", scan: "扫描文件", write: "写入数据库", move: "自动归位", done: "完成" };

function renderScanProgress(p, running) {
  p = p || {};
  const phase = p.phase || (running ? "scan" : "done");
  const fill = $("scan-fill");
  let pct = null, text = "";
  if (phase === "scan") {
    const total = p.est_total || p.total || 0;
    const done = p.done || p.total || 0;
    pct = total ? Math.min(100, Math.round((done / total) * 100)) : null;
    text = total ? `${fmtCount(done)} / ${fmtCount(total)} · ${pct}%` : (done ? `${fmtCount(done)} 个` : "");
  } else if (phase === "write") {
    const total = p.write_total || 0;
    const done = p.done || 0;
    pct = total ? Math.min(100, Math.round((done / total) * 100)) : null;
    text = total ? `${fmtCount(done)} / ${fmtCount(total)} · ${pct}%` : "";
  } else if (phase === "move") {
    const total = p.move_total || 0;
    const done = p.done || 0;
    pct = total ? Math.min(100, Math.round((done / total) * 100)) : null;
    text = total ? `${fmtCount(done)} / ${fmtCount(total)} · ${pct}%` : "";
  } else if (phase === "done") {
    pct = 100;
    text = `${fmtCount(p.total || 0)} 个${p.moved ? ` · 归位 ${p.moved}` : ""} · ${p.elapsed || 0}s`;
  }
  $("scan-label").textContent = (SCAN_PHASE_LABEL[phase] || "扫描") + "…";
  $("scan-progress").textContent = text;
  if (pct == null) { fill.classList.add("indet"); fill.style.width = "100%"; }
  else { fill.classList.remove("indet"); fill.style.width = pct + "%"; }
}

function pollScan() {
  const timer = setInterval(async () => {
    try {
      const s = await api("/api/scan/status");
      renderScanProgress(s.progress, s.running);
      if (!s.running) {
        clearInterval(timer);
        const p = s.progress || {};
        setTimeout(() => { $("scanbar").hidden = true; }, p.error ? 0 : 500);
        if (p.error) toast("扫描出错：" + p.error, "error");
        else { toast(`扫描完成：${p.total} 个文件${p.moved ? `，自动归位 ${p.moved} 个` : ""}（${p.elapsed}s）`); scheduleTagsRefresh(); loadFiles(true); }
      }
    } catch (e) { clearInterval(timer); $("scanbar").hidden = true; }
  }, 500);
}

/* ================= 事件绑定 ================= */
function bindEvents() {
  $("q").addEventListener("input", () => {
    state.filters.q = $("q").value.trim();
    qSuggestGroups();
    debouncedSearch();
  });
  $("q").addEventListener("keydown", (e) => {
    const box = $("q-suggest");
    if (e.key === "Escape") { box.hidden = true; qSug.items = []; qSug.hi = -1; return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (box.hidden || !qSug.items.length) qSuggestGroups();
      const n = qSug.items.length;
      if (!n) return;
      e.preventDefault();
      const cur = qSug.hi;
      if (e.key === "ArrowDown") qSugSetHi(cur < 0 ? 0 : cur + 1);
      else qSugSetHi(cur < 0 ? n - 1 : cur - 1);
      return;
    }
    if (e.key === "Enter" && !box.hidden && qSug.items.length) {
      e.preventDefault();
      let idx = qSug.hi;
      if (idx < 0) {
        const ql = normKey($("q").value);
        const exact = qSug.items.findIndex((it) => normKey(it.name) === ql);
        idx = exact >= 0 ? exact : 0;
      }
      qSugChoose(idx);
    }
  });
  document.addEventListener("click", (e) => {
    const box = $("q-suggest");
    if (!box.hidden && !box.contains(e.target) && e.target !== $("q")) box.hidden = true;
  });
  document.addEventListener("click", (e) => {
    if (ftrState.open && !e.target.closest(".ftr")) { ftrState.open = null; renderFilterToolbar(); }
  });
  document.addEventListener("click", (e) => {
    document.querySelectorAll(".tag-edit-sg, .alias-sg").forEach((sg) => {
      if (!sg.hidden && !sg.contains(e.target) && !sg.parentElement.contains(e.target)) sg.hidden = true;
    });
  });
  $("f-untagged").addEventListener("change", () => {
    state.filters.untagged = $("f-untagged").checked;
    loadFiles(true);
  });
  function renderSortDir() {
    const b = $("sort-dir");
    b.textContent = state.sortDir === "desc" ? "↓" : "↑";
    b.title = state.sortDir === "desc" ? "降序（点击切换升序）" : "升序（点击切换降序）";
    b.classList.toggle("on", state.sortDir === "desc");
  }
  renderSortDir();
  $("sort").addEventListener("change", () => {
    state.sort = $("sort").value;
    loadFiles(true);
  });
  $("sort-dir").addEventListener("click", () => {
    state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
    renderSortDir();
    loadFiles(true);
  });
  $("btn-select-all").addEventListener("click", () => {
    let list = [];
    if (state.view === "all" || state.view === "untagged") list = state.files;
    else if (state.view === "inbox") list = state.inboxFiles;
    else if (state.view === "trash") list = state.trashFiles;
    if (!list.length) { toast("暂无已加载文件", "warn"); return; }
    const allLoaded = list.every((f) => state.selected.has(f.rel));
    if (allLoaded) { clearSelection(true); toast("已清空选择"); return; }
    list.forEach((f) => state.selected.add(f.rel));
    syncCardClasses();
    syncTrashRowClasses();
    updateBatchBar();
    if (state.view !== "trash" && list[0]) openInspector(list[0]);
    toast(`已全选当前 ${list.length} 个文件`);
  });
  $("btn-sb-collapse").addEventListener("click", () => {
    document.querySelector(".layout").classList.add("sb-collapsed");
    $("btn-sb-expand").hidden = false;
  });
  $("btn-sb-expand").addEventListener("click", () => {
    document.querySelector(".layout").classList.remove("sb-collapsed");
    $("btn-sb-expand").hidden = true;
  });
  $("btn-insp").addEventListener("click", () => {
    if (!$("inspector").hidden) { closeInspector(); }
    else if (state.lastFile) { openInspector(state.lastFile); }
    else { toast("请先在网格中选择一个文件", "warn"); }
  });
  $("btn-insp-expand").addEventListener("click", () => {
    if (state.lastFile) openInspector(state.lastFile);
  });
  document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => setView(b.dataset.nav)));
  initMarquee();
  document.querySelectorAll(".grp-head").forEach((h) => h.addEventListener("click", () => { const g = h.closest(".tag-group"); if (g) g.classList.toggle("collapsed"); }));
  $("btn-upload").addEventListener("click", openUpload);
  $("btn-scan").addEventListener("click", doScan);
  $("btn-organize").addEventListener("click", openOrganize);
  $("insp-close").addEventListener("click", closeInspector);
  $("inspector").addEventListener("dblclick", (e) => {
    if (e.target.id === "insp-name" && state.current) { e.stopPropagation(); inlineRename(e.target, state.current.rel); }
  });
  $("viewer").addEventListener("dblclick", (e) => {
    if (e.target.id === "vw-name" && state.current) { e.stopPropagation(); inlineRename(e.target, state.current.rel); }
  });
  $("btn-trash-file").addEventListener("click", () => doTrash(state.current));
  const btnPlace = $("btn-place-file");
  if (btnPlace) btnPlace.addEventListener("click", () => { if (state.current) openTagsModal("place", state.current); });
  $("btn-trash-file-vw").addEventListener("click", () => doTrash(state.current));
  $("btn-reveal-file").addEventListener("click", () => doReveal(state.current));
  $("btn-reveal-file-vw").addEventListener("click", () => doReveal(state.current));
  $("btn-trash-clear").addEventListener("click", async () => {
    if (!confirm(`确定清空废纸篓？共 ${state.trashFiles.length} 个文件将移入系统回收站（可撤销）。`)) return;
    try {
      const r = await api("/api/trash/clear", { method: "POST", body: JSON.stringify({}) });
      toast(`已清空 ${r.cleared} 个文件`);
      renderTrash(); scheduleTagsRefresh();
    } catch (e) { toast(e.message, "error"); }
  });
  $("btn-batch-tags").addEventListener("click", () => openTagsModal("batch", null));
  $("btn-batch-trash").addEventListener("click", trashBatch);
  $("btn-batch-restore").addEventListener("click", restoreBatch);
  $("btn-batch-clear").addEventListener("click", () => { clearSelection(true); syncCardClasses(); });
  $("tags-char-q").addEventListener("input", filterCharPicker);
  $("tags-char-q").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const q = $("tags-char-q").value.trim();
    const chips = [...$("tags-chars").querySelectorAll(".tchip")].filter((b) => b.style.display !== "none");
    let pick = null;
    if (chips.length) {
      const ql = q.toLowerCase();
      pick = chips.find((b) => b.dataset.name.toLowerCase() === ql) || chips[0];
    }
    if (pick) pick.click();
    $("tags-char-q").value = "";
  });
  $("tags-artist").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !$("modal-tags").hidden) { e.preventDefault(); doSaveTags(); }
  });
  $("tags-save").addEventListener("click", doSaveTags);
  $("tags-cancel").addEventListener("click", () => { $("modal-tags").hidden = true; });
  $("rename-save").addEventListener("click", doRename);
  $("rename-cancel").addEventListener("click", () => { $("modal-rename").hidden = true; });
  $("vw-close").addEventListener("click", closeViewer);
  $("vw-prev").addEventListener("click", () => vwNav(-1));
  $("vw-next").addEventListener("click", () => vwNav(1));
  $("viewer").addEventListener("click", (e) => { if (e.target === $("viewer") || e.target.classList.contains("vw-stage")) closeViewer(); });
  $("drawer-close").addEventListener("click", () => { $("drawer").hidden = true; });
  $("upload-close").addEventListener("click", () => { $("modal-upload").hidden = true; });
  $("btn-pick").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (e) => { doUpload(e.target.files); e.target.value = ""; });
  const dz = $("dropzone");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => handleDrop(e.dataTransfer));
  $("newtag-cancel").addEventListener("click", () => { $("modal-newtag").hidden = true; });
  $("newtag-ok").addEventListener("click", confirmNewTag);
  $("newtag-acad").addEventListener("change", () => {
    const sel = $("newtag-acad");
    const inp = $("newtag-acad-new");
    inp.hidden = sel.value !== "__new";
    if (sel.value === "__new") inp.focus();
  });

  // 全页拖拽：直接把文件拖到页面任意位置即可上传到「未处理」
  const dropOverlay = $("drop-overlay");
  let dragDepth = 0;
  const hasFileDrag = (e) => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files");
  const overDropzone = (e) => !!(e.target && e.target.closest && e.target.closest("#dropzone"));
  window.addEventListener("dragenter", (e) => {
    if (!hasFileDrag(e) || overDropzone(e)) return;
    e.preventDefault();
    dragDepth++;
    dropOverlay.hidden = false;
  });
  window.addEventListener("dragover", (e) => {
    if (!hasFileDrag(e) || overDropzone(e)) return;
    e.preventDefault();
  });
  window.addEventListener("dragleave", (e) => {
    if (!hasFileDrag(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dropOverlay.hidden = true;
  });
  window.addEventListener("drop", (e) => {
    if (!hasFileDrag(e)) return;
    e.preventDefault();
    dragDepth = 0;
    dropOverlay.hidden = true;
    if (overDropzone(e)) return;
    handleDrop(e.dataTransfer);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (!$("modal-newtag").hidden) { $("modal-newtag").hidden = true; return; }
      if (!$("modal-tags").hidden || !$("modal-rename").hidden || !$("modal-upload").hidden) {
        $("modal-tags").hidden = $("modal-rename").hidden = $("modal-upload").hidden = true;
        return;
      }
      if (!$("viewer").hidden) { closeViewer(); return; }
      if (!$("inspector").hidden) { closeInspector(); clearSelection(true); syncCardClasses(); return; }
      $("drawer").hidden = true;
    }
    if (!$("viewer").hidden) {
      if (e.key === "ArrowLeft") vwNav(-1);
      if (e.key === "ArrowRight") vwNav(1);
    }
  });

  new IntersectionObserver((entries) => {
    if (entries[0].isIntersecting && !state.done && !state.loading && (state.view === "all" || state.view === "untagged")) loadFiles(false);
  }).observe($("sentinel"));
}

/* ================= 启动 ================= */
window.addEventListener("error", (e) => toast("脚本错误: " + (e.message || "未知"), "error"));
(async function init() {
  $("sort").value = state.sort;
  loadRecentTags();
  bindEvents();
  mountTagEditors();
  try { await loadTags(); } catch (e) { toast("标签加载失败：" + e.message, "error"); }
  try { await loadFiles(true); } catch (e) { toast("文件加载失败：" + e.message, "error"); }
  refreshInbox();
})();
