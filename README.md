# 幕僚 Muliáo

幕僚是一个本地优先的中文桌面 Agent：保留文字对话、Jev 决策门控、权限感知的只读电脑工具、通知与缓存账本，并正在并行建设 Ghost 蜂群和“言出法随”语音控制。

## 安全配置

仓库不保存 API 密钥。开发时使用环境变量，或在仓库外创建：

```text
%APPDATA%\Muliao\config.json
```

示例见 [config.example.json](config.example.json)。环境变量优先于本机配置：

```text
MULIAO_LLM_BASE
MULIAO_LLM_KEY
MULIAO_LLM_MODEL
MULIAO_JEV_URL
MULIAO_JEV_MODELS_URL
MULIAO_JEV_KEY
MULIAO_JEV_MODEL
```

开发实例可用 `MULIAO_DATA_DIR` 隔离授权、日志和数据库；正式安装版不设置时仍使用 `%APPDATA%\Muliao`。

## 本地运行

```bash
python -m pip install -r requirements.txt
```

```bash
python server.py
```

默认地址为 `http://127.0.0.1:8930`。

## 测试

```bash
python -B -m unittest discover -s tests -p "test_*.py" -q
```

## 并行开发

两个功能线的文件所有权、运行时隔离和合并流程见 [docs/parallel-development.md](docs/parallel-development.md)。

- `feature/ghost-swarm`：Ghost 蜂群。
- `feature/voice-command`：言出法随。
- `integration/two-lane`：串行集成与最终发布。

构建和安装只能在集成分支进行。正式重建后按项目规则卸载旧版并安装到 `D:\Program Files\幕僚Muliáo`。
