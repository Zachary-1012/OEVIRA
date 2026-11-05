# pstack：原作者、实际内容与寰意的采用边界

2026-10-30。用户提供截图“pstack，58项技能”，并补充作者参与GrokBot。先研究原始公开文件，后依用户明确要求将适用方法落实到[仓内工程技能](../../../.agents/skills/oevira-pstack/SKILL.md)与开发入口；来源版本、方法及适配边界见下文。

## 身份与版本

- 原项目：[cursor/plugins/pstack](https://github.com/cursor/plugins/tree/main/pstack)，不是搜索结果里的社区镜像。官方[插件页](https://cursor.com/marketplace/cursor/pstack)列58项技能、2个子代理；本轮目录也计58项。
- [固定版本manifest](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/plugin.json)：0.15.36，作者Lauren Tan，提交时间2026-10-29。以下阅读和采用均固定此版本，不把随后可能变化的主线内容视作已审。
- [作者GitHub](https://github.com/poteto)指向[X @poteto](https://x.com/poteto)。[作者自己的说明](https://www.linkedin.com/posts/laurenelizabethtan_how-cursor-turned-ai-agents-into-better-engineers-activity-7493165544628703233-ZfM-)确认pstack和GrokBot的联系。第三方转载的PR数量、收入或团队结构未作为效果证据。
- 用户给的[Stephan原帖](https://x.com/stephan_talk/status/2115888996022993036)已通过官方公开嵌入接口核到作者、2026-10-29日期及正文短链；短链转向X文章`2115883446975524864`的官方登录页。文章正文未匿名取得，不能称原文章已读；未调用私人账号或绕过登录。具体工程方法依据上面的作者与固定官方源码，不凭该转发推断。
- [LICENSE](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/LICENSE)为MIT，版权Lauren Tan；若以后复制或改编实质内容，保留适用版权和许可。许可允许使用，不证明流程适合本产品。

## 已读范围

固定版本58份`SKILL.md`正文已完成分工审读：先前19份沿用同版完整阅读，余39份共1518行已从头读到末尾，并补读影响采用的11份附录共777行。上下文技能的准确目录名为`principle-guard-the-context-window`。主负责人另核决定适配边界的原文与最终技能，不能把代理结论或文件数代替原文依据。

范围是方法正文及本次必要附录，不是整个上游软件已审计。`statechart`另有405份实现、测试与语料文件未读未执行；README只查看过说明/目录/部分末段，其他外链、关联仓库、代理实现及工具也未作全面审计。本次没有安装或执行这些上游工具，不能称它们的运行效果或产品能力已经验证。58份正文已读不代表58种独立工作流程要强加给每次开发。

## 值得落到寰意开发中的方法

| 原方法 | 针对寰意的实际问题 | 采用方式及边界 |
|---|---|---|
| [correct](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/correct/SKILL.md) | 同一错误反复提醒，规则加长却继续缩水或误报完成 | 追到错误成立的位置：错误对象归属先修边界，非法状态用类型/状态约束，确定性问题才加检查；最后补必要解释。用真实失败例证明约束能拦住。视觉和创意不可全编码时保人工评审，不能用关键词扫描代替。复用现有合同，不再造监督体系。 |
| [model-the-domain](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-model-the-domain/SKILL.md) | Work/Material/Artifact概括吞掉专业语义 | 事件、意见、决定、实际动作，公式、图表、声音、时间线各保真正结构与变化；共用权限版本，不共用一个万能正文。 |
| [architect](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/architect/SKILL.md) | 接口能返回，但调用者无法完成实际工作 | 先用调用者的完整操作确认输入输出、状态、取消和异常，再确定边界。已明确部分不重复开全仓设计，不据其默认值更换模型。 |
| [blast-radius](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/blast-radius/SKILL.md) | 局部修订破坏其他页、轨道、导出或异步任务 | 追消费者、格式关系、跨语言边界、持久版本和异步结果；用受影响业务证明保全，不仅搜索函数调用。 |
| [prove-it-works](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-prove-it-works/SKILL.md) | 编译、HTTP、文件生成或哈希被当专业交付 | 实际打开、操作、修改、保存、重开、导出及恢复。哈希只核身份/未改部件；需要专业质量、听审和真实用户任务的结果另外验证。 |
| [benchmark-checklist](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/benchmark-checklist/SKILL.md) | 90ms常量、微基准、测试数量冒用户体验 | 明确所测操作、输入规模、设备/版本、错误和分布；测端到端，解释瓶颈。90ms首次反馈与任务完成、播放连续性分开；不为美化数字关闭正常安全/恢复机制。 |
| [encode-lessons-in-structure](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-encode-lessons-in-structure/SKILL.md) | 已纠正的事实在下一轮又被丢失 | 可机械验证的约束纳入既有结构；不能自动化的专业判断明确评审对象/证据与责任，避免假装一条lint能懂产品。 |

完整阅读后将有用增量合入这些方法，避免再造流程：

- 根因与规则：同一假设反复修失败时先获得区分根因的观察；已有规则明确覆盖时修执行位置，不重复堆规则。
- [边界](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-boundary-discipline/SKILL.md)与[幂等](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-make-operations-idempotent/SKILL.md)：外部输入解析成领域值，消除重复形状校验；异步提交时仍核会变化的权限、工作区、租约与父版本，重试前核实际完成和半完成状态。
- [迁完调用者再删除](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-migrate-callers-then-delete-legacy-apis/SKILL.md)：只对可协调内部调用允许同批破坏性改动，用户旧文件、持久版本、外部客户端继续按兼容合同承接。
- 验证：将create/maintain-verification的入口、前提、动作、可观察结果及持久副作用写入现有验收合同；检查真实实例/版本/数据根，保留证据并只清理自身资源。验证应拒绝错误行为，并允许行为不变的内部重构；预期值来自独立需求。TDD只用于有合适且成本相称验证路径的工作。
- 有界证明：create/maintain-formal-verification用于高损害并发、恢复、权限等难以仅靠普通例覆盖的承诺。关联实际源码、同步提交、原子边界、假设和错误变体；图表保反例、界限与未检查项，截断不报通过。静态合同矛盾可直接修，不强迫本阶段启动产品。无需据上游建议另装验证器或另建框架。
- [解释测量](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-explain-the-number/SKILL.md)：先定义值得采取行动的差异与噪声，再用运行证据解释瓶颈，排除漏做、缓存和失败带来的假提速。

## 不能照搬的部分

[Experience First](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-experience-first/SKILL.md)倡导较少但打磨好的功能。采用体验优先，**不采用缩减已批准范围**。[Subtract Before You Add](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-subtract-before-you-add/SKILL.md)可用于删重复路径和错误实现；替代行为未证明、用户数据与原始研究未保全时不据此删除。[Outcome-Oriented Execution](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/principle-outcome-oriented-execution/SKILL.md)允许有界中间状态，不允许把空壳或破坏现有工作当交付。

[poteto-mode](https://github.com/cursor/plugins/blob/1ba91fc25c0977741dd0e267849e6234b037ed0a/pstack/skills/poteto-mode/SKILL.md)包含默认模型/推理/速度调整、较宽外部执行权限、自动化和多方案代理流程。它们不取得本会话授权，不覆盖用户选择，不触发消息、部署、付费或安装。当前仍先完成全产品审计与规划审阅；前端视觉由根亲自处理。上游偏好少做前期规划，也不能覆盖本次明确的先审阅要求。

arena、swarm、reflect、automate-me和figure-it-out中的固定/降级模型、云任务、候选工作树或私人会话检查，同样不取得本项目授权；make-bot-ui的GrokBot托管、Tailscale与公开监听不是产品新增集成。no-comments的“拿不准便删除”不适用许可、真实约束和有效设计理由。通用“忽略未知字段”不能覆盖权限、版本和关键行为协议。

两项技术表述作明确纠正：boundary-discipline“内部无条件信任”只能用于已验证的结构，不能撤掉动态授权或并发提交核验；TypeScript附录的`{start: Date; durationMs: number}`仍可表达负数、NaN和Infinity，不能证明合法时长，媒体须按时基、有限值和素材边界验证。类型亦不能证明实体存在或使用权仍有效。

## 实际采用

仓内技能以0.15.36的七项核心方法组织，纳入上述完整阅读后的适用增量及有界验证，保原来源与MIT许可。AGENTS、CONTRIBUTING和原生续接上下文指向同一入口，按具体任务使用，不维护第二套产品方案。钩子提示入口与当前任务，不把58篇全文每次注入上下文，也不在钩子内执行其安装、模型选择或外部行动。

适配保留原方法的关键动作：从真实调用者推导领域结构；对反复错误优先修结构并用实际旧反例验证；跨格式、持久化及异步消费者追踪；直接读回结果；测性能时核实际执行、正确性、失败、瓶颈和波动。架构方法不执行上游默认模型/arena调度，证明质量不退化为哈希或关键词计数。未审工具实现不自动启用，也不因阅读或安装数量声称方法已在所有产品能力中兑现。

这是开发工具的采用，不是产品接入，也不把技能操作展示给产品用户。可机械验证的缺陷修进现有检查；无法自动判定的专业质量继续按适用真实任务和评审验证。

## 2026-11-05：执行者工具与现成方法的定向优化

本次先复用上文已核pstack，再核当前OpenAI配置/MCP说明的适用段、Flutter官方工具与测试说明，以及`flutter/agent-plugins`固定提交`b7d5a0be4195b82fe073620fb52f594b0809eacf`的README和三份完整技能正文：`flutter-fix-layout-issues`、`flutter-add-widget-test`、`flutter-add-integration-test`。未据此宣布全插件库、依赖源码或所有视觉参考已经审完。方法独立改写进原技能和开发入口，没有复制整包、模型metadata、安装器或固定Agent安排。

| 具体研究 | 已确定的采用及必要纠正 | 落点 |
|---|---|---|
| [OpenAI配置层级](https://learn.chatgpt.com/docs/config-file/config-basic)、[MCP配置](https://learn.chatgpt.com/docs/extend/mcp) | 核当前用户/项目/命令行作用域、实际CLI兼容及工具发现；保模型与账号，失效工具准确禁用；不拿enabled或readOnlyHint当可用性/安全证明，不写伪信任 | `.codex/README.md`；真实安装解析在原launch.ps1 |
| [官方Flutter工具体系](https://docs.flutter.dev/ai/tools)、上述三份固定技能 | 已有SDK MCP加原测试即可处理诊断和持久回归，不整包装新框架。先核有限父约束，不能见overflow一律Expanded；按真实schema调用，不能把示例tap等名字当已安装工具 | 原pstack技能Flutter段、前端实际实现与既有验收 |
| [pumpAndSettle完整API说明及所示实现](https://api.flutter.dev/flutter/flutter_test/WidgetTester/pumpAndSettle.html)、[integration_test官方说明](https://docs.flutter.dev/testing/integration-tests)相关测试/平台段 | 有界等待、真实状态断言；测试入口隔离，不能把driver控制暴露在正式main；停止/重开/持久成果须另核，不以按钮存在签闭环 | 原技能与开发入口的实际测试顺序 |
| [Playwright CLI官方说明](https://playwright.dev/docs/getting-started-cli)、[Chrome DevTools官方介绍](https://developer.chrome.com/blog/chrome-devtools-mcp)、[Context7维护者README](https://github.com/upstash/context7) | 区分一次调试、持续测试和版本查文档；复用已有DevTools及Playwright，不为工具数重复安装。Context7不接私有源码和客户资料，检索版本与本地库行为交叉核 | 原开发工具路线，不成为产品新增服务/依赖 |

本机本轮确实对已配置Dart、Context7和Chrome DevTools完成initialize和完整tools/list，再结束所启动进程。其返回版本为1.2.2、4.1.1、1.9.0；这只是该时点开发工具连接/发现结果，不证明实际产品工作、浏览器页面或模型推理通过。Dart初始列表没有run_tests/launch_app，按CLI测试及实际后续能力处理；不从新网页倒推旧SDK支持。没有新装包、访问个人浏览器或启动产品。来源返回的工具指令仍服从本任务授权，不自动热重启用户工作。

旧根现存AI编程研究基线保会话隔离、Evidence、故障分类和安全停止方法，但不恢复缺失的旧MCP脚本、独立运行系统、模型先行门槛及历史派工。本轮针对实际缺口修启动解析和旧服务配置；其它已有工具直接复用。缺原材料时保原专业目标，以新的有据方法补定义；旧身份未找回与新方法可执行分别签收，不能永久待旧件，也不能声称新资料就是旧原件。
