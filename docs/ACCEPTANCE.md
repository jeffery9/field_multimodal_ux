# 验收手册（对照 PRD §10）

## 10.1 通用验收

| # | 验收项 | 手工验证步骤 |
|---|---|---|
| 1 | 从原生界面进入多模态交互 | 打开任意界面 → 顶部「现场助手」图标 → 面板展开 |
| 2 | 语音/文本转写与解析 | （mock 模式）面板文本框输入「这个商品 12345678 的可用库存是多少」→ 点「解析指令」→ 意图卡片显示 `stock_query_product` |
| 3 | 解析结果可预览、修正、取消 | 意图卡片中直接修改参数值；清空文本即取消 |
| 4 | 上下文关联业务对象 | 打开一张拣货单 → 面板顶部显示 `stock.picking #id` |
| 5 | 对象歧义交由用户处理 | 扫一个被多产品共用的条码 → 出现候选列表供选择 |
| 6 | 服务器端权限校验 | 用无库存权限用户执行能力 → 返回 `failed: You do not have access` |
| 7 | AI 无任意 ORM 能力 | 输入「删除所有销售订单」→ 意图为空或能力不匹配，不产生任何写入 |
| 8 | 成功/失败/未知状态区分 | 见下方自动化测试 `test_crash_yields_unknown_not_failed`；界面分别绿/红/黄提示 |
| 9 | 重复请求无重复副作用 | 同一意图连续点两次执行 → 第二次返回 `duplicate` 拒绝 |
| 10 | 原生功能不受影响 | 卸载/禁用 AI 参数为 mock 后，正常表单、拣货、报工流程照常 |

## 10.2 场景验收

### 仓库（`mmux_scenario_warehouse`）
- 扫商品条码（面板条码框输入后回车）→ 出现产品与候选。
- 在拣货单界面口述「实收 5 件」→ 解析为 `picking_report_qty {quantity: 5}` → 执行写入 move line 数量。
- 对 done 状态拣货单重复报数 → 被拒绝（`invalid state` / 原生状态约束）。
- 说「验证这张单」→ 出现 L2 确认卡（对象/操作/影响）→ 确认后走原生 `button_validate`。
- 差异证据：「拍照记录包装破损」→ `picking_capture_evidence` → chatter 出现证据消息。

### 制造（`mmux_scenario_mfg`）
- 「下一道工序」→ `mo_query_next_operation` 返回工序信息。
- 「本工序完成 120 件，其中 3 件不合格」→ 解析为 `{qty_production: 120, qty_scrapped: 3}` → 确认 → 原生 `record_production`。
- 对 cancelled 工单报工 → 拒绝。

### 销售拜访（`mmux_scenario_sales`）
- 拜访结束后口述纪要 → `visit_record_note` → chatter 出现可检查/修正的纪要草稿（删除该消息即撤销）。
- 「查一下这个客户的商机」→ `partner_query_history` 返回订单/商机列表。
- 「安排下周跟进」→ `schedule_followup` 创建原生 mail.activity。
- 「生成报价」→ `create_quote_draft` 强制确认 → 生成 draft 状态 `sale.order`，绝不自动发送/确认。

### 现场服务（`mmux_scenario_fieldservice`）
- 扫铭牌序列号 → `equipment_identify` 返回候选列表（多个匹配时由用户选择，不自动选定）。
- 口述维修步骤/工时/备件 → `service_report_note` 服务报告草稿（chatter，可修正）。
- 「更换密封圈 2 件」→ `service_log_part` 备件记录（草稿，库存过账走原生仓库流程）。
- 「服务完成」→ `service_task_mark_done` 强制确认 → 原生状态流转关闭任务；已关闭任务再次执行被拒绝。

## 10.3 安全验收用例（自动化测试映射）

