# Windows Lean Loop 0.14.1

一个独立实现的 Windows 原生 Lean 工作流：

```text
Goal Formalizer（自然语言 -> 正式 Lean 声明）
  -> 本地 Mathlib 预检索
  -> Plan（结构化计划）
  -> Prove（修改候选文件）
  -> 本地 Mathlib 检索
  -> Lean 编译检查
  -> Review（评审与下一轮建议）
  -> 成功，或继续下一轮
```

项目只使用 Python 标准库和 Windows 自带的 `curl.exe`，不依赖 Archon，也不复制 Archon 的实现。

## 设计

- 核心状态使用 JSON，不通过 Markdown 正则决定工作流状态。
- Python 根据 Lean 退出码决定成功，模型不能自行宣布完成。
- 状态文件原子写入，终态不能回到 `running`。
- 每轮候选、检索证据、Lean 诊断和 Review 都永久保留。
- 失败时恢复原文件，但不会删除失败候选，便于分析和继续开发。
- Mathlib 检索直接读取本机精确版本，返回模块、文件、行号和源码片段。
- Planner、Prover、Reviewer 有不同提示协议和可独立配置的推理强度。

## 目录

```text
D:\lean_agent_cli       本工具
D:\my_math_project      Lean/Mathlib 项目
```

工作流状态写入目标项目：

```text
.lean-agent/workflows/<run-id>/
  run.json                    工作流清单和确定性状态
  events.jsonl                只追加事件日志
  original.lean               开始前原文件
  initial-check.json          初始 Lean 检查
  initial-retrieval.json      初始检索
  goal.json                   新文件的正式 Lean 目标契约
  formal-goal-check.json      目标声明解析检查
  planning-retrieval.json     Plan 前的扩展检索
  plan.json                   Planner 的结构化计划
  attempts/001/candidate.lean Prover 第一次候选
  attempts/001/check.json     候选的 Lean 结果
  attempts/001/retrieval.json 本地 Mathlib 证据
  reviews/001.json            Reviewer 结论
  checkpoints/001-step-id/    已通过 Lean 和 Review 的 Plan 步骤检查点
```

## 安装

需要 Windows 10/11、Python 3.11 或更高版本、Elan/Lean 4，以及一个可以由 Lake 编译的 Lean/Mathlib 项目。

```powershell
git clone https://github.com/zzw-zzw-zzw/windows-lean-loop.git D:\lean_agent_cli
cd D:\lean_agent_cli
python -m pip install -e .
python -m lean_loop doctor --project D:\my_math_project
```

用户需要配置自己的模型 API。API Key、任务数据库、Mathlib 索引和证明运行记录都保存在用户自己的 Lean 项目中，不包含在本仓库里。

## 环境配置

```powershell
cd D:\lean_agent_cli

$env:LEAN_AGENT_API_BASE = "https://你的中转站API前缀"
$env:LEAN_AGENT_API_KEY = "你的API Key"
$env:LEAN_AGENT_MODEL = "gpt-5.5"
$env:LEAN_AGENT_API_MODE = "responses"
$env:LEAN_AGENT_REASONING_EFFORT = "high"
$env:LEAN_AGENT_DISABLE_RESPONSE_STORAGE = "true"
$env:LEAN_AGENT_MAX_OUTPUT_TOKENS = "8192"
$env:LEAN_AGENT_EMPTY_RESPONSE_RETRIES = "1"
$env:LEAN_AGENT_API_TIMEOUT_RETRIES = "1"
$env:LEAN_AGENT_STREAM_RESPONSES = "true"
$env:LEAN_AGENT_LAKE = "C:\Users\xu\.elan\toolchains\leanprover--lean4---v4.29.0-rc8\bin\lake.exe"
```

这些环境变量现在都是可选回退配置。启动 Dashboard 后可在“配置”中保存 API Base、
默认模型、API 模式、推理强度、超时、重试次数、输出上限、Lake 路径和 API Key。
普通设置保存在项目的 `.lean-agent/config.json`；API Key 使用当前 Windows 用户的 DPAPI
加密并单独保存在 `.lean-agent/secrets.json`，网页不会读回 Key 明文。

