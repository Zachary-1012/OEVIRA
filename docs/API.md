# 寰意 / OEVIRA — 专业对象与接口合同

本文件规定全部领域之间及客户端与核心之间的输入、操作和结果合同。专业行为由[完整规格](PRODUCT-SPEC.md)和[领域合同](domains/README.md)定义，存储与运行边界由[架构](ARCHITECTURE.md)定义。下列专业操作是需要实现的协议要求，不是声称已有的HTTP路由；文末单列现有本地基础接口，不能让其文本模型或首批格式成为全产品能力上限。实现时在同一核心中补对应领域函数、实际路由、客户端类型与调用者，不另建通用RPC平台或第二套对象存储。

## 共同输入与对象引用

以下是协议数据，不要求用户填写内部ID。用户通过已有工作、选择对象、文件和业务条件产生输入；服务端解析身份、来源与允许动作。AI、直接编辑、批量操作和Worker消费同一领域合同。

| 类型 | 必须携带的内容 | 不得混淆的语义 |
|---|---|---|
| 请求上下文 | 服务端核定的用户、工作空间、适用客户、动作、用途；异步执行绑定当前授权与撤销世代 | 客户端可选目标，不能自行签发权限；客户、主体、平台账号三个身份分别核定 |
| 对象引用 | `kind,id,revision`；专业对象需要的父对象/原件版和原生定位 | 页码、名称、相似句、屏幕坐标不是独立稳定身份；移动后依真实对象映射定位，歧义需解决 |
| 来源引用 | 原件/观察ID及版本、页/段/表/单元格/对象/帧或时间区间、原话或数值、用途约束 | 原件版本与解析版本分别记录；文本偏移使用Unicode码点，媒体时间使用有理时基，不能用截图代来源 |
| 输入快照 | 本次工作要求和业务约束、确切输入版、关系版、指标定义版、比较集合、期间与可获得时点 | 事实有效时间、来源发布/修订时间、系统取得时间分开；回看当时判断不能偷用后来获得数据 |
| 文件引用 | 文件ID、原名、媒体类型、大小、完整性值、可读范围与获取方式 | 不暴露服务端裸路径；哈希证明身份，不能证明编辑/播放/专业质量 |
| 修改请求 | `base_version`、明确选区、由领域定义的操作及参数、保留/锁定范围、请求幂等键 | 不接受任意JSON补丁绕过业务规则；同版未选内容保全，无法唯一定位不能扩大为整份重生成 |
| 专业结果 | 成果/工程及版本、具体对象、输入快照、方法或模型配方、来源、覆盖、限制、可继续动作 | 正文可以是文稿、演示、工作簿、媒体工程或分析对象；通用`body`只能是摘要/文本正文，不能覆盖专业结构 |

输入缺失、未知、未经核实和实际零分别编码；数值保单位、币种、含税/退款口径、分母及期间。金额采用有明确精度的十进制或最小货币单位，算法输入输出需显式转换和验证；时间保UTC时刻及业务时区，不能按机器所在地偷偷改活动日期。集合先按权限/客户/期间筛选，再稳定排序分页；分页游标绑定查询条件与快照，禁止截断后宣称完整覆盖。

文件导入先保原件并检查实际格式、资源与可读/可改范围；大文件可分块续传，分块位置、校验及最终组装版可读回。导入限制要给具体原因和适用继续方式，不把“请选择更小文件”作为专业大表/媒体的完整方案。表格/文档/媒体的可用预览与完整解析分别呈现，后续解析不能覆盖用户已经校正的内容。

## 九个业务领域的操作合同

编号对应[业务合同](domains/business.md)的同号章节，各操作保其专业对象，不压成一次“生成报告”。

