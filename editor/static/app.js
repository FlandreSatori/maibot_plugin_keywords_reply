const REPLY_KINDS = {
  text: { label: "文本", prefix: "" },
  music: { label: "音乐", prefix: "" },
  image: { label: "图片", prefix: "images/" },
  voice: { label: "语音", prefix: "records/" },
  emoji: { label: "表情", prefix: "emojis/" },
};

const MUSIC_PLATFORMS = [
  { value: "163", label: "网易云 163" },
  { value: "qq", label: "QQ音乐" },
  { value: "migu", label: "咪咕" },
  { value: "kugou", label: "酷狗" },
  { value: "kuwo", label: "酷我" },
];

const PAGE_SIZE_DEFAULT = 50;
const REPLY_PAGE_SIZE = 40;
const FILTER_DEBOUNCE_MS = 280;

const state = {
  data: { command_triggered: [], auto_detect: [] },
  section: "command_triggered",
  selected: new Set(),
  dirty: false,
  path: "",
  replyRows: [],
  rowSeq: 0,
  page: 1,
  pageSize: PAGE_SIZE_DEFAULT,
  replyPage: 1,
  visibleIndices: [],
  searchHay: [],
  filterTimer: null,
  renderQueued: false,
  addMenuRowId: null,
  selectedEntries: new Set(),
  partSeq: 0,
  reorder: null,
};

const LONG_PRESS_MS = 420;

const $ = (id) => document.getElementById(id);

function emptyEntry() {
  return {
    text: "",
    weight: 100,
    probability: 100,
    images: [],
    records: [],
    ats: [],
    faces: [],
    emojis: [],
    music_cards: [],
  };
}

function emptyRule() {
  return {
    keyword: "",
    regex: false,
    enabled: true,
    mode: "whitelist",
    groups: [],
    require_at_bot: false,
    entries: [],
  };
}

function newRowId() {
  state.rowSeq += 1;
  return `row-${state.rowSeq}`;
}

function newPartId() {
  state.partSeq += 1;
  return `part-${state.partSeq}`;
}

function clampWeight(value) {
  const n = parseInt(String(value ?? 100), 10);
  if (Number.isNaN(n)) return 100;
  return Math.max(0, Math.min(1000, n));
}

function clampProbability(value) {
  const n = parseInt(String(value ?? 100), 10);
  if (Number.isNaN(n)) return 100;
  return Math.max(0, Math.min(100, n));
}

function basename(path) {
  const normalized = String(path || "").replace(/\\/g, "/").trim();
  if (!normalized) return "";
  return normalized.split("/").pop() || "";
}

function normalizeMediaPath(kind, rawPath) {
  const text = String(rawPath || "").trim().replace(/\\/g, "/");
  if (!text) return "";
  const cfg = REPLY_KINDS[kind];
  if (!cfg) return text;
  if (text.includes("/")) return text;
  return `${cfg.prefix}${text}`;
}

function entryHasPayload(entry) {
  if (entry.parts?.length) {
    return entry.parts.some((part) => {
      if (part.type === "text") return !!(part.text || "").trim();
      if (part.type === "music") return !!(part.id || "").trim();
      if (part.type === "image" || part.type === "voice" || part.type === "emoji") {
        return !!(part.file || "").trim();
      }
      return false;
    });
  }
  return Boolean(
    (entry.text || "").trim() ||
      entry.images?.length ||
      entry.records?.length ||
      entry.emojis?.length ||
      entry.music_cards?.length ||
      entry.ats?.length ||
      entry.faces?.length
  );
}

