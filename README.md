<div align="center">
  <img src="logo.png" alt="纸飞机 Logo" width="200">
</div>

# AstrBot 纸飞机插件

纸飞机 —— 让 Bot 帮你在私聊里给指定的人传话。

## 功能特性

- 💬 **私聊专用**：只在私聊场景响应，群聊自动忽略。
- 🎯 **正则 + LLM 双层判断**：常见句式走正则（零成本），自然表达走 LLM（更智能）。
- ✨ **可选 LLM 扩展**：开启后转述和反馈由 AI 生成，风格更贴合 Bot 人格。
- 🧠 **记忆插件集成**：可选从 LivingMemory / Mnemosyne / Memory Companion 查询别名。
- 🚀 **临时会话兜底**：对方不是好友时，可通过指定群聊发起临时会话。
- 🛡️ **多层防御**：忽略自身消息、超长消息、非私聊场景。

## 使用示例

**前提**：把 Bot 加为好友，或确保 Bot 与目标用户在同一个"临时会话群"里。

**私聊 Bot 说：**

```text
给灵灵发"你在干什么"
告诉小明我晚点回
```

**Bot 会自动识别并转述给目标用户。**

## 安装步骤

1. 将 `astrbot_plugin_paper_plane` 整个文件夹放入 AstrBot 的插件目录 `AstrBot/data/plugins/`
2. 在 WebUI 插件管理页面点击 **重载插件**
3. 配置必要项（见下文）

## 配置详解

### 插件总开关

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enable_llm` | true | 关闭后插件完全不响应 |

### 判断阶段

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `judge_regex_enable` | true | 启用正则预判 |
| `judge_regex_patterns` | 空 | 留空使用内置默认模式 |
| `judge_llm_enable` | true | 正则不命中时用 LLM 判断 |
| `judge_llm_provider_id` | 空 | 判断用的 Provider，留空=会话默认 |
| `judge_llm_system_prompt` | 空 | 判断提示词，留空=内置默认 |

**工作方式**：正则先试，命中就直接抽取；不命中则送 LLM 判断。两者都失败则忽略消息。

### LLM 扩展

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enable_llm_extension` | false | LLM 扩展总开关 |
| `enable_llm_generate` | true | 转述用 LLM 生成 |
| `enable_llm_feedback` | true | 反馈用 LLM 生成 |
| `generate_system_prompt` | 空 | 转述提示词 |
| `feedback_system_prompt` | 空 | 反馈提示词 |

**简单模式**（扩展关闭）：转述走 `simple_generate_template`，反馈走写死的固定文案。

**扩展模式**（扩展开启）：转述和反馈都由会话默认 LLM 生成。

### 简单模式转述模板

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `simple_generate_template` | `{sender_name}说：{content}` | 可用变量：`{sender_name}`、`{target}`、`{content}` |

### 好友与发送

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `alias_map` | 空 | 名字到 QQ 号的映射，格式：`灵灵:123456,小明:654321` |
| `cache_ttl` | 1800 | 好友列表缓存秒数，0=每次刷新 |
| `default_temp_group_id` | 空 | 临时会话发起群号 |

### 记忆插件

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `enable_memory_lookup` | false | 启用记忆插件别名查询 |
| `memory_plugin_mode` | auto | `auto` / `livingmemory` / `mnemosyne` / `memory_companion` / `off` |

**auto 模式优先级**：LivingMemory → Mnemosyne → Memory Companion。

**记忆查询失败时的日志**：所有失败场景都会在日志里以 WARNING / ERROR 级别提示，不需要开 debug 模式就能看到。

### 调试

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `debug_mode` | false | 开启后在日志打印 LLM 原始返回 |

## 写死的规则

以下规则不可配置，属于插件的安全底线：

- 只在私聊场景响应，群聊完全忽略
- 忽略 Bot 自己发的消息
- 超过 500 字的消息忽略
- 空消息忽略
- LLM 判断失败视为"非传话"
- 成功/失败**必定**回复发送者

## 临时会话说明

对方不是 Bot 好友时，可通过 `default_temp_group_id` 指定的群发起临时会话。需要同时满足：

1. 适配器支持（NapCat / LLOneBot 支持，LiteLoader 不支持）
2. Bot 在目标群内
3. 目标用户在目标群内
4. 目标群开启"允许群成员发起临时会话"
5. 目标用户允许接收临时会话

**不满足时传话会直接失败，反馈 `发送失败`。**

## 常用命令

| 指令 | 说明 |
|---|---|
| `/刷新好友列表` | 手动刷新好友缓存 |

## 常见问题

### Q1：正则和 LLM 判断的顺序是？

先尝试正则。命中就直接用，不调用 LLM。不命中才送 LLM 判断。

### Q2：两个判断都关掉会怎样？

插件将无响应。建议至少启用一个。

### Q3：对方不是好友为什么发不出去？

QQ 机器人只能给好友或群临时会话成员发消息。解决方案：
- 让对方加 Bot 为好友，或
- 配置 `default_temp_group_id`

### Q4：记忆插件查询失败能看到错误吗？

能。记忆插件相关的失败（未安装、调用异常、查询无结果）都会以 WARNING 或 ERROR 级别输出到日志，无需开启 `debug_mode`。

### Q5：为什么只在私聊响应？

本插件定位为"私人助理"场景。群聊代发容易造成骚扰，因此不响应群聊消息。

## 开发者信息

- **插件名称**：astrbot_plugin_paper_plane（纸飞机）
- **版本**：v1.0.0
- **作者**：ZXY_Baa
- **适用 AstrBot 版本**：v3.0.0+
- **依赖平台**：aiocqhttp
- **插件分类**：娱乐 / 工具

## 许可协议

MIT License
