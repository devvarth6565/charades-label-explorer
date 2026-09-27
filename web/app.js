// Charades Label Explorer front end. Plain ES module: no framework, no build,
// no dependencies. All state lives in the URL, so every view is shareable.

// ------------------------------------------------------------------ helpers

const $ = (selector, root = document) => root.querySelector(selector);

/** Build DOM nodes without innerHTML (dataset text can never become markup). */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "style") Object.assign(el.style, value); // CSSOM is allowed by our CSP
    else if (key === "vars") for (const [name, v] of Object.entries(value)) el.style.setProperty(name, v);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

/** replaceChildren() that skips null/false (which it would otherwise print as text). */
function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c != null && c !== false));
}

const numberFormat = new Intl.NumberFormat();
const fmtInt = (n) => numberFormat.format(n ?? 0);
const fmtSec = (s) => (s == null ? "–" : `${s.toFixed(1)}s`);
const pct = (fraction) => `${(Math.max(0, Math.min(1, fraction)) * 100).toFixed(3)}%`;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON */
  }
  if (!res.ok) throw new Error(body?.error || `Request failed (HTTP ${res.status})`);
  return body;
}

// -------------------------------------------------------------------- state

const MULTI = ["scene", "action", "verb", "object", "issue"];
const SINGLE = ["q", "split", "verified", "min_length", "max_length", "min_quality", "has_video", "sort", "page"];
const ISSUE_EXTRA = { any: "Any QA issue", none: "No issues (clean)" };

let meta = null;
let result = null;
let classById = new Map();
const hueByVerb = new Map();
let state = readUrl();
let openClipId = null;
let requestSeq = 0;
let lastFocus = null;
const ui = { expanded: {}, find: "" };
const lists = {};

function readUrl() {
  const params = new URLSearchParams(location.search);
  const s = { clip: params.get("clip") || null };
  for (const k of MULTI) s[k] = params.getAll(k).flatMap((v) => v.split(",")).filter(Boolean);
  for (const k of SINGLE) s[k] = params.get(k) || "";
  return s;
}

function toParams(s, { api: forApi = false, paging = true } = {}) {
  const params = new URLSearchParams();
  for (const k of SINGLE) {
    if (!s[k]) continue;
    if (k === "page" && (!paging || s[k] === "1")) continue;
    if (k === "sort" && s[k] === "relevance") continue;
    params.set(k, s[k]);
  }
  for (const k of MULTI) for (const v of s[k]) params.append(k, v);
  if (!forApi && s.clip) params.set("clip", s.clip);
  return params;
}

function writeUrl(push) {
  const qs = toParams(state).toString();
  history[push ? "pushState" : "replaceState"](null, "", qs ? `?${qs}` : location.pathname);
}

function update(changes, { push = true } = {}) {
  Object.assign(state, changes);
  if (!("page" in changes)) state.page = "";
  writeUrl(push);
  load();
}

function toggle(key, value) {
  const set = new Set(state[key]);
  if (set.has(value)) set.delete(value);
  else set.add(value);
  // "any" / "none" issue options are mutually exclusive with each other
  if (key === "issue" && value === "any" && set.has("any")) set.delete("none");
  if (key === "issue" && value === "none" && set.has("none")) set.delete("any");
  update({ [key]: [...set] });
}

function hueOf(verb) {
  return hueByVerb.has(verb) ? hueByVerb.get(verb) : 220;
}

// ---------------------------------------------------------------- loading

async function load() {
  const seq = ++requestSeq;
  setLoading(true);
  try {
    const data = await api(`api/clips?${toParams(state, { api: true })}`);
    if (seq !== requestSeq) return; // superseded by a newer request
    result = data;
    showError(null);
    render();
  } catch (err) {
    if (seq === requestSeq) showError(err.message);
  } finally {
    if (seq === requestSeq) setLoading(false);
  }
}

function setLoading(on) {
  $("#progress").hidden = !on;
  $(".table-wrap").classList.toggle("loading", on);
}

function showError(message) {
  const el = $("#error");
  el.hidden = !message;
  el.textContent = message || "";
}

// ---------------------------------------------------------------- rendering

