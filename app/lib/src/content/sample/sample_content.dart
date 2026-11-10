import '../content_source.dart';
import '../models.dart';

/// Illustrative workspace used while the visual client is built apart from
/// the backend. Every name, number and quotation here is invented; the shell
/// marks the workspace as 示例内容 whenever this source is active.
class SampleContentSource implements ContentSource {
  const SampleContentSource();

  @override
  bool get isSample => true;

  @override
  String get workspaceName => '示例工作区';

  @override
  List<Work> get works => _works;
  @override
  List<SourceMaterial> get materials => _materials;
  @override
  List<Artifact> get artifacts => _artifacts;
  @override
  List<Monitor> get monitors => _monitors;
  @override
  List<Subject> get subjects => _subjects;
  @override
  List<Client> get clients => _clients;
  @override
  List<Release> get releases => _releases;
  @override
  List<BrowserPage> get browserPages => _pages;

  @override
  MediaProject? mediaProject(String artifactId) => artifactId == 'a-video' ? _video : (artifactId == 'a-podcast' ? _podcast : null);

  @override
  List<ChatMessage> conversation(String destinationKey) => _conversations[destinationKey] ?? const [];
}

const _works = <Work>[
  Work(
    id: 'w-research',
    title: '城市咖啡店的“第三空间”研究',
    goal: '弄清常客把咖啡店当作第三空间的真实条件，形成一篇可引用的研究文章，并给出门店改造可检验的判断。',
    state: WorkState.inProgress,
    updated: '今天 16:40',
    nextStep: '核对访谈第 7 位受访者的原话，再决定“插座数量”这条判断是否保留',
    materialIds: ['m-interview', 'm-traffic', 'm-notes', 'm-flow', 'm-comments', 'm-podcast-tx'],
    artifactIds: ['a-article'],
    conditions: [
      Condition('研究问题', '常客停留超过 90 分钟的条件是什么'),
      Condition('范围', '一线城市社区店与商场店，不含机场与写字楼店'),
      Condition('交付', '4000 字研究文章 + 一页可检验判断'),
      Condition('引用口径', '只引用原话与原表，不使用二手转述'),
    ],
    timeline: [
      TimelineEntry('今天 16:40', '在论证稿第 3 节加入 2 段访谈原话'),
      TimelineEntry('今天 14:05', '导入《工作日时段客流表》并完成解析', actor: '系统'),
      TimelineEntry('昨天 21:12', '把“安静程度”列为待核判断'),
    ],
  ),
  Work(
    id: 'w-recap',
    title: '滨河里店春季客流复盘',
    goal: '解释三月工作日午后客流下降的原因，给出四月可执行的调整并约定复盘口径。',
    state: WorkState.review,
    updated: '今天 11:20',
    nextStep: '客户在第 4 页留下 2 条意见，等待你确认改动范围',
    clientId: 'c-chengan',
    subjectIds: ['s-brand', 's-store', 's-venue'],
    materialIds: ['m-traffic', 'm-flow'],
    artifactIds: ['a-recap-sheet', 'a-recap-pdf', 'a-dashboard'],
    conditions: [
      Condition('期间', '2026-03-01 至 2026-03-31'),
      Condition('口径', '客流 = 收银小票数，不含外卖'),
      Condition('比较对象', '同商场另两家饮品店（公开观察）', confirmed: false),
    ],
    timeline: [
      TimelineEntry('今天 11:20', '客户留下 2 条意见：第 4 页、第 6 页', actor: '客户 · 林女士'),
      TimelineEntry('昨天 18:00', '交付复盘 PDF 第 2 版'),
      TimelineEntry('3 天前', '确认客流口径为收银小票数'),
    ],
  ),
  Work(
    id: 'w-launch',
    title: '北岬户外“轻山”系列上市传播方案',
    goal: '为四月上市的轻量徒步系列做整合传播方案，含达人合作、首发活动和预算分配。',
    state: WorkState.waitingInput,
    updated: '昨天 19:48',
    nextStep: '补充：首发活动是否必须在门店举办',
    clientId: 'c-beijia',
    subjectIds: ['s-beijia'],
    materialIds: ['m-comments'],
    artifactIds: ['a-launch-ppt'],
    conditions: [
      Condition('预算', '总额 80–95 万元，含达人费用'),
      Condition('上市日', '2026-04-18'),
      Condition('首发活动地点', '未确定', confirmed: false),
    ],
    timeline: [
      TimelineEntry('昨天 19:48', '方案第 3 版：达人组合从 12 位调整为 9 位'),
      TimelineEntry('2 天前', '客户确认预算区间', actor: '客户 · 周先生'),
    ],
  ),
  Work(
    id: 'w-film',
    title: '品牌短片《清晨六点》',
    goal: '为澄岸咖啡剪一支 90 秒品牌短片，横版主片 + 竖版 30 秒切片。',
    state: WorkState.inProgress,
    updated: '今天 09:15',
    nextStep: '替换第 4 镜（开门）为更稳的一条，并对齐鼓点',
    clientId: 'c-chengan',
    artifactIds: ['a-video'],
  ),
  Work(
    id: 'w-podcast',
    title: '播客《开店这件事》第 12 期',
    goal: '把 74 分钟的对谈剪成 42 分钟成片，含章节与文字稿。',
    state: WorkState.paused,
    updated: '3 天前',
    nextStep: '继续从 38:20 开始粗剪',
    artifactIds: ['a-podcast'],
    materialIds: ['m-podcast-tx'],
  ),
];

