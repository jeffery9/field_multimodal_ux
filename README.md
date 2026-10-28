# Odoo Field Multimodal UX（社区版）

面向现场业务作业的 **Odoo 原生多模态交互增强平台**：语音、图像、条码与自然语言输入 → 受控的 Odoo 业务操作。

> 实现基线：《Odoo Field Multimodal UX》PRD V1.0。本仓库覆盖 PRD 阶段一（核心交互闭环）与阶段二的关键能力（视觉服务适配、证据采集），以及仓库、制造、销售拜访、现场服务四个场景适配器。

## 核心使用场景：任务循环（Task Loop）+ 智能体（Agent）

平台的主线工作流是**"取任务 → 执行 → 求助 → 汇报 → 系统发布下一个任务"**的闭环，
而"取任务"这一步由**运行在 Odoo 内的智能体**负责决策：

```
       ┌──────────────────────────────────────────────────────┐
       │                                                      │
       ▼                                                      │
┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌──────────────┐
│ ① 获取任务   │──▶│ ② 执行任务   │──▶│ ③ 汇报完成   │──▶│ ④ 标记完成    │
│ task/next   │   │ task/guide  │   │ task/report │   │ 并发布下一个  │
│ 智能体自主选 │   │ 引导+求助    │   │ 语音/文本    │   │ next_task    │
└─────────────┘   └─────────────┘   └─────────────┘   └──────┬───────┘
                        ▲                   ▲                │
                        │                   │                │
                  task/help（求助）    L2 需人工确认           │
                  「这个库位在哪」     （awaiting_confirmation）│
                                                             │
                        无待办任务 ──▶ finished_all ◀─────────┘
```

- **智能体自主选任务（v2.2 新增）**：后端把**当前用户有权看到的候选任务**（含优先级、截止日期、
  来源）交给 LLM，由它判断"现在该做哪个"并说明理由（`reason` / `confidence` / `alternatives`）。
  LLM **只能从给定 id 中选**——`selected_task_id` 必须落在本次服务端候选集合内，
  否则判为幻觉/越权并拒绝。**队列排序始终作为兜底**：LLM 未配置、超时、返回非法 JSON 或被拒时，
  系统自动改走纯优先队列，**取任务永不因为模型不可用而失败**。
- **权限严格受控**：智能体的自主性**只到"选择"为止**——它没有任何执行权限。
  任何写操作仍必须走白名单能力 + L2 人工确认（FR-014/015/016 完全不变）。
- **服务端驱动**：游标持久化在交互日志中，**切换页面或刷新后继续同一任务**（含智能体理由）。
- **会话即任务（v3.5）**：一次**任务 = 一个对话 session**。session 是推理状态的边界——目标、
  世界状态、行动轨迹、待确认项都只活在本任务内；任务完成或被放弃，session 即终结。
  下一个任务是**全新的 session**，不继承上一个任务的任何推理记忆（跨任务只保留经审批的
  用户级偏好，如技能等级）。详见 [docs/CONFIG.md §14](docs/CONFIG.md)。
- **执行与求助同源**：`task/guide` 把当前任务绑定到对应引导流程（`wh_guided_picking` /
  `mfg_guided_workorder` / `fs_guided_service` / `sales_guided_visit`），`task/help` 用于任务中提问——
  提问可能被解析为**引导提示**，也可能解析为**白名单能力调用**，但绝不会变成"静默的业务写入"。
- **汇报必须匹配任务的完成能力**：汇报文本经 LLM 归一化为纯语言后，解析出的 `capability_id`
  必须等于该任务的 `complete_capability`，否则拒绝（`task_mismatch`）——汇报不会悄悄变成别的业务动作。
- **L2 完成动作仍需人工确认**：`task/report` 走的是同一个 `execute_capability` 漏斗，
  风险分级、幂等、审计全部保留；未确认时循环停在 `awaiting_confirmation`，游标不前进。
