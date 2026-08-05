# Codex临时工作组、共享大脑，以及长期记忆

Codex多任务协作最大的问题不是模型不够聪明，而是任务一多、对话一长，临时结论、任务进度和长期事实全部混在聊天里。上下文压缩或更换对话后，成员就要重新转发历史。

我把它拆成了三层：

1. **LoopX**只管理任务、认领、完成和handoff。
2. **临时工作组**使用Python CLI、短期租约和追加式`events.jsonl`共享事实、假设、冲突和证据；`view.json`由事件账本重建，所以模型上下文可以丢失。
3. **长期记忆**先把旧session冻结、分类和时态合并，再保存来源、有效期和`supersedes`关系；Graphiti不可用时仍可用本地JSONL查询。

```mermaid
flowchart LR
  subgraph A["Codex任务与协调"]
    A1["Codex多任务"]
    A2["可信身份<br/>host_id + thread_id<br/>→ role / scope / lease"]
    A3["LoopX 0.4.1<br/>goal / claim / completion / handoff"]
    A1 --> A2
    A1 --> A3
  end

  subgraph B["临时工作组共享状态"]
    B1["workgroup_brain.py<br/>create / context / post / resolve"]
    B2["members.json<br/>成员、权限、租约Hash"]
    B3["events.jsonl<br/>追加写 + 文件锁 + Hash链"]
    B4["Reducer<br/>fsync + os.replace"]
    B5["view.json<br/>可重建物化快照"]
    B6["freeze / handoff / close<br/>FROZEN_SNAPSHOT + STABLE_HANDOFF"]
    B1 --> B2
    B1 --> B3
    B3 --> B4 --> B5
    B5 -->|"context恢复"| B1
    B1 --> B6
  end

  subgraph C["长期记忆"]
    C1["session归档<br/>manifest + source snapshot"]
    C2["候选提取<br/>classify + extract"]
    C3["时态合并<br/>merge + authority manifest"]
    C4["长期记忆包<br/>CORE + 来源账本 + supersedes"]
    C5["query_long_term_memory.py<br/>verify_long_term_memory_bundle.py"]
    C6["Graphiti导入适配<br/>可选；未接通时live writes = 0"]
    C1 --> C2 --> C3 --> C4 --> C5
    C4 -.-> C6
  end

  subgraph D["权威与观察"]
    D1["项目权威<br/>framework / gate / decision / handoff"]
    D2["不完整因果图<br/>CAUSAL_GRAPH + DSM + coverage / gaps"]
    D3["总控审核<br/>pending review"]
    D4["只读工作组前端<br/>ThreadingHTTPServer + sqlite3标题映射"]
    D1 --> D2
    D3 --> D1
  end

  A2 --> B1
  A3 -. "生命周期对账" .-> B6
  C5 -->|"冻结记忆切片"| B1
  D1 -->|"权威Hash"| B1
  D2 -->|"任务相关因果切片"| B1
  B6 --> D3
  B6 -->|"记忆候选"| C4
  B2 -. "只读成员投影" .-> D4

  E1["verify_workgroup_brain.py<br/>包外验证"]
  E2["错误身份、越权、过期租约、冻结后写入<br/>全部失败关闭"]
  B6 --> E1 --> E2

  classDef work fill:#ddf7ed,stroke:#24856d,color:#111;
  classDef memory fill:#fff4cc,stroke:#9a7d18,color:#111;
  classDef authority fill:#e7efff,stroke:#426fae,color:#111;
  classDef guard fill:#f3e8ff,stroke:#7651a8,color:#111;
  class B1,B2,B3,B4,B5,B6 work;
  class C1,C2,C3,C4,C5,C6 memory;
  class D1,D2,D3,D4 authority;
  class A1,A2,A3,E1,E2 guard;
```

实际运行时，一名成员写入阶段结果，另一名成员在独立上下文中直接读到；相反意见会形成冲突，由总控追加解决记录，而不是覆盖旧内容。最后冻结、生成handoff并撤销全部成员。当前随仓库演示产生12个生命周期事件、5条工作记录，关闭后活跃成员和未解决冲突都为0。

最终效果是：成员不用互相转发聊天；上下文压缩后可以恢复；工作组共识、长期记忆和项目正式真值互不冒充。

完整目录、CLI、Schema、负例和复刻步骤见[完整复刻规范](reproduction-guide.md)。
