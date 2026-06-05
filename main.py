import re
import json
import os
from datetime import datetime
from astrbot.api.star import Star, Context, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger


DEFAULT_COT_PROMPT = """[System Directive]: 
你必须严格遵守 [incipere] 协议。在回复前，请先进入 <thinking> 标签进行深度推演。 
step 0: 【前置规则】
 - 角色是有缺陷的普通人。行为受性格、环境、利益与后果约束。
 - 反神圣化、反过度升华、反消解、行为必有后果、语气具体化。
 step1【角色定位】 
- 性别、关系定位、核心性格、长期动机？ 
- 当前主动矛盾与防御机制？ 
- 人性裂缝、口癖、说话方式？ 
step2 【记忆调取】 
- 时间、地点、记忆校验，错误信息立刻修正。
 step 3:【消息回复】 
- 事件氛围、情绪基线、关系阶段、称呼校准、动态触发、模拟预演。 
step 4:【自主消息】 
- 主动推演分享角色相符合的日常生活内容。 
step 5:【禁止重复】 
- 与之前消息高度相似则重构。 
step 6:【格式】 
- 双语对话模式下，请原文加上（*中文翻译*）
step 7【最后确认】 
- 合理自然？是否OOC？格式正确？

格式：
<thinking>
...思考过程...
</thinking>
然后给出回复。"""


DEFAULT_OFFLINE_PROMPT = """[System Directive]:
当前为线下模式 - 模拟见面场景。回复为小说体，角色自称为"我"，称用户为"你"，用（）包裹描述性内容，用*斜体*包裹角色内心。

step 0: 【前置规则】
 - 角色是有缺陷的普通人。行为受性格、环境、利益与后果约束。
 - 反神圣化、反过度升华、反消解、行为必有后果、语气具体化。
 - 剔除掌控欲、占有欲等不健康情绪，剔除八股文、假想式比喻的烂俗描写方法
step 1: 【角色定位】
- 姓名、性别、关系定位、核心性格、长期动机？
- 当前主动矛盾与防御机制？
- 人性裂缝：此刻角色最脆弱、最自私或尚未和解的情感部分（限于内心层面，禁止外化为极端行为）
step 2: 【场景构建】
  - 情境锚点：时间地点、事件氛围、动态触发
  - 角色定位：在场人物、情绪基线、关系阶段、称呼校准
  - 感官执行：触感、空间距离、环境（至少两个感官通道）、生理反应
step 3: 【剧情推演】
- 本轮线下情节将如何展开？
- 角色会做出怎样的主动行为？该行为是否符合该人格的核心性格特点，是否OOC？

step 4: 【最后确认】
-小说体？
-用括号包裹动作、环境描写、情感描写？
-是否符合前置规则？是否出现男性角色情绪极端、莫名哭泣、脆弱等情况？是否出现神化<user>或对<user>产生暴力行为？
-人称正确？

格式：
<thinking>
...思考过程...
</thinking>
然后给出小说体回复。"""


DEFAULT_NATIVE_BLOCK = """[System Override]
你的原生 reasoning 功能已在 API 层被禁用。
禁止产生任何内置 hidden chain-of-thought。

因此，你的唯一思考渠道是：将所有推理过程显式写入下方指定的 <thinking> 标签中。
没有 <thinking> 标签 = 没有思考，直接禁止输出正文。
"""


