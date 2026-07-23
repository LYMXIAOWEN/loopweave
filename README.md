# LoopWeave

> 面向 Codex 桌面端的开放式终端 Agent 协作与审查循环。

> [!IMPORTANT]
> **当前版本用于 Codex Desktop。** LoopWeave 把 Codex 桌面端中明确选定的
> 审查任务，与一个由用户亲自启动、持续可见的终端 Agent 绑定起来。终端 Agent
> 不限厂商，可以是 `codex`、`claude`、`opencode`、`kimi` 或任意其他可执行
> CLI；但当前的可见审查桥、任务唤醒和右侧终端协作流程仍依赖 **Codex Desktop**。
> 它目前不是面向所有 IDE 或所有桌面宿主的通用插件。

## LoopWeave 是什么

LoopWeave 让一个终端 Agent 在同一托管会话中完成以下闭环：

```text
接收任务包
    → 执行任务
    → 提交阶段结果
    → Codex 桌面端中的审查者进行审查
    → 审查意见自动回到同一个终端会话
    → Agent 继续修改或提交最终结果
```

LoopWeave 负责显式绑定终端、保存运行状态、传递任务与审查意见、校验提交证据，
并保留可审计的状态变化。它不会根据进程名猜测目标终端，也不会设置 Agent
厂商白名单。

## 当前状态

LoopWeave 仍处于 **pre-alpha** 阶段，适合开发者试用和共同完善，尚不建议用于
无人值守的关键生产流程。

目前已经具备：

- `loopweave run <agent>` 可以启动任意位于 `PATH` 中的终端 CLI；未知名称自动
  走通用适配器，不会因为厂商名称被拒绝。
- `--task-file <path>` 可以在启动时安装当前 run 专属的任务包；也可以先启动，
  再通过 `loopweave assign` 派发。
- Agent 可以统一使用 `loopweave submit --stage`、`--final` 或
  `--needs-human` 提交结果，不依赖 Claude 专属 Hook。
- Codex Desktop 可见审查链路已经通过真实终端验证：阶段提交、审查、
  `changes_requested` 自动发送、同 PID 继续执行、最终提交能够形成完整闭环。
- 运行存活检查、误判孤立恢复和任务来源都有受约束的审计记录。
- 核心模块在缺少 POSIX 模块的平台上可以安全导入，终端宿主已经抽象为独立平台
  边界。

当前边界：

- 可见审查桥目前只支持 Codex Desktop。
- 终端宿主仍使用 POSIX PTY 和 Unix Socket；真实终端验收目前在 macOS 完成。
- **Windows ConPTY 尚未实现。** Windows 会明确拒绝启动托管终端，不会假装支持。
- Codex CLI 的沙箱可能在执行 `loopweave submit` 时要求一次本地命令授权；这与
  审查意见是否自动发送是两个不同的边界。

架构决策可参阅：

- [ADR 0001：厂商中立的终端宿主与提交协议](docs/decisions/0001-vendor-neutral-terminal-host-and-submission-protocol.md)
- [ADR 0002：存活审计、恢复与任务来源](docs/decisions/0002-audited-liveness-recovery-and-run-scoped-task-provenance.md)

## 快速开始

### 1. 准备环境

当前推荐环境：

- macOS；
- Python 3.10 或更高版本；
- 已安装并登录 Codex Desktop；
- 已安装 Codex CLI；
- 准备运行的终端 Agent 已加入 `PATH`。

从源码安装：

```bash
git clone https://github.com/Invonear/loopweave.git
cd loopweave
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[desktop]'
```

### 2. 安装并绑定 Codex Desktop 可见审查桥

先在准备作为审查者的 Codex 桌面端任务中运行 `/status`，取得它的 `thread_id`，
然后执行：

```bash
loopweave bridge install
loopweave bridge bind --thread <codex-reviewer-thread-id>
loopweave bridge doctor
```

LoopWeave 只会绑定你明确指定的审查任务，不会按进程名猜测目标。

### 3. 启动一个可见终端 Agent

```bash
loopweave run codex \
  --thread <codex-reviewer-thread-id> \
  --project <project-name> \
  --workspace /absolute/path/to/workspace \
  --mode develop \
  --reviewer visible-thread \
  --task-file /absolute/path/to/task.md
```

把 `codex` 换成其他已安装的交互式 CLI 即可：

```bash
loopweave run opencode ...
loopweave run kimi ...
loopweave run -- /absolute/path/to/custom-agent --flag value
```

`run` 会进入前台托管会话。需要查看 run ID 或处理审查时，请使用另一个终端：

```bash
loopweave runs
loopweave status --json <run-id>
```

### 4. 提交与审查

托管 Agent 内提交阶段结果：

```bash
loopweave submit --stage \
  --summary-file stage.md \
  --evidence-file evidence.json
```

审查端读取并提交裁决：

```bash
loopweave review-next --run-id <run-id>
loopweave review-submit \
  --run-id <run-id> \
  --review-file verdict.md
```

Agent 完成全部任务后提交最终结果：

```bash
loopweave submit --final --summary-file final.md
```

全部命令、参数、状态要求和示例见
[《LoopWeave 命令参考》](docs/CLI_REFERENCE.md)。

## 设计原则

- **显式绑定：** 只控制用户明确选择的终端和 Codex 审查任务。
- **Agent 中立：** Agent 名称不是准入条件，任意可执行 CLI 都可以进入通用协议。
- **同会话续跑：** 审查意见回到原托管 PID，不静默创建隐藏替代 Agent。
- **人工审查：** 保留明确的审查、批准和停止点。
- **可审计：** 任务来源、状态迁移、孤立与恢复都有受约束的记录。
- **默认保护隐私：** 运行日志、任务包、Socket、数据库和项目工作区默认不进入源码。

## 仓库结构

```text
src/        核心 Python 实现
tests/      单元测试、契约测试与集成测试
plugins/    可选的 Codex Desktop 可见审查桥
bin/        源码检出环境下的启动脚本
docs/       架构、命令参考、开发说明与决策记录
```

运行数据默认位于被忽略的 `projects/`、`runs/` 和 `var/` 目录。可以通过
`LOOPWEAVE_HOME` 把运行状态放到其他位置。

## 开发

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
PYTHONDONTWRITEBYTECODE=1 python -B -m pytest -q
ruff check src tests
python -m compileall -q src tests
```

源码检出后也可以直接运行：

```bash
./bin/loopweave --help
```

公开命令统一使用 `loopweave`，Python API 统一使用 `loopweave` 包；仓库不提供
其他历史名称或兼容别名。

进一步阅读：

- [命令参考](docs/CLI_REFERENCE.md)
- [架构说明](docs/ARCHITECTURE.md)
- [开发说明](docs/DEVELOPMENT.md)
- [真实终端测试记录](docs/INTERACTIVE_TEST_FINDINGS.md)
- [贡献指南](CONTRIBUTING.md)

## 安全与隐私

请勿提交终端转录、线程或会话标识、本地数据库、凭据、私人任务包和真实用户绝对
路径。安全问题请参阅 [SECURITY.md](SECURITY.md)。

## 许可证

LoopWeave 使用 [Apache License 2.0](LICENSE) 开源。
