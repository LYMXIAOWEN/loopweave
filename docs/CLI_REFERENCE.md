# LoopWeave 命令参考

本文档对应当前源码中的 `loopweave` CLI，用于查询命令语法、适用角色、状态要求
和常见工作流。

> [!IMPORTANT]
> 当前公开版本面向 **Codex Desktop**。被托管的终端 Agent 可以来自任意厂商，
> 但 `visible-thread` 审查、Bridge 安装、审查任务唤醒和任务绑定依赖 Codex
> 桌面端。本文中的“线程”是兼容字段；用户界面中对应 Codex 的“任务”。

## 约定

- `<run-id>`：LoopWeave 运行 ID，例如 `run-xxxxxxxxxxxx`。
- `<thread-id>`：Codex Desktop 审查任务的 `thread_id`，可在该任务中通过
  `/status` 查看。
- `<agent>`：位于当前 `PATH` 中的可执行终端 Agent 名称。
- `<path>`：应替换成真实文件路径；文档示例不包含任何个人路径。
- `--json`：输出稳定的机器可读 JSON，适合脚本消费。
- 没有显式 `--run-id` 的选择型命令，只会在目标唯一时自动选择；存在歧义时应
  明确传入 ID。

## 命令总览

| 命令 | 主要使用者 | 用途 |
|---|---|---|
| `doctor` | 操作者 | 检查本地 Python、Codex/Claude CLI 和 Codex 会话目录 |
| `run` | 操作者 | 启动并托管一个终端 Agent |
| `runs` | 操作者 | 按存储范围列出 run、保护原因和占用 |
| `status` | 操作者 | 查看并协调一个 run 的当前状态 |
| `assign` | 操作者 | 向可继续执行的 run 派发任务包 |
| `adopt-task` | 操作者 | 在兼容且存活的新 run 中延续已有任务包 |
| `submit` | 托管 Agent | 提交阶段、最终或需要人工介入的结果 |
| `stop` | 操作者 | 停止托管 Agent |
| `recover` | 操作者 | 恢复被误判为孤立、且身份已重新验证的 run |
| `attach` | 高级操作者 | 把存活 run 迁移绑定到另一个 Codex 任务 |
| `archive` / `restore` | 运维者 | 创建经校验的归档，或原子恢复为热 Run |
| `pin` / `unpin` | 运维者 | 显式保护或解除保护一个 Run |
| `gc` | 运维者 | 生成或应用带状态快照的生命周期计划 |
| `maintenance ...` | 运维者 | 管理每日一次的短生命周期维护任务 |
| `review-next` | 审查者 | 读取下一张可见审查卡 |
| `review-submit` | 审查者 | 提交可见审查裁决 |
| `review-heartbeat` | Bridge/诊断 | 查看可见审查交接状态 |
| `bridge ...` | Codex Desktop 用户 | 安装、绑定、诊断或卸载可见审查桥 |
| `reviewer bind` | 高级操作者 | 为一个 run 重新绑定可见审查任务 |
| `deliver` | 内部/恢复 | 手动触发待处理审查意见的交付 |
| `request-review` | 高级/兼容 | 手动生成审查请求 |
| `finalize` | 高级/兼容 | 记录所有者最终裁决 |
| `hook` | 内部适配器 | 接收 Claude Stop Hook；不供人工日常调用 |

## 推荐工作流

### 一次性安装 Codex Desktop Bridge

```bash
python -m pip install -e '.[desktop]'
loopweave bridge install
loopweave bridge bind --thread <thread-id>
loopweave bridge doctor
```

### 启动时直接派发任务

```bash
loopweave run <agent> \
  --thread <thread-id> \
  --project <project-name> \
  --workspace <workspace-path> \
  --mode develop \
  --reviewer visible-thread \
  --task-file <task-file>
```

### 先启动、后派发

终端 A：

```bash
loopweave run <agent> \
  --thread <thread-id> \
  --project <project-name> \
  --workspace <workspace-path> \
  --reviewer visible-thread
```

终端 B：

```bash
loopweave runs
loopweave assign --run-id <run-id> --task-file <task-file>
```

### 阶段审查与同会话续跑

托管 Agent：

```bash
loopweave submit --stage --summary-file <stage-summary>
```

