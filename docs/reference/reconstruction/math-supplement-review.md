# 固定数学原件补读（业务/工程采用边界）

当前仅分配index 1/2/3/4/6/8/15/16/18；固定官方GitHub commit 1581d4cbdd69a0faf34b2089614fe2a814316a6a。原件位于math-originals/；全部下载按Git blob SHA-1公式核对old_pdf_blob相同。解析文件只为阅读，不把提取或文件存在计已读。没有运行原算法、形式证明器、模型或产品；没有扩大外部文献。

| index | PDF页数 | 已读连续页 | 原图核读 | blob | SHA-256 |
|---|---:|---|---|---|---|
| 1 | 38 | 1–38（含附录/参考） | 15/23/24/32/34/36 | 06787a30a9409d724ed6e293db14156dd4d80621 | 32a11e793f6e379c8d750f8f3560a6b0648bee56441efd25bceea9c2b7ffb849 |
| 2 | 83 | 1–83（含表/附录/参考） | 12/36/39/49/54/60/69/78 | e2b0111de2c71532f25b1107ccbd45fe0aebb5d1 | 97ffc7fc55cae485ec1f001a66c4ece8a293215b1ecfc8f9ec50ef4913c8e9f0 |
| 3 | 29 | 1–29（含参考） | 7/8/13/20/26/27 | a3e5e059a5d0d359ca11fc5e258176171734e456 | 606657a956af61c2d133e59134ec98234d6e5386413ab601c1e7d493d1f0e4f2 |
| 4 | 38 | 1–38（含参考） | 17/20/25/33/35 | c78f753bd60429a458620771008fe98ef1c50c92 | 3300408a7ae8900efc7ae23fc7ae3a4678750e57de9b5d89d063ff596c710100 |
| 6 | 108 | 1–108（含参考） | 16/38/44/61/67/79/92/94/102/104 | 4d6bc342e67e238b9e76e97b5d0e6e33869d2da6 | 15030fab23d31a4d91bd944100a547c3e9eff6da8330a4fb94ea999f071fadfc |
| 8 | 13 | 1–13（含附录/参考） | 5/7/8/9/11 | 2906d12b2a41d8b87bb47666ac7a26641078d62e | 9c430672ddf2a1d0d2335c55c03b0b1fdea72f5a000c586bf130c2e984f19ae6 |
| 15 | 35 | 1–35（含参数表/参考） | 6/16/23/30/33 | e69d6ae52b88a6e72b55b809a8fa758340224bb3 | 85aa655473a156ad16db5bc59190d487a55db864d1014af0610c9c1f5b08490b |
| 16 | 55 | 1–55（含参考） | 16/28/32/34/36/37/52 | a1f9593437af57cd5fc1760e441a06fdae9f3fcd | 9e9a41553023f95e18b673f130c2086def56ba2b7a4cd851337a39eb114bf679 |
| 18 | 25 | 1–25（含参考） | 14/16/20/23 | 0ef808568ed51c689d7ce3f6e5899e1d9c01f4e5 | 4c8faec31a1af3d0c640485e464ecedd99fb984f4a2c3b457f032685013804ab |

## 08 An Upper Bound of 9/4 for the Matrix Multiplication Exponent

实际连续核读PDF1–13页全文；第5页Fourier/权重、第7页exact sequence和块矩阵、第8页Figure1三扇区、第9页增长/递归、第11页分离锥与催化不等式已看整页原图。

逐段证据：p1–3定义复数域算术指数，每个epsilon固定算法/常数，n趋无穷；张量角色需加法/乘法/限制单调与归一，nu为exact rank指数；dot-product三个方向的幂指数pX/pY/pZ形成lambda(Tn)=n^(pX+pY+pZ)。p4 Lemma2.3每次退化固定degree L，再tensor j、插值jL+1次，不能把复杂退化当单次无成本运算。p4–6 Proposition3.1要求两腿互不共享且标签匹配、只一腿共享，5M份输入经L=5M根式Fourier过滤u-v+2(g-h)=0，再权重(g-h)^2筛g=h，输出M个完全独立块各带BX(M)，两种M不可重复计数；Corollary3.2依固定频率word的multinomial/Stirling取极限，并非有限M立刻无损。

p6–8对C(a,b)普通多项式乘法，R=a+b-1；六排列几何平均P及t>0用于平衡。Lemma4.1以D=uw-vs的exact sequence构造同一组基底、所有first-input slice保相同块三角关系，kernel/quotient权重消掉星号块，再共同概率q优化得到离散凹性；不同slice不能各自选基冒同一张量退化。Lemma4.2三扇区的中间块交换第二/第三腿并反转第一腿，整体六排列才能合法得到P(a,3h+a-1)>=3P(a,h)。p9以增量单调推Ha的乘积增长、Da>=a^(4/3)，对照P(a,a)<=(2a-1)^(1/t)得t<=3/4；从所有character上界还须Detecting Lemma2.2，不能省掉附录。

p10–12附录：compact convex normalized states，经连续Pz lambda=lambda(zx)/lambda(z)、Schauder–Tychonoff与有限交性质取共同固定点；非空性用有限不一致子系统、有理锥分离、Grothendieck群和固定催化D推出D+ks>=D+m+Tds，迭代保留单个s而非取消s；先j趋无穷、再delta趋0、最后n趋无穷，固定成本消失次序关键。p9递归实际选择某固定u及存在的exact-rank分解、padding到u的幂，常数依epsilon；没有给可竞争有限u、CPU实测、内存、数值误差或实用代码。p12–13参考条目全文读，未递归下载引用。

采用判断：仅作为后台线性代数/计算复杂性研究候选，潜在消费者是已有分析/约束/AI数值工作中的矩阵核，不能给用户新增“9/4算法”操作、宣传有限任务加速、低硬件成本或已实现。本文只论复数算术模型，不能直接推出浮点稳定性、bit complexity、所有域精确算法、现有商业预算优化更准。必须另有实际有限分解/工作负载/正确性和稳定性/时间内存比较才谈工程替换；不影响正常专业分析继续使用已验证计算。