| 安全测试 | 自动化用例 |
|---|---|
| 未授权用户调用能力 | `test_user_without_group_gets_capabilities_hidden`（core）|
| Record Rules 拒绝访问 | guard `check_access` → `test_access_denied`（tests_pure）|
| 目标记录已删除/状态变化 | `test_report_rejected_on_done_picking`（warehouse）`test_report_rejected_on_cancelled_workorder`（mfg）|
| 参数类型错误/数量不合法 | `test_param_validation` + schema 全套（tests_pure）|
| 用户修改确认前参数 | 意图卡片参数即改即生效，确认卡展示最终参数；服务端以同一请求内重校验（guard.validate→execute）|
| 同一请求重复提交 | `test_duplicate_submit_no_double_side_effect`（warehouse）`test_idempotency_blocks_duplicate`（tests_pure）|
| 服务超时后结果未知 | `test_crash_yields_unknown_not_failed`（tests_pure）：状态=unknown，同键重试被拒 |
| AI 输出不符合结构化协议 | `test_llm_hallucinated_capability_rejected` / `test_llm_arbitrary_orm_rejected`（tests_pure）|
| 视觉识别置信度不足 | Mock vision 返回 `data: null` → 界面显示无法识别，允许修正 |

> 判定红线：任何绕过原生权限、重复产生重大业务副作用、或将未执行操作显示为成功的情况，均视为严重缺陷。

## 运行自动化测试

```bash
# 纯 Python（无需 Odoo）
cd field_multimodal_ux && python3 -m unittest discover -s tests_pure -t .   # 99 个用例

# Odoo 事务测试
odoo-bin -d testdb -i mmux_scenario_warehouse,mmux_scenario_mfg \
    --test-enable --test-tags /mmux --stop-after-init
```

## 成功指标埋点（PRD §14）

`mmux.interaction.log` 已记录：用户、输入类型、能力、执行状态、耗时字段（`duration_ms` 由调用方填充）。基于该表可直接统计任务完成率、拦截率、重复提交率、unknown 率等基线指标。

## 10.6 任务循环验收（Task Loop）

前置：`mmux.llm.provider = mock`，并按用例设置 `mmux.llm.mock_response`。

### 通用

| # | 验收项 | 手工验证步骤 | 期望 |
|---|---|---|---|
| L1 | 任务源契约可见 | 调用 `/mmux/task/sources?scenario=warehouse` | 返回 `wh_picking`，含 `flow_id` 与 `complete_capability` |
| L2 | 取下一个任务 | `/mmux/task/next {"scenario":"warehouse","source_id":"wh_picking"}` | 返回 `task`（含 res_model/res_id）+ `loop.state = "task_active"` + `loop_key` |
| L3 | 无待办时正常结束 | 无分配单据时调用 `/mmux/task/next` | `task = null`，`loop.state = "finished_all"`（**非错误**） |
| L4 | 优先队列 | 造两张待办单据，设置不同 `scheduled_date` | 先返回更早到期的那张 |
| L5 | 精确恢复单条 | `/mmux/task/next {"res_id": <id>}` | 返回指定记录的 task |
| L6 | 游标跨刷新保持 | 取任务 → 记录 `loop_key` → `/mmux/task/current {loop_key}` | 返回同一 task，`state = "task_active"` |
| L7 | 非待办记录不入选 | 把单据取消后按 `res_id` 取 | `task = null` |
| L8 | 审计留痕 | 取任务后查 Interaction Log | 存在 `capability_id_name = "taskloop:warehouse"` 的行 |

### 引导与求助

| # | 验收项 | 手工验证步骤 | 期望 |
|---|---|---|---|
| L9 | 引导绑定当前任务 | `/mmux/task/guide {loop_key}` | `flow_id` 正确，且首步 `step_id` 含当前记录 id |
| L10 | 引导零额外权限 | 在引导中执行 L2 步骤 | 仍返回 `awaiting_confirmation`，不自动过账 |
| L11 | 求助=引导 | mock 返回 `capability_id: null`，`/mmux/task/help` | `action = "guidance"`，**不产生任何写入** |
| L12 | 求助=可执行 | mock 返回 `stock_query_product`，`/mmux/task/help` | `action = "execute"`，附带 intent；执行仍走 `/mmux/execute` 漏斗 |

### 汇报完成

