"""关键词回复插件（MaiBot SDK 2.x）。

由 AstrBot 插件 ``astrbot_plugin_keywords_reply`` 迁移而来，保留：

- 指令触发（关键词）与自动监听（检测词）两种模式；
- 正则匹配、群聊黑白名单、检测词冷却、大小写敏感、文本变量模板；
- 文本 / 图片 / At / 语音 / 表情 的组合回复，支持引用消息导入内容。

命令管理，数据保存在 ``ctx.paths.data_dir/keywords.json``（可外部编辑）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from maibot_sdk import Command, EventHandler, Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import EventType, HookMode, HookOrder

from .modules.matching import (
    Matcher,
    apply_group_toggle,
    describe_status,
    describe_status_brief,
    find_indices,
)
from .modules.media import (
    build_forward_messages,
    build_send_segments,
    capture_from_reply,
    capture_media_from_message_dict,
    capture_media_from_trigger,
    extract_inline_ats,
    is_management_command_message,
    sanitize_entry_text,
)
from .modules.store import KeywordsStore
from .modules.templates import is_safe_regex, strip_auto_media_text

SECTION_KEYWORD = "command_triggered"
SECTION_DETECT = "auto_detect"
SECTION_LABELS = {SECTION_KEYWORD: "关键词", SECTION_DETECT: "检测词"}
MATCH_TYPE_LABELS = {SECTION_KEYWORD: "精确匹配", SECTION_DETECT: "包含匹配"}


# ─── 配置模型 ──────────────────────────────────────────────────


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class PermissionConfig(PluginConfigBase):
    """权限配置。"""

    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 1

    whitelist: list[str] = Field(default_factory=list, description="允许管理关键词/检测词的用户 ID 列表")
    notify_permission_denied: bool = Field(default=True, description="无权限时是否发送提醒")
    allow_group_member_list_keywords: bool = Field(default=False, description="允许群成员查看关键词列表")
    allow_group_member_list_detects: bool = Field(default=False, description="允许群成员查看检测词列表")


class ReplyConfig(PluginConfigBase):
    """回复行为配置。"""

    __ui_label__ = "回复"
    __ui_icon__ = "message-circle"
    __ui_order__ = 2

    quote_reply: bool = Field(default=False, description="回复时是否引用触发消息")
    qq_forward_all_replies: bool = Field(default=False, description="多条回复时是否合并转发全部")
    case_sensitive: bool = Field(default=False, description="非正则匹配是否区分大小写")


class DetectConfig(PluginConfigBase):
    """检测词配置。"""

    __ui_label__ = "检测词"
    __ui_icon__ = "radar"
    __ui_order__ = 3

    cooldown: int = Field(default=0, description="检测词触发冷却时间（秒），0 表示不冷却")
    ignore_cooldown_on_exact_match: bool = Field(default=False, description="完全匹配时无视冷却")


class TemplateConfig(PluginConfigBase):
    """文本模板配置。"""

    __ui_label__ = "模板"
    __ui_icon__ = "code"
    __ui_order__ = 4

    enable_text_template: bool = Field(default=True, description="启用回复文本变量模板")


class MediaCacheConfig(PluginConfigBase):
    """入站媒体缓存配置。"""

    __ui_label__ = "媒体缓存"
    __ui_icon__ = "database"
    __ui_order__ = 5

    group_whitelist: list[str] = Field(
        default_factory=list,
        description="允许缓存入站富媒体（图片/语音/表情）的群号白名单；为空时不缓存任何消息",
    )


class KeywordsReplyConfig(PluginConfigBase):
    """关键词回复插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    permission: PermissionConfig = Field(default_factory=PermissionConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    detect: DetectConfig = Field(default_factory=DetectConfig)
    template: TemplateConfig = Field(default_factory=TemplateConfig)
    media_cache: MediaCacheConfig = Field(default_factory=MediaCacheConfig)


# ─── 插件主体 ──────────────────────────────────────────────────


class KeywordsReplyPlugin(MaiBotPlugin):
    """关键词回复插件。"""

    config_model = KeywordsReplyConfig
    _MEDIA_CACHE_MAX = 500

    async def on_load(self) -> None:
        self.store = KeywordsStore(self.ctx.paths.data_dir)
        self.store.setup()
        self.matcher = Matcher(self.store, self._settings)
        self._inbound_media_cache: Dict[str, dict] = {}
        cache_groups = sorted(self._media_cache_group_whitelist())
        self.ctx.logger.info(
            "关键词回复插件已加载：关键词 %d 条，检测词 %d 条，媒体缓存群 %d 个",
            len(self.store.data.get(SECTION_KEYWORD, [])),
            len(self.store.data.get(SECTION_DETECT, [])),
            len(cache_groups),
        )

    async def on_unload(self) -> None:
        self._inbound_media_cache.clear()

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del scope, config_data, version

    # ─── 内部工具 ──────────────────────────────────────────────

    def _settings(self) -> Dict[str, Any]:
        return {
            "case_sensitive": self.config.reply.case_sensitive,
            "qq_forward_all_replies": self.config.reply.qq_forward_all_replies,
            "cooldown": self.config.detect.cooldown,
            "ignore_cooldown_on_exact_match": self.config.detect.ignore_cooldown_on_exact_match,
        }

    async def _is_admin(self, platform: str, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            return False
        scoped = f"{platform}:{uid}" if platform else uid
        whitelist = {str(x).strip() for x in (self.config.permission.whitelist or []) if str(x).strip()}
        if uid in whitelist or scoped in whitelist:
            return True
        try:
            perm = await self.ctx.config.get("plugin.permission")
        except Exception:
            perm = None
        if isinstance(perm, list):
            masters = {str(x).strip().lower() for x in perm if str(x).strip()}
            if scoped.lower() in masters:
                return True
        return False

    async def _send(self, stream_id: str, text: str) -> tuple[bool, str, bool]:
        if stream_id and text:
            await self.ctx.send.text(text, stream_id)
        return True, text, True

    async def _deny(self, stream_id: str, msg: str = "权限不足。") -> tuple[bool, str, bool]:
        if self.config.permission.notify_permission_denied:
            return await self._send(stream_id, msg)
        return True, "", True

    @staticmethod
    def _args(kwargs: Dict[str, Any]) -> str:
        mg = kwargs.get("matched_groups")
        if isinstance(mg, dict) and mg.get("args") is not None:
            return str(mg.get("args"))
        return ""

    def _find_rule(self, section: str, keyword: str, is_regex: bool) -> Optional[dict]:
        cs = self.config.reply.case_sensitive
        for cfg in self.store.data.get(section, []):
            kw = cfg.get("keyword", "")
            if cfg.get("regex", False) or cs:
                equal = kw == keyword
            else:
                equal = kw.lower() == keyword.lower()
            if equal:
                return cfg
        return None

    @staticmethod
    def _extract_group_id(message: Dict[str, Any]) -> str:
        """从消息字典解析群号；私聊返回空字符串。"""

        info = message.get("message_info", {}) if isinstance(message.get("message_info"), dict) else {}
        group_info = info.get("group_info") or {}
        if isinstance(group_info, dict):
            return str(group_info.get("group_id", "") or "").strip()
        return ""

    def _media_cache_group_whitelist(self) -> set[str]:
        return {str(x).strip() for x in (self.config.media_cache.group_whitelist or []) if str(x).strip()}

    def _should_cache_inbound_media(self, message: Dict[str, Any]) -> bool:
        """仅对白名单群聊缓存入站媒体，避免全量消息带来的性能开销。"""

        whitelist = self._media_cache_group_whitelist()
        if not whitelist:
            return False
        group_id = self._extract_group_id(message)
        return bool(group_id) and group_id in whitelist

    def _remember_inbound_media(self, message_id: str, entry: dict) -> None:
        """缓存入站消息媒体，供后续引用导入使用（如引用语音后添加关键词）。"""

        if not message_id or not self.store.entry_has_payload(entry):
            return
        self._inbound_media_cache[message_id] = entry
        while len(self._inbound_media_cache) > self._MEDIA_CACHE_MAX:
            oldest_key = next(iter(self._inbound_media_cache))
            del self._inbound_media_cache[oldest_key]

    async def _build_entry(self, content: str, message: Dict[str, Any]) -> dict:
        """从命令正文 + 触发消息媒体 + 引用消息内容构建一条 entry。"""

        entry = self.store.empty_entry()
        plain, inline_ats = extract_inline_ats(strip_auto_media_text(content or ""))
        entry["text"] = plain.strip()
        entry["ats"].extend(inline_ats)
        message_id = str(message.get("message_id", "") or "").strip()
        cached_media: Optional[dict] = None
        if message_id:
            cached = self._inbound_media_cache.pop(message_id, None)
            if isinstance(cached, dict) and self.store.entry_has_payload(cached):
                cached_media = cached

        if cached_media:
            entry = self.store.merge_entries(entry, cached_media)
        else:
            trigger_media = await capture_media_from_trigger(self.ctx, self.store, message)
            entry = self.store.merge_entries(entry, trigger_media)

        reply_imported = await capture_from_reply(
            self.ctx,
            self.store,
            message,
            media_cache=self._inbound_media_cache,
        )
        entry = self.store.merge_entries(entry, reply_imported)
        return sanitize_entry_text(entry, self.store)

    async def _dispatch_reply(self, stream_id: str, match: dict, message: Dict[str, Any]) -> None:
        """把命中的回复发送到聊天流。"""

        payload = match.get("payload")
        quote = self.config.reply.quote_reply
        enable_tpl = self.config.template.enable_text_template

        if isinstance(payload, list):
            messages = build_forward_messages(payload, self.store, message, enable_template=enable_tpl)
            if messages:
                ok = await self.ctx.send.forward(messages, stream_id)
                if ok:
                    return
            payload = payload[0] if payload else None

        if not isinstance(payload, dict):
            return
        segments = build_send_segments(
            payload, self.store, message, quote=quote, enable_template=enable_tpl
        )
        if segments:
            await self.ctx.send.hybrid(segments, stream_id)

    async def _send_entry_preview(self, stream_id: str, intro: str, entry: dict, message: Dict[str, Any]) -> None:
        """发送词条详情预览（含媒体，不做模板渲染）。"""

        segments = build_send_segments(entry, self.store, message, quote=False, render=False)
        segments.insert(0, {"type": "text", "content": intro})
        await self.ctx.send.hybrid(segments, stream_id)

    # ─── 通用命令实现 ──────────────────────────────────────────

    async def _cmd_add(self, section: str, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        group_id = str(kwargs.get("group_id", "") or "")
        message = kwargs.get("message") or {}
        label = SECTION_LABELS[section]
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)

        args = self._args(kwargs).strip()
        usage = f"格式错误。用法: /添加{label} [-r] <{label}> <回复内容>"
        if not args:
            return await self._send(stream_id, usage)

        is_regex = False
        if args.startswith("-r"):
            is_regex = True
            args = args[2:].lstrip()
        if not args:
            return await self._send(stream_id, usage)

        m = re.search(r"\s", args)
        if m:
            keyword = args[: m.start()]
            reply_text = args[m.start():].lstrip()
        else:
            keyword = args
            reply_text = ""
        keyword = strip_auto_media_text(keyword)
        reply_text = strip_auto_media_text(reply_text)
        if not keyword:
            return await self._send(stream_id, f"{label}不能为空。")

        if is_regex:
            if not is_safe_regex(keyword):
                return await self._send(stream_id, "正则表达式存在安全风险，请简化后重试。")
            try:
                re.compile(keyword)
            except Exception as exc:
                return await self._send(stream_id, f"无效的正则表达式: {exc}")

        entry = await self._build_entry(reply_text, message)
        if not self.store.entry_has_payload(entry):
            return await self._send(stream_id, "回复内容不能为空。")

        rule = self._find_rule(section, keyword, is_regex)
        if rule is not None:
            rule["entries"].append(entry)
            rule["regex"] = is_regex
            status = f"已为现有{label}添加新回复（当前共有 {len(rule['entries'])} 个回复）。"
        else:
            if group_id:
                enabled, mode, groups = True, "whitelist", [group_id]
                status = f"已成功添加{label}，并在当前群聊启用。"
            else:
                enabled, mode, groups = False, "whitelist", []
                status = f"已成功添加{label}。由于非群聊环境创建，已默认全局禁用。"
            rule = {
                "keyword": keyword,
                "entries": [entry],
                "regex": is_regex,
                "enabled": enabled,
                "mode": mode,
                "groups": groups,
            }
            if section == SECTION_DETECT:
                rule["case_sensitive"] = self.config.reply.case_sensitive
            self.store.data[section].append(rule)

        await self.store.save()
        return await self._send(stream_id, f"成功操作{label}: {keyword}\n{status}")

    async def _cmd_edit(self, section: str, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        label = SECTION_LABELS[section]
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)

        parts = self._args(kwargs).split()
        is_regex = False
        if parts and parts[0] == "-r":
            is_regex = True
            parts = parts[1:]
        if len(parts) < 2:
            return await self._send(stream_id, f"格式错误。用法: /编辑{label} [-r] <序号或内容> <新{label}>")

        idx_param, new_keyword = parts[0], parts[1]
        data = self.store.data[section]
        indices = find_indices(data, idx_param, self.config.reply.case_sensitive)
        if not indices:
            return await self._send(stream_id, f"未找到匹配 '{idx_param}' 的{label}。")

        if is_regex:
            try:
                re.compile(new_keyword)
            except Exception as exc:
                return await self._send(stream_id, f"无效的正则表达式: {exc}")

        idx = indices[0]
        old_keyword = data[idx]["keyword"]
        data[idx]["keyword"] = new_keyword
        data[idx]["regex"] = is_regex
        await self.store.save()
        return await self._send(stream_id, f"{label} '{old_keyword}' 已修改为 '{new_keyword}'。")

    async def _cmd_delete(self, section: str, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        label = SECTION_LABELS[section]
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)

        param = self._args(kwargs).strip()
        if not param:
            return await self._send(stream_id, f"格式错误。用法: /删除{label} <序号或内容>")
        data = self.store.data[section]
        indices = find_indices(data, param, self.config.reply.case_sensitive)
        if not indices:
            return await self._send(stream_id, f"未找到匹配 '{param}' 的{label}。")

        deleted = []
        for idx in sorted(indices, reverse=True):
            deleted.append(data.pop(idx)["keyword"])
        await self.store.save()
        return await self._send(stream_id, f"{label} '{', '.join(deleted)}' 已删除。")

    async def _cmd_toggle(self, section: str, enable: bool, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        group_id = str(kwargs.get("group_id", "") or "")
        label = SECTION_LABELS[section]
        cmd_name = "启用" if enable else "禁用"
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)

        parts = self._args(kwargs).split()
        if not parts:
            return await self._send(stream_id, f"格式错误。用法: /{cmd_name}{label} <序号或内容> [群号...]")

        param, group_args = parts[0], parts[1:]
        data = self.store.data[section]
        indices = find_indices(data, param, self.config.reply.case_sensitive)
        if not indices:
            return await self._send(stream_id, f"未找到匹配 '{param}' 的{label}。")

        results = []
        for idx in indices:
            cfg = data[idx]
            ok, desc = apply_group_toggle(cfg, enable, group_args, group_id)
            if not ok:
                return await self._send(stream_id, desc)
            results.append(f"{label} '{cfg['keyword']}' {cmd_name} 群聊: {desc}")
        await self.store.save()
        return await self._send(stream_id, "\n".join(results))

    async def _cmd_list(self, section: str, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        group_id = str(kwargs.get("group_id", "") or "")
        label = SECTION_LABELS[section]
        data = self.store.data[section]

        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            allow = (
                self.config.permission.allow_group_member_list_keywords
                if section == SECTION_KEYWORD
                else self.config.permission.allow_group_member_list_detects
            )
            if not allow or not group_id:
                return await self._deny(stream_id)
            enabled_keywords = [
                cfg.get("keyword", "")
                for cfg in data
                if cfg.get("keyword") and Matcher.is_enabled_in_group(cfg, group_id)
            ]
            res = f"当前群聊({group_id})启用的{label}:\n"
            res += "无" if not enabled_keywords else "\n".join(f"{i}. {k}" for i, k in enumerate(enabled_keywords, 1))
            return await self._send(stream_id, res.strip())

        res = f"{label}列表:\n"
        if not data:
            res += "无"
        else:
            lines = []
            for i, cfg in enumerate(data, 1):
                regex_str = " [正则]" if cfg.get("regex", False) else ""
                lines.append(f"【{i}】 {cfg['keyword']}{regex_str}{describe_status_brief(cfg)}")
                for j, entry in enumerate(cfg.get("entries", []), 1):
                    lines.append(f"  └─ {j}. {self.store.summarize_entry(entry, 50)}")
            res += "\n".join(lines)
        return await self._send(stream_id, res.strip())

    async def _cmd_view(self, section: str, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        message = kwargs.get("message") or {}
        label = SECTION_LABELS[section]
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)

        tokens = self._args(kwargs).split()
        if not tokens:
            return await self._send(stream_id, f"用法: /查看{label} <序号或内容>")
        if len(tokens) >= 2:
            return await self._cmd_view_reply(section, kwargs)

        data = self.store.data[section]
        indices = find_indices(data, tokens[0], self.config.reply.case_sensitive)
        if not indices:
            return await self._send(stream_id, f"未找到匹配 '{tokens[0]}' 的{label}。")

        cfg = data[indices[0]]
        entries = cfg.get("entries", [])
        header = (
            f"{label}: {cfg['keyword']}\n"
            f"类型: {'正则匹配' if cfg.get('regex') else MATCH_TYPE_LABELS[section]}\n"
            f"状态: {describe_status(cfg)}\n"
        )
        if len(entries) == 1:
            await self._send_entry_preview(stream_id, header + "回复详情：\n", entries[0], message)
            return True, header, True
        header += f"回复数量: {len(entries)}\n"
        for j, entry in enumerate(entries, 1):
            header += f"【{j}】{self.store.summarize_entry(entry, 50)}\n"
        header += f"（使用 /查看{label}回复 <序号> <回复序号> 查看完整内容）"
        return await self._send(stream_id, header.strip())

    async def _cmd_view_reply(self, section: str, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        message = kwargs.get("message") or {}
        label = SECTION_LABELS[section]
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)

        tokens = self._args(kwargs).split()
        if not tokens:
            return await self._send(stream_id, f"用法: /查看{label}回复 <{label}序号/内容> [回复序号]")

        data = self.store.data[section]
        indices = find_indices(data, tokens[0], self.config.reply.case_sensitive)
        if not indices:
            return await self._send(stream_id, f"未找到匹配 '{tokens[0]}' 的{label}。")

        cfg = data[indices[0]]
        entries = cfg.get("entries", [])
        if len(tokens) < 2:
            if len(entries) == 1:
                reply_idx = 0
            else:
                return await self._send(stream_id, f"该{label}有 {len(entries)} 个回复，请指定回复序号。")
        else:
            if not tokens[1].isdigit():
                return await self._send(stream_id, "回复序号必须是数字。")
            reply_idx = int(tokens[1]) - 1

        if not (0 <= reply_idx < len(entries)):
            return await self._send(stream_id, "回复序号无效。")
        intro = f"{label} '{cfg['keyword']}' 的第 {reply_idx + 1} 个回复：\n"
        await self._send_entry_preview(stream_id, intro, entries[reply_idx], message)
        return True, intro, True

    async def _cmd_add_reply(self, section: str, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        message = kwargs.get("message") or {}
        label = SECTION_LABELS[section]
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)

        parts = self._args(kwargs).split(None, 1)
        if not parts:
            return await self._send(
                stream_id,
                f"用法: /添加{label}回复 <{label}序号/内容> [回复内容]\n可直接引用一条消息作为回复内容。",
            )
        data = self.store.data[section]
        indices = find_indices(data, parts[0], self.config.reply.case_sensitive)
        if not indices:
            return await self._send(stream_id, f"未找到匹配 '{parts[0]}' 的{label}。")

        cfg = data[indices[0]]
        content = parts[1] if len(parts) > 1 else ""
        entry = await self._build_entry(content, message)
        if not self.store.entry_has_payload(entry):
            return await self._send(stream_id, "回复内容不能为空。")
        cfg["entries"].append(entry)
        await self.store.save()
        return await self._send(
            stream_id, f"已为{label} '{cfg['keyword']}' 添加新回复（当前共有 {len(cfg['entries'])} 个回复）。"
        )

    async def _cmd_edit_reply(self, section: str, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        message = kwargs.get("message") or {}
        label = SECTION_LABELS[section]
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)

        args = self._args(kwargs)
        parts = args.split(None, 2)
        if not parts:
            return await self._send(
                stream_id,
                f"格式错误。用法: /编辑{label}回复 <{label}序号/内容> <回复序号> <新内容>",
            )
        data = self.store.data[section]
        indices = find_indices(data, parts[0], self.config.reply.case_sensitive)
        if not indices:
            return await self._send(stream_id, f"未找到匹配 '{parts[0]}' 的{label}。")

        cfg = data[indices[0]]
        entries = cfg["entries"]
        if len(entries) == 1:
            if len(parts) >= 3 and parts[1].isdigit() and int(parts[1]) == 1:
                reply_idx, content = 0, parts[2]
            else:
                reply_idx = 0
                content = args[len(parts[0]):].strip()
        else:
            if len(parts) < 2:
                return await self._send(stream_id, f"该{label}有 {len(entries)} 个回复，请指定要编辑的序号。")
            if not parts[1].isdigit():
                return await self._send(stream_id, "回复序号必须是数字。")
            ri = int(parts[1])
            if not (1 <= ri <= len(entries)):
                return await self._send(stream_id, f"回复序号无效。请输入 1-{len(entries)} 之间的数字。")
            reply_idx = ri - 1
            content = parts[2] if len(parts) >= 3 else ""

        entry = await self._build_entry(content, message)
        if not self.store.entry_has_payload(entry):
            return await self._send(stream_id, "回复内容不能为空。")
        cfg["entries"][reply_idx] = entry
        await self.store.save()
        return await self._send(stream_id, f"已更新{label} '{cfg['keyword']}' 的第 {reply_idx + 1} 个回复。")

    async def _cmd_delete_reply(self, section: str, kwargs: Dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = kwargs.get("stream_id", "")
        label = SECTION_LABELS[section]
        if not await self._is_admin(kwargs.get("platform", ""), kwargs.get("user_id", "")):
            return await self._deny(stream_id)

        tokens = self._args(kwargs).split()
        if not tokens:
            return await self._send(stream_id, f"格式错误。用法: /删除{label}回复 <{label}序号/内容> [回复序号]")
        data = self.store.data[section]
        indices = find_indices(data, tokens[0], self.config.reply.case_sensitive)
        if not indices:
            return await self._send(stream_id, f"未找到匹配 '{tokens[0]}' 的{label}。")

        cfg = data[indices[0]]
        entries = cfg["entries"]
        if len(tokens) < 2:
            if len(entries) == 1:
                reply_idx = 0
            else:
                return await self._send(stream_id, f"该{label}有 {len(entries)} 个回复，请指定要删除的回复序号。")
        else:
            if not tokens[1].isdigit():
                return await self._send(stream_id, "回复序号必须是数字。")
            reply_idx = int(tokens[1]) - 1

        if not (0 <= reply_idx < len(entries)):
            return await self._send(stream_id, "回复序号无效。")
        entries.pop(reply_idx)
        await self.store.save()
        return await self._send(stream_id, f"已删除{label} '{cfg['keyword']}' 的第 {reply_idx + 1} 个回复。")

    # ─── 关键词命令（command_triggered） ─────────────────────────

    @Command("kr_add_keyword", description="添加关键词", pattern=r"/添加关键词(?:\s+(?P<args>[\s\S]+))?$")
    async def add_keyword(self, **kwargs: Any):
        return await self._cmd_add(SECTION_KEYWORD, kwargs)

    @Command("kr_edit_keyword", description="编辑关键词", pattern=r"/编辑关键词(?:\s+(?P<args>[\s\S]+))?$")
    async def edit_keyword(self, **kwargs: Any):
        return await self._cmd_edit(SECTION_KEYWORD, kwargs)

    @Command("kr_del_keyword", description="删除关键词", pattern=r"/删除关键词(?:\s+(?P<args>[\s\S]+))?$")
    async def del_keyword(self, **kwargs: Any):
        return await self._cmd_delete(SECTION_KEYWORD, kwargs)

    @Command("kr_enable_keyword", description="启用关键词", pattern=r"/启用关键词(?:\s+(?P<args>[\s\S]+))?$")
    async def enable_keyword(self, **kwargs: Any):
        return await self._cmd_toggle(SECTION_KEYWORD, True, kwargs)

    @Command("kr_disable_keyword", description="禁用关键词", pattern=r"/禁用关键词(?:\s+(?P<args>[\s\S]+))?$")
    async def disable_keyword(self, **kwargs: Any):
        return await self._cmd_toggle(SECTION_KEYWORD, False, kwargs)

    @Command("kr_list_keywords", description="查看关键词列表", pattern=r"/查看(?:关键词列表|所有关键词)$")
    async def list_keywords(self, **kwargs: Any):
        return await self._cmd_list(SECTION_KEYWORD, kwargs)

    @Command("kr_view_keyword_reply", description="查看关键词回复", pattern=r"/查看关键词回复(?:\s+(?P<args>[\s\S]+))?$")
    async def view_keyword_reply(self, **kwargs: Any):
        return await self._cmd_view_reply(SECTION_KEYWORD, kwargs)

    @Command("kr_view_keyword", description="查看关键词", pattern=r"/查看关键词(?:\s+(?P<args>[\s\S]+))?$")
    async def view_keyword(self, **kwargs: Any):
        return await self._cmd_view(SECTION_KEYWORD, kwargs)

    @Command("kr_add_keyword_reply", description="添加关键词回复", pattern=r"/添加关键词回复(?:\s+(?P<args>[\s\S]+))?$")
    async def add_keyword_reply(self, **kwargs: Any):
        return await self._cmd_add_reply(SECTION_KEYWORD, kwargs)

    @Command("kr_edit_keyword_reply", description="编辑关键词回复", pattern=r"/编辑关键词回复(?:\s+(?P<args>[\s\S]+))?$")
    async def edit_keyword_reply(self, **kwargs: Any):
        return await self._cmd_edit_reply(SECTION_KEYWORD, kwargs)

    @Command("kr_del_keyword_reply", description="删除关键词回复", pattern=r"/删除关键词回复(?:\s+(?P<args>[\s\S]+))?$")
    async def del_keyword_reply(self, **kwargs: Any):
        return await self._cmd_delete_reply(SECTION_KEYWORD, kwargs)

    # ─── 检测词命令（auto_detect） ───────────────────────────────

    @Command("kr_add_detect", description="添加检测词", pattern=r"/添加检测词(?:\s+(?P<args>[\s\S]+))?$")
    async def add_detect(self, **kwargs: Any):
        return await self._cmd_add(SECTION_DETECT, kwargs)

    @Command("kr_edit_detect", description="编辑检测词", pattern=r"/编辑检测词(?:\s+(?P<args>[\s\S]+))?$")
    async def edit_detect(self, **kwargs: Any):
        return await self._cmd_edit(SECTION_DETECT, kwargs)

    @Command("kr_del_detect", description="删除检测词", pattern=r"/删除检测词(?:\s+(?P<args>[\s\S]+))?$")
    async def del_detect(self, **kwargs: Any):
        return await self._cmd_delete(SECTION_DETECT, kwargs)

    @Command("kr_enable_detect", description="启用检测词", pattern=r"/启用检测词(?:\s+(?P<args>[\s\S]+))?$")
    async def enable_detect(self, **kwargs: Any):
        return await self._cmd_toggle(SECTION_DETECT, True, kwargs)

    @Command("kr_disable_detect", description="禁用检测词", pattern=r"/禁用检测词(?:\s+(?P<args>[\s\S]+))?$")
    async def disable_detect(self, **kwargs: Any):
        return await self._cmd_toggle(SECTION_DETECT, False, kwargs)

    @Command("kr_list_detects", description="查看检测词列表", pattern=r"/查看(?:检测词列表|所有检测词)$")
    async def list_detects(self, **kwargs: Any):
        return await self._cmd_list(SECTION_DETECT, kwargs)

    @Command("kr_view_detect_reply", description="查看检测词回复", pattern=r"/查看检测词回复(?:\s+(?P<args>[\s\S]+))?$")
    async def view_detect_reply(self, **kwargs: Any):
        return await self._cmd_view_reply(SECTION_DETECT, kwargs)

    @Command("kr_view_detect", description="查看检测词", pattern=r"/查看检测词(?:\s+(?P<args>[\s\S]+))?$")
    async def view_detect(self, **kwargs: Any):
        return await self._cmd_view(SECTION_DETECT, kwargs)

    @Command("kr_add_detect_reply", description="添加检测词回复", pattern=r"/添加检测词回复(?:\s+(?P<args>[\s\S]+))?$")
    async def add_detect_reply(self, **kwargs: Any):
        return await self._cmd_add_reply(SECTION_DETECT, kwargs)

    @Command("kr_edit_detect_reply", description="编辑检测词回复", pattern=r"/编辑检测词回复(?:\s+(?P<args>[\s\S]+))?$")
    async def edit_detect_reply(self, **kwargs: Any):
        return await self._cmd_edit_reply(SECTION_DETECT, kwargs)

    @Command("kr_del_detect_reply", description="删除检测词回复", pattern=r"/删除检测词回复(?:\s+(?P<args>[\s\S]+))?$")
    async def del_detect_reply(self, **kwargs: Any):
        return await self._cmd_delete_reply(SECTION_DETECT, kwargs)

    # ─── 自动回复（Hook + EventHandler 双路径） ─────────────────

    _MGMT_PREFIXES = ("/添加", "/编辑", "/删除", "/启用", "/禁用", "/查看")

    @HookHandler(
        "chat.receive.before_process",
        name="keywords_capture_command_media",
        description="在 VLM/ASR 处理前缓存入站消息富媒体二进制",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def capture_command_media_before_process(self, message: Any = None, **kwargs: Any):
        """``message.process()`` 会清空图片二进制并生成占位描述。

        无法修改 MaiBot 主程序时，只能在此 Hook（位于 process 之前）抢先缓存媒体。
        历史语音经 ``get_by_id`` 通常不带二进制，因此还需缓存每条入站语音供引用导入。
        """

        del kwargs
        if not isinstance(message, dict):
            return {"action": "continue"}
        if not self._should_cache_inbound_media(message):
            return {"action": "continue"}

        message_id = str(message.get("message_id", "") or "").strip()
        if not message_id:
            return {"action": "continue"}

        cached = capture_media_from_message_dict(message, self.store)
        if self.store.entry_has_payload(cached):
            self._remember_inbound_media(message_id, cached)
            if is_management_command_message(message):
                self.ctx.logger.debug(f"已缓存管理命令消息媒体: message_id={message_id}")
        return {"action": "continue"}

    async def _try_auto_reply(self, message: Dict[str, Any]) -> str:
        """尝试匹配并发送自动回复。

        Returns:
            ``"keyword"``：命中关键词，应拦截后续主链；
            ``"detect"``：命中检测词，已回复但不拦截；
            ``"none"``：未命中。
        """

        if not self.config.plugin.enabled:
            return "none"

        text = str(message.get("processed_plain_text", "") or "").strip()
        if not text:
            return "none"

        # 管理命令由 @Command 处理，避免与自动回复重复触发。
        if message.get("is_command") or any(text.startswith(p) for p in self._MGMT_PREFIXES):
            return "none"

        info = message.get("message_info", {}) if isinstance(message.get("message_info"), dict) else {}
        group_info = info.get("group_info") or {}
        group_id = str(group_info.get("group_id", "") or "") if isinstance(group_info, dict) else ""
        session_id = str(message.get("session_id", "") or "")
        stream_id = session_id

        try:
            command_match = self.matcher.match_command(text, group_id)
            if command_match:
                await self._dispatch_reply(stream_id, command_match, message)
                return "keyword"

            detect_match = self.matcher.match_detect(text, group_id, session_id)
            if detect_match:
                await self._dispatch_reply(stream_id, detect_match, message)
                return "detect"
        except Exception as exc:
            self.ctx.logger.error(f"关键词自动回复处理失败: {exc}", exc_info=True)

        return "none"

    @HookHandler(
        "chat.receive.after_process",
        name="keywords_auto_reply_hook",
        description="关键词/检测词自动回复（入站消息主链 Hook）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
    )
    async def handle_receive_after_process(self, message: Any = None, **kwargs: Any):
        """在入站消息预处理后触发自动回复。

        当前 MaiBot 主链中 ``ON_MESSAGE`` 事件分发默认未启用，因此自动回复
        必须挂在此 Hook 上才能实际生效。
        """

        del kwargs
        if not isinstance(message, dict):
            return {"action": "continue"}

        result = await self._try_auto_reply(message)
        if result == "keyword":
            return {"action": "abort"}
        return {"action": "continue"}

    @EventHandler(
        "keywords_auto_reply",
        description="关键词/检测词自动回复（ON_MESSAGE 备用路径）",
        event_type=EventType.ON_MESSAGE,
        intercept_message=True,
        weight=50,
    )
    async def handle_message(self, message: Any = None, **kwargs: Any):
        """当宿主重新启用 ON_MESSAGE 事件分发时的备用处理器。"""

        del kwargs
        if not isinstance(message, dict):
            return {"continue_processing": True}

        result = await self._try_auto_reply(message)
        if result == "keyword":
            return {"continue_processing": False}
        return {"continue_processing": True}


def create_plugin() -> KeywordsReplyPlugin:
    """创建关键词回复插件实例。"""

    return KeywordsReplyPlugin()
