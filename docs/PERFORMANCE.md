# 性能工作记录（v3.3）

本文档记录一次针对导览热路径（guided-flow hot path）的性能优化。它分两部分：

1. **改动**：真正修掉的缺陷与新增的缓存，以及**为什么**这么改。
2. **已排查、刻意不改**：几个看起来可疑、但经核实后**故意保持原样**的位置，连同「不改的理由」。

第 2 部分是本文档存在的主要目的。一次性能评审里，**决定「不动什么」和决定「改什么」同等重要**——否则下一个人会把同一批「可疑点」再排查一遍，或者更糟：把一个正确的设计改坏。

---

## 0. 方法论：先量，再改

本次所有结论都建立在**测量**之上，而不是直觉之上。做法是用**桩构造器**（stub builder）压测，因为这能给出确定性的数字：

| 测量项 | 结果 |
| --- | --- |
| `build_steps` × 20（桩构造器，各 4 ms） | 81.5 ms |
| `cursor_view` × 2000（60 步列表） | 466.1 ms → **0.233 ms/次** |

这个次序很关键。最初的假设是「配置文件被反复读取」是热点；一测才发现热点在**步骤列表的重复序列化与重复构建**。如果先改再测，就会去优化一个不存在的问题，同时把真正的问题留在原地。

> 一条贯穿全文的判据：**先证明确实有浪费，再动手；证明不了，就写进「刻意不改」。**

---

## 1. 改动一：`advance` 不再为了读一个游标而序列化整张列表

### 问题

`guide_advance` 处理**一个步骤**时，会对步骤列表做**三次**完整遍历：

1. 调用方为了拿到「当前步」而探一次；
2. `advance` 内部**又**调了一次 `cursor_view`，只为读一个 `step_id`；
3. 最后为了返回给前端，再渲染一次完整视图。

第 2 次是纯粹的浪费。`cursor_view` 的职责是**产出给前端的完整视图**——它会给**每一个**步骤都建一个 dict。用它来读一个 `step_id`，等于为了取一个字段而把整张表序列化了一遍。

### 改动

把「游标 = 第一个未完成的步骤」抽成一个独立、无副作用的静态辅助函数：

```python
@staticmethod
def _current_step(steps, done_ids):
    """The cursor: the first step not in `done_ids`, or None. ..."""
    done = set(done_ids or [])
    for s in steps:
        if s.step_id not in done:
            return s
    return None
```

- `cursor_view` 与 `advance` **共用**这一个实现，两者不可能再对「当前步是谁」产生分歧。
- `advance` 现在**只读对象、完全不序列化**。

### 副作用清理

`advance` 之前调用 `cursor_view` 还带来一个隐藏耦合：`cursor_view` 会写 `s.done = True`。也就是说，**读一个游标居然会改对象状态**——尽管这些对象还没走出调用栈，暂时无害，但它让 `advance` 变成有副作用的函数。现在 `advance` 没有任何副作用，这一点由测试固化：

```python
def test_advance_has_no_side_effects(self): ...
```

`cursor_view` 的**签名与返回结构完全没变**。这是硬性约束：前端依赖 `{current, steps, remaining, finished}` 四个键，任何形状变更都是破坏性变更。

---

## 2. 改动二：导览步骤列表每请求缓存一次

### 问题

`guide_start` / `guide_current` / `guide_advance` 每次都**从数据库重建**整张步骤列表。而构造器要查真实的 `stock.picking`，这是整条路径上最贵的部分。

### 改动

```python
@api.model
def _guide_steps(self, flow_id, params, skill_level):
    key = ("guide_steps", flow_id,
           json.dumps(params or {}, sort_keys=True, ensure_ascii=False),
           skill_level)
    cache = getattr(self.env, "_mmux_guide_steps", None)
    if cache is None:
        cache = {}
        self.env._mmux_guide_steps = cache
    steps = cache.get(key)
    if steps is None:
        steps = FLOW_ENGINE.build_steps(flow_id, self.env, params,
                                        skill_level=skill_level)
        cache[key] = steps
    for s in steps:
        s.done = False
    return steps
```

三个设计决定，每个都有理由：

