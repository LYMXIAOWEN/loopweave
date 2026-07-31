# Windows 原生验收矩阵

本文是 Windows 支持的**人工验收清单**。项目纪律要求（见
`AGENTS.md` / `docs/DEVELOPMENT.md`）：只有以下矩阵在真实 Windows
10/11 环境全部通过后，才允许在 README 与文档中宣称"支持 Windows"。

自动化套件（`pytest`）已覆盖底层行为；本清单覆盖需要真实交互终端与
真实 Codex Desktop 的端到端项目。

## 0. 准备

```powershell
git clone https://github.com/Invonear/loopweave.git
cd loopweave
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[desktop,windows]"
```

- Windows 10 1809+ 或 Windows 11；
- 已登录并运行的 Codex Desktop；
- 准备运行的终端 Agent（`codex` / `claude` / `opencode` / `kimi` / 任意 CLI）在 `PATH` 中；
- 记录执行日期、OS 版本、Python 版本。

## 1. 托管终端启动

- [ ] 1. `loopweave run generic -- cmd /c echo CMD_OK` → 输出 `CMD_OK`，退出码 0
- [ ] 2. `loopweave run generic -- powershell -NoProfile -Command "Write-Output PS_OK"`
      → 输出 `PS_OK`，退出码 0
- [ ] 3. `loopweave run generic -- <python> -u <echo fixture>` → 输出可见，退出码 0

通过标准：右侧托管终端可见、输出完整、`loopweave runs --json` 中该 run 为
`stopped`，且 `socket_path` 形如 `\\.\pipe\loopweave-control-...`。

## 2. 交互输入

- [ ] 4. 在前台终端输入普通文本、中文文本，托管进程正确收到
- [ ] 5. 方向键（上/下历史）与功能键在托管终端中生效
- [ ] 6. 按 `Ctrl+C` 只传给托管 Agent（LoopWeave 自身不退出），再次输入仍正常

通过标准：中文无乱码；Ctrl+C 后 `loopweave status <id>` 仍能连通控制通道。

## 3. 窗口尺寸

- [ ] 7. 拖动/调整窗口大小，托管 TUI 布局正确跟随（`terminal-events.jsonl`
      出现 `terminal_resized` 记录）

## 4. 任务闭环

- [ ] 8. `loopweave run generic --task-file task.md <agent>`：任务包投递到托管 Agent
- [ ] 9. Agent 执行后 `loopweave submit --stage` 提交阶段结果
- [ ] 10. Codex Desktop visible-thread 审查：`--reviewer visible-thread` 下
       阶段提交在桌面任务中弹出审查卡
- [ ] 11. 审查意见自动回传同一托管会话
- [ ] 12. `changes_requested` 后 Agent 继续执行；`approved` 后完成

通过标准：完整闭环无人工搬运；review-inbox 出现卡片且引用正确任务包。

## 5. 生命周期与故障

- [ ] 13. `loopweave stop <id>`：托管进程退出，run 归档为 `stopped`
- [ ] 14. 强制结束托管进程（任务管理器）：`loopweave recover <id>` 能审计恢复
- [ ] 15. `loopweave archive` / `restore` / `gc`：控制端点与临时状态正确清理，
       运行目录不残留 token

通过标准：状态机无卡死；归档后 Named Pipe 端点不再存在；日志/任务包/草稿
不进入版本库。

## 结果记录

```text
日期：____
OS：Windows ____ (build ____)
Python：____
Codex Desktop：在线 / 离线
矩阵结果：__ / 15 通过
未通过项与现象：____
```

全部通过后：在 README 的"当前边界"中移除"Windows 需要验收"措辞，并在
CHANGELOG/发布说明中声明 Windows 支持。

## 自动化验收结果（2026-08-01，Windows 11，Python 3.12，真实 ConPTY）

下列条目已在本机真实 Windows 环境自动验证（真实 CLI + 真实 ConPTY +
真实 Named Pipe 控制通道 + 真实 registry）：

- [x] 1. `loopweave run` 启动 cmd / PowerShell / python：输出正确、退出码 0、
       `runs --json` 显示 `stopped` 且 `socket_path` 为 `\\.\pipe\loopweave-control-...`
- [x] 2. 中文输入经控制通道送达子进程，无乱码
- [x] 3. 方向键转义 `\x1b[A` 被 ConPTY 正确翻译为 Up 键码（`0xe0`+`0x48`）
- [x] 4. `resize` 记录 `terminal_resized` 事件
- [x] 5. `--task-file` 投递：任务包写入 run 目录并送达托管 Agent
- [x] 6. `submit --stage` 排队可见审查卡（引用精确任务包，集成测试覆盖）
- [x] 7. `loopweave stop` 终止运行中 run：2.7s 完成、Agent 进程确认退出、
       前台 run 释放
- [x] 8. 强杀 Agent 进程后 `status` reconcile 正常、`recover` 审计命令可用
- [x] 9. `archive` / `restore` / `gc`（dry-run → apply）正常；运行目录无 token 残留
- [ ] 10. 真实交互终端中按 Ctrl+C 的手感与方向键手感（人工）
- [ ] 11. Codex Desktop 桌面端真实弹出审查卡并完成人工闭环（人工）
