# web_rev_action 系统设计

## 1. 设计目标

`web_rev_action` 是一个单用户、Windows 优先的网页协议分析后端。它把浏览器交互、网络流取证和本地证据分析组织成一次可审计的实验，而不是让 GPT 手工串联多个低层工具。

系统复用三个已有能力：

- `playwright-cli`：页面导航、定位器动作、截图和 Trace。
- `js-reverse-mcp`：CDP 页面映射、网络请求、SSE/EventSource、脚本、initiator 和断点证据。
- 从 `github-gpt-actions-gateway` 移植的本地 workspace inspect/search/read/write/patch/PowerShell 能力。

系统不实现：

- 新的浏览器自动化框架或 CDP collector。
- GPT 可见的 stream start/status/stop 生命周期工具。
- Git、branch、commit、PR、CI 或远程 workspace 同步。
- ZIP 导出层或第二套 artifact 查询 API。
- 在 Action JSON 中返回大型正文、原始字节、Base64、Cookie 或 Token。

所有浏览器证据与派生分析位于一个普通 Windows 目录：

```text
data/analysis-workspace/
```

## 2. 全局设计不变量

以下规则优先于具体 API 和实现细节。

### 2.0 Skill、执行接口与证据分层

系统分成三个职责面：

```text
Skill
  定义实验序列、单变量策略、证据解释、报告和完成标准

Browser Actions
  原子执行 capture、browser-context replay、取消和受控查询

analysis workspace
  保存原始事实、稳定 evidence index、artifact 和派生报告
```

Skill 不能自行拼接任意 JavaScript、读取 credential artifact 后再发请求，或手工协调 MCP 生命周期。执行接口不负责决定 Pandora 六组实验顺序或 required/optional/tracking-only 结论。

### 2.1 一个 workspace、一个服务进程

同一个 analysis workspace 只能由一个 `web_rev_action` 进程持有：

- 内置 CLI 固定 `workers=1`。
- 启动顺序固定为：解析 workspace root → 获取 OS 文件锁 → 创建 `ExperimentStore` → 恢复遗留 `running` experiment → 创建 BrowserService。
- 第二个进程或 Uvicorn worker 使用同一目录时启动失败。

进程锁必须早于 experiment 恢复。第二个进程不能在发现锁冲突前把第一个进程正在运行的 experiment 改成 `interrupted`。

### 2.2 一个共享浏览器、一个活动实验

当前部署共享一个 Chrome CDP endpoint、一个 Playwright CLI 环境和一个长期运行的私有 `js-reverse-mcp`。

`RuntimeCoordinator` 原子管理两类互斥 owner：

```text
browser operation owner
  open_session | close_session | capture_baseline | capture_flow
  replay_request | save_script_source

protected workspace mutation owner
  workspaceWriteFile | workspaceApplyPatch | workspaceExecPwsh
```

两类 owner 互斥，普通 inspect/search/read 不受影响。Browser operation 必须先取得 reservation，再 attach、创建 experiment 目录或写 `running` manifest。

全局同时只允许一个 browser operation，系统不排队：

- 同一 session 已有后台实验时返回 `409 session_busy`。
- 其他 session 或同步请求遇到活动实验时返回 `409 browser_busy`。
- `open_session` 和 `close_session` 在活动实验期间同样被拒绝。
- Protected workspace mutation 活跃时，browser operation 返回 `409 workspace_busy`。
- Browser operation 活跃时，workspace mutation 返回 `409 browser_busy`。

这比等待进程锁更可靠：不会生成长期 `running` manifest，也不会在排队期间耗尽 deadline。

### 2.3 浏览器实验由后端原子拥有

GPT 只调用结构化原子 operation：

```text
runBrowserExperiment(capture_flow)
runBrowserExperiment(replay_request)
```

后端完整执行：

```text
create running manifest
→ align page
→ start Trace
→ start stream collector
→ execute all Playwright steps
→ wait for causal condition
→ stop collector
→ stop Trace
→ collect bounded summaries
→ write terminal manifest
```

