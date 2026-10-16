# video-privacy-blur

给录屏里的隐私文字做高斯模糊，让模糊区域跟着文字平滑移动。

一个面向 Codex 的 macOS Skill，适合处理产品演示、聊天记录、操作教程中的姓名、手机号、邮箱、订单号和验证码。支持用截图定位，也支持一句话修改某个时间段的打码规则。

```text
用 $video-privacy-blur 给这个视频里的姓名、手机号和邮箱打码。
使用高斯模糊，跟着文字移动。
```

## 能做什么

- **高斯模糊**：默认模糊强度 sigma=20，边缘向外柔化，可按字号和画面调整。
- **平滑跟随**：逐帧定位敏感文字，减少滚动时模糊框的抖动，同时检查当前帧的覆盖范围。
- **局部修改**：按时间段保留、取消或补充某一类打码，不必重新识别整段视频。
- **截图定位**：结合截图、播放器时间和原视频定位目标，再检查前后帧的移动。
- **保留时间轴**：按原始帧时间戳处理，支持可变帧率视频和带旋转信息的视频。
- **版本管理**：保存原片、识别缓存、修改规则和各次导出，方便继续调整。
- **导出校验**：检查帧数、时间戳、显示尺寸、音轨起点、遮罩覆盖，并逐帧检查输出中的隐私候选。

## 环境要求

| 依赖 | 用途 |
| --- | --- |
| macOS | 使用 Apple Vision 进行本地 OCR |
| Python 3.9 或更新版本 | 工程、规则、跟踪和校验；运行脚本只依赖标准库 |
| FFmpeg、ffprobe | 解码、读取时间轴、模糊和导出 |
| Swift 编译器 `swiftc` | 编译 OCR 辅助程序，可由 Xcode Command Line Tools 提供 |

当前渲染实现已在 FFmpeg 9 环境验证。其他版本可先运行文末的端到端测试。

在仓库根目录检查依赖，即使尚未安装 Python 也可以运行：

```bash
bash scripts/setup.sh --check
```

缺少依赖时执行：

```bash
bash scripts/setup.sh --install
```

它会复用已有工具，只补缺失的 Python、FFmpeg 和开发工具；需要 Homebrew 且尚未安装时，使用官方安装脚本。Command Line Tools 需要在系统窗口完成安装，完成后重新运行同一条命令。涉及管理员操作时会说明失败原因，不读取密码。

安装后会编译 OCR 程序，用无隐私短片验证识别和渲染，通过后才能继续处理。默认检查不安装软件；不全局升级包、不修改 shell 配置。脚本只更新自身 PATH，后续命令应使用报告中的工具路径，或在当前 shell 加载对应 Homebrew 的 `shellenv`。详细逻辑见 [环境检查与补全](references/environment.md)。

## 安装到 Codex

```bash
git clone https://github.com/QianXiguaAI/video-privacy-blur.git
cd video-privacy-blur
skill_dir="${CODEX_HOME:-$HOME/.codex}/skills/video-privacy-blur"
mkdir -p "$skill_dir"
cp -R SKILL.md agents references scripts "$skill_dir/"
```

在 Codex 中调用 `$video-privacy-blur`，并提供本地视频路径。如果当前会话尚未识别到新 Skill，重新启动 Codex 后再试。

## 用自然语言操作

首次处理：

```text
用 $video-privacy-blur 处理 /本地路径/demo.mov。
给姓名、手机号、邮箱、订单号和验证码做高斯模糊，区域跟着文字平滑移动。
保留原片，导出一个新文件。
```

修改已有版本：

```text
基于刚才的版本，00:48 到结束只保留邮箱打码。
```

```text
00:00 到 00:29 取消全部打码，其他时间保持原来的规则。
```

按截图补充：

```text
这张截图对应 01:12，给红框里的邮箱补上模糊。
检查前后滚动时是否也能覆盖。
```

恢复此前取消的打码需要保留原片和工程。只有已经导出的打码视频，无法还原被遮挡的原始文字。

## 命令行使用

以下命令在仓库根目录运行。将输入路径换成自己的视频，工程建议放在仓库外。

```bash
project_dir="$HOME/Movies/video-privacy-project"

# 建立工程并逐帧识别
python3 scripts/video_privacy.py init --input "$HOME/Movies/demo.mov" --project "$project_dir"
python3 scripts/video_privacy.py scan --project "$project_dir"

# 生成候选打码区域，检查原片关键帧
python3 scripts/video_privacy.py plan --project "$project_dir"
python3 scripts/video_privacy.py inspect --project "$project_dir" --source --at 00:01,00:03

# 确认或修正区域后，导出并校验
python3 scripts/video_privacy.py render --project "$project_dir" --revision r0001
python3 scripts/video_privacy.py verify --project "$project_dir" --revision r0001
python3 scripts/video_privacy.py inspect --project "$project_dir" --revision r0001 --at 00:01,00:03
```