| 领域 | 输入与必要版本 | 操作和输出 | 直接消费者及关键约束 |
|---|---|---|---|
| 1 客户、主体与场内经营 | 客户委托；brand/company身份候选及境内营业依据；商户/门店/账号独立身份；期间关系 | 添加/补证/纠错/去重候选、确认合并/拆分与撤销、移除/重加；返回身份和有向关系版本及受影响引用 | 主体档案、场内品牌、跨城网点、地图/列表、监测与比较；名字相同不自动合并，快照不冒持续营业 |
| 2 经营、市场与消费者 | 指标定义、原生观察、比较集及版本、面积/营业日/业态、样本框、可获得时点 | 描述、可比性检查、分群/趋势/异常/预测；结果含口径、有效样本、区间及不能比较的原因 | 经营看板、比较、研究、客户简报；平台互动、调查反馈、交易数据不能互换分母 |
| 3 财务、预算与资源 | 报价/估计/预算/承诺/实支/退款/收益分别输入；币种、期间、税、硬上限/目标、预留金，已有资源身份/容量/时段/用途和费用依据 | 对账、数量×单价明细、资源占用与现金/估计价值分列、情景与敏感性、可行解/不可行解释；返回成本/资源/方案同版关系与受影响制作和履约项 | 方案选择、工作簿、分镜/节目、排期和客户交付；预留金不双花、同资源不重复占用；filed/capture/取得与授权截止不默认同口径 |
| 4 品牌策略与提案 | 品牌事实、用户洞察、受众、客户要求、禁用主张、预算资源版、已确认内容及案例机制/前提/反例/权利 | 形成实质策略选项、理由/反证、原创路线及执行内容，保本案机制采用与不可迁移条件；范围变更返回影响、差异和新批准范围 | 创作、客户PPT/文稿、预算、活动；客户新意见只改变实际关联部分，可比方案不固定三档，案例自报效果不担保本案结果 |
| 5 创作者与KOS合作 | 创作者/账号身份、品牌适配、分平台期间指标、报价/权益/交付约定及授权 | 候选比较、合作方案、履约/素材检查、分阶段漏斗与结算依据；外部请求单独返回状态和回执 | 合作计划、内容排期、授权素材、复盘；单次手工快照不冒日/周/月原生指标，未知外部结果先查回执 |
| 6 活动、系列与排期 | 活动/系列/Post及变体、资源容量、工作日历/时区、依赖、承诺及锁定项 | 创建/调整排期、冲突解释、局部重排、任务交接及执行记录；保未调整承诺 | 日历、媒体生产、预算、合作与发布；不能把旧单容量固定时长求解限制变成产品范围 |
| 7 实验与归因 | 假设、处理/对照、指标及分母、分配方式、观察期、缺失/退出、样本和渠道 | 建立/修订实验、质量诊断、估计与区间、适用因果或描述结论、复盘 | 内容选择、渠道/预算和学习评价；事后相关不宣称增量，分析更改与原计划可区分 |
| 8 事件、服务与风险 | 原帖/回复/更正、主体/活动关系、Target/Claim、证据、内部事实和处置权限 | 聚合与拆并事件、观点/诉求/传播/严重性分别分析、回应候选、行动/反馈；输出期间简报/事件专报，保事件演化链和ToC/ToB分层影响依据 | 舆情/话题/活动监测、客户工作、服务交接；报告完成、已读、已回复、已解决不是同一状态，无出处案例不进事实或训练金标 |
| 9 决定、行动与结果 | 决策问题、备选、证据/反证、约束、风险、批准范围和观察计划 | 保存选择及理由、形成具体行动、核真实回执、观察结果和修订经验 | 全部业务/创作的后续工作与可纠正记忆；行动申请、获批、执行、效果未知分别成立 |

费用冲正/退款必须引用原费用或实际支出、业务动作、币种、种类与理由；退款累计不能超更正后的可退实付，重复通知按独立退款身份处理。跨平台订单保各自命名域及同源映射，触点按事件时间进入约定窗口，总分配守恒于本次可归因量，退款前后历史均可重开。公开互动份额不转为订单归因或佣金结算。