const _materials = <SourceMaterial>[
  SourceMaterial(
    id: 'm-interview',
    title: '常客访谈记录（12 位）',
    kind: MaterialKind.interview,
    workId: 'w-research',
    tags: ['原话', '社区店'],
    summary: '12 位每周到店 3 次以上的常客，半结构化访谈，平均 38 分钟。',
    provenance: Provenance(
      version: '第 2 版 · 补录第 7、11 位',
      source: '本工作区上传 · 访谈记录.docx（示例）',
      capturedAt: '2026-03-22 20:14',
      method: 'Word 原件解析，按受访者分段；原话与访谈者提问分开标注',
      license: '自有调研资料，仅限本研究使用',
      fingerprint: 'sha256 · 4be1…9c07',
      limitations: '第 3 位受访者录音缺 6 分钟，文字为当场笔记',
    ),
    pages: [
      Segment('访谈说明', '访谈时间为 2026 年 3 月 14 日至 21 日，地点均为受访者常去的门店。每位受访者先描述最近一次停留，再回答固定的 8 个问题。', heading: true),
      Segment('受访者 2 · 自由职业', '“我不是来喝咖啡的，我是来找一个不会被打扰的桌子。店员记得我要靠窗的那张，比任何会员卡都有用。”'),
      Segment('受访者 4 · 研究生', '“下午两点到四点最好，那时候人不多但也不空。完全空的店我反而待不住，会觉得自己在被看。”'),
      Segment('受访者 7 · 设计师', '“插座多不多其实没那么重要，我会带充电宝。重要的是旁边的人在做什么，大家都在干活的时候我就能干活。”'),
      Segment('受访者 9 · 退休教师', '“我早上来，看报，跟熟面孔点个头。要是换了一批店员，我大概会换一家店。”'),
      Segment('受访者 11 · 运营', '“音乐声音一大我就戴耳机，戴了耳机其实在哪都一样，那我就不一定来了。”'),
      Segment('访谈者备注', '“被看见但不被打扰”在 12 位中有 9 位以不同说法提到；“插座”只有 3 位主动提到，且都不是决定性条件。'),
    ],
  ),
  SourceMaterial(
    id: 'm-traffic',
    title: '工作日时段客流表 · 2026 年 3 月',
    kind: MaterialKind.table,
    workId: 'w-recap',
    tags: ['收银口径', '滨河里店'],
    summary: '按小时汇总的收银小票数，22 个工作日。',
    provenance: Provenance(
      version: '第 1 版',
      source: '客户提供 · 收银系统导出.xlsx（示例）',
      capturedAt: '2026-04-02 09:30',
      method: '工作簿原件解析，保留原单元格坐标；空值保留为缺测',
      license: '客户资料，仅限澄岸咖啡委托工作',
      fingerprint: 'sha256 · a91d…02fe',
      limitations: '3 月 9 日收银系统停机 2 小时，该日 10–12 时为缺测，不按 0 计',
    ),
    pages: [
      Segment('表头', '日期 · 时段 · 小票数 · 客单价 · 堂食占比', heading: true),
      Segment('08–10 时', '日均 61 单（2 月为 58 单），堂食占比 34%'),
      Segment('10–12 时', '日均 44 单（2 月为 47 单），3 月 9 日缺测'),
      Segment('12–14 时', '日均 73 单（2 月为 75 单），堂食占比 52%'),
      Segment('14–17 时', '日均 39 单（2 月为 53 单），堂食占比 71%'),
      Segment('17–20 时', '日均 48 单（2 月为 46 单）'),
      Segment('备注', '14–17 时下降集中在 3 月 11 日之后；同期商场 B1 扶梯改造，入口改道。'),
    ],
  ),
  SourceMaterial(
    id: 'm-notes',
    title: '“第三空间”概念阅读笔记',
    kind: MaterialKind.note,
    workId: 'w-research',
    tags: ['概念', '待核'],
    summary: '对第三空间相关讨论的个人整理，含 4 条待找原文的说法。',
    provenance: Provenance(
      version: '第 5 版',
      source: '在寰意中写的笔记',
      capturedAt: '2026-03-18 至今',
      method: '手写笔记，未经来源核对的说法已标为待核',
      license: '自有',
      fingerprint: 'sha256 · 77c0…e1aa',
    ),
    pages: [
      Segment('定义', '家（第一空间）与工作场所（第二空间）之外，人们自愿、经常、非正式聚在一起的场所。', heading: true),
      Segment('条件 1', '中立地带：任何人可以来去，不需要邀请。'),
      Segment('条件 2', '交谈是主要活动，但不是唯一活动；安静的共处也算。'),
      Segment('待核', '“常客构成场所性格”这一说法需要找到原书页码，目前只有二手转述。'),
      Segment('我的疑问', '访谈里多数人并不交谈。这是第三空间的变体，还是另一种东西？'),
    ],
  ),
  SourceMaterial(
    id: 'm-flow',
    title: '门店动线观察记录',
    kind: MaterialKind.report,
    workId: 'w-recap',
    tags: ['现场', '照片 14 张'],
    summary: '3 月 25 日 13:00–17:00 的现场观察，含座位占用与动线草图。',
    provenance: Provenance(
      version: '第 1 版',
      source: '现场记录 · PDF 与照片（示例）',
      capturedAt: '2026-03-25 17:40',
      method: 'PDF 原件按页解析；照片保留原图与拍摄时间',
      license: '自有现场记录；照片含顾客背影，不得公开',
      fingerprint: 'sha256 · 0c3b…5d18',
    ),
    pages: [
      Segment('观察范围', '滨河里店 B1 层，46 个座位，其中靠窗 12 个、长桌 10 个。', heading: true),
      Segment('14:00', '占用 31/46，长桌 9/10，靠窗 12/12。'),
      Segment('15:30', '占用 22/46，扶梯改道后从北口进入的顾客明显减少。'),
      Segment('动线', '原南口扶梯封闭后，顾客需绕行约 90 米，经过两家饮品店。'),
      Segment('待确认', '扶梯恢复时间：商场公告写“四月中旬”，未给具体日期。'),
    ],
  ),
  SourceMaterial(
    id: 'm-comments',
    title: '社交平台评论摘录 · 轻量徒步',
    kind: MaterialKind.webpage,
    workId: 'w-launch',
    tags: ['公开页面', '已取材 36 条'],
    summary: '从公开帖子取材的 36 条评论，保留原链接与取得时间。',
    provenance: Provenance(
      version: '第 3 版 · 新增 8 条',
      source: '寰意浏览器取材 · 公开帖子（示例链接）',
      capturedAt: '2026-04-08 22:03',
      method: '受控浏览会话中选段保存，保留原文、作者显示名与时间',
      license: '公开内容，仅用于分析，不得转载原文',
      fingerprint: 'sha256 · 5f62…ab90',
      limitations: '平台折叠评论未展开，覆盖不完整',
    ),
    pages: [
      Segment('取材说明', '关键词“轻量徒步 鞋 推荐”，按时间排序取前 3 页。', heading: true),
      Segment('评论 4', '“城市里走走够了，真上山还是要换鞋，别把轻量当万能。”'),
      Segment('评论 11', '“颜色好看比参数重要，反正我周末就去郊野公园。”'),
      Segment('评论 19', '“去年买的那双三个月就开胶了，今年先观望。”'),
      Segment('评论 27', '“有没有人试过下雨天？防滑才是我最关心的。”'),
    ],
  ),
  SourceMaterial(
    id: 'm-podcast-tx',
    title: '对谈转写稿 · 第 12 期原始录音',
    kind: MaterialKind.transcript,
    workId: 'w-podcast',
    tags: ['74 分钟', '2 位发言人'],
    summary: '自动转写后人工校对至 38:20，其后未校对。',
    provenance: Provenance(
      version: '第 2 版 · 校对至 38:20',
      source: '录音工程 · take 3（示例）',
      capturedAt: '2026-04-05 15:00',
      method: '本地语音转写，按发言人分段；不确定词已标出',
      license: '嘉宾已签录音授权，仅限本节目',
      fingerprint: 'sha256 · d4e8…3317',
    ),
    pages: [
      Segment('00:00', '主持：今天想聊开店第一年最容易被低估的事。', heading: true),
      Segment('03:12', '嘉宾：我以为最难的是选址，后来发现最难的是排班。人留不住，常客也留不住。'),
      Segment('17:45', '嘉宾：常客记得的是人，不是装修。'),
      Segment('38:20', '（以下未校对）'),
    ],
  ),
];