真实性边界：本次完整阅读并复核关键显示公式，不等独立形式化验证全部定理，也不等学界接受、官方最新版本或产品验收；固定版本身份已核，不为当前撤稿/修订状态做新增外部研究。


## 01 A Direct Proof of Optimal Max-Cut Hardness

实际连续全文PDF1–38页；原图p15(6.3)、p23隐藏坐标TV、p24仿射限制、p32不变list、p34参数次序、p36无权图转换已核。参考1–19读条目，不递归读19篇。论文提出simple unweighted Max-Cut任何固定alpha>alphaGW的NP-hardness；alphaGW≈0.878567是最坏情况近似比例，绝不是某项商业决策准确率。

- p1–6：输入是明确有限图、非负有理权重（边权概率总和1）；最大化问题alpha近似定义为可行cut至少alpha×最优。G-W有限精度SDP+随机超平面通常给期望alphaGW−eta。文中Theorem1.2按有理t、epsilon<(t-B(t))/4构造YES/NO gap；B(t)=2arcsin(t)/pi。关键外部输入为仿射2-to-1 games，量词顺序是对soundness b先定alphabet n，然后才任意缩小completeness error a；不可只拿一般2-to-1数值gap代替该加强输入。Boolean table-entry Fourier与affine-row Fourier不同，概率归一/未归一和标签q(1)=1分别保留。
- p6–13：完整2s树depth m，k=(2s)^m,d=s^m；monomial每对取一子，slice每节点取一对子，两遍历每层恰交一个点，根相关tr^(2d)。MaxCut图的查询只给最终word、不泄漏rows/tables/slice/incidentedge。任意cut拆odd/even；正噪声stability非负去掉even才得上界。row symmetries约束每pair sum=qv；unsplit只在一个选中gate做，不要求全树unsplit；随机slice平均损失s/(2em mu)。自由shift坐标是线性双射，保固定context上的正交投影。
- p14–21：vector-valued hash先保持total mass M，再两次truncate（过大mp和Ap点值），fourth moment用三独立变量/第四xor得到共同q，不能在distinct条件下假称独立。估计(6.3)各常数随zeta控制，不依源alphabet维度。Majority Is Stablest需balanced且[-1,1]、低degree influence；先用它发现entry influence，再投影/去even（K/2^s界）/hash推出heavy label。投影Hv不保证逐点bounded，证明只用integrated influence。p21的theta,p*依目标gap而非alphabet，但极小参数不能当实用计算成本。
- p22–27：去smoothing用Hilbert范数凸性；plant一个slot使context和latent新rows线性系数全0（常数项不归零），p0=2^(-n(2k−2s))，忘mark后density Z/(Tp0)、TV<=1/(2sqrt(Tp0))；q*定义不能看mark/额外edge。跨codimension1仿射restriction只有2s自由rows，Fourier质量损失2^(2s)，不能随手用同样损失到任意维数/投影。两端labeler只用自己的endpoint与共享随机，证明不要求labeler高效。参数必须(t,epsilon,mu)→(s,m,p*,theta)→b→n→T→a，打断此顺序会循环。图大小|X|^T·2^(2^((n+1)T))仍对固定gap叫多项式，但固定常数异常巨大，不是可用优化器。
- p27–35 Appendix A不能略掉：从regular3Lin构造legitimate disjoint tuple，folding标签空间仿射，valid-subspace conditioning有明确丢失mass；q_all与q_edge不相混。补全无效表项为0、保持FU/FV一致，invariant list需Q∩H=0、阈值tau/ambient维数条件；第二玩家对每个Q restriction标签gamma分quotient list且其size不依k0，第一list不需要这样的界。先soundness定ell/k0/beta，以k0=j^4,beta=j^-3同时满足beta k0→∞与smoothness→0，再取原3Lin epsilon<=a/(2k0)。这些依赖[6]及Grassmann/扩展定理的具体引理；本次未独立全文重验证这些外部证明，不能把阅读等同新定理公认有效。
- p35–38 Appendix B：删loops不重归一；每顶点替F_p^6 cluster，edge条件x·y落指定residue，Q=p^12；rectangle误差及任意split-cluster cut都核，非只cluster同号cut。这是hardness reduction所需确定性无权转换，不是产品graph压缩或推荐采样算法。

专业消费者与不能照搬：已有资源/排期/冲突分组若实际可建非负权二分cut模型，可借此提醒精确/近似/启发式须分开且不承诺所有规模全局最优；必须先证明业务目标确实是该图模型，不能把用户预算、因果传播、关系可信度塞进无依据权重。该论文提供复杂度边界与证明技术，不提供品牌策略、经营资源配置或舆情判断的现成算法；不能作为“我们有0.878567质量保证”的依据，实际部署G-W或有限精确法也需独立代码/数据/运行与对照。保留原专业优化能力，用明确任务目标、约束、可行性、上下界/最优性缺口和可解释方案验收，不让难度论文削减用户可做的事情。


## 18 The Factor-Two Hardness Threshold for Vertex Cover

连续全文PDF1–25，原图14/16/20/23（star resampling、restriction variance、list Jensen、最终常数）已核；p24–25共17条参考只读本篇条目。正文强调最小Vertex Cover的alpha近似为返回真实cover且大小<=alpha×optimum，和MaxCut最大化比率方向不同；本文声称固定alpha<2为NP-hard，对simple undirected unweighted graphs。不是“2倍业务收益”或任意覆盖问题的近似保证。

