<p align="center">
  <img src="docs/assets/wrench-mascot.svg" alt="Wrench Board mascot" width="160" />
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.fr.md">Français</a> ·
  <strong>中文</strong> ·
  <a href="README.hi.md">हिन्दी</a>
</p>

# Wrench Board

> 面向板级电子维修的智能体原生诊断工作台，由 Claude Opus 4.8 驱动。**维修权，公开开发，由真正动手维修的人打造。**

🥈 在 Anthropic 的 *Build with Opus 4.7* 黑客马拉松中荣获**第二名**，2026 年 4 月。

**📺 演示视频（3 分钟）：** https://youtu.be/OZ2D_p82z6w

![Wrench Board：在 MNT Reform 主板上运行的点位图与诊断智能体](docs/assets/screenshot-workbench.png)

## 它是什么

每年有数千万吨电子产品最终沦为电子垃圾。其中很大一部分在板级是可以修复的，比如一颗损坏的电容、一个击穿的二极管、一颗坏掉的 PMIC，但只有微焊接技师才能找到并修复它们。我们是垃圾填埋场之前维修的**最后一公里**，而我们这样的人并不多。

Wrench Board 是为这最后一公里打造的资深微焊接搭档。对于经验丰富的技师来说，它是一双永不疲倦的第二只眼。对于学徒来说，它是一位资深搭档，会用他们的语言、配合他们的工具，第十次不带评判地讲解开机时序。它读入一份原理图 PDF 和一份点位图，在两分钟内构建出针对每台设备的知识包，并运行一个 Opus 4.8 诊断智能体，由它在视觉上操控电路板，高亮引脚、追踪网络、模拟故障，而技师则始终手握烙铁。

它与设备无关。给它一份原理图和一份点位图，它在 iPhone 和 MacBook 主板、Android 和三星手机、游戏主机主板、笔记本电脑以及单板计算机上都能同样工作。任何拥有原理图和点位图的东西都不在话下。

我们押注的是**精确胜过魔法**。智能体不被允许凭空捏造一个位号。它说出的每一个位号都源自一次工具查询，而服务端的净化器会在文本到达屏幕**之前**，把任何它无法核实的标记包裹起来。底层的确定性引擎产出可验证的因果链，而不是凭感觉。

## 它为何存在

我做微焊接技师已经三年了。在这段时间里的大部分时候，我都是一张一张手动地把截图发给 Claude，再把答案抄到纸质笔记本上。我打造了我自己需要的这台工作台。

## 它如何工作

四条正交的工作流在 `memory/{slug}/` 下汇入每台设备唯一的磁盘语料库：

- **知识工厂**：四个 Claude 角色（Scout、Registry、Writers、Auditor）在约 2 分钟内从一个设备标签构建出一份经过核实的维修包。三个 Writers（Cartographe / Clinicien / Lexicographe）并行运行，并共享一段缓存预热的前缀，以便在各 Writer 之间摊薄那段很长的共享输入。
- **原理图读取**：Opus 4.8 视觉逐页将一份 PDF 原理图编译为可查询的 `ElectricalGraph`：对网络进行分类、推断开机时序、附上质量报告。
- **诊断智能体**：每台设备一个 Anthropic Managed Agent，配备四存储分层记忆（`global-patterns`、`global-playbooks`、`device-{slug}`、`repair-{repair_id}`），通过 17 个 `bv_*` 工具操控点位图，并通过另外约 27 个工具查询知识包、原理图图谱、测量值、验证结果和技师档案，共计在 `api/agent/manifest.py` 中声明的 44 个自定义工具。智能体从不伪造位号：靠工具纪律加上一道事后净化器。
- **microsolder-evolve**：四个通宵搜索循环，每个对应一个面：确定性模拟器加假设引擎（`sim`）、原理图编译器（`pipeline`）、原理图视觉处理过程（`pipeline-vision`），以及诊断智能体本身（`agent`）。每个循环针对一个 oracle 基准提出补丁，并要么保留它们（以 `evolve:` 为前缀的提交），要么回退。这些循环一直在运行，并在我忙别的事情时持续交付改进。

![Wrench Board：带有知识产物与诊断线程的维修仪表盘](docs/assets/screenshot-dashboard.png)

### 文件 + 视觉：智能体可以请求查看

一次微焊接诊断的成败，取决于探针**此刻**正接触着什么，而一个聊天框无法承载这一点。技师把一台 USB 显微镜或网络摄像头接入工作台，智能体便通过 `cam_capture` 工具按需请求一帧画面，读取图像，并将其反馈回自己的推理之中。技师也可以随时把一张微距照片或一颗可疑芯片的特写丢进聊天里。捕获和上传的内容都会持久化保存在该次维修之下，因此一次会话可以被端到端地回放，包括话语、决策，以及智能体实际看过的那些照片。

这弥合了那条粘贴截图的工作流从来无法闭合的回路：智能体不再**猜测**电路板长什么样，而是开始**看见**它，按技师的指示，用技师的光学设备。

## 内部机理