- **任务来源按场景约定**：仓库=`stock.picking`（分配给我的待办单据）、制造=`mrp.workorder`
  （待执行工序）、现场服务=`project.task`（分配给我的未关闭任务）、销售=`crm.lead`（我负责的商机）。

该层由两个纯 Python 模块构成（可脱离 Odoo 运行时单测）：
`mmux_core/services/mmux_core/taskqueue.py`（`TaskSource` / `Task` / `TaskQueueEngine`，决定论排序 + 状态机）
与 `mmux_core/services/mmux_core/agent.py`（`AgentPick` / `validate_pick` / `fallback_pick`，LLM 决策校验）。
实际 ORM 查询由 Odoo 层以**当前用户身份**执行，不做任何提权。

## 架构（对应 PRD §8.3/§8.4）

```
┌────────────────────────────────────────────────────────┐
│  Web Client (OWL 原生扩展)                              │
│  systray 现场助手 / 现场工作区 client action            │
│  【任务循环】取任务 → 引导/求助 → 汇报 → 下一个任务      │
│  语音·扫码·文本输入 → 意图卡片 → 风险确认 → 结果反馈     │
└───────────────────────┬────────────────────────────────┘
                        │  /mmux/* JSON API（当前用户身份）
┌───────────────────────▼────────────────────────────────┐
│  mmux_core                                             │
│  · Provider Registry（asr/llm/vision 可插拔）           │
│  · Capability Registry（能力白名单，FR-014/015）         │
│  · Intent Parser（LLM 唯一语义路径 + 英文归一化）        │
│  · Execution Guard（FR-016 十步链路 + 幂等 + 审计）      │
│  · Task Queue（任务循环：取任务/完成/发布下一个）        │
│  · Agent Pick（智能体自主选任务 + 队列兜底）             │
│  · Agent Loop（推理型循环：决策归模型、执行权留服务端）  │
│  · Task Session（会话即任务：一任务一 session）          │
│  · TaskFlow（语音引导 + 新手/熟手双模式）                │
│  · Plan Orchestrator（受约束多步编排）                   │
└──────┬──────────────────────┬──────────────────────────┘
       │                      │
┌──────▼───────────┐  ┌───────▼──────────────────────────┐
│ mmux_provider_ai │  │ 场景适配器（能力+引导流+任务源）    │
│ OpenAI 兼容 + Mock│  │ mmux_scenario_warehouse          │
│                  │  │ mmux_scenario_mfg                │
│                  │  │ mmux_scenario_sales              │
│                  │  │ mmux_scenario_fieldservice       │
└──────────────────┘  └───────┬──────────────────────────┘
                              │ 只调用既有业务方法
                ┌─────────────▼──────────────┐
                │ Odoo 原生模块（stock/mrp/...）│
                │ = 业务事实唯一权威来源        │
                └────────────────────────────┘
```

### 服务层文件划分（v3.1.1）

`mmux.service` 是全部业务行为的唯一入口，但**不是**一个 2300 行的单文件。
它按真实耦合边界拆成 7 个文件，每个都 `_inherit = "mmux.service"`，
运行时由 Odoo 合并成**同一个 model** —— 对外 API 零变化：

```
mmux_core/models/
  mmux_service.py              门面：只声明 model，无逻辑（27 行）
  mmux_service_infra.py        provider / 执行守卫 / 幂等 / LLM 出参抽取
  mmux_service_skill.py        语言解析 + 技能等级
  mmux_service_capability.py   权限校验 + 能力执行（唯一收口）
  mmux_service_scenario.py     场景判定 + 审计落库
  mmux_service_loop.py         agent 循环：scratchpad / observe / step / turn
  mmux_service_flow.py         任务与脚本化引导
  mmux_service_plan.py         计划提议与执行
```

切割线来自**方法级调用依赖图**，不是按行数平均分。新增行为请放进它所属的簇，
让门面文件保持"目录"的作用。

## 模块清单