function splitPaths(raw) {
  return String(raw || "")
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function mediaFilesToField(items, prefix) {
  return (items || [])
    .map((item) => {
      const file = item?.file || "";
      if (!file) return "";
      return file.includes("/") ? file : `${prefix}/${file}`;
    })
    .filter(Boolean)
    .join(", ");
}

function fieldToMediaFiles(raw, kind) {
  const files = [];
  for (const part of splitPaths(raw)) {
    const file = basename(normalizeMediaPath(kind, part));
    if (file) files.push({ file });
  }
  return files;
}

const PART_TYPES = [
  { key: "text", label: "文本", addLabel: "文本", placeholder: "回复文本" },
  { key: "image", label: "图片", addLabel: "图片", placeholder: "images/a.jpg" },
  { key: "voice", label: "语音", addLabel: "语音", placeholder: "records/a.silk" },
  { key: "emoji", label: "表情", addLabel: "表情", placeholder: "emojis/a.gif" },
  { key: "music", label: "音乐", addLabel: "音乐" },
];

function partTypeLabel(type) {
  return PART_TYPES.find((p) => p.key === type)?.label || type;
}

function createEmptyPart(type) {
  return {
    id: newPartId(),
    type,
    text: "",
    paths: "",
    platform: "163",
    musicId: "",
  };
}

function partHasContent(part) {
  if (!part) return false;
  if (part.type === "text") return !!(part.text || "").trim();
  if (part.type === "music") return !!(part.musicId || "").trim();
  if (part.type === "image" || part.type === "voice" || part.type === "emoji") {
    return !!(part.paths || "").trim();
  }
  return false;
}

function entryPartFromJson(part) {
  const type = part.type || "text";
  const rowPart = createEmptyPart(type);
  if (type === "text") rowPart.text = part.text || "";
  if (type === "image" || type === "voice" || type === "emoji") {
    const file = part.file || "";
    rowPart.paths = file ? normalizeMediaPath(type, file.includes("/") ? file : `${REPLY_KINDS[type].prefix}${file}`) : "";
  }
  if (type === "music") {
    rowPart.platform = part.platform || "163";
    rowPart.musicId = part.id || "";
  }
  return rowPart;
}

function entryToParts(entry) {
  if (entry.parts?.length) {
    return entry.parts.map(entryPartFromJson);
  }
  const parts = [];
  const text = (entry.text || "").trim();
  if (text) {
    for (const line of text.split("\n")) {
      if (line.trim()) parts.push({ ...createEmptyPart("text"), text: line.trim() });
    }
  }
  for (const img of entry.images || []) {
    if (img?.file) {
      parts.push({
        ...createEmptyPart("image"),
        paths: normalizeMediaPath("image", img.file.includes("/") ? img.file : `images/${img.file}`),
      });
    }
  }
  for (const voice of entry.records || []) {
    if (voice?.file) {
      parts.push({
        ...createEmptyPart("voice"),
        paths: normalizeMediaPath("voice", voice.file.includes("/") ? voice.file : `records/${voice.file}`),
      });
    }
  }
  for (const emoji of entry.emojis || []) {
    if (emoji?.file) {
      parts.push({
        ...createEmptyPart("emoji"),
        paths: normalizeMediaPath("emoji", emoji.file.includes("/") ? emoji.file : `emojis/${emoji.file}`),
      });
    }
  }
  const music = entry.music_cards?.[0];
  if (music?.id) {
    parts.push({
      ...createEmptyPart("music"),
      platform: music.platform || "163",
      musicId: music.id || "",
    });
  }
  return parts;
}

function partToJson(part) {
  const out = { type: part.type };
  if (part.type === "text") out.text = String(part.text || "").trim();
  if (part.type === "image" || part.type === "voice" || part.type === "emoji") {
    const file = basename(normalizeMediaPath(part.type, splitPaths(part.paths)[0] || ""));
    if (file) out.file = file;
  }
  if (part.type === "music") {
    out.platform = String(part.platform || "163").trim();
    out.id = String(part.musicId || "").trim();
  }
  return out;
}

function entryHasReplyType(entry, type) {
  if (type === "all") return true;
  const parts = entry.parts?.length ? entry.parts : entryToParts(entry).map(partToJson);
  if (type === "text") return parts.some((p) => p.type === "text" && (p.text || "").trim());
  if (type === "image") return parts.some((p) => p.type === "image");
  if (type === "voice") return parts.some((p) => p.type === "voice");
  if (type === "emoji") return parts.some((p) => p.type === "emoji");
  if (type === "music") return parts.some((p) => p.type === "music");
  return true;
}

function ruleHasReplyType(rule, type) {
  if (type === "all") return true;
  return (rule.entries || []).some((entry) => entryHasReplyType(entry, type));
}

function rowHasPayload(row) {
  return (row.parts || []).some(partHasContent);
}

function entryToRow(entry) {
  const parts = entryToParts(entry);
  return {
    id: newRowId(),
    kind: "entry",
    weight: clampWeight(entry.weight),
    probability: clampProbability(entry.probability),
    parts: parts.length ? parts : [],
  };
}

function entriesToRows(entries) {
  const rows = (entries || []).map(entryToRow).filter(rowHasPayload);
  return rows.length ? rows : [createEmptyRow()];
}

function rowToEntry(row) {
  const parts = (row.parts || []).filter(partHasContent).map(partToJson);
  return {
    weight: clampWeight(row.weight),
    probability: clampProbability(row.probability),
    parts,
    text: "",
    images: [],
    records: [],
    ats: [],
    faces: [],
    emojis: [],
    music_cards: [],
  };
}

function rowsToEntries(rows) {
  const entries = [];
  for (const row of rows || []) {
    const entry = rowToEntry(row);
    if (entryHasPayload(entry)) entries.push(entry);
  }
  return entries;
}

function describeRowKind(row) {
  const labels = (row.parts || []).filter(partHasContent).map((p) => partTypeLabel(p.type));
  return labels.length ? labels.join("→") : "空";
}

function createEmptyRow() {
  return {
    id: newRowId(),
    kind: "entry",
    weight: 100,
    probability: 100,
    parts: [],
  };
}

function rowHasContentType(row, type) {
  if (type === "all") return true;
  return (row.parts || []).some((part) => {
    if (type === "text") return part.type === "text" && !!(part.text || "").trim();
    if (type === "image") return part.type === "image" && !!(part.paths || "").trim();
    if (type === "voice") return part.type === "voice" && !!(part.paths || "").trim();
    if (type === "emoji") return part.type === "emoji" && !!(part.paths || "").trim();
    if (type === "music") return part.type === "music" && !!(part.musicId || "").trim();
    return false;
  });
}

function getEntryFilterType() {
  return $("entryFilterReplyType")?.value || "all";
}

function getFilteredReplyRows() {
  const type = getEntryFilterType();
  return state.replyRows.filter((row) => rowHasContentType(row, type));
}

function renderPartContent(part) {
  const meta = PART_TYPES.find((p) => p.key === part.type);
  if (part.type === "music") {
    const platformOptions = MUSIC_PLATFORMS.map(
      (p) => `<option value="${p.value}" ${part.platform === p.value ? "selected" : ""}>${p.label}</option>`
    ).join("");
    return `
      <div class="music-inline">
        <select data-field="platform">${platformOptions}</select>
        <input type="text" data-field="musicId" value="${escapeHtml(part.musicId || "")}" placeholder="歌曲 ID" />
      </div>`;
  }
  if (part.type === "text") {
    return `<input type="text" data-field="text" value="${escapeHtml(part.text || "")}" placeholder="${meta?.placeholder || ""}" />`;
  }
  return `<input type="text" data-field="paths" value="${escapeHtml(part.paths || "")}" placeholder="${meta?.placeholder || ""}" />`;
}

function renderEntryBlock(row) {
  const checked = state.selectedEntries.has(row.id) ? "checked" : "";
  const rail = `
    <aside class="entry-rail">
      <input type="checkbox" data-action="select-entry" ${checked} title="选中本条回复" />
      <div class="rail-field rail-weight">
        <span class="rail-label">权重</span>
        <input type="number" min="0" max="1000" step="1" data-field="weight" value="${clampWeight(row.weight)}" title="多条回复时按权重随机抽取" />
      </div>
      <div class="rail-field rail-probability">
        <span class="rail-label">概率</span>
        <div class="prob-inline">
          <input type="number" min="0" max="100" step="1" data-field="probability" value="${clampProbability(row.probability)}" title="抽中后实际回复的概率" />
          <span>%</span>
        </div>
      </div>
    </aside>`;

  const parts = row.parts || [];
  if (!parts.length) {
    return `
      <div class="entry-block" data-row-id="${row.id}">
        <div class="entry-block-layout">
          ${rail}
          <div class="entry-body">
            <div class="reply-row part-row is-empty">
              <div class="drag-handle-placeholder"></div>
              <div class="kind-badge">—</div>
              <div class="hint-inline">点击 + 添加内容</div>
              <div class="row-actions">
                <span class="action-placeholder"></span>
                <button type="button" class="btn-add-part" data-action="toggle-add-menu" title="添加内容">+</button>
              </div>
            </div>
          </div>
        </div>
      </div>`;
  }

  const partRows = parts
    .map((part, index) => {
      const isLast = index === parts.length - 1;
      const addBtn = isLast
        ? `<button type="button" class="btn-add-part" data-action="toggle-add-menu" title="添加内容">+</button>`
        : `<span class="action-placeholder" aria-hidden="true"></span>`;
      return `
        <div class="reply-row part-row" data-part-id="${part.id}">
          <button type="button" class="drag-handle" data-action="drag-handle" title="长按拖动排序" aria-label="拖动排序">⠿</button>
          <div class="kind-badge">${partTypeLabel(part.type)}</div>
          <div class="part-content">${renderPartContent(part)}</div>
          <div class="row-actions">
            <button type="button" class="btn-icon secondary" data-action="remove-part" data-part-id="${part.id}" title="移除此行">×</button>
            ${addBtn}
          </div>
        </div>`;
    })
    .join("");

  return `
    <div class="entry-block" data-row-id="${row.id}">
      <div class="entry-block-layout">
        ${rail}
        <div class="entry-body">${partRows}</div>
      </div>
    </div>`;
}

function summarizeEntry(entry) {
  const parts = [];
  const weight = clampWeight(entry.weight);
  const probability = clampProbability(entry.probability);
  if (weight !== 100) parts.push(`权重${weight}`);
  if (probability !== 100) parts.push(`${probability}%`);
  const ordered = entry.parts?.length ? entry.parts : entryToParts(entry).map(partToJson);
  for (const part of ordered) {
    if (part.type === "text" && part.text) parts.push(part.text.slice(0, 24));
    if (part.type === "image" && part.file) parts.push(`[图 ${part.file}]`);
    if (part.type === "voice" && part.file) parts.push(`[语音 ${part.file}]`);
    if (part.type === "emoji" && part.file) parts.push(`[表情 ${part.file}]`);
    if (part.type === "music" && part.id) parts.push(`[音乐 ${part.platform}:${part.id}]`);
  }
  if (!ordered.length) {
    if (entry.text) parts.push(entry.text.slice(0, 36));
    if (entry.images?.length) parts.push(`[图片 ${entry.images.map((x) => x.file).join(",")}]`);
  }
  return parts.join(" ") || "[空]";
}

function buildSearchHay(rule) {
  const keyword = (rule.keyword || "").toLowerCase();
  const entries = rule.entries || [];
  if (!entries.length) return keyword;
  const previews = entries.slice(0, 2).map((entry) => summarizeEntry(entry)).join(" ").toLowerCase();
  return `${keyword} ${previews}`;
}

function rebuildSearchHay() {
  const rules = getRules();
  state.searchHay = rules.map((rule) => buildSearchHay(rule));
}

function summarizeEntriesForList(rule) {
  const entries = rule.entries || [];
  const count = entries.length;
  if (!count) return '<span class="entry-preview">无回复</span>';
  const preview = escapeHtml(summarizeEntry(entries[0]));
  return `<div class="entry-preview"><strong>${count} 条回复</strong><br>${preview}</div>`;
}

function getFilterValues() {
  return {
    q: $("filterSearch").value.trim().toLowerCase(),
    enabled: $("filterEnabled").value,
    requireAt: $("filterRequireAt").value,
    regex: $("filterRegex").value,
    group: $("filterGroup").value.trim(),
  };
}

function ruleMatchesFilters(rule, index, filters) {
  if (filters.enabled === "enabled" && !rule.enabled) return false;
  if (filters.enabled === "disabled" && rule.enabled) return false;
  if (filters.requireAt === "yes" && !rule.require_at_bot) return false;
  if (filters.requireAt === "no" && rule.require_at_bot) return false;
  if (filters.regex === "yes" && !rule.regex) return false;
  if (filters.regex === "no" && rule.regex) return false;
  if (filters.group && !(rule.groups || []).includes(filters.group)) return false;
  if (filters.q) {
    const hay = state.searchHay[index] || buildSearchHay(rule);
    if (!hay.includes(filters.q)) return false;
  }
  return true;
}

function rebuildVisibleIndices() {
  const filters = getFilterValues();
  const rules = getRules();
  const indices = [];
  for (let index = 0; index < rules.length; index += 1) {
    if (ruleMatchesFilters(rules[index], index, filters)) indices.push(index);
  }
  state.visibleIndices = indices;
  const totalPages = Math.max(1, Math.ceil(indices.length / state.pageSize));
  if (state.page > totalPages) state.page = totalPages;
  if (state.page < 1) state.page = 1;
}

function getVisibleRules() {
  return state.visibleIndices.map((index) => ({
    rule: getRules()[index],
    index,
  }));
}

function getPagedVisibleRules() {
  const all = getVisibleRules();
  const start = (state.page - 1) * state.pageSize;
  return all.slice(start, start + state.pageSize);
}

function scheduleRender(resetPage = false) {
  if (resetPage) state.page = 1;
  if (state.filterTimer) clearTimeout(state.filterTimer);
  state.filterTimer = setTimeout(() => {
    state.filterTimer = null;
    rebuildVisibleIndices();
    queueRender();
  }, FILTER_DEBOUNCE_MS);
}

function queueRender() {
  if (state.renderQueued) return;
  state.renderQueued = true;
  requestAnimationFrame(() => {
    state.renderQueued = false;
    render();
  });
}

function describeGroups(cfg) {
  const mode = cfg.mode || "whitelist";
  const groups = (cfg.groups || []).join(",");
  if (mode === "blacklist") return groups ? `黑名单:${groups}` : "全局启用";
  return groups ? `白名单:${groups}` : "未配置群";
}

function getRules() {
  return state.data[state.section] || [];
}

function setRules(rules) {
  state.data[state.section] = rules;
  rebuildSearchHay();
}

function setStatus(text, isError = false) {
  const el = $("statusBar");
  el.textContent = text;
  el.style.color = isError ? "#ff6b6b" : "";
}

function markDirty() {
  state.dirty = true;
  setStatus("有未保存的修改");
}

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderReplyRows() {
  const container = $("replyRows");
  container.innerHTML = "";

  if (!state.replyRows.length) {
    $("replyPagination").hidden = true;
    container.innerHTML = '<p class="hint">暂无回复，请点击「添加回复」。</p>';
    return;
  }

  const filtered = getFilteredReplyRows();
  const totalPages = Math.max(1, Math.ceil(filtered.length / REPLY_PAGE_SIZE));
  if (state.replyPage > totalPages) state.replyPage = totalPages;
  if (state.replyPage < 1) state.replyPage = 1;
  const start = (state.replyPage - 1) * REPLY_PAGE_SIZE;
  const pageRows = filtered.slice(start, start + REPLY_PAGE_SIZE);

  const frag = document.createDocumentFragment();
  for (const row of pageRows) {
    const wrapper = document.createElement("div");
    wrapper.innerHTML = renderEntryBlock(row);
    frag.appendChild(wrapper.firstElementChild);
  }
  container.appendChild(frag);

  $("replyPagination").hidden = totalPages <= 1;
  $("replyPageInfo").textContent = `第 ${state.replyPage} / ${totalPages} 页（共 ${filtered.length} 条）`;
  $("btnReplyPrev").disabled = state.replyPage <= 1;
  $("btnReplyNext").disabled = state.replyPage >= totalPages;
  updateDeleteReplyButton();
}

function updateDeleteReplyButton() {
  const btn = $("btnDeleteReplyRows");
  if (!btn) return;
  const count = state.selectedEntries.size;
  btn.disabled = count === 0;
  btn.textContent = count > 0 ? `删除选中 (${count})` : "删除选中";
}

function syncReplyRowsFromDom() {
  for (const el of $("replyRows").querySelectorAll(".entry-block")) {
    const rowId = el.dataset.rowId;
    const existing = state.replyRows.find((r) => r.id === rowId);
    if (!existing) continue;

    const weightInput = el.querySelector('.entry-rail [data-field="weight"]');
    if (weightInput) existing.weight = clampWeight(weightInput.value);

    const probabilityInput = el.querySelector('.entry-rail [data-field="probability"]');
    if (probabilityInput) existing.probability = clampProbability(probabilityInput.value);

    for (const partEl of el.querySelectorAll(".part-row[data-part-id]")) {
      const partId = partEl.dataset.partId;
      const part = (existing.parts || []).find((p) => p.id === partId);
      if (!part) continue;

      const textInput = partEl.querySelector('[data-field="text"]');
      if (textInput) part.text = textInput.value;

      const pathsInput = partEl.querySelector('[data-field="paths"]');
      if (pathsInput) part.paths = pathsInput.value;

      const platformInput = partEl.querySelector('[data-field="platform"]');
      if (platformInput) part.platform = platformInput.value || "163";

      const musicInput = partEl.querySelector('[data-field="musicId"]');
      if (musicInput) part.musicId = musicInput.value;
    }
  }
}

function findReplyRow(rowId) {
  return state.replyRows.find((r) => r.id === rowId);
}

function clearReplyPart(row, partId) {
  row.parts = (row.parts || []).filter((p) => p.id !== partId);
}

function closePartAddMenu() {
  state.addMenuRowId = null;
  const menu = $("partAddMenu");
  menu.hidden = true;
}

function openPartAddMenu(rowId, anchorEl) {
  const row = findReplyRow(rowId);
  if (!row) return;

  state.addMenuRowId = rowId;
  const menu = $("partAddMenu");
  const rect = anchorEl.getBoundingClientRect();
  menu.style.left = `${Math.min(rect.left, window.innerWidth - 160)}px`;
  menu.style.top = `${rect.bottom + 6}px`;
  menu.hidden = false;

  for (const btn of menu.querySelectorAll("button[data-part]")) {
    btn.hidden = false;
  }
}

function addPartToRow(row, partKey) {
  if (!row.parts) row.parts = [];
  row.parts.push(createEmptyPart(partKey));
}

function deleteSelectedEntries() {
  if (!state.selectedEntries.size) return;
  const count = state.selectedEntries.size;
  if (!confirm(`确定删除选中的 ${count} 条回复？此操作不可撤销。`)) return;
  syncReplyRowsFromDom();
  state.replyRows = state.replyRows.filter((r) => !state.selectedEntries.has(r.id));
  state.selectedEntries.clear();
  closePartAddMenu();
  renderReplyRows();
}

function reorderPartInRow(row, fromIndex, toIndex) {
  const parts = row.parts || [];
  if (fromIndex < 0 || toIndex < 0 || fromIndex >= parts.length || toIndex >= parts.length) return;
  if (fromIndex === toIndex) return;
  const [moved] = parts.splice(fromIndex, 1);
  parts.splice(toIndex, 0, moved);
}

function applyPartRowShiftTransforms(reorder) {
  updateReorderPlaceholder(reorder);
}

function updateReorderPlaceholder(reorder) {
  const { body, partRow, placeholder, targetIndex } = reorder;
  if (!body || !placeholder) return;
  const siblings = [...body.querySelectorAll(".part-row[data-part-id]:not(.part-row--lifted)")];
  const clampedIndex = Math.max(0, Math.min(targetIndex, siblings.length));
  if (clampedIndex >= siblings.length) {
    body.appendChild(placeholder);
    return;
  }
  body.insertBefore(placeholder, siblings[clampedIndex]);
}

function clearPartRowShiftTransforms(reorder) {
  if (reorder?.placeholder) reorder.placeholder.remove();
}

function resetLiftedPartRow(partRow) {
  if (!partRow) return;
  partRow.classList.remove("part-row--lifted");
  partRow.style.position = "";
  partRow.style.top = "";
  partRow.style.left = "";
  partRow.style.width = "";
  partRow.style.height = "";
  partRow.style.zIndex = "";
  partRow.style.boxShadow = "";
  partRow.style.transform = "";
  partRow.style.transition = "";
  partRow.style.pointerEvents = "";
}

function computeReorderTargetIndex(reorder, clientY) {
  const { body } = reorder;
  const siblings = [...body.querySelectorAll(".part-row[data-part-id]:not(.part-row--lifted)")];
  for (let i = 0; i < siblings.length; i += 1) {
    const rect = siblings[i].getBoundingClientRect();
    if (clientY < rect.top + rect.height / 2) return i;
  }
  return siblings.length;
}

function beginPartReorder(ev, handleEl) {
  const partRow = handleEl.closest(".part-row");
  const block = handleEl.closest(".entry-block");
  const body = block?.querySelector(".entry-body");
  if (!partRow || !block || !body) return;

  const rowId = block.dataset.rowId;
  const partId = partRow.dataset.partId;
  if (!rowId || !partId) return;

  ev.preventDefault();
  if (state.reorder?.timer) clearTimeout(state.reorder.timer);

  state.reorder = {
    rowId,
    partId,
    pointerId: ev.pointerId,
    active: false,
    moved: false,
    timer: null,
    handleEl,
    partRow,
    body,
    block,
    rows: [],
    fromIndex: -1,
    targetIndex: -1,
    rowHeight: 0,
    rowHeights: [],
    offsetY: 0,
    anchorLeft: 0,
    anchorWidth: 0,
  };

  state.reorder.timer = setTimeout(() => activatePartReorder(ev), LONG_PRESS_MS);

  try {
    handleEl.setPointerCapture(ev.pointerId);
  } catch (_) {
    /* ignore */
  }
}

function activatePartReorder(ev) {
  const reorder = state.reorder;
  if (!reorder || reorder.active) return;

  syncReplyRowsFromDom();
  const body = reorder.body;
  const partRow = reorder.partRow;
  const rows = [...body.querySelectorAll(".part-row[data-part-id]")];
  const fromIndex = rows.indexOf(partRow);
  if (fromIndex < 0) return;

  const rect = partRow.getBoundingClientRect();
  const rowHeights = rows.map((row) => row.getBoundingClientRect().height);

  reorder.active = true;
  reorder.rows = rows;
  reorder.fromIndex = fromIndex;
  reorder.targetIndex = fromIndex;
  reorder.rowHeight = rect.height;
  reorder.rowHeights = rowHeights;
  reorder.offsetY = ev.clientY - rect.top;
  reorder.anchorLeft = rect.left;
  reorder.anchorWidth = rect.width;

  reorder.block.classList.add("entry-block--reordering");
  const placeholder = document.createElement("div");
  placeholder.className = "part-row-placeholder";
  placeholder.style.height = `${rect.height}px`;
  body.insertBefore(placeholder, partRow);
  reorder.placeholder = placeholder;

  partRow.classList.add("part-row--lifted");
  partRow.style.position = "fixed";
  partRow.style.left = `${rect.left}px`;
  partRow.style.top = `${rect.top}px`;
  partRow.style.width = `${rect.width}px`;
  partRow.style.height = `${rect.height}px`;
  partRow.style.zIndex = "1200";
  partRow.style.pointerEvents = "none";

  for (const rowEl of rows) {
    if (rowEl !== partRow) rowEl.classList.add("part-row--dimmed");
  }
}

function finishPartReorder(commit = true) {
  const reorder = state.reorder;
  if (!reorder) return;

  if (reorder.timer) clearTimeout(reorder.timer);

  if (reorder.active && commit && reorder.fromIndex !== reorder.targetIndex) {
    const row = findReplyRow(reorder.rowId);
    if (row) {
      const toIndex = Math.min(reorder.targetIndex, Math.max(0, (row.parts || []).length - 1));
      reorderPartInRow(row, reorder.fromIndex, toIndex);
    }
  }

  if (reorder.block) reorder.block.classList.remove("entry-block--reordering");
  if (reorder.rows) {
    for (const rowEl of reorder.rows) rowEl.classList.remove("part-row--dimmed");
  }
  resetLiftedPartRow(reorder.partRow);
  clearPartRowShiftTransforms(reorder);

  const shouldRender = reorder.active && commit;
  state.reorder = null;
  if (shouldRender) renderReplyRows();
}

function handlePartReorderMove(ev) {
  const reorder = state.reorder;
  if (!reorder) return;

  const delta = Math.abs(ev.clientY - (reorder.startClientY ?? ev.clientY));
  if (!reorder.startClientY) reorder.startClientY = ev.clientY;
  if (delta > 4) reorder.moved = true;

  if (!reorder.active) return;

  ev.preventDefault();
  reorder.partRow.style.top = `${ev.clientY - reorder.offsetY}px`;
  reorder.partRow.style.left = `${reorder.anchorLeft}px`;
  reorder.partRow.style.width = `${reorder.anchorWidth}px`;

  const nextIndex = computeReorderTargetIndex(reorder, ev.clientY);
  if (nextIndex !== reorder.targetIndex) {
    reorder.targetIndex = nextIndex;
    applyPartRowShiftTransforms(reorder);
  }
}

function updatePaginationBar(totalVisible) {
  const totalPages = Math.max(1, Math.ceil(totalVisible / state.pageSize));
  $("pageInfo").textContent = `第 ${state.page} / ${totalPages} 页`;
  $("btnPrevPage").disabled = state.page <= 1;
  $("btnNextPage").disabled = state.page >= totalPages;
}

function render() {
  const visible = getVisibleRules();
  const paged = getPagedVisibleRules();
  const tbody = $("ruleTableBody");
  tbody.innerHTML = "";

  $("statTotal").textContent = String(visible.length);
  $("statSelected").textContent = String(state.selected.size);
  $("emptyState").hidden = visible.length > 0;
  updatePaginationBar(visible.length);

  const frag = document.createDocumentFragment();
  for (const { rule, index } of paged) {
    const tr = document.createElement("tr");
    const checked = state.selected.has(index) ? "checked" : "";
    tr.innerHTML = `
      <td><input type="checkbox" data-select="${index}" ${checked} /></td>
      <td><strong>${escapeHtml(rule.keyword || "")}</strong>${rule.regex ? '<span class="badge">正则</span>' : ""}</td>
      <td>
        <span class="badge ${rule.enabled ? "on" : "off"}">${rule.enabled ? "启用" : "禁用"}</span>
        ${rule.require_at_bot ? '<span class="badge on">需@</span>' : ""}
      </td>
      <td>${escapeHtml(describeGroups(rule))}</td>
      <td>${summarizeEntriesForList(rule)}</td>
      <td>
        <button class="secondary" data-edit="${index}">编辑</button>
        <button class="danger" data-del="${index}">删除</button>
      </td>`;
    frag.appendChild(tr);
  }
  tbody.appendChild(frag);

  const pageIndices = paged.map(({ index }) => index);
  $("checkAll").checked =
    pageIndices.length > 0 && pageIndices.every((index) => state.selected.has(index));
}

function refreshList(resetPage = false) {
  if (resetPage) state.page = 1;
  rebuildVisibleIndices();
  queueRender();
}

function openRuleModal(index = -1) {
  $("ruleEditIndex").value = String(index);
  const isNew = index < 0;
  $("ruleModalTitle").textContent = isNew ? "新建词条" : "编辑词条";
  const rule = isNew ? emptyRule() : structuredClone(getRules()[index]);

  $("ruleKeyword").value = rule.keyword || "";
  $("ruleEnabled").checked = !!rule.enabled;
  $("ruleRegex").checked = !!rule.regex;
  $("ruleRequireAt").checked = !!rule.require_at_bot;
  $("ruleMode").value = rule.mode || "whitelist";
  $("ruleGroups").value = (rule.groups || []).join(",");

  state.replyRows = entriesToRows(rule.entries || []);
  state.replyPage = 1;
  state.selectedEntries.clear();
  if ($("entryFilterReplyType")) $("entryFilterReplyType").value = "all";
  renderReplyRows();
  $("ruleModal").classList.add("open");
}

function closeRuleModal() {
  closePartAddMenu();
  finishPartReorder();
  $("ruleModal").classList.remove("open");
}

function closeBatchGroupModal() {
  $("batchGroupModal").classList.remove("open");
}

function bindModalDismiss(backdropId, closeFn) {
  const backdrop = $(backdropId);
  backdrop.addEventListener("click", (ev) => {
    if (ev.target === backdrop) closeFn();
  });
}

function bindEscapeClose(closeFn, backdropId) {
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    if (!$(backdropId).classList.contains("open")) return;
    closeFn();
  });
}