主体移除不等历史结果撤权；仍有合法用途的原Case、原分析及成果可继续读取，停止新的主体使用与监测。取消不应先要求已经撤回的原来源仍可读，应以当前任务控制权限停止后续动作；继续或重新计算再核所需来源。商户/门店/账号独立业务可从自身有据身份开始，不能强制先补商场/品牌父关系才允许操作。历史恢复次数和旧单状态Case不是专业服务生命周期或恢复能力的上限。

日报/事件专报共享已有事件、工作及Artifact对象。其输入快照保目标/人群、查询和来源版、截止时点、覆盖/缺测、内容计量单位、纳排和基线版；分析返回逐主张证据/反证、事实或推断类别、指标分子分母与口径、比较资格、不确定性和受影响人群。事件时间线分发生、可观察、发现、核实、批准和实际行动。回应版本与具体发布授权、承诺/负责人/履约期限及真实回执关联；服务结果、复发/重开和调查反馈分别持久化。修订按原版影响集修改报告、提醒与批准，不替换所有正文或改写历史分母；没有可用调查或经营数据时返回确切缺口，不伪造满意度、NPS或经营损失。

## 十个研究与创作领域的操作合同

文档类操作由后台按目标、材料和明确修改意见执行；客户端提供文件预览、来源核对、业务参数、意见、版本与下载，不以本合同对象字段要求建设完整Office手动编辑器。DOCX/PPTX/XLSX/PDF保持各自原生结构、公式、母版、备注和未选内容；动态HTML交付须有真实数据交互与隔离预览。视频/播客继续由完整媒体编辑消费者操作。以下目标合同不因此升级为当前已实现/运行接口。

编号对应[创作合同](domains/creative.md)。这些工作可独立开始，不强制客户、主体、Campaign或先创建分析工作。

| 领域 | 专业输入/定位 | 操作和可继续结果 | 跨领域保全 |
|---|---|---|---|
| 1 研究与分析 | 问题、材料/来源版、页段表时码、计算变量、适用期间 | 阅读/检索/并读、引证/反证、专业计算和研究写作；主张保依据、方法、区间和不确定性 | 更正定位受影响主张/段落/导出；没有AI仍可读、算、写和导出 |
| 2 原创与跨媒介 | 受众、事实/禁用主张、品牌口径、创意意图、Persona及Topic/Series/Post/Campaign关联 | 构思、路线比较、叙事与完整内容、跨媒介/语言变体、局部修订；输出可编辑创作对象 | 事实主稿与创意表达分别版本化；父层更改不能无差别重生成全部变体 |
| 3 文稿/Word | 原件版、结构/样式、段落/表/引用/批注/修订锚点 | 原创及原稿编辑、精确修改、布局/批注/修订处理、原生文件往返 | 未选样式、页眉脚、媒体与备注等按完整合同保全；不能降为纯文本再导出 |
| 4 演示/PPT | 原件版、幻灯片稳定ID、对象ID/属性、母版/主题/媒体及备注 | 新建完整演示、指定对象编辑、布局/图表/媒体/动画、评论修订及原生往返 | 页序/对象/母版关系保全；仅用页码和新字符串不足以表达完整专业编辑 |
| 5 工作簿 | 工作簿/工作表/区域/表/命名范围、公式与缓存值、口径与计算环境 | 表格编辑/清洗、公式/图表/透视、参数情景、重算及原生交付 | 公式文本与结果分开，未重算/不支持函数明确；不能用缓存或CSV冒完整工作簿 |
| 6 PDF | 原件、页坐标系/旋转、文本/图像位置、批注及出处 | 阅读、检索、比对、批注、终稿检查与导出 | 批注版和原件分开；扫描OCR不冒原生文字，改稿应回到可编辑来源或明确新文档版 |
| 7 视频全生产 | 素材/代理/原片、脚本/分镜/take、媒体时基、轨道/片段/复合对象、色彩/声音/字幕 | 导入/整理、粗精剪、多轨/多机位、合成/调色/音频/字幕、预览渲染、工程交换和成片 | 代理编辑映回原片；时间线重排保源时间和关联字幕/声音；完整工程及依赖与成片分别交付 |
| 8 播客全生产 | 前采/脚本/录音/take、多轨/说话人、源时间、编辑决定、响度目标、章节/转写 | 录制、选段/剪辑、修复/混音/自动化、章节/节目包装、工程与多规格母版 | 文本校对不自行重新配音；编辑时间与源时间保映射，章节/字幕/转写跟随真实剪辑 |
| 9 声音与语言 | 指定文本/原声、语言/发音词典、说话人/音色和用途授权、风格及可编辑区间 | 转写/校对、翻译/本地化、获准配音及精确重做；返回时码/置信边界/真实音频和版本 | 人名/品牌/数值/禁用主张跨语种保真；音色包安装、授权、生成和质量分别核验 |
| 10 客户修订与成套交付 | 意见原文/作者/所评版/对象、交付成员与用途、批准和依赖快照 | 定位意见、查看影响、采用/拒绝并保理由、局部重交、冻结批次与复盘 | 文稿/PPT/工作簿/PDF/媒体及各语言成员一致，单件失败不伪称全包完成，旧批次可读回 |

