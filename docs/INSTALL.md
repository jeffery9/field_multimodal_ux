# 安装部署指南

## 1. 前置条件

- Odoo 19 Community（源码或 Docker 部署均可）
- 已安装模块：`stock`、`barcodes`、`mail`（core）；`mrp`、`maintenance`（制造场景，可选）
- 若使用真实 AI 服务：任一 OpenAI 兼容端点（OpenAI / Azure OpenAI / 本地 vLLM、LocalAI 等）及 API Key

## 2. 安装模块

```bash
# 将仓库放入 addons 路径
cp -r field_multimodal_ux/* /opt/odoo/custom-addons/

# 更新 apps 列表后，在 Apps 界面依次安装：
#   1) MMUX Core            (mmux_core)
#   2) MMUX AI Providers    (mmux_provider_ai)
#   3) MMUX Warehouse       (mmux_scenario_warehouse)   可选
#   4) MMUX Manufacturing   (mmux_scenario_mfg)         可选
#   5) MMUX Sales Visit     (mmux_scenario_sales)       可选
#   6) MMUX Field Service   (mmux_scenario_fieldservice) 可选
```

或命令行：

```bash
odoo-bin -d yourdb -c odoo.conf \
    -i mmux_core,mmux_provider_ai,mmux_scenario_warehouse,mmux_scenario_mfg,mmux_scenario_sales,mmux_scenario_fieldservice \
    --stop-after-init
```

> 依赖方向：`core ← provider_ai ← scenario_*`。不得反向安装。

## 3. 配置（Field Assistant → Configuration → Settings）

**推荐路径：使用设置页**。安装后打开 *Field Assistant → Configuration → Settings*，
一个表单页里可以完成全部配置——provider 选择、端点、密钥、模型、超时、语言、智能体开关。
下拉选项由已注册的 provider 动态生成，非法 provider 名与越界超时**在保存时即报错**，
而不是等到现场作业时才暴露成 `ProviderError`。

**API Key 是只写字段**：保存后**永远不会回显**到浏览器（页面源代码与网络响应中都不含明文），
输入框旁边用「A key is stored」标记当前是否有值。**留空 = 不修改**，
所以打开设置页直接保存不会清空已配置的密钥。

### 3.1 参数对照（高级/自动化场景）

设置页写入的就是下面这些系统参数——**键名未变**，因此既有的自动化脚本、
升级前的配置**全部继续生效**，无需迁移。需要脚本化部署时仍可直接设置它们：

| 参数 | 设置页字段 | 默认 |
|---|---|---|
| `mmux.asr.provider` | Speech provider | `mock` |
| `mmux.asr.base_url` | Speech base URL | `https://api.openai.com/v1` |
| `mmux.asr.api_key` | Speech API key | 空（mock 时不需要） |
| `mmux.asr.model` | Speech model | `whisper-1` |
| `mmux.asr.language` | Speech language | 自动检测 |
| `mmux.asr.timeout` | Speech timeout | `20` |
| `mmux.llm.provider` | Semantic provider | `mock` |
| `mmux.llm.base_url` | Base URL | `https://api.openai.com/v1` |
| `mmux.llm.api_key` | API key | 空（mock 时不需要） |
| `mmux.llm.model` | Model | `gpt-4o-mini` |
| `mmux.llm.timeout` | Timeout | `20` |
| `mmux.vision.provider` | Vision provider | `mock` |
| `mmux.vision.model` | Vision model | `gpt-4o-mini` |
| `mmux.ocr.provider` | OCR provider | 空（回退到 vision provider） |
| `mmux.agent.enabled` | Agent loop | `1`（开启） |

> **不在设置页的参数**：`mmux.llm.mock_response` / `mmux.llm.mock_error` /
> `mmux.asr.mock_transcript`。三者是**测试注入通道**（`mock_response` 更是"提供
> provider 响应体"的入口），所以**刻意不做成界面字段**——把测试后门做成正式功能
> 会让系统可以被指示说任何话。它们仍然有效，供自动化测试使用。

