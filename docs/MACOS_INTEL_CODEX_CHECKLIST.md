# Intel Mac Codex 安装与演示执行清单

更新时间：2026-09-01

## 1. 任务目标

在 2020 Intel MacBook Pro 上，从私人 GitHub 仓库克隆 AI Scoring System AI 评分系统，完成依赖安装、离线演示启动和本机验收。

目标仓库：

```text
chenbaitao88-ctrl/buddy-claw-ai-scoring-system
```

本清单只适用于：

- Intel Mac，`uname -m` 返回 `x86_64`。
- macOS Sequoia 15.x。
- 8GB 或以上内存。
- 首次安装时可以连接互联网。

Apple Silicon（M1/M2/M3/M4，`arm64`）暂不在本清单范围内，不要绕过架构检查。

## 2. 安全边界

Mac Codex 执行前必须遵守：

1. 克隆后先读取项目根目录 `AGENTS.md`。
2. 不创建或读取真实 `.env`。
3. 不配置模型 API Key，不调用在线模型。
4. 不导入真实学生信息、作品、报名表、网盘链接或数据库。
5. 不修改业务代码，不提交，不 push，不创建标签或 Release。
6. 不删除、覆盖已有项目目录。
7. 不结束未知进程；端口被占用时改用其他端口。
8. Apple 命令行工具、Homebrew、管理员密码或网页授权需要人工操作时，暂停并提示用户。
9. 任一步骤失败时立即停止，保留原始错误，不修改业务代码绕过。

## 3. 需要安装的软件

必须具备：

- Apple Command Line Tools：提供 Git 和基础编译环境。
- Homebrew：安装 Mac 命令行软件。
- GitHub CLI：访问私人 GitHub 仓库。
- Python 3.11。
- Node.js 22.12 或更高的 22.x 版本。
- npm：随 Node.js 安装。

项目安装脚本会自行处理：

- 项目内 `.venv`。
- 项目内锁定版本的 `uv`。
- Python 项目依赖。
- 前端 npm 依赖。
- 前端生产构建。

演示不需要：

- Docker、MySQL、PostgreSQL、FFmpeg。
- 真实模型 API Key。
- 真实学生数据。
- 全局 Python 包或全局 npm 包。

## 4. 设备预检

执行：

```bash
uname -m
sw_vers -productVersion
df -h /
```

通过标准：

- 架构必须是 `x86_64`。
- macOS 为 15.x。
- 建议至少有 10GB 可用空间。

如果架构不是 `x86_64`，立即停止并回传，不要修改 `setup_mac.sh`。

## 5. Apple Command Line Tools

检查：

```bash
xcode-select -p
git --version
```

如果 `xcode-select -p` 失败，执行：

```bash
xcode-select --install
```

系统会弹出安装窗口。让用户人工确认并等待安装完成，然后重新执行：

```bash
xcode-select -p
git --version
```

## 6. Homebrew

检查：

```bash
brew --version
```

如果 Homebrew 不存在，执行官方安装命令：

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

出现密码或确认提示时交由用户处理。Intel Mac 安装完成后执行：

```bash
eval "$(/usr/local/bin/brew shellenv)"
brew --version
```

不要擅自修改用户的 shell 配置文件。

## 7. 安装 GitHub CLI、Python 和 Node.js

只安装缺失项：

```bash
brew install gh python@3.11 node@22
```

`node@22` 是独立版本，在当前终端加入路径：

```bash
export PATH="$(brew --prefix node@22)/bin:$PATH"
```

核验：

```bash
gh --version
python3.11 --version
node --version
npm --version
git --version
```

通过标准：

- Python 为 `3.11.x`。
- Node.js 不低于 `22.12`，且保持 22.x。
- GitHub CLI、npm、Git 均可运行。

不要单独安装全局 `uv`。

## 8. 登录 GitHub

检查：

```bash
gh auth status
```

如果尚未登录，执行：

```bash
gh auth login --hostname github.com --git-protocol https --web
```

浏览器打开后，暂停并提示用户完成授权。授权完成后核验：

```bash
gh auth status
gh api user --jq '.login'
```

账号必须显示：

```text
chenbaitao88-ctrl
```

## 9. 克隆私人仓库

使用独立目录：