## 七个系统与智能领域的操作合同

编号对应[系统与智能合同](domains/intelligence.md)。内部选路、模型和队列不成为用户必须操作的流程。

| 领域 | 请求与范围 | 返回及后续消费者 | 关键失败/恢复 |
|---|---|---|---|
| 1 采集 | 用户任务、问题/对象/期间、来源范围、必要字段/媒体、用途及资源预算 | 原始观察、可读材料、原生字段、来源/取得时点、覆盖与检查点；研究/经营/监测消费同一结果 | 分页/限流/部分/真实空结果/授权挑战分别返回；不能把前N条冒完整数据 |
| 2 浏览 | 用户/工作空间隔离会话、实际运行端/设备、受支持引擎/context与授权世代、官方登录目标、已授权页面动作、页面版本及选择范围 | 用户可接管页面、操作实际结果、必要上下文/文件引用；凭据只在明确选择的权威端保管，不随任务/设置同步 | 只连接不启动采集，任务内登录续原范围；换端/重连核身份和会话版；匿名请求/重定向不继承凭据，不重复外部动作 |
| 3 监测 | 对象/关系/话题/活动及规则版、来源/字段、条件/频率/期限/通知偏好 | 每源检查/空窗、原始变化、事件与信号、处置/转工作关联、通知实际结果 | 规则变更使旧评价/提醒写回失效，当前仍获准来源观测可保存供补评；主体移除/授权撤销按实际范围停止采集和源写回；恢复不宣称停机期间持续监测 |
| 4 行业AI | 当前业务目标、用户选择、许可输入与对象版、可用专业工具、模型任务及质量预算 | 引证判断/创意候选/专业操作建议或获准实际修改、模型/配方、结果引用和不确定性 | 迟到结果不覆盖人工稿；无模型时基础业务继续；训练、重载、留出评价和采用回滚分别成立 |
| 5 知识与学习 | 有用途的资料/反馈、正确对象/版本、范围/时效和审阅依据 | 可重建索引、可纠正记忆、偏好/经验证策略/训练候选分别存储；检索与专业消费者使用 | 更正/撤回定位派生和模型版本影响；索引相似或用户点赞不授事实/训练资格 |
| 6 运行与恢复 | 当前任务/输出检查点、权限世代、用户取消/恢复、精确备份/恢复目标 | 实际执行/持久化/可读回状态、受影响工作、恢复点及未核外部结果 | 失租旧Worker不可写回；权限变化后重核；文件/DB一致恢复，不能用进程健康代成果可用 |
| 7 既有扩展 | 已批准扩展身份/版本、schema、账号/目标/用途、具体调用和作用 | 正确归属的业务结果/来源/回执及实际限制，更新/禁用/回滚结果 | 发现不等启用；变更重核、禁用止新动作；原合法作品继续可用，不造第二模型/权限/来源系统 |

