# Web Rev Action 功能实现计划

## 1. 项目定位

`web_rev_action` 是给个人 GPT-5.6 GPT Actions 使用的 Web 逆向执行层。

它不重新实现浏览器自动化、CDP 调试器或文件系统工具，而是把已有工具组合成一套适合 GPT 调用的高层能力：

- `playwright-cli`：页面行为执行器。
- `js-reverse-mcp`：网络、脚本、断点、调用栈和运行时证据采集器。
- `github-gpt-actions-gateway` workspace 工具：本地分析目录、文件读写、搜索、编辑和 PowerShell 7 执行。
- `web_rev_action`：GPT Action 契约、后端原子编排、session 映射、证据索引和结果查询。

`PLAN.md` 只描述本项目要实现的功能。Pandora 类复现方法单独写在根目录 `PANDORA_REPRODUCTION.md`。

---

## 2. 不做什么

本项目不做以下事情：

- 不复制 `playwright-cli` 的页面操作能力。
- 不复制 `js-reverse-mcp` 的 CDP、网络、脚本、断点和调用栈能力。
- 不复制 `github-gpt-actions-gateway` 的 workspace 文件工具。
- 不把两个上游项目的所有原子工具直接暴露给 GPT。
- 不让 GPT 通过多次分散 Action 自己协调“开始抓包 → 点击 → 等待 → 结束抓包”。
- 不实现企业级多租户后台、复杂审计系统、CI/PR 发布流。
- 不把分析 workspace 当 Git 仓库管理。

本项目只实现三件事：

1. 面向 GPT Actions 的简洁操作接口。
2. 对三个已有工具层的后端原子编排。
3. 分析证据的轻量索引、读取和比较。

---

## 3. 总体架构与调用边界

### 3.1 GPT 可见工具

```text
GPT-5.6
  │
  ├── Skill API
  │   ├── retrieveSkillContext
  │   ├── readSkillContent
  │   └── searchSkillDocs
  │
  ├── Browser Actions
  │   ├── inspectBrowserEvidence
  │   └── runBrowserExperiment
  │
  └── Gateway Workspace Tools
      ├── workspaceExecPwsh
      ├── workspaceInspect
      ├── workspaceSearch
      ├── workspaceReadFiles
      ├── workspaceWriteFile
      └── workspaceApplyPatch
```

### 3.2 后端实际编排

一次浏览器实验必须由 `web_rev_action` 后端自己完成核心编排。

```text
GPT
  ↓
runBrowserExperiment
  ↓
web_rev_action 后端
  ├── 调 playwright-cli 执行页面动作
  ├── 调 js-reverse-mcp 抓网络、脚本、断点和调用栈
  └── 写入 data/analysis-workspace/
```

GPT 可以直接使用 Gateway Workspace Tools，但它们只适合实验完成之后：

- 读报告。
- 搜索 artifact。
- 修改 schema。
- 写重放脚本。
- 生成 diff。
- 整理笔记。

不允许把一次 `capture_flow` 拆成 GPT 多步协调，例如：

```text
GPT 调开始抓包
GPT 调页面点击
GPT 调等待
GPT 调结束抓包
```

这种做法容易出现延迟、页面切换、target 错位和证据缺口。正确方式是：一次 `capture_flow` 由后端在同一执行上下文中启动捕获、执行动作、等待条件、收集证据并写入 workspace。

### 3.3 内部结构

```text
web_rev_action
  ├── Action schema
  ├── Orchestrator
  ├── Session registry
  ├── Page alignment checker
  ├── Playwright CLI adapter
  ├── JS Reverse MCP adapter
  ├── Gateway workspace adapter
  ├── Artifact manifest
  ├── Evidence index
  └── Capture diff

Shared Chrome / Chromium
  ├── playwright-cli attaches to CDP endpoint
  └── js-reverse-mcp attaches to same CDP endpoint
```

---

## 4. GPT 可见 Actions

### 4.1 inspectBrowserEvidence

只读 Action。