const _artifacts = <Artifact>[
  Artifact(
    id: 'a-article',
    title: '第三空间不是座位数',
    kind: ArtifactKind.article,
    version: 4,
    updated: '今天 16:40',
    status: '草稿 · 已保存在本机',
    workId: 'w-research',
    outline: ['一、问题：为什么常客会久坐', '二、被看见但不被打扰', '三、插座与 Wi‑Fi 是条件还是借口', '四、店员是场所的一部分', '五、可检验的三条判断'],
    sections: [
      '12 位常客中有 9 位每周到店 3 次以上，单次停留中位数 96 分钟。我们要回答的不是“座位够不够”，而是什么让他们愿意留下。',
      '7 位受访者提到“有人但没人管我”。靠窗的长桌比独立小桌更受欢迎，原因是可以看到街面，同时不必与人对视。',
      '主动提到插座的只有 3 位，其中受访者 7 明确说“插座多不多其实没那么重要”。插座更像是离开的理由，而不是留下的理由。',
      '“店员记得我要不要糖”出现 4 次。认得人、不打扰，是常客描述中最稳定的一条。',
      '判断一：午后靠窗长桌的平均停留长于小桌。判断二：增加插座不会显著延长停留。判断三：固定班次店员与常客复访相关。三条均需用下一季客流表检验。',
    ],
  ),
  Artifact(
    id: 'a-recap-sheet',
    title: '滨河里店客流复盘表',
    kind: ArtifactKind.sheet,
    version: 2,
    updated: '昨天 18:00',
    status: '已交付第 2 版',
    workId: 'w-recap',
    outline: ['时段对比', '堂食占比', '缺测说明', '四月调整与复盘口径'],
  ),
  Artifact(
    id: 'a-recap-pdf',
    title: '春季客流复盘报告',
    kind: ArtifactKind.pdf,
    version: 2,
    updated: '昨天 18:00',
    status: '客户审阅中',
    workId: 'w-recap',
    outline: ['摘要', '三月发生了什么', '午后下降的两个原因', '四月可执行调整', '复盘口径'],
    sections: [
      '三月滨河里店 14–17 时日均小票由 53 降至 39，下降集中在 3 月 11 日南口扶梯封闭之后；早餐与晚间时段基本持平。',
      '3 月 11 日起 B1 南口扶梯施工，北口进店需绕行约 90 米并经过两家饮品店。第 10 周收银系统升级，当周数据缺测，未做插值。',
      '一是入口可达性下降；二是午后堂食占比高（71%），久坐客对绕行更敏感。外卖小票同期未见同等幅度下降。',
      '北口增设两处导视；午后套餐延长至 17:30，早餐价格不变；扶梯恢复后两周按同口径复盘。',
      '口径为收银小票数，按自然周汇总；堂食与外卖分列；缺测周保留空缺并注明原因。',
    ],
    comments: [
      ReviewComment('林女士', '第 4 页 · 调整建议 2', '延长午后套餐可以，但不要动早餐时段的价格。'),
      ReviewComment('林女士', '第 6 页 · 复盘口径', '外卖也请单列一行，老板会问。'),
      ReviewComment('我', '第 2 页 · 摘要', '已补充缺测日说明。', resolved: true),
    ],
  ),
  Artifact(
    id: 'a-dashboard',
    title: '四月客流看板',
    kind: ArtifactKind.html,
    version: 1,
    updated: '2 天前',
    status: '可筛选 · 按日更新',
    workId: 'w-recap',
    outline: ['按时段筛选', '与三月对比', '扶梯恢复前后'],
    data: {
      '14–17 时': [52, 54, 53, 55, 51, 49, 41, 38, 37, null, 39, 40],
      '全天': [198, 203, 201, 206, 199, 194, 181, 176, 174, null, 177, 179],
      '堂食': [37, 39, 38, 40, 36, 34, 27, 25, 24, null, 26, 27],
      '外卖': [15, 15, 15, 15, 15, 15, 14, 13, 13, null, 13, 13],
    },
  ),
  Artifact(
    id: 'a-launch-ppt',
    title: '轻山系列上市方案',
    kind: ArtifactKind.ppt,
    version: 3,
    updated: '昨天 19:48',
    status: '待补充首发地点',
    workId: 'w-launch',
    outline: ['市场观察', '人群与场景', '传播主线', '达人组合', '首发活动', '预算分配', '排期'],
    sections: [
      '轻量徒步讨论在 3 月增长明显；“下雨天防滑”是新品预告帖下最集中的提问。',
      '城市近郊半日徒步人群；周末出行，重视轻便与通勤两用。',
      '“城市到山脚，一双就够”——先回应防滑疑问，再讲轻量。',
      '3 位中腰部户外博主 + 6 位城市生活博主；湿滑石阶实测内容优先。',
      '首发地点待补充：客户倾向杭州近郊步道入口，需确认场地许可。',
      '达人 48% · 内容制作 22% · 首发活动 20% · 机动 10%（客户希望达人压到 45% 以内）。',
      '4 月 10 日方案定稿 · 4 月 14 日内容上线 · 4 月 18 日上市。',
    ],
    comments: [ReviewComment('周先生', '第 6 页 · 预算分配', '达人费用占比能否压到 45% 以内？')],
  ),
  Artifact(
    id: 'a-video',
    title: '清晨六点 · 粗剪',
    kind: ArtifactKind.video,
    version: 7,
    updated: '今天 09:15',
    status: '粗剪 · 1:32',
    workId: 'w-film',
    outline: ['开门', '磨豆', '第一位客人', '窗边', '片尾'],
  ),
  Artifact(
    id: 'a-podcast',
    title: '开店这件事 · 第 12 期',
    kind: ArtifactKind.podcast,
    version: 2,
    updated: '3 天前',
    status: '粗剪至 38:20',
    workId: 'w-podcast',
    outline: ['开场', '选址', '排班', '常客', '结尾'],
  ),
];

