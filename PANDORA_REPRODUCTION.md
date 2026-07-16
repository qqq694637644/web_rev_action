# Pandora 类网页协议分析复现方法

## 1. 目标

本文复现的是 Pandora 的分析思路，不是假设当前网页仍使用相同字段或接口。

需要独立发现：

```text
认证如何进入业务请求
会话如何创建
message / parent message 如何关联
分支和 regenerate 如何表示
流事件如何推进消息状态
停止生成如何影响网络和消息状态
请求由哪段前端代码构造
哪些字段是后端必需、状态字段或埋点字段
```

所有实验只在自己的授权账号和正常登录会话中进行。浏览器负责正常登录，分析程序研究登录后的业务协议。

---

## 2. 工具分工

```text
pandora-protocol-reproduction Skill 负责六组实验、单变量 mutation、证据解释和报告模板
playwright-cli 负责制造和重放页面行为
js-reverse-mcp 负责解释网络、流、脚本、调用栈和运行时证据
web_rev_action 负责原子 capture/replay、证据索引、deadline 和实验 manifest；只记录事实、完整性和提示，不替分析者输出最终协议结论
workspaceInspect/Search/ReadFiles 负责实验后的浏览和文本分析
workspaceWriteFile/ApplyPatch/ExecPwsh 负责 schema、报告、二进制、diff 和重放脚本
```

浏览器实验调用：

```text
inspectBrowserEvidence
runBrowserExperiment
```

实验文件调用：

```text
workspaceInspect
workspaceSearch
workspaceReadFiles
workspaceWriteFile
workspaceApplyPatch
workspaceExecPwsh
```

`js-reverse-mcp` 的三个 stream primitive 是后端私有工具：

```text
start_stream_capture
get_stream_status
stop_stream_capture
```

一次 `capture_flow` 的生命周期必须由同一个后台 job 原子完成 start → flow → wait → stop → manifest。GPT 只创建 job 并查询 experiment 状态，不分步控制 collector。

字段必要性实验使用成对 `replay_request`。Control 声明所有执行参数、无 mutation，并为每个 volatile binding设置：

```text
generated + fresh_equivalent
  默认用于 message/request ID、nonce、timestamp

generated + same_value
  Control/Treatment 共用一个新生成值

preserve_source + same_value
  保留 source 中现有的 conversation ID、parent node 或固定上下文
```

Treatment 只传 `control_experiment_id + mutation`，其余参数从 Control 的
immutable `pair_protocol_hash` 继承。Fresh值可不同，后端规范化后比较非目标
字段。Control target baseline、Treatment target delta、volatile bindings wire命中、
非目标字段等价和pre-dispatch环境比较都是供分析者判断因果性的证据维度。
后端不直接输出required/optional等字段结论；环境为`insufficient`时仍保留全部证据，
由Skill和人工分析决定下一轮实验。凭据不进入Skill、Action响应或报告。

对于有状态接口，Control应声明不可变 `setup_flow`，Treatment自动继承：

```text
setup_flow
→ pre_dispatch_environment
→ replay fetch
→ verification_flow
```

Setup用于reload、重新打开同一conversation、选中同一分支或建立独立测试conversation。Verification只描述响应后的验证。

长 job 提交错误时使用公开 `cancel_experiment {experiment_id, session_id}`。取消会等待 collector/Trace cleanup 和 terminal manifest，不通过重启服务或关闭 session 实现。

---

## 3. 实验目录

每次实验创建独立目录：

```text
data/analysis-workspace/
  experiments/
    exp_001/
      manifest.json
      playwright/
        snapshots/
      js-reverse/
        network/
        console/
        capture-<uuid>/
      replay/
      reports/
  schemas/
  scripts/
  reports/
  notes/
```

`web_rev_action` 把 `experiment_id` 作为受限 artifact namespace 传给上游，流证据进入：

```text
experiments/exp_001/js-reverse/capture-<uuid>/
```

这是 Action 服务本机的普通分析目录，不需要 Git、PR、branch、CI、ZIP 或远程同步。浏览器 Orchestrator 和六个 workspace Action 直接操作同一个目录。

