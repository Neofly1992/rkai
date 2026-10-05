# M2 审查与修复记录

2026-10-05。用户授权修复独立审查发现的 9 项问题；未提交、推送或发布，保留原有未提交内容。以下为本轮修复结果，不代表完整 M2 硬件验收。

| 问题 | 修复 | 实际验证与限制 |
| --- | --- | --- |
| RKLLM 转换依赖在 CPython 3.12 上默认 CUDA 源码构建失败 | 仅 rkllm-convert 设置 BUILD_CUDA_EXT=0、UV_NO_CACHE=true，并纳入环境身份及 ready 记录 | AutoGPTQ 0.7.1 固定摘要源码安装通过，无 CUDA 扩展；未运行 Linux RKLLM SDK import/build/export |
| 已安装后中断误报取消，转换记录不一致 | 提交与 completed 记录一起延后 SIGINT；显式 KeyboardInterrupt 使用共同提交证明恢复结果 | 真实 SIGINT 和异常注入两种提交窗口均通过，别名与记录保持 completed |
| doctor 动态加载的孤儿 native 进程 | 加载复用已有 guardian/死亡管道，停止进程组后返回 | 宿主实际编译卡死 constructor，父 SIGKILL 和加载超时均回收；非 Rockchip ABI 证据 |
| 压缩图片突破内存准入 | 解码前限制 8 Mi 像素，native 预算额外加图像/张量工作区 | 4000×4000 压缩 PNG 在 load 前拒绝，真实 worker 返回输入错误；准入预算拒绝通过 |
| RK3588 W4A16 配方被接受却按 SDK 默认精度转换 | 当前 RKNN 量化只接受 W8A8，并显式传 quantized_dtype | 支持条件拒绝回归通过；真实 SDK 编译尚未运行 |
| 图像校准预处理不同于运行 | 共用 image_tensor；生成已归一化 NPY，SDK 不重复归一化 | 校准/运行数组一致且参考 RGB 数值通过；真实量化精度尚未验证 |
| 多输入校准样本序列化错误 | 二维样本行，列顺序与图输入一致；检查 dtype/shape/有限值，NPY 分配前检查头部 | 两样本三输入正确分组；列数/顺序/原始文本和危险 NPY 头部拒绝通过 |
| native stderr/stdout 被丢弃，无法定位准备失败 | 有界连续读取，只保留固定诊断标签；附 stage/program/exit_code | 实际大量噪声输出无堵塞，missing_module:torch 可诊断；错误和日志无测试秘密内容 |
| run 中 worker 输入错误统一返回 1 | error.exit_code 跨 worker/事件/CLI 传递并校验 | 独立 wheel 的真实 MiniLM 上下文超限退出 2；图像 worker 超限也返回 2 |

最新全量 **173 passed**（原 158 项保留，新增 15 项）；Ruff、36 个 Python 文件格式和离线 sdist/wheel 构建通过。公开 MobileNet 分类、MiniLM 批量 embedding、stories260K 流式文本/输出后取消/慢消费者回收重新实测 CPU 通过；独立 wheel release/debug 分类、三轮交互/reset 通过。

证据：[修复检查与源码摘要](verification/m2-review-fixes-20261005.json)、[AutoGPTQ 源码构建](verification/m2-autogptq-build-fix-20261005.json)、[公开 CPU](verification/m2-public-cpu-20261005.json)、[独立 wheel](verification/m2-wheel-smoke-20261005.json)、[真实输入退出码](verification/m2-input-status-fix-20261005.json)。新增回归入口为 `uv run --locked pytest -q tests/test_m2_review.py`；整体入口见 VERIFICATION。

当前宿主 macOS ARM64，没有可用 Linux 转换主机或 RK3588 连接。Linux SDK 完整导入/编译、目标 ABI/动态加载/驱动及真实 NPU 推理和量化精度仍未验证。校准 helper 的数组测试不证明 Toolkit2 实际接受数据集，也不证明 NPU 精度。后续条件与入口见 M2-STATUS；不能宣布 M2 全部验收完成。M1 最新公网 MS 403 记录保留，不以 CPU 成功覆盖来源失败。
