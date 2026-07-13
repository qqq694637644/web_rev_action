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

协议复刻采用三层职责：

```text
Skill
  决定六组实验、单变量 mutation、证据解释和报告完成标准

Browser Actions
  原子执行 capture、browser-context replay、取消和受控查询

Analysis workspace
  保存 evidence_id、artifact 和派生报告
```

内置 `pandora-protocol-reproduction` Skill 不直接执行 fetch，也不读取凭据后自行拼请求；它只调用结构化 Action。

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

一次 `replay_request` 同样由后端原子执行：

```text
验证 source_experiment_id + source_evidence_id
→ Control 保存 immutable pair_protocol_hash 和 volatile binding policy
→ 本地读取 exact network snapshot
→ Treatment 只提交 control_experiment_id + 唯一 mutation
→ generated+fresh_equivalent 重新生成一次性值
→ preserve_source+same_value 复用真实 source 上下文
→ 启动新 experiment 和 capture/checkpoint
→ 执行 Control 定义且 Treatment 自动继承的 setup_flow
→ 重新对齐并记录 pre_dispatch_environment
→ 在当前页面上下文执行 fetch(credentials=include)
→ 导出新 network/page/console evidence
→ 唯一锁定 Control/Treatment exact outbound request
→ 验证 Control baseline、target delta、volatile effectiveness
→ 规范化后验证 non-target fields equivalent
→ 保存 request diff 与 response artifact
```

response category 不再是 replay 的固定后端 verdict。默认只保存 HTTP status、
Content-Type、response artifact、wire mutation assessment 和环境比较事实。需要旧式
HTTP response 分类提示时，Control 显式声明：

```json
{
  "response_analyzer": {
    "name": "http_response_classifier",
    "version": "1"
  }
}
```

analyzer 配置会写入 immutable pair protocol 并由 Treatment 继承。完整输出只保存在
对应 `replay_attempt` evidence 的 `response_analysis` 中；manifest 仅保存 evidence ID
和 analyzer/classification 的有限摘要。它不会把实验自动改成 failed、partial 或
“可推断”。

公开 payload 不接受任意本地路径，也不返回 Cookie、Authorization 或 CSRF。JSON mutation 使用 RFC 6901 Pointer，例如 `/messages/0/content/parts/0`；不支持 wildcard。JSON Pointer 和 query 参数名严格区分大小写，header 名不区分大小写。Cookie、Origin、Referer、Host、Content-Length 和 `Sec-*` 等 browser-managed header mutation 会被拒绝。

Control 必须 `mutations=[]`，并在 actual wire snapshot 中观察到所有 volatile bindings。HTTP status 是比较事实，不是 Treatment 的入口条件；Control 和 Treatment 的状态及是否变化记录在 `replay_comparison`。若实验需要限定 2xx，应由该实验显式声明，而不是作为全局规则。每个 binding 选择：

```text
value_source=generated + fresh_equivalent
  message/request ID、nonce、timestamp分别生成新值

value_source=generated + same_value
  Control/Treatment共用一个新生成值

value_source=preserve_source + same_value
  保留现有 conversation ID、parent node 或固定上下文
```

`same_value` 本身不表示保留 source 原值；需要原值时必须使用
`preserve_source`。Binding 路径是 mutation祖先时会被拒绝，避免规范化先
抹掉被测试字段。

Treatment 的公开 payload 只能包含 `control_experiment_id` 和一个 `mutation`；target、capture、wait、verification、deadline、source 和 network selector全部从 Control 的 `pair_protocol_hash` 继承。Fresh 值不要求物理相同，而是在成对比较时规范化为同一逻辑 placeholder。若 Control 中没有 target、Treatment 没有产生预期 delta、volatile binding未上 wire、非目标字段不等价或 replay request候选不唯一，实验直接失败。

有状态请求可以在 Control 中声明不可变 `setup_flow`。Treatment 自动继承并按固定顺序执行：

```text
start collector
→ setup_flow
→ 重新对齐页面并记录 pre_dispatch_environment
→ replay fetch
→ verification_flow
```

`setup_flow` 用于 reload、重新打开同一 conversation、选中同一分支或创建隔离测试状态；`verification_flow` 只描述响应后的验证，不能拿来伪造发送前环境等价。

若 source response 是 `text/event-stream`，replay 默认要求 raw capture、
semantic parse 和 stream artifacts；只有显式 `raw_only=true` 才跳过 semantic
要求。Evaluate 使用增量 `ReadableStream` reader和小型 SSE parser，支持 LF、
CRLF、CR、混合换行和 EOF 最终 event；只在完整 event的合并 `data` 精确等于 marker、且可选 event name匹配时终止。正文、JSON
或工具参数中的字面量 `[DONE]` 不会提前结束。`idle_timeout`、字节上限截断、
缺失 marker、semantic失败或Content-Type不符都会进入 partial/failed。

响应恰好等于 `max_response_bytes` 时会再读一次：下一次 EOF 才判完整，只有出现额外字节才标记 truncated。

有效 Treatment 返回非流错误响应时，ordinary exact response 可以终结本轮实验而不误报 collector 故障，但只有以下情况能支持 required：

```text
validation_rejection = remove mutation + HTTP 400 / 422
且结构化 field_required 精确引用被测试目标
```

Replace返回 enum/type/format校验错误只说明 `constrained_value`，不说明字段
required。HTTP 409统一是 `conflict`。自然语言字段名只算 weak text hint；required
必须来自 exact network response body，或确认未截断且长度完全匹配的 bounded
replay response body。Preview-only、认证失败、限流、5xx、通用4xx、redirect和
response contract mismatch都必须 partial/inconclusive。

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

执行 endpoint 和 `get_experiment` 只返回有界实验摘要及 `manifest_relative_path`。完整 manifest、network summary、requests 和 artifact 索引通过 `workspaceReadFiles` 读取。

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

流请求生成 `stream_request` 和按 source 分开的 `stream_event_range` evidence。Stream 与 ordinary network evidence 优先按 `networkRequestId + collectorGeneration`、CDP ID、persistent ID关联；URL+method只允许作为唯一候选 fallback。Replay 找到唯一 ordinary evidence 后，primary stream再锁定到同一稳定请求；同 URL 的其他流只作为 supporting evidence，不能拖低 replay objective。Ordinary snapshot只补充request body/header完整性，不能升级缺失的raw/events/metadata stream artifact。

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

## Stop-generation 模板

带 `intent=stop_generation` 的 flow 必须满足：

```text
发送消息
→ wait first_event 或 event_predicate
→ 点击 Stop
→ wait network_canceled
```

底层 `network_canceled` 只有在 request、页面、Stop 时间窗口和后续页面行为同时匹配时，才被实验层标记为 `expected_user_cancel`。

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
python -m skill_temple.evals evals/skill_queries.jsonl
```

阶段 0 真实验证：

```powershell
python tools/toolchain_validation.py `
  --js-reverse-entry <js-reverse-mcp>/build/src/main.js

python tools/browser_action_smoke.py `
  --js-reverse-entry <js-reverse-mcp>/build/src/main.js
```

详细路线见 `PLAN.md`，Pandora 分析方法见 `PANDORA_REPRODUCTION.md`。