- p1–4：YES cover<(1/2+1/m)n，NO>(1−1/m)n，ratio=2−6/(m+2)趋2；每固定m才多项式，不能把m当自由规模输入。外部唯一hardness输入为perfect-completeness Label Cover，固定soundness决定constant alphabets；未依旧UGC为假设，但本次未重验证引用PCP/repetition原证明。问题不要求源约束图regular或expansion。
- p5–9：query只包含位置类型与自身端点问题，不携edge occurrence、opposite endpoint或projection map；valid local label须满足scope内所有源约束，compatible selection不只重叠一致，还满足scope union内所有cross-scope约束。norm为compatible选择有符号绝对线性和最大值，hereditary且singletons使其为norm；每query增量Linf界。图顶点是所有indexed seeds×grid weights，即使向量相同也不去重；邻接为norm(Sv+Sw)<=2t。局部norm枚举最多(1+r)^(2d)在固定参数下才可称多项式。完成性用midpoint-grid对连续uniform的误差与反集中界获得约半独立集；无模拟样本替代保证。
- p9–16：从大独立集删至多一个低norm点后固定集合A*、odd 1-Lipschitz距离半差F；F在fresh种子/权重之前固定，不能每样本重新选函数。continuous weight cube保原jointlaw且rounding d/p<t/2，方差nu d。restriction lemma在16h+32h²<=nu d/2、三个oscillation 2/4/2条件下保own-question variance>=nu h/4。按独立seeds/weights Efron–Stein分解，不能按相关endpoint questions假设独立；同seed两endpoint projections不必commute。star Dk只保相反端点问题，令incident invisible能量O(d)而非O(d²)，随机batch保留条件S∩J={j},N∩J空,T无非incidenthiddenpair。固定冻住数据后剩余抽样仍原productlaw，不以成功事件condition。
- p16–20：gradient在有限分片affine hyperplanes外等实际梯度，exceptional点明确0，始终在signed compatible incidence convex hull；block L1<=1，无效slots为0。own conditional均值导数=条件平均gradient仅almost everywhere，cube variance<=2 E L2²，不能当每个固定点均成立。用beta/2阈值得长度<=ell的local list；必须先conditional Jensen把list能量传回同一个full gradient，再用hull的compatible hit界，不能把不同conditional gradients拼成同一compatible选择。故EZ>=2。
- p21–24：pair解码固定其他weights/seeds后list函数本身不能重算，也不能看到被隐藏occurrence。枚举ell² rank pairs得到每pair成功<=ell²sigma，Z<=1+sum(pair events)给EZ<3/2，与原restrictedlaw的下界矛盾。p23关键常数已原图核：nu=1/(2^7 m³)、beta=1/(2^10 m³)、ell=2^11 m³、h=2^12 m³、d=2^38 m^10、sigma=1/(2^46 m^12)，不是文本提取可能误读的1/(27m³)。所有constants先于源alphabet选择；如此巨大d决定reduction polynomial degree，绝不构成可运行优化方案。最终阈值用整数m|cover|与(m−1)n比较。最大匹配端点的2-approx只对本文unweighted vertex cover。

专业采用：已有依赖检查、冲突处理、最小干预方案若确实映射为graph vertex cover，可提供可行cover、实际成本、最优性/近似边界与用户固定约束；商业预算有异权成本时不可拿unweighted matching端点算法直接宣称2-approx。允许小实例精确求解或结构化特例，并不因worst-case hardness删除能力；不把NP-hard文献包装专业判断引擎，不要求用户理解Lipschitz/LabelCover或配置证明参数。论文的own-information与统一可行域可启发后端避免信息泄漏/拼接不可行方案，但它本身不是产品权限证明。全文核读不是本定理的独立形式证明或产品算法验收。


## 03 Constant-factor hardness of directed feedback vertex set

连续全文PDF1–29已读；原图7/8/13/20/26/27核参数、Ramsey/minimax、pivotal预算及unweighting公式。末页14项参考条目已读，未递归核引文证明。论文声称对finite loopless digraph的DFVS，任意固定A≥1近似均NP-hard，允许opposite arcs；YES≤k/NO>Ak是固定A的最坏情形复杂性，绝不是用户依赖图一概不可解。

- p1–7：唯一外部hardness输入为imperfect-completeness 2-to-1约束、fiber恰2、任意constant θ的YES≥1−θ/NO≤θ，无需UGC、perfect completeness或regularity。中间构造protected rank vertices/deletable tests，正有理cost；YES≤4/NO>D=10A。dyadic-biased junta由uniform Friedgut经k-bit AND提升，I_half(F)=2kp I_p(f)≤kb，按触及blocks条件预测后junta大小独立于alphabet；近似junta不必monotone。
- p7–12：M=128(D+1)、p_i=2^(−i−1)、q_i=2p_i、γ=p_M/1000；T由Chernoff界、K≥M·2^T D/γ、B0=(D+1)/γ、junta J先选，再取θ使Kθ≤1且J²θ≤γ，最后alphabet m与kmax=2^T m、L=10kmax+30、ρ=min(γ,1/m)、η使3ηL³2^(m^T)≤ρ。固定A常数极大；多项式只对源instance规模，不保证可用运行时。rank profile有prefix identification且class cost D+1；global labeling的evaluation在rank arcs严格增加。wildcard ordered cells保空cell，重复vertex各slot仍独立抽样。X/Y coupling对每fiber保持R_u⊆π^-1R_v，Y marginal q=2p：无X命中时额外概率p²/(1−p)²。complete删除fullprefix≤1、两singlelabel各1、comparison Kθ≤1合≤4；所有随机结果枚举为理性权重，不以随机运行结果代reduction。
- p13–16：先固定假设cost≤D的删除集和完整topological order，再由finite Ramsey/minimax取同一随机递增ψ令任意triple分布TV≤η。TV期望差不能直接当同ψ事件一致；证明用pointwise intermediate ordering再union bound 3ηL³2^|Λ|。good tuples导出monotone g、empty=0/full=1和prefix兼容，wildcard test若跨0→1必须删除，否则出现order矛盾。good bound只对抽样tuple，不是所有tuple同时成立。
- p16–22：shared data D=(ψ,t,prefix vertices,bias indices,subsets)必须在fresh edge/scale之前固定，且所有bias共用这个分布；bad tuple用固定dictator fallback。fullstar强制删除推E h_u(p_i)≥p_i/4，不能条件化在成功事件上再跨尺度累加。B(f)对每坐标只按“某个随机排列prefix使其pivotal”的事件收费一次，不是总influence/把所有prefix重复收费。arrival-time证明I_p(f)≤B(f)、h(p)≤pB(f)，使用潜在见证事件与本坐标arrival独立，实际switching coordinate不独立。singlelabel删除cost推出同分布E B_u,E B_v≤D+1。
- p23–26：comparison删除界≤2ρ+M·2^T D/K；短lists在fresh edge前选，projection命中概率≤θJ²。坏budget用Markov≤2γ；不交list在fiber coupling下independent，再以junta替换误差4γ得E[h_u(p_i)(1−h_v(q_i))]≤10γ≤p_i/16。H_i={h_v(q_i)≥1/2}推出Pr(H_i)≥p_i/8与B_v≥1/(4p_i)，对同一个B_v的dyadic tail求和得E B_v≥M/64=2(D+1)矛盾；不能把不同随机函数的tails相加。
- p26–29：正权上界C、S个vertex，clone count c_v=ceil(Sw_v)，每cluster独立，原arc转全部intercluster arcs；任何原剩余cycle可lift，因此partial clone删除不会优于对应full-cluster方案。总≤CS²+S，rounding每删除集增≤1，YES≤5S/NO>DS；无需大公分母。相反arc需用private protected subdivision后再unweight，不可直接删边。DFAS扩展把vertex分裂key arc与n+1 private connector paths；若删除数≥n输出全vertex，否则每connector至少一条存活，可恢复解，保opt和近似；不是业务依赖删除授权。

