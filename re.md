# CAPSLOCK

CapsLock 不是一个简单的“LLM + tools”循环，而是一个面向本地代码工作区、强调权限治理、可恢复执行和持久化审计的异步 Agent Runtime。当前实现可以概括为：六边形分层架构 + 持久化工作流状态机 + 受治理的 Tool Loop。

```text
CLI / Inline TUI / Fullscreen TUI / JSONL
                    |
                    v
          WorkspaceApplication
             组合与资源生命周期
                    |
                    v
              AgentSession
        稳定门面、单 Session 编排
                    |
       +------------+-------------+
       |            |             |
 Context Builder  RunGovernor   ModelRouter
       |            |             |
       +------------+-------------+
                    |
                    v
                ToolLoop
          Model <-> Tool 多轮循环
                    |
                    v
    ToolRuntime + Middleware Pipeline
       |        |          |
   Permission  Plan     Schema/Policy
       |
       v
 ActionCoordinator / Tool implementation
       |
       v
 SQLite Journal + Events + Artifacts
```

## 1. 分层与依赖方向

CapsLock 大致分为以下层次：

| 层次 | 主要目录 | 职责 |
|---|---|---|
| 表现层 | `capslock/cli/` | CLI 参数、TUI、斜杠命令、JSONL 输出 |
| 组合层 | `bootstrap.py`、`composition/` | 创建数据库、模型、工具、MCP、LSP、插件、协作服务 |
| 运行时层 | `runtime/` | 上下文、模型循环、事件流、预算、恢复执行 |
| 应用层 | `application/` | 工作流事务、Action 协调和用例服务 |
| 领域层 | `domain/` | 状态、实体、枚举和状态转换规则 |
| 端口层 | `ports/` | Runtime 依赖的抽象接口 |
| 基础设施层 | `storage/`、`mcp/`、`lsp/`、`shell/`、`plugins/` | SQLite、外部集成、进程与沙箱实现 |

最重要的依赖约束是：`runtime` 和 `tooling` 只依赖领域对象与 Port，不直接依赖 SQLite、CLI 或应用实现。这个约束不仅写在[架构文档](./docs/architecture.md:1)中，也由[边界测试](./tests/test_boundaries.py:159)持续验证。

## 2. 组合根：WorkspaceApplication

所有具体实现都在 [`WorkspaceApplication.open()`](./capslock/bootstrap.py:98) 中组装。它依次负责：

- 打开 workspace 数据库和独立的 memory 数据库；
- 创建或恢复 Session；
- 加载权限模式、计划状态、Skills 和项目指令；
- 初始化 MCP、LSP、插件进程、Shell 进程管理器；
- 构造 Tool Runtime、模型 Router、MemoryService 和 ActionFactory；
- 最后把这些依赖显式注入 `AgentSession`。

初始化使用 `AsyncExitStack` 管理半初始化失败时的资源回滚；正常退出时，[`close()`](./capslock/bootstrap.py:426) 按顺序关闭进程、MCP、LSP、插件、IDE bridge、模型客户端、记忆服务和数据库。

因此 `bootstrap.py` 是依赖图中唯一应该“知道所有具体实现”的地方。

## 3. AgentSession：稳定运行时门面

[`AgentSession`](./capslock/runtime/agent.py:118) 是 Agent 的核心公开入口，但它本身主要承担 Facade 和编排职责。内部进一步组合了：

- `ContextBudgetManager`：构建和压缩上下文；
- `ToolLoop`：模型与工具之间的多轮循环；
- `RunOrchestrator`：创建 Run、Governor 和模型会话；
- `RunFinalizer`：统一完成、失败、取消和用量结算；
- `SessionAdministration`：队列及 Session 管理；
- `PermissionRequestService`、`PlanRequestService`：处理可恢复请求；
- `RunExecutionCoordinator`：运行与恢复入口。

`RunEngine` 使用一个异步锁保证同一 Session 的前台请求串行执行，并通过队列把事件暴露为 `AsyncIterator`，见 [`RunEngine.run_stream()`](./capslock/runtime/engine.py:46)。所以 UI、`capslock exec` 和 JSONL 消费的是同一条事件流，而不是不同的 Agent 实现。

## 4. 一次请求的完整执行时序

一轮请求主要经过以下步骤：

1. `AgentSession.run_stream()` 把 `RunRequest` 交给 `RunEngine`。
2. `RunOrchestrator.start()` 创建或恢复 WorkItem/Run，初始化预算 Governor 和绑定当前 Run 的模型会话，见[启动逻辑](./capslock/runtime/run_support.py:56)。
3. 加载核心指令、运行时控制、项目 `AGENTS.md/CAPSLOCK.md` 和 Skill catalog。
4. `ContextBudgetManager` 合并历史消息、显式附件、记忆召回和已有 compaction。
5. `ToolLoop` 请求模型；若模型返回最终文本则结束，否则执行工具调用。
6. 工具结果作为 `role=tool` 消息重新放回上下文，继续下一轮模型调用。
7. 最终文本经过 citation resolver，校验 evidence、source 和 memory 引用。
8. 保存用户/助手消息、用量、引用和终止状态。
9. 终止事件可能是 `completed`、`waiting_approval`、`waiting_input`、`failed`、`cancelled` 或 `stopped`。
10. 正常完成且没有待审批内容时，后台触发记忆候选抽取和维护。

核心循环位于 [`ToolLoop.run()`](./capslock/runtime/tool_loop.py:413)。只读、无副作用且声明为并发安全的工具会有限并发执行；写入、外部调用、上下文变更和交互型工具保持串行，判定规则见 [`_execution_batches()`](./capslock/runtime/tool_loop.py:699)。

## 5. Tool Runtime 与 Action 的区别

Tool 是模型可见的中立协议。每个 [`ToolContract`](./capslock/tooling/contracts.py:172) 定义名称、版本、输入输出 JSON Schema、结果大小、是否延迟发现以及 Plan Mode 可见性；运行时策略则声明只读性、并发安全性、破坏性、外部副作用、开放世界内容和中断行为。

工具执行流水线是：

```text
参数规范化 -> JSON Schema 校验 -> 工具级 validate
-> 动态策略解析 -> Plan Mode 边界 -> 权限判定
-> timeout / interrupt 策略 -> execute
-> 输出 Schema 校验 -> 结果投递与审计
```

具体实现见 [`ToolExecutor.invoke()`](./capslock/tooling/executor.py:49)。

Action 则是需要持久化治理的副作用操作，例如文件修改、Shell、Web、MCP、worktree 和凭据访问。它有独立的：

```text
pending -> approved -> running -> completed / failed / cancelled
pending -> rejected
```

[`ActionCoordinator`](./capslock/application/action_system/core.py:82) 负责风险、审批、状态和审计；各 Handler 负责具体执行与重新校验。文件 Action 还保存哈希前置条件并支持反向操作。

## 6. 权限内核

权限不是写在 Prompt 里的建议，而是 Tool Runtime 的强制中间件。其决策顺序是：

1. 参数规范化和 hard safety boundary；
2. 显式 `deny`；
3. 显式 `ask`；
4. 显式 `allow`；
5. 一次性、与 invocation 和参数摘要绑定的授权；
6. 当前权限模式的默认行为。

实现见 [`PermissionEngine.decide()`](./capslock/tooling/permission_policy/engine.py:84) 和纯规则选择器 [`PermissionDecisionEngine`](./capslock/tooling/permission_policy/decision.py:14)。

普通 Tool 需要审批时会生成 `ToolPause`；Invocation、请求参数 SHA-256、恢复数据和消息 checkpoint 会写入 journal，见 [`InvocationPreparer`](./capslock/runtime/tool_invocation.py:48)。进程退出后可以重新组合应用并从稳定 step 恢复，而不依赖原进程内的 Future。

## 7. Prompt、上下文与信任边界