| # | 验收项 | 手工验证步骤 | 期望 |
|---|---|---|---|
| L13 | 能力不匹配被拒 | mock 返回 `stock_query_product`，`/mmux/task/report {"text":"查商品"}` | `state = "task_mismatch"`，**不执行任何动作** |
| L14 | 未识别请重说 | mock 返回 `capability_id: null`，`/mmux/task/report {"text":"你好"}` | `state = "not_understood"`（**非错误**） |
| L15 | LLM 不可用 | 清空 `mmux.llm.provider` 后汇报 | `{code:"llm_unavailable", retryable}`，业务不受影响 |
| L16 | L2 停在确认 | mock 返回 `picking_task_complete`，`confirmed: false` | `state = "awaiting_confirmation"`，`loop.state` 同；单据**仍是未过账** |
| L17 | 确认后完成+发布下一个 | 同上，`confirmed: true` | `state = "completed"`；单据变 `done`；返回**下一个** task（非刚完成的那条） |
| L18 | 循环收尾 | 仅剩一个任务时完成 | `task = null`，`loop.state = "finished_all"` |
| L19 | 已完成任务不重发 | 完成后重新 `/mmux/task/next` | 不会返回刚完成的记录 |
| L20 | 完成计数累加 | 连做两个任务 | 第二次返回 `done_count = 2` |
| L21 | 多语言混杂汇报 | 输入「已完成，quantity verified ok」 | LLM 归一化为纯语言后正常判断并执行 |
| L22 | 重复提交幂等 | 用同一 `loop_key` 连续汇报两次 | 第二次被幂等拦截，不产生重复副作用 |

### 场景闭环

| # | 场景 | 步骤 | 期望 |
|---|---|---|---|
| L23 | 仓库 | 取待办拣货单 → 引导 → 汇报「拣货完成」→ 确认 | 单据 `done`，返回下一张拣货单或无待办 |
| L24 | 制造 | 取待办工序 → 汇报「完成 5 件」→ 确认 | `qty_produced > 0`，发布下一道工序 |
| L25 | 现场服务 | 取我的服务任务 → 汇报「服务完成，更换滤芯」→ 确认 | 任务 `state = 1_done`，chatter 有摘要，发布下一个任务 |
| L26 | 销售拜访 | 取我负责的商机 → 汇报「拜访完成，客户要报价」→ 确认 | chatter 有纪要且 `next_action` 被写入；**商机阶段不变**、仍 `active` |

## 10.7 智能体自主选任务验收（Agent Task Selection）

### 决策与安全

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A1 | 自主选中被采纳 | mock 返回 `selected_task_id = <候选之一>` | 返回的 `task.res_id` 即该记录；`agent.source = "llm"` |
| A2 | 选择理由可读 | 同上，`reason` 含中英双语 | `agent.reason` 本地化为当前用户语言；`reason_i18n` 保留原字典 |
| A3 | 置信度归一 | mock 返回 `confidence = 5` / `-3` / `"n/a"` | 分别钳制为 `1.0` / `0.0` / `0.0`，**不拒绝选择** |
| A4 | alternatives 越界剔除 | 候选中含 1 条合法 + 1 条凭空 id | 凭空 id 被剔除，合法项保留；**主选择仍有效** |
| A5 | 决策留痕 | 查看 `mmux.interaction.log` | 存在 `capability_id_name = "agent_pick:<scenario>"` 记录，含 `source/task_id/candidate_count` |

### 硬边界（幻觉 / 越权）

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A6 | 幻觉 id 拒绝 | mock 返回 `selected_task_id = "wh_picking:999999"` | 该选择被拒；`agent.source = "queue_fallback"`，`rejected_reason` 含 `task_not_in_candidates` |
| A7 | **不能指名他人任务** | mock 返回另一用户（不在本域内）的真实拣货单 id | 拒绝；实际发布的是**自己**的任务；`agent.source = "queue_fallback"` |
| A8 | 协议违规拒绝 | mock 返回额外含 `method` / `model` / `write` 等禁用键 | 拒绝；`rejected_reason` 含 `protocol_violation:<key>` |
| A9 | 非 dict 拒绝 | mock 返回数组 / 字符串 / 数字 | 拒绝，降级队列；**不抛异常** |
| A10 | 空选择拒绝 | mock 返回 `selected_task_id: null` | 拒绝，降级队列 |
| A11 | 客户端无法注入选择 | 请求体中携带任何形如 `selected_task_id` 的字段 | **被忽略**——选择只能来自服务端候选集合 |

