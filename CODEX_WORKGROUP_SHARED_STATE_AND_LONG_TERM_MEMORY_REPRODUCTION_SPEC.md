# Codex临时工作组、共享大脑，以及长期记忆：完整复刻规范
```text
项目权威／长期记忆切片／不完整因果索引
→ 总控冻结任务上下文
→ 成员通过追加事件账本与可重建快照协作
→ 上下文丢失后重新读取同一状态
→ 冻结工作组并形成稳定交接
→ 总控审核后分别进入项目权威或长期记忆
```

---

## 0. 给收到本文的Codex

你的任务是在一个现有或新建项目中，复刻以下三套彼此分离的能力：

1. **临时工作组**：管理成员、角色、任务归属、租约、冻结、交接和关闭。
2. **工作组共享大脑**：让多个Codex任务不依赖互相转发聊天，也能读取同一份当前工作状态。
3. **长期记忆**：保存经过审核的跨任务事实、转折、失败路线和替代关系。

如果项目已经存在：

- 先发现项目根、现有控制面、运行目录和版本管理边界；
- 不覆盖既有状态；
- 把本系统作为项目拥有的适配层接入；
- 所有运行数据放在仓库之外，或加入本地忽略规则；
- 先完成隔离试验，再接真实任务。

如果项目尚不存在：

- 创建一个最小项目根；
- 按本文目录施工；
- 使用Python标准库优先实现，减少外部依赖；
- 所有命令提供结构化JSON输出和非零失败码。

除非项目路径或正式权威文件确实无法判断，否则不要停下来询问。完成后必须交付源码、配置、启动方式、验收回执和未解决边界。

---

## 1. 最终要得到的东西

完成后，多个Codex任务应具有以下行为：

```text
总控创建一个临时工作组
    ↓
绑定施工者、审查者和观察者
    ↓
每个成员通过可信身份和短期租约读取同一工作快照
    ↓
成员把事实、假设、结果、证据、问题和冲突写成有类型事件
    ↓
任何修正都追加新事件，不静默覆盖旧结论
    ↓
快照可由事件日志完全重建
    ↓
任务结束时冻结、生成handoff、撤销所有成员
    ↓
只有经总控审核的少量结论才能进入长期记忆候选
    ↓
工作组完成和长期记忆都不能自动改写项目正式真值
```

这套系统必须允许模型上下文被压缩或完全丢弃。成员下一轮只靠外部状态即可恢复：

```text
成员身份
+ 当前工作快照
+ 与当前任务相关的最近事件
+ 冻结的长期记忆切片
+ 未解决冲突
+ 证据指针
= 可继续工作的上下文
```

原始聊天、隐藏思维链和完整session不属于恢复输入。

---

## 2. 四层必须分开

| 层 | 负责 | 不负责 |
|---|---|---|
| 协调层（LoopX或等价实现） | goal、todo、claim、assignment、completion、handoff生命周期 | 组内事实、长期历史、正式接纳 |
| 临时工作组共享大脑 | 当前事实、假设、部分结果、问题、冲突、局部决定、证据 | 正式项目真值、跨任务永久记忆 |
| 长期记忆 | 经审核的跨任务事实、历史转折、有效期、来源和supersedes | 当前任务认领、工作组实时草稿 |
| 项目权威层 | 用户决定、正式状态、稳定handoff、发布和接纳 | 临时协作过程 |

强制边界：

```text
协调层的done != 项目完成
工作组共识 != 长期事实
长期记忆active != 项目正式真值
前端显示成员在线 != 成员产出了有效结果
```

不要实现一个可以同时直写四层的“万能网关”。任何提升都必须通过显式审核和追加式回执。

---

## 3. 推荐目录

### 3.1 项目拥有的代码

```text
<PROJECT_ROOT>/
├─ integrations/
│  ├─ workgroup/
│  │  ├─ workgroup_brain.py
│  │  ├─ verify_workgroup_brain.py
│  │  ├─ workgroup_contract.json
│  │  └─ README.md
│  ├─ coordination/
│  │  ├─ coordination_adapter.py
│  │  ├─ reconciliation.py
│  │  └─ coordination_contract.json
│  ├─ memory/
│  │  ├─ memory_store.py
│  │  ├─ memory_contract.json
│  │  ├─ LONG_TERM_MEMORY_POINTER.json
│  │  └─ README.md
│  └─ frontend/
│     ├─ workgroup_status_frontend.py
│     └─ start_workgroup_status_frontend.ps1
└─ project_control/                  # 若项目已有正式控制面则沿用
```

### 3.2 仓库之外的私有运行态

```text
<RUNTIME_ROOT>/
├─ coordination/
├─ workgroups/
│  ├─ <group_id>/
│  │  ├─ group.json
│  │  ├─ members.json
│  │  ├─ events.jsonl
│  │  ├─ view.json
│  │  ├─ FROZEN_SNAPSHOT.json
│  │  ├─ STABLE_HANDOFF.json
│  │  └─ receipts/
│  └─ tombstones/
├─ long_term_memory/
│  ├─ events.jsonl
│  ├─ current_index.json
│  ├─ candidates/
│  ├─ receipts/
│  └─ archive/
└─ frontend/
```

运行态不得提交Git。若必须放在项目目录，至少把以下内容加入本地忽略：

```gitignore
.loopx/
.codex/goals/
runtime/
*.lease
*.token
```

---

## 4. 临时工作组的数据合同

## 4.1 `group.json`

```json
{
  "schema_version": "codex_workgroup_v1",
  "group_id": "group-001",
  "task_id": "task-001",
  "project_name": "实际项目名称",
  "display_name": "本工作组的实际工作包名称",
  "objective": "一句话、可验收的当前目标",
  "status": "ACTIVE",
  "created_at": "ISO-8601",
  "expires_at": "ISO-8601",
  "controller_member_id": "controller-001",
  "authority_bundle_sha256": "冻结输入的SHA-256",
  "coordination_goal_id": "goal-001",
  "member_registry_sha256": "成员表语义SHA-256",
  "event_log_sha256": null,
  "view_sha256": null
}
```