**（a）缓存在 `self.env` 上，不是 `env.registry` 上。**
这是正确性问题，不是性能偏好。步骤列表由**某个操作员的真实记录**构建（他的调拨单、他的行）。`registry` 是**进程级共享**的——所有 worker、所有用户共用一份。把某个操作员的步骤列表放进 registry，就等于把它**显示到另一个操作员的屏幕上**。`env` 是每请求的，天然隔离。

对比：下面 §3 的 `_caps_payload` **确实**放在 `registry` 上，而且是对的。区别在于，能力清单是从**代码**派生的、不含任何用户数据——放在 registry 上不会泄露任何人的东西。

> 判据：**载荷是否含用户数据？** 含 → `env`；纯代码派生 → `registry`。

**（b）出缓存时重置 `done`。**
`GuidedStep` 是**可变且有状态**的：`cursor_view` 会写 `s.done = True`。缓存下来的列表保留着上一次请求留下的 `done` 标记。如果原样返回，第二次读就会看到一堆「已完成」的步骤。所以每次出缓存都清一遍：

```python
for s in steps:
    s.done = False
```

注意这里**不能**用 `copy.deepcopy` 之类去图省事——重置一个布尔字段就够了，而且更便宜。

**（c）缓存的键包含 `params` 与 `skill_level`。**
同一个 `flow_id` 在不同参数或不同技能等级（新手/专家）下会产出**不同的步骤列表**。只按 `flow_id` 做键会把专家的精简步骤发给新手。`json.dumps(..., sort_keys=True)` 保证参数顺序不同但内容相同的两次调用命中同一个键。

### 失效：这是正确性要求，不是优化

构造器读的是**实时**的 `picking.move_line_ids`。而 `picking_validate` 可能新增一条**欠单行**（backorder line）。如果缓存不失效，操作员会看着一份**已经不存在**的步骤列表操作。

所以写入之后**必须**失效：

```python
@api.model
def _guide_invalidate_steps(self):
    self.env._mmux_guide_steps = {}
```

**一处细节值得记下来：** 我最初的测试是**直接调用**这个失效函数的。把调用点删掉、留下这个孤儿函数，测试**照样全绿**（16/16）——这是缺陷注入抓出来的。修正方法是**驱动真实的 `guide_advance` 路径**（而不是直接调辅助函数），这样删掉调用点就会真的挂。现在另加了一条**源码级**断言，检查 `guide_advance` 里确实存在这个调用点。

### 效果

| | 改动前 | 改动后 |
| --- | --- | --- |
| 单次 `advance` 的完整序列化次数 | 3 | **1** |
| 构造器调用次数（start → current） | 2 | **1** |

---

## 3. 改动三：`_caps_payload` 的 registry 级缓存（指纹键）

能力清单在**每个 LLM 提示词**里都要被序列化一次。它完全由代码派生（`CAPABILITY_REGISTRY`），不含用户数据，因此放在 `registry` 上是安全的。

```python
@api.model
def _caps_payload(self) -> list:
    fingerprint = tuple(sorted(
        c.capability_id for c in CAPABILITY_REGISTRY.all() if c.enabled))
    cached = getattr(self.env.registry, "_mmux_caps_payload", None)
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    payload = [c.to_public_dict()
               for c in CAPABILITY_REGISTRY.all() if c.enabled]
    self.env.registry._mmux_caps_payload = (fingerprint, payload)
    return payload
```

**为什么用「指纹」而不是「长度」？**

这是缺陷注入逼出来的一个真实教训。我最初用 `len(...)` 做键，注入「改成长度键」的缺陷后，测试**没有报错**（8/8 通过）。分析后发现：长度键在**简单开关**场景下其实是对的——禁用 1 个能力，数量就变了，缓存自然失效。

长度键**只在「数量不变、成员变了」时失效**。例如：禁用能力 A、同时启用能力 B——数量相同，内容不同。

修正方法：把测试补上这个**等数量换成员**的用例。补上之后，那个注入**立刻被抓到**（2 条失败）。这说明测试的**区分力**比测试的**数量**重要得多。

---

## 4. 已排查、刻意不改