const _monitors = <Monitor>[
  Monitor(
    id: 'mon-beijia',
    title: '轻山系列口碑',
    scope: '北岬户外 · 新品 · 公开平台',
    state: MonitorState.watching,
    workId: 'w-launch',
    keywords: ['轻山', '北岬 徒步鞋', '轻量徒步 防滑'],
    checks: [
      SourceCheck('社交平台 A', '10 分钟前', '新增 6 条'),
      SourceCheck('社交平台 B', '12 分钟前', '无变化'),
      SourceCheck('电商问答', '1 小时前', '登录已过期，暂停取材'),
    ],
    events: [
      MonitorEvent(time: '16:52', source: '社交平台 A', title: '下雨天防滑的提问集中出现', excerpt: '“有没有人试过下雨天？”同类提问 2 小时内 5 条，均在新品预告帖下。', stance: Stance.question, replies: 14),
      MonitorEvent(time: '15:20', source: '社交平台 A', title: '配色获得自发讨论', excerpt: '“雾蓝那双好看”被 3 位用户在不同帖子中提到。', stance: Stance.positive, replies: 6),
      MonitorEvent(time: '昨天 22:10', source: '社交平台 B', title: '去年款开胶的旧帖被翻出', excerpt: '一篇去年 9 月的开胶帖新增 11 条回复，其中 4 条提到“今年先观望”。', stance: Stance.negative, replies: 11),
      MonitorEvent(time: '昨天 18:40', source: '社交平台 B', title: '一位户外博主询问合作', excerpt: '博主在评论区询问是否有试穿计划，已转入合作排期待办。', stance: Stance.neutral, handled: true, replies: 2),
    ],
  ),
  Monitor(
    id: 'mon-chengan',
    title: '滨河里店服务评价',
    scope: '澄岸咖啡 · 滨河里店 · 点评与社交平台',
    state: MonitorState.checking,
    workId: 'w-recap',
    keywords: ['澄岸 滨河里', '滨河里 咖啡 安静'],
    checks: [SourceCheck('点评平台', '正在检查', '—'), SourceCheck('社交平台 A', '35 分钟前', '无变化')],
    events: [
      MonitorEvent(time: '昨天 14:30', source: '点评平台', title: '“找不到入口”出现 3 次', excerpt: '扶梯封闭后，3 条评价提到从北口找不到店。', stance: Stance.negative, replies: 0),
    ],
  ),
  Monitor(
    id: 'mon-topic',
    title: '“城市第三空间”话题',
    scope: '独立研究 · 公开讨论',
    state: MonitorState.paused,
    workId: 'w-research',
    keywords: ['第三空间 咖啡', '久坐 咖啡店'],
    checks: [SourceCheck('社交平台 A', '4 天前', '已暂停')],
    events: [],
  ),
];