| 模块 | 内容 |
|---|---|
| `mmux_core` | 内核：输入协议、能力/Provider 注册表、意图解析、执行守卫、**任务循环（Task Queue）**、**智能体自主选任务（Agent Pick）**、**场景上下文识别（Scenario Context）**、**计划编排（Plan）**、**语音引导（TaskFlow，含新手/熟手双模式）**、**多语言与多语言混合**、**设置页（res.config.settings 封装）**、审计日志、场景配置、OWL 前端、`/mmux/*` API |
| `mmux_provider_ai` | OpenAI 兼容 ASR/LLM/视觉适配器（配置经设置页，无需碰系统参数），复用核心内置 Mock |
| `mmux_scenario_warehouse` | 仓库场景：库存查询、数量录入、差异证据、单据验证、盘点过账；**语音引导拣货流**；**任务源 = 待办拣货单** |
| `mmux_scenario_mfg` | 制造场景：工序查询、语音报工、设备报修；**语音引导工单作业流**；**任务源 = 待执行工序** |
| `mmux_scenario_sales` | 销售拜访：口述拜访纪要（草稿）、客户/商机查询、后续跟进任务、报价草稿（强制确认）；**语音引导拜访流**；**任务源 = 我负责的商机** |
| `mmux_scenario_fieldservice` | 现场服务：设备识别（候选列表）、维修取证、服务报告草稿、备件记录、任务完工（强制确认）；**语音引导服务流**；**任务源 = 分配给我的服务任务** |

## 配置：一个设置页，不用碰系统参数（v3.1.1）

> 管理员的配置入口是 *Field Assistant → Configuration → Settings*，**不是**
> 「设置 → 技术 → 系统参数」。后者是开发者界面：没有标签、没有校验、没有默认值，
> 也无法让人发现"到底有什么可配"。

设置页覆盖 provider 选择、端点、密钥、模型、超时、语言与智能体开关。三个设计要点：

- **键名一个都没变**。字段用 `config_parameter="mmux.llm.provider"` 之类的属性
  直接指向**运行时读的同一把键**，所以升级**无需迁移**（值从未搬家），
  也不可能出现"设置页改了但运行时没生效"（只有一份值）。
- **API Key 是只写字段**。保存后**永不回显**——页面源代码与网络响应里都不含明文，
  只有旁边一个「A key is stored」标记。**留空 = 不修改**，因此打开设置页直接保存
  不会清空已配置的密钥。
- **错误在保存时报**。非法 provider 名、越界超时都在设置页拦下，
  而不是等到现场作业时才变成 `ProviderError`。

> 🚨 **密钥为什么不能像别的字段那样声明 `config_parameter`**：
> `default_get()` 会**遍历每个带该属性的字段并用参数值填充**，且发生在合并
> `get_values()` **之前**。因此"在 `get_values()` 里 pop 掉密钥"是**不够的**——
> 框架已经把明文放进 payload 了。让密钥**根本不进入**框架的 config 分类，
> 才真正把它挡在表单之外。

> **测试后门不做成功能**：`mmux.llm.mock_response` / `mock_error` / `asr.mock_transcript`
> 是测试注入通道（前者更是"提供 provider 响应体"的入口），**刻意没有界面字段**，
> 但它们仍然有效，供自动化测试使用。

详见 [docs/CONFIG.md §3.1](docs/CONFIG.md)；安装步骤见 [docs/INSTALL.md §3](docs/INSTALL.md)。


## 安全模型（PRD §6）

- **无任意 ORM**：LLM 只能输出 `capability_id + params`，服务端 schema 严格校验，未知字段直接拒绝；
  额外扫描顶层黑名单键（`model`/`method`/`create`/`unlink`/…），出现即判协议违规并拒绝。