合法状态：

```text
PLANNED
OPEN
MEMBERS_BOUND
ACTIVE
FREEZING
HANDOFF_READY
RECONCILED
MEMBERS_REVOKED
EXPIRED_OR_ARCHIVED
```

状态只能向前迁移。异常退出后允许从事件源重建，但不能倒退到较早状态。

## 4.2 `members.json`

```json
{
  "schema_version": "codex_workgroup_members_v1",
  "group_id": "group-001",
  "members": [
    {
      "member_id": "worker-001",
      "host_id": "由可信启动器注入",
      "thread_id": "由可信启动器注入",
      "display_name": "Codex界面中的实际任务名称",
      "role": "worker",
      "read_scope": ["task/shared"],
      "write_scope": ["task/shared"],
      "joined_at": "ISO-8601",
      "lease_expires_at": "ISO-8601",
      "lease_token_sha256": "只保存摘要",
      "status": "ACTIVE"
    }
  ]
}
```

角色固定为：

- `controller`
- `worker`
- `reviewer`
- `observer`

权限规则：

| 角色 | 读取 | 追加事件 | 解决冲突 | 增删成员 | 冻结/关闭 |
|---|---:|---:|---:|---:|---:|
| controller | 是 | 是 | 是 | 是 | 是 |
| worker | 是 | 是 | 否 | 否 | 否 |
| reviewer | 是 | 是 | 是 | 否 | 否 |
| observer | 是 | 否 | 否 | 否 | 否 |

成员不能在提示词中自行声明身份。`host_id`、`thread_id`和租约必须由可信启动器、桌面工具元数据或本地包装器注入。

明文租约令牌：

- 只在创建或绑定成员时返回一次；
- 只存在于该成员进程内；
- 不写入聊天；
- 不写入事件；
- 不写入仓库；
- 服务端只保存SHA-256摘要。

## 4.3 `events.jsonl`

它是临时工作状态的唯一源账本。每行一个完整JSON对象：

```json
{
  "schema_version": "codex_workgroup_event_v1",
  "seq": 12,
  "event_id": "event-012",
  "group_id": "group-001",
  "task_id": "task-001",
  "author_member_id": "worker-001",
  "entry_type": "PARTIAL_RESULT",
  "subject_key": "module.interface",
  "content": "已得到一个阶段结果",
  "payload": {},
  "status": "candidate",
  "confidence": 0.82,
  "scope": ["task/shared"],
  "evidence_refs": ["artifacts/result.json"],
  "supersedes": [],
  "created_at": "ISO-8601",
  "previous_event_hash": "上一事件的content_hash",
  "content_hash": "本事件规范化后的SHA-256"
}
```

固定事件类型：

```text
FACT_CONFIRMED
HYPOTHESIS
HYPOTHESIS_REJECTED
PARTIAL_RESULT
ARTIFACT_PUBLISHED
EVIDENCE_ATTACHED
QUESTION_OPENED
QUESTION_RESOLVED
CONFLICT_RECORDED
LOCAL_DECISION
SCOPE_WARNING
HANDOFF_READY
```

写入要求：

1. 严格追加，不允许原地编辑已有行。
2. `seq`连续递增。
3. `previous_event_hash`形成哈希链。
4. 相同`event_id`幂等，不得重复生效。
5. 修正旧结论时必须填写`supersedes`。
6. 互相矛盾的有效事件必须进入`CONFLICT_RECORDED`，不能只保留最后写入者。
7. 证据只保存相对路径、内容摘要或安全URL，不保存凭据。

并发写入必须使用同一文件锁。推荐：

- Windows：`msvcrt.locking`；
- Unix：`fcntl.flock`；
- 跨平台时可实现锁文件加原子创建；
- 写入临时文件后`fsync`，再原子替换物化视图。

## 4.4 `view.json`

`view.json`是可丢弃、可重建的当前快照，不是第二真值源。

```json
{
  "schema_version": "codex_workgroup_view_v1",
  "group_id": "group-001",
  "snapshot_version": 12,
  "last_event_hash": "event-012的content_hash",
  "semantic_sha256": "忽略波动字段后的语义SHA-256",
  "objective_and_scope": {},
  "frozen_authority_context": {},
  "current_claims": [],
  "confirmed_facts": [],
  "current_best_hypotheses": [],
  "rejected_routes": [],
  "partial_results": [],
  "artifact_and_evidence_refs": [],
  "open_questions": [],
  "conflicts": [],
  "next_actions": [],
  "handoff_readiness": {}
}
```

必须提供独立命令：

```text
rebuild-view
```

该命令删除旧`view.json`后，仅依据`group.json`、`members.json`和`events.jsonl`重建，并得到相同语义Hash。

---

## 5. 工作组黑盒接口

至少实现以下CLI；后续可再包装为MCP，但CLI必须先独立可用。

```text
create
add-member
remove-member
context
post
resolve
rebuild-view
freeze
handoff
close
status
```

统一调用形状：

```powershell
python workgroup_brain.py --root <RUNTIME_ROOT> <command> <arguments>
```

所有成功输出必须是JSON：

```json
{
  "ok": true,
  "command": "post",
  "group_id": "group-001",
  "receipt": {},
  "error": null
}
```

所有失败也必须是JSON，并非零退出：

```json
{
  "ok": false,
  "command": "post",
  "group_id": "group-001",
  "receipt": null,
  "error": {
    "code": "LEASE_EXPIRED",
    "message": "member lease has expired"
  }
}
```

建议退出码：

| 退出码 | 含义 |
|---:|---|
| 0 | 成功 |
| 2 | 参数或Schema错误 |
| 3 | 身份、权限或租约错误 |
| 4 | 生命周期状态不允许 |
| 5 | 哈希、并发或事件冲突 |

