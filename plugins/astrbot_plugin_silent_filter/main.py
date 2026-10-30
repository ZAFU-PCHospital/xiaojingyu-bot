import re
import asyncio
import random

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, At, AtAll, Image
from astrbot.core.star.filter.custom_filter import CustomFilter
from astrbot.api.star import Context, Star, register

SILENT_RE = re.compile(
    r"(?:\[SILENT\]|【SILENT】|［SILENT］)",
    re.IGNORECASE,
)


def scrub_silent(text: str) -> str:
    if not text:
        return text
    return SILENT_RE.sub("", text)


# Only explicitly bound friend sessions participate. This filter runs before
# native waking, so loading this plugin does not wake unrelated sessions.
_AUTO_REPLY_SESSIONS = set()
_AUTO_REPLY_PROBABILITY = 0.0

class FriendAutoReplyScope(CustomFilter):
    def filter(self, event, cfg):
        return str(event.unified_msg_origin) in _AUTO_REPLY_SESSIONS


def split_friend_text(text: str, cfg: dict) -> list[str]:
    if not text or '```' in text:
        return [text]
    parts = [x.strip() for x in re.split(r'\n[ \t]*\n+', text) if x.strip()]
    if len(parts) < 2:
        if len(text) > int(cfg.get('words_count_threshold', 150)):
            return [text]
        try:
            parts = re.findall(cfg.get('regex', r'.*?[。？！~…]+|.+$'), text, re.DOTALL | re.MULTILINE)
            if any(not isinstance(x, str) for x in parts):
                return [text]
            parts = [x.strip() for x in parts if x.strip()]
        except re.error:
            return [text]
    if len(parts) > 3:
        parts = parts[:2] + ['\n\n'.join(parts[2:])]
    return parts or [text]


