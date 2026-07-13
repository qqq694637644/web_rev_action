# web_rev_action 项目设计与演进计划

## 1. 文档定位

`web_rev_action` 是面向网页版 GPT Action 的网页协议实验后端。它把浏览器操作、网络证据采集、请求重放和本地分析组织为结构化 Action，使 GPT 能在人工监督下完成可重复、可审计的网页协议研究。

本文只描述：

- 产品目标与边界；
- 系统架构与职责；
- 实验、证据和 workspace 的核心模型；
- 安全原则、验证要求和演进方向。

文档关系：

```text
PANDORA_REPRODUCTION.md  项目目的、实验方法和报告标准
PLAN.md                 系统设计与演进方向
README.md               当前使用说明
代码、OpenAPI、测试      当前实现事实
```

具体缺陷、修复方案和实现细节应记录在 issue 或 PR 中，不进入本计划。

---

## 2. 产品定义

系统服务于以下闭环：

```text
GPT / Skill 提出实验目标
→ Browser Action 执行 capture 或 replay
→ 后端保存 manifest、evidence 和 artifact
→ Workspace Action 读取和分析证据
→ GPT 形成结论、报告和下一轮实验
```

核心目标：

1. 让 GPT 通过稳定、有限、结构化的 Action 操作浏览器实验环境；
2. 在正常浏览器上下文中采集页面、网络、流、console、脚本和 initiator 证据；
3. 支持基线捕获、流程捕获、请求重放和成对实验；
4. 保存稳定、可追溯的实验与证据关系；
5. 对凭据、原始证据和大型数据执行默认保护；
6. 如实表达成功、失败、部分完成和证据不足；
7. 为不同站点和持续变化的网页协议保留扩展空间。

当前实现已经具备人工监督下的 JSON/SSE 协议实验能力。后续工作以提高通用性、证据质量、运行可靠性和 GPT 分析效率为主。

### 非目标

本项目不负责：

- 新造浏览器、CDP 或网络采集底层；
- 让 GPT 直接控制私有 collector 生命周期；
- 绕过登录、验证码、授权或站点访问控制；
- 代替 Skill 决定具体站点的业务语义；
- 在证据不足时生成确定性结论；
- 管理 Git、PR、CI 或远程代码仓库；
- 返回完整凭据、无限制正文或大型二进制；
- 提供任意脚本执行环境；
- 为多用户和分布式调度做过早设计。

---

## 3. 设计原则

### 3.1 结构化 Action

GPT 只调用公开 Action，不直接拼接 collector 命令、浏览器内部脚本或私有协议调用。复杂生命周期由后端原子执行。

### 3.2 证据优先

后端优先保存实际请求、响应、页面状态、完整性和 artifact。站点级结论由 Skill 和 GPT 综合多轮实验形成。

### 3.3 当前事实与未来设计分开

尚未实现的能力不能写成现状。公开行为必须能在 OpenAPI、代码、测试或真实运行结果中找到依据。

### 3.4 原子实验

一次 capture 或 replay 是一个完整实验事务：

```text
创建 manifest
→ 占用资源
→ 启动采集
→ 执行流程
→ 等待条件
→ 停止采集
→ 保存证据
→ 写入终态 manifest
→ 释放资源
```

失败时也应尽可能留下终态和已获得证据。

### 3.5 有界输出

Action 返回适合 GPT 消费的摘要。完整内容保存为 workspace artifact，通过后续读取、搜索和分页访问。

### 3.6 默认安全

凭据默认隐藏，原始证据默认只读，危险操作默认禁止，外部副作用不自动重试。

---

## 4. 系统架构

```text
GPT
├── Skill Actions
├── Browser Actions
└── Analysis Workspace Actions

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
├── Playwright CLI
├── js-reverse-mcp
└── analysis workspace
```

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| Skill | 实验顺序、研究目标、站点知识、证据解释、报告 | 控制 collector、读取凭据后自行发请求 |
| Browser Actions | session、capture/replay、证据持久化、timeout/cancel/cleanup | 决定站点业务结论 |
| Workspace Actions | 读取、搜索、派生分析、报告和辅助脚本 | 控制浏览器生命周期、改写原始证据 |
| Adapters | 调用 Playwright 与 js-reverse-mcp，规范化结果 | 解释研究目标 |

---

## 5. 公开 Action

### Skill Actions

```text
retrieveSkillContext
readSkillContent
searchSkillDocs
```

### Browser Actions

```text
inspectBrowserEvidence
runBrowserExperiment
```

`runBrowserExperiment` 当前包含：

```text
open_session
capture_baseline
capture_flow
replay_request
save_script_source
close_session
cancel_experiment
```