```bash
mkdir -p "$HOME/AI Scoring System"
cd "$HOME/AI Scoring System"
```

先检查目标目录不存在：

```bash
test ! -e buddy-claw-ai-scoring-system
```

如果目录已经存在，立即停止，不覆盖、不删除。

克隆并进入仓库：

```bash
gh repo clone chenbaitao88-ctrl/buddy-claw-ai-scoring-system
cd buddy-claw-ai-scoring-system
```

核验：

```bash
git remote -v
git branch --show-current
git status --short
git rev-parse --short HEAD
```

通过标准：

- 远端为 `chenbaitao88-ctrl/buddy-claw-ai-scoring-system`。
- 分支为 `main`。
- `git status --short` 没有输出。

## 10. 读取项目规则

依次读取：

```bash
cat AGENTS.md
cat docs/MACOS_INTEL.md
cat docs/RUNBOOK.md
cat docs/MACOS_INTEL_CODEX_CHECKLIST.md
```

只读取，不修改。

## 11. 安装项目依赖并构建

确保 Node 22 在当前终端中可用：

```bash
export PATH="$(brew --prefix node@22)/bin:$PATH"
```

执行：

```bash
chmod +x scripts/setup_mac.sh
chmod +x scripts/start_demo.command
chmod +x scripts/stop_demo.command
chmod +x scripts/accept_demo.sh
bash scripts/setup_mac.sh
```

该脚本应当：

- 检查 Intel Mac。
- 检查 Python 3.11。
- 检查 Node.js 22.12+。
- 创建项目内 `.venv`。
- 安装锁定的 Python 依赖。
- 执行前端 `npm ci` 和构建。
- 不启动服务。
- 不读取 `.env`。

安装后检查：

```bash
test -x .venv/bin/python
test -f frontend/dist/index.html
```

## 12. 启动离线演示

默认使用端口 8000：

```bash
bash scripts/start_demo.command
```

成功后打开：

```bash
open http://127.0.0.1:8000
```

如果 8000 已被占用，不结束占用进程，改用 8010：

```bash
BUDDY_CLAW_PORT=8010 bash scripts/start_demo.command
open http://127.0.0.1:8010
```

## 13. 运行本机验收

默认端口：

```bash
bash scripts/accept_demo.sh
```

如果使用 8010：

```bash
BUDDY_CLAW_PORT=8010 bash scripts/accept_demo.sh
```

验收必须确认：

- `/health` 返回健康状态。
- 前端首页可以打开。
- 队伍、作品和评分统计接口正常。
- 存在 12 条 `DEMO-*` 虚构案例。
- 没有使用真实数据。
- 没有调用外部模型。

## 14. 人工页面检查

在浏览器中至少打开：

- 团队管理。
- 作品管理。
- 批量评分。
- 人工评分。
- 复核队列。
- 结果导出。

只浏览和演示，不导入真实文件，不启动真实模型评分。

## 15. 停止服务

演示结束执行：

```bash
bash scripts/stop_demo.command
```

不要使用 `killall`，不要按端口批量结束进程。

## 16. 日常使用

以后每次演示只需：

```bash
cd "$HOME/AI Scoring System/buddy-claw-ai-scoring-system"
bash scripts/start_demo.command
```

打开：

```text
http://127.0.0.1:8000
```

演示结束：

```bash
bash scripts/stop_demo.command
```

## 17. 最终回传格式

执行完成后回传：

```json
{
  "status": "completed_or_blocked",
  "machine": {
    "architecture": "x86_64",
    "macos": "",
    "python": "",
    "node": "",
    "npm": "",
    "gh": ""
  },
  "github": {
    "account": "chenbaitao88-ctrl",
    "repository": "chenbaitao88-ctrl/buddy-claw-ai-scoring-system",
    "branch": "main",
    "commit": ""
  },
  "setup": {
    "dependencies_installed": false,
    "frontend_built": false
  },
  "runtime": {
    "url": "",
    "health": "",
    "demo_case_count": 0,
    "accept_demo_passed": false
  },
  "security": {
    "real_data_imported": false,
    "api_key_configured": false,
    "source_modified": false,
    "push_performed": false
  },
  "blocker": null
}
```

只有安装、启动、12 条虚构案例和验收全部通过时，状态才能写为 `completed`。