```yaml
operationId: inspectBrowserEvidence
x-openai-isConsequential: false
```

功能范围：

- 查询 session。
- 查询 experiment。
- 查询请求列表。
- 读取请求详情。
- 读取请求体和响应体。
- 读取请求 initiator。
- 查询 WebSocket 连接和消息。
- 搜索已保存脚本。
- 读取脚本源码。
- 读取 artifact。
- 比较两个 capture。
- 查询 capture health。

不允许做：

- 页面点击、输入、上传。
- 清理站点状态。
- 设置断点。
- HTTP 重放。
- 关闭浏览器 session。

### 4.2 runBrowserExperiment

执行 Action。

```yaml
operationId: runBrowserExperiment
x-openai-isConsequential: false
```

功能范围：

- 打开或连接浏览器 session。
- 导航页面。
- 执行页面动作 flow。
- 开始和停止 Trace。
- 清理当前站点状态。
- 设置和移除 XHR/fetch 断点。
- 重放一个已定义页面流程。
- 执行 workspace 中的 HTTP 重放脚本。
- 关闭浏览器 session。

---

## 5. Action 契约

两个 Action 都使用明确的 `operation/mode + payload` 结构，避免一个请求模型包含大量无关可选字段。

### 5.1 inspectBrowserEvidence 请求

```json
{
  "contract_version": "1.0",
  "mode": "list_requests",
  "session_id": "sess_001",
  "experiment_id": "exp_001",
  "payload": {
    "filter": {
      "url_contains": ["api"],
      "method": "POST"
    }
  },
  "cursor": null,
  "max_items": 50,
  "max_chars": 12000
}
```

`mode` 枚举：

```text
get_session
list_experiments
get_experiment
list_requests
get_request
get_request_body
get_response_body
get_request_initiator
list_websockets
get_websocket_messages
search_scripts
get_script_source
read_artifact
compare_captures
get_capture_health
```

示例：读取 artifact。

```json
{
  "contract_version": "1.0",
  "mode": "read_artifact",
  "session_id": "sess_001",
  "experiment_id": "exp_001",
  "payload": {
    "artifact_id": "art_001",
    "cursor": null,
    "max_chars": 12000
  }
}
```

### 5.2 runBrowserExperiment 请求

```json
{
  "contract_version": "1.0",
  "operation": "capture_flow",
  "skill_binding": {
    "skill_id": "web-protocol-analysis",
    "content_hash": "sha256:..."
  },
  "session": {
    "session_id": null,
    "profile_ref": "default",
    "reuse_existing_browser": true
  },
  "payload": {
    "target": {
      "start_url": "https://example.com/app"
    },
    "objective": "capture one browser flow",
    "flow": [],
    "capture": {
      "network": true,
      "websocket": true,
      "console": true,
      "trace": true,
      "source_queries": []
    }
  },
  "limits": {
    "timeout_ms": 30000,
    "max_inline_chars": 12000
  }
}
```

`operation` 枚举：

```text
open_session
capture_baseline
capture_flow
trace_request
reset_and_replay
http_replay
close_session
```

### 5.3 operation payload 示例

`capture_flow`：

```json
{
  "operation": "capture_flow",
  "payload": {
    "target": {
      "start_url": "https://example.com/app"
    },
    "objective": "observe message submission",
    "flow": [],
    "capture": {
      "network": true,
      "websocket": true,
      "console": true,
      "trace": true,
      "source_queries": ["conversation", "parent_message_id"]
    }
  }
}
```

`trace_request`：

```json
{
  "operation": "trace_request",
  "payload": {
    "request_match": {
      "url_contains": "/conversation",
      "method": "POST"
    },
    "trigger_flow": [],
    "pause_mode": "xhr_fetch",
    "capture_scope": [
      "stack",
      "locals",
      "request_body"
    ]
  }
}
```

`http_replay`：