`inspectBrowserEvidence` 用于读取 session、experiment、network、stream、script、initiator 和 console 证据摘要。

### Workspace Actions

```text
workspaceInspect
workspaceSearch
workspaceReadFiles
workspaceWriteFile
workspaceApplyPatch
workspaceExecPwsh
```

OpenAPI 使用严格模型和 discriminated union：

- 每个 operation 只接受自己的 payload；
- 每种 flow step 只接受相关字段；
- read-only 与 consequential Action 明确区分；
- 新能力先定义输入、边界和失败语义，再加入公开契约。

---

## 6. 运行模型

当前部署目标是单用户、Windows 优先、本地运行：

```text
一个 web_rev_action 进程
一个 analysis workspace
一个共享 Chrome CDP endpoint
一个 Playwright CLI 环境
一个长期运行的 js-reverse-mcp
一个活动 browser operation
```

`RuntimeCoordinator` 负责 browser operation 互斥、session 与 experiment reservation、workspace mutation 冲突控制，以及 shutdown 和取消协调。

同一 workspace 由单个服务实例持有，并通过 OS 文件锁阻止多个进程同时写入。资源被占用时返回明确 busy 状态，不使用隐式排队掩盖竞争。

Playwright、PowerShell、ripgrep 和 MCP 子进程必须在 timeout、cancel 与 shutdown 时清理完整 Windows 进程树。外部副作用发生超时后，不假设其未执行，也不自动重试。

未来只有在明确需要并行实验时，才考虑每 session 独立浏览器和 collector。

---

## 7. Session 与 Experiment

### Session

Session 保存浏览器 endpoint、Playwright page、collector page identity 和 service instance 之间的稳定关联。

状态：

```text
open | closed | stale
```

服务重启后，旧实例创建的活动 session 不能继续作为有效 session 使用。

### Experiment

Experiment 是最小可审计运行单元。

状态：

```text
running | completed | partial | failed | interrupted
```

每个 experiment 至少记录：

```text
experiment_id
session_id
operation
objective
input summary
steps
capture metadata
evidence
artifacts
warnings
errors
terminal status
```

长实验默认使用后台 job；同步和后台模式共享同一原子实现、reservation 和终态规则。

每个实验只有一个绝对 deadline。取消时应尽可能停止采集、保存已有证据、写入终态 manifest 并释放资源。已经发出的浏览器或网络副作用可能无法回滚，结果中必须保留这种不确定性。

---

## 8. Capture 与 Replay

### Capture

Capture 用于记录页面和协议行为，可执行：

- 基线采集；
- 页面导航和交互；
- 页面或网络条件等待；
- 网络、流、脚本和 initiator 证据保存；
- console、trace、snapshot 和 screenshot 记录。

需要观察页面初始化行为时，采集必须在导航之前启动。

### Replay

Replay 从受信任的 source evidence 构建请求，在当前浏览器上下文中执行，并保存：

- 重放计划；
- 实际 outbound request；
- response 与 stream 证据；
- 页面前后状态；
- 实验比较信息；
- 验证流程结果。

Replay 请求必须可追溯到已有 experiment 和 evidence，不能接受任意本地文件作为来源。

### Primary evidence

每次实验可以声明 primary request matcher 和预期数量。Supporting request 可用于诊断，但不能替代 primary objective。

### 普通与流式响应

系统应保存原始网络事实，并在此基础上提供协议解析。解析结果不能替代 raw evidence。

### 成对实验

成对实验用于比较 Control 与 Treatment。系统保存输入、目标变化、非目标变化和环境信息；是否足以形成站点级结论，由 Skill 根据实验目的和全部证据判断。

---

## 9. 证据与 Workspace

目录结构：

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

跨实验引用使用：

```text
experiment_id
evidence_id
artifact_id
```

临时请求编号、collector 内部 ID 和文件顺序不能作为长期主键。

原始 evidence、trace、network export 和 manifest 由后端写入并视为只读。Workspace Action 只能在允许目录创建派生分析、报告、笔记和辅助脚本。实验运行期间禁止修改该实验目录。

Action 返回有界摘要，包括数量、状态、完整性、preview 和 artifact 引用。大型正文、raw stream、二进制和 Base64 内容保存在 artifact 中。

Manifest 必须表达：

- 实验是否执行到预期阶段；
- collector 是否正常；
- primary request 是否捕获；
- request/response/stream 证据是否完整；
- artifact 是否持久化；
- cleanup 是否完成；
- 实验是否适合继续人工分析。

完整性信息必须进入 manifest，不能只存在于日志。

---

## 10. 安全与数据边界

### 凭据

Cookie、Authorization、CSRF、Set-Cookie 和类似字段默认：

