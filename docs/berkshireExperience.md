# BerkshireAgent Experience Log

> 维护这个仓库过程中**踩过的坑 + 验证过的修复**。按时间倒序,新的在上面。
>
> 与框架文档(`AGENTS.md` / `backend/AGENTS.md` / `backend/app/channels/AGENTS.md`)互补:那些写"应该怎么用",这本写"实际怎么坏、怎么修"。新坑都往这里加,避免下次再栽。

---

## 2026-09-30 — QQ 私聊 bot 接入 + LangGraph checkpointer 落地

### TL;DR

QQ 通过 WebSocket 接入 BerkshireAgent,`config.yaml` 配 `database.backend: sqlite` 后,所有 LangGraph state 写到 `backend/.berkshire-agent/data/deerflow.db`。修复了 3 个独立 bug:`_on_outbound` 同步/异步签名错配、错误的字段名引用、`config.yaml` 的 `tools:` 列表里漏注册 `read_file` / `ls` / `glob` / `grep`。

### 环境快照

- `config.yaml` 里 `models[0]`: `minimax-m3`, `MiniMax-M3`, `base_url=https://api.minimaxi.com/v1`, `supports_thinking: true`, `when_thinking_enabled.thinking.type: adaptive`(注:`enabled` 是国际端的合法值,但**国内端只接受 `adaptive` 或 `disabled`**)
- `channels.qq.transport: websocket`, `sandbox: true` → `sandbox.api.sgroup.qq.com`,沙盒不需要 IP 白名单
- `database.backend: sqlite`, `sqlite_dir: .berkshire-agent/data` → 实际文件 `backend/.berkshire-agent/data/deerflow.db`(因为 gateway CWD 是 `backend/`)
- `subagents.buffett-persona.tools` / `munger-persona.tools` 各自列了 `python_repl` / `bash` / `read_file` / `grep` / `glob`(给子 agent 用)
- `tool_groups` 声明了 `file:read`,但 **`tools:` 列表里没注册 `read_file` 等** ← 直接坑掉 lead-agent

### 坑 #1 — `_on_outbound` 同步/异步签名错配 → `TypeError: object NoneType can't be used in 'await' expression`

**症状**

```
[ERROR] Error in outbound callback for channel=qq
TypeError: object NoneType can't be used in 'await' expression
RuntimeWarning: coroutine 'QQChannel.send' was never awaited
```

agent response 已经写进 checkpointer,但消息**完全没发出去**。

**根因**

`backend/app/channels/base.py` 里 `_on_outbound` 是 `async def`,bus 注册 callback 时 `await callback(msg)`。`backend/app/channels/qq.py` 子类**把签名覆盖成了同步 `def`**——返回 `None`,`await None` → TypeError。

覆盖前是参考了 `feishu.py` / `discord.py` 那套"通过 SDK 回调线程 + `_submit_threadsafe_coroutine`"的写法,但 `_on_outbound` 本身是 **Gateway 主事件循环里被 await 调用的**,根本不需要 threadsafe 包装,直接 `await self.send(msg)` 即可。

**修复**

`backend/app/channels/qq.py`:

```python
async def _on_outbound(self, msg: OutboundMessage) -> None:
    if msg.channel_name != self.name:
        return
    try:
        await self.send(msg)
    except Exception:  # noqa: BLE001
        logger.exception("[QQ] failed to send outbound message")
```

参照 `wecom.py::286` 的写法(它也是直接 `await self.send(msg)`)。

### 坑 #2 — `OutboundMessage` 不存在的字段 → `AttributeError`

**症状**(同一次事故的早期版本)

```
AttributeError: 'OutboundMessage' object has no attribute 'message_id'
```

**根因**

`OutboundMessage` 的字段是 `chat_id` / `thread_id` / `channel_name` / `text` / `attachments` / `is_final` 等,**没有 `message_id`**。`message_id` 是 QQ 入站回调里的 `msg_id`,只有 `InboundMessage` 有。

**修复**

需要"唯一标识一条 outbox 消息"时用 `msg.thread_id`(语义对得上:每个 agent turn 一个 thread)。

### 坑 #3 — `read_file is not a valid tool` → lead-agent 没法真正加载人格

**症状**

```
Error: read_file is not a valid tool, try one of [web_search, web_fetch,
present_files, ask_clarification, review_skill_package, list_uploaded_files,
view_image].
```

agent 想读 `/mnt/skills/legacy/munger-persona/SKILL.md` 加载人格,失败,然后** fallback 角色扮演**出一段"芒格在..."的回复(在 reasoning 里其实自承"我不是 Munger,我在 channel 化 Munger")。

**根因**

`config.yaml` 的 `tool_groups` 声明了 `name: file:read`,但 **`tools:` 列表里漏注册 `read_file` / `ls` / `glob` / `grep`**。lead-agent 拿到的工具集只有 web + knowledge。

