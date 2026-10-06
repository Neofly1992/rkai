# 本地服务与 systemd

M3 的 HTTP 子集已实现；实际验证与未测平台见 M3-STATUS、VERIFICATION。
服务前台运行，不创建 PID 文件、不 daemonize、不自动启用 systemd、不改系统驱动。

## 本地运行

先显式准备环境并安装模型（见 USAGE），再运行：

```sh
rkai --data-dir /absolute/library serve
rkai --config /absolute/config.toml serve --port 8080 --preload installed/alias
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/v1/models
```

默认 127.0.0.1:8080；配置可选择 ::1，拒绝非回环绑定。占用端口返回 listen_failed/退出 4，不自动换端口。
延迟加载时 ready=true 仅表示服务能接收请求，模型可为 unloaded。preload 指一个 alias，失败则启动失败。
请求只用已安装 alias；服务不会 pull/convert/prepare。CPU 仍默认关闭，在服务配置中显式开启。

原生 classification 上传示例：

```sh
curl http://127.0.0.1:8080/api/v1/infer \
  -F 'metadata={"model":"installed/alias","capability":"classification","input":{"image":"media:image"},"parameters":{"top_k":5},"stream":false}' \
  -F 'image=@/absolute/image.jpg'
```

文件名不参与服务路径选择；任意服务端路径、远端媒体 URL 和内嵌 classification base64 不支持。
文本 JSON 与 multipart 的严格字段、SSE 和预算见 API。较大的 HTTP 预算不改变模型 16 MiB 压缩图像/8 Mi 像素上限。
无鉴权服务仅用于本机；不提供公网/多用户部署。日志只含阶段与安全错误，不记录输入、媒体或 native 原始输出。

## systemd 安装（用户显式执行）

wheel 内包含 `rkai/share/rkai.service` 和 `config.toml`，仓库同名文件也可使用。
以下为管理员操作说明，本轮没有执行本机系统修改。
根据发行版选择创建系统用户的工具；示例为 Linux useradd：

```sh
sudo useradd --system --user-group --home-dir /var/lib/rkai --shell /usr/sbin/nologin rkai
sudo install -d -m 0700 -o rkai -g rkai /var/lib/rkai /var/cache/rkai
sudo install -d -m 0755 /opt/rkai /etc/rkai
sudo install -d -m 0755 /opt/rkai/python
sudo env UV_PYTHON_INSTALL_DIR=/opt/rkai/python uv python install 3.12.13
sudo env UV_PYTHON_INSTALL_DIR=/opt/rkai/python uv venv --managed-python --python 3.12.13 /opt/rkai/venv
sudo uv pip install --python /opt/rkai/venv/bin/python /absolute/rkai-0.1.0-py3-none-any.whl
sudo install -m 0644 src/rkai/share/rkai.service /etc/systemd/system/rkai.service
sudo install -m 0644 src/rkai/share/config.toml /etc/rkai/config.toml
```

解释器显式安装在 /opt/rkai/python，虚拟环境不引用管理员的 /root 或 /home。
uv 的受管 Python 默认位于用户数据目录，venv 会继续引用该位置；因此不能在这里省略 UV_PYTHON_INSTALL_DIR。
依据：[uv Python 存储规则](https://docs.astral.sh/uv/reference/storage/#python-versions)。
unit 的 ProtectHome=yes 会隐藏 /root、/home 与 /run/user；安装后先验证服务身份可执行入口：

```sh
sudo -u rkai /opt/rkai/venv/bin/python --version
sudo -u rkai /opt/rkai/venv/bin/rkai --help
readlink -f /opt/rkai/venv/bin/python
```

按实际 BSP 设备节点配置 rkai 用户所需的组/权限，不默认 chmod 设备或改驱动。
准备 runtime 与安装模型必须使用该身份及相同数据目录，例如：

```sh
sudo -u rkai env XDG_CACHE_HOME=/var/cache/rkai PATH=/usr/local/bin:/usr/bin:/bin \
  /opt/rkai/venv/bin/rkai --config /etc/rkai/config.toml runtime prepare rknn-runtime
sudo -u rkai /opt/rkai/venv/bin/rkai --config /etc/rkai/config.toml pull /absolute/model/manifest.json --alias installed/alias
sudo -u rkai /opt/rkai/venv/bin/rkai --config /etc/rkai/config.toml doctor --model installed/alias --json
sudo systemd-analyze verify /etc/systemd/system/rkai.service
sudo systemctl daemon-reload
sudo systemctl start rkai.service
```

先确认模型/驱动/runtime 实测与服务健康，再由管理员决定是否执行 `systemctl enable rkai.service`。
unit 使用固定 `/opt/rkai/venv/bin/rkai`，不依赖登录 shell 激活环境；受管 vendor 库留在 /var/lib/rkai，主 wheel 不带 SDK/权重。
`StateDirectory` 与 `CacheDirectory` 由 systemd 保持私有，日志进入 journald。
`PrivateTmp=no` 是必要约束：否则 CLI 与服务看不到相同的同 UID /tmp 设备锁。
`ProtectSystem=strict` 配合 `ReadWritePaths=/tmp` 为共享锁保留写访问；依据 [systemd 官方执行规范](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml) 的只读文件系统与写路径排除规则。
用其他 UID 运行 CLI 不在当前同 UID 准入域内；首期不承诺跨 UID NPU 调度隔离。不要部署多个身份并行使用同一设备。

SIGINT/SIGTERM 先停止应用准入、取消在途和排队请求；HTTP 清理等待 native 停止，actor 关闭 Session、释放 lease，再退出。
unit 的 KillMode=control-group/TimeoutStopSec=45 是额外故障兜底，不以 PID 文件判断 worker 所有权。
应用上传保存在私有 http-staging 中，正常失败/断连/关闭清理；崩溃遗留只在下次服务启动取得该上传 owner lock 后回收。
取消期间锁仍由 guardian/native 持有，不把请求断连当作 native 已停止。

## 卸载与诊断

```sh
sudo systemctl disable --now rkai.service
sudo rm /etc/systemd/system/rkai.service
sudo systemctl daemon-reload
```

停止后可删除专用 /opt/rkai/venv 与 /opt/rkai/python。默认保留 /etc/rkai/config.toml、/var/lib/rkai 和模型数据；不自动删账户、缓存或用户数据。
没有对应模型删除授权时不要递归清除数据目录。实际模型删除用同服务身份执行 `rkai delete`。

- 启动失败：`systemctl status rkai`、`journalctl -u rkai -b`；核对绑定端口、用户、绝对路径、preload 与 CPU 许可。
- busy/429：查看 /health requests/busy，检查同 UID CLI/其他库的活跃执行；不删除锁文件绕过 flock。
- 模型列表为空：核对服务身份和 data_dir；列表只含文件完整的 alias，文件完整不等于 NPU 已验证。
- 环境/设备失败：同身份运行 doctor；runtime prepare 是单独显式操作，不让 HTTP 隐式下载或修权限。
- 413：分别检查 json/multipart/metadata/media_file 限额；媒体解码与模型上限另外执行。
- 无法证明进程停止：保留 lease、停止准入并检查进程/cgroup；不要宣称已释放或强删安装。

Linux systemd 启停、设备权限与 RK3588 NPU 未在当前 macOS 宿主运行。可重复入口是上面的 verify/start/stop/journal 命令和 M2-STATUS 的板卡流程；不能用宿主信号测试替代 systemd/cgroup 验收。
