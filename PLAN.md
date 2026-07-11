# Web Rev Action 个人版实施计划

## 1. 目标

`web_rev_action` 是给 GPT-5.6 GPT Actions 使用的个人 Web 逆向与协议复现工具层。

它不是企业级多租户平台，也不是重新实现一套 Playwright/CDP 框架。目标是把已经存在的两个项目组合起来，让 GPT-5.6 用 Skill 指导分析流程，用少量高层 Actions 完成最新版网页协议分析、证据读取、文件整理和最小请求复现。

核心原则：

1. **复用现有工具，不重复造轮子。**
2. **GPT-5.6 足够聪明，工具不需要过度拆碎。**
3. **浏览器行为交给 `playwright-cli`。**
4. **网络、脚本、断点、调用栈交给 `js-reverse-mcp`。**
5. **文件、diff、schema、HTTP 重放交给 workspace PowerShell 7。**
6. **Action 分成只读证据查询和有副作用浏览器实验两个操作。**
7. **只保留必要安全边界：不泄露凭据、不越权访问、不执行危险宿主操作。**

最终工具面：

```text
Skill API
  ├── retrieveSkillContext
  ├── readSkillContent
  └── searchSkillDocs

Browser Analysis Actions
  ├── inspectBrowserEvidence     # 只读，x-openai-isConsequential: false
  └── runBrowserExperiment       # 执行交互，x-openai-isConsequential: true

Workspace Tools
  ├── workspaceExecPwsh          # PowerShell 7，工作目录限定在 workspace
  ├── workspaceInspect           # 查询目录、读文件片段
  ├── workspaceSearch            # 搜索文件内容
  └── workspaceEdit              # 写入或补丁编辑文件
```

其中 Workspace Tools 可以是单独 Action，也可以复用现有代码维护类工具。它们不需要理解浏览器，只负责保存和处理分析产物。

---

## 2. 上游项目分工

本方案复用两个已有项目。

### 2.1 playwright-cli：行为执行器

仓库：`https://github.com/qqq694637644/playwright-cli`

用途：制造可重复的页面行为。

直接复用能力：

- 打开和连接浏览器。
- 复用登录后的浏览器 profile。
- 页面导航、刷新、前进、后退。
- 元素定位、snapshot、find。
- click、fill、type、press、select、check、hover。
- 文件上传和下载。
- 多标签页切换。
- 截图、PDF、Trace。
- 保存和加载 storage state。
- 关闭 session 和清理测试数据。

它负责回答：

```text
我怎样稳定地复现一次用户操作？
```

### 2.2 js-reverse-mcp：协议显微镜

仓库：`https://github.com/qqq694637644/js-reverse-mcp`

用途：解释页面行为背后的网络、脚本和运行时状态。

直接复用能力：

- 页面和 frame 选择。
- 网络请求列表。
- 请求头、请求体、响应头、响应体导出。
- 请求 initiator 和调用栈。
- WebSocket 消息。
- 控制台日志。
- 脚本列表。
- 脚本源码读取和保存。
- bundle 文本搜索。
- XHR/fetch 断点。
- 断点暂停状态、局部变量、作用域和调用栈。
- 单步和恢复执行。
- 清理站点状态。

它负责回答：

```text
这个请求从哪里来？请求体怎么构造？响应流怎么返回？
```

### 2.3 两者的关系

```text
playwright-cli 负责制造事件
js-reverse-mcp 负责解释事件
web_rev_action 负责组织实验、整理证据、给 GPT 返回结构化结果
workspace PowerShell 负责保存、diff、schema、重放脚本和报告
```

---

## 3. 不重复实现的内容

`web_rev_action` 不做以下事情：

- 不重新实现 Playwright 页面操作。
- 不重新实现 Chrome DevTools Protocol collector。
- 不重新实现断点、调用栈、脚本搜索、WebSocket 抓取。
- 不重新实现 Trace。
- 不把 `playwright-cli` 和 `js-reverse-mcp` 的所有原子工具直接塞进 GPT Action schema。
- 不做企业级权限系统、多租户隔离、复杂审计后台。

`web_rev_action` 只做：

- GPT Action 契约。
- 两个上游工具的轻量 Adapter。
- 共享浏览器 session 映射。
- Evidence ID 和 Artifact 索引。
- 两个 capture 的基础 diff。
- 缺口工具的最小补丁规划。
- Skill 文档与执行流程绑定。

---

## 4. 推荐 Action 方案

采用两个浏览器相关 Action。

