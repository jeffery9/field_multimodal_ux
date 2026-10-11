# 配置与扩展指南

## 1. 场景配置（FR-012）

「Field Assistant → Configuration → Scenarios」。每个场景声明：

- `code`：稳定标识（能力通过它归属场景）
- `default_input`：默认交互方式（barcode / voice / camera / form）
- 界面同时展示该场景在代码中注册的能力白名单（只读）

安装时预置 `warehouse`（仓库）与 `manufacturing`（制造）两个场景。

## 2. 能力注册（FR-014/FR-015）

能力只能通过 Python 代码注册（首期决策，符合 PRD「不为注册能力强制创建新业务模型」）。示例：

```python
from odoo.addons.mmux_core.services.mmux_core import (
    CAPABILITY_REGISTRY, Capability, ParamSchema, Field,
)

def _my_executor(env, record, params):
    record.do_something_native(params["qty"])   # 必须调用既有业务方法
    return {"state": record.state}

CAPABILITY_REGISTRY.register(Capability(
    capability_id="my_scenario_my_action",      # 全局唯一
    name="Do something",
    description="执行影响说明（会展示在确认对话中）",
    target_model="my.model",
    action="do_something_native",
    schema=ParamSchema([
        Field("qty", "float", required=True, min_val=0),
    ]),
    risk="L2",                    # L0 查询 / L1 低风险 / L2 状态变更 / L3 高风险
    require_confirmation=True,    # L2/L3 自动强制
    scenario="my_scenario",
    precondition=lambda record, params: None,   # 状态前置检查，可抛 UserError
    executor=_my_executor,
    result_check=lambda env, record, result: result,  # 验证实际执行结果
))
```

能力要点：

- **risk="L3" 的能力默认 enabled=False**，需管理员改代码或配置后才会出现。
- 执行器中禁止直接 `env[model].create/write/unlink` 绕过业务方法——代码评审时重点检查。
- 参数 schema 严格校验：未知字段、类型错误、越界一律拒绝。
- **不需要注册关键词/规则**：语义识别由 LLM 完成，能力只需声明 `capability_id` 与参数 schema；
  LLM 会在提示词中看到 `to_public_dict()` 暴露的这份声明。

## 3. Provider 扩展

实现 `BaseProvider` 子类并注册：

```python
from odoo.addons.mmux_core.services.mmux_core import PROVIDER_REGISTRY
from odoo.addons.mmux_core.services.mmux_core.provider import BaseProvider

class MyASR(BaseProvider):
    kind, name = "asr", "my_asr"
    def call(self, payload, config):
        return {"text": "...", "confidence": 0.9}

PROVIDER_REGISTRY.register(MyASR())
```

随后 `mmux.asr.provider = my_asr` 即可切换。OCR/视觉/LLM 同理（kind：`ocr` / `vision` / `llm`）。

**新注册的 provider 会自动出现在设置页的下拉里**——选项由 `PROVIDER_REGISTRY.names(kind)`
实时生成，不需要改视图或改代码清单。`mock` 永远可选。

## 3.1 配置入口：设置页（v3.1.1）

配置**不再意味着**去「设置 → 技术 → 系统参数」敲键名。管理员在
*Field Assistant → Configuration → Settings* 一个页面完成全部配置：

```python
class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    mmux_llm_provider = fields.Selection(
        selection=lambda self: self._provider_selection("llm"),
        config_parameter="mmux.llm.provider")
```

**字段用 `config_parameter=` 直接指向既有参数键**，这是本节唯一重要的设计决定：

- 键名未变 → **没有迁移**。旧配置就在同一把键上，升级即生效；
- 读与写是**同一份数据** → 不存在"设置页改了但运行时没生效"；
- 现有测试里满地的 `set_param(...)` **继续有效**，因为它们操作的就是这些键。

### 密钥为什么没有 `config_parameter`

`mmux_llm_api_key` / `mmux_asr_api_key` 是**仅有的两个不带该属性**的字段，由
`get_values` / `set_values` 手工管理。原因是 `res.config.settings.default_get()`
会**遍历每一个带 `config_parameter` 的字段并用参数值填充它**，而且这一步发生在
合并 `get_values()` **之前**：

```python
# odoo/addons/base/models/res_config.py
for name, icp in classified['config']:      # ← 密钥会被填进来
    value = IrConfigParameter.get_param(icp, ...)
    res[name] = value
res.update(self.get_values())               # ← 此时再 pop 已经晚了
```

所以「在 `get_values()` 里 pop 掉密钥」是**不够的**——框架已经把明文放回 payload 了。
让它**根本不进入 `classified['config']`**，才是真正把密钥挡在表单之外的做法。

配套的两条不变式：

| 规则 | 原因 |
|---|---|
| **读不出来**：输入框永远为空 + 一个 `*_api_key_set` 只读布尔 | `password="True"` 只遮**渲染**，ORM 照样返回明文 |
| **留空不改**：空值视为"不修改"，不写参数 | 输入框为空 → 原样保存会提交空串 → 会把可用密钥**删掉** |

### 校验前置

错误在**设置页保存时**就报，而不是等到现场作业时才变成 `ProviderError`：

- `timeout` 超出 `[3, 3600]` → 报错（下限对齐 provider 适配器的 `max(3, ...)` 钳制，
  把"静默改成 3"变成"显式拒绝"）；
- provider 名不在注册表里 → 报错；
- **注册表为空时校验让位**：`mmux.ocr.provider` 目前没有已注册的 `ocr` provider，
  若照搬白名单校验，该字段将**永远无法设置任何值**——那不是校验，是锁死。

## 4. 语义识别（LLM 唯一路径 + 英文归一化）

**无论输入是混合语言还是纯单语言，一律先送 LLM 做语义识别。** 没有关键词/规则解析器，
不存在"规则优先、LLM 兜底"。

```
任意输入 → ① LLM 语义识别 → ② 归一化为纯英文（固定 en_US）→ ③ Odoo 按 capability_id + params 执行
```

LLM 同一次回答里完成三件事，返回严格 JSON：

```json
{
  "normalized_text": "set the stock quantity of MMUX-0001 to 5 pieces",
  "capability_id": "picking_report_qty",
  "params": {"quantity": 5, "barcode": "MMUX-0001"},
  "missing": [],
  "ambiguous": [],
  "confidence": 0.9
}
```

- `normalized_text`：整句英文改写，业务实体逐字保留（型号/数量/批次/单据号**不翻译**）。
- 提示词注入能力白名单与参数 schema；`detect_languages` 的结果作为 `detected_scripts` 一并告知模型。

