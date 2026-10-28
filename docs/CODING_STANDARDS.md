# 编码规范（强制约束）

> 本文件是**项目级硬性规范**，优先级高于任何通用最佳实践或个人偏好。
> 与规范冲突的代码，即使"能跑通"，也需要修正或明确豁免。

## 一、四条不可协商的约束

### 1. 遵循 Odoo 官方编码规范

以 Odoo 官方 [Coding guidelines](https://www.odoo.com/documentation/19.0/contributing/development/coding_guidelines.html)
与 OCA 惯例为准。关键条目：

| 要求 | 说明 |
|---|---|
| 模块结构 | `<module>/models/`、`views/`、`security/`、`data/`、`tests/`、`__manifest__.py` |
| manifest 键序 | `name` → `summary` → `description` → `version` → `category` → `author` → `maintainers` → `license` → `copyright` → `depends` → `data` → `assets` → `installable` |
| 版权头（强制） | 每个 `.py` 文件前三行：`# -*- coding: utf-8 -*-`、`# Copyright (C) 2026 jeffery9@gmail.com & geninit`、`# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl-3.0.html)`；每个 `.js` 资产文件首行块注释含同样的 Copyright/License 两行。manifest 的 `author` 统一为 `jeffery9@gmail.com & geninit`，并声明 `copyright` 键 |
| 模型 | `_name` 用点分层（`mmux.service`）；**必须有 `_description`** |
| 方法 | 私有方法前缀 `_`；模型方法用 snake_case |
| 导入顺序 | 标准库 → 第三方 → `from odoo import ...` → 本地相对导入 |
| 翻译 | **用户可见字符串必须用 `_()` 包裹** |
| 日志 | 用 `_logger.info/warning`，**禁止 `print()`** |
| 数据访问 | **只走 ORM**；禁止裸 SQL（`self._cr.execute`） |
| 权限 | 用 `ir.model.access.csv` + record rules 设计权限；**`sudo()` 是例外，必须能说明理由** |

### 2. Active Record 模式

数据访问**必须**通过 Odoo ORM 的 recordset：

```python
# 正确
records = self.env["stock.picking"].search([("state", "=", "assigned")])
for rec in records:
    rec.button_validate()

# 禁止
self._cr.execute("SELECT * FROM stock_picking WHERE state = 'assigned'")
```

- 业务操作调用**原生模型方法**（`button_validate`、`record_production`），不绕过；
- 不引入"数据映射层"或"仓储层"来替代 ORM。

### 3. 绝不使用 DDD

**禁止**引入以下 DDD 构件：

| 禁止 | 原因 |
|---|---|
| Repository / 仓储层 | Odoo 的 model + recordset **就是**数据访问层，再加一层是重复抽象 |
| Entity / Value Object / Aggregate | Odoo model 已承担身份与生命周期 |
| Domain Service / Use Case 类 | 与 Odoo 的 model 方法职责重叠 |
| DTO 层 | 用 `dict` 或 model 的 `to_xxx_dict()` 即可 |
| Factory / Builder | Odoo 用 `create()` |
| 贫血模型 + 独立业务层 | Odoo 是**富模型**：业务逻辑写在 model 方法上 |

**判定法**：如果新增的分类/抽象，其职责已经由 Odoo 的 `models.Model`、
recordset、`_inherit` 机制承担，那它就是 DDD 残留，应删除。

### 4. 遵循 SOLID / DRY / 奥卡姆剃刀

- **S**（单一职责）：一个模型/模块只承担一类职责。判定标准是**变更原因**——
  改 A 功能必须动 B 文件，说明职责没分清。
- **O/L/I/D**：用 Odoo 的 `_inherit` 扩展，而非改原类；依赖抽象（注册表）而非具体实现。
- **DRY**：同一知识只写一处。但**不要为了消除表面相似的代码而制造错误抽象**——
  两段代码长得像但会因不同原因变化，就应该分开。
- **奥卡姆剃刀**：**不引入当前不需要的抽象**。特别禁止：
  - 只有一个实现者的接口；
  - 无人调用的参数 / 分支 / 函数；
  - "将来可能会用到"而预留的层。

  > 判定法：**这个抽象删掉之后，代码是变简单还是变复杂？** 如果变简单，就该删。

## 二、已知的、经过核实的偏离项

> 状态：**已核实**。不含未经验证的猜测。

### 已修复

| # | 位置 | 问题 | 违反 | 修复 |
|---|---|---|---|---|
| 1 | `mmux_core/models/mmux_service.py` `_user_skill_level` / `set_skill_level` | 用户读写**自己的**偏好字段却用 `self.env.user.sudo()` | 权限设计（第 1 条） | 去掉 `sudo()` |
| 2 | `mmux_core/services/mmux_core/idempotency.py` `normalize_key()` | 生产代码**零调用**，仅测试引用 | 奥卡姆剃刀（第 4 条） | 删除函数及其测试 |

### 已核实为「非违规」——不得"修复"

这两项曾被误判为问题，核实后确认是**刻意设计**，改掉反而会破坏架构：

| 位置 | 表面现象 | 为什么不是违规 |
|---|---|---|
| `mmux_scenario_*/models/*_capabilities.py` 的 `description=` | 中文字面量，未用 `_()` | **不可能用 `_()`**：`capability.py` 位于 `services/mmux_core/`，该目录**禁止导入 odoo**（设计约束，用于脱离 Odoo 跑单测）。`_()` 是 Odoo 运行时函数。`name`（英文）+ `description`（中文）是**刻意的双语分工**，不是漏翻译 |
| `mmux_core/services/mmux_core/` 整个目录 | 看似"领域层" | **不是 DDD**：①无状态函数库 + 注册表，无 Entity/Aggregate/Repository；②零 odoo 导入，纯算法（schema 校验、风险分级、意图归一化）；③存在理由是**可脱离 Odoo 做单元测试**。业务数据访问 100% 留在 Odoo 层 |

### 待决策（需要人为判断，不自动改）

| # | 位置 | 问题 | 备注 |
|---|---|---|---|
| 4 | `mmux_provider_ai/models/mmux_providers.py:147` | `names()` 被调用两次，且与注册表已有校验重叠 | 轻度 DRY |
| 5 | 4 个场景模块的 `tests/` | `setUp` 与 `_make_*` helper 高度同构 | 测试代码容忍度高，可选 |

> **#3 已决策并执行**（v3.1.1）：`mmux_service.py` 按真实耦合边界拆成 7 个
> `_inherit = "mmux.service"` 的文件。详见 §三。

### 已核实为「无需处理」

| 位置 | 原因 |
|---|---|
| 6 个 `__manifest__.py` 的重复键 | Odoo **不支持** manifest 继承，重复是**语言要求的**，不是 DRY 违规。空 `"data": []` 已删 |

## 三、`mmux_service.py` 拆分（v3.1.1 已执行）

**原则：按已有的耦合边界拆，不引入新抽象。**

拆法是**同表多文件**：每个新文件都 `_inherit = "mmux.service"`，因此运行时
Odoo 把它们合并成**同一个 model**。对外 API 一个都没变，控制器、前端、
23 个 capability 全部无需改动 —— 拆的是**文件**，不是**抽象层**。

切割线不是按行数平均分的，而是先做**方法级调用依赖图**（扫描 76 个方法体内的
`self.xxx(` 引用），再按聚类边界切。结果：42 个方法是叶子、34 个存在互调，
且互调几乎全部落在簇内。

| 文件 | 职责 | 方法数 | 依赖位置 |
|---|---|---|---|
| `mmux_service.py` | 门面：只声明 model，无逻辑 | 0 | — |
| `mmux_service_infra.py` | provider 装配 / 执行守卫 / 幂等 / LLM 数据抽取 | 12 | 最底层，自身不调业务方法 |
| `mmux_service_skill.py` | 语言解析 + 技能等级 | 3 | 叶子 |
| `mmux_service_capability.py` | 权限校验 / 能力执行（唯一收口） | 7 | 只调 infra |
| `mmux_service_scenario.py` | 场景判定 + 审计落库 | 12 | 只调 infra |
| `mmux_service_loop.py` | agent 循环：scratchpad / observe / step / turn | 17 | 唯一有内部回指的簇 |
| `mmux_service_flow.py` | 任务与脚本化引导 | 21 | 单向调 loop |
| `mmux_service_plan.py` | 计划提议与执行 | 4 | 只调 infra |

**关键设计决定**：`_provider_config` 放在 `infra`，不放 `scenario` 或 `loop`。
因为它被 `parse_intent`、`transcribe`、`analyze_image`、`propose_plan` 共同使用，
放在任一业务簇都会制造反向依赖。

### 拆分带来的真实风险（已发生并已修复）

拆文件会**移动方法体，但不会移动模块级 import**。这产生了一类 AST 比对
**查不出来**的缺陷（import 在方法体之外）：`_extract_llm_data` 搬进了
`infra` 文件，但 `import json` 留在了原文件，于是运行到 `json.loads()` 抛
`NameError`，而外层 `except Exception` 把它吞了 —— 表现为"消歧静默失效"。

因此 `tools/check_standards.py` 新增**规则 10**（`no unimported module
references`），专门扫"用了 `json.` 却没 `import json`"。该规则已注入缺陷自测，
确认可捕获。

### 另一类静默失效：QWeb 模板里的裸 `&`

配合 v3.1.2 免手语音时，给 `panel.xml` / `guide.xml` 的输入框补了
`t-on-keydown="ev => ev.key === 'Enter' && go()"` —— 在编辑器里完全正常，
但 **XML 里 `&` 必须写成 `&amp;`**。三个文件因此整份解析失败，浏览器
直接拒绝加载模板，控件**静默不渲染**。

这类缺陷没有任何既有环节能拦住：JS 语法是对的，Python 是对的，单元测试
也不加载 QWeb。所以新增**规则 11**（`XML is well-formed`），对全部
13 份 XML 做一次解析。同样注入缺陷自测过：报错精确到
`panel.xml:46`，与浏览器定位一致。

> 教训是同一个：**"看起来正确"不等于"能被解析"**。凡是工具链有解析器的东西
> （Python、XML、manifest），就交给解析器去判，不要靠肉眼。

### 同一课的另外两种写法（规则 12、13）

v3.1.3 又踩到同一课，但换了两副面孔。

**面孔一：XML 注释里的 `--`。** 在模板注释里写下
`Steps that are decisions show nothing here -- captureIntent is null`
是完全自然的英文写法，但 XML 注释**不允许出现双连字符**。整份模板
同样解析失败，而报错行号落在**注释自己那一行**，很容易被误判成
"周围的标签写错了"。规则 11 能抓到，但只告诉结果不告诉原因，
所以补了**规则 12**（`XML comments are legal`）直接点名问题。

**面孔二：`.js` 文件的语法错误。** JS 打包只在**浏览器请求资源时**
由 Odoo 组装，所以一个 `.js` 语法错误对 Python 检查、单测、规范检查
**全部不可见** —— 症状只是"按钮没反应"，日志里什么都没有。补
**规则 13**（`JS is well-formed`）：剥掉 `import`/`export` 后用
`node --check` 逐文件解析（`@odoo-module` 的路径本来就不该在这里解析，
要判的是**能不能被解析**，不是能不能被加载）。

两条规则都做过缺陷注入自测。

### 版权申明必须机器可查（规则 14）

本项目版权声明人为 **jeffery9@gmail.com & geninit**，许可证 **LGPL-3**
（见根目录 `LICENSE`）。落地为三处，且由**规则 14**（`copyright headers
present`）逐一核对：

| 位置 | 要求 |
|---|---|
| 每个 `.py` 文件 | 前三行固定为 coding 声明、`# Copyright (C) 2026 jeffery9@gmail.com & geninit`、`# License LGPL-3.0 or later (...)` |
| 每个 `.js` 资产文件 | 首部块注释含同样的 Copyright / License 两行 |
| 每个 `__manifest__.py` | `author` 统一为版权人，并声明 `copyright` 键（规则 7 同步钉住键序与取值） |

写这条规则的理由和前面所有规则同源：**手写的声明会在第一个新建文件时
开始腐烂**。只写在 README 里的版权是意图，能被 linter 判 pass/fail 的
版权才是事实。规则 14 已做缺陷注入自测（删掉某文件的头行 → 精确报错）。

> 到这里，"交给解析器"这条原则已经覆盖了 Python、XML、manifest，
> 现在补上 JS。判据很统一：**如果这个文件格式有权威解析器，
> 就不要用正则去猜它是否合法。**

## 四、提交前自检清单

- [ ] 新增数据访问是否**全部**走 ORM？
- [ ] 用户可见字符串是否用 `_()` 包裹？（**例外**：`services/mmux_core/` 下的纯核心）
- [ ] 有没有引入 Repository / Entity / DTO / UseCase / Factory？
- [ ] 新增的抽象，删掉之后代码会不会**更简单**？会的话就删。
- [ ] 新增的 `sudo()` 能不能说明理由？
- [ ] manifest 键序、`_description`、导入顺序是否符合规范？
- [ ] **新建的 `.py` / `.js` 文件有没有带版权头三行？**（跑规则 14；版权人 `jeffery9@gmail.com & geninit`，LGPL-3）
- [ ] 有没有 `print()` 或裸 SQL？
- [ ] **搬动方法时，它用到的模块 import 是否也跟着搬了？**（跑规则 10）
- [ ] **模板里写条件/回调时，`&` 有没有写成 `&amp;`？**（跑规则 11）
- [ ] **注释里有没有出现 `--`？**（跑规则 12）
- [ ] **改过 `.js` 吗？改完跑过规则 13 吗？**（没有它就只能靠"按钮有没有反应"发现语法错误）
