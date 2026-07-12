# web_rev_action 实现计划

## 1. 项目目标

`web_rev_action` 是一个单用户、自用的 GPT 5.6 网页协议分析后端。

目标不是重新实现浏览器自动化或 CDP collector，而是复用：

- `playwright-cli`：执行稳定、可重放的页面动作。
- `js-reverse-mcp`：抓取网络、流、脚本、initiator、断点和运行时证据。
- Gateway workspace 工具：读取、搜索、编辑实验文件和编写重放脚本。

系统需要把一次实验变成可重复、可查询、可比较的证据包，并支持复现 Pandora 类网页协议分析方法。

不做：

- 登录、验证码、风控或速率限制绕过。
- 自研 Playwright、locator、CDP network collector 或 JavaScript debugger。
- 让 GPT 用多个 Action 手工协调开始抓包、点击、等待和停止。
- 在 Action 返回值中直接塞入大响应、Base64、Cookie 或 Token。
- Git、PR、CI 或 workspace 版本控制功能；分析目录就是普通文件夹。

---

## 2. 核心设计原则

### 2.1 只有两个 GPT 可见浏览器 Action

```text
inspectBrowserEvidence
runBrowserExperiment
```

`inspectBrowserEvidence` 只读：

```yaml
operationId: inspectBrowserEvidence
x-openai-isConsequential: false
```

`runBrowserExperiment` 会导航、点击、输入、上传、清理状态、设置断点、重放请求或关闭 session：

```yaml
operationId: runBrowserExperiment
x-openai-isConsequential: true
```

OpenAPI 无法根据 `operation` 动态改变 consequential 属性，因此整个执行 endpoint 标记为 `true`。只读查询全部放到 `inspectBrowserEvidence`。

### 2.2 后端原子编排

一次 `capture_flow` 必须由一个后端调用完成：

```text
GPT
  ↓
runBrowserExperiment(capture_flow)
  ↓
web_rev_action Orchestrator
  ├── 对齐 Playwright page 与 js-reverse page/target
  ├── 调私有 js-reverse MCP: start_stream_capture
  ├── 调 playwright-cli 执行完整 flow
  ├── 私有等待 stream/network/page condition
  ├── 调私有 js-reverse MCP: stop_stream_capture
  ├── 收集 Trace、截图、网络和源码证据
  └── 写 experiment manifest
```

不允许：

```text
GPT 调 start
GPT 调 click
GPT 调 wait
GPT 调 stop
```

Action 之间存在网络延迟、页面切换和状态漂移，这种分步方式不能形成可靠证据。

### 2.3 上游 MCP 始终使用普通模式

`js-reverse-mcp` 始终注册三个 stream lifecycle primitive：

```text
start_stream_capture
get_stream_status
stop_stream_capture
```

`web_rev_action` 通过私有 MCP client 调用，并在 `JsReverseMcpAdapter` 中使用 allowlist。GPT 本来就看不到内部 MCP `tools/list`，不需要上游再提供“gpt-action 隐藏模式”。

### 2.4 一次实验只改变一个变量

每个 flow 应有明确 objective、baseline 和 primary request matcher。实验结果必须区分：

```text
直接观察
已验证
推测
未知
```

---

## 3. 总体架构

```text
GPT 5.6
  │
  ├── Skill Actions
  │   ├── retrieveSkillContext
  │   ├── readSkillContent
  │   └── searchSkillDocs
  │
  ├── Browser Actions
  │   ├── inspectBrowserEvidence
  │   └── runBrowserExperiment
  │
  └── Gateway Workspace Tools
      ├── workspaceInspect
      ├── workspaceSearch
      ├── workspaceReadFiles
      ├── workspaceWriteFile / patch
      └── workspaceExecPwsh

web_rev_action
  ├── API / OpenAPI
  ├── Orchestrator
  ├── Session Registry
  ├── Deadline Budget
  ├── Page Alignment
  ├── Experiment Store
  ├── Evidence Index
  ├── Capture Health
  ├── Diff / Replay
  └── Adapters
      ├── PlaywrightCliAdapter
      ├── JsReverseMcpAdapter
      └── GatewayWorkspaceAdapter

private runtime
  ├── playwright-cli
  ├── js-reverse-mcp
  └── data/analysis-workspace/
```

推荐一 workspace 对应一个 `web_rev_action` 实例和一个 `js-reverse-mcp` 进程。若共享进程，每个 session/experiment 必须使用独立 artifact namespace，不能只依赖全局 root。

---

## 4. Action 契约

### 4.1 通用请求外壳

两个 Action 都使用 operation 分支，不使用包含大量可选字段的单一大对象：

```json
{
  "contract_version": "1.0",
  "operation": "capture_flow",
  "payload": {},
  "skill_binding": {
    "skill_id": "web-protocol-analysis",
    "content_hash": "sha256:..."
  }
}
```

