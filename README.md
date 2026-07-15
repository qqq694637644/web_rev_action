# web_rev_action

`web_rev_action` 是一个单用户、Windows 优先的 GPT 5.6 网页协议分析后端。

它组合三类能力：

- `playwright-cli`：页面操作、snapshot、截图和 Trace。
- `js-reverse-mcp`：网络、SSE、initiator、脚本和断点证据。
- 从 `github-gpt-actions-gateway` 移植的本地 workspace 工具：读写、搜索、补丁和 PowerShell 7。

GitHub、branch、commit、PR、CI 等功能没有复制。这里的 workspace 只是：

```text
data/analysis-workspace/
```

浏览器实验和 workspace 工具直接使用同一个目录。

## 后端模型

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
└── Analysis Workspace Actions
    ├── workspaceInspect
    ├── workspaceSearch
    ├── workspaceReadFiles
    ├── workspaceWriteFile
    ├── workspaceApplyPatch
    └── workspaceExecPwsh

web_rev_action
├── RuntimeCoordinator
│   ├── browser operation reservation
│   └── protected workspace mutation reservation
├── Browser Orchestrator
│   ├── PlaywrightCliAdapter
│   └── JsReverseMcpAdapter
├── ExperimentStore
│   └── 只保存 session 和 experiment manifest
└── AnalysisWorkspaceService
    └── 直接操作 data/analysis-workspace/
```

`ExperimentStore` 不是另一套文件查询系统。它只负责：

- session JSON。
- experiment 目录分配。
- running/completed/failed/interrupted manifest。
- 服务重启后的 interrupted 恢复。

所有普通文件读取、搜索、编辑和脚本执行都由 6 个 workspace Action 完成。

## Public GPT Actions

| operationId | Consequential | 作用 |
| --- | --- | --- |
| `retrieveSkillContext` | false | 发现或加载 Skill。 |
| `readSkillContent` | false | 读取 Skill 文件。 |
| `searchSkillDocs` | false | 搜索 Skill 文档。 |
| `inspectBrowserEvidence` | false | 查询 session、experiment 和 stream 状态。 |
| `runBrowserExperiment` | true | 打开/关闭 session 或运行浏览器实验。 |
| `workspaceInspect` | false | 一次返回目录树、搜索结果和相关文件片段。 |
| `workspaceSearch` | false | 使用 ripgrep 搜索文本。 |
| `workspaceReadFiles` | false | 按行读取多个 UTF-8 文件。 |
| `workspaceWriteFile` | true | 创建或替换 UTF-8 文件。 |
| `workspaceApplyPatch` | true | 应用受控 Codex 文本补丁。 |
| `workspaceExecPwsh` | true | 在分析目录运行 PowerShell 7。 |

## Browser Actions

网页协议分析采用三层职责：

```text
Skill
  先盘点当前网页，再决定假设、实验、证据解释和报告完成标准

Browser Actions
  原子执行 capture、browser-context replay、取消和受控查询

Analysis workspace
  保存 evidence_id、artifact 和派生报告
