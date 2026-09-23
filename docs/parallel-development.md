# 双窗口并行开发契约

版本：1.0  
日期：2026-09-23

## 1. 总原则

两个窗口从同一个 Git 基线工作，禁止同时修改同一份工作目录。一个文件在一次迭代内只有一个写入者；共享入口只由集成分支串行修改。

目录：

```text
D:\2026暑假一切\进化酒馆\muliao                  # 集成检出
D:\2026暑假一切\进化酒馆\muliao-worktrees\ghost # 蜂群窗口
D:\2026暑假一切\进化酒馆\muliao-worktrees\voice # 言出法随窗口
```

分支：

```text
main
integration/two-lane
feature/ghost-swarm
feature/voice-command
```

## 2. 文件所有权

### Ghost 蜂群窗口独占

```text
swarm.py
swarm_api.py
swarm_runtime.py
swarm_models.py
swarm_store.py
capsule.py
static/swarm-controller.js
static/swarm-ui.js
static/swarm.css
tests/test_swarm*.py
tests/test_capsule.py
tests/swarm_fixture.py
```

蜂群窗口不得修改 `voice/**`、`static/voice/**` 或 `tests/test_voice_*.py`。

### 言出法随窗口独占

```text
voice/**
static/voice/**
tests/test_voice_*.py
requirements-voice.txt
voice-models.json
```

M0–M4 不得修改：

```text
static/index.html
static/app.js
static/styles.css
machine_tools.py
collectors.py
swarm*.py
capsule.py
```

语音应使用独立 `/voice/` 页面和 `/api/voice/*` 路由，不得第二次接管 `#send.onclick`，不得把语音命令写入聊天 `_sessions` 或缓存账本 `_cache_stats`。

### 仅集成分支可修改

```text
server.py
permissions.py
runtime_paths.py
jev_client.py
static/index.html
static/app.js
static/styles.css
requirements.txt
muliao.spec
installer.iss
deploy.py
使用说明.md
```

功能分支如需改变上述文件，应在交接说明中提供：目的、接口、测试和最小补丁，由集成窗口应用。

## 3. 冻结的共享契约

### SYSTEM_PROMPT

`server.py` 中 `SYSTEM_PROMPT` 必须逐字节不变。当前 UTF-8 SHA-256：

```text
50e07b3b31dc5ca87420135484c4002cbce6878ece982defb026ce36e12e7ea7
```

长度：3909 个 Unicode 字符。

### 权限

```text
DATA_SCOPES = notifications, processes, windows, browser, ai_logs, files, system
CAPABILITY_SCOPES = voice_control
```

规则：

- `all=true` 只开启七类 `DATA_SCOPES`。
- `voice_control` 默认关闭，只能独立显式授权。
- 能力权限变化不改变聊天或蜂群可见工具时，不清空聊天历史、不增加工具权限版本。
- `/api/collect` 只遍历 `DATA_SCOPES`。

### 蜂群 API

```text
POST /api/swarm/plan
POST /api/swarm/run
POST /api/swarm/{run_id}/cancel
GET  /api/swarm/{run_id}
```

蜂群 SSE 公共信封：

```json
{
  "event_id": "string",
  "seq": 1,
  "run_id": "string",
  "task_id": "string",
  "ts": 0.0,
  "type": "string",
  "payload": {}
}
```

只允许增加可选字段；不得删除、重命名或改变已有字段类型和语义。未知事件由客户端忽略。每个 run 只能有一个最终终态。

### 语音 API

语音功能只使用命名空间：

```text
/voice/
/api/voice/*
voice.* 事件
```

推荐事件：

```text
voice.state
voice.partial
voice.final
voice.decision
voice.confirmation
voice.action
voice.tts
voice.metric
voice.error
```

不得使用会与蜂群冲突的裸 `done`、`error`、`delta`。

## 4. 运行时隔离

| 环境 | 端口 | 数据目录 | 实例 Token |
|---|---:|---|---|
| Ghost | 8941 | `muliao-runtime/ghost` | `ghost-lane` |
| Voice | 8942 | `muliao-runtime/voice` | `voice-lane` |
| Integration | 8943 | `muliao-runtime/integration` | `integration` |

Git Bash 示例：

```bash
MULIAO_NO_BROWSER=1 \
MULIAO_PORT=8941 \
MULIAO_INSTANCE_TOKEN=ghost-lane \
MULIAO_DATA_DIR='D:/2026暑假一切/进化酒馆/muliao-runtime/ghost' \
python server.py
```

另一窗口替换成 Voice 对应值。

要求：

- 不使用 8930，避免连接已安装正式版。
- 每个 worktree 使用独立 `.venv`。
- 不修改 `LOCALAPPDATA`；真实通知和浏览器能力探测依赖它。
- 自动测试必须 mock 写动作和敏感采集。
- 两个功能窗口都不得运行 `deploy.py`。

## 5. 开发顺序

### Ghost 窗口

1. 修复运行中二次发送和结果写入错误会话。
2. 统一唯一 planner schema。
3. 修正持久化 schema：全局 `db_seq` 与每 run 的 `run_seq` 分离。
4. 接入真实 WorkCapsule 验证和 ACK。
5. 接入 `SwarmStore`、事件回放、恢复和 purge。
6. 再实现 pause/resume、产物和预算聚合。

### Voice 窗口

1. M0：本地唤醒、VAD、SenseVoice、Jev 白名单路由、打开记事本、本地 TTS。
2. M1：FAST、实时字幕、MiMo、复合命令、取消与打断基础。
3. M2：独立 `/voice/` 窗口与 `/api/voice/*`。
4. M3：UIA、OCR、validate、settle、stall、dry-run。
5. M4：受限上下文和指代。
6. M5：急停、回环防护、模型资产、打包需求交接。

M0–M5 完成前不做 M6 微信回复建议。

## 6. 交接格式

每次功能分支交接必须包含：

```text
Base commit:
Head commit:
Changed files:
Shared-file requests:
API/schema changes:
Tests run and results:
Known limitations:
```

禁止复制整个目录覆盖集成分支。功能分支用普通 merge commit 合入 `integration/two-lane`；失败用 `git revert -m 1` 回滚，不强制 reset 覆盖另一分支历史。

## 7. 集成顺序

1. 两个功能分支分别通过自己的单元测试。
2. 先合 `feature/ghost-swarm` 到 `integration/two-lane`。
3. 跑蜂群、权限、安全和 Prompt 哈希测试。
4. 再合 `feature/voice-command`。
5. 集成窗口应用共享入口的最小接线。
6. 跑全量测试与浏览器冒烟。
7. 最后统一修改 requirements、PyInstaller、Inno、deploy 和文档。
8. 只有集成窗口可构建、卸载旧版和安装到 D 盘。

## 8. 发布门禁

发布前必须满足：

- 全部自动测试通过。
- `SYSTEM_PROMPT` 哈希不变。
- 仓库扫描不含 API key、私钥、数据库或日志。
- 七类数据权限行为不回归。
- `voice_control` 默认关闭，`all=true` 不开启它。
- 蜂群计划、确认、取消、错误脱敏和唯一终态通过。
- 语音未唤醒时 Jev 调用为 0、云上传为 0、动作执行为 0。
- PyInstaller 包含新增模块；静态资源在安装态可访问。
- 构建完成后卸载旧版，安装到 `D:\Program Files\幕僚Muliáo`。
- 保存 EXE、安装包和 SHA-256，完成已安装态冒烟。

## 9. GitHub

远端仓库固定为：

```text
farlyfeifei/muliao
```

仓库必须保持 private，除非未来完成凭据、隐私、许可和发布审查后由用户明确要求公开。
