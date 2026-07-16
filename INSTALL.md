# 安装与启动指南

本文说明如何从 GitHub 拉取 `web_rev_action`、安装依赖、启动带 CDP 的浏览器，以及运行本地服务。

项目以 **Windows 10/11 + PowerShell 7** 为主要运行环境。

## 1. 环境要求

请先安装：

- Git。
- Python 3.11 或更高版本。
- Node.js 20.19 或更高版本，以及 npm。
- PowerShell 7，命令名为 `pwsh`。
- ripgrep，命令名为 `rg`。
- Google Chrome 或 Microsoft Edge。

检查基础命令：

```powershell
git --version
py -3 --version
node --version
npm --version
pwsh --version
rg --version
```

## 2. 首次拉取代码

### HTTPS

```powershell
git clone https://github.com/qqq694637644/web_rev_action.git
cd web_rev_action
git switch main
git pull --ff-only origin main
```

### SSH

已配置 GitHub SSH key 时，可以使用：

```powershell
git clone git@github.com:qqq694637644/web_rev_action.git
cd web_rev_action
git switch main
git pull --ff-only origin main
```

`main` 是可安装主线，不要从旧阶段分支安装。

## 3. 更新已有代码

以后更新时，在已有仓库目录执行：

```powershell
cd C:\path\to\web_rev_action
git status --short
git switch main
git pull --ff-only origin main
```

如果 `git status` 显示未提交修改，请先提交或暂存修改，再执行 `git pull`。不要使用
`git reset --hard` 清理本地内容，除非你明确接受永久删除未提交修改。

更新代码后重新安装一次 editable package：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
skill-temple-build-contracts `
  --protocol-root src/skill_temple/example_skills/browser-action-protocol
skill-temple-build-prompt `
  --skills-dir src/skill_temple/example_skills `
  --template GPT_ACTION_PROMPT.md `
  --output dist/GPT_INSTRUCTIONS.md
```

## 4. 创建 Python 虚拟环境

在仓库根目录执行：

```powershell
py -3 -m venv .venv
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1

python --version
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[dev]"
```

以后每次打开新的 PowerShell，都需要重新激活虚拟环境：

```powershell
cd C:\path\to\web_rev_action
.\.venv\Scripts\Activate.ps1
```

## 5. 安装浏览器工具

安装项目验证过的 Playwright CLI fork：

```powershell
git clone https://github.com/qqq694637644/playwright-cli.git
cd playwright-cli
git checkout 793cfb32572733cbcb401e6f28d05a7a914ce408
npm ci
npm link
Get-Command playwright-cli
```

本项目依赖 `qqq694637644/js-reverse-mcp` 的 `main` 分支，其中包含
`web_rev_action` 使用的流式捕获能力。不要用 npm registry 中的同名发布包代替。

把仓库克隆到与 `web_rev_action` 同级的目录，安装依赖并构建：

```powershell
cd ..
git clone https://github.com/qqq694637644/js-reverse-mcp.git
cd js-reverse-mcp
git checkout 5e4d61aced29636f8249d5c3bce168ab3aaa6588

npm ci
npm run build
npm link

Get-Command js-reverse-mcp
js-reverse-mcp --help
```

`npm link` 会根据该仓库 `package.json` 的 `bin` 配置创建全局
`js-reverse-mcp` 命令，实际入口是：

```text
js-reverse-mcp/build/src/index.js
```

更新 fork commit 前，必须先在 `web_rev_action` 的真实三仓验证中修改固定 SHA 并通过
`browser_action_smoke.py`。不要仅安装 npm registry 的同名版本来代替 fork 合同。

重新构建当前固定版本：

```powershell
cd C:\path\to\js-reverse-mcp
git checkout 5e4d61aced29636f8249d5c3bce168ab3aaa6588
npm ci
npm run build
npm link
```

如果 npm 全局命令不在 `PATH`，运行：

```powershell
npm config get prefix
```

将该目录对应的可执行文件目录加入用户 `PATH`，然后重新打开 PowerShell。

## 6. 启动带 CDP 调试端口的浏览器

使用独立 profile 启动 Chrome，不要复用日常浏览器 profile：

```powershell
New-Item -ItemType Directory -Force .\data\chrome-profile | Out-Null

& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$((Get-Location).Path)\data\chrome-profile"
```

如果 Chrome 安装在其他位置，请修改可执行文件路径。Edge 使用相同参数，将可执行文件换成
`msedge.exe` 即可。

保持浏览器进程运行，并在该浏览器中打开需要分析的网站。

检查 CDP：

```powershell
Invoke-RestMethod http://127.0.0.1:9222/json/version
```

## 7. 创建 `.env`

服务读取**当前工作目录**中的 `.env`，因此应始终从仓库根目录启动。

生成本地 bearer token：

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

在仓库根目录创建 `.env`：

```dotenv
SKILL_TEMPLE_BEARER_TOKEN=replace-with-generated-token

WEB_REV_BROWSER_CDP_URL=http://127.0.0.1:9222
WEB_REV_EVIDENCE_DIR=C:/path/to/web_rev_action/data/analysis-workspace
WEB_REV_PLAYWRIGHT_CLI=playwright-cli
WEB_REV_JS_REVERSE_COMMAND=js-reverse-mcp
WEB_REV_WORKSPACE_SHELL=pwsh
WEB_REV_WORKSPACE_ALLOW_NETWORK=false
```

真实三仓验证可额外指定 fork 的精确 Node 入口，避免 `npx` 从 registry 解析包：

```dotenv
WEB_REV_PLAYWRIGHT_CLI_ENTRY=C:/path/to/playwright-cli/playwright-cli.js
WEB_REV_CHROME_EXECUTABLE=C:/path/to/chrome.exe
```

`WEB_REV_CHROME_EXECUTABLE` 也可指向 Playwright 安装的 Chromium；Linux CI 会从 Python
Playwright 的固定 runtime 自动写入该值。

可选配置：

```dotenv
# 本地调试不需要设置。通过 HTTPS 隧道或反向代理接入 GPT Action 时再填写。
SKILL_TEMPLE_SERVER_URL=https://your-public-host.example

# 仅在显式部署自定义 Skill 目录时设置；默认使用 Python package 内置 Skills。
# 自定义目录必须包含 browser-action-protocol。修改其 SKILL.md 后，必须针对该目录
# 重新运行 skill-temple-build-contracts 和 skill-temple-build-prompt。
SKILL_TEMPLE_SKILLS_DIR=C:/path/to/custom/skills

# 通常不需要设置。需要追加 js-reverse-mcp 参数时，必须填写 JSON 字符串数组；
# 不能覆盖 browser URL、allowed roots 或 stream artifact root。
# WEB_REV_JS_REVERSE_EXTRA_ARGS=["--logFile","C:/path/to/js-reverse.log"]
```

`.env`、`.env.*` 和 `.venv/` 已被 `.gitignore` 忽略。不要把本机 token 或绝对路径提交到 Git。

## 8. 启动服务

确认：

- 虚拟环境已激活；
- Chrome 或 Edge 的 9222 调试端口仍在运行；
- 当前目录是仓库根目录。

启动：

```powershell
web-rev-action --host 127.0.0.1 --port 8765
```

也可以使用模块入口：

```powershell
python -m skill_temple.app --host 127.0.0.1 --port 8765
```

服务固定使用单 worker。同一个 `WEB_REV_EVIDENCE_DIR` 不能同时启动第二个服务进程。

## 9. 检查安装结果

另开一个 PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

正常结果包含：

```text
status     : ok
skills_dir : ...
```

OpenAPI：

```text
http://127.0.0.1:8765/openapi.json
```

Skill 目录已在构建后的 Instructions 中，不通过 Action 动态查询。验证精确加载：

```powershell
$headers = @{
  Authorization = "Bearer $env:SKILL_TEMPLE_BEARER_TOKEN"
  "Content-Type" = "application/json"
}

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/v1/skills/load `
  -Headers $headers `
  -Body '{"skill_ids":["browser-action-protocol"]}'