function render() {
  renderSummary();
  renderChips();
  renderFacets();
  renderRows();
  renderPager();
  const exportParams = toParams(state, { api: true, paging: false });
  $("#export-csv").href = `api/export.csv?${exportParams}`;
  $("#export-json").href = `api/export.json?${exportParams}`;
  $("#sort").value = state.sort || "relevance";
  updateDrawerNav();
}

function renderStats() {
  const st = meta.stats;
  const items = [
    ["Clips", fmtInt(st.clips)],
    ["Hours", st.hours],
    ["Action segments", fmtInt(st.segments)],
    ["Action classes", st.classes],
    ["Scenes", st.scenes],
    ["Verified", `${((st.verified / st.clips) * 100).toFixed(1)}%`],
    ["QA-flagged", fmtInt(st.with_issues)],
  ];
  $("#stats").replaceChildren(...items.map(([k, v]) => h("div", null, h("dt", null, k), h("dd", null, v))));
}

function renderSummary() {
  const r = result;
  const parts = [
    h("strong", null, fmtInt(r.total)), r.total === 1 ? " clip" : " clips",
    h("span", { class: "faint" }, ` · ${r.hours} h · ${fmtInt(r.segments)} action segments`),
  ];
  if (state.q.trim()) parts.push(h("span", null, " matching ", h("b", null, `“${state.q.trim()}”`)));
  $("#summary").replaceChildren(...parts);
}

function labelFor(key, value) {
  if (key === "action") return classById.get(value)?.label || value;
  if (key === "issue") return ISSUE_EXTRA[value] || meta.issues.find((i) => i.value === value)?.label || value;
  return value;
}

function renderChips() {
  const chips = [];
  const chip = (name, value, onRemove) =>
    h("span", { class: "chip" },
      h("span", { title: `${name}: ${value}` }, h("b", null, `${name}: `), value),
      h("button", { type: "button", "aria-label": `Remove filter ${name}: ${value}`, onclick: onRemove }, "×"));

  if (state.q.trim()) chips.push(chip("Search", `“${state.q.trim()}”`, () => { $("#q").value = ""; update({ q: "" }); }));
  const names = { scene: "Scene", action: "Action", verb: "Verb", object: "Object", issue: "QA" };
  for (const key of MULTI) {
    for (const value of state[key]) chips.push(chip(names[key], labelFor(key, value), () => toggle(key, value)));
  }
  if (state.split) chips.push(chip("Split", state.split, () => update({ split: "" })));
  if (state.verified) chips.push(chip("Verified", state.verified === "yes" ? "yes" : "no", () => update({ verified: "" })));
  if (state.min_length) chips.push(chip("Length", `≥ ${state.min_length}s`, () => update({ min_length: "" })));
  if (state.max_length) chips.push(chip("Length", `≤ ${state.max_length}s`, () => update({ max_length: "" })));
  if (state.min_quality) chips.push(chip("Quality", `≥ ${state.min_quality}`, () => update({ min_quality: "" })));
  if (state.has_video) chips.push(chip("Video", "sample available", () => update({ has_video: "" })));
  if (chips.length > 1) chips.push(h("button", { type: "button", class: "link-btn chip-clear", onclick: resetAll }, "Clear all"));
  $("#chips").replaceChildren(...chips);
}

function resetAll() {
  for (const k of MULTI) state[k] = [];
  for (const k of SINGLE) if (k !== "sort") state[k] = "";
  syncInputs();
  update({});
}

// ------------------------------------------------------------------ filters

function section(title, note, ...content) {
  return h("section", { class: "filter" },
    h("div", { class: "filter-head" }, h("h3", null, title), note ? h("span", { class: "filter-note" }, note) : null),
    ...content);
}