**服务端严格校验**（`IntentParser.validate_llm_output`）：

1. 非 dict / 非对象 → 空意图
2. **顶层黑名单键扫描**：出现 `model`/`method`/`ids`/`create`/`write`/`unlink`/`search`/`sql`/`sudo`
   等即判 **协议违规** → 空意图 + `ambiguous`（安全兜底，不修补）
3. `capability_id` 不在白名单（幻觉 id）→ 空意图 + `ambiguous=["unknown capability ..."]`
4. 参数过 schema 严格校验；缺必填 → 写入 `missing`
5. **数量/条码一致性护栏**：用确定性提取器比对 `params`，不一致写入 `ambiguous`，缺失则补全
6. `confidence` clamp 到 [0,1]

### 错误与空结果的语义边界

| 情形 | 返回 |
|---|---|
| LLM 未配置 / 超时 / 网络错误 / 非法 JSON | 结构化错误 `{code:"llm_unavailable", retryable}` |
| LLM 正常回答，`capability_id=null`（无法映射到任何能力） | **空意图**（提示"换个说法"），**不报错** |
| LLM 正常回答但 id 不在白名单 | **空意图** + `ambiguous`，不报错 |

原则：**流程失效 → 错误；语义未命中 → 空意图。**

结构化错误形态（前端按 `code`/`retryable` 分支）：

```json
{"error": "LLM request timeout", "code": "llm_unavailable",
 "retryable": true, "detail": {"subtag": "timeout"}}
```

`subtag` 取值：`not_configured` / `timeout` / `provider_error` / `invalid_json`。

> ⚠️ **离线可用性**：未配置 LLM provider 时 `/mmux/parse_intent` 不可用（有意放弃离线规则解析）。
> 业务执行（`/mmux/execute`）与原生 Odoo 流程**完全不受影响**。

## 5. 幂等策略（PRD §6.4）

- 前端为每个用户意图生成一次 `idempotency_key`，重试复用同一键。
- 服务端 `mmux.interaction.log` 以键去重：`submitted/success/unknown` 状态的键拒绝再次执行。
- 超时导致 `unknown` 时，界面提示先核实记录状态，不做盲目重试。

## 6. 自动编排（Plan Orchestration / Agent Loop）

后端提供**受约束的多步编排引擎**，使现场指令可以编排为多步计划自动循环执行，同时不突破 PRD 边界（无通用 Agent Runtime、无任意 ORM）：

### 概念

- **Plan** = 有序步骤列表；每个步骤只能引用**已注册能力**（白名单）。
- **绑定（bindings）**：参数值只能是字面量、`$input:name`（用户补充输入）、`$stepN.result.field`（前置步骤结果）或 `$context:res_id`（当前界面上下文）。
- **执行循环**：逐步串行，每一步都走同一个 ExecutionGuard（schema → ACL → 状态 → 风险确认 → 幂等）。循环在 `input_required / awaiting_confirmation / failed / unknown` 处**暂停等待用户**，绝不自我修复或越权继续。
- **计划级幂等**：同一 idempotency key 的计划运行结果（成功/进行中）拒绝重入。

### API

| 路由 | 说明 |
|---|---|
| `/mmux/plan/propose` | 提案计划：先匹配代码定义计划库，未命中则由 LLM 生成提案（**仅提案，不执行**，且必须通过 `validate_llm_plan` 白名单校验） |
| `/mmux/plan/run` | 运行计划：传 `plan_id`（代码库）或 `llm_plan`（已验证提案）+ `inputs` + `idempotency_key`；暂停后带同一 key + 补充的 `inputs`/`confirmed_steps` 恢复 |

### 代码定义计划示例（scenario 模块内）

```python
from odoo.addons.mmux_core.services.mmux_core import Plan, PlanStep, register_plan

plan = register_plan(Plan(
    "my_closeout", "现场收尾",
    [
        PlanStep("s1", "equipment_identify", bindings={"barcode": "$input:serial"}),
        PlanStep("s2", "equipment_query_history",
                 record_ref="$step1.result.candidates.0.id"),
        PlanStep("s3", "service_task_mark_done", record_ref="$input:task_id"),
    ]))
plan._keywords = ("收尾", "closeout")   # 供 propose_plan 离线匹配
```

内置示例：`wh_receipt_roundtrip`（收货闭环）、`fs_service_closeout`（服务收尾）。

### 安全不变量

- LLM 提案中出现未注册能力、未声明参数、前向引用 → 计划整体拒绝，**零步骤执行**。
- L2 步骤（如 `picking_validate`、`service_task_mark_done`）在计划中同样强制人工确认。
- 计划运行的每一步与手动执行能力走完全相同的守卫与审计（`mmux.interaction.log` 记录 `plan:<plan_id>`）。

## 7. 语音引导 / 任务导航（Guided Task Flow）

多模态内核新增**语音引导模式**：后端把现场任务（如拣货）拆解为有序步骤，逐条语音播报引导用户，用户以扫码 / 口述数量 / 确认回应，闭环推进到任务完成。

### 组成

- **TaskFlow（流程定义，代码优先）**：场景模块声明 `steps_builder(env, params) -> [GuidedStep]`，从**实时业务记录**生成步骤（如当前用户的待办拣货单 → 每个明细一条扫码步骤 + 一条报数步骤）。
- **GuidedStep（步骤）**：`voice_prompt`（TTS 播报文本）、`input_type`（scan / voice_qty / confirm / capability）、可选 `capability_id`（步骤完成时调用的白名单能力）、`skippable`（可跳过，如"到达单据"）。
- **游标**：每次引导运行的进度（done_ids）持久化在 `mmux.interaction.log`，页面切换/刷新后可恢复。

### API

| 路由 | 说明 |
|---|---|
| `/mmux/guide/flows` | 列出可用引导流程 |
| `/mmux/guide/start` | 开始流程，返回第一步语音提示 |
| `/mmux/guide/current` | 恢复/刷新当前引导状态 |
| `/mmux/guide/advance` | 完成或跳过当前步骤；能力步骤走**同一个执行守卫** |

### TTS

前端使用浏览器 Web Speech API（`speechSynthesis`）本地合成语音，**无需任何外部服务**，离线可用；不支持的浏览器自动降级为纯文字提示（渐进增强，PRD §2.6）。如需云端音色，可在 Provider Registry 注册 `tts` 类 Provider 扩展。

### 安全与顺序不变量

