# web_rev_action 设计与演进计划

## 1. 文档定位

`web_rev_action` 是面向网页版 GPT Action 的单用户、Windows 优先网页协议实验后端。它把浏览器操作、网络取证、请求重放和本地证据分析组织成结构化 Action，使 GPT 能在人工监督下研究持续变化的网页协议。

本文只回答四个问题：

1. 产品长期保持哪些不变量；
2. 当前系统已经提供什么；
3. 下一版通用协议实验契约应是什么；
4. 实现应按什么顺序演进。

文档优先级如下：

```text
PANDORA_REPRODUCTION.md  项目目的、实验方法和报告完成标准
PLAN.md                 系统设计、边界和演进路线
README.md               当前公开 Action 的使用说明
代码、OpenAPI、测试      当前实现事实
```

若文档与代码不一致，当前行为以代码和测试为准；设计目标以本文标注的“目标契约”为准。本文不会把尚未实现的能力写成现状。

`PANDORA_REPRODUCTION.md` 描述的是一种可复用的研究方法，不是要求后端永久绑定 Pandora 的 endpoint、字段名、SSE 结束标记或错误码。

---

## 2. 产品目标与边界

### 2.1 产品目标

系统服务于以下闭环：

```text
GPT / Skill 提出实验假设
→ Browser Action 原子执行 capture 或 replay
→ 后端保存可审计原始证据
→ GPT 使用 workspace Action 分析证据
→ Skill 综合多轮实验形成结论和报告
```

核心能力是：

- 在正常登录的浏览器上下文中捕获页面、网络、流、console、脚本和 initiator 证据；
- 从受信任的 source evidence 构造 browser-context replay；
- 支持探索性重放和严格成对单变量实验；
- 保存稳定的 `experiment_id + evidence_id + artifact_id` 证据链；
- 让 GPT 看到结构化事实、完整性状态和提示，而不是未经证明的协议结论；
- 对凭据、大型正文和原始字节执行默认隐藏与有界访问。

### 2.2 非目标

本项目不负责：

- 新造浏览器自动化框架或 CDP collector；
- 让 GPT 直接控制私有 MCP 的 start/status/stop 生命周期；
- 绕过登录、验证码、访问控制或站点授权；
- Git、branch、commit、PR、CI 或远程 workspace 同步；
- 把完整 Cookie、Token、Authorization、原始二进制或大型 Base64 放入 Action JSON；
- 在后端硬编码某个站点的 required/optional 业务结论；
- 在证据不足时伪装成自动因果证明。

### 2.3 部署边界

当前部署模型保持简单：

```text
一个 analysis workspace
一个 web_rev_action 服务进程
一个共享 Chrome CDP endpoint
一个 Playwright CLI 环境
一个长期运行的私有 js-reverse-mcp
一个活动 browser operation
```

同一 workspace 由 OS 文件锁保证单进程持有。进程内由 `RuntimeCoordinator` 原子互斥 browser operation 与受保护的 workspace mutation。系统不通过排队掩盖竞争；冲突立即返回 busy。

只有需要并行实验时，才升级为每 session 独立浏览器和 collector。现阶段不通过放宽全局锁模拟并发。

---

## 3. 分层架构

```text
GPT
├── Skill Actions
│   ├── retrieveSkillContext
│   ├── readSkillContent
│   └── searchSkillDocs
├── Browser Actions
│   ├── inspectBrowserEvidence
│   └── runBrowserExperiment
└── Analysis Workspace Actions
    ├── workspaceInspect
    ├── workspaceSearch
    ├── workspaceReadFiles
    ├── workspaceWriteFile
    ├── workspaceApplyPatch
    └── workspaceExecPwsh

web_rev_action
├── FastAPI / OpenAPI
├── RuntimeCoordinator
├── BrowserActionService
├── ExperimentStore
├── AnalysisWorkspaceService
├── PlaywrightCliAdapter
└── JsReverseMcpAdapter

private runtime
├── Chrome CDP endpoint
├── playwright-cli subprocesses
├── js-reverse-mcp stdio process
└── data/analysis-workspace/
```

职责必须保持分离：

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| Skill | 实验序列、研究假设、站点知识、证据解释、报告 | 拼接任意脚本、读取凭据后自行发请求 |
| Browser Actions | 原子执行、生命周期、证据采集、通用比较和完整性 | 决定站点字段必需性、写死 Pandora 协议 |
| Workspace Actions | 有界读取、搜索、派生分析、报告和脚本 | 控制浏览器生命周期、改写原始证据 |
| Adapters | 调用 Playwright 与 js-reverse-mcp，转换上游结构 | 解释用户业务意图 |

