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

### `runBrowserExperiment`

支持：

```text
open_session
capture_baseline
capture_flow
close_session
```

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

GPT 不直接协调 start、click、wait、stop。

### `inspectBrowserEvidence`

只查询浏览器运行状态：

```text
get_session
list_experiments
get_experiment
get_stream_status
```

需要查看 `manifest.json`、`events.jsonl`、源码、schema、脚本或报告时，使用 workspace Actions。

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
network_idle
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
content
truncated
error
```

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
- 默认禁止网络下载和 secret/认证管理命令。

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
        traces/
      js-reverse/
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
  schemas/
  scripts/
  reports/
  notes/
```

`js-reverse-mcp` 和 `web_rev_action` 必须看到同一个目录。不存在 ZIP 导出层，也不存在另一个 Gateway workspace 同步层。

## Credentials

完整 headers 可能包含 Cookie、Authorization、CSRF 和 Set-Cookie。上游同时生成完整文件和 redacted 文件。

这是单用户本地工具，因此 workspace 工具不会再创建一套 `credential_mode` API。GPT 应默认读取 `*.redacted.json`；只有本地重放明确需要时才读取完整文件，并且不要把真实凭据复制到自然语言回复。

## Configuration

```dotenv
SKILL_TEMPLE_SERVER_URL=https://example.com
SKILL_TEMPLE_SKILLS_DIR=C:/path/to/project/skills
SKILL_TEMPLE_BEARER_TOKEN=replace-with-a-long-random-secret

WEB_REV_BROWSER_CDP_URL=http://127.0.0.1:9222
WEB_REV_EVIDENCE_DIR=C:/path/to/web_rev_action/data/analysis-workspace
WEB_REV_PLAYWRIGHT_CLI=playwright-cli
WEB_REV_JS_REVERSE_COMMAND=js-reverse-mcp
WEB_REV_WORKSPACE_SHELL=pwsh
WEB_REV_WORKSPACE_ALLOW_NETWORK=false
```

默认私有 MCP 参数：

```text
--browserUrl <WEB_REV_BROWSER_CDP_URL>
--allowedRoots <WEB_REV_EVIDENCE_DIR>
--streamArtifactRoot 0
```

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
```

详细路线见 `PLAN.md`，Pandora 分析方法见 `PANDORA_REPRODUCTION.md`。
