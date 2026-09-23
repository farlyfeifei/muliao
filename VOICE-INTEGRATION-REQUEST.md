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

---

# M0 交接报告

## Base commit

```text
8ed88c9b5ce8c5923859243977317d352c5893a3
```

## Head commit

M0 代码提交完成时：

```text
9b8a1c49c6c25acf28caafc679cfd54bcf240ec6
```

本交接报告会作为后续纯文档提交追加；合入时以 `feature/voice-command` 最新 HEAD 为准。

## Changed files

```text
VOICE-INTEGRATION-REQUEST.md
requirements-voice.txt
tests/test_voice_adapters.py
tests/test_voice_m0.py
voice-models.json
voice/__init__.py
voice/__main__.py
voice/actions.py
voice/asr_local.py
voice/capture.py
voice/cli.py
voice/config.py
voice/contracts.py
voice/engine.py
voice/events.py
voice/jev_router.py
voice/permission_gate.py
voice/runtime.py
voice/tts_local.py
voice/wake.py
```

未修改任何共享文件、聊天文件或蜂群文件。

## Shared-file requests

详见本文件 §1–§7。M0 独立运行不需要共享改动；后续由集成窗口处理：

- `server.py` 注册 `/api/voice/*` 与生命周期；
- `static/index.html` / `static/app.js` 仅添加独立 `/voice/` 入口和 capability 开关；
- `requirements.txt` 合并 `requirements-voice.txt`；
- `muliao.spec` / `installer.iss` / `deploy.py` 纳入模块、静态页、运行库和安装态冒烟；
- `使用说明.md` 更新正式使用方式。

## API/schema changes

M0 新增的内部契约和 `voice.*` 事件见 §8。

独立 CLI：

```bash
python -m voice --text "幕僚幕僚，打开记事本" --no-tts
python -m voice --audio-file command.wav --no-tts
python -m voice --microphone --no-tts
```

默认 dry-run；只有 `--act` 才真正打开记事本。所有模式先检查隔离数据目录中的
`voice_control`。

## Tests and results

### 自动测试

```text
python -m pytest -q
71 passed

python -B -m unittest discover -s tests -p "test_*.py" -q
Ran 71 tests - OK
```

覆盖：

- 未授权时不打开麦克风，ASR/Jev/动作/TTS 调用数均为 0；
- 未唤醒时 Jev、云上传和动作均为 0，事件流不暴露环境语音原文；
- 识别后撤权会阻止 Jev；Jev 后撤权会阻止动作；
- 固定唤醒词剥离、停顿标点容错和单次/句中误唤醒拒绝；
- Jev MockTransport 响应解析、缺 Key 零网络、非记事本/危险请求拒绝；
- Windows 记事本严格白名单；
- VAD 起止、尾静音与无语音超时；
- SenseVoice PCM 适配、SAPI 适配和 `voice.*` JSON 事件序列；
- 全量蜂群、权限、安全和 Prompt 哈希测试无回归。

### 冻结与安全门禁

```text
SYSTEM_PROMPT SHA-256:
50e07b3b31dc5ca87420135484c4002cbce6878ece982defb026ce36e12e7ea7

Voice secret scan: no offenders
File ownership violations: none
```

### 真实 SenseVoice 验收

Windows SAPI 合成“幕僚幕僚，打开记事本”后，SenseVoice 实测稳定转写：

```text
木聊木聊打开记事本。
```

冷启动约 2.1–2.4 秒，暖态约 152–180ms。`voice/wake.py` 只增加有限、显式的同音别名
白名单；仍要求句首重复两次，未使用宽泛模糊匹配。剥离结果为：

```text
打开记事本。
```

### 真实 Jev dry-run 验收

本机系统代理会导致 TypeSafe TLS `UNEXPECTED_EOF`；直连且保持 TLS 校验成功。因此独立语音
Jev client 使用 `trust_env=False`，不使用 `verify=False`。

隔离授权下真实结果：

```text
command=打开记事本
kind=open_app
target=notepad
confidence=1.0
destructive=false
action=dry-run: would launch notepad.exe
```

真实 Jev 延迟约 561–690ms；测试结束后隔离 `voice_control` 已撤销。

### 真正记事本动作验收

在显式 `--act` 下：

- 运行前无 `notepad.exe`；
- 新建 PID 34276；
- 仅终止本次新进程；
- 其他进程未触碰；
- 隔离权限测试后立即撤销。

### 真实麦克风验收

在隔离授权、3 秒等待窗口内打开真实麦克风成功。未说唤醒词时事件只有：

```text
voice.state listening
voice.state recognizing
voice.metric wake_miss
voice.state sleeping
```

没有 `voice.final` 原文、没有 Jev 调用、没有云上传、没有动作；测试后权限已撤销。

## Known limitations

