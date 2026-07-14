# CapsLock

一个单机、只读、可恢复的工作区 Agent。它可分析本地文本和代码、检索带行号的证据并读取 Git 状态/diff；默认不联网、不运行任意命令、不写入用户文件。

开发计划、架构决策与实施记录见 [v1 开发文档](docs/v1-development.md)。

## 安装

需要 Python 3.11+。在项目目录执行：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
```

本项目使用 OpenAI 兼容 Chat Completions 接口（默认 DeepSeek）和 Rich CLI；测试还需要 `pytest`。若安装器不识别可选测试依赖，请执行：

```bash
pip install -e . pytest
```

程序启动时会自动读取项目根目录的 `.env.example`，再读取未提交的 `.env`；shell 中已设置的同名变量优先级最高。请将真实密钥放在 `.env`（已被 `.gitignore` 排除），不要写入 `.env.example`：

```bash
cp .env.example .env
# 编辑 .env，将 DEEPSEEK_API_KEY 替换为真实密钥
```

也可以通过 shell 临时覆盖：

```bash
export DEEPSEEK_MODEL='deepseek-v4-pro'
```

## 使用

```bash
capslock chat
```

输入 `/exit` 或 `/quit` 结束会话。会话和执行轨迹保存在工作区的 `.capslock/capslock.sqlite3`，可用 `capslock sessions` 查看，或 `capslock resume <session-id>` 恢复。

在自然语言中直接指定文件或目录（路径相对于工作区）：

```text
你> 阅读 examples/company-handbook.md，员工每周可以远程办公几天？
你> 在 examples 目录中查找报销需要审批的规则。
```

输出包含答案、证据位置和耗时。可使用 `capslock --debug chat` 查看工具执行摘要；`capslock doctor` 检查配置和工作区状态。

无充分本地证据时，Agent 会明确说明信息不足。一次性提问可使用：

```bash
capslock ask "公司总部在哪里？"
```

## 配置与边界

环境变量优先于项目根目录的 `capslock.toml`。支持 `CAPSLOCK_API_KEY`、`CAPSLOCK_BASE_URL`、`CAPSLOCK_MODEL`、`CAPSLOCK_TIMEOUT_SECONDS` 和 `CAPSLOCK_MAX_TURNS`；为兼容现有使用方式，`DEEPSEEK_*` 变量仍然有效。

需要项目级配置时，可复制 `capslock.toml.example` 为 `capslock.toml`；不要把 API key 写入该文件。

Agent 可调用 `list_files`、`read_file`、`search_files`、`git_status`、`git_diff` 和会话内 `task_list_update`。只允许 UTF-8 文本与常见源码格式，单文件最多 512KB、最多扫描 1000 个文件；所有路径必须位于工作区内。它不联网、不写文件，也不执行任意命令。

## 架构

- `runtime.py`：模型循环、证据校验与运行状态。
- `tools.py`：模型可调用的只读工作区工具与统一结果协议。
- `policy.py`：工作区路径、文件大小和只读安全边界。
- `evidence.py`：稳定、可定位的文件证据。
- `session.py`：SQLite 会话、运行、工具调用与引用存储。
- `cli.py`：命令行交互与可观测性展示。

## 验证

```bash
pytest
```

测试不需要 API 密钥：它们使用模拟客户端验证工具循环、证据 ID、会话恢复、越界路径拒绝和只读工具结果。