### 4.1 inspectBrowserEvidence

只读证据查询 Action。

```yaml
operationId: inspectBrowserEvidence
x-openai-isConsequential: false
```

用途：

- 查看已有 session。
- 查看已有 experiment。
- 查询请求列表。
- 读取指定 request 的 headers/body/response。
- 读取 request initiator。
- 读取 WebSocket 消息。
- 搜索已保存脚本。
- 读取 Artifact。
- 比较两个 capture。
- 查询 capture health。

这个 Action 不执行页面点击、输入、上传、清理站点状态或请求重放。

### 4.2 runBrowserExperiment

有副作用的浏览器实验 Action。

```yaml
operationId: runBrowserExperiment
x-openai-isConsequential: true
```

用途：

- 打开或连接浏览器 session。
- 导航页面。
- 执行点击、输入、上传等页面交互。
- 开始和停止 Trace。
- 清理当前站点状态。
- 设置 XHR/fetch 断点。
- 重放一个已定义页面流程。
- 执行独立 HTTP 重放实验。
- 关闭 session。

拆成这两个 Action 的原因：

- 读证据不需要每次确认。
- 执行页面交互、发送消息、上传文件、清理状态属于 consequential。
- GPT-5.6 可以根据 Skill 文档决定何时读证据、何时执行实验。
- 这个拆分比单一万能 Action 更符合 GPT Actions 语义，也不会显著增加复杂度。

---

## 5. inspectBrowserEvidence 设计

### 5.1 请求结构

```json
{
  "contract_version": "1.0",
  "mode": "list_requests",
  "session_id": "sess_01",
  "experiment_id": "exp_01",
  "filter": {
    "url_contains": ["conversation", "responses"],
    "method": "POST"
  },
  "cursor": null,
  "max_items": 50,
  "max_chars": 12000
}
```

### 5.2 mode 枚举

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

### 5.3 返回结构

```json
{
  "ok": true,
  "mode": "list_requests",
  "session_id": "sess_01",
  "experiment_id": "exp_01",
  "items": [
    {
      "evidence_id": "ev_req_001",
      "method": "POST",
      "url": "https://example.com/api/conversation",
      "status": 200,
      "request_body_artifact_id": "art_req_body_001",
      "response_body_artifact_id": "art_resp_body_001",
      "initiator_evidence_id": "ev_stack_001"
    }
  ],
  "artifacts": [],
  "truncated": false,
  "next_cursor": null,
  "warnings": []
}
```

---

## 6. runBrowserExperiment 设计

### 6.1 请求结构

```json
{
  "contract_version": "1.0",
  "skill_binding": {
    "skill_id": "web-protocol-analysis",
    "content_hash": "sha256:..."
  },
  "operation": "capture_flow",
  "session": {
    "session_id": null,
    "profile_ref": "default",
    "reuse_existing_browser": true
  },
  "target": {
    "start_url": "https://example.com/app"
  },
  "objective": "观察首次发送消息时产生的请求、流和请求构造代码",
  "flow": [
    {
      "action": "fill",
      "locator": {
        "kind": "role",
        "role": "textbox",
        "name": "Message"
      },
      "value": "TEST"
    },
    {
      "action": "press",
      "key": "Enter"
    }
  ],
  "capture": {
    "network": true,
    "websocket": true,
    "console": true,
    "trace": true,
    "source_queries": ["conversation", "parent_message_id"]
  },
  "limits": {
    "timeout_ms": 30000,
    "max_inline_chars": 12000
  }
}
```

### 6.2 operation 枚举

```text
open_session
capture_baseline
capture_flow
trace_request
reset_and_replay
http_replay
close_session
```

`inspect_request`、`inspect_source`、`compare_captures` 这类只读操作放到 `inspectBrowserEvidence`，不要混在执行 Action 里。

### 6.3 页面动作 DSL

`flow` 支持：

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

这部分直接映射到 `playwright-cli`。GPT 不需要知道底层 CLI 参数格式。

Locator 支持：

```text
snapshot_ref
role + accessible name
label
placeholder
test id
text
css
```

个人工具可以保留 CSS 作为实用兜底，不需要过度限制。

---

## 7. Workspace 工具设计

浏览器分析之外，协议复现需要大量文件操作：保存抓包、写 schema、写 diff 脚本、跑 HTTP 重放、整理报告。这里不需要复杂文件 MCP，一个限定目录的 PowerShell 7 workspace 足够。

### 7.1 workspaceExecPwsh