专业采用：实际有向工作依赖出现环时，仍须检测环、列可行顺序/最小干预备选、保强制任务和业务cost，必要时小实例精确求解或结构化启发式并报告可行性、成本与最优差距。把vertex删掉会直接丢业务工作，产品不可为了DAG自动删除用户要求；范围变更需用户确认。本文是复杂性边界，不提供新的实用DFVS求解器，不是经营/品牌判断算法或产品权限证明；也不能借NP-hard削减能力。全文核读不等独立形式验证。


## 04 Constant-factor hardness of Min-UnCut

连续全文PDF1–38（含15条参考）已读；原图17/20/25/33/35核指数、四角共享变量、joint TV、分母和路径图。正文声称对simple undirected unweighted graph任意固定C>1近似Min-UnCut NP-hard，k≥1不等于普通二分图识别。Min-UnCut为删最少edge使bipartite，两边可空且无需平衡；|E|−MaxCut虽然恒等，MaxCut乘法近似不能转换为近二分图的Min-UnCut乘法近似。固定K预处理常数巨大，非实用求解算法。

- p2–9：Håstad near-satisfiable parity gap＋ordinary parallel repetition是外部hardness输入，无UGC；原权重按最大余数转uniform equation list并对全assignment保TV误差。folded proof认Boolean query真值表本身，重复描述不能生不同变量。face array m轴，每row affine function独立，truth-table重复codeword须一起翻；T1 Gaussian、T2 rowresampling、T3指定tablebit三种cost分别正规化，budget未必是概率。任意加权proof family只获平均fail界，不能提升每instance界。
- p10–21：Gaussian smoothing→mean-zero能量≥3/4；row degree计非零row而非alphabetbits，Hermite cutoff因微分shift用s+2。第三test给gradient signed-codeword凸包权重期望≤2J/η，再先截高权重取得product-of-faces相关。gauge invariance强制localrow equal odd frequencies，选择pair仅看mask/Hermite支持，不枚举频率。randomsubbox去其他row/support；projection传递常数C0=4^M binom(N+s,s)（不是4M），slices的discard依背景/整函数，不可依实际自由localrow值。clip后boxCS先消掉任意facesigns，再在背景下用四独立row但四和仅parity约束的square；不同square用不交rows。参数J→σ/δ/η/R/s/T→m→H/γ/A0→ell→n→p/θ，全在alphabet前，有限不代表低成本。
- p22–30：outer game重复变量仍freshanswer slots，仅单equation内重复变量要求一致；secondvariable question不带原equation/position。hints仅canonical truth tables，不夹带pullback原式/隐藏metadata。零hint系数active slots在固定背景后仍原独立抽样，soundness≤(1−2^(−q)(1−c))^t0。joint smoothness是conditional U的整组forms TV≤0.5√((2^(4dt0)−1)t0²/t)，不是固定已见hint后新row uniform。diffuse Fourier transfer需把q背景＋3fresh forms一起替换，再用fourthmoment；frequency collisions允许相加，不要求pullbackinjective。内常数→b4/u/Delta/s*→t0→t→ζ，completeness ζ最后取，不偷换quantifier。
- p31–38：完整joint Gaussian sign tables以截尾、grid、正偶阶Taylor density、精确rational归一化同时近似所有proof cost<1/2；相依阈值无须协方差可逆。midpoints的a+b√N用rational square compare，ties明确定义。integer multiplicity总数D0 W=O_K(M0^t)，不是只数bitlength。folding complementary pair统一变量；equality四边path/inequality三边path、每次freshinternalvertices使violation准确一条uncut，无加性baseline；selfequality成4cycle、自不等成triangle，保simplegraph。k=5D0，YES<4.5D0，NO>(10K−0.5)D0。deterministicalgorithm后果P=NP，随机固定成功率后果NP⊆RP，二者不能混写。2CNF每edge两clause精确保opt和可恢复assignment。

采用/限制：只在真实二类相容/冲突约束已被业务明确映射时作为后端求解保证边界；预算、人员、渠道通常带权、多组/时序/硬约束，不能直接套本无权二分模型。用户应获可行备选、违反了哪条软约束、代价和固定项保全；无法满足的硬约束须解释并请求改变，不能自动删客户要求。保留专业优化能力，按实际有限规模选精确/启发式并验有效解；不用论文名代替能力、也不以hardness作缩减借口。全文阅读未独立形式证明或实现该reduction。


## 15 Staggered extraction for exact matrix multiplication over every field

连续全文PDF1–35已读，原图6/16/23/30/33核Laurent系数、presence-mask修复、schedule、区间/整数表；15条引文只读本篇条目。本文定理固定任意field（含positive characteristic）ω_F<2.371054886006746，exact scalar additions/multiplications渐近成本，不含bit-complexity、有限规模crossover或浮点稳定性。无算法/附带verifier执行，不能把全文或证书描述当本次重验通过。

