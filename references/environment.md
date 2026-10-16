# 环境检查与补全

首次使用、工具缺失或编译失败时阅读。先补环境，再恢复原来的打码任务；不要重新建立已有工程。

## 入口与状态

在 Skill 根目录运行，不要求预先存在 Python：

```bash
bash scripts/setup.sh --check
```

- `--check`：不安装软件、不打开系统安装窗口；检查已有依赖，通过后运行完整能力验证。验证会在临时目录编译程序、生成虚构短片并清理。
- `--install`：补装缺项，然后执行同样的完整验证。已有可用依赖不重复安装，不批量升级环境。
- 返回 `0`：完整验证通过，可以继续；`2`：依赖缺失、系统安装待完成或能力验证未通过；`1`：安装、下载或其他执行错误。保留实际错误，不把非零返回当作成功。

用户已要求打码或补环境时，在当前会话权限内直接运行 `--install`；不增加逐项确认。遵循工具实际权限，不能绕过系统权限、代输密码或代替用户接受 Xcode 许可。

## 补全顺序

1. **操作系统**：先检查 `uname -s`。非 macOS 时说明 Apple Vision OCR 暂不支持，停止这条处理流程；不在其他系统安装 Homebrew，也不换成云端 OCR。
2. **开发工具**：检查 `xcode-select -p`、`xcrun --find swiftc` 和 `swiftc --version`，不能只看 `/usr/bin/swiftc` 是否存在。macOS 的启动占位程序不代表工具已安装。
3. **Python 与媒体工具**：实际执行 Python 标准库导入和版本检查，以及 `ffmpeg -version`、`ffprobe -version`。Python 要求 3.9 或更新；仅 FFmpeg 可用但 ffprobe 缺失，也需要补齐。
4. **包管理器**：仅在 Python 或 FFmpeg 缺失时需要 Homebrew。优先发现现有 `brew`，并检查 Apple Silicon 的 `/opt/homebrew/bin/brew` 或 Intel 的 `/usr/local/bin/brew`，处理“已安装但不在 PATH”的情况。
5. **安装与复验**：运行下列命令，只补缺失的 Python、FFmpeg；ffprobe 随 FFmpeg 安装。安装后重新发现路径，执行完整验证，再继续原任务。

```bash
bash scripts/setup.sh --install
```

### 没有开发工具

脚本调用 `xcode-select --install` 请求系统安装，然后返回待完成状态。安装窗口出现不代表安装完成。告诉用户需要完成该系统窗口；之后重新运行同一条 `--install` 命令检查并继续。无需安装完整 Xcode。

已有选中的开发目录但 Swift 不可用时，不反复触发安装。查看编译器报错、SDK、许可及当前 `DEVELOPER_DIR`。有可用工具链时可在当前任务设置 `DEVELOPER_DIR`；不擅自全局切换 `xcode-select` 或接受许可。

### 没有 Homebrew

仅在需要补 Python 或 FFmpeg 时，从 Homebrew 官方安装仓库下载脚本到临时文件，以 `NONINTERACTIVE=1` 执行；不读取密码。管理员操作、网络限制或安装条件导致失败时，明确说明具体卡点和官方安装入口。用户完成所需系统操作后复验。

不使用第三方镜像、非官方一键安装脚本，不自动执行 `brew upgrade`、`brew cleanup` 或修改 `.zshrc`、`.zprofile`。现有依赖全部可用时，不为这个 Skill 额外安装 Homebrew。

### PATH 与重复执行

安装脚本只更新自身进程的 PATH，不能改变 Codex 后续命令的父进程环境。读取验证结果中的 `python_path`、`ffmpeg`、`ffprobe`、`swiftc`，使用绝对路径，或在当前任务 shell 临时加载所发现 `brew` 的 `shellenv`。

只有基本检查通过、完整检查失败时，应先按 `issues` 定位能力或 SDK 问题。若确认现有 Homebrew FFmpeg 缺少所需功能，按会话授权仅修复或更新该包，再复验；不要循环安装已经存在但当前调用路径错误的工具。

每次失败只针对实际原因修复。网络或权限条件没有变化时不反复重试；保留原片和已有工程，不声称视频处理完成。

## 完整验证

有 Python 后也可单独运行：

```bash
python3 scripts/video_privacy.py doctor --full
```

完整验证会编译真正的 `vision_ocr.swift`，生成无隐私的两帧短片，运行 Apple Vision OCR，再执行当前高斯模糊、动态遮罩、H.264 与 FFV1 导出以及 ffprobe 读取。临时文件随后清理。工具路径存在或安装命令返回成功，都不能替代此检查。

`doctor` 不带 `--full` 时只做基础可执行性检查。两种模式都返回 `ready` 和 `issues`，失败返回 `2`；不会自行安装软件。不要把环境能力验证解释成用户视频的隐私验收。

## 官方资料

- [Apple：安装 Command Line Tools](https://developer.apple.com/documentation/xcode/installing-the-command-line-tools/)
- [Homebrew：安装、非交互模式及 shell 环境](https://docs.brew.sh/Installation)
- [Python：macOS 安装与系统 Python 的区别](https://docs.python.org/3/using/mac.html)