提供一个 PowerShell 7 执行工具。

要求：

- 当前目录固定为 workspace 根目录。
- 默认无外部密钥。
- 不暴露宿主真实路径。
- 支持运行 Python、Node、curl、git、rg、jq 等常用命令，具体取决于环境安装。
- 输出截断并返回 exit code。
- 可读写 workspace 目录内文件。

用途：

- 整理 capture 文件。
- 写 JSON schema。
- 比较请求体。
- 运行 Python/Node diff 脚本。
- 用 curl/httpx 做独立请求重放。
- 生成 Markdown 报告。
- 管理 Git 工作区。

### 7.2 workspaceInspect

用于轻量查看当前目录和文件片段。

能力：

```text
list tree
read file snippets
show file metadata
```

### 7.3 workspaceSearch

用于全文搜索。

能力：

```text
ripgrep 搜索
按路径过滤
返回上下文行
```

### 7.4 workspaceEdit

用于编辑文件。

能力：

```text
write file
apply patch
create directories
```

对个人工具而言，这四个 workspace 能力已经足够，不需要再额外设计复杂 Filesystem MCP。

---

## 8. 完整工具组合

### 8.1 必需工具

| 工具 | 作用 |
| --- | --- |
| Skill API | 加载分析流程和方法论 |
| `playwright-cli` | 页面操作、登录态、Trace、快照 |
| `js-reverse-mcp` | 网络、脚本、断点、调用栈、WebSocket |
| `inspectBrowserEvidence` | 只读查询证据和 Artifact |
| `runBrowserExperiment` | 执行页面实验和重放 |
| workspace PowerShell 7 | 文件、diff、schema、报告、HTTP 重放 |
| workspace 查询/搜索/编辑 | 快速读写分析产物 |

### 8.2 需要补的缺口工具

只补三个最小能力。

#### 缺口 1：Raw CDP Stream Capture

用途：SSE、chunked fetch、长连接和实时增量响应。

优先补到 `js-reverse-mcp`，不要在 `web_rev_action` 里重写 CDP 网络采集。

建议工具：

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

能拿到 chunk 文本时保存文本，拿不到也要保存 chunk 顺序和大小。这样至少能判断响应是否持续、是否完成、是否漏帧。

#### 缺口 2：Worker / Service Worker Target 元数据

用途：确定请求来自 page、iframe、worker 还是 service worker。

优先补到 `js-reverse-mcp` 的请求结构中。

建议每个请求增加：

```json
{
  "targetType": "page | iframe | worker | service_worker | shared_worker",
  "targetId": "...",
  "frameId": "...",
  "workerUrl": "..."
}
```

并确认内部使用：

```text
Target.setAutoAttach
Target.attachedToTarget
```

#### 缺口 3：独立请求重放和差异分析

不需要单独 MCP。

使用 workspace PowerShell 7 即可：

- Python `httpx`。
- Node `fetch` / `undici`。
- curl。
- 自写 JSON diff 脚本。
- JSON Schema。
- SQLite/DuckDB 可选。

目录建议：

```text
workspace/
  captures/
    baseline/
    new-conversation/
    second-turn/
    regenerate/
    edit-message/
    stop-generation/
    file-upload/
  schemas/
    request.schema.json
    stream-events.schema.json
    conversation-state.md
  scripts/
    diff-request.ps1
    diff-json.py
    replay-http.py
  reports/
    protocol-map.md
```

---

## 9. 共享浏览器方案

两个上游工具必须连同一个浏览器。

推荐：

```text
Chrome / Chromium with remote debugging
  ├── playwright-cli attach --cdp=<endpoint>
  └── js-reverse-mcp --browserUrl <endpoint>
```

`web_rev_action` 保存映射：

```text
session_id
playwright_session_name
js_reverse_session/process
selected_page
selected_frame
workspace_dir
profile_ref
```

个人使用场景中，profile 可以简单很多：

- 一个默认 profile。
- 一个干净测试 profile。
- 一个临时 profile。

不需要复杂多租户 profile 隔离。

---

## 10. Orchestrator 调用流程

### 10.1 capture_baseline

```text
1. 连接或打开浏览器 session
2. playwright-cli goto / snapshot
3. js-reverse-mcp clear_network_requests
4. 等待页面稳定
5. js-reverse-mcp list_network_requests
6. js-reverse-mcp list_console_messages
7. js-reverse-mcp get_websocket_messages
8. playwright-cli screenshot / trace 可选
9. 保存 baseline manifest
```