CapsLock 明确区分四类 Prompt 来源：核心策略、运行时控制、用户级仓库指令、不可信数据。只有前两类进入 system role；项目指令是较低优先级用户指令；Skills、记忆、附件、压缩摘要和外部结果都包装为不可信 JSON，见 [`PromptSection.render()`](./capslock/runtime/prompts.py:36)。

上下文管理还包括：

- 历史与记忆召回并发加载；
- token 预算估算和 provider 实际用量校准；
- 旧大工具结果的 micro-compaction；
- 结构化历史摘要及摘要缓存；
- 记忆版本变化后使旧 compaction 失效；
- checkpoint 恢复时重新加载当前可信 Prompt，而不是盲目复用旧 system message。

开放世界工具结果会进行 prompt-injection 风险检测；可疑内容被隔离到 artifact store，只把摘要、哈希和读取句柄返回模型，相关逻辑在[工具调用准备器](./capslock/runtime/tool_invocation.py:217)。

## 8. 模型路由、预算与防循环

模型接口是 provider-neutral 的 `ChatModel`/`ModelRunSession`；Provider 传输只使用 OpenAI Responses API，Adapter 负责 `input`、`text.format`、function call 与流式事件之间的转换，见 [`AsyncOpenAIResponsesModel`](./capslock/runtime/model.py:162)。这里的 `ChatModel` 只是内部协议名称，不代表兼容 Chat Completions API。

[`ModelRouter`](./capslock/runtime/routing.py:171) 按 `reasoning`、`fast` 等角色选择模型 Profile，支持重试和 fallback，但禁止 fallback 改变 provider data policy；每次选择、调用、错误、token 和费用都会审计。

[`RunGovernor`](./capslock/runtime/governance.py:23) 同时控制工具轮次、调用次数、时长、token 和费用，还会对“同一失败调用反复执行”“连续重复”和周期性工具调用做确定性检测。

## 9. 持久化状态模型

CapsLock 把 Agent 执行拆成多个持久化层级：

- `Session`：对话和配置边界；
- `WorkItem`：可排队的用户意图；
- `Run`：某次执行或恢复尝试；
- `RunStep`：模型、工具、审批步骤及 checkpoint；
- `ToolInvocation`：单个工具调用的完整生命周期；
- `Action`：可审批、可审计的副作用；
- `AgentEvent`：对 UI/JSONL 暴露的有序事件。

状态转换由领域层定义，例如 [`WorkItemStatus`](./capslock/domain/workflow.py:10)，实际跨表变更由 `WorkflowUnitOfWork` 事务化执行。

workspace 状态和全局 memory 使用两个独立 SQLite 数据库。前者持有会话、Run、工具、权限、计划、协作和审计；后者持有记忆、候选、关系、embedding 和维护任务。数据库采用 WAL、单写事务锁和只读连接池，见 [`AsyncDatabase`](./capslock/storage/async_database.py:19)。

## 10. 记忆、Plan Mode 与子 Agent

MemoryService 是多个专用服务的 Facade，包括召回、候选抽取、embedding、后台任务、设置和导入导出。记忆不会直接作为可信指令，而是作为带来源的检索数据注入；只有完成且来源可验证的 Run 才参与自动抽取。

Plan Mode 通过 `PlanningBoundaryMiddleware` 叠加在权限系统之上。激活时模型只能看到计划控制工具和本地只读工具；即便基础权限是 `full_access`，也不能写文件、执行外部副作用或启动子 Agent。

多 Agent 不是共享上下文的线程池。父 Agent 为每个子任务创建带路径白名单的私有 workspace snapshot；子 Agent 再次调用 `WorkspaceApplication.open(child_mode=True)`，拥有独立 Session、数据库和受限工具目录。协作服务限制为一层委派，并通过 semaphore 控制并发，见 [`CollaborationService.delegate()`](./capslock/collaboration/service.py:91)。子输出还必须经过 schema、artifact、evidence 和 check 验证后，才能返回父 Agent或发布到父工作区。

总体来看，CapsLock 的核心架构思想是：让模型负责提出下一步意图，让确定性的 Runtime 负责状态、权限、预算、恢复、证据和副作用。这使它更接近一个“以 LLM 为决策组件的持久化工作流引擎”，而不是一个直接给模型暴露 Python 函数的聊天机器人。

# Q & A

## 项目、框架与 Agent 架构

### 如果要从0到1设计一个agent，具体要实现哪些模块？
我会从入口层，agent服务层，上下文系统，模型系统，工具系统，权限系统，记忆系统，事件系统，工作流和持久化这几方面入手。
入口层负责前端的agent和用户或者其他系统的交互，典型的入口包括CLI/HTTP API/IDE插件等。主要职责是负责解析输入，创建run请求，展示agent运行状态或者用户提问等。这一层只依赖agent的服务层，不直接调用工具，数据库或者模型SDK。
agent服务层负责让任务完整运行起来。包括session的创建，run的创建或取消，模型和工具调用的循环等。它负责控制整个agent运行的流程。
上下文的话决定“模型这一个run模型能够看到什么”，包含核心的agent指令，会话历史，当前用户的请求，附件，记忆召回结果，工具schema，历史的上下文（压缩摘要），token预算等。记忆系统负责“找出可能相关的记忆”，上下文系统负责“是否以及如何把它放进 Prompt”。
模型系统负责隔离模型提供方的差异，让agent独立起来。主要负责统一的model接口，流式响应，模型reasoning和最终回答的分离，重试和fallback，token成本和延迟统计等。模型系统只负责一次的模型调用，不是整个agentloop。
工具系统负责把外部能力以统一、安全的方式暴露给模型，包括工具的名称、描述、输入输出schema、注册、搜索、以及统一执行管线等。
权限系统算是工具系统的拓展，用来决定操作是否可以执行。包含工具调用参数规范化，用户、工作区、session的规则，allow/deny/ask的决策等。权限规则是决定某个调用是否可以执行，工具系统只是负责工具的作用域声明（只读，外部副作用，并发性）。工具声明策略，权限系统基于策略、参数和当前模式作出决定。
记忆系统负责处理跨session、跨工作区的用户记忆处理。包括记忆的写入、记忆的查询、记忆的保存和修订等。
事件系统将Agent执行过程中发生的事情，转换成统一、有序、可消费的事件，并实时发送给入口层、日志和审计模块。主要是用于agent的测试、性能跟踪、审计和调试回放等。
工作流和持久化负责管理 Agent 的“执行状态”和“可恢复事实”。Agent 服务层决定下一步做什么；工作流系统保证执行过程按合法状态推进；持久化系统保证进程退出后这些状态仍然存在。工作流系统负责定义和执行agent的状态机（比如queue，running、completed等状态），管理执行的层级、处理暂停和恢复、管理队列和并发、统一的完成和错误处理。持久化系统负责上述涉及到的状态保存下来，包括会话历史，checkpoint等。

### Agent设计中你觉得最重要的部分是什么
（执行内核）agent设计我觉得最重要的是建立模型的决策和确定性执行的边界。模型只能负责提出意图（读取这个文件，修改这段代码，运行测试等）。真正决定“是否允许、如何执行、执行到哪一步、失败后怎么办”的必须是确定性的 Runtime。
模型能力决定 Agent 的上限，但执行内核决定 Agent 是否可靠。模型不够强，通常表现为分析不够深入，需要更多工具轮次，回答质量不高。但是执行的内核有问题时，就可能产生：未经批准修改甚至删除文件，工具无限调用，重复执行有副作用的操作，甚至无法解释agent到底执行了什么。
最关键的四条原则：模型的输出永远是提案，权限必须在执行层强制实施、状态必须可恢复、“执行过”必须有确定性证据。
第二重要的是上下文系统，执行内核解决“Agent 能否可靠做事”，上下文系统解决“Agent 是否理解正确”。很多所谓的“模型不稳定”，实际是上下文来源混乱、历史裁剪错误或工具结果污染造成的。
所以我对 Agent 架构最核心的判断是：不要把模型当作程序本身，而要把它当作一个不完全可信、概率性的决策组件。Agent Runtime 才是真正负责执行、安全、状态和事实的系统。

