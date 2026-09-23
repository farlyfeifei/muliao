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

---

# M5 收尾批次交接（Jev 缓存 / ASR 降级 / 分段计时 / VITS）（HEAD d16dfda，2026-09-24 追加）

> 本节记录 M5 收尾批次（Jev 响应 TTL 缓存、MiMo 云 ASR 降级接线、ASR/Jev/执行/TTS
> 分段计时指标、离线 VITS 说话人与 MiMo→VITS→SAPI 降级链，及一批对抗审查修复）的
> 成果与遗留项。依据：`13-言出法随语音控制方案.md` §4.5/§7.4、
> `18-幕僚voice-worktree状态审计与集成实施计划.md` §5.4。
> 全部改动只在 voice 独占范围（`voice/**`、`tests/test_voice_*.py`、`voice-models.json`），
> **未改任何共享/受保护/蜂群文件**。

## Base / Head commit

```text
Base commit:  8394a0d  (M3：FAST/DESKTOP-GOAL orchestrator 接入生产 runtime)
Head commit:  d16dfda  (fix：收紧 MiMo ASR 云备援门控——审查发现)
```

更早的 `6e00f6d`（M5 加固：echo guard / 撤权 / teardown）是本批的血缘锚点，已在上一节
（M3A 交接）记为 Base，不在本批提交范围内。

本批 7 个提交（`8394a0d..d16dfda`）：

```text
0849099 feat: MiMo 云 ASR 降级 + 熔断器（FallbackRecognizer，本地优先，云仅备援）
339fad2 feat: Jev 响应 TTL 缓存（FAST router 与 GOAL chooser 共用一个传输包裹）
db6ba32 feat: 把 MiMo 云 ASR 降级接进 runtime 识别器（默认关，仅本地缺失才上云）
355aa76 feat: ASR/Jev/exec/TTS 分段计时指标（沿用 voice.metric，不改事件契约）
fc44fc8 feat: 声明 VITS aishell3 模型为可选降级层（manifest，缺失降级不 fail closed）
61445df feat: 离线 VITS 说话人 + MiMo→VITS→SAPI TTS 降级链接线
d16dfda fix: 收紧 MiMo ASR 云备援门控（HIGH 误判上云 + MEDIUM 取消污染熔断器）
```

## Changed files（全部 voice 独占）

```text
voice/jev_cache.py            # 新增：JevResponseCache + cache_key（有界 TTL，线程安全，只缓存成功响应）
voice/asr_fallback.py         # 新增：FallbackRecognizer + FallbackStats（本地优先 / 云备援 / 熔断器）
                              #     fix d16dfda：_is_missing_model_error 收窄为 FileNotFoundError/ImportError；
                              #     VoiceCancelled 在云往返中直接重抛，不计入熔断器失败
voice/tts_vits.py             # 新增：VitsSpeaker（sherpa-onnx VITS aishell3，懒加载、可注入、可取消）
voice/config.py               # 改：新增 jev_cache_enabled/jev_cache_seconds、mimo_asr_model/mimo_asr_enabled、
                              #     vits_dir/vits_tts_enabled；负 jev_cache_seconds 夹到 0（禁用，不崩）
voice/runtime.py              # 改：_build_recognizer 接 FallbackRecognizer；_build_goal_engine 接 jev_cache.wrap；
                              #     TTS 降级链 MiMo→(VITS→SAPI) 嵌套 FallbackSpeaker；
                              #     VoiceRuntimeResources 增 jev_cache/recognizer 字段，close 时清缓存并关云 client
voice/jev_router.py           # 改：_JevRouterBase 接可选 cache，命中即返回不发网络
voice/engine.py               # 改：新增 _timed()，对 asr/jev/exec/tts 四段各发一条 voice.metric 计时
voice-models.json             # 改：声明 vits_aishell3 条目（model.onnx/tokens.txt/lexicon.txt/dict，整条 optional）
tests/test_voice_jev_cache.py        # 新增：缓存单元 + FAST-router 集成 + config 夹取（22）
tests/test_voice_asr_fallback.py     # 新增：降级/熔断/取消/close + 误判上云回归 + 取消不污染熔断（19）
tests/test_voice_engine_metrics.py   # 新增：分段计时与隐私 payload（9）
tests/test_voice_tts_vits.py         # 新增：VITS 懒加载/分块/取消/缺资产/降级到 SAPI（20）
tests/test_voice_models.py           # 改：vits_aishell3 条目 schema 校验（+5，无模型文件也能过）
tests/test_voice_runtime.py          # 改：识别器四路径、VITS 链 wiring、资源 close 顺序、_get_recognizer 委托（+11）
```

