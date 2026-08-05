# 工作组持久记忆 v5 可执行示例

这个示例验证四层结构：

1. `working_snapshot.json`：工作组热状态；
2. `group_memory_candidates.jsonl` 与 `group_memory_index.json`：工作组级持久记忆候选；
3. `events.jsonl`、冻结快照和稳定交接：不可变原始归档；
4. 项目级长期记忆：只保留审核接口，本示例不会自动提升。

它还验证：

- 240 条追加事件；
- 第 120 条后的异常恢复；
- 第 200 条后的只读压缩；
- `supersedes` 与冲突保留；
- 约 8KB、最多 20 条的上下文预算；
- goal、todo、claim、group、handoff 的双向绑定；
- 提前关闭时自动生成未审核记忆候选；
- 包外 Reader 独立重建。

在仓库根目录运行：

```powershell
python examples/workgroup-memory-v5/task_dispatch_gate.py
python examples/workgroup-memory-v5/run_v5_canary.py
python examples/workgroup-memory-v5/external_reader_v5.py
```

输出写入 `sandbox/runtime/v5-canary-group-001`，该目录已被 Git 忽略。

本示例不会保存聊天全文、明文租约、令牌或凭据，也不会启动 Graphiti 或修改任何正式项目状态。
