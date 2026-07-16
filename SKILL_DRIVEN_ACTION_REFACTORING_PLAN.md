# Skill 驱动的 GPT Action 与 Skill 加载改造计划

## 1. 文档目的

本文给出 `web_rev_action` 的完整改造方案，统一解决两个相互关联的问题：

1. Browser Action 当前使用复杂 `payload.oneOf`，经 GPT Actions 导入后可能丢失 `payload`；
2. 当前 Skill Runtime 仍通过 `retrieveSkillContext` 动态返回目录、选择结果和 Skill 正文，增加了公开 Action 数量、运行时分支和上下文不确定性。

改造后的系统采用以下原则：

```text
Skill 目录在构建时编译进 GPT Instructions
GPT-5.6 根据静态目录自行选择 Skill
loadSkills 只按精确 skill_id 加载完整 SKILL.md
readSkillContent 只按精确相对路径读取引用资料
Skill 渐进披露 operation 调用合同
Browser Action 只运输稳定的 operation + payload_json
后端 Pydantic 继续承担严格领域验证
```

Skill 加载方式以 `qqq694637644/skill_temple` 的 compiled-catalog 模型为基线。参考基准提交：

```text
d6804cb648356c9576f1ef13cdcc75e90ead8b2c
```

本文是实施基线。后续代码、Skill、测试、GPT Instructions、OpenAPI 和 Builder 发布流程都应遵守本文定义的边界。

本次改造采用**破坏式更新**：不保留旧接口、旧请求格式、旧 operation alias、隐藏搜索路径、双写逻辑或兼容窗口。开发阶段应让所有未迁移调用、文档、测试和配置立即失败，以便一次性暴露并修复真实问题。

---

## 2. 当前架构与问题

## 2.1 当前公开 Skill Actions

当前项目向 GPT 公开：

```text
retrieveSkillContext
searchSkillDocs
readSkillContent
```

其中 `retrieveSkillContext` 同时承担：

- 动态返回 Skill 目录；
- 处理 hint 和 `$skill` / `@skill`；
- 加载一个或多个 `SKILL.md`；
- 做响应预算和截断；
- 返回下一步决策字段。

`searchSkillDocs` 又在运行时为 Skill 内文档建立搜索索引并返回候选片段。

这套实现功能较多，但与当前项目目标存在冲突：

- GPT 每次任务可能先消耗一次 Action 查询目录；
- 目录可能因为响应预算被截断；
- 服务端和模型共同参与 Skill 选择，职责不清；
- Skill 正文加载协议复杂；
- 多出一个搜索 Action；
- FTS、ranking、resolve、catalog budget 等实现并不是 Browser 分析核心能力；
- Skill 增加后，运行时返回目录会进一步占用 Action 响应和上下文预算。

## 2.2 `skill_temple` 的目标加载模型

目标模型不在运行时做语义路由：

```text
构建阶段
  扫描 SKILL.md
  -> 提取 name + description + skill_id
  -> 编译进 GPT Instructions 的 {{SKILL_CATALOG}}

运行阶段
  GPT 初始上下文已看到完整 Skill 目录
  -> GPT 根据 description 自行选择精确 skill_id
  -> loadSkills([skill_id...])
  -> 返回完整 SKILL.md 的 Codex 风格 <skill> 上下文块
  -> SKILL.md 明确引用资料时 readSkillContent 精确读取
```

核心约束：

- 不通过公开 Action 查询 Skill 目录；
- 服务端不判断哪个 Skill 语义匹配用户任务；
- `loadSkills` 只做精确 ID 加载；
- `readSkillContent` 只做安全相对路径读取；
- 目录只包含 metadata，不包含 Skill 正文；
- Skill 正文只在被选中后进入上下文；
- docs、references、scripts、assets 继续按需披露。

## 2.3 当前 Browser Action 故障

当前 `browser_routes.py` 把内部 Pydantic discriminated union 转换为：

```text
operation: enum
payload:
  type: object
  oneOf:
    - OpenSessionPayload
    - CaptureFlowPayload
    - ReplayRequestPayload
    - ...
```

该结构在本地 OpenAPI 存在，但 GPT Actions 导入后可能只保留 `operation`，导致：

```text
不传 payload
  -> 服务端返回 payload 缺失

尝试传 payload
  -> Action 工具入口拒绝未暴露字段
```

根因不是 GPT-5.6 无法理解 operation，而是公开 Action schema 承担了过多内部类型系统。

## 2.4 两个问题的共同根因

当前系统把以下职责压在公开 Actions 中：

1. Skill 目录发现和语义选择；
2. Skill 正文加载；
3. Skill 文档搜索；
4. operation 调用知识；
5. operation-specific payload 类型；
6. Browser 原子执行。

目标架构必须将它们拆开：

```text
Instructions：静态 Skill 目录
GPT-5.6：Skill 选择和实验规划
Skill Actions：精确加载和精确读取
Skill 内容：operation 调用知识
Browser Action：稳定传输
Pydantic：严格领域合同
Browser Service：原子执行
Evidence Store：事实来源
```

---

## 3. 不可破坏的约束

## 3.1 少量稳定 Actions

改造后公开 Skill Actions 只有两个：

```text
loadSkills
readSkillContent
```

公开 Browser Actions 仍然只有两个：

```text
inspectBrowserEvidence
runBrowserExperiment
```

不得把每个 Skill、文档或 Browser operation 拆成独立 Action。

## 3.2 operation 可持续增加

新增 operation 不应要求：