> ⚠️ **LLM 是语义识别的唯一路径**。未配置（或配置为 `mock` 之外的 provider 但无 api_key）时，
> `/mmux/parse_intent` 返回结构化错误 `{code:"llm_unavailable", retryable:false}`，
> **不再有离线规则解析兜底**。业务执行（`/mmux/execute`）与原生 Odoo 功能**完全不受影响**。
> 建议生产环境配置私有端点（LocalAI / vLLM）以避免文本出境。

## 4. 权限组

- **Field Multimodal UX / User**：可使用现场助手（普通仓库/现场人员）。
- **Field Multimodal UX / Administrator**：可配置场景、查看审计日志、开启 L3 能力。

安装后请将现场人员加入 User 组。

## 5. 验证安装

1. 顶部导航出现「现场助手」麦克风图标（systray）。
2. 菜单「Field Assistant → Field Workspace」可打开工作区。
3. 菜单「Field Assistant → Configuration → Interaction Log」可查看审计日志。

## 6. 升级到 19.0.2.1.0（任务循环）

本版本新增**任务循环**（取任务 → 执行 → 求助 → 汇报 → 发布下一个任务）。
升级步骤：

```bash
# 升级内核与四个场景模块
odoo-bin -d yourdb -u mmux_core,mmux_scenario_warehouse,mmux_scenario_mfg,\
mmux_scenario_sales,mmux_scenario_fieldservice --stop-after-init
```

升级后：

1. 打开「Field Assistant → Field Workspace」，默认标签页即**任务循环**。
2. 选择场景（可不填）→ 点「获取任务」。有分配给你的待办记录时即会返回任务卡。
3. 任务卡上「开始引导」进入该任务的语音引导；「向系统求助」可中途提问。
4. 在「汇报完成」文本框用**中文/English/中英混杂**描述完成情况 → 提交。
   若为 L2 完成动作，会弹出确认卡；确认后系统**标记完成并自动发布下一个任务**。

新增的 4 个完成能力（`picking_task_complete` / `wo_task_complete` /
`service_task_complete` / `visit_task_complete`）均为 **L2 强制确认**，无需额外配置；
若不需要某个场景的任务循环，**卸载对应的场景模块**即可（任务源随场景模块注册）。

> 权限说明：任务循环**不新增任何业务变更语义**，也不引入新的权限提升——
> 任务查询以当前用户身份执行，完成动作走既有的 `execute_capability` 漏斗。

## 7. 升级到 v2.2（智能体自主选任务）

v2.2 无需数据库迁移（**不新增模型、不改 schema**），仅升级代码：

```bash
odoo-bin -d yourdb -u mmux_core,mmux_provider_ai,mmux_scenario_warehouse,\
mmux_scenario_mfg,mmux_scenario_sales,mmux_scenario_fieldservice --stop-after-init
```

升级后：

1. 任务卡上方出现「**智能体建议**」决策条，显示选择该任务的理由。
2. 若 LLM 可用，徽章显示「智能体自主判断」，并给出置信度与被排除的其他候选。
3. 若 LLM 未配置 / 超时 / 返回被拒，徽章显示「**队列兜底**」，
   任务仍照常发布——**取任务绝不会因为模型不可用而失败**。

> ⚠️ 与语义识别不同：`/mmux/parse_intent` 在无 provider 时会返回 `llm_unavailable` 错误，
> 而任务选择会**静默降级为优先队列**并正常返回任务。
> 若要强制回到 v2.1 的纯队列行为，可将 `mmux.llm.provider` 留空（等效于 `use_agent=False`）。

> 安全提示：智能体**只选择、不执行**。它的输出只是"候选集合中的一个 id 加一段理由"，
> 任何业务写入仍走白名单能力 + L2 人工确认。因此**即使模型被提示词注入攻破，
> 也无法越过权限边界**——它能做的只是"选错一个你有权看到的任务"。

## 8. 升级到 v3.1（场景上下文识别）

v3.1 新增**位置感知层**，并且**新增 3 个非必填字段**（有默认值，Odoo 自动补列，
**无需手工迁移脚本**）。

```bash
odoo-bin -d yourdb -u mmux_core,mmux_provider_ai,mmux_scenario_warehouse,\
mmux_scenario_mfg,mmux_scenario_sales,mmux_scenario_fieldservice --stop-after-init
```

