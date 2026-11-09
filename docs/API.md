# 寰意 / OEVIRA — 专业对象与接口合同

本文件规定全部领域之间及客户端与核心之间的输入、操作和结果合同。专业行为由[完整规格](PRODUCT-SPEC.md)和[领域合同](domains/README.md)定义，存储与运行边界由[架构](ARCHITECTURE.md)定义。下列专业操作是需要实现的协议要求，不是声称已有的HTTP路由；文末单列现有本地基础接口，不能让其文本模型或首批格式成为全产品能力上限。实现时在同一核心中补对应领域函数、实际路由、客户端类型与调用者，不另建通用RPC平台或第二套对象存储。

## 共同输入与对象引用

以下是协议数据，不要求用户填写内部ID。用户通过已有工作、选择对象、文件和业务条件产生输入；服务端解析身份、来源与允许动作。AI、直接编辑、批量操作和Worker消费同一领域合同。

| 类型 | 必须携带的内容 | 不得混淆的语义 |
|---|---|---|
| 请求上下文 | 服务端核定的initiator用户、真实executor、工作空间、适用客户、动作、用途；适用delegation引用/版本/范围/到期、Skill/tool版本和permission decision，异步执行绑定当前授权与撤销世代 | 客户端可选目标，不能自行签发权限；发起用户、执行者、客户、主体、平台账号分别核定，工具不因服务身份扩大用户委托 |
| 对象引用 | `kind,id,revision`；专业对象需要的父对象/原件版和原生定位 | 页码、名称、相似句、屏幕坐标不是独立稳定身份；移动后依真实对象映射定位，歧义需解决 |
| 来源引用 | 原件/观察ID及版本、页/段/表/单元格/对象/帧或时间区间、原话或数值、用途约束 | 原件版本与解析版本分别记录；文本偏移使用Unicode码点，媒体时间使用有理时基，不能用截图代来源 |
| 输入快照 | 本次工作要求和业务约束、确切输入版、关系版、指标定义版、比较集合、期间与可获得时点 | 事实有效时间、来源发布/修订时间、系统取得时间分开；回看当时判断不能偷用后来获得数据 |
| 文件引用 | 文件ID、原名、媒体类型、大小、完整性值、可读范围与获取方式 | 不暴露服务端裸路径；哈希证明身份，不能证明编辑/播放/专业质量 |
| 修改请求 | `base_version`、明确选区、由领域定义的操作及参数、保留/锁定范围、请求幂等键 | 不接受任意JSON补丁绕过业务规则；同版未选内容保全，无法唯一定位不能扩大为整份重生成 |
| 专业结果 | 成果/工程及版本、具体对象、输入快照、方法或模型配方、来源、覆盖、限制、可继续动作 | 正文可以是文稿、演示、工作簿、媒体工程或分析对象；通用`body`只能是摘要/文本正文，不能覆盖专业结构 |

输入缺失、未知、未经核实和实际零分别编码；数值保单位、币种、含税/退款口径、分母及期间。金额采用有明确精度的十进制或最小货币单位，算法输入输出需显式转换和验证；时间保UTC时刻及业务时区，不能按机器所在地偷偷改活动日期。集合先按权限/客户/期间筛选，再稳定排序分页；分页游标绑定查询条件与快照，禁止截断后宣称完整覆盖。

文件导入先保原件并检查实际格式、资源与可读/可改范围；大文件可分块续传，分块位置、校验及最终组装版可读回。导入限制要给具体原因和适用继续方式，不把“请选择更小文件”作为专业大表/媒体的完整方案。表格/文档/媒体的可用预览与完整解析分别呈现，后续解析不能覆盖用户已经校正的内容。

解析结果带原件版、parser/config版、attempt、解析版及按页/表/对象的成功/失败/未处理范围。失败回退不混前次暂存结果；替代解析依赖缺失准确返回不可用，不能将同一缺失底层库包装成可用回退。部分可用、完整解析与专业可编辑资格分别表达，重试保持同一原件和既有人工校正。

### 类型、坐标与实际请求语义

以下类型扩充已有同一API合同，不另建RPC总线或第二协议权威。字段按真实已版本化消费者兼容演进；没有实现的动作仍是目标合同，不能注册空路由冒可用。

- `ObjectRef(kind,id,revision)`指确切业务对象，`revision`与目标Owner的版本类型一致；原件文件身份另用FileRef，不能把展示标题/页码当引用。旧数值版本与新复合版本通过显式适配，不在客户端猜测转换。
- `Anchor`是带类型的联合：文本含源/解析版、坐标单位、半开start/end、原句与邻近校验；Office含文件版、部件与稳定对象/子对象及所选属性；表格含工作表身份、单元格/区域和类型；媒体含源/工程引用、片段实例/轨/流、坐标域source/timeline、整数分子分母的时基与区间。文本规范主合同用Unicode码点，旧UTF-8字节接口须声明单位并在同原文确定转换，不能混用UTF-16。坐标合法仍需语义定位核验。
- `Quantity`保十进制字符串或声明数值类型、单位/币种、量纲、分母/期间、精度及有效值/未知/缺失/不适用原因；不存在的原因字段不能补成0。小数运算与JSON传输分别约定，时间和原生ID不经浮点舍入。
- `OperationIntent`保本次独立用户动作ID、目标Work与专业对象版、save/execute/revise语义、选区和输出要求。用户能明确另做一次相同参数工作，不能仅靠参数hash合并；断线重试同意图用原Idempotency-Key。当前身份、权限、lease与source-use只能由服务端核发，模型或客户端不能自行声明。
- `Coverage`保范围定义/截止、实际处理单元与单位、完整/部分/未请求/未检查/不可用、纳排与失败原因；资料、页面、事件和独立人群的计数不互换。无界或未知总体不填虚构total/召回率，传输分页的next_cursor不冒内容全部覆盖。

观察更新带`update_mode=partial_patch|observation_snapshot`、确切父版、此次requested/covered字段集合、逐字段值或MISSING/UNKNOWN/NOT_OBSERVED及其真实观察时点。partial_patch省略键不改原值/时点；snapshot范围内明确未知覆盖当前投影，范围外未观察不伪刷新。显式0、明确移除、未知与省略不是同一操作；全未知观察也留历史及来源。语义内容指纹不含DNS/重试/传输时刻，重复取得沿原Job保存本次receipt和旧内容版引用，不能用复用Evidence掩盖本次失败或覆盖不足。

### 统一主体识别、真实对象与正式数据边界

`SubjectQuery`由名称/别名、可选MaterialRef/原生链接、当前Work及任务用途构成；用户不提交brand/company枚举。既有Business身份用例先复用当前有据对象，再给候选与最少必要消歧。`SubjectRef`为选择投影，含目标`ObjectRef`（尚未解决可空）、显示名、`resolution_state`（unresolved/candidate/verified/conflict）、来源/身份版本及候选原因；它不创建第二主体数据库，也不以界面选择代证据。识别失败保当前Work、材料和可继续任务，不能强迫先建完整目录。

真实对象的`kind`保brand/legal_organization/place/merchant/store/account等对应领域语义；运营者、所有者、承租、加盟、代理和入驻由有类型、有方向的关系记录，含两端ObjectRef、有效时间（或明确未知）、记录/观察时间、来源、确认状态与relation_revision。角色不覆盖对象身份，多对多关系不压成父子树。合并/拆分须返回保留的规范身份、旧引用映射、受影响关系/消费者及撤销方式，禁止静默改写历史修订。旧`type=brand|company`仅用于§历史本地基础接口的兼容记录；适配时信息不足返回待核，不能猜company全是公司或删除非这两种真实对象。

`directory_eligibility`与识别状态、访问权分开：正式目录需有中国境内营业依据，含国际品牌境内经营；境外任务资料/比较及合法历史继续保全。没有依据可保待核或当前Work候选，不自动注册全球目录、不改变用户地域资格。新正式空间的具体主体集合为空；测试/演示环境返回服务端绑定的DataContext.data_domain，客户端不可将其对象ID用于正式操作，服务端亦拒绝跨根引用。通用方法/空模板不携带演示业务观察、指标、私有账号或默认监测。

以上通过现Business/Work身份和关系用例实现，后文历史subject路由仍保其原观测范围；适配消费者、序列化、持久数据和回读须共同更新，不能只改前端名称。

### 专业目的地与连续导航

目的地由应用内受控注册表按SPEC§5标识，客户端取得当前用户可读的功能/对象投影；外部网页、模型输出和Skill文案不能注册任意路由或执行代码。既有路由状态携带destination_id、workspace_ref及适用的client_scope_ref、work_ref、object_ref、revision_mode、return_anchor、open_disposition；缺省客户或工作表示未绑定，不自动生成。open_disposition区分当前查看、新工作标签及并读；已打开同对象时默认召回原控制器，明确比较才建另一视图实例。

导航、搜索选中、深链和浏览返回只做定位与当前权限读取；只读查询沿既有范围、权限与读取协议，不提交业务修改/执行意图或新Job。明确“新建并开始”“应用修改”“取材”“发布”等命令才调用相应领域执行用例。标签与导航偏好归原用户/Workspace的SceneState或既有偏好存储，内容、授权、任务与工具绑定沿原Owner。切目的地不改ToolBinding目标、草稿代次或当前版；显式切换操作目标更新对应绑定。

响应区分已定位、尚待加载、未登录、失权/不可见、对象撤回及旧链接无法唯一解析。登录后恢复经核目的地和返回锚，不重执行历史动作；隐藏导航项不构成权限拒绝。旧资料/成果链接映射到同一资源视图及原对象/版本，旧工作与监测链接沿现工作面解析，不复制对象或恢复旧外壳。显示分组和服务端专业对象kind分别版本化，不能将目的地数量当接口能力集合。

### 物理空间视图与输入协议

以下为同一API内需实现的目标协议，不是已有运行路由声明。`SceneState`包含scene_id、用户/Workspace、可选work_ref、personal/shared、scene_revision、coordinate_system_version、camera及instances；`SpatialInstance`包含稳定instance_id、ObjectRef、pose(position/quaternion/positive scale)、local bounds/thickness、active_face、parent_group、pinned/stowed及允许保存的局部视图锚。注册的face_kind决定功能面，客户端或模型不能提交任意脚本、文件路径或权限。一次创建/共享场景不自动授权里面的对象。

| 目标路径 | 输入与准确结果 | 错误与保全 |
|---|---|---|
| `POST /api/scenes`；`GET /api/scenes/{id}` | 创建或读回本用户/已获准共享场景，引用当前可访问对象；新场景用Idempotency-Key，可不绑定Work用于工作区入口 | 不可见引用不返回标题/缩略图/私有面；空场景不伪造工作 |
| `POST /api/scenes/{id}/operations` | `base_scene_revision`、operation_id和明确的move/rotate/flip/resize/group/ungroup/pin/stow/dock/undock/resize_region/restore_layout；每个受影响instance有基线、目标姿态或组关系 | 有限数值、非零单位四元数/正尺度、有效面、无环组、当前写权；冲突返回调用者可见的双版差异，不最后写覆盖 |
| `GET /api/scenes/{id}/operations/{operation_id}` | 同用户/当前权限下读原提交结果与applied_scene_revision，响应丢失先查而非再建意图 | 读取回执不重新移动、不重发领域动作；同键异输入冲突 |
| `POST /api/scenes/{id}/manipulations`及同identity续租/结束 | 仅显式共享场景用于协调某实例的操纵；绑定scene/instance/current revision与世代、有限租期 | 失租/断线不能再提交旧抓取；租约不授内容编辑、来源读取或平台账号权 |

服务端保存的姿态单位与相机模型均须版本化；对轻微可规范化数值返回实际applied_pose，不用未声明裁切改变用户摆放。非法变换、越范围引用、循环组、陈旧基线分别给准确错误；接近视野边界不删对象，可通过索引召回。控制帧/拖动中间态是客户端预览，最终空间提交和内容保存分别返回回执，不用HTTP受理表示已落盘。

`SpatialInput`在客户端绑定pointer、scene/frame、instance、visible_face、局部坐标和gesture_kind。向浏览器转发时还含page_id/page_revision、实际CSS viewport、输入序号和控制世代；向媒体/文档转发时转换为现有专业Anchor，不发送世界坐标冒时间码/文本offset。被遮挡、背向视线的面及过期目标不接输入；物件命名为“背面”的功能面翻向用户后按当前active_face正常交互，名称不决定命中资格。空间移动手势不得复用click请求。重复外部效果未知仍先对账。

将资料放入研究、素材放入轨道、作品放入交付容器等是**领域动作**：先在客户端显示实际目标/用途/版本与影响，再调用已有相应领域API，返回内容变化与领域回执。仅移动/分组不能调用这些业务写入；领域失败保原对象与可恢复空间位置，部分空间/内容成功分别报告，不声称跨客户端几何与数据库天然原子。无权跨客户落点拒绝但不删除合法源稿。

`SceneState`的可选`regions`保存导航/内容/工具的停靠槽、成员instance、顺序、宽度偏好及可见性，`SpatialInstance.presentation`持久保存docked/free及可恢复free_pose；focused是InspectionState覆盖后的临时有效呈现，不作为独立focus命令写scene版；`revision_mode`区分follow_current/pinned，后者保存确切pinned_revision，用于历史并读/审阅，不复制另一作品。三者共享同一instance_id、ObjectRef、ToolBinding和内容控制器；dock/undock只改变scene版。设备临时折叠与专注沿InspectionState，不改内容版、不覆盖别的屏宽偏好。分隔线拖动、键盘或菜单设宽归同一个resize_region操作，宽屏停靠最小宽度、余量分配及失焦恢复按DESIGN；窄屏正文/对话/表单按可用宽重排，不以560宽屏初值拒绝320宽或文字缩放；场景结构升级保旧实例和未知兼容信息，不能以新字段为由清空既有布局。

### 合成片、焦点揭示与运行资源的边界

`SpatialInput`增补本次实际呈现的geometry_revision、face_content_revision及可见片映射；render片仅为同一face的局部裁剪和世界变换，不能成为新的ObjectRef、语义节点或业务动作ID。runtime texture/picture/EngineLayer句柄只在对应视图与资源世代内使用，不持久化进SceneState、来源或业务API；缓存命中仍重核对象/用途。分片后的同一原始事件只向原功能面投递一次，场景/内容新版使旧输入失效而非猜坐标继续。

焦点揭示沿现InspectionState保存临时调整集合及原focus_anchor/view_edit_generation，返回仍为原语义节点和内容选区；系统默认布局的焦点可见性由客户端主动保证。用户明确保留揭示后的摆放才提交Scene operation；取消/退出不倒转期间内容修改、用户改宽或新的协作位置，失权引用不恢复。该类型只解决呈现，不增加通用窗口控制或替浏览器发起动作。

### 临时查看与继续工作的明确类型