- 新增 GPT Action；
- 把全部 operation 字段写进 GPT Instructions；
- 把全部 operation 合同放进一个超长 `SKILL.md`；
- 手工维护第二套 OpenAPI union；
- 重新实现服务端语义路由。

## 3.3 Skill 选择由模型完成

服务端不得根据 query 做关键词、FTS、embedding 或自定义 scoring 来替模型选择 Skill。

模型只根据编译进 Instructions 的：

```text
name
description
skill_id
```

决定是否调用 `loadSkills`。

## 3.4 逐级披露

Skill 内容分三级进入上下文：

```text
第一级：Instructions 中的 Skill catalog metadata
第二级：loadSkills 返回被选 Skill 的完整 SKILL.md
第三级：readSkillContent 返回当前阶段明确引用的单个资料
```

不得在一级目录中包含正文、operation schema 或大段示例。

## 3.5 后端严格类型化

公开 Browser Action 使用简单传输格式，但后端必须保留：

- operation-specific Pydantic models；
- discriminated unions；
- `extra="forbid"`；
- `Literal`；
- `Field` 约束；
- model validators；
- operation-specific 安全策略。

## 3.6 外部副作用安全

只有错误明确返回：

```text
dispatch_started=false
```

模型才可修改参数并重试。

若返回：

```text
dispatch_started=true
outcome=unknown
```

必须先 inspect session 或 experiment，禁止直接重复 consequential operation。

## 3.7 证据优先

Skill 不得把文档、历史经验或文件名当作当前站点事实。协议结论继续绑定：

```text
experiment_id
evidence_id
observation_id
artifact_id
```

## 3.8 破坏式迁移

改造版本发布后只接受新合同：

```text
Skill：loadSkills + readSkillContent
Browser：contract_version=2.0 + operation + payload_json
Capture：capture_flow
```

必须在同一个变更集中删除：

- `retrieveSkillContext` 公开路由、模型、Instructions 和测试；
- `searchSkillDocs` 公开路由、索引、Instructions 和测试；
- 旧 Browser `payload` 请求格式和 OpenAPI 后处理；
- `capture_baseline` model、dispatcher 分支、preset、文档和测试；
- 任何旧字段到新字段的自动转换；
- 任何旧 operation 到新 operation 的 alias；
- 任何新旧响应双写；
- 任何“未找到新路径时再走旧路径”的兜底。

旧调用应直接得到明确失败，不得静默转换。开发、测试和 Builder smoke 都只验证新合同。

---

## 4. 目标总体架构

```text
Build / release
  -> 扫描所有 SKILL.md frontmatter
  -> 编译 name + description + skill_id 到 GPT Instructions
  -> 生成 dist/GPT_INSTRUCTIONS.md
  -> 生成 openapi.json
  -> 将 Instructions 和 OpenAPI 导入 Custom GPT

Runtime
  用户任务
    -> GPT-5.6 从静态 catalog 选择最小 Skill 集合
    -> loadSkills 精确加载 SKILL.md
    -> 完整阅读 <skill> 内容
    -> readSkillContent 按 Skill 明确路径读取当前资料
    -> Skill 决定 Browser operation
    -> 读取当前 operation contract
    -> GPT-5.6 生成 operation + payload_json
    -> Browser Action 运输固定 envelope
    -> 后端解析并用 TypeAdapter 严格校验
    -> dispatcher 执行
    -> 返回状态和证据句柄
    -> Skill 决定下一步
```

---

## 5. Skill 加载方式改造

## 5.1 构建时编译 Skill Catalog

新增：

```text
src/skill_temple/prompt_builder.py
```

提供：

```python
CATALOG_PLACEHOLDER = "{{SKILL_CATALOG}}"

def render_catalog(runtime: SkillRuntime) -> str: ...

def build_instructions(*, runtime, template_path, output_path) -> Path: ...
```

目录行格式固定为：

```text
- current-site-analysis: Analyze an unknown current webpage... (skill_id: current-site-analysis)
- browser-action-protocol: Load exact Browser Action transport and operation contracts... (skill_id: browser-action-protocol)
```

要求：

- description 合并多余空白；
- 不包含 Skill 正文；
- 不包含引用资料；
- 输出顺序确定；
- 缺少 `{{SKILL_CATALOG}}` 直接失败；
- 输出统一 LF；
- Skill 增删或 description 修改后必须重新生成。

默认：

```text
模板：GPT_ACTION_PROMPT.md
输出：dist/GPT_INSTRUCTIONS.md
```

## 5.2 `GPT_ACTION_PROMPT.md` 改为模板

模板必须包含：

```text
{{SKILL_CATALOG}}
```

Instructions 中说明：

- 目录已在当前上下文中，不调用 Action 查询目录；
- 用户显式指定 Skill 或 description 明确匹配时调用 `loadSkills`；
- 多个 Skill 确有帮助时可一次精确加载，但只加载最小集合；
- `loadSkills` 返回完整 `<skill>...</skill>`；
- 完整阅读 `SKILL.md` 后再执行；
- 只有 `SKILL.md` 明确引用资料时才调用 `readSkillContent`；
- 截断时按 `next_start_line` 继续；
- 没有匹配 Skill 时直接处理，不强行加载；
- 不调用隐藏目录或 console endpoint 完成普通任务。

根 Instructions 不包含：

- 所有 Skill 正文；
- 所有 Browser operation；
- operation payload 字段；
- Pandora 六场景正文；
- 动态 catalog 响应规则。

## 5.3 新增 `loadSkills`

公开 endpoint：

