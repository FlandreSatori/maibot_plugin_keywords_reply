# OhData 词库迁移工具

将OhData 插件的SQL `database.db` 读写并导入为 MaiBot 关键词插件的 `keywords.json`。

## 数据库位置

默认路径（相对 Maibot 工作区）：

```
ohdata/ohdata/database.db
```

## 1. 数据库读写自检

```bash
cd maibot_plugin_keywords_reply

# 查看表结构、字段、行数
python tools/ohdata_db.py inspect

# 查看 outerheaven 规则分布
python tools/ohdata_db.py stats

# 读取单条
python tools/ohdata_db.py get --id 2

# 读写自检：临时插入 → 更新 → 删除
python tools/ohdata_db.py write-test
```

`write-test` 会在 `outerheaven` 表插入一条 `__maibot_import_test__` 记录，验证后自动删除，不会污染词库。

## 2. 导入为 keywords.json

在插件根目录执行（输出到 ``keywords.json``，供外部编辑器直接加载）：

```bash
cd maibot_plugin_keywords_reply
python tools/import_ohdata_db.py --deploy
```

若已生成 ``tools/keywords.imported.json``，可一键部署到编辑器/MaiBot 数据目录：

```bash
python tools/deploy_imported.py
```

启动外部编辑器：

```bash
python editor/server.py --data-dir "."
# 浏览器打开 http://127.0.0.1:8765
```

生产环境请将 ``--data-dir`` 指向 ``MaiBot/data/plugins/maibot_plugin.keywords_reply``。

可选参数：

| 参数 | 说明 |
| :-- | :-- |
| `--db` | database.db 路径 |
| `--out` | 输出 keywords.json |
| `--ini` | 分群.ini（读取已启用群号作为白名单） |
| `--merge` | 与已有 keywords.json 合并 |

### 字段映射

| OhData (`outerheaven`) | MaiBot (`keywords.json`) |
| :-- | :-- |
| `完整匹配` | `command_triggered` |
| `关键词匹配` | `auto_detect` |
| `正则表达式` | `auto_detect` + `regex: true` |
| `question` | `keyword` |
| `answer` 按 `\|` 拆分 | 多条 `entries` |
| `answer` 内 `&` | 同一条 entry 内多段组合 |
| `probability` | `entry.probability`（触发概率）；`|` 拆出的多条回复 `weight` 固定为 100 |
| `at=真` | `require_at_bot: true` |
| `[CQ:image,file=...]` | `images[].file` |
| `[CQ:record,file=...]` | `records[].file` |
| `{CQ:time,period=...}` | 导入时剥离（MaiBot 暂不支持时段条件） |

空答案的管理用正则（如 id=1）会自动跳过。

### 媒体文件

数据库里只存文件名（如 `滑稽 (277).jpg`、`.silk` 语音）。导入后需把原 OhData 插件目录下的图片/语音文件复制到新插件数据目录：

```
data/plugins/maibot_plugin.keywords_reply/images/
data/plugins/maibot_plugin.keywords_reply/records/
```

`.silk` 语音若 MaiBot 发送失败，需自行转码为 `.amr` 后改文件名。

## 3. 导入后

1. 检查 `keywords.imported.json`
2. 复制到插件数据目录并重命名为 `keywords.json`
3. 复制媒体文件
4. 在 MaiBot 执行 `/重载词库`，或用外部编辑器打开后保存