**受保护/共享文件改动：0**（`server.py`、`permissions.py`、`runtime_paths.py`、`static/*`、
`machine_tools.py`、`collectors.py`、`requirements.txt`、`muliao.spec`、`installer.iss`、
`deploy.py`、`使用说明.md` 均未触碰）；**蜂群/capsule 文件改动：0**。

## 对外契约要点（供集成方）

### A. JevResponseCache（Jev 响应 TTL 缓存）

- `wrap(ask, model)` 包裹任意 `(state, questions) -> answers` 传输，返回同签名 callable；
  FAST router、桌面 GOAL chooser、WEB-GOAL chooser 都复用同一实现，不改各自问答逻辑。
- key = `sha256(model + 规范化 state JSON + 规范化 questions JSON)`；key 里**不含 API key**，
  日志也不打印 key 原文。
- **只缓存成功**的 `Mapping` answers；网络/HTTP 异常原样抛出、**不进缓存**（瞬态失败不会被当成答案）。
- TTL 默认 300s；`ttl_seconds == 0` 表示**禁用缓存**（每次必发网络）。有界（`max_entries`，
  插入序淘汰），线程安全，过期项惰性清理。
- **一个 runtime 级实例**，由 FAST router 与被包裹的 goal ask 共用；`VoiceRuntimeResources.close()`
  会 `clear()` 它，所以跨 runtime 生命周期复用绝不会读到陈旧答案。

### B. MiMo 云 ASR 降级

- 默认**关闭**（`mimo_asr_enabled` 默认 `False`）：把音频送出设备是隐私敏感动作，属显式 opt-in。
- 仅当**显式开启且有 MiMo key** 时，`_build_recognizer()` 才把本地 `SenseVoiceRecognizer`
  包进 `FallbackRecognizer`；否则返回纯本地识别器（或 ASR 关闭时的 transcript-only stub）。
- 只有本地模型**确实缺失/不可加载**（`FileNotFoundError` / `ImportError`，即资产文件不存在或
  sherpa_onnx 未安装）才上云；**真实解码错误绝不上云**（`OSError`/`RuntimeError` 等原样抛出，
  绝不把已过唤醒门控的音频泄漏给云端）。审查修复 `d16dfda` 已把此前过宽的判定收窄。
- 云端失败**重抛本地原始错误**（绝不给 Jev 空文本/垃圾文本）；连续失败触发**熔断器**，
  到重置窗口前半开探测，成功即闭合。无 key 时云备援直接禁用，零网络。
- 云往返途中用户 **barge-in**（`VoiceCancelled`）**不计入**熔断器失败、也不误开熔断；直接重抛，
  取消路径仍权威（审查修复 `d16dfda`，MEDIUM）。
- `FallbackRecognizer._get_recognizer()` **委托本地懒加载**，所以预热与模型校验
  （`voice/service.py`、`voice/models.py`）仍强制走**本地 SenseVoice**，本地资产缺失照旧令预热失败；
  云 client 永不预加载。识别器记在 `VoiceRuntimeResources` 上，云 httpx client 随 runtime 一起 close。

### C. 分段计时指标