### 了解主流的Agent设计吗？
1. **Tool Calling**
最简单的agent模式，用户发出请求，模型判断是否调用工具，工具结果返回模型，模型输出答案
2. **ReAct**
最典型的运行模式，遇到问题时，不是一次性给出答案，而是思考需要什么信息，行动去获取（如使用工具），观察结果，然后基于新信息再次思考，如此循环，直到问题解决。
代码 Agent、研究 Agent 和诊断 Agent通常都以这种循环为基础。它的关键不是 Prompt，而是 Runtime 必须控制：最大工具轮数；最大调用次数；token 和费用；重复调用检测；工具并发；超时和取消；权限与副作用。
3. **Plan-and-Execute**
是一种旨在解决复杂、多步骤任务的AI Agent架构范式。它的核心思想是将“战略规划”与“战术执行”进行解耦，即Agent在执行任何操作前，先制定一个完整的、多步行动计划，然后再按照计划逐步执行。
4. **Workflow 或 Graph Agent**
这类架构把 Agent 表达成状态图，将任务分解为一系列预定义的、按固定顺序执行的步骤，流程的每一步、每个分支都由代码预先硬编码好。
其优势是行为可预测、容易测试和恢复。缺点是开放式任务适配能力弱。
5. **Multi-agent**
核心思想是通过多个专用Agent的协作，来解决单一Agent难以胜任的复杂问题。
Supervisor模式：一个主管Agent统一调度，子Agent之间不直接沟通。流程清晰可控，但主管可能成为瓶颈
Generator-Verifier（生成-验证模式）：校对员。一个Agent生成内容，另一个负责验证和修正，形成反馈循环，适合对输出质量要求极高的场景
Network（网状协作模式）：去中心化的“头脑风暴”。所有Agent点对点自由交流，灵活性极高，但易失控

### 你认为当前agent在复杂业务中的场景中落地最容易被低估的挑战是什么？如何应对？
我认为最容易被低估的挑战不是模型“够不够聪明”，而是：如何让概率性的模型决策，在复杂、并发、长时间运行的业务系统中产生一致、可追责、可恢复的确定性结果。

复杂业务真正困难的通常不是正常路径，而是操作执行到一半、外部系统状态不明确、数据发生并发变化时，Agent 应该怎么办。

比如一个客服agent，执行用户的退款，在agent调用支付接口时请求超时，此时无法直接判断退款的执行状态，简单的agent loop模型会选择再试一次，但是可能会造成重复退款。

如何解决？模型只能提出业务意图，不能直接决定最终业务意图；副作用必须要有明确的执行语义，不能单纯的分为成功和失败；使用持久化工作流

复杂业务 Agent 的工程重点不应是让模型获得更多自主权，而应是把自主权拆成可验证的小决策，并通过类型化工具、持久化工作流、幂等操作、状态复验和人工接管，将每一步限制在明确的业务边界内。

## 意图识别、Prompt 与推理模式

### capslock的意图识别模块有实现吗，是怎么实现的
CapsLock 当前没有独立、统一的“自然语言意图识别模块”，用户意图主要通过确定性语法路由+模型在loop中隐式识别完成。

对于明确的控制指令（斜杠命令、skill显示调用），capslock不会让模型猜测，而是直接解析。

对于用户普通输入的自然语言，会由模型自身隐式理解，直接进入上下文的构建和tool loop，模型选择了什么样的工具，就隐式表达了对于用户意图的理解。

### 系统是否用到ReAct模式
CapsLock 使用了 ReAct 思想，基于模型原生 Function Calling 实现的工程化 ReAct Tool Loop。源码没有把它命名为 ReActAgent，核心实现叫 ToolLoop。

```text
用户请求 + 上下文
        │
        ▼
Reason：模型推理
        │
        ├─ 无工具调用 → 输出最终答案并结束
        │
        └─ 产生 tool_calls
                 │
                 ▼
Action：执行工具
                 │
                 ▼
Observation：tool result 写回 messages
                 │
                 └────────→ 再次调用模型
```

**reason**：模型会分析当前状态（系统prompt，用户请求，前几轮的tool result等）

**action**：模型选择并调用工具（如果模型返回tool call的模版，则会先把工具调用写入消息历史，再调用执行其执行）

**observation**：工具执行完成后，结果回座位标准的role=tool消息写回，然后循环进入下一轮

如果模型不再返回工具调用指令，那么capslock就会将模型的content作为最终答案返回。

capslock也支持一轮多个action。

### prompt常见结构，capslock的prompt构建

prompt常见的结构包含：角色、目标、背景、约束、*流程、输出、*示例、验收。
agent的system prompt会额外关注：身份与目标、工具使用规则、权限与信任边界、任务规划与执行策略、上下文和记忆的使用规则、错误暂停以及终止条件、最终回答规范。

capslock采用的prompt构建方式是：
```text
静态核心策略+运行时强约束+项目级指令+动态上下文+会话历史+当前用户请求+工具schema
```

**1.静态核心指令**：硬编码在代码中的字符串，包含身份/文件策略/工具策略/交互策略/任务策略/规划策略/动态工具/真实性/信任边检/引用规则/输出风格。

**2.PromptBundle：结构化组装**：
```python
@dataclass(frozen=True)
class PromptSection:
    name: str
    source: str
    trust: PromptTrust
    content: str
    summary: str = ""
```
capslock没有直接使用字符串拼接，而是定义了一个promptseciton，每一个prompt都携带name、source、trust、content、summary，这样runtime可以根据source来决定内容应该进入什么消息角色。

prompt的组装顺序是：
```python
bundle = PromptBundle.core(self.core_instructions)

bundle += runtime_controls
bundle += repository_instructions
bundle += skill_catalog
```
runtime controls:runtime_controls 也是 system 消息，来源通常是 Runtime，而不是项目文件。它适合承载不能被用户或仓库内容覆盖的约束，例如：
子 Agent 只能访问哪些路径；子 Agent 可使用哪些工具；预算限制；验证要求；
子 Agent 会使用独立的核心 Prompt

repository instructions:capslock通过instructionloader加载工作区及目录级的AGENTS.md或者CAPSLOCK.md。加载过程有明确限制：单文件最多 40 KiB；总预算最多约 12,000 tokens；拒绝符号链接；拒绝 @include；超出预算时保留优先级更高的指令；为加载结果计算 digest。加载结果是USER_INSTRUCTION，不是核心系统策略。

skill catalog：Skill Registry 只把 Skill 的名称和 description 放进初始上下文。完整 SKILL.md 不会全部进入基础 Prompt。用户显式调用 $skill-name 后，才会加载对应 Skill 内容，并作为不可信数据 Section 加入。

**3.四级信任模型**：

CapsLock 定义了四种信任等级：corepolicy、runtimecontrol、user instruction、untrusteddata。前两个角色为system，后两个为user。

**4.contextbuilder加入动态上下文**：
基础 PromptBundle 构建完成后，capslock会继续添加与当前问题相关的
1. Memory；
2. 用户显式引用的文件或 IDE 附件；
3. 历史压缩摘要；
4. 未被压缩的会话历史；
5. 当前用户问题。
最终消息结构类似：
```python
messages = [
    {"role": "system", "content": CORE_INSTRUCTIONS},
    {"role": "system", "content": RUNTIME_CONTROLS},

    {"role": "user", "content": REPOSITORY_INSTRUCTIONS_WRAPPER},
    {"role": "user", "content": SKILL_CATALOG_WRAPPER},
    {"role": "user", "content": MEMORY_WRAPPER},
    {"role": "user", "content": ATTACHMENT_WRAPPER},
    {"role": "user", "content": COMPACTION_SUMMARY_WRAPPER},

    *conversation_history,

    {"role": "user", "content": CURRENT_QUESTION},
]
```
不是每一项都会存在。

