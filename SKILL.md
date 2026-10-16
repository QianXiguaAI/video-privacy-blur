---
name: video-privacy-blur
description: 在本地给录屏或视频中的姓名、手机号、邮箱、证件、银行卡、订单、验证码及付款码做模糊或纯色遮挡，支持稳定跟踪、断点续扫、截图定位和按时间段修改。不用于普通滤镜、人脸换脸或音频消音。
---

# 视频隐私处理

本地使用 Apple Vision、Python 标准库和 FFmpeg，要求 macOS。保留原片，工程和每次输出分开保存。用户已请求处理或修改时直接执行可逆操作；只在处理范围有实质歧义时澄清。

## 开始前

首次使用阅读 [环境检查与补全](references/environment.md)，执行 `bash scripts/setup.sh --check`。缺项时在会话授权内执行 `--install`，验证通过后继续。需要系统安装窗口或管理员操作时说明具体卡点。后续使用报告中验证通过的工具路径。

先确定输入文件和用户指定的隐私类型。查看视频元数据及代表帧，定位聊天、表单、截图、通知和键盘。HDR、奇数显示尺寸等预检问题的处理见环境与工程文档。

## 新视频流程

1. 建立工程、逐帧识别；中断后用相同命令继续，已校验的分块会复用。
2. 按用户范围填写 `policy.json`。姓名和已知值从素材取证；无标签姓名需检查聊天列表、群成员、联系人等位置，再填 `known_names` 或人工框。
3. 运行 plan，查看 `review_findings`。逐处检查疑似错读、边缘截断、过宽的框，以及同屏重复出现的字段。
4. 用 inspect 查看原片。缺漏补字段框；确认是普通文字的弱候选才加局部排除。未覆盖候选会拦住 render，不需要增加导出确认。
5. 导出新文件，运行 verify；默认逐帧检查输出。查看报告并复查关键画面，失败时修正后生成新版本。

```bash
python3 scripts/video_privacy.py init --input /absolute/input.mov --project /absolute/project
python3 scripts/video_privacy.py scan --project /absolute/project
python3 scripts/video_privacy.py plan --project /absolute/project
python3 scripts/video_privacy.py inspect --project /absolute/project --source --at 00:10,00:35
python3 scripts/video_privacy.py render --project /absolute/project --revision r0001 --output /absolute/redacted-v1.mp4
python3 scripts/video_privacy.py verify --project /absolute/project --revision r0001
python3 scripts/video_privacy.py inspect --project /absolute/project --revision r0001 --at 00:10,00:35
```

检查时间应在视频时长内。具体配置、待复查项、模式和区间命令见 [工程与编辑规则](references/editing-rules.md)。

## 需要重点判断的画面

- 输入中的姓名、手机号、半截邮箱，键盘候选、剪贴板建议，以及后续引用中的同一信息。
- 小字截图、滚动边缘、通知与无标签姓名。只发现部分字段不代表其余字段已处理。
- 付款码页上的二维码、一维码及数字。普通商品条码只作候选，结合画面确认用途。
- 静止、滚动启停、切镜和相邻字段：框应稳定且完整覆盖，避免扩大到普通对话或整块键盘。

默认高斯模糊，支持按类型配置纯色遮挡；尊重用户指定效果与类型。默认简体、繁体和英语 OCR，实际语言支持由当前 Vision 检查。

## 修改已有视频

先找到原片、工程和基础 revision，复用缓存，只改指定区间。时间采用半开区间；截图需先匹配视频时间并映射到有效画面坐标。读取 [编辑规则](references/editing-rules.md) 后使用 revise，保留原版本。

恢复内容需要原片和规则；压平的遮挡视频不能承诺恢复。升级技能不会自动改变已有视频。

## 验收与交付

- verify 默认全帧 OCR，检查报告的 `ocr_complete`、覆盖、时间轴、尺寸和音轨结果。退出码 2 表示发现问题，需要处理。
- 正数 `--sample-seconds` 仅为快速抽查，`--skip-ocr` 仅为专项回归；两者不能当作完整验收。
- 用户指出漏码或抖动时，定位实际 PTS，逐帧查看附近过程，并直接复查原来的问题帧。
- 核心覆盖和全帧 OCR 仍不能证明所有隐私都被识别，保留视觉复查。音轨默认保留，不声称已处理说出的隐私。

交付新视频的绝对路径链接，说明处理类型、效果、时间规则和实际验证范围。原片、OCR、截图及工程留在用户本地，不放进技能包。

## 维护

修改脚本后运行 `python3 scripts/self_test.py`；涉及识别、跟踪或导出时再执行 `--e2e --workdir /absolute/empty-folder`，使用虚构素材。实现约束和回归范围见 [实现与验证](references/implementation.md)。打包使用 `scripts/package_skill.py --output /absolute/video-privacy-blur.zip`，排除缓存和媒体。
