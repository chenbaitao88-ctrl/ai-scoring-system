# 安装与运行

推荐 Python 3.11、uv 和 Node.js 24 LTS。Python 依赖由 uv.lock 锁定，前端依赖由 package-lock.json 锁定。

## 安装

在项目根目录运行：

~~~bash
uv sync --frozen
npm ci --prefix frontend
npm run build --prefix frontend
~~~

uv 会在项目内创建 .venv，不需要复制其他机器的虚拟环境或 node_modules。

## 启动

macOS：

~~~bash
bash scripts/start_demo.command
~~~

Windows PowerShell：

~~~powershell
powershell -File scripts/start_demo.ps1
~~~

打开 http://127.0.0.1:8000。第一次启动会初始化12个虚构案例；后续启动复用演示目录。默认不调用模型。

macOS 端口被占用时，可以指定其他端口：

~~~bash
BUDDY_CLAW_PORT=8010 bash scripts/start_demo.command
BUDDY_CLAW_PORT=8010 bash scripts/accept_demo.sh
~~~

脚本不会按端口结束其他进程。项目运行文件在 runtime/demo，日志为 backend.log，数据库在 data/teams.db。

## 检查与停止

macOS：

~~~bash
bash scripts/accept_demo.sh
bash scripts/stop_demo.command
~~~

Windows：

~~~powershell
powershell -File scripts/stop_demo.ps1
~~~

停止脚本先核对 PID 与项目进程标记。不要把运行目录、数据库或日志提交进仓库。

## 开发与测试

~~~bash
uv run --frozen python -m pytest -q -m "not slow"
npm run build --prefix frontend
~~~

前端开发可运行 npm run dev --prefix frontend，同时启动后端。实时模型测试需要独立配置，不包含在默认合成案例验收中。

## 常见问题

- 缺少 .venv 或前端构建：重新执行安装命令。
- npm 缓存权限失败：先核对本机缓存权限，或使用项目内独立缓存，例如 npm ci --prefix frontend --cache runtime/npm-cache。
- 端口占用：选择另一个端口，或先确认占用服务是否属于自己。
- 服务启动失败：查看 runtime/demo/backend.log，保留错误原因，不通过关闭校验绕过问题。

当前实测平台与结果见[验证说明](VALIDATION.md)。[模型配置](CONFIGURATION.md) · [返回首页](../README.md)

## 已确认结果导出

打开“结果导出”，选择评分任务、勾选可导出条目，再下载Excel、Word或CSV。默认合成案例保留待处理工单，因此不一定有可导出项；[导出说明](EXPORTS.md)提供受限于合成数据的结案复现步骤。旧下载接口不带task_id会返回409，这是保护条件。

Word指定平台中文字体：macOS为Heiti SC、Windows为Microsoft YaHei、Linux为Noto Sans CJK SC。无界面文档转换环境必须能找到相应中文字体；文件未嵌入字体。