`InspectionState`是客户端临时视图，字段为`inspection_id/parent_inspection_id/scene_ref/base_scene_revision/selected_instance_ids/original_view/transient_view/focus_anchor`。original_view逐成员保存pose/parent_group/active_face/presentation/dock_region/slot/order/free_pose，逐受影响region保存宽度与可见性，同时保存camera及焦点；每项带本次进入时的view_edit_generation。嵌套检查沿父检查逐层退出，只恢复本层仍受临时状态控制且未被人工/协作新改的字段，不覆盖期间有效宽度/位置/焦点变更。已删除或失权实例跳过并给准确状态；内容版、未Apply草稿和媒体状态不随临时视图回滚。结束检查恢复未改字段，不调用内容restore；明确保存排列时转成原Scene operation，不能把临时位置悄悄写成共享布局。目录与搜索返回合法成员ID、名称和真实分页状态，不要求全部几何实例先渲染后才能查到。

`ResumeContext`由现Work/Job/Artifact读取组合，含`last_acknowledged_refs/current_refs/committed_results/pending_intents/required_inputs/return_anchor/coverage`；只有当前调用者可读内容进入结果。`unchanged`需要同范围完整读回，未取得历史事件返回范围未知。继续动作绑定原意图及当前版，已经提交的结果只读回；需重新开始的独立动作生成新意图，旧外部效果待核不重发。本类型不创建新任务状态机或把客户端离开时刻当服务端处理停止时刻。

### 用户意图、工具绑定和可继续结果

以下扩充现有领域请求/响应，不新增通用RPC平台或第二任务系统。具体路由沿原Work、领域操作和Job，空间动作另沿Scene协议；这里定义要由所有实际消费者一致表达的语义，不是声明接口已运行。

| 合同 | 输入/返回的确定含义 | 不能代替什么 |
|---|---|---|
| `WorkContext` | 当前Work/Workspace、适用委托、object_ref/anchor、requirements_revision、已确认输入及原出处、未确认候选、选用材料/输出要求；服务端解析实际权限 | 传了客户ID不授权限；继承上下文不代表继承另一客户数据或平台身份 |
| `ToolBinding` | 工具实例明确绑定target_ref、anchor、content_revision、当前操作及未应用参数；用户切换目标有显式动作，父版变化重新核；视图跟随当前稿或钉住历史版的模式与SpatialInstance一致，不能因更新另一视图而改历史Anchor | 移动物件、改变焦点或最近对象不触发重新绑定，不把摆放版本当内容版 |
| `ActionAvailability` | 由相应Owner根据当前合法状态返回动作、所需输入、可逆性、可能效果及具体不可用原因；仅核该任务需要的条件 | 能力列表是当前可操作提示，不是授权令牌；模型不能自填allowed=true，调用/提交仍重核 |
| `RequiredInput` | 真正需要人的字段/决定、影响的动作/结果、缺失或歧义原因、已有候选与最小解决方式；先复用已确认上下文并尝试获准的系统取得，已确认字段不重复请求 | 不把内部技术参数、后台检查、可自动取得的数据或系统故障伪装成用户必填；不编未知，不要求用户填内部ID/算法JSON |
| `ChangeSet` | 冻结输入/目标基线、已确定与疑似影响对象、逐处差异、保留项、锁定冲突、预览资格和请求身份；应用时复核范围、父版和当前权利 | 预览不写最终作品，不证明之后仍有权；新来源/参数/范围变化使旧预览不能直接采用 |
| `ResultEnvelope` | 确切对象/版本、已提交部分、未完成单元/原因、实际Coverage、可恢复请求/Job、后续可用动作；文件名/MIME/范围来自真实结果 | accepted/processing/partial与completed不互换；下载、对外发送、对方收到和业务Outcome另有回执 |
| `ReturnAnchor` | work/scene/instance/face、内容版、局部页/选区/播放头及仍合法的视点，重新打开先核权限与对象存在 | 锚不是访问权，删除/撤权时不能用旧缩略图或布局正文复活私料 |

一个明确直接操作在允许范围内可以直接提交，不人为强制每步preview/approve；大范围重算、涉及锁定或目标歧义时必须返回相应差异/缺口，外部效果沿既有精确授权。请求身份按用户意图生成而非按参数永久去重；超时查询同一已提交结果，用户明确“另做一次”允许同参数新意图。

修改共同事实/预算/素材后，返回实际受影响成员与当前计算/制作版，客户端只刷新这些对象，保未发送草稿代次与其他任务。不可唯一映射的客户意见返回待定位原意见，不以相似文本自动应用；读到旧回执也不能把它写进当前新版。

RequiredInput沿现有请求补充`resolution_owner`（user|authorized_reviewer|identity_provider）、`reason_code`、`affected_refs`、`blocking_scope`和`context_revision`；原因只表达真实歧义、业务取舍、不可替代用户材料、本人认证、锁定冲突或外部效果许可。服务端保存已复用来源/检查依据，不向前端泄露其他客户或秘密。可由系统解决的内部前置仍在原Job/领域处理器推进，不新建第四类用户表单；`waiting_for_auth`与`waiting_for_input`按真实缺口使用，客户端不能仅因JSON字段为空切入等待用户。问题回复绑定原意图和上下文版，过期或已解决回复不重复建任务；后台补齐后无需用户再点一次相同“继续”，但不解除明确暂停/取消。无外发变化、无锁定冲突且原指令已清楚的局改按原OperationIntent直接形成新版本，ChangeSet同时保留实际差异供查看，不要求每次先preview再approve。

### 两种入口执行同一动作的往返约定

对话传输视图`ConversationAction`携带原话、明确Work/任务、context_revision、所选ObjectRef/Anchor和同一OperationIntent，不另造业务状态；其中operation_id就是该独立动作的请求身份。`ClarificationResponse`引用原意图、RequiredInput字段及用户补充/当前对象版；补问已解决或输入过期时返回准确情况，不重新建立项目。`DataContext`由服务端可信工作区和运行配置绑定production/test/demo域及当前SourceUse，客户端、模型和导入文件不能签发或提升域。所有异步、文件、检索、备份/恢复及学习消费者复核相同域和来源血缘；改文件名/ID/标签不把样本变真实事实。


自然语言和直接操作使用同一`OperationIntent`协议和领域处理器；每个独立用户动作有自己的operation_id，同一动作的查询/内部重试复用原ID及输入摘要。请求补`origin`（conversation/direct/batch）、已被用户明确选择的对象/范围、所用要求版及草稿代次供追溯；origin不改变权限、质量门槛或用户意图。对话提议未被采用时只是候选，明确获准且无歧义的可逆操作可直接提交，不能强制每条命令经过聊天批准。工具返回真实`ResultEnvelope`后，对话与专业面均引用该结果和新版本；不得从模型回复补造“保存成功”。

任务补问使用`RequiredInput`明确影响对象、原因、已有值/候选及最小解决方式；已确认上下文继续复用。用户改输入生成新要求版，旧运行可保其真实结果但不得写入新稿；晚到回执不覆盖当前草稿或工具绑定。暂停/取消/重开沿现Job及ResumeContext，查询丢失回执不再产生业务效果。用户后续精调、改变要求或明确“另做一版”均建立新的独立意图；只有同一已提交动作的查询和内部重试沿原意图及受控attempt，不能按参数永久去重。

自然语言修改从v1提交得到v2，直接精调须基于读回的v2提交得到v3，再从对话核差异、撤销与继续导出，须读到同一版本链；并发仍持v1的动作返回冲突或待审差异，不静默覆盖v3；反向操作也相同。Undo分别选择内容操作、空间操作或明确的任务取消，历史恢复新建修订，外部已有效果只做实际支持的补救，不把删除对话当撤销业务。

跨任务引用沿已有`WorkContext`的材料与对象引用携带来源工作、来源对象/版、当前工作及拟用范围。服务端分别核来源读取和当前用途，查无权时不回传该源正文；撤销授权仅使受影响的当前引用待复核，独立工作不共用草稿代次。复盘用已有Analysis输入绑定实际行动/发布回执、观察版和原方案版；准备稿、内部发表、外部分享、实际结果分别有可回读状态。

### 等待表达属于本地呈现，不改变任务状态

既有本地偏好增加独立waiting_motion、work_humor及concise/light表达选项，使用同一偏好读写与失败回执；系统减少动态/低功耗对装饰有效，关闭work_humor不关闭必要状态。映射仅消费真实ResultEnvelope/Job阶段和当前语言，不额外读取客户正文或私人会话；十六语义动效仍由具体对象状态触发。表达资源、显示完成和用户已读均不得回写为Job完成/批准/发布。该本地偏好不要求云账号、不进入客户交付，也不自动变成模型训练偏好。

### 实时语音沿同一工作与命令

实时语音沿同一`WorkContext/ConversationAction/OperationIntent`，`VoiceSession`仅为现有Media/Gateway的会话引用，含session/turn、输入/输出方向、单调seq、真实采样格式/声道/源时间、授权世代、固定Work/context_revision与响应版本。采音、识别、生成和实际播放各报状态及确认位置；临时转写不提交业务动作，最终转写或用户校正绑定一个明确意图。静音输入、打断回复、结束会话与取消指定Job是不同命令；每种回执分别说明请求收到、底层实际生效和失败。打断后的旧响应音频块按世代/turn拒绝，重连查询原意图/块回执，不重复外部效果；不建立独立语音权限库或任务状态机。具体发言端点、回声/设备、保存与恢复规则见I4.1的实时语音合同。

### 智能物件呈现的类型合同

`PresentationProposal`沿同一智能任务结果携带：`proposal_id/task_id/run_id/attempt/input_revision`、本次作用的`object_refs/anchors`、已确认内容版、`blocks`、表达理由、缺口和适用条件。每个block有稳定ID、修订号/顺序、注册`face_kind`、类型化数据引用/单位/覆盖、来源定位和允许动作引用；需要物理组合时仅给注册形态/面、允许材质参数、建议姿态/组合及保留/固定范围，不提供脚本或自定义权限。

现有客户端Experience解析器按当前支持schema、合法数值/枚举/尺寸/层级、对象版本、资料读取和领域动作白名单校验；来源内容无法注册组件，HTML/JS/shader源码和未定义字段不能绕过解析。`ActionAvailability`仍只是展示资格，执行和提交前由领域Owner再次核当前授权；呈现按钮不授权动作。缺真实消费者的既有能力保明确未实现义务，不以空按钮冒通过。

增量块绑定同一run/输入版和递增revision；同块重复幂等、旧版拒绝、缺块保持partial，终态需要真实任务结束及结果核对。用户编辑后保新的人工代次，拒绝提案不回退seen_revision；重连读取已确认位置。呈现已采用、专业内容已保存、空间姿态已保存、动作已执行各有独立回执，重开旧提案不会重新执行动作。模型缺失仍可使用相同注册面和确定性对象操作，但不宣称产生了真实智能表现选择。

### 官网公开投影与进入产品的合同

官网内容沿既有公开发布/运行信息责任提供`PublicPage`（稳定content_id、family、slug、locale、revision、title、结构化正文、适用产品版、公开依据/资产和翻译对应），只含已批准公开字段。它不是用户Artifact副本；Publication详情引用准确release并每次核当前受众/撤回与媒体资格。`PublicCapability`的available/planned/unavailable、适用端、核验版与时点取实际发布记录，不从代码分支、截图或文案推断。

`ReleaseDownload`含product/platform/architecture/version、真实制品ID、文件名/MIME/大小、独立签名或摘要、最低环境、兼容/升级说明、发布/撤回状态和核验时点；下载URL只来自已核制品记录及批准域。没有已发布制品返回明确不可用，不生成示例URL；公开读取不暴露构建机路径或内部日志。`ProductEntry`只接注册目标kind/id及必要语言/返回锚，目标URL由配置产生；任意URL、token或客户端自报scope均不能成为回跳授权。

联系沿现支持/联系责任接`ContactIntent`：本次请求ID、locale、问题、用户主动给出的回复渠道和用途确认；返回持久受理ID/时点或准确拒绝/未配置/失败。受理不等客服已答复或已建付费承诺，网络不明先查原请求、不可查询时保待核而不重发。未配置通道不接受正文假成功，普通官网浏览不创建用户业务对象。帮助搜索只返回公开同语种content_id/revision/命中定位，拒绝索引客户素材或从URL采集私人问题。具体实现仍使用现有Owner，以上不是宣称本轮已有运行路由。

## 九个业务领域的操作合同

编号对应[业务合同](domains/business.md)的同号章节，各操作保其专业对象，不压成一次“生成报告”。

| 领域 | 输入与必要版本 | 操作和输出 | 直接消费者及关键约束 |
|---|---|---|---|
| 1 客户、主体与场内经营 | 客户委托；名称/材料形成的统一主体候选及独立目录资格；品牌/法律组织/场所/商户/门店/账号身份；期间关系 | 添加/补证/纠错/去重候选、确认合并/拆分与撤销、移除/重加；返回身份和有向关系版本及受影响引用 | 主体档案、场内品牌、跨城网点、地图/列表、监测与比较；名字相同不自动合并，快照不冒持续营业 |
| 2 经营、市场与消费者 | 市场地域/用途/价格/渠道/时期及替代品、可触达/可服务条件；研究问题、样本框/同意和原话、商圈到达方式/时段/供需观察；指标定义、原生观察、比较集及版本、面积/营业日/业态、可获得时点 | 市场边界/机会假设、消费者主题与反例、商圈需求诊断及验证方案；描述、可比性检查、分群/趋势/异常/预测；结果含证据范围、未证实项、口径、有效样本、区间及不能比较的原因 | 经营看板、品牌策略、比较、研究、客户简报；平台互动、调查反馈、交易数据不能互换分母，研究计划不冒已经完成调查 |
| 3 财务、预算与资源 | 报价/估计/预算/承诺/实支/退款/收益分别输入；币种、期间、税、硬上限/目标、预留金，已有资源身份/容量/时段/用途和费用依据 | 对账、数量×单价明细、资源占用与现金/估计价值分列、情景与敏感性、可行解/不可行解释；返回成本/资源/方案同版关系与受影响制作和履约项 | 方案选择、工作簿、分镜/节目、排期和客户交付；预留金不双花、同资源不重复占用；filed/capture/取得与授权截止不默认同口径 |
| 4 品牌策略与提案 | 品牌事实、用户洞察、受众、客户要求、禁用主张、预算资源版、已确认内容及案例机制/前提/反例/权利；竞品广告与对应落地页的来源版及实际覆盖 | 形成实质策略选项、理由/反证、原创路线及执行内容，保本案机制采用与不可迁移条件；源版创意六类拆解及有据差异；范围变更返回影响、差异和新批准范围 | 创作、客户PPT/文稿、预算、活动；客户新意见只改变实际关联部分，可比方案不固定三档，公开广告不证明效果，案例自报效果不担保本案结果 |
| 5 创作者与KOS合作 | 创作者/账号身份与归属证据、采集批次质量及判断资格、品牌适配、分平台期间指标、报价/权益/交付约定及授权 | 候选比较、合作方案、履约/素材检查、分阶段漏斗与结算依据；受质量缺口影响的决定范围及补证方式；外部请求单独返回状态和回执 | 合作计划、内容排期、授权素材、复盘；未核/异常批次不冒正式分层或效果排名，单次手工快照不冒日/周/月原生指标，未知外部结果先查回执 |
| 6 活动、系列与排期 | 活动/系列/Post及变体、资源容量、工作日历/时区、依赖、承诺及锁定项 | 创建/调整排期、冲突解释、局部重排、任务交接及执行记录；保未调整承诺 | 日历、媒体生产、预算、合作与发布；不能把旧单容量固定时长求解限制变成产品范围 |
| 7 实验与归因 | 假设、处理/对照、指标及分母、分配方式、观察期、缺失/退出、样本和渠道 | 建立/修订实验、质量诊断、估计与区间、适用因果或描述结论、复盘 | 内容选择、渠道/预算和学习评价；事后相关不宣称增量，分析更改与原计划可区分 |
| 8 事件、服务与风险 | 原帖/回复/更正、主体/活动关系、Target/Claim、证据、内部事实和处置权限 | 聚合与拆并事件、观点/诉求/传播/严重性分别分析、回应候选、行动/反馈；输出期间简报/事件专报，保事件演化链和ToC/ToB分层影响依据；B8的参与者角色、内容类型与媒介、经营身份及带证据的事件关系分别返回，营销/传播/舆情与服务视图引用同一事件版 | 舆情/话题/活动监测、客户工作、服务交接；报告完成、已读、已回复、已解决不是同一状态，无出处案例不进事实或训练金标 |
| 9 决定、行动与结果 | 决策问题、备选、证据/反证、约束、风险、批准范围和观察计划 | 保存选择及理由、形成具体行动、核真实回执、观察结果和修订经验 | 全部业务/创作的后续工作与可纠正记忆；行动申请、获批、执行、效果未知分别成立 |