```

Browser Actions 只接受稳定的六字段、版本绑定 envelope。先取得当前 Skill 和 operation
hash，再发送请求：

```powershell
$loaded = Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/v1/skills/load `
  -Headers $headers `
  -Body '{"skill_ids":["browser-action-protocol"]}'
$skillHash = $loaded.skills[0].content_hash

$contracts = Get-Content `
  .\src\skill_temple\example_skills\browser-action-protocol\docs\generated\operation-contracts.json `
  -Raw | ConvertFrom-Json
$contractHash = ($contracts.operations | Where-Object operation -eq 'list_experiments').operation_contract_hash

$body = @{
  contract_version = '2.0'
  operation = 'list_experiments'
  payload_json = '{"limit":10}'
  skill_id = 'browser-action-protocol'
  skill_content_hash = $skillHash
  operation_contract_hash = $contractHash
} | ConvertTo-Json -Compress

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/v1/browser/inspect `
  -Headers $headers `
  -Body $body
```

`stale_operation_contract` 表示 dispatch 尚未开始。重新加载协议 Skill、读取精确 operation
合同并重建六字段 envelope；不要猜测或截断 hash。

旧的 Skill retrieve/search endpoint、Browser 顶层 `payload` 对象和 baseline alias 均已删除，
不会自动转换。

## 10. 更新 GPT Builder

每次 Skill、description 或根 Instructions 修改后执行：

```powershell
skill-temple-build-contracts `
  --protocol-root src/skill_temple/example_skills/browser-action-protocol

skill-temple-build-prompt `
  --skills-dir src/skill_temple/example_skills `
  --template GPT_ACTION_PROMPT.md `
  --output dist/GPT_INSTRUCTIONS.md
```

把生成的 `dist/GPT_INSTRUCTIONS.md` 完整复制到 GPT Builder 的 Instructions，然后重新导入
`http://127.0.0.1:8765/openapi.json`。重新导入后确认公开 operationId 只有新 Skill Actions、
两个 Browser Actions 和 Workspace Actions。

导入前运行：

```powershell
skill-temple-builder-preflight --root .
```

导入后逐项执行 `BUILDER_SMOKE_CHECKLIST.md`。真实 Builder smoke 是发布硬门槛；本地
preflight 和 CI 只验证输入与 schema，不能证明 Builder 缓存、模型选择和登录态浏览器调用。

服务监听 `127.0.0.1` 时仅本机可访问。需要远程接入 GPT Action 时，应自行准备 HTTPS
反向代理或隧道，并设置 `SKILL_TEMPLE_SERVER_URL`。不要直接把无 TLS 的 8765 端口暴露到公网。

## 11. 可选验证

```powershell
python -m ruff check .
python -m pytest
node --test tests/runtime/replay_runtime.test.js
python -m skill_temple.evals evals/skill_queries.jsonl
python -m skill_temple.dead_code_audit --root .
skill-temple-build-contracts --protocol-root src/skill_temple/example_skills/browser-action-protocol
skill-temple-build-prompt --skills-dir src/skill_temple/example_skills --template GPT_ACTION_PROMPT.md --output dist/GPT_INSTRUCTIONS.md
python -m skill_temple.builder_preflight --root .
git diff --exit-code -- src/skill_temple/example_skills/browser-action-protocol
```

真实浏览器工具链验证：

```powershell
python tools/toolchain_validation.py `
  --js-reverse-entry <js-reverse-mcp>/build/src/index.js

python tools/browser_action_smoke.py `
  --js-reverse-entry <js-reverse-mcp>/build/src/index.js
```