const _subjects = <Subject>[
  Subject(
    id: 's-brand',
    name: '澄岸咖啡',
    kind: SubjectKind.brand,
    city: '上海',
    facts: [
      Fact('门店数', '14 家（客户提供，2026-03）', '委托资料'),
      Fact('主力客群', '社区常客与周边办公人群', '访谈与客户口述'),
      Fact('价格带', '美式 22–26 元', '门店菜单照片'),
      Fact('会员规模', '未提供', '—', pending: true),
    ],
    relations: [
      Relation('品牌所有方', 's-org', '2019 年至今', '营业执照（客户提供）'),
      Relation('在营门店', 's-store', '2023-06 至今', '门店租约摘要'),
    ],
    insight: Insight(
      question: '滨河里店三月午后客流为什么下降？',
      change: '14–17 时日均小票从 53 降到 39，下降集中在 3 月 11 日之后',
      series: [52, 54, 53, 55, 51, 49, 41, 38, 37, null, 39, 40],
      seriesLabel: '14–17 时日均小票 · 按周（第 10 周缺测）',
      reasons: ['南口扶梯封闭，北口进店需绕行约 90 米，并经过两家饮品店', '午后堂食占比高（71%），绕行对久坐客影响更大'],
      evidence: ['工作日时段客流表 · 14–17 时', '门店动线观察记录 · 15:30', '点评平台 3 条“找不到入口”'],
      actions: ['北口导视两处（扶梯恢复前）', '午后套餐延长至 17:30，不动早餐价格', '扶梯恢复后按同口径复盘 2 周'],
    ),
  ),
  Subject(
    id: 's-org',
    name: '澄岸餐饮管理有限公司',
    kind: SubjectKind.organization,
    city: '上海',
    facts: [Fact('统一社会信用代码', '待核', '—', pending: true), Fact('成立', '2019 年', '营业执照（客户提供）')],
    relations: [Relation('拥有品牌', 's-brand', '2019 年至今', '营业执照（客户提供）')],
  ),
  Subject(
    id: 's-venue',
    name: '滨河里购物中心',
    kind: SubjectKind.venue,
    city: '上海',
    facts: [Fact('楼层', 'B1–L5', '商场公告'), Fact('改造', 'B1 南口扶梯，预计四月中旬恢复', '商场公告 · 未给具体日期', pending: true)],
    relations: [Relation('场内门店', 's-store', '2023-06 至今', '租约摘要')],
  ),
  Subject(
    id: 's-store',
    name: '澄岸咖啡 · 滨河里店',
    kind: SubjectKind.store,
    city: '上海',
    facts: [Fact('座位', '46 个', '现场观察'), Fact('营业时间', '07:30–21:00', '门店公示')],
    relations: [Relation('所属品牌', 's-brand', '2023-06 至今', '租约摘要'), Relation('所在场所', 's-venue', '2023-06 至今', '租约摘要')],
  ),
  Subject(
    id: 's-beijia',
    name: '北岬户外',
    kind: SubjectKind.brand,
    city: '杭州',
    facts: [Fact('品类', '徒步鞋服', '客户提供'), Fact('新品', '轻山系列，4 月 18 日上市', '委托书')],
  ),
];