- p1–6：CW5 strand8→4→2→1，zero-coordinate终止matrix tensor。以整数Laurent恒等式常数项消去负幂，R(CW_q^m)≤(q+2)^m binom(3m+2,2)，无field division/interpolation，故finitefield可用。rank到arithmeticalgorithm使用固定d decomposition递归，常数依field/构造；identical-summand sum inequality要求每输出同一个oriented(a,b,c)，相同volume不够。
- p7–18：跨depth pooling仅同一physicalpriorityorder，population/ancestry/placement保持分开；p须在其marginals下entropymax（productweights或唯一coupling）。childwindows/inheritedpairmixture必须一致且positivegap；Y仅Zzero指定groups，Z仅X/Yzero指定groups，residual即单shape也不能新增individualtest。commonhash模37^k，coarse差≤16为unit，survival条件只Se不夹后续eligibility；先freezeXeligible再Y再Z保证每实际monomial归一输出。变量为completeword非localfactor。修复须每full-variable orbit缺失≤C/N，不是平均全局缺失率；independenthalfpermutation pairvariance≤n/8，Chebyshev保每orbit。presence masks L=O(N/logN)，≤2^(3L)copies每矩形选一次，使各coefficient准确一次，不能直接相加shiftedcopies重计。允许改变旧holepattern，group只需保ideal tensor；replications按操作不按summand重复倍增。固定finitepopulation/subdivision/tolerances先于N→∞。
- p19–27：tiedshape先均分labelplacements，descendant不resort，相同u来自不同t保各自d_t,u。StageB代表(3,2,3)故weight2是middlepriority；StageC四shape由marginals唯一coupling，包括d=0/1。A→B用同一V mixture，Brepair后不再留A旧holes；λ按capacitybalance拆每history，不改law。K个lot共K+2ticks，K−2full＋4boundary；β为LA/LC range和非0损失。6physicalorders乘输出counts，每history恰1/6。terminaldimension由exacthistorycounts＋commonterminaltypes定同orientedtriple，zero-axis matchedpairs计完整weight1labels；orderedpair z的offdiagonal出现两次仅概率normalization，DiDj另计multiplicity。先固定K、rationalconsistentlaws/tolerances，再N→∞，loss→0，K→∞最后，不能同时增K声称uniform。
- p27–35：五精确arrays17/118/54/75/36和lextraversal均读，(6,1,1)及zeroamountparents也必须consumeentries；二进制d=n/10^6，不套F(n)。F为16阶Taylor后64次幂的正rational，223不同arguments。entropycert有rational-logseries31项和明确tail，directednative-log仅crosscheck；严区间比率(2.371054886006745684,2.371054886006745685)，圆整表只够2.371056。本次未运行verify_parameters.py，未核所有整数到可执行代码的一致性；固定官方论文声明不是独立proofreview。

采用：可作为精确线性代数内核的长期理论候选；经营/市场矩阵、统计/优化消费者的实际要求仍是数值/类型正确、规模可承受、结果可复算、误差与输入口径保全。不能以ω下降宣称CPU/GPU/业务实时性加速，不新增独立“数学工作台”，也不把tensor阶段暴露为用户步骤。实用采用须给有限构造、同硬件同输入性能/内存/精度比较及恢复fallback；这不削减已有分析、优化、数学场景全范围。


## 16 Subpolynomial query complexity for well-conditioned log-concave sampling

已连续原文PDF1–55（23条参考条目），原图16/28/32/34/36/37/52核trace、Hermiteendpoint、circuitinterface、seedcovariance、两类depth、noise和下界公式。固定snapshot标题2026-09-26但正文p2引随后2026-09-30 preprint，是此固定blob内容，不能从封面单独推版本先后。范围：C² potential全R^d、已知minimizer0、V(0)=0、Hessian在I与2I，全局exactfirstorderoracle，TV≤1/10，everyexecution querycap；query间realcomputation无限制，d^epsilon不是polylog或实际运行时间。

p3–11：nearquadratic F仅C²且gradient Lipschitz≤δ=d^−e，不必convex，但fullquadratic Hessian(1±l)I；center任意无gradientbound，D(x)=sqrt(d)+|g(x)|。noisynearquadratic theorem先产ν*N(I)，pre保独立1/2Gaussianreserve以W2→TV，还不是原目标采样。Poincare/conditionalcumulants通过S=(z,f/L)只用Df，所有非平凡tensor split控制，不只方向derivative。conditionalmean Mρ center x+rρy/scale r√a仍是待实现oracle；transport dY/dρ=−rMρ，rho<1。
p12–23：materialderivative由adjointdivergence表达，singlecontraction双方各保free slot，不允许selftrace随意套norm。divergence高tracemoment展开只一个closingtrace d^b，取s≥(j+C)log(d+1)吸收；最后vectordivergence才支付一次sqrt(d)。不把fixed-timeRMS换成path-sup。K^T=∫BρU1，B′0=0保证kernelendpoint正则，transpose不可漏。stationaryskewflow product ξ*×Gaussian invariant，G1独立Y1但不独立initialanchors，mean u仍在velocity，idealξ*sample尚未实现，不可称实际routine。
p24–30：globalPicard每layer全mesh并行依赖，不按cell增depth；logtimegrid cell长度配endpoint导数奇异性，quadrature仅nodewiseRMS。kernelaction直接用短harmonictrajectory和forward correction，不先近似K再乘任意G；保forwardintegral抵消1/r。首cellHermite插值强制B′0=0再积1/rho，approxvalues亦无端点奇异。approxcenteringvelocityglobalLipschitz可做Picard，truejointflow不globalLipschitz。先固定J→PicardN≤ceil10(J+2)→dependencydepthO(N²)，再w再interpolationm；提高m不增加依赖depth。31–55后续核读结论如下。