审查者：

```bash
loopweave review-next --run-id <run-id>
loopweave review-submit --run-id <run-id> --review-file <verdict-file>
```

如果裁决为 `changes_requested`，意见会回到同一个托管会话；Agent 修改完成后再次
提交阶段结果，或在全部工作结束后提交最终结果：

```bash
loopweave submit --final --summary-file <final-summary>
```

### 右侧终端重连后延续任务

旧受管进程已经退出、但需要在同一项目中继续原任务时，启动一个新的可见终端并
显式指定来源 Run：

```bash
loopweave run <agent> \
  --continue-run <source-run-id> \
  --thread <thread-id> \
  --project <project-name> \
  --workspace <workspace-path> \
  --mode develop \
  --reviewer visible-thread
```

LoopWeave 会先验证项目、工作区、Agent、模式、审查任务和来源摘要，再原子安装
任务包，并把任务内容与提交键送入新受管终端。投递中断后可安全重试，已成功的
投递步骤不会重复。存在多个可能来源时不会猜测。

## 操作者命令

### `loopweave doctor`

```text
loopweave doctor
```

检查当前环境能否找到 Python、Codex CLI、Claude CLI，以及 Codex 本地会话目录。
该检查仍包含旧版 Claude 可选适配器项目；Claude 缺失不会阻止其他通用 Agent
被 `run` 启动，但当前 `doctor` 会把缺失项显示出来并返回非零状态。

### `loopweave run`

```text
loopweave run [--thread THREAD] [--cwd CWD]
              [--project PROJECT] [--workspace WORKSPACE]
              [--mode {develop,design}]
              [--reviewer {ephemeral,visible-thread}]
              [--task-file TASK_FILE | --continue-run RUN_ID]
              [agent] [agent_args ...]
```

启动一个前台托管终端，并把 `LOOPWEAVE_RUN_ID` 注入子进程环境。

主要参数：

| 参数 | 说明 |
|---|---|
| `agent` | 可执行文件名；除 `claude` 的可选增强适配器外，其他名称统一走通用适配器 |
| `agent_args` | 原样传给 Agent 的参数 |
| `--thread` | 明确指定 Codex Desktop 审查任务；使用可见审查时强烈建议提供 |
| `--project` | LoopWeave 项目标识 |
| `--workspace` | Agent 实际工作的目录；必须与 `--project` 一起使用 |
| `--cwd` | 未使用项目绑定时的工作目录，也用于 Codex 任务发现兼容路径 |
| `--mode develop` | 允许阶段审查、修改和继续执行 |
| `--mode design` | 设计型任务模式 |
| `--reviewer visible-thread` | 使用 Codex Desktop 中可见的审查任务 |
| `--reviewer ephemeral` | 使用兼容的临时审查后端 |
| `--task-file` | 启动时原子安装并派发任务包，推荐使用 |
| `--continue-run` | 从一个兼容的历史 Run 延续已验证任务；不能与 `--task-file` 同用 |

示例：

```bash
loopweave run codex --project demo --workspace <workspace-path>
loopweave run opencode --project demo --workspace <workspace-path>
loopweave run -- /absolute/path/to/custom-agent --flag value
```

注意：

- Agent 必须能被当前环境找到；LoopWeave 不会自动激活 Conda、虚拟环境或搜索
  其他安装目录。
- `run` 占用当前终端并进入前台。请在另一个终端运行 `runs`、`status` 和审查命令。
- 未提供 `--task-file` 时，run 可以启动，但提交审查前必须先执行 `assign`。
- `visible-thread` 要求 Bridge 已安装并绑定到同一个明确的 Codex 任务。

### `loopweave runs`

```text
loopweave runs [--json] [--all | --archived]
```

默认只显示 `hot` Run；`--archived` 显示 `archived` 与 `ledger_only`，
`--all` 显示全部存储状态。输出包含运行状态、存储状态、保护原因和当前占用。
该命令只协调可继续执行 run 的存活状态，不会把历史记录擅自恢复为活跃会话。

### `loopweave status`

```text
loopweave status [--json] [run_id]
```

查看一个 run。未提供 ID 时选择最近的非终态 run；存在歧义或需要稳定脚本行为时，
应始终显式提供 ID。