- **十步执行链**：schema → 白名单 → ACL/Record Rules → 前置状态 → 风险确认 → 幂等 → 调用原生方法 → 结果验证 → 审计。
- **风险分级**：L0 查询直通；L2 强制确认；L3 默认禁用（需管理员显式开启）。
- **幂等**：客户端幂等键 + 服务端日志唯一拦截，重复提交、超时未知状态均不会重复产生业务副作用。
- **结果状态机**：`recognized / validated / awaiting_confirmation / submitted / success / failed / unknown` 严格区分，超时返回 `unknown` 并要求核实，绝不伪装成功。
- **归一化文本不进执行链**：LLM 产出的英文归一化文本仅用于审计留痕与抽参语言上下文，执行入口始终只有 `capability_id + params`。

## 场景上下文识别：由位置定位场景（v3.1）

> agent 需要能**识别场景上下文**——通过 web client UX 所在的**位置/定位**来确定。

agent 要"选任务"，先得知道**自己身处哪个作业现场**。v3.1 把这件事从"调用方告知"
变成一条**服务端确定性推导**，位置信号按可靠度排序采集：

```
当前界面 (action_xmlid / res_model / menu_xmlid / geo)
        │
        ▼  服务端确定性打分（加法，非平均）
   候选场景集合 ──唯一？──▶ 直接确定（不咨询 LLM）
        │
        └─真歧义 / 无信号──▶ ask_user 提问（选项 = 服务端真实场景）
                                   │
                              LLM 只能在候选集内消歧
```

**核心不变式：场景就是安全边界，所以不能由模型来选。**
`_agent_allowed_capabilities(scenario)` 被**同一份数据**用了两次——既决定**发给模型**的
能力清单，又作为**校验模型答案**的白名单。若场景可由模型指定，模型只要回一句
"我现在在仓库"，就能把自己看见的能力清单换成仓库的——**提权连一条校验都不用攻破**。
因此分工被钉死：**服务端推导候选集，LLM 只在候选集之内消歧**，越界即拒。

- **确定性打分**：`action_xmlid`(100) > `res_model`(60) > `menu_xmlid`(30)，`geo`(0)。
  **加法而非平均**——两个独立信号指向同一场景是**更强**的证据，平均会把它稀释。
  单独一条菜单命中(30) **低于** `MIN_DECISIVE_SCORE`(60)，不足以定场景。
  并列时按场景 code 排序，候选列表**跨请求稳定**。
- **歧义可达且必须提问**：多值模型（`stock.move` → 仓库/制造；`project.task` → 现场服务/销售）
  给**每个场景等权一条** hint → 并列 → `ask_user`。无信号同样提问，
  **绝不静默放宽为全局**——那等于取消场景边界。
- **提问必须可回答**：候选集会被补满**真实存在**的场景，但**不代为选择**——
  `scenario` 保持空，**在人工回答前不推导任何白名单**。
- **已声明场景优先**：显式 `loop.scenario` 是权威，位置**只做审计探测**、**不覆盖**；
  两者分歧作为 `position_mismatch` **显示在界面上**（操作员可能走错了车间），
  但**不阻塞**作业。
- **GPS 只采集、不判定**：`WEIGHT_GEO = 0`，坐标**默认脱敏为布尔**（视图与审计都不含数值），
  采集**默认关闭**；"设了权重却没开采集"直接**校验报错**。
- **旧客户端零回归**：`signal` **不传**（`None`）= "本项不适用"，走 v3.0 原路径、**不提问**；
  传 **`{}`** = "查过但什么都没有"，才提问。**混同这两个值会让每个现存客户端突然开始选场景。**

详见 [docs/CONFIG.md §12](docs/CONFIG.md)；验收见 [docs/ACCEPTANCE.md §10.9](docs/ACCEPTANCE.md)。

## 语义识别：LLM 唯一路径 + 英文归一化

现场口语天然混杂语言（*"帮我看看 stock 够不够"*、*"把 MMUX-0001 的 stock 数量改成 5 个"*）。
无论输入是**多语言混杂**还是**纯单语言**，一律走同一条路径：

```
任意输入 → ① LLM 语义识别 → ② 归一化为纯英文（固定 en_US）→ ③ Odoo 按 capability_id + params 执行
```