GPT 看不到内部 stream primitive，也不能在多个 HTTP 请求之间手工协调 start、click、wait 和 stop。

`replay_request` 同样由后端原子执行：从 source experiment 的 exact network evidence 读取请求，先执行无 mutation 的 control，再让 treatment 复用同一组 volatile binding 值并应用唯一一个结构化 mutation，通过当前页面上下文发起 fetch，并捕获新 network/stream/page/console evidence。Cookie、Authorization 和 CSRF 不进入 Action JSON。

JSON body mutation 使用无 wildcard 的 RFC 6901 JSON Pointer，支持对象属性和数组索引。Browser-managed Cookie、Origin、Referer、Host、Content-Length 和 `Sec-*` header mutation 在 schema 层直接拒绝。Treatment 只有在 exact outbound request 中观察到 mutation 时才有效。

### 2.4 证据以文件为主，Action 返回摘要

原始响应、事件、headers、Trace、页面 snapshot、console、普通 network snapshot 和 replay 结果写入 analysis workspace。Action 只返回：

```text
experiment_id
status
objective_integrity
primary request summary
capture health summary
manifest_relative_path
evidence_id / artifact_id metadata
```

完整内容通过现有 workspace 工具读取。

Manifest 的 `evidence` 数组为语义索引。核心结论使用：

```text
experiment_id + evidence_id + artifact_id
```

而不是临时 reqid 或目录排序。

### 2.5 原始证据不可由分析工具改写

以下路径由后端管理并视为原始证据：

```text
sessions/
experiments/*/manifest.json
experiments/*/js-reverse/
experiments/*/playwright/
```

Workspace write/patch 不允许修改这些路径。派生内容写入：

```text
experiments/*/reports/
experiments/*/derived/
experiments/*/replay/
reports/
scripts/
notes/
```

实验为 `running` 时，禁止 workspace 写入该实验，并暂停 PowerShell 执行，避免与 collector 竞争文件。

## 3. 总体架构

```text
GPT
├── Skill Actions
│   ├── retrieveSkillContext
│   ├── readSkillContent
│   └── searchSkillDocs
│
├── Browser Actions
│   ├── inspectBrowserEvidence
│   └── runBrowserExperiment
│
└── Workspace Actions
    ├── workspaceInspect
    ├── workspaceSearch
    ├── workspaceReadFiles
    ├── workspaceWriteFile
    ├── workspaceApplyPatch
    └── workspaceExecPwsh

web_rev_action
├── FastAPI / OpenAPI
├── RuntimeCoordinator
│   ├── browser operation reservation
│   └── protected workspace mutation reservation
├── BrowserActionService
│   ├── global experiment reservation
│   ├── session registry
│   ├── deadline and cleanup manager
│   ├── page alignment
│   ├── causal wait engine
│   └── objective integrity evaluation
├── ExperimentStore
├── AnalysisWorkspaceService
├── PlaywrightCliAdapter
└── JsReverseMcpAdapter

private runtime
├── Chrome CDP endpoint
├── playwright-cli subprocesses
├── long-lived js-reverse-mcp stdio process
└── data/analysis-workspace/
```

### 3.1 组件职责

`BrowserActionService` 负责实验语义：

- 全局单实验约束。
- session 和 page 对齐。
- 原子 flow 编排。
- checkpoint 与因果等待。
- Stop cancellation 归因。
- complete/partial/failed 判定。
- cleanup 和 terminal manifest。
- 普通 network high-water checkpoint、精确导出和 evidence index。
- browser-context replay 与 source evidence 绑定。
- control/treatment replay、volatile binding 复用和 wire mutation effectiveness。
- JSON request shape/redacted body、stream request/event-range evidence。
- bounded source region 持久化及 SHA-256。
- experiment series/predecessor 校验。

`ExperimentStore` 只负责生命周期持久化：

- session JSON。
- experiment ID 和目录分配。
- running/terminal manifest 原子写入。
- 服务启动时把遗留 `running` 标记为 `interrupted`。

`RuntimeCoordinator` 只负责当前进程的原子 reservation，不保存业务 manifest。OS 文件锁负责跨进程单实例，Coordinator 负责进程内 browser/workspace TOCTOU。

