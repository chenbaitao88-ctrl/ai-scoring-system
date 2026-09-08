# Intel Mac 使用说明

## 面向设备

本说明针对 2020 Intel MacBook Pro、macOS 15.7.9、8GB 内存。它不是 Apple Silicon 的安装说明；`setup_mac.sh` 会在非 `x86_64` 机器上停止。

## 准备工作

请先人工安装：

- Python 3.11，并确认终端能运行 `python3.11 --version`。
- Node.js 22.12 或更高版本，并确认终端能运行 `node --version` 和 `npm --version`。

可以使用官方安装包或 Homebrew。安装这些基础软件时可能需要管理员密码和网络；项目脚本不会替你修改系统配置。

## 安装

打开终端，进入项目文件夹后执行：

```bash
chmod +x scripts/setup_mac.sh scripts/start_demo.command scripts/stop_demo.command scripts/accept_demo.sh
bash scripts/setup_mac.sh
```

首次安装会下载依赖，8GB 内存设备建议关闭浏览器中不需要的标签页。安装结束后脚本不会启动服务。

## 日常三步

1. 双击 `scripts/start_demo.command`，或在终端执行 `bash scripts/start_demo.command`。
2. 浏览器访问 <http://127.0.0.1:8000>。
3. 使用完毕后执行 `bash scripts/stop_demo.command`。

需要确认环境时，在服务运行期间执行 `bash scripts/accept_demo.sh`。它只访问本机，不需要互联网。

## 演示内容和安全边界

默认是带 12 个纯虚构案例的离线演示：页面可以打开，列表、统计和复核状态均可讲解，数据库在项目内的 `runtime/demo/data/`。不要把真实学生信息、作品压缩包、手机号、截图、音视频、云盘链接或 API 密钥复制到该目录。

启动过程不会读取外部 `.env`，不会自动调用在线模型，也不会按端口关闭其他应用。模型评分和真实数据导入应另行安排，不要在这台 8GB 设备上把便携演示当成正式生产环境。

## 端口占用

系统默认使用 `8000`。如果提示端口已占用，说明其他应用正在使用它。脚本会停止启动并不会替你关闭那个应用；请先人工确认占用者，再决定处理方式。