function buildFilters() {
  lists.split = h("div", { class: "segmented", role: "group", "aria-label": "Dataset split" });
  lists.scene = h("div", { class: "facet-list" });
  lists.action = h("div", { class: "facet-list" });
  lists.verb = h("div", { class: "facet-list" });
  lists.object = h("div", { class: "facet-list" });
  lists.issue = h("div", { class: "facet-list" });
  lists.more = {};
  for (const key of ["action", "verb", "object"]) {
    lists.more[key] = h("button", { type: "button", class: "link-btn", onclick: () => {
      ui.expanded[key] = !ui.expanded[key];
      renderFacets();
    } });
  }

  const actionFind = h("input", {
    id: "action-find", class: "facet-find", type: "search", placeholder: `Find among ${meta.classes.length} actions…`,
    "aria-label": "Find an action class", autocomplete: "off",
    oninput: (e) => { ui.find = e.target.value; renderFacets(); },
  });

  const lengthInput = (id, placeholder, key) => h("input", {
    id, type: "number", min: "0", step: "1", inputmode: "decimal", placeholder, "aria-label": placeholder,
    oninput: debounce((e) => update({ [key]: e.target.value }, { push: false }), 450),
  });

  const minQuality = h("select", { id: "min-quality", class: "select", onchange: (e) => update({ min_quality: e.target.value }) },
    h("option", { value: "" }, "Any"),
    [3, 4, 5, 6].map((n) => h("option", { value: String(n) }, `≥ ${n}`)),
    h("option", { value: "7" }, "7 only"));
  const verified = h("select", { id: "verified", class: "select", onchange: (e) => update({ verified: e.target.value }) },
    h("option", { value: "" }, "Any"), h("option", { value: "yes" }, "Verified"), h("option", { value: "no" }, "Unverified"));

  const videoToggle = h("label", { class: "toggle" },
    h("input", { id: "has-video", type: "checkbox", onchange: (e) => update({ has_video: e.target.checked ? "1" : "" }) }),
    "Has sample video", h("span", { class: "count" }, fmtInt(meta.stats.with_video)));

  $("#filters").replaceChildren(
    section("Split", null, lists.split),
    section("Scene", "any of", lists.scene),
    section("Action", "all of", actionFind, lists.action, lists.more.action),
    section("Verb", "all of", lists.verb, lists.more.verb),
    section("Object", "all of", lists.object, lists.more.object),
    section("Length (seconds)", `${meta.stats.avg_length}s avg`,
      h("div", { class: "range" }, lengthInput("min-length", "min", "min_length"), h("span", null, "–"), lengthInput("max-length", "max", "max_length"))),
    section("Annotation", null,
      h("div", { class: "field-row" },
        h("label", null, "Min quality", minQuality),
        h("label", null, "Verified", verified))),
    section("QA issues", "any of", lists.issue),
    section("Video", null, videoToggle),
  );
}

function syncInputs() {
  $("#q").value = state.q;
  $("#min-length").value = state.min_length;
  $("#max-length").value = state.max_length;
  $("#min-quality").value = state.min_quality;
  $("#verified").value = state.verified;
  $("#has-video").checked = Boolean(state.has_video);
}

function facetButton(key, value, label, count, max, pressed, hue) {
  const bar = h("span", { class: "bar" });
  bar.style.width = `calc((100% - 36px) * ${max ? count / max : 0})`;
  return h("button", {
    type: "button", class: `facet-item${count ? "" : " zero"}`, "aria-pressed": String(pressed),
    title: `${label} (${fmtInt(count)} clips)`, onclick: () => toggle(key, value),
  },
  h("span", { class: "box", "aria-hidden": "true" }),
  h("span", { class: "name" }, hue != null ? h("span", { class: "dot", vars: { "--hue": hue } }) : null, label),
  h("span", { class: "count" }, fmtInt(count)),
  bar);
}

function renderFacetList(key, entries, { labelOf = (v) => v, hueFor = null, limit = Infinity, find = "" } = {}) {
  const selected = new Set(state[key]);
  const counts = new Map(entries.map((e) => [e.value, e.count]));
  let values = entries.map((e) => e.value);
  for (const v of selected) if (!counts.has(v)) values.push(v);
  if (find) {
    const needle = find.trim().toLowerCase();
    values = values.filter((v) => labelOf(v).toLowerCase().includes(needle) || v.toLowerCase() === needle);
  }
  const chosen = values.filter((v) => selected.has(v));
  const rest = values.filter((v) => !selected.has(v));
  const expanded = ui.expanded[key] || Boolean(find);
  const shown = [...chosen, ...(expanded ? rest : rest.slice(0, Math.max(0, limit - chosen.length)))];
  const max = Math.max(1, ...entries.map((e) => e.count));

  const list = lists[key];
  list.classList.toggle("scroll", expanded && shown.length > 12);
  list.replaceChildren(
    ...shown.map((v) => facetButton(key, v, labelOf(v), counts.get(v) || 0, max, selected.has(v), hueFor ? hueFor(v) : null)),
    ...(shown.length ? [] : [h("p", { class: "faint" }, find ? "No match." : "None in these results.")]),
  );
  const more = lists.more?.[key];
  if (more) {
    more.hidden = Boolean(find) || values.length <= limit;
    more.textContent = ui.expanded[key] ? "Show fewer" : `Show all ${values.length}`;
  }
}

