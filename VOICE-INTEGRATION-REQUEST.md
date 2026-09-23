# VOICE-INTEGRATION-REQUEST

日期：2026-09-23  
分支：`feature/voice-command`  
范围：言出法随 M0–M5 的共享文件接线请求  
状态：M0 核心可通过 `python -m voice` 独立运行；以下改动由集成窗口在对应阶段统一应用。

## 0. M0 当前是否需要共享文件改动

**不需要。**

M0 已全部位于 `voice/**`，自动测试位于 `tests/test_voice_*.py`，依赖位于
`requirements-voice.txt`。可使用：

```bash
python -m voice --text "幕僚幕僚，打开记事本" --no-tts
```

默认 dry-run；只有显式 `--act` 才会真正启动记事本。真实麦克风使用 `--microphone`。
所有入口均先检查现有 `permissions.is_granted("voice_control")`。

---

## 1. `server.py`：注册独立语音 API 与生命周期

### 为什么需要

M2 需要在 8942 voice lane 和最终集成实例中提供 `/api/voice/*`，但语音不得进入聊天
`_sessions`、`_cache_stats`、`SYSTEM_PROMPT` 或 `machine_tools`。

### 建议的最小改动

1. 从 `voice.api` 导入独立 FastAPI router（将在 M2 由 voice 分支提供）。
2. 使用 `app.include_router(voice_router, prefix="/api/voice")`。
3. 进程关闭时只调用语音 runtime 的 `close()`，释放麦克风、HTTP client 和播放器。
4. 为 `/voice/` 提供独立静态页入口，不能复用或修改聊天发送按钮。
5. 不改 `SYSTEM_PROMPT`，不改聊天、蜂群和缓存逻辑。

### 输入输出接口

建议端点：

```text
GET  /api/voice/status
POST /api/voice/start
POST /api/voice/stop
POST /api/voice/command/test
GET  /api/voice/events      # SSE 或 WebSocket，事件类型统一 voice.*
```

公共事件：

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

未知 `voice.*` 事件由前端忽略；禁止裸 `done/error/delta`。

### 对应测试

- 未授权时 `/api/voice/start` 返回拒绝，采音/Jev/动作调用数均为 0。
- 未唤醒时 `voice.final` 不暴露环境语音正文，Jev/云上传/动作均为 0。
- 路由和事件不改变聊天 `_sessions` 或 `_cache_stats`。
- `SYSTEM_PROMPT` SHA-256 保持现有冻结值。
- 蜂群 API 和事件信封全部回归通过。

### 影响面

- 聊天：不影响。
- 蜂群：不影响。
- 权限：读取现有 `voice_control`，不扩大数据 scope。
- 缓存：不使用聊天缓存账本。

---

## 2. `permissions.py`：无需结构改动，仅复用现有契约

### 当前事实

当前共享基线已经包含：

```text
CAPABILITY_SCOPES = ["voice_control"]
```

并满足：

- 默认关闭；
- `all=true` 不开启；
- 可独立显式授权；
- 旧 consent 自动迁移为关闭。

### 建议

M0–M5 不改权限模型。集成前端只需调用现有 `/api/permissions/scope` 显式切换
`voice_control`。

### 对应测试

继续保留 `tests/test_permissions.py` 全部用例，并补 UI/API 测试：不开启 capability 时麦克风、
Jev、上传和动作均为 0。

---

## 3. `static/index.html` / `static/app.js` / `static/styles.css`：只添加入口链接

### 为什么需要

语音界面独立位于 `static/voice/index.html`，不能接管聊天输入与 `sendTurn()`。

### 建议的最小改动

- 主页面只增加一个“言出法随”入口，打开 `/voice/` 独立窗口或页面。
- 权限管理页增加 `voice_control` 独立开关，默认关闭。
- 不改 `#send.onclick`、`sendTurn()`、聊天 session 和缓存 UI。
- 语音样式全部放在 `static/voice/**`，共享 CSS 只在确有必要时增加入口按钮最小样式。

### 输入输出接口

独立页面只消费 `/api/voice/*` 与 `voice.*` 事件。

### 对应测试