以下五点都**曾经看起来像是性能问题**。逐一核实后，我决定**保持原样**。理由按「证据强度」排列。

### 4.1 `_provider_config`：**不要**加缓存 ⚠️ 最重要

**可疑点：** `_provider_config()` 每次调用读 **17 个** `ir.config_parameter` 键。看起来是个现成的缓存目标。

**结论：不但不该加缓存，加了就是引入 bug。**

Odoo 19 的 `ir.config_parameter.get_param` **本身就已经是 ORM 缓存的**（源码 `odoo/addons/base/models/ir_config_parameter.py`）：

```python
@api.model
def get_param(self, key, default=False):
    self.browse().check_access('read')
    return self._get_param(key) or default

@api.model
@ormcache('key', cache='stable')
def _get_param(self, key):
    ...
```

而且写入侧的失效是**自动**的——`create` / `write` / `unlink` 全部会清缓存：

```python
def write(self, vals):
    ...
    self.env.registry.clear_cache('stable')
    return super().write(vals)
```

所以：

- **性能上**：这 17 次读大多命中内存缓存，本来就不贵。再包一层是我自己的重复劳动。
- **正确性上**：如果我在外面套一层自己的缓存，就**绕开了**上面这套自动失效。操作员在设置页改了 API Key，我的缓存不会知道——他会一直用旧配置，直到重启。**这是一条真实的「设置改不动」bug。**

> **把这条写进文档的首要目的**：下一个人看到 17 次连续读取，很可能「顺手优化」掉，然后引入这条 bug。这里的结论是**明确的反向指令**。

顺带一提：`_provider_config` 的注释里已经写了它**故意**直接读取、不绕道设置模型，理由是避免出现两条可能互相矛盾的读取路径。这个判断是对的，予以保留。

### 4.2 `idempotency_key` 索引：**已经存在**

**可疑点：** `Log.search([("idempotency_key", "=", key)])` 在每个能力执行、任务上报、导览推进里都会跑，而且这是安全检查路径。

**结论：无需改动。**

```
mmux_core/models/mmux_interaction_log.py:35:
    idempotency_key = fields.Char("Idempotency key", index=True)
```

`index=True` 已经有了。这些是按索引列的等值查询，正是索引最擅长的形状。

**我没有实测查询计划**——因为不具备条件：本沙箱没有 Odoo 运行时，也无法构造出有意义的数据量。所以这里的结论是「**按构造无需改动**」，而不是「实测证明无需改动」。这个区别应当如实保留，不要粉饰。

### 4.3 `_attach_photos`：**故意**不做成独立上传路径

**可疑点：** 照片以 base64 通过 execute 请求体传给后端，看起来笨重；一个独立的上传端点似乎更「高效」。

**结论：现状更好，改成独立端点会**降低**系统质量。**

该方法自己的 docstring 已经把理由写得很清楚，而且理由是对的：

- 它**在能力执行器内部**被调用，因此运行在**同一个事务**里，受**同一个 `ExecutionGuard`** 的访问检查约束。另开一条上传路由，会在 ACL、记录规则、幂等、审计链路之外**制造第二个写入面**。
- **先创建、后挂载，同事务。** 若 `message_post` 抛异常，整个操作回滚，因此不会留下指向一条从未存在过的消息的孤儿附件。

换句话说：这里的「低效」是在**买**事务一致性与统一鉴权。把它「优化」掉，等于用一个安全问题换一个尚不存在的性能问题。**不改。**

### 4.4 前端请求周期：**故意保持无状态**

**可疑点：** 每个操作员动作都会发一次 JSON-RPC（`/mmux/guide/advance` 等）。看起来可以减少往返。

**结论：当前形状是对的；本轮唯一的改动是让每次往返**更便宜**，而不是减少往返次数。**

先看事实：前端（`mmux_core/static/src/services/mmux_api.js`）对每个动作走一次 `jsonrpc`；控制器（`mmux_core/controllers/mmux_controllers.py`）是**薄透传**：

```python
@http.route("/mmux/guide/advance", type="json", auth="user")
def guide_advance(self, guide_key=None, step_id=None, input_data=None,
                  skip=False):
    ...
    return request.env["mmux.service"].guide_advance(
        guide_key, step_id, input_data, skip=bool(skip))
```

