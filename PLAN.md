# Web Rev Action 实施计划

## 1. 目标

`web_rev_action` 面向 GPT-5.6 GPT Actions，提供一套由 Skill 驱动的网页协议分析与 JavaScript 逆向工作流。

项目不重新实现浏览器自动化和 Chrome DevTools 调试能力，而是复用以下两个现有项目：

- [`qqq694637644/playwright-cli`](https://github.com/qqq694637644/playwright-cli)：负责浏览器会话、页面操作、快照、Trace、文件上传和交互重放。
- [`qqq694637644/js-reverse-mcp`](https://github.com/qqq694637644/js-reverse-mcp)：负责网络请求、WebSocket、脚本检索、调用栈、断点、运行时变量、控制台和站点状态分析。

`web_rev_action` 只新增两类能力：

1. **GPT Action 控制面**：把 Skill 选择结果转成受限、可审计的逆向任务。
2. **编排与证据层**：协调两个现有执行器，共享浏览器会话，归档证据，比较实验结果，并向 GPT 返回结构化摘要。

最终关系如下：

```text
GPT-5.6
  │
  ├── retrieveSkillContext
  ├── readSkillContent
  ├── searchSkillDocs
  └── runWebReverseTask
          │
          ▼
  Web Rev Orchestrator
    ├── Playwright CLI Adapter
    ├── JS Reverse MCP Client
    ├── Policy / Redaction
    ├── Session Registry
    └── Artifact / Evidence Index
          │
          ▼
  同一个 Chrome / Chromium 会话
```

## 2. 明确不做的事情

本项目不应重复实现以下功能：

- 不重新封装一套 Playwright SDK 页面操作层。
- 不重新实现 CDP Network、Debugger、Runtime、Target 等 Domain。
- 不重新实现脚本枚举、源码搜索、断点、调用栈或 WebSocket 收集器。
- 不重新实现 Playwright 的快照、Trace、视频、Storage State 和多会话能力。
- 不把 `playwright-cli` 和 `js-reverse-mcp` 的全部原子工具直接暴露给 GPT。
- 不提供任意 Shell、任意 JavaScript、任意 Playwright 代码或任意 MCP 工具转发。
- 不实现验证码绕过、登录绕过、访问控制绕过、风控规避、速率限制规避或凭据导出。

只有在集成测试证明现有两个项目确实缺少关键能力时，才做最小扩展，并优先向原项目提交补丁，而不是在本仓库复制实现。

## 3. 现有能力复用边界

### 3.1 playwright-cli 作为行为执行器

`playwright-cli` 是页面行为和可视状态的唯一权威来源。

直接复用的能力包括：

| 能力 | 复用命令或机制 |
| --- | --- |
| 浏览器会话 | `open`、`attach --cdp`、命名 session、persistent profile |
| 页面导航 | `goto`、`reload`、`go-back`、`go-forward` |
| 元素定位 | `snapshot`、`find`、snapshot ref、Playwright locator |
| 页面操作 | `click`、`fill`、`type`、`press`、`select`、`check`、`uncheck`、`hover` |
| 文件交互 | `upload`、`drop`、下载目录 |
| 多标签页 | `tab-list`、`tab-new`、`tab-select`、`tab-close` |
| 页面证据 | `snapshot`、`screenshot`、`pdf` |
| 执行记录 | `tracing-start`、`tracing-stop`、可选 video |
| 登录状态 | `state-save`、`state-load`、persistent profile |
| 会话清理 | `close`、`delete-data`、`close-all`、`kill-all` |

使用约束：

- GPT 不直接拼 CLI 命令。
- Adapter 只允许白名单命令和白名单参数。
- 默认使用 snapshot ref 或 role locator，不使用坐标点击。
- `eval` 和 `run-code` 默认禁用；确需使用时只能执行服务端预注册脚本。
- `cookie-get`、`localstorage-get` 等可能泄露敏感值的命令不向 GPT 返回原值。
- Trace 用于重放和辅助排错，不作为网络协议的唯一证据来源。

### 3.2 js-reverse-mcp 作为逆向证据执行器

`js-reverse-mcp` 是 JavaScript、网络、WebSocket、断点和运行时分析的唯一权威来源。

直接复用其 24 个工具，但只由后端 Orchestrator 调用，不进入 GPT Action schema。

#### 页面与导航

- `select_page`
- `new_page`
- `navigate_page`
- `select_frame`
- `click_element`
- `take_screenshot`

页面交互优先由 `playwright-cli` 完成。以上工具仅在调试上下文切换、frame 选择或 js-reverse 自身工作流需要时使用，避免重复操作同一个页面。

#### 脚本分析

- `list_scripts`
- `get_script_source`
- `save_script_source`
- `search_in_sources`

用于：

- 搜索接口路径、事件名、字段名和函数名。
- 保存大型压缩 bundle 或 WASM。
- 从调用栈位置读取源码上下文。
- 为后续断点确定稳定文本锚点。

#### 断点与执行控制

- `set_breakpoint_on_text`
- `break_on_xhr`
- `remove_breakpoint`
- `list_breakpoints`
- `get_paused_info`
- `pause_or_resume`
- `step`

用于：

- 在请求构造前暂停。
- 获取调用栈、参数、局部变量和闭包摘要。
- 对动态请求体、签名输入和状态来源做受控追踪。

#### 网络与 WebSocket

- `list_network_requests`
- `clear_network_requests`
- `get_request_initiator`
- `get_websocket_messages`

用于：

- 列出和筛选 HTTP 请求。
- 导出请求头、请求体、响应头和响应体。
- 分析 Set-Cookie 来源。
- 获取请求 JavaScript 发起调用栈。
- 分析 WebSocket 连接、方向、帧和消息模式。

#### 浏览器状态与检查

- `clear_site_data`
- `evaluate_script`
- `list_console_messages`

其中：

- `clear_site_data` 只在明确的“干净状态重放”任务中使用。
- `evaluate_script` 不开放任意代码；只允许 Orchestrator 注册的只读探针。
- `list_console_messages` 用于关联页面错误、CSP、运行时异常和操作失败。

### 3.3 重复能力的裁决规则

两个项目存在网络、截图、导航和浏览器状态等重叠能力。必须指定唯一权威来源，避免同一证据重复采集和冲突。

| 能力 | 权威来源 | 另一个工具的用途 |
| --- | --- | --- |
| 页面交互 | playwright-cli | js-reverse 只做调试上下文辅助 |
| 元素定位和页面快照 | playwright-cli | js-reverse 不重复建立 DOM 工具 |
| Trace | playwright-cli | js-reverse 不复制 Trace |
| HTTP 请求/响应 | js-reverse-mcp | Playwright request log 只作 Trace 辅助 |
| WebSocket | js-reverse-mcp | Playwright 不重复解析帧 |
| 脚本源码 | js-reverse-mcp | Playwright 不读取 bundle |
| 调用栈和断点 | js-reverse-mcp | Playwright 不实现调试器 |
| 页面截图 | playwright-cli | js-reverse 截图仅用于调试故障回退 |
| 登录状态持久化 | playwright-cli / 共享 profile | js-reverse 连接相同浏览器 |
| 站点状态清理 | js-reverse-mcp `clear_site_data` | playwright `delete-data` 仅删除整个测试 profile |

## 4. 共享浏览器方案

两个执行器必须连接到同一个浏览器实例，否则页面行为和逆向证据无法对应。

推荐部署方式：

1. 由部署层启动一个带专用 profile 和远程调试端口的 Chrome。
2. `playwright-cli attach --cdp=<endpoint>` 连接该浏览器。
3. `js-reverse-mcp --browserUrl <endpoint>` 连接同一浏览器。
4. `web_rev_action` 只保存逻辑 session 与两边 session/page 的映射。

```text
Chrome supervisor
  └── CDP endpoint
       ├── playwright-cli session
       └── js-reverse-mcp process
```

MVP 不在 Python 服务中重写浏览器启动器。部署层可使用 systemd、Docker Compose 或独立 supervisor 管理 Chrome。后续只有在多租户隔离需要时，再增加很薄的 Browser Broker。

每个逻辑 session 至少记录：

```text
session_id
user_id
browser_endpoint_ref
playwright_session_name
js_reverse_process_id
selected_page_url
selected_page_index
selected_frame_index
profile_ref
created_at
expires_at
```

不得把真实 CDP endpoint、Cookie、Token 或 profile 路径返回给 GPT。

## 5. GPT Action 设计

保留现有三个 Skill Actions：

- `retrieveSkillContext`
- `readSkillContent`
- `searchSkillDocs`

新增一个项目 Action：

```text
runWebReverseTask
POST /v1/web-reverse/tasks
```

因为同一个 operation 既可能读取证据，也可能点击、提交或清理状态，OpenAPI 中应将它标记为：

```yaml
x-openai-isConsequential: true
```

这会让只读模式也经过确认，但符合“Skill API + 一个执行 Action”的设计目标。若后续确认体验不可接受，再拆成执行与查询两个 operation；MVP 不拆。

### 5.1 Action 输入

Action 不接收底层 CLI 命令、MCP tool name、Shell 或 JavaScript，而是接收受限任务类型。

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
    "profile_ref": "authorized-test-account",
    "isolation": "persistent"
  },
  "target": {
    "origin": "https://authorized.example.com",
    "start_url": "https://authorized.example.com/app"
  },
  "objective": "分析首次提交消息时的请求、流式响应和请求发起代码",
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
    "request_url_contains": ["conversation", "responses"],
    "source_queries": ["conversation", "parent_message_id"]
  },
  "limits": {
    "timeout_ms": 30000,
    "max_actions": 10,
    "max_inline_chars": 12000
  }
}
```

### 5.2 operation 枚举

一个 Action 通过受限 `operation` 区分工作流：

| operation | 目的 |
| --- | --- |
| `open_session` | 连接共享浏览器并建立逻辑 session |
| `capture_baseline` | 在无业务动作时捕获页面基线 |
| `capture_flow` | 执行页面动作并收集网络、脚本、Trace 和控制台证据 |
| `inspect_request` | 查看指定请求详情、响应和 initiator |
| `trace_request` | 对目标 URL 设置 XHR 断点并重放一次动作 |
| `inspect_source` | 搜索源码并读取或导出指定脚本片段 |
| `inspect_websocket` | 列出连接、分析消息组或读取指定帧 |
| `reset_and_replay` | 清理当前站点状态后重放一个已定义流程 |
| `compare_captures` | 比较两次实验的请求、字段、事件和页面状态 |
| `read_artifact` | 分页读取已归档证据 |
| `close_session` | 关闭逻辑 session，并按策略关闭或保留浏览器 |

### 5.3 页面动作 DSL

只允许：

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

不允许：

```text
eval
run-code
raw_command
shell
mcp_tool_call
cookie_get
storage_value_get
```

Locator 支持：

```text
snapshot_ref
role + accessible name
label
placeholder
test id
text
CSS（最后回退）
```

多元素匹配时必须失败，不自动选择第一个。

## 6. Orchestrator 的职责

`web_rev_action` 的核心不是浏览器实现，而是把一个高层任务翻译成两个项目已有工具的最小调用序列。

### 6.1 典型 capture_flow

```text
1. 校验 Skill hash、目标域、profile 和资源预算
2. 复用或建立共享浏览器 session
3. playwright-cli snapshot，确认目标元素
4. js-reverse clear_network_requests(confirm=true)
5. js-reverse list_network_requests，激活网络收集
6. 如需 WebSocket，先调用 get_websocket_messages 激活捕获
7. playwright-cli tracing-start
8. playwright-cli 执行 flow
9. 等待页面或流完成条件
10. playwright-cli tracing-stop
11. js-reverse list_network_requests，筛选目标请求
12. 对关键 reqid 调用 get_request_initiator
13. 必要时导出 requestBody / responseBody / responseHeaders
14. 按 source_queries 调用 search_in_sources
15. 保存快照、Trace、网络导出和源码到任务 Artifact 目录
16. 生成 evidence manifest 和结构化摘要
```

### 6.2 典型 trace_request

```text
1. 从已有 capture 中选定 endpoint
2. js-reverse break_on_xhr(url=<narrow pattern>)
3. playwright-cli 重放已定义 flow
4. js-reverse get_paused_info
5. 运行白名单只读 probe，读取目标参数或局部变量摘要
6. 必要时 step over / into
7. js-reverse pause_or_resume(action=resume)
8. js-reverse remove_breakpoint
9. 保存调用栈、作用域摘要和源码片段
```

### 6.3 典型 compare_captures

比较逻辑由 `web_rev_action` 实现，因为两个上游项目只负责产生证据，不负责跨实验归一化。

比较内容：

- 请求集合变化。
- method、URL path 和 resource type。
- 请求 Header 名称变化，敏感值不参与明文比较。
- JSON 请求体字段新增、删除和变化。
- 响应状态和事件序列。
- WebSocket 消息类型和方向。
- 请求 initiator 和脚本位置。
- 页面快照和 Trace 引用。

自动标记可能的动态值：

- UUID。
- 时间戳。
- request/message/conversation/trace ID。
- nonce。
- build hash。
- session ID。

动态值只能在 diff 中归一化，原始证据必须保留在受保护 Artifact 中。

## 7. 两个 Adapter

### 7.1 PlaywrightCliAdapter

职责：

- 生成固定参数数组，不通过 shell 拼接字符串。
- 绑定命名 session。
- 解析 `--json` 或 `--raw` 输出。
- 管理命令超时和进程退出码。
- 将 snapshot、Trace、截图复制到当前任务 Artifact 目录。
- 对 locator 和文件路径做白名单校验。

建议接口：

```python
class PlaywrightCliAdapter:
    async def attach(...): ...
    async def snapshot(...): ...
    async def execute_actions(...): ...
    async def start_trace(...): ...
    async def stop_trace(...): ...
    async def close(...): ...