- **LLM 是唯一语义路径**：没有关键词/规则解析器，不存在"规则优先、LLM 兜底"双路径。
- **归一化目标语言固定英文**：LLM 同一次回答里既完成语义识别，又输出整句英文改写
  （`normalized_text`）；业务实体（型号 `MMUX-0001`、数量、批次、单据号）**逐字保留，不翻译**。
- **不匹配 ≠ 错误**：LLM 正常回答但无法映射到任何能力 → 返回**空意图**（提示"换个说法"）。
- **LLM 不可用 = 结构化错误**：未配置 / 超时 / 非法 JSON → 返回 `{code:"llm_unavailable", retryable}`，
  **绝不回退规则解析**。业务执行与原生 Odoo 流程不受影响。
- **数量/条码一致性护栏**：归一化后用确定性提取器比对 LLM 的 `params`，型号编码里的数字
  绝不会被当成数量；不一致时写入 `ambiguous` 而非静默采信。
- **输出本地化**：引导提示、能力名称按用户语言（含回退链，末位恒为 `en_US`）解析；TTS 随语言选发音。

> ⚠️ 权衡：无 LLM provider 时 `/mmux/parse_intent` 不可用（离线解析能力被有意放弃）。
> 可用 Mock provider 做确定性测试，生产建议配置私有端点（LocalAI / vLLM）。

详见 [docs/CONFIG.md §9](docs/CONFIG.md)。

## HTTP API 一览

| 分组 | 路由 | 说明 |
|---|---|---|
| 能力/输入 | `/mmux/capabilities`、`/mmux/transcribe`、`/mmux/vision` | 能力白名单、语音转写、图像识别 |
| 语义 | `/mmux/parse_intent` | LLM 唯一语义路径（归一化为英文） |
| 执行 | `/mmux/resolve`、`/mmux/execute` | 条码解析、单一执行漏斗（十步守卫） |
| 计划 | `/mmux/plan/propose`、`/mmux/plan/run` | 受约束多步编排 |
| 引导 | `/mmux/guide/flows`、`/start`、`/current`、`/advance`、`/skill_level` | 语音引导 + 新手/熟手 |
| **任务循环** | `/mmux/task/sources`、`/next`、`/current` | 任务源契约、取下一个任务（**含智能体决策**）、恢复游标 |
| **任务循环** | `/mmux/task/guide`、`/help`、`/report` | 绑定引导、任务中求助、汇报完成并发布下一个 |
| **场景上下文** | `/mmux/agent/start`（`signal`） | 位置信号入参；响应追加 `scenario_context` |
| **智能体循环** | `/mmux/agent/start`、`/mmux/agent/turn` | 推理型循环：一个任务 = 一个 session（`loop_key`），逐回合 observe→decide→validate→act |

> `/mmux/task/next`、`/current`、`/report` 的响应新增 `agent` 键
> （`source` ∈ `llm` / `queue_fallback` / `no_candidates`，附 `reason`、`confidence`、`alternatives`），
> **原有键完全不变**，向后兼容。
>
> `/mmux/agent/start` 新增**可选** `signal` 参数（`res_model` / `res_id` / `action_xmlid` /
> `menu_xmlid` / `geo`）。**不传即保持 v3.0 行为**；传 `{}` 表示"查过但什么都没有"并触发提问。

## 免手语音闭环：君子动口不动手（v3.1.2）

现场作业的人**不该动手点屏幕** —— 手上有货、戴着手套、手机在兜里。
要做到开口即可完成工作，需要补上四个环节（缺一个就还是要动手）：

```
说「助手」唤醒 ──▶ 说要做什么 ──▶ 系统复述待办 ──▶ 说「确认」执行
   (唤醒词)      (静音自动断句)      (TTS 播报)      (语音确认)
      ▲                                              │
      └──────────────── 循环，全程零点击 ◀────────────┘
```

