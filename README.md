# agent-brain

面向 Codex 多任务协作的临时工作组、共享工作状态、工作组持久记忆候选与长期记忆分层框架。

它解决三个不同问题：

- 谁正在做什么：工作组成员、角色、租约和任务边界；
- 当前共同知道什么：追加事件、可重建视图、冲突、证据和交接；
- 跨任务应保留什么：经过审核的持久记忆候选与 `supersedes` 关系。

它不保存聊天全文，不把 Agent 的“完成”自动升级成项目真值，也不允许临时工作状态覆盖正式控制面。

## 架构

```text
Codex任务
  ↓
工作组CLI
  ├─ members / leases
  ├─ append-only events
  ├─ bounded working snapshot
  ├─ group-memory candidates
  └─ stable handoff
  ↓
总控审核
  ├─ 正式项目状态
  └─ 项目级长期记忆
```

LoopX 或其他任务协调器可以绑定 `goal / todo / claim / completion / handoff`，但不拥有项目真值。Graphiti 可以作为长期记忆索引，但是否提升记忆仍由总控审核。

## 仓库内容

```text
src/
  workgroup_brain.py
  verify_workgroup_brain.py
  workgroup_status_frontend.py
  verify_workgroup_status_frontend.py

examples/workgroup-memory-v5/
  dispatch_contract_v5_task_spec.json
  task_dispatch_gate.py
  run_v5_canary.py
  external_reader_v5.py
  VALIDATED_CANARY_RESULTS.md

CODEX_WORKGROUP_SHARED_STATE_AND_LONG_TERM_MEMORY_REPRODUCTION_SPEC.md
```

## 快速验证

要求 Python 3.11+，核心实现只使用标准库。

```powershell
python -m py_compile src/*.py examples/workgroup-memory-v5/*.py
python src/verify_workgroup_status_frontend.py
python examples/workgroup-memory-v5/task_dispatch_gate.py
python examples/workgroup-memory-v5/run_v5_canary.py
python examples/workgroup-memory-v5/external_reader_v5.py
```

`src/verify_workgroup_brain.py` 是针对指定冻结工作组的包外 Reader，需要显式传入 `--group-dir`、`--protected-snapshot` 和 `--output`，不作为无参数 smoke test。

## 启动工作组前端

```powershell
.\start_frontend.ps1 -RuntimeRoot .\runtime -Port 8766
```

浏览器打开：

```text
http://127.0.0.1:8766/
```

前端只显示仍在组内的活跃成员。已退出、过期或撤权成员不会继续显示；历史仍保留在事件归档中。

## 安全边界

- `runtime/`、`sandbox/`、令牌、密钥和环境文件默认忽略；
- 前端不返回 `host_id`、`thread_id`、租约或 token hash；
- 工作组记忆候选默认是 `candidate/unreviewed`；
- 原始事件只追加，压缩不会删除或改写事件；
- 项目正式状态必须有独立总控接纳。

完整复刻规范见 [CODEX_WORKGROUP_SHARED_STATE_AND_LONG_TERM_MEMORY_REPRODUCTION_SPEC.md](CODEX_WORKGROUP_SHARED_STATE_AND_LONG_TERM_MEMORY_REPRODUCTION_SPEC.md)。