- **严格顺序**：只能完成当前游标步骤；跳到后面的步骤会被拒绝（`test_cannot_complete_out_of_order`）。
- **零额外权限**：引导步骤中声明的能力（如 `picking_report_qty`、`picking_validate`）与手动执行走**完全相同**的守卫——L2 步骤在引导循环中同样暂停等待确认，引导模式不授予任何额外权限。
- **幂等**：每步用 `guide_key:step_id` 作幂等键，网络重试不重复过账。
- 内置流程：
  - `wh_guided_picking`（语音引导拣货）
  - `mfg_guided_workorder`（语音引导工单作业）

  新增流程只需在场景模块注册 `TaskFlow`，无需改动内核或前端。

## 8. 新手 / 熟手模式（Skill Levels）

引导流程支持两种技能等级，**只改变步骤粒度与播报详细度，不改变任何权限**：

| 维度 | 新手模式 novice（默认） | 熟手模式 expert |
|---|---|---|
| 步骤粒度 | 复合动作拆细（到达 → 找库位 → 扫码 → 确认匹配 → 报数 → 复核） | 合并（库位+商品+数量一句搞定） |
| 语音播报 | 长句，含"为什么"和操作要点 | 短句，只报关键信息 |
| 屏上提示 | 显示 `hint`（操作要点）与 `expected`（期望值/建议数量）供自检 | 不显示，界面更干净 |
| 额外引导 | 含开工前检查、异常上报等可选步骤 | 跳过非必要步骤 |
| 权限 | 完全相同——L2 步骤仍强制确认 | 完全相同——L2 步骤仍强制确认 |

### 流程定义（场景模块）

```python
register_flow(TaskFlow(
    "my_flow", "我的引导流",
    steps_builder_novice=_novice_steps,   # 细粒度 + hint/expected
    steps_builder_expert=_expert_steps,   # 合并 + 精简
))
```

也可只提供单个 `steps_builder`，此时两种等级使用同一套步骤。

### 用户偏好

- 字段：`res.users.mmux_skill_level`（`novice` 默认 / `expert`），跨会话保存。
- 前端：引导标签页顶部「新手模式 / 熟手模式」切换，运行中也可一键切换（下次构建步骤生效）。
- API：`/mmux/guide/skill_level`（不带参数读取，带 `level` 参数设置）。

### 设计要点

`GuidedStep` 新增两个新手专用字段：

- `hint`：屏幕上的操作要点提示（新手模式显示）
- `expected`：期望值/建议值，让新手在提交前自检（如"应为：商品 XX"、"建议 5 个"）

测试覆盖（`tests_pure/test_flow.py::TestSkillLevels`、`test_mmux_skill_levels.py`）：
新手步骤数 > 熟手；两者能力集合一致（熟手不得增加权限）；新手播报总字数更多且带 hint/expected；
未知等级回退新手；熟手模式下 L2 验证步骤仍走确认守卫。

## 9. 多语言与多语言混合（Multilingual & Mixed-Language）

现场作业常出现"中文动词 + 英文术语 + 型号编码"的混合口语（如 *"把 MMUX-0001 的
stock 数量改成 5 个"*）。本架构的统一处理方式是：

> **混合语言与纯单语言走同一条 LLM 语义路径，统一归一化为一种纯语言（固定英文）。**

### 9.1 归一化（核心变化）

LLM 在同一次回答中既完成语义识别，又输出英文归一化文本 `normalized_text`：

```python
intent = service.parse_intent("把 MMUX-0001 的 stock 数量改成 5 个")
# intent["normalized_text"] == "set the stock quantity of MMUX-0001 to 5 pieces"
# intent["capability_id"]    == "picking_report_qty"
# intent["params"]           == {"quantity": 5, "barcode": "MMUX-0001"}
```

- 目标语言 **固定 `en_US`**（`mmux_core.intent.NORMALIZED_LANG`）。
- 业务实体（型号 `MMUX-0001`、数量、批次、单据号）**逐字保留，绝不翻译**。
- `normalized_text` **仅用于审计留痕 + 抽参语言上下文**，**不进入执行链**：
  执行入口始终只有 `capability_id + params`，仍走十步守卫链。

### 9.2 脚本检测（仅用于提示与审计）

```python
from mmux_core.i18n import detect_languages, is_mixed, mixed_language_hint

detect_languages("把 MMUX-0001 的 stock 数量改成 5")  # -> ["zh", "en"]
detect_languages("数量を報告 してください")            # -> ["ja"]  假名优先于汉字
mixed_language_hint("abc 中文")                       # -> "en+zh"
```

支持 `zh / ja / ko / en / ru / ar / th` 等脚本区间；纯数字/编码返回空列表。
检测结果有两条去向：**(a)** 作为 `detected_scripts` 注入提示词，告知模型输入含哪些语言；
**(b)** 作为审计标签（`languages` / `lang_hint`）。**不再参与任何匹配**（已无规则）。

### 9.3 输出本地化（不受影响）

引导步骤、能力名称等文本用 `{lang: text}` 字典声明，按回退链解析：

```python
from mmux_core.i18n import resolve_text, resolve_dict

resolve_text({"zh_CN": "你好", "en_US": "hello"}, "zh_CN")  # -> "你好"
resolve_text({"en_US": "hello"}, "ja_JP")                   # -> "hello"（兜底）
resolve_dict(step_dict, "zh_CN")
```

场景模块用 `T(zh, en)` 辅助函数声明双语文本。`resolve_text` **永不抛异常**：
字典无用户语言时返回最后一个可用值，避免引导因缺翻译而中断。

### 9.4 数量 / 条码一致性护栏

归一化后，用**确定性提取器**（`mmux_core.quantity.extract_quantities`）比对 LLM 的 `params`：

```python
from mmux_core.quantity import extract_quantities
extract_quantities("把 MMUX-0001 的 stock 数量改成 5 个")
# -> {"qty": 5.0, "barcode": "MMUX-0001", "scrap": None}
```

实现要点：**先识别并遮蔽型号编码，再提数量** —— 编码里的数字绝不会被当成数量。
`reconcile_with_text` 将不一致写入 `ambiguous`（不静默采信），缺失则补全。

### 9.5 语音与 LLM

- **ASR**：`/mmux/transcribe` 接受 `lang` 参数（缺省取当前用户语言），提升识别准确率；
  混合语音由 LLM 在语义识别阶段统一处理。
- **TTS**：前端 `tts.setLang()` 按用户语言选择发音（Web Speech API）。
- **LLM**：提示词显式说明"用户可能混合多种语言，请全部理解并归一化为英文"。

### 9.6 API 速查