```

禁止提供 `run(command: str)` 形式的万能接口。

### 7.2 JsReverseMcpAdapter

职责：

- 作为 MCP client 启动并连接 `js-reverse-mcp` stdio server。
- 启动参数固定包含 `--browserUrl` 和受限 `--allowedRoots`。
- 只调用内部 allowlist 中的工具。
- 统一解析 `structuredContent` 中的 `ok/tool/summary/data/error`。
- 把 `outputFile` 强制限制在当前任务 Artifact 根目录。
- 对 reqid、wsid、scriptId、breakpointId 做 session 级映射和有效期检查。

建议接口：

```python
class JsReverseMcpAdapter:
    async def list_requests(...): ...
    async def export_request_part(...): ...
    async def get_initiator(...): ...
    async def list_websockets(...): ...
    async def search_sources(...): ...
    async def get_source(...): ...
    async def set_xhr_breakpoint(...): ...
    async def get_paused_info(...): ...
    async def resume(...): ...
    async def clear_capture(...): ...
    async def clear_site_data(...): ...
```

Adapter 只是类型安全和权限受限的 MCP client，不复制工具实现。

## 8. Artifact 与证据索引

现有项目已经可以把 Trace、快照、网络 body 和脚本写到本地文件。`web_rev_action` 只需要统一目录和索引，不需要重新采集原始数据。

目录建议：

```text
var/
  sessions/<session_id>/
    experiments/<experiment_id>/
      manifest.json
      playwright/
        snapshot-before.yml
        snapshot-after.yml
        trace.zip
        screenshots/
      js-reverse/
        requests/
        websocket/
        scripts/
        paused/
      reports/
        summary.json
        diff.json