### prompt层面有哪些优化手段？提示词模版是怎么设计和迭代的？怎么判断一个模板是真的变好了？

Prompt 优化不是单纯“润色措辞”，而是在任务成功率、安全性、稳定性、成本和延迟之间做可验证的工程优化。

**prompt层面的优化手段**

*1、明确任务契约*

一个可靠的agent prompt通常明确：
```text
身份：你是谁
目标：最终要完成什么
范围：允许处理哪些对象
约束：哪些行为禁止
工作流：什么时候搜索、读取、修改、测试
工具规则：什么场景调用什么工具
证据规则：什么结论需要证据
交互规则：什么时候询问用户
终止条件：什么时候算完成
输出契约：最终答案的格式
```

*2.分层设计*

capslock不会把所有的内容都拼成一个字符串当做prompt输入。capslock将prompt拆成：
```text
Core Policy          静态、不允许覆盖
Runtime Controls     本次运行的能力和预算边界
Task Instructions    当前任务要求
Repository Rules     项目约定
Dynamic Context      文件、记忆、Skill、搜索结果
Output Contract      输出格式和验收标准
```
这样可以：单独迭代和测试某一层；知道每段prompt来自哪里；可以做token的分类统计、可以阻止仓库文件、memory冒充系统指令、更容易进行缓存和版本管理。

*3.分离指令和数据*

外部数据不会直接说：以下是文件内容...。更安全的方式时是：使用json/xml格式明确边界；转义边界字符；记录来源；不允许数据改变工具权限；对不同来源的文件输出做统一处理（子agent，MCP，Memory，Web，工作区文件）。

*4.使用正向可判定的规则*

通常只写禁止规则的效果不是特别好：不要瞎编，不要调用错误的工具、不要过度询问用户等。应该也同时写出正确的替代行为：证据不足时明确说明缺少什么证据、本地文件结论必须通过搜索或读取工具验证、只有缺少会实质改变结果的用户选择时才询问用户等。

*5.利用模型的原生能力*

现代的agent应该优先使用agent harness：原生的function calling，json schema输出、独立reasoning通道、tool result消息等。不建议让模型生成并解析thought、action、observation，这样容易产生格式漂移和解析错误，CapsLock 使用原生 tool_calls 实现 ReAct 循环就是更稳妥的方案。

*6.优化上下文而不只是优化文字*

很多“Prompt问题”实际是指上下文的问题：放入了无关的历史，重要约束距离当前问题太远，tool schemataiduo，skill全量注入，tool result输出过多挤占上下文窗口，同一个规则重复多次，历史摘要遗漏关键决策。

对于这些问题常见的优化包括：相关性召回，skill/tool渐进式披露，保留最近轮次，结构化压缩旧历史，大结果外部化，去重，固定静态前缀来利用prompt cache，为输出token预留空间。

*7.few-shot示例*

示例最适合解决“规则已经明确，但模型仍经常选错”的问题。例如需要模型区分分析与修改：
```text
用户：解释这个模块为什么报错
正确行为：只读分析，不修改文件

用户：修复这个模块的报错并运行测试
正确行为：读取、修改、测试
```
示例要覆盖决策边界，而不是堆很多正常案例。通常 2～5 个高质量反例比几十个相似案例有效。

*8.明确失败和终止行为*

Agent Prompt 应回答：工具不存在怎么办；工具失败后是否重试；重试多少次；权限被拒绝怎么办；测试失败是否继续修改；何时询问用户；何时停止；如何报告部分完成。

不过最大轮数、预算、重试次数、权限等硬限制应由 Runtime 实现，Prompt 只负责告诉模型如何配合。

**提示词模板如何设计**
```text
[Identity]
你是具备哪些职责的 Agent。

[Objective]
你的首要目标是什么。

[Authority and Trust]
指令优先级是什么。
哪些内容只是数据。
哪些内容不能授予权限。

[Operating Rules]
如何搜索、读取、修改、验证。
什么时候使用工具。
什么时候询问用户。

[Tool Policy]
工具的适用场景、前置条件和成功判定。
禁止根据工具名猜测执行结果。

[Planning]
什么任务需要规划。
计划和执行之间是什么关系。

[Evidence]
哪些结论必须引用证据。
证据不足时如何回答。

[Failure and Stop Conditions]
失败、权限拒绝、循环、预算不足时如何处理。

[Output Contract]
最终输出需要包含什么，不能包含什么。
```
动态内容不要直接写进模板，而是通过变量或 Section 注入：
```python
PromptBundle(
    core_policy,
    runtime_controls,
    repository_instructions,
    skill_catalog,
    memory,
    attachments,
    history,
    user_request,
)
```

**提示词如何迭代**

***第一步：定义行为指标***

明确希望提升什么，当前最常见的失败是什么，哪些指标不能够接受退化。

例如：文件修改成功率从 72% 提升到 82%；无关工具调用率低于 5%；权限违规率必须为 0；平均工具轮数不能增加超过 10%；Token 成本不能增加超过 15%。

***第二步：建立失败分类***

从真实失败案例中分类：意图误解、工具选择错误、参数错误、没有验证、过早结束、工具失败后虚假声明完成、重复调用、过度询问用户等。每次prompt修改应对应一个明确的失败类别，比如：
```text
失败：模型修改后经常不运行测试
修改：增加“改动后执行与风险匹配的验证”
评测：修改任务中的验证执行率和最终通过率
```

***第三步：建立评测集***
分为四个模板：
```text
development set：日常调试模板；
regression set：历史上修复过的失败；
holdout set：不参与提示词编写；
adversarial set：注入、歧义、工具失败、权限攻击；
```
任务类型也要分层：
| 类别 | 示例 |
|---|---|
| 代码问答 | 解释模块结构 |
| 故障诊断 | 查明失败原因但不修改 |
| 代码修改 | 修复并验证 |
| 规划 | 只制定方案 |
| 工具异常 | Shell 或 MCP 失败 |
| 权限场景 | 修改敏感文件 |
| 上下文场景 | 长会话、压缩、Memory |
| 安全场景 | 文件中包含恶意指令 |

***第四步：一次只改一个主要因素***

对于prompt修改时，一次只对于一个任务类型的方面的问题进行修改。

capslock的迭代记录：
```text
版本：prompt-v17
假设：模型没有区分“诊断”和“修复”
改动：加入只读请求决策规则及两个边界示例
目标指标：诊断任务意外写入率
保护指标：修复任务成功率、Token、延迟
```

**怎样判断模版真的变好？**

1. 任务结果：测试是否通过、目标文件是否正确修改、输出json是否符合模板、是否引用真实数据、用户要求的步骤是否完成。
2. agent行为指标：工具选择准确率、平均工具调用数、平均工具调用论文、重复调用率。
3. 安全指标：越界文件访问率、未授权工具调用率、提示词注入成功率等。
4. 性能指标：输入输出token、首token延迟、完成延迟、tool调用次数、模型调用次数等，promptcache命中率。
5. 稳定性指标：同一个模板应测试不同表达方式、不同语言、信息顺序变化，不同模型等。

### capslock开发过程中有遇到过模型幻觉吗，如何确定是模型的原因还是agent侧的原因？如何处理幻觉？

有遇到过幻觉，比如：
1. 模型生成非法工具参数；
2. 模型可能声称未执行的操作已经执行；
3. 模型可能把已经执行的操作误判为未执行；
4. 历史没有真正进入模型上下文，表现得像“模型忘了”；
5. 模型生成不存在或无效的 Evidence ID；
6. 达到工具轮数后模型仍继续请求工具；

**案例一：模型误以为操作没有发生**
CapsLock v2.3.0 开发过程中发现：工具已经写文件、发请求或产生其他副作用，但工具结果过大，超过结果传输上限，Runtime 把整次工具调用改写成失败，模型看到的是 ok=false，模型可能认为操作没有发生，然后重复执行同一个副作用。