数字 `captureId` 只属于当时的 MCP generation，不写入跨实验分析流程。公开 stream status 使用 `experiment_id`，可选使用 `capture_uuid` 校验；experiment 结束或 MCP 重启后读取持久 manifest。

跨实验引用使用：

```text
experiment_id + evidence_id + artifact_id
```

不要把临时 reqid 当作报告主键。

---

## 4. 第一轮实验范围

第一轮只做六组：

```text
01 baseline
02 第一轮消息
03 第二轮消息
04 重新生成
05 修改旧消息
06 停止生成
```

这六组足以观察：

```text
conversation ID
message ID
parent message ID
消息树和分支
regenerate / variant
流事件序列
中断状态
```

第二轮：

```text
模型切换
标题生成
删除会话
```

第三轮：

```text
文件上传
网页搜索
图片
工具调用
```

现代工具功能会引入大量额外请求，不应在核心对话协议尚未分清时加入。

---

## 5. 每组实验的输入

`capture_flow` 至少声明：

```text
objective
primary_request matcher
expected match count
allow supporting failures
flow steps
wait condition
execution_mode=job
适合目标流长度的 job timeout
raw / semantic / snapshot / artifact requirements
network_evidence selectors
analysis series / scenario / predecessor / sequence
```

示例：

```json
{
  "objective": "observe the first conversation request and complete stream",
  "flow": [
    {
      "step_id": "navigate_app",
      "action": "navigate",
      "value": "https://example.com/app"
    }
  ],
  "primary_request": {
    "url_contains": "/conversation",
    "method": "POST",
    "expected_min_matches": 1,
    "expected_max_matches": 1,
    "allow_supporting_failures": true,
    "include_in_flight": false
  },
  "wait_for": {
    "type": "event_predicate",
    "predicate": {
      "type": "exact_data",
      "value": "[DONE]"
    }
  },
  "requirements": {
    "require_raw_capture": true,
    "require_semantic_parse": false,
    "require_request_snapshot": false,
    "require_artifacts": true
  }
}
```

`[DONE]` 只是这个实验的默认结束谓词，不是所有网页流协议的通用定义。其他页面可以使用 event name、JSON 字段、网络终态或页面状态。

Capture 阶段不使用 `target.start_url`。若需要记录页面初始化请求，显式 `navigate` 必须是 flow 的第一条页面变更动作。`target.page_index` 省略时复用 session 当前 tab。

每条会改变页面或请求状态的 flow step 之前，后端记录每个 request 的 responseObserved、status、terminal wall time、raw event index 和 semantic event index。后续 wait 只接受 checkpoint 后的新 request、状态转换或 source-specific event，因此第二轮消息不能被第一轮的终态、`[DONE]` 或旧 semantic event 满足。

Collector 为 raw 和 semantic JSONL 分别维护 `event index → byte offset`。Predicate 从 `afterEventIndex` 后的 offset 直接 seek，不随事件总量反复从文件头解析。

普通 network evidence 在第一条 mutation 前记录 reqid high-water mark。Finalization只接受窗口内请求；JSON Pointer和query参数大小写敏感，header名大小写不敏感。重复header/query按完整有序列表比较。JSON request生成request shape/redacted body；只有`id`、`*_id`、`*Id/*ID`按identifier脱敏。

Browser-managed Cookie、Origin、Referer、Host、Content-Length 和 `Sec-*` header
mutation会被拒绝。Source为`text/event-stream`时，replay默认要求raw capture、
semantic parse和artifacts；只有`raw_only=true`才跳过semantic要求。增量reader解析
完整SSE event，支持LF、CRLF、CR、混合换行和EOF最终event；只在event data精确等于marker、且可选event name匹配时终止。正文中
出现`[DONE]`不算终态。终止marker、idle timeout、byte limit、truncation和semantic
状态共同进入response contract。

响应恰好达到byte limit时不会立即标截断；必须再读到额外字节才截断，下一次EOF则完整。HTTP 3xx统一为redirect/cache类inconclusive结果。

