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
| `native_block_prompt` | 卡掉模型原生思维链。禁止模型用自己的 reasoning，强制只走你写的 `<thinking>`。留空使用内置默认 |
| `online_prompt` | 线上模式 Prompt，适用于聊天场景的思维链。留空使用内置默认 |
| `offline_prompt` | 线下模式 Prompt，适用于见面/RP 场景的思维链。留空使用内置默认 |
| `default_mode` | 默认启动模式 (`online` / `offline`) |
| `max_history` | 保留最近多少条思考记录 |

## 🎮 指令

- `/最近思考` — 查看最新一次完整思考
- `/思考列表` — 查看最近 10 次思考标题
- `/清空思考` — 清空记录（仅管理员）
- `/reload` — 重新加载