OpenAPI 使用 discriminated union / `oneOf`，确保每种 operation 只接受自己的 payload。

### 4.2 inspectBrowserEvidence

只读 operation：

```text
get_session
get_experiment
list_experiments
list_requests
get_request
get_stream_status
list_artifacts
read_artifact
search_artifacts
get_request_initiator
search_scripts
get_script_source
get_capture_diff
```

默认读取 credential artifact 时不返回原文，而返回：

```text
artifact metadata
header names
redacted artifact
containsCredentials
```

只有明确的本地 replay operation 可以读取完整 credential artifact；完整值不得进入自然语言报告、diff、日志或 Action summary。

### 4.3 runBrowserExperiment

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

---

## 5. capture_flow 请求模型

```json
{
  "operation": "capture_flow",
  "payload": {
    "session_id": "sess_001",
    "objective": "observe the primary conversation request and its stream",
    "target": {
      "start_url": "https://example.com/app",
      "expected_url_contains": "/app"
    },
    "primary_request": {
      "url_contains": "/conversation",
      "method": "POST",
      "resource_types": ["fetch"],
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
    "deadline_ms": 42000,
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

### 5.1 primary request

实验成功不能取所有匹配请求的最坏值。`primary_request` 决定目标请求：

```text
url_contains
method
resource_types
expected_min_matches
expected_max_matches
allow_supporting_failures
include_in_flight
```

后端返回：

```text
collector_integrity
primary_request_integrity
objective_integrity
```

- `collector_integrity`：整个 collector 的诊断状态。
- `primary_request_integrity`：目标请求证据状态。
- `objective_integrity`：结合等待条件、动作结果和目标请求计算的最终实验状态。

遥测流、心跳流或其他 supporting request 失败，不应自动让主消息实验失败。

### 5.2 总 deadline

GPT Action round trip 必须控制在平台上限之内。后端使用一个小于 45 秒的总 deadline，不允许每层各自重新获得完整 timeout。

默认：

```text
总预算              42s
连接和页面对齐       4s
启动 collector       3s
Playwright flow      18s
等待目标条件         10s
stop/finalize         5s
manifest/response     2s
```

未使用的预算可以向后转移。每个 adapter 接收同一个 absolute deadline / AbortSignal：

```text
alignment
Playwright command
waitForStreamCondition
stop_stream_capture
workspace write
```

接近 deadline 时优先完成 stop 和 best-effort manifest，不继续执行新动作。

---

## 6. Flow step 数据契约

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

支持动作：

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

不支持任意 JavaScript、Python、PowerShell 或原始 Playwright 命令作为 flow step。

Locator 优先级：

```text
role + accessible name
label
placeholder
test id
text
CSS
```

XPath 默认不使用。

Step 返回值：

```json
{
  "step_id": "click_send",
  "status": "completed",
  "started_at": "...",
  "ended_at": "...",
  "snapshot_ref": "art_...",
  "warnings": []
}
```

---

## 7. Wait condition 契约

`wait` step 和 capture 结束都使用统一 condition：

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

`[DONE]` 只是 `default_done_marker` 或 `exact_data` 的默认值，不是通用完成定义。

受控 event predicate：

```json
{
  "type": "json_path_equals",
  "path": "$.type",
  "value": "message_end"
}
```

支持：

```text
exact_data
event_name
json_path_equals
network_terminal
selector_state
```

不接受任意代码 predicate。

---

## 8. Adapter 契约

### 8.1 PlaywrightCliAdapter

职责：

- attach/open browser session。
- page list/select。
- goto/reload。
- locator action。
- snapshot、screenshot、Trace。
- upload approved workspace file。
- 在 deadline 或 AbortSignal 到达时停止后续 step。

不得实现：

- 自研 locator 引擎。
- 任意 shell 拼接。
- 自行抓取 CDP stream。

### 8.2 JsReverseMcpAdapter

通过私有 MCP client 调普通 `js-reverse-mcp`，只允许调用明确 allowlist：

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
start_stream_capture(
  artifactNamespace = experiment_id,
  includeInFlight = false
)
→ execute Playwright flow
→ waitForStreamCondition(...)
→ stop_stream_capture(deadline)
```

私有等待方法：

```text
waitForStreamCondition(
  captureId,
  requestMatcher,
  condition,
  sinceVersion,
  deadline,
  abortSignal
)
```

它通过有界轮询 `get_stream_status` 实现，至少支持：

```text
first_event
event_predicate
default_done_marker
network_finished
network_canceled
failed
```

返回目标 request、终态、匹配事件摘要和最后 version。该方法不是 GPT Action。

### 8.3 GatewayWorkspaceAdapter

`web_rev_action` 自己管理：

```text
data/analysis-workspace/
```