`PlaywrightCliAdapter` 负责页面动作，不解释网络语义。

`JsReverseMcpAdapter` 负责把上游 MCP 的分页、稳定 request ID、source-specific event predicate、精确 network 导出、initiator、source search、console 和受控 evaluate 转换为内部结构，不解释用户业务意图。

`AnalysisWorkspaceService` 负责本地文件分析，不参与浏览器生命周期。

## 4. 公开 Action 契约

系统公开 11 个 operationId。

### 4.1 Skill Actions

```text
retrieveSkillContext       read-only
readSkillContent           read-only
searchSkillDocs            read-only
```

### 4.2 Browser Actions

```text
inspectBrowserEvidence     read-only
runBrowserExperiment       consequential
```

`inspectBrowserEvidence` 当前 operation：

```text
get_session
list_experiments
get_experiment
get_stream_status
list_evidence
get_network_evidence
get_request_shape
get_request_initiator
search_scripts
get_script_source
list_console_errors
```

`runBrowserExperiment` 当前 operation：

```text
open_session
capture_baseline
capture_flow
replay_request
save_script_source
close_session
cancel_experiment
```

OpenAPI 使用 discriminated union。每个 operation 和 flow action 只能接受自己的字段组合。

### 4.3 Workspace Actions

```text
workspaceInspect           read-only
workspaceSearch            read-only
workspaceReadFiles         read-only
workspaceWriteFile         consequential
workspaceApplyPatch        consequential
workspaceExecPwsh          consequential
```

没有 `list_artifacts`、`read_artifact`、`search_artifacts`、`export_experiment` 或 ZIP endpoint。

`workspaceInspect`、`workspaceSearch` 和 `workspaceReadFiles` 默认隐藏 manifest 标记为 `credential` 或 `containsCredentials=true` 的 artifact 正文。只有显式 `include_credentials=true` 才允许本机专家读取；自然语言 Action 摘要永远不返回完整凭据。

## 5. Session 模型

Session 保存稳定的运行时关联：

```text
session_id
status
browser_endpoint_ref
playwright_session_ref
playwright_page_index
playwright_page_url
playwright_page_title
js_reverse_page_id
js_reverse_page_index
js_reverse_page_url
page_alignment_status
service_instance_id
process_started_at
created_at
updated_at
```

状态：

```text
open
closed
stale
```

规则：

- `open_session` attach Playwright，并将当前 page 与 js-reverse stable `pageId` 对齐。
- `page_index=null` 表示复用 session 已保存的 tab；数值表示显式切换。
- 服务重启后，旧 instance 创建的 `open` session 自动变为 `stale`。
- 服务 shutdown 会 best-effort detach 本实例仍为 `open` 的 Playwright session；成功标记 `closed`，失败标记 `stale`。
- 捕获前和 Stop 后都重新验证 Playwright URL 与 js-reverse pageId。

## 6. Experiment 模型

### 6.1 状态

```text
running
completed
partial
failed
interrupted
```

`running` manifest 在第一条浏览器动作之前写入。

### 6.2 执行模式

后台模式是默认值：

```text
runBrowserExperiment
→ reserve global experiment
→ write running manifest
→ start background task
→ immediately return experiment_id
```

GPT 使用 `inspectBrowserEvidence.get_experiment` 查询终态。

错误提交的长 job 可通过 consequential operation 主动取消：

```text
cancel_experiment {
  experiment_id
  session_id
}
```

它只取消对应后台 task，等待 collector/Trace cleanup 和 terminal manifest 完成后返回；不通过 `close_session` 间接取消。

同步模式只用于明确能在一个 Action round trip 内完成的短实验：

```json
{
  "execution_mode": "sync",
  "deadline_ms": 42000
}
```

无论 job 或 sync，都使用同一原子实现和同一全局单实验约束。

### 6.3 capture_flow 顺序

Capture payload 禁止 `target.start_url`。需要捕获页面初始化请求时，导航必须是 flow 的第一条显式 `navigate` step：