升级后：

1. **Setting → Field Assistant → Configuration → Scenarios** 的表单多出一组
   **「设备位置（v3.1）」** 字段：`geo_collect` / `geo_weight` / `geo_radius_m`。
   **出厂值全部为关闭/0**。
2. 打开任意作业界面（如拣货单），现场助手面板顶部新增**场景上下文条**：
   显示当前判定到的场景徽章及其**来源**（`signal` / `ambiguous` / `none` / `user`）。
3. 若位置**无法确定**，系统**主动提问**——选项是服务端**真实存在**的场景列表。
4. 若你在 `loop.scenario` 里**显式声明**了场景，而位置看起来是**另一个**，
   面板会显示一条**分歧告警**，但**不会**改变你声明的场景，也**不会**阻塞作业。

### 8.1 关于浏览器定位（默认关闭）

v3.1 **默认不会请求浏览器的定位权限**。若要启用采集：

1. 在 Scenarios 里把目标场景的 **`geo_collect`** 勾上；
2. （可选）同时设置 **`geo_weight`**。**只设权重不勾采集会直接报校验错**——
   一个配了却永不生效的配置比报错更难被发现。

> ⚠️ **本期 GPS 只采集、不判定**：即使开启，`geo_weight` 的**出厂默认值仍是 0**，
> 定位只写入审计（且**坐标默认脱敏为布尔**），**不影响任何场景判定结论**。
> 这样可以在**真实环境里先观察定位质量**，再决定是否让它参与判定。

### 8.2 向后兼容说明

- **旧前端零回归**：不传 `signal` 参数时，服务端走**与 v3.0 逐字节一致**的路径——
  不提问、响应结构不变。升级后端**不会**让现存客户端突然开始要求操作员选场景。
- **`null` ≠ `{}`**：`signal` **不传**表示"本项不适用"（静默）；
  传 **`{}`** 表示"客户端查过了，什么都没有"（提问）。集成方务必区分这两个值。
- **坐标不外泄**：响应体与交互日志中均**不含经纬度数值**。

## 9. 升级到 v3.1.1（配置迁移到设置页）

**无需数据库迁移**：设置页写入的就是原有的 `mmux.*` 系统参数，**键名一个都没变**，
因此升级后既有配置**原样生效**。

```bash
odoo-bin -d yourdb -u mmux_core,mmux_provider_ai,mmux_scenario_warehouse,\
mmux_scenario_mfg,mmux_scenario_sales,mmux_scenario_fieldservice --stop-after-init
```

升级后：

1. *Field Assistant → Configuration* 下新增 **Settings** 菜单，
   取代「设置 → 技术 → 系统参数」成为**推荐的配置入口**。
2. 现有参数值会**直接出现在设置页里**（读的是同一把键，没有搬运步骤）。
3. API Key 字段显示为**空**（只写字段，不回显明文），旁注「A key is stored」表明已有值。
   **不动它、直接保存，密钥不会被清空。**

> **为什么不需要迁移**：设置页字段用 `config_parameter="mmux.llm.provider"` 之类的属性
> 直接指向旧键。值从来没有搬家，所以不存在"搬到一半"或"搬错"的可能。

## 10. 升级到 v3.1.1（服务层文件拆分）

**无需数据库迁移，无需重启配置**：这次只搬动**代码文件**，模型的 `_name` 仍是
`mmux.service`，所有对外 API 与系统参数键名**一个都没变**。

```bash
odoo-bin -d yourdb -u mmux_core,mmux_provider_ai,mmux_scenario_warehouse,\
mmux_scenario_mfg,mmux_scenario_sales,mmux_scenario_fieldservice --stop-after-init
```

### 变了什么

`mmux_core/models/mmux_service.py` 从 2324 行的单文件，变成 27 行的**门面**，
行为分散到 7 个同表文件：