const _clients = <Client>[
  Client(
    id: 'c-chengan',
    name: '澄岸咖啡',
    contact: '林女士 · 市场负责人',
    boundary: '仅本客户可见：收银数据、现场照片、复盘报告',
    commissions: [
      Commission('滨河里店春季复盘', '客流复盘、四月调整、复盘口径', '4 月 12 日', '客户审阅', workIds: ['w-recap']),
      Commission('品牌短片《清晨六点》', '90 秒横版 + 30 秒竖版', '4 月 20 日', '制作中', workIds: ['w-film']),
    ],
  ),
  Client(
    id: 'c-beijia',
    name: '北岬户外',
    contact: '周先生 · 品牌经理',
    boundary: '仅本客户可见：预算、达人报价、上市排期',
    commissions: [Commission('轻山系列上市传播', '整合传播方案、达人合作、首发活动', '4 月 10 日', '待补充', workIds: ['w-launch'])],
  ),
];

const _releases = <Release>[
  Release(
    id: 'r-1',
    title: '一家社区咖啡店的 365 个早晨',
    author: '示例作者 · 阿青',
    kind: ReleaseKind.video,
    published: '2 小时前',
    duration: '12:48',
    aspect: 16 / 9,
    summary: '横屏纪录短片，记录店主一年里每天开门前的十分钟。',
    license: '仅寰意内观看',
  ),
  Release(
    id: 'r-2',
    title: '门店复盘怎么写才不变成指标墙',
    author: '示例作者 · 周末研究所',
    kind: ReleaseKind.experience,
    published: '昨天',
    summary: '问题 → 关键变化 → 原因 → 证据 → 行动，附 3 个反例。',
    license: '可收藏，不可转载',
    body: ['先写清楚你要回答的那个问题。', '只放能解释变化的数字，其余进附录。', '每个原因都要能指回原表或原话。'],
  ),
  Release(
    id: 'r-3',
    title: '雨天徒步，鞋底纹路的 9 张近照',
    author: '示例作者 · 山野札记',
    kind: ReleaseKind.gallery,
    published: '昨天',
    images: 9,
    aspect: 4 / 5,
    summary: '同一条湿滑石阶上，九种鞋底的抓地对比。',
  ),
  Release(
    id: 'r-4',
    title: '开店第一年：排班比选址难',
    author: '示例作者 · 开店这件事',
    kind: ReleaseKind.podcast,
    published: '3 天前',
    duration: '41:52',
    summary: '两位店主聊人员流动如何影响常客。含文字稿与章节。',
  ),
  Release(
    id: 'r-5',
    title: '品牌短片分镜模板 · 90 秒',
    author: '寰意官方 · 示例',
    kind: ReleaseKind.template,
    published: '上周',
    summary: '5 段结构、镜头时长建议与替换槽，使用后生成你自己的私有工程。',
    license: '可用于创建私有作品',
  ),
  Release(
    id: 'r-6',
    title: '为什么我不再用“第三空间”这个词',
    author: '示例作者 · 城市观察',
    kind: ReleaseKind.article,
    published: '上周',
    summary: '一篇关于概念被滥用的短文。',
    body: ['概念好用的时候，最容易让人停止观察。', '我在三个城市的咖啡店坐了二十个下午，发现大多数人并不交谈。'],
  ),
];