### 5.1 `create`

输入：

```text
group_id
task_id
objective
controller_member_id
host_id
thread_id
scope
lease_hours
expires_hours
authority_bundle_sha256
coordination_goal_id
```

输出一次性`controller_lease_token`。重复创建同一`group_id`必须失败，除非输入完全相同且明确使用幂等参数。

### 5.2 `add-member`

只能由controller执行。绑定：

```text
member_id
role
host_id
thread_id
display_name
scope
lease_hours
```

输出一次性成员租约令牌。

### 5.3 `context`

校验：

```text
member_id
lease_token
host_id
thread_id
requested_scope
```

返回：

- 当前工作快照；
- 该成员任务；
- 与scope相关的最近事件；
- 未解决冲突；
- 冻结的长期记忆切片；
- 证据指针；
- 当前语义Hash。

不得返回：

- 其他成员令牌；
- 原始聊天；
- 隐藏思维链；
- 不属于该scope的内部信息；
- 整个长期记忆库。

### 5.4 `post`

成员只能追加自己scope内的有类型事件。每次成功后：

```text
锁定事件源
→ 校验哈希链和seq
→ 追加事件
→ fsync
→ 重建view
→ 原子替换view
→ 返回事件与视图Hash
```

### 5.5 `resolve`

仅controller或reviewer可用。它不能删除冲突，只能追加：

- `QUESTION_RESOLVED`
- `HYPOTHESIS_REJECTED`
- `LOCAL_DECISION`
- 带`supersedes`的新结论。

### 5.6 `freeze`

冻结后：

- 普通`post`和`resolve`全部失败；
- 计算事件源、成员表、视图和冻结输入的Hash；
- 生成`FROZEN_SNAPSHOT.json`；
- 所有有效成员读取到相同`semantic_sha256`。

### 5.7 `handoff`

只从冻结快照生成：

```json
{
  "schema_version": "codex_workgroup_handoff_v1",
  "group_id": "group-001",
  "task_id": "task-001",
  "status": "PENDING_PROJECT_REVIEW",
  "summary": "工作组交接摘要",
  "delivered_artifacts": [],
  "evidence_refs": [],
  "confirmed_facts": [],
  "open_questions": [],
  "conflicts": [],
  "rejected_routes": [],
  "memory_candidates": [],
  "coordination_reconciliation_ref": "receipt.json",
  "frozen_snapshot_sha256": "SHA-256",
  "created_at": "ISO-8601"
}
```

handoff不能自动写正式项目状态，也不能自动晋升长期记忆。

### 5.8 `close`

关闭前门：

```text
writes_frozen
event_log_hashed
view_hashed
handoff_generated
coordination_completion_reconciled
all_member_leases_revocable
project_authority_automatic_writes_zero
long_term_memory_automatic_writes_zero
```

关闭后：

- 立即撤销全部成员；
- 所有`context`、`post`和`resolve`失败；
- `archive`保留完整只读目录；
- `delete`只保留最小tombstone和handoff摘要。

---

## 6. 成员每轮怎样使用共享大脑

所有成员提示词或启动器必须包含这条固定协议：

```text
开始工作前：
1. 使用可信身份调用context；
2. 读取当前任务、共享快照、未解决冲突和证据指针；
3. 不依赖对话记忆猜测工作组状态。

工作过程中：
4. 重要事实、阶段结果、问题、冲突和工件立即使用post追加；
5. 不记录隐藏思维链和冗长聊天；
6. 不静默覆盖其他成员的结论；
7. 超出scope时追加SCOPE_WARNING并停止越界施工。

结束前：
8. 发布PARTIAL_RESULT或HANDOFF_READY；
9. 附上可复核证据；
10. 不自行修改项目正式真值或长期记忆。
```

推荐把以下内容写成机器生成的成员上下文，而不是人工长提示词：

```text
workgroup_id
member_id
role
objective
assigned_task
frozen_authority_hash
current_view_hash
current_facts
current_hypotheses
open_conflicts
open_questions
evidence_refs
write_scope
prohibited_actions
```

---

## 7. 协调层接入

如果已经安装LoopX，使用它记录：

```text
goal
todo
claim
assignment
completion
handoff lifecycle
```

工作组只保存：

```text
facts
hypotheses
partial results
questions
conflicts
local decisions
evidence references
```

推荐状态：

```text
OPEN
→ CLAIMED
→ DONE_IN_COORDINATION
→ PENDING_PROJECT_REVIEW
→ ACCEPTED | REJECTED | SUPERSEDED
```

协调层只需要通过适配器把以下指针投影给工作组：

```json
{
  "goal_id": "goal-001",
  "task_id": "task-001",
  "claimed_by": "worker-001",
  "status": "CLAIMED",
  "evidence_refs": [],
  "source_state_sha256": "SHA-256"
}
```

工作组关闭前生成一份`coordination reconciliation receipt`，证明：

- claim与成员身份一致；
- completion有actor、note和evidence；
- 重复对账幂等；
- completion尚未被冒充为正式项目接纳。

如果没有LoopX：

- 实现同样字段的最小`coordination/events.jsonl`；
- 不因此削弱工作组、长期记忆或项目权威边界；
- 后续可以替换协调后端，而不改工作组事件合同。

---

## 8. 长期记忆

长期记忆与工作组运行态完全独立。成员默认不能在运行中搜索整个长期记忆库。

## 8.1 记忆记录

```json
{
  "schema_version": "codex_long_term_memory_v1",
  "memory_id": "memory-001",
  "author_type": "controller",
  "agent_id": "controller-001",
  "source_thread": "thread-id",
  "source_workgroup": "group-001",
  "task_id": "task-001",
  "content": "一条经过审核的长期结论",
  "status": "active",
  "confidence": 0.93,
  "created_at": "ISO-8601",
  "valid_at": "ISO-8601",
  "invalid_at": null,
  "evidence_refs": ["evidence/file.json"],
  "supersedes": [],
  "scope": ["module-a"],
  "content_hash": "SHA-256"
}
```