1. M0 只允许 `open_app:notepad`；其他 FAST/GOAL 动作尚未实现。
2. 尚无 `/api/voice/*` 和 `/voice/` 页面；当前通过独立 CLI 验收，接线请求已记录。
3. 唤醒词 0–800ms 的真实时间戳级容错尚未实现；M0 文本门控容忍 ASR 输出中的空白/标点，真麦克风停顿专项留给 M1 流式状态机。
4. SenseVoice 冷启动约 2.1–2.4 秒；正式产品需启动预热，暖态约 152–180ms。
5. 真实用户口音、距离、噪声和误唤醒率尚需扩充样本；当前只将实测到的有限同音变体加入白名单。
6. M0 本地播报只接 Windows SAPI；MiMo/VITS、打断、回环防护属于 M1/M5。
7. 当前 CLI 的 `--microphone` 一次只处理一个 utterance；8 秒会话窗口属于 M1。
8. API Key 仅从环境变量或仓库外配置读取；仓库不包含凭据。
9. `voice_control` 默认关闭；集成 UI 尚未提供 capability 开关。
10. M0–M5 完成前未实施 M6 微信回复建议。

---

# M3A WEB-GOAL 契约层交接（2026-09-23 追加）

> 本节记录 WEB-GOAL 浏览器第三通道 M3A 阶段的成果与共享文件请求。
> 依据：`16-幕僚浏览器WEB-GOAL升级方案.md`、`18-幕僚voice-worktree状态审计与集成实施计划.md`。
> 全部改动只在 voice 独占范围（`voice/**`、`tests/test_voice_*.py`），**未改任何共享/蜂群文件**。

## Base / Head commit

```text
Base commit:  6e00f6d  (M5 加固收尾)
Head commit:  63288f1  (WEB-GOAL dry-run 闭环)
```

本批 10 个提交：

```text
69acf74 feat: WEB-GOAL 冻结数据契约 (web_contracts)
be3e0d5 feat: 把受限上下文 state 送进 Jev 路由 (M4 接线)
7ef04d2 fix: "停止播报"命令真正打断上一 operation 的音频
b44209a feat: WEB-GOAL observation 构建器 + 节点身份 (web_observation)
778ec89 feat: WEB-GOAL policy 引擎 + 防注入 (web_policy)
1bf82b2 feat: WEB-GOAL 一次性确认令牌 (web_confirm)
69ef61c feat: WEB-GOAL 独立完成验证 (web_verifier)
4352349 feat: WEB-GOAL 第二次 Jev 双头 chooser (web_goal_router)
72f0544 fix: target_id observation-local、node 句柄 document-stable
63288f1 feat: WEB-GOAL dry-run 闭环 + fake backend (web_backend/web_goal_loop)
```

## Changed files（全部 voice 独占）

```text
voice/web_contracts.py      # 冻结数据契约（Observation/ActionProposal/ConfirmationToken/…）
voice/web_observation.py    # 节点身份 + page_revision + target 编号 + 隐私/不支持面过滤
voice/web_policy.py         # ALLOW/REQUIRE_CONFIRMATION/NEEDS_INPUT/BLOCK + scheme 拒绝 + 防注入
voice/web_confirm.py        # 一次性确认令牌（绑定最终结构化计划，非用户原话）
voice/web_verifier.py       # 独立完成验证（三态）+ commit_state=unknown + answer_from_page 接地
voice/web_goal_router.py    # 第二次 Jev：operation + *_target 双头 Choice + validate_choice
voice/web_backend.py        # BrowserBackend Protocol + FakeDomBackend（M3A 内存 DOM）
voice/web_goal_loop.py      # observe→route→policy→confirm→act→re-observe→verify 闭环
voice/jev_router.py         # route(command, state) 转发受限上下文；needs_screen 提为稳定字段
voice/engine.py             # "停止播报"无条件停止；TTS 失败仍 executed 不重试
tests/test_voice_web_contracts.py / _observation / _policy / _confirm / _verifier / _goal_router / _goal_loop.py
tests/test_voice_router.py  # 补 state 转发与 needs_screen 覆盖
tests/test_voice_m0.py      # 补 stop 命令无条件停止覆盖
```

## API/schema changes

- **无对外 HTTP/事件 schema 变更**：M3A 全部是 voice 内部模块，未接 `service.py` 生产 runtime，未新增 `/api/voice/*` 端点，未新增 `voice.*` 事件。
- WEB-GOAL 内部契约（`voice/web_contracts.py`）已冻结：`Observation`（observation_id/page_revision/session_id/tab_id/origin/targets/omitted_target_count/supported_actions）、`ActionProposal`（三元组绑定、无自由文本字段）、`ConfirmationToken`、`VerificationResult`（三态）、`WebErrorCode`（含 TARGET_STALE/UNSUPPORTED_SURFACE/COMMIT_UNKNOWN/…）。
- 计划中的对外变更（留待 M3B/M3C 接线时提交）：`voice.*` payload 增 `channel=fast|browser|desktop`；可能增 `voice.verification` 事件。**均未实施。**

## 自动测试

```text
python -m pytest -q
456 passed, 63 subtests passed

python -m unittest discover -s tests -p 'test_voice_*.py'
Ran 391 tests - OK

SYSTEM_PROMPT SHA-256: 50e07b3b31dc5ca87420135484c4002cbce6878ece982defb026ce36e12e7ea7（与 origin/main 逐字节一致）
受保护/蜂群文件改动：0
秘密扫描（含 tp-/sk-/apikey_/ghp_/github_pat_）：无命中
git diff --check：通过
compileall：通过
```