function renderFacets() {
  const f = result.facets;
  const splitCounts = new Map(f.split.map((e) => [e.value, e.count]));
  const allCount = [...splitCounts.values()].reduce((a, b) => a + b, 0);
  lists.split.replaceChildren(
    ...[["", "All", allCount], ["train", "Train", splitCounts.get("train") || 0], ["test", "Test", splitCounts.get("test") || 0]].map(
      ([value, label, count]) =>
        h("button", { type: "button", "aria-pressed": String(state.split === value), onclick: () => update({ split: value }) },
          label, h("span", { class: "count" }, fmtInt(count)))),
  );

  renderFacetList("scene", f.scene);
  renderFacetList("action", f.action, {
    labelOf: (id) => classById.get(id)?.label || id,
    hueFor: (id) => hueOf(classById.get(id)?.verb),
    limit: 8,
    find: ui.find,
  });
  renderFacetList("verb", f.verb, { hueFor: hueOf, limit: 8 });
  renderFacetList("object", f.object, { limit: 8 });

  const issueCounts = new Map(f.issue.map((e) => [e.value, e.count]));
  const issueEntries = ["any", "none", ...meta.issues.map((i) => i.value)].map((value) => ({ value, count: issueCounts.get(value) || 0 }));
  renderFacetList("issue", issueEntries, { labelOf: (v) => labelFor("issue", v) });
}

// --------------------------------------------------------------------- table

function miniTimeline(clip) {
  if (!clip.segments.length) return h("span", { class: "mini-empty" }, "no actions labeled");
  const L = clip.length || Math.max(1, ...clip.segments.map((s) => Math.max(s.start, s.end)));
  const laneEnds = [];
  const placed = clip.segments.map((s) => {
    const a = clamp(Math.min(s.start, s.end), 0, L);
    const b = clamp(Math.max(s.start, s.end), 0, L);
    let lane = laneEnds.findIndex((end) => end <= a);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(b);
    } else laneEnds[lane] = b;
    return { s, a, b, lane };
  });
  const lanes = Math.min(laneEnds.length, 6);
  const gap = 1;
  const laneH = Math.max(2, (24 - 4 - gap * (lanes - 1)) / lanes);
  const el = h("div", { class: "mini", role: "img", "aria-label": `${clip.segments.length} action segments over ${fmtSec(L)}` });
  for (const p of placed) {
    const lane = Math.min(p.lane, lanes - 1);
    el.append(h("span", {
      class: `seg${p.s.end < p.s.start ? " bad" : ""}`,
      title: `${p.s.label}  ${p.s.start.toFixed(1)}–${p.s.end.toFixed(1)}s`,
      vars: { "--hue": hueOf(p.s.verb) },
      style: {
        left: pct(p.a / L), width: `max(2px, ${pct((p.b - p.a) / L)})`,
        top: `${2 + lane * (laneH + gap)}px`, height: `${laneH}px`,
      },
    }));
  }
  return el;
}

function labelChips(clip) {
  const seen = new Map();
  for (const s of clip.segments) if (!seen.has(s.label)) seen.set(s.label, s.verb);
  const labels = [...seen.entries()];
  const shown = labels.slice(0, 2);
  return h("div", { class: "labels" },
    shown.map(([label, verb]) => h("span", { class: "label-chip", title: label, vars: { "--hue": hueOf(verb) } }, label)),
    labels.length > shown.length ? h("span", { class: "more", title: labels.slice(2).map(([l]) => l).join("\n") }, `+${labels.length - shown.length}`) : null,
    labels.length ? null : h("span", { class: "faint" }, "–"));
}