后端应尽量返回中性观察。Skill 可以结合多个实验、站点上下文和用户目标形成推断。

---

## 4. 当前公开契约

当前系统公开 11 个 operationId：

| operationId | 类型 | 作用 |
| --- | --- | --- |
| `retrieveSkillContext` | read-only | 发现或加载 Skill |
| `readSkillContent` | read-only | 读取 Skill 文件 |
| `searchSkillDocs` | read-only | 搜索 Skill 文档 |
| `inspectBrowserEvidence` | read-only | 查询 session、experiment 和证据摘要 |
| `runBrowserExperiment` | consequential | 打开/关闭 session，执行或取消实验 |
| `workspaceInspect` | read-only | 返回目录树、搜索结果和片段 |
| `workspaceSearch` | read-only | 使用 ripgrep 搜索文本 |
| `workspaceReadFiles` | read-only | 按行读取 UTF-8 文件 |
| `workspaceWriteFile` | consequential | 创建或替换文本文件 |
| `workspaceApplyPatch` | consequential | 应用受控文本补丁 |
| `workspaceExecPwsh` | consequential | 在分析目录运行 PowerShell 7 |

`runBrowserExperiment` 当前支持：

```text
open_session
capture_baseline
capture_flow
replay_request
save_script_source
close_session
cancel_experiment
```

`inspectBrowserEvidence` 当前支持：

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

OpenAPI 使用 discriminated union。每个 operation 和 flow action 只能接受自己的字段组合。

当前 replay 只支持 `control` 与 `treatment`，mutation 只支持 remove/replace，且 stream 逻辑带有 SSE 和 `[DONE]` 默认值。这些是当前实现事实，不是长期设计不变量。

---

## 5. 原子实验模型

### 5.1 Session

Session 保存浏览器 endpoint、Playwright page、js-reverse stable page ID 和服务实例之间的稳定关联。状态为：

```text
open | closed | stale
```

服务重启后，旧实例创建的 open session 变为 stale。每次 capture、replay 和 Stop 后都重新检查页面对齐。

### 5.2 Experiment

Experiment 生命周期为：

```text
running | completed | partial | failed | interrupted
```

`running` manifest 必须在第一条浏览器副作用之前写入。后台 job 是默认模式；短实验可显式使用 sync，但两者共享同一原子实现和 reservation。

一次 capture 的基本顺序：

```text
reserve runtime
→ write running manifest
→ align page
→ start Trace / collector
→ execute flow
→ wait for causal condition
→ stop collector / Trace
→ persist bounded evidence
→ write terminal manifest
→ release reservation
```

需要捕获页面初始化行为时，`navigate` 必须是 flow 的第一条显式动作，不能在 collector 启动前通过 `target.start_url` 隐式导航。

一次 replay 的基本顺序：

```text
validate source experiment and evidence
→ build replay plan from exact snapshot
→ start experiment capture
→ run setup_flow
→ align page and record pre-dispatch environment
→ dispatch browser-context request
→ correlate exact outbound request
→ collect response and verification evidence
→ write comparison and terminal manifest
```

GPT 不直接协调 collector 生命周期，也不能让 replay 接受任意本地文件路径。

### 5.3 Deadline、取消与清理

每个实验只有一个绝对执行 deadline。Adapter 不能在每个阶段重新获得完整 timeout。

清理优先级固定为：

```text
stop collector
→ stop Trace
→ write terminal manifest
→ release reservation
```

Playwright、MCP、PowerShell 和 ripgrep 的 timeout、取消与 shutdown 都必须终止 Windows 进程树。已发送到浏览器或远端站点的副作用不保证可回滚；发生在执行型 step 内的取消应记录为 `canceled_outcome_unknown`，不得自动重试。

MCP side-effect 调用发生 timeout 或 cancellation 时，旧 transport generation 必须失效，下一次调用建立新 generation；副作用调用不得自动重试。

---

## 6. 证据与完整性模型

### 6.1 证据优先于摘要

Action 返回有界摘要，完整内容保存在：

```text
data/analysis-workspace/
  sessions/
  experiments/<experiment_id>/
    manifest.json
    playwright/
    js-reverse/
    replay/
    reports/
    derived/
  reports/
  scripts/
  notes/
```

原始证据路径由后端管理并视为只读：

```text
sessions/
experiments/*/manifest.json
experiments/*/js-reverse/
experiments/*/playwright/
```

Workspace 写入只允许派生目录。实验运行期间，禁止修改该实验目录，并暂停会与 collector 竞争的 PowerShell mutation。

核心引用使用：

```text
experiment_id + evidence_id + artifact_id
```

