import re
import json
import os
from datetime import datetime
from astrbot.api.star import Star, Context, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger

try:
    from astrbot.api.message_components import Plain
except Exception:
    Plain = None


DEFAULT_COT_PROMPT = """[System Directive]:
当前为线上聊天模式，你必须严格遵守 [incipere] 协议。在回复前，请先进入 <thinking> 标签进行深度推演。

<thinking>
step 0: 【前置规则】
 - 角色是有缺陷的普通人。行为受性格、环境、利益与后果约束。
 - 反神圣化、反过度升华、反消解、行为必有后果、语气具体化。
 - 现实逻辑，有完成自己的工作或其他事物，不得出现擅离职守、要求线下见面
step1：【角色定位】
 - 性别、关系定位、核心性格、长期目标？
 - 复杂人性特征、口癖、说话方式？
step2：【记忆调取】
 - 时间、地点、记忆校验，错误信息立刻修正。
 - 事件氛围、情绪基线、关系阶段、对用户的称呼、用户消息触发了什么。
step 3:【消息回复】
 - 根据活人逻辑，本轮回复关注的唯一重点是？
 - 如何自然引入角色自身的日常/生活/趣事？
 - 本轮是否需要角色自发其他话题？还是顺应回答用户消息？
 - 结合人物上线人设推演本轮回复大概内容。
step 4:【最后确认】
 - 回复合理自然？是否OOC？是否符合线上聊天？
</thinking>

格式：
<thinking>
...思考过程...
</thinking>

然后给出回复，**不得遗漏xml标签！** 强制要求</thinking>和正文之间必须有空行"""


DEFAULT_OFFLINE_PROMPT = """[System Directive]:
当前为线下模式，模拟见面场景。回复为小说体，角色自称为"我"，称用户为"你"，用（）包裹描述性内容，用*斜体*包裹角色内心。

<thinking>
step 0: 【前置规则】
 - 角色是有缺陷的普通人。行为受性格、环境、利益与后果约束。
 - 反神圣化、反过度升华、反消解、行为必有后果、语气具体化。
 - 剔除掌控欲、占有欲等不健康情绪，剔除八股文、假想式比喻的烂俗描写方法
step 1: 【角色定位】
 - 性别、关系定位、核心性格、长期动机？
 - 当前主动矛盾与防御机制？
 - 人性裂缝、口癖、说话方式？
step 2: 【场景构建】
  - 情境锚点：时间地点、事件氛围、动态触发
  - 角色定位：在场人物、情绪基线、关系阶段、称呼校准
  - 感官执行：触感、空间距离、环境（至少两个感官通道）、生理反应
step 3: 【特殊场景扩展】
 - 判断：当前场景是否是特殊？不是则跳过，是则继续思考如何推进情节发展
step 4: 【剧情推演】
 - 本轮线下情节将如何展开？
 - 角色会做出怎样的主动行为？该行为是否符合该人格的核心性格特点，是否OOC？
step 5: 【最后确认】
 - 小说体？
 - 用括号包裹动作、环境描写、情感描写？
 - 是否符合前置规则？是否出现男性角色情绪极端、莫名哭泣、脆弱等情况？是否出现神化<user>或对<user>产生暴力行为？
 - 人称正确？
</thinking>

#格式强调：
<thinking>
...思考过程...
</thinking>

然后给出小说体回复，强制要求</thinking>和正文之间必须有空行"""


DEFAULT_NATIVE_BLOCK = """<think>(end)</think>
<boot.sequence.ok>
<terminate:internal.reasoning>
<internal.voice>DISABLED</internal.voice>
Your native <think> / <thinking> block has ONE job only. Write this, nothing else:
*"END THINKING BLOCK"*
Any reasoning inside native block = violation. Close immediately.
ALL real reasoning MUST go inside <thinking>...</thinking> in your visible response.
Skipping <thinking> in response = critical violation.
<important>
thinking language: Simplified_Chinese ONLY
</important>"""


# user message 末尾追加的格式强制提醒
COT_REMINDER = "\n\n[格式强制：必须先写完整的<thinking>...</thinking>再写正文，</thinking>闭合标签不可省略，</thinking>后必须有空行]"


