# web_rev_action 实现计划

## 1. 项目目标

`web_rev_action` 是一个单用户、Windows 优先的 GPT 5.6 网页协议分析后端。

它复用：

- `playwright-cli`：稳定执行页面动作。
- `js-reverse-mcp`：网络、SSE、脚本、initiator、断点和运行时证据。
- 从 `github-gpt-actions-gateway` 移植的 workspace 读写、搜索、补丁和 PowerShell 7 能力。

不做：

- 自研浏览器自动化、locator 或 CDP collector。
- 让 GPT 用多个 Action 手工协调 start、click、wait 和 stop。
- Git、branch、commit、PR、CI 或远程 workspace 同步。
- ZIP 导出层。
- 在 Action 返回值中直接塞入大型响应、Base64、Cookie 或 Token。

分析根目录是一个普通 Windows 文件夹：

```text
data/analysis-workspace/
```

浏览器实验和 workspace 工具使用同一个目录。

---

## 2. 总体架构

```text
GPT 5.6
├── Skill Actions
│   ├── retrieveSkillContext
│   ├── readSkillContent
│   └── searchSkillDocs
│
├── Browser Actions
│   ├── inspectBrowserEvidence
│   └── runBrowserExperiment
│
└── Analysis Workspace Actions
    ├── workspaceInspect
    ├── workspaceSearch
    ├── workspaceReadFiles
    ├── workspaceWriteFile
    ├── workspaceApplyPatch
    └── workspaceExecPwsh

web_rev_action
├── API / OpenAPI
├── Browser Orchestrator
├── Session Registry
├── Deadline / Job Manager
├── Page Alignment
├── ExperimentStore
├── AnalysisWorkspaceService
└── Adapters
    ├── PlaywrightCliAdapter
    └── JsReverseMcpAdapter

private runtime
├── playwright-cli
├── js-reverse-mcp
└── data/analysis-workspace/
```

### 2.1 ExperimentStore

`ExperimentStore` 只负责浏览器生命周期必须的少量内部状态：

- session JSON。
- experiment ID 和目录创建。
- running/completed/failed/interrupted manifest。
- 服务重启后把遗留 running manifest 标记为 interrupted。

它不提供通用文件读取、搜索、编辑、ZIP 或 credential API。

### 2.2 AnalysisWorkspaceService

直接移植 `github-gpt-actions-gateway` 的本地 workspace 能力，去掉 Git 仓库语义：

```text
workspaceInspect
workspaceSearch
workspaceReadFiles
workspaceWriteFile
workspaceApplyPatch
workspaceExecPwsh
```

所有工具都以 `data/analysis-workspace/` 为根目录。

---

## 3. Browser Action 契约

### 3.1 `inspectBrowserEvidence`

只读、non-consequential：

```yaml
operationId: inspectBrowserEvidence
x-openai-isConsequential: false
```

第一阶段 operation：

```text
get_session
list_experiments
get_experiment
get_stream_status
```

它只查询浏览器 session、experiment/job 和 stream collector 状态。普通文件读取由 workspace Actions 完成。

后续可增加的浏览器语义查询：

```text
list_requests
get_request
get_request_initiator
search_scripts
get_script_source
get_capture_diff
```

这些查询应返回结构化摘要或路径索引，不重新实现通用文件工具。

### 3.2 `runBrowserExperiment`

执行型、consequential：

```yaml
operationId: runBrowserExperiment
x-openai-isConsequential: true
```

第一阶段 operation：

```text
open_session
capture_baseline
capture_flow
close_session
```

后续 operation：

```text
trace_request
reset_and_replay
browser_context_replay
external_http_replay
```

OpenAPI 使用 discriminated union / `oneOf`，每个 operation 只接受自己的 payload。

---

## 4. 后端原子编排

一次 `capture_flow` 必须由后端完整执行：

```text
GPT
  ↓
runBrowserExperiment(capture_flow)
  ↓
web_rev_action Orchestrator
  ├── 创建 experiment 目录并写 running manifest
  ├── 对齐 Playwright page 与 js-reverse pageId
  ├── start_stream_capture
  ├── 执行完整 Playwright flow
  ├── 等待 stream/network/page condition
  ├── stop_stream_capture
  ├── 收集 Trace、截图和网络摘要
  └── 写 completed / failed manifest
```

不允许：

```text
GPT 调 start
GPT 调 click
GPT 调 wait
GPT 调 stop
```

