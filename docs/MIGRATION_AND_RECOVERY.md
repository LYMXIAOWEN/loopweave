# LoopWeave 迁移与恢复

本文说明 LoopWeave 自身的任务连续性、运行根目录迁移和回滚原则。公开发行版不
携带任何特定旧系统的一次性转换器；本机历史数据转换应在仓库外完成。

## 任务重连

最可靠的做法是在第一次启动时原子安装任务包：

```bash
loopweave run <agent> \
  --thread <thread-id> \
  --project <project-name> \
  --workspace <workspace-path> \
  --reviewer visible-thread \
  --task-file <task-file>
```

如果受管 Agent 已退出，需要新拉一个兼容终端继续原任务：

```bash
loopweave run <agent> \
  --continue-run <source-run-id> \
  --thread <thread-id> \
  --project <project-name> \
  --workspace <workspace-path> \
  --reviewer visible-thread
```

LoopWeave 会校验来源任务摘要，以及项目、工作区、Agent、模式、审查后端和可见
任务绑定；校验通过后先原子安装任务包，再把任务内容和提交键真正送入新终端。
来源不唯一或不兼容时会明确拒绝，不会猜测。投递中断可对同一目标 Run 重试，
LoopWeave 会从已审计的投递步骤继续。

对已经启动且身份验证通过的新 Run，也可以显式执行：

```bash
loopweave adopt-task \
  --run-id <target-run-id> \
  --from-run <source-run-id>
```

不要手工补写任务文件。正式恢复还会写入不可变摘要、来源引用和审计事件，这些
信息同时用于提交校验与 Run 治理保护。

## 启动投递恢复

LoopWeave 在派发启动任务前会读取受管终端的输出活动，优先等待终端产生输出并
进入可配置的静默期；没有可识别输出的通用 CLI 会在有界回退时间后继续。相关
参数位于本机配置的 `[delivery]`：

```toml
[delivery]
task_ready_quiet_ms = 500
task_ready_fallback_ms = 5000
task_ready_timeout_ms = 10000
```

如果旧版本已经把同一任务写入 PTY、但 Agent 启动界面清除了输入，可显式重投：

```bash
loopweave assign \
  --run-id <run-id> \
  --task-file <same-task-file> \
  --redeliver
```

重投必须与已绑定任务的 SHA-256 一致，并写入
`assignment_redelivery_attempted`、`terminal_readiness_observed` 和
`task_redelivered` 审计事件。

## 运行根目录迁移

迁移前：

1. 停止创建新 Run；
2. 核验没有存活的受管 Agent、待审查投递或有效 Bridge 租约；
3. 记录 CLI、Bridge、配置和运行根目录；
4. 使用 SQLite 在线备份保存注册表；
5. 为全部文件生成数量、大小与 SHA-256 清单；
6. 先执行只读 dry-run。

转换到一个全新临时根目录，不要覆盖原目录。历史进程的 PID、启动身份、控制
token、Socket 和 Bridge 绑定必须失效化；任务包、状态事件、审查证据与摘要应
保留。完成结构、计数、引用和校验和验证后，再原子切换正式根目录。

一次性转换器必须放在公开仓库之外，并做到：

- 默认只读 dry-run；
- 输出逐 Run 映射与冲突；
- 不把历史活跃标记伪装成当前可恢复会话；
- 隔离但不自动删除未登记目录；
- 生成可复核的转换与验证报告。

## Bridge 切换

同一个审查请求只能有一个可见 Bridge：

```bash
loopweave bridge install
loopweave bridge bind --thread <thread-id>
loopweave bridge doctor
```

安装或刷新插件后需要重启 Codex Desktop。重启后再次运行 `bridge doctor`，确认
插件源码、运行根目录和绑定任务都指向当前 LoopWeave，再进行真实 stage/final
验收。旧入口应先解绑，验收通过后才退役；不要让两个 Bridge 同时消费事件。

## 回滚

迁移窗口内保留：

- 原源码或安装来源；
- 原 SQLite 在线备份；
- 原 Run 目录；
- 原 CLI 与插件配置清单；
- 切换前配置文件；
- 转换报告与 SHA-256 清单。

发生以下情况应暂停或回滚：

- 活跃进程身份无法可靠判断；
- 审查卡重复投递或丢失；
- 正式任务包缺失；
- 同会话反馈失败；
- 转换计数或校验和不一致；
- 自动维护触碰受保护 Run；
- 两个 Bridge 同时处理同一事件。

回滚时先停止自动维护和新建 Run，再恢复完整的“注册表 + 文件目录 + 配置 +
Bridge/CLI 指向”。不要只替换数据库或只复制 `runs/`。确认新旧两边均可读后，
再恢复唯一入口。
