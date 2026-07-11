# Web Rev Action 功能实现计划

## 1. 项目定位

`web_rev_action` 是给个人 GPT-5.6 GPT Actions 使用的 Web 逆向执行层。

它不重新实现浏览器自动化、CDP 调试器或文件系统工具，而是把已有工具组合成一套适合 GPT 调用的高层能力：

- `playwright-cli`：页面行为执行器。
- `js-reverse-mcp`：网络、脚本、断点、调用栈和运行时证据采集器。
- `github-gpt-actions-gateway` workspace 工具：本地分析目录、文件读写、搜索、编辑和 PowerShell 7 执行。
- `web_rev_action`：GPT Action 契约、工具编排、session 映射、证据索引和结果查询。

`PLAN.md` 只描述本项目要实现的功能。Pandora 类复现方法单独写在根目录 `PANDORA_REPRODUCTION.md`。

---

## 2. 不做什么

本项目不做以下事情：

- 不复制 `playwright-cli` 的页面操作能力。
- 不复制 `js-reverse-mcp` 的 CDP、网络、脚本、断点和调用栈能力。
- 不复制 `github-gpt-actions-gateway` 的 workspace 文件工具。
- 不把两个上游项目的所有原子工具直接暴露给 GPT。
- 不实现企业级多租户后台、复杂审计系统、CI/PR 发布流。
- 不把分析 workspace 当 Git 仓库管理。

本项目只实现三件事：

1. 面向 GPT Actions 的简洁操作接口。
2. 对三个已有工具层的组合编排。
3. 分析证据的轻量索引、读取和比较。

---

## 3. 总体架构

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

web_rev_action
  ├── Action schema
  ├── Orchestrator
  ├── Session registry
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
x-openai-isConsequential: true
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

### 5.1 inspectBrowserEvidence 请求

```json
{
  "contract_version": "1.0",
  "mode": "list_requests",
  "session_id": "sess_001",
  "experiment_id": "exp_001",
  "filter": {
    "url_contains": ["api"],
    "method": "POST"
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

页面动作 `flow` 支持：

```text
navigate
reload
snapshot
find
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
```

Locator 支持：

```text
snapshot_ref
role
label
placeholder
test_id
text
css
```

---

## 6. 上游工具 Adapter

### 6.1 PlaywrightCliAdapter

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

### 6.2 JsReverseMcpAdapter

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

### 6.3 GatewayWorkspaceAdapter

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

## 7. Session 与共享浏览器

两个上游工具必须连接同一个 Chrome / Chromium CDP endpoint。

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
js_reverse_session_ref
selected_page
selected_frame
analysis_workspace_dir
created_at
updated_at
expires_at
```

个人版只需要三类 profile：

- `default`：日常授权浏览器 profile。
- `clean`：干净测试 profile。
- `temp`：一次性临时 profile。

---

## 8. Artifact 与 Evidence

分析结果存入 workspace。

```text
analysis-workspace/
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
  "artifacts": [],
  "warnings": [],
  "errors": []
}
```

Action 响应只返回摘要、ID、hash、preview 和 cursor。大文件通过 `inspectBrowserEvidence.read_artifact` 或 workspace 工具读取。

---

## 9. Capture Health

每次实验返回 `capture_health`：

```json
{
  "network_capture_started_before_action": true,
  "trace_started_before_action": true,
  "websocket_observed": false,
  "page_aligned": true,
  "frame_aligned": true,
  "breakpoints_removed": true,
  "paused_execution_resumed": true,
  "response_bodies_available": true,
  "stream_capture_complete": null,
  "warnings": []
}
```

这不是复杂安全系统，而是避免 GPT 把“不完整抓取”误认为“事实不存在”。

---

## 10. Diff 能力

`web_rev_action` 只做轻量 diff，不做完整协议推理。

需要支持：