## 精确修改、长任务与交付协议

一次修改先固定父版本、选区和操作意图，再读取所需的原件和关系版。结果返回实际受影响对象、属性级差异、保留对象、输入/输出版本和未解决意见。撤销是对具体编辑历史的逆操作或新修订；不能把“恢复旧整份文件”冒局部撤销。对于并发编辑，能证明不冲突的独立对象可合并；其余返回当前版及差异，保本机草稿，不靠最后写入覆盖。

每个客户端提交在发送前保本地请求身份、父版和未发送编辑。超时后先查同一请求的持久化结果；同一意图的重试复用幂等键，输入变化或用户明确发起新的独立动作产生新身份。相同参数不等同一意图，例如再次刷新、重新录制或重新创作；未决外部效果仍先核对。同一键/不同输入冲突，已完成操作返回同一成果引用。当前初始客户端每次调用新建键的写法不能直接沿用为断线重试策略；不把“服务端支持幂等”误写成端到端已防重复。

Job按架构的实际状态、权威运行端世代、attempt/lease generation与检查点恢复；返回已完成/待处理单位及单位含义、当前结果引用、可取消性、错误原因和可行恢复动作。重建或恢复运行端后，旧租约和旧世代不能提交，即使任务ID相同；运行世代不是新的用户权限。并发任务按工作空间及资源公平安排，取消仅停止相应任务。流式输出带请求/对象、父版、递增序号和已确认位置；重连回放缺块，停止不触发另一次生成，文本暂显不冒已保存。专业对象只有经领域校验和保存读回后才能更新已保存状态。

导出请求绑定确切成果版、格式与用途参数；响应返回服务端确定的文件名、MIME、大小、导出状态和授权下载引用，客户端不能将所有未知格式强行命名为`.md`。工程包还要列确切成员/依赖/缺件及合法用途；文件写成、下载完成、原生应用打开/播放/重算、完整批次可交付分别验证。外部发送/发布/购买另有目标和批准范围；超时待核不能重发。

错误保可编程code与用户可理解原因/恢复动作。读取越权对象不泄漏其存在；版本冲突返回调用者可看的当前版与差异；权限撤销停止新动作，保允许保留的合法结果；来源更正/不可用不能用旧缓存冒最新。系统内部ID、栈、租约、模型路由等不进入正常业务文案。新增协议版本需向旧客户端明确可读/可改能力，不能默默丢弃未知专业字段后写回。

## 备份、迁入和升级的接口边界

工作区导出、整端备份、旧数据迁入、隔离恢复和切换权威端是不同授权动作，不能共享一个任意文件路径的写接口。请求引用获准的数据根/包对象、目标工作区及操作意图，实际路径由运行端解析并校验。普通用户选择自己的工作，不提交跨工作区表名、进程号、密钥或数据库命令。

恢复点返回备份对象身份、实际快照时点、数据库模式、包格式、所含范围、文件清单身份、完整性/引用核验结果、所需受保护恢复条件及缺失项。创建中、完整可核、失败与已验证恢复分别成立；没有实际恢复演练的完整包不能返回“已验证恢复”。恢复返回隔离目标及实际可继续的对象，切换另核当前写入者、授权、版本与未知效果；恢复点不能携带可直接复活的访问令牌或运行租约。秘密只经架构§5规定的明确受保护流程处理，不出现在普通响应或诊断包。