对热 run 执行 `status` 时，也会完成受约束的绑定协调和误判孤立恢复检查。已经
归档或进入其他非热存储状态的 run 仍可查询登记状态，但不会读取已经移走的热目录，
也不会尝试恢复会话绑定。

```bash
loopweave status <run-id>
loopweave status --json <run-id>
```

`run_id` 是位置参数，不使用 `--run-id`。

### `loopweave assign`

```text
loopweave assign (--run-id RUN_ID | --latest) --task-file TASK_FILE [--redeliver]
```

向 `running` 或 `worker_continuing` 状态的 run 派发不可变任务包。

```bash
loopweave assign --run-id <run-id> --task-file <task-file>
loopweave assign --latest --task-file <task-file>
```

只有唯一可派发 run 时才能安全使用 `--latest`。相同任务内容重复派发是幂等操作；
不同内容不能覆盖已经绑定的任务来源。如果控制通道曾接受过启动期输入、但 Agent
界面尚未就绪而丢失输入，可在确认目标终端可见后用 `--redeliver` 明确重投同一
摘要；LoopWeave 会拒绝重投不同任务，并记录重投审计事件。

### `loopweave adopt-task`

```text
loopweave adopt-task --run-id RUN_ID --from-run SOURCE_RUN_ID
```

把来源 Run 的不可变任务包安装到一个已经存活且认证通过的新 Run，并将任务实际
送入该受管终端。来源与目标的项目、工作区、Agent、模式和可见审查绑定必须兼容；
目标已有不同任务时拒绝覆盖。正常重连更推荐在启动时使用
`run --continue-run`，本命令用于显式恢复。

### `loopweave stop`

```text
loopweave stop [run_id]
```

停止指定托管 Agent。省略 ID 时选择最近的活动 run。

```bash
loopweave stop <run-id>
```

### `loopweave recover`

```text
loopweave recover [--json] run_id
```

只恢复能够同时通过进程身份与认证控制通道校验、且原状态允许恢复的误判孤立 run。
它不是任意修改状态的后门。

```bash
loopweave recover <run-id>
```

### `loopweave attach`

```text
loopweave attach [--thread THREAD] run_id
```

把存活 run 的 Codex 任务绑定迁移到另一个明确任务，并增加绑定代次。用于 Codex
任务迁移或恢复，不建议在正常执行中频繁调用。

```bash
loopweave attach <run-id> --thread <thread-id>
```

## Run 生命周期治理命令

运行状态描述任务进度；存储状态描述数据位于 `hot`、`archived`、`trash`、
`ledger_only` 或恢复异常位置。两类状态不会互相代替。

### `loopweave archive`

```text
loopweave archive RUN_ID [--reason TEXT]
```

对一个已结束、进程身份确认死亡且未受保护的 Run 执行两阶段归档。归档包含内容
清单和 SHA-256；重新打开验证成功后才更新注册表，并把原目录移入宽限期
`trash/`。活跃、待审查、人工待处理、被引用、被 pin 或身份不确定的 Run 会被拒绝。

### `loopweave restore`

```text
loopweave restore RUN_ID [--reason TEXT]
```

重新验证归档校验和与内容清单，再原子恢复到热 Run 目录。不会把失效的 PID、
控制 token 或 Socket 重新变成可用会话。

### `loopweave pin` / `loopweave unpin`

```text
loopweave pin RUN_ID [--reason TEXT]
loopweave unpin RUN_ID
```

`pin` 是显式保留决策，会写入注册表和审计事件，并阻止自动归档、日志裁剪和
trash 清理。解除保护后仍要满足全部生命周期规则，才会成为 GC 候选。

### `loopweave gc`

```text
loopweave gc --dry-run [--json]
loopweave gc --apply [--json] [--plan PLAN_PATH]
```

`--dry-run` 生成逐 Run 决策、保护原因、未登记目录和状态快照，并将计划持久化到
运行根目录的 `maintenance/`。`--apply` 默认应用最新计划；也可用 `--plan`
指定精确计划。应用前会复核快照，任何状态、目录或引用漂移都会令该项跳过或失败，
不会按新的现场状态擅自扩大范围。

### `loopweave maintenance`