```json
{
  "operation": "http_replay",
  "payload": {
    "mode": "browser_context",
    "request_artifact_id": "art_req_001",
    "mutation_script_ref": "scripts/replay-first-message.py",
    "output_dir": "experiments/exp_010/replay"
  }
}
```

---

## 6. Flow step 契约

`flow` 是后端 Orchestrator 的输入，不是 adapter 私有格式。每个 adapter 必须按同一份 step schema 解释。

### 6.1 通用 step 结构

```json
{
  "step_id": "send_message",
  "action": "fill",
  "locator": {
    "placeholder": "Message"
  },
  "value": "hello",
  "timeout_ms": 10000
}
```

字段说明：

```text
step_id      必填。实验内唯一，便于结果和错误定位。
action       必填。动作类型。
locator      可选。需要元素定位的动作使用。
value        可选。fill/type/select/upload 等动作使用。
key          可选。press 动作使用。
condition    可选。wait/assert 动作使用。
timeout_ms   可选。单步超时。
```

### 6.2 click

```json
{
  "step_id": "click_send",
  "action": "click",
  "locator": {
    "role": "button",
    "name": "Send"
  },
  "timeout_ms": 10000
}
```

### 6.3 fill

```json
{
  "step_id": "fill_message",
  "action": "fill",
  "locator": {
    "placeholder": "Message"
  },
  "value": "hello"
}
```

### 6.4 press

```json
{
  "step_id": "press_enter",
  "action": "press",
  "locator": {
    "placeholder": "Message"
  },
  "key": "Enter"
}
```

### 6.5 wait

```json
{
  "step_id": "wait_conversation_request",
  "action": "wait",
  "condition": {
    "type": "request",
    "url_contains": "/conversation",
    "method": "POST"
  },
  "timeout_ms": 30000
}
```

可支持的 wait condition：

```text
timeout
selector_visible
selector_hidden
request
response
network_idle
stream_event
stream_end
page_url
```

### 6.6 assert

```json
{
  "step_id": "assert_response_started",
  "action": "assert",
  "condition": {
    "type": "request_observed",
    "url_contains": "/conversation",
    "method": "POST"
  }
}
```

### 6.7 step result

每一步返回统一结构：

```json
{
  "step_id": "click_send",
  "action": "click",
  "status": "completed",
  "started_at": "2026-07-11T00:00:00Z",
  "ended_at": "2026-07-11T00:00:01Z",
  "snapshot_ref": "art_snapshot_after_click",
  "matched_locator_count": 1,
  "warnings": []
}
```

失败时：

```json
{
  "step_id": "wait_conversation_request",
  "action": "wait",
  "status": "failed",
  "error": {
    "code": "TIMEOUT",
    "message": "request was not observed before timeout"
  },
  "warnings": []
}
```

---

## 7. 上游工具 Adapter

### 7.1 PlaywrightCliAdapter

职责：把 `runBrowserExperiment.flow` 转换成 `playwright-cli` 调用。

需要实现：

- attach/open session。
- goto/reload。
- snapshot/find。
- click/fill/type/press/select/check/hover/upload。
- wait/assert。
- tracing start/stop。
- screenshot。
- session close。
- 输出解析和错误归一化。

不得实现：

- 自研 locator 引擎。
- 自研浏览器自动化。
- 任意 shell 命令拼接。

### 7.2 JsReverseMcpAdapter

职责：以 MCP client 方式调用 `js-reverse-mcp`。

需要实现：

- select page/frame。
- clear/list network requests。
- export request/response body。
- get request initiator。
- list WebSocket messages。
- list/search/read scripts。
- break on XHR/fetch。
- get paused info。
- step/resume/remove breakpoint。
- list console messages。
- clear site data。

不得实现：

- 自研 CDP Network collector。
- 自研 Debugger controller。
- 自研 Runtime inspector。

### 7.3 GatewayWorkspaceAdapter

职责：复用 `github-gpt-actions-gateway` 的 workspace 工具。

需要暴露：

- `workspaceExecPwsh`
- `workspaceInspect`
- `workspaceSearch`
- `workspaceReadFiles`
- `workspaceWriteFile`
- `workspaceApplyPatch`