API Key 也可以继续使用 `OPENAI_API_KEY`。Reviewer 默认使用主模型，也可以设置：

```powershell
$env:LEAN_AGENT_REVIEW_MODEL = "gpt-5.5"
```

Responses 模式默认使用 SSE 流式接收。Dashboard 只显示“正在推理、开始输出、累计输出
字符、完成”等元数据，不保存推理正文。API 超时独立于 Lean 候选尝试；默认降低一级推理
强度重试一次。只有 `reasoning`、没有最终文本时也会降低强度重试；日志不保存
`encrypted_content`。

模型的结构化输出具有容错层：Prover 直接返回 Lean 源码、Lean Markdown 代码块或常见的
未转义 `{"content": ...}` 时会自动恢复；Planner、Formalizer、Reviewer 或 Prover 的最终
JSON 无法解析时自动追加一次严格格式重试。仍无法恢复的 Prover 输出会归档到对应
`agent-calls/<call>/raw-output.txt`，记录为本轮失败并继续使用剩余候选预算，不再直接使
workflow 崩溃。Dashboard 的 Agent Calls 中可以查看该原始输出。

### 多 Provider 与 DeepSeek 官方 API

Dashboard 的“配置”支持多个独立 Provider 档案。`default` 保留现有中转站配置；选择
“新建 Provider”后可创建 `deepseek`：

```text
Provider ID: deepseek
Provider 类型: DeepSeek Official
API Base: https://api.deepseek.com
默认模型: deepseek-reasoner
API 模式: chat-completions
```

每个 Provider 的 API Key 分别使用 Windows DPAPI 加密，不会互相覆盖。DeepSeek 请求
使用其官方 Chat Completions 参数，不发送 OpenAI Responses 专用的 `store`、
`reasoning_effort` 或 `max_completion_tokens` 字段。

## 基础检查

```powershell
python -m lean_loop doctor --project D:\my_math_project
python -m lean_loop api-check --timeout 60 --reasoning-effort low
python -m lean_loop check --project D:\my_math_project --file MyMathProject.lean
```

## 本地 Mathlib 搜索

首次使用时在项目所在磁盘建立紧凑声明索引：

```powershell
python -m lean_loop mathlib-index build `
  --project D:\my_math_project

python -m lean_loop mathlib-index status `
  --project D:\my_math_project
```

索引保存在 `.lean-agent/indexes/mathlib.sqlite3`，只记录声明、命名语法、模块、
文件位置和最长 300 字符的声明行，不复制证明体。索引指纹包含 Lean 工具链、Mathlib
commit 和索引格式版本；不匹配时检索自动回退，重建使用 `--force`。

```powershell
python -m lean_loop mathlib-search `
  --project D:\my_math_project `
  --query pi_gt_three `
  --query lt_tan `
  --limit 5 `
  --suggest-imports