function snippetNodes(text) {
  return text.split(/(\u0002[^\u0003]*\u0003)/).filter(Boolean).map((part) =>
    part.startsWith("\u0002") ? h("mark", null, part.slice(1, -1)) : part);
}

function issueTitle(clip) {
  return clip.issues.map((code) => labelFor("issue", code)).join("\n");
}

function renderRows() {
  const rows = [];
  for (const clip of result.items) {
    const tr = h("tr", {
      class: `row${clip.id === openClipId ? " selected" : ""}${clip.snippet ? " has-snippet" : ""}`,
      tabindex: "0", dataset: { id: clip.id }, "aria-label": `Clip ${clip.id}, ${clip.scene}, ${fmtSec(clip.length)}`,
    },
    h("td", null,
      h("div", { class: "clip-id" }, clip.id),
      h("div", { class: "badges" },
        clip.split === "test" ? h("span", { class: "badge" }, "test") : null,
        clip.has_video ? h("span", { class: "badge video", title: "Sample video available" }, "▶ video") : null)),
    h("td", { class: "scene-cell" }, h("span", { class: "scene", title: clip.scene_detail || clip.scene }, clip.scene)),
    h("td", { class: "num" }, fmtSec(clip.length)),
    h("td", null, miniTimeline(clip)),
    h("td", { class: "labels-cell" }, labelChips(clip)),
    h("td", { class: "quality-cell quality" },
      `${clip.quality ?? "–"} / ${clip.relevance ?? "–"}`,
      clip.verified === false ? h("span", { class: "unverified", title: "Not verified by annotator" }, " ✗") : null),
    h("td", null, clip.issues.length ? h("span", { class: "flag", title: issueTitle(clip) }, "⚠", clip.issues.length) : null));
    rows.push(tr);
    if (clip.snippet) {
      rows.push(h("tr", { class: "snippet-row", dataset: { id: clip.id } }, h("td", { colspan: "7" }, snippetNodes(clip.snippet))));
    }
  }
  $("#rows").replaceChildren(...rows);
  $("#empty").hidden = result.total > 0;
  $("#clips").hidden = result.total === 0;
}

function renderPager() {
  const { page, pages } = result;
  const pager = $("#pager");
  if (pages <= 1) {
    pager.replaceChildren();
    return;
  }
  const go = (p) => () => {
    update({ page: String(p) });
    $("#results").focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  };
  const numbers = [...new Set([1, page - 1, page, page + 1, pages].filter((p) => p >= 1 && p <= pages))].sort((a, b) => a - b);
  const items = [h("button", { type: "button", disabled: page === 1, onclick: go(page - 1), "aria-label": "Previous page" }, "‹")];
  numbers.forEach((p, i) => {
    if (i && p - numbers[i - 1] > 1) items.push(h("span", { class: "gap" }, "…"));
    items.push(h("button", { type: "button", "aria-current": p === page ? "page" : null, onclick: go(p) }, fmtInt(p)));
  });
  items.push(h("button", { type: "button", disabled: page === pages, onclick: go(page + 1), "aria-label": "Next page" }, "›"));
  pager.replaceChildren(...items);
}

// -------------------------------------------------------------------- drawer

function niceTicks(length) {
  const step = [1, 2, 5, 10, 15, 20, 30, 60].find((s) => length / s <= 6) || 120;
  const ticks = [];
  for (let t = 0; t <= length + 1e-9; t += step) ticks.push(t);
  return ticks;
}

async function openClip(id, { push = true } = {}) {
  if (!openClipId) lastFocus = document.activeElement;
  openClipId = id;
  state.clip = id;
  writeUrl(push);
  document.title = `${id} · Charades Label Explorer`;
  for (const tr of document.querySelectorAll("#rows tr.row")) tr.classList.toggle("selected", tr.dataset.id === id);

  $("#drawer").hidden = false;
  $("#scrim").hidden = false;
  $("#drawer-title").replaceChildren(h("span", { class: "clip-id" }, id));
  $("#drawer-body").replaceChildren(h("p", { class: "muted" }, "Loading…"));
  updateDrawerNav();
  $("#close-drawer").focus();
  try {
    const clip = await api(`api/clips/${encodeURIComponent(id)}`);
    if (openClipId === id) renderDrawer(clip);
  } catch (err) {
    if (openClipId === id) $("#drawer-body").replaceChildren(h("div", { class: "error" }, err.message));
  }
}