p31–43：exactmean circuits的所有scalarcoefficients、nodes/scales/queryshape先于oracle值，finalmean尺度恰rR，中心expression仍rτ≥rR/√5。Gaussianabsorption的pathwisetranslation须同时保持querypoint；噪声center x+nZ配seed P G，P=(I−vvᵀ/(4D_s²))^1/2，PPᵀ+vvᵀ/(4D_s²)=I，直接用新independentseeds不成立。routineheight和expressiondepth不同：同一个parent的center-supplying和目标mean各是directchild，label误差p−1/4只扣一次；重复mean必须uncached复制整subtree，不能为省查询复用随机值破坏独立。finalMchild(b+t,p)、finalSchild(b,p)、其余(b−10t,p−1/4)确保boundedheight与b>1/2；terminal一次query，σ/η附加noise按3/4+1/4合方差。先Picarddepth/H→w→interpolationm→bottom-upshift，与oracle值无关，everyexecution cap polylog δ^−t。accuracy先fixed-seed稳定替换inaccuratecenter，再absorb恢复standaloneseedlaw，再用child分布保证；不声称真实intermediate全有idealinputlaw。
p43–48：proximalconditional κ_y加√hGaussian的chain保π_V*N(h)，W2contraction1/(1+h)，局部误差依|y|但actualmomentrecurrence控，额外一次smoothingtransition才到TV。之后variancehalving条件式Z=Y/2+Znew/√2、x/r递推，J=O(logd)；逐次替换用idealprefixmoment与data-processing而非actualhistory假设。base固定O(logd)modeiteration，Gaussian近似TV≤C l sqrt(d)，最终已移除auxiliaryconvolution。ε先取2e<ε、K0>2+1/e、t受40(K0+2)t<1/4，oraclecap d^{e(1+t)+o(1)}；有限小d采用cappedGaussianrejection，至多ceil(4·2^{d/2})，全失败回0导致TV≤e^-4，不是exactlaw。故常数可极大且realcompute无上界。
p48–55：lowerGaussianA=OΛOᵀ，queries=value已被gradientAx确定；deferredrotation给任意adaptiveoutput落(q+1)GaussianblockKrylovspan，但g_i仅相互独立、不独立A/O。output自身作为额外direction保unrestrictedrandomoutputs；cosinefrequency2q+1高于degree2q，Gram/Frobenius界对整个span统一，coefficients可依transcript。targetquadraticstatisticbias d·(3−√8)^(2q+1)/√2与alg≤160(q+1)^2√d分离；用joint(A,x)event证明某固定input失败，不要求算法能计算该test。下界Ω(logd)与upper d^ε不合并成polylogexactrate。

采用/限制：只有后端已定义可验证的C²/全局Hessian/knownminimizer模型、exactgradient条件成立时，才能研究这一采样构造；一般受众情绪/传播/经营后验、多模态或离散分布不自动符合。查询次数不等walltime/成本/精度，TV1/10不等每项业务结论90%正确；无AI专业统计和约束分析完整保留，AI增强结果单验。当前不运行新sampler、不宣称论文模型已进入产品；若采用须证明有限计算/数值误差/终止、适用模型、可复现随机性与基准改善。固定原件全文已读，不含独立形式证明或外部引文全文/实现验收。


## 02 Complex Matrix Multiplication Below 2.258 and Rectangular Bounds

实际连续全文PDF1–83；首批输出p6曾截断，后完整重读p6，故没有缺页。原图12/36/39/49/54/60/69/78核GROUP几何、控制/比较式、整数递推/舍入、inset、dual参数及精确系数。引用33条只读本篇条目，未再递归外文献；没有执行随文certificate/checker。

- p1–12：平方omega<1129/500、dual alpha>.465、w(.709)<2.092为复数/特征零算术操作指数，任意epsilon固定常数、输入规模趋无穷；不含bit复杂度/浮点稳定性。rank与给定Laurent多项式border-cost witness不混；fixed witness degree先定，tensor N后插值O(N+1)R^N。GROUP两个reader不共享本地变量、第三side可共享，完整群tensor rank成本必计。sphere tags同半径，slice displacement正交，模数4Q无wrap才推出匹配；保相关未解决fiber是locality而非nonvanishing证明。
- p13–31：逐层label必须真实被双方各自读取、原support/word retained、canonical labels与物理six permutations分开。COMPLETE/PARITY与binary是有限退化，child在parent确定后处理；所有basefactor和aux成本计入。score gamma(D−A)+M无payload偷计。finite recipe closure不等无限算法；严格超额才抽出某个有限recipe。law/utility修正用singleton稀释而非默认为相同分布。重复旧GROUP不消除每份rankexcess，须重编整个固定conditional transcript。matching boundary需各bank确切类型、tightness和APhash后的isolated edges，不能只按总直方图。
- p32–40：comparison用full covariance C:D²，无1/2。有限AP目标窗中每条允许路径均到窗，非概率大即算成功。shrinking corridor soft clipped drift、hard singleton inward、三段Taylor代价共同证明；选择次序local constants→finite targetL→finiten/tree→finite approximating recipes→outerBankB，未把无限depth用到finite tensor。supportcovector误差含max |Lalpha_i|/alpha_i，需positive inset。
- p40–50：W gamma0=300000/225925；shell degree112整数recurrence/residual、2161端点及曲率插值/精确lawcorrection，不能仅采样判边界。finite library候选优化器不算utility证明，后用精确law/entropy重算。六分量chart P780,500/Pref420,224有限有理递推而非假设无限series；operator36非18；whole-disc residual<3.329e−22和一二阶导数误差均需保留。
- p51–60：480²几何中心需tail/Lipschitz/Hessian补齐全域；E1低位严格径向下降、clipped face/cap角处不假称二阶光滑。52 exactdecimal barrier；128²sector boxes14阶Taylor、128bit范围和floor/截断语义、最大depth28未解决即拒绝，非samplepass。65536boundarycells及6×8192transition subcells，用face/cap不同attainability来源、concavity chord+singleton correction、root误差和derivative界；实际face gap28.49e−6而非弱checker阈值12e−6。continuous margin5.5e−6，inset eta2e−9转移损失<1.3e−6；support误差<1e−12与interior strict9.8e−8才导矛盾。
- p61–72：strict score抽取有限L,R,v，整数L^1500 v^1129>(3^n R)^1500，全部参数先定；不提供有用finite implementation尺度。readable label tree每个prefix三side可识别，节点aux只计一次、不同prefix共享固定size；各pair conditional entropies相加Hπ。F7是两个base、最多7次size2/3completion、各termination/side之有限族，supportfunction最优只对族，不是所有算法全局最优。dual要求nonincident choices与X条件独立确保精确S=H(X)，不能近似相等认证exponent2；七步law288basefactor，.465以有理界证。rectangular75support curve t2/3推出.709/2.092；另72factor平方2.267独立于更强2.258。
- p72–83：比较只针对注明版本published tablebaseline及其ordinaryconvex/block/subdivision组合，separator是记录上界成本的约束，非未知真实exponent下界，也不覆盖全部未列参数。追加baseline需单独通过13.5。finiteexception跨域以两个fixedexactscheme共同numberfield/cleardenominatorD，排除D素因子集合S但未算S；有限extension全电路constant模拟，不每tensor层乘成本。dual只characteristiczero，不对positive characteristic断言统一有限exception。附录52系数与certificate身份/环境全文读；纸上作者reproduced日志是历史记录，本次未跑。final-summary把analytic margins当输入，未证明它们；hash一致也不证明chartrecurrence，certificate非proofassistant。