```text
POST /v1/skills/load
operationId: loadSkills
x-openai-isConsequential: false
```

请求：

```json
{
  "skill_ids": [
    "current-site-analysis",
    "browser-action-protocol"
  ]
}
```

运行时行为：

- 只接受精确 `skill_id`；
- 保持输入顺序；
- 去重；
- 未知 ID 整体失败并返回 `skill_not_found`；
- 不做 query 语义匹配；
- 不自动展开依赖；
- 不自动读取 docs；
- 返回每个 Skill 的完整 `SKILL.md`；
- 返回 content hash 和被入口明确引用的路径。

响应：

```json
{
  "skills": [
    {
      "skill_id": "current-site-analysis",
      "name": "current-site-analysis",
      "description": "...",
      "source_path": "current-site-analysis/SKILL.md",
      "content": "<skill>...完整 SKILL.md...</skill>",
      "content_hash": "sha256:...",
      "referenced_paths": ["docs/..."]
    }
  ],
  "loaded_skill_ids": ["current-site-analysis"]
}
```

Codex 风格上下文块固定为：

```xml
<skill>
<name>current-site-analysis</name>
<path>current-site-analysis/SKILL.md</path>
完整 SKILL.md
</skill>
```

项目策略建议一次最多加载 3 个 Skill：

```text
1 primary workflow
1 protocol
1 specialized supporting workflow
```

限制应在 request model 或 Instructions 中明确，避免一次加载整个目录。

## 5.4 保留并简化 `readSkillContent`

公开 endpoint：

```text
POST /v1/skills/read
operationId: readSkillContent
x-openai-isConsequential: false
```

请求：

```json
{
  "skill_id": "browser-action-protocol",
  "path": "docs/run/open-session.md",
  "start_line": 1,
  "max_lines": 300
}
```

要求：

- path 必须是 Skill 目录内相对路径；
- 禁止绝对路径；
- 禁止 `..` 逃逸；
- 只读取文件；
- 返回行号、总行数、hash、truncated、next_start_line；
- 截断续读时必须确保位置前进；
- `SKILL.md` 也可通过该接口续读，但正常 `loadSkills` 应返回完整入口。

## 5.5 移除公开 `retrieveSkillContext`

删除公开 endpoint：

```text
POST /v1/skills/retrieve
operationId: retrieveSkillContext
```

删除公开模型：

```text
RetrieveSkillContextRequest
RetrieveSkillContextResponse
SelectedSkillPacket
AvailableSkillMetadata
Decision
```

不再向 GPT 返回：

```text
available_skills
omitted_skill_count
descriptions_truncated
explicit_skill_ids
unknown_skill_mentions
decision.next_action
```

原因：目录在构建时已进入 Instructions，运行时不应再次查询或裁剪。

## 5.6 移除公开 `searchSkillDocs`

删除公开 endpoint：

```text
POST /v1/skills/search
operationId: searchSkillDocs
```

运行时不再为 GPT 提供 Skill 文档搜索 Action。

每个 `SKILL.md` 必须明确列出可按需读取的资料路径。模型通过精确路径使用 `readSkillContent`。

如果某个 Skill 的资料很多，应改进 `SKILL.md` 的索引和路由，而不是恢复公共全文搜索。

同时删除服务端 Skill 文档全文搜索、SQLite FTS、ranking 和相关调试入口。开发工具也不得保留另一套搜索路径；资料不可达应由 Skill 索引测试直接暴露。

## 5.7 Runtime 简化

`runtime.py` 最终只保留核心能力：

```text
扫描 SKILL.md
解析 frontmatter
验证 name / description
缓存 content_hash
list_skills（仅隐藏调试和 prompt build）
load_skills（精确 ID）
read（精确相对路径）
发现入口引用路径
```

删除公开流程不再需要的运行时复杂度：

```text
resolve
retrieve
catalog response budget
query tokenization
@/$ mention解析
SQLite FTS index
search ranking
heading ranking
API symbol ranking
search response clipping
server-side selection reason
```

不保留隐藏搜索实现。缺少明确引用路径时，Skill 合同测试必须失败。

## 5.8 Skill 目录来源

保留查找顺序：

```text
1. create_app / CLI 显式 skills_dir
2. SKILL_TEMPLE_SKILLS_DIR
3. 当前目录 .env
4. 当前目录 skills/
5. 包内 example_skills
```

当前项目实际 Skill 位于：

```text
src/skill_temple/example_skills/
```

P0 固定继续使用当前 package data 路径，不同时引入根目录 `skills/`。`SKILL_TEMPLE_SKILLS_DIR` 只用于显式部署配置，不作为找不到 package data 时的容错路径。

## 5.9 CLI 与 pyproject

新增命令：

```text
skill-temple-build-prompt
```

建议 `pyproject.toml`：

```toml
[project.scripts]
skill-temple-build-prompt = "skill_temple.prompt_builder:main"
```

构建命令：

```powershell
skill-temple-build-prompt `
  --skills-dir src/skill_temple/example_skills `
  --template GPT_ACTION_PROMPT.md `
  --output dist/GPT_INSTRUCTIONS.md