Gateway workspace 工具用于实验完成后的读取、搜索、编辑、PowerShell 和重放脚本，不参与毫秒级抓包编排。

---

## 9. Session 与页面对齐

Session 至少保存：

```text
session_id
browser_endpoint_ref
playwright_session_ref
playwright_page_index
playwright_page_url
playwright_page_title
js_reverse_session_ref
js_reverse_target_id
js_reverse_frame_id
js_reverse_page_url
page_alignment_status
workspace_dir
created_at
updated_at
```

每次 baseline、capture_flow、trace_request 前检查：

```text
Playwright 当前 page
        ↕
js-reverse 当前 page/target/frame
```

`page_alignment_status=aligned` 需要同时满足：

- 当前 URL 一致或满足已声明的 redirect/normalization 规则。
- 页面标题或稳定页面标识一致。
- Playwright page index 映射到 js-reverse page ID。
- 选中 frame 与 flow 目标 frame 一致。
- 页面没有在对齐检查期间关闭或跳转。

否则实验不开始。

---

## 10. Experiment 与 artifact

目录：

```text
data/analysis-workspace/
  experiments/
    exp_001/
      manifest.json
      playwright/
      js-reverse/
      reports/
  schemas/
  scripts/
  reports/
  notes/
```

`web_rev_action` 创建 `experiment_id`，并把它作为受限 `artifactNamespace` 传给 `js-reverse-mcp`。上游最终写入：

```text
experiments/exp_001/js-reverse/capture-<uuid>/
```

Manifest 最少保存：

```json
{
  "experiment_id": "exp_001",
  "operation": "capture_flow",
  "objective": "...",
  "deadline": {},
  "page_alignment": {},
  "steps": [],
  "primary_request_matcher": {},
  "requests": [],
  "stream_wait_result": {},
  "collector_integrity": "partial",
  "primary_request_integrity": "complete",
  "objective_integrity": "complete",
  "capture_health": {},
  "artifacts": [],
  "warnings": [],
  "errors": []
}
```

Artifact ID 使用上游 UUID，不使用进程内数字 capture ID。`networkRequestId/reqid` 只用于当前 page collector generation 的临时关联；`web_rev_action` 生成稳定 `evidence_id`。

---

## 11. Capture health

至少包含：

```text
page_aligned
stream_collector_started_before_flow
pre_arm_requests_excluded
primary_request_match_count_ok
raw_capture_integrity
semantic_parse_integrity
request_snapshot_integrity
artifact_integrity
wait_condition_met
collector_stopped
manifest_written
capture_scope
worker_coverage
```

当前上游明确返回：

```text
capture_scope = page-target-only
worker_coverage = false
```

这不是失败，但必须作为覆盖缺口展示。只有请求来源无法解释时才进入 Worker / Service Worker 深入诊断阶段。

### 11.1 取消语义

上游 collector 只返回：

```text
status = canceled
terminalReason = network_canceled
```

`web_rev_action` 只有在以下条件同时成立时才标记：

```text
expected_user_cancel
```

- flow 中存在 Stop step。
- cancellation 位于 Stop step 的限定时间窗口内。
- page/session 对齐。
- request ID 匹配 primary request。
- 没有导航或页面关闭等更合理原因。

---

## 12. Stream 与请求快照

关键 artifact：

```text
raw.bin                 精确原始字节
decoded.sse             UTF-8 阅读副本，可能含 replacement
chunks.jsonl            chunk offset / timing
events.jsonl            语义事件和 raw byte range
request-headers.json    完整请求 headers，可能含凭据
request-headers-extra.json
request-headers.redacted.json
request-body.txt        CDP postData 的 UTF-8 文本，不是 wire bytes
request-body.meta.json  encoding/source/completeness
response-headers.json
response-headers-extra.json
response-headers.redacted.json
initiator.json
redirects.json
metadata.json
```

完整性分开保存：

```text
rawCaptureIntegrity
semanticParseIntegrity
requestSnapshotIntegrity
artifactIntegrity
headersCompleteness
bodyCompleteness
```

对于 multipart、文件或二进制 body，`request-body.txt` 不得描述为精确 wire bytes。

### 12.1 凭据策略

Artifact descriptor：

```text
sensitivity = public | private | credential
containsCredentials
redactedArtifactId
```

默认读取、搜索、diff 和报告只使用 redacted artifact。完整 credential artifact 只供明确的 browser-context/external replay 使用，不进入 GPT summary。

---

## 13. Diff 与重放

### 13.1 Diff

支持：

- 请求集合、method/path/status。
- Header 名称和脱敏值。
- JSON 请求/响应字段。
- SSE 事件类型、顺序和 raw byte range。
- initiator 脚本位置。
- artifact hash。

动态值归一化规则必须写入 diff 报告，不静默忽略。