| 文件 | 职责 | 方法数 |
|---|---|---|
| `mmux_service_infra.py` | provider / 执行守卫 / 幂等 / LLM 出参抽取 | 12 |
| `mmux_service_skill.py` | 语言解析 + 技能等级 | 3 |
| `mmux_service_capability.py` | 权限校验 + 能力执行 | 7 |
| `mmux_service_scenario.py` | 场景判定 + 审计落库 | 12 |
| `mmux_service_loop.py` | agent 循环 | 17 |
| `mmux_service_flow.py` | 任务与脚本化引导 | 21 |
| `mmux_service_plan.py` | 计划提议与执行 | 4 |

每个文件都 `_inherit = "mmux.service"`，Odoo 在加载时把它们**合并成同一个 model**。
因此控制器、OWL 前端、23 个 capability 的调用方式**完全不变**。

### 升级后请确认

1. 重启 Odoo 后，`env["mmux.service"]` 仍是**一个** model（不是 8 个）。
   若某个文件漏进 `models/__init__.py`，它的方法会**静默消失**——
   `test_mmux_service_split.py` 正是为此设的护栏，建议升级时一并跑测试。
2. 若你**曾自行修改过** `mmux_service.py`，本次升级会与服务端版本冲突。
   请把你的改动按职责搬到对应的新文件（对照上表），而不是整个覆盖回去。

> **为什么拆**：原单文件跨 8 类职责（provider、守卫、意图、计划、引导、
> 任务循环、场景判定、agent 循环），2324 行确实违反 SRP。拆法按**方法级调用
> 依赖图**确定切割线，不是按行数平均分，也没有引入任何新抽象层
> （无 Repository / Service 接口 / DI 容器）。

## 11. 升级到 v3.1.2（免手语音闭环）

纯前端增量，**无数据库迁移、无配置变更**。刷新页面即可（`assets_backend`
的 `static/src/**/*` 通配会自动带上新文件，无需改 manifest）。

### 新增能力

| 目标 | 实现 |
|---|---|
| 不用点屏幕就能开口 | 唤醒词「助手」/「assistant」，说一次即可 |
| 不用点「停止」 | RMS 能量 + 静音时长（约 800ms）自动判定句末 |
| 高风险步骤不用点确认 | 说「确认」执行、说「取消」放弃，与点击等价 |
| 不被自己的播报打扰 | 系统说话时暂停采集，说完自动恢复 |
| **不用点开界面** | 系统托盘挂全局唤醒监听，说「助手」即打开面板 |
| **智能体问题不用点选项** | 说选项的**标签**作答，发出的是选项的**代码** |
| **任务汇报不用点按钮** | 说「下一个」取任务；**把做了什么说出来就是汇报** |

入口：现场助手面板与引导流的 **「免手模式」** 按钮（opt-in，默认关）。

盘点一下"还要动手"的残余路径，v3.1.2 覆盖了前两处，本轮补齐后三处：

| 界面 | v3.1.2 | 本轮 |
|---|---|---|
| 面板 | 已接入 | 已接入 |
| 流程引导 | 已接入 | 已接入 |
| 智能体轨迹 | 要手点选项 | **语音作答（发代码）** |
| 任务循环 | 要手点汇报 | **说「下一个」+ 口头汇报** |
| 打开界面 | 要手点托盘 | **全局唤醒监听** |

> 打开界面这件事有个容易踩的坑：面板只在**已经打开**时才存在，
> 所以它自己不可能成为"把它打开"的东西。因此全局监听必须挂在
> 整个会话都在的**系统托盘**上，并且它**只负责打开，不执行任何动作**。

### 导航指令按上下文限定

「跳过」在引导/智能体里有意义，在面板里没有。每条指令都声明自己生效的
上下文，**上下文写错时一条都不匹配** —— 打错字不会意外放大某个词的作用域。

| 指令 | 可用上下文 | 说法 |
|---|---|---|
| `next` | 任务循环 | 下一个 / 继续 / next |
| `help` | 任务循环 / 引导 / 智能体 | 怎么办 / 帮助 / help |
| `skip` | 引导 / 智能体 | 跳过 / 略过 / skip |
| `repeat` | 全部 | 再说一次 / 重复 / repeat |

### 隐私边界（重要）

**唤醒词之前的音频只在浏览器内存里比对，然后丢弃** —— 不上传、不落盘、
不保留。只有唤醒之后的片段才送去转写。这正是设置唤醒词、而不是让录音
一直开着的原因。