| 路由 | 参数 | 说明 |
|---|---|---|
| `/mmux/parse_intent` | `text`, `context?`, `langs?` | **LLM 唯一路径**；`langs` 已废弃（保留但忽略） |
| `/mmux/transcribe` | `audio`, `lang?` | 语音转写；`lang` 提升准确率 |
| `/mmux/guide/flows` | ― | 流程列表，按用户语言本地化 |
| `/mmux/guide/start` | `flow_id`, `params` | 启动引导，返回本地化步骤与首句播报 |

返回含 `normalized_text`（英文归一化）、`languages`（检测到的脚本）、`lang_hint`。

### 9.7 测试覆盖

- `tests_pure/test_i18n.py`：归一/回退链/脚本检测/文本解析
- `tests_pure/test_llm_intent.py`：LLM 输出校验、黑名单键拒绝、一致性护栏
- `tests_pure/test_llm_errors.py`：Mock provider 受控失败注入 + 错误契约
- `tests_pure/test_normalize_en.py`：混合/纯中/纯英 → 英文归一化且实体逐字保留
- `mmux_core/tests/test_mmux_llm_intent.py`：Odoo 层错误路径、审计记录、执行链不吃归一化文本

---

## 10. 任务循环（Task Loop）

### 10.1 概念

任务循环回答的是"**当前这个人接下来该干哪件活**"。它把既有的三块拼起来：

| 已有构件 | 回答的问题 |
|---|---|
| Capability Registry + ExecutionGuard | **允许做什么**（白名单、风险、幂等、审计） |
| PlanOrchestrator | **一次运行如何串多个能力** |
| TaskFlow | **单个配方如何逐步导航**（语音引导） |
| **Task Queue（本篇）** | **下一个工作项是哪个** |

任务循环本身**不执行任何东西**——`/mmux/task/report` 汇报完成时走的是同一个
`execute_capability` 漏斗，因此**引导/循环模式不带来任何额外权限**。

### 10.2 任务来源（TaskSource）

任务来源由**场景模块代码注册**（code-first，与能力/流程一致），内核只提供抽象。
每个来源声明：任务模型、待办 domain、排序字段、对应的引导流程、以及**完成能力**。

| 场景 | source_id | 任务模型 | 排序字段 | 引导流程 | 完成能力 |
|---|---|---|---|---|---|
| 仓库 | `wh_picking` | `stock.picking` | `scheduled_date, id` | `wh_guided_picking` | `picking_task_complete` |
| 制造 | `mfg_workorder` | `mrp.workorder` | `production_id, sequence` | `mfg_guided_workorder` | `wo_task_complete` |
| 现场服务 | `fs_task` | `project.task` | `priority, date_deadline` | `fs_guided_service` | `service_task_complete` |
| 销售拜访 | `sales_visit` | `crm.lead` | `priority, date_deadline` | `sales_guided_visit` | `visit_task_complete` |

待办判定（domain）：

- **仓库**：`state in (assigned, confirmed)` 且 `user_id = 当前用户`（分配给我的单据）
- **制造**：`state in (pending, ready, progress)`（Community MRP 无逐人派工，以原生状态为队列）
- **现场服务**：`user_ids in 当前用户` 且 `state not in (1_done, 1_canceled)`
- **销售**：`user_id = 当前用户` 且 `active = true`

> 查询**以当前用户身份执行，不做任何 `sudo()` 提权**：domain 看不见的记录本就不该是他的活。

### 10.3 选择规则（决定论）

`TaskQueueEngine.next_task(candidates, only_source=None)` 按以下顺序稳定排序，取第一个：

1. `priority` 数值最小者（紧迫 / 早到期在前）
2. 来源**注册顺序**（同级时保持场景声明的先后）
3. `task_id` 字符串（保证刷新不会来回跳变）

`only_source` 可把选择限定在单一来源（例如"我就想先做拣货"）。

### 10.4 循环状态

| 状态 | 含义 |
|---|---|
| `idle` | 尚未取任务 |
| `task_active` | 有任务正在处理 |
| `awaiting_confirmation` | 完成能力是 L2，**停在人工确认**，游标不前进 |
| `finished_all` | 当前用户/场景没有待办任务（循环正常结束，**不是错误**） |

### 10.5 API

| 路由 | 参数 | 说明 |
|---|---|---|
| `/mmux/task/sources` | `scenario?` | 列出已注册任务源（只读契约） |
| `/mmux/task/next` | `scenario?`, `source_id?`, `res_id?` | 取下一个任务；`res_id` 精确恢复某条记录 |
| `/mmux/task/current` | `loop_key` | 刷新/重载后恢复当前任务 |
| `/mmux/task/guide` | `loop_key` | 为当前任务启动/恢复引导流程 |
| `/mmux/task/help` | `loop_key`, `text` | 任务中求助：返回 `action: "guidance"` 或 `"execute"` |
| `/mmux/task/report` | `loop_key`, `text?`, `intent?`, `params?`, `confirmed?` | 汇报完成 → 标记 → 发布下一个 |

### 10.6 汇报完成的语义（重要）

`/mmux/task/report` 的文本**先经 LLM 归一化**（与全局语义路径一致：无论中英混杂还是纯单语言，
统一为一种纯语言），再校验：

- 解析出的 `capability_id` **必须等于该任务的 `complete_capability`**，否则返回
  `state: "task_mismatch"` —— 汇报不会悄悄变成别的业务动作；
- 无法映射到任何能力 → `state: "not_understood"`（提示换个说法，**不是错误**）；
- LLM 不可用 → `{code: "llm_unavailable", retryable}` 结构化错误，前端按 `retryable` 重试。

随后经 `execute_capability` 执行：

| 执行结果 | 循环行为 |
|---|---|
| `awaiting_confirmation` | 停在 `awaiting_confirmation`，游标与任务**都不动**，等待人工确认 |
| `success` | 任务标记完成，`done_count` 累加，**自动发布下一个任务**（`next_loop_key`） |
| 其它（failed/unknown） | 返回 `report_failed`，游标不动，可重试（幂等键为 `{loop_key}:complete`） |

### 10.7 完成能力（新增，全部 L2）

| capability_id | 场景 | 目标模型 | 执行的原生方法 |
|---|---|---|---|
| `picking_task_complete` | warehouse | `stock.picking` | `button_validate()` |
| `wo_task_complete` | manufacturing | `mrp.workorder` | 写入数量后 `record_production()` |
| `service_task_complete` | field_service | `project.task` | 可选 chatter 摘要 + `write({"state": "1_done"})` |
| `visit_task_complete` | sales_visit | `crm.lead` | 可选 chatter 纪要 + `next_action`；**不动商机阶段** |

