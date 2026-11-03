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
| 3 文稿/Word | 原件版、结构/样式、段落/表/引用/批注/修订锚点 | 原创写作与文稿编辑；系统生成或按意见精确改版、布局/批注/修订保全、预览/检查/下载、原生文件往返 | 未选样式、页眉脚、媒体与备注等按完整合同保全；不能降为纯文本再导出 |
| 4 演示/PPT | 原件版、幻灯片稳定ID、对象ID/属性、母版/主题/媒体及备注 | 系统生成完整演示、按意见限定对象改版、布局/图表/媒体/动画与备注保全、预览/检查/下载及原生往返 | 页序/对象/母版关系保全；仅用页码和新字符串不足以表达完整专业改版 |
| 5 工作簿 | 工作簿/工作表/区域/表/命名范围、公式与缓存值、口径与计算环境 | 系统生成或按意见改版工作簿、清洗、公式/图表/透视、参数情景、重算、预览/下载及原生交付 | 公式文本与结果分开，未重算/不支持函数明确；不能用缓存或CSV冒完整工作簿 |
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

已核的具体兼容点：`ArtifactItem`只显式承载body/citations/inputSnapshot，专业对象类型与选区须扩到其实际编辑消费者；`ApiClient.exportArtifact`已按服务端同一格式集合`md/docx/pptx/xlsx/pdf/html`处理文件名和MIME（`app/lib/src/api.dart:1886`、`:1989–1994`；服务端`server/oevira/api.py:1354`），新增格式须两端同改；PPT修订的`base_version/allowed_slides/changes`与监测的规则版/逐源检查可复用其边界，但不能代表完整Office和语义事件能力。旧训练审阅只接CTH/ZBMP而另一训练预检接受Shared的范围差异，按智能合同统一到同一工作空间/客户/专业任务，不再由旧领域字符串挡住独立创作。

### Local foundation wire format

JSON successes use `{ "data": ... }`; failures use
`{ "error": { "code": "...", "message": "...", "details": {} } }`.
Dates are UTC ISO-8601. IDs are opaque. Unknown values remain null with a reason.

### Session and transport

The local core serves the Web build and `/api` on the same loopback origin
(`http://127.0.0.1:8317` by default, matching `scripts/start.ps1` and `app/lib/src/platform/launch_context_io.dart`; `-Port` selects another loopback port, as the 8329 development run did). It validates Host and Origin; no wildcard
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

### Account lifecycle routes

Account operations use the same `users`, `sessions`, `memberships` and Store
as the local workspace. Success remains `{data:...}` and errors remain the
common error envelope. The current app factory injects email delivery only
when explicit SMTP environment settings are complete. It uses STARTTLS by
default or implicit TLS when configured; cleartext SMTP is accepted only for a
loopback test relay. `OEVIRA_PUBLIC_BASE_URL` is required to create clickable
workspace invitation links. An SMTP `250` acceptance means the transport
accepted the message, not that the recipient's inbox received it. The local
API binds loopback only; a public OAuth callback/deployment is not enabled by
setting a public URL alone. `GET /api/account/capabilities` is the availability
source; missing mail or callback configuration is reported unavailable, never
replaced by a test-only mailer or client-only OAuth success.

SMTP settings read by `create_app` are `OEVIRA_SMTP_HOST`, `OEVIRA_SMTP_PORT`,
`OEVIRA_SMTP_FROM`, optional `OEVIRA_SMTP_USERNAME` and
`OEVIRA_SMTP_PASSWORD` (both or neither), `OEVIRA_SMTP_SECURITY` (`starttls`,
`tls`, or loopback-only `none`), and optional `OEVIRA_SMTP_FROM_NAME`.
Google/Apple server values use `OEVIRA_GOOGLE_CLIENT_ID`,
`OEVIRA_GOOGLE_CLIENT_SECRET`, `OEVIRA_GOOGLE_REDIRECT_URI` and the matching
`OEVIRA_APPLE_*` values. Provider callback URLs must exactly equal the product
base URL plus `/api/account/oauth/{provider}/callback`; Apple additionally
requires a public HTTPS domain. These settings are read from the process
environment by the normal launcher; secrets are not returned by capabilities.