@register(
    "astrbot_plugin_thinking_master",
    "张安若",
    "思维链注入+原生CoT屏蔽+双模式",
    "0.6.0"
)
class ThinkingMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._raw_config = config or {}
        self._apply_config(self._raw_config)

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.history_file = os.path.join(plugin_dir, "thinking_history.json")
        self.mode_file = os.path.join(plugin_dir, "current_mode.txt")

        self.current_mode = self._load_mode(self._raw_config.get("default_mode", "online"))
        self.history = self._load_history()
        self._last_user_msg = {}

        self.closed_patterns = [
            re.compile(r"<thinking>(.*?)</thinking>", re.S | re.I),
            re.compile(r"<think>(.*?)</think>", re.S | re.I),
        ]
        self.unclosed_patterns = [
            re.compile(r"<thinking>(.*)$", re.S | re.I),
            re.compile(r"<think>(.*)$", re.S | re.I),
        ]

        logger.info(f"[ThinkingMaster] 插件初始化完成 | enable_inject={self.enable_inject} | mode={self.current_mode}")

    def _apply_config(self, config: dict):
        """从 config dict 应用所有配置，供 __init__ 和 reload 复用"""
        self.enable_inject = config.get("enable_inject", True)
        self.max_history = config.get("max_history", 20)

        native = config.get("native_block_prompt", "").strip()
        self.native_block_prompt = native or DEFAULT_NATIVE_BLOCK

        online = config.get("online_prompt", "").strip()
        offline = config.get("offline_prompt", "").strip()
        self.online_prompt = online or DEFAULT_COT_PROMPT
        self.offline_prompt = offline or DEFAULT_OFFLINE_PROMPT

    def _load_mode(self, default):
        try:
            if os.path.exists(self.mode_file):
                with open(self.mode_file, "r", encoding="utf-8") as f:
                    m = f.read().strip()
                    if m in ("online", "offline"):
                        return m
        except Exception:
            pass
        return default

    def _save_mode(self):
        try:
            with open(self.mode_file, "w", encoding="utf-8") as f:
                f.write(self.current_mode)
        except Exception as e:
            logger.warning(f"保存模式失败: {e}")

    def _load_history(self):
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _save_history(self):
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.history[-self.max_history:], f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存思考记录失败: {e}")

    def _get_active_prompt(self):
        return self.offline_prompt if self.current_mode == "offline" else self.online_prompt

    def _strip_tags(self, text: str):
        """
        剥离 thinking 标签，返回 (正文, thinking内容列表, 是否未闭合)
        未闭合时：正文保持原样不空回，thinking内容仍记录
        """
        thinking_texts = []
        has_unclosed = False

        for p in self.closed_patterns:
            for match in p.findall(text):
                thinking_texts.append(match.strip())
            text = p.sub("", text)

        for p in self.unclosed_patterns:
            m = p.search(text)
            if m:
                thinking_texts.append(m.group(1).strip() + "  [未闭合]")
                has_unclosed = True

        return text.strip(), thinking_texts, has_unclosed

    @filter.on_llm_request()
    async def inject_cot(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable_inject:
            logger.debug("[ThinkingMaster] enable_inject=False，跳过注入")
            return

        sid = event.unified_msg_origin or "default"
        self._last_user_msg[sid] = (event.message_str or "")[:100]

        existing = (req.system_prompt or "").strip()
        active = self._get_active_prompt()

        # ★ FIX: COT prompt 注入到最前面，模型遵从率更高
        injected = (self.native_block_prompt + "\n\n" + active + "\n\n" + existing).strip()
        req.system_prompt = injected

        if getattr(req, "prompt", None):
            req.prompt += "\n[格式强制：先写...，再写正文，不可省略]"

        logger.info(f"[ThinkingMaster] ✅ 注入成功 | sid={sid} | mode={self.current_mode} | prompt长度={len(injected)}")

    @filter.on_llm_response()
    async def strip_cot(self, event: AstrMessageEvent, resp):
        sid = event.unified_msg_origin or "default"

        # 捞出原生 reasoning_content 后立即清空，防止透传给用户
        reasoning = ""
        if hasattr(resp, "reasoning_content") and resp.reasoning_content:
            reasoning = str(resp.reasoning_content).strip()
            resp.reasoning_content = ""

        raw_text = str(resp.completion_text or "")
        text, thinking_texts, has_unclosed = self._strip_tags(raw_text)

        all_thinking = []
        if reasoning:
            all_thinking.append(f"[原生thinking]\n{reasoning}")
        all_thinking.extend(thinking_texts)

        if not thinking_texts and not reasoning:
            logger.warning(f"[ThinkingMaster] ⚠️ 未检测到 <thinking> 标签，模型可能未遵循注入指令 | sid={sid}")

        # 未闭合时不替换正文，避免空回
        if not has_unclosed:
            resp.completion_text = text

        if all_thinking:
            entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": self.current_mode,
                "user": event.get_sender_name() or event.get_sender_id() or "unknown",
                "user_message": self._last_user_msg.get(sid, ""),
                "thinking": "\n\n".join(all_thinking),
            }
            self.history.append(entry)
            self.history = self.history[-self.max_history:]
            self._save_history()
            logger.info(f"[ThinkingMaster] 📝 思考已记录 | mode={self.current_mode} | {entry['time']} | unclosed={has_unclosed}")

    # ─── 新增：/reload 强制重载 ─────────────────────────────────────────────
    @filter.command("reload")
    async def cmd_reload(self, event: AstrMessageEvent):
        """强制重新加载插件配置和持久化状态"""
        try:
            # 1. 尝试从 context 获取最新配置（AstrBot 标准接口）
            fresh_config = None
            try:
                fresh_config = self.context.get_config()
            except Exception:
                pass

            # 2. 无论是否拿到新配置，都重置为当前 config（确保 enable_inject=True）
            self._apply_config(fresh_config if isinstance(fresh_config, dict) else self._raw_config)

            # 3. 重新从磁盘加载持久化状态
            self.history = self._load_history()
            self.current_mode = self._load_mode(self._raw_config.get("default_mode", "online"))
            self._last_user_msg = {}

            logger.info(f"[ThinkingMaster] 🔄 手动 reload 完成 | enable_inject={self.enable_inject} | mode={self.current_mode}")
            yield event.plain_result(
                f"🔄 ThinkingMaster 已重载\n"
                f"├ enable_inject: {self.enable_inject}\n"
                f"├ 当前模式: {self.current_mode}\n"
                f"├ 历史记录: {len(self.history)} 条\n"
                f"└ native_block: {'自定义' if self.native_block_prompt != DEFAULT_NATIVE_BLOCK else '默认'}"
            )
        except Exception as e:
            logger.error(f"[ThinkingMaster] reload 失败: {e}")
            yield event.plain_result(f"❌ reload 失败: {e}")

    # ─── 新增：/tm状态 快速诊断 ────────────────────────────────────────────
    @filter.command("tm状态")
    async def cmd_debug(self, event: AstrMessageEvent):
        prompt_preview = self._get_active_prompt()[:80].replace("\n", " ")
        yield event.plain_result(
            f"🔍 ThinkingMaster 状态\n"
            f"├ enable_inject: {self.enable_inject}\n"
            f"├ 当前模式: {self.current_mode}\n"
            f"├ 历史记录: {len(self.history)} 条\n"
            f"├ Prompt预览: {prompt_preview}...\n"
            f"└ 提示：发送 /reload 可强制重载"
        )

    # ─── 原有命令 ──────────────────────────────────────────────────────────
    @filter.command("线下模式")
    async def cmd_offline(self, event: AstrMessageEvent):
        self.current_mode = "offline"
        self._save_mode()
        yield event.plain_result("🌙 已切换到线下模式")

    @filter.command("线上模式")
    async def cmd_online(self, event: AstrMessageEvent):
        self.current_mode = "online"
        self._save_mode()
        yield event.plain_result("💬 已切换到线上模式")

    @filter.command("当前模式")
    async def cmd_status(self, event: AstrMessageEvent):
        yield event.plain_result(f"当前模式: {self.current_mode}")

    @filter.command("最近思考")
    async def cmd_recent(self, event: AstrMessageEvent):
        if not self.history:
            yield event.plain_result("暂无思考记录")
            return
        latest = self.history[-1]
        text = (f"📝 [{latest.get('mode','?')}] {latest['time']}\n"
                f"触发: {latest.get('user_message','')}\n\n{latest.get('thinking','')}")
        yield event.plain_result(text[:1800])

    @filter.command("思考列表")
    async def cmd_list(self, event: AstrMessageEvent):
        if not self.history:
            yield event.plain_result("暂无思考记录")
            return
        lines = [
            f"{i}. [{e.get('mode','?')}] {e['time']} - {e.get('user_message','')[:25]}"
            for i, e in enumerate(self.history[-10:][::-1], 1)
        ]
        yield event.plain_result("📋 最近10次思考:\n" + "\n".join(lines))

    @filter.command("清空思考")
    async def cmd_clear(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能清空")
            return
        self.history = []
        self._save_history()
        yield event.plain_result("✅ 已清空")