```text
running manifest
→ page alignment
→ Trace start
→ stream start
→ navigate
```

否则首屏请求、redirect、脚本和自动 SSE 可能发生在 collector 之前。

## 7. Deadline 与取消

### 7.1 Deadline 分层

每个实验只有一个 absolute execution deadline。Adapter 不重新获得完整 timeout。

后端保留：

```text
5 seconds finalize reserve
8 seconds independent cleanup grace
```

进入 reserve 后不再执行非关键工作：

- 最终截图。
- network summary。
- Trace 文件扫描。

优先级始终是：

```text
stop collector
→ stop Trace
→ write terminal manifest
```

### 7.2 Playwright subprocess

Playwright、PowerShell 和 ripgrep subprocess 都必须处理：

```text
normal completion
timeout
asyncio cancellation
service shutdown
```

Windows 下使用进程树终止，而不是只 kill 包装进程。取消中的 cleanup 使用 `asyncio.shield`，完成后重新抛出 `CancelledError`。

进程树终止只能保证本地命令不再继续发送后续操作，不能通用回滚已经送达浏览器或远端服务的 click、navigate、upload 等副作用。取消发生在执行型 step 内时，step 记录为：

```text
canceled_outcome_unknown
```

系统不重试该 step，由证据和页面终态判断副作用是否已经发生。

### 7.3 MCP side effects

MCP 队列项保存：

```text
absolute monotonic deadline
transport generation
future cancellation state
```

Worker 执行前丢弃：

- 已取消调用。
- 已过期调用。
- 旧 generation 调用。

对于 `start_stream_capture`、`stop_stream_capture`、`select_page`、断点和 resume 等副作用调用：

- timeout 或调用方 cancellation 都会中止整个私有 MCP worker。
- transport generation 增加。
- 下一次调用启动全新 MCP process/session。
- 副作用调用绝不自动重试。

旧 worker 只能更新自己的 generation；退出时不能污染新 worker 的 ready/error 状态。

MCP 异常分为两类：

- Tool 业务错误：当前调用失败，worker 可以继续。
- stdio/session/connection closed、EOF、子进程退出：当前 generation stale，worker 退出；调用方等待清理完成后收到错误，下一次调用自动启动新 generation。

## 8. Stream checkpoint 与因果等待

仅比较 capture version 不足以证明事件或终态来自本轮动作。系统为每个匹配 request 保存状态快照。

### 8.1 Checkpoint 结构

```text
capture_version
requests[request_id]
  response_observed
  status
  terminal_wall_time_ms
  raw_event_index
  semantic_event_index
  primary_event_source
```

Checkpoint 在每条可能改变页面或网络状态的 flow step 之前创建。

### 8.2 Request 身份

稳定引用同时保留：

```text
cdpRequestId
persistentRequestId
networkRequestId / reqid
collectorGeneration
target/session identity
```

等待返回的 `matched_request_ids` 只能包含真正满足条件的 request，而不是 matcher 命中的全部 request。

### 8.3 条件判定矩阵

`request_observed`：

- request ID 不存在于 checkpoint。
- 当前 status 中首次出现。

`response_observed`：

- checkpoint 中不存在该 request，或 `response_observed=false`。
- 当前变为 `true`。

`network_finished`、`network_canceled`、`failed`：

- 当前 status 是目标终态。
- checkpoint 中 status 不同，或 terminal wall time 更新。

`network_terminal`：

- 使用同样的终态转换规则。
- 返回实际转换的 request 集合和对应 terminal status。

`first_event`：

- raw event index 或 semantic event index 在 checkpoint 后增加。

`event_predicate` / `default_done_marker`：

- 先确定 matcher 命中的具体 request ID。
- 分别检查 raw 和 semantic source 是否前进。
- 使用各自的 source-specific `afterEventIndex` 查询上游 collector。
- 校验 `eventMatch.matchedRequestId` 属于该 request。
- 返回唯一实际命中的 request 和 `matchedSource`。

旧终态、旧 `[DONE]` 或 supporting stream 的事件不能因为无关 capture version 增长而满足新等待。

### 8.4 Raw 与 semantic 双游标