开发记录直接指出：如果工具已经写文件、发请求或执行第三方操作，模型会误以为操作没有发生并可能重试，这是严重的一致性风险。

*解决方法*：capslock会将ok状态拆分成多个正交状态
```python
class ToolOutcome:
    status: ToolOutcomeStatus
    executed: bool
    delivery_status: DeliveryStatus
    error: str | None
    error_code: str | None
```
这样就可一知道工具执行过程的结果，交付成功或者失败，副作用是否真实发生，回答“操作发生了，但结果处理失败”这样的问题。这类处理的本质是：**不要让模型根据模糊文本猜测系统状态，而是把系统事实结构化地返回给模型。**

**案例二：模型忘记历史->上下文构造问题**

v1.7.1 开发过程中出现过一种现象：TUI 可以显示历史对话；用户视觉上认为会话已经恢复；但 Runtime 不一定真的把这些历史发送给模型。

这种情况下模型可能：重复完成已经完成的工作，否认用户之前提供的信息，给出与之前决定冲突的答案，重新询问已经回答过的问题。

后来修复为：Runtime 上下文包含当前 Session 的可见历史；用户请求在模型调用前持久化；中断、恢复时不会丢失原始问题；长历史使用结构化 Compaction。

**如何判断是模型问题还是 Agent 问题**

不能只看最终回答，必须沿完整链路定位：模型输入 → 模型原始输出 → 工具执行
 → Tool Result → 下一轮模型输入 → 最终回答 → UI 展示

1. 先检查模型到底看到了什么
需要检查：System Prompt 是否正确；用户问题是否完整；历史消息是否存在；Compaction 是否遗漏关键约束；Memory 是否错误；Tool Schema 是否真的暴露；动态工具是否已经发现；Tool Result 是否进入下一轮；Plan Mode 状态是否最新。

比如：
```text
正确事实没有进入模型输入 → Agent 上下文问题

错误事实进入了模型输入 → 上游数据或 Agent 组装问题

输入正确但模型输出错误 → 更可能是模型或 Prompt 决策问题
```

2. 检查模型的原始输出

如果输入和 Schema 都正确，模型仍然产生：不存在的工具名；非法 JSON；缺少必填字段；错误枚举；明确违背 Tool Result 的最终回答；那么才更接近模型侧问题。

3. 检查tool result是否符合事实：Agent 不能只检查模型输出，还要检查 Tool Result 自己是否撒谎。如果excuted = false但是模型说成功，那么就是模型或者prompt错误。还有一种情况，如果文件已经被创建或者修改，但是tool result返回excuted = false，那么就是agent runtime的错误。这些也需要独立手段来验证，比如文件是否存在，文件的哈希值，git diff等。

4. 测试端可以使用可重复的fake model测试，能够很好的区分问题
```text
固定模型输出 + Runtime 仍然出错 → Agent/Runtime 问题

Runtime 对固定错误输出能正确恢复 → 模型错误被正确隔离

相同输入下真实模型偶发错误 → 更可能是模型随机性

不同模型都稳定犯同样错误 → 更可能是 Prompt、Schema 或上下文设计问题
```

5. 做同输入，同上下文额对照试验。典型判断：同模型偶尔发生，模型随机性导致。所有模型都发生，那么就是agent侧问题。

**Capslock如何处理幻觉**

1. 对事实性幻觉要求工具证据，system prompt要求：本地文件和git结论使用工作区工具，本地证据使用[[evidence:...]]，外部来源使用 [[source:...]]，Memory 使用 [[memory:...]]，证据不足时明确说明。

2. 对工具幻觉：schema校验和失败回填。模型可能产生未知工具，非法参数等。capslock会把这些转成结构化失败的结果，然后放回tool loop，让模型能有机会修正，而不是直接信任或者崩溃。

3. 对执行状态幻觉：executed作为事实源，system prompt中ing却要求：只有 tool result 的 executed=true，才能声称操作执行过。

4. 对权限幻觉：代码层强制约束，即使模型幻觉出有权限越界，也不会因此获得权限，因为工具执行还要经过Workspace path policy、参数 Schema、Permission Engine、hard deny、Shell sandbox、Action approval、capability grant、执行前 revalidation。

5. 对循环幻觉：预算和循环检测。capslock会根据工具名恶化规范化参数计算指纹，来检测是否重复调用了作用相同的工具。达到**限制**后由runtime停止。

6. 对上下文幻觉：结构化压缩和信任分类。旧历史会按照固定字段进行摘要，memory、skill、附件、压缩摘要都会被标记为untrusted data。无法覆盖core policy或者授予权限。

### 从架构视角来看。react、chain-of-thought、plan-and-excute三种agent模式分别适用什么场景？

从架构视角看，这三者并不是完全并列的 Agent 架构：
Chain-of-Thought（CoT）主要是模型的推理方式；
ReAct 是“推理—行动—观察”的执行循环；
Plan-and-Execute 是“规划器—执行器”职责分离架构。
它们可以组合使用，而不是只能三选一。

**CoT**:输入 -> 逐步推理 -> 最终答案。CoT 的核心是让模型在输出答案前进行多步推导。它本身通常没有工具调用、状态更新和环境反馈，因此严格来说不一定构成完整 Agent。适用于：数学推导，逻辑题，规则判断，方案比较等这些所有规则和请求都已经提供，模型只需要内部推导，不需要操作外部环境。

**ReAct**：适合探索性，反馈驱动任务，比如故障诊断、bug修复、命令执行、搜索资料等。ReAct 是 Reasoning + Acting。基本流程是Reason -> Action -> Observation -> ...。现代实现通常使用原生 Function Calling：模型生成 tool_call → Runtime 执行工具 → role=tool 结果返回模型 → 模型继续决策。

**Plan-and-excute**：适合长任务和工作流，比如代码重构，数据迁移，长周期开发任务等。Planner -> 结构化计划（Step 1、2、3）+ 验收标准 -> excutor逐步执行并更新状态 -> verifier -> 完成或者重新规划。

### capslock的ReAct完整执行链路，失败重试、降级兜底策略

**完整执行链路**
```text
用户请求
  │
  ▼
RunEngine：同一 Session 串行化
  │
  ▼
Workflow.prepare：创建新 Run / 恢复暂停 Run
  │
  ├─ 创建 RunGovernor
  ├─ 绑定 ModelRunSession
  └─ 初始化事件发布和 Journal
  │
  ▼
构建 Context / Prompt
  │
  ├─ Core Prompt
  ├─ Runtime Control
  ├─ AGENTS.md / 项目指令
  ├─ Skill Catalog / 显式 Skill
  ├─ Memory Recall
  ├─ 历史对话
  ├─ Attachment
  └─ Tool Schema
  │
  ▼
┌──────────── ReAct ToolLoop ─────────────┐
│                                         │
│  1. 刷新动态 Tool                       │
│  2. 压缩上下文、注入最新 Plan           │
│  3. Governor 检查预算和限制             │
│  4. ModelRouter 选择模型                 │
│  5. 流式调用模型                        │
│       ├─ reasoning → Thought             │
│       ├─ content → 文本增量              │
│       └─ tool_calls → Action             │
│                                         │
│  无 tool_calls                           │
│       └───────────────→ 最终回答         │
│                                         │
│  有 tool_calls                           │
│       │                                 │
│       ├─ 持久化 assistant/tool_calls     │
│       ├─ 调度 Tool Invocation            │
│       ├─ 校验、权限、审批、执行          │
│       ├─ ToolOutcome                     │
│       └─ role=tool → Observation         │
│                            │             │
│                            └── 回到第 1 步│
└─────────────────────────────────────────┘
  │
  ▼
Citation 解析、保存 assistant 消息
  │
  ▼
Workflow.finish：completed / stopped / failed
  │
  ▼
后台 Memory 提取，不阻塞主结果
```