采用：与08/15合并为现有后台线性代数研究候选，保留各域/精确性/数值稳定性边界和原已批准采用要求；不能因较好指数就替换实际数值后端或承诺有限任务省时。工程采用需构造实际finite scheme、匹配真实矩阵形状/域/精度、正确性/稳定性/时间内存/恢复对照；研究完整阅读没有关闭算法实现。用户不需要算法菜单、tensor参数或所谓2.258工作模式；业务约束与结果质量仍由专业领域处理。


## 06 Exact Derandomization of Logarithmic Space: L=RL=BPL

实际连续全文PDF1–108；原图16/38/44/61/67/79/92/94/102/104核copy公式、精度、压缩、coupling、负数floor、物理stack、单点损失和compiler参数。34项参考读本篇条目，未递归核引文全文。固定版本声称L=RL=BPL；本次是完整原文/采用边界审读，未独立形式证明、未编译运行该构造、未核最新同行评审/更正状态，不能把标题作为寰意已经实现的结论。

- p1–10：模型为finitecontrol/endmarkedreadonlyinput、fresh independent fairbits、所有随机带上polynomial worstcase time、O(log(n+2))workbits（含空白visitedcell、所有livecounter、suspendedcall）；outputwriteonly不能偷作workspace。概率近似输出a/2^(q+2)，误差≤2^−q，spaceO(logn+q)、time(n+2)^O(1)2^O(q)，q为unary，通用精度非poly(q)。time-layered完整配置不合并halted tapes，strictforward substochastic S使finiteinverse和所有配置p∈[0,1]。rankchannels有framecondition、retentionπ≈f、conditionalpairbound，不是所有ID独立；fixedpadding需injectiveaffine且fieldradix可逆。
- p11–19：E recurrence的fixedpoint只作comparison，不是无限迭代实现；同一row严格forward路径有限support≤1/(theta1 f)产生factorial truncation。pilot先用trueexpectations固定detector multiplier，actual全程用final同一g，不能用估计值定义自身truth。D=C−E+EC非负substochastic且支配unsignedterms，估计数组仍可负。copyrouting含weightedprojection C+Pr=PrD；D0与detectorK/J/R*/Kc顺序固定避免循环。activevertex最多2倍而density2^−H降；reward全primary error≤|V0|2^−(H−1)l。
- p20–29：densebin/normalizing preparation分开，broadYa与narrowusageYt不可互换；denominatorclip[1/2,1]即使mean0也total，只有truepositiveQ且g>0需≥3/4，g=0不要求假下界。computedtables需同一finalrawarray/tuple/repeatabletest，不假设unbiased。ranks带无限defaultdummy但dummy不是realport；supportmargin3g<e与fixedfiniteaccuracyexposedclass适用于任意错误/负raw。incomingcap是overfullcolumn全部移除而非任取Kc，zero-weightports也计；actualdistinctpair去重与inversepairing必须实现。assemblyerror释放旧source付f/u，不能把不同source误差当同一child。
- p29–42：propertyT外部输入明确Shalomlinear/relative，deriveaffinefiniteactiongap；conditionedchannel(F,w)按不同modP/modL作用，freebits合一torus，fixednumbercomponents保证gap不随bits增。guessedwrapvariantfullenvironmentmeasurepreserving，在指定ID每word恰一validwrap，权重不可再除variant数。trueatomsproduct可条件独立，estimatederror不用独立假设。additiveschedule对同endpoint先合并budgetm−(m−1)，同word/wrap保持cancellation；coarse b/a和fine(b/a)^(s−1)、sourcef/u入norm，精度M/Kprec−.5log1/a−.5log1/u−A*rank支付。omittedbins必须证明fine；meanbias与absolutenoise分开，walk先整体spectral估计再crudevariantcount。
- p42–52：compressionexceptionlist+commondefault，不是把所有小数无条件丢弃。update有floor/rankcap、有限support、全arrayLipschitz≤28；firstoverflow分trueheavy与errorfake，conditionalrarity仅能乘deterministicmeanmagnitude，不能对依赖event的noise同做。stationaryreference/two-large-entrycases与sentinel是证明device非可免费识别storedvalue。clamp同increments保relevantfunctions，但不保证accessdescriptors相同；actual/clean都clamped直接比较。
- p52–62：shortfingerprintfixedpaircollisionO(1/B)不足处理adaptivecandidates。预算block h*=floor(epsilonlogB)，salt独立；actual只能读current及earlier有效salt，walk冻结currentlater。clean同block全部value/structure改exactequality，lowerblockactual不变；cleanenvelope由witnesssupport构成与currentsalt独立。escapedpath用unitwitness差异包括zero-weightports，右factor借actualincomingcap消除旧source；不在saltdependent stayinside事件上condition。bucket只aliasincrements，不搬/合并旧storedvalues，保distinctrealIDs；coupling误差以childbudgetdrop关闭，epsilon最后选。catalyticfourvisitsXOR恢复任意原内容，Bbitvector仍算ordinaryspace。
- p62–73：每fixedtuple有一个exactvalue，数字clock提高不能改变结构/port/list。signeddigit alphabet−6..6，Nt scaleerror<2，differenceNt−2Nt−1定义stream；有限approx可以不同截断，不能依无穷exactreal比较。乘法按minindexband，负floor residue兼容，晚operandquery需早释放coefficientword；reciprocal仅positivecompactinterval，earlyword保存只在j≤t/2+O(1)付clockgap。semanticrankdomain/validity固定、clockdependent仅finiteapprox，invalidslot先branch不访问未定义值。publiccoefficients可scratch，operanddependentword不可伪装public。
- p73–92：singlecursor，leftcorrectionpairedmove/boundedinversetoken，righttuple不保oldsource。terminalprogram区分opaque lowerquery（内部工作态不入uppergraph）与explicitterminalchain；guard不能代替指定执行已有rawrecordproof。syntheticpredecessor也freshpresencecheck，旧validflag不可信；holezero与rankdefault不同。sinkcomponenttreecontour枚举canonicalinitiations，无secondaddress；incomingcapcount全列，incomingrank量化scoreword是state非rootparameter，先least-significant suffixassembly，完整score只跨structuralcall。exact/clean长counter不要求同physicalbound；实际端点salt由outeractionownertag取得，不影响lowerpath。rawcoeffa0,b0不递归替换成expandedphysicalA,D，copy只localrawchain，pendinglowerphysicalappend，Phi差望远镜合O(B)。parameterreplay只scratchscanactivechain无丢弃地址，guards/syntheticstates遵effective盐权限。
- p92–98：integratedmoment→specifiedstart损失N明确支付；式12.5原图为p≥r+3+(CNB+ceil(log2Ctail)+3)/8，不是把CNB放括号外。枚举原fairbitstrings保hashreductionmultiplicity，不能只枚举distinctreducedkeys；conditionalframe/hit筛选等权，中位数重扫不存multiset。每坏environment也须total才由O(B)completeconfigurations推polytime。promiseoutside仅total不保证分类；q变化用virtualpadding固定accuracytheorem，单改digitclock不改statisticalerror。acceptingwitness需p≥inversepoly且writeonlyoutput，先silentverify终态再secondpassstream，低于promise只给validaccepting或failure。
- p98–108：compiler接suppliedtime/spacebounds为hypotheses，不判断任意程序是否满足。fixedresourcecappedU/固定separatorA先于source，virtualpadding N=2^(P Bn), P=1000(dcode+2)^4(a+b+2)可能极大；程序grammar是actualfinitefields非递归可执行描述。输入callback扫描真实endmarkers、librarytapes同时计存储，所有finitealphabet转bitactions。式13.11 H=2^((u+100)^2)(qD+1)不是2乘平方；polynomial常数/exponent巨大，不是效率承诺。可选onewayrandomheadstay用finitecache保源步数，与一般two-way随机带不同。compiler有限可终止不等实用、形式化或现成软件。