费用冲正/退款必须引用原费用或实际支出、业务动作、币种、种类与理由；退款累计不能超更正后的可退实付，重复通知按独立退款身份处理。跨平台订单保各自命名域及同源映射，触点按事件时间进入约定窗口，总分配守恒于本次可归因量，退款前后历史均可重开。公开互动份额不转为订单归因或佣金结算。

主体移除不等历史结果撤权；仍有合法用途的原Case、原分析及成果可继续读取，停止新的主体使用与监测。取消不应先要求已经撤回的原来源仍可读，应以当前任务控制权限停止后续动作；继续或重新计算再核所需来源。商户/门店/账号独立业务可从自身有据身份开始，不能强制先补商场/品牌父关系才允许操作。历史恢复次数和旧单状态Case不是专业服务生命周期或恢复能力的上限。

日报/事件专报共享已有事件、工作及Artifact对象。其输入快照保目标/人群、查询和来源版、截止时点、覆盖/缺测、内容计量单位、纳排和基线版；分析返回逐主张证据/反证、事实或推断类别、指标分子分母与口径、比较资格、不确定性和受影响人群。事件时间线分发生、可观察、发现、核实、批准和实际行动。回应版本与具体发布授权、承诺/负责人/履约期限及真实回执关联；服务结果、复发/重开和调查反馈分别持久化。修订按原版影响集修改报告、提醒与批准，不替换所有正文或改写历史分母；没有可用调查或经营数据时返回确切缺口，不伪造满意度、NPS或经营损失。

### 业务方法与结果的最小完整语义

`business-results`与同类型`business-jobs`消费同一operation合同，前者只适用于可有界完成的处理，长计算返回持久Job；两者不分别实现业务算法。

| 方法/对象 | 输入中必须明确 | 实际返回与下游 |
|---|---|---|
| 身份合并/拆分/关系改版 | 对象父版、稳定标识及命名域、关系类型/方向/有效期、证据与选定处理范围 | 候选或已采用的新身份/关系版、保留旧ID映射、未定项及准确影响；不授新客户权限 |
| 市场/消费者/商圈研究 | 边界与替代方案、总量/触达/服务条件及其来源；抽样/提问/同意与真实记录版；地点/到达方式/时段/供给观察；方法及纳排版 | 品类/机会图、逐主题原话与反例、商圈需求/障碍和可编辑验证计划；数字保单位/分母/覆盖，未知不补TAM；使用同一Material/Analysis/Artifact与MethodInvocation，改边界/编码只更新影响集 |
| 品牌识别与资产一致性 | 合法资产/用途/渠道/变体版、研究人群与招募、无提示测试规则/暴露条件、原始回答、编码与纳排口径、既有测试的可比资格 | 资产台账、跨渠道差异、真实无提示识别/误认/未知及对应分母和限制、应用与验证计划；策略、原创及交付引用同版结果，审美判断或AI预测不能冒实测识别 |
| 比较与趋势 | MetricDefinition、原行/事实引用、期间/可获得时点、可比群与纳排、基线/方法配方版 | 每指标比较资格、已成立数值/区间/诊断、样本/分母/缺测、出处；同值对象由正文/图表/文件共同引用 |
| 财报事实/预算 | 申报主体、context维度/期间/币种/倍率、纠正关系；预算行数量/价/税/成本状态与资源版 | 当时/最新视图选择、无重复事实、费用/承诺/实付/退款及情景，估算与未知不变真实支付 |
| 约束排期 | 任务时长/开始域、容量、日历/时区、前置/排他/资格、锁定、预算及目标配方 | 可核排程、逐硬约束检查、停止原因、可行性/最优性/界与精度，未证无解不返回已证无解；不自动订场或发布 |
| 策略/合作 | 已核事实与受众依据、机制/兑现、预算/资源、权益、报价/协议版和客户意见 | 实际不同备选、可编辑方案及取舍；协议、提交、补改、验收、执行回执与费用分别对象化 |
| 竞品广告与落地页比较 | 广告/发布者/媒体及落地页确切Source/Material版本、市场/语言/期间/用途、纳排和覆盖、分类方法版、获准效果原始计数及口径 | hook/format/offer/CTA/awareness_stage/landing_mismatch逐项证据锚、观察/解释/未知、弃答和人工校正；同一Analysis/Artifact进入B4 Brief、B3条件预算、C2及C10，缺落地页不判不符，缺效果不参与效果排名 |
| KOS批次质量与决定资格 | 现有采集批次/主体账号/规则版、八质量指标各自分子/分母/排除/未知、复核标签与样本覆盖、归属证据、决定用途与条件版 | 取得/有效内容/主体精确/重复/作者解析/时间/互动/证据完整性分别返回；逐依赖决定的资格、理由、待核及复核版，B1/B5/B7/B8/B9共用源→内容/账号→批次→归属→复核链；纠错只重算影响集，不另造来源或权限库 |
| 实验/分摊/增量 | 真实分配/资格单位、作品版、观察成熟期、窗口和冻结方法；归因旅程/订单/退款或对照/供体 | 组计数/分母、差值及适用区间、分摊总额守恒或有条件因果诊断；观察性与随机实验类别不可混 |
| 事件/服务 | 原生内容/线程版、事实/指控/反证、归并依据、规则/评价版、承诺与适用授权 | 日报/专报、可改回应、误归属纠正、真实履约/反馈/重开；已回复不写成已解决 |
| 决定/经验 | 备选及证据版、硬软约束、取舍、动作/结果与评价窗 | 采用记录、真实动作或明确人工记录、Outcome和有条件经验候选；一次成功不自动晋升 |

`MethodInvocation`记录方法ID/配方版、适用条件、实际参数、输入引用、随机性/预算及诊断。它附在已有Analysis/Artifact，不另开算法服务。`AnalysisResult`中结构化值与章节、图表、文件引用同源；格式合格、计算完成、独立专业质量和用户采用分别返回，不能以一个ok包揽。方法撤回后的历史结果保当时依据，但新调用不能继续沿旧资格运行。主题派生操作另保任务用途、分词/完整别名/stop_terms及过滤规则版、计量单位/分母/覆盖；一般主题的可选停用词不能套用到品牌提及、SOV或主体比较，原标题/正文/标签与各派生结果分别可读回。

### 业务算法资格、影响集与明确停止结果

以下字段扩展原MethodInvocation/AnalysisResult，不建立新分析服务。身份候选保candidate_pair、原生标识命名域、comparison_levels、missing/conflict、blocking_paths及model/calibration_revision；有分值但未经概率校准时只返回score，不返回伪probability。候选簇的桥接和相斥证据逐项可见，采用合并仍调用原Business父版命令，不由图连通性触发。

预算请求的scenario_set明确各情景版本、成本/收益原单位、固定费用、锁定承诺、选项组、资源占用、硬约束及objective_mode；非概率情景不接受伪造概率。响应区分solver_status、独立feasibility_check、objective/bound/gap、precision、stop_reason和实际缺口；允许返回已核可行但未证最优，不允许把未找到可行解写成已证不可行。金额与资源由B3/B6同版计算，UI、PPT和XLSX直接消费结果引用。

排期ChangePlan的impact_set每条边声明precedence、resource_competition、shared_budget、delivery_commitment或确切既有依赖，包含生成闭包所用时间域/资源版及边界固定项。锁定不因被纳入影响集自动解除；采用时核全部相关父版及资源世代。对实验另保assignment_unit、planned_probabilities、assigned/exposed/recorded/analyzed计数、成熟期、SRM方法/适用性/结果、固定期或预定K时点的序贯停止设计、总α/检查分配及M个主比较的检验族；诊断不通过仍可返回描述统计，但effect_qualification须明确不能支持何种判断。后续补证生成新分析版，不更改原分配和原交付。

### 数学请求、构造证书与正常业务结果

139的现行数值准入在既有parameter_recipe内分清统一envelope证明与逐维全类TV证书；二者都不是用户填写常数的产品入口。逐维参数构造只接收d、ε_q、原方法/程序版、V_d类别与资源预算，不传入目标oracle、客户材料或其值/梯度句柄；实际运行随后才绑定获权oracle_ref。记录offline_target_oracle_calls=0须由该隔离调用边界及真实调用记录支持，不能由客户端自签。

逐维证书返回program_ref及完整固定形状、Gaussian槽数、正的final_sigma、static_query_count、静态资格、共同jet网格/盒覆盖、全pair插值约束版、CAD判定/余项、各项区间/尾部误差、TV_upper及tau_verify。只有全部可行盒均处理且误差预算满足时才返回complete_certificate；admission_result区分selected、complete_not_selected和incomplete。完整证书未达采用门槛可以选正确的拒绝法，但不能据上界较大就宣称候选真实TV超限；若要报告已证超门槛，另须下界TV_upper−tau_verify>9/100。预算/取消导致的partial不是complete_not_selected。selected_route为经证P_d或明确的exact_rejection，回传实际worst_case_query_cap、目标law模型及1/10保证资格。理想连续Gaussian与实际量化目标分别标识，连续TV证书不直接替代有限位验收。参数/证明缓存绑定维度、指数、类别、程序/误差规则版和精度，纯公共证明可复用，不能加入客户数据后跨客户共享。

缺少旧六字段envelope不是整项方法不可执行；逐维构造可以给出完整证书及明确的采用/未采用选择。未完成验证须返回RESOURCE_EXHAUSTED/CANCELLED或NUMERICAL_UNCERTIFIED及检查点，不能无限采用fallback后仍称原一般查询率。原∀ε∃C_ε不等一个已知数值C_ε，除有独立数值证明外不序列化拟合的全维常数；当前维度的实际计数、离线CPU/内存/时间与oracle次数分别记录。完整公式与有限步骤由智能域§139唯一拥有，不另建采样服务、证明平台或任务状态机。

数学工作仍通过现有Analysis的MethodInvocation与持久Job，不新增算法服务或权限系统。MathematicalProblem声明family/algorithm_id/revision、problem_kind、完整输入及来源版、scalar_domain、integer/rational/algebraic/finite_field/float/interval表示、准确形状/图类/概率/变量域、误差/失败预算及计算资源边界。整数与有理分子分母在JSON用十进制字符串；有理分母为正，代数商环另保单首多项式与定长系数向量，有限域保特征及确切表示，不能把浮点容差当精确等式。

构造参数归同一Invocation的parameter_recipe、parameter_witness、identity_checks和compile_status。103每源编译请求绑定M有限描述及a/b承诺和适用依据；既有方法Owner选用预先构建的固定U/A编译库，库引用另保转移表、blocking width、构造版和分离/资源资格，不把A作为用户额外输入。compile_status区分library_unbuilt、library_ready、source_compiled，源承诺不成立另报ASSUMPTION_NOT_ESTABLISHED；内部库未就绪报DEPENDENCY_UNAVAILABLE，不将其误作参数搜索未完成。输出绑定生成D表和由其实际语法计算的K/H/c，数学类相等证明与合成表不能代具体编译产物。有限参考输出按其实际方法版返回；107分开记录两份系数再生、证书全覆盖和独立解析目标。139构造回执保J_prim、N/D_0、标签高度H、C3_bound、w、m及实际表达树/seed/终端调用计数，固定近似阶与外层J_desc分字段；静态δ指数资格不能冒全部误差系数/维度门槛已量化。区分原精确实数一阶oracle的primitive证明、维度分支及每次查询上限，与有限位格点的分箱误差和资源；小维全部拒绝回退的实际标记不冒接受样本。矩阵见证含a/b/c/r、系数域与alpha/beta/gamma及逐系数核验范围，博弈输入保MAX/MIN/CHANCE及有理概率，输出分别含winning_vertices、value_enclosures、精确值及两方策略的实际完成状态。方法详情仅由intelligence§4.2定义；API不复制公式。参数生成、见证恒等成立、主算法运行、原定复杂度/误差保证、独立形式重放与业务收益分别记录，任何一项成功不自动推进其他项。

stop_reason沿原错误信封分清INPUT_INVALID、ASSUMPTION_NOT_ESTABLISHED、DEPENDENCY_UNAVAILABLE、PARAMETER_SEARCH_INCOMPLETE、NUMERICAL_UNCERTIFIED、CANCELLED、RESOURCE_EXHAUSTED和完成；已有合法确定性业务结果仍可交付。暂停/重开固定输入摘要、方法版、已核参数、当前阶段及确定/随机状态；随机恢复不能丢掉失败样本或改停止律。通用公开参数可以按方法/域/精度复用，含客户输入的见证不得跨客户泄露。数学方案被采用与用户实际采购、付款、发布或修改作品仍是不同命令。

数学返回沿同一AnalysisResult声明target_law、count_semantics（显式对象/带标签选择/整数占用向量等）、feasibility_status、estimate/interval、relative_error_budget、failure_probability_budget、total_variation_budget、exactness、runtime_bound_kind=worst_case|expected、completed_trials/failed_trials/unfinished_trials及实际停止策略。exact_uniform与TV近似不同，确定性512^n计数界与(1±ε)FPRAS不同；近似试验0不是输入不可行证明。目标分布和分母不得在结果显示时根据“只保成功/按时输出”改写。暂停恢复绑定已消费随机状态、当前阶段、合法结果与参数，改变预算/停止律生成新的方法执行资格，不沿用旧保证。

114整数多拟阵的oracle请求绑定秩来源/修订、稳定元素顺序、总秩和答案位长；输入改版中止原执行。回传整数向量目标、精确可行性和估计的保证分账；分离器返回违反集合、0/1法向、右边及有理违反量。块体积阶段另保内外球、分离/精度预算、已完成次数及是否满足预定保证。140返回原命题身份、观测/先验/持久状态/停止规则的资格和保证量词。球面有限期限判据另保d/M/T、角误差、目标成功率、块长及块数、比较两侧和排除/未排除/预算未决；未排除不能显示为可行。有限纤维返回rank、中心、投影、半径平方及精确性，只有适用条件独立核实才输出对应统计解释。