function saveRuleModal() {
  syncReplyRowsFromDom();
  const index = parseInt($("ruleEditIndex").value, 10);
  const keyword = $("ruleKeyword").value.trim();
  if (!keyword) {
    alert("触发词不能为空");
    return;
  }

  const entries = rowsToEntries(state.replyRows);
  if (!entries.length) {
    alert("请至少添加一条有效回复");
    return;
  }

  const groups = $("ruleGroups")
    .value.split(",")
    .map((g) => g.trim())
    .filter(Boolean);

  const rule = {
    keyword,
    enabled: $("ruleEnabled").checked,
    regex: $("ruleRegex").checked,
    require_at_bot: $("ruleRequireAt").checked,
    mode: $("ruleMode").value,
    groups,
    entries,
  };
  if (state.section === "auto_detect") rule.case_sensitive = false;

  const rules = [...getRules()];
  if (index >= 0) rules[index] = rule;
  else rules.push(rule);
  setRules(rules);
  markDirty();
  closeRuleModal();
  refreshList();
}

function openBatchGroupModal() {
  if (!state.selected.size) {
    alert("请先勾选词条");
    return;
  }
  $("batchGroupCount").textContent = String(state.selected.size);
  $("batchGroupModal").classList.add("open");
}

function applyBatchGroup() {
  if (!state.selected.size) return;

  const action = $("batchGroupMode").value;
  const policy = $("batchGroupPolicy").value;
  const rawGroups = $("batchGroupIds")
    .value.split(",")
    .map((g) => g.trim())
    .filter(Boolean);

  const rules = [...getRules()];
  for (const index of state.selected) {
    const rule = rules[index];
    if (!rule) continue;

    if (action === "global_disable") {
      rule.enabled = false;
      continue;
    }

    rule.enabled = true;
    if (action === "global_enable") {
      rule.mode = "blacklist";
      rule.groups = [];
      continue;
    }

    if (action === "replace") {
      rule.mode = policy;
      rule.groups = [...rawGroups];
      continue;
    }

    if (action === "append") {
      rule.mode = policy;
      const merged = new Set([...(rule.groups || []), ...rawGroups]);
      rule.groups = [...merged];
    }
  }

  setRules(rules);
  markDirty();
  closeBatchGroupModal();
  refreshList();
  setStatus(`已批量更新 ${state.selected.size} 个词条的群策略`);
}