Raw parser 与 EventSource semantic mirror 是两个独立序列：

```text
raw_event_index
semantic_event_index
```

不能使用 `max(rawCount, semanticCount)` 合并。Raw heartbeat 数量可能远大于 semantic message；只有双游标才能发现新的 semantic event。

上游 `get_stream_status` 支持：

```text
requestId
eventPredicate
afterEventIndex
eventSource = raw-stream | eventsource
```

它只返回匹配元数据，不返回正文。

Collector 在成功写入事件 JSONL 时维护 source-specific：

```text
event index → JSONL byte offset
```

`findEventMatch()` 从第一个大于 `afterEventIndex` 的 offset 直接 seek，不从文件头反复解析旧事件，避免长流轮询退化为 O(n²)。

## 9. Request 分页与 primary 归属

`get_stream_status` 和 `list_network_requests` 都必须完整遍历 pagination，不能固定读取前 100 条。

流程：

```text
fetch all bounded pages
→ filter primary matcher
→ lock concrete request ID
→ subsequent predicate query by request ID
```

Supporting request 可作为诊断，但不能满足 primary objective。

Primary matcher 映射到 collector filter：

```text
urlFilter       ← url_contains
methods         ← method
resourceTypes   ← resource_types
mimeTypes       ← mime_types
includeInFlight ← include_in_flight
```

## 10. 完整性模型

一个 `integrityStatus` 无法表达所有证据维度。每个 primary request 分开记录：

```text
rawCaptureIntegrity
semanticParseIntegrity
requestSnapshotIntegrity
artifactIntegrity
```

Experiment requirements 声明哪些维度必须 complete：

```text
require_raw_capture
require_semantic_parse
require_request_snapshot
require_artifacts
```

Objective 结果：

```text
complete
partial
failed
```

规则：

- stream=true 时，普通 network summary 只能作为诊断，不能替代 stream evidence。
- supporting request 失败是否影响 objective 由 `allow_supporting_failures` 控制。
- `expected_min_matches=0` 且没有 primary request 是有效 baseline。
- partial、semantic-only 或 artifact error 不得被压成 complete。

## 11. Collector cleanup 与持久身份

MCP process 内的数字 `captureId` 只适合作为短期 handle。Manifest 同时保存：

```text
capture_id
capture_uuid
capture_relative_dir
capture_metadata_artifact_id
collector_cleanup
orphan_capture_id
transport_generation
stream_start_status
```

`stream_start_status`：

```text
not_attempted
failed_before_send
confirmed
outcome_unknown
```

若 start 已 dispatch 但结果未知：

- `collector_cleanup=unknown`
- `collector_stopped=false`
- 保存 artifact namespace 和 transport generation
- 扫描 `experiments/<id>/js-reverse/capture-*/capture.json` 恢复 UUID、相对目录和 metadata artifact
- 不把发现的旧数字 capture ID 当作新 generation 中可查询的 live handle

`collector_cleanup`：

```text
not_required
completed
timed_out
unknown
```

即使 MCP worker 被重建，`capture_uuid`、相对目录和 metadata artifact ID 仍可用于 workspace 检查未完成证据。`orphan_capture_id` 只作为原进程诊断值，不是跨进程恢复主键。

`stop_stream_capture` 与 post-stop status 查询是两个独立阶段。Stop 已确认成功时，后续 status 超时只产生 warning，不覆盖 `collector_cleanup=completed`，也不产生 orphan。

公开 `inspectBrowserEvidence.get_stream_status` 输入为：

```text
experiment_id
capture_uuid (optional validation)
```

只有 manifest 为 running、Coordinator owner 匹配 experiment、UUID 匹配且 transport generation 未变化时才查询 live MCP。Experiment 已结束或 MCP 已重启时直接返回持久 manifest，不接受裸数字 capture ID。

## 12. Stop-generation 语义

底层 collector 只报告中性事实：

```text
status = canceled
terminalReason = network_canceled
```

只有 experiment 层同时满足以下条件时，才标记 `expected_user_cancel`：