### 安全性说明

语音确认对**高风险（L2）操作**是真实授权，因此做了三条约束：

1. 必须是**整句**等于肯定词 —— 「别确认」这类否定句**不会**被当成同意；
2. 只有在**确有操作待确认**时，肯定词才算授权；平时说「确认」只是普通说话，
   不能预先批准后面的步骤；
3. 确认栏关闭（执行完成/失败/被取消）时立即失效。

同意判定永远排在导航指令**之前**，因此"确认"不会被任何导航规则遮蔽；
分类也只在 `voice_loop.js::_emit` 一处发生，调用方无法各自解释规则。

> **残留风险**：声音可被旁人模仿，且现场噪音可能造成误识别。若场景中
> 存在他人可能听到并复述的情况，建议对 L2 步骤保留屏幕点击路径
> （点击路径未被移除，随时可用）。

### 模板修复（同版本内）

本轮给输入框补 `t-on-keydown` 时，有三处把 `&&` 直接写进了 XML 属性。
XML 里 `&` 必须写成 `&amp;`，否则**整份模板解析失败**，控件静默不渲染
（JS 语法是对的、Python 是对的、单测也不加载 QWeb，所以哪一环都没拦住）。

已修正 3 处（`panel.xml` 1 处、`guide.xml` 2 处），并给
`tools/check_standards.py` 加了**规则 11**（`XML is well-formed`），
对全部 XML 做解析校验（当时 12 份，v3.1.3 增至 13 份）。该规则同样注入缺陷自测过，报错精确到行号。

如果你是从 v3.1.2 升级且**浏览器控制台报 QWeb 模板解析错误**，
那就是这个问题，重新拉取本版本即可。

### 不支持的设备

浏览器没有 `SpeechRecognition`（或没有麦克风）时，免手按钮会明确提示
「此设备不支持免手模式，请使用屏幕操作」，原有全部屏幕操作**原样保留**。

---

## 12. 升级到 v3.1.3（设备动作：拍照与扫码）

**无数据库迁移、无配置变更**，但**需要重启 Odoo 服务**（新增了
`ir.attachment` 的写入路径与两个能力参数）。前端刷新即可。

```bash
# 更新代码后
sudo systemctl restart odoo    # 或等价的重启方式
# 浏览器硬刷新（Ctrl+Shift+R）
```

### 12.1 解决什么问题

免手模式走到最后，还剩两件事**说不出**：拍照和扫码。没有哪句话能变出
一张照片。v3.1.3 的目标不是让这两件事也变成语音，而是让**它们不打断
对话**：动手 → 交回 → 自动接着听，上下文和确认状态都还在。

### 12.2 新增的三个入口

| 按钮 | 位置 | 结果 |
|---|---|---|
| `扫码` | 条码框旁 | 相机解码 → 填条码框 → **不自动提交** |
| `拍照识别` | 输入区 | OCR → 填文字/数量框 → **不自动提交** |
| `拍照留证` | 输入区 | 照片作为本次操作的**记录附件** |

引导流程里按步骤的 `input_type` 自动显示对应的那一个：
`scan` 显示扫码，`ocr`/`receipt`/`voice_qty` 显示拍照识别，
`evidence`/`photo` 显示拍照留证。**`confirm` 和 `capability` 步骤不显示
任何相机按钮** —— 给决策步骤开相机，等于给「拍照」一条绕过决策的路。

### 12.3 安全红线：识别只回填，不提交

**OCR 的结果永远只填进草稿框，绝不自动提交。**

理由很直接：识别会错，而错的东西如果直接提交，就是一次没人授权的
业务写入。填进框里，写错你能看见；直接提交，你可能永远不知道。

代码层面体现在三处：

1. `capture_service.js` 只调用 `/mmux/vision`，**没有任何 `execute` 调用**；
2. 面板识别后赋给 `state.text` / `state.barcode`，由用户按按钮或回车触发；
3. 引导流程识别后调 `stepFieldFor()` 取**该步骤本来就有的字段**，
   取不到值（`null`）就**直接 return**，不提交占位符。

### 12.4 拍照时免手循环怎么处理