async function loadData() {
  setStatus("正在加载词库…");
  const res = await fetch("/api/data");
  const payload = await res.json();
  if (!payload.ok) throw new Error(payload.error || "加载失败");
  state.data = payload.data;
  state.path = payload.path;
  state.selected.clear();
  state.dirty = false;
  state.page = 1;
  rebuildSearchHay();
  $("pathMeta").textContent = `数据文件: ${payload.path}`;
  const total = (state.data.command_triggered?.length || 0) + (state.data.auto_detect?.length || 0);
  setStatus(`已加载 ${total} 条词条`);
  refreshList();
}

async function saveData() {
  const res = await fetch("/api/data", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ data: state.data }),
  });
  const payload = await res.json();
  if (!payload.ok) throw new Error(payload.error || "保存失败");
  state.dirty = false;
  setStatus(`已保存 · 关键词 ${payload.keyword_count} · 检测词 ${payload.detect_count}`);
}

function batchUpdate(mutator) {
  if (!state.selected.size) {
    alert("请先勾选词条");
    return;
  }
  const rules = [...getRules()];
  for (const index of state.selected) {
    if (rules[index]) mutator(rules[index]);
  }
  setRules(rules);
  markDirty();
  refreshList();
}

function bindEvents() {
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.section = btn.dataset.section;
      state.selected.clear();
      state.page = 1;
      rebuildSearchHay();
      refreshList();
    });
  });

  ["filterSearch", "filterEnabled", "filterRequireAt", "filterRegex", "filterGroup"].forEach((id) => {
    $(id).addEventListener("input", () => scheduleRender(true));
    $(id).addEventListener("change", () => scheduleRender(true));
  });

  $("pageSize").addEventListener("change", (ev) => {
    state.pageSize = parseInt(ev.target.value, 10) || PAGE_SIZE_DEFAULT;
    state.page = 1;
    refreshList();
  });
  $("btnPrevPage").addEventListener("click", () => {
    if (state.page > 1) {
      state.page -= 1;
      queueRender();
    }
  });
  $("btnNextPage").addEventListener("click", () => {
    const totalPages = Math.max(1, Math.ceil(state.visibleIndices.length / state.pageSize));
    if (state.page < totalPages) {
      state.page += 1;
      queueRender();
    }
  });
  $("btnReplyPrev").addEventListener("click", () => {
    syncReplyRowsFromDom();
    if (state.replyPage > 1) {
      state.replyPage -= 1;
      renderReplyRows();
    }
  });
  $("btnReplyNext").addEventListener("click", () => {
    syncReplyRowsFromDom();
    const totalPages = Math.max(1, Math.ceil(getFilteredReplyRows().length / REPLY_PAGE_SIZE));
    if (state.replyPage < totalPages) {
      state.replyPage += 1;
      renderReplyRows();
    }
  });

  $("entryFilterReplyType").addEventListener("change", () => {
    syncReplyRowsFromDom();
    state.replyPage = 1;
    renderReplyRows();
  });

  $("partAddMenu").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button[data-part]");
    if (!btn || !state.addMenuRowId) return;
    syncReplyRowsFromDom();
    const row = findReplyRow(state.addMenuRowId);
    const partKey = btn.getAttribute("data-part");
    if (row && partKey) {
      addPartToRow(row, partKey);
      closePartAddMenu();
      renderReplyRows();
    }
  });

  document.addEventListener("click", (ev) => {
    if ($("partAddMenu").hidden) return;
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    if (target.closest("#partAddMenu") || target.closest("[data-action='toggle-add-menu']")) return;
    closePartAddMenu();
  });

  $("btnReload").addEventListener("click", () => {
    if (state.dirty && !confirm("有未保存修改，确定重新加载？")) return;
    loadData().catch((err) => setStatus(err.message, true));
  });
  $("btnSave").addEventListener("click", () => saveData().catch((err) => setStatus(err.message, true)));
  $("btnAddRule").addEventListener("click", () => openRuleModal(-1));
  $("btnRuleCancel").addEventListener("click", closeRuleModal);
  $("btnRuleClose").addEventListener("click", closeRuleModal);
  $("btnRuleSave").addEventListener("click", saveRuleModal);
  bindModalDismiss("ruleModal", closeRuleModal);
  bindEscapeClose(closeRuleModal, "ruleModal");

  $("btnBatchGroup").addEventListener("click", openBatchGroupModal);
  $("btnBatchGroupCancel").addEventListener("click", closeBatchGroupModal);
  $("btnBatchGroupClose").addEventListener("click", closeBatchGroupModal);
  $("btnBatchGroupSave").addEventListener("click", applyBatchGroup);
  bindModalDismiss("batchGroupModal", closeBatchGroupModal);
  bindEscapeClose(closeBatchGroupModal, "batchGroupModal");

  $("btnAddReplyRow").addEventListener("click", () => {
    syncReplyRowsFromDom();
    state.replyRows.push(createEmptyRow());
    renderReplyRows();
  });

  $("btnDeleteReplyRows").addEventListener("click", deleteSelectedEntries);

  $("replyRows").addEventListener("click", (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;

    const selectEntry = target.closest('[data-action="select-entry"]');
    if (selectEntry instanceof HTMLInputElement) {
      const block = selectEntry.closest(".entry-block");
      const rowId = block?.dataset.rowId;
      if (!rowId) return;
      if (selectEntry.checked) state.selectedEntries.add(rowId);
      else state.selectedEntries.delete(rowId);
      updateDeleteReplyButton();
      return;
    }

    const toggleMenu = target.closest("[data-action='toggle-add-menu']");
    if (toggleMenu) {
      syncReplyRowsFromDom();
      const block = toggleMenu.closest(".entry-block");
      const rowId = block?.dataset.rowId;
      if (!rowId) return;
      if (state.addMenuRowId === rowId && !$("partAddMenu").hidden) {
        closePartAddMenu();
      } else {
        openPartAddMenu(rowId, toggleMenu);
      }
      return;
    }

    const removePart = target.closest("[data-action='remove-part']");
    if (removePart) {
      if (!confirm("确定移除此行内容？此操作不可撤销。")) return;
      syncReplyRowsFromDom();
      const block = removePart.closest(".entry-block");
      const row = findReplyRow(block?.dataset.rowId || "");
      const partId = removePart.getAttribute("data-part-id");
      if (row && partId) {
        clearReplyPart(row, partId);
        renderReplyRows();
      }
    }
  });

  $("replyRows").addEventListener("pointerdown", (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    const handle = target.closest('[data-action="drag-handle"]');
    if (!handle) return;
    beginPartReorder(ev, handle);
  });

  document.addEventListener("pointermove", (ev) => {
    if (!state.reorder) return;
    if (state.reorder.pointerId !== ev.pointerId) return;
    handlePartReorderMove(ev);
  });

  document.addEventListener("pointerup", (ev) => {
    if (!state.reorder || state.reorder.pointerId !== ev.pointerId) return;
    finishPartReorder(true);
  });

  document.addEventListener("pointercancel", (ev) => {
    if (!state.reorder || state.reorder.pointerId !== ev.pointerId) return;
    finishPartReorder(false);
  });

  $("btnBatchEnable").addEventListener("click", () => batchUpdate((r) => (r.enabled = true)));
  $("btnBatchDisable").addEventListener("click", () => batchUpdate((r) => (r.enabled = false)));
  $("btnBatchRequireAtOn").addEventListener("click", () => batchUpdate((r) => (r.require_at_bot = true)));
  $("btnBatchRequireAtOff").addEventListener("click", () => batchUpdate((r) => (r.require_at_bot = false)));
  $("btnBatchDelete").addEventListener("click", () => {
    if (!state.selected.size) return alert("请先勾选词条");
    if (!confirm(`确定删除 ${state.selected.size} 个词条？此操作不可撤销。`)) return;
    const keep = getRules().filter((_, i) => !state.selected.has(i));
    setRules(keep);
    state.selected.clear();
    markDirty();
    refreshList();
  });

  $("checkAll").addEventListener("change", (ev) => {
    const checked = ev.target.checked;
    for (const index of getPagedVisibleRules().map(({ index }) => index)) {
      if (checked) state.selected.add(index);
      else state.selected.delete(index);
    }
    queueRender();
  });

  $("ruleTableBody").addEventListener("click", (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    const edit = target.getAttribute("data-edit");
    const del = target.getAttribute("data-del");
    const select = target.getAttribute("data-select");
    if (edit !== null) return openRuleModal(parseInt(edit, 10));
    if (del !== null) {
      const index = parseInt(del, 10);
      if (!confirm("确定删除该词条？此操作不可撤销。")) return;
      setRules(getRules().filter((_, i) => i !== index));
      state.selected.delete(index);
      markDirty();
      refreshList();
      return;
    }
    if (select !== null && target instanceof HTMLInputElement) {
      const index = parseInt(select, 10);
      if (target.checked) state.selected.add(index);
      else state.selected.delete(index);
      queueRender();
    }
  });

  $("btnExport").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(state.data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "keywords.json";
    a.click();
    URL.revokeObjectURL(url);
  });

  $("importFile").addEventListener("change", async (ev) => {
    const file = ev.target.files?.[0];
    if (!file) return;
    try {
      state.data = JSON.parse(await file.text());
      state.selected.clear();
      state.page = 1;
      rebuildSearchHay();
      markDirty();
      refreshList();
      setStatus(`已导入 ${file.name}，请检查后保存`);
    } catch (err) {
      setStatus(`导入失败: ${err.message}`, true);
    } finally {
      ev.target.value = "";
    }
  });

  window.addEventListener("beforeunload", (ev) => {
    if (state.dirty) ev.preventDefault();
  });
}

bindEvents();
loadData().catch((err) => setStatus(err.message, true));
