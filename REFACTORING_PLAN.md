# 面向未知网页分析的删减与重构计划

> 状态：进行中。本计划先进入 PR #6，后续每个删减或重构 PR 都应更新这里的结论、证据和完成状态。
>
> 基线：`main` 已包含 PR #5；PR #6 已开始移除固定 Stop 编排和后端协议结论。

## 1. 项目的唯一目标

本项目的目标不是维护一个固定版本的 Pandora 协议实现，也不是在后端固化一套“正确的六组实验”。

项目的目标是：

> 在网页实现、接口、认证方式、流协议和交互流程都可能已经发生巨大变化的情况下，帮助分析者和模型共同观察当前网页、设计下一步实验、保存可审计证据，并逐步形成对当前实现的认识。

因此，当前网页本身才是事实来源。历史 Pandora 行为、旧接口字段、旧 UI 流程和旧流终止标记只能作为假设，不能作为核心代码的默认真相。

理想工作循环应当是：

```text
观察当前页面和网络
  ↓
人工 / 模型提出一个可证伪的小假设
  ↓
运行一次尽量小的浏览器实验
  ↓
保存原始证据和有限事实
  ↓
人工 / 模型解释结果并调整假设
  ↓
设计下一轮实验
```

后端负责可靠执行和证据保存，不负责替分析者决定协议语义。

## 2. 关键前提

后续所有删减和重构必须接受以下前提：

1. 当前网页很可能已经与历史 Pandora 页面存在重大差异。
2. 当前实现可能使用 Fetch、XHR、SSE、NDJSON、raw stream、WebSocket、Service Worker、Web Worker、GraphQL、RPC 或尚未预见的组合。
3. 页面结构、按钮、字段名、URL、请求顺序、鉴权方式和状态机都不能被预设。
4. “没有满足理想实验条件”不等于请求非法。应尽量执行、保存事实并明确缺失项。
5. `unknown`、`unclassified` 和 `insufficient` 是正常分析结果，不是系统失败。
6. 同一个现象可能需要页面证据、网络证据、源码证据和持久状态证据共同解释。
7. 人工分析者可以根据现场信息改变实验顺序，不应被固定工作流阻断。

## 3. 核心设计原则

### 3.1 证据优先，结论外置

核心 Action 应输出：

- 实际执行了什么；
- 页面、网络、流、脚本和控制台观察到了什么；
- 哪些原始 artifact 被保存；
- 哪些证据缺失、不完整或无法关联；
- 哪些自动计算只是提示。

核心 Action 不应输出：

- 某字段最终是 `required` 或 `optional`；
- 当前协议已经被“证明”；
- 某组固定实验必须先于另一组；
- 当前响应一定代表某个历史 Pandora 语义；
- 某次实验是否具有最终推断资格。

### 3.2 探索路径可调整

理想的强实验可以有建议模板，但模板应放在 Skill、分析笔记或可选 analyzer 中，不能成为核心请求校验。

### 3.3 保留原始事实，减少重复派生状态

一份事实只应有一个权威来源。派生摘要可以重算，不应在 manifest 多处重复存储同一结论。

### 3.4 安全边界和分析约束分离

以下属于必须保留的安全边界：

- credential 脱敏；
- 原始证据只读；
- deadline、取消和进程清理；
- artifact 大小和响应预算；
- 路径限制；
- browser-managed header 限制；
- 不把 secret 写入报告或 Action 响应。

以下属于分析策略，不应伪装成安全边界：

- 必须按固定顺序运行实验；
- 必须存在某个旧字段；
- 必须使用某个旧终止标记；
- 必须比较固定的环境维度；
- 必须运行 Control 后才能做任何 exploratory replay。

## 4. 当前代码结构事实

当前代码已经显示出“功能继续堆叠，但边界没有形成”的问题：

| 区域 | 当前规模 | 主要问题 |
| --- | ---: | --- |
| `browser_service.py` | 5660 行 | `BrowserActionService` 5264 行，混合 session、实验编排、replay、证据导出、持久化和结论计算 |
| `_capture_flow()` | 1815 行 | 一次函数同时执行 setup、普通 flow、replay、finalize、证据关联和完整性判断 |
| `browser_adapters.py` | 2088 行 | MCP transport、Playwright、stream 等待和浏览器内 replay runtime 混在一起 |
| `protocol_evidence.py` | 1777 行 | 请求匹配、脱敏、mutation、响应语义和比较逻辑混在一个模块 |
| `browser_models.py` | 85 个模型 | API 模型逐步承载了过多固定实验语义和交叉校验 |
| `workspace_service.py` | 1429 行 | 文件读取、搜索、写入、补丁、PowerShell 和安全策略集中在单类 |
| `tests/test_browser_actions.py` | 5677 行 | fake、单元测试、集成测试和协议假设集中在一个文件 |