**为什么不引入会话状态去减少往返：** 这个平台的使用场景是**仓库/外勤操作员拿着手机工作**。会话可能中断、电话可能切后台、网络会抖。无状态请求意味着**任何一次调用都能独立重放**——这正是 §4.2 那套幂等键机制能够成立的前提。用服务端会话换掉它，就得让会话在断网时也保持权威，复杂度上升而可靠性下降。

而且：这一层本来就已经被**请求内**缓存（§2）和**registry 级**缓存（§3）覆盖了。**每次往返现在是 1 次序列化、1 次构造器调用**。真正的浪费在往返**内部**，已经拿掉了。

### 4.5 `FLOW_ENGINE.get`：**已经是** O(1) 内存查询

**可疑点：** `get` 在每次导览流程里都会被调用，看起来像一个查询点。

**结论：这是 dict 查询，零 I/O、O(1)，没有任何可优化的东西。**

```python
def get(self, flow_id) -> Optional[TaskFlow]:
    return self._flows.get(flow_id)
```

注册的流程一共 **4 个**（三个业务场景各一 + 核心一）——量级上根本不值得引入索引结构。

---

## 5. 回归与验证

| 检查项 | 结果 |
| --- | --- |
| `python3 -m pytest tests_pure -q` | **540 passed**，1 skipped，184 subtests |
| `python3 tools/check_standards.py` | **13 条规则，no problems found** |
| `verify_caps_cache.py` | 10/10 |
| `verify_guide_cache.py` | 20/20 |
| `verify_agent_service.py` | 68/68 |
| `verify_agent_frontend.py` | 106/106 |
| `verify_mmux_settings.py` | 53/53 |
| `verify_photo_evidence.py` | 9/9 |
| `verify_scenario_context.py` | 48/48 |
| `verify_voice_hands_free.py` | 74/74 |
| `verify_wiring.py` | INTEGRITY PROBLEMS: none |
| `verify_scenario_odoo_tests.py` | 34/34 |

### 新增的永久性性能回归守卫

`tests_pure/test_flow_perf.py` —— **按调用计数**断言，不按墙钟时间。

理由：墙钟断言在 CI 上会抖（一台繁忙的机器能把 0.2 ms 变成 2 ms，测试因为与代码无关的理由变红）。而这里真正退化的是**重复做功**，那就数「做功次数」——精确、与机器无关、正是未来一次粗心改动会改变的量。

```
TestNoRedundantSerialisation   —— advance 序列化次数必须为 0
TestBuilderCallCount           —— advance 不得重跑构造器
TestSerialisationVolume        —— 以比值表达「游标读是 O(1)、视图是 O(n)」
TestWallClockOptional          —— 仅 MMUX_PERF=1 时运行的计时探针（默认跳过）
```

**一个自己踩过的坑，写进文件里防止后人重蹈：** 最初的计数用的是**按实例**打补丁（`s.to_dict = wrapper`）。`GuidedStep` 有 `__slots__`，这个写法**静默失效**，结果数到的是桩而不是真实代码路径，产生了**假失败**。现在统一改为**按类**打补丁（`GuidedStep.to_dict = counting`），并在 `finally:` 里还原。

同时记下第二个坑：`cursor_view` 对 60 步列表的序列化次数是 **61** 而不是 60——它会为 `"current"` 键**额外**序列化一次当前步。这个 `n+1` 是前端契约固有的，不是浪费。断言必须写 `n + 1`，或直接断**比值**。

### 缺陷注入（这是守卫「有效」的唯一证据）

测试全绿**不能证明**守卫有效——它可能只是在数一个自己造的桩。所以每条优化都**故意把缺陷重新注入**，确认守卫会变红：

| 注入的缺陷 | 结果 |
| --- | --- |
| `advance` 回退为经由 `cursor_view` 读游标 | **4 条失败**（`test_advance_serialises_nothing` 等） |
| `cursor_view` 对当前步多序列化一次 | **4 条失败**（含线格式契约检查） |

注入完成后已核对 `flow.py` 与注入前的备份**逐字节一致**。