> 四个完成能力均为 **L2 且 `require_confirmation=True`**：任务的"完成"通常意味着过账/关单，
> 必须人工确认。刻意**不新增任何业务变更语义**——它们调用的都是与既有能力相同的原生方法。

### 10.8 持久化

循环游标复用交互日志（`mmux.interaction.log.detail` 存 JSON），
`capability_id_name = "taskloop:<scenario>"`，`exec_status = "running"|"success"`，
以 `idempotency_key` 为键、按当前用户隔离。**切换页面或刷新浏览器后同一任务可继续。**

### 10.9 测试覆盖

- `tests_pure/test_taskqueue.py`：注册/去重/场景隔离、优先队列三档排序、`only_source` 过滤、
  空池返回 None、循环状态机、多语言标题解析、跨场景选择
- `mmux_core/tests/test_mmux_taskloop.py`：取任务、优先队列、L2 确认暂停、完成后自动发布下一个、
  循环结束、已完成任务不再重发、`done_count` 累加、能力不匹配拒绝、LLM 不可用错误契约、游标恢复、审计
- `mmux_scenario_*/tests/test_*_taskloop.py`：四个场景各自的端到端闭环

## 11. 智能体自主选任务（Agent Task Selection）

### 11.1 定位

> 相当于**有一个 agent 运行在 Odoo**，它引导/指导用户通过多模态 UX 来执行任务。

第 10 节的循环解决的是"取任务 → 做 → 汇报 → 再取"的**流程**；
但"取任务"这一步原本是**纯 SQL 优先队列排序**——只排序，不判断。
第 11 节补上缺失的**决策层**：让 LLM 看着候选任务列表，自主判断"现在该做哪个"，并说明理由。

| 能力 | v2.1（哑调度器） | v2.2（智能体） |
|---|---|---|
| 取任务 | 纯优先队列排序 | **LLM 自主选择 + 说明理由**，队列作兜底 |
| 引导 | 写死的 TaskFlow + 游标 | 不变（仍是脚本化流程） |
| 汇报 | LLM 语义理解 + 单向校验 | 不变（仍是同一条能力栅栏） |

### 11.2 权限边界（严格受控）

智能体的自主性**只到"选择"为止**：

| 可以自主做 | 绝不能做 |
|---|---|
| 从候选列表中选择一个任务 | 执行任何写操作 |
| 给出选择理由与置信度 | 调用任何 ORM 方法 |
| 列出被排除的候选及其原因 | 指定候选集合之外的记录 |

**任何写操作仍必须走白名单能力 + L2 人工确认**（FR-014/FR-015/FR-016 完全不变）。
智能体选的"完成"，依然要经过 `task_report → execute_capability → ExecutionGuard`，
L2 仍强制人工确认——选任务层**零执行权限**。

### 11.3 LLM 输出契约

```json
{
  "selected_task_id": "wh_picking:42",
  "reason": {"zh_CN": "客户加急，且计划日期已过", "en_US": "priority customer, overdue"},
  "confidence": 0.85,
  "alternatives": [{"task_id": "wh_picking:57", "why_not": "计划日期更晚"}]
}
```

### 11.4 校验规则（安全锚点）

`validate_pick(data, candidates, normalized_lang, only_source)`

**拒绝条件（任一命中即返回无效 AgentPick，绝不猜测、绝不抛异常）：**

1. `data` 不是 dict
2. `data` 顶层命中 `FORBIDDEN_KEYS`（**与 `intent.py` 复用同一份 frozenset**，口径一致）
3. `selected_task_id` 缺失 / 非字符串 / 为空
4. **`selected_task_id` 不在本次候选集合内**（幻觉 / 越权）← **核心不变式**
5. 候选集合为空（返回 `no_candidates`，不是错误）
6. `alternatives[].task_id` 不在候选集合内 → **剔除该条**（不是整体拒绝）

`confidence` 经 `_clamp` 归一到 0..1，不导致拒绝；`reason` 只保留白名单语言键
（`zh_CN/zh/en_US/en/default`），长度截断至 400 字符防审计膨胀。

> 🚨 **最易被"优化"掉的安全边界**：`selected_task_id` 的权威来源**永远是服务端本次候选集合**，
> **绝不是 LLM 文本，更不是前端回传**。一旦有人为省一次 RPC 而把它做成客户端参数直传
> `_select_task`，成员校验立即形同虚设。
> 同理 `alternatives[].task_id` 也必须校验——它是"纯展示"字段，最容易被漏掉，
> LLM 可借此让前端渲染出用户无权看到的记录标识。

### 11.5 降级契约（always-on）

| 失败情形 | `agent.source` | 结果 |
|---|---|---|
| 未配置 provider / 无 api_key | `queue_fallback` | 用纯优先队列选（**结果照常返回**） |
| provider 报错 / 超时 | `queue_fallback` | 同上 |
| 返回非法 JSON | `queue_fallback` | 同上 |
| `selected_task_id` 越出候选集 | `queue_fallback` | 同上（`rejected_reason` 记录幻觉证据） |
| 协议违规（出现禁用键） | `queue_fallback` | 同上 |
| 候选集合本身为空 | `no_candidates` | `finished_all`，属正常循环结束 |

> ⚠️ **与 `parse_intent` 的关键区别**：`parse_intent` 在 LLM 不可用时向**前端返回错误**；
> 而**取任务不能因 LLM 挂了而取不到任务**，所以此处降级为纯队列**直接给结果**。
> 降级与自主共享**同一份候选集合**，兜底永不选出候选外的记录。

### 11.6 API 响应增量（向后兼容）

`/mmux/task/next`、`/mmux/task/current`、`/mmux/task/report` 的响应**追加** `agent` 键：

```json
"agent": {
  "task_id": "wh_picking:42",
  "source": "llm",                    // "llm" | "queue_fallback" | "no_candidates"
  "reason": "客户加急，且计划日期已过",
  "reason_i18n": {"zh_CN": "...", "en_US": "..."},
  "confidence": 0.85,
  "alternatives": [{"task_id": "wh_picking:57", "why_not": "计划日期更晚"}],
  "rejected_reason": null             // 降级时的证据（如 task_not_in_candidates:999）
}
```

**原有的 `task`/`loop`/`loop_key`/`loop_label`/`sources` 五个键完全不变**，
旧读取方（含既有测试与前端）不受影响；`agent` 是纯增量。
`res_id` 显式恢复某一记录时**不咨询智能体**（用户已指明任务，无需决策）。