| Route | Access | Input/result |
|---|---|---|
| `GET /api/account/capabilities` | Anonymous; an included valid session is read | Reports email, OAuth, export and deletion availability. |
| `POST /api/account/email/register` | Anonymous | `{email,display_name?,password?}` → accepted-if-eligible plus one-time `reference`, masked address, `expires_at`, `resend_at`; code is delivered only by configured mail. |
| `POST /api/account/email/verify` | Anonymous or existing local session | `{reference,code}` → `email_verified`; it confirms registration but does not silently create a product session. |
| `POST /api/account/email/login/request` | Anonymous | `{email}` → uniform accepted-if-eligible response, regardless of account existence. |
| `POST /api/account/email/login/complete` | Anonymous or local session | `{reference,code,workspace_id?}` → authenticated session or an expiring workspace-selection challenge. A current local session produces a 10-minute local-data choice preview plus a transient local return session, so the user can finish active local operations before choosing. |
| `POST /api/account/password/login` | Anonymous or local session | `{email,password,workspace_id?}` → authenticated session or workspace-selection challenge. A current local session receives the same explicit local-data choice. Email equality does not bind an OAuth identity. |
| `POST /api/account/password/reset/request`, `POST /api/account/password/reset/complete` | Anonymous | Request uses `{email}`; completion uses `{reference,code,new_password}`. |
| `POST /api/account/oauth/{google\|apple}/start`, `POST /api/account/oauth/{google\|apple}/complete`, `GET/POST /api/account/oauth/{google\|apple}/callback` | Start/complete may begin without a product session; linking requires the current valid session; callback is bound to the configured provider URL and a pending state | Start uses `{code_verifier,intent}`; callback accepts the provider's GET query or form POST and returns only `code/state` (or a normalized error) in the product URL fragment. The Web client keeps the PKCE verifier in tab-scoped session storage, removes callback values from the URL, and completes using `{code,state,code_verifier}`. Provider claims are locally verified; matching email never merges or links accounts automatically. Native callback consumers are not enabled. Completing from a current local session returns the same explicit local-data choice. |
| `POST /api/account/local-data/preview`, `POST /api/account/local-data/choice` | Account session bound to the choice | Preview uses `{reference}`. Choice uses `{reference,choice}` where choice is `keep_separate` or `merge_local_data`; merge repeats all conflict/CAS/ownership checks inside the transaction. `keep_separate` returns a transient local return-session token without adding an account workspace membership. |
| `GET/PATCH /api/account/preferences` | Account session | Model-improvement consent is versioned; update uses `{model_improvement_consent,expected_version}` and reports `training_started:false`. |
| `GET /api/account/login-methods`, `GET /api/account/sessions` | Account session | Returns current login-method state and the caller's active sessions without credentials or token hashes. |
| `GET /api/account/workspaces`, `POST /api/account/workspaces/{workspace_id}/switch`, `GET /api/account/workspaces/{workspace_id}/members` | Account session with active membership in the selected workspace | Lists accessible workspaces and active members by display name/role/join time. Switching revokes the current workspace session and issues a session for another existing active membership; it never moves data or grants a new membership. |
| `GET /api/account/workspaces/{workspace_id}/invitations` | Active workspace owner | Lists invitation IDs, recipient email, requested role, delivery state and expiry; never returns the secret invitation token. |
| `POST /api/account/workspaces/{workspace_id}/invitations`, `POST /api/account/workspaces/{workspace_id}/invitations/{invitation_id}/resend`, `DELETE /api/account/workspaces/{workspace_id}/invitations/{invitation_id}` | Active workspace owner; SMTP and product URL must be configured | Create uses `{email,role}` with assignable roles `editor` or `viewer`; owner status is granted only by explicit transfer. A transport-accepted invitation returns `delivery_semantics=smtp_accepted_not_proof_of_inbox_delivery`; an unknown send outcome stays explicit and requires owner inspection/rotation. Resend invalidates the previous token first. |
| `POST /api/account/invitations/preview`, `POST /api/account/invitations/accept` | Preview is token-bound and anonymous; acceptance requires a signed-in account with a verified matching email | Preview uses `{token}` and returns workspace name, masked invited email, requested role and expiry without consuming the invitation. The email link opens the product and preserves intent through sign-in. Acceptance also uses `{token}`, consumes it and creates/reactivates a row in existing `memberships`; the user must explicitly accept and same-email login methods are never auto-linked. Returns a session in the joined workspace. |
| `PATCH /api/account/workspaces/{workspace_id}/members/{user_id}`, `POST /api/account/workspaces/{workspace_id}/owner-transfer` | Active workspace owner | Role changes carry `expected_role` for CAS. Owner transfer carries `expected_owner_user_id` and `new_owner_user_id`; the previous owner becomes editor. The last active owner cannot be removed/demoted. |
| `DELETE /api/account/workspaces/{workspace_id}/members/{user_id}`, `POST /api/account/workspaces/{workspace_id}/leave` | Owner removes a member; a user leaves their own membership | Deactivates membership, revokes only that user's sessions in this workspace, invalidates in-flight job leases, pauses their monitoring and closes/scrubs their browser sessions. Legal works/versions are preserved. A last owner must transfer ownership first; if a non-owner leaves their last workspace, a new empty personal workspace keeps the account usable without moving old data. |
| `POST /api/account/exports`, `GET /api/account/exports/{id}/download` | Recent-auth account session to create; same account to download | Creates a ZIP of account-owned records/files in member workspaces. Download checks owner, expiry, exact size and SHA-256; links expire after 24 hours. Secrets and external browser authorization state are excluded. |
| `POST /api/account/deletion/request`, `POST /api/account/deletion/confirm`, `GET /api/account/deletion/status`, `POST /api/account/deletion/cancel/request`, `POST /api/account/deletion/cancel` | Request/confirmation/status use an account session and email proof; cancellation request/completion can recover a scheduled account | Requires a completed export and a clean owner handoff preflight. Last workspace/commission owner, active browser/recording operation or unfinished upload blocks scheduling with a precise resolution. Confirmation revokes sessions, stops lease-fenced writes, pauses owned monitoring and clears saved browser auth state. After 30 days Worker removes private workspaces and transfers shared current ownership to surviving owners; historical authors keep separate opaque IDs with an anonymous display name. Public Publication/Release source content and version copies are withdrawn/erased under SPEC §20.4. |