相机打开时，循环进入**挂起**状态（麦克风不再采集）；关闭相机时自动
**恢复**，上下文与确认门状态原样保留。

> 关键点：挂起是一个**引用计数**（`Set`），不是一个布尔开关。
> 因为有两个持有者 —— TTS 播报和相机转交 —— 布尔开关会出现「第一个
> 持有者恢复时把麦克风打开，而第二个还需要安静」，结果是拍照中途把
> 房间里的声音传去转写，或者播报中途听到自己。

### 12.5 照片去哪了

照片**不是**在客户端转成附件，而是作为 `photos` 参数走**同一个
`/mmux/execute` 漏斗**（`service_capture_evidence` /
`picking_capture_evidence`），因此 `ExecutionGuard` 的 ACL、记录规则、
公司隔离、幂等、审计**全部照旧适用**。

- 附件创建与 `message_post(attachment_ids=...)` 在**同一事务**：
  post 失败则整体回滚，**不会留下孤儿附件**；
- 服务器会剥离 `data:` 前缀；客户端也剥一次（纵深防御）；
- 空数据条目**跳过而不报错** —— 相机失败不应该让那份工作汇报也失败；
- 没有 `photos` 参数时行为**完全不变**（向后兼容）。

### 12.6 设备降级

| 情况 | 表现 |
|---|---|
| 无相机 / 拒绝权限 | 「无法访问相机，请改用相册或手输」，手输照常 |
| 浏览器无 `BarcodeDetector` | 「此浏览器不能自动识别条码，请对准后拍照，或直接输入」 |
| iOS 非 HTTPS / WebView | 走 `<input type="file" capture="environment">`（`相册` 按钮） |

> `BarcodeDetector` 只有 Chrome/Edge 有，**Safari 和 Firefox 都没有**。
> 所以降级链不是可选项。条码本来就能手输，扫码只是省事。

### 12.7 隐私

相机预览界面上常驻一行说明：

> 照片会上传用于识别，并作为记录附件留存。

照片在客户端会**缩到最长边 1280px、JPEG 质量 0.7**（约为原图的
1/10 体积，现场网络够用，铭牌上的字仍然看得清）。相机流在关闭和
组件卸载时都会 `getTracks().forEach(t => t.stop())` —— 不释放的话
硬件指示灯会一直亮着，在工人看来就是「它在拍我」。

### 12.8 顺带修掉的一个既有缺陷

`tts.speak()` 开头会调 `speechSynthesis.cancel()`，而这会**同步**触发
上一句的 `onend`，也就是 `voiceLoop.resume()`。结果是：前一句的结束
回调在新一句还在播的时候就把麦克风打开了 —— 正是静音机制本该防止的
回声。

已用 `tag` / `_current` 代次校验 + 绑定到 `onend` 与 `onerror` 的幂等
`finish` 修复。

### 12.9 工具链加固（规则 12、13）

这一轮踩到一个新的静默失效，顺手补了两条规则：

| 规则 | 抓什么 | 为什么 |
|---|---|---|
| **12** `XML comments are legal` | XML 注释里的 `--` | 写在注释里的 `-- the intent is null` 是非法的，报错行号落在注释**自己**那一行，容易被误判成周围的标签写错 |
| **13** `JS is well-formed` | JS 语法错误 | 打包只在 Odoo 提供资源时发生；`.js` 里的语法错误对 Python 检查、单测全都不可见，症状只是「按钮没反应」，日志里什么都没有 |

两条都做过**缺陷注入自测**（把缺陷放回去，确认能抓到，再还原）。

### 12.10 升级后请确认

```bash
# 全部应当通过
python3 -m pytest tests_pure -q                  # 540 passed
python3 tools/check_standards.py                 # 13 rules, no problems
python3 /tmp/verify_voice_hands_free.py          # 74/74
python3 /tmp/verify_photo_evidence.py            # 9/9
```

在真实 Odoo 上：

```bash
odoo -d <db> --test-enable --stop-after-init \
     -m mmux_scenario_warehouse,mmux_scenario_fieldservice
```

应看到 `test_photo_evidence.py` 的 9 个用例通过（附件落在记录上、
消息引用附件、无照片向后兼容、越权被守卫拒绝且不留附件）。