```

搜索不会联网。有效索引存在时使用 SQLite；否则才回退到 `rg` 或 Python 扫描。
精确 import 建议是有源码证据的候选，最终仍必须通过 Lean 检查。

检索结果按 Mathlib 指纹和查询参数缓存在
`.lean-agent/cache/retrieval.sqlite3`，缓存结果上限 64 MB、条目上限 5000。查看状态或清空：

```powershell
python -m lean_loop mathlib-index status --project D:\my_math_project
python -m lean_loop mathlib-index clear-cache --project D:\my_math_project
python -m lean_loop mathlib-index benchmark --project D:\my_math_project --query pi_gt_three
```

每次 workflow 都会从当前 Lean 源码、任务文字和上一轮诊断提取检索词，
因此修复错误或去掉 `sorry` 也会触发本地 Mathlib 检索，不依赖 Planner 是否填写
`search_terms`。索引同时确定性验证候选中的每个 `Mathlib.*` import；不存在的模块会在
启动 Lean 前被拒绝，并附带当前本地版本中的相近模块。Lean 检查完成后会立即使用本轮
最新诊断重新检索，再把证据交给 Reviewer，避免 Reviewer 使用检查前的旧证据。

Import 策略支持 `auto`、`proof-first`、`precise` 和 `broad`。新文件的 `auto` 默认采用
proof-first：先允许 `import Mathlib` 验证证明，成功后再尝试缩小 import；缩小失败时保留
已经通过 Lean 的宽 import 版本。已有声明的修复任务默认继续在检查前尝试精确 import。
每次候选的 `retrieval.json` 会记录 `import_optimization`、`probe_ok` 和返回码。

## 稳定 Agent 协议

Goal Formalizer、Planner、Prover、Reviewer、全局 Auditor 和自然语言 Explainer 通过版本化的
`lean-agent/v1` 协议调用。协议请求不包含 API Key，固定记录 role、run、phase、attempt、
step、模型、推理强度、输入提示和期望输出类型；响应固定记录状态、输出、错误和耗时。

```powershell
python -m lean_loop agent-protocol
```

每次调用归档到：

```text
.lean-agent/workflows/<run-id>/agent-calls/
  0001-planner-<id>/request.json
  0001-planner-<id>/response.json
```

`AgentBackend` 是可替换边界。当前 `DirectModelBackend` 继续使用现有 Responses API；后续
Subagent、本地进程 Agent、多模型 lane 或远程 worker 只需实现相同 Backend 协议，不需要
改写 workflow 状态机。Dashboard 的 `/api/capabilities` 返回协议版本、角色和特性列表。

## Plan -> Prove -> Review

```powershell
python -m lean_loop workflow run `
  --project D:\my_math_project `
  --file MyMathProject.lean `
  --task "修复所有 Lean 编译错误，不改变定理声明" `
  --model gpt-5.6-sol `
  --max-attempts 12 `
  --max-attempts-per-step 3 `
  --formalize-goal `
  --import-policy auto `
  --api-timeout 600 `
  --api-retries 1 `
  --plan-effort high `
  --prove-effort high `
  --review-effort medium
```

`--model` 是单次 workflow 覆盖项；留空时继续使用 `LEAN_AGENT_MODEL`。中转站模型名按
中转站实际提供的名称填写。失败时默认恢复原文件。只有明确传入 `--keep-failed` 才保留
最后候选作为工作文件。

在正式任务前可以单独确认中转站是否接受该模型名：

```powershell
python -m lean_loop api-check --project D:\my_math_project --model gpt-5.6-sol --timeout 60
```

Planner 必须把复杂任务拆成可独立编译的步骤。执行器逐步运行，每一步只有同时满足 Lean
检查成功和 Reviewer 确认 success criteria 才写入 checkpoint，再进入下一步。
`--max-attempts` 是整个 workflow 的候选总上限，`--max-attempts-per-step` 是任一 Plan
步骤的候选上限；Plan、API 超时重试、初始检查和最终审计不占候选次数。候选只在事务式
Lean 检查和本步 Review 都通过后才提交到目标文件并保存 checkpoint。已有 theorem/lemma
声明默认冻结；可用重复的 `--protect-declaration <name>` 进一步完整冻结指定声明。

当目标文件没有 theorem/lemma 等顶层声明时，默认先运行 Goal Formalizer，生成一个不含
证明体的正式 theorem 声明，并使用临时 `import Mathlib` 文件检查该声明能否在当前环境中
解析。通过后该声明成为确定性契约，后续候选改变或删除它都会被源代码审计拒绝。可用
`--no-formalize-goal` 关闭。Reviewer 的 `stop` 不再直接终止候选预算；没有独立验证的外部
阻塞条件时会被状态机改为 `retry`。