Account challenge references are random, single-use and purpose-bound. Codes
are salted hashes with server-side expiration, resend cooldown, request and
attempt limits. Incorrect code returns `INVALID_VERIFICATION_CODE`; an
expired code returns `VERIFICATION_CODE_EXPIRED`; resend cooldown/rate limits
return `429`. On anonymous account routes an expired cookie is treated as
anonymous and cleared so login cannot become a redirect loop. An explicitly
invalid Bearer token remains `401`; public community routes continue to
authenticate every supplied session so private/team reads are not downgraded.

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

Comparison accepts two or more distinct authorized materials without the old
10/20-material or 500-citation ceilings. Database reads are batched without
changing input order. Processing budgets remain explicit: an over-budget task
keeps its selection and reports the actual input size rather than publishing a
truncated result. Evidence matching reports omitted hits separately from the
candidate analysis; it does not claim those omitted excerpts were analyzed.

### Subject directory and monitoring binding

`GET/POST /api/business/subjects` lists or creates `brand`/`company` subjects;
names are at most 200 characters. Creation returns `{subject,possible_matches,replayed}`.
`GET /api/business/subjects/{id}` returns the subject with `evidence` and
`relationships`; evidence text is `source.excerpt`. PATCH/DELETE and POST
`/{id}/restore` use `base_version`. Evidence and relationship endpoints attach
exact source/identity/time objects, not a third subject type. Same names are
candidates for review, not implicit merges. Removal preserves history; re-add
requires fresh review.

Monitoring configuration accepts nullable `subject_id`. An explicit binding
must resolve in the current workspace; free-text scope never creates one.
Removing a bound subject pauses the corresponding rules and stops their new
execution transactionally, preserving already acquired materials and history.
Re-adding a subject does not resume its monitors without an explicit action.
Existing unbound monitors remain valid during the additive v4-to-v5 migration.

### CTH Cases and shared source observations

| Method / path | Input | Returned data |
| --- | --- | --- |
| GET `/api/business/cases` | optional `state=open|closed|all`, `subject_id` | Workspace-scoped Case records |
| POST `/api/business/cases` | `{subject_id,submission_id,title,question,source_refs:[{material_id,source_native_id?}]}` + `Idempotency-Key` | New open Case, exact saved source Material references, review state |
| GET `/api/business/cases/{id}` | — | Case, active Subject link, saved source references, provenance and current review state |
| GET `/api/business/cases/{id}/versions` | — | Immutable Case snapshots |
| PATCH `/api/business/cases/{id}` | `{base_version,title?,question?}` | New Case version; stale writes return 409 |
| GET `/api/business/relationships` | — | Unique relationship edges in the current workspace |
| GET `/api/business/subjects/{id}/metric-observations` | — | Metric history associated with that Subject |
| GET `/api/business/materials/{id}/metric-observations` | — | Metric history associated with that Material |

Case state, source review, Subject identity review and historical creation receipts
are separate. A historical `app_evidence_records` Case-creation receipt is not a
business-fact Evidence item; source Materials remain independently addressable.
`has_tenant` retains venue/company-to-brand-or-merchant direction. Merchant literals
remain candidates; `market_activity` is a market-level observation and does not
establish current China operations. Incoming and outgoing Subject views refer to
the same stored edge, while the workspace relationship route returns each edge once.

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
| POST `/api/artifacts/{id}/exports` | `{version,format:"md"|"docx"|"pptx"|"xlsx"|"pdf"|"html",audience?:"internal"|"client"}` + `Idempotency-Key` | Export with authenticated `download_url` |
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

### Word original-file revision

GET `/api/materials/{id}/document` inspects body/table paragraphs without
rewriting the source. POST `/{id}/document-revisions` accepts
`{title?,work_id?,revision_note?,changes:[{object_id,before,after}]}` and an
Idempotency-Key. It returns `{artifact,document}` with real DOCX download data.
GET `/api/artifacts/{id}/document?version=n` opens a saved revision; POST
`/{id}/document-revisions` requires `base_version`. Document object IDs bind to
the exact file hash and structural location, so edits from a different source
version cannot silently target another paragraph. The unchanged text retains
its original run styling, and unselected objects and package parts are preserved.
Unsupported structures stay read-only; signed files reject edits that would
invalidate their signature. These are backend file-processing operations for automatic generation and
requested revisions, not a requirement for a complete manual Office editor.