代码规模本身不是删除理由，但它说明当前大量复杂度来自职责重叠和派生状态重复。

## 5. 必须保留的最小核心能力

无论网页如何变化，以下能力对探索未知网页仍然有价值，不应在清理阶段误删。

### 5.1 浏览器与页面控制

- 打开、附着和关闭浏览器 session；
- Playwright 与网络分析页面对齐；
- 通用 navigate、click、fill、type、press、select、check、hover、upload；
- 通用 wait、assert 和 snapshot；
- 同步或后台 job；
- deadline、取消、失败收尾。

### 5.2 原始证据采集

- 页面 snapshot、截图和 trace；
- 网络请求、响应头、响应体和 timing；
- console error；
- request initiator 和 script source；
- SSE、NDJSON、raw stream 的有界保存；
- 无 Content-Type 的响应保存；
- wire order、重复 header/query occurrence；
- 原始 artifact 的 hash、大小、路径和敏感级别。

### 5.3 通用 replay

- 在浏览器上下文中重放已观察请求；
- 保留 cookie、origin 和浏览器环境；
- 支持 JSON/header/query 的 add、remove、replace；
- 支持可配置 transport semantics；
- 支持可配置终止条件，而不是默认假设 `[DONE]`；
- 允许 exploratory replay，不要求先形成完整 Control/Treatment 契约。

### 5.4 人机协同检查

- 列出实验和 evidence；
- 有界读取 manifest、网络、流、脚本和 console；
- 搜索分析目录；
- 保存分析笔记、派生 schema、报告和 replay 草稿；
- 明确显示缺失证据和不确定性。

## 6. 明确无关或高度可疑的功能

这些功能与“分析当前网页”没有直接关系，应优先删除或移出默认发行物。

### 6.1 IDAPython 示例 Skill

涉及文件：

```text
src/skill_temple/example_skills/idapython/
evals/skill_queries.jsonl 中的 idapython 用例
tests/test_runtime.py 中只验证 idapython 示例的测试
```

判断：**明确无关，已删除。**

它是通用 Skill 模板遗留，不参与网页分析，却增加 runtime、eval 和测试维护成本。

### 6.2 通用 Skill catalog 中只为示例存在的复杂度

当前项目真正需要的是网页分析方法和相关文档。动态 Skill catalog、多个示例 Skill、通用语义选择和演示 eval 是否仍有价值，需要单独审计。

初步方向：

- 保留最小的项目分析 Skill 读取能力；
- 删除仅用于展示“多 Skill 平台”的示例逻辑；
- 不把本项目继续发展成通用 Skill 托管服务。

状态：**IDAPython 删除后，默认发行物只剩项目分析 Skill；通用 catalog runtime 是否继续保留，留待单独审计。**

### 6.3 Pandora 专有命名的 smoke fixture 和报告名

此前存在：

```text
tools/toolchain_validation_server.py 中的 /api/pandora/conversation
tools/browser_action_smoke.py 中大量 pandora_* 变量
reports/pandora-comparison.md
```

这些测试能力本身有价值，但命名和断言会让维护者误以为旧 Pandora 行为就是产品契约。当前分支已将 fixture endpoint、页面控件、变量和输出键改为通用 stateful stream 命名；历史对照报告名仍留待后续 Skill/文档清理。

方向：

- 将 fixture 改为通用的 stateful streaming web fixture；
- 保留对 cookie、stream、mutation、setup output 的覆盖；
- 删除旧产品名和旧字段语义；
- 报告模板改为 `current-site-comparison.md` 或 `implementation-comparison.md`。

判断：**保留能力，删除专有假设。**

## 7. 重复功能与合并候选

### 7.1 `capture_baseline` 与空 `capture_flow`

当前 `CaptureBaselinePayload` 继承 `CaptureFlowPayload`，主要差异是：

- 默认 objective；
- primary request 允许 0 到 100 个匹配；
- `flow` 被限制为空。

服务端最终仍进入同一个 `_capture_flow()`。