### 11.7 数据最小化

送进提示词的候选字段**仅**：
`task_id / source_id / scenario / title / priority / due / kind`。
**绝不送**：完整 name/description、成本价、供应商、内部备注、chatter 内容。
未被展示的信息，就无法泄漏进提示词、日志或 UI。

### 11.8 审计

复用 `mmux.interaction.log`（**不新增模型、不改 schema**），
`capability_id_name = "agent_pick:<scenario>"`：

| 字段 | 说明 |
|---|---|
| `source` | 自主决策 vs 队列兜底 —— 事后评估 agent 质量的关键 |
| `rejected_reason` | LLM 幻觉 id / 协议违规证据，**即使降级成功也留痕** |
| `reason` | 存**原始多语言字典**（不 resolve），证据完整 |
| `candidate_count` | 记规模而非明细，避免日志膨胀与 PII 泄漏 |

被拒的决策 `exec_status = "failed"`，被接受的 `= "success"`。

### 11.9 前端决策条

任务卡上方新增「智能体建议」决策条：

- 显示 `reason`（并一并 **TTS 播报**，便于现场免手操作）；
- `source !== "llm"` 时显示**「队列兜底」徽章**（透明性：让用户分清自主判断 vs 降级）；
- 「其他候选」可折叠，展示 `alternatives` 的 `why_not`。

### 11.10 测试覆盖

- `tests_pure/test_agent.py`（38 条）：有效选择 / 幻觉 id 拒绝 / **遍历 `FORBIDDEN_KEYS` 逐键验证** /
  非 dict 与 null 拒绝 / 空候选 / `only_source` 限制 / alternatives 越界剔除与上限 /
  confidence clamp / reason 多语言与未知键剥离 / **兜底等价性**（`fallback_pick` == `TASK_ENGINE.next_task`）/
  `never_raises_on_hostile_input` 容错
- `mmux_core/tests/test_mmux_taskloop_agent.py`（19 条）：自主选中被采纳、理由本地化、
  幻觉/超时/无 provider/非法 JSON/协议违规**全部降级仍能取到任务**、
  **智能体不削弱 L2 确认**、**不能指名候选外（他人）的任务**、决策审计（含被拒决策）、
  理由跨刷新不丢、原响应键不变、`use_agent=False` 强制队列、端到端自主选择→完成→发布下一个、
  `_serialize_candidates` 数据最小化

## 12. 场景上下文识别（Scenario Context，v3.1）

### 12.1 定位

> agent 需要能**识别场景上下文**——通过 web client UX 所在的**位置/定位**来确定。

第 11 节让 agent 会**选任务**，但"我现在身处哪个作业现场"这件事此前是**调用方告知的**：
`/mmux/agent/start` 的 `loop.scenario` 若不给，服务端就无从判断。
第 12 节补上**位置感知层**：把"用户此刻在哪个界面"变成一条**确定性的**场景推断。

三个信号按**位置可靠度**排序采集：

| 信号 | 含义 | 可靠度 |
|---|---|---|
| `action_xmlid` | 当前打开的 action（如 `stock.action_picking_tree`） | 最高 |
| `res_model` / `res_id` | 当前记录所属模型 | 高 |
| `menu_xmlid` | 当前菜单项 | 中 |
| `geo` | 浏览器定位（经纬度） | **本期不参与判定** |

### 12.2 核心不变式：场景就是安全边界

**场景不是"给模型的一句提示"，而是"派发权限的那个开关"。**
`_agent_allowed_capabilities(scenario)` 被**同一份数据**用了两次：

1. **发给模型**的能力清单（它能看见什么）；
2. **校验模型答案**时用的白名单（它被允许说什么）。

因此——**场景绝不能让模型来选**。否则模型只要回一个"我现在在仓库"，
就能把自己看见的能力清单换成仓库的能力清单，**提权不需要攻破任何校验**。

由此确立本节的**两条硬边界**：

| 谁 | 能做什么 | 绝不能做什么 |
|---|---|---|
| **服务端** | 由位置信号**确定性**推导候选场景集合 | —— |
| **LLM** | 在**候选集合之内**消歧、给理由 | **指定候选集合之外的场景** |

> 🚨 **最易被"优化"掉的安全边界**：如果推导出的候选集合**只有一个**场景，
> 就直接确定，**不咨询 LLM**——模型在只有一个正确答案时没有消歧价值，
> 却多了一个出错机会。只有**真歧义**（≥2 个等权候选）才交给 LLM。

### 12.3 确定性打分（加法，不是平均）

`resolve_scenario(signal, known, geo_weight=0)`

```python
WEIGHT_ACTION_XMLID = 100     # 最强：action 基本等价于"用户点进了这个作业界面"
WEIGHT_RES_MODEL    = 60
WEIGHT_MENU_XMLID   = 30
WEIGHT_GEO          = 0       # 本期恒为 0
MIN_DECISIVE_SCORE  = WEIGHT_RES_MODEL
```

**加法而非平均**：两个**独立**信号指向同一场景，证据**确实更强**（action 说是仓库、
模型也是 `stock.move`，比只有其一更可信）；平均会把"双重确认"稀释成"单信号水平"。

**`MIN_DECISIVE_SCORE = 60` 的含义**：一条 `menu_xmlid`（30）**单独不够**——
菜单只是"用户在这个模块里"，而不是"用户在做这个作业"，**不足以单独定场景**。

**并列时按场景 code 排序**：候选列表在**跨请求之间稳定**。
同一个位置刷新两次得到同样的候选顺序，否则操作员会看到选项顺序跳变。

### 12.4 歧义必须提问（`ask_user`）

当最高分**并列**（`SOURCE_AMBIGUOUS`）或**无任何信号**（`SOURCE_NONE`）时，
结果是 `needs_asking = True`，走 agent 循环**既有的**提问通道（不新增机制）。

> ⚠️ **无信号 ≠ 静默用全局**。这是本节最容易被写错的一行代码。
> "不知道在哪"的正确反应是**问**，而不是"当作哪个场景都能用"——
> 后者会把场景白名单**悄悄放宽成全集**，等于**取消了场景边界**。

歧义之所以在数学上**可达**，是因为多值模型映射的存在：

```python
AMBIGUOUS_RES_MODEL_MAP = {
    "stock.move":      ("warehouse", "manufacturing"),  # 两边都用
    "stock.move.line": ("warehouse", "manufacturing"),
    "project.task":    ("field_service", "sales_visit"),
    "project.project": ("field_service", "sales_visit"),
}
```