- flow 中实际完成 `intent=stop_generation` 的 click。
- Stop 前已经观察到同一 primary request 的事件。
- Stop 后等待命中同一 request 的新 canceled transition。
- cancellation 位于 Stop 时间窗口。
- Stop 后 Playwright page 与稳定 js-reverse pageId 仍对齐。
- 该 cancellation 关联最近的已完成 Stop step。
- 没有 navigation、reload、新 tab 或 page close 等更合理原因。

多个匹配 request 中只有实际 canceled 的 request 参与分类。

## 13. Evidence 目录与 manifest

```text
data/analysis-workspace/
  sessions/
  experiments/
    exp_<timestamp>_<id>/
      manifest.json
      playwright/
        screenshots/
        snapshots/
        traces/
      js-reverse/
        network/
          ev_<experiment>_network_request_<selector>_<reqid>/
            all.json
            request-shape.json
            request-body.redacted.json
            initiator.json
            cookie-*.json
        sources/
          ev_<experiment>_script_source_<label>_<hash>.js
          ev_<experiment>_script_source_<label>_<hash>.metadata.json
        console/
          console.jsonl
        capture-<uuid>/
          capture.json
          request-0001/
            metadata.json
            raw.bin
            decoded.sse
            chunks.jsonl
            events.jsonl
            eventsource.jsonl
            request-headers.json
            request-headers.redacted.json
            response-headers.json
            response-headers.redacted.json
            payloads/
      reports/
      derived/
      replay/
        request-spec.json
        request-diff.json
        response.json
  reports/
  scripts/
  notes/
```

所有 manifest 路径都是 workspace 相对路径，不返回操作系统绝对路径。

Manifest 至少包含：

```text
experiment_id
session_id
service_instance_id
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
objective_requirements
primary_integrity_dimensions
capture_health
network_checkpoint
console_checkpoint
series
replay_source
replay
replay_http_status
mutation_assessment
evidence
capture_uuid
capture_relative_dir
capture_metadata_artifact_id
artifacts
warnings
errors
created_at
updated_at
```

每个 evidence entry 至少包含：

```text
evidence_id
kind
artifact_ids
artifact_paths
step_ids / request_ids（按 kind）
redacted summary
source_experiment_id / source_evidence_id（replay）
```

当前 evidence kind：`network_request`、`stream_request`、`stream_event_range`、`console_message`、`page_screenshot`、`page_snapshot`、`replay_attempt`、`script_source`。

每个 JSON network evidence 自动生成 public `request-shape.json` 和 `request-body.redacted.json`。Shape 使用 JSON Pointer 显示对象、数组长度、scalar 类型和安全 placeholder，不返回原始字符串值。

每个 replay classification 使用：

```text
control replay
  mutations = []
  generates volatile bindings
  must complete with HTTP 2xx/3xx

treatment replay
  control_experiment_id = control
  reuses the exact generated values
  mutations = exactly one
  requires mutation_effective = true on wire evidence
```

若 source response 为 `text/event-stream`，后端自动启用 stream collector 和 raw/artifact requirements。成功 treatment 保存 stream evidence；若有效 treatment 得到明确非流 4xx，记录 `protocol_rejection_observed`，raw/semantic 维度为 `not_applicable_protocol_rejection`，而不是误报 collector 故障。

## 14. Workspace 服务

### 14.1 workspaceInspect

一次返回：

```text
bounded tree
multiple ripgrep searches
related UTF-8 snippets
truncation metadata
```

`max_depth` 相对于每个 requested base path 计算。

默认 `include_credentials=false`。Tree 可以显示 credential artifact 路径和大小，但 search/related snippet 不返回正文。

### 14.2 workspaceSearch

使用 `rg --json`，逐行消费 stdout：

- 达到 match limit 后终止进程。
- 达到 byte budget 后终止进程。
- 达到 timeout 或 Action cancellation 后终止进程树。
- 不使用无界 `communicate()`。
- 默认过滤 manifest 标记为 credential 的 artifact match；显式 `include_credentials=true` 才返回。

### 14.3 workspaceReadFiles

文本读取只打开文件一次，在同一流式遍历中完成：