合法状态：

```text
active
uncertain
superseded
rejected
archived
```

## 8.2 提升流程

```text
工作组handoff
→ 提取memory_candidates
→ 写入待审核队列
→ controller逐条接受、拒绝或要求补证据
→ 接受项追加进入长期记忆事件源
→ 重建current_index
```

禁止：

- 工作组关闭时全量导入；
- 成员直接写`active`长期记忆；
- 新结论覆盖旧文件；
- 删除不再有效的旧结论；
- 不记录来源、时间和supersedes。

## 8.3 新任务挂载记忆

创建新工作组前，controller执行：

```text
按task_id、scope、时间和状态检索
→ 选择少量相关记忆
→ 排除rejected和无关历史
→ 保留active、必要的uncertain及其supersedes链
→ 生成冻结记忆切片
→ 计算SHA-256
→ 将切片作为authority bundle的一部分挂载
```

成员只读冻结切片；运行过程中即使长期记忆库变化，也不能静默改变当前工作组输入。

## 8.4 存储后端

第一版使用JSONL即可复刻完整语义。Graphiti、SQLite或图数据库只是后端替换：

```text
统一memory contract
        ↓
JSONL | SQLite | Graphiti | 其他图数据库
```

无论使用什么后端，都必须保留：

- 时间有效性；
- 来源；
- 证据；
- 状态；
- 可信度；
- scope；
- supersedes关系；
- 追加式审计轨迹。

---

## 8.5 已实际采用的长期记忆工具链

要复刻本文所描述的当前能力，不能只创建一个`memory.jsonl`。至少应提供下面这些同名或等价工具。它们分成“原始会话归档”“候选提炼”“时态合并”“权威对账”“检索与验证”五段。

| 工具 | 输入 | 输出 | 作用 |
|---|---|---|---|
| `build_session_archive_manifest.py` | 历史session归档根 | 归档清单 | 只做文件身份、大小、时间与Hash登记，不把原文注入模型 |
| `build_session_source_snapshot.py` | 一个当前或历史session | 冻结源快照 | 分离源文件、消息索引和冻结前缀，保证后续可追溯 |
| `classify_session_memory_candidates.py` | 结构化消息 | 分类候选目录 | 从用户决定、转折、任务结果、冲突等类型中提取候选 |
| `extract_key_referenced_sessions.py` | 关键任务选择表与多处归档根 | 关键任务副本/索引 | 只深读被明确引用的旧任务，避免扫描结果全部进入上下文 |
| `merge_temporal_memory_candidates.py` | 当前快照与关键旧任务 | 时态候选账本 | 合并重复节点，保留来源、时间、状态与潜在supersedes边 |
| `build_project_authority_manifest.py` | 正式控制面目录 | 权威来源清单 | 钉住当前正式文件的路径与Hash，防止对话记忆覆盖较新真值 |
| `promote_conversation_source_events.py` | 合并候选与人工整理核心 | 来源事件账本 | 把可定位的对话事件提升成可查询但非正式权威的长期来源 |
| `build_graphiti_shadow_import.py` | 语义化用户消息与来源清单 | Graphiti导入请求 | 生成待发送episode，不直接调用Graphiti |
| `build_graphiti_curated_import.py` | 人工整理长期记忆核心 | 精选Graphiti导入请求 | 为少量高价值记忆生成独立episode请求 |
| `query_long_term_memory.py` | 精选核心、合并账本、查询词 | 相关记忆切片 | Graphiti不可用时的确定性本地检索后备 |
| `verify_long_term_memory_bundle.py` | 记忆指针、脚本目录 | 独立验证回执 | 复核文件存在、Hash、计数、来源和权威边界 |

推荐运行顺序：

```text
历史session
→ build_session_archive_manifest
→ build_session_source_snapshot
→ classify_session_memory_candidates
→ extract_key_referenced_sessions
→ merge_temporal_memory_candidates
→ build_project_authority_manifest
→ 人工整理长期记忆核心
→ promote_conversation_source_events
→ build_graphiti_*_import
→ query_long_term_memory / Graphiti
→ verify_long_term_memory_bundle
```

推荐的长期记忆发布目录：

```text
<RUNTIME_ROOT>/long_term_memory/<release_id>/
├─ LONG_TERM_MEMORY_CORE.md
├─ LONG_TERM_MEMORY_CORE.json
├─ CONVERSATION_SOURCE_EVENT_MEMORY_LEDGER.jsonl
├─ MERGED_TEMPORAL_MEMORY_CANDIDATES.jsonl
├─ POTENTIAL_SUPERSESSION_EDGES.jsonl
├─ SOURCE_OCCURRENCE_INDEX.jsonl
├─ PROJECT_AUTHORITY_SOURCE_MANIFEST.json
├─ GRAPHITI_CURATED_CORE_ADD_EPISODE_REQUESTS.jsonl
├─ GRAPHITI_SOURCE_EVENT_ADD_EPISODE_REQUESTS.jsonl
└─ LONG_TERM_MEMORY_BUILD_RECEIPT.json
```

项目内只保留一个轻量指针：