function closeDrawer({ push = true } = {}) {
  if (!openClipId) return;
  const video = $("#drawer video");
  if (video) video.pause();
  openClipId = null;
  state.clip = null;
  writeUrl(push);
  document.title = "Charades Label Explorer";
  $("#drawer").hidden = true;
  $("#scrim").hidden = !$("#sidebar").classList.contains("open");
  for (const tr of document.querySelectorAll("#rows tr.selected")) tr.classList.remove("selected");
  if (lastFocus && document.contains(lastFocus)) lastFocus.focus();
}

function currentIndex() {
  return result ? result.items.findIndex((c) => c.id === openClipId) : -1;
}

function updateDrawerNav() {
  if (!result) return;
  const i = currentIndex();
  $("#prev-clip").disabled = i === -1 || (i === 0 && result.page === 1);
  $("#next-clip").disabled = i === -1 || (i === result.items.length - 1 && result.page === result.pages);
}

async function step(delta) {
  const i = currentIndex();
  if (i === -1) return;
  const j = i + delta;
  if (j >= 0 && j < result.items.length) return openClip(result.items[j].id, { push: false });
  const page = result.page + delta;
  if (page < 1 || page > result.pages) return;
  state.page = String(page);
  writeUrl(false);
  await load();
  const items = result.items;
  if (items.length) openClip(delta > 0 ? items[0].id : items[items.length - 1].id, { push: false });
}