```text
incremental UTF-8 decode
byte count
total line count
requested line range
optional incremental SHA-256
```

请求可设置 `include_sha256=false`，查看少量行时不计算全文件 hash。读取前后比较 size、mtime 和 file identity；文件增长或替换时返回 `changed_during_read=true`，不宣称稳定 SHA。二进制文件返回 per-file error，使用 PowerShell 定向读取。

Credential artifact 默认返回明确的 hidden error，不返回内容。`include_credentials=true` 只用于本机专家验证；Skill 和自然语言报告不得复制其值。

### 14.4 workspaceWriteFile / workspaceApplyPatch

支持：

```text
create_only
overwrite
overwrite_if_sha256_matches
line-ending policy
dry-run
Codex patch
changed-file limits
rollback
```

写入遵守原始证据只读边界和 patch snapshot 总内存上限。

### 14.5 workspaceExecPwsh

用于：

- raw.bin offset、hex 和 Base64。
- SHA-256。
- JSONL/CSV 处理。
- schema、diff 和 replay 脚本。
- 本地压缩文件分析。

stdout/stderr 并发流式消费，只保留 byte budget 内内容。Timeout、Action cancellation 和 shutdown 都终止 Windows 进程树。

`workspaceWriteFile`、`workspaceApplyPatch` 和 `workspaceExecPwsh` 先通过共享 RuntimeCoordinator 取得 mutation reservation，避免“检查没有 running experiment 后，browser job 立即开始”的 TOCTOU。

网络和危险命令检查是 best-effort 本地策略，不是安全沙箱。真正离线需要 Windows Firewall、隔离账户或 VM。

## 15. 凭据与大型数据

完整 request/response headers 可能包含 Cookie、Authorization、CSRF 和 Set-Cookie。

默认分析使用 redacted summaries。完整 credential 文件只用于后端 `replay_request` 或显式本机专家读取，不复制到自然语言回复、summary 或 diff。`workspaceInspect/Search/ReadFiles` 默认执行该策略，而不是只依赖文档约定。

大型 Base64、binary payload 和 raw stream 不通过 Action JSON 返回：

```text
workspaceInspect/Search/ReadFiles 处理文本索引
workspaceExecPwsh 处理 binary/Base64/compressed/offset
```

## 16. 真实运行验证

### 16.1 Stage 0 工具链

固定本地 fixture 验证：

```text
page alignment
network request/response
request and response body
exact raw SSE bytes
initiator
script read/search
XHR/fetch breakpoint
analysis workspace write
```

要求 `8/8 passed`。

### 16.2 BrowserAction success smoke

真实 Windows 路径：

```text
start Chrome remote debugging
→ start private js-reverse-mcp
→ open_session
→ explicit navigate
→ click
→ source-specific SSE predicate
→ stop/finalize
→ inspect raw/events/headers
→ capture authenticated Pandora-like messages[] request with SSE response
→ export exact network evidence with evidence_id
→ inspect request shape and array JSON Pointer paths
→ control replay with fresh volatile message ID (SSE 200)
→ treatment removing tracking field (SSE 200)
→ treatment removing required message ID (JSON 422)
→ verify mutation_effective on exact outbound snapshots
→ verify stream_request / stream_event_range evidence IDs
→ workspace PowerShell SHA verification
→ close_session
→ residual process check
```

### 16.3 Cancellation 与因果门禁

必须覆盖：

```text
cancel during Playwright navigate/click
cancel during start_stream_capture
old finished request plus unrelated version change
two matching requests with one terminal transition
raw heartbeat count greater than semantic message count
two sequential message actions with independent checkpoints
more than 100 stream/network requests
shutdown with open Playwright session
```

测试必须确认：

- 无残留 Playwright/Node/MCP/PowerShell/ripgrep 子进程。
- 被取消的本地进程树已终止，后续 step 未执行，当前 step 的外部结果明确标记为 unknown。
- matched request ID 精确。
- raw/semantic source 和 offset 精确。
- terminal manifest 可读取。
- MCP read-only call 中进程崩溃后，下一次调用启动新 generation。
- start outcome unknown 不会报告 collector stopped。
- stop 成功不会被 post-stop status 失败覆盖。
- `cancel_experiment` 返回时 browser reservation 已释放。
- growing file 返回 `changed_during_read` 或稳定单次快照。