- `VoiceEngine._timed(operation, name, work)` 对四段各发一条 `voice.metric`：`asr_ms`（仅音频路径）、
  `jev_ms`、`exec_ms`、`tts_ms`；值是**毫秒浮点标量**（`round(ms, 3)`）。
- payload **不含**文本/音频/候选词/URL；只有时长。异常**原样抛出且不记采样**，所以既有错误路径仍权威，
  不会记下幻影延迟。
- 计时发生在 `_allowed_call` 的**内层**，所以权限门等待**不计入**模型/执行延迟。
- **沿用既有 `voice.metric` 的 `{name, value}` 约定，未改 `voice.*` 事件契约**；`_emit` 已盖
  `operation_id` 并丢弃陈旧/终态 operation。未运行的阶段不发采样（被拒路由只记 `jev_ms`，无 `exec_ms`/`tts_ms`）。

### D. 新增配置项（全部安全默认值，只读环境变量或仓库外 JSON）

```text
jev_cache_enabled    默认开（True）         MULIAO_JEV_CACHE_ENABLED
jev_cache_seconds    默认 300（负值夹到 0）  MULIAO_JEV_CACHE_SECONDS
mimo_asr_model       默认 mimo-v2.5-asr     MULIAO_MIMO_ASR_MODEL
mimo_asr_enabled     默认关（False）         MULIAO_MIMO_ASR_ENABLED
vits_dir             默认 C:/ProgramData/Muliao/models/vits-aishell3   MULIAO_VOICE_VITS_AISHELL3_DIR
vits_tts_enabled     默认关（False）         MULIAO_VOICE_VITS_TTS_ENABLED
```

`VoiceSettings` 保留 M0 前四个必填字段与其后位置参数顺序，新增项全部追加并带默认值；
API key 仍只从环境变量或仓库外配置读取，仓库不含凭据。`vits_dir` 复用 manifest 的
`MULIAO_VOICE_VITS_AISHELL3_DIR` 约定，与 `voice-models.json` 的 `vits_aishell3` 条目一致。

### E. 离线 VITS 说话人与 TTS 降级链

- `voice/tts_vits.py` 的 `VitsSpeaker` 是 sherpa-onnx VITS（aishell3）离线神经 TTS，公共契约对齐
  `SapiSpeaker`（`speak/stop/close`）并暴露 `cancel()`，可直接充当 `FallbackSpeaker` 的 cloud 层。
- runtime 用**嵌套两层 FallbackSpeaker** 组成 **MiMo → (VITS → SAPI)**：`vits_tts_enabled` 默认关，
  关闭时仍是原 MiMo→SAPI 或纯 SAPI；开启时本地层变为 VITS→SAPI。
- **懒加载 + 可注入**：构造不碰文件系统、不导入 sherpa_onnx、不开声卡；默认 `tts_factory`/
  `player_factory` 只在真机执行，测试注入 fake，因此本机无模型也能完整单测。
- `_get_tts()` 先检查四个资产文件（`model.onnx`/`tokens.txt`/`lexicon.txt`/`dict`），缺失**在导入
  sherpa_onnx 之前**抛 `FileNotFoundError`，由内层 FallbackSpeaker **降级到 SAPI**，绝不 fail closed。
- **并发/急停**：transition lock 只保护 ownership 交接与 player begin()，慢速 native 加载与
  `generate()` 在锁**外**执行，所以 `cancel()`/`stop()` 能在合成途中拿到锁、递增 generation；被取代的
  operation 在 `generate()` 返回瞬间复查并丢弃结果，**音频绝不越过急停存活**。播放经
  `CancellableAudioPlayer` 按有界块流式送出 int16-LE PCM，块间复查取消；speaker 自带并 close 自己的
  player，经链的 close() 传播可达。

## Shared-file requests（M5 收尾批次）

**这批没有要求改任何共享文件。** §1–§7 与 M3A 节的既有请求不变；本批纯 voice 独占
（`voice/**`、`tests/test_voice_*.py`、`voice-models.json`），可安全合入。