| 环节 | 为什么必须有 |
|---|---|
| **唤醒词** | 麦克风要常开才能免手，但常开就不能啥都上传。唤醒词前的音频**只在内存比对后丢弃**，不上传不落盘 |
| **静音自动断句** | 原来点一下开始、再点一下停止 —— 每说一句动两次手，等于没免手 |
| **语音确认** | 越是高风险操作（验证单据、过账、报工）越要确认，而确认原来只能点按钮 |
| **播报时静音** | 否则扬声器的声音被自己听进去，系统会把自己说的话转写回来 |

**五个界面全部接入，才算"完全"免手**（v3.1.2 只做了面板与引导流，
这两处仍需点按钮打开界面）：

| 界面 | 免手动线 |
|---|---|
| **智能体轨迹** | 说选项的**标签**即可作答；发出去的是选项的**代码**（与点按钮完全一致），语音无法扩大答案集合 |
| **任务循环** | 说「下一个」取任务；**把做了什么说出来就是汇报** —— 不必先说"汇报"再说内容 |
| **系统托盘** | 全局唤醒监听挂在这里。面板只在打开时存在，救不了"还没打开"的情形；托盘整个会话都在，**它的监听只负责打开界面，不能执行任何动作** |

入口是面板与引导流上的 **「免手模式」** 按钮（opt-in，默认关）。
设备不支持时明确提示，原有屏幕操作**全部保留**。

> **现场作业人员请直接看 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)** ——
> 那份手册不假设读者懂 Odoo，包含三十秒上手、语音指令速查表、
> 风险等级说明和常见问题。

### 导航指令按上下文限定

「跳过」在流程引导里有意义，在面板里没有。因此每条导航指令都声明了自己
生效的上下文，**上下文写错时一条指令都不匹配** —— 打错字不会意外放大某个词的作用域。

| 指令 | 可用上下文 | 说法 |
|---|---|---|
| `next` | 任务循环 | 下一个 / 继续 / next |
| `help` | 任务循环 / 引导 / 智能体 | 怎么办 / 帮助 / help |
| `skip` | 引导 / 智能体 | 跳过 / 略过 / skip |
| `repeat` | 全部 | 再说一次 / 重复 / repeat |

## 设备动作：拍照与扫码（v3.1.3）

免手模式走到最后，还剩两件事**说不出**：拍照和扫码。没有哪句话能变出
一张照片。所以目标不是把它们也变成语音，而是让它们**不打断对话**。

| 按钮 | 结果 |
|---|---|
| `扫码` | 相机解码 → 填条码框 → **不自动提交** |
| `拍照识别` | OCR → 填文字/数量框 → **不自动提交** |
| `拍照留证` | 照片作为本次操作的**记录附件**（走 `photos` 参数） |

**挂起 → 转交 → 自动恢复。** 相机打开时循环挂起，关闭时自动恢复，
上下文和确认门状态原样保留。挂起是**引用计数**（`Set`）而非布尔开关 ——
TTS 和相机是两个持有者，布尔开关会让先释放的那个把麦克风打开，
而另一个还需要安静（拍照时把房间传去转写，或播报时听到自己）。

**识别只回填、不提交**，因为识别会错：错的值填进框里你能看见，
直接提交就是一次没人授权的业务写入。引导流程里识别结果只写进
**该步骤本来就有的字段**，取不到值就中止，不提交占位符。

**决策步骤（`confirm` / `capability`）不显示任何相机按钮** ——
给决策步骤开相机，等于给「拍照」一条绕过决策的路。

照片**不在客户端转成附件**，而是作为参数走**同一个 `/mmux/execute` 漏斗**，
所以 `ExecutionGuard` 的 ACL、记录规则、公司隔离、幂等、审计全部照旧。
附件创建与 `message_post` 在**同一事务**，post 失败则整体回滚，
**不留孤儿附件**；没有 `photos` 参数时行为完全不变。

**降级链是必需的**：`BarcodeDetector` 只有 Chrome/Edge 有，
**Safari 和 Firefox 都没有**；iOS 非 HTTPS / WebView 下
`<input type="file" capture>` 是唯一能走的路。三条路都接到同一对回调。

### 语音确认的三条安全约束

