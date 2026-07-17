# Pandora 协议分析参考案例

本文档是一个**可选的站点分析案例**，用于说明如何组合当前仓库提供的原子 Browser Action 能力。它不是后端固定流程，也不是任何网站都必须执行的场景清单。

真正的执行合同以以下内容为准：

- `current-site-analysis`：选择问题、证据和最小实验；
- `browser-action-protocol`：提供每个 operation 的当前请求结构；
- `capture_flow`：在一个浏览器 session 中捕获页面、普通网络、stream、console 和可选 trace 事实；
- `inspectBrowserEvidence`：读取实验、网络证据、initiator、脚本、console 和 stream 状态；
- `replay_request`：从一个明确的 `network_request` evidence 生成浏览器上下文 replay；
- Workspace：读取已保存 artifact，并写派生报告或代码。

Pandora 只在当前页面确实呈现会话树、继续、重新生成、编辑、停止或重新加载等行为时，作为实验设计参考使用。没有证据时不得假定这些语义存在。

## 1. 先建立 session，不预设业务场景

先调用 `get_session` 检查准备使用的 ID。仅当 session 不存在、已关闭，或确认未发送过 attach 时才调用 `open_session`。

```json operation=open_session
{
  "session_id": "pandora-analysis",
  "target": {
    "expected_url_contains": "example.test"
  },
  "deadline_ms": 15000
}
```

记录当前页面 URL、标题、页面 ID、用户可见状态，以及本轮要回答的一个具体问题。不要把历史抓包、产品名称或旧 fixture 当作当前页面事实。

## 2. 最小基线捕获

基线实验的目的不是证明所有业务规则，而是建立可引用的证据清单：

- 触发动作和 UI 结果；
- 请求 URL、方法、resource type 和 MIME；
- exact network evidence ID；
- stream 是否存在、如何结束；
- console error/warning；
- request initiator 和相关脚本；
- 证据完整性和未观察到的维度。

下面的 payload 是一个当前模型可接受的示例。实际 locator、endpoint 和 MIME 必须来自当前页面。

```json operation=capture_flow
{
  "session_id": "pandora-analysis",
  "objective": "capture one current request and its visible result",
  "primary_request": {
    "url_contains": "/api/resource",
    "method": "POST",
    "resource_types": ["fetch"],
    "mime_types": ["application/json"],
    "expected_min_matches": 1,
    "expected_max_matches": 3,
    "allow_supporting_failures": true,
    "include_in_flight": false
  },
  "flow": [
    {
      "step_id": "submit_current_action",
      "action": "click",
      "locator": {
        "css": "button[type='submit']"
      },
      "timeout_ms": 10000
    }
  ],
  "execution_mode": "job",
  "deadline_ms": 42000,
  "job_timeout_ms": 300000,
  "capture": {
    "network": true,
    "stream": false,
    "trace": true,
    "screenshots": false,
    "page_snapshots": true,
    "console_errors": true
  },
  "requirements": {
    "require_raw_capture": false,
    "require_semantic_parse": false,
    "require_request_snapshot": true,
    "require_artifacts": true
  },
  "network_evidence": [
    {
      "selector_id": "current_primary_request",
      "matcher": {
        "url_contains": "/api/resource",
        "method": "POST",
        "resource_types": ["fetch"],
        "mime_types": ["application/json"]
      },
      "max_matches": 3,
      "export_parts": ["all"],
      "include_initiator": true,
      "include_cookie_provenance": false,
      "cookie_names": []
    }
  ],
  "series": {
    "analysis_series_id": "pandora-current-analysis",
    "scenario_type": "observed-baseline",
    "sequence_index": 0
  }
}
```

Job 模式返回 `running` 时，使用 `get_experiment` 轮询到终态。引用请求时使用 `evidence_id` 或 canonical observation ID，不要用“第一个相似 URL”代替精确选择。

## 3. 证据检查顺序

对一个候选请求，按问题需要选择最少的 inspect 操作：

1. `list_evidence`：确认 exact evidence ID；
2. `get_network_evidence`：读取公开摘要、完整性和 artifact 路径；
3. `get_request_shape`：读取脱敏后的请求结构；
4. `get_request_initiator`：判断是否有已捕获的脚本 initiator；
5. `search_scripts`：以函数名、endpoint 或代码文字定位 source；
6. `get_script_source`：读取小范围 source；
7. `save_script_source`：将需要引用的 JavaScript 范围保存为实验 evidence；
8. `list_console_errors`：检查本实验 checkpoint 之后的新 warning/error；
9. `get_stream_status`：仅在存在 stream capture 时检查 raw/semantic/terminal 事实。

普通网络 artifact、initiator、source、stream 和 UI 状态是不同证据维度。某一维度缺失时要保留 `missing` 或 `ambiguous`，不要由其他维度补写结论。

