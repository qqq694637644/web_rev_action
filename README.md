# web_rev_action

`web_rev_action` 是一个单用户、自用的 GPT 5.6 网页协议分析后端。

它不重新实现浏览器自动化和 CDP collector，而是原子编排：

- `playwright-cli`：页面操作、snapshot、截图和 Trace。
- `js-reverse-mcp`：网络、SSE、initiator、脚本和断点证据。
- `LocalEvidenceStore`：Action 本机的 manifest、artifact、搜索、导出、报告和重放脚本。
- 现有 Skill runtime：向 GPT 提供 `SKILL.md` 和渐进式文档读取。

当前已实现 `PLAN.md` 的阶段 1 和阶段 2：Action schema、真实 adapter、session、页面对齐、统一 deadline、最小原子 Orchestrator、experiment store 和 fake-adapter 验收测试。

## Public GPT Actions

| operationId | Path | Consequential | 作用 |
| --- | --- | --- | --- |
| `retrieveSkillContext` | `POST /v1/skills/retrieve` | false | 发现或读取明确选择的 Skill。 |
| `readSkillContent` | `POST /v1/skills/read` | false | 读取 Skill 内的安全相对路径。 |
| `searchSkillDocs` | `POST /v1/skills/search` | false | 搜索一个 Skill 的资源。 |
| `inspectBrowserEvidence` | `POST /v1/browser/inspect` | false | 查询 session、experiment、stream 状态和 artifact。 |
| `runBrowserExperiment` | `POST /v1/browser/run` | true | 打开/关闭 session，或运行一次原子浏览器实验。 |

GPT 看不到内部 MCP 工具。`web_rev_action` 私下调用：

```text
start_stream_capture
get_stream_status
stop_stream_capture
```

## Atomic capture flow

一次 `capture_flow` 在一个 HTTP 请求内完成：

```text
page alignment
→ create experiment
→ optional Trace start
→ stream capture start
→ before screenshot
→ Playwright flow
→ private stream/page wait
→ stream stop/finalize
→ network summary
→ after screenshot
→ Trace archive
→ primary/objective integrity
→ atomic manifest write
```

页面动作失败时仍会尝试 stop/finalize，并写出失败 manifest。

## Implemented operations

### `runBrowserExperiment`

```text
open_session
capture_baseline
capture_flow
close_session
export_experiment
```

### `inspectBrowserEvidence`

```text
get_session
list_experiments
get_experiment
list_artifacts
read_artifact
search_artifacts
get_stream_status
```

请求模型使用 OpenAPI discriminated union / `oneOf`，不同 operation 不共享一堆无关可选字段。

## Flow steps

支持：

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

Locator 支持：

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

Stop step 可以声明：

```json
{
  "step_id": "stop_generation",
  "action": "click",
  "locator": {"role": "button", "name": "Stop"},
  "intent": "stop_generation"
}
```

底层 `network_canceled` 只有在主请求、Stop step、时间窗口、页面对齐且无后续导航同时匹配时，才在 experiment manifest 中派生为 `expected_user_cancel`。

## Wait conditions

```text
timeout
selector_visible
selector_hidden
request_observed
response_observed
network_idle
first_event
event_predicate
default_done_marker
network_finished
network_canceled
failed
page_url
```

Event predicate 支持：

```text
exact_data
event_name
json_path_equals
network_terminal
selector_state
```

`[DONE]` 只是 `default_done_marker` 的默认语义，不是通用协议完成定义。

## Job 与 deadline

`capture_flow` 默认使用后台 job：Action 立即返回 `experiment_id` 和 `status=running`，后台仍原子执行完整 start → flow → wait → stop → manifest。使用 `inspectBrowserEvidence.get_experiment` 查询终态。

需要快速同步结果时显式设置：

```json
{"execution_mode": "sync", "deadline_ms": 42000}
```

同步模式使用不超过 42 秒的总 deadline；job 模式使用独立 `job_timeout_ms`，默认 300 秒。

- step 使用总 deadline 的子预算。
- wait 使用 condition timeout 与总 deadline 的较小值。
- Orchestrator 在执行新动作前为 stop/finalize 预留时间。
- Playwright subprocess 超时会被终止。
- MCP tool 调用使用剩余 deadline。
- `stop_stream_capture` 获得剩余的 `finalizeTimeoutMs`。

## Primary request and integrity

实验请求声明目标请求：

```json
{
  "url_contains": "/conversation",
  "method": "POST",
  "resource_types": ["fetch"],
  "expected_min_matches": 1,
  "expected_max_matches": 1,
  "allow_supporting_failures": true,
  "include_in_flight": false
}
```

Manifest 分开返回：

```text
collector_integrity
primary_request_integrity
objective_integrity
```

遥测或 supporting request 失败，不会在 `allow_supporting_failures=true` 时覆盖主消息实验结果。

上游 request 的详细维度也被保留：

```text
rawCaptureIntegrity
semanticParseIntegrity
requestSnapshotIntegrity
artifactIntegrity
```

## Local evidence

默认目录：

```text
data/analysis-workspace/
  sessions/
    session_one.json
  experiments/
    exp_<timestamp>_<id>/
      manifest.json
      playwright/
        screenshots/
        traces/
      js-reverse/
        capture-<uuid>/
      reports/
```

`web_rev_action` 把 `experiment_id` 作为受限 `artifactNamespace` 传给 `js-reverse-mcp`，因此两个本地进程必须看到同一个 evidence 目录。