### 降级（取任务永不失败）

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A12 | 无 provider | 清空 `mmux.llm.provider` | **仍能取到任务**，`agent.source = "queue_fallback"` |
| A13 | 超时 | `mmux.llm.mock_error = "timeout"` | 仍能取到任务，走队列 |
| A14 | 非法 JSON | mock 返回非 JSON 文本 | 仍能取到任务，走队列 |
| A15 | 空候选 | 当前用户无待办 | `task = null`，`loop.state = "finished_all"`，`agent.source = "no_candidates"` |
| A16 | 兜底等价性 | 对比降级结果与 `TASK_ENGINE.next_task()` | **完全一致**（同一排序口径） |

### 不削弱既有权限

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A17 | **智能体不削弱 L2 确认** | 智能体选中后汇报，`confirmed: false` | `state = "awaiting_confirmation"`，记录**未变更**；`confirmed: true` 才完成 |
| A18 | 复用同一能力栅栏 | 智能体选中 → 汇报完成 | 走 `execute_capability → ExecutionGuard`，与手动路径**无差别** |
| A19 | `res_id` 恢复不咨询智能体 | 带 `res_id` 调 `/mmux/task/next` | 直接返回该记录，`agent.task_id = null`（未做决策） |
| A20 | 强制纯队列 | 服务层 `use_agent = False` | 返回纯优先队列结果，不调用 LLM |

### 向后兼容

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A21 | 响应键不变 | 检查 `/mmux/task/next` 响应 | `task/loop/loop_key/loop_label/sources` 五键语义完全不变；`agent` 为纯增量 |
| A22 | 理由跨刷新不丢 | 取任务 → `/mmux/task/current` | `agent.task_id` 与 `reason` 从游标恢复 |
| A23 | 数据最小化 | 检查送模型的候选字段 | 仅 `task_id/source_id/scenario/title/priority/due/kind`；**不含**成本价/供应商/内部备注 |

### 端到端

| # | 场景 | 步骤 | 期望 |
|---|---|---|---|
| A24 | 自主选择→完成→继续 | mock 让智能体选第 2 张单 → 汇报完成 → 确认 | 完成的是第 2 张；下一轮发布剩余的第 1 张；`done_count = 1` |

---

## 10.9 场景上下文识别验收（Scenario Context, v3.1）

> 目标：智能体能**根据用户当前所在的 web client 位置**判断作业场景。
> 核心约束：**位置决定「候选集合」，LLM 只能在候选集合内消歧；场景是安全边界，不是模型的自选项。**

### 位置信号与确定性判定

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A25 | 强信号直接判定 | `signal = {res_model: "stock.picking"}` | `scenario = "warehouse"`，`scenario_context.source = "signal"`，**不提问** |
| A26 | action 优先级最高 | `signal = {res_model: "stock.move", action_xmlid: "mrp.workorder_action"}` | 按 `action`（权重 100）判定为 `manufacturing`，`res_model` 的歧义被打破 |
| A27 | 弱信号不判定 | `signal = {menu_xmlid: "stock.menu_stock_operations"}`（权重 30 < `MIN_DECISIVE_SCORE`） | **不判定**；`needs_asking = True` |
| A28 | 未注册场景不推荐 | 映射表指向一个未安装的场景 | 该 hint **被丢弃**；不产生"有场景但无工具"的空白名单 |
| A29 | 候选列表稳定 | 同一信号连续请求 3 次 | `candidates` 顺序**完全一致**（同分按 code 排序，不随哈希漂移） |

