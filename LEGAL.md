# 法务与合规注意事项（Legal & Compliance Notice）

> 本文件是 ATRI-X（Python 版）的**使用边界、法律义务提示与免责声明**，**不是**许可协议——版权许可见
> [LICENSE](LICENSE)（Apache-2.0）。本文不构成法律意见；涉及具体业务的合规判断，请咨询具备资质的
> 法律专业人士。
>
> 最近更新：2026-10-28　|　适用范围：本仓库源码、文档、发行包（安装版 / 便携版 / 源码归档）及容器镜像。

---

## 一、项目定位

ATRI-X（本仓库）是 Go 项目 [ATRI-X](https://github.com/fujiwarashigure/ATRI-X) 的 Python 全量重写，
**仅供学习、技术研究、个人实验与自托管探索使用**。

- **非官方**：本项目不是腾讯 / QQ / Telegram / NapCat / SnowLuma / DeepSeek / 任何模型、搜索、生图、
  语音服务商的官方产品或合作项目；与其**没有**隶属、代理、授权、赞助或背书关系。
- **非商业服务**：作者与贡献者不以本项目提供商业服务，不承诺 SLA、技术支持或商业授权。Apache-2.0
  允许你自行商用，但商用前的合规、备案、许可、内容审核与责任由**你自行承担**。
- **不提供规避能力**：本项目不提供、也**不得被用于**破解、绕过或对抗任何平台的技术措施、风控、
  实名 / 年龄校验、内容审核或收费机制。

## 二、你必须遵守的法律法规

使用本项目时，你应自行确保行为符合**你所在司法辖区**以及**服务实际发生地**的法律法规。以中国大陆
为例（不构成穷尽列举，也不代表其他地区没有类似要求）：

- 《中华人民共和国网络安全法》《数据安全法》《个人信息保护法》；
- 《生成式人工智能服务管理暂行办法》《互联网信息服务深度合成管理规定》《网络信息内容生态治理规定》；
- 《民法典》（人格权、隐私权、名誉权、肖像权）、《广告法》《反电信网络诈骗法》《未成年人保护法》
  以及《刑法》相关条款；
- 涉及新闻信息、网络文化、出版、医疗、金融、教育等特殊领域的行政许可与备案要求（例如面向公众提供
  生成式 AI 服务可能需要相应备案 / 安全评估）。

### 明确禁止的用途（包括但不限于）

- 诈骗、赌博、洗钱、传销、非法集资等违法犯罪活动；
- 制作、传播违法违规信息：淫秽色情、暴力恐怖、极端主义、政治谣言、虚假信息、侮辱诽谤、
  侵犯知识产权等；
- 侵犯他人隐私与个人信息：未经同意收集 / 使用 / 买卖他人信息，人肉搜索，偷拍偷录，
  爬取非公开数据；
- 骚扰、辱骂、恶意骚扰、群发广告 / 垃圾信息、刷屏、恶意 @、批量加好友 / 拉群等；
- 绕过平台风控与账号体系：多开养号、批量注册、代实名、绕过登录 / 付费 / 限流 / 审核，
  或伪造在线状态实施欺诈；
- 冒充他人或机构（含冒充官方账号、客服、公检法）实施欺骗；
- 未经授权自动化操作他人账号或系统，或对第三方服务实施压力测试 / 攻击；
- 其他违反法律法规、公序良俗或侵害第三方合法权益的用途。

## 三、你必须遵守的平台协议与第三方条款

本项目通过公开接口或**非官方协议端**接入平台，平台协议优先于本项目文档：

- **QQ / 腾讯**：《QQ 用户协议》《腾讯服务条款》、QQ 机器人 / 开放平台相关规则与《QQ 隐私政策》。
  使用非官方协议端（NapCat、SnowLuma）**可能违反平台规则**，并可能导致**账号被限制、封禁或承担
  法律责任**，风险自负。
- **Telegram**：Telegram Terms of Service、Bot API 条款与当地法律。
- **NapCat**：以 NapCat 项目自身的 LICENSE 与文档为准；发行包内置的 NapCat 未修改其代码，版权归
  NapCat / NapNeko 项目作者，使用须遵守其许可与平台规则。
- **SnowLuma**：其 EULA（§5.4 限制"并入第三方安装包 / 镜像 / 自动化脚本部署"）与隐私政策。
  本仓库 / 发行包**不内置、不再分发** SnowLuma，须由你自行安装并**自行阅读同意**其 EULA；
  `SNOWLUMA_ACCEPT_EULA` 等无人值守同意方式属于你的自主决定，请自行确认合规。
- **模型 / 搜索 / 生图 / 语音等服务**：DeepSeek、OpenAI 兼容中转站、Tavily、Firecrawl、SauceNAO、
  ASR / TTS 等 API 的服务条款、计费规则与内容政策；你的 API Key 使用行为受对应服务商约束。
- **GitHub / 容器镜像**：GitHub Terms of Service、ghcr.io 使用条款与开源许可。

平台协议与本项目文档冲突时，**以平台协议与法律规定为准**；本项目不构成对任何平台协议的豁免或替代。

## 四、账号、凭据与费用

- 你需要**自备** QQ / Telegram 账号、模型与第三方服务 API Key，并自行承担费用、配额与计费风险。
- 凭据安全由你负责：不要把真实 Key、令牌、Cookie、二维码登录态提交到公开仓库、截图、群聊或 Issue；
  建议使用**独立小号**进行测试。
- 不得使用他人账号或未经授权使用他人凭据；不得共享、转售、代充或出借账号用于本项目。
- 账号风控、封禁、冻结、找回失败、财产损失等后果由你自行承担。

## 五、内容、数据与隐私

- **内容责任**：你对通过本项目输入、生成、存储、发布的一切内容负责。AI 生成内容可能不准确、
  有偏见或侵权，请自行审核后再使用。
- **深度合成标识**：在适用法律要求时（如《互联网信息服务深度合成管理规定》《生成式人工智能服务
  管理暂行办法》），你应依法对 AI 生成 / 合成内容进行显著标识，不得利用其制作、传播虚假信息或
  冒充他人。
- **个人信息保护**：群聊 / 私聊场景下处理他人信息须有合法性基础并履行告知义务；不得采集、存储、
  分析他人敏感个人信息（身份证号、生物识别、行踪轨迹、健康、金融账户等）。建议在群内明示机器人
  的存在与用途，并提供退出 / 删除渠道。
- **数据存储**：会话、记忆、媒体、日志默认保存在本机（配置目录 / `data/`），请自行做好备份、访问
  控制与加密。除你主动配置的模型 / 搜索 / 生图等 API 外，本项目不会把数据上传到项目作者处；
  第三方 API 的数据处理以其隐私政策为准。
- **日志与脱敏**：日志可能包含消息内容、URL、文件路径与凭据片段；对外分享日志 / Issue 前请自行脱敏。
- **内容安全模块只是辅助**：内置 `[safety]` 词表拦截与敏感信息脱敏是降低风险的技术手段，**不保证**
  拦截全部违规内容，不能替代你的合规审查义务。
- **未成年人**：不得利用本项目侵害未成年人权益；未满 18 周岁应在监护人同意与指导下使用。

### 科研研究助理（1.1.3）的额外说明

- **原创实现**：随包研究引擎（`research_pack/engine/`）与 15 个 AgentSkill 是本项目的
  原创代码与文案，**不包含**上游 `academic-research-skills`（CC BY-NC 4.0）的任何文本或代码；
  调研阶段只参考了其工作流思路（思路本身不受版权保护），未复制表达。可复现的相似度核查
  结果随发行物留档（逐字节重复文件 0、CJK 8-gram 命中 0、正文最长公共片段 9 词、
  上游内部标识符 0、版权声明头 0）。
- **数据源条款**：文献检索与核验会访问 OpenAlex、Crossref、DataCite、arXiv、PubMed、
  Semantic Scholar 等公开 API。它们各有使用条款、速率限制与署名要求；请不要绕过限速、
  不要批量抓取、不要把这些服务的数据整体再分发。引擎已内置最小请求间隔、缓存与
  `contact_mailto` 配置，仍请你按各服务条款使用。
- **第三方库**：PDF 导出 / 全文抽取使用可选的 `reportlab` 与 `pypdf`（宽松许可，随冻结版
  一起分发，版权归各自权利人；详见发行包 `第三方组件说明.md`）。不安装时引擎降级为
  Markdown / HTML / DOCX 并在结果里显式声明。
- **学术诚信（重要）**：本功能是**辅助工具**，不是代写工具。使用 AI 参与研究与写作时，
  你必须遵守所在学校、期刊、基金与出版机构关于 AI 使用的规定（是否允许、如何声明、
  署名与责任归属）；**不得**用它抄袭、伪造数据、伪造引用或规避查重。引擎的 DOI 核验与
  引用比对只能降低错误概率，**不能**保证文献真实、结论正确或符合目标期刊要求——
  投稿前请逐条人工核验。
- **生成内容的责任**：研究产物（综述、报告、评审意见等）由你审阅、修改并承担最终责任；
  直接以 AI 生成内容投稿可能构成学术不端，请自行判断并披露。

### 图表能力（1.1.5）的额外说明

- **随包组件**：图表能力基于上游 [drawio-skill](https://github.com/Agents365-ai/drawio-skill)
  3.4.2（**MIT**，作者 Agents365-ai）与 draw.io 桌面版 32.6.1（**Apache-2.0**，JGraph）。
  draw.io 桌面版是 Electron 应用，随包还包含 Electron / Chromium 及其第三方组件，
  其许可证文本随产物放在 `_internal/drawio_runtime/`（`LICENSES.chromium.html`、
  `LICENSE.electron.txt`）；shape 索引来自 jgraph/drawio-mcp 的官方 shape 检索索引
  （Apache-2.0，见 `drawio_pack/.../data/SHAPE-INDEX-NOTICE.md`）。
- **图表内容会外发**：生成的 PNG / SVG / PDF / `.drawio` 会随聊天消息发出（图片进媒体
  中心）。`build` 动作会把源码目录 / IaC / SQL / API schema 里的名称、标签、属性写进图，
  **导入前请确认这些内容可以外发**；不要把密钥、真实内网地址、个人信息写进节点标签或
  properties。仅超级管理员可调用，但这不改变"内容会离开本机"的事实。
- **生成结果的责任**：图的结构来自源码解析或模型推断，可能不完整或有误；作为架构评审
  材料时应由人复核，不要把自动生成的图当作运行事实。

## 六、第三方组件、商标与许可

- 本项目以 **Apache-2.0** 发布（见 [LICENSE](LICENSE)），**不覆盖**随包分发或可选的第三方组件
  （NapCat、便携 QQ、Node.js / DSH 内核、CPython、Pillow、SnowLuma、draw.io 桌面版 / Electron /
  Chromium、drawio-skill 等）。第三方组件的版权与许可归各自权利人，使用前请阅读其许可证与
  EULA（详见发行包内的 `第三方组件说明.md`）。
- **商标**：ATRI-X、QQ、腾讯、NapCat、SnowLuma、DeepSeek、Telegram、OpenAI 等名称与标识归各自
  权利人所有；本项目仅作事实性指称，不表示任何授权、赞助或关联。
- 本项目不包含破解、去广告、绕过授权或规避数字版权管理（DRM）的功能；如你自行修改用于此类目的，
  与本项目作者无关。

## 七、免责声明（AS IS）

- 本项目按 **"现状"（AS IS）** 提供，不附带任何明示或默示担保，包括但不限于适销性、特定用途
  适用性、无缺陷、不侵权、安全性、可用性与持续维护。
- 在法律允许的最大范围内，作者与贡献者**不对**你或任何第三方因使用 / 无法使用本项目而产生的
  任何直接或间接损失承担责任，包括但不限于：账号封禁 / 被盗、数据丢失或泄露、业务中断、利润损失、
  设备损坏、第三方索赔、行政处罚或刑事责任。
- 使用本项目即表示你理解并接受：**风险由你自行承担**。
- 本项目可能随时变更、暂停或停止维护，且不承诺向后兼容。

## 八、责任与赔偿

如你违反本文件、法律法规或第三方协议，导致作者 / 贡献者或任何第三方遭受索赔、损失或费用
（含合理的律师费），你应自行承担并使其免受损害。作者 / 贡献者有权拒绝为违规使用者提供支持、
接受其贡献或终止相关服务。

## 九、与 LICENSE 的关系

本文件是**使用指引、法律义务提示与风险声明**，不构成对 [LICENSE](LICENSE)（Apache-2.0）的补充、
修改或额外许可限制；就版权许可事项，以 LICENSE 为准。但请注意：**许可证授予的是版权许可，不能
免除或替代你应遵守的法律法规、平台协议与第三方权利**。

## 十、举报与联系

- 发现本项目被用于违法或侵权用途，或发现仓库内存在不应公开的敏感信息（凭据、个人信息等），
  请通过 GitHub Issue 或仓库维护者联系方式告知，我们会在合理范围内处理。
- 安全问题请勿公开披露利用细节，建议先私下联系维护者。

## 十一、生效与更新

- 本文件自发布之日起生效，最近更新：**2026-10-28**。
- 我们可能随项目演进更新本文件；更新后继续使用视为接受最新版本，重大变更会记录在
  [CHANGELOG.md](CHANGELOG.md)。

---

## English Summary

ATRI-X is a community, source-available reimplementation provided **for learning, research and
personal / self-hosted experimentation only**. It is **not affiliated with, endorsed by, or
authorized by** Tencent / QQ, Telegram, NapCat, SnowLuma, DeepSeek or any other platform or API
provider. You are solely responsible for complying with all applicable laws (e.g. China's
Cybersecurity Law, Data Security Law, Personal Information Protection Law, the Interim Measures for
the Management of Generative AI Services, and deep-synthesis labelling rules), with platform terms
(QQ / Telegram terms and third-party EULAs, including SnowLuma's), and for any content you process
or generate. Do not use it for fraud, harassment, privacy violations, illegal content, bot farming,
or circumventing platform controls. The software is provided **"AS IS"**, without warranty of any
kind; to the maximum extent permitted by law, the authors accept no liability for account bans, data
loss, legal claims or any other damages arising from its use. This notice complements — and does not
modify — the Apache-2.0 [LICENSE](LICENSE). The Chinese text above is the authoritative version.