### 合规预审与具体修订的结果协议

同一预审可含多个actor/role引用与各自期间关系，每项义务绑定承担者、适用行为、规则版/条款及受影响成果。促销类型、奖品估值、币种和最高奖额分别保事实/规则来源，规则值不作全平台常量；直播的平台、运营者、营销人员、服务机构与实际卖方分别返回义务和缺口，合同关系不自动转移责任。官方原文的替代获取保存同一文本身份、公布/生效版、核验时刻与覆盖，重复转载不增加独立证据数；正文或适用版本仍缺时只阻相关结论，不用摘要签规则已核。记录保留要求绑定具体记录种类/责任主体/起算事件/期限与来源版，不扩成全产品统一永久留存。

合规分析复用AnalysisResult/Claim/SourceUse：被审对象及版、jurisdiction/market、actor/role、intended_action、product_category、audience、渠道、事件与计划发布日期组成适用事实；RuleAssessment含确切规则Source版/锚、rule_kind、published/effective/retired/checked_at、事实条件、例外、版本替代关系、支持与反证。fact_sufficiency、rule_applicability、claim_support与effect_authorization分别返回，不用一个risk_score或ok代替。未核/冲突/不适用与明确不支持分型。

每项finding必须定位实际文案/画面/音轨/价格或活动规则，给有据理由、可采用replacement_candidates、必要补证或适任复核条件。修改沿现有ChangeSet/OperationIntent的base_version与影响集，返回新作品版、未改内容及受影响批准；检查通过不代客户承诺、外发权限或实际发布。不同法域、不同有效日期和不同媒介的结论不得直接复制；来源更正/撤权只重核实际依赖，保合法原稿和历史判断。

### 预算诊断与条件收益的回执

沿原AnalysisResult增加constraint_refs（id/source/base_version/unit/bounds/adjustability）、conflict_set、conflict_qualification=solver_irreducible|solver_infeasible_superset|not_established、diagnostic_trials及budget_exhausted。每个trial记录临时排除集、完整固定条件摘要、solver_status与独立可行性；unknown不得变infeasible；另保solver_version、scaling、feasibility_tolerance、proof_kind=numerical_solver|independent_exact_certificate及确有的certificate_ref，数值status不冒精确证明。诊断不调用采用/付款/删除动作，建议变更仍绑定原预算及资源父版。情景返回response_kind=incremental|total|cost、support_ranges/extrapolation_flags、paired_draw_revision、delta_quantiles和probability_available；未校准情景不返回概率，模型分布不冒实际ROI。

### 行业机制与框架分析的同一成果协议

现有ContentLearningAsset/方案候选补充mechanism_id/version、prerequisites、supported_facts、counterexamples、resource_constraints、rights_constraints、causal_hypotheses、proposed_actions、measurement_plan_ref、stop_conditions及实际采用范围。编号表示本产品方法身份，不等原案例身份或用户已同意执行。实例化返回既有Artifact/Brief/Business方案引用及准确依赖；不生成另一知识/策略/实验数据库。

框架结果仍是AnalysisResult内的观察集合：analysis_unit_ref、source_revision/anchors、speaker_role、issue_salience_fields、problem_definition、attribution、evaluation_basis、proposed_remedy、supported|mixed|unknown状态、method_revision、reviewed_by及实际分母/覆盖。不存在的框架维度为unknown/absent并保原因，不补空字符串冒已核。更正使用同一ChangeSet和父版CAS，返回受影响分析/Case/作品，不更改原Source；用该结果生成回应与实际发布仍使用各自OperationIntent。

### 选品组合与生成性质的精确引用

原Business方案/选品项保存`product_ref/variant_ref`、门店/渠道/市场、原生URL、实际已核属性、价格/库存观察版及`selection_reason`，未知按Quantity/Coverage处理。组合保存`relation_kind=official_look|editorial_styling|recommendation|sale_bundle`、成员引用/数量及来源或编辑决定；销售组合另需真实组合ID/条件，推荐不能填成商售。图片沿FileRef/MaterialRef保已取得范围、variant映射和本次SourceUse，URL不是媒体字节或商用权；更新组合只触发真实依赖的方案/文件/预算，不改变商品原观察。

Artifact与Publication现有来源/派生记录补`generation_provenance`：实际处理类型（人工、确定性转换、AI生成或AI辅助）、输入/输出版本、实际方法/模型调用引用、适用Anchor范围和人工修订关系；没有真实调用回执不可生成“AI已完成”的出处。`disclosure`关联已核生成范围、语言、提示内容版和实际展示/导出位置，不是事实审核状态、公开许可或客户品牌覆盖指令。导出和公开release引用同版说明；原件、只读材料、纯人工区段保原身份，客户端不自报生成/审核通过绕Owner。

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

### 文件、媒体与交付的修改合同

文件生成输入为目标/读者/格式、结构内容或当前Artifact引用、同版数据/图表、品牌/字体及许可约束。修改输入为确切原件/父Artifact、用户原意见、已确定目标Anchor、按领域定义的命令与未选保全范围；模糊意见先返回合法候选位置，不扩大为整文件重建。输出包含原生文件、新Artifact版、真实对象差异、兼容/损失说明和可读预览，不能只回标题和body。

`ChangePlan`保plan_id、对象父版集合、source/rights版本、target anchors、commands、影响关系、人工锁定、所需输出和plan_digest。无歧义且已获权的直接编辑可由一次调用内部生成并执行计划，不新增普通保存审批；需用户处理冲突或接受损失时才展示预览。采用时再次核父版、当前权利和用户所选范围，过期返回准确冲突，不自动把旧命令应用到新对象。

媒体命令除父工程版，还保源/实例/轨、source/timeline坐标域、有理时基、剪切/移动/速度/混音等类型和依赖影响。源预览、工程合成预览、A/B候选和最终输出各自有类型、所据版本及范围；预览可读不代表已采用。录音take是稳定素材身份和已落盘分段集合，停止/文件完成/可播放/已入工程分别回执，设备错误不吞掉成功段。

`RevisionBatch`仅协调本次跨对象变更，保存每成员父版/目标/实际新版本或失败，不复制领域状态。`DeliveryBatch`冻结实际文件/工程/媒体成员与共享依赖、用途和检查状态；只有必需成员完成才可标完整，单成员失败可续做。客户意见始终关联所评版、原锚与处置理由；采取修改时另关联实际差异和新对象，不能只改resolved字段。有据不采取或原稿已满足可按创作§10保原版证据关闭意见，准确标未改，不制造新对象或冒已改稿。对外发送、客户接收和发布沿独立授权/效果回执，不由导出完成推断。

### 共享部件、三方合并与媒体交付的具体字段

原ChangePlan增加可核read_set、write_set、shared_usage、copy_map及preserve_invariants；每项含part/object引用、原父版、预期原值或语义指纹及允许变化的字段。copy_map记录原共享part/对象到新私有副本的身份与关系重绑，仅在确有局改隔离需要时生成，不复制整份客户成果。未变part按字节核，变更part的未选使用者按格式语义核，二者不能相互代替。输出包含实际改动、未选保全检查、签名/兼容性变化、原生文件版及独立重开结果；规划字段不冒本轮已实现协议。

三方合并绑定base/current/incoming三份确切版本及object_id/field；冲突保两边值、删除/修改和顺序关系，不能仅行号匹配。跨文件RevisionBatch引用对应ChangePlan及真实依赖，新版本不覆盖锁定稿。模板实例化复用同一copy_map和SourceUse检查，不新增模板专属权限或成果状态机。

媒体交换响应沿原Artifact输出target_profile/adapter_revision、完整输入依赖、逐对象loss_items（exact/approximate/bake_required/unsupported）、时间/声音/效果比较项及实际验证范围。bake_required须绑定用户选择的范围，不自动变成已渲染。分段Job记录owned_output_range、source_required_ranges和全局时基；重叠计算区不在多个成品片段重复输出。Podcast成员附episode_ref、audio_revision、transcript_revision、chapter_revision及对应媒体hash/时长，格式、媒体类型及时间映射保准确；附件版与音频版允许独立变化但必须声明兼容的成品版本。导出包、内发布和外部订阅状态分别返回。

### 媒体编辑的语境与关系引用

视频的Shot/Beat在现有工程保存叙事节点、情绪节点与母题的可选引用，以及creative§7的镜头、声音、时长和出处字段。原Media `EditDecisionList`按同一候选身份返回有序`EditDecision`。决策引用源片段和流、原时基的半开入出点、确切父版、当前适用许可、拟改操作和受影响的画面/字幕/声音及理由。旧秒值仅作兼容投影。应用前由既有权限与Artifact写入端核真实Evidence与适用批准，预览的返回值或调用者自填审核字段不改变工程。提议与实际Media命令有明确状态，预览不写已确认版。明确获准成片委托直接回同一工程的私有草稿和决策差异。局改按已有OperationIntent、锁定与版本冲突规则提交，未经授权的应用或失效提议返回具体原因和补救。

现有clip/take及ChangePlan保`context_basis`，引用确切源版/源区间、说话者、必要相邻语境和已知录制/事件时间；`editorial_relation`区分有据连续关系、蒙太奇、重现/示意及生成片段，并保关系依据/限定和确认状态。字段描述原编辑决定，不创建第二时间线或给素材授使用权；缺失录制时间不补为同时。对一次剪辑返回可能改变否定/条件、人物归属或互动关系的具体差异与源Anchor，普通无冲突操作仍走原命令。人工纠正创建后继工程版，相关字幕/章节/切片沿原依赖更新；来源被撤权与用户选择表达风格是不同的限制。

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

### 来源、监测、智能与扩展的接续合同

采集请求固定query/source/access-class/data-surface/目的/窗口/账号世代与input_revision；每页原件/覆盖/解析版和安全游标一起提交。批次按B5质量合同返回各指标计数/覆盖及待核原因，业务决定资格由同一现有来源与分析对象结合实际用途核定，采集成功或模型置信值不直接授权排名、联系或加投。监测另存source_watermark、按rule/method_revision的evaluation_watermark及notification_receipt；变规则不推进尚未评价位置。来源撤权与规则更改是不同动作，前者限制新使用，后者可在原合法材料上补评。

Observation响应分别表达取得状态、source_validity、SourceUse、identity_resolution和analysis_coverage；未绑定主体的合法观察可引用现有Source/Observation ID，不能编造SubjectRef或以相关性失败删原件。确认身份、改相关性与接收新观察是不同的现有领域操作，各自绑定父版、依据与影响；原件字节和取得时间不被后置解释覆盖。Analysis结果引用稳定method_id/method_revision、输入快照、MetricDefinition版、实际参数/随机种子或误差合同及result_ref；界面、文件与AI消费相同result_ref，展示转换不能改变业务量纲或再造计算结果。

智能请求区分短答与持久执行，保目标/对象/选区/资料用途、模型真实ID及能力合同。每块结果有run_id、attempt、object_ref、base_revision、block_id/revision、sequence和type；ACK只确认连续已收/持久范围，接收最大序号不代表中间已齐。重连按原run补内容或返回一致快照，不重新产生工具效果；迟到块附为待审候选而非覆盖人改稿。

扩展能力响应保协议族/版本、完整工具schema指纹、包/launcher/依赖版、权限和真实状态；列表展示截断与实际schema分页分开。MCP 2026-07-28族采用每请求版本及能力元数据和server/discover，旧2025-11-25族使用initialize；兼容切换只按协议允许的版本错误处理，不能对普通业务失败重复执行另一协议版本。异步operation ID、InputRequired与取消仅在对应协议能力成立时消费同一Job，不伪造远端取消。

本地stdio或脚本调用还需要已建立且可验证的OS隔离、受权文件/网络broker及拥有者范围内的资源限制；readOnlyHint或未抛异常不表示安全。结果按其真实类型进入Material/专业Owner，再返回可继续成果；工具不支持的输出或只取得第一页明确partial，不因JSON结构合法标整个用户任务完成。

### 原文采用与交付的完整性细则

引文采用请求同时绑定确切源/解析版、类型化Anchor/原句、目标稿父版、用途及操作意图，正文变更和来源采用关系由已有同一领域事务提交；不能有正文已存而来源ledger缺失仍放行的状态。恢复和导出逐所选版本重核依赖，缺关系或损坏关系返回准确待核，不假设无来源。只移除UI引用行不消除实际正文用途；明确去引用/改写沿新版本和相应影响处理。

整版研究采用使用原Work/Artifact的采纳命令及原资料库Owner，输入确切研究Artifact版本、目标scope/用途和同一OperationIntent。事务提交的采用关系和Material/Source投影均须可读回，响应给两者的版本引用，原快照的读权限另核。中途失败保未采用且原保存版可继续，同键重试回原回执。撤回或版本变化保精确失效范围，未知作者旧资料不能自动确权。

`Anchor.offset_unit`声明UTF-16码元、Unicode码点或UTF-8字节；规范服务端文本操作用码点，边界映射取同版原文及明确分段规则。界面字素保护不是自动转换授权，半个代理项/字素和未映射显示文本拒绝该范围操作，保原输入；不静默规范化。完整字段替换与局部范围操作分型，expected原文及父版同时核，不能靠可见diff猜底层提交位置。

`ExportRef`区分源Artifact版本/hash、生成配方/用途、文件身份和实际输出bytes hash；服务端回执和客户端核验对应同一下载对象。验证后的字节到交付阶段保不可变副本或受控独占，客户端选择/权限/格式改变使迟到结果不能冒新导出。响应hash自洽不代独立语义验证，下载请求发出不代磁盘读回或接收方已收到。

### 扩展连接的协议与发现回执

原ConnectionRef/Extension记录protocol_family=legacy|modern、protocol_version、transport、endpoint_or_process_digest、auth_generation、capabilities_digest、schema_digest、discovered_at/expires_at及discovery_coverage。发现响应、实际允许调用和专业结果分开；serverInfo不进入授权条件。显式pin不降级，无公共版本返回PROTOCOL_INCOMPATIBLE；认证失败、限流、部分工具发现和业务效果待核沿既有错误信封分型。工具执行绑定精确schema及同一OperationIntent，协议重连不能创建第二次外部动作。请求级元数据、旧初始化及兼容探测的具体规则由智能领域§7唯一持有。

### 取得验证、页面读集与原生方法描述

来源回执将checked_at与published/event/observed时点分开；条件取得含representation_key、validator和所复用完整Source版，304不返回伪正文，字段覆盖仍逐数据面保留。Browser动作除现有page/frame/navigation及控制世代，增加只含必要非秘密信息的read_set与expected_postcondition；quote/position锚绑定source_representation和offset_unit。重定位产生新锚与映射状态，多个匹配不能自动取第一个；历史引用继续指原版。