判断：**重复入口。**

计划：

1. 将 baseline 表达为普通 `capture_flow` 的 preset；
2. 短期保留 `capture_baseline` 作为兼容 alias；
3. 文档和 Skill 停止生成新 `capture_baseline`；
4. 完成兼容窗口后删除独立模型和 dispatch 分支。

### 7.2 普通 `flow`、`setup_flow`、`verification_flow`

三者使用相同的 `FlowStep`，但当前存在多套执行循环、checkpoint、失败处理和 step result 写入。

判断：**执行逻辑重复。**

计划：

- 统一为一个 `StepExecutor.execute_many(phase, steps)`；
- phase 只是标签，不改变可执行 step 类型；
- setup、action、verification 都通过同一执行器；
- 不再为历史实验模板复制循环。

### 7.3 Control、Exploratory、Treatment 三套 replay payload

当前：

- Control 携带完整 replay 配置；
- Exploratory 继承 Control，只放开 mutations；
- Treatment 只携带 `control_experiment_id + mutation`，再从旧 manifest 继承大量状态。

这导致：

- payload 结构不对称；
- service 中存在复杂 `_prepare_replay_execution()` 和 `_resolve_replay_pair()`；
- 分析者无法轻松表达“基于某次请求做任意新实验”；
- Control/Treatment 成为核心协议，而不是一种可选分析方法。

判断：**核心 API 过度绑定因果实验模板。**

计划：

- 统一为一个通用 replay request；
- source、mutations、bindings、setup、transport、termination 都显式表达；
- 可选 `comparison` 描述与哪个实验比较、比较哪些维度；
- Control/Treatment 降级为 Skill 生成的 preset，不再是不同核心模型；
- exploratory 成为默认思维，而不是特殊例外。

### 7.4 多层完整性字段

当前存在或曾存在：

```text
execution_integrity
evidence_integrity
collector_integrity
primary_request_integrity
network_snapshot_integrity
network_artifact_integrity
stream_artifact_integrity
request_body_completeness
request_headers_completeness
response_body_completeness
causal_comparability
inference_eligibility
objective_integrity（PR #6 已删除，不兼容旧 manifest）
```

问题：

- 同一事实被多个聚合字段重复表达；
- service 在不同阶段重复计算相近状态；
- 字段之间存在隐式优先级；
- 分析者很难知道哪个字段是原始事实，哪个是后端结论。

判断：**重复派生状态。**

计划：

保留少量事实维度：

```text
execution.status
artifacts[].completeness
observations[].completeness
associations[].confidence
missing_evidence[]
```

如果需要摘要，只生成一个可重算的 `quality_summary`，不再在 manifest 顶层保存多个相互推导的 verdict。

当前状态：核心 manifest 已改为 `execution.status + quality_summary +
network_observations[].completeness + artifacts[].completeness`。旧的 execution、
evidence、collector、primary request 多套 integrity 字段不再生成，也不做兼容转换。

### 7.5 network snapshot 与 stream request 的重复摘要

当前同一个请求可能同时存在：

- js-reverse network snapshot；
- stream capture request record；
- public network summary；
- evidence entry；
- replay source snapshot；
- 多套 integrity 字段和关联字段。

原始来源需要保留，但派生摘要不应重复复制整个请求事实。

计划：

- 一个 canonical `NetworkObservation`；
- 记录它引用的 network artifact、stream artifact 和 association method；
- stream 与 network 是来源，不是两套并列业务模型；
- public response 通过 canonical observation 动态裁剪；
- 删除重复 hash、完整性和关联状态计算。

### 7.6 响应分类、inference hints 与 inference eligibility

`classify_replay_response()` 当前包含对 validation、field rejection、conflict、redirect/cache 等固定分类。

分类作为提示可以保留，但当前还有：

- `protocol_rejection_observed`；
- `inference_hints`；
- `inference_eligibility`；
- mutation assessment；
- evidence integrity；
- causal comparability。

这些层次部分重复，而且 `inference_eligibility` 仍然在替分析者决定是否可以推断。

判断：**过度派生。**

计划：

- 保留 HTTP status、content type、结构化错误路径、body hash 和有界 observations；
- 分类器改为可选 analyzer；
- hints 明确标记 analyzer 名称和版本；
- 删除核心 manifest 的 `inference_eligibility`；
- 不再把 response category 与字段必要性绑定。