不暴露：

- Git commit / push。
- PR 创建 / 更新 / 合并。
- CI 查询。
- workflow dispatch。
- GitHub artifact 同步。

workspace 是普通分析目录，不是 Git 发布目录。

---

## 8. Session、共享浏览器与页面对齐

两个上游工具必须连接同一个 Chrome / Chromium CDP endpoint，但“同一个 endpoint”不等于“同一个页面”。

```text
Chrome / Chromium
  ├── playwright-cli attach --cdp=<endpoint>
  └── js-reverse-mcp --browserUrl <endpoint>
```

`web_rev_action` 维护逻辑 session：

```text
session_id
profile_ref
browser_endpoint_ref
playwright_session_ref
playwright_page_url
playwright_page_title
playwright_page_index
js_reverse_session_ref
js_reverse_target_id
js_reverse_frame_id
js_reverse_page_url
selected_frame
page_alignment_status
analysis_workspace_dir
created_at
updated_at
expires_at
```

每次 `capture_baseline`、`capture_flow`、`trace_request` 前必须执行页面对齐检查：

```text
Playwright 当前页面
  ↕
JS Reverse 当前 target/frame
```

`page_alignment_status` 计算规则：

```text
aligned
  Playwright 当前 page URL 与 js-reverse 当前 target URL 相同或通过规范化后相同，
  且 page title 一致或 js-reverse target 可确认属于该 page。

frame_mismatch
  page URL 一致，但 js-reverse selected frame 与预期 frame 不一致。

page_mismatch
  CDP endpoint 一致，但 Playwright 当前页面和 js-reverse 当前 target 指向不同 tab/page。

target_missing
  js-reverse 找不到与 Playwright 当前页面匹配的 target。

unknown
  任一侧无法返回足够元数据。
```

如果状态不是 `aligned`，Orchestrator 必须先尝试让 js-reverse 选择匹配 target/frame；仍失败则停止实验并返回 `capture_health.page_aligned=false`。

个人版只需要三类 profile：

- `default`：日常授权浏览器 profile。
- `clean`：干净测试 profile。
- `temp`：一次性临时 profile。

---

## 9. Artifact 与 Evidence

分析结果默认由 `web_rev_action` 自己写入本地目录：

```text
data/analysis-workspace/
  experiments/
    exp_001/
      manifest.json
      playwright/
        snapshot-before.yml
        snapshot-after.yml
        trace.zip
        screenshot.png
      js-reverse/
        requests/
        websocket/
        scripts/
        paused/
      reports/
        summary.md
        summary.json
```

Gateway workspace 工具只在 GPT 需要做复杂文件操作时接入，例如读取、搜索、修改、运行 diff 脚本、写 schema 或整理笔记。

ID 规则：

```text
sess_001
exp_001
cap_001
ev_req_001
ev_resp_001
ev_stack_001
ev_ws_001
ev_script_001
art_001
```

`manifest.json` 最少保存：

```json
{
  "experiment_id": "exp_001",
  "operation": "capture_flow",
  "objective": "...",
  "created_at": "...",
  "actions": [],
  "requests": [],
  "websockets": [],
  "scripts": [],
  "stream_events": [],
  "artifacts": [],
  "warnings": [],
  "errors": []
}
```

Action 响应只返回摘要、ID、hash、preview 和 cursor。大文件通过 `inspectBrowserEvidence.read_artifact` 或 workspace 工具读取。

---

## 10. Capture Health

每次实验返回 `capture_health`：

```json
{
  "network_capture_started_before_action": true,
  "trace_started_before_action": true,
  "websocket_observed": false,
  "page_aligned": true,
  "page_alignment_status": "aligned",
  "frame_aligned": true,
  "breakpoints_removed": true,
  "paused_execution_resumed": true,
  "response_bodies_available": true,
  "sse_event_sequence_available": true,
  "sse_end_marker_observed": true,
  "stream_capture_complete": true,
  "warnings": []
}
```