```

默认 `current-site-analysis` Skill 从当前页面、network/stream、worker、storage、auth 和
evidence gap 开始，不预设产品状态机。它不直接执行 fetch，也不读取凭据后自行拼请求；
只调用结构化 Action。

`pandora-protocol-reproduction` 作为可选专用模板保留。只有当前网页实际呈现对话树、
regenerate、edit、stop 等语义时，才使用其六场景实验矩阵。

### `runBrowserExperiment`

支持：

```text
open_session
capture_flow
replay_request
save_script_source
close_session
cancel_experiment
```

旧客户端仍可发送 `capture_baseline`。它只在请求边界应用一个 `capture_flow`
preset：默认 baseline objective、primary request 允许 0 到 100 个匹配、`flow=[]`。
随后内部调度和 manifest 都使用 `capture_flow`。非空 flow 会被拒绝；新调用不应再
生成 `capture_baseline`。

一次 `capture_flow` 由后端原子执行：

```text
写 running manifest
→ 对齐 Playwright page 与 js-reverse pageId
→ start_stream_capture
→ 执行完整 Playwright flow
→ 等待目标 stream/network/page condition
→ stop_stream_capture
→ 收集网络摘要、截图和 Trace
→ 写 completed / failed manifest
```

普通 `flow`、replay `setup_flow` 和 `verification_flow` 使用同一个 step executor。
phase 只是 step result 中的 `setup`、`action` 或 `verification` 标签，不改变可执行
step 类型、checkpoint、超时、失败或取消语义。

GPT 不直接协调 start、click、wait、stop。

一次 `replay_request` 只接受一个通用 payload：

```json
{
  "session_id": "session_one",
  "objective": "replay one observed request",
  "source": {
    "experiment_id": "exp_source",
    "evidence_id": "ev_network"
  },
  "mutations": [],
  "extractors": [],
  "bindings": [],
  "transport": {},
  "response_reader": {"mode": "auto"},
  "termination": {"conditions": [{"type": "network_close"}]},
  "comparison": null
}
```

核心 API 不再包含 Control、Exploratory 或 Treatment 类型，也不继承 pair protocol。
`src/skill_temple/replay_presets.py` 提供同名客户端 preset，但三个 helper 最终都生成
同一个 `ReplayRequestPayload`。调用方必须显式提交 source、setup、binding、mutation、
reader、termination 和 comparison；后端只负责执行、观察和保存。

一次 replay 的内部顺序是：

```text
验证 source experiment + evidence
→ 读取 exact network snapshot
→ 应用 generated / preserve_source / literal / manual_input binding
→ 运行可选 setup_flow
→ 独立运行 extractor 并记录每项 completed / failed
→ 将成功 extractor 输出注入对应 binding
→ 在当前 browser context 执行 fetch
→ 保存 ordinary network、stream、artifact 和 wire observation
→ 运行可选 verification_flow
→ 按 comparison.references 和 comparison.dimensions 生成事实差异
```

Extractor 失败默认只是 `replay_extractor` evidence，不阻止探索；只有显式
`required=true` 时才进入 `quality_summary`。未解析 binding 保留在
`replay.unresolved_binding_ids`，不会伪装成已注入。

Binding 支持：

```text
value_source=generated
value_source=preserve_source
value_source=extractor + extractor_id
value_source=literal + value
value_source=manual_input + value
```

JSON Pointer 和 query 参数名严格区分大小写，header 名不区分大小写。Cookie、Origin、
Referer、Host、Content-Length 和 `Sec-*` 等 browser-managed header 仍不能通过 replay
header mutation 覆盖。

`comparison` 完全可选，可以引用零个、一个或多个精确事实来源。每个 reference 必须
包含 `experiment_id`，并且恰好包含一个 `evidence_id` 或 `observation_id`；
`include_source=true` 使用当前 replay 的 exact source experiment/evidence。未配置时不生成
comparison 结果。当前可选维度为 `request_body`、`response_status`、
`response_content_type`、`stream_summary` 和 `environment`；输出只使用
`equivalent`、`different`、`missing`、`ambiguous`、`unknown`，不生成字段必要性或因果
资格结论。Environment 默认 `preset=none`，可显式选择 `minimal`、`browser_context` 或
指定 dimensions 的 `explicit`。

Response 读取和终止策略是独立对象：

```text
response_reader.mode = auto | ordinary | sse | ndjson | raw_stream
response_reader.max_bytes / max_events
termination.conditions = exact_sse_data | text_pattern | network_close | idle_window
```

`max_events` 对 SSE 按完整 event、对 NDJSON 按 record、对 raw stream 按 accepted chunk
计数；达到上限时记录 `terminationReason=max_events`。

Manifest 同时保存请求与实际执行协议：

```text
replay.requested_replay_protocol
replay.requested_replay_protocol_hash
replay.replay_protocol
replay.replay_protocol_hash
```

`replay_protocol` 和其 hash 表示应用默认值及 stream 自动升级后的有效配置，包括最终
`capture`、`requirements` 和 network evidence selectors。显式 reader mode 只有在 runtime
返回相同 `observed_response_mode` 时才满足 stream contract；`auto` 接受 runtime 的有效
自动选择结果。

`response_reader.mode=auto` 会启动 stream collector 作为探测，但执行前不强制要求 stream
completeness。执行后根据 runtime 的 `observed_response_mode` 决定有效 requirements：

```text
observed ordinary              → ordinary network completeness
observed sse/ndjson/raw_stream → raw/semantic/artifact/terminal stream completeness
```

因此 source 为普通 JSON、实际 replay 变为 SSE 或 NDJSON 时不会静默漏掉 stream evidence。
HTTP 4xx/5xx 不改变该规则；observed NDJSON/raw stream 仍按 stream contract 评估。

Content-Type 在显式 `sse`、`ndjson` 或 `raw_stream` reader 下只作为 consistency fact；缺失
或不规范 header 不会替代 runtime 已成功使用的显式 reader。`auto` 必须遵守 runtime 的
自动选择规则：event-stream → SSE，NDJSON Content-Type → NDJSON，其他或缺失 → ordinary。

Idle 不再是 reader 的无条件超时。只有显式声明
`{"type":"idle_window","window_ms":...}` 时才启动该窗口，并记录
`terminalConditionMatched=idle_window`。`text_pattern` 匹配 UTF-8 解码后的文本，不宣称
匹配原始字节。

Query binding/mutation 默认使用 `query_serialization=preserve_raw`，只替换目标 occurrence
的原始区间，保留其他参数的 `%20`、hex 大小写、顺序和重复项。只有显式选择
`query_serialization=normalize` 才会整体解析并重新编码 query。

Replay 始终内部注入保留的 `network_evidence.selector_id=replay_request`，用于 exact
outbound wire snapshot。调用方提供的 selector 只会追加，不能覆盖该 selector；
`replay_request` 是保留 ID。Remove/replace/extractor 的整数 occurrence 必须大于等于 0，
add header/query 只接受 `occurrence=append`。

Binding 先应用，mutation 再按列表顺序应用。若中间 binding 或 mutation 被后续操作覆盖，
manifest 会记录 `operation_applied_to_spec=true` 和
`final_wire_observability=overwritten_by_later_operation`，不会把正确的有序执行误报为
ineffective。最后仍可见的操作必须与 exact wire snapshot 一致。

空 `termination.conditions` 会规范化为 `network_close`。Stream contract 同时验证
`terminationReason` 和 `terminalConditionMatched`；缺失或矛盾时为 partial。

SSE parser 仍支持 LF、CRLF、CR、混合换行和 EOF flush；只有完整 event 的合并 `data`
精确匹配条件时才结束。字节/event 预算、raw-only、analyzer 和 transport semantics
均由请求显式配置；idle 只有在 `idle_window` condition 中声明时才启用。Analyzer 默认
关闭；启用时完整结果只保存在 `replay_attempt`
evidence，不影响执行合法性或 comparison。

HTTP status、mutation 是否出现在 wire、binding 是否注入成功都作为 observation 保存。
4xx/5xx 不会使 replay 请求本身非法，也不会自动证明 required、optional 或 conflict。

Capture 阶段禁止 `target.start_url`。需要观察页面初始化请求、重定向、首屏脚本或初始 SSE 时，必须把导航写成 flow 的第一个显式 `navigate` step。这样 running manifest、Trace 和 stream collector 都会在导航前创建。

`target.page_index` 默认是 `null`，表示复用 session 已选择的 tab；只有显式传值时才切换。当前部署共享一个浏览器和一个私有 MCP，因此全局同时只允许一个 browser operation，不排队第二个操作。Open、close、sync capture 和 background capture 都必须先原子取得 RuntimeCoordinator reservation，未取得前不会 attach 或创建 running manifest。

错误提交的后台任务可以显式调用：

```text
cancel_experiment { experiment_id, session_id }
```

它会取消对应 task、等待 collector/Trace cleanup 和 terminal manifest 完成，并在 browser reservation 已释放后返回。

### `inspectBrowserEvidence`

只查询浏览器运行状态：

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

公开 `get_stream_status` 使用：

```text
experiment_id
capture_uuid (optional)
```

数字 `captureId` 是私有 MCP generation 内短期 handle，不是公开查询主键。运行中的 experiment 只有在 UUID 和 transport generation 一致时查询 live MCP；结束后的 experiment 或 MCP 重启后的查询直接读取持久 manifest。

需要查看 `manifest.json`、`events.jsonl`、源码、schema、脚本或报告时，使用 workspace Actions。

执行 endpoint 和 `get_experiment` 只返回有界实验摘要及 `manifest_relative_path`。完整 manifest、raw network/stream source、canonical network observations 和 artifact 索引通过 `workspaceReadFiles` 读取。

Manifest 的质量模型只保留：

```text
execution.status
quality_summary.status
quality_summary.required_completeness
quality_summary.missing_evidence
quality_summary.errors
network_observations[].completeness
network_observations[].association.confidence
artifacts[].completeness
```

执行、证据质量和分析提示分别记录：

```text
execution.status + execution.errors
quality_summary.status + quality_summary.errors
analysis_warnings
```

普通 step、取消或 replay dispatch 失败只影响 execution。`quality_summary` 只聚合
observation count、请求明确要求的 completeness、明确要求的 collector/artifact、
association failure 和显式 stream terminal contract。Observation 自身可以列出全部
`missing_evidence`，但 quality summary 不会提升未要求维度。

不再生成顶层 `execution_integrity`、`evidence_integrity`、
`collector_integrity`、`primary_request_integrity`、
`primary_integrity_dimensions` 或 `primary_requests`。旧 manifest 不做兼容转换。

核心结论使用稳定引用：

```text
experiment_id + evidence_id + artifact_id
```

### Current-site 侦察报告

完成一轮当前网页 capture 后，可以直接从 experiment manifest 生成阶段 B 的四份
侦察报告：

```powershell
python tools/current_site_inventory.py data/analysis-workspace `
  --output-dir reports `
  --analysis-series-id current-site-2026-07