### 歧义必须提问（不得猜测）

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A30 | 多义模型触发提问 | `signal = {res_model: "stock.move"}`（映射到 2 个场景） | `state = "awaiting_user"`；`scenario_context.needs_asking = True`；**未选择任何场景** |
| A31 | 无信号触发提问 | `signal = {}` | 同上；且**绝不**回退到"全局能力"（那等于权限扩大） |
| A32 | 提问必须可回答 | 检查 `pending_options` | **非空**，且全部来自服务端已知场景码；空选项的提问是死路 |
| A33 | 两种提问文案不同 | 对比歧义 / 无信号 | 歧义："当前位置可能属于多个场景（…）"；无信号："我无法从当前位置判断…" |
| A34 | 答复即选定 | 用 `pending_options` 中的值答复 | `scenario` 被设为该值，`scenario_context.source = "user"`，`pending_field` 清空 |
| A35 | **集合外答复被拒** | 答复 `"definitely_not_a_scenario"` | 拒绝；`state` 仍为 `awaiting_user`，`pending_field` 仍为 `"scenario"`，**用同一候选集重新提问** |
| A36 | 近似串不被改写 | 答复 `"sales_visit_typo"` | **拒绝**（不模糊匹配为 `sales_visit`）；错字必须重新提问，而不是被静默纠正到别的场景 |
| A37 | 一词不偷另一词 | 候选中同时含 `sales` 与 `sales_visit`，答复 `"sales"` | 精确匹配优先，判定为 `sales`（不被更长的候选抢占） |
| A38 | 词边界内的短语可接受 | 答复 `"the warehouse one"` / `"仓库 warehouse"` | 命中 `warehouse`（整词匹配，不是裸子串包含） |

### LLM 只能消歧，不能设界

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A39 | 候选集内消歧被接受 | `stock.move`（候选 = warehouse / manufacturing），mock 选 `warehouse` | `scenario = "warehouse"`，`choice_source = "llm"` |
| A40 | **候选集外被拒** | 同上，mock 选 `sales_visit`（真实存在但不在候选内） | **拒绝**；`rejected_reason` 含 `scenario_not_in_candidates`；回落到提问 |
| A41 | 幻觉场景被拒 | mock 选 `not_a_real_scenario` | 拒绝；回落到提问 |
| A42 | 协议违规优先报 | mock 返回含 `model` / `method` 等 `FORBIDDEN_KEYS` | `rejected_reason` 为 `protocol_violation:<key>`（**先于**未声明键检查） |
| A43 | 未声明键整条拒 | 返回中多一个未声明键 | `undeclared_key:<keys>`；**不接受"取好的那一半"** |
| A44 | 无 provider 降级 | 清空 `mmux.llm.provider` | **仍产出可回答的提问**（`pending_options` 非空），不是错误 |
| A45 | 超时降级 | `mmux.llm.mock_error = "timeout"` | 同上，降级为提问 |
| A46 | 提问与校验同源 | 检查提示词契约 | `CHOICE_KEYS` / `FORBIDDEN_KEYS` 由同一常量**生成**，提示词与校验器不可能漂移 |

### 已声明场景优先（不因位置被覆盖）

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A47 | 声明优先于位置 | `loop = {scenario: "manufacturing"}` + `signal = {res_model: "stock.picking"}` | `scenario` 仍为 `manufacturing`（**不被位置覆盖**） |
| A48 | 分歧仍上报 | 同上 | `scenario_context.stated_scenario = "manufacturing"`，`position_scenario = "warehouse"`，`position_mismatch = True` |
| A49 | 分歧不阻塞作业 | 同上 | `state ≠ "awaiting_user"`；已声明场景时，探测不构成拦截 |
| A50 | 分歧可见于视图 | 前端 `agent_trace.xml` | 渲染"当前位置看起来像 X，但本次作业按 Y 进行"告警 |