该目录是 Action 服务本机的普通文件夹，不包含 Git/PR 语义，也不等于 GitHub Gateway workspace。GPT 通过 `inspectBrowserEvidence` 读取、搜索和分页；跨环境搬运使用 `runBrowserExperiment(export_experiment)` 创建 ZIP。

## Credential artifacts

完整 request/response headers 可能包含 Cookie、Authorization、CSRF 或 Set-Cookie。

Artifact descriptor 可以声明：

```text
sensitivity = credential
containsCredentials = true
redactedArtifactId = ...
```

`read_artifact` 默认读取关联的脱敏 artifact：

```json
{
  "operation": "read_artifact",
  "payload": {
    "experiment_id": "exp_...",
    "artifact_id": "art_...",
    "credential_mode": "redacted"
  }
}
```

自用本地重放需要原值时可显式使用 `credential_mode=full`。

## Configuration

复制 `.env.example` 到 `.env`。

最小浏览器配置：

```dotenv
WEB_REV_BROWSER_CDP_URL=http://127.0.0.1:9222
WEB_REV_EVIDENCE_DIR=C:/path/to/web_rev_action/data/analysis-workspace
WEB_REV_PLAYWRIGHT_CLI=playwright-cli
WEB_REV_JS_REVERSE_COMMAND=js-reverse-mcp
```

默认情况下，后端自动用以下参数启动私有 MCP：

```text
--browserUrl <WEB_REV_BROWSER_CDP_URL>
--allowedRoots <WEB_REV_EVIDENCE_DIR>
--streamArtifactRoot 0
```

需要自定义完整参数时：

```dotenv
WEB_REV_JS_REVERSE_ARGS=["--browserUrl","http://127.0.0.1:9222","--allowedRoots","C:/workspace","--streamArtifactRoot","0"]
```

`WEB_REV_JS_REVERSE_ARGS` 必须是 JSON 字符串数组。

Skill 与 Action 服务配置：

```dotenv
SKILL_TEMPLE_SERVER_URL=https://example.com
SKILL_TEMPLE_SKILLS_DIR=C:/path/to/skills
SKILL_TEMPLE_BEARER_TOKEN=replace-with-a-long-random-secret
```

配置了 Bearer token 后，所有 `/v1/*` 路由需要：

```text
Authorization: Bearer <token>
```

## Install

要求：

- Python 3.11+
- Node.js 18+
- 可用的 `playwright-cli`
- 当前 stream PR 版本的 `js-reverse-mcp`
- 已开启 remote debugging 的 Chrome/Edge

安装：

```powershell
py -3 -m pip install -e .[dev]
```

启动：

```powershell
web-rev-action --host 127.0.0.1 --port 8765
```

兼容命令 `skill-temple` 仍保留。

OpenAPI：

```text
http://127.0.0.1:8765/openapi.json
```

## Example

打开 session：

```json
{
  "operation": "open_session",
  "payload": {
    "session_id": "chatgpt_research",
    "target": {
      "start_url": "https://example.com/app"
    }
  }
}
```

运行实验：

```json
{
  "operation": "capture_flow",
  "payload": {
    "session_id": "chatgpt_research",
    "objective": "capture the first conversation request and stream",
    "target": {
      "expected_url_contains": "/app"
    },
    "primary_request": {
      "url_contains": "/conversation",
      "method": "POST",
      "resource_types": ["fetch"],
      "mime_types": ["text/event-stream"],
      "expected_min_matches": 1,
      "expected_max_matches": 1,
      "allow_supporting_failures": true,
      "include_in_flight": false
    },
    "flow": [
      {
        "step_id": "fill_message",
        "action": "fill",
        "locator": {"placeholder": "Message"},
        "value": "hello"
      },
      {
        "step_id": "send",
        "action": "click",
        "locator": {"role": "button", "name": "Send"}
      }
    ],
    "wait_for": {
      "type": "default_done_marker",
      "request_matcher": {
        "url_contains": "/conversation",
        "method": "POST"
      }
    },
    "execution_mode": "job",
    "job_timeout_ms": 300000
  }
}
```

## Validation

```powershell
python -m ruff check .
python -m pytest
python -m skill_temple.evals evals/skill_queries.jsonl
```

阶段 1/2 测试覆盖：

- OpenAPI 的两个 Browser Action 和 `oneOf` schema。
- consequential 属性。
- page alignment。
- `stream start → flow → wait → stop` 顺序。
- 失败时仍 finalize。
- baseline 默认模型。
- primary/supporting integrity。
- Stop cancellation correlation。
- experiment namespace。
- include-in-flight 传递。
- screenshot/Trace/manifest artifact。
- credential 默认脱敏。
- 私有 MCP stream primitive 调用。
- 同一 CDP endpoint 和本地 evidence root 启动参数。
- 后台 job、running→completed 查询和 restart 后 interrupted 恢复。
- artifact 分页、搜索和不含凭据的显式 ZIP 导出。
- collector-side event predicate 和稳定 pageId。

## Next stages

尚未实现：

- `trace_request` 和 XHR/fetch breakpoint orchestration。
- capture diff。
- browser-context replay。
- external HTTP replay。
- Worker/Service Worker Target auto-attach。
- Pandora 实际站点实验和协议报告生成。

详细路线见 `PLAN.md`，分析方法见 `PANDORA_REPRODUCTION.md`。