迁入返回每类原身份到新身份的映射、保持的版本/关系、冲突和未处理项；重试按同一迁入意图处理，不重复建立主体或导入同版作品。用户处理一个冲突不得隐式批准其余冲突。兼容信息返回实际API/对象/草稿/媒体工程/模型包的可读可写版本及必要更新动作；客户端据此保留原件和新稿，不靠丢弃未知字段达到保存成功。上述调用属于现有运行、文件、身份和领域消费者，具体接线必须与真实实现同时完成，不以空路由计可用。

## 当前基础接口与兼容迁入边界

以下保留原本地基础协议，供复用和兼容核对；它不是全产品实现清单。现有 `server/oevira/api.py` 与 `app/lib/src/api.dart`、`models.dart` 已有Work/Material/Job/文本Artifact消费者；`office_api.py`、`monitoring.py` 含独立的PPT修订与监测接口。静态源码存在不等运行或专业验收，未提交成果也不自动算线上发布。迁入时在这些实际消费者上接完整对象，不用“以后再接”掩盖前端仍只有文本。

已核的具体兼容点：`ArtifactItem`只显式承载body/citations/inputSnapshot，专业对象类型与选区须扩到其实际编辑消费者；`ApiClient.exportArtifact`目前只识别DOCX/Markdown文件名和MIME，必须同步所有已支持格式；PPT修订的`base_version/allowed_slides/changes`与监测的规则版/逐源检查可复用其边界，但不能代表完整Office和语义事件能力。旧训练审阅只接CTH/ZBMP而另一训练预检接受Shared的范围差异，按智能合同统一到同一工作空间/客户/专业任务，不再由旧领域字符串挡住独立创作。

### Local foundation wire format

JSON successes use `{ "data": ... }`; failures use
`{ "error": { "code": "...", "message": "...", "details": {} } }`.
Dates are UTC ISO-8601. IDs are opaque. Unknown values remain null with a reason.

### Session and transport

The local core serves the Web build and `/api` on the same loopback origin
(`http://127.0.0.1:8317` by default). It validates Host and Origin; no wildcard
CORS or implicit access to the user's personal browser profiles is permitted.
The launcher creates a short-lived, single-use local capability in its protected
data root. Web receives it in a fragment, clears the fragment immediately, then
exchanges it; fragments never enter HTTP requests. Windows receives it through
the local launch flow. A capability is not a production or platform credential.
No capability or session value is written to diagnostics or committed evidence.

- `GET /api/status`: `{product:"OEVIRA",version,setup_required,
  runtime:{api:"available",worker:{status,last_heartbeat_at}},ai:{status}}`.
  Worker status and nullable UTC heartbeat come from the running process;
  an available API does not by itself mean background jobs are running.
- `POST /api/local-session`: `{capability, display_name?}` creates or reconnects
  the local workspace. Returns `{token, user:{id,name}, workspace:{id,name}}`
  and sets an HttpOnly SameSite=Strict session cookie. The token supports the
  native client and is never placed in a URL or localStorage.
- `GET /api/session`: authenticated user/workspace; does not return the token.

All following routes require a valid cookie or Bearer session and derive
workspace authorization on the server. Another workspace's IDs return 404.
Member/source revocation is checked again before background writes. Remote
team accounts and invitations retain their full product requirements; a local
capability cannot be used as remote authentication.

### Work, inputs and comparison

| Method / path | Input | Returned data |
| --- | --- | --- |
| GET `/api/works` | — | Work list |
| POST `/api/works` | `{title,goal}` | Persisted Work |
| GET `/api/works/{id}` | — | Work with material/result references |
| PATCH `/api/works/{id}` | `{base_version,title?,goal?,status?}` | New Work version |
| GET `/api/materials` | — | Authorized Material list |
| POST `/api/materials` | multipart `file`, optional `work_id`; or `{title,text,work_id?}` | Stored original and actual parse state |
| GET `/api/materials/{id}` | — | Material and exact source segments |
| GET `/api/materials/{id}/file` | — | Original bytes as attachment |
| POST `/api/materials/url` | `{url,work_id?}` + `Idempotency-Key` | Durable public-acquisition Job |
| POST `/api/works/{id}/compare` | `{material_ids,criteria:[string]}` + `Idempotency-Key` | Durable Job |
| GET `/api/jobs/{id}` | — | Current Job |
| POST `/api/jobs/{id}/cancel` | — | Cancelled/requested state |
| POST `/api/jobs/{id}/retry` | — | Safely resumed Job |