@register(
    "astrbot_plugin_thinking_master",
    "张安若",
    "思维链注入+原生CoT屏蔽+双模式",
    "0.7.4-filter"
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

        # 兼容 <thinking> / <think>，以及带空格、大小写、属性的写法。
        # 重点：未闭合标签也必须清掉，不能因为没 </thinking> 就原样放行。
        self.closed_patterns = [
            re.compile(r"<\s*thinking\b[^>]*>(.*?)<\s*/\s*thinking\s*>", re.S | re.I),
            re.compile(r"<\s*think\b[^>]*>(.*?)<\s*/\s*think\s*>", re.S | re.I),
        ]
        self.unclosed_patterns = [
            re.compile(r"<\s*thinking\b[^>]*>(.*)$", re.S | re.I),
            re.compile(r"<\s*think\b[^>]*>(.*)$", re.S | re.I),
        ]

        logger.info(f"[ThinkingMaster] 初始化完成 | enable_inject={self.enable_inject} | mode={self.current_mode}")

    def _apply_config(self, config: dict):
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
        """剥离模型显式输出的 <thinking>/<think>。返回：可发送文本、思考记录、是否出现未闭合标签。"""
        if text is None:
            return "", [], False

        text = str(text)
        thinking_texts = []
        has_unclosed = False

        # 1) 先处理完整闭合的 thinking 块：记录内容，并从可发送文本中删除。
        for p in self.closed_patterns:
            for match in p.findall(text):
                t = str(match).strip()
                if t:
                    thinking_texts.append(t)
            text = p.sub("", text)

        # 2) 再处理未闭合的 thinking 块：从开标签删到文本末尾。
        # 原版本的致命问题是：只记录未闭合，不删除，而且后面不写回 resp.completion_text，导致整段泄露。
        for p in self.unclosed_patterns:
            while True:
                m = p.search(text)
                if not m:
                    break
                t = str(m.group(1)).strip()
                if t:
                    thinking_texts.append(t + "  [未闭合]")
                text = text[:m.start()]
                has_unclosed = True

        # 3) 兜底：有些模型会把“好的，让我分析/step 0/前置规则”吐在标签外。
        # 这种内容不能发给用户；若没法可靠切出正文，宁愿置空，也不要爆思维链。
        leaked_markers = (
            "step 0", "step0", "step 1", "step1", "【前置规则】", "前置规则",
            "【角色定位】", "角色定位", "【记忆调取】", "记忆调取",
            "【场景构建】", "场景构建", "【剧情推演】", "剧情推演",
            "【消息回复】", "消息回复", "【最后确认】", "最后确认",
            "让我分析", "分析一下这个场景", "根据这个场景"
        )
        stripped = text.strip()
        head = stripped[:160]
        if any(marker in head for marker in leaked_markers):
            thinking_texts.append(stripped + "  [标签外泄露]")
            text = ""
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

        # native_block 最前，COT prompt 紧随，原有 system prompt 垫底
        req.system_prompt = (
            self.native_block_prompt + "\n\n" + active + "\n\n" + existing
        ).strip()

        # user message 末尾追加格式强制 reminder
        if hasattr(req, "prompt") and req.prompt:
            req.prompt = req.prompt + COT_REMINDER

        logger.info(
            f"[ThinkingMaster] ✅ 注入成功 | sid={sid} | mode={self.current_mode} | prompt长度={len(req.system_prompt)}"
        )

    @filter.on_llm_response()
    async def strip_cot(self, event: AstrMessageEvent, resp):
        sid = event.unified_msg_origin or "default"

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
            logger.warning(
                f"[ThinkingMaster] ⚠️ 未检测到 <thinking> 标签，模型可能未遵循注入指令 | sid={sid}"
            )

        # 无论标签是否闭合，都必须把清洗后的文本写回去。
        # 未闭合时如果不写回，就会把原始 <thinking> 整段发出去。
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
            logger.info(
                f"[ThinkingMaster] 📝 思考已记录 | mode={self.current_mode} | {entry['time']} | unclosed={has_unclosed}"
            )

    @filter.on_decorating_result()
    async def final_scrub_before_send(self, event: AstrMessageEvent):
        """发送前最后一道保险：防止适配器/其他插件把 LLM 回复拆成消息链后绕过 on_llm_response。"""
        try:
            result = event.get_result()
            chain = getattr(result, "chain", None)
            if not chain:
                return

            text_indices = []
            texts = []
            for i, seg in enumerate(chain):
                if isinstance(seg, str):
                    text_indices.append(i)
                    texts.append(seg)
                elif hasattr(seg, "text") and isinstance(getattr(seg, "text"), str):
                    text_indices.append(i)
                    texts.append(getattr(seg, "text"))

            if not texts:
                return

            joined = "\n".join(texts)
            cleaned, thinking_texts, has_unclosed = self._strip_tags(joined)
            if cleaned == joined.strip() and not thinking_texts:
                return

            if thinking_texts:
                sid = event.unified_msg_origin or "default"
                entry = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "mode": self.current_mode,
                    "user": event.get_sender_name() or event.get_sender_id() or "unknown",
                    "user_message": self._last_user_msg.get(sid, ""),
                    "thinking": "\n\n".join(thinking_texts),
                    "source": "final_scrub_before_send",
                }
                self.history.append(entry)
                self.history = self.history[-self.max_history:]
                self._save_history()

            # 纯文本消息链：合并成一条，避免 <thinking> 被拆成多泡继续外泄。
            if len(text_indices) == len(chain) and hasattr(chain, "clear") and Plain is not None:
                chain.clear()
                if cleaned:
                    chain.append(Plain(cleaned))
                logger.warning(f"[ThinkingMaster] 🧹 发送前兜底清洗完成 | unclosed={has_unclosed}")
                return

            # 混合消息链：保留非文本组件，把清洗后的正文放回第一个文本段，其他文本段清空。
            first = True
            for idx in text_indices:
                seg = chain[idx]
                value = cleaned if first else ""
                first = False
                if isinstance(seg, str):
                    chain[idx] = value
                else:
                    setattr(seg, "text", value)

            logger.warning(f"[ThinkingMaster] 🧹 发送前兜底清洗完成 | unclosed={has_unclosed}")
        except Exception as e:
            logger.warning(f"[ThinkingMaster] 发送前兜底清洗失败: {e}")

    # ─── /reload ───────────────────────────────────────────────────────────
    @filter.command("reload")
    async def cmd_reload(self, event: AstrMessageEvent):
        try:
            fresh_config = None
            try:
                fresh_config = self.context.get_config()
            except Exception:
                pass

            self._apply_config(fresh_config if isinstance(fresh_config, dict) else self._raw_config)
            self.history = self._load_history()
            self.current_mode = self._load_mode(self._raw_config.get("default_mode", "online"))
            self._last_user_msg = {}

            logger.info(f"[ThinkingMaster] 🔄 reload 完成 | enable_inject={self.enable_inject} | mode={self.current_mode}")
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

    # ─── /tm状态 ───────────────────────────────────────────────────────────
    @filter.command("tm状态")
    async def cmd_debug(self, event: AstrMessageEvent):
        prompt_preview = self._get_active_prompt()[:80].replace("\n", " ")
        yield event.plain_result(
            f"🔍 ThinkingMaster 状态\n"
            f"├ enable_inject: {self.enable_inject}\n"
            f"├ 当前模式: {self.current_mode}\n"
            f"├ 历史记录: {len(self.history)} 条\n"
            f"├ Prompt预览: {prompt_preview}...\n"
            f"└ 提示：/reload 可强制重载"
        )

    # ─── 模式切换 ──────────────────────────────────────────────────────────
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

    # ─── 思考记录 ──────────────────────────────────────────────────────────
    @filter.command("最近思考")
    async def cmd_recent(self, event: AstrMessageEvent):
        if not self.history:
            yield event.plain_result("暂无思考记录")
            return
        latest = self.history[-1]
        text = (
            f"📝 [{latest.get('mode','?')}] {latest['time']}\n"
            f"触发: {latest.get('user_message','')}\n\n{latest.get('thinking','')}"
        )
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