```json
{
  "schema_version": "codex_long_term_memory_pointer_v1",
  "project_id": "project-id",
  "read_order": [
    {
      "role": "curated_human_readable_core",
      "path": "<absolute-runtime-path>/LONG_TERM_MEMORY_CORE.md",
      "sha256": "SHA-256"
    },
    {
      "role": "curated_machine_core",
      "path": "<absolute-runtime-path>/LONG_TERM_MEMORY_CORE.json",
      "sha256": "SHA-256"
    },
    {
      "role": "exact_source_event_ledger",
      "path": "<absolute-runtime-path>/CONVERSATION_SOURCE_EVENT_MEMORY_LEDGER.jsonl",
      "sha256": "SHA-256",
      "read_policy": "query_only_unless_lineage_dispute"
    },
    {
      "role": "merged_temporal_candidate_ledger",
      "path": "<absolute-runtime-path>/MERGED_TEMPORAL_MEMORY_CANDIDATES.jsonl",
      "sha256": "SHA-256",
      "read_policy": "query_only_unless_temporal_reconciliation"
    },
    {
      "role": "project_authority_shadow_manifest",
      "path": "<absolute-runtime-path>/PROJECT_AUTHORITY_SOURCE_MANIFEST.json",
      "sha256": "SHA-256"
    }
  ],
  "graphiti": {
    "requested": true,
    "callable": false,
    "live_episode_writes": 0,
    "curated_import_requests": "<path-or-null>",
    "source_event_import_requests": "<path-or-null>"
  },
  "fallback_query": {
    "script": "integrations/memory/query_long_term_memory.py"
  }
}
```

Graphiti边界必须如实记录：

- Graphiti MCP可调用时，才执行真实`add_episode`；
- MCP不可调用时，只生成待导入请求和本地检索索引；
- “生成Graphiti请求文件”不等于“已经写入Graphiti”；
- Graphiti中的episode仍然不是项目正式真值；
- 新任务通过长期记忆指针或查询接口读取，不直接吞入整个图。

本地后备查询的调用形状：

```powershell
python query_long_term_memory.py `
  --core <LONG_TERM_MEMORY_CORE.json> `
  --merged <MERGED_TEMPORAL_MEMORY_CANDIDATES.jsonl> `
  --query "<当前任务查询>" `
  --limit 20
```

验证调用形状：

```powershell
python verify_long_term_memory_bundle.py `
  --pointer <LONG_TERM_MEMORY_POINTER.json> `
  --scripts-dir <integrations/memory> `
  --output <verification-receipt.json>
```

## 8.6 运行期记忆与历史重建不能混用

上述工具链有两类用途：

```text
历史重建
    原始session → 候选 → 时态合并 → 精选核心

日常运行
    工作组handoff → memory candidates → controller审核 → 追加长期记忆
```

历史重建是低频迁移任务；不能在每个新工作组启动时重新扫描全部session。日常运行只读取：

- 当前精选核心；
- 当前任务相关查询结果；
- 必要的supersedes链；
- 当前权威来源Hash。

---

## 9. 不完整因果图怎样接入

因果图不是完整世界模型，也不能因为被画成一张图就被视为完备。它在本架构中是一种**带覆盖声明的派生索引**：

```text
项目权威事实
+ 已审核长期记忆
+ 工作组当前候选
→ 带来源与状态的因果节点/边
→ 当前任务的可读因果切片
```

因果图不得成为第五个任意写入的真值源。每条边必须说明来自哪一层。

## 9.1 节点与边

```json
{
  "node_id": "node-001",
  "node_type": "goal|state|mechanism|artifact|decision|risk|unknown",
  "label": "节点名称",
  "status": "confirmed|candidate|uncertain|superseded|rejected|unknown",
  "scope": ["module-a"],
  "valid_at": "ISO-8601",
  "invalid_at": null,
  "source_refs": ["memory-or-authority-ref"],
  "content_hash": "SHA-256"
}
```

```json
{
  "edge_id": "edge-001",
  "from": "node-001",
  "to": "node-002",
  "relation": "enables|blocks|depends_on|produces|consumes|invalidates|supersedes",
  "status": "confirmed|candidate|uncertain|rejected|unknown",
  "confidence": 0.8,
  "source_layer": "project_authority|long_term_memory|workgroup_candidate",
  "source_refs": ["evidence-ref"],
  "valid_at": "ISO-8601",
  "invalid_at": null,
  "content_hash": "SHA-256"
}
```

## 9.2 必须同时发布覆盖与缺口

除`CAUSAL_GRAPH.json`外，必须发布：

```text
CAUSAL_GRAPH_COVERAGE.json
CAUSAL_GRAPH_GAPS.jsonl
CAUSAL_GRAPH_RECEIPT.json
```

`CAUSAL_GRAPH_COVERAGE.json`至少包含：

```json
{
  "schema_version": "codex_causal_graph_coverage_v1",
  "graph_status": "INCOMPLETE",
  "declared_domains": [],
  "represented_domains": [],
  "missing_domains": [],
  "confirmed_node_count": 0,
  "candidate_node_count": 0,
  "unknown_node_count": 0,
  "confirmed_edge_count": 0,
  "candidate_edge_count": 0,
  "unsupported_edge_count": 0,
  "known_blind_spots": [],
  "claim_ceiling": "This graph is a partial, sourced index."
}
```

缺口记录：

```json
{
  "gap_id": "gap-001",
  "scope": ["module-a"],
  "missing_node_or_relation": "尚未建立的节点或关系",
  "why_missing": "缺数据、缺机制、尚未审核或明确延期",
  "status": "OPEN|DEFERRED|ABSTAIN",
  "required_evidence": [],
  "owner": null
}
```

## 9.3 因果图更新规则

```text
正式权威变化
→ 追加新的图节点/边或supersedes

长期记忆变化
→ 更新历史/研究关系，但不覆盖正式权威

工作组候选
→ 只能进入candidate边

