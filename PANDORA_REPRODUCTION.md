# Pandora 类复现方法

## 1. 目标

本文描述如何使用 `web_rev_action` 的工具组合，复现 Pandora 类网页协议分析流程。

这里的“复现”指：

- 用正常浏览器和授权账号观察网页端行为。
- 记录页面动作、网络请求、SSE 流式响应、调用栈和相关前端脚本。
- 整理出业务协议状态机、请求字段含义和响应事件类型。
- 在 workspace 中写最小重放脚本，验证字段必要性和状态变化。

本文不属于 `web_rev_action` 的功能实现计划。功能实现见 `PLAN.md`。

---

## 2. 工具分工

```text
playwright-cli
  负责稳定制造页面事件

js-reverse-mcp
  负责解释事件背后的网络、脚本、断点、调用栈和运行时状态

web_rev_action
  负责把一次实验组织成 GPT 可调用的 Action，并保存 evidence / artifact

github-gpt-actions-gateway workspace
  负责保存抓包、脚本、schema、diff、报告和实验笔记
```

核心主线：

```text
先确认工具能抓到一条完整消息
  ↓
再封装 GPT Action
  ↓
再建立实验和证据模型
  ↓
再分析消息树和 SSE
  ↓
再定位构造代码
  ↓
最后重放验证
```

---

## 3. 工作目录

建议在 analysis workspace 中保留固定结构。

```text
analysis-workspace/
  captures/
    baseline/
    first-message/
    second-message/
    regenerate/
    edit-message/
    stop-generation/
  experiments/
    exp_001/
      manifest.json
      playwright/
      js-reverse/
      reports/
  schemas/
    request.schema.json
    stream-events.schema.json
    conversation-state.md
  scripts/
    diff-json.py
    replay-http.py
    extract-stream-events.py
  reports/
    protocol-map.md
    stream-events.md
    state-machine.md
    pandora-comparison.md
  notes/
    timeline.md
    open-questions.md
```

workspace 只是记录目录，不需要 Git、PR、CI 或 branch 流程。

---

## 4. 阶段一：建立协议地图

先不要写客户端。先用 `runBrowserExperiment.capture_baseline` 和 `runBrowserExperiment.capture_flow` 记录核心对话动作。

第一轮只做六组实验：

```text
01 baseline
02 第一轮消息
03 第二轮消息
04 重新生成
05 修改旧消息
06 停止生成
```

这六组已经足以分析：

```text
会话创建
conversation ID
message ID
parent message ID
消息树
分支
variant / regenerate
中断状态
SSE 事件序列
```

第二轮再做：

```text
切换模型
标题生成
删除会话
```

第三轮再扩展：

```text
文件上传
网页搜索
图片
工具调用
```

后面的现代工具协议会产生大量额外请求，太早加入会干扰对核心对话协议的判断。

每个实验至少保存：

```text
页面动作
实验前 snapshot
实验后 snapshot
Trace
请求列表
请求头
请求体
响应头
响应体或 SSE 事件序列
请求 initiator
相关脚本 URL 和位置
控制台错误
manifest.json
```

每个实验都要有独立 `experiment_id`，每条关键证据都要有 `evidence_id` 或 `artifact_id`。

---

## 5. 阶段二：整理请求分类

用 `inspectBrowserEvidence.list_requests` 和 workspace 脚本把请求按用途分类。

核心分类：

```text
页面初始化
账号 / session 状态
配置 / feature flag
模型列表
会话列表
会话详情
消息提交
停止生成
重新生成
标题生成
遥测 / 埋点
静态资源
```

后续扩展分类：

```text
文件上传
网页搜索
图片生成
工具调用
WebSocket 或实时通道
```

对每个核心请求记录：

```text
method
path
query
status
content-type
request body schema
response body schema 或 stream event schema
是否和页面动作直接相关
initiator evidence
```

输出到：

```text
reports/protocol-map.md
schemas/request.schema.json
schemas/stream-events.schema.json
```

---