Goal Formalizer 会自动移除声明末尾多余的 `:=`。第一次临时 Lean 声明检查失败时，会把
精确诊断交回 Formalizer 修复一次。每个 Prover 尝试都会重新加入自然语言任务映射出的
精确本地检索词；错误 `Mathlib.*` import 只有在索引首选候选达到高置信度且明显领先其他
候选时才会被确定性替换。

所有步骤完成后还会执行一次完整 Lean 检查、无占位符/新增公理审计和全局 Reviewer 审计，
结果保存在 `final-audit.json`。全局审计拒绝时不会把 workflow 标成成功。

失败后可复用同一个 run 的 Plan、attempt 历史和 checkpoint 继续；提高的预算是新的总上限：

```powershell
python -m lean_loop workflow resume `
  --project D:\my_math_project `
  --run-id <run-id> `
  --max-attempts 20 `
  --max-attempts-per-step 5
```

Resume 会验证目标文件、原始文件和 checkpoint 的 SHA-256；检测到外部编辑时拒绝覆盖。

## 查看状态

```powershell
python -m lean_loop workflow list --project D:\my_math_project

python -m lean_loop workflow show `
  --project D:\my_math_project `
  --run-id <run-id>

python -m lean_loop workflow timings `
  --project D:\my_math_project `
  --run-id <run-id>
```

每个 workflow 的 `timings.json` 独立记录初始 Lean 检查、初始检索、Plan API、
每轮检索、Prove API、Lean 检查和 Review API。失败与取消也保留已完成统计。

旧的单 Agent `python -m lean_loop run ...` 仍保留用于兼容，但新功能应使用 `workflow run`。

## 持久化任务队列

队列使用 Python 标准库 SQLite，数据库保存在 Lean 项目的
`.lean-agent/queue.sqlite3`。API Key 不进入队列数据库。每个任务按以下状态机运行：

```text
queued -> planning -> proving -> lean_checking -> reviewing -> auditing
                                            |          |
                                            +----------+
                                                       -> succeeded
任意运行阶段 -> failed / cancelled
```

添加一个任务：

```powershell
python -m lean_loop queue add `
  --project D:\my_math_project `
  --file MyMathProject.lean `
  --task "修复所有 Lean 错误，不改变已有定理陈述" `
  --model gpt-5.6-sol `
  --max-attempts 3 `
  --api-timeout 600 `
  --api-retries 1 `
  --lean-timeout 120
```

命令会输出任务 ID。第二个任务可以等待第一个任务成功后再运行：

```powershell
python -m lean_loop queue add `
  --project D:\my_math_project `
  --file Next.lean `
  --task "完成 Next.lean 中的证明" `
  --depends-on <第一个任务ID>
```

在已通过环境变量或 Dashboard 项目配置提供 API 凭据后处理所有就绪任务：

```powershell
python -m lean_loop queue work --project D:\my_math_project
```

查看状态和完整事件记录：

```powershell
python -m lean_loop queue list --project D:\my_math_project
python -m lean_loop queue show --project D:\my_math_project --task-id <任务ID>
```

在另一个 PowerShell 窗口取消任务：

```powershell
python -m lean_loop queue cancel `
  --project D:\my_math_project `
  --task-id <任务ID>
```

工作器会轮询 SQLite 取消标记，并终止该任务当前的 `curl.exe`、`lake.exe`、
`lean.exe` 或 `rg.exe` 进程树。取消时默认恢复任务开始前的 Lean 文件。失败或取消的任务
可以重新入队：

```powershell
python -m lean_loop queue retry `
  --project D:\my_math_project `
  --task-id <任务ID>
```

`queue retry` 会在原 workflow 上真正恢复，不会重新生成 Plan；如果总预算或当前步骤预算
已经耗尽，显式重试会自动增加一段同等大小的预算。目标文件必须仍等于最后一个已验证
checkpoint，否则 Resume 会拒绝覆盖外部编辑。

如果工作器或电脑异常退出，下次 `queue work` 会把失去工作器的运行中任务标记为
`failed`，不会自动重复 API 调用；确认状态后再执行 `queue retry`。

## 单机多 Prover Worktree