多个 HTTP Action 之间存在延迟、tab 切换和状态漂移，不能作为可靠抓包流程。

---

## 5. Background job 与 deadline

### 5.1 默认 job 模式

`capture_flow` 默认：

```text
创建 running manifest
→ 创建后台 experiment task
→ HTTP 立即返回 experiment_id
→ 后台继续原子执行完整生命周期
```

GPT 使用：

```text
inspectBrowserEvidence.get_experiment
```

查询：

```text
running
completed
failed
interrupted
```

默认 `job_timeout_ms=300000`，自用环境可配置到 30 分钟。

### 5.2 快速同步模式

显式使用：

```json
{
  "execution_mode": "sync",
  "deadline_ms": 42000
}
```

用于明确可以在一个 GPT Action round trip 内完成的短实验。

### 5.3 为什么 job 保留

job 解决的是 HTTP Action 调用时限和长流问题，不是文件访问问题。即使 workspace 工具已经完整，长时间 SSE、工具调用或网络异常仍可能超过同步调用时间。

两种模式都使用一个 absolute deadline，adapter 不各自重新获得完整 timeout。接近 deadline 时优先 stop/finalize 和 best-effort manifest。

---

## 6. capture_flow 数据契约

```json
{
  "operation": "capture_flow",
  "payload": {
    "session_id": "sess_001",
    "objective": "observe the primary conversation request and stream",
    "target": {
      "start_url": "https://example.com/app",
      "expected_url_contains": "/app",
      "page_index": 0
    },
    "primary_request": {
      "url_contains": "/conversation",
      "method": "POST",
      "resource_types": ["fetch"],
      "mime_types": ["text/event-stream"],
      "expected_min_matches": 1,
      "expected_max_matches": 1,
      "allow_supporting_failures": true,
      "include_in_flight": false
    },
    "flow": [],
    "wait_for": {
      "type": "event_predicate",
      "request_matcher": {
        "url_contains": "/conversation",
        "method": "POST"
      },
      "predicate": {
        "type": "exact_data",
        "value": "[DONE]"
      }
    },
    "execution_mode": "job",
    "job_timeout_ms": 300000,
    "capture": {
      "network": true,
      "stream": true,
      "trace": true,
      "screenshots": true,
      "scripts": false
    }
  }
}
```

### 6.1 primary request

```text
url_contains
method
resource_types
mime_types
expected_min_matches
expected_max_matches
allow_supporting_failures
include_in_flight
```

它确定转换为 collector filter：

```text
urlFilter      ← url_contains
methods        ← method
resourceTypes  ← resource_types
mimeTypes      ← mime_types
includeInFlight← include_in_flight
```

实验结果分开返回：

```text
collector_integrity
primary_request_integrity
objective_integrity
```

遥测或 supporting request 失败不能自动覆盖主消息实验结果。

---

## 7. Flow step 契约

统一结构：

```json
{
  "step_id": "send_message",
  "action": "fill",
  "locator": {
    "placeholder": "Message"
  },
  "value": "hello",
  "timeout_ms": 5000
}
```

支持：

```text
navigate
reload
click
fill
type
press
select
check
uncheck
hover
upload
wait
assert
snapshot
```

Locator：

```text
snapshot ref
role + accessible name
label
placeholder
test id
text
CSS
```

Flow 不接受任意 JavaScript、Python、PowerShell 或原始 Playwright 命令。复杂文件处理在实验前后使用 workspaceExecPwsh，不能插进毫秒级抓包编排。

Step result：

```json
{
  "step_id": "click_send",
  "status": "completed",
  "started_at": "...",
  "ended_at": "...",
  "snapshot_ref": "experiments/exp_001/playwright/...",
  "warnings": []
}
```

---

## 8. Wait condition 契约

```text
timeout
selector_visible
selector_hidden
request_observed
response_observed
network_idle
first_event
event_predicate
default_done_marker
network_finished
network_canceled
failed
page_url
```

Event predicate：

```text
exact_data
event_name
json_path_equals
network_terminal
selector_state
```

`[DONE]` 只是默认 predicate，不是通用完成定义。

`exact_data`、`event_name` 和 `json_path_equals` 由 `js-reverse-mcp` collector 在完整事件 artifact 中匹配。调用方传入 `afterEventIndex`，MCP 只返回：

```text
matched
matchedEventIndex
matchedRequestId
matchedSource
```