The client preserves an uncertain save's exact request and idempotency key
before retrying, including across reopening. An explicit rejection permits
correction; a lost response is not evidence that the first write failed.
Downloads use authenticated `/api/document-exports/{id}/file` and return real
DOCX bytes. Generic text editing must not rewrite a document-backed Artifact.

Markdown/DOCX are the first result delivery bindings, not the full document or
media scope. Office object editing, PDF annotation, video, podcast, acquisition,
monitoring and AI use the same authorization and version foundations as their
corresponding implementations are connected. No empty endpoint represents them.

## Media语音生成与转写接口

以下Media接口已由同一Media owner注册并使用workspace身份、现有Material上传和版本链；接口成功不等于语种质量或来源权利通过。

| 路由 | 请求与真实行为 | 保存、限制与失败 |
|---|---|---|
| `GET /api/media/speech/capabilities` | 返回本机实际可执行的Flite/eSpeak voice、语言映射和Whisper CPU运行时版本/哈希；缺件/不匹配显示不可用，不下载、不联网回退 | 声音自然度/发音、ASR准确率与词级时间精度均单独评估，不从voice清单推导质量 |
| `POST /api/media/speech/synthesize` | `{engine?:"ffmpeg_libflite"|"espeak_ng", text, language, voice}`；按明确引擎/语言/voice生成完整合成WAV和来源声明。Flite现限ASCII英文；eSpeak接受运行时真实列出的voice；两者保真实原文、引擎/voice和合成标志 | 不改项目、不建Material、不发布；客户端以原`POST /api/uploads`分块写入Material，再显式调用项目`sources`添加。同一文件后续保存有Material SHA和WAV内嵌声明； rights待核、synthetic、非本人克隆，长度超限整体拒绝不截断 |
| `POST /api/media/projects/{project_id}/transcriptions` | 请求`version/source_asset_id/audio_stream_index/audio_channel_index/task`及可选`language_hint`，并带`Idempotency-Key`。冻结输入来源/哈希/单一源声道/音流/任务，进入同一Worker Job；仅当前打开版本可创建 | 不改音频；Whisper base在CPU本地以30秒块与1.5秒上下文生成未审阅候选，持久化各完成块。已检测PTS缺口/重叠则该次转写失败，源和工程保持不变 |
| `GET /api/media/transcriptions/{id}?word_offset=&word_limit=`；`POST /api/media/transcriptions/{id}/resume` | 分页读取源SHA/声道/源样本范围/PTS时基、候选文本/估计词界、模型版本及任务状态；只对cancelled/interrupted任务允许resume | 候选默认`review_state=unreviewed`并注明词界估计、质量NOT_EVALUATED；恢复保留已完成块，从最后完整块续做 |
| `POST /api/media/projects/{project_id}/transcriptions/{id}/adopt` | `{base_version, word_corrections?, words?, review_state?}`；同一project当前版本CAS。`word_corrections`按候选`word_id`只改文字/样本范围；若提交`words`，须保留完整候选的有序ID集合 | 仅采用时写入新的可撤销Timeline版；旧素材/音频未改。实际word-cut必须另行选确切片段/词实例并调用`word-cut-previews`；translate结果不作为源词时间切音频。 |

转写`task`为`transcribe`或`translate`；language hint使用Whisper语言代码。`audio_channel_index`必选，立体声/多声道不折叠求平均。对流内非连续包PTS目前明确拒绝，避免扁平化后错误映射；该限制不阻止原件播放、人工对齐或手工剪辑。产品取消使用现有`POST /api/jobs/{job_id}/cancel`，不得用HTTP请求超时推断Job失败。

## 作品发布、模板与经验协议

本节列当前Server注册的社区/发布/media路径及其边界，沿现有Store、身份、Artifact、Material、Media工程和Job工作；不建立第二账户、项目、媒体存储或审核应用。除本节列明的公开读取与公开媒体grant外，接口要求有效产品session并按Workspace/Work/Material当前成员权限授权；任何公开路由仍验证所带Cookie/Bearer，防止过期令牌被降级成公开匿名访问。撤回/过期许可使release及其媒体取用失败。正式公网托管/CDN未配置，release响应固定`delivery=local_review_only`；配置一个URL本身不改变此状态。