const _pages = <BrowserPage>[
  BrowserPage(
    title: '滨河里购物中心 · 改造公告（示例页面）',
    address: 'example.invalid/binheli/notice',
    sections: [
      Segment('公告', 'B1 南口扶梯改造施工，预计四月中旬恢复通行。施工期间请由北口进入。', heading: true),
      Segment('施工时间', '3 月 11 日起，每日 22:00 后进行高噪音作业，营业时间内仅做围挡内安装。'),
      Segment('影响范围', 'B1 层南侧商户，请留意导视。南口临时通道仅供消防使用。'),
      Segment('绕行路线', '由北口进入后沿中庭向南约 90 米，经过 B1-08、B1-09 两家饮品店后到达南侧区域。'),
      Segment('联系', '物业服务台 · 示例电话 000-0000-0000'),
    ],
  ),
  BrowserPage(
    title: '轻量徒步鞋讨论帖（示例页面）',
    address: 'example.invalid/forum/light-hiking',
    sections: [
      Segment('主帖', '大家觉得轻量徒步鞋适合真正的山路吗？周末想去走一段石阶，怕下雨打滑。', heading: true),
      Segment('回复 1', '城市够了，上山还是换鞋。去年那款下雨天在石阶上很滑。'),
      Segment('回复 2', '看鞋底纹路。浅纹路的轻量鞋在湿石头上确实一般，干燥土路没问题。'),
      Segment('回复 3', '有没有人试过新出的那个雾蓝配色？想知道防水做得怎么样。'),
      Segment('回复 4', '我觉得轻量的意义就是短途，别指望它干重装的活。'),
    ],
  ),
];