- 不进入普通 Action 响应；
- 不写入公开报告；
- 不允许通过 workspace 搜索直接泄露；
- 只在后端内部完成必要处理；
- 以 redacted、shape 或 hash 形式暴露。

### 原始证据

原始证据不能被 workspace write/patch 改写。大型与二进制内容不能直接嵌入 Action JSON。

### 浏览器管理字段

由浏览器管理或涉及安全边界的字段不默认开放修改。新增能力必须说明安全范围和审计方式。

### PowerShell

`workspaceExecPwsh` 是受控分析工具，不是安全沙箱。它只能在 analysis workspace 内运行，并受到命令、路径、timeout、输出和进程树限制。

### 授权范围

所有实验只针对用户有权访问和测试的账号、页面与服务。

---

## 11. 可观测性与失败语义

Manifest 是实验事实的主记录，应包含输入、生命周期、adapter 与 collector 状态、page alignment、step 结果、网络与流摘要、evidence、artifact、warning、error 和 cleanup 状态。

至少区分：

```text
输入或 schema 错误
session 无效
运行资源 busy
页面对齐失败
adapter 或 collector 失败
实验条件未满足
证据不完整
timeout
cancel
cleanup 失败
```

以下情况不能报告为完整成功：

- 目标请求未实际发出；
- primary evidence 缺失；
- artifact 保存失败；
- cleanup 未完成；
- 只得到 supporting request；
- 只有截断 preview；
- 外部副作用结果未知。

---

## 12. 演进路线

### 阶段 A：巩固现有闭环

保持当前 JSON/SSE 人工监督实验稳定可用：

- session 和 page alignment 稳定；
- capture/replay 生命周期一致；
- terminal manifest 完整；
- 原始证据与派生文件边界清楚；
- timeout、cancel 和 shutdown 可验证；
- OpenAPI、README 与实现一致。

### 阶段 B：提高协议通用性

减少站点专用假设：

- 扩展请求、响应和流式协议表达；
- 增强请求修改与动态状态处理；
- 提高重放关联和实验比较可信度；
- 完整保存网络顺序、重复值和传输信息；
- 允许 Skill 声明站点特有实验需求。

### 阶段 C：提高 GPT 分析效率

减少获取有效上下文所需的 Action 数量：

- 更好的实验摘要和证据索引；
- 面向协议字段和事件的搜索；
- 自动生成有界差异报告；
- 更清楚的不确定性表达；
- 可复用的实验模板。

### 阶段 D：扩展运行能力

在核心闭环稳定后评估：

- 更多流式协议；
- WebSocket 证据采集；
- 多页面流程；
- 独立 session runtime；
- 可选并行实验；
- 更细粒度的 artifact 分析。

新增能力必须先定义证据、终态、取消、安全和测试语义。

---

## 13. 验证策略

### 自动测试

每次修改至少运行相关测试，合理时运行完整测试集：

```text
python -m pytest
```

涉及公开模型时验证 OpenAPI；涉及生命周期时验证 Browser Action；涉及 workspace 时验证路径与只读边界；涉及证据时验证完整性与脱敏。

测试数量随实现变化，不在本文固定永久计数。

### 静态检查

```text
ruff
类型或语法检查
OpenAPI schema 检查
文档结构检查
```

### Windows 真实运行

真实 smoke test 应覆盖：

```text
open session
page alignment
baseline capture
flow capture
network and stream evidence
request replay
control/treatment experiment
script and initiator evidence
cancel and cleanup
workspace inspection
close session
residual process check
```

任何新增公开能力必须具备：

1. 明确输入模型；
2. 明确失败与取消语义；
3. manifest 记录；
4. 凭据和大型数据边界；
5. 自动测试；
6. 真实运行验证方案；
7. README 或 Skill 使用说明。

---

## 14. 完成标准

项目达到稳定可维护状态时，应满足：

1. GPT 只通过结构化 Action 操作浏览器实验和 workspace；
2. Browser Action 原子管理 capture/replay 生命周期；
3. 每次实验都有可读 terminal manifest；
4. 原始证据可追溯、只读且有稳定 ID；
5. 凭据默认隐藏，大型数据通过 artifact 访问；
6. 当前行为与 OpenAPI、README 和测试一致；
7. 失败、部分完成、取消和证据不足不会被伪装成成功；
8. Skill 能组织多轮实验而不直接控制私有 collector；
9. 后端不依赖单一站点才能执行基本实验；
10. 扩展能力遵守统一的证据、安全、取消和验证规则。

`web_rev_action` 的最终定位不是某个站点的专用脚本，而是网页版 GPT Action 可调用的通用网页协议实验基础设施。