缺少证据
→ 写UNKNOWN/ABSTAIN或gap
→ 不自动补边
```

强制规则：

- 图不完整时必须明确标记`INCOMPLETE`；
- 图上没有节点不代表现实中不存在；
- candidate边不能被下游当成confirmed依赖；
- 可视化布局不能反向证明因果；
- 任何自动推断边必须携带算法、输入、置信度和可撤销状态；
- 新图不能静默覆盖旧图，必须保留版本和supersedes；
- 因果图只提供上下文导航、依赖检查和缺口发现，不能绕过项目正式接纳。

工作组`context`应只挂载当前任务相关的因果切片：

```text
当前目标的上游依赖
当前任务直接影响的下游
当前阻塞
当前未知缺口
与本任务相关的supersedes链
```

不能把完整因果图默认塞进所有成员上下文。

---

## 10. 只读工作组前端

前端名称使用“工作组”，不要显示“共享脑”。

至少显示：

- 当前活动工作组数；
- 当前活跃成员数；
- 每个工作组的真实`project_name`或`display_name`、目标和状态；
- controller、worker、reviewer、observer数量；
- 各成员的Codex实际任务名称；
- 租约是否有效；
- 最近更新时间；
- 已归档工作组列表。

页面标题禁止直接显示内部`task_id`、`group_id`或类似`WORKGROUP-...-R0`的机器标识。显示优先级固定为：

```text
display_name
→ project_name
→ 已登记的任务中文名称
→ 中文objective
→ “未命名项目”
```

如果知道项目真实名称，就不得退回“临时工作组运行”“共享大脑试验”之类的技术空话。

不得显示：

- 内部事实、假设和冲突正文；
- 原始聊天；
- `host_id`；
- `thread_id`；
- 租约明文；
- 凭据；
- 长期记忆正文；
- 项目私有证据内容。

建议只读接口：

```text
GET /status.json
GET /groups.json
GET /groups/<group_id>.json
GET /coordination/status.json
```

接口只从运行态构建投影，绝不拥有写入能力。

默认监听：

```text
127.0.0.1:8766
```

不得默认绑定`0.0.0.0`。

---

## 11. 原子性、确定性与隐私要求

## 11.1 原子写入

除`events.jsonl`追加外，所有JSON文件使用：

```text
写入同目录临时文件
→ flush
→ fsync
→ os.replace
```

## 11.2 规范化Hash

语义Hash必须：

- UTF-8；
- JSON键排序；
- 固定分隔符；
- 排除生成时间、进程号、临时路径等波动字段；
- 对相同语义产生相同Hash。

## 11.3 隐私

以下内容不得进入共享状态：

```text
raw_chat_transcript
hidden_chain_of_thought
raw_session
lease_token
credentials
authorization_headers
unredacted_private_data
```

## 11.4 唯一写入者

- 事件日志：工作组CLI在锁内追加；
- 物化视图：reducer独占写入；
- 长期记忆：审核后的memory writer独占追加；
- 项目权威：既有项目总控独占写入；
- 前端：全部只读。

---

## 12. 必须实施的负例

不能只跑成功路径。至少证明下列操作全部失败且不改变稳定状态：

1. 未注册成员读取工作组；
2. 正确成员使用错误`host_id`；
3. 正确成员使用错误`thread_id`；
4. 错误租约令牌；
5. 过期租约；
6. observer尝试写入；
7. worker尝试添加成员；
8. worker写入越界scope；
9. 重复`event_id`携带不同内容；
10. 事件哈希链被手工破坏；
11. 不带`supersedes`静默替换旧结论；
12. 冻结后继续`post`；
13. 关闭后继续`context`；
14. 关闭后继续`post`；
15. handoff试图直接写项目权威；
16. 工作组事件试图自动写长期记忆；
17. 前端接口泄露`host_id`、`thread_id`或令牌；
18. 删除`view.json`后重建得到不同语义Hash。

每个负例必须记录：

```text
测试名称
预期错误码
实际错误码
稳定文件前后Hash
是否发生越界写入
```

---

## 13. 一次完整的真实运行过程

本节不是伪代码。下面的顺序已经用对应CLI实际执行过。租约令牌只保存在当前进程变量中，真实值不得写入文档或共享状态。

### 13.1 创建工作组

```powershell
$python = (Get-Command python).Source
$project = (Resolve-Path ".").Path
$brain = Join-Path $project "integrations\workgroup\workgroup_brain.py"
$runtime = Join-Path $env:LOCALAPPDATA "codex-workgroups\demo-runtime"
New-Item -ItemType Directory -Force -Path $runtime | Out-Null

$create = & $python $brain --root $runtime create `
  --group-id demo-group-001 `
  --task-id demo-task-001 `
  --objective "演示多个Codex任务共享工作状态并在关闭后撤权" `
  --controller-member-id controller-001 `
  --host-id host-controller `
  --thread-id thread-controller `
  --scope task/shared `
  --lease-hours 4 `
  --expires-hours 24 `
  --authority-bundle-sha256 <冻结输入SHA-256> `
  --loopx-goal-id demo-goal-001 |
  ConvertFrom-Json

$controllerToken = $create.lease_token
```

真实返回形状：

```json
{
  "controller_member_id": "controller-001",
  "group_id": "demo-group-001",
  "lease_token": "<只显示一次>",
  "lease_token_display_policy": "display_once_do_not_persist",
  "status": "CREATED",
  "view_version": 1
}
```

### 13.2 总控绑定两个成员

```powershell
$worker = & $python $brain --root $runtime add-member `
  --group-id demo-group-001 `
  --actor-member-id controller-001 `
  --lease-token $controllerToken `
  --actor-host-id host-controller `
  --actor-thread-id thread-controller `
  --member-id worker-001 `
  --role worker `
  --host-id host-worker `
  --thread-id thread-worker `
  --scope task/shared `
  --lease-hours 4 |
  ConvertFrom-Json

$workerToken = $worker.lease_token

$reviewer = & $python $brain --root $runtime add-member `
  --group-id demo-group-001 `
  --actor-member-id controller-001 `
  --lease-token $controllerToken `
  --actor-host-id host-controller `
  --actor-thread-id thread-controller `
  --member-id reviewer-001 `
  --role reviewer `
  --host-id host-reviewer `
  --thread-id thread-reviewer `
  --scope task/shared `
  --lease-hours 4 |
  ConvertFrom-Json

$reviewerToken = $reviewer.lease_token
```

成员全部保留在真实运行态中。只读前端列出所有成员和实际Codex任务名称，但成员卡右侧只标识唯一“总控”，不显示“施工”“审查”“观察”等功能标签。