有效 Treatment 返回非流错误响应时，raw/semantic维度标记为 `not_applicable_non_stream_response`，而不是 collector失败。错误分类：

```text
validation_rejection      remove + 400/422 + structured field_required
value_constraint          replace + invalid enum/type/format
conflict                  409 duplicate/version/state conflict
authentication_failure    401/403
rate_limited              429
server_failure            5xx
unknown_rejection
unexpected_redirect
response_contract_mismatch
```

后端只输出响应分类、结构化匹配事实、observations和inference hints，不输出
required/optional结论。严格的remove-field `validation_rejection`可以成为required
假设的强证据，但仍应由Skill或人工结合持久状态验证决定。结构化path/loc/field
必须与目标精确相等；`invalid request`之类文本不能因包含`id`子串而命中。自然语言
单词边界匹配只能是weak hint。Exact response body优先；preview-only只能作为待复核提示。

环境分为pre-dispatch、post-response和post-verification。因果等价只比较
pre-dispatch，结果为`observed_equivalent | different | insufficient`。通用后端无法
观察current node或关键bundle时必须保留insufficient，不能把缺失值当相等。

本项目是个人本机工具，不做Cookie加密、KMS、vault或企业级secret管理。原始凭据
仍只保存在本地证据文件；环境比较只记录Cookie名值、Authorization、CSRF及组合请求
上下文的SHA-256摘要，用于发现session/账号变化，不用于恢复凭据。

请求上下文只有在headers完整性已证明时才标`observed`：显式完整性字段，或同一稳定请求具备request headers与ExtraInfo/associatedCookies artifact。空列表或普通headers数组不能证明“确实没有凭据”。Cookie hash保留wire顺序；可选ignore列表默认空。Post环境只记录页面，不复用旧请求context。

Replay primary stream锁定到唯一ordinary replay evidence；同URL/method的其他流只作supporting evidence。

---

## 6. 每组实验必须保存的证据

```text
页面动作和 step result
实验前后 snapshot
Playwright Trace
请求列表
primary request 选择结果
request / response headers
ExtraInfo headers 和 cookie 关联
request body 文本及完整性说明
response status
raw stream bytes
UTF-8 decoded stream
SSE 事件和 raw byte range
网络终态
initiator
console error/warn
前后 page snapshot
普通 network evidence ID
artifact ID
相关脚本 URL 和位置
控制台错误
capture health
manifest.json
```

每个结论都应引用：

```text
experiment_id
evidence_id
artifact_id
```

数字 `reqid` 只在当前 page collector generation 中有效，不能作为跨实验主键。

---

## 7. 请求分类

先用 `inspectBrowserEvidence.get_experiment` 确认实验终态，再用 `workspaceInspect`、`workspaceSearch`、`workspaceReadFiles` 和 PowerShell 脚本分类：

```text
页面初始化
账号 / session 状态
配置 / feature flag
模型列表
会话列表和详情
消息提交
停止生成
重新生成
标题生成
遥测 / 埋点
静态资源
```

后续再加入：

```text
文件上传
网页搜索
图片生成
工具调用
WebSocket / 实时通道
```

对每个核心请求记录：

```text
method
path
query
status
content-type
request schema
response / stream event schema
initiator
responseObserved
failurePhase
collectorGeneration
```

默认排除 capture 前已经发出的请求。只有明确研究在途请求时才启用 `includeInFlight=true`。

完整 credential artifact 默认被 workspace inspect/search/read 隐藏。后端 replay 可以按 evidence ID 本地使用；本机专家只有显式 `include_credentials=true` 才能读取正文。即使 running manifest还没有写入 descriptor，固定原始证据路径中的 `all.json`、request/response body、完整 headers、cookie provenance 和 replay request spec也按路径规则隐藏。

若必须在实验运行中查看增长中的文本文件，`workspaceReadFiles` 可能返回 `changed_during_read=true`，此时不应引用稳定 SHA。正式协议结论应在 experiment 终态后读取；仅查看片段时使用 `include_sha256=false`。

---

## 8. 流式响应分析

流式分析应在状态机分析之前完成，因为 conversation ID、assistant message ID、current node 和中断状态可能在中间事件逐步出现。