- **后端**：Python 3.11+ / FastAPI / 原生 WebSocket / Pydantic v2 / pdfplumber。无构建步骤，无打包器。
- **前端**：原生 HTML + CSS + JS，OKLCH 设计令牌，知识图谱使用 D3 v7，点位图使用 Three.js r128（WebGL）。内联 SVG 图标。无框架。
- **模型**：Claude Opus 4.8（重型 pipeline writers、原理图视觉、`deep` 诊断档位）、Claude Sonnet 4.6（Scout、Registry、Mapper、Lexicographe、`normal` 档位）、Claude Haiku 4.5（意图分类器、阶段叙述器、覆盖度门控、`fast` 档位）。
- **记忆**：每台设备的 Anthropic Managed Agents 记忆存储。智能体通过阅读自己的书记官笔记本（`state.md`、`decisions/`、`measurements/`、`open_questions.md`）跨会话自我定位，而不是依赖一份由 LLM 生成的摘要。
- **点位图**：`api/board/parser/` 中的 16 个解析器，按扩展名分派：KiCad `.kicad_pcb`、OpenBoardView Test_Link `.brd`、KiCad-boardview BRD2，外加 `.asc` `.bdv` `.bv` `.bvr` `.cad` `.cst` `.f2b` `.fz` `.gr` `.pcb` `.tvw`。新增一种格式 = 一个新文件。
- **测试**：2 600+ 个快速测试（约 1 分钟），外加一套 `@slow` 精度门控套件，其中包括对模拟器加假设引擎的 10 项确定性不变量，以及冻结 oracle 门控。
- **工具链**：`make doctor` 运行 8 项本地健康检查（环境、知识包、解析器、摄像头），用于工坊部署。`make eval-all` 编排四个评估面（模拟器、pipeline、视觉、智能体），并带有跨技能回归检测。`make tools-inventory` 写出一份本地的 agent-manifest 索引，供离线审阅。
- **反幻觉**：纵深防御，两层。（1）工具对未知位号返回 `{found: false, closest_matches: [...]}`；系统提示词指示智能体从建议中挑选，或向用户询问。（2）`api/agent/sanitize.py` 扫描每一段对外文本中形如位号的标记（`\b[A-Z]{1,3}\d{1,4}\b`），并在任何未经核实的匹配到达技师之前将其包裹为 `⟨?U999⟩`。

两个纯同步的确定性引擎（`simulator.py`、`hypothesize.py`）坐镇诊断栈的核心。模拟器沿开机时序逐阶段推进，并输出一条由失效轨道、失效元件以及各阶段阻断原因构成的时间线。假设器接受一份部分观测，并枚举能够解释它的单故障和双故障位号击杀候选项，按相对观测的 F1 排名。两者在运行时都不调用 LLM。

诊断智能体有两套可互换的运行时：通过 Anthropic Managed Agents 的 **managed**，以及通过 Messages API 的 **direct**。managed 是默认值，也是生产路径；direct 在 MA 测试版不可用时充当回退，并在开发期间作为一个磁盘检查框架。WebSocket 协议是完全一致的，因此前端并不知道正在运行的是哪一个。

## 路线图：社区演进循环

Wrench Board 在本地运行。每位技师的实例都能针对自己的现场案例改进其确定性模拟器。当演进循环发现一条经得起检验的规则时，它会向上游仓库提出一个候选 pull request。维修权，公开开发，由真正动手维修的人打造。

## 快速上手

```bash
git clone https://github.com/Junkz3/wrench-board
cd wrench-board
make install          # create .venv and install deps (incl. [dev])
cp .env.example .env  # then fill in ANTHROPIC_API_KEY
make run              # uvicorn --reload on http://localhost:8000
```

在 Managed Agents 模式（默认）下首次执行 `make run` 时，启动脚本会打印一屏警告，说明它即将在你的 Anthropic 账户上创建什么（1 个 environment + 3 个按档位限定范围的 agent，闲置状态，使用前不产生任何费用），并等待 5 秒以便你按 Ctrl+C，然后才进行引导。这些 ID 会落在 `managed_ids.json`（已 gitignore）中，后续运行会直接进入 uvicorn。

如果你的账户上没有 Managed Agents 测试版，可回退到 direct 模式，无需引导，使用普通的 `messages.create` 工具循环：

```bash
make demo-fallback
# or: DIAGNOSTIC_MODE=direct make run
```

## 许可与致谢

以专有许可证提供源码可见，详见 [`LICENSE`](LICENSE)。可免费用于个人评估、学习和本地使用。**独立电子维修专业人士在为自己的客户提供服务时，也可将其作为内部工具使用**（可接受商业报酬），无需单独的许可。再分发、托管的 SaaS 部署、再授权，以及任何用于训练竞争性 AI / ML 模型的用途，仍需书面许可（联系方式：alexis@repairmind.co.uk）。依赖项仅限 MIT / Apache 2.0 / BSD。作为标准测试目标使用的 MNT Reform 主板采用 CERN-OHL-S-2.0。由一人在 Repair Valley（一家独立电子维修工坊）独立打造。

## 贡献

Wrench Board 欢迎所有关心维修权的贡献者。现场报告、新的点位图解析器、模拟器规则，欢迎提 issue 或 PR。