### 10.2 capture_flow

```text
1. 确认 session 和页面
2. js-reverse-mcp clear_network_requests
3. playwright-cli tracing-start
4. playwright-cli 执行 flow
5. 等待网络、流或页面稳定
6. playwright-cli tracing-stop
7. js-reverse-mcp list_network_requests
8. 对关键请求 get_request_initiator
9. 对目标关键字 search_in_sources
10. 保存 request/response/source/trace 到 workspace
11. 返回 evidence summary
```

### 10.3 trace_request

```text
1. 根据已有 capture 选择 URL pattern
2. js-reverse-mcp break_on_xhr
3. playwright-cli 重放 flow
4. js-reverse-mcp get_paused_info
5. 必要时 step / resume
6. remove_breakpoint
7. 保存 paused info、scope 摘要、调用栈、源码片段
```

### 10.4 http_replay

```text
1. 从 Artifact 复制一个已捕获请求到 workspace
2. 由 GPT 编写 replay 脚本或 curl/httpx 命令
3. 在 workspaceExecPwsh 中运行
4. 保存响应、错误和 diff
5. 不把浏览器 Cookie/Token 明文写入报告
```

个人使用可以允许 GPT 写 replay 脚本，但应默认提醒不要把账号凭据提交到 Git。

---

## 11. Artifact 和 Evidence

`web_rev_action` 只做轻量索引，不做复杂对象存储。

目录：

```text
workspace/
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

Evidence ID：

```text
exp_001
cap_001
ev_req_001
ev_resp_001
ev_stack_001
ev_ws_001
ev_script_001
art_001
```

`manifest.json` 保存：

```json
{
  "experiment_id": "exp_001",
  "operation": "capture_flow",
  "objective": "first message",
  "created_at": "...",
  "actions": [],
  "requests": [],
  "websockets": [],
  "scripts": [],
  "artifacts": []
}
```

读取大文件时使用 `inspectBrowserEvidence.read_artifact` 或 workspace 文件工具。

---

## 12. Skill 设计

新增主 Skill：

```text
skills/web-protocol-analysis/
  SKILL.md
  docs/
    action-contract.md
    browser-experiment.md
    network-analysis.md
    stream-analysis.md
    websocket-analysis.md
    source-tracing.md
    request-replay.md
    evidence-report.md
```

`SKILL.md` 写高层流程，不写太多限制。

建议内容：

```text
1. 先 baseline，再执行动作。
2. 一次只改变一个变量。
3. 用 playwright-cli 制造事件。
4. 用 js-reverse-mcp 解释事件。
5. 网络请求先看 list_network_requests。
6. 关键请求再看 get_request_initiator。
7. 看不懂请求体来源时再 break_on_xhr。
8. 长流或 SSE 需要 stream capture。
9. Worker 请求需要检查 targetType。
10. 浏览器外复现放到 workspace，用脚本保存结果。
11. 结论分为：已观察、已验证、推测、未验证。
12. 结论引用 experiment/evidence/artifact。
```

---

## 13. 实际分析流程

### 阶段 1：协议地图

记录以下实验：

```text
01 打开首页
02 新建会话
03 第一轮消息
04 第二轮消息
05 停止生成
06 重新生成
07 修改旧消息
08 切换模型
09 删除会话
10 上传文件
11 启用网页搜索
12 生成图片
```

每个实验保存：

```text
页面动作
请求列表
请求头
请求体
响应体或流事件
WebSocket 数据
请求发起调用栈
相关脚本位置
前后页面状态
Trace
```

### 阶段 2：状态机

整理：

```text
会话如何创建
消息如何关联
parent/child/branch 如何表达
重新生成如何表达
停止生成是否发取消请求
流式事件有哪些类型
工具调用如何开始和结束
文件如何上传和绑定
错误如何返回
```

### 阶段 3：请求构造代码

对核心请求执行：

```text
list_network_requests
get_request_initiator
search_in_sources
break_on_xhr
get_paused_info
保存源码和调用栈
```

目标是区分：

```text
后端真正需要的字段
前端生成的状态字段
A/B 实验字段
埋点字段
动态 ID
```

### 阶段 4：独立重放

在 workspace 中完成：

```text
保存请求样本
写 replay-http.py 或 curl 脚本
一次删一个字段
一次改一个 header
保存响应和 diff
更新 schema
```

不要一开始复现登录。浏览器负责正常登录，重放只研究登录后的业务协议。

---

## 14. 代码结构

保留当前 `skill_temple` 运行时，新增轻量项目模块。

```text
src/skill_temple/
  app.py
  runtime.py