**Run入口和初始化**

1. 同一session串行执行：RunEngine 使用 asyncio.Lock，防止同一会话中的两个前台 Run 同时修改会话状态、Plan、权限或上下文。客户端停止消费时，执行 Task 会被取消。

2. 创建或恢复Run：RunOrchestrator.start() 负责：workflow.prepare() 创建新 Run，或者恢复已有 Run；创建 RunGovernor；绑定 Run ID、模型角色、预算基线；EXEC 模式设置为 hard budget，不能交互式扩展预算。

**上下文和Prompt构建**

CapsLock 在第一次模型调用前会组合：
```text
内置 Core Prompt；
Runtime Control；
项目中的 AGENTS.md 等指令；
Skill Catalog 或显式调用的 Skill；
Memory Recall；
附件、IDE Selection 等；
会话历史；
当前用户问题；
Tool Function Schema。
```
短会话直接输入完整历史。达到默认输入预算的 80% 后开始压缩：

1. 先对旧的大型 Tool Result 做 micro-compaction；
2. 再把旧历史总结成固定 JSON：goal / constraints / completed_work / decisions / files / failures / evidence / pending；
3. 默认保留最近 6 轮原始对话；
4. 总结模型失败时使用本地 _fallback_summary()；
5. 如果压缩后仍超过窗口，才报 ContextBudgetExceeded。

而且不只是 Run 开始时压缩：每次 ReAct 循环调用模型前，还会执行 active-run checkpoint compaction。

**一次ReAct循环**

*动态准备阶段*

每轮开始都会：刷新动态工具；压缩当前 messages；移除旧 Plan Attachment，注入最新 Plan；调用 governor.before_model() 检查时间、Token、费用、工具轮数。

Plan Mode 激活时，只把 plan_schemas 暴露给模型；普通模式暴露完整工具 Schema。

*调用模型（Thought）*

ModelStepExecutor 创建一个 Model Step，然后流式接收：

- delta.reasoning：模型的 reasoning/Thought；
- delta.content：普通回答文本；
- delta.tool_index/name/arguments：Function Calling；
- delta.usage：Token Usage。

源码会把 reasoning 发成 THINKING 事件，并在有工具调用时将其保存在 reasoning_content 字段中。

*如果模型没有调用工具*

如果 message.tool_calls 为空：
- content 非空：认为 ReAct 完成，返回最终回答；
- content 为空：抛出 ToolLoopError("model returned an empty answer")。

*模型调用function calling（Action）*

有 Tool Call 时，CapsLock先将以下消息写入上下文和 checkpoint：
```json
{
  "role": "assistant",
  "content": null,
  "tool_calls": [
    {
      "id": "call_xxx",
      "type": "function",
      "function": {
        "name": "read_file",
        "arguments": "{\"path\":\"...\"}"
      }
    }
  ]
}
```
然后增加一个 Tool Round，进入工具调度。

*工具并发策略*

只有同时满足以下条件的工具才可以并发：
```text
read_only
&& concurrency_safe
&& !context_mutation
&& !destructive
&& !external_side_effects
```
其他工具形成顺序屏障，避免写操作、外部请求、上下文修改和审批工具乱序。
并发工具默认是“单调用失败、其他调用继续”。只有工具策略配置了 fail_fast=True，才取消同批兄弟调用。

*单次Tool Invocation状态机*

一次工具调用大概经过：
```text
解析 arguments JSON
→ 创建 Tool Invocation
→ Governor 预检查
→ normalize 参数
→ JSON Schema 校验
→ tool.validate
→ middleware.pre_authorize
→ resolve_policy
→ middleware.authorize
→ 执行工具
→ output schema 校验
→ middleware.after
→ ToolOutcome
→ 结果交付
→ Journal 终结
→ role=tool 回填
```

ToolOutcome：工具结果返回的是结构化字段：
```json
{
  "status": "succeeded | failed | denied | cancelled",
  "ok": true,
  "executed": true,
  "delivery_status": "inline | artifact | truncated | delivery_failed",
  "data": {},
  "content": [],
  "error": null,
  "error_code": null,
  "content_trust": "tool_data",
  "suspicious": false,
  "risk_signals": []
}
```
特别重要的是三个维度相互独立：
- status：业务执行是否成功；
- executed：工具是否已经发生执行或副作用；
- delivery_status：结果是否成功交付给模型。
例如“文件已经写入，但返回结果不符合 Schema”，结果可以是：
```text
status=failed
executed=true
error_code=invalid_tool_output
```
这样不会把已经发生的副作用误判成“没有执行”。

Observation回填：工具执行结束后被追加为：
```json
{
  "role": "tool",
  "tool_call_id": "call_xxx",
  "content": "{...ToolOutcome...}"
}
```
随后进入下一轮模型调用。模型可以根据 Observation：给出最终回答/修正参数后重新调用/换一个工具/请求用户输入/放弃当前路线。

这正是 CapsLock ReAct 的核心闭环。

**失败重试策略**

1. 模型API自动重试：ModelRouter 默认 retries=2，因此每个 Model Profile 最多尝试 3 次。如果模型流还没有产生任何 content、reasoning 或 tool delta，失败后可以重试。但只要已经输出过任何可见增量，再发生异常就会返回异常信息并终止输出，不会重试，也不会换候选模型。否则可能把两次模型输出拼接到一起，或者重复产生 Tool Call。

2. 工具失败通常不自动重试：工具 Runtime 会把常见错误转换为 ToolOutcome，这些结果会作为 Observation 回填给模型，Run 通常不会立刻失败。

**模型路由降级**

同一个 Profile 的可重试次数耗尽后，Router 按配置顺序尝试下一个候选 Profile。

候选模型会提前排除：
- API 凭据缺失；
- 上下文窗口装不下当前 messages + tools + max output；
- 开启费用预算但 Profile 没有价格配置；
- Provider 不支持当前流式接口。
- 但有一个重要安全边界：
- 不允许从一个 Provider 降级到 data_policy 不同的 Provider。

例如主模型配置为本地数据策略，备用模型是公共云策略，CapsLock 不会为了可用性自动把上下文发送到公共云，而是抛出 ModelDataPolicyMismatch。

**循环、预算和失控兜底**
RunGovernor 从 Runtime 层强制约束模型，不依赖 Prompt 自觉。
默认限制包括：
- 最大 Tool Round：32；
- 最大 Tool Call 数；
- 最大运行时间；
- 最大 Token；
- 最大费用；
- 重复 Tool Call 检测。

每次 Tool Call 使用：tool name + 脱敏后的规范化 arguments生成 fingerprint，检测：
- 连续相同调用，默认 3 次；
- 相同失败调用，默认第 3 次阻止；
- 长度 2～4 的调用周期，默认重复 3 次。

交互模式达到最大 Tool Round 时：可以调用 authorize_limit 请求用户扩展，批准后增加 32 轮，用户不扩展时，进行一次不带工具的停止摘要，Prompt 明确要求“只总结已完成工作，不再请求工具”。

## 上下文工程与记忆系统

### capslock的记忆系统

**整体架构**
```text
                     ┌──────────────────────┐
用户问题 ───────────→│ ContextBudgetManager │
                     └──────────┬───────────┘
                                │ 并行
                 ┌──────────────┴──────────────┐
                 ▼                             ▼
          会话历史查询                   Memory Recall
                 │                    lexical + semantic
                 │                             │
                 └──────────────┬──────────────┘
                                ▼
                    PromptBundle / Tool Schemas
                                │
                                ▼
                           Agent ReAct
                                │
                                ▼
                           最终回答完成
                                │
              ┌─────────────────┴─────────────────┐
              ▼                                   ▼
       返回用户终态事件                   后台 Memory Job
                                             │
                                             ▼
                                     Candidate Extraction
                                             │
                                  ┌──────────┴──────────┐
                                  ▼                     ▼
                             Review 队列          Automatic Gate
                                  │                     │
                                  └──────────┬──────────┘
                                             ▼
                                      Memory Revision
```
主要组件由 MemoryService.py (line 39)组合：
- RecallService：召回与排序；
- CandidateService：自动提取、去重、冲突识别和采纳；
- EmbeddingService：本地或外部向量；
- MemoryLifecycleRepository：创建、编辑、忘记、撤销、彻底删除；
- MemoryJobWorker：后台持久化任务；
- MemoryMaintenanceRepository：去重、来源失效和维护；
- MemoryTransferService：导入导出；
- MemorySettingsService：开关和策略叠加。