### 7.7 环境比较默认维度

当前默认 required dimensions 包含 `page_origin` 和 `request_context_sha256`，advisory dimensions 又固定包含 URL、path、conversation node、bundle hash 等。

对于未知网页：

- 这些维度可能不存在；
- 可能还有 service worker、worker version、tab state、feature flag、account state 等更关键维度；
- 固定默认值容易制造虚假的“不可比”。

判断：**固定策略不适合作为核心默认。**

计划：

- 环境 observation 全量记录可获得事实；
- 比较维度由实验请求或 Skill 显式选择；
- 未配置 comparison 时不生成 comparability verdict；
- comparison 输出 difference，不输出最终因果资格。

### 7.8 `setup_outputs` 与 volatile binding 的强耦合

当前要求：

- 有 `setup_outputs` 必须有 `setup_flow`；
- setup output ID 必须与 `value_source=setup_output` 的 binding ID 完全相等；
- 只支持 `network_response_json + RequestMatcher + JSON Pointer`。

这套能力对有状态网页可能重要，不能直接删除；但强耦合会阻断探索未知来源，例如 DOM、localStorage、cookie metadata、script variable 或 WebSocket 消息。

判断：**能力有价值，模型过于固定。**

计划：

- 暂时保留现有实现；
- 重构为通用 `extractors[]` 和 `bindings[]`；
- extractor 可独立运行并保存结果；
- binding 可以选择引用 extractor 输出，也可以由人工在下一次请求中提供；
- extractor 失败不必使整个实验请求非法。

### 7.9 inspect 操作和 `save_script_source`

当前脚本可以先通过 inspection 获取，再通过独立 consequential operation 保存到实验目录。这里可能存在读取、保存和 evidence 索引的重复路径。

状态：**需要调用链和真实使用审计。**

候选方向：

- inspection 支持 `persist=true`；或
- 统一 artifact import API；或
- 保留独立保存，但移除重复 metadata 拼装。

在没有确认现有调用方之前不删除。

### 7.10 Workspace PowerShell

PowerShell 对二进制分析、hash、压缩数据和自定义脚本有价值，但它不是浏览器分析核心，同时带来大量安全策略、进程树和测试代码。

状态：**候选提取，不立即删除。**

计划：

- 先统计实际网页分析中 PowerShell 的必要用例；
- 优先用内建 binary/hash/artifact inspection 替代常见用法；
- 如果只剩少量高级用例，将 PowerShell 移到可选扩展；
- 保留只读 inspect/search/read 作为核心人机协同能力。

## 8. 不必要的固定逻辑

以下逻辑不应继续存在于核心后端：

1. 固定“六组实验”顺序。
2. 把历史第一条消息、第二条消息、regenerate、edit、stop 当作所有网页的共同状态机。
3. 后端判断字段 `required/optional/tracking_only`。
4. 后端决定实验 `eligible/ineligible`。
5. 固定旧 `[DONE]` 或旧 SSE 事件作为成功条件。
6. 固定某些 URL、MIME、按钮或字段名。
7. 强制所有探索实验都构造完整 Control/Treatment pair。
8. 因缺少理想 checkpoint 而拒绝执行实验。
9. 默认要求固定环境维度完全可比。
10. 在多个 manifest 字段中重复保存同一个质量结论。
11. 为旧 schema 永久保留兼容分支。
12. 与网页分析无关的示例 Skill、eval 和演示逻辑。

这些内容可以作为历史笔记、Skill 模板或可选 analyzer 保留，但不能继续扩大核心代码。

## 9. 当前不能删除的复杂能力

因为网页大概率已经变化，以下看似复杂的能力反而应暂时保留，直到真实页面侦察完成：

- SSE、NDJSON、raw stream 和无 Content-Type；
- header/query wire order 和重复 occurrence；
- raw body 和有界 chunk boundary；
- browser-context replay；
- add/remove/replace mutation；
- source request 的 method、完整 URL/query、body 指纹和时间窗口关联；
- setup 后提取动态 ID 的基本能力；
- script source、initiator、worker 和 console 证据；
- credential redaction；
- cancellation、deadline 和 finalization；
- artifact hash 和只读保护。

这些能力应被简化和模块化，但不能因为当前代码难维护就直接删掉。

## 10. 先做当前网页侦察，再决定第二轮删除

重构不能只基于旧文档和测试。下一阶段必须对当前目标网页做一次不带旧假设的侦察。