---

## 13. 升级到 v3.3（热路径性能）

这一版只做一件事：把导览热路径上**确实存在的**重复做功去掉，并给两条真实
的热点加缓存。**没有**改任何对外契约——`cursor_view` 的签名与返回结构、
所有 `/mmux/*` 路由的形状、前端依赖的字段，全部保持原样。

完整记录（含「已排查但刻意不改」的五项及其理由）见
[`docs/PERFORMANCE.md`](PERFORMANCE.md)。这里只列要点。

### 13.1 顺带修掉的一个 P0 崩溃

加固规范的模块名检查时，它**意外**标出 `mmux_service_flow.py` 调用了
`fields.Datetime.now()`，但 `fields` 从未被导入。用 AST 核实：`fields`
被使用、却从未被绑定——**每一次任务成功完成**都会抛 `NameError`。

与性能工作无关，属于排查途中的意外收获，已修复并加了永久性检测
（`tests_pure/test_module_names.py`）。

> **若你在此之前遇到过「上报任务后报错」，原因就是这个。** 升级即可。

### 13.2 改了什么

| 改动 | 效果 |
| --- | --- |
| `advance` 不再为读一个游标而序列化整张列表 | 单次推进的完整序列化 **3 次 → 1 次** |
| 新增 `_current_step`，`advance` 与 `cursor_view` 共用 | 两者不可能再对「当前步」产生分歧；`advance` 变为无副作用 |
| 导览步骤列表按请求缓存 | start → current 的构造器调用 **2 次 → 1 次** |
| `_caps_payload` 加 registry 级缓存（指纹键） | LLM 提示词不再重复序列化能力清单 |

### 13.3 两条缓存的作用域规则（重要，别搞反）

| 缓存 | 作用域 | 为什么 |
| --- | --- | --- |
| 导览步骤列表 | `self.env`（每请求） | 内容来自**某个操作员的真实记录**；放进进程级 registry 会把他的步骤显示到别人屏幕上 |
| 能力清单 `_caps_payload` | `env.registry`（进程级） | 内容**纯由代码派生**，不含任何用户数据 |

判据：**载荷是否含用户数据？** 含 → `env`；纯代码派生 → `registry`。

### 13.4 写入后必须失效（正确性要求）

导览构造器读的是**实时**的 `picking.move_line_ids`，而 `picking_validate`
可能新增欠单行。若缓存不失效，操作员会看着一份**已经不存在**的步骤列表
操作。因此 `guide_advance` 在能力写入成功后调用 `_guide_invalidate_steps()`。

这条有测试固化，且驱动的是**真实推进路径**——因为注入实验证明，只直接
调用失效函数的测试，抓不到「调用点被删」这种情况。

### 13.5 五个「看起来该优化、实际不该动」的位置

详细理由见 [`docs/PERFORMANCE.md` §4](PERFORMANCE.md)，此处只给结论，
**防止下一个人「顺手优化」出 bug**：

| 位置 | 结论 |
| --- | --- |
| `_provider_config()` 读 17 个配置键 | **绝对不要加缓存。** `get_param` 本身已是 `@ormcache('key', cache='stable')`，且 `write`/`create`/`unlink` 会自动清缓存。套一层自己的缓存 = 绕开自动失效 = **改了设置不生效的 bug** |
| `idempotency_key` 查询 | 该字段**已有** `index=True`，等值查询走索引，无需改动 |
| `_attach_photos` 走 execute 请求体 | **故意**不另开上传端点：它借的是同一事务 + 同一 `ExecutionGuard`，另开路由会制造绕过 ACL/幂等/审计的第二个写入面 |
| 前端每动作一次 JSON-RPC | **故意**保持无状态：断网可重放是幂等机制成立的前提。本轮只是让每次往返**更便宜**，不减少往返次数 |
| `FLOW_ENGINE.get` | 就是 `dict.get`，O(1)、零 I/O，注册流程总共 4 个 |

### 13.6 升级后请确认