**记忆领域模型**

capslock有四种作用域：global-所有工作区，所有会话；workspace-当前工作区所有会话；session-当前工作区当前会话；agent-当前工作区指定namespace的子agent。普通的主agent自动召回的是global+当前workspace+当前session。

capslock分了七种memory type：fact、preference、decision、todo、note、project、temporary。类型不仅用于展示，也影响召回时的时效衰减。preference 365 天；fact、decision	180 天；project、note	90 天；todo、temporary	30 天

每条记忆还会记录来源：manual-用户手动添加；imported-从导入文件创建；reviewed-用户确认 Candidate 后创建；automatic-自动策略直接采纳

**identity和不可变revision**

capslock会把记忆身份和记忆正文分开：
```text
memories
  └─ identity、scope、status、current_revision

memory_revisions
  └─ 每一版正文、类型、置信度、来源、过期时间
```
一次编辑不会覆盖旧的记忆正文，而是经过多次revision。

**记忆如何进入prompt**

构建context的时候会并行执行sessions.context_entries()，memory.recall_context(question)，召回结果写入prompt section。

**记忆召回：混合召回算法**

[todo]

**召回的可解释性**

CapsLock 不只记录最终命中的记忆，还记录被过滤的候选：
```text
score
lexical_rank
semantic_rank
cosine
retrieval_score
selected_reason
filter_reason
reasons_json
```
查询的正文只保存哈希值。这样每个记忆选中的理由可解释，终态事件也会携带 memory_recalls，方便解释“为什么这条记忆进入了本轮 Context”。

**记忆引用**

召回和get-memory工具都会给模型提供引用标签：[[memory:mem_xxx]]。回答完成后，CitationResolver 只接受本轮真正召回或通过工具读取过的 Memory ID。

**按需Memory工具**

模型可以调用两个只读工具：search_memories、get_memory。

需要注意：自动 Prompt Recall 使用 lexical + semantic 混合算法；search_memories 当前直接使用词法查询；两个工具都是 safe_read，可以并发；模型没有直接写 Memory 的工具。

Memory 写入只能来自：用户 /memory add/edit/...；经用户确认的 Candidate；严格自动采纳；经过父 Agent 验证的子 Agent Proposal；导入。这避免模型在 ReAct 中随意把自己的推断写成长期事实。

**自动捕获链路**

[todo]

**candidate提取和防止幻觉**

提取prompt位于candidates.py。它要求模型：

- 只提取用户直接陈述的持久信息；
- 或从 verified evidence 提取事实；
- Assistant 文本不是权威来源；
- 不提取 Secret；
- 不提取仓库中随时可以重新读取的普通摘要；
- 最多输出 20 个 Candidate；
- 返回严格 JSON。

每个candidate必须提供原文来源：
```json
{
  "source": {
    "kind": "message",
    "id": "message-id",
    "quote": "I prefer Ruff",
    "direct": true,
    "verified": false
  }
}
```
runtime会验证：
1. Source ID 确实存在于 Envelope；
2. quote 必须是对应原文的精确子串；
3. direct=true 只能指向用户消息；
4. verified=true 只能指向 Evidence；
5. 两者都不成立则丢弃 Candidate。

这是 CapsLock 防止“模型自己说了一句话，然后把它记成用户事实”的核心机制。

**Review和Automatic策略**

策略有off、review、automatic三种，默认为automatic。自动采纳条件非常严格：
```text
relation == new
confidence >= 0.90
scope ∈ {workspace, session, agent}
source 是用户直接陈述或 verified evidence
risk_flags 为空
```
以下情况不会自动采纳：
- global：添加 global_scope 风险；
- Secret 被脱敏；
- 来源不是 direct/verified；
- namespace 非法；
- project/note 可能构成指令提升；
- 去重协调失败；
- 与已有记忆冲突。

精确重复且无风险时，不创建第二条 Memory，只给旧 Memory 增加一个新来源。
review 模式下 Candidate 保持 pending/conflict，由用户接受、拒绝或编辑后接受。TODO 类型不会保存成普通 Memory，而是路由到持久化 Task 系统。

**Embedding系统**

[todo]

**来源实效和上下文污染控制**

capslock每次记忆召回都会记录：memory_id + revision + run_id。如果之后该memory被编辑，被遗忘，revision改变，自动记忆的来源实效，那么之前使用旧 Memory 生成的 Run 会出现在 excluded_runs() 中。下一次构建会话历史时，这些 Run 会被排除，避免模型继续从旧 Assistant 回答中间接读回已经失效的记忆。同时，Context Compaction 保存 memory_revision_digest。访问过的 Memory 发生变化后，旧 Compaction 会被失效并重新生成。

完整传播链：
```text
Memory 修改
→ 旧 Revision 失效
→ 使用过旧 Revision 的 Run 被排除
→ 依赖旧 Memory 的 Compaction 失效
→ 下一轮重新构建 Context
```
(为什么不直接从向量库删除一条记录？)

**维护和合并**

维护触发条件之一满足即可：Active Workspace Memory 超过 200；待处理冲突超过 10；距上次维护超过 24 小时，且新增至少 5 个完成会话。

维护分两层：

1. 确定性层会：合并正文完全相同的自动记忆；保留手动/Reviewed Memory；将重复自动记忆的来源迁移给 Survivor；Forget 来源已经失效的自动记忆；为相似度 ≥ 0.90 的近重复建立待审核 Relation。
2. 模型层只生成 Proposal：
```text
duplicate
conflict
supersedes
rewrite
instruction_promotion
```
模型 Proposal 不直接修改 Memory，只进入 Review Queue。

**持久化后台任务**

Memory Job支持：extract_run、consolidate_workspace、promote_agent_memory。

具备：幂等键，例如 extract:{run_id}；queued/running/completed/failed 状态；最多 3 次尝试；1 秒、2 秒指数退避；进程重启后恢复未完成 Job；第 3 次仍失败则终结。

*后台任务失败只记录事件，不影响主回答。*

**Multi-agent Memory**

子 Agent 不直接共享父 Agent 的整个 Memory DB。执行子任务时：

1. Contract 可以声明 memory_namespace；
2. 父进程只加载该 namespace 下的 Agent Memory；
3. 以 untrusted_data 注入子 Agent Prompt；
4. 子 Agent 自身的捕获、召回、写入和维护全部关闭；
5. 子 Agent 只能在输出中提交 memory_proposals；
6. Proposal 必须引用经过验证的 Evidence；
7. 父 Agent 验证后才可能持久化。

**关闭和降级策略**

| 故障 | 行为 |
|---|---|
| Memory Recall 整体异常 | 本轮无 Memory，主 Run 继续 |
| Embedding 不可用 | 降级为词法检索 |
| 新 Memory 建向量失败 | Memory 仍保存，只记录事件 |
| Extraction 失败 | 主回答不失败 |
| Maintenance 失败 | 主回答不失败 |
| 外部 Consent 无效 | 禁止外发，词法召回仍可工作 |
| 记忆过期 | 查询时自动过滤 |

### 出现Lost in the middle如何解决？

Lost in the middle:关键信息已经正确进入模型上下文，但位于长 Prompt 中部时，模型利用率明显低于位于开头或结尾时。

