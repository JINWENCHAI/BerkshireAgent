# BerkshireAgent 部署与使用指南

> 一个跑在 DeerFlow 2.0 之上的「伯克希尔家庭顾问」系统。一主二从:
> - **lead_agent** = 伯克希尔主理人(你直接对话的对象),负责把问题路由到合适的子 agent
> - **buffett-persona** = 沃伦·巴菲特(投资 / 资本分配 / 家庭财富)
> - **munger-persona** = 查理·芒格(理性 / 逆向思考 / 人生决策)

长期记忆用 Qdrant Cloud,三个 placeholder 用户对应你的家庭成员,记忆**按用户隔离、家庭共享**。

---

## 架构一览

```
你提问 ─► lead_agent (Berkshire manager, 路由层)
              │
              ├─ @芒格/Charlie/munger  →  task(subagent_type="munger-persona", ...)
              ├─ @巴菲特/Warren/buffett →  task(subagent_type="buffett-persona", ...)
              ├─ 行情/价格查询  →  自己调 market-quote skill
              └─ 其它  →  自己回答,或同时调度两个 subagent 让它们讨论
                       │
                       ▼
              两个 subagent 各自写一段(每人最多一段) → lead_agent 缝合 → 返回给你
```

每个子 agent 通过三个 skill 落地:
- **`<persona>-persona`** — 自己的语气 + 思考方式
- **`longterm-memory`** — 读写 Qdrant(按 `user_id` 隔离)
- **`market-quote`** — 行情查询(免费:Yahoo Finance / Sina)

---

## 已完成的功能

| 模块 | 路径 | 状态 |
|---|---|---|
| Qdrant 长期记忆 helper | `tools/qdrant_memory.py` | ✅ 已实现 + 7 个集成测试通过 |
| Embedding helper | `tools/embedding.py` | ✅ 已实现(本地优先 + MiniMax 兜底)+ 5 个单元测试 |
| 行情查询 helper | `tools/market_quote.py` | ✅ 已实现(Yahoo + Sina,无 key)+ 21 个单元测试 |
| lead_agent 路由 overlay | `config.yaml` → `lead_prompt_overlay.prepend` | ✅ 注入到 lead_agent 系统提示最顶部 |
| Buffett subagent | `config.yaml` → `subagents.custom_agents.buffett-persona` | ✅ 注册并被 framework 发现 |
| Munger subagent | `config.yaml` → `subagents.custom_agents.munger-persona` | ✅ 注册并被 framework 发现 |
| 4 个 skill | `skills/custom/{buffett-persona, munger-persona, market-quote, longterm-memory}/SKILL.md` | ✅ 全部 enabled,framework 发现 |

---

## 你需要做的事(我没法替你做)

### 1. 提供可用的 LLM API key

`.env` 里现在的 `MINIMAX_API_KEY` 是过期的(或测试用的)。运行时会 401。
请:
- 编辑 `C:\Users\13281\Documents\BerkshireAgent\.env`,把 `MINIMAX_API_KEY=...` 换成你自己的有效 key
- 或者在 `config.yaml` 的 `models:` 段换成别的 provider(Anthropic / OpenAI / DeepSeek / 任意 OpenAI 兼容 endpoint)

### 2. (可选)安装本地 embedding

```bash
cd backend
uv pip install sentence-transformers
```

这样 `tools/embedding.py` 走完全本地免费路径,**不消耗 token**。
不装也行 —— 会自动 fallback 到 MiniMax,但每次写记忆/读记忆都要花一次 embedding API 钱。

### 3. (可选)接 QQ 通道

你的需求里提到"链接多个人的 QQ"。这部分属于 DeerFlow 的 **IM channels**(`app/channels/`)。
QQ 不是开箱即用的 channel,你需要:

- 在 `extensions_config.json` 里注册一个 MCP server(例如 `mcp/qqbot` 之类)
- 或者写一个自定义 channel adapter(`app/channels/qqbot.py`),对接到 QQ 机器人开放平台
- 在 `config.yaml` 里启用

这一步涉及 QQ 开放平台的开发者账号 + WebSocket 长连接,**我没有 QQ bot 的现成模板**,需要你确认用哪个方案:
1. 用 QQ 官方机器人 SDK(需要企业认证)
2. 用第三方框架如 `go-cqhttp` + 自建 bridge
3. 先不接 QQ,只用 Web UI(`http://localhost:2026`)做原型

---

## 启动方式

```bash
cd C:\Users\13281\Documents\BerkshireAgent
make dev          # 起 Gateway (8001) + Frontend (3000) + Nginx (2026)
```