```text
loopweave maintenance install
loopweave maintenance status
loopweave maintenance run
loopweave maintenance uninstall
```

- `install`：安装并加载 macOS LaunchAgent，默认每日 04:15 运行一次；
- `status`：显示 plist、调度时间、加载状态、手动与停止命令及最近结果；
- `run`：立即完成一次 dry-run 计划和受快照约束的应用；
- `uninstall`：停止并移除 LaunchAgent，不删除 Run、归档或审计账本。

调度器执行的是短生命周期命令，不是常驻 daemon。调度触发时只要存在活跃 Run，
整次重型维护就会延后。

## 托管 Agent 提交命令

### `loopweave submit`

```text
loopweave submit (--stage | --final | --needs-human)
                 [--run-id RUN_ID]
                 [--summary-file SUMMARY_FILE]
                 [--message-file MESSAGE_FILE]
                 [--evidence-file EVIDENCE_FILE]
```

`run` 启动的子进程会获得 `LOOPWEAVE_RUN_ID`，因此正常情况下可以省略
`--run-id`。

阶段提交：

```bash
loopweave submit --stage \
  --summary-file <stage-summary> \
  --evidence-file <evidence-json>
```

最终提交：

```bash
loopweave submit --final \
  --summary-file <final-summary> \
  --evidence-file <evidence-json>
```

请求人工介入：

```bash
loopweave submit --needs-human --message-file <message-file>
```

规则：

- `--stage` 和 `--final` 必须提供非空 `--summary-file`。
- `--needs-human` 必须提供非空 `--message-file`。
- `--evidence-file` 是可选 JSON；字段和大小受提交协议限制，越界会拒绝而不是静默
  截断。
- 没有 `--run-id` 且环境中没有 `LOOPWEAVE_RUN_ID` 时，命令返回错误。
- 使用可见审查的 run 必须已经拥有正式任务包。

## 可见审查命令

### `loopweave review-next`

```text
loopweave review-next [--run-id RUN_ID]
```

读取下一张待处理可见审查卡，并输出审查依据、工作区和提交命令。没有显式 ID 时，
只有恰好一张待处理卡才能自动选择。

### `loopweave review-submit`

```text
loopweave review-submit [--run-id RUN_ID] --review-file REVIEW_FILE
```

提交审查裁决，并按裁决继续交付或完成流程。

裁决文件示例：

```markdown
---
verdict: changes_requested
summary: 请修复审查中发现的问题。
---

这里写给托管 Agent 的具体修改要求。
```

常用 `verdict`：

- `approved`：批准当前提交；
- `changes_requested`：把修改要求送回同一个托管会话；
- `needs_human`：需要人工决定；
- `failed`：提交无法接受或流程失败。

审查者应先检查真实任务包、工作区改动与测试证据，再提交唯一的正式裁决。

### `loopweave review-heartbeat`

```text
loopweave review-heartbeat [--json]
```

检查是否存在已经交接给可见审查任务、但仍待处理的审查请求。主要供 Bridge 和
诊断流程使用。

### `loopweave reviewer bind`

```text
loopweave reviewer bind --run-id RUN_ID [--thread THREAD]
```

为一个现有 run 绑定或重新绑定 Codex Desktop 可见审查任务。推荐显式传入
`--thread`；省略时才使用当前 Codex 环境或会话发现逻辑。

## Codex Desktop Bridge 命令

### `loopweave bridge install`

```text
loopweave bridge install [--dry-run] [--json]
```

安装 Codex Desktop 可见审查桥。`--dry-run` 只显示计划，不写入安装状态。

### `loopweave bridge bind`

```text
loopweave bridge bind --thread THREAD [--run-id RUN_ID]
```

把 Bridge 显式绑定到一个 Codex Desktop 任务。`--run-id` 用于同时限定一个 run。

### `loopweave bridge status`

```text
loopweave bridge status [--json]
```

显示当前绑定、代次、关联 run 和待处理审查信息。

### `loopweave bridge doctor`

```text
loopweave bridge doctor [--json]
```

验证绑定、Codex Desktop IPC 和可选空闲观察器状态。失败时不会静默改绑其他任务。

### `loopweave bridge unbind`

```text
loopweave bridge unbind
```

移除当前可见审查任务绑定，不卸载 Bridge。