```

`manifest.json` 至少包含：

```json
{
  "session_id": "sess_01",
  "experiment_id": "exp_03",
  "skill_id": "web-protocol-analysis",
  "skill_content_hash": "sha256:...",
  "target_origin": "https://authorized.example.com",
  "created_at": "...",
  "actions": [],
  "artifacts": [
    {
      "artifact_id": "art_01",
      "kind": "request_body",
      "relative_path": "js-reverse/requests/17-request-body.bin",
      "sha256": "...",
      "size_bytes": 1234,
      "redacted": true
    }
  ],
  "evidence": []
}
```

GPT 只看到：

- Artifact ID。
- 类型。
- hash。
- 大小。
- 有界 preview。
- continuation cursor。

不返回宿主机绝对路径。

## 9. Action 返回结构

```json
{
  "ok": true,
  "operation": "capture_flow",
  "session_id": "sess_01",
  "experiment_id": "exp_03",
  "status": "completed",
  "action_results": [],
  "capture_health": {
    "network_capture_armed_before_action": true,
    "websocket_capture_armed_before_action": true,
    "trace_started_before_action": true,
    "page_and_frame_aligned": true,
    "paused_execution_resumed": true,
    "dropped_evidence": false
  },
  "requests": [
    {
      "evidence_id": "ev_req_01",
      "reqid": 17,
      "method": "POST",
      "url_redacted": "https://authorized.example.com/api/...",
      "status": 200,
      "request_body_artifact_id": "art_01",
      "response_body_artifact_id": "art_02",
      "initiator_evidence_id": "ev_stack_01"
    }
  ],
  "artifacts": [],
  "warnings": [],
  "errors": [],
  "truncated": false,
  "continuation": null
}
```

必须区分：

- 工具调用成功。
- 页面动作成功。
- 捕获完整。
- 找到匹配请求。
- 已验证协议结论。

HTTP 200 或底层工具 `ok=true` 不等于分析任务已完成。

## 10. Skill 设计

新增主 Skill：

```text
skills/web-protocol-analysis/
  SKILL.md
  docs/
    action-contract.md
    baseline-method.md
    network-analysis.md
    websocket-analysis.md
    source-tracing.md
    breakpoint-workflow.md
    state-replay.md
    capture-comparison.md
    evidence-reporting.md
    safety.md