```

## 5.10 发布流程

每次 Skill 增删或 description 修改后：

```text
运行 prompt builder
检查 dist/GPT_INSTRUCTIONS.md diff
运行 Skill catalog tests/evals
部署后端
生成 openapi.json
在 GPT Builder 更新 Instructions
在 GPT Builder 重新导入 OpenAPI
执行真实 smoke
```

`dist/GPT_INSTRUCTIONS.md` 提交到仓库，便于 review 和识别 catalog 漂移；CI 重新生成后要求工作树无差异。

---

## 6. Browser Action 传输协议改造

## 6.1 固定 Envelope

两个 Browser Action 统一使用：

```json
{
  "contract_version": "2.0",
  "operation": "open_session",
  "payload_json": "{\"session_id\":\"analysis-main\",\"deadline_ms\":30000}"
}
```

P0 公开字段只有：

```text
contract_version
operation
payload_json
```

后续版本绑定字段必须扁平：

```text
skill_id
skill_content_hash
operation_contract_hash
```

不得公开嵌套 `skill_binding`。

## 6.2 使用 `payload_json` 的原因

公开 Action schema 不再承担 operation-specific 类型系统，只运输简单字符串。

首版请求 schema 不含：

```text
oneOf
anyOf
allOf
discriminator
operation-specific $ref
开放嵌套 payload object
```

具体 JSON 结构由 `browser-action-protocol` Skill 按需披露，后端再严格验证。

## 6.3 operation 使用普通字符串

P0：

```yaml
operation:
  type: string
```

不公开完整 enum，原因：

- operation 持续增长；
- enum 会永久披露全部操作；
- 新 operation 会要求重导 Action schema；
- 破坏 Skill 渐进披露；
- 后端可返回结构化 `unknown_operation`。

## 6.4 公开 Envelope 模型

新增：

```python
class RunBrowserExperimentEnvelope(StrictModel):
    contract_version: Literal["2.0"] = "2.0"
    operation: str
    payload_json: str = Field(min_length=2, max_length=262_144)


class InspectBrowserEvidenceEnvelope(StrictModel):
    contract_version: Literal["2.0"] = "2.0"
    operation: str
    payload_json: str = Field(min_length=2, max_length=262_144)
```

P2 再增加扁平 hash 字段。

## 6.5 内部 Canonical Request 保留

以下继续作为领域权威合同：

```text
RunBrowserExperimentRequest
InspectBrowserEvidenceRequest
所有 operation-specific payload models
```

不得降级为 `dict[str, Any]`。

## 6.6 严格解析

新增：

```text
decode_run_envelope
decode_inspect_envelope
```

规则：

- 长度限制；
- 标准 JSON；
- 顶层必须是 object；
- 拒绝重复 key；
- 拒绝 NaN / Infinity；
- 不执行表达式；
- 不做插值；
- 不读取文件；
- 解析失败保证 `dispatch_started=false`。

## 6.7 TypeAdapter 验证

```python
RUN_REQUEST_ADAPTER = TypeAdapter(RunBrowserExperimentRequest)
INSPECT_REQUEST_ADAPTER = TypeAdapter(InspectBrowserEvidenceRequest)
```

转换：

```python
RUN_REQUEST_ADAPTER.validate_python({
    "contract_version": "1.0",
    "operation": envelope.operation,
    "payload": decoded_payload,
})
```

公开 transport 版本与内部 domain 版本独立：

```text
Action transport contract: 2.0
Browser domain contract: 1.0
```

## 6.8 删除 Browser OpenAPI 后处理

直接删除：

```text
_request_object_schema
normalize_browser_action_openapi
app.openapi 中的 Browser schema 替换
```

公开 OpenAPI 必须由真实 Envelope request model 直接生成。

---

## 7. 结构化错误与恢复

统一错误示例：

```json
{
  "error": {
    "code": "invalid_operation_payload",
    "operation": "open_session",
    "dispatch_started": false,
    "issues": [
      {
        "path": "/payload/session_id",
        "type": "string_type",
        "message": "Input should be a valid string"
      }
    ],
    "suggested_next_action": "Read browser-action-protocol/docs/run/open-session.md and correct payload_json."
  }
}
```

必须覆盖：

```text
invalid_json
payload_must_be_object
unknown_operation
invalid_operation_payload
stale_operation_contract
browser_busy
session_busy
operation_outcome_unknown
```

`dispatch_started` 定义：

- JSON 解析失败：false；
- Pydantic 校验失败：false；
- unknown operation：false；
- session 预检查失败：false；
- 已进入 browser adapter 后连接中断：true；
- 已开始但未获终态：true + outcome=unknown。

Skill 规则：

```text
dispatch_started=false
  -> 可按错误路径修正一次

dispatch_started=true 或 outcome=unknown
  -> 先 inspect session / experiment
  -> 不直接重复 run operation
```

---

## 8. Skill 总体设计

## 8.1 Skill 三层分类

### A. 协议 Skill

负责 Browser Action envelope、operation 合同、错误恢复和调用示例。

### B. 通用工作流 Skill

负责 session、capture、evidence、replay、script tracing、stream diagnosis 等方法。

### C. 领域复现 Skill

负责 Pandora 或其他站点特定的实验假设、比较矩阵和报告。

## 8.2 不是一个 operation 一个 Skill

operation 合同放在协议 Skill 的 docs 中：

```text
browser-action-protocol/docs/run/open-session.md
browser-action-protocol/docs/run/capture-flow.md
browser-action-protocol/docs/inspect/get-session.md
...
```

工作流 Skill 只引用需要的精确合同。

## 8.3 Skill Frontmatter

P0 保持最小必需字段：

```yaml
---
name: browser-action-protocol
description: Use when calling inspectBrowserEvidence or runBrowserExperiment; provides exact transport and operation contracts.
---
```

`name` 同时是稳定 `skill_id`。

P1 可增加只用于生成器和 eval 的可选 metadata：

```yaml
requires:
  - browser-action-protocol