每个场景**以相同权重**各得一条 hint → 最高分并列 → 必须提问。
（若只有单值映射且权重各不相同，并列就是**死代码**，永远走不到。）

**`RES_MODEL_MAP` 与 `AMBIGUOUS_RES_MODEL_MAP` 必须不相交**——
重叠会让结果取决于分支顺序，这是最难复现的一类 bug。

**映射表刻意稀疏**：`res.partner` / `res.users` / `product.product` **故意不收录**——
它们会出现在**每个场景**的界面上，收录只会产生"信心十足的胡说"。

### 12.5 候选集必须"可回答"

提问的选项是**从候选集生成的**。因此无信号时若候选集为空，
操作员会被问"你在哪个场景？"而**看到一个空列表**——那不是提问，是死路。

`unresolved_with_all(resolution, all_scenarios)` 用**真实存在**的场景补满候选列表，
但**绝不代为选择**：`scenario` 保持 `None`，**在人工回答之前不推导任何白名单**。

> 把提问**放宽**，不等于把权限**放宽**。这两件事在代码里只差一行，
> 在后果上差一个安全边界。

### 12.6 LLM 消歧的输入契约

```json
{
  "scenario": "warehouse",
  "reason": {"zh_CN": "当前是拣货单，属于仓库作业"},
  "confidence": 0.85
}
```

**键集合是封闭的**：`CHOICE_KEYS = {"scenario", "reason", "confidence"}`。
出现**任何一个未声明键**，**整份答案作废**（不是忽略该键）。

`FORBIDDEN_KEYS` **复用 `intent.py` 的同一份 frozenset**，且**优先检查**——
这样 `{"scenario": "warehouse", "model": "..."}` 报的是 `protocol_violation`（**协议违规**）
而不是 `undeclared_key`（**拼写问题**）。前者是攻击特征，后者是笔误，**证据性质不同**。

### 12.7 回答的解析：先精确，再词边界

`choice_from_answer(text, allowed)`

1. **先整体精确匹配**（`text == code.lower()`）→ 命中即返回，`confidence = 1.0`；
2. 否则用 `re.search(r"(?<!\w)<code>(?!\w)")` **词边界**匹配 → 唯一命中时 `confidence = 0.8`；
3. 命中 **≥2 个** → `ambiguous_answer`（拒绝）；
4. 一个都没有 → `answer_not_in_candidates`（拒绝）。

**空白裁剪，大小写不裁剪**：`" warehouse "` 接受；`"WAREHOUSE"` / `"warehous"` 拒绝。

> 🚨 **这里曾有一个真实 bug**：早先版本用**裸子串包含**判断，
> 于是 `"sales_visit_typo"` 因为**包含** `"sales_visit"` 而**被判为合法**——
> 一个拼错的名字被**悄悄"修正"**成了另一个场景。
> **精确优先**正是为了堵住这个口子：短码 `"sales"` 不会被更长的候选 `"sales_visit"` 抢走。

### 12.8 已声明场景优先（stated beats derived）

若 `loop.scenario` **显式给出**，它就是**权威**：位置信号**只做审计探测**，
**绝不覆盖**操作员的声明。

此时 `scenario_context` 会**同时**呈现两者：

```json
{
  "scenario": "manufacturing",
  "source": "user",
  "stated_scenario": "manufacturing",
  "position_scenario": "warehouse",
  "position_mismatch": true
}
```

**分歧要显示在界面上**（不只是写进审计）：操作员可能**拿着设备走错了车间**，
也可能**场景声明填错了**——这两种情况都需要**当场**被看到。

> **不阻塞作业**：已声明场景时，探测结果**不构成拦截**，`state ≠ awaiting_user`。
> 有一份可信声明在手时，"位置看起来不太对"是**提示**，不是**关卡**。

### 12.9 GPS：只采集，不判定

`WEIGHT_GEO = 0`，本期**恒定**。定位**记录在案**（`geo_recorded = True`），
**但不影响任何判定结论**。

**坐标默认脱敏**：`ContextSignal.to_dict(include_geo_coords=False)` 返回 `bool(self.geo)`
而非经纬度数值。`to_dict()` 同时喂给**交互日志**和**客户端视图**，
因此坐标**两处都不外泄**，只有显式 `include_geo_coords=True` 才输出数值。

**采集默认关闭**：所有出厂场景 `geo_collect = False`、`geo_weight = 0`。
未启用时，浏览器**根本不会被请求定位权限**。

**权重与采集强联动**（`@api.constrains`）：

```python
if rec.geo_weight and not rec.geo_collect:
    raise ValidationError(...)
```

> 配了权重却不开采集，是**一个永远不生效的配置**——
> 比报错更糟，因为**没人会注意到**。

预留了 `geo_weight=...` 参数，使该能力可**纯配置启用**而无需改代码，
但它**永远不是默认值**。

### 12.10 向后兼容：`None` ≠ `{}`

| 传入 | 语义 | 行为 |
|---|---|---|
| **不传 / `None`** | "**旧客户端，本项不适用**" | 走 v3.0 原路径，**逐字节一致**，不提问 |
| **`{}`** | "客户端**尝试过**，但**什么都没有**" | **提问** |

**混同这两者是本节最危险的写法**：一旦把"不适用"也当成"查无所获"，
**每一个现存客户端都会突然开始要求操作员选场景**。

前端因此**省略** `signal` 键，而**不是**用 `|| {}` 兜底（见 `mmux_api.js`）；
`context_service.js` 的 `getSignal()` 在无可上报内容时返回 **`null`**（不是 `{}`）。

`mmux_context` 服务缺失或抛错时，`_currentSignal()` **降级为 `null`**（= 不适用），**作业不中断**。

### 12.11 审计

复用 `mmux.interaction.log`（**不新增模型、不改 schema**）：

| 事件 | `exec_status` | 说明 |
|---|---|---|
| 判定成功 | `success` | `detail` 记录命中信号与得分 |
| **提问（未能确定）** | `failed` | `detail.candidates` 非空——"**没能确定**"本身就是**要能被统计**的事件 |
| 声明 vs 位置分歧 | 仍写入 | 探测记录**不被抹掉** |

审计中的 `geo` 为**布尔**，坐标数值**从不落库**。

### 12.12 测试覆盖

- `tests_pure/test_scenario.py`：确定性判定 / 权重与阈值 / 并列排序稳定 /
  多选题可达 / `RES_MODEL_MAP` 与 `AMBIGUOUS_RES_MODEL_MAP` 不相交 /
  回答解析（精确优先、词边界、拼写不修正、**以合法码开头的错字被拒**）/
  键集合封闭 / 坐标脱敏不对称性