```bash
python3 -m pytest tests_pure -q                     # 540 passed, 1 skipped
python3 tools/check_standards.py                    # 13 rules, no problems
python3 /tmp/verify_caps_cache.py                   # 10/10
python3 /tmp/verify_guide_cache.py                  # 20/20
python3 /tmp/verify_wiring.py                       # INTEGRITY PROBLEMS: none
```

新增的性能回归守卫是 `tests_pure/test_flow_perf.py`：它**按调用计数**断言，
不按墙钟时间（后者在 CI 上会抖）。需要看真实耗时数字时：

```bash
MMUX_PERF=1 python3 -m pytest tests_pure/test_flow_perf.py -q
```

### 13.7 新增的 Odoo 测试需要真机

`mmux_core/tests/test_mmux_guide_cache.py`（6 条）**需要真实的
`stock.picking` 语义**，无法在无 Odoo 的环境里跑。它在
`mmux_core/tests/__init__.py` 已注册，请用 13.6 的 `--test-enable`
命令在真机上确认。同样的不变量已有纯逻辑模拟器
`/tmp/verify_guide_cache.py`（20/20，经注入验证）覆盖。

---

## 14. 升级到 v3.4（运行时配置化，19.0.6.0.0）

这一版把三处原先只能改代码的参数搬进配置面，动机是明确的需求：
**只要不是硬编码的、可以通过配置调整，可以接受**。

| 配置面 | 入口 | 效果 |
|---|---|---|
| Agent 三预算（轮数/能力提议/重复上限） | 设置页 → Agent | 留空 = 模块默认；越界报错；非法值服务端回退默认 |
| 能力开关 `mmux.capability.toggle` | 配置菜单 → Capability Switches | 开关代码注册的能力（含 L3 的管理员启用）；仅 manager 组；每次切换写审计 |
| 流程覆盖 `mmux.flow.override` | 配置菜单 → Flow Overrides | 同 `flow_id` 的行优先于代码流程；删行即回归；影响**新会话**（进行中会话按开始时的脚本走完） |

**动作集 `AGENT_ACTIONS` 保持代码封闭**（用户确认的安全边界）：模型能
说什么不能配置化，否则管理员可扩大模型权限。同理，能力白名单本身仍由
代码定义——开关表只能开/关，不能发明。

### 14.1 安全设计（三处写路径共同）

- 能力开关：ACL（仅 manager）+ 模型层 `user_has_groups` 双校验 + 审计行
  （含风险等级）。
- 流程覆盖：保存时校验步骤（类型/去重/能力在白名单且启用）；步骤引用
  的能力仍走同一 `ExecutionGuard`——**覆盖改变操作员被要求做什么，
  不改变系统可以做什么**。
- 预算：设置页约束 + 服务端回退双层；「读不到配置」永远不等于「无限制」。

### 14.2 升级后请确认

```bash
python3 -m pytest tests_pure -q                     # 546 passed, 1 skipped
python3 tools/check_standards.py                    # 13 rules, no problems
python3 /tmp/verify_config_layer.py                 # config layer: all green (34)
python3 /tmp/verify_guide_cache.py                  # 20/20
python3 /tmp/verify_mmux_settings.py                # 56/56
```

在真实 Odoo 上（新增两个测试模块文件）：

```bash
odoo -d <db> --test-enable --stop-after-init \
     -i mmux_core -u mmux_scenario_warehouse
```

应看到 `test_mmux_capability_toggle.py`（6 条）与
`test_mmux_flow_override.py`（8 条）通过。

### 14.3 已知的一致性窗口（设计内）

能力开关在**多 worker** 下为最终一致：每个 worker 在下一次热路径读取时
各自同步 DB 开关（秒级窗口）。**开→关的延迟不构成安全弱化**——L2 确认
门槛独立于开关存在；关→开的延迟只影响可用性。不引入跨 worker 信号
（复杂度不值）。

### 14.4 一个被 harness 抓到的真实缺陷（留痕）

预算约束最初写成 `if not value: continue`——把 **0** 当「未设置」放行。
0 预算会让 agent 完全不可用（每回合立即 `budget_exhausted`），必须报错
拒绝而不是静默通过。`verify_config_layer.py` 的 D2 检查抓到，已修复为
显式 `value is False or value is None` 判空并加固注释。
