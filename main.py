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


def _optional_hook(name: str, **kwargs):
    """兼容不同 AstrBot 版本：没有某个 hook 时不让插件导入失败。"""
    hook = getattr(filter, name, None)
    if hook is None:
        def deco(fn):
            return fn
        return deco
    try:
        return hook(**kwargs)
    except TypeError:
        return hook()


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


# 注意：这里不要再要求模型把 END THINKING BLOCK 当成可见文本吐出来。
# 有些模型没有真正的 hidden native CoT 通道，会把这句话当普通正文发出。
DEFAULT_NATIVE_BLOCK = """<native_reasoning_control>
Do not output your native hidden reasoning.
Do not output <think>, </think>, <thinking>, </thinking>, or "END THINKING BLOCK" as normal reply text.
Only use the custom visible <thinking>...</thinking> block required below for structured analysis; middleware will remove it before sending.
</native_reasoning_control>
<important>
thinking language: Simplified_Chinese ONLY
</important>"""


# user message 末尾追加的格式强制提醒
COT_REMINDER = "\n\n[格式强制：必须先写完整的<thinking>...</thinking>再写正文，</thinking>闭合标签不可省略，</thinking>后必须有空行。注意：END THINKING BLOCK 不是正文，不要输出。]"


@register(
    "astrbot_plugin_thinking_master",
    "张安若",
    "思维链注入+原生CoT屏蔽+双模式",
    "0.7.8-command-bypass"
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

        # 用于处理“分段发送/流式发送”：上一条消息如果只发了 <thinking>，后续 step 0 也要继续吞掉。
        self._thinking_open_state = {}

        self.open_tag_re = re.compile(r"<\s*(thinking|think)\b[^>]*>", re.I)
        self.close_tag_re = re.compile(r"<\s*/\s*(thinking|think)\s*>", re.I)
        self.any_tag_re = re.compile(r"<\s*/?\s*(thinking|think)\b[^>]*>", re.I)
        self.native_noise_re = re.compile(r"^\s*(?:[*_`\s]*END\s+THINKING\s+BLOCK[*_`\s]*\n*)+", re.I)

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

    def _sid(self, event: AstrMessageEvent):
        return event.unified_msg_origin or "default"

    def _is_command_event(self, event: AstrMessageEvent) -> bool:
        """所有命令输出都不要走发送前兜底清洗。

        原因：发送前清洗器是按同一个 sid 维护 <thinking> 分段状态的。
        如果上一轮刚吞过一个未闭合/分段 thinking，紧接着用户发 /reset、/help、/tm状态
        这类命令时，命令回执也会被当成“仍在 thinking 块内”误吞。

        所以：只要本轮用户原始消息是命令，就直接跳过清洗，并顺手清空该会话的分段状态。
        """
        msg = (getattr(event, "message_str", "") or "").strip()
        if not msg:
            return False

        # AstrBot/QQ 常见命令前缀。这里不要把普通中文句号当命令前缀，避免误伤聊天。
        if msg.startswith(("/", "／", "!", "！")):
            return True

        normalized = msg.lstrip("/／!！\\").strip()
        first = normalized.split()[0] if normalized.split() else normalized
        commands = {
            "tm状态", "reload", "reset", "help", "plugin", "插件",
            "线上模式", "线下模式", "当前模式",
            "最近思考", "查看最近思考", "思考列表", "清空思考",
        }
        return normalized in commands or first in commands

    # 兼容旧方法名，避免后面有地方没改到。
    def _is_self_command_event(self, event: AstrMessageEvent) -> bool:
        return self._is_command_event(event)

    def _looks_like_leaked_reasoning(self, text: str) -> bool:
        """标签外泄露兜底：只检查开头，避免误伤正常正文。"""
        head = (text or "").strip()[:260]
        if not head:
            return False
        markers = (
            "step 0", "step0", "step 1", "step1", "step 2", "step2",
            "【前置规则】", "前置规则", "【角色定位】", "角色定位",
            "【记忆调取】", "记忆调取", "【场景构建】", "场景构建",
            "【剧情推演】", "剧情推演", "【消息回复】", "消息回复",
            "【最后确认】", "最后确认", "让我分析", "分析一下这个场景",
            "根据这个场景", "行为受现实约束", "行为受性格", "现实逻辑"
        )
        return any(m in head for m in markers)

    def _strip_tags(self, text: str, sid: str = "default", stateful: bool = False):
        """
        剥离 <thinking>/<think>。
        stateful=True 时支持分段/流式：如果上一段打开了 <thinking> 未闭合，本段继续吞，直到遇到 </thinking>。
        返回：cleaned_text, thinking_texts, blocked
        """
        if text is None:
            return "", [], False

        src = str(text).replace("\r\n", "\n").replace("\r", "\n")
        thinking_texts = []
        output = []
        blocked = False
        pos = 0

        in_thinking = bool(self._thinking_open_state.get(sid, False)) if stateful else False

        while pos < len(src):
            if in_thinking:
                close_m = self.close_tag_re.search(src, pos)
                if close_m:
                    captured = src[pos:close_m.start()].strip()
                    if captured:
                        thinking_texts.append(captured)
                    pos = close_m.end()
                    in_thinking = False
                    if stateful:
                        self._thinking_open_state[sid] = False
                    blocked = True
                    continue
                else:
                    captured = src[pos:].strip()
                    if captured:
                        thinking_texts.append(captured + "  [分段thinking]")
                    pos = len(src)
                    blocked = True
                    if stateful:
                        self._thinking_open_state[sid] = True
                    break

            open_m = self.open_tag_re.search(src, pos)
            close_m = self.close_tag_re.search(src, pos)

            # 没有开标签。孤立闭合标签直接跳过，普通文本保留。
            if not open_m:
                if close_m:
                    output.append(src[pos:close_m.start()])
                    pos = close_m.end()
                    blocked = True
                    if stateful:
                        self._thinking_open_state[sid] = False
                    continue
                output.append(src[pos:])
                break

            # 如果先遇到孤立闭合标签，先删闭合标签。
            if close_m and close_m.start() < open_m.start():
                output.append(src[pos:close_m.start()])
                pos = close_m.end()
                blocked = True
                if stateful:
                    self._thinking_open_state[sid] = False
                continue

            # 遇到开标签：开标签前的普通正文保留，标签内开始吞。
            output.append(src[pos:open_m.start()])
            pos = open_m.end()
            close_after = self.close_tag_re.search(src, pos)
            if close_after:
                captured = src[pos:close_after.start()].strip()
                if captured:
                    thinking_texts.append(captured)
                pos = close_after.end()
                blocked = True
                if stateful:
                    self._thinking_open_state[sid] = False
                continue
            else:
                captured = src[pos:].strip()
                if captured:
                    thinking_texts.append(captured + "  [未闭合]")
                pos = len(src)
                blocked = True
                in_thinking = True
                if stateful:
                    self._thinking_open_state[sid] = True
                break

        cleaned = "".join(output)
        cleaned = self.any_tag_re.sub("", cleaned)
        cleaned = self.native_noise_re.sub("", cleaned)
        cleaned = cleaned.strip()

        # 如果当前片段没有标签，但明显是思维步骤，直接吞掉。
        # 注意：这里不要把 _thinking_open_state 置为 True。
        # 只有真的看到 <thinking> 未闭合时才允许进入跨消息吞噬状态；
        # 否则一个孤立的 “step 0” 会把后续 /reset、/help 等命令也全部误吞。
        if cleaned and self._looks_like_leaked_reasoning(cleaned):
            thinking_texts.append(cleaned + "  [标签外泄露]")
            cleaned = ""
            blocked = True

        return cleaned, thinking_texts, blocked

    def _record_thinking(self, event: AstrMessageEvent, thinking_texts, source="strip"):
        if not thinking_texts:
            return
        sid = self._sid(event)
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": self.current_mode,
            "user": event.get_sender_name() or event.get_sender_id() or "unknown",
            "user_message": self._last_user_msg.get(sid, ""),
            "thinking": "\n\n".join([t for t in thinking_texts if t]),
            "source": source,
        }
        self.history.append(entry)
        self.history = self.history[-self.max_history:]
        self._save_history()

    def _is_plain_component(self, seg) -> bool:
        if isinstance(seg, str):
            return True
        if Plain is not None and isinstance(seg, Plain):
            return True
        return hasattr(seg, "text") and isinstance(getattr(seg, "text"), str)

    def _set_response_text(self, resp, text: str):
        """同时改 completion_text 和 result_chain，避免新版本只读 result_chain 时漏掉。"""
        safe_text = text or ""
        try:
            resp.completion_text = safe_text
        except Exception as e:
            logger.warning(f"[ThinkingMaster] 设置 completion_text 失败: {e}")

        chain_obj = getattr(resp, "result_chain", None)
        chain = getattr(chain_obj, "chain", None)
        if chain is None:
            return

        try:
            other_parts = [seg for seg in list(chain) if not self._is_plain_component(seg)]
            new_chain = []
            if safe_text.strip():
                if Plain is not None:
                    new_chain.append(Plain(safe_text.strip()))
                else:
                    new_chain.append(safe_text.strip())
            new_chain.extend(other_parts)
            chain_obj.chain = new_chain
        except Exception as e:
            logger.warning(f"[ThinkingMaster] 设置 result_chain 失败: {e}")

    def _scrub_response_obj(self, event: AstrMessageEvent, resp, source: str):
        sid = self._sid(event)
        all_thinking = []

        if hasattr(resp, "reasoning_content") and getattr(resp, "reasoning_content", None):
            reasoning = str(getattr(resp, "reasoning_content") or "").strip()
            if reasoning:
                all_thinking.append(f"[原生thinking]\n{reasoning}")
            try:
                resp.reasoning_content = ""
            except Exception:
                pass

        raw_text = str(getattr(resp, "completion_text", "") or "")
        cleaned, thinking_texts, blocked = self._strip_tags(raw_text, sid=sid, stateful=False)
        all_thinking.extend(thinking_texts)

        # 关键：不管有没有检测到 blocked，只要 cleaned 和 raw 不同，都强制写回。
        if blocked or cleaned != raw_text.strip():
            self._set_response_text(resp, cleaned)
            logger.warning(
                f"[ThinkingMaster] 🧽 {source} 已清洗 | raw_len={len(raw_text)} | clean_len={len(cleaned)} | blocked={blocked}"
            )

        if all_thinking:
            self._record_thinking(event, all_thinking, source=source)
        else:
            logger.warning(f"[ThinkingMaster] ⚠️ {source} 未检测到 <thinking> 标签 | sid={sid}")

    @filter.on_llm_request(priority=10)
    async def inject_cot(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable_inject:
            logger.debug("[ThinkingMaster] enable_inject=False，跳过注入")
            return

        sid = self._sid(event)
        self._last_user_msg[sid] = (event.message_str or "")[:100]
        self._thinking_open_state[sid] = False

        existing = (req.system_prompt or "").strip()
        active = self._get_active_prompt()

        req.system_prompt = (
            self.native_block_prompt + "\n\n" + active + "\n\n" + existing
        ).strip()

        if hasattr(req, "prompt") and req.prompt:
            req.prompt = req.prompt + COT_REMINDER

        logger.info(
            f"[ThinkingMaster] ✅ 注入成功 | sid={sid} | mode={self.current_mode} | prompt长度={len(req.system_prompt)}"
        )

    @filter.on_llm_response(priority=-1000)
    async def strip_cot(self, event: AstrMessageEvent, resp):
        # 旧 LLM 完成钩子：先清一遍。
        self._scrub_response_obj(event, resp, source="on_llm_response")

    @_optional_hook("on_agent_done", priority=-1000)
    async def strip_cot_agent_done(self, event: AstrMessageEvent, run_context, resp):
        # 新版 AstrBot Agent 完成钩子：on_llm_response 之后还可能重组最终回复，所以这里再清一遍。
        self._scrub_response_obj(event, resp, source="on_agent_done")

    @filter.on_decorating_result(priority=-10000)
    async def final_scrub_before_send(self, event: AstrMessageEvent):
        """发送前最后一道保险。特别处理流式/分段发送：一旦看到 <thinking>，后续分段继续吞到 </thinking>。"""
        try:
            sid = self._sid(event)

            if self._is_command_event(event):
                # 命令回执不属于 LLM 正文，绝不能被 thinking 兜底过滤器吞掉。
                # 同时清掉可能残留的跨分段状态，避免 /reset 之后仍然 empty=True。
                self._thinking_open_state[sid] = False
                logger.debug("[ThinkingMaster] 跳过命令输出清洗，并清空分段屏蔽状态")
                return

            result = event.get_result()
            chain = getattr(result, "chain", None)
            if chain is None:
                return

            text_parts = []
            other_parts = []
            for seg in list(chain):
                if isinstance(seg, str):
                    text_parts.append(seg)
                elif hasattr(seg, "text") and isinstance(getattr(seg, "text"), str):
                    text_parts.append(getattr(seg, "text"))
                else:
                    other_parts.append(seg)

            if not text_parts:
                return

            joined = "\n".join(text_parts)
            cleaned, thinking_texts, blocked = self._strip_tags(joined, sid=sid, stateful=True)

            if not blocked and cleaned == joined.strip():
                return

            if thinking_texts:
                self._record_thinking(event, thinking_texts, source="final_scrub_before_send")

            # 重建 chain，比逐段清空更稳，避免某些适配器已经拿到旧 seg 的引用。
            try:
                chain.clear()
            except Exception:
                try:
                    result.chain = []
                    chain = result.chain
                except Exception:
                    pass

            # 有正文则发送正文；没正文说明这条只是思维链片段，直接阻断这条消息。
            if cleaned:
                if Plain is not None:
                    chain.append(Plain(cleaned))
                else:
                    chain.append(cleaned)
                for seg in other_parts:
                    chain.append(seg)
            else:
                try:
                    event.stop_event()
                except Exception:
                    pass

            logger.warning(f"[ThinkingMaster] 🧹 发送前兜底清洗完成 | blocked={blocked} | empty={not bool(cleaned)}")
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
            self._thinking_open_state = {}

            logger.info(f"[ThinkingMaster] 🔄 reload 完成 | enable_inject={self.enable_inject} | mode={self.current_mode}")
            yield event.plain_result(
                f"🔄 ThinkingMaster 已重载\n"
                f"├ enable_inject: {self.enable_inject}\n"
                f"├ 当前模式: {self.current_mode}\n"
                f"├ 历史记录: {len(self.history)} 条\n"
                f"├ 版本: 0.7.8-command-bypass\n"
                f"└ native_block: {'自定义' if self.native_block_prompt != DEFAULT_NATIVE_BLOCK else '默认'}"
            )
        except Exception as e:
            logger.error(f"[ThinkingMaster] reload 失败: {e}")
            yield event.plain_result(f"❌ reload 失败: {e}")

    # ─── /tm状态 ───────────────────────────────────────────────────────────
    @filter.command("tm状态")
    async def cmd_debug(self, event: AstrMessageEvent):
        prompt_preview = self._get_active_prompt()[:80].replace("\n", " ")
        sid = self._sid(event)
        yield event.plain_result(
            f"🔍 ThinkingMaster 状态\n"
            f"├ enable_inject: {self.enable_inject}\n"
            f"├ 当前模式: {self.current_mode}\n"
            f"├ 历史记录: {len(self.history)} 条\n"
            f"├ 版本: 0.7.8-command-bypass\n"
            f"├ 分段屏蔽状态: {self._thinking_open_state.get(sid, False)}\n"
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
            f"触发: {latest.get('user_message','')}\n"
            f"来源: {latest.get('source','?')}\n\n{latest.get('thinking','')}"
        )
        yield event.plain_result(text[:1800])

    @filter.command("查看最近思考")
    async def cmd_recent_alias(self, event: AstrMessageEvent):
        async for r in self.cmd_recent(event):
            yield r

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
