# 当前交接

2026-10-05：用户已授权 M2「转换与本地执行」，真 RK3588 静态诊断后置，不阻塞实现。M1 与 M2 当前注册路径已落地；decision/Jev/Laya 与混合组件编排仍是功能缺项，Linux SDK/真机仍待验证；M3 未开始。保护启动时全部未提交修改，没有提交、推送或发布。

先读 PRODUCT → CLI/MANIFEST → ARCHITECTURE/INTERFACES/LIFECYCLE；实际支持与硬件条件见 [M2-STATUS](M2-STATUS.md)，使用见 USAGE，完整证据见 VERIFICATION。旧设计 manifest 是占位，不是可运行模型；examples/recipes 为实际 CPU 配方及尚未真机验证的 RKNN 配方。

已实现 convert/run/runtime prepare；来源、输入快照、校验与安装继续复用 M1 模型库。能力为 classification/embedding/chat，adapter 与 backend 边界明确；decision/Jev/Laya、混合多环境组件编排仍未支持，不注册空 adapter。CPU 默认关闭、独立产物、不隐式下载、不输出后重跑。SQLite v1→v2 只新增执行证据，保留全部安装/alias/文件。

执行管理含 lease、跨 data_dir 单活跃方案、资源准入、session、分帧、有界事件、取消/背压/卡死/崩溃/父退出。guardian 保留资源锁并拥有独立 native 进程组，父死亡不提前放开安装；双方证明 native 停止再释放。受管环境精确 CPython 3.12.13，依赖/构建及官方 SDK 来源固定；厂商文件仅在私有受管目录，不覆盖系统库或驱动。

前次审查修复：**173 tests passed**，Ruff/36 文件格式、sdist/wheel 通过。9 项修复与证据见 M2-REVIEW；包括安装提交后中断、doctor 父退出清理、解码内存准入、量化校准契约、安全诊断和请求退出码。AutoGPTQ 固定摘要源码安装在最终独立构建设置下通过，仅为依赖打包证据。新隔离环境中 MobileNet 分类、MiniLM 批量 embedding、stories260K 流式文本/输出后取消/缩放慢消费者回收真实 CPU 通过；独立 wheel release/debug 分类及三轮交互/reset 通过。这些不证明 NPU。SDK 实物/许可核对、RKNN 宿主结构比对及 RKLLM header syntax-only 通过；Linux SDK 实际编译/目标链接/ABI/动态加载/驱动/NPU 推理均未运行，不能宣布 M2 全部验收完成。当前宿主 macOS ARM64，Docker daemon 不可用；没有板卡连接信息，不反复询问或用 mock/CPU 补硬件证据。

M1 独立审查历史见 M1-REVIEW。10-04 公网成功保留；本次 10-05 原脚本复验在 MS 元数据 HTTP 403/业务码 10013201001 失败，未盲目重试/变更身份或端点，不能称本次公网全通过。M1 全部回归仍通过；失败记录在 VERIFICATION。

复验：`uv sync --locked --python 3.12`；`uv run --locked ruff check src tests scripts`；`uv run --locked ruff format --check src tests scripts`；`uv run --locked pytest -q`；`uv build --offline`。公开 CPU 入口：`uv run --locked python scripts/verify_m2_public.py`（默认隔离临时库）。后续用真实 Linux 转换主机/RK3588 按 M2-STATUS 的入口做 SDK、ABI、实际 NPU 与参考精度/生命周期验证；保留未验证状态，不代签用户验收。

2026-10-05 本会话独立复审见 M2-INDEPENDENT-REVIEW：另复现并修复 3 项 P2（fallback 先筛本机目标/能力；坏环境记录受控报错；终态后的 Ctrl+C 保持退出码）。最终 **180 passed**（线程异常作为错误）、Ruff/36 文件格式、sdist/wheel 通过；独立公开 CPU 流程复跑通过，证据 m2-independent-public-cpu-20261005.json，复用既有隔离环境。公开执行与测试不能并行：共用主机独占锁，一次验证安排冲突后串行复跑通过。本轮未重装 wheel、未测 Linux SDK/真机、未重跑 M1 公网；硬件与 decision/混合组件覆盖边界仍见 M2-STATUS，不宣布 M2 全部验收完成。

2026-10-05 复审报告续接核对：三项修复已在源码，针对性 7 项回归重新通过，未发现新的已复现阻塞问题。本轮仅同步 PRODUCT/ROADMAP/ACCEPTANCE 的状态措辞，未新增执行功能、未重跑全量或公开 CPU、未重装 wheel。后续分开推进真实 Linux SDK 转换验证、decision/Laya 实物与参考适配、多组件实际编排；硬件后置不视为功能缺项完成。

交付续接：用户要求提交当前 M1/M2 成果，并提供新会话的 M3 开工指令。M3 可基于当前执行层推进，不必等待所有模型适配；M2 的 Linux SDK/真机证据与 decision/混合组件缺项继续独立追踪，不标为已完成。本会话未开始 M3 实现。