Work fields: `id,title,goal,brief,status,version,material_ids,artifact_ids,created_at,updated_at`.
`brief` is optional structured context: `audience` and `deliverable` are text
(up to 2000 characters); `budget` contains optional finite nonnegative
`minimum,maximum`, optional `currency` and `notes`, with minimum <= maximum
when both exist. A notes-only budget is valid. `required_facts,forbidden_claims,
customer_requests` are lists of up to 50 text entries, each up to 1000 characters.
POST/PATCH work accepts it; updates retain omitted fields and use base_version.
These values are user requirements, not verified source facts. A comparison job
freezes the current goal, brief, criteria and material IDs in its input snapshot;
the resulting artifact retains that snapshot so later work need not re-enter it.
Material fields: `id,title,filename,status,reason,format,text,segments,sha256,created_at,work_id?`.
Segment fields: `id,label,text,start,end,kind`; offsets refer to the saved parser
text in Unicode code points, not UTF-16 indices. PDF page and Office object labels
remain inspectable. Unsupported/partial input is preserved without invented text.
Job fields: `id,kind,status,progress:{completed,total,label},artifact_id,material_id,error,work_id`.
`acquire_url` completes with a material ID, source URL, native/capture time,
method and limitations. It keeps original response bytes separate from the
readable text projection. A denied, unavailable or rate-limited source is not
automatically retried through an account or browser. Known access rules and
public-network destination checks apply to every request and redirect.
Progress is measured work, never decorative percentages.

The deterministic comparison preserves excerpts, dates, numbers and source scope.
Matching is not semantic verification, contradiction proof or an AI answer.
Users can change comparison criteria or remove materials and create a new result.
The result contains useful source-aligned rows and explicit limits; unavailable
inputs remain distinguishable from a successfully searched input with no match.

### Results, versions and delivery

| Method / path | Input | Returned data |
| --- | --- | --- |
| GET `/api/artifacts` | — | Artifact list |
| POST `/api/artifacts` | `{title,body,citations,work_id?}` | Persisted draft |
| GET `/api/artifacts/{id}` | optional `version=n` | Selected immutable version and history |
| POST `/api/artifacts/{id}/versions` | `{base_version,title,body,citations}` | New version |
| POST `/api/artifacts/{id}/restore` | `{base_version,restore_version}` | New version based on history |
| POST `/api/artifacts/{id}/exports` | `{version,format:"md"|"docx"}` + `Idempotency-Key` | Export with authenticated `download_url` |
| GET `/api/exports/{id}/file` | — | Actual validated export bytes |

Artifact fields: `id,title,body,version,versions:[{version,created_at,revision_note}],citations,work_id,input_snapshot,created_at,updated_at`.
Version creation accepts optional `revision_note` describing the user's change.
Citations: `{material_id,segment_id,quote}`; each must resolve to accessible exact
saved source text. Source text is untrusted content, never an instruction.

Stale `base_version` returns 409 `VERSION_CONFLICT` with current version data;
the client retains the user's draft. Restore adds a version and never rewrites
history. A repeated idempotency key with different input returns 409; a completed
request returns its persisted result. File writes, DB metadata and job completion
must survive interrupted execution without silently creating duplicate results.

Markdown/DOCX are the first result delivery bindings, not the full document or
media scope. Office object editing, PDF annotation, video, podcast, acquisition,
monitoring and AI use the same authorization and version foundations as their
corresponding implementations are connected. No empty endpoint represents them.