这不是复杂安全系统，而是避免 GPT 把“不完整抓取”误认为“事实不存在”。

---

## 11. SSE 与 Raw Stream Capture 要求

Pandora 类核心消息接口通常是流式响应。因此 SSE 能力不是后期附加项，必须在阶段 0 验证。

### 11.1 最低要求

如果 `js-reverse-mcp` 的普通 response body 导出能完整保留以下 SSE 事件序列，则足够做协议语义分析：

```text
data: {...}

data: {...}

data: [DONE]
```

最低要求必须能拿到：

- 每条 `data` 事件。
- 事件顺序。
- 结束标记。
- 错误事件。
- 取消后的最终结果。

### 11.2 Raw CDP Stream Capture 触发条件

如果普通 response body 导出不能完整保留 SSE 事件序列，则 Raw CDP Stream Capture 立即升级为核心依赖。

即使普通导出足够，也只有在分析以下行为时才必须使用 Raw CDP Stream Capture：

- 每个 chunk 的到达时间。
- stop-generation。
- 网络中断。
- 未正常结束的流。
- chunk 边界。
- 心跳。
- 工具调用增量。

优先补到 `js-reverse-mcp`：

```text
start_stream_capture
get_stream_chunks
stop_stream_capture
export_stream_capture
```

至少记录：

```text
requestId
sequence
timestamp
encodedDataLength
dataLength
mimeType
url
loadingFinished/loadingFailed
```

---

## 12. Diff 能力

`web_rev_action` 只做轻量 diff，不做完整协议推理。

需要支持：

- 请求集合 diff。
- URL / method / status diff。
- Header 名称 diff。
- JSON 请求体字段 diff。
- JSON 响应字段 diff。
- SSE 事件序列 diff。
- WebSocket 消息序列 diff。
- initiator 脚本位置 diff。
- Artifact hash diff。

动态值识别：

```text
timestamp
uuid
nonce
request_id
message_id
conversation_id
trace_id
build_hash
session_id
```

diff 结果写入 workspace，例如：

```text
data/analysis-workspace/experiments/exp_002/reports/diff.json
```

---

## 13. Worker / Service Worker Target 元数据

Worker / Service Worker 不是 Pandora 核心路径的前置依赖，但现代页面可能会通过它们发请求。`web_rev_action` 需要先在阶段 0 验证现有工具能否识别 target 元数据。

优先补到 `js-reverse-mcp` 的 request 结果：

```json
{
  "targetType": "page | iframe | worker | service_worker | shared_worker",
  "targetId": "...",
  "frameId": "...",
  "workerUrl": "..."
}
```

只有遇到以下情况时，复现流程才需要深入 Worker / Service Worker 诊断：

- initiator 为空。
- 主页面脚本中找不到请求。
- 请求看起来被 Service Worker 转发。
- 页面操作与请求时间对应，但 frame 中不存在发起栈。

---

## 14. 代码结构

```text
src/skill_temple/
  app.py
  runtime.py

src/web_rev_action/
  actions.py
  models.py
  orchestrator.py
  sessions.py
  alignment.py
  artifacts.py
  evidence.py
  diff.py
  adapters/
    playwright_cli.py
    js_reverse_mcp.py
    gateway_workspace.py

skills/
  web-protocol-analysis/
    SKILL.md
    docs/
      action-contract.md
      browser-experiment.md
      network-analysis.md
      stream-analysis.md
      source-tracing.md
      request-replay.md
      evidence-report.md

data/analysis-workspace/
  captures/
  experiments/
  schemas/
  scripts/
  reports/
  notes/
```

MVP 不做包名大迁移，避免把模板重命名和功能实现混在一起。

---

## 15. 开发阶段

### 阶段 0：工具链验证

只验证现有工具链是否足够支撑最小闭环，不实现完整 Action。共享 CDP endpoint 作为已确认前置条件，不放入阶段 0 范围；阶段 0 只验证同一 endpoint 下的页面对齐和证据完整性。