`config.example.yaml` 行 1305–1323 有完整正确的 5 个 tool 注册样板,直接抄过来就行。

**修复**(`config.yaml`)

```yaml
tools:
  # ... 已有 web_search / web_fetch / knowledge_search / list_knowledge_bases ...

  # ── File reading (sandbox-backed) — required so the lead agent can load
  # SKILL.md files referenced by routing rules (e.g. munger-persona /
  # buffett-persona). Without these, the routing layer falls back to
  # roleplay and never actually grounds the persona.
  - name: read_file
    group: file:read
    use: deerflow.sandbox.tools:read_file_tool

  - name: ls
    group: file:read
    use: deerflow.sandbox.tools:ls_tool

  - name: glob
    group: file:read
    use: deerflow.sandbox.tools:glob_tool
    max_results: 200

  - name: grep
    group: file:read
    use: deerflow.sandbox.tools:grep_tool
    max_results: 100
```

### 坑 #4 — `thinking.type: "enabled"` 在国内 MiniMax 端报错

**症状**

```
LLM request failed: Error code: 400 - invalid params,
invalid thinking.type: "enabled" (allowed: adaptive, disabled) (2013)
```

**根因**

国内 `api.minimaxi.com` 端 `thinking.type` 只接受 `adaptive` 或 `disabled`。国际端 `api.minimax.io` 才接受 `enabled`。**直接抄 config.yaml 第 71 行**的写法:`type: adaptive`。

### Checkpointer 落地的几个事实

1. **默认 `InMemorySaver`**(进程内存,重启即丢)。`config.yaml` 没 `database:` 也没 `checkpointer:` section 时走这条。
2. **加 SQLite 后**:`database.backend: sqlite` + `sqlite_dir` → 落到 `<cwd>/.berkshire-agent/data/deerflow.db`。Gateway CWD 是 `backend/`,所以实际是 `backend/.berkshire-agent/data/deerflow.db`。改完 `config.yaml` 建议**重启 gateway**(虽然 `app_config.py::_apply_singleton_configs` 看起来是热加载,但 `database.*` 在 `STARTUP_ONLY_FIELDS` 里,改它要重启)。
3. **checkpoint blob 是 msgpack + LangGraph ExtType**,metadata 是 JSON。**不能用 DB Browser for SQLite 直接看 messages**——只会看到一堆二进制。要看内容用脚本(下面附)。
4. **inspect 脚本** `backend/inspect_checkpoints.py`:用 `langgraph.checkpoint.serde.jsonplus._msgpack_ext_hook` 把 msgpack 的 ExtType 还原成 `AIMessage` / `HumanMessage` / `ToolMessage` 对象,然后打印。
   - 关键:`JsonPlusSerializer.loads` 内部走 `json.loads`,**解不开 msgpack blob**。要用 `ormsgpack.unpackb(blob, ext_hook=_msgpack_ext_hook, option=ormsgpack.OPT_NON_STR_KEYS)`。

---

## 2026-09-30 (下午) — Outbox 缺位:checkpoint 在,但用户没收到答复

### TL;DR

LLM 已经写完 final reply、checkpoint 已经持久化,但**用户那边的 QQ 聊天里什么都没收到**。重启 gateway 后**没有任何钩子重发这条 orphaned reply** —— `MessageBus.publish_outbound` 是纯 in-process pubsub,`OutboundMessage` 一旦 fanout 出去就丢了。

修复:走方案 B,**新增 outbox 表 `channel_outbox.db`**,每次 publish 先 INSERT 一行 pending,channel send 成功后再 mark delivered;启动时扫 pending 重发。

### 坑 #5 — `MessageBus` 没有任何 outbox / 持久化层

**症状**

用户在 QQ 发"查理芒格你在吗",agent 完整跑了(读 SKILL、调工具、写 final AIMessage、checkpoint 落库),但** QQ 那边的用户什么都没看到**。

`gateway.log` 里有 `Error in outbound callback for channel=qq: TypeError: object NoneType can't be used in 'await' expression` 或类似,然后日志直接断了。

**根因**

`backend/app/channels/message_bus.py`:

```python
async def publish_outbound(self, msg: OutboundMessage) -> None:
    ...
    for callback in self._outbound_listeners:
        try:
            await callback(msg)         # ← 完全 in-process,callback 抛了就完了
        except Exception:
            logger.exception("Error in outbound callback for channel=%s", msg.channel_name)
```

`MessageBus` **没有** 写盘逻辑。`OutboundMessage` 在以下任意一种情况下**永久丢失**:

1. channel adapter 抛异常(`_on_outbound` → `await self.send(msg)` raise)
2. gateway 在 callback await 期间被 kill -9 / OOM / 断电
3. channel 启动失败导致 callback 没注册,但 message_bus 已经把消息发出去了