采用判断：保留原已批准数学能力的真实要求，本文可供现有计算/验证后端研究有界随机过程的确定性估计、可复现/有限资源语义。不是任意AI、商业判断、网络实时采集、舆情真实性或全产品省内存的通用解法；环境随机性与信息不确定性不会因数学derandomization消失。工程采用首先须实际工作负载确实满足模型、固定完整构造/常数和数值误差，再用有限输入验证输出、终止、时间/内存、错误/恢复；不能只实现bounded示例或finiteinverse就宣称全文算法完成。用户输入业务问题/约束，得到依据与不确定性，不需要编译器/盐/谱隙菜单。

## 收束：采用、限制与未闭

固定分配9篇全部全文核读，共424页；文件身份全部与指定commit的old_pdf_blob一致。必要公式/图共56页（1:6，2:8，3:6，4:5，6:10，8:5，15:5，16:7，18:4），不声称所有页面逐像素放大。没有本批未读正文/附录/参考条目；引文原作未递归全文读取。

| 原件 | 可进入现有能力的内容 | 不可照搬/尚未关闭 |
|---|---|---|
| 01/03/04/18 | 图优化任务的目标/约束/可行解/近似与最优界区分；保留小实例精确和适配特例 | 硬度不是solver、不删用户任务；没有业务建模或运行证明 |
| 02/08/15 | 同属矩阵计算研究，按field/shape/exactness分别保留 | 没有有限规模加速、稳定性、内存、实际实现证明；不能合并成一条无边界omega承诺 |
| 16 | 匹配强凸/光滑/knownminimizer条件时的采样研究 | exactoracle/unrestrictedrealquerymodel不等实机时间；不是任意分布/社媒数据采样器 |
| 06 | 有界计算模型下的确定性概率估计/重现与资源语义 | 论文声明、全算法实现、当前运行验收仍分开；不是全产品AI准确性或实时性证明 |

全9篇本次均未运行算法/证书/形式证明器、未替换后端、未新增服务或对外效果。原有9 family/完整采用合同不能因本批文献数量改成26项用户新功能，也不能因只完成过小场景而缩减原完整算法要求。论文版本状态、同行核验、实现/适用数据/真实运行/专业结果验收均不是本次全文阅读的已完项；交由完整方案如实保留，不用研究数量抵交付。

## 固定版本README与源码树附件的补核

`math_revision_status_research_20261018/evidence/`中26份README及26份tree已完整读正文/全字段；树的重复URL/mode按经逐字段核对的生成规则展开，全部path/hash/size保对应，不因此重读已有效的26篇PDF或宣称树中源码全部已读。归档README的Git blob与对应tree所列README blob均不同，LF归一后仍不同：它们只能作为确切归档文字读取，不能猜转录原因或宣称与固定官方原字节相同；真正采用复现代码前须核固定源码与其完整依赖。

主负责人另完整核`02_readme.md`1–47及`15_readme.md`1–29。前者明确输入清单检查仅证明输入完整性；方阵证书重放需要POSIX fork、C++17有符号128位整数、64位long、算术右移、UBSan和GMP，两个实际证书步骤须成立且保断言。矩形可选高精度计算不等严格区间证书。后者需分别做rational-log与native-log区间核对，有限证书仍不证明张量构造或极限论证。Windows现有编译器或Python可运行不自动满足这些条件；相应数学Owner准备适配、独立数值对照和实际资源预算，不能以文件/退出码冒数学证明，也不以该特定复现工具条件阻断无关专业工作。未执行原命令、安装依赖或新增GPU要求。