或 docker:

```bash
make docker-start
```

打开浏览器 `http://localhost:2026`,对着主 agent 说话即可。

---

## 路由规则(lead_agent 会按这个优先级匹配)

1. **显式 @**:query 里有 `巴菲特` / `Warren` / `buffett` / `@巴菲特` → 只调 buffett;同理 `芒格` / `Charlie` / `munger` → 只调 munger。
2. **行情/价格**:query 是 `今天黄金多少` / `AAPL 现在多少` 之类 → 主 agent 自己调 `market-quote`,不派人。
3. **无 @ 但要建议**:`该不该买房` / `我该不该结婚` → 主 agent 同时调度 Buffett 和 Munger(并行),每人一段,主 agent 缝合后返回。**这是 token 消耗最贵的路径** —— 我已经在 overlay 里硬性要求两人"各写一段"以限制长度。
4. **无 @ 且是事实性问题**:主 agent 自己答。
5. **空 query**:主 agent 寒暄。

---

## 长期记忆 — 谁读谁写(persona × asker 是正交的)

**核心原则**:persona(巴菲特 / 芒格)回答谁的问题,就读谁的 collection、写谁。不是 persona 自己绑定一个用户。

三个 placeholder 槽位:

| 槽位 | 谁 |
|---|---|
| `PLACEHOLDER_USER_1` | 你本人 |
| `PLACEHOLDER_USER_2` | 父亲 |
| `PLACEHOLDER_USER_3` | 母亲 |

`ACTIVE_USER_ID=<id>` 由 lead_agent 在 dispatch persona 时注入到 task prompt 的第一行。persona skill 读取这一行作为本次会话的目标用户。

例如:
- 父亲(`PLACEHOLDER_USER_2`)问"芒格,我该不该买房" → lead_agent dispatch `munger-persona`,prompt 第一行 `ACTIVE_USER_ID=PLACEHOLDER_USER_2`,芒格读父亲的 collection 后回答
- 母亲(`PLACEHOLDER_USER_3`)问"巴菲特,我的退休账户怎么配置" → lead_agent dispatch `buffett-persona`,prompt 第一行 `ACTIVE_USER_ID=PLACEHOLDER_USER_3`,巴菲特读母亲的 collection 后回答
- 你(`PLACEHOLDER_USER_1`)问"芒格,我该不该结婚" → lead_agent dispatch `munger-persona`,prompt 第一行 `ACTIVE_USER_ID=PLACEHOLDER_USER_1`,芒格读你的 collection 后回答

| Persona | 适用范围 | `user_id` 来源 |
|---|---|---|
| `munger-persona` | 任何家庭成员都可以问 | task prompt 第一行的 `ACTIVE_USER_ID=...` |
| `buffett-persona` | 任何家庭成员都可以问 | 同上 |
| `family` scope | 全家共享 | `scope="family"`,`owner_user_id=None` |

每个 persona skill 的 SKILL.md 里都明确写了**只使用 `ACTIVE_USER_ID`**,不允许硬编码或默认到任何特定槽位。**这是硬约束**。

---

## 调试入口

```bash
# 跑 Qdrant 集成测试(会动真实 cluster,会插一些临时数据并清理)
cd backend
.\.venv\Scripts\python.exe -m pytest ../tests/test_qdrant_memory.py -v

# 列 cluster 上的 collection
python ../tools/qdrant_memory.py

# 行情查询 smoke
python ../tools/market_quote.py AAPL ^IXIC GC=F

# 端到端客户端 smoke(需要 LLM key)
cd backend
.\.venv\Scripts\python.exe -c "
from deerflow.client import BerkshireAgentClient
client = BerkshireAgentClient(subagent_enabled=True)
print(client.chat('芒格,我该不该结婚?'))
"
```

---

## 已知限制 / 待你确认

1. **QQ 通道未接入**。需要你选(官方 / go-cqhttp / 暂不接)。
2. **MiniMax API key 是无效的**,需要你换成有效的 key 才能跑端到端对话。
3. **第三个 placeholder 用户(母亲)还没有对应的 subagent**。当前只有 `buffett-persona` 和 `munger-persona`。如果你想让母亲也是一个独立 persona,需要再加一个 subagent。
4. **没有为中文市场指数(`000001.SS` 之类)专门测过**,代码逻辑有但没真实调过新浪接口。
5. **没自动 commit / push**(按你 CLAUDE.md 里的要求)。所有改动都在工作区,你审完再 `git add && git commit`。