现有能力注册条目补origin_kind（native_method或reviewed_external）、method_revision、legacy_aliases、operation_ref、input_schema/output_schema及完整指纹、effect_class、required_scope、resource_bounds、cancel/recovery语义和acceptance_refs。原生方法绑定B/C/I当前用例，不等待旧包；外部方法另有包字节/来源/许可与隔离资格。operation_ref未接处理器时准确返回不可用，不能生成空success。旧编号、展示名称和包内allowed-tools均不授权限；真实schema/方法/依赖变化使旧批准需重核。参数错误、输入缺失、范围失权、资源超限、效果待核和领域失败沿现有错误信封分型，不另造任务状态机。

### 通用检索、原生资料与主体消费者的目标合同

下列字段扩现有retrieval/Acquisition/Browser与Analysis用例；具体接口新增或兼容接线须在实现时连同当前客户端完成，不是本轮运行声明。`POST /api/search`接收同一OperationIntent及query、WorkContext、可选SubjectRef、允许来源集合、业务时区/期间、所需字段/内容、用途、freshness要求和分页/预算；不要求SubjectRef或平台登录永远存在。范围可组合workspace、public、connected及显式browser_targets，不能从空范围推全部私有资料。当前相关工具和聊天调用同一检索用例，不另存聊天专属结果。

响应沿现有成功信封返回query_id/query_revision、scope_used、真实来源覆盖/错误、可续接cursor及结果。每项包含可访问ObjectRef/SourceRef、精确来源/解析版、原生ID与URL、作者/平台、published/modified/event/observed时点及各自未知状态、Anchor/选区、content_kind、原文/摘要/系统说明的区分、可用媒体/评论范围、SourceUse与当前允许动作；未取得正文只标snippet，不能签全文。排序理由与具体方法版可查，相关性不授事实资格。长期监测另有明确意图，查询/打开发现不创建订阅；仅预览可临时展示，保存/分析/引用时由同一Material/Observation取得可持久版本。

检索返回的字段/全文排序先做当前权限/用途/数据域过滤，分页、总数与片段都不能泄露其他客户。缓存含输入/来源/权限版本和实际取得时刻；超freshness、断连、撤权与源变更准确标stale/unavailable/partial，旧缓存不冒实时。结果不足时在原允许范围补取，用户显式限定集合不暗扩；全期间统计沿独立analysis input_snapshot的完整纳排集合，不能以Top-N或当前页充当分母。cursor只延续原query/账号/源版/排序及水位，过期重建可解释结果而非拼接不同集合。

现有Observation/Material投影的`subject_associations`逐项复用SubjectRef与关联版，保role（publisher/about/mention/account_owner/location）、evidence_anchor、有效期间/未知、observed_at、relation_revision及resolution_state；内容可关联多个有据主体，未知关联不必丢弃合法观察。角色可按现有业务关系schema扩展，不能通过更改字符串自动授已核身份。关联/纠错沿Business既有命令，带base_version、精确来源版、理由和影响；不得重写原取得时间、原作者或原字节。

Analysis input_snapshot冻结本次纳入/排除ObjectRef集合或可独立重建的不可变集合引用、纳排理由、采样单位、去重规则及query/source/relation/MetricDefinition/method版本。结果返回实际支持的指标、单位/分母/覆盖与资格、支持/反证、可修改锚、准确依赖和下游Artifact/Work引用；展示游标只决定显示子集。原生内容→主体档案/关系→经营/传播/合作/事件/预算等专业结果→原生文件与作品均用这条依赖，不让前端/模型另算另一真值。更正或撤权按同一影响集返回需要重核/可继续/失权部分，保合法历史及人工锁定。

### 检索字符投影与引用候选的精确字段

原search结果新增lexical_route、tokenization_revision、projection_revision/digest及match_ranges。范围声明coordinate_unit，允许unicode_code_point、utf8_byte、utf16_code_unit或grapheme_index，但服务端Anchor唯一规范单位为unicode_code_point；转换同时带原串摘要与Unicode规则版。搜索投影使用token map定位回不可变原文；不能把FTS预编码串的offset直接返回为正文坐标。排序含实际合格corpus_revision与rank_method_revision，BM25原生负rank与本产品作用域内正相关分分别命名，不混升降序。

Anchor保source_revision、projection_revision/digest、[start,end)、exact/prefix/suffix与原生block/page/media位置。重定位返回resolved|ambiguous|unresolved以及具权限的候选新Anchor和差异，禁止多个候选时自动选第一项或执行全局替换。解析/源版/选区变化使旧修订预览失效；用户选择或已核唯一映射通过原ChangeSet提交，不设第二批注系统。规范化映射包含source_ranges、target_range、transform_kind及lossy标记；无可靠映射的匹配只给候选，不授权直接改原文。

### 多路径检索与既有成果复用的目标字段

Dense索引和查询回执携带embedding_space_ref（模型/权重revision、预处理与归一化版、维度、距离度量）及index_generation；使用跨空间映射时另带经验证的mapping_revision与适用边界。空间合同不匹配逐路返回vector_space_mismatch并排除该分数，不能改成0或伪装未命中；其它合法路线继续。新代次记录已覆盖源/解析版、缺口和验证结果，完整读回且当前用途成立后才原子切换；失败可回退原有效代次，不混代或复活已撤权向量。

同一`query_id/query_revision`在现有QueryPlan及取得回执中返回可按需展开的`query_paths`，逐路径含`path_id/parent_path_id`、起点SourceRef/Anchor、query/scope/data_surface、发现的原生对象引用、实际取得或未执行状态、coverage、checkpoint_ref和stop_reason。路径身份与内容身份分开：不同路径发现同一对象仅增加发现关系，返回同一获权ObjectRef及本次实际Observation；路径不能自行授SubjectRef或来源独立性。路径/父线索的返回同样按当前权限过滤，不通过数量或标题泄露其他客户；普通界面先显示结果和必要缺口，不要求用户操作内部路径ID。

复用现有`input_snapshot`和`OperationReceipt`的结果引用，增加可解释的reuse说明：所用输入/来源/解析/方法版、覆盖、原观察时刻、当前核验时刻及本次复用或失效理由；这是原结果的消费记录，不建立第二结果存储或完成状态。缓存键覆盖workspace/data_domain、获权来源与身份、查询范围/排序/数据面、输入及方法版；freshness与撤权另作当前条件核验，命中键不自动有效。新源观察和旧结果重用不得共用虚构的“刚刚取得”时间。集合统计绑定完整输入快照，不以query_paths数或显示子集作分母。

取消、续接和补证继续调用原Job/Acquisition命令，沿同一原操作读回已持久单元，仅请求仍缺且获准的数据面。路径重复、缺UID、规范化歧义和反证是可报告结果；它们不触发自动合并主体、联系、投诉或公开发布。

### 用户现有浏览器与实时上下文的目标协议

`BrowserSession`扩connection_mode=managed|attached_extension|attached_protocol、process_ownership=owned|borrowed及服务端核定ConnectionRef。ConnectionRef含已配对device_ref/adapter_id/version、获准浏览器实例、目标标签集合、站点/动作/用途范围、permission_generation与真实capabilities；是现Browser会话的连接元数据，不另建权限系统。浏览器原生权限和产品授权都必须成立，不能客户端自报任意tab_id便取得访问。建立配对时给用户可辨设备/浏览器/账户与范围、短期一次nonce及受众；复连核原身份和当前权限，失联不自动改用其他浏览器或账户。

| 现有接口的目标扩充 | 输入与回执要求 |
|---|---|
| `POST /api/browser-sessions`及`/{id}/reopen` | 建managed会话或引用已建立的ConnectionRef及选定目标；只做连接/必要可读性核验，已有任务等待连接时按原意图续接。Browser能力逐项返回live_context、semantic_actions、coordinate_actions、tab_capture、target_audio、files等实际支持/限制。 |
| `POST /api/browser-sessions/{id}/read`与`/capture` | read返回有界当前投影及coverage，capture才把所选范围沿Source/Material持久化；都保connection/page/frame、navigation_generation、document_revision、semantic_revision、captured_at、projection_kind/version、SourceUse及权限代次。请求目标或选区过期时返回STALE_TARGET及合法刷新动作。 |
| `POST /api/browser-sessions/{id}/actions`与既有semantic-actions | 带page/frame/navigation/document/semantic基线、实际语义节点或当前CSS坐标/视口、独立operation_id、控制世代和必要效果授权。绑定相同目标，执行前复核、执行后读实际结果；不以dispatch成功签网站提交。 |
| `POST .../checkpoint`、`/suspend`、`/close`及host detach | 复用原暂停/控制/关闭语义；borrowed仅停止订阅与动作、释放产品持有连接，不关用户标签/进程/登录或改其viewport。owned临时目标只有确属产品时才释放。取消Job与断开会话分别记录。 |
| `GET .../operations/{operation_id}`与`POST .../reconcile` | 查询原意图是否送达、实际提交及可查回执。已执行但响应失联是outcome_unknown，先对账；重连不重放旧点击、上传、发布或付款。 |

document_revision随同URL正文、DOM及实际可读内容的更改推进，不只在导航时变化；semantic_revision分别绑定对应可访问语义/动作目标。原生tab_id只留适配器映射，对外仍用服务端page_id。上下文增量经现Browser事件/实时通道发送单调seq、base_revision/new_revision及受影响page/frame。缺序、导航、页面重建或授权换代必须取新快照；服务端校接收次序与原始捕获时刻，不能把收到时刻伪作网页更新时刻。SPA、跨源iframe、懒加载/虚拟列表、shadow DOM、附件、媒体和字幕分别保coverage；截图与OCR不冒语义树/完整原文，字幕不冒直接音频理解。原目标已变或节点消失不根据旧坐标猜点。

连接策略由现Browser和权限责任共同持有：activeTab临时授权不能跨域延续；debugger不能作为optional_permissions；原生附着只用实际浏览器支持且用户允许的机制。扩展内容消息核sender/tab/frame/origin及schema，本机桥核已配对扩展身份、用户/工作/设备和nonce/序号，网页脚本不能变成受信指令。秘密值不进投影、日志或模型，第三方页面不能改变允许动作；文件只接当前chooser与用户选定对象，不枚举电脑文件。borrowed音频只能目标标签捕获，缺能力单项不可用；普通Web或手机缺配对桥时返回连接入口，不假装已读电脑标签。

平台AI会话沿原QueryPath/BrowserSession/Job扩展data_surface=platform_ai_conversation。提交前用现有SourceUse核提供方许可版本、应用用途及材料可提交范围；登录状态不能替代用途准入。调用绑定operation_id、当前Work/context_revision、provider_surface、connection_revision、page_ref、conversation_ref、指定材料版与准许用途、问题摘要。消息提交回执独立于回答，保可核message_ref、submission_state、generation_state、原问题/回答及附件引用；未知发送回执先核同会话，禁止据超时新建重复发送。回答进入原Material/Observation并附可见原文链接、capture_time、coverage和限制，核实后的主张再按既有证据准入。登录、接管、额度、取消、页面改版和撤权沿原状态机返回真实原因，不另建平台专属权限。主聊天所选provider只承担推理/对话，工具动作仍返回同一领域OperationIntent/ResultEnvelope。

### 浏览快照与操作回执不可互相替代

Open Original默认沿当前SourceRef及确切来源版进入真实受控原文；用户明确外开时，服务端从该引用投影并重核允许的URL scheme、目标/重定向与当前权限，不接受任意裸路径冒来源。缺URL、非法目标、拒绝、启动失败及`HANDOFF_ACCEPTED`分别返回；后者仅证外部浏览器接受启动，无法观察外部页时loaded/readable保持UNKNOWN。`RETURNED`仅证回到原任务，不签原文读取、采集或来源准入成功；返回仍恢复原对象与阅读锚。

`CaptureRef`沿既有Source/Material持有服务端核定的capture_id、session/page/context身份、captured_at、原始或可见快照hash、projection_kind/version、完整/部分范围及用途；`SelectionRef`绑定同版块和坐标单位、半开区间与精确原句。客户端输入只能引用已获权捕获，不能提交虚构来源标识来取得已核状态；手写内容返回用户笔记类别。导航版和内容快照版同时记录，同URL不得默认同一正文。

`OperationReceipt`使用原Job/请求记录返回操作是否开始、是否提交、确切结果引用、读取资格和可继续动作，正文预览TTL独立。回执可查不授材料读取；预览过期不把已提交结果改写成未发生。队列中超时与提交后响应丢失分别返回，控制权拒绝保其可恢复原因，不吞成泛化CAPTURE_UNAVAILABLE；资源限额按实际开始/完成的工作计，拒绝请求不能假耗尽用户已有捕获成果。

### 轻量记录、方法采用及通知沿现有对象交付

便笺沿既有文稿/Artifact及Scene实例保存，不额外引入第二笔记数据库；最少含当前作者/空间、正文及版本、可选Work、明确的SourceRef/Anchor、原始记录与整理候选的关系、保存资格。关闭视图不调用内容删除，draft↔saved按原幂等/冲突合同；来源用途与客户边界在采用到正式稿时再次核。

`MethodApplication`属于同一Analysis/Artifact的输入快照，记录精确方法/案例版本、当前问题、被采用机制与不采用部分、适用条件/反例、目标对象、来源/权利和实际参数；不创建另一策略平台。推荐、预览、采用、正式成果与独立效果各有状态，API不能只返回目录ID就宣布方法已被用到作品。

通知引用确切Task/Artifact/事件及ReturnAnchor，状态区分创建、发送待核/已受理、用户已读和实际处理；传输接受不证明收件，重试复用通知身份不重跑业务。列表、详情、预览与打开均核当前收件范围，深链不授权限；未知旧版本不可读时给准确不可用状态，不查询另一个客户的相似对象代替。

## 精确修改、长任务与交付协议

一次修改先固定父版本、选区和操作意图，再读取所需的原件和关系版。结果返回实际受影响对象、属性级差异、保留对象、输入/输出版本和未解决意见。撤销是对具体编辑历史的逆操作或新修订；不能把“恢复旧整份文件”冒局部撤销。对于并发编辑，能证明不冲突的独立对象可合并；其余返回当前版及差异，保本机草稿，不靠最后写入覆盖。

每个客户端提交在发送前保本地请求身份、父版和未发送编辑。超时后先查同一请求的持久化结果；同一意图的重试复用幂等键，输入变化或用户明确发起新的独立动作产生新身份。相同参数不等同一意图，例如再次刷新、重新录制或重新创作；未决外部效果仍先核对。同一键/不同输入冲突，已完成操作返回同一成果引用。当前初始客户端每次调用新建键的写法不能直接沿用为断线重试策略；不把“服务端支持幂等”误写成端到端已防重复。

Job按架构的实际状态、权威运行端世代、attempt/lease generation与检查点恢复；返回已完成/待处理单位及单位含义、当前结果引用、可取消性、错误原因和可行恢复动作。重建或恢复运行端后，旧租约和旧世代不能提交，即使任务ID相同；运行世代不是新的用户权限。并发任务按工作空间及资源公平安排，取消仅停止相应任务。流式输出带请求/对象、父版、递增序号和已确认位置；重连回放缺块，停止不触发另一次生成，文本暂显不冒已保存。专业对象只有经领域校验和保存读回后才能更新已保存状态。

