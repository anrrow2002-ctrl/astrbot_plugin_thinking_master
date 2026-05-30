import re
import json
import os
import asyncio
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




@register(
    "astrbot_plugin_thinking_master",
    "张安若",
    "思维链注入+原生CoT屏蔽+双模式",
    "0.5.0"
)
class ThinkingMaster(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        config = config or {}
        self.enable_inject = config.get("enable_inject", True)

        online = config.get("online_prompt", "").strip()
        offline = config.get("offline_prompt", "").strip()
        self.online_prompt = online or DEFAULT_COT_PROMPT
        self.offline_prompt = offline or DEFAULT_OFFLINE_PROMPT

        self.max_history = config.get("max_history", 200)
        self.panel_port = int(config.get("panel_port", 7799))

        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.history_file = os.path.join(plugin_dir, "thinking_history.json")
        self.debug_log_file = os.path.join(plugin_dir, "thinking_debug.jsonl")
        self.mode_file = os.path.join(plugin_dir, "current_mode.txt")

        default_mode = config.get("default_mode", "online")
        self.current_mode = self._load_mode(default_mode)
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

        # 面板已禁用（Docker环境无端口映射）

    # ── 持久化 ──

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
        # 优先从 jsonl debug 日志加载（更完整）
        records = []
        try:
            if os.path.exists(self.debug_log_file):
                with open(self.debug_log_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                records.append(json.loads(line))
                            except Exception:
                                pass
                return records[-self.max_history:]
        except Exception:
            pass
        # 兜底读旧 json
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _append_debug_log(self, entry: dict):
        """追加一条到 jsonl，不重写整个文件"""
        try:
            with open(self.debug_log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"写debug日志失败: {e}")

    def _save_history(self):
        # 兼容旧格式，也写一份 json
        try:
            with open(self.history_file, "w", encoding="utf-8") as f:
                json.dump(self.history[-self.max_history:], f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存思考记录失败: {e}")

    def _get_active_prompt(self):
        return self.offline_prompt if self.current_mode == "offline" else self.online_prompt


    # ── 核心逻辑 ──

    def _do_strip(self, text: str):
        """剥离thinking标签，返回 (正文, thinking列表, is_repaired, is_fallback)"""
        repaired_once = False
        fallback_once = False
        for open_tag, close_tag in [("<thinking>", "</thinking>"), ("<think>", "</think>")]:
            lo = open_tag.lower()
            tl = text.lower()
            open_pos = tl.find(lo)
            if open_pos != -1 and tl.find(close_tag.lower(), open_pos) == -1:
                content_start = open_pos + len(open_tag)
                rest = text[content_start:]
                split_pos = rest.find("\n\n")
                if split_pos != -1 and split_pos < len(rest) - 1:
                    thinking_part = text[:content_start + split_pos]
                    body_part = rest[split_pos:].strip()
                    text = thinking_part + "\n" + close_tag + "\n\n" + body_part
                    logger.info(f"[thinking_master] 未闭合{open_tag}，双换行切割修复，正文保留{len(body_part)}字符")
                else:
                    text = text.rstrip() + "\n" + close_tag
                    logger.warning(f"[thinking_master] 未闭合{open_tag}且无正文分隔，正文可能被吞！")
                repaired_once = True
                break

        thinking_texts = []
        for p in self.closed_patterns:
            for match in p.findall(text):
                content = match.strip()
                if repaired_once:
                    content += "  [自动补全闭合]"
                thinking_texts.append(content)
            text = p.sub("", text)
        for p in self.unclosed_patterns:
            m = p.search(text)
            if m:
                thinking_texts.append(m.group(1).strip() + "  [未闭合-兜底]")
                text = p.sub("", text)
                fallback_once = True

        return text.strip(), thinking_texts, repaired_once, fallback_once

    @filter.on_llm_request()
    async def inject_cot(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.enable_inject:
            return
        sid = event.unified_msg_origin or "default"
        self._last_user_msg[sid] = (event.message_str or "")[:100]
        existing = (req.system_prompt or "").strip()
        active = self._get_active_prompt()
        req.system_prompt = (existing + "\n\n" + active).strip()

    @filter.on_llm_response()
    async def strip_cot(self, event: AstrMessageEvent, resp):
        # ── 处理 Node（合并转发）类型 ──
        if hasattr(resp, 'result_chain') and resp.result_chain:
            # MessageChain 不可直接迭代，需访问 .chain 属性
            chain_items = None
            if hasattr(resp.result_chain, 'chain'):
                chain_items = resp.result_chain.chain
            elif hasattr(resp.result_chain, '__iter__'):
                try:
                    chain_items = list(resp.result_chain)
                except TypeError:
                    chain_items = None
            if chain_items is not None:
                for node in chain_items:
                    if hasattr(node, 'text') and isinstance(node.text, str) and node.text:
                        node.text, _, _, _ = self._do_strip(node.text)
                return

        raw_text = str(resp.completion_text or "")
        reasoning_text = str(resp.reasoning_content or "") if hasattr(resp, "reasoning_content") else ""

        # AstrBot 4.25+ 已原生分离 reasoning_content，只有 completion_text 里还混有标签才剥离
        has_tag = any(tag in raw_text.lower() for tag in ["<thinking>", "<think>", "</thinking>", "</think>"])
        if has_tag:
            text, thinking_texts, is_repaired, is_fallback = self._do_strip(raw_text)
            resp.completion_text = text
        else:
            text = raw_text
            thinking_texts = [reasoning_text] if reasoning_text else []
            is_repaired = False
            is_fallback = False

        is_empty = not text.strip() and bool(thinking_texts)
        if is_empty:
            logger.warning("[thinking_master] 正文为空！")

        # ── 写 debug 日志 ──
        sid = event.unified_msg_origin or "default"
        entry = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": self.current_mode,
            "user": event.get_sender_name() or event.get_sender_id() or "unknown",
            "user_message": self._last_user_msg.get(sid, ""),
            "raw_output": raw_text[:800],
            "body": text[:400],
            "thinking": (reasoning_text or "\n\n".join(thinking_texts))[:600],
            "is_empty": is_empty,
            "is_repaired": is_repaired,
            "is_fallback": is_fallback,
        }

        if thinking_texts or is_empty:
            self._append_debug_log(entry)
            self.history.append(entry)
            self.history = self.history[-self.max_history:]
            self._save_history()
            logger.info(f"[思考已记录][{self.current_mode}] {entry['time']} empty={is_empty} repaired={is_repaired}")

    # ── 指令 ──

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
            f"{i}. [{e.get('mode','?')}] {e['time']}"
            f"{'🔴' if e.get('is_empty') else '🟢'}"
            f"{'🔧' if e.get('is_repaired') else ''}"
            f" {e.get('user_message','')[:25]}"
            for i, e in enumerate(self.history[-10:][::-1], 1)
        ]
        yield event.plain_result("📋 最近10条 (🔴空回 🔧修复):\n" + "\n".join(lines))

    @filter.command("清空思考")
    async def cmd_clear(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("只有管理员能清空")
            return
        self.history = []
        self._save_history()
        try:
            open(self.debug_log_file, "w").close()
        except Exception:
            pass
        yield event.plain_result("✅ 已清空")