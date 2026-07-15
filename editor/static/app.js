const REPLY_KINDS = {
  text: { label: "文本", prefix: "" },
  music: { label: "音乐", prefix: "" },
  image: { label: "图片", prefix: "images/" },
  voice: { label: "语音", prefix: "records/" },
  emoji: { label: "表情", prefix: "emojis/" },
  video: { label: "视频", prefix: "videos/" },
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
  aliasRows: [],
  aliasSeq: 0,
  rowSeq: 0,
  page: 1,
  pageSize: PAGE_SIZE_DEFAULT,
  replyPage: 1,
  visibleIndices: [],
  searchHay: [],
  filterTimer: null,
  renderQueued: false,
  addMenuTarget: null,
  selectedEntries: new Set(),
  messageSeq: 0,
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
    videos: [],
    music_cards: [],
  };
}

function emptyRule() {
  return {
    keyword: "",
    aliases: [],
    regex: false,
    enabled: true,
    mode: "whitelist",
    groups: [],
    require_at_bot: false,
    entries: [],
  };
}

function formatAliases(rule) {
  const aliases = Array.isArray(rule?.aliases) ? rule.aliases.filter(Boolean) : [];
  return aliases.join("、");
}

function newAliasId() {
  state.aliasSeq += 1;
  return `alias-${state.aliasSeq}`;
}

function aliasesToRows(aliases) {
  return (Array.isArray(aliases) ? aliases : [])
    .map((text) => String(text || "").trim())
    .filter(Boolean)
    .map((text) => ({ id: newAliasId(), text }));
}

function syncAliasRowsFromDom() {
  const root = $("aliasRows");
  if (!root) return;
  for (const row of state.aliasRows) {
    const input = root.querySelector(`[data-alias-id="${row.id}"] input[data-alias-text]`);
    if (input) row.text = input.value;
  }
}

function collectAliases(keyword = "") {
  syncAliasRowsFromDom();
  const primary = String(keyword || "").trim().toLowerCase();
  const out = [];
  const seen = new Set();
  for (const row of state.aliasRows) {
    const text = String(row.text || "").trim();
    if (!text) continue;
    const key = text.toLowerCase();
    if (primary && key === primary) continue;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(text);
  }
  return out;
}

function renderAliasRows() {
  const root = $("aliasRows");
  if (!root) return;
  if (!state.aliasRows.length) {
    root.innerHTML = `<div class="alias-empty">暂无别名，点击右上角「添加别名」</div>`;
    return;
  }
  root.innerHTML = state.aliasRows
    .map(
      (row) => `
      <div class="alias-row" data-alias-id="${row.id}">
        <input data-alias-text placeholder="别名" value="${escapeHtml(row.text || "")}" />
        <button type="button" class="danger" data-alias-del="${row.id}">删除</button>
      </div>`
    )
    .join("");
}

function addAliasRow(text = "") {
  syncAliasRowsFromDom();
  state.aliasRows.push({ id: newAliasId(), text: String(text || "") });
  renderAliasRows();
  const last = $("aliasRows")?.querySelector(".alias-row:last-child input[data-alias-text]");
  if (last) last.focus();
}

function removeAliasRow(id) {
  syncAliasRowsFromDom();
  state.aliasRows = state.aliasRows.filter((row) => row.id !== id);
  renderAliasRows();
}

function newRowId() {
  state.rowSeq += 1;
  return `row-${state.rowSeq}`;
}