如何解决？：
1. **不会无限堆叠完整历史**：capslock的输入预算为context_window - max_output_tokens。默认时80%输入预算时触发压缩。压缩目标为60%，最近六轮保留原文，连续3轮压缩失败后终止，不会静默阶段。
2. **旧历史转换成结构化摘要**：超过阈值后，就历史会被压缩为固定结构。
3. **最近信息放在Prompt尾部**：高优先级规则在开头；最近任务状态和当前问题在结尾；中部主要承载可压缩的数据。
4. **长工具链每轮重新检查上下文**：ReAct 工具循环不是只在会话开始时压缩一次。每次调用模型前都会重新执行 active-run compaction，并刷新 Plan 上下文，因此几十轮工具调用产生的 Tool Result 不会无限积累。
5. **Memory 将旧关键信息重新提到当前上下文**：Memory 使用词法和语义混合召回，最多返回 5 条、总计 4 KiB。召回结果会作为独立 Memory Section 注入当前 Prompt。这相当于把过去的重要事实重新搬到当前问题附近，而不是让模型在完整历史中寻找。


### capslock记忆是如何设计的？如何区分长期记忆短期记忆，长段记忆的设计思路，存储机制和淘汰机制是怎么样的？

capslock的通过存储位置、生命周期和使用方式区分长短期记忆，两套记忆走两个链路：
```text
短期记忆：会话历史 → 上下文预算管理 → 压缩摘要 → 当前 Prompt
长期记忆：完成的 Run → 候选提取 → 校验/去重 → Memory DB → 检索召回
```
| 维度 | 短期记忆 | 长期记忆 |
|---|---|---|
| 主要内容 | 当前任务、最近对话、工具结果、Plan、执行状态 | 用户偏好、事实、项目决策、稳定经验 |
| 存储 | workspace SQLite | 独立 memory SQLite |
| 使用方式 | 顺序放入 Prompt，过长后压缩 | 根据当前问题检索 Top-K |
| 生命周期 | 随 Session 存在 | 可以跨 Session、跨 Workspace |
| 压缩/淘汰 | 最近原文 + 旧历史摘要 | 过期过滤、相关性降权、去重、forget/purge |
| 可信级别 | 用户消息有用户指令语义 | 一律作为不可信数据，不能授予权限 |

**短期记忆**：用户和助手消息持久化在 workspace 数据库的 messages 表，构造上下文时：读取当前 Session 的历史消息；默认保留最近 6 轮原文；更早历史压缩成结构化摘要；摘要包含 goal / constraints / completed_work / decisions / files / failures / evidence / pending；当前用户问题放在最后。

短期历史并不会因为压缩而立即从数据库删除。压缩只是改变“本轮送给模型的视图”。删除 Session 时，消息、compaction、工具记录等才通过外键级联删除。

**长期记忆**：长期记忆定义了4种作用域：global，workspace，session，agent。其中普通对话召回只搜索 global + 当前 workspace + 当前 session；agent Memory 不进入普通召回，而是通过 namespace 显式加载。此外还有 temporary / session / project / durable 四种 durability 元数据。不过当前实现中，durability 主要是分类元数据，还没有自动映射为 TTL 或强制生命周期策略。真正影响召回的是 scope、type、expires_at、更新时间和来源有效性。

**长期记忆写入**

一次普通 Run 完成后，CapsLock 才会异步提取 Memory。CapsLock 的长期记忆写回不是“模型觉得重要就直接保存”，而是一个分阶段决策流程：
```text
Run 是否适合提取
  → LLM 提出候选
  → 来源真实性校验
  → 安全与结构校验
  → 重复/冲突判断
  → Policy + 自动采纳规则
  → Memory DB 持久化
```
(模型负责语义判断，代码负责准入控制)

通常（memory启动，不是/init，不在plan mode等），一个run结束后会触发长期记忆的写回，capslock会构造一个extraction envelop：
```json
{
  "messages": [
    {
      "role": "user",
      "content": "本轮用户问题"
    }
  ],
  "evidence": [
    {
      "id": "...",
      "text": "...",
      "verified": true
    }
  ],
  "assistant_context": {
    "content": "本轮最终回答",
    "authoritative": false
  },
  "explicit_memory_ids": []
}
```
（envelop的设计和用处？）

主run的终止事件完成后，创建后台记忆提取任务，用幂等键保证一个run不会创建多个任务，后台的任务最多尝试3次，失败时按照1s、2s退避，提取失败不会影响用户主回答。

**如何判断是否值得长期保存？**

1. 第一层-模型判断，通过系统提取的prompt告诉模型的返回内容和禁止边界。只是提案。一次最多返回20个按要求格式返回的candidates。
2. 第二层-校验来源是否真实存在。每个candidate必须：引用当前用户消息的一段原文/引用当前run中经过验证的evidence。
3. 第三层-内容安全和格式：候选的正文还需要检查是否为非空字符串、最大8KiB、confidence是否在[0, 1]、type和scope是否为合法枚举，私钥/api key等敏感信息脱敏。（脱敏为模型提示+代码正则兜底两层实现）
4. 第四层判断-是否重复或者冲突：capslock会先检索相同type和scope下的最多5条相关Memory。如果规范化后正文完全相同，则判定为重复。如果没有完全相同但是存在相关memory，调用模型判断。调用模型失败会标记为reconciliation_failed，禁止自动采纳。

```text
不同结果的处理：
new：可能创建新 Memory；
duplicate：不创建正文副本，只给已有 Memory 追加新来源；
conflict：进入人工审核；
reconciliation 失败：保留 Candidate，但不能自动写回
```
5. 第五层-自动采纳规则：capslock有三种策略：off（不提取，不写回）、review（只生成candidate，等待人工确认）、automatic（低风险candidate可以自动采纳）。automatic自动采纳必须满足：
```python
relation == "new"
confidence >= 0.90
scope in {"workspace", "session", "agent"}
direct or verified
risk_flags 为空
```
6. 正式写入数据库：通过所有条件后，会在一个事务中写入数据库，随后长时间里embedding，embedding失败只会记录事件，memory仍然有效，之后可以通过FTS召回。

**召回机制**

每轮 Context Build 会使用当前用户问题查询长期 Memory，并和历史上下文并行加载，召回时同时执行：FTS/BM25词法检索和Embedding语义检索，均为20条（语义服务失败的时候会降级为纯词法检索）。得分大致为：
```text
0.72 × retrieval
+ 0.08 × scope
+ 0.08 × confidence
+ 0.06 × freshness
+ 0.06 × source_validity
```
候选至少满足词法top10或者cosine >= 0.45。最终得分至少为0.5。最后还会进行剪枝和判重等操作。

**淘汰机制**

capslock记忆淘汰是多种机制的叠加。
1. 显式过期：提供`expires_at`接口，FTS和向量召回都会过滤过期内容，但不会立即删除
2. 时间衰减：不同类型有不同 freshness 周期：偏好 365 天，事实/决策 180 天，项目/Note 90 天，TODO/Temporary 30 天。这只是召回降权，不是自动删除。
3. 来源失效：自动 Memory 的全部来源失效后，不再被召回；曾使用旧 revision 的 Run 也会被排除出后续历史，避免旧答案继续污染上下文。
4. Consolidation：活跃 Memory 超过 200、冲突超过 10，或者超过 24 小时且新增至少 5 个完成 Session 时触发维护。完全重复的 automatic Memory 合并来源后被 forget；来源无效的 automatic Memory 被 forget；相似度超过 0.90 的近重复只进入审核队列。Manual/Reviewed Memory 不会被自动合并删除。
5. Forget 与 Purge：forget 将状态改为 forgotten、移出索引，但保留正文和 revision，可以 undo；purge 删除正文、revision、FTS、向量、来源和待处理任务中的关联内容，只保留无正文 identity 与审计
6. Session 删除：删除 Session 时，会物理 purge 对应的 session scope Memory，但 workspace/global Memory 保留。