### 8.1 必须得到的内容

```text
每个事件
事件顺序
raw byte range
chunk 顺序和时间
网络完成、失败或取消
错误事件
取消后的最后可见状态
```

上游 artifact：

```text
raw.bin
  精确原始字节

decoded.sse
  UTF-8 阅读副本；非法 UTF-8 可能被替换

chunks.jsonl
  chunk offset、长度和时间

events.jsonl
  事件字段、raw byte range、chunk range 和完成时间
```

精确定位必须使用 `raw.bin + rawByteStart/rawByteEnd`，不能把 decoded character offset 当成 raw byte offset。

### 8.2 完整性分维度判断

```text
rawCaptureIntegrity
semanticParseIntegrity
requestSnapshotIntegrity
artifactIntegrity
```

例如：

```text
rawCaptureIntegrity = complete
semanticParseIntegrity = partial
```

仍然可以对 `raw.bin` 做 chunk 和离线 parser 分析，不应因语义 parser 降级而丢弃整个实验。

### 8.3 结束条件

底层只记录事件和网络终态。实验层使用受控 predicate：

```text
exact_data
event_name
json_path_equals
network_terminal
selector_state
```

`defaultDoneMarkerObserved=true` 只表示出现过文本 `[DONE]`。

Stream 启动状态必须同时检查：

```text
not_attempted
failed_before_send
confirmed
outcome_unknown
```

`outcome_unknown` 表示 start 可能已在旧 MCP generation 中执行。即使 experiment namespace 下出现 capture.json，也不能声明 collector 已停止，不能用数字 capture ID 查询新 generation；只能按 UUID 和相对路径检查持久证据。

---

## 9. 状态机分析

根据 primary request 和流事件整理：

```text
会话何时创建
conversation ID 何时出现
user message ID 如何生成
assistant message ID 何时出现
parent / child 如何关联
current node 如何变化
重新生成产生新节点还是覆盖
修改旧消息如何产生分支
停止生成后消息处于什么状态
错误和恢复如何表示
```

输出：

```text
reports/state-machine.md
schemas/conversation-state.md
```

每条结论标记：

```text
已观察
已验证
推测
未知
```

---

## 10. 停止生成实验

底层 collector 只能确认：

```text
status = canceled
terminalReason = network_canceled
```

它不能知道是否由用户点击 Stop 引起，因为导航、AbortController、页面关闭和浏览器内部行为都可能取消请求。

`web_rev_action` 只有同时满足以下条件才标记 `expected_user_cancel`：

```text
flow 中实际执行了 Stop step
Stop 前已经观察同一 primary request 的 first_event 或受控 event predicate
取消发生在 Stop step 的限定时间窗口
取消请求匹配 primary request
Stop 后重新获取的实际页面仍与同一稳定 pageId 对齐
同一 canceled request 在 Stop 前后都被观察到
该 request 关联最近的已完成 Stop step
没有导航或 page close 等更合理原因
```

这些条件只控制归因强度，不限制实验能否执行。缺少Stop前事件或Stop后终态观察时，
实验仍然运行并保存证据，取消只标记为`unclassified_network_cancel`或保持未知，供人工补充实验。

分析时同时保存：

```text
Stop step 时间
目标 request ID
Stop 前后的 event index 与 raw byte offset
network_canceled 时间
页面最终状态
消息是否可继续 / regenerate
```

---

## 11. 请求构造代码定位

对每个核心请求执行：

```text
1. workspaceInspect 定位 manifest、request metadata 和 initiator artifact
2. workspaceReadFiles 读取请求摘要、headers、initiator 和相关源码文本
3. workspaceSearch 搜索请求 path、字段和脚本符号
4. 必要时由后续 runBrowserExperiment.trace_request 执行断点实验
5. workspaceWriteFile / ApplyPatch 保存源码片段、调用栈和 paused info
```

只有 initiator 和源码搜索无法解释请求时，才使用 XHR/fetch breakpoint。

断点实验结束必须确认：

```text
execution 已恢复
breakpoint 已移除
页面没有停在 paused 状态
```