function newMessageId() {
  state.messageSeq += 1;
  return `msg-${state.messageSeq}`;
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

function mediaDirPrefix(kind) {
  return REPLY_KINDS[kind]?.prefix || "";
}

/** 编辑器只展示/编辑文件名；目录由类型决定，保存时仍只写 basename。 */
function toMediaFilename(kind, rawPath) {
  const text = String(rawPath || "").trim().replace(/\\/g, "/");
  if (!text) return "";
  const prefix = mediaDirPrefix(kind);
  if (prefix && text.toLowerCase().startsWith(prefix.toLowerCase())) {
    return basename(text.slice(prefix.length));
  }
  return basename(text);
}

function normalizeMediaPath(kind, rawPath) {
  const file = toMediaFilename(kind, rawPath);
  if (!file) return "";
  return `${mediaDirPrefix(kind)}${file}`;
}

function entryHasPayload(entry) {
  if (entry.parts?.length) {
    return entry.parts.some((part) => {
      if (part.segments?.length) {
        return part.segments.some((seg) => {
          if (seg.type === "text") return !!(seg.text || "").trim();
          if (seg.type === "music") return !!(seg.id || "").trim();
          if (seg.type === "face") return seg.id != null && String(seg.id).trim() !== "";
          if (seg.type === "image" || seg.type === "voice" || seg.type === "emoji" || seg.type === "video") {
            return !!(seg.file || "").trim();
          }
          return false;
        });
      }
      if (part.type === "text") return !!(part.text || "").trim();
      if (part.type === "music") return !!(part.id || "").trim();
      if (part.type === "face") return part.id != null && String(part.id).trim() !== "";
      if (part.type === "image" || part.type === "voice" || part.type === "emoji" || part.type === "video") {
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
      entry.videos?.length ||
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

function mediaFilesToField(items, kind) {
  return (items || [])
    .map((item) => toMediaFilename(kind, item?.file || ""))
    .filter(Boolean)
    .join(", ");
}

function fieldToMediaFiles(raw, kind) {
  const files = [];
  for (const part of splitPaths(raw)) {
    const file = toMediaFilename(kind, part);
    if (file) files.push({ file });
  }
  return files;
}

const PART_TYPES = [
  { key: "text", label: "文本", addLabel: "文本", placeholder: "回复文本，换行可用 \\r\\n" },
  { key: "image", label: "图片", addLabel: "图片", placeholder: "a.jpg" },
  { key: "voice", label: "语音", addLabel: "语音", placeholder: "a.silk" },
  { key: "emoji", label: "表情", addLabel: "表情", placeholder: "a.gif" },
  { key: "video", label: "视频", addLabel: "视频", placeholder: "a.mp4" },
  { key: "qqface", label: "QQ表情", addLabel: "QQ表情", placeholder: "face id，如 13" },
  { key: "music", label: "音乐", addLabel: "音乐" },
];

function partTypeLabel(type) {
  return PART_TYPES.find((p) => p.key === type)?.label || type;
}

function createEmptyMessage() {
  return {
    id: newMessageId(),
    segments: [],
  };
}

function createEmptyPart(type) {
  return {
    id: newPartId(),
    type,
    text: "",
    paths: "",
    platform: "163",
    musicId: "",
    faceId: "",
  };
}

function partHasContent(part) {
  if (!part) return false;
  if (part.type === "text") return !!(part.text || "").trim();
  if (part.type === "music") return !!(part.musicId || "").trim();
  if (part.type === "qqface") return String(part.faceId ?? "").trim() !== "";
  if (part.type === "image" || part.type === "voice" || part.type === "emoji" || part.type === "video") {
    return !!(part.paths || "").trim();
  }
  return false;
}

function entryPartFromJson(part) {
  const rawType = part.type || "text";
  const type = rawType === "face" ? "qqface" : rawType;
  const rowPart = createEmptyPart(type);
  if (type === "text") rowPart.text = encodeTextForEditor(part.text || "");
  if (type === "image" || type === "voice" || type === "emoji" || type === "video") {
    rowPart.paths = toMediaFilename(type, part.file || "");
  }
  if (type === "qqface") rowPart.faceId = part.id != null ? String(part.id) : "";
  if (type === "music") {
    rowPart.platform = part.platform || "163";
    rowPart.musicId = part.id || "";
  }
  return rowPart;
}

function legacySegmentsFromEntry(entry) {
  const segments = [];
  const rawText = String(entry.text ?? "");
  if (rawText.trim()) {
    segments.push({ ...createEmptyPart("text"), text: encodeTextForEditor(rawText) });
  }
  for (const face of entry.faces || []) {
    if (face?.id != null && String(face.id).trim() !== "") {
      segments.push({ ...createEmptyPart("qqface"), faceId: String(face.id) });
    }
  }
  for (const img of entry.images || []) {
    if (img?.file) {
      segments.push({
        ...createEmptyPart("image"),
        paths: toMediaFilename("image", img.file),
      });
    }
  }
  for (const voice of entry.records || []) {
    if (voice?.file) {
      segments.push({
        ...createEmptyPart("voice"),
        paths: toMediaFilename("voice", voice.file),
      });
    }
  }
  for (const emoji of entry.emojis || []) {
    if (emoji?.file) {
      segments.push({
        ...createEmptyPart("emoji"),
        paths: toMediaFilename("emoji", emoji.file),
      });
    }
  }
  for (const video of entry.videos || []) {
    if (video?.file) {
      segments.push({
        ...createEmptyPart("video"),
        paths: toMediaFilename("video", video.file),
      });
    }
  }
  const music = entry.music_cards?.[0];
  if (music?.id) {
    segments.push({
      ...createEmptyPart("music"),
      platform: music.platform || "163",
      musicId: music.id || "",
    });
  }
  return segments;
}

function entryMessagesFromJson(entry) {
  const hasParts = Array.isArray(entry.parts) && entry.parts.length > 0;
  if (hasParts) {
    const messages = entry.parts.map((part) => {
      if (part.segments?.length) {
        return {
          id: newMessageId(),
          segments: part.segments.map(entryPartFromJson),
        };
      }
      return {
        id: newMessageId(),
        segments: [entryPartFromJson(part)],
      };
    });
    const flatText = String(entry.text ?? "").trim();
    if (flatText && !messages.some((msg) => (msg.segments || []).some((seg) => seg.type === "text" && seg.text))) {
      const first = messages[0];
      if (first) {
        first.segments.unshift({
          ...createEmptyPart("text"),
          text: encodeTextForEditor(String(entry.text ?? "")),
        });
      } else {
        messages.push({
          id: newMessageId(),
          segments: [{ ...createEmptyPart("text"), text: encodeTextForEditor(String(entry.text ?? "")) }],
        });
      }
    }
    return messages;
  }
  const segments = legacySegmentsFromEntry(entry);
  if (!segments.length) return [];
  return [{ id: newMessageId(), segments }];
}

function collectEntrySegments(entry) {
  if (entry.parts?.length) {
    const out = [];
    for (const part of entry.parts) {
      if (part.segments?.length) out.push(...part.segments);
      else out.push(part);
    }
    return out;
  }
  return legacySegmentsFromEntry(entry).map(partToJson);
}

function entryHasReplyType(entry, type) {
  if (type === "all") return true;
  const segments = collectEntrySegments(entry);
  if (type === "text") return segments.some((p) => p.type === "text" && (p.text || "").trim());
  if (type === "image") return segments.some((p) => p.type === "image");
  if (type === "voice") return segments.some((p) => p.type === "voice");
  if (type === "emoji") return segments.some((p) => p.type === "emoji");
  if (type === "video") return segments.some((p) => p.type === "video");
  if (type === "music") return segments.some((p) => p.type === "music");
  return true;
}
function partToJson(part) {
  const out = { type: part.type === "qqface" ? "face" : part.type };
  if (part.type === "text") out.text = decodeTextFromEditor(part.text);
  if (part.type === "image" || part.type === "voice" || part.type === "emoji" || part.type === "video") {
    const file = toMediaFilename(part.type, splitPaths(part.paths)[0] || "");
    if (file) out.file = file;
  }
  if (part.type === "qqface") {
    const faceId = String(part.faceId ?? "").trim();
    if (faceId !== "") out.id = Number.parseInt(faceId, 10);
  }
  if (part.type === "music") {
    out.platform = String(part.platform || "163").trim();
    out.id = String(part.musicId || "").trim();
  }
  return out;
}

function ruleHasReplyType(rule, type) {
  if (type === "all") return true;
  return (rule.entries || []).some((entry) => entryHasReplyType(entry, type));
}

function messageHasPayload(message) {
  return (message?.segments || []).some(partHasContent);
}

function rowHasPayload(row) {
  return (row.messages || []).some(messageHasPayload);
}

function entryToRow(entry) {
  const messages = entryMessagesFromJson(entry);
  return {
    id: newRowId(),
    kind: "entry",
    weight: clampWeight(entry.weight),
    probability: clampProbability(entry.probability),
    messages: messages.length ? messages : [createEmptyMessage()],
  };
}

function entriesToRows(entries) {
  const rows = (entries || []).map(entryToRow).filter(rowHasPayload);
  return rows.length ? rows : [createEmptyRow()];
}

function rowToEntry(row) {
  const parts = [];
  for (const message of row.messages || []) {
    const segments = (message.segments || []).filter(partHasContent).map(partToJson);
    if (segments.length) parts.push({ segments });
  }
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
    videos: [],
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

function describeMessageSegments(message) {
  const labels = (message.segments || []).filter(partHasContent).map((p) => partTypeLabel(p.type));
  return labels.length ? labels.join("→") : "空";
}

function describeRowKind(row) {
  const messages = (row.messages || []).filter(messageHasPayload);
  if (!messages.length) return "空";
  if (messages.length === 1) return describeMessageSegments(messages[0]);
  return messages.map((msg, index) => `消息${index + 1}[${describeMessageSegments(msg)}]`).join(" | ");
}

function createEmptyRow() {
  return {
    id: newRowId(),
    kind: "entry",
    weight: 100,
    probability: 100,
    messages: [createEmptyMessage()],
  };
}

function rowHasContentType(row, type) {
  if (type === "all") return true;
  return (row.messages || []).some((message) =>
    (message.segments || []).some((part) => {
      if (type === "text") return part.type === "text" && !!(part.text || "").trim();
      if (type === "image") return part.type === "image" && !!(part.paths || "").trim();
      if (type === "voice") return part.type === "voice" && !!(part.paths || "").trim();
      if (type === "emoji") return part.type === "emoji" && !!(part.paths || "").trim();
      if (type === "video") return part.type === "video" && !!(part.paths || "").trim();
      if (type === "music") return part.type === "music" && !!(part.musicId || "").trim();
      return false;
    })
  );
}

function findMessage(row, messageId) {
  return (row?.messages || []).find((message) => message.id === messageId);
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
    const encoded = encodeTextForEditor(part.text || "");
    return `<textarea rows="2" data-field="text" class="text-segment-input" placeholder="${meta?.placeholder || ""}">${escapeHtml(encoded)}</textarea>`;
  }
  if (part.type === "qqface") {
    return `<input type="number" min="0" step="1" data-field="faceId" value="${escapeHtml(part.faceId || "")}" placeholder="${meta?.placeholder || ""}" />`;
  }
  if (part.type === "image" || part.type === "voice" || part.type === "emoji" || part.type === "video") {
    const prefix = mediaDirPrefix(part.type);
    const filename = toMediaFilename(part.type, part.paths || "");
    return `
      <div class="media-path-field" title="只需填写文件名；目录由类型固定为 ${prefix}">
        <span class="media-path-prefix">${escapeHtml(prefix)}</span>
        <input type="text" data-field="paths" value="${escapeHtml(filename)}" placeholder="${meta?.placeholder || ""}" spellcheck="false" />
      </div>`;
  }
  return `<input type="text" data-field="paths" value="${escapeHtml(part.paths || "")}" placeholder="${meta?.placeholder || ""}" />`;
}

function renderSegmentRow(segment, options = {}) {
  const { isLast = false, messageId = "" } = options;
  const addBtn = isLast
    ? `<button type="button" class="btn-add-part" data-action="toggle-add-menu" data-message-id="${messageId}" title="在本条消息内追加内容">+</button>`
    : `<span class="action-placeholder" aria-hidden="true"></span>`;
  return `
    <div class="reply-row part-row" data-part-id="${segment.id}">
      <button type="button" class="drag-handle" data-action="drag-handle" title="长按拖动排序" aria-label="拖动排序">⠿</button>
      <div class="kind-badge">${partTypeLabel(segment.type)}</div>
      <div class="part-content">${renderPartContent(segment)}</div>
      <div class="row-actions">
        <button type="button" class="btn-icon secondary" data-action="remove-part" data-part-id="${segment.id}" title="移除此段">×</button>
        ${addBtn}
      </div>
    </div>`;
}

function renderMessageGroup(message, messageIndex, rowId) {
  const segments = message.segments || [];
  const segmentRows = segments.length
    ? segments
        .map((segment, index) =>
          renderSegmentRow(segment, {
            isLast: index === segments.length - 1,
            messageId: message.id,
          })
        )
        .join("")
    : `
      <div class="reply-row part-row is-empty">
        <div class="drag-handle-placeholder"></div>
        <div class="kind-badge">—</div>
        <div class="hint-inline">点击 + 在本条消息内添加内容</div>
        <div class="row-actions">
          <span class="action-placeholder"></span>
          <button type="button" class="btn-add-part" data-action="toggle-add-menu" data-message-id="${message.id}" title="在本条消息内追加内容">+</button>
        </div>
      </div>`;

  return `
    <div class="message-group" data-message-id="${message.id}">
      <div class="message-head">
        <span class="message-label">消息 ${messageIndex + 1}</span>
        <span class="message-summary">${escapeHtml(describeMessageSegments(message))}</span>
        <button type="button" class="link-btn" data-action="remove-message" data-message-id="${message.id}" title="删除本条聊天消息">删除消息</button>
      </div>
      <div class="message-body">${segmentRows}</div>
    </div>`;
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

  const messages = row.messages || [];
  const messageGroups = (messages.length ? messages : [createEmptyMessage()])
    .map((message, index) => renderMessageGroup(message, index, row.id))
    .join("");

  return `
    <div class="entry-block" data-row-id="${row.id}">
      <div class="entry-block-layout">
        ${rail}
        <div class="entry-body">
          ${messageGroups}
          <div class="message-actions">
            <button type="button" class="btn-add-message secondary" data-action="add-message" title="再发一条独立消息">+ 添加下一条消息</button>
          </div>
        </div>
      </div>
    </div>`;
}

function summarizeEntry(entry) {
  const parts = [];
  const weight = clampWeight(entry.weight);
  const probability = clampProbability(entry.probability);
  if (weight !== 100) parts.push(`权重${weight}`);
  if (probability !== 100) parts.push(`${probability}%`);
  const messageParts = entry.parts?.length ? entry.parts : [];
  if (messageParts.length) {
    for (const [index, messagePart] of messageParts.entries()) {
      const segments = messagePart.segments?.length
        ? messagePart.segments
        : messagePart.type
          ? [messagePart]
          : [];
      const preview = [];
      for (const segment of segments) {
        if (segment.type === "text" && segment.text) preview.push(encodeTextForEditor(segment.text).slice(0, 32));
        if (segment.type === "image" && segment.file) preview.push(`[图 ${segment.file}]`);
        if (segment.type === "voice" && segment.file) preview.push(`[语音 ${segment.file}]`);
        if (segment.type === "emoji" && segment.file) preview.push(`[表情 ${segment.file}]`);
        if (segment.type === "video" && segment.file) preview.push(`[视频 ${segment.file}]`);
        if (segment.type === "face" && segment.id != null) preview.push(`[QQ表情 ${segment.id}]`);
        if (segment.type === "music" && segment.id) preview.push(`[音乐 ${segment.platform}:${segment.id}]`);
      }
      if (preview.length) {
        const label = messageParts.length > 1 ? `消息${index + 1}:` : "";
        parts.push(`${label}${preview.join("→")}`);
      }
    }
  } else {
    const ordered = collectEntrySegments(entry);
    for (const segment of ordered) {
      if (segment.type === "text" && segment.text) parts.push(encodeTextForEditor(segment.text).slice(0, 32));
      if (segment.type === "image" && segment.file) parts.push(`[图 ${segment.file}]`);
      if (segment.type === "voice" && segment.file) parts.push(`[语音 ${segment.file}]`);
      if (segment.type === "emoji" && segment.file) parts.push(`[表情 ${segment.file}]`);
      if (segment.type === "video" && segment.file) parts.push(`[视频 ${segment.file}]`);
      if (segment.type === "face" && segment.id != null) parts.push(`[QQ表情 ${segment.id}]`);
      if (segment.type === "music" && segment.id) parts.push(`[音乐 ${segment.platform}:${segment.id}]`);
    }
    if (!ordered.length && entry.text) parts.push(encodeTextForEditor(entry.text).slice(0, 36));
    if (!ordered.length && entry.images?.length) parts.push(`[图片 ${entry.images.map((x) => x.file).join(",")}]`);
    if (!ordered.length && entry.videos?.length) parts.push(`[视频 ${entry.videos.map((x) => x.file).join(",")}]`);
  }
  return parts.join(" ") || "[空]";
}

function buildSearchHay(rule) {
  const keyword = (rule.keyword || "").toLowerCase();
  const aliases = formatAliases(rule).toLowerCase();
  const entries = rule.entries || [];
  const head = [keyword, aliases].filter(Boolean).join(" ");
  if (!entries.length) return head;
  const previews = entries.slice(0, 2).map((entry) => summarizeEntry(entry)).join(" ").toLowerCase();
  return `${head} ${previews}`;
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

const toastState = { seq: 0 };

function yieldToUi() {
  return new Promise((resolve) => {
    requestAnimationFrame(() => setTimeout(resolve, 0));
  });
}

function createProgressToast(title, message = "处理中…") {
  const container = $("toastContainer");
  const id = `toast-${++toastState.seq}`;
  const el = document.createElement("article");
  el.className = "toast is-loading";
  el.dataset.toastId = id;
  el.innerHTML = `
    <div class="toast-header">
      <span class="toast-title">${escapeHtml(title)}</span>
      <button type="button" class="toast-close secondary" aria-label="关闭">×</button>
    </div>
    <p class="toast-message">${escapeHtml(message)}</p>
    <div class="toast-progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
      <div class="toast-progress-fill"></div>
    </div>
  `;

  const titleEl = el.querySelector(".toast-title");
  const messageEl = el.querySelector(".toast-message");
  const progressBar = el.querySelector('[role="progressbar"]');
  const progressFill = el.querySelector(".toast-progress-fill");
  const closeBtn = el.querySelector(".toast-close");
  let dismissTimer = null;
  let creepTimer = null;
  let currentProgress = 0;

  const clearTimers = () => {
    if (dismissTimer) {
      clearTimeout(dismissTimer);
      dismissTimer = null;
    }
    if (creepTimer) {
      clearInterval(creepTimer);
      creepTimer = null;
    }
  };

  const dismiss = (delayMs = 0) => {
    clearTimers();
    const run = () => {
      el.classList.add("is-dismissing");
      setTimeout(() => el.remove(), 220);
    };
    if (delayMs > 0) dismissTimer = setTimeout(run, delayMs);
    else run();
  };

  const setProgress = (percent, nextMessage) => {
    currentProgress = Math.max(currentProgress, Math.min(100, Math.round(percent)));
    progressFill.style.width = `${currentProgress}%`;
    progressBar.setAttribute("aria-valuenow", String(currentProgress));
    if (nextMessage) messageEl.textContent = nextMessage;
  };

  const startCreep = () => {
    creepTimer = setInterval(() => {
      if (currentProgress < 88) setProgress(currentProgress + 2);
    }, 320);
  };

  const finish = (kind, finalMessage, autoDismissMs = 2800) => {
    clearTimers();
    el.classList.remove("is-loading");
    el.classList.add(kind === "error" ? "is-error" : "is-success");
    setProgress(100, finalMessage);
    dismiss(kind === "error" ? 5200 : autoDismissMs);
  };

  closeBtn.addEventListener("click", () => dismiss());

  container.appendChild(el);
  setProgress(8);
  startCreep();

  return {
    setProgress,
    succeed(message) {
      finish("success", message);
    },
    fail(message) {
      finish("error", message);
    },
    dismiss,
  };
}

async function runWithProgressToast(title, task, { successMessage } = {}) {
  const toast = createProgressToast(title);
  try {
    const result = await task((percent, message) => toast.setProgress(percent, message));
    const finalMessage =
      typeof successMessage === "function"
        ? successMessage(result)
        : successMessage || (typeof result === "string" ? result : "操作完成");
    toast.succeed(finalMessage);
    return result;
  } catch (err) {
    toast.fail(err.message || "操作失败");
    throw err;
  }
}

function markDirty() {
  state.dirty = true;
  setStatus("有未保存的修改");
}

function encodeTextForEditor(text) {
  return String(text ?? "")
    .replace(/\r\n/g, "\\r\\n")
    .replace(/\n/g, "\\n")
    .replace(/\r/g, "\\r");
}

function decodeTextFromEditor(text) {
  return String(text ?? "")
    .replace(/\\r\\n/g, "\r\n")
    .replace(/\\n/g, "\n")
    .replace(/\\r/g, "\r");
}

function escapeAttr(text) {
  return escapeHtml(encodeTextForEditor(text))
    .replace(/\t/g, "&#9;");
}

function confirmDeleteIfNeeded(hasContent, message, onConfirm) {
  if (!hasContent || confirm(message)) onConfirm();
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

    for (const messageEl of el.querySelectorAll(".message-group[data-message-id]")) {
      const messageId = messageEl.dataset.messageId;
      const message = findMessage(existing, messageId);
      if (!message) continue;

      for (const partEl of messageEl.querySelectorAll(".part-row[data-part-id]")) {
        const partId = partEl.dataset.partId;
        const part = (message.segments || []).find((p) => p.id === partId);
        if (!part) continue;

        const textInput = partEl.querySelector('[data-field="text"]');
        if (textInput) part.text = textInput.value;

        const pathsInput = partEl.querySelector('[data-field="paths"]');
        if (pathsInput) {
          // 粘贴 images/a.jpg 时自动剥掉目录，只保留文件名
          part.paths =
            part.type === "image" || part.type === "voice" || part.type === "emoji" || part.type === "video"
              ? toMediaFilename(part.type, pathsInput.value)
              : pathsInput.value;
          if (pathsInput.value !== part.paths) pathsInput.value = part.paths;
        }

        const platformInput = partEl.querySelector('[data-field="platform"]');
        if (platformInput) part.platform = platformInput.value || "163";

        const musicInput = partEl.querySelector('[data-field="musicId"]');
        if (musicInput) part.musicId = musicInput.value;

        const faceInput = partEl.querySelector('[data-field="faceId"]');
        if (faceInput) part.faceId = faceInput.value;
      }
    }
  }
}

function findReplyRow(rowId) {
  return state.replyRows.find((r) => r.id === rowId);
}

function clearReplySegment(row, messageId, partId) {
  const message = findMessage(row, messageId);
  if (!message) return;
  message.segments = (message.segments || []).filter((p) => p.id !== partId);
}

function removeMessageFromRow(row, messageId) {
  row.messages = (row.messages || []).filter((message) => message.id !== messageId);
  if (!row.messages.length) row.messages = [createEmptyMessage()];
}

function closePartAddMenu() {
  state.addMenuTarget = null;
  const menu = $("partAddMenu");
  menu.hidden = true;
}

function openPartAddMenu(rowId, messageId, anchorEl) {
  const row = findReplyRow(rowId);
  const message = findMessage(row, messageId);
  if (!row || !message) return;

  state.addMenuTarget = { rowId, messageId };
  const menu = $("partAddMenu");
  const rect = anchorEl.getBoundingClientRect();
  menu.style.left = `${Math.min(rect.left, window.innerWidth - 160)}px`;
  menu.style.top = `${rect.bottom + 6}px`;
  menu.hidden = false;

  for (const btn of menu.querySelectorAll("button[data-part]")) {
    btn.hidden = false;
  }
}

function addSegmentToMessage(row, messageId, partKey) {
  const message = findMessage(row, messageId);
  if (!message) return;
  if (!message.segments) message.segments = [];
  message.segments.push(createEmptyPart(partKey));
}

function addMessageToRow(row) {
  if (!row.messages) row.messages = [];
  row.messages.push(createEmptyMessage());
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

function reorderSegmentInMessage(message, fromIndex, toIndex) {
  const segments = message.segments || [];
  if (fromIndex < 0 || toIndex < 0 || fromIndex >= segments.length || toIndex >= segments.length) return;
  if (fromIndex === toIndex) return;
  const [moved] = segments.splice(fromIndex, 1);
  segments.splice(toIndex, 0, moved);
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
  const messageGroup = handleEl.closest(".message-group");
  const block = handleEl.closest(".entry-block");
  const body = messageGroup?.querySelector(".message-body");
  if (!partRow || !block || !body || !messageGroup) return;

  const rowId = block.dataset.rowId;
  const messageId = messageGroup.dataset.messageId;
  const partId = partRow.dataset.partId;
  if (!rowId || !messageId || !partId) return;

  ev.preventDefault();
  if (state.reorder?.timer) clearTimeout(state.reorder.timer);

  state.reorder = {
    rowId,
    messageId,
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
    const message = findMessage(row, reorder.messageId);
    if (message) {
      const toIndex = Math.min(reorder.targetIndex, Math.max(0, (message.segments || []).length - 1));
      reorderSegmentInMessage(message, reorder.fromIndex, toIndex);
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
      <td><strong>${escapeHtml(rule.keyword || "")}</strong>${
        formatAliases(rule)
          ? `<div class="entry-preview">别名: ${escapeHtml(formatAliases(rule))}</div>`
          : ""
      }${rule.regex ? '<span class="badge">正则</span>' : ""}</td>
      <td>
        <span class="badge ${rule.enabled ? "on" : "off"}">${rule.enabled ? "启用" : "禁用"}</span>
        ${rule.require_at_bot ? '<span class="badge on">需@</span>' : ""}
      </td>
      <td>${escapeHtml(describeGroups(rule))}</td>
      <td>${summarizeEntriesForList(rule)}</td>
      <td>
        <button class="secondary" data-edit="${index}">编辑</button>
        <button class="secondary" data-copy="${index}">创建副本</button>
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
  state.aliasRows = aliasesToRows(rule.aliases || []);
  renderAliasRows();
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

function openRuleCopyModal(index) {
  const source = getRules()[index];
  if (!source) return;
  const copy = structuredClone(source);
  copy.keyword = "";
  copy.aliases = [];
  $("ruleEditIndex").value = "-1";
  $("ruleModalTitle").textContent = "创建副本（请填写触发词后保存）";
  $("ruleKeyword").value = "";
  state.aliasRows = [];
  renderAliasRows();
  $("ruleEnabled").checked = !!copy.enabled;
  $("ruleRegex").checked = !!copy.regex;
  $("ruleRequireAt").checked = !!copy.require_at_bot;
  $("ruleMode").value = copy.mode || "whitelist";
  $("ruleGroups").value = (copy.groups || []).join(",");
  state.replyRows = entriesToRows(copy.entries || []);
  state.replyPage = 1;
  state.selectedEntries.clear();
  if ($("entryFilterReplyType")) $("entryFilterReplyType").value = "all";
  renderReplyRows();
  $("ruleModal").classList.add("open");
  $("ruleKeyword").focus();
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

  const aliases = collectAliases(keyword);

  const rule = {
    keyword,
    aliases,
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

async function loadData({ toastTitle = "加载词库" } = {}) {
  return runWithProgressToast(
    toastTitle,
    async (report) => {
      report(12, "连接服务器…");
      const res = await fetch("/api/data");
      report(62, "解析词库数据…");
      const payload = await res.json();
      if (!payload.ok) throw new Error(payload.error || "加载失败");
      report(86, "刷新列表…");
      state.data = payload.data;
      state.path = payload.path;
      state.selected.clear();
      state.dirty = false;
      state.page = 1;
      rebuildSearchHay();
      $("pathMeta").textContent = `数据文件: ${payload.path}`;
      refreshList();
      return payload;
    },
    {
      successMessage: (payload) => {
        const total =
          (state.data.command_triggered?.length || 0) + (state.data.auto_detect?.length || 0);
        setStatus(`已加载 ${total} 条词条`);
        return `已加载 ${total} 条词条`;
      },
    },
  );
}

async function saveData() {
  return runWithProgressToast(
    "保存到磁盘",
    async (report) => {
      report(14, "序列化词库…");
      await yieldToUi();
      const body = JSON.stringify({ data: state.data });
      report(38, "正在写入磁盘…");
      const res = await fetch("/api/data", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body,
      });
      report(72, "等待服务器确认…");
      const payload = await res.json();
      if (!payload.ok) throw new Error(payload.error || "保存失败");
      report(92, "更新状态…");
      state.dirty = false;
      return payload;
    },
    {
      successMessage: (payload) => {
        const text = `已保存 · 关键词 ${payload.keyword_count} · 检测词 ${payload.detect_count}`;
        setStatus(text);
        return text;
      },
    },
  );
}

function summarizeMergeReport(report) {
  if (!report || typeof report !== "object") return "";
  const parts = [];
  for (const [section, stats] of Object.entries(report)) {
    const label = section === "command_triggered" ? "关键词" : "检测词";
    parts.push(
      `${label} ${stats.before}→${stats.after}（合并组 ${stats.groups_merged}，去掉 ${stats.rules_removed}）`
    );
  }
  return parts.join("；");
}

async function mergeDuplicateRules() {
  if (state.dirty && !confirm("有未保存修改。合并会直接改磁盘上的 keywords.json 并重新加载，未保存改动将丢失。继续？")) {
    return;
  }
  if (
    !confirm(
      "将把「回复内容 + 群策略等行为相同」的词条合并：保留一条主触发词，其余写入别名。\n会先自动备份 keywords.json.bak_merge_* 。\n确定继续？"
    )
  ) {
    return;
  }

  return runWithProgressToast(
    "合并重复词条",
    async (report) => {
      report(18, "备份并合并…");
      const res = await fetch("/api/merge-duplicates", { method: "POST" });
      report(70, "读取合并结果…");
      const payload = await res.json();
      if (!payload.ok) throw new Error(payload.error || "合并失败");
      report(88, "刷新列表…");
      state.data = payload.data;
      state.path = payload.path || state.path;
      state.selected.clear();
      state.dirty = false;
      state.page = 1;
      rebuildSearchHay();
      if (payload.path) $("pathMeta").textContent = `数据文件: ${payload.path}`;
      refreshList();
      return payload;
    },
    {
      successMessage: (payload) => {
        const detail = summarizeMergeReport(payload.report);
        const text = detail
          ? `合并完成 · ${detail} · 备份 ${payload.backup || ""}`
          : `合并完成 · 关键词 ${payload.keyword_count} · 检测词 ${payload.detect_count}`;
        setStatus(text);
        return text;
      },
    },
  );
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

function selectAllRules() {
  const rules = getRules();
  if (!rules.length) {
    setStatus("当前分类下没有词条");
    return;
  }
  const total = rules.length;
  let selectedCount = 0;
  for (let index = 0; index < total; index += 1) {
    if (state.selected.has(index)) selectedCount += 1;
  }

  if (selectedCount === 0) {
    for (let index = 0; index < total; index += 1) {
      state.selected.add(index);
    }
    queueRender();
    setStatus(`已全选 ${total} 条词条`);
    return;
  }

  if (selectedCount === total) {
    state.selected.clear();
    queueRender();
    setStatus("已取消全选");
    return;
  }

  for (let index = 0; index < total; index += 1) {
    if (state.selected.has(index)) state.selected.delete(index);
    else state.selected.add(index);
  }
  queueRender();
  setStatus(`已反选（当前选中 ${state.selected.size} 条）`);
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
    const target = state.addMenuTarget;
    if (!btn || !target) return;
    syncReplyRowsFromDom();
    const row = findReplyRow(target.rowId);
    const partKey = btn.getAttribute("data-part");
    if (row && partKey) {
      addSegmentToMessage(row, target.messageId, partKey);
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
    loadData({ toastTitle: "重新加载" }).catch((err) => setStatus(err.message, true));
  });
  $("btnSave").addEventListener("click", () => saveData().catch((err) => setStatus(err.message, true)));
  $("btnMergeDupes").addEventListener("click", () =>
    mergeDuplicateRules().catch((err) => setStatus(err.message, true))
  );
  $("btnAddRule").addEventListener("click", () => openRuleModal(-1));
  $("btnRuleCancel").addEventListener("click", closeRuleModal);
  $("btnRuleClose").addEventListener("click", closeRuleModal);
  $("btnRuleSave").addEventListener("click", saveRuleModal);
  bindModalDismiss("ruleModal", closeRuleModal);
  bindEscapeClose(closeRuleModal, "ruleModal");

  $("btnBatchGroup").addEventListener("click", openBatchGroupModal);
  $("btnSelectAll").addEventListener("click", selectAllRules);
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

  $("btnAddAlias").addEventListener("click", () => addAliasRow(""));
  $("aliasRows").addEventListener("click", (ev) => {
    const target = ev.target;
    if (!(target instanceof HTMLElement)) return;
    const del = target.getAttribute("data-alias-del");
    if (del !== null) removeAliasRow(del);
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
      const messageGroup = toggleMenu.closest(".message-group");
      const rowId = block?.dataset.rowId;
      const messageId = toggleMenu.getAttribute("data-message-id") || messageGroup?.dataset.messageId;
      if (!rowId || !messageId) return;
      if (
        state.addMenuTarget?.rowId === rowId &&
        state.addMenuTarget?.messageId === messageId &&
        !$("partAddMenu").hidden
      ) {
        closePartAddMenu();
      } else {
        openPartAddMenu(rowId, messageId, toggleMenu);
      }
      return;
    }

    const addMessage = target.closest("[data-action='add-message']");
    if (addMessage) {
      syncReplyRowsFromDom();
      const block = addMessage.closest(".entry-block");
      const row = findReplyRow(block?.dataset.rowId || "");
      if (row) {
        addMessageToRow(row);
        renderReplyRows();
      }
      return;
    }

    const removeMessageBtn = target.closest("[data-action='remove-message']");
    if (removeMessageBtn) {
      syncReplyRowsFromDom();
      const block = removeMessageBtn.closest(".entry-block");
      const row = findReplyRow(block?.dataset.rowId || "");
      const messageId = removeMessageBtn.getAttribute("data-message-id");
      const message = findMessage(row, messageId);
      confirmDeleteIfNeeded(messageHasPayload(message), "确定删除本条聊天消息？此操作不可撤销。", () => {
        if (row && messageId) {
          removeMessageFromRow(row, messageId);
          renderReplyRows();
        }
      });
      return;
    }

    const removePart = target.closest("[data-action='remove-part']");
    if (removePart) {
      syncReplyRowsFromDom();
      const block = removePart.closest(".entry-block");
      const messageGroup = removePart.closest(".message-group");
      const row = findReplyRow(block?.dataset.rowId || "");
      const messageId = messageGroup?.dataset.messageId;
      const partId = removePart.getAttribute("data-part-id");
      const message = findMessage(row, messageId);
      const part = (message?.segments || []).find((p) => p.id === partId);
      confirmDeleteIfNeeded(partHasContent(part), "确定移除此段内容？此操作不可撤销。", () => {
        if (row && messageId && partId) {
          clearReplySegment(row, messageId, partId);
          renderReplyRows();
        }
      });
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
    const copy = target.getAttribute("data-copy");
    const select = target.getAttribute("data-select");
    if (edit !== null) return openRuleModal(parseInt(edit, 10));
    if (copy !== null) return openRuleCopyModal(parseInt(copy, 10));
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
    runWithProgressToast(
      "导出 JSON",
      async (report) => {
        report(18, "准备词库数据…");
        await yieldToUi();
        report(48, "生成 JSON 文件…");
        const blob = new Blob([JSON.stringify(state.data, null, 2)], { type: "application/json" });
        report(78, "触发下载…");
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "keywords.json";
        a.click();
        URL.revokeObjectURL(url);
        return "keywords.json";
      },
      { successMessage: (name) => {
        const text = `已导出 ${name}`;
        setStatus(text);
        return text;
      } },
    ).catch((err) => setStatus(err.message, true));
  });

  $("importFile").addEventListener("change", async (ev) => {
    const file = ev.target.files?.[0];
    if (!file) return;
    try {
      await runWithProgressToast(
        "导入 JSON",
        async (report) => {
          report(16, `读取 ${file.name}…`);
          const text = await file.text();
          report(52, "解析 JSON…");
          await yieldToUi();
          const data = JSON.parse(text);
          report(84, "刷新编辑器…");
          state.data = data;
          state.selected.clear();
          state.page = 1;
          rebuildSearchHay();
          markDirty();
          refreshList();
          return file.name;
        },
        {
          successMessage: (name) => {
            const text = `已导入 ${name}，请检查后保存`;
            setStatus(text);
            return text;
          },
        },
      );
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
loadData({ toastTitle: "加载词库" }).catch((err) => setStatus(err.message, true));