| 当前路由 | 输入与实际结果 | 关键失败与边界 |
|---|---|---|
| `GET /api/community/capabilities`；`GET /api/community/media/sources` | capability报告审核/精选角色及`delivery=local_review_only`、ffprobe状态；媒体来源返回当前session可读的Material、Media工程、已完成render和可读字幕/章节sidecar，不返回服务端文件路径 | capability不是托管/审核质量证明；源权限在创建、读/写、审核、range读取时再次核对；不可读原件/旧工程源明确返回不可用 |
| `GET /api/publications`、`GET /api/publications/{id}`；`POST /api/publications`；`PATCH /api/publications/{id}` | 私有列表/精确草稿版本。创建支持`artifact_id/artifact_version`，或`source_kind=media_material|media_render|media_project`及精确`source_id/source_version/media_selection`。种类为`work/template/experience`，范围为`private/team/unlisted/public`；媒体manifest只从已保存原件/完成render/工程版生成 | 客户端不能设置official、review或approved；私有输入快照不复制到release；本机路径、原Material/Artifact ID只在获准私有响应中存在；可见范围改变不自行发布 |
| `POST /api/publications/{id}/media-source-revisions` | 只允许Media template草稿/驳回稿使用更高的同项目确切Media版；提交`base_version/source_version/media_selection/content`，由服务端重建槽、依赖和媒体manifest，重新检查精确权利 | 并发修改返回版冲突；不接受客户端伪造的`media/template`快照；提交或已审核版本须先按正常状态流程重开草稿，不沿用旧审核 |
| `POST /api/publications/{id}/submit`、`GET /api/moderation/submissions`、`GET /api/moderation/submissions/{id}/media/{member}`、`POST /api/publications/{id}/review` | submit带精确`base_version`和`preview_acknowledged=true`；审核队列只给服务端审核角色已提交快照/媒体成员；结论`approved/rejected`加理由和确切版；媒体预览只读提交版中的所选成员 | 上传不等已审核或公开；作者不能审核自身提交；修改内容、媒体、依赖或权利后旧审核无效；队列不暴露源Material ID、文件路径及私有输入 |
| `POST /api/publications/{id}/publish`；`POST /api/publications/{id}/withdraw`；`POST /api/publications/{id}/appeals` | publish带已批准精确`base_version/review_id`和`Idempotency-Key`；发布新不可变release并撤回同一publication旧release；withdraw立即撤销新取用；appeal关联具体review及理由 | 同键异输入冲突；下架停止Discovery/新媒体grant；SMTP/公开托管未配置不产生虚构外部效果 |
| `GET /api/discovery?q=&kind=&order=&before=`；`GET /api/creators/{id}`；`GET /api/community/follows`；`POST /api/creators/{id}/follow` | Discovery支持`latest/following/featured`，limit为1–100，返回`next_cursor/order/explanation`；featured须有明确审核角色/原因/排序窗口。创作者名来自公开署名而非账户邮箱/姓名；follow输入`{follow: bool}` | following需登录；featured不是热度或成效统计；release需当前public、许可有效且未撤回；无发布的创作者页不可枚举 |
| `POST /api/releases/{id}/items`；`GET /api/community/items` | `kind=collection|queue`、非负`position`、可选有限`progress_seconds`；续播位置仅适用于有确切时长的媒体并须在programme duration内；返回当前可访问release或`availability=unavailable` | 非媒体/时长未知不得伪造进度；撤回后保存项保留为不可用，不绕过release权限 |
| `GET/POST /api/releases/{id}/comments`；`POST /api/releases/{id}/reports`；`GET /api/moderation/reports`；`POST /api/moderation/reports/{id}/resolve` | 文本评论可锚精确UTF-8正文区间；媒体评论锚`member_id/start_ms/end_ms`。举报需`Idempotency-Key`及用户理由；审核报告仅能dismiss或withdraw_release并写理由 | 评论/举报身份为不含联系信息的内部UUID；举报不自动证明或删除内容；每用户每日20条限频，report state冲突需刷新审核队列 |
| `POST /api/moderation/releases/{id}/feature`；`DELETE /api/moderation/releases/{id}/feature` | 审核角色按理由、rank 1–10000、时区明确的`starts_at/expires_at`作手工精选；可撤销 | 仅当前有效public release可精选；精选排序不使用伪造播放/收藏/效果数 |
| `POST /api/templates/{release}/instantiate` | 文档模板接受工作、用途及slot参数，生成新私有Artifact；`oevira-media-template/1`接受用途和每个公开slot ID到授权Material ID的替换映射，可选Work/title，生成真实MediaProject初版 | 源发布必须当前可取用且适用许可允许改作/用途；替换媒体须匹配kind、流数和工程实际使用时长；精确幂等重放返回同一私有副本；撤回后不创建新副本 |
| `POST /api/media/projects/{id}/template-updates/preview`；`POST /api/media/projects/{id}/template-updates/apply` | preview带私有项目`base_version`和同一Publication lineage的更新release；返回`preview_hash/changes/conflicts/required_source_replacements`。apply带同hash、逐冲突`keep_current/take_template`、可选新slot映射及幂等键 | 更新release与Media源版本都须高于现有血缘；base/hash过期或有未解决冲突拒绝；CAS失败不写入；按稳定对象键三方合并并保留用户手改 |
| `POST /api/releases/{id}/media/{member}/playback`；`GET/HEAD /api/community/media/streams/{token}` | grant指定`purpose=view|download`和可选share token；返回仅用10分钟的内部stream URL；文件流支持HTTP Range/HEAD、正确MIME、ETag、Content-Range、`Accept-Ranges`及`no-store`。字幕/章节也是各自有权利检查的release成员 | private/team流每个Range请求重核当前session/成员；过期/撤回/许可失效令牌404/拒绝；当前没有HLS分片、CDN或正式公网托管，接口返回local_review_only，不等于长时、多码率、并发/外网分发已验收 |
| `POST /api/uploads`；`HEAD/PATCH /api/uploads/{id}`；complete/cancel | kind、目标空间、expected_size/hash、upload_id、offset、块摘要；成员来自服务端 | 持久偏移和同一上传身份；并发/摘要/长度冲突不推进；续传先读偏移；正式采用tus时按其全部适用协议实现，当前自有合同不冒tus兼容（规划合同；以代码核实为准，未实现前不冒已交付） |
| experiences创建/修订/提交 | problem、method、steps、conditions、pitfalls、results、evidence、所关联release与父版 | 带出处的可编辑经验和版本；作者陈述/实测/推断/未知分开，不自动入训练（规划合同；以代码核实为准，未实现前不冒已交付） |

