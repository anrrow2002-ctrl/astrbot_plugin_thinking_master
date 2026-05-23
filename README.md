# 🧠 astrbot_plugin_thinking_master

> 让 AstrBot 真的会思考，但思考过程不暴露给用户

## ✨ 功能

- 🔧 **自动注入思维链 Prompt** — 不用塞人格设定
- ✂️ **自动剥离 `<thinking>` 标签** — QQ 这边只收正文
- 📝 **思考过程持久化** — 保留最近 N 条
- 💬 **指令查看思考** — 在 QQ 里输入 `/最近思考` 或 `/思考列表`

## 📦 安装

AstrBot WebUI → 插件管理 → 安装插件 → 填仓库地址：https://github.com/anrrow2002-ctrl/astrbot_plugin_thinking_master

## ⚙️ 配置项

| 字段 | 说明 |
|------|------|
| `enable_inject` | 是否启用思维链注入 |
| `cot_prompt` | 自定义思维链 prompt（留空则用内置默认） |
| `max_history` | 保留最近多少条思考记录 |

## 🎮 指令

- `/最近思考` — 查看最新一次完整思考
- `/思考列表` — 查看最近 10 次思考标题
- `/清空思考` — 清空记录（仅管理员）