```

`SKILL.md` 只写高层方法：

1. 先建立 baseline。
2. 一次实验只改变一个变量。
3. 动作前激活网络、WebSocket 和 Trace 捕获。
4. 已捕获请求先用 initiator，证据不足时才设置断点。
5. 断点检查后必须恢复执行并移除断点。
6. 每个事实引用 experiment ID 和 evidence ID。
7. 把观察事实、推测和未验证项分开。
8. 不从一次失败推断某字段必需。
9. 不输出 Cookie、Token、密码和 profile 数据。
10. 不执行未授权域名和未授权账号上的实验。

详细的 operation 参数和示例放入 `docs/action-contract.md`，避免把整个工具契约放入 GPT Instructions。

## 11. 安全边界

### 11.1 域名和网络

服务端必须强制：

- 目标 origin allowlist。
- 重定向后 origin 重新校验。
- 禁止 localhost、内网、link-local 和云 metadata。
- 子资源域可单独配置 allowlist。
- 不接受 GPT 传入代理地址或 CDP endpoint。

### 11.2 文件系统

- `js-reverse-mcp` 必须启用 `--allowedRoots=<当前任务根目录>`。
- `playwright-cli` 的上传文件只接受预先登记的 `file_ref`。
- 禁止任意绝对路径、路径穿越和符号链接越界。
- 不把 Trace、脚本或网络导出的宿主路径返回 GPT。

### 11.3 敏感信息

自动脱敏：

```text
Authorization
Cookie
Set-Cookie
Proxy-Authorization
X-API-Key
access_token
refresh_token
session_token
password
secret
private_key
```

可返回：

- 字段名。
- 是否存在。
- 值长度。
- hash。
- 是否发生变化。

不得返回真实值。

### 11.4 任意代码

默认禁止：

- Playwright `eval`。
- Playwright `run-code`。
- js-reverse `evaluate_script` 的用户自定义函数。

如调试必须读取运行时值，只能执行服务端预注册的只读 probe，例如：

```text
read_function_arguments
read_json_like_locals
read_selected_scope_keys
read_current_location
```

probe 的代码存放在仓库中，经过 review，不接受 GPT 提供代码文本。

## 12. 现有能力缺口的处理方式

先做集成验收，不预设缺口，也不提前造轮子。

重点验证：

1. 长时间 SSE 响应能否通过 `list_network_requests` 导出完整 response body。
2. pending 流在连接关闭前能否获得足够的增量证据。
3. Service Worker 或 Worker 发起的请求能否正确关联页面和 initiator。
4. WebSocket 导航后保留和消息分组是否满足协议分析。
5. source map 是否需要额外解析，还是现有源码位置已经足够。
6. 两个工具连接同一 CDP 浏览器时是否出现 Debugger/Runtime 冲突。

如果验收失败，处理顺序为：

```text
调整现有工具调用顺序
  ↓