## 6. 阶段三：流式响应分析

对 Pandora 类聊天协议，流式响应分析应在状态机分析之前完成。很多状态信息可能在 SSE 中间事件里逐步出现，而不是只在最终响应里出现。

重点观察：

```text
conversation ID 何时出现
assistant message ID 何时出现
current node 如何变化
重新生成产生新节点还是覆盖节点
停止生成后的消息状态是什么
错误如何返回
取消后的最终事件是什么
```

### 6.1 最低要求

普通 response body 导出如果能完整保留以下事件序列，则足够做协议语义分析：

```text
data: {...}

data: {...}

data: [DONE]
```

必须拿到：

```text
每条 data 事件
事件顺序
结束标记
错误事件
取消后的结果
```

### 6.2 需要 Raw Stream Capture 的情况

如果普通导出无法完整保留 SSE 事件序列，Raw CDP Stream Capture 就是核心依赖，应先补到 `js-reverse-mcp` 再继续分析。

即使普通导出足够，以下行为仍需要 Raw Stream Capture：

```text
每个 chunk 的到达时间
stop-generation
网络中断
未正常结束的流
chunk 边界
心跳
工具调用增量
```

建议输出：

```text
schemas/stream-events.schema.json
reports/stream-events.md
```

不能只看最终响应文本，要看事件序列。

---

## 7. 阶段四：分析状态机

基于协议地图和 SSE 事件序列整理状态关系。

重点观察：

```text
会话如何创建
消息 ID 如何生成
parent / child 如何关联
分支如何产生
重新生成如何表示
修改旧消息如何表示
停止生成是否发送取消请求
继续生成如何表示
错误如何返回
```

建议输出：

```text
reports/state-machine.md
schemas/conversation-state.md
```

状态机文档应区分四类结论：

```text
已观察：抓包中直接看到
已验证：通过对照或重放验证
推测：有证据支持但尚未验证
未知：还没有足够证据
```

---

## 8. 阶段五：定位请求构造代码

对每个核心请求执行以下顺序。

```text
1. inspectBrowserEvidence.list_requests
2. inspectBrowserEvidence.get_request
3. inspectBrowserEvidence.get_request_initiator
4. inspectBrowserEvidence.search_scripts
5. 必要时 runBrowserExperiment.trace_request
6. inspectBrowserEvidence.get_script_source
7. 保存源码片段、调用栈和 paused info
```

目标不是反混淆全部前端，而是找到：

```text
请求在哪个模块发出
请求体在哪里组装
动态字段来自哪里
状态字段来自哪里
哪些字段只是埋点或实验参数
```

只有 initiator 和源码搜索无法解释请求体时，才设置 XHR/fetch 断点。

断点实验结束后要确认：

```text
paused execution 已恢复
breakpoint 已移除
页面没有停在暂停状态
```

---

## 9. 可选诊断：Worker / Service Worker

Worker / Service Worker 对现代复杂网页分析有价值，但不是 Pandora 核心复现的前置阶段。

只有遇到以下情况才进入这一步：

```text
initiator 为空
主页面脚本中找不到请求
请求看起来被 Service Worker 转发
页面操作与请求时间对应，但 frame 中不存在发起栈
```

诊断时确认：

```text
targetType
frameId
targetId
workerUrl
initiator stack
```

如果 `js-reverse-mcp` 当前请求结果缺少 target 元数据，需要优先补到上游。

---

## 10. 阶段六：独立重放验证

浏览器抓到请求不等于理解了协议。需要在 workspace 中做最小重放和字段对照。

重放分两级。

### 10.1 browser-context replay

在当前已登录页面上下文中重放请求。

特点：

```text
自动使用现有 Cookie
最容易验证字段删除和修改
不需要先分析登录
适合第一轮字段实验
```

实现方式：

```text
在当前登录页面中用 fetch 发送变体请求
请求样本来自 artifact
每次只删改一个字段
结果保存到 workspace
```

这一级用于快速判断字段是否可能必需。