### 10.1 页面层

记录：

- 初始 URL、重定向和 origin；
- 主要页面区域和可交互控件；
- iframe、shadow DOM、虚拟列表；
- 登录、验证码、feature flag 和地区差异；
- localStorage、sessionStorage、IndexedDB、cookie 的类别，不记录 secret 值；
- Service Worker、Web Worker、Shared Worker。

### 10.2 网络层

记录：

- 实际使用 Fetch、XHR、SSE、WebSocket、GraphQL 或其他 transport；
- API origin、path pattern 和请求方法；
- content type 与压缩方式；
- client-generated ID、server-generated ID 和关联关系；
- 请求顺序、并发、重试、polling、keepalive；
- 流终止方式；
- 页面刷新和重新登录后的差异。

### 10.3 源码层

记录：

- 关键 bundle 和 source map 是否可用；
- 请求构造函数、stream parser、状态 reducer；
- 是否存在 worker/service worker 内请求；
- 旧 Pandora 字段或 endpoint 是否还存在；
- 页面更新后是否使用新的协议抽象。

### 10.4 输出

第一次侦察只产出：

```text
reports/current-site-inventory.md
reports/current-ui-map.md
reports/current-network-map.md
reports/open-questions.md
```

不要求立即生成完整协议 clone，也不要求套用历史六组实验。

### 10.5 报告生成工具

`tools/current_site_inventory.py` 从选定 session 或 analysis series 的 experiment
manifest 生成上述四份报告。它只整理已有事实：

- page alignment 和 step result；
- network endpoint、method、resource type、Content-Type 和 status；
- stream event source、终止原因、事件数量和 artifact integrity；
- credential 相关 header 名、query 名和 identifier-like request shape 路径；
- 当前 manifest 无法回答的 UI、认证来源、worker、WebSocket、ID 来源和刷新差异。

工具不会读取 raw body、raw header、stream payload 或 credential artifact，也不会把
未观察到的 transport、worker 或认证方式写成“不存在”。真实目标网页尚未运行现场
capture 时，阶段 B 的事实确认项仍保持未完成。

侦察结果将决定：

- WebSocket 支持是否需要优先实现；
- 哪些旧 SSE 专用代码可以删除；
- setup output 是否仍然必要；
- Control/Treatment 是否适合当前接口；
- 哪些历史报告模板已经失效。

## 11. 删除判定标准

一个功能只有满足以下大部分条件时才能删除：

1. 对当前网页侦察没有用途；
2. 对一般未知网页分析没有通用价值；
3. 没有真实外部调用方；
4. 现有引用仅来自测试、示例或旧文档；
5. 删除后仍可保存原始证据；
6. 删除不会降低 credential、artifact、deadline 或取消安全；
7. 已有 characterization test 证明公共行为变化是有意的；
8. PR 中明确迁移路径或说明不再兼容。

不要仅因为函数很长、模型很多或当前测试难写就认定功能无效。

## 12. 分阶段执行计划

### 阶段 A：清除明确无关和死结论

目标：减少不需要现场验证的维护负担。

- [x] 删除 replay response 中无信息的 `conclusion` 和 `usable_for_required_classification`。
- [x] 停止生成 `objective_integrity`。
- [x] 删除 Treatment 对旧 `objective_integrity` manifest 的兼容兜底。
- [x] 移除固定 Stop 顺序拒绝。
- [x] 删除 IDAPython 示例 Skill、docs、eval 和专用测试。
- [x] 清理仍引用 `objective_integrity` 的 smoke 输出和旧文档。
- [x] 将 Pandora 专有 fixture 命名改为通用 stateful stream fixture。

### 阶段 B：当前网页无假设侦察

目标：获得 2026 年当前页面事实，而不是继续围绕历史接口重构。

- [x] 提供从 experiment manifest 生成四份侦察报告和 evidence gaps 的工具。
- [ ] 建立 current-site inventory。
- [ ] 确认 transport 类型和流终止方式。
- [ ] 确认认证与动态状态来源。
- [ ] 确认是否存在 worker/service worker/WebSocket。
- [ ] 列出当前工具无法观察的证据缺口。
- [ ] 根据事实更新本计划中的删除候选。

### 阶段 C：合并重复入口和重复状态