临时 reqid、数字 capture ID 或目录顺序不能作为跨实验主键。

### 6.2 目标完整性契约

下一版必须把“执行是否完整”和“是否足以做因果推断”拆开：

```text
execution_integrity:
  complete | partial | failed

evidence_integrity:
  complete | partial | failed

causal_comparability:
  observed_equivalent | different | insufficient | not_applicable

inference_eligibility:
  eligible | supervised_only | ineligible
```

含义：

- `execution_integrity`：请求是否实际发送，capture、cleanup 和 manifest 是否完成；
- `evidence_integrity`：目标所需 request/response/stream/artifact 是否完整；
- `causal_comparability`：Control 与 Treatment 的必需环境和非目标字段是否可比较；
- `inference_eligibility`：后端是否具备自动因果推断资格。

示例：请求、抓包和 artifact 全部完成，但站点当前节点不可观察：

```text
execution_integrity = complete
evidence_integrity = complete
causal_comparability = insufficient
inference_eligibility = supervised_only
```

这不应再被压成整体 `partial`。

当前 `objective_integrity` 在迁移期保留为兼容字段，但不再作为唯一真相。完成迁移后，它只可作为上述维度的摘要，不能反向覆盖具体维度。

### 6.3 Primary 与 supporting evidence

每次实验显式声明 primary matcher 和预期数量。Supporting request 可以用于诊断，但不能满足 primary objective。

等待条件必须基于 request-state checkpoint，而不是只比较 collector version。Raw 与 semantic 事件使用独立游标。分页必须完整遍历后再锁定具体 request ID。

---

## 7. 通用 Capture 目标契约

当前 capture 已能完成 JSON/SSE 人工监督实验。下一版需要去除 SSE 专用默认值，把响应方式建模为通用协议事实。

### 7.1 响应模式

目标模型：

```text
response_mode:
  auto | ordinary | sse | ndjson | raw_stream | websocket
```

- `auto` 根据实际网络证据选择采集方式，但不能仅凭一个 Content-Type 推断全部语义；
- `ordinary` 保存普通完整响应；
- `sse` 解析 SSE event，同时保留 raw bytes；
- `ndjson` 按换行 JSON 记录解析结果与原始边界；
- `raw_stream` 只保证有界原始 chunk 和终态；
- `websocket` 作为独立传输类型，不伪装成 fetch stream。

当前尚未完整实现 NDJSON、通用 raw stream 和 WebSocket replay；它们属于目标契约。

### 7.2 终止条件

目标模型：

```text
terminal_conditions:
  exact_sse_data
  event_predicate
  byte_pattern
  network_close
  idle_window
  manual_stop
```

默认 marker 为 `null`。Pandora Skill 可以显式传 `[DONE]`，但后端不能把它当作所有 SSE 的永久约定。

终止结果必须记录：

```text
condition type
matched source
matched request ID
terminal reason
truncated flag
bytes observed
event/chunk range
```

### 7.3 Primary request 过滤

`mime_types=[]` 表示不使用 MIME 过滤。没有 Content-Type、204、下载、异常错误响应或旧接口都必须允许 capture/replay。

URL、method、resource type、MIME 和 in-flight 过滤都是可选 selector，不应因为某个可观察维度缺失而产生模型校验错误。

---

## 8. 通用 Replay 目标契约

### 8.1 三种模式

目标模型：

```text
replay_mode = control
  无 mutation，建立可工作的基线和不可变 pair protocol

replay_mode = treatment
  恰好一个 mutation，用于严格因果比较

replay_mode = exploratory
  允许 0..N mutations，用于寻找新版协议的可工作请求
  inference_eligibility 固定为 ineligible
```

探索模式解决“新版同时增加字段、删除旧字段、更新 header 或 query 才能工作”的情况。找到可工作请求后，再建立严格 Control/Treatment 验证单变量。

### 8.2 Mutation

目标 mutation 使用受限 RFC 6902 风格语义：

```text
add | remove | replace
```

适用目标：

```text
JSON Pointer
header occurrence
query occurrence
```

JSON add 支持普通 pointer 和数组 `/-`，继续禁止 wildcard。严格 Treatment 只允许一个 operation；Exploratory 可允许多个 operation。

重复 header/query 必须声明 occurrence：

```text
first | last | all | index
```

`all` 保留完整有序值列表。不能把重复值无声压缩成一个值。

### 8.3 Setup 输出与动态绑定

`setup_flow` 不能只制造页面副作用，还需要受控输出绑定：

```text
setup_outputs:
  - binding_id
    source: network_response_json | page_json | selector_value
    selector
    pointer
```