`experience`内容的`experience`字段精确保留`problem/mechanism/steps/conditions/counterexamples/results/limitations/evidence/claim_type`；step至少一条，非`unknown`结果主张必须带evidence。响应明确`independent_validation=not_established_by_publication`及`training_eligibility=not_granted`。作品、模板、经验均使用同一私有Publication状态机/审核/release，不把作者报告、观察、推断和未知互相改写。

每个发布依赖以精确ID/version/SHA、included与单独view/download/adapt/commercial/redistribute/training/attribution/expiry记录权利；媒体真实流探测/章节/字幕边界复核，依赖必须与分发成员用途相符。公开视频API匿名响应把依赖ID映射为`dependency-N`，去除服务端路径、Work/Material ID、原稿输入快照和工程原manifest。Media-template公开graph仅含槽ID、需求约束、时间线结构、可编辑章节/字幕/效果和替换说明，不携带原始媒体字节/源ID；私有原manifest仅为准确审查保留，账号删除时擦除源信息并保留收件人的合法Media副本、许可快照和更新时间记录。

上传仍使用现有`/api/uploads`分块偏移/摘要/完成协议，不声称tus互通。当前本地测试已验证有界WAV上传、许可检查、Range 206、VTT/章节读取、Source-version CAS、模板复制、用户冲突保留、源撤回后精确重放及账号删除后合法副本/许可保全；这不能代替正式公开托管、HLS/自适应码率/长媒体压力、多浏览器解码或独立内容审核质量验收。HTTP范围读取遵[ RFC 9110](https://www.rfc-editor.org/rfc/rfc9110.html)。

## 浏览承载、来源读取与监测的统一协议

浏览仍由 `browser.py / browser_api.py` 的同一 BrowserSession、权限、受保护状态和 Material 保存操作拥有。默认引擎为 Chrome；每个会话拥有独立 Chrome 进程和非持久 context，跨用户不共享进程音频。Windows 宿主嵌入该会话的真实窗口，Web 宿主通过 WebRTC 传输实际页面画面与输入；不对客户端暴露 CDP 端口，不改写目标站 CSP/DOM，也不以截图轮询代替实时浏览。明确的外部打开动作使用用户的外部浏览器；默认行为留在产品内。

| 接口 | 输入与实际结果 |
|---|---|
| `POST /api/browser-sessions/{id}/host/offer` | 当前 `page_id`、CSS viewport 与完整 offer SDP；同一成功 envelope 返回 answer SDP、`host_id`。信令沿当前认证，实时通道持续重核会话/工作/客户权限；不配置外部 STUN/TURN 服务。 |
| `POST .../host/native`、`POST .../host/{host_id}/heartbeat`、`POST .../detach` | 本机宿主取得经同会话目标/PID 核定的窗口；心跳返回当前页、标签和事件。隐藏、尺寸调整与关闭宿主不另建会话；正常分离隐藏 Chrome 窗口，继续/恢复仍走原 BrowserSession。 |
| `GET/POST .../host/tabs` | 读取标签；新增、切换、关闭携带 `base_version` 和 `Idempotency-Key`，提交同一会话的保护状态。 |
| `POST .../host/{host_id}/uploads`、`POST .../uploads/cancel` | 只接受当前文件选择事件的 `chooser_id` 与用户选定文件字节；取消保原选择，不接受客户端裸路径。 |
| `GET .../host/{host_id}/downloads/{download_id}`、`POST .../save` | 以会话内下载对象取得实际文件，或通过同一 Material/Work 保存、解析、来源记录和幂等回执承接。部分/失效下载不当成完整文件；成功结果可重开。 |
| `GET /api/browser-history`、`POST /api/browser-history/clear`、`GET /api/browser-bookmarks` | 用户/工作区私有的安全地址与标题；历史最多 1000 项，清理记录不删除已保存 Material/Work，也不冒清除网站登录。 |
| `POST /api/browser-sessions/{id}/bookmark`、`DELETE /api/browser-bookmarks/{id}` | 服务端核当前页保存书签；同 URL 去重，删除核书签版本与归属。含临时授权参数的脱敏地址不能伪造可用的登录续接链接。 |

Web 的有序 `input` 通道绑定 `sequence / page_id / page_revision`；鼠标、滚轮、键盘、IME 文本和网页弹窗分别分型。陈旧页面和未知效果不自动重放。网页确认框可保持原点击等待，同时交付事件并接用户明确答复；不默认确认。复制读取当前网页/输入框选区，粘贴接用户本机明确提供的文字，不能读取服务器系统剪贴板。Web 声音来自单一浏览进程的独立音频轨，能力缺失单独返回；画面不签音频成功。Native 宿主服从页面可见性与 `TickerMode`，隐藏时不能盖住产品导航、资料或对话层。

公开 URL 读取继续使用 `acquire_url` 和原共享 Worker。HTML 原始响应与主内容投影分开；RSS/Atom 保逐条来源、作者、原生时间及实际观察到的分页，`entries_truncated / links_truncated` 明示范围。`text_sha256` 是文本投影身份，不是原站原件证明。现有本机代理使用合格数值地址建立隧道；遇网络客户端的合成地址时只向当前系统配置的 DNS 重新核公网地址，仍禁止私网/保留地址和重绑定，不新增 DNS 服务或修改系统配置。HTTPS 沿系统信任链验证，不能关闭证书核验。

监测 `work_id` 与 `follow_source_links` 是不同输入，后者默认关闭。`GET /api/works/{id}/monitor-source-candidates` 仅提供该工作已有资料的可选来源、去重、排除原因与分页；用户选定的 URL 才进入规则。启用后续发现时，仅沿已选来源同站的获准链接/Feed 条目进入同一采集队列，保父资料、来源、规则版与发现时刻；跨站链接不自动扩大范围，预算和截断不冒全站覆盖。来源读取水位与规则评价水位分别持久化，旧规则迟到结果不能吞掉新规则下应复核的变化。变化证据区分新增、删除和改写，保确切前后文本；HTML 字节/空白变化不能单独冒内容变化。`Retry-After` 与实际失败原因沿原 Job 记录保存，下一次允许检查时间可读回；重开后先收取已完成结果再安排新检查，避免重复效果。

## 业务成果、委托和发布的实际核心协议

`business_execution`使用已有Work、Material、Artifact及其不可变版本，不增加第二套业务成果库。`POST /api/works/{id}/business-results`接收`operation`、业务`inputs`及可选`citations/subjects/relationships/tables/title`；每次写入带`Idempotency-Key`。服务端核当前成员和客户委托、资料权限、主体/关系版及文件完整性，冻结输入后在数据库写事务外计算，提交时重核要求与来源。结构化结果的每段有稳定`id/title/body/data`；正文表格和结构化数值同源，现有`/api/artifacts/{id}/exports`按同一格式集合提供Word(docx)、PPT(pptx)、XLSX、PDF、动态HTML及适用时的Markdown(md)。业务成果导出可传`audience:"client"`生成客户视图；缺章节客户正文、待复核客户正文或客户限制清单时拒绝导出，不回退内部正文。客户视图只冻结`client_body`、客户适用限制和逐项明确授权的`client_citations`，不携带内部账本、原始业务快照或私有引用；缺省`internal`保持原行为，幂等键按视图区分。

`GET /api/business/operations`列出当前可调用操作。经营、消费者、财务情景、财报事实/更正/季度选择、策略、媒介、合作、活动排期、实验/归因/增量、事件与服务回应各保自己的输入和结果结构。调用者不得把枚举本身当完整专业能力或要求用户手填算法JSON。保存的CSV/TSV/XLSX可以用`tables`指定材料、工作表、业务字段到源列的映射和已确认的逐行上下文；数值类型明确，缺格为null，全量非空行计数，不因列重排改口径。财报操作只从已授权Material读原字节/原文，成果保存来源hash、事实、位置及采用条件，不把原件字节塞进JSON。

`POST /api/works/{id}/business-jobs`将相同业务请求放入现有Job/Worker。操作返回202及持久Job；读取、取消、失败重试沿现`/api/jobs/{id}`。业务要求版、成员/客户/来源、租约世代及取消状态在提交前再次核对，已保存结果凭同键读回。取消、失租和失败不生成成功成果；数值计算不占数据库长写锁。

`POST /api/business-results/{id}/revisions`使用`base_version`、命名的`input_changes`或`section_edits`。只采用实际变化段；碰到已确认或人工改写的段返回`CONFIRMED_SECTION_AFFECTED`及逐段前后差异，用户明确选择`accept_sections`后才能替换。`confirm_sections`由用户确认，模型不能代签或借采纳参数覆盖人工稿。重放返回原提交的确切版本，不能把历史请求落进后来的用户版本。`/restore`恢复方案内容为新版本，保当前实际行动、费用、退款、服务记录与观察，相关旧批准不自动恢复；普通通用文本保存/恢复不能绕过业务结构。

`POST /api/business-results/{id}/commands`用`Idempotency-Key`接收`base_version`、命名`command`、对象型`payload`及可选精确引用/修订说明；事务内核当前成员、委托、版本与来源后追加同一Artifact的新版本。通用动作含记录选择、计划行动、内部行动决定/撤回、人工记录执行、费用及更正/退款、结果观察、服务履约事件。行动目标、负责人、交付物、前置项与结果依赖保持独立；人工记录明确不等平台回执或产品实际发出。效果未知必须指向原待核记录查回执，不自动重发。费用分原币种、估计/承诺/实支/退款；退款必须指原实支，向下更正实支也重核累计退款。服务回应、承诺、履约、关闭和重开分记录，履约引用真实已存承诺。

`POST /api/business-results/{id}/actions/{action_id}/execute`的`base_version`绑定保存该行动的业务Artifact当前版；`execution.version`独立绑定行动指定导出的源Artifact精确保存版。保存行动会递增行动Artifact版本，因此源版本可以早于计划版本，消费者应按该精确源版读取并复核，不得把“当前行动版不同于文件目标版”判为目标变化。文件Job完成时只代表精确导出字节已生成并有可下载回执，不代表已发送客户。`artifact_export`记录状态`executed`/回执`file_generated`；浏览器已确认输入则是`awaiting_outcome_verification`/`browser_input_delivered`，对方发布、联系或付款仍需独立结果核对；`execution_unknown`不允许盲目重发。文件受众随行动精确保存为`internal`或`client`，失败不降级到内部视图。

`collaboration_plan`还使用同一路由追加完整协作生命周期，动作名为`adopt_collaboration_candidate`、`decline_collaboration_candidate`、`record_collaboration_terms`、`confirm_collaboration_agreement`、`submit_collaboration_deliverable`、`record_collaboration_feedback`、`request_collaboration_revision`、`accept_collaboration_deliverable`、`reject_collaboration_deliverable`、`cancel_collaboration`及`record_collaboration_cost`。`payload`身份引用解析自当前保存的协作候选；候选项目须为当前Work。协议来源与交付来源只能选择同一Work可访问的精确Material SHA-256或Artifact版本。协议确认需精确的当前`creator_collaboration_agreement`批准、完整报价/权益/交付/档期/取消条件和已明确为本协议金额的报价状态；quote或estimate不会由审批自动转成承诺。每次交付提交冻结其`agreement_id`和精确来源；协议变化后旧交付不得按新协议接收，必须在当前协议下提交新版本。已审批接收决定不可改写为返修或拒收；同协议已接收的交付不得重复提交。逐项反馈可关联锚定该提交Artifact版本的原评论，或明确记录为用户输入；返修请求必须指向该提交的反馈。拒收、取消、实支与退款都是保留依据的用户记录，不等外部通知、客户回执、付款或退款成功；只有当前已连接且已授权的原执行渠道才可能产生外部回执。

客户私域由`/api/business/commissions`及其成员、工作绑定维护。委托版本与当前成员角色决定读写；绑定在加入私有内容前完成，不把已含资料的工作静默移入另一客户。主体客观身份仍沿原Business目录；引用客户来源须核目标委托。`/api/artifacts/{id}/comments`锚定确切版本的Unicode原文范围，解决意见须指向实际改版或保不采用理由；`/approvals`绑定精确版本、用途、范围及批准人，真实效果前重核当前版本与批准人资格。

发布API使用同Store的PublicationDraft/Release及审核记录。创建/改版保存用户明确选定的公开内容，私有输入快照不随包复制；提交、审核、明确发布、撤回互不替代。审核角色和官方身份只能由服务端角色授予，审核者只能取已提交选定内容。`/api/discovery`仅列当前公开且许可有效的Release，`/api/releases/{id}`按private/team/unlisted/public及许可重新鉴权。没有配置正式托管时返回`local_review_only`，不能称全网已发布。文稿模板`instantiate`创建新私有Artifact和对象映射，保原许可、取得时点、替换参数与缺依赖；不覆盖原作者或已有改稿。公开经验始终区分作者记录、观察、推断和未知，发布、收藏与复用均不授训练资格。