function renderDrawer(clip) {
  const L = clip.length || Math.max(1, ...clip.segments.map((s) => Math.max(s.start, s.end)));
  fill($("#drawer-title"),
    h("span", { class: "clip-id" }, clip.id),
    h("span", { class: "badge" }, clip.split),
    clip.issues.length ? h("span", { class: "badge warn" }, `${clip.issues.length} QA issue${clip.issues.length > 1 ? "s" : ""}`) : null);

  const video = clip.has_video
    ? h("video", { src: `videos/${encodeURIComponent(clip.id)}.mp4`, controls: true, preload: "metadata", playsinline: true })
    : null;
  const now = h("div", { class: "now", "aria-live": "off" });

  // Gantt: one row per labeled segment, sharing a playhead via the --t variable.
  const gantt = h("div", { class: `gantt${video ? " playable" : ""}` });
  gantt.append(h("div", { class: "g-row g-axis", "aria-hidden": "true" },
    h("div"),
    h("div", { class: "g-track" }, niceTicks(L).map((t) => h("span", { class: "tick", style: { left: pct(t / L) } }, `${t}s`))),
    h("div")));
  const rows = clip.segments.map((s) => {
    const a = clamp(Math.min(s.start, s.end), 0, L);
    const b = clamp(Math.max(s.start, s.end), 0, L);
    const inverted = s.end < s.start;
    const overrun = Math.max(s.start, s.end) > L + 2;
    const problem = inverted ? "ends before it starts" : overrun ? "ends after the video" : "";
    const el = h("div", {
      class: "g-row seg-row", dataset: { start: a },
      role: video ? "button" : null, tabindex: video ? "0" : null,
      title: `${s.class_id} ${s.label}\nverb: ${s.verb || "–"} · object: ${s.object || "–"}\n${s.start.toFixed(2)}s → ${s.end.toFixed(2)}s${problem ? `\n⚠ ${problem}` : ""}`,
    },
    h("div", { class: "g-label" }, h("span", { class: "cid" }, s.class_id), s.label),
    h("div", { class: "g-track" },
      h("span", { class: `g-bar${inverted ? " bad" : ""}`, vars: { "--hue": hueOf(s.verb) }, style: { left: pct(a / L), width: pct((b - a) / L) } })),
    h("div", { class: `g-time${problem ? " bad" : ""}` }, `${s.start.toFixed(1)}–${s.end.toFixed(1)}s`));
    gantt.append(el);
    return { el, s, a, b };
  });
  if (!rows.length) gantt.append(h("p", { class: "faint" }, "No action segments were labeled for this clip."));

  if (video) wireVideo(video, gantt, rows, now, L);

  const verifiedText = { true: "Yes", false: "No", null: "–" }[String(clip.verified)];
  const metaItems = [
    ["Quality", clip.quality != null ? `${clip.quality} / 7` : "–", clip.quality == null],
    ["Relevance", clip.relevance != null ? `${clip.relevance} / 7` : "–", clip.relevance == null],
    ["Verified", verifiedText, clip.verified === false],
    ["Labeled", `${Math.round(clip.coverage * 100)}% of video`, false],
    ["Actions", `${clip.n_actions} segments`, clip.n_actions === 0],
    ["Subject", clip.subject, false],
  ];

  fill($("#drawer-body"),
    h("p", { class: "subline" },
      h("b", null, clip.scene), clip.scene_detail ? ` (${clip.scene_detail})` : "", ` · ${fmtSec(L)} · ${clip.split} split`),
    video ? h("div", { class: "video-wrap" }, video) : h("div", { class: "no-video" },
      "No sample video downloaded for this clip. setup.sh fetches ",
      h("b", null, `${meta.stats.with_video}`), " sample videos; filter by ", h("i", null, "Has sample video"), " to find them."),
    video ? now : null,
    h("div", { class: "section" },
      h("h3", null, `Action timeline (${clip.segments.length})`, video ? h("span", { class: "filter-note" }, "click a row to jump there") : null),
      gantt),
    h("div", { class: "section" }, h("h3", null, "Annotation"),
      h("dl", { class: "meta-grid" }, metaItems.map(([k, v, bad]) => h("div", null, h("dt", null, k), h("dd", { class: bad ? "warn" : null }, v))))),
    clip.issues.length ? h("div", { class: "section" }, h("h3", null, "QA issues"),
      h("ul", { class: "issue-list" }, clip.issues.map((code) => h("li", null, h("code", null, code), labelFor("issue", code))))) : null,
    h("div", { class: "section" }, h("h3", null, "Script (what the actor was asked to do)"), h("blockquote", { class: "script" }, clip.script || "–")),
    h("div", { class: "section" }, h("h3", null, `Descriptions by annotators (${clip.descriptions.length})`),
      h("ol", { class: "descriptions" }, clip.descriptions.map((d) => h("li", null, d)))),
    h("div", { class: "section" }, h("h3", null, "Objects"),
      h("div", { class: "obj-list" }, clip.objects.length ? clip.objects.map((o) => h("span", { class: "obj" }, o)) : h("span", { class: "faint" }, "–"))),
    h("div", { class: "section" },
      h("details", { class: "raw" }, h("summary", null, "Raw label string (as in Charades_v1_*.csv)"),
        h("pre", null, clip.raw_actions || "(empty)"))),
  );
}

function wireVideo(video, gantt, rows, now, L) {
  let frame = 0;
  let lastKey = null;
  const tick = () => {
    const t = video.currentTime;
    gantt.style.setProperty("--t", String(clamp(t / L, 0, 1)));
    const active = [];
    for (const row of rows) {
      const on = t >= row.a && t <= row.b;
      row.el.classList.toggle("active", on);
      if (on) active.push(row.s);
    }
    const key = `${t.toFixed(1)}|${active.map((s) => s.class_id).join()}`;
    if (key !== lastKey) {
      lastKey = key;
      now.replaceChildren(
        h("b", null, `${t.toFixed(1)}s`), active.length ? " now: " : " no labeled action",
        active.map((s) => h("span", { class: "label-chip", vars: { "--hue": hueOf(s.verb) } }, s.label)));
    }
    if (!video.paused && !video.ended) frame = requestAnimationFrame(tick);
  };
  video.addEventListener("play", () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(tick); });
  for (const evt of ["pause", "seeked", "loadedmetadata", "ended"]) video.addEventListener(evt, tick);

  const seek = (t) => {
    video.currentTime = clamp(t, 0, Math.min(L, video.duration || L));
    video.play().catch(() => {});
  };
  gantt.addEventListener("click", (e) => {
    const track = e.target.closest(".seg-row .g-track");
    if (track) {
      const box = track.getBoundingClientRect();
      return seek(((e.clientX - box.left) / box.width) * L);
    }
    const row = e.target.closest(".seg-row");
    if (row) seek(Number(row.dataset.start));
  });
  gantt.addEventListener("keydown", (e) => {
    const row = e.target.closest(".seg-row");
    if (row && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      seek(Number(row.dataset.start));
    }
  });
  tick();
}