### 13.2 Browser-context replay

优先在已登录页面上下文中执行 `fetch`：

- 自动复用 Cookie。
- 一次只修改一个字段。
- 使用明确的 credential mode，不把凭据返回 GPT。

### 13.3 External HTTP replay

后置实现。请求样本必须检查：

```text
headersCompleteness
bodyCompleteness
bodyCaptureSource
```

只有完整度足够时才宣称可独立重放；`cdp-postData-utf8` 不能当作 multipart/binary wire bytes。

---

## 14. 开发阶段

### 阶段 0：已完成的工具链验证

已验证：页面对齐、网络、request/response、initiator、脚本、断点、workspace。普通 response 导出不能稳定保存完整 SSE，因此 `js-reverse-mcp` Raw Stream Capture 已成为核心依赖。

### 阶段 1：Action schema

- 两个 Action。
- `runBrowserExperiment` consequential=true。
- operation `oneOf`。
- flow step/result。
- primary request 和 wait condition。
- 42 秒总 deadline。
- Fake adapter 契约测试。

### 阶段 2：Adapter 与最小 Orchestrator

- PlaywrightCliAdapter。
- 私有 JsReverseMcpAdapter allowlist。
- GatewayWorkspaceAdapter。
- open_session / baseline / capture_flow / close_session。
- 页面自动对齐。
- 原子 start → flow → wait → stop → manifest。

### 阶段 3：Pandora 最小闭环

- baseline。
- 第一轮消息。
- 第二轮消息。
- 重新生成。
- 查询请求快照、SSE、initiator 和脚本。

### 阶段 4：Evidence、health 与 diff

- evidence ID。
- artifact 查询和 credential redaction。
- primary/objective integrity。
- capture diff。

### 阶段 5：Trace 与 replay

- trace_request。
- breakpoint/paused stack/locals/resume。
- browser-context replay。
- external replay 后置。

### 阶段 6：扩展实验

- 修改旧消息。
- 停止生成及 cancellation correlation。
- 标题、删除。
- 文件、搜索、图片、工具调用。
- Worker / Service Worker 深入诊断。

---

## 15. 跨仓库验收测试

验收环境使用本地测试页面和真实 adapter，不只测试 collector 单元。

必须覆盖：

1. `web_rev_action` 私有 MCP client 能调用三个 stream primitive。
2. GPT OpenAPI 只有两个 Browser Action，不包含内部 MCP 工具。
3. `start_stream_capture` 的记录时间早于第一条 Playwright 变更动作。
4. pre-arm 请求默认不进入 experiment；`includeInFlight=true` 时可进入。
5. 没有 response 的 `loadingFailed` 仍写入 evidence 和 manifest。
6. Stop step 与 `network_canceled` 只在 page/request/time window 匹配时转换为 `expected_user_cancel`。
7. stop 后关闭页面不会修改历史 capture 状态或 manifest hash。
8. MCP 重启后 UUID artifact ID 不冲突。
9. experiment manifest 中所有相对路径都能被 Gateway workspace 读取。
10. credential artifact 默认返回 redacted artifact；summary/diff/log 不含真实值。
11. `waitForStreamCondition` 支持 first event、predicate、network finished/canceled/failed。
12. supporting request 失败不会覆盖 primary objective success。
13. `rawCaptureIntegrity=complete` 且 parser partial 时仍可按 raw byte range 离线分析。
14. 整个 `capture_flow` 在一个 42 秒总 deadline 内完成或生成 best-effort timeout manifest。
15. Action cancellation 能传入 stop/finalize，而不是只取消外层 HTTP 等待。

---

## 16. MVP 完成标准

1. GPT 可见 Browser Action 只有两个。
2. inspect 只读且 non-consequential；run 执行型且 consequential。
3. `playwright-cli` 与 `js-reverse-mcp` 操作同一浏览器和同一页面。
4. 私有 MCP 始终可调用三个 stream primitive。
5. capture_flow 由后端原子编排。
6. 每个 experiment 有独立 namespace、manifest、evidence ID 和 UUID artifact ID。
7. primary request、supporting request 和 objective integrity 分开。
8. 能保存完整 raw stream、事件顺序、错误和取消结果。
9. `[DONE]` 只是默认 predicate，不是通用完成定义。
10. 无 response 失败请求仍可查询。
11. pre-arm 请求默认不污染实验。
12. 请求快照声明 ExtraInfo、headers 和 body 的真实完整性。
13. credential artifact 默认脱敏。
14. stop/finalize 接收同一 Action deadline 和 AbortSignal。
15. capture health 明确报告 page-target-only 和 workerCoverage=false。
16. Gateway workspace 能读取所有 manifest 相对路径并编写 replay/diff 脚本。
17. 上述跨仓库验收测试全部通过。