示例检查时间应在视频时长以内。默认输出为工程目录下的 `exports/r0001-gaussian.mp4`。打开 `inspect` 生成的图片检查效果；需要补充姓名、已知值或人工区域时，编辑工程的 `policy.json`，再重新执行 `plan`。

将 00:48 到结束改成只保留邮箱打码：

```bash
python3 scripts/video_privacy.py revise --project "$project_dir" --base r0001 --start 00:48 --end end --keep email
python3 scripts/video_privacy.py render --project "$project_dir" --revision r0002
python3 scripts/video_privacy.py verify --project "$project_dir" --revision r0002
```

`revise` 每次生成新版本，以上 `r0002` 假设此前只有 `r0001`；实际版本号以命令输出为准。时间区间采用 `[开始, 结束)`，后加入的规则覆盖相交区间内对应的旧规则。

支持的类型：

| 类型 | 内容 |
| --- | --- |
| `email` | 邮箱 |
| `phone` | 手机号、部分座机和国际号码格式 |
| `name` | 带姓名等标签的中文姓名字段，或配置的已知姓名 |
| `id`、`card` | 证件、银行卡及上下文中的部分尾号 |
| `order` | 订单、保单等编号 |
| `verification` | 验证码、动态码 |
| `payment_code` | 付款上下文中的二维码、一维码和长数字；未知用途的码需复查 |
| `custom` | 人工指定区域 |

识别依赖文字格式、标签和上下文，类型不等于识别保证。完整配置、人工框坐标和修改语义见 [工程与编辑规则](references/editing-rules.md)。

## 帧率与跟踪

默认保留源视频的每帧显示时间。OCR 结果、时间段规则和模糊框都对齐到原始时间戳；平滑处理调整每帧模糊框的坐标。

平滑后的区域必须包含当前帧需要遮挡的范围，避免跟随滞后露字。发生位置骤变时会重建轨迹，短暂 OCR 缺帧只在满足时间和位置条件时补齐。

导出后检查总帧数和逐帧时间，时间偏差要求小于 2 毫秒。输出 OCR 默认逐帧检查；正数 `--sample-seconds` 仅用于快速抽查，报告会标记为不完整验收。

## 处理范围与隐私

视频解码、OCR 和渲染由本地脚本完成，脚本不调用云端 OCR 或上传接口。Codex 的模型运行方式由使用环境决定。

工程缓存会保存识别到的原文和检查截图，属于私人素材。发布仓库时不要包含自己的视频、工程目录或识别缓存。

使用时需要了解这些限制：

- 自动识别可能漏掉小字、无标签姓名、半截输入和键盘候选栏；正式发布前需要视觉复查，并按需补框。
- 覆盖校验说明已规划的区域被完整模糊，输出逐帧 OCR 也可能发现遗漏；两者都不能证明视频已完全匿名化。
- 默认保留音轨，不处理说出的隐私；人脸、车牌也不在当前检测流程内。
- HDR、奇数显示尺寸和异常时间戳会停止处理，需要先确定转换或裁剪方案。
- 输出为 H.264 MP4，会重新编码。AAC 音轨直接保留，其他音频编码转为 AAC。
- 逐帧 OCR 和渲染耗时随视频长度、分辨率及机器性能增加；扫描和完整输出 OCR 默认按 30 秒保存检查点，中断后重跑相同命令可续扫。

## 开发与测试

行为测试：

```bash
python3 scripts/self_test.py
```

包含依赖缺失、系统安装待完成、仅补缺项、重复运行复用和安装失败等环境分支；安装分支使用替身工具，不会安装系统软件。

端到端测试使用生成的虚构素材，覆盖移动文字、半截输入、时间段修改、音轨、可变帧率和旋转视频：

```bash
test_dir="$(mktemp -d)"
python3 scripts/self_test.py --e2e --workdir "$test_dir"
```

请用虚构信息提供测试素材和问题复现，避免将真实隐私放进 Issue、PR 或测试夹具。

## 仓库结构

```text
video-privacy-blur/
├── README.md
├── SKILL.md                     Codex 执行说明
├── agents/openai.yaml           Skill 展示信息
├── references/
│   ├── editing-rules.md         工程配置与编辑规则
│   └── environment.md           环境补全与失败处理
└── scripts/
    ├── video_privacy.py         工程、规则、跟踪、渲染和校验
    ├── setup.sh                 无需 Python 的环境检查与安装入口
    ├── vision_ocr.swift         Apple Vision 文字与码区识别
    ├── self_test.py             行为与端到端测试
    └── make_test_images.swift   虚构测试素材生成
```

## 新配置与分发

默认保持高斯模糊，`mode` 和 `type_modes` 可选择 `solid` 不透明黑色。识别默认启用简体、繁体及英语。静止区域锁框、运动区域平滑拟合，相关配置见 [编辑规则](references/editing-rules.md)，实现约束见 [实现与验证](references/implementation.md)。

生成不含字节码、媒体和 OCR 缓存的源码包：

```bash
python3 scripts/package_skill.py --output /absolute/video-privacy-blur.zip
```

## 许可

[MIT](LICENSE)
