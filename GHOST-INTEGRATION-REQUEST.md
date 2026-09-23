# Ghost 蜂群集成请求

日期：2026-09-23  
来源分支：`feature/ghost-swarm`  
适用目标：`integration/two-lane`

本文只描述需要由集成窗口修改的共享文件。Ghost 功能分支不会直接修改这些文件，也不涉及 `voice/**`、`static/voice/**` 或任何言出法随接口。

## 1. 共享文件最小修改清单

仅需要集成窗口修改：

```text
server.py
muliao.spec
deploy.py（最终打包阶段）
```

不要修改：

```text
SYSTEM_PROMPT
/api/chat
voice/**
/api/voice/*
static/voice/**
```

## 2. 进程级长期 Ledger

### 目的

当前未接线时，`SwarmOrchestrator` 会为每次 run 创建临时内存 Ledger。功能分支已经实现 SQLite schema v2、事件回放、真实 Capsule/ACK、终态原子状态和 interrupted 恢复，但正式服务器必须注入同一个进程级 Ledger 才能获得重启恢复。

### 建议接线

在 `server.py` 的蜂群 composition root 中：

```python
from pathlib import Path
from runtime_paths import data_dir
from swarm_persistence import SwarmLedger
from swarm_store import SwarmStore

_swarm_store = SwarmStore(Path(data_dir()) / "swarm.db")
_swarm_ledger = SwarmLedger(_swarm_store)
_swarm_ledger.recover_interrupted_runs()
```

构造 orchestrator 时注入：

```python
_swarm_orchestrator = SwarmOrchestrator(
    _swarm_bee_runner,
    jev_ask,
    max_parallel=...,
    max_jev_calls=...,
    ledger=_swarm_ledger,
)
```

要求：

- 每个进程只创建一个 `SwarmStore` 和一个 `SwarmLedger`。
- 不要为每个请求重新建数据库。
- 应用关闭时调用 `_swarm_ledger.close()`；由于 Ledger持有外部 store，最终还应调用 `_swarm_store.close()`。
- 数据库路径必须走 `runtime_paths.data_dir()`，从而保持 Ghost、Voice、Integration 三个开发实例的数据隔离。
- `/api/permissions/purge` 的最终“一键焚毁”语义应决定是否删除 `swarm.db`。如果删除，必须先停止在途 swarm 并关闭连接。

## 3. confirmed 事件必须进入同一事件账本

### 当前共享入口问题

旧 `_ServerSwarmBackend` 在 orchestrator 外部合成 `swarm.confirmed seq=1`，随后把已经持久化的核心事件序号全部 `+1`。这会造成：

```text
实时 SSE：confirmed=1, plan=2, contract=3 ...
SQLite：   plan=1, contract=2 ...
```

而且旧 confirmed 使用伪造 task ID：

```text
tsk_confirmed_<run_id>
```

同一 run 因此出现两个 task ID。

### 集成要求

- 删除 `_ServerSwarmBackend` 中合成 confirmed、offset 和事后改写 seq 的逻辑。
- confirmation audit 作为 plan 的 `_confirmation_audit`/`confirmation` 输入传给核心 orchestrator。
- 核心 orchestrator 应使用真实 contract task ID，通过自身 `emit()` 发出并持久化 `swarm.confirmed`。
- 适配层不得在事件持久化后修改 `event_id`、`seq`、`run_id` 或 `task_id`。
- live SSE 与 `_swarm_ledger.events(run_id)` 必须逐项一致。

## 4. 权限快照的实时复查接口

真实 Capsule ACK 需要区分：

- 契约创建时的权限快照；
- ACK 时的当前权限快照；
- 接收蜂当前可用工具集合。

当前 `_ServerSwarmBackend` 应向 orchestrator 注入一个轻量 provider，而不是让 orchestrator把 contract snapshot 同时当作“当前权限”。建议接口：

```python
def _current_swarm_permissions() -> dict:
    return _swarm_permission_snapshot()
```

并由 orchestrator/ledger handoff exchange 在每次 ACK 前读取。要求：

- 权限版本变化后，旧 snapshot 不得继续被视为 current。
- 当前可见工具集合变化时，ACK 应返回 `forbidden`，下游蜂不得启动。
- 不要把 `voice_control` 加入聊天/蜂群工具集合。

## 5. 状态与事件回放

功能分支已提供：

```python
_swarm_ledger.status(run_id)
_swarm_ledger.events(run_id, after_seq)
_swarm_ledger.recover_interrupted_runs()
```

集成建议：

- `GET /api/swarm/{run_id}` 内存 miss 时回退到 Ledger。
- 后续可增加：

```text
GET /api/swarm/{run_id}/events?after=<run_seq>
```

- 不要让 `SwarmService._runs` 与 Ledger 对同一终态产生不同结论。
- 后端流正常 EOF 但没有显式终态时，必须返回 `swarm.error(code=missing_terminal)`；不能只把状态改为 completed。

## 6. 共享服务器 Planner 兼容层

功能分支已建立唯一：

```text
ghost.planner/1
```

集成时，`server.py` 中 `_swarm_recipe`、`_swarm_risk_score` 和 `_normalize_swarm_plan` 应逐步退化为兼容投影，不再重新决定 recipe、风险或用户 gate。

最低要求：

- 保留新计划中的 `planner_schema`、`risk`、`clarification`、`confirmation`、`user_gate`。
- `confirmed=True` 只能满足 confirmation，绝不能绕过 clarification。
- 不得通过把真实风险临时改成 `low` 来穿过服务门禁。
- 旧平面字段只用于兼容：`recipe/task_type/risk_level/needs_clarify/requires_confirmation`。

## 7. 打包文件

`muliao.spec` hidden imports 增加：

```text
swarm_planner
swarm_persistence
```

已有项继续保留：

```text
swarm_models
swarm_store
capsule
swarm
swarm_api
swarm_runtime
```

最终打包态必须验证：

```text
/swarm-ui.js               200
/swarm-controller.js       200
/swarm.css                 200
POST /api/swarm/plan       200
GET /api/swarm/<run_id>    200
```

## 8. 集成门禁

合并 Ghost 后、合并 Voice 前至少运行：

```bash
python -B -W error::ResourceWarning -m unittest discover -s tests -p "test_*.py" -q
```

并确认：

1. `SYSTEM_PROMPT` SHA-256 仍为：

```text
50e07b3b31dc5ca87420135484c4002cbce6878ece982defb026ce36e12e7ea7
```

2. 七类数据权限行为不变。
3. `voice_control` 默认关闭且不进入蜂群工具列表。
4. live SSE 与 Ledger replay 的 event ID、seq、run ID、task ID 一致。
5. confirmed、cancelled、done、error 都只有一个最终权威状态。
6. 权限撤销后旧 Capsule 不再 accepted。
7. 不运行 `deploy.py`，直至 Ghost 与 Voice 两条分支都已集成并完成源码审核。

## 9. 当前功能分支交付边界

Ghost 分支实现：

- 唯一 Planner schema。
- SQLite schema v2 与 migration。
- run-local event seq 和 replay。
- 真实 WorkCapsule / receiver ACK。
- Capsule/ACK 原子持久化与失效传播。
- 蜂群前端会话隔离、取消竞态、事件去重、ACK 诊断。
- 等待/确认恢复的底层接口。

Ghost 分支不实现：

- `server.py` 的长期 Ledger 装配。
- 通知触发候选任务。
- AionUi/MCP。
- 语音或电脑控制。
- 最终 PyInstaller/Inno 构建和 D 盘覆盖安装。