```

输出：

```text
reports/current-site-inventory.md
reports/current-ui-map.md
reports/current-network-map.md
reports/open-questions.md
```

生成器只读取 `experiments/*/manifest.json` 中已经保存的结构事实，包括 page
alignment、step result、network/stream evidence summary、header 名、query 名、request
shape 路径和完整性状态。它不会读取 raw body、raw header、stream payload、截图或
credential artifact，也不会用历史 Pandora 结构补全缺失事实。

可以使用 `--session-id` 或 `--analysis-series-id` 限定一次明确的现场侦察。没有匹配
manifest 时命令直接失败，不生成看似完整的空报告。

`capture_flow.network_evidence` 在第一条页面 mutation 前记录 reqid high-water mark，finalize 时只选择本 experiment 窗口中的请求，并在 MCP generation 仍有效时导出 exact headers/body/initiator。`series` 字段保存 analysis series、scenario、predecessor、sequence 和 conversation key。

每个 JSON request evidence 还生成 public `request-shape.json` 和 `request-body.redacted.json`。`get_request_shape` 默认只返回有界路径页，支持 `path_prefix`、`page_idx`、`page_size`、`max_depth` 和 `max_array_items`；只有显式 `include_redacted_body=true` 才返回裁剪后的 redacted subtree。Identifier 脱敏只匹配 `id`、`*_id` 和 camelCase `*Id/*ID`，不会把 `valid`、`grid`、`hybrid` 或 `solid` 误标为 identifier。

流请求生成 `stream_request` 和按 source 分开的 `stream_event_range` evidence。Stream 与 ordinary network evidence 会收集所有可用稳定 ID，并对候选集取交集；重复 network request ID 可以由唯一 CDP ID 或 persistent ID继续消歧。URL+method只作为最后的 heuristic fallback。Replay 找到唯一 ordinary evidence 后，primary stream再锁定到同一稳定请求；同 URL 的其他流只作为 supporting evidence。

每个选中的请求只生成一条 `network_observations[]` 派生视图。它引用 ordinary
network evidence、stream source、artifact ID 和 association method，并集中保存 request/
response、raw/semantic stream 与 artifact completeness。`network_request` 和
`stream_request` evidence 只保留各自来源事实及 `network_observation_id`，不再复制
snapshot/stream 完整性结论。Ordinary snapshot 不能升级缺失的 raw/events/metadata
stream artifact。

Canonical observation 明确区分：

```text
facts.http_status
facts.request_lifecycle_status
```

`finished`、`canceled`、`failed` 等生命周期状态不能作为 HTTP status 参与 comparison。

Replay 使用 `replay_attempt_id + 有上下界的dispatch window + canonical request
body SHA-256` 锁定唯一 outbound request。缺少 numeric `observedAt` 的请求不参与
自动关联；retry、Service Worker重发或多个候选时 fail closed。

Manifest 分别保存 `pre_dispatch_environment`、`post_response_environment` 和
`post_verification_environment`。因果比较只使用 pre-dispatch，并返回
`observed_equivalent | different | insufficient`；缺失 current node、bundle、
page或认证上下文时不能用 `None == None` 冒充相等。

这是个人本机工具，不加密 Cookie，也不引入 KMS、vault 或密钥管理。后端只在
本机对实际 outbound Cookie名值、Authorization和CSRF计算 SHA-256摘要，用于发现
身份/session轮换；manifest不保存这些原值。该摘要是变化检测，不是加密。

认证上下文只有在 exact request headers明确完整时才标为 `observed`：显式完整性标志，或与同一稳定请求关联且写入成功的 request headers + ExtraInfo（包含 associatedCookies）证据。仅有空数组或普通 headers列表时返回 `unavailable`。Cookie按实际header/segment顺序计算hash；`ignored_cookie_names` 和 `ignored_context_headers` 默认空数组，只在用户确认某项会无关轮换时显式忽略。Post-response和post-verification环境只记录页面信息，不复用旧请求凭据。

Header和query mutation比较完整有序值列表并记录 multiplicity；不会只取第一个同名值。

Stream 和普通 network status 都会读取全部分页，并在执行 event predicate 前锁定具体 primary request ID；`matchedRequestId` 不属于该 request 时不会满足等待。同一 session 重复提交返回 `409 session_busy`，其他 browser operation 返回 `409 browser_busy`。Protected workspace mutation 与 browser operation 通过同一 RuntimeCoordinator 双向互斥，避免 TOCTOU。

## Background job

`capture_flow` 默认使用后台 job：

```text
runBrowserExperiment
→ 立即返回 experiment_id 和 status=running
→ 后台继续完整实验
→ inspectBrowserEvidence.get_experiment 查询终态
```

终态：

```text
completed
failed
interrupted
```

快速实验可以显式使用：

```json
{
  "execution_mode": "sync",
  "deadline_ms": 42000
}
```

后台 job 保留的原因只与 GPT Action HTTP 调用时限有关，与文件读写无关。

## Flow contract

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

Locator：

```text
ref
role + name
label
placeholder
test_id
text
css
```

示例：

```json
{
  "step_id": "send_message",
  "action": "fill",
  "locator": {"placeholder": "Message"},
  "value": "hello",
  "timeout_ms": 5000
}
```

等待条件：

```text
timeout
selector_visible
selector_hidden
request_observed
response_observed
request_log_stable
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

正文谓词由 `js-reverse-mcp` collector 在完整事件文件中匹配。MCP 只返回匹配索引和 request ID，不把事件正文塞回 Action。

Collector 同时维护 source-specific `event index → JSONL byte offset`。每次 predicate 从 `afterEventIndex` 后的记录 offset 直接 seek，不从文件头反复扫描旧事件。

每个会改变页面或请求状态的动作前，后端记录每个 request 的 response 状态、terminal wall time、raw event index 和 semantic event index。后续 wait 只匹配 checkpoint 后新出现或发生状态转换的 request/event；raw 与 EventSource semantic mirror 使用独立游标和 source-specific offset。

取消执行型 step 时会终止本地 Playwright 进程树并停止后续 step，但已经送达页面的 click、navigate 或 upload 无法通用回滚。该 step 在 manifest 中标记为 `canceled_outcome_unknown`，不会自动重试。取消 `wait`、`assert` 或 `snapshot` 等只读 step 时标记为 `canceled`。

Stream start 显式建模为：

```text
not_attempted | failed_before_send | confirmed | outcome_unknown
```

Start 已 dispatch 但调用超时或取消时，后端扫描 experiment namespace 中的 `capture.json` 恢复持久身份，但 `collector_stopped=false`、`collector_cleanup=unknown`，不会把旧数字 ID 当作新 MCP generation 中的 live capture。

Objective 可以分别声明：

```text
require_raw_capture
require_semantic_parse
require_request_snapshot
require_artifacts
```

最终结果为 `complete | partial | failed`。stream 开启时，普通 network summary 只用于诊断，不能替代 primary stream evidence。

## 推荐的强证据 Stop 模板

当实验目标是归因用户点击 Stop 时，推荐使用：

```text
发送消息
→ wait first_event 或 event_predicate
→ 点击 Stop
→ wait network_canceled
```

底层 `network_canceled` 只有在 request、页面、Stop 时间窗口和后续页面行为同时匹配时，才被实验层标记为 `expected_user_cancel`。

该序列不是后端有效性约束。缺少 first-event、Stop 或 terminal checkpoint 的 flow 仍可
执行，但取消归因保持 `unknown` 或 `unclassified`，不能写成已确认的用户取消。

## Analysis workspace Actions

以下能力从 `github-gpt-actions-gateway` 的 workspace 实现移植并去除了 Git 语义。

### `workspaceInspect`

一次返回：

- 目录树。
- 多个 ripgrep 查询结果。
- 与搜索结果相关的 UTF-8 文件片段。
- 完整的输出预算和截断标记。

### `workspaceSearch`

使用 `rg --json`，支持：

```text
fixed string / regex
case sensitive / insensitive
path scope
context lines
match limit
response byte limit
```

### `workspaceReadFiles`

按相对路径和行号读取多个 UTF-8 文本文件，并返回：

```text
path
start_line / end_line
total_lines
bytes
sha256
changed_during_read
content
truncated
error
```

读取不会先把整个文件载入内存，也不会用两次扫描拼出一个不一致结果。一次流式遍历同时完成 UTF-8 decode、字节数、总行数、目标行和可选 SHA-256。仅查看片段时可传 `include_sha256=false`。文件在读取期间增长或被替换时返回 `changed_during_read=true` 且不提供稳定 SHA。

二进制文件不会被伪装成文本。对 `raw.bin`、压缩数据或二进制 payload 使用 PowerShell。

### `workspaceWriteFile`

支持：

```text
create_only
overwrite
overwrite_if_sha256_matches
preserve / lf / crlf
dry_run
```

### `workspaceApplyPatch`

支持 Codex patch：

```text
*** Begin Patch
*** Update File: ...
@@
-old
+new
*** Add File: ...
+content
*** Delete File: ...
*** End Patch
```

具有 changed-file 限制、delete opt-in、dry-run 和失败回滚。

### `workspaceExecPwsh`

在分析目录根目录运行 PowerShell 7，支持：

- UTF-8 console/output 默认值。
- plain output / ANSI 清理。
- stdout/stderr 大小限制。
- timeout。
- Windows 进程树终止。
- 常见网络命令、secret/认证管理命令和远程发布命令的 best-effort 拦截。

这不是安全沙箱：PowerShell alias、.NET、Python 或其他可执行文件仍可能绕过字符串规则。真正离线运行应使用 Windows 防火墙、隔离账户或单独虚拟机。

PowerShell、ripgrep 和 Playwright CLI 的 stdout/stderr 都按流读取；达到输出预算后继续丢弃超限内容或终止搜索，不会先把完整输出缓冲到内存。

它适合：

```text
读取 raw.bin 的指定 offset
SHA-256 / Base64 / 十六进制
解析 JSONL
解压缩本地文件
生成 schema
编写和运行 replay/diff 脚本
批量整理实验报告
```

示例：

```powershell
$bytes = [IO.File]::ReadAllBytes(
  'experiments/exp_001/js-reverse/capture-xxx/request-0001/raw.bin'
)
$hash = [Security.Cryptography.SHA256]::HashData($bytes)
[Convert]::ToHexString($hash).ToLowerInvariant()
[Convert]::ToBase64String($bytes[0..63])
```

## Analysis directory

```text
data/analysis-workspace/
  sessions/
    session_one.json
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
            request-headers.json
            request-headers.redacted.json
            request-body.txt
            response-headers.json
            initiator.json
      replay/
        request-spec.json
        request-diff.json
        response.json
  schemas/
  scripts/
  reports/
  notes/
```

`js-reverse-mcp` 和 `web_rev_action` 必须看到同一个目录。不存在 ZIP 导出层，也不存在另一个 Gateway workspace 同步层。

## Credentials

完整 headers 可能包含 Cookie、Authorization、CSRF 和 Set-Cookie。上游同时生成完整文件和 redacted 文件。

Workspace inspect/search/read 通过统一 `include_credentials` 开关执行默认隔离：默认值为 `false`，manifest 标记为 `credential` 或 `containsCredentials=true` 的 artifact 不返回正文。只有显式 `include_credentials=true` 才允许本机专家读取。`replay_request` 由后端直接使用 exact artifact，不需要把凭据送进 GPT 上下文，也不得把真实凭据复制到自然语言回复、diff 或生成脚本。

## Configuration

```dotenv
SKILL_TEMPLE_SERVER_URL=https://example.com
SKILL_TEMPLE_SKILLS_DIR=C:/path/to/project/skills
SKILL_TEMPLE_BEARER_TOKEN=replace-with-a-long-random-secret

WEB_REV_BROWSER_CDP_URL=http://127.0.0.1:9222
WEB_REV_EVIDENCE_DIR=C:/path/to/web_rev_action/data/analysis-workspace
WEB_REV_PLAYWRIGHT_CLI=playwright-cli
WEB_REV_JS_REVERSE_COMMAND=js-reverse-mcp
WEB_REV_JS_REVERSE_EXTRA_ARGS=["--headless","false"]
WEB_REV_WORKSPACE_SHELL=pwsh
WEB_REV_WORKSPACE_ALLOW_NETWORK=false
```

同一个 analysis workspace 由 OS 文件锁强制只能有一个服务进程。启动顺序是 workspace root → OS lock → ExperimentStore recovery → BrowserService。内置 CLI 固定 `workers=1`；多个 Uvicorn worker 或第二个服务进程会在恢复 running experiment 之前失败。

原始证据路径是只读的：

```text
sessions/
experiments/*/manifest.json
experiments/*/js-reverse/
experiments/*/playwright/
```

实验运行期间禁止 workspace 写入和 PowerShell。Browser operation 与 write/patch/PowerShell 使用同一个 RuntimeCoordinator，reservation 原子互斥。实验结束后，派生文件只能写到 `reports/`、`derived/`、`replay/` 或顶层分析目录中的相应工作区，避免修改原始证据。

默认私有 MCP 参数：

```text
--browserUrl <WEB_REV_BROWSER_CDP_URL>
--allowedRoots <WEB_REV_EVIDENCE_DIR>
--streamArtifactRoot 0
```

这三个关键参数始终由服务生成。`WEB_REV_JS_REVERSE_EXTRA_ARGS` 只能追加非冲突参数；尝试覆盖 browser URL、allowed roots 或 stream artifact root 会在启动时被拒绝。

## Install and run

要求：

- Windows。
- Python 3.11+。
- Node.js 18+。
- PowerShell 7。
- ripgrep。
- `playwright-cli`。
- 当前 stream PR 版本的 `js-reverse-mcp`。
- 已开启 remote debugging 的 Chrome/Edge。

```powershell
py -3 -m pip install -e .[dev]
web-rev-action --host 127.0.0.1 --port 8765
```

OpenAPI：

```text
http://127.0.0.1:8765/openapi.json
```

## Validation

```powershell
python -m ruff check .
python -m pytest
node --test tests/runtime/replay_runtime.test.js
python -m skill_temple.evals evals/skill_queries.jsonl
```

测试按能力组织：

```text
tests/browser/     capture、steps、replay、sessions、finalization、transports
tests/evidence/    network observations 与 stream association
tests/protocol/    mutations、matching、analyzers、evidence primitives
tests/workspace/   inspect、search、write、PowerShell
tests/runtime/     独立 browser replay JavaScript
tests/smoke/       通用 authenticated stateful streaming fixture
tests/fakes/       adapter fakes 与 scenario builders
```

详细命令和 fake 约束见 `tests/README.md`。任何新增 transport、extractor 或 analyzer 应在对应
能力目录增加小型测试，不要重新创建单一数千行 browser test。

阶段 0 真实验证：

```powershell
python tools/toolchain_validation.py `
  --js-reverse-entry <js-reverse-mcp>/build/src/main.js

python tools/browser_action_smoke.py `
  --js-reverse-entry <js-reverse-mcp>/build/src/main.js
```

Synthetic fixture 使用通用 resource/record/cursor 状态模型和自定义 `fixture-complete`
终止事件。它覆盖 2xx、4xx、5xx、cookie/session、stream、replay、mutation、binding、取消和
artifact；不代表任何真实网页协议。

详细路线见 `PLAN.md`，Pandora 分析方法见 `PANDORA_REPRODUCTION.md`。