**checkpoint 不知道 outbound 是否成功** —— 它只是 LangGraph state;成功投递是 channel adapter 的责任,且**没有 ack 回流到 checkpoint 体系**。

`ChannelManager._handle_streaming_chat()` (manager.py:2713) 调一次 `publish_outbound(text, is_final=True)`,然后 `finally` 块跑 `RunJournal.close()` 之类,**不重试**。

**修复 — 方案 B 实施**

新增 `app/channels/outbox/` 子模块,包含 4 个文件:

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | `OutboundRecord` dataclass + `DEFAULT_PENDING_HORIZON_SECONDS=86400` + `build_message_from_row` |
| `model.py` | `OutboxRow` SQLAlchemy ORM(独立表 `channel_outbox`,不复用 `deerflow.db`) |
| `engine.py` | 独立 `aiosqlite` engine,`init_outbox_engine(sqlite_dir)`,文件落到 `<sqlite_dir>/channel_outbox.db`。`is_outbox_disabled_by_env()` 走 `DEER_FLOW_DISABLE_OUTBOX=1` 紧急关停 |
| `repository.py` | `OutboxRepository`: `enqueue` / `list_pending` / `mark_delivered`(幂等) / `mark_delivered_by_msgid` / `record_failure` / `increment_attempt` / `trim_delivered` / `trim_old_pending` |
| `replay.py` | `replay_pending_for_channel()` 启动时扫,`start_periodic_replay()` 周期扫 |

接入点 4 处:

| 文件 | 改动 |
| --- | --- |
| `app/channels/message_bus.py` | `enable_outbox(enabled)` 切换;`publish_outbound` 先 INSERT 一行 pending(用 `_pending_outbox_ids[callback]=row_id` 暂存),fanout 后各 callback 自己 `ack_outbound(self._on_outbound)` |
| `app/channels/base.py` | `Channel._on_outbound` 包了 `try / finally`,`send()` 成功才 `ack_outbound`,失败则 `record_outbound_failure`(仍 pending,等重试) |
| `app/channels/service.py` | `start()` 时 `init_outbox_engine`;每个 channel start 后 `await self._replay_pending_for(name)`(dispatcher 直接走 `channel._on_outbound`,**复用 live 路径,代码不分裂**);`stop()` 时 `close_outbox_engine` |
| `config.yaml` + `config.example.yaml` | 加 `channels.outbox.enabled: true` + `sqlite_dir` + `horizon_seconds` + `replay_interval_seconds`(默认 30s 周期扫) |

**关键设计决策**

1. **is_final 流式 chunk 不入 outbox**,只有 final reply 入。QQ 没有 in-place edit,中间 chunk 重发没有意义,只会 spam 用户。
2. **live publish 路径**:publish_outbound INSERT 一行 pending → `_pending_outbox_ids[callback] = row_id` → callback 执行 → callback `finally` 块调 `bus.ack_outbound(callback)` → bus 读 `_pending_outbox_ids` 拿 row_id → mark_delivered。
3. **replay 路径**:replay 启动时 SELECT → 函数 `replay_pending_for_channel` 调 dispatcher → dispatcher 调 `channel._on_outbound(msg)` → 同上走 ack 流程。**replay 自己不调 ack_outbound**,因为 dispatcher 不是 bus 的注册 callback,`_pending_outbox_ids` 里查不到 row_id;**replay 直接 `repo.mark_delivered(row_id)`**(放在 dispatcher 成功之后)。
4. **ack 幂等**:`UPDATE ... WHERE id=? AND delivered_at IS NULL`,rowcount==0 表示已经被 ack 过,no-op。
5. **crash 边界**:
   - INSERT 成功 + fanout 失败 → 行 pending,重试 ✓
   - INSERT 成功 + send 成功 + ack 失败 → 行 pending,**会重发**(QQ 端可能看到重复,但 `allowed_users` 一般 1 人,且人看 duplicate 也比死回复好)
   - INSERT 失败(outbox 引擎挂了)→ log error,**降级到 legacy 行为**(继续 fanout,不持久化)。日志说"delivery will NOT survive a crash"。
6. **trim**:后台 `trim_delivered(keep_last_n=100_000)` 删旧 delivered 行,文件不无限增长。`trim_old_pending(horizon_seconds=24h)` 给运维应急通道。
7. **测试**:`backend/tests/test_channel_outbox.py`,9 个 case:写盘 + pending + crash-redeliver(replay 后 redelivered list == ["orphan"],再 replay no-op) + 失败留 error + 双重 ack 幂等 + horizon trim + 默认 outbox 关闭的向后兼容 + attachments/JSON roundtrip + listener 抛异常留 last_error。

### 坑 #6 — `MessageBus._pending_outbox_ids` 在异常路径里 pop 时机错了