const _video = MediaProject(
  artifactId: 'a-video',
  duration: 92,
  fps: 25,
  markers: ['00:18 鼓点', '00:41 转场', '01:10 片尾字幕'],
  tracks: [
    MediaTrack('V2 · 字幕', 'title', [MediaClip('澄岸咖啡', 2, 8), MediaClip('清晨六点', 70, 84)]),
    MediaTrack('V1 · 画面', 'video', [MediaClip('开门', 0, 14), MediaClip('磨豆', 14, 31), MediaClip('第一位客人', 31, 52), MediaClip('窗边', 52, 74), MediaClip('片尾', 74, 92)]),
    MediaTrack('A1 · 环境声', 'audio', [MediaClip('街道', 0, 20), MediaClip('店内', 20, 92)]),
    MediaTrack('A2 · 音乐', 'music', [MediaClip('钢琴 · 主题', 0, 92)]),
  ],
);

const _podcast = MediaProject(
  artifactId: 'a-podcast',
  duration: 2520,
  fps: 0,
  markers: ['00:00 开场', '03:12 选址', '17:45 常客', '38:20 未校对'],
  tracks: [
    MediaTrack('主持', 'voice', [MediaClip('take 3', 0, 1300), MediaClip('take 3', 1500, 2520)]),
    MediaTrack('嘉宾', 'voice', [MediaClip('take 3', 180, 2400)]),
    MediaTrack('音乐', 'music', [MediaClip('片头', 0, 25), MediaClip('片尾', 2480, 2520)]),
  ],
);

const _conversations = <String, List<ChatMessage>>{
  'workbench': [
    ChatMessage(ChatRole.user, '帮我把滨河里复盘里客户的两条意见先整理出来，看看要改哪几页。', refs: [ObjectRef(ObjectKind.artifact, 'a-recap-pdf')]),
    ChatMessage(ChatRole.assistant, '两条意见分别落在第 4 页“调整建议 2”和第 6 页“复盘口径”。第 4 页只需把“延长午后套餐”与早餐价格分开写；第 6 页要新增外卖单列一行，口径仍为收银小票数。改动不影响其余页面。要我按这个范围生成第 3 版吗？', refs: [ObjectRef(ObjectKind.artifact, 'a-recap-pdf')]),
  ],
  'research': [
    ChatMessage(ChatRole.user, '访谈里提到插座的有几位？原话给我。', refs: [ObjectRef(ObjectKind.material, 'm-interview')]),
    ChatMessage(ChatRole.assistant, '主动提到插座的有 3 位，其中受访者 7 明确说“插座多不多其实没那么重要”。原话已在访谈记录第 4 段，可以直接拿到论证稿旁核对。', refs: [ObjectRef(ObjectKind.material, 'm-interview')]),
  ],
};