Job同时返回control_revision、pause_requested/paused的真实状态、suspended_from与未决RequiredInput引用，及当前可用控制动作。暂停、续接、补问输入和取消各有独立operation_id及base_control_revision；重试同一控制读取原回执，控制版冲突不覆盖新决定。补问绑定原意图/要求版，暂停期间补问解决不自动运行；网络等待超时只使本次客户端回调代次失效，不改服务端Job或租约。取消后的普通续接拒绝；有明确领域恢复能力时才能按架构保取消历史并开新attempt，不能假称取消没发生。

以下定义目标Job控制接口及兼容扩充，尚不作为下文2026-11-03协议观察中的已实现能力；实现时沿同一Job Owner补齐并用对应反例验证。

| 方法/路径 | 输入 | 实际结果 |
|---|---|---|
| POST `/api/jobs/{id}/cancel` | operation_id、base_control_revision | Cancelled/requested state；真实完成竞态与未知外部效果另保 |
| POST `/api/jobs/{id}/retry` | operation_id、base_control_revision及可重试原因 | Safely resumed Job；不解除用户暂停、不重做已发生效果 |
| POST `/api/jobs/{id}/pause` | operation_id、base_control_revision | 持久pause_requested或已确认paused，返回实际检查点；不冒远端动作撤销 |
| POST `/api/jobs/{id}/resume` | operation_id、base_control_revision、确切ResumeContext | 重核前置后queued或真实等待态；仅支持明确可恢复状态/领域，已完成回原成果 |
| POST `/api/jobs/{id}/inputs` | operation_id、base_control_revision及原ClarificationResponse | 保存经校验的最小补充与要求版；保持用户暂停，过期输入返回冲突及可见当前要求 |

导出请求绑定确切成果版、格式与用途参数；响应返回服务端确定的文件名、MIME、大小、导出状态和授权下载引用，客户端不能将所有未知格式强行命名为`.md`。工程包还要列确切成员/依赖/缺件及合法用途；文件写成、下载完成、原生应用打开/播放/重算、完整批次可交付分别验证。外部发送/发布/购买另有目标和批准范围；超时待核不能重发。

错误保可编程code与用户可理解原因/恢复动作。读取越权对象不泄漏其存在；版本冲突返回调用者可看的当前版与差异；权限撤销停止新动作，保允许保留的合法结果；来源更正/不可用不能用旧缓存冒最新。系统内部ID、栈、租约、模型路由等不进入正常业务文案。新增协议版本需向旧客户端明确可读/可改能力，不能默默丢弃未知专业字段后写回。

### Web工作区宿主的HTTP合同

既有Web host只把合法工作区入口与无扩展深链映射到同一构建入口；API/运行命名空间、真实资产和目录/穿越请求不能回退成HTML 200。构建不可用返回503及可理解的恢复状态，资产不存在返回404；无权业务资源仍沿当前权限合同处理。响应MIME、构建身份和资源版本必须匹配实际字节，深链只定位原工作，不授读取权或重放业务动作。

入口HTML返回Cache-Control: no-store；未散列启动/runtime资源返回max-age=0, must-revalidate及对应表示ETag，If-None-Match匹配返回304，字节改变返回新200/ETag。内容散列命名的不可变公共构建资产可返回public, max-age=31536000, immutable；该规则不覆盖资料/成果/下载/会话的私有访问策略。缺资产、失效缓存及新旧客户端/API不兼容返回确切状态，不靠HTML回退、旧缓存或静默丢字段伪装可用。

现有Job回执的受权诊断按attempt可返回stage_timings，记录阶段、同钟域单调开始/持续、观察UTC、实际完成/失败/中止、方法配置版、重试及缓存，计量未知为null。首实际模型输出、首个可用成果及最终提交各有真实事件，重叠阶段和排队分别计。ASR与声音回执另带实际源音频秒、适用处理秒、实时系数与倍速，分母为0或不可知时对应比例为null；不向普通读者暴露私有配置或素材。

### 成功、部分结果与错误的消费者规则

协议声明schema方言/版、实际支持关键字和被执行的格式规则；schema错误、未支持必需语义、外部引用不获准及资源越界给具体失败。校验器不能将未知关键字、未配置format检查或加载失败的空规则标成功。通过类型后，领域仍核对象/来源资格、单位/时间/币种及父版/选区的组合语义；这与保存原件未知字段的兼容责任不冲突。

继续使用既有`data`成功与`error:{code,message,details}`失败信封，目标专业字段按schema版兼容扩展，不突然换一套全局包装。异步受理返回持久任务身份而不是专业完成；结果对象分别表达处理、持久化、兼容/专业检查和交付状态。缺项不能以空字符串、空列表或0冒成功，合法空结果保实际检查范围。

版本过期/同键异输入为409且仅返回调用者可见差异；身份失效401；对象访问拒绝按防枚举政策404/统一拒绝；来源站点401/403/429保实际source_error和恢复条件，不能一律转产品登录失败。输入类型/坐标/公式不合法给字段或对象位置；资源预算、取消、依赖不可用及外部效果未知各自有可编程原因。当前代码中的具体code可兼容映射，但不得合并不同业务后果。

控制取消只核当前用户对任务的控制权，不先要求已失权资料仍可读；续算再核全部必要来源。响应不泄秘密、裸路径或内部堆栈。客户端按返回的可用部分/恢复动作工作，不能看到HTTP200便把已受理、草稿保存、审核通过、正式发布和实际解决写成同一完成状态。

## 备份、迁入和升级的接口边界

工作区导出、整端备份、旧数据迁入、隔离恢复和切换权威端是不同授权动作，不能共享一个任意文件路径的写接口。请求引用获准的数据根/包对象、目标工作区及操作意图，实际路径由运行端解析并校验。普通用户选择自己的工作，不提交跨工作区表名、进程号、密钥或数据库命令。

恢复点返回备份对象身份、实际快照时点、数据库模式、包格式、所含范围、文件清单身份、完整性/引用核验结果、所需受保护恢复条件及缺失项。创建中、完整可核、失败与已验证恢复分别成立；没有实际恢复演练的完整包不能返回“已验证恢复”。恢复返回隔离目标及实际可继续的对象，切换另核当前写入者、授权、版本与未知效果；恢复点不能携带可直接复活的访问令牌或运行租约。秘密只经架构§5规定的明确受保护流程处理，不出现在普通响应或诊断包。

迁入返回每类原身份到新身份的映射、保持的版本/关系、冲突和未处理项；重试按同一迁入意图处理，不重复建立主体或导入同版作品。用户处理一个冲突不得隐式批准其余冲突。兼容信息返回实际API/对象/草稿/媒体工程/模型包的可读可写版本及必要更新动作；客户端据此保留原件和新稿，不靠丢弃未知字段达到保存成功。上述调用属于现有运行、文件、身份和领域消费者，具体接线必须与真实实现同时完成，不以空路由计可用。

<a id="当前基础接口与兼容迁入边界"></a>

## 历史本地基础接口与兼容适配记录

以下及后续现有路由段保留2026-11-03工作树形成的协议/实施观察，供兼容和源码定位；本轮未运行或重签其中测试。它们不是当前运行声明、永久资源上限或全产品实现清单。与上文完整目标合同不同的限制须在后续获准开发中适配并验证，不据旧观察删需求。现有 `server/oevira/api.py` 与 `app/lib/src/api.dart`、`models.dart` 已有Work/Material/Job/文本Artifact消费者；`office_api.py`、`monitoring.py` 含独立的PPT修订与监测接口。静态源码存在不等运行或专业验收，未提交成果也不自动算线上发布。迁入时在这些实际消费者上接完整对象，不用“以后再接”掩盖前端仍只有文本。

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
The historical local-mode binding below uses loopback only; the approved user-controlled server mode retains its own authenticated deployment contract. A public OAuth callback/deployment is not enabled by
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

Historical compatibility only: the older `GET/POST /api/business/subjects` contract
lists or creates `brand`/`company` records. The current target is the unified SubjectRef
projection above; these legacy values do not impose a new-product entry requirement.
In that old contract,
names are at most 200 characters. Creation returns `{subject,possible_matches,replayed}`.
`GET /api/business/subjects/{id}` returns the subject with `evidence` and
`relationships`; evidence text is `source.excerpt`. PATCH/DELETE and POST
`/{id}/restore` use `base_version`. Evidence and relationship endpoints attach
exact source/identity/time objects. The old two-type limit is superseded while
actual entity identities and relationships remain distinct. Same names are
candidates for review, not implicit merges. Removal preserves history; re-add
requires fresh review.

Monitoring configuration accepts nullable `subject_id`. An explicit binding
must resolve in the current workspace; free-text scope never creates one.
Removing a bound subject pauses the corresponding rules and stops their new
execution transactionally, preserving already acquired materials and history.
Re-adding a subject does not resume its monitors without an explicit action.
Existing unbound monitors remain valid during the additive v4-to-v5 migration.

<a id="cth-cases-and-shared-source-observations"></a>

### Business Cases and shared source observations（原CTH协议来源）

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

以下为历史Media路由及其当时限制的兼容记录；目标仍使用同一Media职责、workspace身份、Material与版本链。是否注册/运行须按实际版本重核，接口成功不等于语种质量或来源权利通过。

| 路由 | 请求与真实行为 | 保存、限制与失败 |
|---|---|---|
| `GET /api/media/speech/capabilities` | 返回本机实际可执行的Flite/eSpeak voice、语言映射和Whisper CPU运行时版本/哈希；缺件/不匹配显示不可用，不下载、不联网回退 | 声音自然度/发音、ASR准确率与词级时间精度均单独评估，不从voice清单推导质量 |
| `POST /api/media/speech/synthesize` | `{engine?:"ffmpeg_libflite"|"espeak_ng", text, language, voice}`；按明确引擎/语言/voice生成完整合成WAV和来源声明。Flite现限ASCII英文；eSpeak接受运行时真实列出的voice；两者保真实原文、引擎/voice和合成标志 | 不改项目、不建Material、不发布；客户端以原`POST /api/uploads`分块写入Material，再显式调用项目`sources`添加。同一文件后续保存有Material SHA和WAV内嵌声明； rights待核、synthetic、非本人克隆，长度超限整体拒绝不截断 |
| `POST /api/media/projects/{project_id}/transcriptions` | 请求`version/source_asset_id/audio_stream_index/audio_channel_index/task`及可选`language_hint`，并带`Idempotency-Key`。冻结输入来源/哈希/单一源声道/音流/任务，进入同一Worker Job；仅当前打开版本可创建 | 不改音频；Whisper base在CPU本地以30秒块与1.5秒上下文生成未审阅候选，持久化各完成块。已检测PTS缺口/重叠则该次转写失败，源和工程保持不变 |
| `GET /api/media/transcriptions/{id}?word_offset=&word_limit=`；`POST /api/media/transcriptions/{id}/resume` | 分页读取源SHA/声道/源样本范围/PTS时基、候选文本/估计词界、模型版本及任务状态；只对cancelled/interrupted任务允许resume | 候选默认`review_state=unreviewed`并注明词界估计、质量NOT_EVALUATED；恢复保留已完成块，从最后完整块续做 |
| `POST /api/media/projects/{project_id}/transcriptions/{id}/adopt` | `{base_version, word_corrections?, words?, review_state?}`；同一project当前版本CAS。`word_corrections`按候选`word_id`只改文字/样本范围；若提交`words`，须保留完整候选的有序ID集合 | 仅采用时写入新的可撤销Timeline版；旧素材/音频未改。实际word-cut必须另行选确切片段/词实例并调用`word-cut-previews`；translate结果不作为源词时间切音频。 |

转写`task`为`transcribe`或`translate`；language hint使用Whisper语言代码。`audio_channel_index`必选，立体声/多声道不折叠求平均。对流内非连续包PTS目前明确拒绝，避免扁平化后错误映射；该限制不阻止原件播放、人工对齐或手工剪辑。产品取消使用现有`POST /api/jobs/{job_id}/cancel`，不得用HTTP请求超时推断Job失败。

### 寰意内创意分享与发布后外部分享的目标合同

本节定义完整目标；后文历史Server社区路由只保当时兼容边界，其media/artifact限制不缩减这里的短文、图文、多图、横竖/长短视频、音频/播客和系列。沿同一PublicationDraft/Release、Store、Artifact/Material/Media、身份和Job扩充，不复制社区业务引擎。

`Publication.kind`仍为work|template|experience。`content.blocks`含稳定block_id及paragraph|heading|link|image|video|audio类型，文字/链接有原生内容与允许引用，媒体引用确切成员/源版本；有序item_id和可选group_id表达多图组合，成员保alt、caption、原比例、展示crop、字幕/章节及各用途权利。私有草稿保存来源Material/Artifact映射，公开响应只给获准publication member ID、发布派生物及可公开署名/来源，不泄私有路径、客户ID、原输入快照。短文由原Artifact用例自动创建最小私有稿，title/summary/模板字段按实际格式需要，不强迫每条短文填写。旧body在兼容读取时映为稳定文字块，迁移保原件与版本映射，不维护可独立改写的第二正文。草稿的变化带base_version和幂等意图；保存失败保输入，重开恢复同一稿。

`creator/coauthors`保实际署名及共同署名的接受记录，`subject_refs`只表达作品的创作对象/相关主体，不能代官方身份、商标权、客户批准或经营事实。Series沿现有内容集合引用有序Publication/Release成员，冻结版和跟随当前公开版分别明示，逐成员当前权限/撤回状态生效；一个成员失效不把其他合法作品删掉或泄出受限标题。

发布请求沿`POST /api/publications/{id}/publish`及OperationIntent，绑定base_version、content_digest、visibility、rights_digest、selected_member_ids、用户意图与当前授权世代。必要自动检查和人工审阅按同一已定义策略执行：审核决定不创造用户意图，已明确且未改变的发布意图可等待检查后继续提交，实质内容/受众/权利变化才返回需要修正或确认的差异。自动合格不伪造人工审核员，人工角色只访问对应提交版；不为普通内容创建必经全量人工审批。响应分别报accepted/checking/review_required及原Job、已提交ReleaseRef和实际delivery状态；未托管只返回local_review_only，不能创建假公网URL。

`PublicationRelease`冻结确切内容/成员/许可/审阅及可见范围。private/team/unlisted/public继续分别授权，评论绑定release及block_id/item_id、同版文字范围或媒体时码，旧版评论的文字锚/媒体成员/时码不漂到新版；新改稿形成新草稿/新release，旧链接不得静默指向不同正文冒原分享版。公开索引/封面/媒体、收藏/关注/评论和下载按当前访问资格过滤；撤回保适用墓碑与合法历史，不继续取原正文。播放进度按确切release/member和真实时长，多图位置按item_id，返回原筛选、游标、焦点与滚动锚，不以换页重建另一作品。

`POST /api/releases/{id}/shares`为原发布Owner上的目标适配入口；请求含同一OperationIntent/幂等键、channel=permalink|system_share|export|platform、精确release/member选择、目的地/账号/受众、SourceUse/许可及父访问世代。permalink产生当前可访问的确切发布链接；private/team链接仍需成员，unlisted明确可撤销凭证且不入发现；export仅生成获准成员文件/格式，platform仅用实际已支持且授权的通道。系统不得因为分享方便把私有稿改公开、夹带未选/无该次分发权的客户材料或重新上传同一未知效果；获准且明确选择的客户作品沿同一成员与权利交集处理。