### `loopweave bridge uninstall`

```text
loopweave bridge uninstall [--dry-run] [--json]
```

卸载 Bridge；非 `--dry-run` 模式也会解除当前绑定。

## 高级、兼容与内部命令

以下命令不是新用户的正常主路径。自动化脚本使用前应先理解状态机和兼容边界。

### `loopweave deliver`

```text
loopweave deliver --run-id RUN_ID
```

手动解析并交付一个已有审查结果。正常可见审查流程会自动完成这一步。

### `loopweave request-review`

```text
loopweave request-review --run-id RUN_ID
                            [--summary SUMMARY]
                            [--stage | --final]
```

手动创建审查请求，主要用于兼容或恢复路径。`--stage` 只适用于 `develop` 模式。

### `loopweave finalize`

```text
loopweave finalize --run-id RUN_ID
                   (--approve | --changes-requested)
                   [--message MESSAGE | --message-file MESSAGE_FILE]
```

记录所有者的全局最终裁决。普通可见终审通过后通常会自动进入相应完成路径。

### `loopweave hook`

```text
loopweave hook claude-stop --run-id RUN_ID
```

内部 Claude Stop Hook 入口，从标准输入接收 Hook JSON。它只是把 Claude 的专属
完成信号翻译成统一提交协议，不是 Agent 准入条件，也不应由用户手动调用。

## 环境变量

| 变量 | 用途 |
|---|---|
| `LOOPWEAVE_HOME` | 指定运行状态根目录；用于隔离不同项目或测试环境 |
| `LOOPWEAVE_RUN_ID` | 由 `run` 自动注入托管 Agent；供 `submit` 自动定位当前 run |
| `LOOPWEAVE_CODEX_BIN` | 显式指定 Codex CLI 可执行文件 |
| `LOOPWEAVE_CONFIG` | 指定本机保留与维护策略配置文件 |
| `LOOPWEAVE_RAW_TERMINAL_LOG` | 临时显式启用或关闭受大小限制的 raw 终端日志 |

公开环境变量统一使用 `LOOPWEAVE_*`，命令统一使用 `loopweave`，Python API
统一使用 `loopweave` 包。仓库不提供其他历史名称或兼容别名。

## 常见问题

### Agent 名称是否有限制？

没有厂商白名单。除可选的 Claude 增强适配器外，其他名称都会按通用可执行命令
启动。失败通常表示命令不在当前 `PATH`、不是持续交互式 TTY，或 Agent 没有执行
提交命令的权限。

### 为什么 `status --run-id` 报错？

`status` 的 run ID 是位置参数：

```bash
loopweave status --json <run-id>
```

### 为什么提交提示缺少任务包？

可见审查要求正式任务来源。启动时提供 `--task-file`，或在提交前执行：

```bash
loopweave assign --run-id <run-id> --task-file <task-file>
```

如果这是退出后新拉起的兼容终端，可以直接使用：

```bash
loopweave run <agent> --continue-run <source-run-id> ...
```

不要手工复制 `assigned-task-latest.md`；LoopWeave 需要同时写入摘要、连续性记录和
审计事件。

### 为什么 `gc --apply` 拒绝最新计划？

从 dry-run 到 apply 之间，Run 状态、目录内容、引用、pin 或进程身份发生了变化。
重新运行 `loopweave gc --dry-run` 并检查新决策，不要编辑计划绕过快照。

### 为什么 Codex 要求批准 `loopweave submit`？

这是 Codex CLI 的本地命令沙箱授权边界。批准命令后 LoopWeave 可以继续校验托管
进程并提交结果；它不同于审查文字停留在输入框而没有发送。

### Windows 能运行吗？

可以。Windows 10 1809+ / Windows 11 支持原生托管终端：ConPTY 终端宿主
（`pywinpty`）与 per-run Named Pipe 控制通道，Codex Desktop 可见审查走
`\\.\pipe\codex-ipc`。安装时需启用 `windows` extra：

```powershell
python -m pip install -e '.[desktop,windows]'
```

未安装 `windows` extra 时，启动托管终端会明确失败并提示安装命令，不会回退到
未经验证的伪支持。PowerShell 可直接使用 `bin\loopweave.ps1` 或安装后的
`loopweave` 命令。
