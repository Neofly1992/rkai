# 当前交接

2026-10-06 独立复核：另复现并修复加载超时污染同模型排队请求（首请求 504，第二请求误报 503）。新增真实 HTTP/guardian 回归；完整 212 passed、Ruff/格式/构建通过，重新构建的独立 wheel 公开 CPU/SDK、信号关闭及退出后 CLI 准入通过。见 M3-INDEPENDENT-REVIEW、VERIFICATION 与 m3-independent-wheel-http-20261006.json。以下 211 项为此前修复轮次；差异仍未提交，目标平台验证边界不变。

2026-10-06：用户授权 M3 实现及审查问题修复；基线 1572628，现有差异未提交，未推送/发布或改系统配置。M3 本地服务子集已实现，8 项审查问题已修复；实际范围分别见 M3-STATUS、M2-STATUS、VERIFICATION。Linux systemd/RK3588 完整验收仍未完成。

先读 PRODUCT → CLI/API/SERVICE → ARCHITECTURE/INTERFACES/LIFECYCLE/MANIFEST；遵守 CONTRIBUTING。serve 默认 127.0.0.1:8080，固定线程持有 Library/Session/生成器，HTTP 只映射协议；保留单 UID 单活跃方案、lease/设备锁、CPU 默认关闭、无隐式准备/下载、输出后不重放。一个 preload，同模型串行复用，不同 alias 先退役旧 context。

本轮修复取消 request_id 隔离、停止证明后的终态、JSON/SSE 发送预算与期限、排队取消独立回收、握手/load 取消、临时上传锁回收，以及 systemd 解释器 home 路径问题。API/LIFECYCLE/SERVICE 已同步。回收可晚于执行期限，失败诊断只在证明停止后有界发送；写入阻塞可终止连接，不继续成功输出。上传注册锁保护 UUID owner 创建/删除，恢复保留活动 owner 和其他文件；数据库仍 v2，无模型数据迁移/删除。

最新完整回归 211 passed（原 203 + 新增 8 项 HTTP），Ruff/42 文件格式/构建通过。独立主 wheel 使用位于 /tmp 前缀的 CPython 3.12.13，公开 CPU/官方 SDK、两种 profile、输出后 SIGINT/SIGTERM 与退出后 CLI 准入完整复跑通过。新增证据为 m3-fix-wheel-http-20261006.json 和 m3-fix-wheel-package-20261006.json；命令/过程失败保留在 VERIFICATION。公开 CPU 脚本复用此前显式准备的 /tmp/rkai-m2-final-preparation-library 与 /tmp/rkai-m2-public，未重下权重；native 验证不可并行（同 UID 锁）。

Linux SDK 实际转换、RK3588 ABI/驱动/NPU、systemd User=rkai/ProtectHome/cgroup 未验证；decision/Jev/Laya 与混合组件仍未实现。继续独立追踪，不作为新增 M3 前置条件、不标完成。完整验收仍需真实目标平台与用户证据；重复入口见 SERVICE/M2-STATUS。M1 公网本轮未重跑，历史 MS 403 保留。不自动提交、部署、启用服务或修改驱动。