supports:
  - replay_request
  - get_network_evidence
```

但运行时不自动递归加载依赖。GPT 根据已编译 catalog 和 Skill 指令显式调用 `loadSkills`。

## 8.4 Skill 路由原则

模型初始上下文已经包含目录，直接选择：

| 用户任务 | Primary Skill | Supporting Skills |
|---|---|---|
| 分析未知当前网页 | `current-site-analysis` | `browser-action-protocol` |
| 打开 session 或最小 capture | `browser-session-capture` | `browser-action-protocol` |
| 查找请求或证据 | `browser-evidence-inspection` | `browser-action-protocol` |
| replay 或 mutation | `browser-request-replay` | `browser-evidence-inspection`, `browser-action-protocol` |
| 脚本调用链 | `browser-script-tracing` | `browser-evidence-inspection`, `browser-action-protocol` |
| SSE/NDJSON/stream/stop | `browser-stream-diagnostics` | `browser-action-protocol` |
| 失败恢复 | `browser-experiment-recovery` | `browser-action-protocol` |
| Pandora 协议复现 | `pandora-protocol-reproduction` | `current-site-analysis`, `browser-action-protocol` |

一次调用不超过 3 个 Skill。不同阶段需要新的 supporting Skill 时再调用 `loadSkills`，不预加载全部。

---

## 9. 必须新增的 Skill

## 9.1 `browser-action-protocol`（P0 必须）

### 定位

所有 Browser Action 调用的权威协议 Skill。

它负责：

- `operation + payload_json` envelope；
- JSON 编码规则；
- run/inspect operation 映射；
- 每个 operation 的 decoded payload 合同；
- 完整 Action 调用示例；
- validation error 修复；
- outcome unknown 规则。

它不负责：

- 决定用户实验目标；
- 推断当前站点行为；
- 解释 Pandora 协议；
- 复制后端实现细节。

### 目录

```text
src/skill_temple/example_skills/browser-action-protocol/
  SKILL.md
  docs/
    transport-envelope.md
    json-encoding.md
    error-recovery.md
    operation-index.md
    run/
      open-session.md
      capture-flow.md
      replay-request.md
      save-script-source.md
      cancel-experiment.md
      close-session.md
    inspect/
      get-session.md
      list-experiments.md
      get-experiment.md
      get-stream-status.md
      list-evidence.md
      get-network-evidence.md
      get-request-shape.md
      get-request-initiator.md
      search-scripts.md
      get-script-source.md
      list-console-errors.md
```

### `SKILL.md` 必须说明

1. 调用 Browser Actions 前先读取当前 operation contract；
2. 固定 envelope 字段；
3. `payload_json` 是序列化 JSON object；
4. 不得把 decoded payload 字段提升到 Action 顶层；
5. 不得同时发送 `payload` 和 `payload_json`；
6. run 和 inspect operation 不可混用；
7. `dispatch_started=false` 才可修正重试；
8. outcome unknown 必须先 inspect；
9. operation index 路径；
10. 只读取当前步骤合同。

### 每个 operation contract 内容

```text
Operation
Action
Purpose
Consequential
Prerequisites
Decoded payload schema
Required fields
Optional fields/defaults
Constraints
Complete Action envelope example
Expected response handles
Safe retry rule
Typical errors
Next recommended inspect operation
Contract hash
```

完整调用示例：

```json
{
  "contract_version": "2.0",
  "operation": "open_session",
  "payload_json": "{\"session_id\":\"analysis-main\",\"deadline_ms\":30000}"
}
```

同时展示 decoded payload：

```json
{
  "session_id": "analysis-main",
  "deadline_ms": 30000
}
```

---

## 10. 建议新增的通用工作流 Skills

这些 Skill 在 P1 分阶段增加，不阻塞 P0。

## 10.1 `browser-session-capture`

覆盖：

```text
get_session
open_session
capture_flow
get_experiment
close_session
```

职责：

- session 复用与 stale 判断；
- page/target 选择；
- 最小 capture；
- job polling；
- 关闭条件；
- 避免重复 session 或 capture。

目录：

```text
browser-session-capture/
  SKILL.md
  docs/
    session-state-machine.md
    minimal-capture.md
    target-selection.md
    job-polling.md
