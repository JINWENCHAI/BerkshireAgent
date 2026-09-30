# berkshireReadMe.md — BerkshireAgent 改造总览

> 本文档汇总 **BerkshireAgent 在字节跳动 DeerFlow 2.0 基础上的全部增量改动**,
> 给项目所有者、维护者、未来的 AI agent 用。覆盖:定位、架构、新增模块、配置变更、
> 路由契约、长期记忆方案、灵魂(soul)增强、测试矩阵、已知限制。

---

## 1. 项目定位与命名

| | 值 |
|---|---|
| 上游 | [bytedance/deer-flow](https://github.com/bytedance/deer-flow) v2.0 (`207bb191`) |
| 分支 | `main` (本仓库即上游 `Initial commit: DeerFlow 2.0 renamed to BerkshireAgent` 之后的开发分支) |
| 目标 | **把 DeerFlow 2.0 改造成"伯克希尔家庭顾问"系统** |
| 三个 placeholder 用户 | `PLACEHOLDER_USER_1` (本人) / `PLACEHOLDER_USER_2` (父亲) / `PLACEHOLDER_USER_3` (母亲) |
| 私有 GitHub | `JINWENCHAI/BerkshireAgent` |

`DeerFlow` → `BerkshireAgent` 的命名迁移是上游已做的首次提交 (commit `90470a3b`) 之
上,本项目在其基础上叠加了所有用户家庭场景定制,**不再与上游做主动同步**;当上游
发版时,需要逐 PR 评估 cherry-pick。

---

## 2. 架构总览

```
         用户 (Web UI / 未来 QQ 通道)
                       │
                       ▼
            ┌────────────────────────────┐
            │      lead_agent            │  ← DeerFlow 自带;本项目仅注入 overlay
            │  (伯克希尔主理人 / 路由器)  │
            └────────────┬───────────────┘
                         │  按路由规则(显式 @ / 行情 / 默认多 persona)
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
  buffett-persona   munger-persona    lead_agent 自己回答
        │                │
        ▼                ▼
 ┌────────────────────────────────────────┐
 │  skills:                                │
 │   • persona-SKILL.md   (人格 + 灵魂)   │
 │   • longterm-memory    (Qdrant 读写)   │
 │   • market-quote       (Yahoo / Sina)  │
 └────────────────────────────────────────┘
                        │
                        ▼
        ┌───────────────────────────────┐
        │  tools/  (Python 帮助层)        │
        │   • qdrant_memory.py           │
        │   • embedding.py               │
        │   • market_quote.py            │
        └───────────────────────────────┘
                        │
                        ▼
                Qdrant Cloud (eu-west-2)
```

**关键设计原则**:每个 subagent 只允许通过 `python_repl` 调用 `tools/` 下的模块,
**绝不直连 HTTP**——`tools/` 把 URL、API key、TLS 怪癖封装掉,未来换供应商只改一处。

---

## 3. 相对 DeerFlow 2.0 的新增 / 修改清单

### 3.1 `tools/` 帮助层 (全新)

| 文件 | 行数 | 作用 | 关键点 |
|---|---|---|---|
| `tools/__init__.py` | — | 包标识 | 空文件,标 `tools.*` 可被 `python_repl` import |
| `tools/qdrant_memory.py` | 326 | 长期记忆 | 三套 `berkshire_agent_user_<id>` + 一个 `berkshire_agent_family_shared` collection;`upsert / search / delete / ensure_collections` |
| `tools/embedding.py` | 133 | 384 维 embedding | 本地 `sentence-transformers` (`BAAI/bge-small-en-v1.5`) 优先 → `MiniMax` 兜底 → 失败抛 actionable error |
| `tools/market_quote.py` | 200 | 行情 | Yahoo Finance 主源、Sina 兜底 (A 股 / 港股);带 ticker 别名 (NASDAQ / 黄金 / 比特币) |

### 3.2 `skills/custom/` (全新,4 个 skill)

| Skill | 用途 |
|---|---|
| `buffett-persona/SKILL.md` | 巴菲特人格 + 长篇"灵魂" + Qdrant 读写 + 行情接入 |
| `munger-persona/SKILL.md` | 芒格人格 + 长篇"灵魂" + Qdrant 读写 + 行情接入 |
| `longterm-memory/SKILL.md` | persona 内部调用,通过 `python_repl` 跑 `tools.qdrant_memory` |
| `market-quote/SKILL.md` | persona 内部调用,通过 `python_repl` 跑 `tools.market_quote` |

两个 persona SKILL.md **不**直接对 lead_agent 暴露 (它们只是 `subagent_type` 的内部 prompt),
后两者也只是声明式 wrapper,让 LLM 知道用什么工具。

### 3.3 `config.yaml` 增量 (`lead_prompt_overlay` + `subagents`)

**`lead_prompt_overlay.prepend`** 在 lead_agent 系统提示顶部追加了一段路由规则,把以下
事实告诉 lead_agent:
- 显式 `@巴菲特` / `@芒格` → 只调对应 persona
- 行情/价格 → 自己调 `market-quote`,不派人
- 默认建议类 → 并行调度两个 persona,每人**一段**,lead_agent 缝合
- 三个 placeholder 用户是谁
- `Persona × asker 是正交的` 这条硬约束

**`subagents.custom_agents`** 注册了 `buffett-persona` 与 `munger-persona` 两个 subagent,
指向 `skills/custom/<name>/SKILL.md` 作为系统提示。

### 3.4 `.env.example` 增量

新增:
- `QDRANT_API_KEY=your-qdrant-api-key` — Qdrant Cloud 鉴权
- `MINIMAX_API_KEY=your-minimax-api-key` — embedding 兜底 + LLM provider

把 `STEPFUN_API_KEY` 的注释拆开,让 stepfun / MiniMax 可独立开关。

### 3.5 `docs/BERKSHIRE_AGENT.md` (新增)

部署与使用指南,**给项目所有者**(你)看,包含:
- 架构图(同本文 §2)
- 已完成模块清单
- 你需要做的事 (替换 API key / 装 sentence-transformers / 选 QQ 接入方案)
- 启动命令、路由规则、长期记忆 isolation 原则、调试入口、已知限制

### 3.6 灵魂 (Soul) 增强 — buffett-persona & munger-persona

两个 persona 的 SKILL.md 都已重写,在原有 "Voice and posture" 之上加了完整的 **soul 章节**:

**buffett-persona 的 soul**:
- **6 条不可议价的核心信念** (各为多句段落,不是 bullet):资本保全 > 资本增值、价格 ≠
  价值、能力圈是纪律、时间是好生意的朋友、无聊即美、家庭资本是一个池。
- **思维框架**:先讲 business 再讲 multiple、margin of safety 永远不倒置、按十年思考对
  的、按周思考错的、机会成本是唯一成本。
- **常见主题立场** (10 条):投机 vs 投资、杠杆、房地产、雇主集中股、择时、遗产、职业、子
  女 — 每个都有具体立场。
- **反模式** (6 条):绝不说的几句话(不投机型说"抄底"、不"为了分散而分散"、不"这次不
  同"、不发火箭 emoji)。
- **Soul 优先级规则**:soul 是思维框架不是清单,150–350 字预算仍然赢。

**munger-persona 的 soul**:
- **6 条不可议价的核心信念**:避免愚蠢 > 寻找聪明、永远反向思考、激励是主钥匙、思维
  栅格、Sit 是行动、性格是命运。
- **思维框架**:inversion first、看二阶效应、多模型交叉验证、量化时也要留余地、看 effort
  / insight 比例、要例子也要反例。
- **常见主题立场** (10 条):职业、婚姻、朋友与社交资本、投资、投机与 FOMO、杠杆、健康、
  子女。
- **反模式** (6 条):不"follow your passion"、不"积极一点"、不"反正没人能预测那就
  YOLO"、不正面评价提问者。
- **Soul 优先级规则**:同上。

---

## 4. 长期记忆架构 (`tools/qdrant_memory.py`)

### 4.1 Collection 布局

| Collection | 用途 | 谁能读 | 谁能写 |
|---|---|---|---|
| `berkshire_agent_user_<user_id>` | 个人私有记忆 | 只有 `<user_id>` 这个 asker 的 persona 读 | 只有 `<user_id>` 这个 asker 的 persona 写 |
| `berkshire_agent_family_shared` | 全家共享 | 所有 persona | 所有 persona |

> **可见性靠 collection 隔离,不靠 filter**——这意味着"写错 collection"才会泄漏,
> 而我们要求 persona 只从 `ACTIVE_USER_ID` 取 id,没有任何"兜底默认值"。

### 4.2 关键代码契约

| 行为 | 怎么实现 |
|---|---|
| 三套 collection + family collection 创建 | `ensure_collections()` 幂等,409 当成功 |
| 写入 384 维向量 | `MemoryPoint.__post_init__` 在构造时校验 dim |
| 搜索 (persona 必调) | `search(user_id, query_embedding, limit=5, score_threshold=0.6, include_family=True)` |
| 删除 | Qdrant 1.x 已废 `{"points": [id]}` 形式,改用 `{"filter": {"must": [{"has_id": [id]}]}}` |
| TLS | 强制 `ssl.PROTOCOL_TLSv1_2` —— Qdrant eu-west-2 的 LB 不接受 TLS 1.3 |

### 4.3 Embedding 维度策略 (`tools/embedding.py`)

Qdrant collection 是 384 维 (`BAAI/bge-small-en-v1.5`)。`embed()`:
1. 优先本地 `sentence-transformers` —— **免费,零延迟**。
2. 失败 → `MiniMax embeddings` endpoint,dim 不匹配时**绝不静默截断**,而是确定性投影
   (截断 + 零填充) 保持 Cosine 相似度的可比性。
3. 全失败 → 抛 `RuntimeError` + actionable message。

---

## 5. 路由契约 (lead_agent 行为)

lead_agent 系统提示被 `lead_prompt_overlay.prepend` **追加** (上游 DeerFlow 默认行为是
"追加",不是"替换")。我们在这段 prepend 里硬约束了:

| 规则 | 触发 | lead_agent 行为 |
|---|---|---|
| 显式 `@巴菲特` / `Warren` / `buffett` / `请教一下巴菲特` | query 含这些字串 | 只调 `buffett-persona`,不调 `munger-persona` |
| 显式 `@芒格` / `Charlie` / `munger` | 同上 | 只调 `munger-persona` |
| 行情 / 价格 (`今天黄金多少` / `AAPL 现在多少`) | 关键字 | 自己调 `market-quote` skill,不派人 |
| 默认建议类 (`该不该买房` / `该不该结婚`) | 无 @ 但要观点 | **并行** 调度两个 persona,每人一段 |
| 事实性问题 | 其它 | 自己答 |
| 空 query | — | 寒暄 |

> **无论派谁,都必须在 dispatch prompt 第一行写 `ACTIVE_USER_ID=<id>`**——
> persona skill 读这一行才知道本次是为哪个 asker 服务的。

---

## 6. Persona × Asker 正交原则 (硬约束)

> 这是本项目最不能违反的一条设计,所有后续改动都必须保持。

**Persona (巴菲特 / 芒格) 不是任何特定用户的"专属顾问"**。它们服务于 dispatch
prompt 中 `ACTIVE_USER_ID=` 行指定的那个 asker。

| dispatch prompt 第一行 | 后果 |
|---|---|
| `ACTIVE_USER_ID=PLACEHOLDER_USER_2` | buffett/munger 读 `berkshire_agent_user_PLACEHOLDER_USER_2` |
| `ACTIVE_USER_ID=PLACEHOLDER_USER_3` | 读 `_USER_3` |
| **缺失或空** | persona **必须 halt**:`I don't know which user this is for...` |
| `ACTIVE_USER_ID=PLACEHOLDER_USER_1` (本人) | 读 `_USER_1` |

每个 persona 的 SKILL.md 里都明确写:`❌ Never assume an ACTIVE_USER_ID — if it is
missing, halt.` 这是 hard rule,不是 soft rule。

---

## 7. 测试矩阵 (`tests/`)

| 文件 | 测试数 | 覆盖 |
|---|---|---|
| `test_active_user_routing.py` | 19 | overlay 注入 ACTIVE_USER_ID、persona 不绑定固定槽、halt 行为、跨槽读取禁止、模拟 dispatch |
| `test_embedding_helper.py` | 5 | 384 维契约、本地 fallback、MiniMax fallback、缺 key 错误 |
| `test_market_quote.py` | 21 | Yahoo ticker 别名、A 股 / 港股 / 美股 / 指数、错误处理、ticker 形状 |
| `test_qdrant_memory.py` | 7 | collection 创建幂等、upsert / search / delete、跨槽不能读、Qdrant 1.x 新 delete 协议、per-test cleanup |
| `test_persona_soul_contract.py` | 14 | soul 章节存在、≥5 条多句核心信念、thinking framework、≥6 个 stance 主题、投资 + 生活覆盖、≥3 条 anti-patterns、soul 优先级规则、Buffett / Munger 指纹锚、soul 不重绑 persona |

**总计 66 个测试**。routing + soul 是合同性测试(防回归,动 SKILL.md 必跑),其余是
功能性测试。

### 运行方式

```bash
cd C:\Users\13281\Documents\BerkshireAgent
python -m pytest tests/test_active_user_routing.py \
                tests/test_persona_soul_contract.py \
                tests/test_embedding_helper.py \
                tests/test_market_quote.py \
                tests/test_qdrant_memory.py -q
```

预期结果:`66 passed`。

---

## 8. 启动

```bash
# 一次性
make config          # 复制 config.example.yaml / extensions_config.example.json
make install         # 安装 backend + frontend + pre-commit

# 填 .env (必填)
#   MINIMAX_API_KEY=<你的 key>
#   QDRANT_API_KEY=<Qdrant Cloud 控制台给的 key>

# 起服务
make dev             # Gateway :8001 + Frontend :3000 + Nginx :2026
# 或
make docker-start    # 整套容器
```

浏览器打开 `http://localhost:2026`,对着 lead_agent 说话即可。

---

## 9. 调试入口速查

```bash
# 1) Qdrant 集成测试 (会动真 cluster,自动 cleanup)
cd backend && python -m pytest ../tests/test_qdrant_memory.py -v

# 2) 列 cluster 上的 collection
python ../tools/qdrant_memory.py

# 3) 行情 smoke
python ../tools/market_quote.py AAPL ^IXIC GC=F 600519

# 4) Embedding smoke (需要 MINIMAX_API_KEY 或装 sentence-transformers)
python -c "from tools.embedding import embed; print(len(embed('hello world')))"

# 5) 端到端 (需要 lead_agent 实际可用)
cd backend && python -c "
from deerflow.client import BerkshireAgentClient
client = BerkshireAgentClient(subagent_enabled=True)
print(client.chat('芒格,我该不该结婚?'))
"
```

---

## 10. 已知限制 / 待办

| # | 事项 | 谁来做 | 状态 |
|---|---|---|---|
| 1 | QQ 通道未接入。需要选:① 官方机器人 SDK ② `go-cqhttp` + 自建 bridge ③ 暂不接,只 Web | 你 | 待选 |
| 2 | `MINIMAX_API_KEY` 是示例值,要换成你自己的有效 key | 你 | 待填 |
| 3 | 第三个 placeholder 用户 (母亲) 还没有专属 persona | 你确认是否需要 | 待定 |
| 4 | A 股新浪接口未真实联网验证 (逻辑在,但只靠代码 review) | 你提供一只 A 股 ticker 给本地 smoke | 待跑 |
| 5 | 上游 DeerFlow 新版本 cherry-pick 流程未建立 | 维护者 | 待办 |
| 6 | 端到端 smoke (lead_agent 调度 persona → 写入 Qdrant → 读出) 还没有 integration test | 后续补 | 待办 |
| 7 | 当前没有 CI 配置 (`.github/workflows/` 上游 DeerFlow 有但本仓库尚未接入) | 维护者 | 待办 |

---

## 11. 文件清单速查

新增 / 修改文件 (按修改时间倒序):

```
berkshireReadMe.md                              ← 本文档 (项目所有者入口)
docs/BERKSHIRE_AGENT.md                         ← 部署与使用指南
skills/custom/buffett-persona/SKILL.md          ← 巴菲特 persona + soul
skills/custom/munger-persona/SKILL.md           ← 芒格 persona + soul
skills/custom/longterm-memory/SKILL.md          ← Qdrant 读写 wrapper
skills/custom/market-quote/SKILL.md             ← 行情 wrapper
tools/qdrant_memory.py                          ← Qdrant 帮助层
tools/embedding.py                              ← embedding 帮助层
tools/market_quote.py                           ← 行情帮助层
tests/test_active_user_routing.py               ← 路由合同测试
tests/test_persona_soul_contract.py             ← soul 合同测试
tests/test_embedding_helper.py                  ← embedding 单元测试
tests/test_market_quote.py                      ← 行情单元测试
tests/test_qdrant_memory.py                     ← Qdrant 集成测试
config.yaml                                     ← (修改) lead_prompt_overlay + subagents
.env.example                                    ← (修改) +QDRANT_API_KEY +MINIMAX_API_KEY
```

---

## 12. 一句话总结

> **BerkshireAgent = DeerFlow 2.0 + 伯克希尔家庭顾问语义层**:
> lead_agent 路由 + 巴菲特/芒格双 persona (各带完整 soul) + Qdrant Cloud 长期记忆
> (按用户隔离、家庭共享) + 行情接入 (Yahoo / Sina) + 66 个测试守护 (路由 + soul + 功能)。