@register(
    "astrbot_plugin_silent_filter",
    "local",
    "去掉模型误输出的 [SILENT] 字样。已取消静默：被 @/唤醒时若只剩 SILENT 则改成「？」。",
    "0.2",
)
class SilentFilterPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        global _AUTO_REPLY_SESSIONS, _AUTO_REPLY_PROBABILITY
        cfg = config if config is not None else {}
        self.config = cfg
        _AUTO_REPLY_SESSIONS = set(map(str, cfg.get('auto_reply_sessions', [])))
        _AUTO_REPLY_PROBABILITY = max(0.0, min(1.0, float(cfg.get('auto_reply_probability', 0.0))))
        logger.info('friend features active: sessions=%d, probability=%.2f, separate=%s', len(_AUTO_REPLY_SESSIONS), _AUTO_REPLY_PROBABILITY, bool(cfg.get('separate_official_messages', False)))

    @filter.custom_filter(FriendAutoReplyScope)
    async def maybe_reply_to_friend_group(self, event: AstrMessageEvent):
        parts = event.get_messages()
        if str(event.get_sender_id()) == str(event.get_self_id()):
            event.stop_event()
            return
        raw = getattr(event.message_obj, 'raw_message', None)
        author = raw.get('author', {}) if isinstance(raw, dict) else getattr(raw, 'author', None)
        author_is_bot = author.get('bot') if isinstance(author, dict) else getattr(author, 'bot', False)
        if author_is_bot is True:
            event.stop_event()
            return
        if any(isinstance(p, At) and str(p.qq) == str(event.get_self_id()) for p in parts):
            return
        if any(isinstance(p, (At, AtAll)) for p in parts):
            event.stop_event()
            return
        if getattr(event, 'is_at_or_wake_command', False):
            return
        # An unaddressed image-only message should not wake the conversational
        # agent, but it must still reach other handlers such as meme collection.
        eligible = any(isinstance(p, Plain) and p.text.strip() for p in parts)
        if not eligible and any(isinstance(p, Image) for p in parts):
            return
        if not eligible or random.random() >= _AUTO_REPLY_PROBABILITY:
            event.stop_event()
            return
        # AstrBot 4.29 requires an explicit wake flag for its native agent.
        # should_call_llm(True) actually suppresses that default agent path.
        event.is_at_or_wake_command = True
        event.should_call_llm(False)
        logger.info('friend auto reply selected: origin=%s inbound=%s', event.unified_msg_origin, str(event.message_obj.message_id))

    @filter.on_llm_request()
    async def focus_friend_chat_on_text(self, event, req):
        if str(event.unified_msg_origin) not in _AUTO_REPLY_SESSIONS:
            return
        req.system_prompt = (req.system_prompt or "") + (
            "\n[群聊图片] 别人发的表情包通常只是表达情绪，不必点评、"
            "解读或描述画面。优先自然回应文字和聊天语境；只有用户明确"
            "要求看图、问图片内容，或理解当前问题确实需要时才讨论图片。"
            "用户要求看图时正常识图回答。\n"
        )

    @filter.on_llm_response()
    async def scrub_llm_response(self, event: AstrMessageEvent, response) -> None:
        if str(event.unified_msg_origin) not in _AUTO_REPLY_SESSIONS:
            return
        if getattr(response, "is_chunk", False):
            return
        text = response.completion_text or ""
        if not SILENT_RE.search(text):
            return
        cleaned = scrub_silent(text).strip()
        woke = getattr(event, "is_at_or_wake_command", False) or event.is_wake_up()
        if not cleaned and woke:
            cleaned = "？"
        chain = getattr(response, "result_chain", None)
        if chain is not None:
            new_comps = []
            for comp in list(chain.chain):
                if isinstance(comp, Plain):
                    piece = scrub_silent(comp.text or "").strip()
                    if piece:
                        comp.text = piece
                        new_comps.append(comp)
                else:
                    new_comps.append(comp)
            chain.chain = new_comps
            if cleaned and not any(isinstance(c, Plain) for c in chain.chain):
                chain.chain.insert(0, Plain(cleaned))
        else:
            response.completion_text = cleaned
        logger.info("stripped leftover [SILENT], remaining %d chars", len(cleaned))

    @filter.on_decorating_result()
    async def drop_silent_outgoing(self, event: AstrMessageEvent) -> None:
        if str(event.unified_msg_origin) not in _AUTO_REPLY_SESSIONS:
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        changed = False
        new_chain = []
        for comp in result.chain:
            if isinstance(comp, Plain):
                original = comp.text or ""
                cleaned = scrub_silent(original)
                if cleaned != original:
                    changed = True
                if cleaned.strip():
                    comp.text = cleaned
                    new_chain.append(comp)
                else:
                    changed = True
            else:
                new_chain.append(comp)
        if not changed:
            return
        if not new_chain:
            # This hook also sees intermediate tool-turn text. A SILENT-only
            # placeholder is not a final answer and must not create a bubble.
            # Final SILENT fallback stays in scrub_llm_response, called by
            # MainAgentHooks.on_agent_done after the agent has completed.
            result.chain.clear()
            logger.info("suppressed SILENT-only outgoing placeholder")
            return
        result.chain[:] = new_chain
        logger.info("stripped leftover [SILENT] from outgoing message")

    @filter.on_decorating_result(priority=-1000)
    async def split_official_friend_reply(self, event: AstrMessageEvent):
        if not self.config.get('separate_official_messages', False):
            return
        if event.get_platform_name() != 'qq_official_webhook' or str(event.unified_msg_origin) not in _AUTO_REPLY_SESSIONS:
            return
        result = event.get_result()
        if result is None or not result.chain or not all(isinstance(x, Plain) for x in result.chain):
            return
        cfg = self.context.get_config(event.unified_msg_origin).get('platform_settings', {}).get('segmented_reply', {})
        if not cfg.get('enable') or (cfg.get('only_llm_result', True) and not result.is_model_result()):
            return
        if len(split_friend_text(''.join(x.text for x in result.chain), cfg)) < 2 or event.get_extra('_friend_split_send', False):
            return
        try:
            intervals = [float(x.strip()) for x in str(cfg.get('interval', '1.5,3.5')).split(',')]
            low, high = sorted(intervals[:2]) if len(intervals) >= 2 else (1.5, 3.5)
            low, high = max(0.0, low), min(10.0, max(0.0, high))
            if low > high:
                low, high = 1.5, 3.5
        except (TypeError, ValueError):
            low, high = 1.5, 3.5
        original_send = event.send
        event.set_extra('_friend_split_send', True)

        async def send_separately(chain):
            try:
                if not chain.chain or not all(isinstance(x, Plain) for x in chain.chain):
                    return await original_send(chain)
                parts = split_friend_text(''.join(x.text for x in chain.chain), cfg)
                for i, text in enumerate(parts):
                    if i:
                        await asyncio.sleep(random.uniform(low, high))
                    await original_send(chain.derive([Plain(text)]))
                    logger.info('official friend split send returned: part %d/%d', i + 1, len(parts))
            finally:
                event.send = original_send
                event.set_extra('_friend_split_send', False)

        # Scope this compatibility shim to this event only. Keep the normal
        # respond stage and after-send hooks; never replay already-sent pieces.
        event.send = send_separately