Replay binding 可以引用 setup 输出，将新 conversation ID、current node、CSRF 值或页面 nonce 注入请求。该能力必须通过声明式 selector 实现，不开放任意 JavaScript。

现有 `generated` 与 `preserve_source` binding 继续保留，并扩展 occurrence 语义。

### 8.4 Replay request 精确关联

候选 outbound request 必须同时满足：

```text
actual method == replay spec method
normalized actual URL == replay spec URL
full mutated query matches
body fingerprint matches（有 body 时）
dispatch time is inside bounded window
```

零个或多个候选都 fail closed。GET、HEAD 或无 body 请求不能仅靠 selector ID 和时间窗口关联。

URL normalization 只能处理明确等价的编码形式，不能丢弃 query 顺序或参数重复。

### 8.5 非目标字段比较

默认保留 wire 顺序：

```text
normalization:
  query_order: preserve | ignore
  header_order: preserve | ignore
```

默认值均为 `preserve`。只有 Skill 明确知道站点语义与顺序无关时才使用 `ignore`。

比较结果必须分别记录：

```text
target delta
volatile binding effectiveness
non-target body equivalence
non-target header equivalence
non-target query equivalence
transport semantics equivalence
```

### 8.6 环境要求

Control 声明环境维度：

```text
environment_requirements:
  required:
    - page_origin
    - request_context
  advisory:
    - page_url
    - conversation_current_node
    - critical_bundle_sha256
```

默认 required 只包含后端能稳定观察的维度。Current node、bundle hash 和站点新增状态标识不应永久硬编码为必需维度。Skill 可以按站点和实验目的提升某个维度。

请求上下文 header 支持配置：

```text
context_header_names
```

默认覆盖 Cookie、Authorization、CSRF/XSRF；Skill 可增加 `X-API-Key`、设备 token、attestation 或 challenge header。Manifest 只保存本地 SHA-256 和完整性状态，不保存原值。

### 8.7 Fetch 传输语义

Replay 必须显式记录实际 transport 语义：

```text
replay_transport_semantics:
  credentials
  redirect
  cache
  referrer_policy
  keepalive
  mode
  priority
  source_fetch_options_known
```

当前固定 `credentials=include`、`redirect=follow` 的行为必须在 manifest 中如实说明。后续只开放少量安全、可验证的 fetch options。Wire 字段等价不能自动解释为前端请求行为完全等价。

### 8.8 中性响应观察

后端不再直接把单次响应定性为 `required`、`candidate_optional` 或 `constrained_value`。目标返回：

```text
observations:
  http_status
  response_content_type
  mutation_effective
  target_reference_strength
  raw_validation_path
  normalized_validation_path
  structured_error_code
  response_contract_match
  stream_terminal_reason

inference_hints:
  validation_like
  conflict_like
  authentication_like
  rate_limit_like
  server_failure_like
  redirect_like
  success_like
  signals_conflict
```

固定 HTTP 状态或 validation code 集可以作为可替换 hint profile，但不是后端最终事实。

精确 JSON Pointer 按原样保留。只有框架式 location 数组才允许按可配置规则去掉 transport wrapper。`missing` 与 `not_required` 等相反信号同时出现时，必须记录 `signals_conflict=true`，交给 GPT/Skill 判断。

---

## 9. 凭据、安全与数据边界

完整 request/response headers 可能包含 Cookie、Authorization、CSRF 和 Set-Cookie。系统遵守：

- 默认只返回 redacted summary、shape 和 hash；
- credential artifact 正文默认对 inspect/search/read 隐藏；
- replay 可在后端内部使用凭据，但不把凭据放入 Skill、Action 响应或报告；
- 原始 evidence 不允许被 workspace write/patch 改写；
- 大型 binary、Base64、raw stream 不通过 Action JSON 返回；
- PowerShell 网络和危险命令检查是本地策略，不宣称为安全沙箱；
- 所有实验只针对用户有权访问的账号和站点。

Browser-managed Cookie、Origin、Referer、Host、Content-Length 和 `Sec-*` header 默认禁止 mutation。若未来开放例外，必须是明确、受控且可审计的专用能力。

---

## 10. 实施路线

### P0：解除旧协议假设，修正事实模型

1. 拆分 `execution_integrity`、`evidence_integrity`、`causal_comparability`、`inference_eligibility`，保留 `objective_integrity` 兼容映射。
2. 将环境 required/advisory 变为 Control 可声明配置，不再永久要求 current node 和 bundle hash。
3. 将 replay response 改为 observations + inference hints，移除后端固定 required/optional 结论。
4. 引入通用 `response_mode` 与 `terminal_conditions`；默认 done marker 改为 `null`。
5. 允许 `PrimaryRequest.mime_types=[]`，修复无 Content-Type source replay。
6. Replay request 关联强制比较实际 method、完整 URL/query、body fingerprint 和 dispatch window。