src/web_rev_action/
  actions.py              # inspectBrowserEvidence / runBrowserExperiment
  models.py               # Action request/response schema
  orchestrator.py         # 调度 playwright-cli 和 js-reverse-mcp
  sessions.py             # session 映射
  artifacts.py            # manifest 和 artifact 索引
  evidence.py             # evidence id 和查询
  diff.py                 # capture diff
  adapters/
    playwright_cli.py
    js_reverse_mcp.py
  workspace/
    powershell.py         # workspaceExecPwsh 适配
    files.py              # inspect/search/edit

skills/
  web-protocol-analysis/
    SKILL.md
    docs/

workspace/
  captures/
  experiments/
  schemas/
  scripts/
  reports/
```

MVP 不做大规模重命名，避免把模板改名和功能开发混在一起。

---

## 15. 开发阶段

### PR 1：文档和 Action 契约

- 更新 `PLAN.md`。
- 定义 `inspectBrowserEvidence` schema。
- 定义 `runBrowserExperiment` schema。
- 更新 OpenAPI。
- Fake adapter 返回固定数据。

### PR 2：Workspace 工具

- `workspaceExecPwsh`。
- `workspaceInspect`。
- `workspaceSearch`。
- `workspaceEdit`。
- workspace 根目录限制。

### PR 3：Playwright CLI Adapter

- attach/open session。
- snapshot。
- 执行 flow。
- trace start/stop。
- 保存 snapshot、trace、screenshot。

### PR 4：JS Reverse MCP Adapter

- MCP client。
- list_network_requests。
- get_request_initiator。
- get_websocket_messages。
- search_in_sources。
- get_script_source。
- break_on_xhr / get_paused_info / resume。

### PR 5：共享 session 和 capture_flow

- 同一个 Chrome CDP endpoint。
- Playwright 与 js-reverse 当前页面对齐。
- baseline。
- capture_flow。
- evidence manifest。

### PR 6：缺口补丁验证

- 验证 SSE/chunk 是否足够。
- 验证 Worker/Service Worker target 元数据。
- 必要时给 `js-reverse-mcp` 补：
  - `start_stream_capture`
  - `get_stream_chunks`
  - `stop_stream_capture`
  - `export_stream_capture`
  - request target metadata

### PR 7：diff、HTTP replay 和 Skill

- compare_captures。
- workspace replay 示例脚本。
- `web-protocol-analysis` Skill。
- 示例报告模板。

---

## 16. MVP 完成标准

第一版完成时应满足：

1. GPT 可见浏览器 Action 只有两个：`inspectBrowserEvidence` 和 `runBrowserExperiment`。
2. 只读查询不触发 consequential 确认。
3. 页面交互触发 consequential 确认。
4. `playwright-cli` 和 `js-reverse-mcp` 连接同一个浏览器。
5. `web_rev_action` 不重复实现 Playwright 或 CDP collector。
6. 能执行 baseline 和 capture_flow。
7. 能拿到请求、响应、initiator、WebSocket、源码搜索结果和 Trace。
8. 能把证据保存到 workspace。
9. 能用 workspace PowerShell 写 diff/replay/schema/report。
10. 能识别或补齐 SSE/chunk capture 缺口。
11. 能识别或补齐 Worker/Service Worker target 元数据缺口。
12. 能用 evidence ID 和 artifact ID 写可追溯报告。
13. 不把 Cookie、Token、profile 路径、CDP endpoint 明文写进报告或提交到 Git。

---

## 17. 最终最小工具组合

```text
1. Skill API
   - retrieveSkillContext
   - readSkillContent
   - searchSkillDocs

2. Browser Actions
   - inspectBrowserEvidence
   - runBrowserExperiment

3. Browser Engines
   - playwright-cli
   - js-reverse-mcp

4. Workspace Tools
   - workspaceExecPwsh
   - workspaceInspect
   - workspaceSearch
   - workspaceEdit

5. 必要补丁
   - Raw CDP stream capture，优先补到 js-reverse-mcp
   - Worker / Service Worker target metadata，优先补到 js-reverse-mcp
```

这套组合已经足够完成最新版网页端协议分析和第一版 Pandora 类复现。不要再增加大型 MCP、移动端工具、Frida、mitmproxy 或自研浏览器框架，除非网页端协议画像完成后证明必须需要。