### 13.3 施工成员发布阶段结果

```powershell
$partial = & $python $brain --root $runtime post `
  --group-id demo-group-001 `
  --member-id worker-001 `
  --lease-token $workerToken `
  --host-id host-worker `
  --thread-id thread-worker `
  --entry-type PARTIAL_RESULT `
  --subject-key demo.interface `
  --scope task/shared `
  --content "施工成员完成第一版接口，并发布可复核工件。" `
  --evidence-ref artifacts/demo-interface.json `
  --confidence 0.85 |
  ConvertFrom-Json
```

这一步真实产生：

```text
events.jsonl追加PARTIAL_RESULT
view.json的partial_results增加一项
view_version递增
```

### 13.4 审查成员独立读取

```powershell
$reviewerContext = & $python $brain --root $runtime context `
  --group-id demo-group-001 `
  --member-id reviewer-001 `
  --lease-token $reviewerToken `
  --host-id host-reviewer `
  --thread-id thread-reviewer `
  --scope task/shared |
  ConvertFrom-Json

$reviewerContext.context.partial_results
```

审查成员能够读取刚才的阶段结果，不需要施工成员转发聊天内容。

### 13.5 记录相反判断

```powershell
$conflict = & $python $brain --root $runtime post `
  --group-id demo-group-001 `
  --member-id reviewer-001 `
  --lease-token $reviewerToken `
  --host-id host-reviewer `
  --thread-id thread-reviewer `
  --entry-type CONFLICT_RECORDED `
  --subject-key demo.interface `
  --scope task/shared `
  --content "审查成员确认工件存在，但认为当前证据不足以支持接口已经完成。" `
  --evidence-ref reviews/demo-interface-review.json `
  --confidence 0.90 |
  ConvertFrom-Json
```

施工结果和审查异议同时保留。系统不能只留下最后写入者。

### 13.6 总控追加解决意见

```powershell
$resolution = & $python $brain --root $runtime resolve `
  --group-id demo-group-001 `
  --member-id controller-001 `
  --lease-token $controllerToken `
  --host-id host-controller `
  --thread-id thread-controller `
  --target-entry-id $conflict.entry_id `
  --status resolved `
  --resolution "保留施工结果和审查异议；当前只认定候选已经形成，完成主张等待独立消费证据。" |
  ConvertFrom-Json
```

解决冲突不会修改旧事件，而是追加一条`ENTRY_RESOLVED`。

### 13.7 丢弃进程上下文并恢复

关闭原来的成员进程，或至少清空所有进程内对象。重新注入可信身份和仍有效的租约后，再从一个全新进程调用`context`。

恢复结果必须包含：

- 工作组目标；
- 施工结果；
- 审查异议；
- 总控解决意见；
- 当前事件链头；
- 当前语义Hash。

不能从旧聊天中复制这些内容。这一步证明外部状态而非模型上下文承担连续性。

### 13.8 冻结、交接和关闭

```powershell
$freeze = & $python $brain --root $runtime freeze `
  --group-id demo-group-001 `
  --member-id controller-001 `
  --lease-token $controllerToken `
  --host-id host-controller `
  --thread-id thread-controller `
  --reason "演示完成，冻结共享工作状态。" |
  ConvertFrom-Json

$handoff = & $python $brain --root $runtime handoff `
  --group-id demo-group-001 `
  --member-id controller-001 `
  --lease-token $controllerToken `
  --host-id host-controller `
  --thread-id thread-controller `
  --summary "三个成员完成共享读取、冲突保留和追加式解决。" `
  --evidence-ref artifacts/demo-interface.json `
  --evidence-ref reviews/demo-interface-review.json `
  --loopx-reconciliation-ref coordination/demo-reconciliation.json |
  ConvertFrom-Json

$close = & $python $brain --root $runtime close `
  --group-id demo-group-001 `
  --member-id controller-001 `
  --lease-token $controllerToken `
  --host-id host-controller `
  --thread-id thread-controller `
  --reason "交接已经生成，关闭并撤销全部成员。" `
  --retention archive |
  ConvertFrom-Json
```

冻结后再次`post`必须失败。关闭后所有成员再次`context`和`post`都必须失败。

### 13.9 查看真实终态

```powershell
& $python $brain --root $runtime status --group-id demo-group-001
```

一次真实执行得到的终态形状：

```json
{
  "counts": {
    "active_members": 0,
    "entries": 2,
    "events": 9,
    "open_conflicts": 0,
    "open_questions": 0
  },
  "group_id": "demo-group-001",
  "state": "ARCHIVED",
  "view_version": 9
}
```

运行目录实际出现：

```text
events.jsonl
group.json
members.json
view.json
FROZEN_SNAPSHOT.json
STABLE_HANDOFF.json
```

这才叫真实运行：一个成员写入，另一个独立成员读取，相反意见被保留，总控追加解决，工作组冻结并形成交接，最后全部成员撤权。只有目录、Schema或演示文本不算运行。

---

## 14. 复刻施工顺序

收到本文的Codex按以下顺序执行，不要先做漂亮前端：

### Stage 1：发现与隔离

1. 发现`PROJECT_ROOT`。
2. 识别现有正式控制面及唯一写入者。
3. 选择仓库之外的`RUNTIME_ROOT`。
4. 记录初始权威文件Hash。
5. 创建项目拥有的`integrations`目录和合同。

### Stage 2：工作组内核

1. 实现成员身份和租约。
2. 实现追加式事件源。
3. 实现物化视图reducer。
4. 实现冲突和supersedes。
5. 实现生命周期。
6. 实现冻结、handoff和关闭。

### Stage 3：包外验证器

验证器不得导入主实现。它直接读取运行目录并复核：

- Schema；
- 哈希链；
- `seq`连续性；
- 视图可重建性；
- 成员权限；
- 生命周期；
- 稳定文件；
- handoff引用；
- 禁止写入边界。

### Stage 4：协调层

