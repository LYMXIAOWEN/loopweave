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
  再通过 `loopweave assign` 派发。右侧终端重连时，可以用
  `--continue-run <run-id>` 从经过兼容性校验的历史 run 延续同一任务。首次
  派发和续跑都会等待终端输出进入稳定期后再发送，避免 TUI 初始化吞掉早到输入。
- Agent 可以统一使用 `loopweave submit --stage`、`--final` 或
  `--needs-human` 提交结果，不依赖 Claude 专属 Hook。
- Codex Desktop 可见审查链路已经通过真实终端验证：阶段提交、审查、
  `changes_requested` 自动发送、同 PID 继续执行、最终提交能够形成完整闭环。
- 运行存活检查、误判孤立恢复和任务来源都有受约束的审计记录。
- `runs`、`archive`、`restore`、`pin`、`gc` 与每日一次的短生命周期维护任务，
  共同管理热 Run、可恢复归档、审计账本和延迟删除区；活跃、待审查、人工待处理
  和身份不确定的 Run 默认受保护。
- 核心模块在缺少 POSIX 模块的平台上可以安全导入，终端宿主已经抽象为独立平台
  边界。

当前边界：

- 可见审查桥目前只支持 Codex Desktop。
- POSIX 终端宿主使用 PTY 和 Unix Socket；Windows 终端宿主使用 ConPTY
  （`pywinpty`/`winpty`）和 per-run Named Pipe 控制通道。两套后端共享同一个
  控制协议、状态机和审查流程。
- Windows 支持需要在安装时启用 `windows` extra（见下方快速开始）；未安装时
  仍会明确失败并给出可操作的安装提示，不会假装支持。
- 真实终端验收：POSIX 在 macOS 完成；Windows ConPTY 通过原生 Windows 单元测试
  与验收矩阵验证。
- Codex CLI 的沙箱可能在执行 `loopweave submit` 时要求一次本地命令授权；这与
  审查意见是否自动发送是两个不同的边界。

架构决策可参阅：

- [ADR 0001：厂商中立的终端宿主与提交协议](docs/decisions/0001-vendor-neutral-terminal-host-and-submission-protocol.md)
- [ADR 0002：存活审计、恢复与任务来源](docs/decisions/0002-audited-liveness-recovery-and-run-scoped-task-provenance.md)

## 快速开始

### 1. 准备环境

当前推荐环境：

- macOS 或 Windows 10 1809+ / Windows 11；
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

Windows 需要额外安装 ConPTY 后端：

```powershell
python -m pip install -e '.[desktop,windows]'
```

PowerShell 下也可以用仓库内的 `bin\loopweave.ps1` 启动（等价于 POSIX 的
`bin/loopweave`）。

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

如果任务包已经原子安装，但旧版本或特殊 Agent 的启动界面清除了输入，可以在
确认右侧终端已经就绪后重投同一份任务：

```bash
loopweave assign \
  --run-id <run-id> \
  --task-file /absolute/path/to/task.md \
  --redeliver
```

重投只接受与已绑定任务相同的 SHA-256，不会覆盖任务来源。

全部命令、参数、状态要求和示例见
[《LoopWeave 命令参考》](docs/CLI_REFERENCE.md)。

## Run 生命周期治理

LoopWeave 将工作流状态与数据存储状态分开管理。运行数据默认位于
`~/.codex/loopweave`：

```text
runs/          当前可直接使用的热 Run
archives/      带清单和 SHA-256 的可恢复归档
ledger/        有界长期审计账本
trash/         处于宽限期、尚未永久删除的原目录
maintenance/   GC 计划、最近结果和维护日志
var/           注册表、锁和短期控制文件
```

先查看逐 Run 决策，再应用同一份持久化计划：

```bash
loopweave runs --all
loopweave gc --dry-run
loopweave gc --apply
```

`gc --apply` 会拒绝已经发生状态漂移的计划。未登记目录、活进程、身份不确定、
待审查、`needs_human`、所有者待处理、被引用或被 `pin` 的 Run 均不会自动处理。
归档成功后原目录先进入 `trash/`，默认保留 7 天；归档可用
`loopweave restore <run-id>` 恢复。

保留策略由 `~/.config/loopweave/config.toml` 管理。完整操作手册见
[《Run 运维手册》](docs/OPERATIONS.md)，任务重连与数据迁移见
[《迁移与恢复》](docs/MIGRATION_AND_RECOVERY.md)。

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

运行数据不会写入源码检出目录，默认位于 `~/.codex/loopweave`。可以通过
`LOOPWEAVE_HOME` 指定其他运行根目录；本机保留策略默认从
`~/.config/loopweave/config.toml` 读取。

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
- [Run 运维手册](docs/OPERATIONS.md)
- [迁移与恢复](docs/MIGRATION_AND_RECOVERY.md)
- [架构说明](docs/ARCHITECTURE.md)
- [开发说明](docs/DEVELOPMENT.md)
- [真实终端测试记录](docs/INTERACTIVE_TEST_FINDINGS.md)
- [贡献指南](CONTRIBUTING.md)

## 安全与隐私

请勿提交终端转录、线程或会话标识、本地数据库、凭据、私人任务包和真实用户绝对
路径。安全问题请参阅 [SECURITY.md](SECURITY.md)。

## 许可证

LoopWeave 使用 [Apache License 2.0](LICENSE) 开源。
