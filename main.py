import json
import re
import time
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain
from astrbot.api import logger


# ============ 内置默认提示词 ============

DEFAULT_JUDGE_PROMPT = """你是一个消息意图分类器。当前场景是【用户私聊机器人】，用户可能想让机器人帮忙给另一个人传话。

请判断用户消息是否属于传话请求。

属于传话请求的特征：
- 用户明确表达“替我/帮我给某人说某话”的意图
- 用户在对另一个人喊话，而不是在对机器人说话
- 消息中出现了“转告、带话、说一声、问一下、跟某人说”等表达
- 用户提供了明确的接收者对象和要传达的内容

不属于的特征：
- 闲聊、问机器人问题、向机器人求助
- 命令机器人执行某个功能
- 与传话无关的内容
- 消息中无法确定唯一的接收者

如果是传话请求，输出严格 JSON（不要 markdown 代码块，不要多余文字）：
{"is_forward": true, "target": "接收者昵称（只保留人名，去掉'给/发/说/跟/帮'等词）", "content": "要传达的核心内容摘要"}

如果不是，输出：
{"is_forward": false}
"""

DEFAULT_GENERATE_PROMPT = """你是一个消息代发助手。现在需要你把发送者想传达的内容，用发送者本人的口吻，转述成一条自然口语化的消息，直接发给接收者。

要求：
- 以发送者本人的口吻说话，例如“XX说：……”或“XX问……”
- 保持自然口语，可带语气词，但不要编造原文没有的信息
- 只输出这一句消息，不要加引号、前缀、解释或换行
- 长度不超过 100 字
- 如果内容本身已经足够口语化，可以直接使用，无需强行改写
"""

DEFAULT_FEEDBACK_PROMPT = """你正在回复用户的传话请求。请根据下面的发送结果，用你的角色口吻，简短地告诉用户结果。

发送结果类型：
- success：消息已成功送达
- not_found：找不到接收者
- multi_match：存在多个同名接收者
- fail：发送失败

要求：
- 只输出一句话，不要加引号、前缀、解释、换行
- 保持你的角色语气，但不要编造发送结果
- 长度不超过 30 字
"""

# ============ 内置默认正则 ============

DEFAULT_REGEX_PATTERNS = [
    r"给\s*(.+?)\s*发\s*[\"“”'‘’]?(.+?)[\"“”'‘’]?\s*$",
    r"告诉\s*(.+?)\s*[，,]?\s*(.+)$",
    r"跟\s*(.+?)\s*说\s*[，,]?\s*(.+)$",
    r"转告\s*(.+?)\s*[，,]?\s*(.+)$",
]

# ============ 写死的反馈文案（简单模式） ============

FIXED_REPLY_SUCCESS = "已传达给{target}"
FIXED_REPLY_NOT_FOUND = "未找到“{target}”"
FIXED_REPLY_MULTI_MATCH = "找到多个“{target}”"
FIXED_REPLY_FAIL = "发送失败"

# ============ 写死的限制 ============

MAX_MESSAGE_LENGTH = 500  # 超过此长度的消息直接忽略