- [ ] 将 `capture_baseline` 变为 `capture_flow` preset/alias。
- [ ] 统一 flow/setup/verification step executor。
- [ ] 删除 `inference_eligibility`。
- [ ] 将固定 response classifier 降级为可选 analyzer。
- [x] 合并完整性和 completeness 字段。
- [x] 建立 canonical network observation，删除重复 summary 计算。

### 阶段 D：简化 replay 模型

- [ ] 合并 Control/Exploratory/Treatment payload。
- [ ] 将 pair comparison 改为可选配置。
- [ ] 将 environment comparison 改为显式选择维度。
- [ ] 将 setup output 改为通用 extractor/binding。
- [ ] 保持 wire、transport 和 response reader 的通用能力。

### 阶段 E：结构重构

在删减完成前，不做大规模搬文件。

删减后再形成：

```text
browser/application   实验编排
browser/domain        typed facts 和通用规则
browser/infrastructure Playwright、MCP、artifact
protocol/analyzers    可选匹配、mutation、response hints
workspace             人工分析工具
```

`BrowserActionService` 最终只保留兼容 facade 或完全移除。

### 阶段 F：测试重组

- [ ] 按 capture、replay、evidence、stream、workspace 拆分测试。
- [ ] fake adapter 独立到 `tests/fakes/`。
- [ ] 纯 analyzer 使用参数化单元测试。
- [ ] 浏览器 runtime JavaScript 使用独立 JS 测试。
- [ ] 保留少量端到端 current-site-agnostic smoke。
- [ ] 删除只证明旧 Pandora 结论的测试。

## 13. 推荐后续 PR 顺序

顺序可以根据当前网页侦察调整，但默认如下：

1. **PR #6：** 写入本计划，删除第一批死结论、固定 Stop 校验、IDAPython 示例和专有 smoke fixture 命名。
2. **侦察 PR/报告：** 加入当前网页 inventory 与 evidence gaps，不改核心架构。
3. **入口合并 PR：** `capture_baseline` 兼容 alias，统一 flow executor。
4. **结论删减 PR：** 删除 `inference_eligibility` 和固定后端 response verdict。
5. **证据模型 PR：** canonical network observation，合并完整性字段。
6. **Replay 简化 PR：** 通用 replay + 可选 comparison/extractor。
7. **结构 PR：** 在功能已经减少后拆分 service、adapter 和测试。

每个 PR 应满足：

- 只处理一个清晰主题；
- 代码净删除优先于抽象新增；
- 提交前查看 diff；
- 运行 Ruff、全量 pytest 和最相关 smoke；
- PR 描述列出删除内容、保留内容和兼容风险。

## 14. 重构完成标准

完成不以“文件变小”作为唯一标准，而以以下结果为准：

1. 分析者可以在不知道当前网页协议的情况下开始 capture。
2. 不满足历史模板的实验不会被无谓拒绝。
3. Action 输出事实和缺失项，不输出最终协议语义。
4. Skill 和人工可以自由调整实验顺序。
5. 原始网络、流、页面和脚本证据仍然可审计。
6. 新 transport 或新网页状态不需要修改一个 1800 行主函数。
7. 同一请求事实不在 manifest 中复制五份。
8. Control/Treatment 是可选分析方法，不是核心 API 的唯一道路。
9. 与网页分析无关的模板代码已经删除。
10. 当前网页事实与历史 Pandora 假设在文档中明确分离。
11. 删除后的核心代码明显少于当前实现，而不是把原代码机械分散到更多文件。

## 15. 当前开放问题

这些问题必须通过真实网页观察解决，不能在代码里预设答案：

- 当前页面是否仍使用 SSE？
- 是否已经迁移到 WebSocket、GraphQL subscription 或其他 transport？
- 是否仍存在 conversation/message/parent 等历史 ID？
- ID 是客户端生成、服务端生成还是 setup 响应产生？
- 是否存在 Service Worker 或 Worker 内请求？
- 认证是否仍依赖 cookie/Authorization/CSRF？
- 浏览器上下文 fetch 是否仍能复现真实请求，还是需要页面原函数调用？
- 页面是否有反自动化、签名、proof token 或动态加密？
- 流终止是网络关闭、事件、状态字段还是页面状态变化？
- 编辑、重试、停止等交互是否仍存在，语义是否已经变化？
- 当前最小可复现目标到底是接口重放、页面自动化，还是状态机说明？

这些问题的答案将持续修订本计划。任何旧文档与当前网页冲突时，以当前网页证据为准。