## 4. Source tracing

Source tracing 从 exact request evidence 开始：

```text
network evidence ID
→ request initiator
→ source URL / script ID / line
→ bounded source read
→ optional saved source evidence
→ hypothesis about one field or branch
```

`initiator=null` 是真实结果，表示当前 retained request 没有可用 initiator；它通常要求在 initiator capture 已启用后重新触发动作。它不是协议损坏。

大型或压缩 JavaScript 应先用 offset/length 读取 bounded preview，再决定是否保存完整 source。WASM 元数据不能当作 JavaScript 文本保存；需要真实字节时应使用固定 fork 提供的完整 source 保存能力并写入 `.wasm` artifact。

## 5. 只在有问题需要回答时 replay

Replay source 必须包含一个实验 ID 和一个明确的 network evidence ID。修改一个字段、header 或 query 参数时，单独运行一个实验，并把结果与 source 或另一个明确 reference 比较。

```json operation=replay_request
{
  "session_id": "pandora-analysis",
  "objective": "test whether one observed JSON field is required",
  "source": {
    "experiment_id": "exp_source_001",
    "evidence_id": "ev_exp_source_001_network_request_current_primary_request_12"
  },
  "execution_context": "browser_context",
  "mutations": [
    {
      "type": "remove_json_path",
      "path": "/tracking_id"
    }
  ],
  "extractors": [],
  "bindings": [],
  "setup_flow": [],
  "verification_flow": [],
  "execution_mode": "job",
  "deadline_ms": 42000,
  "job_timeout_ms": 300000,
  "query_serialization": "preserve_raw",
  "transport": {
    "credentials": "include",
    "redirect": "follow",
    "cache": "no-store",
    "referrer_policy": "",
    "keepalive": false,
    "mode": "cors",
    "priority": "auto"
  },
  "response_reader": {
    "mode": "auto",
    "max_bytes": 8388608,
    "max_events": 10000,
    "raw_only": false
  },
  "termination": {
    "conditions": [
      {
        "type": "network_close"
      }
    ]
  },
  "capture": {
    "network": true,
    "stream": false,
    "trace": false,
    "screenshots": false,
    "page_snapshots": true,
    "console_errors": true
  }
}
```

在真实 dispatch 前，后端会重新读取 Playwright 当前页面并要求 js-reverse alignment 为 `aligned`。重新对齐失败时，setup/extractor 证据会保留，但 replay 请求不会发送。

不要因一次 2xx、4xx 或 5xx 就宣布字段“必需”或“无效”。至少同时记录：

- HTTP status 和 response Content-Type；
- response body 是否完整；
- stream 是否真正到达 terminal condition；
- UI 或后续请求是否显示持久状态变化；
- source 与 replay 的页面、origin 和 request context 是否可比；
- 是否存在多个同 endpoint 请求造成关联歧义。

## 6. 可选场景如何选择

当当前证据确实显示以下行为时，可以分别设计实验：

- 首次提交：捕获初始请求和第一段结果；
- 后续提交：观察前次 response 值是否进入新 request；
- 重新生成：确认是新请求、参数变化还是仅 UI 行为；
- 编辑后提交：比较 body shape、dynamic identifiers 和持久状态；
- 停止：观察取消分类、stream terminal reason 和服务端状态；
- 重载：验证页面重新加载后的持久状态和请求恢复。

这些只是候选实验。每个实验都必须从当前 evidence 提出一个可证伪问题，不得为了填满场景表而执行。

## 7. 最小闭环

一个可审计的最小闭环是：

```text
检查或打开 session
→ 捕获一个当前动作
→ 选定 exact network evidence
→ 检查 request shape / initiator / bounded source
→ 提出一个小假设
→ 可选地运行一次窄 replay 或 UI 验证
→ 检查终态和完整性
→ 写 observed / derived / hypothesis / gap / next step
```

若 transport 已发送但终态未知，不要重复 consequential operation。使用错误响应中的 session、experiment 和 manifest 句柄检查事实。

## 8. 报告边界

最终报告应区分：

- **Observed**：直接来自 experiment、evidence、artifact 或页面 snapshot；
- **Derived**：从两个明确 evidence reference 得出的比较；
- **Hypothesis**：尚需实验验证的解释；
- **Gap**：当前 operation 或 collector 无法直接观察的 worker、storage、WebSocket frame、paused runtime 或其他维度；
- **Next experiment**：能缩小一个具体 gap 的最小动作。

公开摘要中的 URL 只保留 scheme、host、path、query 参数名称、顺序和重复次数；query 值与 fragment 不得进入公开 manifest 或报告。Cookie、Authorization、CSRF、session、签名和 private response 值也不得复制到聊天、报告或生成代码。

完成标准不是复刻某个产品的全部行为，而是用可追踪 evidence 回答用户当前的问题，并把未知事实留为未知。