不把事件正文返回 MCP，也不依赖 recent-event 小窗口。

---

## 9. Adapter 契约

### 9.1 PlaywrightCliAdapter

职责：

- attach browser session。
- tab list/select。
- navigate/reload。
- locator actions。
- snapshot、screenshot 和 Trace。
- upload 分析目录内的文件。
- deadline 时终止 CLI 进程树。

Windows timeout 使用进程树终止，而不是只取消 Python coroutine。

### 9.2 JsReverseMcpAdapter

私有 allowlist：

```text
select_page
select_frame
list_network_requests
get_request_initiator
search_in_sources
get_script_source
break_on_xhr
get_paused_info
pause_or_resume
start_stream_capture
get_stream_status
stop_stream_capture
get_websocket_messages
```

Stream sequence：

```text
start_stream_capture(filter, artifactNamespace=experiment_id)
→ execute Playwright flow
→ waitForStreamCondition(...)
→ stop_stream_capture(deadline)
```

上游 MCP 是长生命周期进程。对 click、fill、upload、replay 等有副作用操作不做自动重试。

---

## 10. 页面对齐

Session 保存：

```text
session_id
browser_endpoint_ref
playwright_session_ref
playwright_page_index
playwright_page_url
playwright_page_title
js_reverse_page_id
js_reverse_page_index
js_reverse_page_url
page_alignment_status
evidence_root_ref
created_at
updated_at
```

初次对齐保存稳定 `js_reverse_page_id`。后续实验按 pageId 选择并重新验证 URL；page index 只用于首次发现和显示。

每次实验前必须确认：

```text
Playwright 当前 page
        ↕
js-reverse 当前 pageId / target / frame
```

对齐失败时实验不开始。

---

## 11. Experiment 目录和 manifest

```text
data/analysis-workspace/
  sessions/
  experiments/
    exp_001/
      manifest.json
      playwright/
        screenshots/
        traces/
      js-reverse/
        capture-<uuid>/
      reports/
  schemas/
  scripts/
  reports/
  notes/
```

`web_rev_action` 创建 `experiment_id` 并作为受限 `artifactNamespace` 传给上游。

Manifest 至少包含：

```text
experiment_id
session_id
operation
status
execution_mode
objective
deadline
page_alignment
steps
primary_request_matcher
primary_requests
stream_wait_result
collector_integrity
primary_request_integrity
objective_integrity
capture_health
artifacts
warnings
errors
```

目录和 `status=running` manifest 必须在第一条浏览器动作之前写入。服务重启时遗留 running manifest 变成 interrupted，已有文件保留。

---

## 12. Analysis Workspace Action 契约

这些接口从 `github-gpt-actions-gateway` 移植，保留主要字段、输出预算和行为语义，只删除 owner/repo/branch/Git 状态。

### 12.1 workspaceInspect

一次返回：

```text
tree
searches
related files
truncated
```

请求支持：

```text
paths
queries
max_depth
max_tree_entries
context_lines
max_search_matches
max_read_files
max_file_lines
max_bytes_per_file
max_bytes
```

### 12.2 workspaceSearch

使用 `rg --json`，不启动 PowerShell。

```text
query
regex
case_sensitive
paths
context_lines
max_matches
max_bytes
```

### 12.3 workspaceReadFiles

读取多个 UTF-8 文件并返回行号、总行数、字节数和 SHA-256。二进制文件返回 per-file error。

### 12.4 workspaceWriteFile

```text
create_only
overwrite
overwrite_if_sha256_matches
expected_sha256
preserve / lf / crlf
dry_run
```

### 12.5 workspaceApplyPatch

支持 Codex `*** Begin Patch` 格式、delete opt-in、changed-file 限制、dry-run 和失败回滚。

### 12.6 workspaceExecPwsh

PowerShell 7 从分析目录根运行：

```text
script
timeout_seconds
max_output_bytes
allow_network
plain_output
utf8_output
```

用于：

- `raw.bin` offset 读取和十六进制/Base64。
- SHA-256。
- JSONL/CSV 解析。
- 本地压缩文件解析。
- schema、diff、replay 和报告脚本。
- 批量文件操作。

默认禁止网络下载、Git push、GitHub CLI 认证/secret 管理、环境枚举、SSH/SCP。没有 Git commit、branch 或 PR 功能。

---

## 13. 凭据和大型数据