说「确认」会执行真实的业务写入，所以不能"听见就算"：

1. **整句**必须等于肯定词 —— 「别确认」**不会**被当成同意（有单测守着）；
2. 只有在**确有操作待确认**时才算授权，平时说「确认」只是普通说话，
   不能预先批准后面的步骤；
3. 确认栏一关（完成/失败/取消）立即失效。

分类只在一处发生（`voice_loop.js::_emit`），所以同意闸门与上下文
不可能在不同调用方之间跑偏；且同意判定永远排在导航指令**之前**，
同意不会被任何导航规则遮蔽。

> **残留风险**：声音可被旁人模仿，噪音也可能误识别。若现场有他人可能
> 听到并复述，建议对 L2 步骤继续用屏幕确认 —— 点击路径没有移除。

## 测试

```bash
# 纯 Python 单测（无需 Odoo 运行时）
cd field_multimodal_ux && python3 -m unittest discover -s tests_pure -t .

# Odoo 测试（安装模块后）
odoo-bin -d yourdb -i mmux_scenario_warehouse,mmux_scenario_mfg,mmux_scenario_sales,mmux_scenario_fieldservice \
    --test-enable --stop-after-init
```

纯单测覆盖：核心协议与守卫、LLM 意图与错误契约、英文归一化、计划编排、引导流与技能等级、
多语言解析、**任务循环队列（`tests_pure/test_taskqueue.py`）**、
**智能体选任务校验（`tests_pure/test_agent.py`）**，以及
**场景上下文判定（`tests_pure/test_scenario.py`）**——确定性打分、并列稳定、
回答解析的精确优先与拼写不修正、坐标脱敏不对称性，以及
**免手语音闭环（`tests_pure/test_voice_logic.py`，28 条）**——唤醒词边界、
否定句不得成为同意、静音断句不截断句中停顿、**导航指令的上下文限定**，以及
**设备拍照/扫码决策（`tests_pure/test_capture_logic.py` + `test_capture_intent.py`
+ `test_capture_service.py`）**——缩放比不失真且不放大、条码去重只压相同值、
`/mmux/vision` 无 `fallback` 键、**决策步骤不给相机**、**数量取不到数字就返回
null 而不是 0**、照片参数恒为 `ir.attachment` 形状、**识别提示语绝不宣称"已完成"**，以及
**热路径性能回归（`tests_pure/test_flow_perf.py`）**——按**调用计数**而非墙钟时间断言：
`advance` 读游标的序列化次数必须为 **0**、不得重跑步骤构造器、游标读是 O(1) 而视图是
O(n)。（需要真实耗时数字时用 `MMUX_PERF=1` 开启计时探针。）
另有**可配置预算（`tests_pure/test_agentloop_run.py` 的 `TestConfigurableBudgets`，6 条：
注入回合/能力调用/重复步预算生效、非法值回落默认、首回合提示词如实报告注入后的
预算、预算不持久化到 scratchpad）**。
当前共 **546 条**。

Odoo 侧覆盖：`test_mmux_taskloop.py`（取任务/优先队列/L2 确认暂停/完成后发布下一个/游标恢复/
能力不匹配拒绝/循环结束）、`test_mmux_taskloop_agent.py`（自主选中被采纳、幻觉与超时全部降级仍能取到任务、
**智能体不削弱 L2 确认**、**不能指名他人的任务**、决策审计、响应键向后兼容）、
`test_mmux_scenario_context.py`（3 类 34 条：判定收窄白名单 / 歧义提问且**选项可回答** /
已声明优先 / **坐标既不回显视图也不写入审计** / **v3.0 路径保持** / GPS 记录但不判定）、
`test_mmux_settings.py`（2 类 18 条：字段↔参数映射 / **密钥不出现在表单 payload** /
**留空不清空密钥** / 旧参数原样可见（无需迁移）/ 超时与 provider 名**在保存时报错** /
**注册表为空时不锁死字段** / mock 参数**没有界面字段**）、
`test_mmux_service_split.py`（4 条：**拆分后仍合成同一个 model** / 每个拆分文件的方法
都可达 / 公开 API 未变 / 没有任何 siblings 误声明 `_name`）、
**`test_photo_evidence.py`（9 条：照片落到记录而非仅消息 / 消息引用附件 /
无照片向后兼容 / 空数据跳过不致命 / `data:` 前缀被剥离且字节仍是 JPEG /
**越权被守卫拒绝且不留任何附件**）**，
以及四个场景各自的 `test_*_taskloop.py` 端到端闭环。