// -------------------------------------------------------------------- events

function isTyping(target) {
  return target.closest("input, select, textarea, video, [contenteditable]");
}

function trapFocus(e) {
  const drawer = $("#drawer");
  if (drawer.hidden || e.key !== "Tab") return;
  const focusable = [...drawer.querySelectorAll("button:not([disabled]), [href], input, select, video, [tabindex='0'], summary")]
    .filter((el) => el.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function setSidebar(open) {
  $("#sidebar").classList.toggle("open", open);
  $("#filters-toggle").setAttribute("aria-expanded", String(open));
  $("#scrim").hidden = !open && !openClipId;
}

function wireEvents() {
  const onSearch = debounce((value) => update({ q: value }, { push: false }), 180);
  $("#q").addEventListener("input", (e) => onSearch(e.target.value));
  $("#q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") update({ q: e.target.value });
  });
  $("#sort").addEventListener("change", (e) => update({ sort: e.target.value }));
  $("#empty-reset").addEventListener("click", resetAll);
  $("#close-drawer").addEventListener("click", () => closeDrawer());
  $("#prev-clip").addEventListener("click", () => step(-1));
  $("#next-clip").addEventListener("click", () => step(1));
  $("#filters-toggle").addEventListener("click", () => setSidebar(!$("#sidebar").classList.contains("open")));
  $("#scrim").addEventListener("click", () => {
    setSidebar(false);
    closeDrawer();
  });

  const rowsEl = $("#rows");
  rowsEl.addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-id]");
    if (tr) openClip(tr.dataset.id);
  });
  rowsEl.addEventListener("keydown", (e) => {
    const tr = e.target.closest("tr.row");
    if (!tr) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openClip(tr.dataset.id);
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const all = [...rowsEl.querySelectorAll("tr.row")];
      all[clamp(all.indexOf(tr) + (e.key === "ArrowDown" ? 1 : -1), 0, all.length - 1)]?.focus();
    }
  });

  document.addEventListener("keydown", (e) => {
    trapFocus(e);
    if (e.key === "Escape") {
      if (openClipId) closeDrawer();
      else setSidebar(false);
      return;
    }
    if (isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "/") {
      e.preventDefault();
      $("#q").focus();
      $("#q").select();
    } else if (openClipId && (e.key === "j" || e.key === "ArrowRight")) step(1);
    else if (openClipId && (e.key === "k" || e.key === "ArrowLeft")) step(-1);
  });

  window.addEventListener("popstate", () => {
    state = readUrl();
    syncInputs();
    load().then(() => {
      if (state.clip && state.clip !== openClipId) openClip(state.clip, { push: false });
    });
    if (!state.clip && openClipId) closeDrawer({ push: false });
  });
}

// ---------------------------------------------------------------------- init

async function init() {
  wireEvents();
  try {
    meta = await api("api/meta");
  } catch (err) {
    showError(`Could not load the dataset: ${err.message}. Did you run ./setup.sh?`);
    return;
  }
  classById = new Map(meta.classes.map((c) => [c.id, c]));
  // Spread verb hues around the colour wheel with the golden angle so
  // neighbouring verbs never get similar colours.
  meta.verbs.forEach((v, i) => hueByVerb.set(v.value, Math.round((i * 137.508 + 215) % 360)));

  renderStats();
  $("#sort").replaceChildren(...meta.sorts.map((s) => h("option", { value: s.value }, s.label)));
  buildFilters();
  syncInputs();
  await load();
  if (state.clip) openClip(state.clip, { push: false });
}

init();