Dashboard 添加任务时可启用“单机多 Prover worktree”，配置 2-4 条独立路线。每条路线
可选择不同 Provider、模型、Plan/Prove/Review 强度和附加提示词。当前选择策略固定为：

```text
first_verified_wins
```

每条 lane 都独立执行完整的 Plan、逐步 checkpoint、Lean 检查、声明/占位符审计和全局
最终 Reviewer。只有完整 workflow 成功后才获得 `score=1` 并进入主项目复检；第一个复检
成功的 lane 整体写回主文件，其他 lane 的 API/Lean 进程树立即取消。

项目即使没有 commit 或有未提交修改也可以使用。系统不会修改主仓库的 branch、index、
commit 或 stash，而是在项目同盘建立内部快照仓库：

```text
D:\.<project>-lean-agent-worktrees\<race-id>\
  _baseline\
  lane-1\
  lane-2\
```

lane 共享主项目的 `.lake`、Mathlib SQLite 索引和检索缓存，不复制完整依赖。所有 lane
失败或任务取消时保留 worktree 和各自 run/checkpoint；Dashboard“重试任务”会提高预算并
分别从各 lane 的最后有效 checkpoint 恢复。成功后归档 lane 状态并清理内部 worktree。

任务的 race 状态保存在：

```text
.lean-agent/races/<race-id>/race.json
```

添加任务时 Lean 文件可以留空。系统会在项目中创建唯一的
`GeneratedProof_<timestamp>_<id>.lean`，再让各 lane 从任务文字建立精确 imports、声明和证明。

## 本地 Dashboard

```powershell
python -m lean_loop dashboard `
  --project D:\my_math_project `
  --port 8765
```

打开 `http://127.0.0.1:8765`。服务器固定监听本机回环地址，不接受远程连接。Dashboard
显示队列任务、workflow、当前 Plan 步骤、流式模型活动、活动 PID、步骤检查点、候选 Lean
证明、自然语言证明、Lean 诊断、Plan/Review、检索证据、阶段耗时和任务事件。

页面可以添加任务、设置依赖和阶段强度、取消或重试任务，并在确认后启动后台队列
worker。添加任务本身不会调用 API；只有启动 worker 后才会处理队列。控制请求使用
当前 Dashboard 实例的随机令牌，仅接受本机请求。后台 worker 的终端输出保存在：

```text
<项目>\.lean-agent\dashboard-worker.log
```

页面通过 `/api/events` 的 SSE 流每秒更新任务阶段和 PID。关闭浏览器不会停止队列任务；
在服务器终端按 `Ctrl+C` 会关闭 Dashboard，但已经启动的独立 worker 会继续处理队列。

## Codex 与 Claude 订阅后端

`direct` 仍是默认后端，并继续使用上面的 API 配置。若要复用官方客户端已经完成的订阅登录，
请安装官方 Codex CLI 或 Claude Code，并只通过客户端自己的登录流程完成认证：

```powershell
codex login
codex login status

claude
claude auth status
```

模型名必须显式给出；工具不会选择默认模型、切换模型或回退到另一个 provider。Codex 会用官方
catalog 校验 requested model，但当前客户端事件不报告 actual model，因此 metadata 会明确记录
`actual_model=null` 和 `actual_model_status=NOT_REPORTED_BY_CLIENT`。先做 readiness 与隔离连接检查：

```powershell
python -m lean_loop doctor --project D:\my_math_project `
  --agent-backend codex-subscription --model gpt-5.6-sol --reasoning-effort low

python -m lean_loop api-check --project D:\my_math_project `
  --agent-backend claude-subscription --model claude-sonnet-5 `
  --reasoning-effort low --timeout 180
```

随后在 workflow 或 queue 中显式选择同一后端：

```powershell
python -m lean_loop workflow run `
  --project D:\my_math_project --file Main.lean --task "完成证明" `
  --agent-backend codex-subscription --model gpt-5.6-sol