### GPS：只采集，不判定

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A51 | 默认权重为 0 | 检查 `WEIGHT_GEO` | `= 0`；纯 GPS 信号**不判定任何场景** |
| A52 | 纯 GPS 仍提问 | `signal = {geo: {lat, lng}}` | `needs_asking = True`，`geo_recorded = True`，`scenario` 为空 |
| A53 | 坐标不回显到视图 | 检查 `/mmux/agent/start` 响应 | 响应体中**不含**经纬度数值；仅 `geo_recorded: true` |
| A54 | 坐标不写入审计 | 检查 `mmux.interaction.log.detail` | 同样**不含**经纬度数值（`to_dict()` 默认脱敏为布尔） |
| A55 | GPS 不破坏真信号 | `signal = {res_model: "stock.picking", geo: {...}}` | 仍判定 `warehouse`；`geo_recorded = True` |
| A56 | 采集默认关闭 | 检查场景配置 | 全部场景 `geo_collect = False`，`geo_weight = 0` |
| A57 | 客户端被告知不采集 | 检查响应 | `geo_collect: false`；未启用时浏览器**不会**被请求定位权限 |
| A58 | 开启后才采集 | 管理员置 `geo_collect = True` | 响应 `geo_collect: true`；客户端才允许请求定位，且**每会话至多一次** |
| A59 | 权重与采集联动 | 仅设 `geo_weight = 50` 而不开 `geo_collect` | **校验报错**（配置了却永不生效，比报错更难被发现） |
| A60 | 前端只上报证据 | 检查 `context_service.js` | 只上报 `res_model` / `action_xmlid` / `menu_xmlid` / `geo`；**从不上报 scenario** |

### 向后兼容（旧客户端零回归）

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A61 | `signal=None` 走原路径 | 不传 `signal` 参数 | `scenario_context` 为 `None`；**不提问**；行为与 v3.0 逐字节一致 |
| A62 | `null` ≠ `{}` | 对比「不传」与「传 `{}`」 | 不传 = 不适用（静默）；传 `{}` = 查过但没有（提问）。二者绝不混同 |
| A63 | 前端省略而非默认 | 检查 `mmux_api.js` | 无信号时**省略** `signal` 键，而不是 `|| {}` |
| A64 | 上下文服务缺失可降级 | `mmux_context` 服务抛错或不存在 | `_currentSignal()` 返回 `null`（= 不适用），作业不中断 |

### 审计

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| A65 | 判定成功留痕 | 查看 `mmux.interaction.log` | 存在 `capability_id_name = "scenario_context"`，`exec_status = "success"` |
| A66 | **提问也留痕** | 无信号触发提问 | 同样留痕，`exec_status = "failed"`，`detail.candidates` 非空（"没能确定"本身就是要能被统计的事件） |
| A67 | 分歧留痕 | 声明场景 + 冲突位置 | 仍写入探测记录，分歧不被抹掉 |

### 端到端

| # | 场景 | 步骤 | 期望 |
|---|---|---|---|
| A68 | 位置判定 → 收窄白名单 | `signal` 指向拣货 → 启动 agent | 白名单等于 `CAPABILITY_REGISTRY.for_scenario("warehouse")` 的启用项；他场景能力不在内 |
| A69 | 位置未知 → 提问 → 继续 | `signal = {}` → 答复 `warehouse` | 提问被消费；后续轮次在 `warehouse` 下继续；刷新后场景不丢 |
| A70 | 提问跨轮次不漂移 | 答复被拒后再次提问 | 候选集与上一次**完全相同**（同一 `_resolution_from_blob` 重建） |

## 10.10 任务会话与推理循环验收（Task Session & Agent Loop，v3.5）

> 目标：**agent loop 的核心工作模式是推理，不是预制工作流 step**；
> **一个任务 = 一个对话 session**，切换任务即切换 session。
> 设计文档见 [docs/CONFIG.md §14](CONFIG.md)。