### 本轮注入抓出的三个真实问题（都在我**刚写好的测试**里）

这一节的目的是留痕：产出「全绿」很容易，但下面这三条只有注入才能发现。

1. **长度键 vs 指纹键无法区分**（§3）。注入未被捕获（8/8）。补齐「等数量换成员」用例后捕获。
2. **直接调用 vs 调用点**（§2）。我的测试直接调了失效函数，因此**删掉真实调用点**（16/16）不被发现。改为驱动真实 `guide_advance` 路径后捕获。
3. **我自己的断言写错了**：我断言「写入后 `guide_current` 会重建」，但 `guide_advance` 已经在返回前重建并缓存了**写入后**的列表，所以「0 次重建」才是**正确**行为。是断言错，不是代码错。已改为断言「写入后确实发生过一次重建，且新列表被缓存」。

> 记忆点：三次注入，抓到三个**测试**的问题，代码一次都没错。这正是注入的价值——它检验的是守卫，不是实现。

### 一处如实说明的验证缺口

新增的 Odoo 测试文件 `mmux_core/tests/test_mmux_guide_cache.py`（6 条）**无法在本沙箱真实运行**：它需要真实的 `stock.picking` / `product.product` / `action_assign` 语义，而沙箱里没有 Odoo 运行时。

已经做到的静态校验：

- `py_compile` 通过；
- 未绑定名分析（即本会话早些时候修掉的那类 `NameError`）**零命中**；
- 已在 `mmux_core/tests/__init__.py:6` **注册**。

同样的这些不变量，已经由纯逻辑模拟器 `/tmp/verify_guide_cache.py`（**20/20**，且经过注入验证）覆盖。但**「已在真机跑过」这句话，本轮说不了**——如实记录，不做粉饰。

---

## 6. 变更清单

| 文件 | 变更 |
| --- | --- |
| `mmux_core/services/mmux_core/flow.py` | 新增 `_current_step`；`cursor_view` 委托它；`advance` 改为无副作用且零序列化 |
| `mmux_core/models/mmux_service_flow.py` | 新增 `_guide_steps` / `_guide_invalidate_steps`；重写 `guide_advance`；补 `fields` 导入（P0 崩溃） |
| `mmux_core/models/mmux_service_infra.py` | `_caps_payload` 改为指纹键 registry 缓存 |
| `mmux_core/models/mmux_service_loop.py` | `_build_llm_prompt` 改用 `_caps_payload()`；移除未用导入 |
| `mmux_core/models/mmux_service_plan.py` | 同上改用 `_caps_payload()` |
| `tools/check_standards.py` | 规则 10 加固；`used` 收窄为属性基名 |
| `tests_pure/test_flow.py` | 新增 `TestCursorSharedHelper`（6 条） |
| `tests_pure/test_flow_perf.py` | **新增**（9 条 + 1 条 opt-in） |
| `tests_pure/test_module_names.py` | **新增**（4 条） |
| `mmux_core/tests/test_mmux_guide_cache.py` | **新增**（6 条，需真实 Odoo） |

### 顺带修掉的一个 P0 崩溃

加固规则 10 的过程中，它**顺带**标出了 `mmux_service_flow.py`：该行调用 `fields.Datetime.now()`，但 `fields` **从未被导入**（文件头只导入了 `api, models`）。

用 AST 核实：`fields` 被**使用**，却从未被**绑定**。这意味着**每一次任务成功完成**都会抛 `NameError`——这是 100% 触发的线上崩溃，与本次性能工作无关，属于排查途中的意外收获。

修法是补上导入。随后把这个检测器**固化为测试**（`tests_pure/test_module_names.py`），并再注入一次确认能抓到。

需要说明的是：加固后的规则 10 **立刻产生了一个误报**——`schema.py` 里 `ParamSchema.__init__(self, fields)` 把 `fields` 绑定为**参数名**。诊断方式是把 AST 打出来（是 `Name` 形式，没有 `Attribute` 形式），据此把匹配范围**收窄为属性基名**（只匹配 `X.y` 里的 `X`，不匹配光秃秃的 `Name`）。修完后该误报消失，`fields` / `tools` / `http` 的真实使用点各自都确认了有正确导入。