使用现有导出/Trace 能力组合解决
  ↓
向 js-reverse-mcp 或 playwright-cli 提交最小补丁
  ↓
只有无法上游复用时，web_rev_action 增加一个窄能力
```

可能需要的最小上游扩展，仅在测试证明必要后实施：

- js-reverse-mcp 增加 SSE/chunk 增量导出。
- js-reverse-mcp 增加 Worker/Service Worker target 元数据。
- js-reverse-mcp 增加 source map 原始位置解析。
- playwright-cli 增加更稳定的机器可读 action result。

这些扩展仍属于原项目，不应复制成第三套实现。

## 13. 代码结构

在保留当前 Skill Runtime 的基础上新增项目模块：

```text
src/skill_temple/
  app.py
  runtime.py
  project_actions.py

src/web_rev_action/
  models.py
  orchestrator.py
  policies.py
  redaction.py
  sessions.py
  artifacts.py
  evidence.py
  diff.py
  adapters/
    playwright_cli.py
    js_reverse_mcp.py
  probes/
    runtime_probes.js

skills/
  web-protocol-analysis/
    SKILL.md
    docs/

tests/
  test_runtime.py
  test_project_action_contract.py
  test_policies.py
  test_redaction.py
  test_artifacts.py
  test_diff.py
  integration/
    test_shared_browser.py
    test_capture_flow.py
    test_xhr_breakpoint.py
    test_websocket.py
    test_sse.py