必须验证：

- Playwright 当前页面与 js-reverse 当前 target 对齐。
- 网络请求抓取。
- 请求体和响应体导出。
- SSE 事件序列是否完整保留。
- request initiator。
- 脚本读取和搜索。
- XHR/fetch 断点暂停与恢复。
- workspace 写文件。

输出：

```text
data/analysis-workspace/reports/toolchain-validation.md
```

如果阶段 0 证明普通响应导出无法完整保留 SSE 事件序列，则 Raw CDP Stream Capture 立即进入阶段 1 的核心实现范围。

### 阶段 1：Action 契约

- 定义 `inspectBrowserEvidence` schema。
- 定义 `runBrowserExperiment` schema。
- 使用 `operation/mode + payload` 分支结构。
- 定义 flow step schema。
- 定义 step result schema。
- 加入 OpenAPI。
- Fake adapter 返回固定数据。
- 补契约测试。

### 阶段 2：最小 Orchestrator

只支持：

- `open_session`
- `capture_baseline`
- `capture_flow`
- `close_session`

同时实现：

- 共享 CDP session 映射。
- 页面对齐检查。
- 后端原子编排。
- Capture Health。
- Artifact manifest。

### 阶段 3：Pandora 最小闭环

用真实页面或测试页面完成：

- baseline。
- 首次消息。
- 第二轮消息。
- 重新生成。

必须能查询：

- 请求体。
- SSE 响应流或完整 SSE 事件序列。
- initiator。
- 相关脚本。

### 阶段 4：Evidence 与 diff

- 生成 evidence ID。
- 生成 artifact ID。
- 实现 artifact 查询。
- 实现 capture diff。
- 比较请求体、SSE 事件序列和 initiator。

### 阶段 5：请求追踪

- 实现 `trace_request`。
- 支持 XHR/fetch breakpoint。
- 保存 paused stack。
- 保存 locals 摘要。
- 确保 resume 和 breakpoint 清理。

### 阶段 6：重放

先实现：

- browser-context replay。

再实现：

- external HTTP replay。

### 阶段 7：扩展能力

最后增加：

- 编辑旧消息。
- 停止生成。
- 文件上传。
- 网页搜索。
- 图片。
- 工具调用。
- Worker / Service Worker 深入诊断。

### 阶段 8：Skill 文档

- 新增 `web-protocol-analysis` Skill。
- 写 Action 使用说明。
- 写证据报告格式。
- 写 request replay 的 workspace 用法。

---

## 16. MVP 完成标准

1. GPT 可见浏览器 Action 只有两个：`inspectBrowserEvidence` 和 `runBrowserExperiment`。
2. 只读查询不触发 consequential 确认。
3. 页面交互触发 consequential 确认。
4. `playwright-cli` 和 `js-reverse-mcp` 连接同一个浏览器。
5. 每次实验前能确认 Playwright 当前页面与 js-reverse 当前 target 对齐。
6. `web_rev_action` 不重复实现 Playwright 或 CDP collector。
7. `capture_flow` 由后端原子编排，不依赖 GPT 分步协调。
8. 能执行 `capture_baseline` 和 `capture_flow`。
9. 能保存请求、响应、initiator、WebSocket、脚本搜索结果、Trace 和截图。
10. 能保留 Pandora 类消息接口的完整 SSE 事件序列，至少包括每条 `data` 事件、顺序、结束标记、错误事件和取消后的结果。
11. 如果普通响应导出无法满足第 10 条，Raw CDP Stream Capture 已作为核心依赖补齐。
12. 能通过 evidence ID 和 artifact ID 查询证据。
13. 能用 gateway workspace 工具写脚本、schema、diff、报告和 notes。
14. 能比较两个 capture。
15. 能报告 capture health。
16. 能验证 Worker / Service Worker target metadata 是否足够；不足时有明确上游补丁计划。
17. 不把 Cookie、Token、profile 路径、CDP endpoint 明文写进报告或共享文件。
