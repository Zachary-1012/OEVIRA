# 寰意 / OEVIRA

**可靠信息、持续监测、专业判断与可交付成果。**

寰意是面向媒体、广告、品牌营销与商业运营行业的自研专业智能工作平台，服务商业运营团队、品牌与代理机构，以及
个人研究与创作者。用户围绕目标工作，获得可以核对、修改、保存、重开和交付的成果。

> 产品规格、源码、实际运行与发布验收分别核验。可交付版本须完成完整功能、视觉、
> Windows运行、保存恢复与真实工作的独立验收；源码或文档存在不代表已可用。
> 工具职责见[scripts](scripts/README.md)，历史资料用于核实复用。

仓库：[Zachary-1012/OEVIRA](https://github.com/Zachary-1012/OEVIRA)
本地主路径：`D:\寰意`。

## 唯一现行产品资料

| 文件 | 职责 |
| --- | --- |
| [PRODUCT.md](PRODUCT.md) | 定位、用户、范围和取舍 |
| [完整功能与流程](docs/PRODUCT-SPEC.md) | 功能、真实工作、页面映射及取舍理由 |
| [DESIGN.md](DESIGN.md) | 导航、页面交互、视觉、三语和无障碍 |
| [架构](docs/ARCHITECTURE.md) | 自研模块、数据、接口、任务和运行边界 |
| [专业协议](docs/API.md) | 26领域的输入对象、操作、结果、跨端消费者与现有接口兼容 |
| [验收](docs/ACCEPTANCE.md) | 真实成果、异常恢复、质量与发布要求 |

[AGENTS.md](AGENTS.md) 只负责工程执行，[CONTRIBUTING.md](CONTRIBUTING.md) 说明协作流程。
完整专业行为见[领域合同](docs/domains/README.md)，前端实施见[FRONTEND-EXECUTION](docs/FRONTEND-EXECUTION.md)。各文件按职责提供完整执行定义；不需回旧规划、矩阵或研究附件拼接做法。[历史署名与许可](docs/reference/README.md)只处理来源权利，原始资料受保护且不参与规则加载。

## 新目录

```text
app/          Flutter Web / Windows 客户端
server/       Python 产品核心、API 与后台工作进程
website/      Astro 官网，展示与真实入口
assets/       原创身份资产与获准公共素材
docs/         唯一产品规格、26领域合同、设计依据、架构与验收
研究资料/     忽略提交：研究原件、来源结构与逐件保全映射
scripts/      构建、验证、运行与维护工具
tests/        跨模块验证；模块测试随所属源码维护
.github/      PR 流程与质量检查
.agents/      适用于本仓库的开发技能与原许可
.codex/       当前根开发入口、任务续接与有范围的技能钩子
```

工作、监测、资料、成果组织连续工作；主体业务与专业编辑有完整可发现入口，导航不是范围上限。自研采集与受控浏览器共用一套来源、任务、
授权和恢复机制。用户自行授权自己的平台账号，不继承开发者身份或地区限制。
基础工作可无 AI 运行；AI 增强和专业媒体制作分别验收。

## 检查本次结构

```powershell
Set-Location D:\寰意
& '.\.venv\Scripts\python.exe' scripts/verify_repository.py
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
```

上述检查只提供其实际覆盖的源码、结构与合同证据。产品发布还须独立完成构建、
权限、恢复和真实操作验收，不能把仓库检查通过当成完整产品可用。

## License

原有 [Apache-2.0 LICENSE](LICENSE) 保持原文，不因品牌或架构重构变更。
第三方代码、字体、素材和模型保留独立许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

---

OEVIRA is a self-developed professional intelligent workspace for media,
advertising, brand marketing and commercial operations. Product specifications,
source, runtime behavior and release acceptance are verified separately.

## 内容生态与前端实施

用户与官方在寰意内的短文、图文、多图、视频、播客及系列作品上传/发布、发布后的对外分享、视频创意和方案模板、经验共享已经纳入完整规划；公共发布快照与私人作品隔离，许可、审核和模板复用均需真实实现。原26专业领域及全部交付要求保留。详见[完整规格](docs/PRODUCT-SPEC.md)、[API](docs/API.md)与[验收](docs/ACCEPTANCE.md)。

此前ChatGPT视觉样稿被PO否定，不作为实现或截图基线。新的原创视觉和交互按[前端实施步骤](docs/FRONTEND-EXECUTION.md)执行；该文件从属于[DESIGN](DESIGN.md)，不另建产品总规划。规划入库、结构检查与产品/视觉完成分别判断。
