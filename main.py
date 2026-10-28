import json
import re
import time
import sqlite3
import os
import asyncio
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

MAX_MESSAGE_LENGTH = 500


class PaperPlanePlugin(Star):
    """纸飞机 —— 私聊消息代发插件"""

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self._user_cache = {}
        self._cache_time = 0
        self._detected_memory_plugin = None

    # ==================== 配置读取 ====================

    def _get_ttl(self) -> int:
        raw = self.config.get("cache_ttl", 1800)
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            return 1800

    def _get_temp_group_ids(self) -> list[int]:
        raw = str(self.config.get("temp_session_group_ids", "")).strip()
        if not raw:
            return []
        raw = raw.replace("，", ",").replace("\n", ",")
        ids = []
        for x in raw.split(","):
            x = x.strip()
            if x.isdigit():
                ids.append(int(x))
        return ids

    def _is_debug(self) -> bool:
        return bool(self.config.get("debug_mode", False))

    # ==================== 全局用户缓存 ====================

    async def _refresh_user_cache(self, event) -> bool:
        if not hasattr(event, "bot") or not event.bot:
            logger.warning("[纸飞机] 当前环境不支持获取列表")
            return False

        new_cache = {}
        
        try:
            friend_list = await event.bot.api.call_action('get_friend_list')
            for f in friend_list:
                uid = str(f.get('user_id', ''))
                if uid:
                    new_cache[f.get('nickname', '')] = uid
                    if f.get('remark'):
                        new_cache[f['remark']] = uid
            logger.info(f"[纸飞机] 好友抓取成功: {len(friend_list)} 人")
        except Exception as e:
            logger.error(f"[纸飞机] 好友抓取失败: {e}")

        temp_groups = self._get_temp_group_ids()
        if temp_groups:
            for gid in temp_groups:
                try:
                    member_list = await event.bot.api.call_action('get_group_member_list', group_id=gid)
                    for m in member_list:
                        uid = str(m.get('user_id', ''))
                        if uid:
                            new_cache[m.get('nickname', '')] = uid
                            if m.get('card'):
                                new_cache[m['card']] = uid
                    logger.info(f"[纸飞机] 临时会话群 {gid} 成员抓取成功: {len(member_list)} 人")
                except Exception as e:
                    logger.warning(f"[纸飞机] 临时会话群 {gid} 成员抓取失败: {e}")
        else:
            logger.info("[纸飞机] 未配置临时会话发起群号，已跳过群成员抓取")

        self._user_cache = new_cache
        self._cache_time = time.time()
        logger.info(f"[纸飞机] 全局缓存刷新成功，共索引 {len(new_cache)} 个名字，TTL={self._get_ttl()}s")
        return True

    async def _ensure_cache(self, event):
        ttl = self._get_ttl()
        if ttl == 0:
            await self._refresh_user_cache(event)
            return
        if not self._user_cache or (time.time() - self._cache_time) > ttl:
            await self._refresh_user_cache(event)

    def _is_user_cached(self, target_user_id) -> bool:
        return str(target_user_id) in self._user_cache.values()

    def _find_user_by_name(self, name: str):
        uid = self._user_cache.get(name)
        return [uid] if uid else []

    def _find_by_alias_map(self, name: str):
        raw = self.config.get("alias_map", "")
        if not raw:
            return None
        raw = str(raw).replace("，", ",")
        
        alias_dict = {}
        for item in raw.split(","):
            if ":" in item:
                k, v = item.split(":", 1)
                k = k.strip()
                v = v.strip()
                if k and v:
                    alias_dict[k] = v

        if name in alias_dict:
            return alias_dict[name]

        fuzzy_matches = []
        for k, v in alias_dict.items():
            if name in k or k in name:
                fuzzy_matches.append(v)

        fuzzy_matches = list(set(fuzzy_matches))

        if len(fuzzy_matches) == 1:
            logger.info(f"[纸飞机·别名] 模糊匹配命中: “{name}” -> {fuzzy_matches[0]}")
            return fuzzy_matches[0]
        elif len(fuzzy_matches) > 1:
            logger.warning(f"[纸飞机·别名] 模糊匹配到了多个 QQ 号: {fuzzy_matches}，请配置精确名称")
            return "MULTI_MATCH"

        return None

    # ==================== 记忆插件查询 ====================

    async def _detect_memory_plugin(self):
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
                        logger.info(f"[纸飞机·记忆查询] 检测到记忆插件: {plugin_id}")
                        return key
            except Exception:
                pass

        self._detected_memory_plugin = "none"
        logger.warning("[纸飞机·记忆查询] 未检测到任何已安装的记忆插件")
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
            
        data_dir = os.path.join("data", "plugin_data", "astrbot_plugin_livingmemory")
        if not os.path.exists(data_dir):
            data_dir = os.path.join(os.getcwd(), "data", "plugin_data", "astrbot_plugin_livingmemory")
            
        conv_db = os.path.join(data_dir, "conversations.db")

        def _do_query():
            if not os.path.exists(conv_db):
                logger.warning(f"[纸飞机·记忆查询] 找不到数据库文件: {conv_db}")
                return None
                
            try:
                conn = sqlite3.connect(f"file:{conv_db}?mode=ro&immutable=0", uri=True)
                cursor = conn.cursor()
                
                cursor.execute(
                    "SELECT sender_id, count(*) as c FROM messages WHERE sender_name=? GROUP BY sender_id ORDER BY c DESC LIMIT 1",
                    (name,)
                )
                row = cursor.fetchone()
                
                if not row:
                    cursor.execute(
                        "SELECT sender_id, sender_name, count(*) as c FROM messages WHERE sender_name LIKE ? GROUP BY sender_id ORDER BY c DESC LIMIT 1",
                        (f"%{name}%",)
                    )
                    row = cursor.fetchone()
                    if row:
                        logger.info(f"[纸飞机·记忆查询] 模糊匹配命中: “{row[1]}” -> {row[0]}")
                else:
                    logger.info(f"[纸飞机·记忆查询] 精准匹配命中: “{name}” -> {row[0]}")

                conn.close()
                if row and row[0]:
                    return str(row[0])
            except Exception as e:
                logger.warning(f"[纸飞机·记忆查询] 查 conversations.db 失败: {e}")

            logger.warning(f"[纸飞机·记忆查询] LivingMemory 数据库中未找到包含“{name}”的发言记录")
            return None

        try:
            return await asyncio.to_thread(_do_query)
        except Exception as e:
            logger.error(f"[纸飞机·记忆查询] 线程池执行异常: {e}")
            return None

    async def _query_mnemosyne(self, name: str):
        star = self._get_memory_plugin_instance("astrbot_plugin_mnemosyne")
        if not star:
            return None
        try:
            engine = getattr(star, "memory_engine", None)
            if not engine:
                return None
            for method_name in ("query_alias", "lookup_alias", "find_user_by_name", "search_alias"):
                if hasattr(engine, method_name):
                    method = getattr(engine, method_name)
                    result = await method(name) if callable(method) else None
                    if result:
                        return str(result)
            return None
        except Exception:
            return None

    async def _query_memory_companion(self, name: str):
        star = self._get_memory_plugin_instance("astrbot_plugin_memory_companion")
        if not star:
            return None
        try:
            engine = getattr(star, "memory_engine", None) or getattr(star, "engine", None)
            if not engine:
                return None
            for method_name in ("query_alias", "lookup_alias", "find_user_by_name", "search_alias"):
                if hasattr(engine, method_name):
                    method = getattr(engine, method_name)
                    result = await method(name) if callable(method) else None
                    if result:
                        return str(result)
            return None
        except Exception:
            return None

    async def _query_memory_alias(self, name: str):
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
            except Exception:
                pass
        return None

    # ==================== LLM ====================

    async def _get_default_provider(self, event):
        try:
            try:
                return self.context.get_using_provider(umo=event.unified_msg_origin)
            except TypeError:
                return self.context.get_using_provider()
        except Exception as e:
            logger.error(f"[纸飞机·LLM] 获取默认 provider 失败: {e}")
            return None

    async def _get_judge_provider(self, event):
        judge_id = str(self.config.get("judge_llm_provider_id", "")).strip()
        if judge_id:
            try:
                if hasattr(self.context, "get_provider_by_id"):
                    provider = self.context.get_provider_by_id(judge_id)
                    if provider:
                        return provider
            except Exception:
                pass
            try:
                pm = getattr(self.context, "provider_manager", None)
                if pm and hasattr(pm, "get_provider_by_id"):
                    provider = pm.get_provider_by_id(judge_id)
                    if provider:
                        return provider
            except Exception:
                pass
        return await self._get_default_provider(event)

    async def _judge_intent(self, event, message_str: str, sender_name: str):
        provider = await self._get_judge_provider(event)
        if not provider:
            return None

        user_prompt = f"发送者昵称：{sender_name}\n消息内容：{message_str}\n\n请按要求输出 JSON。"
        system_prompt = self.config.get("judge_llm_system_prompt", "").strip() or DEFAULT_JUDGE_PROMPT

        try:
            response = await provider.text_chat(prompt=user_prompt, system_prompt=system_prompt, contexts=[])
            text = (response.completion_text or "").strip()

            if self._is_debug():
                logger.info(f"[纸飞机·DEBUG] 判断 LLM 原始返回: {text[:500]}")

            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if not json_match:
                return None
            data = json.loads(json_match.group(0))
            return data
        except Exception as e:
            logger.error(f"[纸飞机·LLM] 判断解析失败: {e}")
            return None

    async def _generate_message(self, event, sender_name: str, target: str, content: str):
        use_llm = self.config.get("enable_llm_extension", False)

        def _simple():
            template = str(self.config.get("simple_generate_template", "") or "{sender_name}说：{content}")
            try:
                return template.format(sender_name=sender_name, target=target, content=content)
            except (KeyError, IndexError):
                return f"{sender_name}说：{content}"

        if not use_llm:
            return _simple()

        provider = await self._get_default_provider(event)
        if not provider:
            return _simple()

        # 动态判断：如果发信人和收信人不同，必须强调带上发信人
        if sender_name != target:
            name_rule = f"请务必在句子中自然地带上发送者的名字（{sender_name}），让接收者知道是谁找TA。"
        else:
            name_rule = ""

        core_task = (
            f"【系统核心任务：消息代发】\n"
            f"发送者：{sender_name}\n"
            f"接收者：{target}\n"
            f"要传达的内容：“{content}”\n\n"
            f"【要求】\n"
            f"1. {name_rule}\n"
            f"2. 视角转换：请用第三人称视角，告诉接收者这段话。不要直接扮演接收者自问自答。\n"
            f"3. 人称代词处理：如果原话中的“他/她”实际上是指代接收者（{target}），请务必将其转换为“你”；如果是正常的第三人称对话，请确保句子通顺。\n"
            f"4. 请自然地带入你当前的人格语气，不要死板。\n"
            f"5. 只输出转述的那一句话本身，不要包含解释。"
        )

        custom_prompt = self.config.get("custom_system_prompt", "").strip()
        use_persona = self.config.get("use_persona_in_extension", True)
        prompt_mode = self.config.get("prompt_mode_in_extension", "append")
        
        kwargs = {}
        if custom_prompt:
            if prompt_mode == "override":
                kwargs["system_prompt"] = custom_prompt
                user_prompt = core_task
            else:
                user_prompt = f"{core_task}\n\n【附加语气/角色要求】\n{custom_prompt}"
                if not use_persona:
                    kwargs["system_prompt"] = DEFAULT_GENERATE_PROMPT
        else:
            user_prompt = core_task
            if not use_persona:
                kwargs["system_prompt"] = DEFAULT_GENERATE_PROMPT

        try:
            response = await provider.text_chat(prompt=user_prompt, contexts=[], **kwargs)
            text = (response.completion_text or "").strip()

            if self._is_debug():
                logger.info(f"[纸飞机·DEBUG] 生成 LLM 原始返回: {text[:500]}")

            text = text.strip().strip('"').strip("'").strip("“”").strip("‘’").replace("\n", " ").strip()
            if len(text) > 200:
                text = text[:200]
            return text if text else _simple()
        except Exception as e:
            logger.error(f"[纸飞机·LLM] 生成调用失败: {e}")
            return _simple()

    async def _generate_feedback(self, event, result_type: str, target_name: str):
        use_llm = self.config.get("enable_llm_extension", False)

        def _fixed():
            if result_type == "success":
                return FIXED_REPLY_SUCCESS.format(target=target_name)
            if result_type == "not_found":
                return FIXED_REPLY_NOT_FOUND.format(target=target_name)
            if result_type == "multi_match":
                return FIXED_REPLY_MULTI_MATCH.format(target=target_name)
            return FIXED_REPLY_FAIL

        if not use_llm:
            return _fixed()

        provider = await self._get_default_provider(event)
        if not provider:
            return _fixed()

        core_task = (
            f"【系统核心任务：代发结果反馈】\n"
            f"用户刚刚让你帮忙传话。\n"
            f"发送结果类型：{result_type}（success=成功送达，not_found=找不到人，multi_match=重名，fail=发送失败）\n"
            f"接收者名字：{target_name}\n\n"
            f"【要求】\n"
            f"1. 请结合你当前的人格设定，用自然、生动（甚至带点俏皮）的语气告诉用户结果。\n"
            f"2. 不要像机器一样回复，只输出一句话即可。"
        )

        custom_prompt = self.config.get("custom_system_prompt", "").strip()
        use_persona = self.config.get("use_persona_in_extension", True)
        prompt_mode = self.config.get("prompt_mode_in_extension", "append")
        
        kwargs = {}
        if custom_prompt:
            if prompt_mode == "override":
                kwargs["system_prompt"] = custom_prompt
                user_prompt = core_task
            else:
                user_prompt = f"{core_task}\n\n【附加语气/角色要求】\n{custom_prompt}"
                if not use_persona:
                    kwargs["system_prompt"] = DEFAULT_FEEDBACK_PROMPT
        else:
            user_prompt = core_task
            if not use_persona:
                kwargs["system_prompt"] = DEFAULT_FEEDBACK_PROMPT

        try:
            response = await provider.text_chat(prompt=user_prompt, contexts=[], **kwargs)
            text = (response.completion_text or "").strip()

            if self._is_debug():
                logger.info(f"[纸飞机·DEBUG] 反馈 LLM 原始返回: {text[:500]}")

            text = text.strip().strip('"').strip("'").strip("“”").strip("‘’").replace("\n", " ").strip()
            if len(text) > 60:
                text = text[:60]
            return text if text else _fixed()
        except Exception as e:
            logger.error(f"[纸飞机·LLM] 反馈调用失败: {e}")
            return _fixed()

    # ==================== 正则抽取 ====================

    def _try_regex(self, message_str: str):
        if not self.config.get("judge_regex_enable", True):
            return None

        raw = self.config.get("judge_regex_patterns", "")
        if raw and str(raw).strip():
            patterns = [p.strip() for p in str(raw).splitlines() if p.strip()]
        else:
            patterns = DEFAULT_REGEX_PATTERNS

        for pattern in patterns:
            try:
                m = re.search(pattern, message_str)
                if m and m.lastindex and m.lastindex >= 2:
                    target = m.group(1).strip()
                    content = m.group(2).strip()
                    
                    invalid_targets = ["我", "你", "您", "他", "她", "它", "大家", "自己", "别人"]
                    if target in invalid_targets:
                        continue
                        
                    if target and content:
                        return {"is_forward": True, "target": target, "content": content}
            except re.error:
                continue
        return None

    # ==================== 发送 ====================

    async def _send_private(self, event, target_user_id, message):
        try:
            await event.bot.api.call_action(
                'send_private_msg',
                user_id=int(target_user_id),
                message=message
            )
            return True
        except Exception as e:
            logger.warning(f"[纸飞机·发送] 纯私聊失败: {e}")
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
            logger.error(f"[纸飞机·发送] 临时会话失败: {e}")
            return False

    async def _send_message(self, event, target_user_id, message):
        if self._is_user_cached(target_user_id):
            if await self._send_private(event, target_user_id, message):
                return "私聊", True
            logger.info("[纸飞机·发送] 私聊失败，尝试临时会话兜底")

        temp_groups = self._get_temp_group_ids()
        if temp_groups:
            for gid in temp_groups:
                if await self._send_temp_session(event, target_user_id, gid, message):
                    return f"临时会话(群{gid})", True
        else:
            logger.warning("[纸飞机·发送] 未配置临时会话群号，无法使用临时会话")

        return "无可用通道", False

    # ==================== 主逻辑 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        try:
            group_id = event.get_group_id()
        except Exception:
            group_id = ""
        if group_id:
            return

        try:
            if event.get_sender_id() == event.get_self_id():
                return
        except Exception:
            pass

        message_str = event.message_str
        if not message_str:
            return

        if len(message_str) > MAX_MESSAGE_LENGTH:
            return

        if message_str.strip() in ["/刷新好友", "/刷新好友列表", "刷新好友列表"]:
            ok = await self._refresh_user_cache(event)
            if ok:
                yield event.plain_result(f"全局用户缓存刷新成功，共索引 {len(self._user_cache)} 个名字喵~\n当前 TTL：{self._get_ttl()} 秒")
            else:
                yield event.plain_result("刷新失败了喵，请稍后再试~")
            return

        sender_name = event.get_sender_name() or "某人"

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

        logger.info(f"[纸飞机·判断] {sender_name} -> {target_name}：{content}")

        await self._ensure_cache(event)
        target_user_id = self._find_by_alias_map(target_name)

        if target_user_id == "MULTI_MATCH":
            reply = await self._generate_feedback(event, "multi_match", target_name)
            yield event.plain_result(reply)
            return

        if not target_user_id:
            try:
                res = await self._query_memory_alias(target_name)
                if res == "MULTI_MATCH":
                    reply = await self._generate_feedback(event, "multi_match", target_name)
                    yield event.plain_result(reply)
                    return
                target_user_id = res
            except Exception as e:
                logger.error(f"[纸飞机·记忆查询] 调用异常: {e}")
                target_user_id = None

        if not target_user_id:
            matched = self._find_user_by_name(target_name)
            if len(matched) == 1:
                target_user_id = matched[0]

        if not target_user_id:
            reply = await self._generate_feedback(event, "not_found", target_name)
            yield event.plain_result(reply)
            return

        final_message = await self._generate_message(event, sender_name, target_name, content)
        if self._is_debug():
            logger.info(f"[纸飞机·DEBUG] 最终发送内容: {final_message}")

        channel, success = await self._send_message(event, target_user_id, final_message)

        if success:
            reply = await self._generate_feedback(event, "success", target_name)
            yield event.plain_result(reply)
        else:
            reply = await self._generate_feedback(event, "fail", target_name)
            yield event.plain_result(reply)

    async def terminate(self):
        self._user_cache.clear()
        logger.info("[纸飞机] 插件已卸载")