- `mmux_core/tests/test_mmux_scenario_context.py`（3 类 34 条）：
  判定与白名单收窄 / 跨轮次持久 / 歧义提问 / 选项**可回答** / 越界拒绝 /
  已声明优先 / 分歧**可见于视图** / 探测不阻塞 / GPS 记录但不判定 /
  **坐标既不入视图也不入审计** / v3.0 路径保持 / LLM 消歧（界内接受、界外拒绝）
- 验收用例：`docs/ACCEPTANCE.md` §10.9（A25–A70）

---

## 13. 运行时配置化（v3.4 / 19.0.6.0.0）

本节回答一个明确的需求：**只要不是硬编码的、可以通过配置调整，是可以
被接受的**。v19.0.6 把三处原先只能改代码的参数搬进了配置面。动作集
（`AGENT_ACTIONS`）**保持代码封闭**——它是安全模型的根基，配置化等于让
管理员能扩大模型权限，不属于「可接受的配置」。

### 13.1 Agent 预算（设置页）

| 设置项 | 参数键 | 范围 | 默认（留空时） |
|---|---|---|---|
| 每循环最大推理轮数 | `mmux.agent.max_turns` | 1–50 | 6 |
| 每循环最大能力提议数 | `mmux.agent.max_capability_calls` | 1–50 | 3 |
| 同一步最大重复次数 | `mmux.agent.max_repeated_step` | 1–20 | 2 |

行为要点：

- **超限即安全终止**：预算由服务端强制执行，从不依赖模型自觉。
- **非法值回退默认**：设置页会拒绝越界值（`ValidationError`）；
  服务层对读到的非法值（负数/0/非数字）静默回退模块默认——
  「读不到」永远不等于「无限制」。
- **进行中会话**：预算不持久化在会话记忆里，每回合重新注入。因此
  **中途调小预算会立即终止已超限的会话**——这是安全方向，设置页
  help 文案有说明。
- 模型在提示词里能看到自己的预算（`turns_left` / `capability_calls_left`），
  因此预算调整对模型是可见的、可规划的。

### 13.2 能力开关（`mmux.capability.toggle`）

配置菜单 → **Capability Switches**。每行 = 一个能力的开/关覆盖：

- **仅管理员组**（`group_mmux_manager`）可写：ACL 之外，模型层还有
  `user_has_groups` 第二道校验——ACL 配错也打不开这个面。
- **每次切换写审计**：`mmux.interaction.log` 一行，含谁、哪个能力、
  开/关、以及能力的风险等级（L3 开启显式标注）。
- **能力 ID 必须是代码注册过的**：这张表只能开关既有能力，**永远不能
  扩大白名单**——白名单仍由代码定义。
- **生效时机**：服务层在两个热读点（能力 JSON、agent 白名单）把 DB
  开关同步进内存注册表。多 worker 各自内存，下一次请求各自同步
  （最终一致，秒级窗口）。**开→关的延迟不构成安全弱化**：L2 确认门槛
  独立于开关；关→开的延迟只影响可用性。
- 删除开关行 = 不覆盖 = 回到代码注册的默认状态。

### 13.3 流程覆盖（`mmux.flow.override`）

配置菜单 → **Flow Overrides**。`flow_id` 与代码注册流程相同的行
**优先**；删掉该行，代码流程原样回归：

- **字段**：`flow_id`（唯一）、`name` / `description`（纯文本或
  `{lang: text}`，与正文多语言机制一致）、`voice_on`、`steps`
  （JSON 步骤列表）。
- **步骤校验**（保存时强制）：必含 `step_id/title/voice_prompt`；
  `input_type` 限 `scan/voice_qty/confirm/capability`；`step_id` 去重；
  `capability_id` 若非空必须在白名单内且当前已启用。
- **覆盖不能扩权**：步骤引用的能力仍走同一 `ExecutionGuard`
  （白名单、记录规则、L2 确认全部照旧）。覆盖改变的是**让操作员做什么**，
  不是**系统可以做什么**。
- **覆盖是静态步骤，不是构造器**：需要跟随实时记录（如按 `move_line_ids`
  动态生成步骤）的流程仍应在代码里实现。DB 覆盖适合改措辞、调顺序、
  加确认/扫码步骤。
- **会话中途稳定**：agent 会话把菜谱快照进会话记忆（一次），因此
  **编辑覆盖只影响新会话**；进行中的会话按它开始时的脚本走完——
  这是刻意设计（操作员听到的步骤不能中途变卦）。
- 普通用户组对该表**只读**（跟随覆盖流程=跟着步骤走，无任何提权）。

### 13.4 什么仍然只能改代码（刻意）

| 项 | 为什么不能配置化 |
|---|---|
| 动作集 `AGENT_ACTIONS` 及各动作的负载键 | 模型能说什么由代码封闭；配置化 = 管理员可扩大模型权限，破坏整个安全模型 |
| 能力白名单本身（能力定义/schema/执行器） | 代码定义 = 可审计、可评审；DB 只能开关，不能发明 |
| 场景模块的四个代码流程 | 构造器读实时记录，DB 行表达不了；覆盖表已提供不改代码的替代路径 |

### 13.5 测试与验证

- `tests_pure/test_agentloop_run.py::TestConfigurableBudgets`（6 条）：
  三个预算的注入生效、非法值回退、预算不入持久化记忆、提示词可见性。
  **缺陷注入自验**：预算改回常量 → 对应测试必红（已验证）。
- `mmux_core/tests/test_mmux_capability_toggle.py`（6 条，需真机）：
  非 manager 拒绝、审计行、注册表同步生效、无陈旧缓存、未知 id 拒绝。
- `mmux_core/tests/test_mmux_flow_override.py`（8 条，需真机）：
  覆盖优先、无覆盖回归、非法步骤拒绝、多语言、会话中途稳定。
- 沙箱 harness `verify_config_layer.py`（34 项，无需 Odoo）：预算注入
  （真纯核心）、开关同步语义、覆盖优先级（含**源码级**守卫 C11——
  `_flow_for` 的 DB 行查找必须先于 `FLOW_ENGINE.get`，注入验证过）、
  设置约束语义。此 harness 还**抓到过一个真实缺陷**：预算约束最初用
  `if not value` 判空，会把 `0` 当「未设置」放行——0 预算会让 agent
  完全不可用，必须报错拒绝。已修复并固化。