```

## 10.2 `browser-evidence-inspection`

覆盖：

```text
list_experiments
get_experiment
list_evidence
get_network_evidence
get_request_shape
get_request_initiator
list_console_errors
```

职责：

- bounded summary -> exact evidence；
- evidence 选择和关联；
- fact/inference 分离；
- evidence gap；
- 稳定句柄引用。

## 10.3 `browser-request-replay`

覆盖：

```text
get_network_evidence
get_request_shape
replay_request
get_experiment
list_evidence
```

职责：

- source evidence；
- browser-context replay；
- 单变量 mutation；
- binding/extractor 顺序；
- streaming response；
- comparison；
- credential 安全。

## 10.4 `browser-script-tracing`

覆盖：

```text
get_request_initiator
search_scripts
get_script_source
save_script_source
```

职责：

- initiator stack；
- script 搜索策略；
- bounded source；
- 源码证据保存；
- bundle/minified 结论边界。

## 10.5 `browser-stream-diagnostics`

覆盖：

```text
get_stream_status
get_experiment
list_evidence
replay_request
cancel_experiment
```

职责：

- SSE / NDJSON / raw stream；
- transport vs semantic completion；
- partial/interrupted/failed；
- stop vs cancel；
- termination contract。

## 10.6 `browser-experiment-recovery`（P2 按 eval 决定）

职责：

- retry matrix；
- outcome unknown；
- stale session；
- stale contract；
- busy；
- cancel/close 顺序。

P1 可先把规则保存在 `browser-action-protocol/docs/error-recovery.md`。

---

## 11. 现有 Skill 改造

## 11.1 `current-site-analysis`

新定位：未知网页分析的通用编排 Skill。

必须修改：

1. frontmatter description 足够明确，能在静态 catalog 中被 GPT 选择；
2. 明确同时加载 `browser-action-protocol`；
3. 每个步骤链接精确 operation contract；
4. 所有示例使用完整 `payload_json` envelope；
5. 删除 Skill 中全部 `capture_baseline` 引用，只允许 `capture_flow`；
6. session 初始化前先 `get_session`；
7. 明确 background job polling；
8. 明确 validation retry 和 outcome unknown；
9. 保持 inventory -> capture -> evidence -> replay -> report；
10. 不复制全部 operation 字段。

不应包含：

- Pandora 固定语义；
- 所有协议合同；
- 后端内部实现；
- 运行时 Skill 目录查询说明。

## 11.2 `pandora-protocol-reproduction`

新定位：证据确认后的领域扩展 Skill。

必须修改：

1. description 明确只用于 Pandora 类对话树、regenerate/edit/stop/reload；
2. 入口要求已完成 current-site inventory；
3. 使用完整 `payload_json` envelope；
4. operation 合同引用 `browser-action-protocol`；
5. 六场景是可选实验模板，不是未知站点默认流程；
6. 保持 one-variable mutation；
7. 保持 evidence ID 绑定；
8. 不复制 payload 字段；
9. 不负责通用 Skill 加载逻辑；
10. 不假设站点符合 Pandora。

保留并更新：

```text
docs/experiment-matrix.md
docs/evidence-contract.md
docs/report-templates.md
```

---

## 12. Operation Registry 与合同生成

P1 新增 `OperationRegistry`：

```python
@dataclass(frozen=True)
class OperationSpec:
    name: str
    action: Literal["run", "inspect"]
    request_model: type[BaseModel]
    payload_model: type[BaseModel]
    handler_name: str
    consequential: bool
    contract_doc_path: str