目标是区分：

```text
后端必需字段
前端状态字段
动态 ID
实验 / feature flag
追踪和埋点字段
```

---

## 12. 请求快照的真实边界

Stream request artifact 会分别保存：

```text
request-headers.json
request-headers-extra.json
request-headers.redacted.json
request-body.txt
request-body.meta.json
response-headers.json
response-headers-extra.json
response-headers.redacted.json
initiator.json
redirects.json
```

检查：

```text
headersCompleteness
bodyCompleteness
bodyCaptureSource
requestSnapshotIntegrity
```

`request-body.txt` 来自 CDP `postData` 的 UTF-8 表示，不是 wire bytes。对于 multipart、文件、压缩或二进制 body，不能宣称已经获得完整请求体。

### 12.1 凭据

完整 headers 可能包含 Cookie、Authorization、CSRF 和 Set-Cookie。

默认读取、搜索、diff 和报告只使用：

```text
*.redacted.json
```

完整 credential artifact 只供明确的本地 replay 使用，不复制到 GPT summary、自然语言报告或日志。

---

## 13. Worker / Service Worker

当前 stream collector 明确报告：

```text
captureScope = page-target-only
workerCoverage = false
```

这足够完成 Pandora 核心页面协议分析，但不是完整 worker coverage。

只有出现以下情况才进入 Worker / Service Worker 诊断：

```text
initiator 为空
主页面脚本中找不到请求
response.fromServiceWorker=true
页面动作与请求时间对应但 frame 中没有发起栈
```

后续若确实需要，再补 Target auto-attach 和 target/session metadata；不能仅靠 URL 猜测 worker 来源。

---

## 14. 重放验证

### 14.1 Browser-context replay

先在当前登录页面执行受控 fetch：

- 自动复用 Cookie。
- 一次只删改一个字段。
- 请求样本来自 artifact。
- 结果通过 workspaceWriteFile / ApplyPatch 写入分析目录。

这是第一轮字段必要性实验的首选方式。

### 14.2 External HTTP replay

后置实现。先确认：

```text
headersCompleteness 足够
bodyCompleteness 足够
bodyCaptureSource 可接受
credential mode 已明确
```

然后再生成 Python/Node 脚本。若 body 只是 `cdp-postData-utf8`，multipart 或 binary 请求不能宣称已完整复刻。

字段分类：

```text
required
conditionally_required
optional
tracking_only
dynamic
server_generated
client_generated
unknown
```

---

## 15. Pandora 对照矩阵

维护：

```text
reports/pandora-comparison.md
```

| 分析目标 | 独立观察结果 | Pandora 参考结构 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| 认证方式 | Bearer / Cookie / 其他 | access token | 待确认 | exp/evidence |
| 消息入口 | 实际 path | conversation 类接口 | 待确认 | exp/evidence |
| 流协议 | SSE / WS / chunked | SSE | 待确认 | exp/evidence |
| 首次消息 | 实际 action/字段 | next 类语义 | 待确认 | exp/evidence |
| 重新生成 | 实际字段 | variant 类语义 | 待确认 | exp/evidence |
| 继续生成 | 实际字段 | continue 类语义 | 待确认 | exp/evidence |
| 会话关联 | conversation ID | conversation ID | 待确认 | exp/evidence |
| 消息关联 | parent ID | parent message ID | 待确认 | exp/evidence |
| 分支结构 | 实际 mapping | message tree | 待确认 | exp/evidence |
| 结束条件 | 实际 predicate | `[DONE]` 类标记 | 待确认 | exp/evidence |
| 停止生成 | canceled / finished / control request / page state | 取消 / 截断 | 待确认 | exp/evidence |
| 错误事件 | HTTP / stream error | 错误事件 | 待确认 | exp/evidence |

验证的是是否能独立发现同一类协议结构，不要求新网页与旧 Pandora 字段完全相同。

---

## 16. 最小闭环