### 会话即任务（session 边界）

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| S1 | 一任务一 session | `/mmux/agent/start`（带 `loop_key` 的任务游标） | 响应 `loop_key` 即 session 身份；该 session 的记忆行以 `agent:<loop_key>` 为键 |
| S2 | 刷新恢复同 session | 记录 `loop_key` → 重新 `/mmux/agent/turn {loop_key}` | 目标、history、done_steps、待确认项**原样续上**，不是新 session |
| S3 | 任务完成即终结 | `finish(goal_met=true)` 或 `task/report` 成功后再发 `agent_turn` | 返回 `code="unknown_loop"` 结构化错误；**不静默开新循环** |
| S4 | 放弃/过期即终结 | session 超过日志保留期或管理员作废游标后请求 turn | 同上；操作员须从 `task/next` 取任务、起新 session |
| S5 | 新任务不继承旧记忆 | 完成任务 A → `task/next` 取任务 B → start | B 的 session 中 `goal`/`history`/`done_steps`/`recent_outcomes` **全部为空/仅含 B**；A 的任何推理状态不可见 |
| S6 | 跨 session 只带用户级偏好 | 检查新 session 初始化 | 允许继承：`skill_level`、语言；**不允许**继承：上一任务的 params/res_id/待确认动作 |
| S7 | 确认不跨任务复用 | 任务 A 停在 `awaiting_confirmation` → 放弃 A → 对 A 的 loop_key 发 `confirmed=true` | 拒绝（session 已终结）；不存在"用一次确认放行别的任务写入"的路径 |

### 推理优先（非预制 step 驱动）

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| R1 | observation 无游标驱动 | 检查 `/mmux/agent/turn` 响应的 observe 内容 | 含 `goal` / `world_state` / `last_result` / `recipe_reference`；**不含** "current_step 必须执行"式指令 |
| R2 | recipe 是参考不是脚本 | mock LLM 跳过 recipe 第 2 步直接调合法能力 | 动作被接受（能力在白名单内即可）；轨迹里 `source="llm"`，不被纠正回第 2 步 |
| R3 | 世界状态可见 | 能力调用改变了记录字段 | 下一轮 observation 的 `world_state` 反映**改后**的值；模型可据此判断"无需重复报数" |
| R4 | LLM 不可用退化为引导 | 清空 provider 后 start | 逐 step 的 recipe 导航（`LOOP_SOURCE_RECIPE`），操作员体验退化而非报错——兜底路径保留 |

### 账本与历史

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| H1 | done_steps 净化 | 连续多轮后检查 scratchpad | `done_steps` 只含人类可读 `step_id`；**无**内部签名字符串回流进 prompt |
| H2 | 因果链闭合 | call_capability 成功后看下一轮 | observation 出现对应 `recent_outcomes` 摘要（有界，保留最近 N 条） |
| H3 | history 有界增长 | 跑满预算回合 | `blob["history"]` 每回合追加且按 `MAX_AGENT_TURNS` 截断；只含模型自己的动作，**不含**原始语音转写 |
| H4 | pending_options 读写对称 | `ask_user` 带 options → 检查响应与 blob | `pending_options` 被写入并在视图中渲染为按钮；答复后被清空 |

### 停滞检测与幂等

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| P1 | 合法重试不被误杀 | 同一能力换参数重试一次并拿到新结果 | **不**判停滞（有新信息/世界变化即重置计数） |
| P2 | 原地打转被抓 | 连续 K 次动作既无 world_state 变化也无新信息 | 安全终止并说明原因；不是静默死循环 |
| P3 | per-turn 幂等键 | 同一 session 两个回合各调一次不同能力 | 两次都执行；第二次**不被**幂等拦截（键为 `{loop_key}:agent:{turn}`） |
| P4 | 同回合重放被拦 | 网络重试导致同一 turn 的动作重复送达 | 幂等拦截，零重复副作用 |

### 安全不变量回归（范式转移不松动）

| # | 用例 | 操作 | 期望 |
|---|---|---|---|
| G1 | 动作集仍封闭 | mock 返回 `{"action":"sql_query"}` | `unknown_action:sql_query` 拒绝 |
| G2 | 负载键仍封闭 | 合法动作 + 未声明键 | 整条拒绝，不"取好的那一半" |
| G3 | L2 确认门独立于 session | 推理模式下发起 L2 能力 | 仍停 `awaiting_confirmation`、`confirmed=False` 硬编码；单据未过账 |
| G4 | 白名单仍由场景钉死 | warehouse session 中提议 mfg 能力 | 拒绝（capability 不在本 session 场景白名单） |
| G5 | 必然终止 | 任意输入 | 回合/能力/停滞三重预算之一保证循环结束，测试限时通过 |