**症状**

`test_record_outbound_failure_attaches_error` 失败:listener `raise RuntimeError("platform down")`,但 `OutboxRow.last_error` 是 None。

**根因**

`publish_outbound` 在 `except` 分支里**先 pop 再调 `record_outbound_failure`**,但 `record_outbound_failure` 内部从 `_pending_outbox_ids[callback]` 读 row_id —— pop 之后 map 是空的,读不到 row_id,默默 no-op。

**修复**

`backend/app/channels/message_bus.py:470-487`:异常路径先调 `record_outbound_failure(callback, error)`,**再 pop**。这是 pop 之前调用的常规顺序(先消费、再清状态)。

### 坑 #7 — replay 自己 ack 才正确,不能依赖 bus.ack_outbound

**症状**

`test_replay_after_crash_redelivers` 第一次 replay 时 redelivered=["orphan"],**第二次 replay 又 redelivered 一条**(明明第一次应该 mark delivered 了)。

**根因**

dispatcher `_dispatch` 在 replay 路径里调 `_live_send`,但 `_live_send` 调 `fresh_bus.ack_outbound(_live_send)` —— `fresh_bus._pending_outbox_ids[_live_send]` **从来没被 publish_outbound 设置过**,因为 `_live_send` 不是 `publish_outbound` 注册的 callback(它是 replay 路径的 inline function)。

也就是说 **live publish 路径用 `_pending_outbox_ids[callback]` 来 track row_id**,但 **replay 路径里 dispatcher 不是 bus listener**,这条线索断了。

**修复**

`backend/app/channels/outbox/replay.py`:dispatcher 成功后,replay 自己直接 `await repo.mark_delivered(row_id=record.id)`,不走 `bus.ack_outbound` 中转。Live publish 和 replay 两条路径 **完全独立** ack,代码反而更清晰。

### 验证

```powershell
# 1. unit
cd C:\Users\13281\Documents\BerkshireAgent\backend
.\.venv\Scripts\python.exe -m pytest tests/test_channel_outbox.py -q
# 期望 9 passed

# 2. regression: 现有 channel 测试不能破
.\.venv\Scripts\python.exe -m pytest tests/test_channels.py -q
# 期望 378 passed (Windows 上有 7 个 pre-existing fail,都是平台/权限问题,跟 outbox 无关)

# 3. 端到端手动
#   a) 配 config.yaml: channels.outbox.enabled: true, sqlite_dir: .berkshire-agent/data
#   b) make dev
#   c) 给 QQ 发一条消息,LLM 出 final reply,bot 收到
#   d) 立刻 Ctrl-C 杀掉 gateway
#   e) 再 make dev
#   f) 查 backend/.berkshire-agent/data/channel_outbox.db:
#      sqlite> SELECT id, channel_name, text, delivered_at FROM channel_outbox WHERE delivered_at IS NULL;
#      期望:0 行 (正常流程已经在 send 成功后 mark delivered 了)
#   g) 故意制造 crash 场景:在 QQChannel.send() 里塞 raise,跑一次 send,kill,重启,看 outbox 重发
```

### 教训

- **outbox 是 at-least-once**。QQ 端可能在 network 抖动时收到重复(我们的 send HTTP 200,outbox 还没 mark delivered 就 crash)。**user-facing duplicate 比 user-facing miss 轻** —— 这个 trade-off 在 v1 接受。如果以后接 idempotent 感知,把 `channel_message_id` 填上(QQ 给每个 reply 一个 msg_id),平台侧可以用 `mark_delivered_by_msgid(channel_message_id=...)` 去重。
- **outbox 跟 deerflow.db 分开**。这是有意的:checkpoint 表和 outbox 表的备份/迁移/损坏域应该正交。混在一起会导致"outbox 错就 checkpoint 也废"的尴尬。
- **bus 改动要后向兼容**。`outbox_enabled=False` 是默认,旧部署 `enable_outbox` 没调过也不会有任何 SQL 写盘。改 9 个文件,但**全部用 `getattr(self.bus, 'ack_outbound', None)` 这种防御性写法**,`_on_outbound` 在 outbox 没启用时也不出问题。
- **SQLAlchemy session 跨 callback 嵌套**时,内层 S2 commit 完,外层 S1 的事务内 SELECT **可能** 看不到(SQLite WAL 的 read snapshot 在 BEGIN 时拿)。replay 内部直接 `mark_delivered`(同 session),不开新 session,避免这个问题。

---

## 模板(下次记录新坑时复制)

```markdown
### 坑 #N — <一句话症状>

**症状**

\`\`\`
<关键错误日志原文>
\`\`\`

**根因**

<一段话讲清楚为什么坏>

**修复**

<具体改动 + 文件路径 + 代码片段>

**教训**

<下次怎么避免>
```