python -m lean_loop queue add `
  --project D:\my_math_project --file Main.lean --task "完成证明" `
  --agent-backend claude-subscription --model claude-sonnet-5
```

`workflow resume` 默认继承 manifest 中的后端；显式传入不同的 `--agent-backend` 会记录配置变化并
触发重新规划。订阅后端在 repo 外一次性临时目录启动无会话继承的官方非交互客户端。Codex 使用
tool-enabled `workspace-write` 沙箱；Shell、`apply_patch`、MCP、Web Search 等实际暴露工具属于 Agent
系统的一部分，沙箱内部文件变化允许存在并会在销毁前归档脱敏的工具事件、命令摘要、文件变化、
退出状态和 sandbox manifest。Windows legacy `workspace-write` 可能具有广泛的只读文件访问能力；
本项目不保证用户目录或认证目录在操作系统层面不可读，这是使用本地 tool-enabled Agent 时必须接受的
已披露风险。metadata 会明确记录 `filesystem_read_scope=WINDOWS_BROAD_READ`、
`filesystem_write_scope=REPO_EXTERNAL_EPHEMERAL_WORKSPACE`、
`read_isolation_status=NOT_ENFORCED_BY_LEGACY_WINDOWS_SANDBOX` 和 `network_policy=DISABLED`。
写入仅限 repo 外的一次性临时 workspace；权威仓库、正式 worktree 和受保护 target 不得改变，工具越出
隔离根目录或 protected-state 发生变化时 fail-closed。Claude 仍使用
`dontAsk`、空工具集和 `safe-mode`，且仅加载隔离目录的 local settings，避免 `plan` 模式触发额外模型。
实现不会主动读取、请求、输出、复制、转换或归档本地认证文件、token、cookie 或 API key；敏感环境变量
会在启动子进程前剔除，网络保持禁用，stdout/stderr 会限长和脱敏；
超时、取消、未登录、额度不可用、模型不可用、客户端协议不兼容或最终结果不唯一时均 fail-closed，
并终止完整进程树。run manifest、queue task 与 response metadata 会保存 backend、CLI、认证、requested/
actual model 状态、reasoning、tool policy 和 sandbox profile。订阅方案及可用模型、额度由对应官方
客户端和订阅计划决定，不代表 API key，也不保证无限用量。

当前订阅 workflow 不允许 `--explain`，避免成功后静默改用 `direct` API；可在成功后另行显式运行
`workflow explain`。诊断时使用 `doctor` 区分安装、登录、模型/推理设置和客户端协议问题，再用
`api-check` 验证一次真实最终结果；若客户端没有报告 actual model，检查结果会保持 UNKNOWN。

## 自然语言证明

Explanation Agent 只解释已经通过 Lean 确定性检查的归档候选。它读取成功候选、原文件、
计划、最终 Review、代码差异和 Lean 检查结果，但不会编辑 Lean 项目，也不能改变正式证明
的成功状态。

为已有的成功任务生成中文证明：

```powershell
python -m lean_loop workflow explain `
  --project D:\my_math_project `
  --run-id 20260713T142143712245Z `
  --language zh-CN `
  --effort medium `
  --api-timeout 600
```

结果保存为该任务状态目录内的 `explanation.json` 和 `explanation.md`。新任务也可以在
`workflow run` 后添加 `--explain`，成功后自动解释；这会多用一次 API 请求。解释 API
失败会单独记录，不会撤销已经通过的 Lean 证明。

可以为解释阶段单独指定模型：

```powershell
$env:LEAN_AGENT_EXPLAIN_MODEL = "gpt-5.5"
```

## 后续扩展方向

- 任务优先级、资源限制和多个隔离工作树并行执行
- 从失败工作流的具体 Plan/Prove/Review 断点继续，而不是重新入队
- Lean LSP 目标状态，而不仅是编译文本
- 多候选并行和确定性评分
- Windows 桌面界面和工作流时间线
- 成本、token、延迟与检索命中率统计

## 许可证

本项目使用 [Apache License 2.0](LICENSE)。
