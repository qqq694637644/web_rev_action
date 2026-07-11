# Pandora 类复现方法

## 1. 目标

本文描述如何使用 `web_rev_action` 的工具组合，复现 Pandora 类网页协议分析流程。

这里的“复现”指：

- 用正常浏览器和授权账号观察网页端行为。
- 记录页面动作、网络请求、流式响应、WebSocket、调用栈和相关前端脚本。
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

核心思路：

```text
先观察真实网页行为
  ↓
再整理协议地图
  ↓
再定位请求构造代码
  ↓
再建立状态机
  ↓
最后用 workspace 脚本做最小重放验证
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
    file-upload/
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
    state-machine.md
  notes/
    timeline.md
    open-questions.md
```

workspace 只是记录目录，不需要 Git、PR、CI 或 branch 流程。

---

## 4. 第一阶段：建立协议地图

先不要写客户端。先用 `runBrowserExperiment.capture_baseline` 和 `runBrowserExperiment.capture_flow` 记录一组基础动作。

建议实验：

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
12 生成图片或其他工具能力
```

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
响应体或流式事件
WebSocket 消息
请求 initiator
相关脚本 URL 和位置
控制台错误
manifest.json
```

每个实验都要有独立 `experiment_id`，每条关键证据都要有 `evidence_id` 或 `artifact_id`。

---

## 5. 第二阶段：整理请求分类

用 `inspectBrowserEvidence.list_requests` 和 workspace 脚本把请求按用途分类。

常见分类：

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
文件上传
工具调用
遥测 / 埋点
静态资源
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

## 6. 第三阶段：分析状态机

从多轮对话和分支动作中整理状态关系。

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
文件如何上传并绑定到消息
工具调用如何开始、更新、结束
错误如何返回
```

建议输出：

```text
reports/state-machine.md
```

状态机文档应区分四类结论：

```text
已观察：抓包中直接看到
已验证：通过对照或重放验证
推测：有证据支持但尚未验证
未知：还没有足够证据
```

---

## 7. 第四阶段：定位请求构造代码

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

## 8. 第五阶段：流式响应分析

对 SSE、chunked fetch 或长连接响应，优先使用 `js-reverse-mcp` 的常规响应导出。若无法看到增量事件，则需要补 Raw CDP stream capture。

流式分析要记录：

```text
request evidence ID
stream protocol
chunk 顺序
event 类型
event payload schema
结束事件
错误事件
取消事件
是否存在心跳
是否存在工具调用增量
```

建议输出：

```text
schemas/stream-events.schema.json
reports/stream-events.md
```

不能只看最终响应文本，要看事件序列。

---

## 9. 第六阶段：Worker / Service Worker 检查

现代网页可能由 iframe、Worker 或 Service Worker 发起请求。

每条核心请求都应确认：

```text
targetType
frameId
targetId
workerUrl
initiator stack
```

如果 `js-reverse-mcp` 当前请求结果缺少 target 元数据，需要优先补到上游。

没有 target 元数据时，不要轻易判断请求来自主页面脚本。

---

## 10. 第七阶段：独立重放验证

浏览器抓到请求不等于理解了协议。需要在 workspace 中做最小重放和字段对照。

建议步骤：

```text
1. 从 artifact 导出一个核心请求样本
2. 写 replay-http.py 或 Node HTTP 脚本
3. 先重放读取类或低风险请求
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

## 11. 最终输出物

完成一轮 Pandora 类复现分析后，workspace 中应有：

```text
reports/protocol-map.md
reports/state-machine.md
reports/stream-events.md
schemas/request.schema.json
schemas/stream-events.schema.json
scripts/replay-http.py
scripts/diff-json.py
notes/open-questions.md
```

报告应包括：

```text
接口地图
核心请求字段说明
消息状态机
流式事件类型
WebSocket 消息类型
文件上传流程
工具调用流程
请求构造源码位置
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

## 12. 最小闭环

最小可用闭环是：

```text
capture_baseline
  ↓
capture_flow: 首次消息
  ↓
list_requests
  ↓
get_request_initiator
  ↓
search_scripts
  ↓
导出请求 / 响应 / 流事件
  ↓
写 protocol-map.md
  ↓
workspace 脚本重放一个低风险请求
  ↓
更新 schema 和 open-questions.md
```

做到这个闭环之后，再扩展到多轮会话、重新生成、编辑消息、上传文件和工具调用。