class MessageForwarderPlugin(Star):
    """私聊消息代发插件"""

    def __init__(self, context: Context):
        super().__init__(context)
        self._friend_cache = {}
        self._cache_time = 0
        self._detected_memory_plugin = None  # 首次扫描结果缓存

    # ==================== 配置读取 ====================

    def _get_ttl(self) -> int:
        raw = self.config.get("cache_ttl", 1800)
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            return 1800

    def _is_debug(self) -> bool:
        return bool(self.config.get("debug_mode", False))

    # ==================== 好友缓存 ====================

    async def _refresh_friend_cache(self, event) -> bool:
        try:
            friend_list = await event.bot.api.call_action('get_friend_list')
            new_cache = {}
            for f in friend_list:
                uid = str(f.get('user_id', ''))
                if uid:
                    new_cache[uid] = {
                        "nickname": f.get('nickname', ''),
                        "remark": f.get('remark', ''),
                    }
            self._friend_cache = new_cache
            self._cache_time = time.time()
            logger.info(f"[好友缓存] 刷新成功，共 {len(new_cache)} 位好友，TTL={self._get_ttl()}s")
            return True
        except Exception as e:
            logger.error(f"[好友缓存] 刷新失败: {e}")
            return False

    async def _ensure_cache(self, event):
        ttl = self._get_ttl()
        if ttl == 0:
            await self._refresh_friend_cache(event)
            return
        if not self._friend_cache or (time.time() - self._cache_time) > ttl:
            await self._refresh_friend_cache(event)

    def _is_friend_cached(self, target_user_id) -> bool:
        return str(target_user_id) in self._friend_cache

    def _find_friend_by_name(self, name: str):
        matched = []
        for uid, info in self._friend_cache.items():
            if name == info.get("nickname") or name == info.get("remark"):
                matched.append(uid)
        return matched

    def _find_by_alias_map(self, name: str):
        raw = self.config.get("alias_map", "")
        if not raw:
            return None
        raw = str(raw).replace("，", ",")
        for item in raw.split(","):
            if ":" in item:
                k, v = item.split(":", 1)
                if k.strip() == name:
                    return v.strip()
        return None

    # ==================== 记忆插件查询 ====================

    async def _detect_memory_plugin(self):
        """首次调用时扫描一次，缓存结果"""
        if self._detected_memory_plugin is not None:
            return self._detected_memory_plugin

        candidates = [
            ("livingmemory", "astrbot_plugin_livingmemory"),
            ("mnemosyne", "astrbot_plugin_mnemosyne"),
            ("memory_companion", "astrbot_plugin_memory_companion"),
        ]

        for key, plugin_id in candidates:
            try:
                if hasattr(self.context, "get_registered_star"):
                    star = self.context.get_registered_star(plugin_id)
                    if star:
                        self._detected_memory_plugin = key
                        logger.info(f"[记忆查询] 检测到记忆插件: {plugin_id}")
                        return key
            except Exception as e:
                logger.warning(f"[记忆查询] 检测 {plugin_id} 时异常: {e}")

        self._detected_memory_plugin = "none"
        logger.warning("[记忆查询] 未检测到任何已安装的记忆插件")
        return "none"

    def _get_memory_plugin_instance(self, plugin_id):
        try:
            if hasattr(self.context, "get_registered_star"):
                return self.context.get_registered_star(plugin_id)
        except Exception:
            pass
        return None

    async def _query_livingmemory(self, name: str):
        star = self._get_memory_plugin_instance("astrbot_plugin_livingmemory")
        if not star:
            return None
        try:
            engine = None
            # v2 架构
            if hasattr(star, "initializer") and hasattr(star.initializer, "memory_engine"):
                engine = star.initializer.memory_engine
            # v1 架构
            elif hasattr(star, "memory_engine"):
                engine = star.memory_engine

            if not engine:
                logger.warning("[记忆查询] LivingMemory 无法获取 memory_engine")
                return None

            # 尝试常见查询接口
            for method_name in ("query_alias", "lookup_alias", "find_user_by_name", "search_alias"):
                if hasattr(engine, method_name):
                    method = getattr(engine, method_name)
                    result = await method(name) if callable(method) else None
                    if result:
                        return str(result)

            logger.warning(f"[记忆查询] LivingMemory 未找到“{name}”的别名")
            return None
        except Exception as e:
            logger.error(f"[记忆查询] LivingMemory 调用异常: {e}")
            return None

    async def _query_mnemosyne(self, name: str):
        star = self._get_memory_plugin_instance("astrbot_plugin_mnemosyne")
        if not star:
            return None
        try:
            engine = getattr(star, "memory_engine", None)
            if not engine:
                logger.warning("[记忆查询] Mnemosyne 无法获取 memory_engine")
                return None

            for method_name in ("query_alias", "lookup_alias", "find_user_by_name", "search_alias"):
                if hasattr(engine, method_name):
                    method = getattr(engine, method_name)
                    result = await method(name) if callable(method) else None
                    if result:
                        return str(result)

            logger.warning(f"[记忆查询] Mnemosyne 未找到“{name}”的别名")
            return None
        except Exception as e:
            logger.error(f"[记忆查询] Mnemosyne 调用异常: {e}")
            return None

    async def _query_memory_companion(self, name: str):
        star = self._get_memory_plugin_instance("astrbot_plugin_memory_companion")
        if not star:
            return None
        try:
            engine = getattr(star, "memory_engine", None) or getattr(star, "engine", None)
            if not engine:
                logger.warning("[记忆查询] Memory Companion 无法获取 memory_engine")
                return None

            for method_name in ("query_alias", "lookup_alias", "find_user_by_name", "search_alias"):
                if hasattr(engine, method_name):
                    method = getattr(engine, method_name)
                    result = await method(name) if callable(method) else None
                    if result:
                        return str(result)

            logger.warning(f"[记忆查询] Memory Companion 未找到“{name}”的别名")
            return None
        except Exception as e:
            logger.error(f"[记忆查询] Memory Companion 调用异常: {e}")
            return None

    async def _query_memory_alias(self, name: str):
        """统一入口：按配置模式查询记忆插件"""
        if not self.config.get("enable_memory_lookup", False):
            return None

        mode = str(self.config.get("memory_plugin_mode", "auto")).strip()

        if mode == "off":
            return None

        if mode == "livingmemory":
            return await self._query_livingmemory(name)
        if mode == "mnemosyne":
            return await self._query_mnemosyne(name)
        if mode == "memory_companion":
            return await self._query_memory_companion(name)

        # auto 模式：按优先级依次尝试
        detected = await self._detect_memory_plugin()
        if detected == "none":
            return None

        for key, fn in [
            ("livingmemory", self._query_livingmemory),
            ("mnemosyne", self._query_mnemosyne),
            ("memory_companion", self._query_memory_companion),
        ]:
            try:
                result = await fn(name)
                if result:
                    return result
            except Exception as e:
                logger.warning(f"[记忆查询] {key} 查询异常: {e}")

        return None

    # ==================== LLM ====================

    async def _get_default_provider(self, event):
        try:
            try:
                return self.context.get_using_provider(umo=event.unified_msg_origin)
            except TypeError:
                return self.context.get_using_provider()
        except Exception as e:
            logger.error(f"[LLM] 获取会话默认 provider 失败: {e}")
            return None

    async def _get_judge_provider(self, event):
        judge_id = str(self.config.get("judge_llm_provider_id", "")).strip()
        if judge_id:
            try:
                if hasattr(self.context, "get_provider_by_id"):
                    provider = self.context.get_provider_by_id(judge_id)
                    if provider:
                        return provider
            except Exception as e:
                logger.warning(f"[LLM] get_provider_by_id 失败: {e}")
            try:
                pm = getattr(self.context, "provider_manager", None)
                if pm and hasattr(pm, "get_provider_by_id"):
                    provider = pm.get_provider_by_id(judge_id)
                    if provider:
                        return provider
            except Exception as e:
                logger.warning(f"[LLM] provider_manager 获取失败: {e}")
            logger.warning(f"[LLM] 未找到 judge_llm_provider_id={judge_id}，回退到会话默认")
        return await self._get_default_provider(event)

    async def _judge_intent(self, event, message_str: str, sender_name: str):
        provider = await self._get_judge_provider(event)
        if not provider:
            logger.error("[LLM] 无可用 provider 用于判断")
            return None

        user_prompt = (
            f"发送者昵称：{sender_name}\n"
            f"消息内容：{message_str}\n\n"
            f"请按要求输出 JSON。"
        )

        system_prompt = self.config.get("judge_llm_system_prompt", "").strip() or DEFAULT_JUDGE_PROMPT

        try:
            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                contexts=[],
            )
            text = (response.completion_text or "").strip()

            if self._is_debug():
                logger.info(f"[DEBUG] 判断 LLM 原始返回: {text[:500]}")

            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if not json_match:
                logger.warning(f"[LLM] 未返回 JSON: {text[:200]}")
                return None
            data = json.loads(json_match.group(0))
            return data
        except json.JSONDecodeError as e:
            logger.error(f"[LLM] 解析 JSON 失败: {e}")
            return None
        except Exception as e:
            logger.error(f"[LLM] 判断调用失败: {e}")
            return None

    async def _generate_message(self, event, sender_name: str, target: str, content: str):
        use_llm = (
            self.config.get("enable_llm_extension", False)
            and self.config.get("enable_llm_generate", True)
        )

        if not use_llm:
            template = str(self.config.get("simple_generate_template", "") or "{sender_name}说：{content}")
            try:
                return template.format(sender_name=sender_name, target=target, content=content)
            except (KeyError, IndexError):
                return f"{sender_name}说：{content}"

        provider = await self._get_default_provider(event)
        if not provider:
            template = str(self.config.get("simple_generate_template", "") or "{sender_name}说：{content}")
            try:
                return template.format(sender_name=sender_name, target=target, content=content)
            except (KeyError, IndexError):
                return f"{sender_name}说：{content}"

        user_prompt = (
            f"发送者：{sender_name}\n"
            f"接收者：{target}\n"
            f"要传达的内容：{content}\n\n"
            f"请以发送者口吻生成一句转述消息。"
        )
        system_prompt = self.config.get("generate_system_prompt", "").strip() or DEFAULT_GENERATE_PROMPT

        try:
            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                contexts=[],
            )
            text = (response.completion_text or "").strip()

            if self._is_debug():
                logger.info(f"[DEBUG] 生成 LLM 原始返回: {text[:500]}")

            text = text.strip().strip('"').strip("'").strip("“”").strip("‘’")
            text = text.replace("\n", " ").strip()
            if len(text) > 200:
                text = text[:200]
            return text if text else f"{sender_name}说：{content}"
        except Exception as e:
            logger.error(f"[LLM] 生成调用失败: {e}")
            return f"{sender_name}说：{content}"

    async def _generate_feedback(self, event, result_type: str, target_name: str):
        use_llm = (
            self.config.get("enable_llm_extension", False)
            and self.config.get("enable_llm_feedback", True)
        )

        # 简单模式：固定文案
        if not use_llm:
            if result_type == "success":
                return FIXED_REPLY_SUCCESS.format(target=target_name)
            if result_type == "not_found":
                return FIXED_REPLY_NOT_FOUND.format(target=target_name)
            if result_type == "multi_match":
                return FIXED_REPLY_MULTI_MATCH.format(target=target_name)
            return FIXED_REPLY_FAIL

        provider = await self._get_default_provider(event)
        if not provider:
            if result_type == "success":
                return FIXED_REPLY_SUCCESS.format(target=target_name)
            if result_type == "not_found":
                return FIXED_REPLY_NOT_FOUND.format(target=target_name)
            if result_type == "multi_match":
                return FIXED_REPLY_MULTI_MATCH.format(target=target_name)
            return FIXED_REPLY_FAIL

        user_prompt = (
            f"发送结果类型：{result_type}\n"
            f"接收者：{target_name}\n\n"
            f"请以你的角色口吻简短回复。"
        )
        system_prompt = self.config.get("feedback_system_prompt", "").strip() or DEFAULT_FEEDBACK_PROMPT

        try:
            response = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                contexts=[],
            )
            text = (response.completion_text or "").strip()

            if self._is_debug():
                logger.info(f"[DEBUG] 反馈 LLM 原始返回: {text[:500]}")

            text = text.strip().strip('"').strip("'").strip("“”").strip("‘’")
            text = text.replace("\n", " ").strip()
            if len(text) > 60:
                text = text[:60]

            if not text:
                if result_type == "success":
                    return FIXED_REPLY_SUCCESS.format(target=target_name)
                if result_type == "not_found":
                    return FIXED_REPLY_NOT_FOUND.format(target=target_name)
                if result_type == "multi_match":
                    return FIXED_REPLY_MULTI_MATCH.format(target=target_name)
                return FIXED_REPLY_FAIL
            return text
        except Exception as e:
            logger.error(f"[LLM] 反馈调用失败: {e}")
            if result_type == "success":
                return FIXED_REPLY_SUCCESS.format(target=target_name)
            if result_type == "not_found":
                return FIXED_REPLY_NOT_FOUND.format(target=target_name)
            if result_type == "multi_match":
                return FIXED_REPLY_MULTI_MATCH.format(target=target_name)
            return FIXED_REPLY_FAIL

    # ==================== 正则抽取 ====================

    def _try_regex(self, message_str: str):
        if not self.config.get("judge_regex_enable", True):
            return None

        raw = self.config.get("judge_regex_patterns", "")
        if raw and str(raw).strip():
            patterns = [p.strip() for p in str(raw).split("\n") if p.strip()]
        else:
            patterns = DEFAULT_REGEX_PATTERNS

        for pattern in patterns:
            try:
                m = re.search(pattern, message_str)
                if m and m.lastindex and m.lastindex >= 2:
                    target = m.group(1).strip()
                    content = m.group(2).strip()
                    if target and content:
                        return {"is_forward": True, "target": target, "content": content}
            except re.error as e:
                logger.warning(f"[正则] 模式无效: {pattern} → {e}")
                continue
        return None

    # ==================== 发送 ====================

    async def _send_private(self, event, target_user_id, message):
        try:
            platform_name = event.get_platform_name()
            session_id = f"{platform_name}:private:{target_user_id}"
            await self.context.send_message(session_id, [Plain(message)])
            return True
        except Exception as e:
            logger.warning(f"[发送] 私聊失败: {e}")
            return False

    async def _send_temp_session(self, event, target_user_id, group_id, message):
        try:
            await event.bot.api.call_action(
                'send_private_msg',
                user_id=int(target_user_id),
                group_id=int(group_id),
                message=message
            )
            return True
        except Exception as e:
            logger.error(f"[发送] 临时会话失败: {e}")
            return False

    async def _send_message(self, event, target_user_id, message):
        if self._is_friend_cached(target_user_id):
            if await self._send_private(event, target_user_id, message):
                return "私聊", True
            logger.info("[发送] 私聊失败，尝试临时会话")

        temp_group = str(self.config.get("default_temp_group_id", "")).strip()
        if temp_group:
            if await self._send_temp_session(event, target_user_id, temp_group, message):
                return "临时会话", True
        else:
            logger.warning("[发送] 未配置 default_temp_group_id，无法使用临时会话")

        return "无可用通道", False

    # ==================== 主逻辑 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        # ---- 前置过滤 ----
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = ""
        if group_id:
            return  # 群聊忽略

        try:
            if event.get_sender_id() == event.get_self_id():
                return  # 忽略自己
        except Exception:
            pass

        message_str = event.message_str
        if not message_str:
            return

        if len(message_str) > MAX_MESSAGE_LENGTH:
            logger.warning(f"[过滤] 消息超过 {MAX_MESSAGE_LENGTH} 字，已忽略")
            return

        if not self.config.get("enable_llm", True):
            return

        # ---- 特殊指令 ----
        if message_str.strip() in ["/刷新好友", "/刷新好友列表", "刷新好友列表"]:
            ok = await self._refresh_friend_cache(event)
            if ok:
                yield event.plain_result(
                    f"好友列表刷新成功，共 {len(self._friend_cache)} 位好友喵~\n"
                    f"当前缓存有效期：{self._get_ttl()} 秒"
                )
            else:
                yield event.plain_result("刷新失败了喵，请稍后再试~")
            return

        sender_name = event.get_sender_name() or "某人"

        # ---- 判断阶段：正则优先，LLM 兜底 ----
        intent = self._try_regex(message_str)

        if not intent:
            if not self.config.get("judge_llm_enable", True):
                return
            await self._ensure_cache(event)
            intent = await self._judge_intent(event, message_str, sender_name)

        if not intent or not intent.get("is_forward"):
            return

        target_name = str(intent.get("target", "")).strip()
        content = str(intent.get("content", "")).strip()
        if not target_name or not content:
            return

        logger.info(f"[判断] {sender_name} → {target_name}：{content}")

        # ---- 查找接收者 ----
        await self._ensure_cache(event)

        target_user_id = self._find_by_alias_map(target_name)

        # 记忆插件查询
        if not target_user_id:
            try:
                target_user_id = await self._query_memory_alias(target_name)
            except Exception as e:
                logger.error(f"[记忆查询] 调用异常: {e}")
                target_user_id = None

        # 好友缓存
        if not target_user_id:
            matched = self._find_friend_by_name(target_name)
            if len(matched) == 1:
                target_user_id = matched[0]
            elif len(matched) > 1:
                reply = await self._generate_feedback(event, "multi_match", target_name)
                yield event.plain_result(reply)
                return

        if not target_user_id:
            reply = await self._generate_feedback(event, "not_found", target_name)
            yield event.plain_result(reply)
            return

        # ---- 生成消息 ----
        final_message = await self._generate_message(event, sender_name, target_name, content)

        if self._is_debug():
            logger.info(f"[DEBUG] 最终发送内容: {final_message}")

        # ---- 发送 ----
        channel, success = await self._send_message(event, target_user_id, final_message)

        # ---- 反馈 ----
        if success:
            reply = await self._generate_feedback(event, "success", target_name)
            yield event.plain_result(reply)
        else:
            reply = await self._generate_feedback(event, "fail", target_name)
            yield event.plain_result(reply)

    async def terminate(self):
        self._friend_cache.clear()
        logger.info("[插件] 消息代发插件已卸载，好友缓存已清空")