上游为 headers 生成完整文件和 redacted 文件。单用户工具不再额外提供 `credential_mode` API。

默认分析：

```text
request-headers.redacted.json
response-headers.redacted.json
```

明确进行本地 replay 时才读取完整 headers。GPT 不应把 Cookie、Authorization、CSRF 或 Set-Cookie 复制到自然语言回复。

大型 Base64、raw stream 和二进制 payload 不通过 Action JSON 返回。使用：

```text
workspaceInspect / Search / ReadFiles 处理文本索引
workspaceExecPwsh 处理二进制、Base64、压缩和 offset
```

---

## 14. Stop-generation 语义

底层 collector 只返回中性终态：

```text
status = canceled
terminalReason = network_canceled
```

Experiment 只有在以下条件同时成立时标记 `expected_user_cancel`：

- flow 中实际执行 Stop step。
- Stop 前观察到同一 primary request 的 first_event 或 event predicate。
- cancellation 位于 Stop 时间窗口。
- request 匹配 primary request。
- pageId 和 session 对齐。
- 没有导航、页面关闭等更合理原因。

Manifest 保存 Stop 前后的 event index、raw byte offset 和目标 request ID。

---

## 15. 开发阶段

### 阶段 0：工具链验证

已完成真实 fixture 8/8：

```text
页面对齐
网络请求
请求/响应体导出
Raw SSE stream
request initiator
脚本读取和搜索
XHR/fetch 断点
分析目录写入
```

SSE 实际验证：

```text
start_stream_capture
→ Playwright click
→ collector-side [DONE] predicate
→ stop_stream_capture
→ 校验 raw.bin、events.jsonl 和 manifest
```

### 阶段 1：Action schema

已完成：

- Browser operation `oneOf`。
- flow/locator/wait schema。
- primary request filter。
- job/sync 模式。
- 两个 Browser Action。
- 六个 Workspace Action。

### 阶段 2：Adapter 与最小 Orchestrator

已完成：

- PlaywrightCliAdapter。
- 私有 JsReverseMcpAdapter。
- ExperimentStore。
- AnalysisWorkspaceService。
- open_session / baseline / capture_flow / close_session。
- 原子 start → flow → wait → stop → manifest。

### 阶段 3：Pandora 最小闭环

```text
baseline
第一轮消息
第二轮消息
重新生成
请求快照
SSE
initiator
脚本
```

### 阶段 4：Evidence、health 与 diff

使用 workspace 工具实现：

- schema 和协议报告。
- JSON/JSONL diff。
- raw byte 定向读取。
- primary/objective integrity 汇总。

### 阶段 5：Trace 与 replay

```text
trace_request
breakpoint / stack / locals / resume
browser-context replay
external HTTP replay
```

### 阶段 6：扩展实验

```text
编辑旧消息
停止生成
标题和删除
文件上传
网页搜索
图片
工具调用
Worker / Service Worker 深入诊断
```

---

## 16. 验收标准

1. GPT 可见内部 MCP lifecycle 工具为零。
2. capture_flow 由后端原子编排。
3. job 模式支持超过 42 秒的流；sync 模式受统一 deadline。
4. Playwright 与 js-reverse 使用同一 CDP endpoint 和稳定 pageId。
5. start_stream_capture 早于第一条页面变更动作。
6. pre-arm 请求默认不进入实验。
7. 无 response 的失败请求仍有 metadata。
8. primary、supporting 和 objective integrity 分开。
9. raw bytes、semantic parse、request snapshot 和 artifact integrity 分开。
10. Stop cancellation 只在实验上下文满足时转换为 expected_user_cancel。
11. experiment manifest 所有路径都相对分析根目录。
12. workspaceInspect 能返回实验树、搜索和相关文件。
13. workspaceSearch 使用 ripgrep 并支持 bounded response。
14. workspaceReadFiles 返回 UTF-8 行号、SHA 和 per-file error。
15. workspaceWriteFile 支持 SHA guard、line endings 和 dry-run。
16. workspaceApplyPatch 支持 update/add/delete opt-in 和 rollback。
17. workspaceExecPwsh 能读取 raw.bin、计算 SHA/Base64、处理 UTF-8 和终止超时进程树。
18. 产品没有 Git、branch、commit、PR、CI 或 ZIP 导出功能。
19. 真实阶段 0 fixture 达到 8/8。
20. Windows 全量测试通过。