- 聊天 Enter、发送按钮、会话切换和缓存面板行为不变。
- 未授权时独立页面不能启动麦克风。
- 关闭语音窗口能停止采音并释放 runtime。

### 影响面

- 聊天：仅新增入口，无行为接管。
- 蜂群：无影响。
- 权限：新增现有 capability 的 UI 开关。
- 缓存：无影响。

---

## 4. `requirements.txt`：发布阶段合并 `requirements-voice.txt`

### 为什么需要

M0 独立依赖在 `requirements-voice.txt`。最终安装包需合并：

```text
numpy
PyAudio (Windows)
pywin32 (Windows，当前共享 requirements 已有)
sherpa-onnx
webrtcvad
```

### 建议的最小改动

由集成窗口在 M5 将已验证版本并入共享 requirements，避免功能分支同时修改同一文件。

### 对应测试

- 全新 `.venv` 安装主依赖 + voice 依赖成功。
- `python -m voice --help` 成功。
- PyAudio 能枚举输入设备；无设备时返回明确错误。

### 影响面

仅安装体积和构建依赖；不改变聊天、蜂群、权限或缓存。

---

## 5. `muliao.spec`：M5 打包语音模块与模型运行库

### 为什么需要

最终 EXE 需要：

- `voice/**` Python 模块；
- `sherpa_onnx`、ONNX Runtime DLL；
- PyAudio/PortAudio；
- `static/voice/**`；
- 可选本地 TTS 模型；
- SenseVoice 模型采用外部 ASCII 路径或首次下载，不应硬塞进源码路径。

### 建议的最小改动

- 按 `voice-models.json` 收集运行时库和静态资源。
- 模型落到 ASCII-only 路径，例如 `D:\ProgramData\Muliao\models\sensevoice` 或安装目录英文子目录。
- 不把 API Key 写入 spec 或 EXE。

### 对应测试

- 安装态能加载 SenseVoice。
- `voice_control=false` 时不启动麦克风。
- 模型缺失时给出明确路径与修复提示，不崩溃。
- 仓库与构建产物扫描无 API Key。

### 影响面

只影响打包体积与语音运行库；不改变聊天/蜂群 schema。

---

## 6. `installer.iss` / `deploy.py`：M5 安装态与冒烟测试

### 为什么需要

最终发布需安装语音静态资源/模型清单，并在隔离 runtime 下验证权限门禁和独立语音页面。

### 建议的最小改动

- 安装 `static/voice/**` 与 `voice-models.json`。
- 保持 D 盘安装规则和单一最新版安装包。
- 冒烟测试新增：
  - `/voice/` 静态资源可读；
  - 默认 `voice_control=false`；
  - 未授权语音测试产生 0 次 Jev、0 次云上传、0 次动作；
  - 假 ASR/Jev/动作 M0 链路通过；
  - 真麦克风/真记事本继续作为单独硬件验收，不在自动部署中执行。

### 对应测试

- 现有部署冒烟全部不回归。
- 安装后语音模块导入成功。
- 卸载旧版、安装 D 盘、快捷方式和哈希流程保持不变。

### 影响面

发布和安装流程；不改变聊天、蜂群或缓存运行语义。

---

## 7. `使用说明.md`：集成后更新

### 建议的最小改动

在 M2/M5 集成后增加：

- “言出法随”独立入口；
- `voice_control` 显式授权；
- 固定唤醒词“幕僚幕僚”；
- 未唤醒不上传；
- dry-run / `--act` 开发验收方式；
- 模型路径和缺失处理；
- 当前阶段能力边界与危险动作确认。

### 影响面

仅文档。

---

## 8. 当前 M0 API/schema 变化

M0 新增的内部契约：

```text
AudioSegment
Transcript
RouteDecision
ActionResult
VoiceResult
```

事件全部使用 `voice.*`，M0 实际发出：

```text
voice.state
voice.final          # 仅唤醒后，且只包含剥离唤醒词后的 command
voice.decision
voice.confirmation
voice.action
voice.tts
voice.metric         # wake_miss 不含原始转写
voice.error
```

M0 Jev 白名单只允许：

```text
kind=open_app
target=notepad
confidence>=0.5
addressed>=0.5
complete>=0.6
destructive<=0.5
```

其他一律拒绝，不执行动作。