M3A 各模块测试：web_contracts 30、web_observation 19+（重编号/节点稳定）、web_policy 22（含防注入）、web_confirm 17、web_verifier 23、web_goal_router 25、web_goal_loop 13。

## 真机验收

**M3A 不需要真机**：全程 dry-run + FakeDomBackend，零真实 Chrome/CDP/网络/麦克风。真实浏览器属于 M3B（专用 Chrome 只读 Alpha），尚未实施。

## Shared-file requests（M3A 新增，交集成窗口）

在原有 §1–§7 请求基础上，WEB-GOAL 追加以下共享文件请求。**voice 分支不直接改这些文件。**

### A. `permissions.py`：WEB-GOAL 的权限语义需裁定（关键）

- 补充方案与 doc 16 §7.1 要求：语音发起 WEB-GOAL 同时需要 `voice_control=true` **且** `browser=true`。
- **现状冲突**：`browser` 当前是 `DATA_SCOPES` 之一，语义是"读取 Chrome/Edge 的部分历史标题、URL、时间"（只读采集）。WEB-GOAL 需要的是"读取当前网页的实时可见文本与控件摘要 + 驱动页面动作"，这与"读历史"不是同一件事。
- **请集成窗口裁定**（三选一）：
  1. 复用现有 `browser` DATA_SCOPE 同时门控实时 DOM 读取（简单，但把"读历史"和"读实时页面+动作"混为一谈，语义偏宽）；
  2. 新增 `browser_control` capability（与 `voice_control` 并列，默认关，`all=true` 不开启），专门门控 WEB-GOAL 的实时 DOM 读取与动作——**voice 分支倾向此项**，但这是共享权限契约变更，必须由集成窗口实施；
  3. M3B 首版先只允许 `voice_control + browser` 双开的**只读**观察，任何写动作再叠加确认门，capability 细分留到 M3C。
- 在裁定前，M3B/M3C 的真实浏览器接线**不启动**。M3A 纯 dry-run 不触碰权限，可安全合入。

### B. `requirements.txt` / `muliao.spec`：M3B 浏览器依赖

- M3B 若采用自研直连 CDP（voice 分支推荐方案），依赖极小（可能只需 websocket 客户端；`websockets` 或标准库）。
- 若采用 `browser-harness==0.1.13` 作外部工具：**不进 requirements、不封进 exe**（PyInstaller onefile 冻结会破坏其 `sys.executable -m browser_harness.daemon` 启动；依赖全 `==` 锁死易与 FastAPI/pydantic 冲突）。应作为外部已安装 CLI/MCP 调用。
- 请集成窗口在 M3B 定稿依赖策略后再改 `requirements.txt`/`muliao.spec`。

### C. `server.py`：M3B/M3C 事件通道

- WEB-GOAL 接线到生产 runtime 时（M3B），`voice.*` payload 会增 `channel` 字段、可能增 `voice.verification` 事件。届时按原 §1 请求由集成窗口挂载，仍不改 SYSTEM_PROMPT、不写聊天 `_sessions`/`_cache_stats`。

## Known limitations（M3A）

1. **纯契约 + fake backend**：M3A 不启动真实 Chrome，不连 CDP，不读真实 DOM。所有网页交互经 `FakeDomBackend` 内存模拟。
2. **未接生产 runtime**：`WebGoalLoop` 尚未被 `service.py`/`runtime.py` 构建；`build_runtime` 仍只 `mode="fast"`。WEB-GOAL 通道对用户不可达，属 M3B 接线工作。
3. **未实现的结构**：iframe/Shadow DOM/canvas/文件上传/下载/弹窗新标签/嵌套滚动/复杂键盘——`web_observation` 只把 iframe/shadow/canvas 报为 `UnsupportedSurface`，其余待 M3B 的 CDP backend 明确拒绝。
4. **无真实 Chrome 生命周期**：专用 profile、随机回环端口、孤儿进程清理属 M3B（`web_chrome.py`，尚未创建）。
5. **确认交互未接 UI**：`ConfirmationStore` + `confirm_provider` 回调已就绪，但独立页面 `/voice/` 的确认 UI、语音确认话术属 M3C。
6. **verifier 的 criteria 由调用方声明**：`web_verifier` 提供四类 check（origin/title/text/control_state/field_value），但"某任务的成功判据"需在 M3B/M3C 按场景配置，尚无自动生成。
7. **Jev 双头 chooser 未经真实 Jev 回归**：`web_goal_router` 的 operation/target fan-out 与 validate_choice 用 fake answers 测通，真实 TypeSafe Jev 的中文网页 state 判定准确率待 M3B 实测（呼应 doc 13 §6.4 中文阈值需真实样本回调）。
8. **主线接线仍是前置**：`orchestrator.py`/`goal.py`/`perception.py`/`context.py`（桌面 GOAL）目前仍是孤儿模块（见 doc 18 §2），未接生产 runtime；WEB-GOAL 与 DESKTOP-GOAL 的三通道分流（`kind=goal` → web/desktop）属后续接线批次。