```

Registry 是以下内容的唯一来源：

- operation 存在性；
- run/inspect 分类；
- payload model；
- handler；
- consequential；
- contract path/hash；
- 测试矩阵。

operation contract 的结构字段最终从：

```text
OperationRegistry + Pydantic model_json_schema()
```

生成。

人工维护：

```text
Purpose
Prerequisites
When to use/not use
Safe retry
Next operation
Security notes
Evidence boundary
```

CI 检查生成后工作树无差异。

`capture_baseline` 在 P0 直接删除：

- 删除 request model 和 union member；
- 删除 dispatcher 分支和 preset 转换；
- 删除 README、Skill、示例、smoke 和测试；
- 旧请求返回 `unknown_operation` 或请求模型校验失败；
- 不提供 replacement redirect，迁移说明只在本计划和发布说明中写明使用 `capture_flow`。

---

## 13. Skill 与 Operation 版本绑定

## P0

Browser Action 只传：

```text
contract_version
operation
payload_json
```

## P2

增加扁平字段：

```json
{
  "skill_id": "browser-action-protocol",
  "skill_content_hash": "sha256:...",
  "operation_contract_hash": "sha256:..."
}
```

后端在 consequential operation 前验证。

不一致返回：

```json
{
  "error": {
    "code": "stale_operation_contract",
    "dispatch_started": false,
    "expected_contract_hash": "sha256:...",
    "suggested_next_action": "Reload the exact Skill and operation contract before retrying."
  }
}
```

Manifest 记录：

```text
action_transport_version
operation
skill_id
skill_content_hash
operation_contract_hash
```

---

## 14. OpenAPI 目标

最终公开 operationId：

```text
loadSkills
readSkillContent
inspectBrowserEvidence
runBrowserExperiment
workspaceInspect
workspaceSearch
workspaceReadFiles
workspaceWriteFile
workspaceApplyPatch
workspaceExecPwsh
```

不再公开：

```text
retrieveSkillContext
searchSkillDocs
```

Skill Action request schema 必须简单：

```text
loadSkills: skill_ids[string[]]
readSkillContent: skill_id + path + start_line + max_lines
```

Browser Action request schema 必须简单：

```text
contract_version + operation + payload_json
```

---

## 15. 测试计划

## 15.1 Skill Runtime 单元测试

覆盖：

- 扫描 package Skills；
- frontmatter 必须有 name/description；
- name 是合法稳定 ID；
- duplicate ID 失败；
- `load_skills` 精确加载；
- 输入顺序保留；
- 去重；
- unknown ID 失败；
- Codex `<skill>` 上下文；
- referenced_paths；
- safe relative read；
- continuation；
- hash；
- 不包含 query semantic selection。

## 15.2 Prompt Builder 测试

覆盖：

- catalog 包含所有 Skill metadata；
- 不包含 Skill 正文；
- placeholder 被替换；
- 缺 placeholder 失败；
- 输出顺序稳定；
- description 空白规范化；
- `dist/GPT_INSTRUCTIONS.md` 重新生成无 diff。

## 15.3 OpenAPI 测试

断言公开 operationId 精确集合。

明确断言：

```text
loadSkills 存在
readSkillContent 存在
retrieveSkillContext 不存在
searchSkillDocs 不存在
```

递归检查 Browser request schema 不含：

```text
oneOf
anyOf
allOf
discriminator
operation-specific $ref
```

## 15.4 Skill 合同测试

检查：

- `browser-action-protocol/SKILL.md` 存在；
- 全部 operation 有合同；
- 每个合同含完整 Action envelope；
- `payload_json` 可 JSON decode；
- decoded payload 通过 Pydantic；
- 新 Skill 不使用 Action 参数 `payload`；
- 代码、OpenAPI、Skill、文档和测试中不存在 `capture_baseline`；
- 所有引用路径可由 `readSkillContent` 读取。

## 15.5 Browser Transport 测试

覆盖：

- 合法 JSON object；
- malformed JSON；
- 非 object 顶层；
- duplicate key；
- 超长输入；
- unknown operation；
- operation/payload 不匹配；
- run/inspect 混用；
- extra field；
- `dispatch_started=false`；
- outcome unknown。

## 15.6 Skill Evals

`evals/skill_queries.jsonl` 不模拟服务端语义路由，只验证：

- 编译目录中存在 expected_skill；
- 精确 `loadSkills` 成功；
- expected_paths 可达；
- expected_symbols 存在。

新增场景：

```text
current-site-analysis 选择描述
browser-action-protocol operation index
Pandora 专用描述不会匹配普通未知站点任务
replay Skill 找到 replay contract
stream Skill 找到 termination 文档
```

## 15.7 真实 GPT Builder Smoke

这是发布硬门槛：

1. 生成最终 Instructions；
2. 更新 GPT Builder Instructions；
3. 重新导入 OpenAPI；
4. 确认工具只有目标 operationId；
5. 让 GPT 从静态 catalog 选择 `current-site-analysis`；
6. 确认调用 `loadSkills`，不调用目录查询；
7. 确认按明确路径调用 `readSkillContent`；
8. 确认 Browser 工具签名含 `operation` 和 `payload_json`；
9. 执行 `get_session`；
10. 执行 `open_session`；
11. 故意提交错误 payload 并安全修正；
12. 执行最小 capture；
13. 查询终态；
14. 验证 outcome unknown 不直接重试。

## 15.8 GPT-5.6 指标

```text
静态 catalog Skill 选择正确率
loadSkills 精确 ID 成功率
平均加载 Skill 数量
平均读取 docs 数量
operation 选择正确率
首次 Browser 参数有效率
validation 自修复率
不安全重复调用率
证据句柄完整率
最终任务成功率
上下文 token 消耗
```

---

## 16. 实施阶段

## P0-A：Skill 加载方式迁移

修改：

```text
src/skill_temple/runtime.py
src/skill_temple/app.py
src/skill_temple/prompt_builder.py（新增）
GPT_ACTION_PROMPT.md
pyproject.toml
tests/test_runtime.py
tests/test_gpt_action_prompt.py
evals/skill_queries.jsonl
README.md
INSTALL.md
```

内容：

- 构建时 catalog；
- `loadSkills`；
- 精确 `readSkillContent`；
- 移除公开 `retrieveSkillContext`；
- 移除公开 `searchSkillDocs`；
- 简化 runtime；
- 生成 `dist/GPT_INSTRUCTIONS.md`；
- 更新 OpenAPI operation 集合。

完成标准：

- GPT 初始 Instructions 有完整 metadata catalog；
- 正常任务不调用 Action 查询目录；
- `loadSkills` 精确加载；
- 公开 schema 不再包含旧两个 Skill Actions。

## P0-B：Browser Transport 修复

修改：

```text
src/skill_temple/browser_models.py
src/skill_temple/browser_routes.py
src/skill_temple/app.py
src/skill_temple/browser/dispatcher.py 或新增 transport decoder
tests/browser/test_contracts.py
tests/browser/test_transport_envelope.py
```

内容：

- Envelope；
- `payload_json`；
- strict decode；
- TypeAdapter；
- 删除 Browser OpenAPI 后处理；
- 结构化错误。

完成标准：

- Builder 工具签名稳定显示 `operation` 和 `payload_json`；
- `get_session` / `open_session` 真实成功；
- 不新增 Browser Action。

## P0-C：Protocol Skill

修改：

```text
src/skill_temple/example_skills/browser-action-protocol/
GPT_ACTION_PROMPT.md
evals/skill_queries.jsonl
Skill contract tests
```

内容：

- 新 Skill；
- 全部 operation contracts；
- 完整 envelope 示例；
- error recovery。

完成标准：

- GPT 能从 catalog 选择协议 Skill；
- 只读取当前 operation contract；
- Browser 调用不依赖 OpenAPI 内部类型提示。

P0-A、P0-B、P0-C 必须放在同一个 PR 中合并，形成一个原子破坏式版本。不得把只完成其中一部分的提交合并到主分支，避免 Instructions、Skill 和 Action schema 不一致。

## P1：Registry 与通用工作流

内容：

- Operation Registry；
- 自动合同生成；
- 新增：
  - `browser-session-capture`；
  - `browser-evidence-inspection`；
  - `browser-request-replay`；
  - `browser-script-tracing`；
  - `browser-stream-diagnostics`；
- 改造 `current-site-analysis`；
- 改造 `pandora-protocol-reproduction`。

## P2：版本绑定与恢复

内容：

- Skill hash；
- operation contract hash；
- manifest 绑定；
- stale contract；
- 按 eval 决定 `browser-experiment-recovery`；
- telemetry。

## P3：发布固化与死代码审计

内容：

- 扫描生产代码、OpenAPI、生成 Instructions、Skills、普通文档和测试，拒绝任何旧 Skill retrieval/search 标识符；
- 扫描同一范围，拒绝任何旧 Browser `payload` transport 标识符；
- 扫描同一范围，拒绝任何 `capture_baseline` 标识符；迁移计划和发布说明中的历史说明除外；
- 检查不存在 alias、fallback、双写或兼容 parser；
- 清理过时 docs 和 Instructions；
- 固化 Builder 发布回归。

---

## 17. PR 拆分建议

### PR 1：P0 Destructive Skill and Browser Contract Cutover

```text
prompt_builder
{{SKILL_CATALOG}}
loadSkills
readSkillContent
移除公开 retrieve/search
runtime 简化
payload_json
strict decode
TypeAdapter
OpenAPI 后处理删除
结构化错误
browser-action-protocol
operation contracts
完整示例
error recovery
删除 capture_baseline
同步更新 Instructions、OpenAPI、测试、eval、README 和 INSTALL
```

该 PR 不接受兼容层或部分迁移，所有旧调用在合并后直接失败。

### PR 2：Operation Registry

```text
registry
合同生成
hash
CI 一致性
```

### PR 3：Workflow Skills and Existing Skill Migration

```text
五个通用工作流 Skill
current-site-analysis
pandora-protocol-reproduction
```

### PR 4：Version Binding and Recovery

```text
skill/contract hash
manifest
stale contract
recovery
telemetry
```

---

## 18. 风险与缓解

### 风险：静态目录与部署 Skill 不一致

缓解：

- CI 重建 `dist/GPT_INSTRUCTIONS.md`；
- dirty check；
- 发布记录 catalog hash；
- Builder 更新作为发布 checklist。

### 风险：description 不足导致模型选错

缓解：

- description 写清触发和排除条件；
- catalog eval；
- GPT-5.6 代表性任务评测；
- 不恢复服务端语义路由掩盖描述问题。

### 风险：移除 search 后资料难找

缓解：

- `SKILL.md` 建立清晰索引；
- docs 按任务阶段组织；
- operation-index 明确路径；
- eval 验证路径可达性。

### 风险：一次加载过多 Skill

缓解：

- Instructions 要求最小集合；
- 建议最多 3 个；
- 分阶段再次 `loadSkills`；
- 指标记录平均加载数量。

### 风险：模型生成无效 payload_json

缓解：

- 完整 envelope 示例；
- decoded payload 示例；
- 结构化 JSON decode error；
- `dispatch_started=false` 安全修正。

### 风险：Skill 合同与后端漂移

缓解：

- Operation Registry；
- 自动生成合同；
- contract hash；
- CI dirty check。

### 风险：Builder 缓存旧 schema

缓解：

- transport version 2.0；
- 强制重新导入；
- 记录最终工具签名；
- smoke 作为发布门槛。

---

## 19. 最终验收标准

## Skill 加载层

- Instructions 中包含完整 metadata catalog；
- 不包含 Skill 正文；
- 不调用 Action 查询 Skill 目录；
- 公开 `loadSkills` 和 `readSkillContent`；
- 不公开 `retrieveSkillContext` 和 `searchSkillDocs`；
- 服务端不做语义 Skill 选择；
- 精确 ID、精确路径、continuation、安全边界均通过测试。

## Browser Action 层

- 仍然只有两个 Browser Actions；
- schema 不含复杂 union；
- Builder 暴露 `operation` 和 `payload_json`；
- 新 operation 不要求新 Action。

## Skill 内容层

- 存在 `browser-action-protocol`；
- 每个 operation 有精确合同；
- 只按需读取当前合同；
- `current-site-analysis` 是通用编排；
- Pandora Skill 只承担领域扩展；
- 不复制底层 payload 字段。

## 后端层

- 内部 union 保留；
- 所有 payload 严格验证；
- malformed/unknown/stale 错误结构化；
- `dispatch_started` 可靠；
- outcome unknown 不自动重试。

## 发布与测试层

- prompt builder、runtime、OpenAPI、合同、集成测试通过；
- `dist/GPT_INSTRUCTIONS.md` 无漂移；
- 真实 Builder smoke 通过；
- GPT-5.6 eval 达标；
- CI 能发现 catalog、Skill、operation 和文档漂移。

---

## 20. 最终 Skill 清单

### P0 必须新增

```text
browser-action-protocol
```

### P1 建议新增

```text
browser-session-capture
browser-evidence-inspection
browser-request-replay
browser-script-tracing
browser-stream-diagnostics
```

### P2 按 eval 决定

```text
browser-experiment-recovery
```

### 保留并改造

```text
current-site-analysis
pandora-protocol-reproduction
```

最终推荐 8 至 9 个 Skill。P0 只硬依赖 `browser-action-protocol`，其他按真实 eval 分阶段上线。

---

## 21. 最终原则

```text
静态 Instructions Catalog 是 Skill 发现层
GPT-5.6 是 Skill 选择和推理层
loadSkills 是精确入口加载层
readSkillContent 是按路径渐进披露层
Skill 是 operation 调用知识层
GPT Action 是极简传输层
Pydantic 是最终权威验证层
Browser Service 是原子执行层
Evidence Store 是事实来源
```

不得再次：

- 用公开 Action 动态查询和裁剪 Skill 目录；
- 让服务端替模型做语义 Skill 路由；
- 用全文搜索 Action 弥补 Skill 索引不足；
- 让公开 OpenAPI 承担全部 operation 类型系统；
- 用大量独立 Actions 绕过 Skill 架构。