### VITS 说话人已落地（提交 61445df / fc44fc8）

- `voice/tts_vits.py` 的 `VitsSpeaker` **已实现并接线**：runtime 的 TTS 降级链现为
  **MiMo → VITS → SAPI**（详见上文契约 E）。`vits_tts_enabled` 默认关，开启即生效。
- `voice-models.json` 新增 `vits_aishell3` 条目（`model.onnx`/`tokens.txt`/`lexicon.txt`/`dict`，
  整条与每个文件均标 `optional: true`）；旧 `vits_tts` 占位条目保留不动（被既有测试引用）。
- **集成方须知**：`voice/models.py` 的 required 校验列表（`validate_model_assets`）**刻意不含** vits，
  仍是 `("sensevoice", "streaming_zipformer")`。VITS 是**可选层**，缺失时优雅降级到 SAPI，**绝不
  fail closed**；`validate_model_assets` 对 `optional` 资产跳过缺失校验（计入 `skipped_optional_files`）。
  **请勿**把 `vits_aishell3` 提升为 required，否则缺模型会令真机启动失败。

## 自动测试

```text
python -m pytest tests/test_voice_*.py -q
546 passed, 67 subtests passed

python -m compileall -q voice
干净（rc=0）

git diff --check
干净（无输出）

SYSTEM_PROMPT SHA-256:
50e07b3b31dc5ca87420135484c4002cbce6878ece982defb026ce36e12e7ea7（与冻结值一致）

受保护/共享/蜂群文件改动：0
仓库守卫测试（tests/test_repository_guards.py）：2 passed（prompt 字节冻结 + 源码无内嵌凭据）
密钥扫描（tracked source）：无命中
```

各模块测试（实测，单文件可独立运行）：jev_cache 22（含 FAST-router 集成证明第二条相同命令零网络、
不同命令仍发、错误不缓存、TTL 过期强制重取、config 负 TTL 夹取）、asr_fallback 19（本地优先 /
缺失模型降级 / **真实解码错误不上云** / 无 key 禁用 / 云端失败抛本地错误 / 熔断开-半开-复位 /
**取消不污染熔断器** / close）、engine_metrics 9（全流分段、音频路径 asr_ms、transcript 路径无 asr_ms、
payload 形状与隐私、各早退路径）、tts_vits 20（int16 端点/夹取/扁平化、懒加载、分块、stale operation_id、
缺资产 FileNotFoundError、合成途中急停不出声、幂等 close、VITS→SAPI 降级）、models 22（含 vits_aishell3
schema 5 项）、runtime 33（含识别器四路径、VITS 链 wiring MiMo→VITS→SAPI、资源 close 顺序、
`_get_recognizer()` 委托）。

## Known limitations（M5 收尾）

1. **VITS 与云 ASR 默认关**：`vits_tts_enabled` 与 `mimo_asr_enabled` 均默认 `False`，属显式 opt-in；
   未开启时 TTS 走 MiMo→SAPI（或纯 SAPI），ASR 走纯本地 SenseVoice。VITS 真机模型（sherpa-onnx
   aishell3 导出）本机缺失，默认 `tts_factory` 未在真机验证，留待真机首块延迟与降级实测。
2. **真实云端未回归**：MiMo 云 ASR 降级、Jev 缓存、VITS 合成均用 fake/MockTransport 测通，真实
   MiMo ASR、真实 TypeSafe Jev、真实 sherpa-onnx VITS 的端到端降级/命中延迟/音质待真机实测。
3. **分段计时只发事件，不落盘**：`voice.metric` 计时样本只进事件流，无聚合/持久化面板，属可观测性
   的后续接线。
4. **桌面 GOAL 仍 dry-run**：`_build_goal_engine` 的桌面 GOAL 执行器在无真实 Windows UIA/OCR 执行器前
   仍走 dry-run（`act=True` 亦然），与本批缓存/计时/VITS 接线无关，属后续里程碑。
