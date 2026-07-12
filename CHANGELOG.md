# 更新日志

## 1.0.1

- 修复自动回复不生效：MaiBot 主链默认未启用 `ON_MESSAGE` 事件，自动回复改挂 `chat.receive.after_process` Hook；保留 `ON_MESSAGE` 处理器作兼容备用。

## 1.0.0

- 从 AstrBot 插件 `astrbot_plugin_keywords_reply` 迁移到 MaiBot SDK 2.x。
- 支持指令触发（关键词）与自动监听（检测词）两种模式。
- 支持文本、图片、At、语音、表情包的组合回复，及正则匹配、群聊黑白名单、冷却、变量模板。
- 富媒体通过触发消息与引用消息捕获（base64），以本地文件形式持久化，数据保存在可外部编辑的 `keywords.json`。
- 已放弃的 AstrBot 专有能力：自动撤回、合并转发聊天记录导入、WebUI 面板（详见 README）。