## 17. 当前边界与演进方向

当前 collector scope 明确为：

```text
captureScope = page-target-only
workerCoverage = false
```

Worker / Service Worker auto-attach 不属于当前完成契约。

当前已实现 paired browser-context replay、JSON Pointer array mutation、request shape/redacted body、ordinary/stream evidence、initiator/source persistence、console/page snapshots、verification flow、series 和 packaged Pandora Skill。尚未完成：

```text
trace_request atomic breakpoint lifecycle
automatic six-scenario Pandora-like fixture series
external HTTP replay
```

未来扩展必须保持现有不变量，可增加：

```text
trace_request
external HTTP replay
capture diff
Worker / Service Worker target coverage
```

只有在需要并行实验时，才把运行模型升级为“每个 session 独立 Chrome/MCP 实例”。在此之前，不通过放宽全局锁或增加队列来模拟并发。

## 18. 验收标准

1. GPT 看不到内部 MCP lifecycle 工具。
2. Capture flow 与 browser-context replay 由后端原子完成。
3. 全局只允许一个活动实验，第二个实验立即返回 busy。
4. Running manifest、Trace 和 collector 早于第一条页面变更动作。
5. Playwright、MCP、PowerShell 和 ripgrep cancellation 都完成进程树 cleanup。
6. Side-effect MCP timeout/cancel 后 generation 重建且不自动重试。
7. MCP transport crash 结束旧 generation，下一次调用自动恢复。
8. 每个 wait 使用 request-state checkpoint，而非只有 capture version。
9. Raw 与 semantic event 使用独立 cursor、source-specific predicate 和 byte-offset seek。
10. Wait 只返回真正满足条件的 request IDs。
11. Stream 和 network status 完整分页。
12. Primary、supporting 和 objective integrity 分开；stream=false 使用 not_required。
13. Capture UUID、relative directory、metadata artifact、namespace 和 generation 持久化。
14. Public stream status 以 experiment/UUID 身份查询，不接受裸 capture ID。
15. Start outcome unknown 不声明 stopped；stop/status 结果互不覆盖。
16. `cancel_experiment` 等待 cleanup 并释放 runtime reservation。
17. Stop cancellation 只在完整实验上下文满足时解释为用户取消。
18. 原始证据只读，派生内容写入指定目录。
19. Browser operation 与 protected workspace mutation 原子互斥。
20. Workspace 大型 I/O 流式有界；文件读取为单次快照并报告 changed_during_read。
21. 服务 shutdown detach 本实例的 open Playwright sessions。
22. OS 进程锁早于 ExperimentStore recovery。
23. Stage 0、BrowserAction success、cancellation 和 causal multi-stream gates 全部通过。
24. 产品不包含 Git、PR、CI、ZIP 或第二套 artifact 文件 API。
25. Ordinary network evidence 使用 experiment high-water checkpoint，只导出窗口内 reqid。
26. 每个核心证据拥有稳定 evidence_id，并链接 artifact_id。
27. Replay 只能从 source experiment/evidence 读取 exact snapshot，不能接受任意本地路径。
28. Credential artifact 默认不被 workspace inspect/search/read 返回。
29. Packaged Pandora Skill 负责六组实验、单变量矩阵和报告完成标准。
30. JSON replay path 使用无 wildcard JSON Pointer，支持数组索引。
31. Treatment 必须引用成功 control、复用 volatile bindings 且恰好一个 mutation。
32. Browser-managed header mutation 在 schema 层拒绝。
33. 每个 treatment 必须由 exact outbound request 证明 mutation_effective=true。
34. JSON request 自动生成 request shape/redacted body public artifacts。
35. SSE source replay 自动启用 raw stream capture，并生成 stream evidence IDs。
36. 保存的源码片段包含 URL/script ID、范围、SHA-256 和 initiator evidence 关联。