- 请求集合 diff。
- URL / method / status diff。
- Header 名称 diff。
- JSON 请求体字段 diff。
- JSON 响应字段 diff。
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
analysis-workspace/experiments/exp_002/reports/diff.json
```

---

## 11. 需要补到上游的最小能力

先用集成测试验证现有能力。只有确实缺失时才补上游，不在 `web_rev_action` 中复制实现。

### 11.1 Raw CDP Stream Capture

优先补到 `js-reverse-mcp`。

建议工具：

```text
start_stream_capture
get_stream_chunks
stop_stream_capture
export_stream_capture
```

用途：

- SSE。
- chunked fetch。
- 长连接响应。
- 流式增量事件。

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

### 11.2 Worker / Service Worker Target 元数据

优先补到 `js-reverse-mcp` 的 request 结果。

建议字段：

```json
{
  "targetType": "page | iframe | worker | service_worker | shared_worker",
  "targetId": "...",
  "frameId": "...",
  "workerUrl": "..."
}
```

---

## 12. 代码结构

```text
src/skill_temple/
  app.py
  runtime.py

src/web_rev_action/
  actions.py
  models.py
  orchestrator.py
  sessions.py
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

analysis-workspace/
  captures/
  experiments/
  schemas/
  scripts/
  reports/
  notes/
```

MVP 不做包名大迁移，避免把模板重命名和功能实现混在一起。

---

## 13. 开发阶段

### 阶段 1：Action 契约

- 定义 `inspectBrowserEvidence` schema。
- 定义 `runBrowserExperiment` schema。
- 加入 OpenAPI。
- Fake adapter 返回固定数据。
- 补契约测试。

### 阶段 2：Gateway workspace 集成

- 接入 `github-gpt-actions-gateway` workspace 工具。
- 创建 analysis workspace 根目录。
- 实现 artifact 读写、preview、cursor。
- 禁用 Git/PR/CI 类 operation。

### 阶段 3：Playwright CLI Adapter

- 连接 session。
- 执行 flow。
- 归档 snapshot、screenshot、trace。
- 返回结构化 action result。

### 阶段 4：JS Reverse MCP Adapter

- 连接 MCP server。
- 获取网络请求、响应、initiator、WebSocket、脚本、控制台。
- 支持 XHR/fetch 断点工作流。
- 返回结构化 evidence。

### 阶段 5：Orchestrator

- 实现 `open_session`。
- 实现 `capture_baseline`。
- 实现 `capture_flow`。
- 实现 `trace_request`。
- 实现失败清理和 breakpoint 恢复。

### 阶段 6：Evidence 与 diff

- 生成 manifest。
- 生成 evidence ID。
- 实现 artifact 查询。
- 实现 capture diff。
- 实现 capture health。

### 阶段 7：上游缺口验证

- 验证 SSE/chunk 能力。
- 验证 Worker / Service Worker 请求归属。
- 必要时向 `js-reverse-mcp` 补最小工具。

### 阶段 8：Skill 文档

- 新增 `web-protocol-analysis` Skill。
- 写 Action 使用说明。
- 写证据报告格式。
- 写 request replay 的 workspace 用法。

---

## 14. MVP 完成标准

1. GPT 可见浏览器 Action 只有两个：`inspectBrowserEvidence` 和 `runBrowserExperiment`。
2. 只读查询不触发 consequential 确认。
3. 页面交互触发 consequential 确认。
4. `playwright-cli` 和 `js-reverse-mcp` 连接同一个浏览器。
5. `web_rev_action` 不重复实现 Playwright 或 CDP collector。
6. 能执行 `capture_baseline` 和 `capture_flow`。
7. 能保存请求、响应、initiator、WebSocket、脚本搜索结果、Trace 和截图。
8. 能通过 evidence ID 和 artifact ID 查询证据。
9. 能用 gateway workspace 工具写脚本、schema、diff、报告和 notes。
10. 能比较两个 capture。
11. 能报告 capture health。
12. 能验证是否需要补 Raw CDP stream capture。
13. 能验证是否需要补 Worker / Service Worker target metadata。
14. 不把 Cookie、Token、profile 路径、CDP endpoint 明文写进报告或共享文件。