> 本仓库另附**十一个**沙箱验收 harness（无需 Odoo 运行时即可跑）：
> `verify_agent_service.py`（68 项服务层断言）、`verify_scenario_odoo_tests.py`（34 项
> Odoo 测试模拟）、`verify_agent_frontend.py`（106 项前端契约）、
> `verify_voice_hands_free.py`（74 项免手 + 设备动作契约，含 V53–V63）、
> `verify_photo_evidence.py`（9 项照片落盘，**忠实模拟 `ir.attachment`、
> `message_post` 与 `check_access_rights`**）、
> `verify_wiring.py`（跨模块布线完整性）、`verify_scenario_context.py`
> （`docs/ACCEPTANCE.md` §10.9 的 A25–A70，48 项）、`verify_mmux_settings.py`
> （设置页封装，53 项），以及本轮新增的
> `verify_caps_cache.py`（10 项：能力清单指纹键缓存的命中、失效与
> **等数量换成员**边界）、`verify_guide_cache.py`（20 项：导览步骤缓存的
> 增量读取、重置 `done`、写入失效与 **env 作用域隔离**），以及本轮新增的
> `verify_config_layer.py`（34 项：v3.4 配置化四域——真实纯核心上的**预算注入**、
> **开关同步语义**、**覆盖优先级（含 `_flow_for` 源码级守卫，优先级倒置可在无
> Odoo 环境下被检出）**、**设置约束的 0 非空语义**）。
> 它们**不是**真实 Odoo 运行的替代品，
> 而是保证断言本身逻辑自洽、且服务层确实满足它们。
>
> 其中 `verify_photo_evidence.py` 的 `ir.attachment`、`message_post` 与
> 访问检查都是**真跑**而不是打桩 —— 正因如此它才暴露了自身的两个盲点：
> 只数附件条数无法发现 `data:` 前缀没被剥离（行还在，字节已经不对），
> 以及"越权被拒"可能来自下游兜底而非守卫本身。两处都已补强，并各自
> 反向注入缺陷确认能被抓到。
>
> 另一个同类发现：`verify_mmux_settings.py` 会**忠实模拟**
> `res.config.settings` 的 `config_parameter` 写入路径（含"空值删除参数"
> "不变则跳过"等行为）—— 正因如此它才发现了一个真实缺陷：只在
> `get_values()` 里屏蔽密钥是**不够的**，因为 `default_get()` 会先把明文
> 填进 payload。

详见 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)（现场作业人员使用手册）、
[docs/INSTALL.md](docs/INSTALL.md)、[docs/CONFIG.md](docs/CONFIG.md)、
[docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)、
[docs/PERFORMANCE.md](docs/PERFORMANCE.md)（热路径性能：改了什么，
以及五个**已排查但刻意不改**的位置及其理由）。

## 版权与许可

Copyright (C) 2026 **jeffery9@gmail.com & geninit**。

本仓库全部代码与文档以 **LGPL-3.0 or later** 许可发布（全文见根目录
[`LICENSE`](LICENSE)）；各模块 manifest 的 `author` / `copyright` 键、
每个 `.py` 文件的前三行头注释、每个 `.js` 资产文件的块注释均携带同一
申明，并由 `tools/check_standards.py` 规则 14（`copyright headers
present`）机器校验——新增文件漏带头注释会直接 lint 失败。规范条目见
[docs/CODING_STANDARDS.md](docs/CODING_STANDARDS.md)。
