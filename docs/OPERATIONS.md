# LoopWeave Run 运维手册

本文面向在 macOS 与 Codex Desktop 上维护 LoopWeave 的操作者，说明运行目录、
保留策略、人工归档、自动维护和故障处置。日常任务与审查命令见
[命令参考](CLI_REFERENCE.md)。

## 数据边界

默认运行根目录是 `~/.codex/loopweave`：

| 目录 | 内容 | 是否可以直接删除 |
|---|---|---|
| `runs/` | 当前热 Run | 不可以 |
| `archives/` | 带清单和 SHA-256 的可恢复归档 | 不可以 |
| `ledger/` | 有界长期审计账本 | 不可以 |
| `trash/` | 已归档原目录的宽限期副本 | 只能由治理命令按策略处理 |
| `maintenance/` | GC 计划、结果、清单与维护日志 | 不应手工篡改 |
| `var/` | SQLite 注册表、锁、Socket 等控制数据 | 不可以 |

`LOOPWEAVE_HOME` 可以覆盖运行根目录。源码目录与运行目录是独立边界；更新或重新
检出源码不会迁移运行数据。

## 默认策略

配置文件默认位于 `~/.config/loopweave/config.toml`：

```toml
[retention]
archive_after_days = 7
orphan_after_days = 14
trash_after_days = 7
prune_after_days = 90
keep_recent_per_project = 3

[logging]
terminal_log_max_bytes = 16777216
terminal_log_backups = 2
raw_log_enabled = false
raw_log_max_bytes = 16777216
raw_log_backups = 1

[delivery]
task_ready_quiet_ms = 500
task_ready_fallback_ms = 5000
task_ready_timeout_ms = 10000

[maintenance]
hour = 4
minute = 15
```

普通结束状态满 7 天后才可能归档；`orphaned` 会再次核验 PID 与进程启动身份，
并等待 14 天。每个项目最近 3 个结束 Run 的完整证据受到保留。raw 日志默认关闭，
普通终端日志采用 16 MiB、两代备份的轮转。`delivery` 控制任务投递前等待终端
输出稳定的静默期、无输出时的有界回退和总超时，单位均为毫秒。

## 永久保护条件

以下任一条件成立时，自动治理不会处理该 Run：

- 进程仍存活，或 PID/启动时间无法可靠确认；
- 运行状态仍活跃；
- 有待审查卡、租约、未投递反馈或所有者待处理；
- 状态为 `needs_human`；
- 已 `pin`；
- 被任务连续性或其他 Run 的账本引用；
- 注册表、目录或控制通道互相冲突；
- 目录包含符号链接、特殊文件或其他不安全内容；
- 归档、锁或数据库处于恢复异常状态。

未登记目录只会报告，不会自动删除。

## 推荐日常流程

先看全貌：

```bash
loopweave runs --all
loopweave maintenance status
```

生成并阅读计划：

```bash
loopweave gc --dry-run
```

确认逐 Run 动作、保护原因和 `UNREGISTERED` 列表后，应用同一状态快照：

```bash
loopweave gc --apply
```

dry-run 和 apply 之间出现状态漂移时，应重新生成计划；不要编辑 JSON 计划。

## 单个 Run 操作

人工长期保留：

```bash
loopweave pin <run-id> --reason "保留发布证据"
```

解除显式保护：

```bash
loopweave unpin <run-id>
```

人工归档与恢复：

```bash
loopweave archive <run-id> --reason "项目阶段结束"
loopweave restore <run-id> --reason "复核历史证据"
```

归档事务会先生成压缩包、清单和 SHA-256，再重新打开校验，最后更新 SQLite 并把
原目录移入 `trash/`。恢复会重新验证归档，不会恢复已经失效的进程凭据。

## 自动维护

先完成人工 dry-run 和单次运行：

```bash
loopweave gc --dry-run
loopweave maintenance run
```

安装每日 LaunchAgent：

```bash
loopweave maintenance install
loopweave maintenance status
```

停止路径：

```bash
loopweave maintenance uninstall
```

卸载只停止调度并移除 plist，不删除任何 Run。LaunchAgent 每次只执行一个短命令，
使用非阻塞全局锁；如果调度触发时存在活跃 Run，则整次重型维护延后。

最近结果位于
`~/.codex/loopweave/maintenance/maintenance-result-latest.json`，标准输出与错误
日志也位于同一目录。不要为排查问题启动第二个并行 GC。

## 故障处置

### 计划漂移

看到 snapshot 或 drift 错误时，重新执行 dry-run。状态变化是保护信号，不应绕过。

### `recovery_required`

停止自动维护，不要删除热目录、归档或 trash 副本。保留
`maintenance/` 中的计划与恢复清单，核对 SQLite 存储记录、归档校验和与三个目录
的位置，再决定恢复哪一份真相。

### 归档无法验证

该 Run 不应进入正式 `archived` 状态。保留原热目录和临时清单，检查磁盘空间、
权限、符号链接与特殊文件。不要手工将损坏压缩包改名成正式归档。

### 活跃 Run 被报告为候选

不要 apply。先用 `loopweave status --json <run-id>` 核对 PID、进程启动身份、
Socket 和运行状态；身份无法确认时应保持 fail-closed，并报告问题。

## 备份

备份运行根目录前，应停止新建 Run，并使用 SQLite 在线备份能力复制注册表。至少
同时保存 `var/registry.sqlite`、`runs/`、`archives/`、`ledger/` 与
`maintenance/`。只复制目录而不保存注册表，不能形成完整可恢复备份。