回执沿原OperationReceipt/Job，effect_type区分link_copy、file_export、share_handoff和platform_publish，effect_phase说明prepared/handed_off/confirmed/failed/cancelled/outcome_unknown并保实际证据；这些是既有外部动作结果，不是第二任务状态机。复制链接完成只证link_copy，Web Share resolve、二维码或拉起外部编辑页只证handoff；外平台成功需对应账号/内容的真实回执。`GET /api/releases/{id}/shares/{operation_id}`读原回执，重试先查结果；无可查询渠道的未知结果保持待核，由用户核实或明确处理，不盲目重复。取消分享不撤回内部作品，内部撤回不假称已经删除外平台副本。

系统交接通道的POST先按release和精确成员生成prepared回执及受限payload；只准备不执行外部动作。准备项绑定短期到期、当前授权世代、原受众、成员摘要及一次客户端操作ID。客户端在用户实际触发前取得该payload并核能力，若过期、已知撤权或版变化立即失效；URL的每次访问仍核当前release资格，不能将链接当已分发事实。交接结束用`PATCH /api/releases/{id}/shares/{operation_id}`幂等回写该同一回执的客户端观测，携带操作ID、receipt revision、观测`handed_off|aborted_or_unavailable|failed`和实际原因；首项推进到handed_off，后两项保未交接并记失败原因；仅有可核用户明确取消的事件才写cancelled。不得以客户端事件晋升`confirmed`，无返回保持待核。回写只记录已发生的观测，即使授权世代已变化也不得重新签发、外发或更改发布版；权限不足时只保原合法观测待有权恢复对账。仅网页端AbortError无法区分用户取消与无目标时原样保合并语义，不虚构用户选择。依据[W3C Web Share](https://www.w3.org/TR/web-share/)。

官方平台的投稿、转发、好友分享及各应用资格分别校通道能力；一个外部通道不支持商业内容不变成寰意内部创作/发布的禁止范围，也不能换账号/伪装应用绕过条件。没有该通道时保合法链接、文件交付和已成立的内部发布，准确显示不可用项；发布环境、供应商和付费仍遵实际授权。网页分享使用真实平台可用性及用户触发，规范依据为[Web Share](https://www.w3.org/TR/web-share/)与[抖音当前发布/分享规范](https://developer.open-douyin.com/docs/resource/zh-CN/dop/operation-standard/platform-capabilities/useclue)，不是承诺已获得其接入资格。

### 发现快照、上传提交与播放资源协议

GET /api/discovery扩展order=recommended，必须存在该用户明确启用的推荐用途；latest/following/featured保持原语义。响应包含feed_snapshot_id、query/filter/ranking_revision、截止时点、preference_generation及稳定release引用/cursor，各项explanation只列实际候选来源和适用过滤。重置推荐沿原账号偏好及派生索引Owner写新代次，旧cursor返回CURSOR_STALE及合法返回锚，不清收藏或私人作品；每页和逐项媒体再次核当前权限。

沿既有POST /api/uploads、HEAD/PATCH /api/uploads/{id}、complete/cancel使用Upload的expected_size、committed_offset、chunk_length/digest、full_digest、lease/cancel_generation及expires_at。JSON中的超出安全整数范围的字节计数使用十进制字符串并在两端校验；HTTP偏移头为十进制整数。HEAD no-store；offset/CAS冲突409不写，摘要错误不推进；complete以所有已提交连续块及全件摘要形成唯一Material与原操作回执，未完全校验仅返回真实处理阶段。

播放请求返回同一PlaybackGrant及RenditionManifest：release/member、rendition_revision、content_digest、delivery=local_review_only|hosted、transport=hls_vod|range、实际codecs/size/duration/time_base、quality列表、字幕/章节成员、expiry/revocation_generation和当前允许动作。目标路由GET/HEAD /api/releases/{id}/media/{member}/renditions/{rendition}/manifest、.../segments/{segment}及.../file只接受服务端登记成员；manifest子URI使用同源鉴权路线，禁止直接挂载私有存储目录。分片、init、字幕和Range均核同一当前权限；Range合法请求206、不可满足416，读取回执不计观看。旧streams/{token}兼容适配到同一grant，不并行维护第二权限表。

内部计量经同一发布Owner接收POST /api/releases/{id}/playback-events，包含event_id、session/sequence、member、metric_definition_revision、单调播放增量、源覆盖区间、速率、可见/收听状态及适用许可。拒重复/非法范围，缺测单列；公开响应只给当前允许的聚合，不回观看者身份。业务消费用同一Observation/MetricDefinition，用户拒绝该用途时不为补计数绕许可。

## 作品发布、模板与经验协议

本节是既有社区、发布和media路由的现行目标合同；下面条目与上文的完整作品模型、发现快照、上传和播放协议共同实施。原字段与旧客户端通过同一Owner作兼容转换，不保两套发布流程。除明确公开读取外，要求有效会话及Workspace/Work/Material当前权限；携带过期或无效身份不能悄悄降级匿名。delivery由实际托管能力、当前可见范围及可读回回执决定，本机未托管时准确local_review_only，托管成立才为hosted。

| 当前路由 | 输入与实际结果 | 关键失败与边界 |
|---|---|---|
| `GET /api/community/capabilities`；`GET /api/community/media/sources` | capability报告当前审核/精选角色、实际`delivery=local_review_only\|hosted`及ffprobe状态；hosted必须具备实际托管、当前可见范围与可读回证据，不由配置URL推定；媒体来源返回当前session可读的Material、Media工程、已完成render和可读字幕/章节sidecar，不返回服务端文件路径 | capability不是托管/审核质量证明；源权限在创建、读/写、审核、range读取时再次核对；不可读原件/旧工程源明确返回不可用 |
| `GET /api/publications`、`GET /api/publications/{id}`；`POST /api/publications`；`PATCH /api/publications/{id}` | 私有列表/精确草稿版本。创建支持`artifact_id/artifact_version`，或`source_kind=media_material|media_render|media_project`及精确`source_id/source_version/media_selection`。种类为`work/template/experience`，范围为`private/team/unlisted/public`；媒体manifest只从已保存原件/完成render/工程版生成 | 客户端不能设置official、review或approved；私有输入快照不复制到release；本机路径、原Material/Artifact ID只在获准私有响应中存在；可见范围改变不自行发布 |
| `POST /api/publications/{id}/media-source-revisions` | 只允许Media template草稿/驳回稿使用更高的同项目确切Media版；提交`base_version/source_version/media_selection/content`，由服务端重建槽、依赖和媒体manifest，重新检查精确权利 | 并发修改返回版冲突；不接受客户端伪造的`media/template`快照；提交或已审核版本须先按正常状态流程重开草稿，不沿用旧审核 |
| `POST /api/publications/{id}/submit`、`GET /api/moderation/submissions`、`GET /api/moderation/submissions/{id}/media/{member}`、`POST /api/publications/{id}/review` | 兼容调用者明确单独送审时submit带精确`base_version`和`preview_acknowledged=true`；正常发布由同一意图推进所需审核，不强制再次预览确认。审核队列只给服务端审核角色已提交快照/媒体成员；结论`approved/rejected`加理由和确切版；媒体预览只读提交版中的所选成员 | 上传不等已审核或公开；作者不能审核自身提交；修改内容、媒体、依赖或权利后旧审核无效；队列不暴露源Material ID、文件路径及私有输入 |
| `POST /api/publications/{id}/publish`；`POST /api/publications/{id}/withdraw`；`POST /api/publications/{id}/appeals` | publish带确切base_version、content/rights摘要、visibility、OperationIntent和幂等键；按creative§11.2发布服务策略返回processing|blocked|auto_eligible|review_required、原因及策略版；仅人审分支需要绑定确切内容/受众的review_id，客户端不传免审。完成检查后仅在原意图、版本、权利和取消世代仍有效时提交不可变release；withdraw使后续取用失效；appeal绑定确切决定及理由 | 不强制普通短文先人工审核、再点第二次发布；修改只生成新发布版，旧分享/评论仍绑定旧版并按当前允许留存和访问规则返回，不静默换正文。新版本发布与撤回旧版是不同用户意图，不自动全部下架；无实际托管/外站回执不得报对应成功 |
| `GET /api/discovery?q=&kind=&order=&before=`；`GET /api/creators/{id}`；`GET /api/community/follows`；`POST /api/creators/{id}/follow` | latest/following/featured及显式启用的recommended，使用上文稳定快照/cursor；分页limit为资源参数而非完整集合上限，返回实际explanation。创作者名来自公开署名，非账户邮箱；follow保持幂等布尔选择 | 未登录不伪造关注/兴趣，精选来自真实角色与有效期；排序后及取媒体时再核当前public/许可/撤回，不得将private/team/unlisted纳入推荐 |
| `POST /api/releases/{id}/items`；`GET /api/community/items` | `kind=collection|queue`、非负`position`、可选有限`progress_seconds`；续播位置仅适用于有确切时长的媒体并须在programme duration内；返回当前可访问release或`availability=unavailable` | 非媒体/时长未知不得伪造进度；撤回后保存项保留为不可用，不绕过release权限 |
| `GET/POST /api/releases/{id}/comments`；`POST /api/releases/{id}/reports`；`GET /api/moderation/reports`；`POST /api/moderation/reports/{id}/resolve` | 文本评论锚定同版正文投影的Unicode码点半开区间；旧UTF-8字节坐标只由兼容适配器在原串摘要匹配、边界合法后转换，不得把字节数直接当码点序号；媒体评论锚`member_id/start_ms/end_ms`。举报需`Idempotency-Key`及用户理由；审核报告仅能dismiss或withdraw_release并写理由 | 评论/举报身份为不含联系信息的内部UUID；举报不自动证明或删除内容；每用户每日20条限频，report state冲突需刷新审核队列 |
| `POST /api/moderation/releases/{id}/feature`；`DELETE /api/moderation/releases/{id}/feature` | 审核角色按理由、rank 1–10000、时区明确的`starts_at/expires_at`作手工精选；可撤销 | 仅当前有效public release可精选；精选排序不使用伪造播放/收藏/效果数 |
| `POST /api/templates/{release}/instantiate` | 文档模板接受工作、用途及slot参数，生成新私有Artifact；`oevira-media-template/1`接受用途和每个公开slot ID到授权Material ID的替换映射，可选Work/title，生成真实MediaProject初版 | 源发布必须当前可取用且适用许可允许改作/用途；替换媒体须匹配kind、流数和工程实际使用时长；精确幂等重放返回同一私有副本；撤回后不创建新副本 |
| `POST /api/media/projects/{id}/template-updates/preview`；`POST /api/media/projects/{id}/template-updates/apply` | preview带私有项目`base_version`和同一Publication lineage的更新release；返回`preview_hash/changes/conflicts/required_source_replacements`。apply带同hash、逐冲突`keep_current/take_template`、可选新slot映射及幂等键 | 更新release与Media源版本都须高于现有血缘；base/hash过期或有未解决冲突拒绝；CAS失败不写入；按稳定对象键三方合并并保留用户手改 |
| `POST /api/releases/{id}/shares`；`GET/PATCH /api/releases/{id}/shares/{operation_id}` | 按精确发布版与选择准备回链、授权文件或系统交接；已批准平台发送沿原Job。GET对账，PATCH仅回写一次客户端实际观测 | 不改内部作品/可见范围；回执幂等；原生交接不授平台确认；失权不重新签发 |
| `POST /api/releases/{id}/media/{member}/playback`；`GET/HEAD /api/community/media/streams/{token}` | 当前grant绑定view/download、release/member、当前访问世代和短期到期；同一Rendition支持经核的HLS VOD或Range文件，返回真实transport/quality/subtitles/chapters和delivery | 每个清单/init/分片/Range/字幕请求同样鉴权；撤回停新签发并拒旧世代，播放器按实际生命周期停网络和已缓冲播放；未部署仍local_review_only，不将既有Range接口当自适应播放已实现 |
| `POST /api/uploads`；`HEAD/PATCH /api/uploads/{id}`；complete/cancel | kind、目标空间、expected_size/hash、upload_id、offset、块摘要；成员来自服务端 | 持久偏移和同一上传身份；并发/摘要/长度冲突不推进；续传先读偏移；正式采用tus时按其全部适用协议实现，当前自有合同不冒tus兼容（规划合同；以代码核实为准，未实现前不冒已交付） |
| experiences创建/修订/提交 | problem、method、steps、conditions、pitfalls、results、evidence、所关联release与父版 | 带出处的可编辑经验和版本；作者陈述/实测/推断/未知分开，不自动入训练（规划合同；以代码核实为准，未实现前不冒已交付） |

`experience`内容的`experience`字段精确保留`problem/mechanism/steps/conditions/counterexamples/results/limitations/evidence/claim_type`；step至少一条，非`unknown`结果主张必须带evidence。响应明确`independent_validation=not_established_by_publication`及`training_eligibility=not_granted`。作品、模板、经验均使用同一私有Publication状态机/审核/release，不把作者报告、观察、推断和未知互相改写。

每个发布依赖以精确ID/version/SHA、included与单独view/download/adapt/commercial/redistribute/training/attribution/expiry记录权利；媒体真实流探测/章节/字幕边界复核，依赖必须与分发成员用途相符。公开视频API匿名响应把依赖ID映射为`dependency-N`，去除服务端路径、Work/Material ID、原稿输入快照和工程原manifest。Media-template公开graph仅含槽ID、需求约束、时间线结构、可编辑章节/字幕/效果和替换说明，不携带原始媒体字节/源ID；私有原manifest仅为准确审查保留，账号删除时擦除源信息并保留收件人的合法Media副本、许可快照和更新时间记录。

上传和媒体分发使用本文件的确定协议及同一Material/Release/Job；旧有界WAV或Range观察不构成新功能当前通过。实现后按ACCEPTANCE检查长媒体、多码率、解码、上传中断和撤回，历史范围限制不成为目标能力上限。自有上传不冒tus互通；HTTP Range按RFC 9110的206/416、实体校验及实际内容范围处理。

## 浏览承载、来源读取与监测的统一协议

浏览仍由 `browser.py / browser_api.py` 的同一 BrowserSession、权限、受保护状态和 Material 保存操作拥有。以下既有managed承载以Chrome独立进程/非持久context隔离用户，不能据此排除上文attached_extension/attached_protocol目标合同；borrowed连接不取得用户浏览器进程所有权，也不共享其整进程音频。Windows旧嵌入窗口接口保兼容记录；当前目标为该会话的实时页面帧参与同一物理空间GPU合成、输入经逆变换映回网页。Web通过WebRTC传输实际页面画面与输入并参与同场景合成；不对客户端暴露CDP端口，不为嵌入而移除目标站CSP或复制改写整页；获准的页面输入、表单编辑和操作仍在原目标发生，也不以截图轮询代替实时浏览。明确外开可交用户原浏览器；需实时读取或操作时必须通过已配对的附着通道，普通外开回执不冒连接成功。managed默认留在产品内，attached始终保用户选择的真实浏览器/目标及可见停止入口。

| 接口 | 输入与实际结果 |
|---|---|
| `POST /api/browser-sessions/{id}/host/offer` | 当前 `page_id`、CSS viewport 与完整 offer SDP；同一成功 envelope 返回 answer SDP、`host_id`。信令沿当前认证，实时通道持续重核会话/工作/客户权限；不配置外部 STUN/TURN 服务。 |
| `POST .../host/native`、`POST .../host/{host_id}/heartbeat`、`POST .../detach` | 既有managed本机宿主取得经同会话目标/PID核定的产品窗口；心跳返回当前页、标签和事件。仅owned窗口允许宿主管理其隐藏/尺寸/分离，继续恢复沿原BrowserSession；borrowed只分离连接，不隐藏、移动、缩放或关闭用户浏览器/标签。 |
| `GET/POST .../host/tabs` | 读取标签；新增、切换、关闭携带 `base_version` 和 `Idempotency-Key`，提交同一会话的保护状态。 |
| `POST .../host/{host_id}/uploads`、`POST .../uploads/cancel` | 只接受当前文件选择事件的 `chooser_id` 与用户选定文件字节；取消保原选择，不接受客户端裸路径。 |
| `GET .../host/{host_id}/downloads/{download_id}`、`POST .../save` | 以会话内下载对象取得实际文件，或通过同一 Material/Work 保存、解析、来源记录和幂等回执承接。部分/失效下载不当成完整文件；成功结果可重开。 |
| `GET /api/browser-history`、`POST /api/browser-history/clear`、`GET /api/browser-bookmarks` | 用户/工作区私有的安全地址与标题；历史最多 1000 项，清理记录不删除已保存 Material/Work，也不冒清除网站登录。 |
| `POST /api/browser-sessions/{id}/bookmark`、`DELETE /api/browser-bookmarks/{id}` | 服务端核当前页保存书签；同 URL 去重，删除核书签版本与归属。含临时授权参数的脱敏地址不能伪造可用的登录续接链接。 |

Web 的有序 `input` 通道绑定 `sequence / page_id / page_revision`；鼠标、滚轮、键盘、IME 文本和网页弹窗分别分型。陈旧页面和未知效果不自动重放。网页确认框可保持原点击等待，同时交付事件并接用户明确答复；不默认确认。复制读取当前网页/输入框选区，粘贴接用户本机明确提供的文字，不能读取服务器系统剪贴板。managed的Web声音来自已核隔离浏览进程的独立音频轨；borrowed只允许目标标签的获准音轨，不能沿用整进程捕获混入其他标签。能力缺失单独返回；画面不签音频成功。历史Native宿主服从页面可见性与`TickerMode`；当前空间功能面须按实际几何遮挡和焦点参与合成，不得让原生窗口/DOM视频压住其他物件。隐藏仅控制绘制，不能自动停止录音/监测或重建会话；实时帧与音频的失败分别呈现。

公开 URL 读取继续使用 `acquire_url` 和原共享 Worker。HTML 原始响应与主内容投影分开；RSS/Atom 保逐条来源、作者、原生时间及实际观察到的分页，`entries_truncated / links_truncated` 明示范围。`text_sha256` 是文本投影身份，不是原站原件证明。现有本机代理使用合格数值地址建立隧道；遇网络客户端的合成地址时只向当前系统配置的 DNS 重新核公网地址，仍禁止私网/保留地址和重绑定，不新增 DNS 服务或修改系统配置。HTTPS 沿系统信任链验证，不能关闭证书核验。

监测 `work_id` 与 `follow_source_links` 是不同输入，后者默认关闭。`GET /api/works/{id}/monitor-source-candidates` 仅提供该工作已有资料的可选来源、去重、排除原因与分页；用户选定的 URL 才进入规则。启用后续发现时，仅沿已选来源同站的获准链接/Feed 条目进入同一采集队列，保父资料、来源、规则版与发现时刻；跨站链接不自动扩大范围，预算和截断不冒全站覆盖。来源读取水位与规则评价水位分别持久化，旧规则迟到结果不能吞掉新规则下应复核的变化。变化证据区分新增、删除和改写，保确切前后文本；HTML 字节/空白变化不能单独冒内容变化。`Retry-After` 与实际失败原因沿原 Job 记录保存，下一次允许检查时间可读回；重开后先收取已完成结果再安排新检查，避免重复效果。

### 捕获资源、监听出口与桥消息

捕获消费者同时记录既有授权对应的scope_kind（source_bound或explicit_tab_session）及human_view/model_observation/source_save用途，字段只是原许可的投影，不产生新grant。源受限消费者还需真实frame_document_binding资格；缺该资格不以当前URL或active状态补成功。explicit_tab_session只在用户已经明确选定相应持续查看范围时成立，不能从扩展安装、一次取材或DOM许可推导；人类显示、模型观察与正式资料保存仍分别核权。

在既有ConnectionRef/BrowserSession下记录`capture_id/capture_generation`、设备/profile的非秘密引用、确切tab/window及document/navigation、当前SourceUse/授权世代、video/audio/semantics/control各自能力与状态；active流不代表新页面已获权。普通与隐私profile不得共用流或配对上下文。消费者订阅引用当前capture及用途，停止只释放该订阅，真实资源终态由Browser Owner回读；借用目标的浏览器进程/标签不随订阅清理关闭。

音频回执保源、`monitoring_sink=origin_device|oevira|none`、monitoring_generation、请求/实际开始/停止及错误；每一捕获源默认一个监听出口，切换失败/未知不得叠新播放。该字段与网页本身静音、录音、制作Job取消分开；未获声音捕获时保真实video/text能力，不补无声音轨冒支持。断开和导航后的资源状态可查询，重连不以旧客户端记录自签已恢复。

Native Messaging控制信封含协议版、会话/目标/授权世代、operation_id、kind、seq、payload字节数、deadline及回执；大合法语义块另含总长度、chunk身份和摘要，重组在总预算内，重复/漏块/旧世代明确处理。单帧产品预算256KiB（UTF-8 JSON总字节，不含4字节前缀），总预算按当前操作预先确定并可报告超限；媒体信令不夹连续帧，真实媒体走同一获准WebRTC通路。所有字段仅表达实际状态，客户端不能以自报grant或scope扩大产品权限；原始stream ID、配对密钥和设备私密路径不进入普通结果/日志/Material。

### 页面辅助语义的目标协议

沿上述同一Browser Owner扩充`GET .../host/{host_id}/semantics`与`POST .../host/{host_id}/semantic-actions`，不另建浏览、任务或权限服务。快照/增量含session/page/frame/navigation_generation/semantic_revision、授权世代、root/focused_node_ref、节点层级与coverage；节点只含不透明node_ref、角色/名称/安全值与状态、允许动作、当前局部界限，原DOM/CDP身份只由服务端持有。增量含base_semantic_revision、删除引用及new_revision；缺口须取新快照，跨页节点不复用。实际未遍历iframe、隐藏/忽略节点及资源截断分别声明，不从标题推完整可达性。

动作提交带上述页面与语义基线、node_ref、已注册动作、必要参数、独立operation_id及控制世代，沿原输入/IME/选区/文件/回执链校验。陈旧或已脱离节点返回STALE_TARGET并刷新合法树；不基于旧位置猜点，也不因重新定位自动更换目标。invoke/set_text可能产生外部效果，仍核该效果授权，未知提交不重放。密码内容仅走用户明确的受保护人工输入，不进入普通语义值、模型上下文、日志或持久材料。客户端读屏属于该用户辅助操作，不能把它当Agent在接管期的读取许可。SourceUse、成员权限或控制撤回使在途响应与缓存失效。实际接口支持/站点可达性与画面、声音各自返回资格，不以HTTP200或能Tab冒读屏全路径。

## 业务成果、委托和发布的实际核心协议

`business_execution`使用已有Work、Material、Artifact及其不可变版本，不增加第二套业务成果库。`POST /api/works/{id}/business-results`接收`operation`、业务`inputs`及可选`citations/subjects/relationships/tables/title`；每次写入带`Idempotency-Key`。服务端核当前成员和客户委托、资料权限、主体/关系版及文件完整性，冻结输入后在数据库写事务外计算，提交时重核要求与来源。结构化结果的每段有稳定`id/title/body/data`；正文表格和结构化数值同源，现有`/api/artifacts/{id}/exports`按同一格式集合提供Word(docx)、PPT(pptx)、XLSX、PDF、动态HTML及适用时的Markdown(md)。业务成果导出可传`audience:"client"`生成客户视图；缺章节客户正文、待复核客户正文或客户限制清单时拒绝导出，不回退内部正文。客户视图只冻结`client_body`、客户适用限制和逐项明确授权的`client_citations`，不携带内部账本、原始业务快照或私有引用；缺省`internal`保持原行为，幂等键按视图区分。

`GET /api/business/operations`列出当前可调用操作。经营、消费者、财务情景、财报事实/更正/季度选择、策略、媒介、合作、活动排期、实验/归因/增量、事件与服务回应各保自己的输入和结果结构。调用者不得把枚举本身当完整专业能力或要求用户手填算法JSON。保存的CSV/TSV/XLSX可以用`tables`指定材料、工作表、业务字段到源列的映射和已确认的逐行上下文；数值类型明确，缺格为null，全量非空行计数，不因列重排改口径。财报操作只从已授权Material读原字节/原文，成果保存来源hash、事实、位置及采用条件，不把原件字节塞进JSON。

`POST /api/works/{id}/business-jobs`将相同业务请求放入现有Job/Worker。操作返回202及持久Job；读取、取消、失败重试沿现`/api/jobs/{id}`。业务要求版、成员/客户/来源、租约世代及取消状态在提交前再次核对，已保存结果凭同键读回。取消、失租和失败不生成成功成果；数值计算不占数据库长写锁。

`POST /api/business-results/{id}/revisions`使用`base_version`、命名的`input_changes`或`section_edits`。来源更正沿同一修订路由，input_changes中使用source_changes数组，每项含op=append|replace|exclude、稳定input_id、原input_snapshot_revision、相应old_ref/new_ref的Source/Material/Observation确切身份和版本，以及已有受影响的MetricDefinition或约束引用；append无old_ref，exclude无new_ref，replace两者都有。服务端按当前工作和权利重核每项、旧引用与原冻结快照的一致性和纠正关系，重算实际依赖，返回变更的输入、数值/分母、章节、预算约束及影响集，分开locked_sections和可改部分。缺少可合法读的新引用时保旧版，标该部分待补或失权，历史获准可见性按现行授权，不能以null替新事实。快照/父版本冲突返回409和可见影响，同键异输入按既有幂等规则。只采用实际变化段；碰到已确认或人工改写的段返回`CONFIRMED_SECTION_AFFECTED`及逐段前后差异，用户明确选择`accept_sections`后才能替换。`confirm_sections`由用户确认，模型不能代签或借采纳参数覆盖人工稿。重放返回原提交的确切版本，不能把历史请求落进后来的用户版本。`/restore`恢复方案内容为新版本，保当前实际行动、费用、退款、服务记录与观察，相关旧批准不自动恢复；普通通用文本保存/恢复不能绕过业务结构。

`POST /api/business-results/{id}/commands`用`Idempotency-Key`接收`base_version`、命名`command`、对象型`payload`及可选精确引用/修订说明；事务内核当前成员、委托、版本与来源后追加同一Artifact的新版本。通用动作含记录选择、计划行动、内部行动决定/撤回、人工记录执行、费用及更正/退款、结果观察、服务履约事件。行动目标、负责人、交付物、前置项与结果依赖保持独立；人工记录明确不等平台回执或产品实际发出。效果未知必须指向原待核记录查回执，不自动重发。费用分原币种、估计/承诺/实支/退款；退款必须指原实支，向下更正实支也重核累计退款。服务回应、承诺、履约、关闭和重开分记录，履约引用真实已存承诺。

`POST /api/business-results/{id}/actions/{action_id}/execute`的`base_version`绑定保存该行动的业务Artifact当前版；`execution.version`独立绑定行动指定导出的源Artifact精确保存版。保存行动会递增行动Artifact版本，因此源版本可以早于计划版本，消费者应按该精确源版读取并复核，不得把“当前行动版不同于文件目标版”判为目标变化。文件Job完成时只代表精确导出字节已生成并有可下载回执，不代表已发送客户。`artifact_export`记录状态`executed`/回执`file_generated`；浏览器已确认输入则是`awaiting_outcome_verification`/`browser_input_delivered`，对方发布、联系或付款仍需独立结果核对；`execution_unknown`不允许盲目重发。文件受众随行动精确保存为`internal`或`client`，失败不降级到内部视图。

`collaboration_plan`还使用同一路由追加完整协作生命周期，动作名为`adopt_collaboration_candidate`、`decline_collaboration_candidate`、`record_collaboration_terms`、`confirm_collaboration_agreement`、`submit_collaboration_deliverable`、`record_collaboration_feedback`、`request_collaboration_revision`、`accept_collaboration_deliverable`、`reject_collaboration_deliverable`、`cancel_collaboration`及`record_collaboration_cost`。`payload`身份引用解析自当前保存的协作候选；候选项目须为当前Work。协议来源与交付来源只能选择同一Work可访问的精确Material SHA-256或Artifact版本。协议确认需精确的当前`creator_collaboration_agreement`批准、完整报价/权益/交付/档期/取消条件和已明确为本协议金额的报价状态；quote或estimate不会由审批自动转成承诺。每次交付提交冻结其`agreement_id`和精确来源；协议变化后旧交付不得按新协议接收，必须在当前协议下提交新版本。已审批接收决定不可改写为返修或拒收；同协议已接收的交付不得重复提交。逐项反馈可关联锚定该提交Artifact版本的原评论，或明确记录为用户输入；返修请求必须指向该提交的反馈。拒收、取消、实支与退款都是保留依据的用户记录，不等外部通知、客户回执、付款或退款成功；只有当前已连接且已授权的原执行渠道才可能产生外部回执。

客户私域由`/api/business/commissions`及其成员、工作绑定维护。委托版本与当前成员角色决定读写；绑定在加入私有内容前完成，不把已含资料的工作静默移入另一客户。主体客观身份仍沿原Business目录；引用客户来源须核目标委托。`/api/artifacts/{id}/comments`锚定确切版本的Unicode原文范围，解决意见须指向实际改版或保不采用理由；`/approvals`绑定精确版本、用途、范围及批准人，真实效果前重核当前版本与批准人资格。

发布API使用同Store的PublicationDraft/Release及审核记录。创建/改版保存用户明确选定的公开内容，私有输入快照不随包复制；提交、审核、明确发布、撤回互不替代。审核角色和官方身份只能由服务端角色授予，审核者只能取已提交选定内容。`/api/discovery`仅列当前公开且许可有效的Release，`/api/releases/{id}`按private/team/unlisted/public及许可重新鉴权。没有配置正式托管时返回`local_review_only`，不能称全网已发布。文稿模板`instantiate`创建新私有Artifact和对象映射，保原许可、取得时点、替换参数与缺依赖；不覆盖原作者或已有改稿。公开经验始终区分作者记录、观察、推断和未知，发布、收藏与复用均不授训练资格。