```text
capture_flow: baseline objective + 空 flow 或最小 snapshot/wait flow
  ↓
capture_flow: 第一轮消息
  ↓
list_evidence / 选择 primary network evidence
  ↓
检查 raw/semantic/snapshot/artifact integrity
  ↓
读取 exact request snapshot、完整流事件和前后 page snapshot
  ↓
inspectBrowserEvidence.get_request_shape
  ↓
inspectBrowserEvidence.get_request_initiator
  ↓
search_scripts → get_script_source → save_script_source
  ↓
control replay: fresh volatile bindings, mutations=[]
  ↓
treatment replay: 引用 control，只改变一个 JSON Pointer/header/query 字段
  ↓
检查 mutation_effective、replay response、stream/network evidence 和 verification_flow 持久状态
  ↓
继续 required/optional/tracking-only 单变量矩阵
  ↓
执行 second/regenerate/edit/stop/reload series
  ↓
写 protocol-map、stream-events、state-machine、schema、comparison 和 open-questions
```

内置 `pandora-protocol-reproduction` Skill 提供该顺序、mutation matrix、evidence contract 和报告模板。标题、删除、文件和工具调用仍在核心六组之后扩展。

---

## 17. 完成标准

一次 Pandora 类复现分析至少应产出：

```text
reports/protocol-map.md
reports/stream-events.md
reports/state-machine.md
reports/pandora-comparison.md
schemas/request.schema.json
schemas/stream-events.schema.json
schemas/conversation-state.md
scripts/replay-http.py
scripts/diff-json.py
notes/open-questions.md
```

并满足：

- start capture 早于第一条页面变更动作。
- pre-arm 请求默认不污染实验。
- 无 response 失败请求仍有 evidence。
- primary request 与 supporting request 分开评价。
- raw bytes、语义事件、请求快照和 artifact 完整性分开，并按 requirements 计算 complete/partial/failed。
- Stop cancellation 不由底层提前解释成用户行为。
- 执行和 get_experiment 只返回有界摘要；完整 manifest 使用 workspaceReadFiles。
- 服务重启后的 open session 必须标记 stale 并重新 attach。
- 一个共享浏览器/MCP 实例下全局只允许一个活动 experiment，不排队第二个实验。
- Open、close、capture 和 workspace write/patch/PowerShell 通过同一个 RuntimeCoordinator 原子互斥，未取得 reservation 前不创建 running manifest。
- credential 默认脱敏。
- credential artifact 默认不被 workspace inspect/search/read 返回；显式 include_credentials 才允许本机读取。
- 每个核心结论引用 experiment_id + evidence_id + artifact_id。
- 普通 network evidence 使用 high-water checkpoint 排除实验前请求。
- browser-context replay 只能引用 source experiment/evidence，不接受任意文件路径。
- JSON body mutation 使用 RFC 6901 Pointer，支持数组索引，不允许 wildcard。
- treatment 必须引用成功 control、复用 volatile bindings 且只有一个 mutation。
- actual outbound evidence 必须证明 mutation_effective=true。
- 流请求必须有 stream_request / stream_event_range evidence ID。
- 源码结论使用 save_script_source 保存 URL/script ID、范围、SHA-256 和 initiator evidence。
- stop 后关闭页面不修改历史 manifest。
- 所有文本 artifact 相对路径可由 workspaceInspect/Search/ReadFiles 处理。
- raw.bin、Base64、压缩和批量 JSONL 可由 workspaceExecPwsh 处理。
- 长流由后台 job 完成；显式快速同步模式才受 42 秒 Action deadline 限制。
- 原始 `manifest.json`、`js-reverse/` 和 `playwright/` 只读；报告、schema、diff 和 replay 输出写入 `reports/`、`derived/` 或 `replay/`。
- 同一 session 重复提交返回 `session_busy`；其他 session 遇到活动实验返回 `browser_busy`。
- supporting request 的事件不能满足 primary predicate；等待结果必须关联具体 primary request ID。
- `get_stream_status` 通过 experiment_id 和可选 capture_uuid 查询，不直接接受数字 capture ID。
- Stop 已成功时，后续 status 查询失败只能产生 warning，不能改写 collector cleanup。
- Stream 未启用时 collector integrity 为 not_required。