P0 完成后，现有 JSON/SSE 能力必须保持兼容，且“完整执行但证据不足”不再被误报为执行失败。

### P1：增强未知协议探索与成对实验可信度

1. 非目标比较默认保留 header/query wire 顺序，并提供显式 normalization。
2. 增加 `setup_outputs`，允许 setup 产生动态 binding。
3. 增加 `exploratory` replay。
4. Mutation 增加结构化 `add`。
5. JSON Pointer 与框架 location 分开规范化，同时保存 raw/normalized path。
6. 冲突 validation 信号不再强制覆盖为 required。
7. 重复 header/query binding 与 mutation 支持 occurrence。
8. Manifest 明确保存 fetch transport semantics，并开放受限安全配置。

### P2：扩展协议覆盖与回归门禁

1. 增加 `context_header_names`。
2. 完成 NDJSON、raw chunked 和无 marker/仅 network close 的 stream 场景。
3. 评估 WebSocket capture/replay 的独立契约，不复用 fetch/SSE 模型。
4. 增加以下回归测试：

```text
source response 无 Content-Type
GET replay 与同窗口后台 GET 共存
header/query 顺序变化但值相同
top-level JSON 字段名为 body
missing 与 not_required 冲突
setup 创建 conversation 并注入 ID
NDJSON/raw chunked response
SSE 无 [DONE]、仅 network close
重复 header/query preserve_source
exploratory replay 同时 add/remove
```

P2 不要求一次完成所有站点类型。每个新增传输模式必须先定义证据、终态、完整性和取消语义，再进入公开 OpenAPI。

---

## 11. 验证策略

### 11.1 自动测试

每次修改至少运行：

```text
python -m pytest
```

涉及模型或 OpenAPI 时增加 schema 断言；涉及比较、路径和响应分类时运行 protocol evidence 定向测试；涉及 browser lifecycle 时运行 browser action 测试。

测试数量会随实现变化，文档不固定某个永久计数。PR 应报告实际执行命令和当次结果。

### 11.2 真实运行门禁

真实 Windows smoke 应覆盖：

```text
session open / page alignment
capture before first navigation
ordinary JSON request/response evidence
stream raw bytes and parsed events
initiator and source persistence
control replay baseline
single-mutation treatment
exploratory replay（实现后）
exact outbound request correlation
cancellation and cleanup
workspace evidence inspection
session close and residual process check
```

每个新增响应模式都要验证：

```text
正常终止
idle timeout
byte limit
network close
manual cancel
collector/adapter failure
terminal manifest 可读
```

### 11.3 结论门禁

自动因果推断只在以下条件同时满足时允许：

```text
execution_integrity = complete
evidence_integrity = complete
causal_comparability = observed_equivalent
mutation_effective = true
Control baseline valid
Treatment target delta observed
non-target comparison passed
```

否则保留证据并标记 `supervised_only` 或 `ineligible`，不得丢弃实验，也不得伪装成自动结论。

---

## 12. 完成标准

本轮设计演进完成时应满足：

1. 后端不依赖 Pandora endpoint、字段名、固定错误码或 `[DONE]` 才能工作；
2. GPT 只能调用结构化公开 Action，不能直接控制私有 collector lifecycle；
3. Capture 与 replay 仍由后端原子执行并留下 terminal manifest；
4. 执行完整性、证据完整性、环境可比性和推断资格分别表达；
5. 无 Content-Type、普通响应和非 SSE 流不会在模型层被拒绝；
6. Replay outbound request 以 method、完整 URL/query、body 和时间窗口唯一关联；
7. Control/Treatment 保持严格单变量，Exploratory 明确不具备自动推断资格；
8. Backend 返回观察与提示，Skill 负责跨实验业务结论；
9. Header/query 顺序和重复值默认按 wire 保留；
10. Setup 可通过受控 binding 把动态状态传入 replay；
11. Fetch transport semantics 在 manifest 中可见；
12. 原始证据只读，凭据默认隐藏，大型数据不进入 Action JSON；
13. 所有新增能力都有模型、回归测试和真实运行门禁；
14. README、PANDORA_REPRODUCTION.md、OpenAPI 与实现不存在互相矛盾的现状声明。

达到这些标准后，`web_rev_action` 才是一个适合网页版 GPT Action 持续分析未知新版协议的通用实验后端，而不是带有旧 Pandora 假设的专用复刻工具。