## 作品发布、模板与经验协议

以下是目标专业合同，实际HTTP路由与实现同次接入；不是当前已经存在的接口清单。沿现有`/api`、服务端身份、Artifact、文件和Job扩展，不复制隔离样机账户/存储。公共无登录读取仅限实际公开release，写操作必须有产品身份与正确作用域；token/Cookie、私稿路径和内部审核备注不出普通响应。

| 操作族与建议路由 | 输入/状态 | 实际结果及失败边界 |
|---|---|---|
| `POST /api/uploads`；`HEAD/PATCH /api/uploads/{id}`；complete/cancel | kind、目标空间、expected_size/hash、upload_id、offset、块摘要；成员来自服务端 | 持久偏移和同一上传身份；并发/摘要/长度冲突不推进；续传先读偏移；正式采用tus时按其全部适用协议实现，当前自有合同不冒tus兼容 |
| `POST/PATCH /api/publications`及submit | 源作品版或完成上传、metadata_revision、可见范围、许可/依赖、base_version和幂等意图 | 私有草稿及提交状态；上传/处理完成不公开；客户端不能提交official或审核通过 |
| moderation/appeal/review | 精确提交版、处理角色、结论/理由与复核父项 | 只读合法提交范围；审核角色不读未提交私稿；改内容/权利后旧决定不能自动沿用 |
| publish/withdraw/resolve | 精确release候选、有效审核和许可、发布/撤回意图 | 不可变发布版、真实公开读回或待核；超时先查同请求，不重复发布；下架停止索引和新取用 |
| `GET /api/releases/{id}`及授权media/renditions | 精确release、用途、Range/If-Range及适用清单/分片 | 正确200/206/416与HEAD、媒体MIME/大小/范围；所有派生端点仍核权限，模板原包不能借播放接口绕下载许可 |
| `POST /api/templates/{id}/instantiate` | template_release、目标Workspace、用途、替换参数、请求身份 | 新私有Artifact/对象映射/来源/许可快照/首版；同请求同输入返回同副本，不改源模板；不兼容依赖具体报告 |
| template更新/差异 | 原模板版、作者新版、个人稿版及选区 | 三方差异、保人工修改与冲突，不整体覆盖；目标文档生成/改版或媒体编辑消费者必须按类型消费专业对象 |
| experiences创建/修订/提交 | problem、method、steps、conditions、pitfalls、results、evidence、所关联release与父版 | 带出处的可编辑经验和版本；作者陈述/实测/推断/未知分开，不自动入训练 |
| discovery/creator/collection/queue/comment/report | 按授权/公开范围、查询快照、release、锚点、真实用户意图分页 | 不泄私域，不把收藏当授权；评论/播放计数有定义，拒绝伪造指标；举报不自动删除原件 |

Upload、media processing、moderation、publication和private editing各有独立状态。发布version不能替代源Artifact版本，改公开标题也须按元数据版CAS。PrivateWorkRef不随公共响应泄漏；SourceRevision与公开回链仅在权利允许范围展示。

媒体声明与实际检测分开；Rendition包含源revision、recipe版本、尺寸/编码/声道/字幕、manifest及成员校验、readiness。TemplateManifest包含kind/schema、EditableGraph、ReplacementSlot、DependencyManifest、LicenseSnapshot；许可记录观看/下载/改作/商用/再分发/署名/有效期，训练独立。普通保存/编辑不因此增加重复批准，真正公开和外部效果仍由精确意图约束。

协议研究来源：tus https://tus.io/protocols/resumable-upload 只指导明确采用的续传行为；HTTP范围读取按 https://www.rfc-editor.org/rfc/rfc9110.html 。采用协议名、HEAD/PATCH方法或状态码不是完整互通/安全/容量证明，客户端与服务端版本及负向测试必须对应。