### 10.2 external HTTP replay

浏览器外独立脚本重放请求。

需要从已登录浏览器或已捕获请求中整理当前实验需要的：

```text
Cookie
Authorization 或同类认证头
CSRF 或同类防护字段
User-Agent
content-type
必要动态 header
```

然后生成 Python 或 Node 脚本。

这一级才是真正的浏览器外复现。先做 browser-context replay，再做 external HTTP replay，成功率更高。

### 10.3 字段对照

建议步骤：

```text
1. 从 artifact 导出一个核心请求样本
2. 先 browser-context replay
3. 再 external HTTP replay
4. 一次只删除或修改一个字段
5. 保存响应、错误和 diff
6. 更新 schema 和协议报告
```

字段分类：

```text
required
conditionally_required
optional
tracking_only
dynamic
server_generated
client_generated
unknown
```

不要一开始复现登录。浏览器负责正常登录，重放脚本只研究登录后的业务协议。

---

## 11. Pandora 对照验证矩阵

复现的目标不是证明最新版网页字段名和旧 Pandora 完全一致，而是证明能独立发现同一类协议结构和状态关系。

建议维护：

```text
reports/pandora-comparison.md
```

矩阵：

| 分析目标 | 独立观察结果 | Pandora 参考结构 | 是否确认 | 证据 |
| --- | --- | --- | --- | --- |
| 认证方式 | Bearer / Cookie / 其他 | access token | 待确认 | exp / evidence |
| 消息入口 | 请求 path | conversation 类接口 | 待确认 | exp / evidence |
| 流协议 | SSE / WS / chunked | SSE | 待确认 | exp / evidence |
| 首次消息动作 | 实际字段 | next 类语义 | 待确认 | exp / evidence |
| 重新生成 | 实际字段 | variant 类语义 | 待确认 | exp / evidence |
| 继续生成 | 实际字段 | continue 类语义 | 待确认 | exp / evidence |
| 会话关联 | 实际字段 | conversation ID | 待确认 | exp / evidence |
| 消息关联 | 实际字段 | parent message ID | 待确认 | exp / evidence |
| 分支结构 | 实际结构 | message mapping tree | 待确认 | exp / evidence |
| 结束事件 | 实际事件 | `[DONE]` 类结束标记 | 待确认 | exp / evidence |
| 停止生成 | 实际行为 | 取消 / 截断状态 | 待确认 | exp / evidence |
| 错误事件 | 实际事件 | SSE 错误事件或 HTTP 错误 | 待确认 | exp / evidence |

每一行至少引用一个：

```text
experiment_id
evidence_id
artifact_id
```

---

## 12. 最终输出物

完成一轮 Pandora 类复现分析后，workspace 中应有：

```text
reports/protocol-map.md
reports/stream-events.md
reports/state-machine.md
reports/pandora-comparison.md
schemas/request.schema.json
schemas/stream-events.schema.json
schemas/conversation-state.md
scripts/replay-http.py
scripts/diff-json.py
notes/open-questions.md
```

报告应包括：

```text
接口地图
核心请求字段说明
SSE 事件类型
消息状态机
请求构造源码位置
browser-context replay 结果
external HTTP replay 结果
已验证字段
推测字段
未解决问题
下一组实验
```

每个结论必须能回溯到：

```text
experiment_id
evidence_id
artifact_id
```

---

## 13. 最小闭环

最小可用闭环是：

```text
capture_baseline
  ↓
capture_flow: 首次消息
  ↓
list_requests
  ↓
导出请求 / 响应 / SSE 事件序列
  ↓
get_request_initiator
  ↓
search_scripts
  ↓
写 protocol-map.md 和 stream-events.md
  ↓
整理 state-machine.md
  ↓
browser-context replay 一个低风险变体
  ↓
更新 schema、pandora-comparison.md 和 open-questions.md
```

做到这个闭环之后，再扩展到第二轮消息、重新生成、编辑消息、停止生成、标题、删除、文件上传和工具调用。