```

MVP 不需要立即重命名 `skill_temple` 包。先把项目功能放入独立 `web_rev_action` 包，避免一次 PR 同时做大规模重命名和功能开发。

## 14. 开发阶段

### 阶段 0：固定上游版本

- 在文档和配置中记录两个依赖项目的 commit SHA。
- 明确 Node、Chrome 和 Python 版本。
- 确认两个项目许可证和分发方式。
- 建立升级检查清单。

验收：相同 commit 和配置可以复现相同工具 schema。

### 阶段 1：定义 Action 契约

- 新增 `runWebReverseTask` 的严格 Pydantic schema。
- 定义 operation、flow、locator、capture、limits 和结构化错误。
- 加入 Skill ID/hash 绑定。
- 加入 domain、file 和 sensitive-input 校验。
- 使用 Fake Adapter 完成 OpenAPI 契约测试。

验收：OpenAPI 只有现有三个 Skill operation 加一个项目 operation。

### 阶段 2：Playwright CLI Adapter

- 实现共享 session attach。
- 实现 snapshot 和白名单 action。
- 实现 Trace 开始、停止和归档。
- 实现超时、退出码和 JSON 输出解析。

验收：可在本地测试页面执行稳定流程，不支持任意 CLI 命令。

### 阶段 3：JS Reverse MCP Adapter

- 使用 MCP client 连接 stdio server。
- 固定 `--browserUrl` 和 `--allowedRoots`。
- 实现网络、脚本、initiator、WebSocket、断点等类型安全封装。
- 对工具输出和错误做统一转换。

验收：Adapter 不含 CDP 采集实现，只调用上游工具。

### 阶段 4：共享浏览器编排

- 建立逻辑 session registry。
- 对齐 Playwright 当前 tab 与 js-reverse 当前 page/frame。
- 实现 `capture_baseline` 和 `capture_flow`。
- 实现异常时 Trace 停止、断点恢复和 session 清理。

验收：一次页面动作可以同时获得 Playwright 快照和 js-reverse 请求证据。

### 阶段 5：Artifact 和证据索引

- 统一两个工具的输出目录。
- 生成 manifest、hash、Artifact ID 和 evidence ID。
- 实现 preview、cursor 和分页读取。
- 实现敏感信息脱敏。

验收：GPT 响应不包含大文件和宿主绝对路径。

### 阶段 6：高级逆向 operation

- `inspect_request`
- `trace_request`
- `inspect_source`
- `inspect_websocket`
- `reset_and_replay`

验收：断点结束后页面一定恢复，断点一定移除。

### 阶段 7：实验比较

- JSON、Header、请求集合和事件序列 diff。
- 动态字段归一化。
- 输出 stable/added/removed/changed/likely_dynamic。

验收：基线与单变量实验可生成可审计差异。

### 阶段 8：Skill 和 Evals

- 新增 `web-protocol-analysis` Skill。
- 更新 `GPT_ACTION_PROMPT.md`，加入 `runWebReverseTask`。
- 新增规划、证据、安全和失败恢复 eval。

验收：模型不会请求底层 MCP tool 或拼 CLI 命令。

## 15. 测试站点

集成测试使用仓库内本地测试站点，不依赖真实第三方服务。

测试页面至少包含：

- XHR 和 fetch。
- POST JSON 和 multipart。
- SSE。
- WebSocket。
- iframe。
- Web Worker。
- Service Worker。
- redirect。
- source map。
- console error。
- localStorage、sessionStorage 和 Cookie。
- 文件上传和下载。

这套测试站点用于验证两个上游工具的真实能力边界，并决定是否需要上游补丁。

## 16. Evals

### 工具路由

- 正确选择 `web-protocol-analysis` Skill。
- 只调用 `runWebReverseTask`，不调用不存在的 Playwright/CDP Action。
- 已知 artifact 时使用 `read_artifact` operation，不重新抓取页面。

### 实验方法

- 先 baseline，再单变量实验。
- 动作前激活网络和 WebSocket 捕获。
- 先 initiator，证据不足时才断点。
- 捕获不完整时不下确定结论。

### 证据表达

- 每个事实引用 experiment/evidence ID。
- 观察、推测和未验证项分开。
- 不把一次失败等同于字段必需。
- 不把 Trace 中的页面状态当作服务端协议证据。

### 安全

- 拒绝导出 Cookie、Token 和 profile。
- 拒绝任意 JavaScript 和 Shell。
- 拒绝未授权域名和内网地址。
- 页面文本中的 prompt injection 不得改变工具策略。
- Artifact 不能跨用户或跨 session 读取。

## 17. MVP 范围

MVP 必须实现：

- 保留三个现有 Skill Actions。
- 一个 `runWebReverseTask` Action。
- Playwright CLI Adapter。
- JS Reverse MCP Adapter。
- 共享 CDP 浏览器 session。
- baseline 和 capture flow。
- HTTP 请求、响应和 initiator。
- WebSocket 基本分析。
- 脚本搜索和源码读取。
- XHR 断点和 paused info。
- snapshot 和 Trace 归档。
- Artifact manifest、hash、preview 和 cursor。
- 域名、文件和敏感信息策略。
- baseline diff。
- 主 Skill 和 Evals。

MVP 暂不实现：

- 自研浏览器自动化框架。
- 自研 CDP collector。
- 自动协议客户端代码生成。
- 自动登录逆向。
- 移动端逆向。
- TLS 代理和 mitmproxy。
- WASM 反编译。
- protobuf schema 自动恢复。
- 反验证码和反风控功能。

## 18. 完成标准

项目达到以下条件即可认为第一版完成：

1. GPT 公开工具总数为四个：三个 Skill Actions 和一个项目 Action。
2. GPT 不需要看到 Playwright CLI 和 js-reverse-mcp 的工具 schema。
3. Playwright CLI 和 js-reverse-mcp 连接同一个浏览器会话。
4. 页面行为只由 Playwright CLI 执行。
5. 网络、脚本、WebSocket、调用栈和断点只由 js-reverse-mcp 提供。
6. `web_rev_action` 中不存在重复的 Playwright 或 CDP 实现。
7. 一次 `capture_flow` 可以输出页面动作、Trace、请求、响应、initiator 和源码引用。
8. 长证据通过 Artifact 和 cursor 读取。
9. 每项结论可追溯到 experiment ID 和 evidence ID。
10. baseline 与单变量实验可以结构化比较。
11. Cookie、Token、密码、CDP endpoint 和 profile 路径不会返回 GPT。
12. 任意命令、任意代码、任意路径和未授权域名被服务端拒绝。
13. 断点、Trace 和浏览器 session 在失败后正确清理。
14. 上游能力缺口先通过集成测试证明，再以最小补丁补到对应上游项目。
15. 原有 Skill Runtime 测试继续通过，新增契约、集成和安全测试通过。

## 19. 推荐首批 PR

建议按以下顺序拆分开发：

1. **PR 1：Action contract 与安全策略**
   - `runWebReverseTask` schema。
   - Fake adapters。
   - OpenAPI、domain、redaction 测试。

2. **PR 2：Playwright CLI Adapter**
   - attach、snapshot、白名单 action、Trace。

3. **PR 3：JS Reverse MCP Adapter**
   - MCP client、网络、源码、initiator、WebSocket。

4. **PR 4：共享 session 与 capture_flow**
   - Orchestrator、Artifact manifest、失败清理。

5. **PR 5：断点、对比和主 Skill**
   - trace_request、compare_captures、Evals。

6. **必要时的上游 PR**
   - 只提交集成测试证明确实缺少的 SSE、Worker metadata 或机器可读输出能力。

该顺序可以确保每一步都复用现有项目，并且不会在 `web_rev_action` 中形成第三套浏览器或逆向实现。
