# M2 独立复审（2026-10-05）

结论：上一轮的 CPU 与进程验证有实际代码和证据支撑，但并非没有遗漏。本轮独立复现并修复 3 项 P2 问题，新增 7 个回归用例；最终 180 项测试、Ruff、格式、构建及公开 CPU 流程通过。在本次审查范围内，没有保留已复现而未修复的阻塞问题；这不是 M2 全部硬件验收通过。

## 范围与方法

核对 PRODUCT、MANIFEST、贡献规范、当前交接与 M2 状态/审查记录；阅读转换、环境、执行管理、IPC、guardian、worker、能力、adapter/backend、RKNN/RKLLM binding、CLI、doctor、执行证据存储及相关测试。先独立运行原有全量测试，得到 173 passed；再针对遗漏场景编写回归，确认失败后修复，没有仅依据上一会话的总结判断。

## 发现与修复

| 优先级 | 可复现问题 | 处理与验证 |
| --- | --- | --- |
| P2 | 同一 manifest 安装了本机与其他 OS 的 CPU 方案时，NPU 不可用后的 fallback 将两者都计入候选，错误返回 ambiguous_plan | 候选先按本机 OS/arch、声明能力及默认能力语义过滤，再消歧；安装本机/异机/NPU 三方案的用例最终在真实 CPU worker 完成分类。NPU 不可用由注入模拟，不是 NPU 证据 |
| P2 | 受管环境 ready.json 为 [] 或 null 时，resolve 直接调用 .get，产生未受控 AttributeError，破坏 CLI 的统一错误输出 | 验证记录是对象，并将递归解析失败纳入受控错误；数组、null、深嵌套记录回归通过。保留坏环境，不自动删除或重建可能被使用的环境 |
| P2 | CLI 已收到 completed/failed/cancelled 后，显示结果期间的 Ctrl+C 仍调用 cancel 并返回 130，改写已经落定的结果 | 统一终态退出码映射，迟到中断保持原终态，不再次取消。三个终态的中断注入分别保持 0/2/130；不是硬件信号时序证明 |

修改限于 execution.py、environments.py、run_cli.py 及对应测试，没有改变存储格式、SDK 版本、模型语义或硬件支持声明。

## 产品与架构判断

- 管理层没有绑定 Qwen3 或 token；分类、embedding 与流式文本走同一 Session。adapter/backend 分工和转换共用安装权威成立。受管环境准备是显式操作，run 不隐式下载。
- CPU opt-in、损坏/权限错误不降级、输出后不重放、lease 与 native 停止顺序仍保留；完整回归覆盖取消、超时、guardian/父退出、安装和删除保护。
- convert 当前依赖显式配方，支持范围是已注册的四条路径。CPU ONNX/GGUF 导入与运行不能证明 RKNN/RKLLM 编译成功，也不是“任意模型一键转换”。
- decision/Jev/Laya 和混合多组件编排仍未支持；没有空 adapter 冒充完成。它们是产品覆盖缺项，不能因为分类/embedding/chat 通过就写成全部模型能力已实现。
- Linux SDK 实际导入/编译、目标 ABI/动态加载、驱动、NPU 推理与量化精度未验证。用户已允许真机验证后置，不反复以缺板卡阻塞独立工作；完整 M2 验收仍需要这些证据。

## 本轮实际验证

1. 原始基线：173 passed。
2. 最终串行全量：`uv run --locked pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning`，**180 passed**。
3. Ruff、36 个 Python 文件格式、离线 sdist/wheel 构建通过；本轮未重新安装 wheel，历史独立 wheel 证据不冒充本轮结果。
4. 独立运行 `scripts/verify_m2_public.py`，复用已有隔离的 `/tmp/rkai-m2-public` 与 `/tmp/rkai-m2-final-preparation-library`，不使用默认模型库，不宣称新环境重建。MobileNet top-1 为 812；MiniLM 返回 384 维向量并通过相似度检查；stories260K 流式输出、输出后取消和缩放慢消费者回收通过。新结果见 [本轮 CPU 证据](verification/m2-independent-public-cpu-20261005.json)。
5. 一次错误地并行安排公开执行与回归测试，使 8 项测试因共用主机独占锁而失败（172 passed）。未关闭锁或修改隔离策略；公开执行结束后串行重跑，全量 180 passed。此失败属于验证安排冲突，保留说明，不冒充一次全绿。

本轮没有重跑 M1 公网脚本、Linux SDK 或真 RK3588，没有安装新厂商库、修改驱动、提交、推送、发布或开始 M3。