1. 接入LoopX或最小协调账本。
2. 对账claim、completion和handoff。
3. 生成待项目审核回执。
4. 保持正式项目状态零自动写入。

### Stage 5：长期记忆

1. 实现session归档清单与冻结源快照。
2. 实现消息分类、关键任务抽取与时态候选合并。
3. 实现项目权威来源清单。
4. 实现记忆候选队列与controller审核。
5. 实现时态状态和supersedes。
6. 实现任务相关冻结切片。
7. 实现本地查询、Graphiti导入请求和独立验证器。
8. 保持工作组零自动提升；Graphiti不可调用时不得伪报已写入。

### Stage 6：不完整因果图

1. 从正式权威、长期记忆和工作组候选生成分层图。
2. 为每条节点和边保存来源、状态、时间、scope与Hash。
3. 同时发布coverage、gaps和receipt。
4. 图不完整时明确标记`INCOMPLETE`。
5. 禁止candidate边冒充confirmed，禁止自动补齐未知因果。

### Stage 7：只读前端

1. 显示工作组和成员。
2. 使用Codex实际任务名称作为`display_name`。
3. 不显示内部工作内容和身份机密。
4. 关闭工作组后实时更新人数和状态。

### Stage 8：真实恢复演练

至少使用三个独立进程模拟：

- controller；
- worker；
- reviewer。

演练过程：

```text
controller创建组
→ worker发布阶段结果
→ reviewer读取该结果并提出相反判断
→ 系统保留双方并形成冲突
→ controller追加局部决定
→ 删除各进程内存和view.json
→ 三进程重新读取context
→ 得到一致语义Hash
→ freeze
→ handoff
→ close
→ 全部租约撤销
```

这一步验证的是“上下文可丢弃、工作状态可恢复”，不是业务结论是否正确。

---

## 15. 最终验收门

只有全部满足才算复刻完成：

### A. 功能

- 三种以上角色可加入同一组；
- 一个成员的事件可被另一个成员通过`context`读取；
- 冲突双方都保留；
- 修正存在`supersedes`链；
- 快照可从事件源重建；
- 冻结、handoff、关闭完整执行；
- 长期记忆候选与正式记忆分离；
- 前端能显示真实工作组和成员名称。

### B. 安全

- 可信身份不依赖成员自报；
- 明文租约不落盘；
- 前端不泄露内部身份；
- 关闭后全部撤权；
- 工作组不能直写长期记忆；
- 工作组和协调层不能直写正式项目状态。

### C. 确定性

- 两次全新重建语义Hash一致；
- `events.jsonl`哈希链完整；
- `view.json`删除后可恢复；
- 重复命令幂等或明确拒绝；
- 稳定handoff不包含波动字段。

### D. 独立复核

- 包外验证器不导入主实现；
- 所有负例真实运行；
- 运行前后正式权威文件字节不变；
- 验收回执列出所有输入、输出和SHA-256。

---

## 16. Codex最终必须交付

```text
1. 工作组主CLI源码
2. 包外验证器源码
3. 协调层适配器
4. session归档、冻结、分类、关键任务抽取与时态合并工具
5. 长期记忆存储、审核、查询、Graphiti请求生成与独立验证器
6. 长期记忆运行目录指针
7. 带coverage与gaps的不完整因果图生成器
8. 只读状态前端及启动脚本
9. 全部JSON Schema或合同
10. 工作组运行目录指针
11. 最小演练脚本
12. 正例与负例验收回执
13. README
14. 一份稳定handoff
15. 一份反向审查报告
```

反向审查报告必须写：

- 真正实现了什么；
- 哪些只是接口；
- 哪些边界仍依赖宿主Codex提供可信身份；
- 是否存在并发、权限、恢复或隐私风险；
- 哪些结果不能证明；
- 下一项最小可证伪检查是什么。

不要用文件数、测试数或Hash代替功能。必须证明一个成员写入的有效工作状态，确实被另一个独立成员读取，并且在上下文丢失后仍能恢复。

---

## 17. 可直接复制给Codex的执行指令

```text
请完整阅读随附的《Codex临时工作组、共享大脑，以及长期记忆》。

你的任务不是总结文档，而是在当前项目中复刻它：

1. 自动发现项目根、正式控制面、运行目录和Git边界；
2. 不覆盖任何现有状态；
3. 按文档实现临时工作组、追加式共享工作状态、协调层适配和独立长期记忆；
4. 提供create/add-member/context/post/resolve/rebuild-view/freeze/handoff/close/status CLI；
5. 提供session归档清单、冻结快照、消息分类、关键任务抽取、时态合并、权威清单、长期记忆查询、Graphiti导入请求和独立验证工具；
6. 提供带source/status/coverage/gaps的不完整因果图，缺失关系必须标记UNKNOWN、ABSTAIN或DEFERRED，不得自动补齐；
7. 提供包外验证器、只读工作组前端和最小三进程恢复演练；
8. 真实运行全部正例和负例；
9. 保持项目正式权威零自动写入、长期记忆零自动提升；
10. 最后交付源码、配置、启动命令、验收回执、文件Hash、稳定handoff和反向审查。

不要停留在方案、Schema或说明文档；必须完成可执行实现。
不要把原始聊天或隐藏思维链保存为共享状态。
不要让成员通过提示词自行声明可信身份。
如果LoopX不可用，先用兼容合同的最小协调账本实现，保持以后可替换。
如果Graphiti不可用，先用JSONL实现完整长期记忆语义，不得因此阻塞工作组内核。
```

---

复刻成功不看“Codex记住了多少聊天”，只看三件事：

1. **临时协作可恢复**：任一成员上下文丢失后，都能从工作组外部状态继续任务。
2. **长期历史可追溯**：新任务只加载相关记忆，旧结论保留来源、有效期和替代关系。
3. **正式真值不被污染**：协调完成、工作组共识和长期记忆都必须经过独立审核，才能影响项目权威层。
