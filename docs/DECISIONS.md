# 设计选择

2026-10-02 更新。这里记录工程选择，不是要求用户逐项答题的审批清单。用户重点 review 使用方式和模型抽象，普通实现选择由工程负责。

## 已确认的方向

- RK3588 通用模型平台；Qwen3 只是 LLM 示例，LLM 只是模型类别之一。
- CPU fallback 默认关闭，用户可手动开启。
- 本地优先，凭据/私有来源/API 鉴权及对外服务能力放到二期。
- convert 是首期能力；run 支持单次与持续交互，serve 可被 systemd 托管。
- uv 管理工程依赖与虚拟环境。

## 直接采用的工程默认

| 事项 | 选择与原因 |
| --- | --- |
| 格式 | pyproject.toml 管工程、config.toml 管用户设置；机器产物 manifest/recipe 用 JSON，无需重复支持 YAML |
| 版本 | 用户可用 latest/tag；解析后固定快照，运行使用已安装快照 |
| 本地记录 | SQLite + 文件目录，适合安装/任务事务与恢复 |
| 模型描述 | model / plan / component；backend 在组件上，能力由实际方案声明 |
| 转换 | recipe 独立，下载与转换共用校验/安装路径 |
| 进程 | native worker 由父进程拥有；socketpair/STREAM 分帧，验证失败再按证据调整 |
| 语言 | Python 为主，Typer/Rich 按需引入，native binding 保持薄层 |
| HTTP 框架 | FastAPI；仅承担协议入口、请求校验与流式响应，业务和 worker 生命周期归共享执行层 |
| 来源 | HF/MS、兼容自定义端点与静态 manifest 来源；镜像与身份分离 |
| fallback | 执行前选择有效 CPU 方案，不在输出后静默重跑，不隐式下载 |

这些默认仍可在 review 中修改，但不会要求用户逐一确认才能推进。内部兼容检查属于实现责任，不因用户打开 fallback 就省略，也不再上升为产品问题。

## 官方核查后的工程结论

详细来源、固定 commit、平台矩阵和证据边界见 [Rockchip 核查](ROCKCHIP.md)。

- rkai 采用 Python wheel + uv；首个 SDK 验证环境选 Python 3.12。
- 厂商库从官方固定来源准备到产品管理目录，不内嵌整个 SDK，不覆盖系统库；驱动由 OS/BSP 提供。
- RK3588 采用 RKNN Toolkit2/RKLLM 两条路径；RKNN 转换已有 Linux ARM64/x86_64 wheel，当前 RKLLM 转换包为 Linux x86_64。
- 后续 RKNN3 属于独立不兼容工具链，正确仓库为 rknn3-toolkit。
- CPU 方向为 ONNX Runtime / llama.cpp 对应独立产物，适配范围通过实际模型验证。

没有需要用户代查的 SDK 问题。二进制 ABI、安装体验、驱动与模型实际执行保留为工程验证任务；事实调查不冒充真机验收。

## 用户 review 范围

当前没有阻塞推进、必须由用户先回答的问题。请 review 产品使用方式和 manifest 是否清晰表达需求；框架、数据库、IPC 等普通技术选型由工程负责。HTTP 采用 FastAPI，无需为本地模型 API 引入 Django 的完整 Web 应用结构。具体依赖版本在实现时锁定。

## 使用与交付细化

registry 支持限定 REF、--registry 和默认配置，来源冲突报错；默认公开 HF。convert 接收本地路径和远程源引用，共用来源解析。release/debug 同一代码、不同默认日志级别，可显式覆盖。HTTP 按 JSON/媒体/元数据分别限额并可配置。实施划为 M1 获取诊断、M2 转换执行、M3 服务交付，具体契约见 CLI/API/ACCEPTANCE。

## 2026-10-03 M1 工程落地

- argparse 保持 CLI 薄层，无需 Typer/Rich；httpx 统一公开 HTTP 和可注入传输；Pydantic 严格模型承担通用 manifest/config 结构，不再维护第二套 schema 规则。uv.lock 固定实际依赖。首个验证 Python=3.12.13，pytest/Ruff 为统一检查入口。
- 直接使用经官方文档/源码核对的 HF/MS 公开协议，避免为获取配置引入整套 ModelScope 训练工具。provider 的身份与访问镜像分开，日志关闭 transport 的 URL 诊断。
- NPU 静态诊断不 dlopen native library，不用“能加载”假称推理成功。显式 runtime_paths 检查 ELF 架构与可用静态符号工具；加载/ABI/兼容组合 unknown，等待 M2。
- paused/failed 下载可显式丢弃，解决持久准备引用保护和用户可清理需求之间的遗漏；此操作不触及 alias/外部源文件。

## 2026-10-05 M2 工程落地

- 继续采用 socketpair STREAM 4 字节分帧；实测慢消费者揭示 select 后整帧阻塞风险，改为非阻塞描述符和固定截止时间。父退出使用 guardian + EOF 管道，管道只承担死亡通知，不创建 transport 框架。为避免父进程死亡提前放开 lease，guardian 保留锁并拥有独立 native 进程组；双方验证 native 停止后释放。worker 原生卡死和父 SIGKILL 的实际 CPU 测试通过。
- 官方 Lite2 包存在隐式系统 librknnrt 查找，因此采用固定官方 RKNN C API 的薄 ctypes binding，显式受管绝对路径；RKLLM 结构按固定头编译 bridge，避免复制大 ABI struct。目标动态加载/驱动仍需板端实测。
- converter 使用严格、显式 recipe；未知目录无法可靠推断标签、prompt、pooling 和资源预算，不能由 CLI 猜模型名。目标来自 output_plan，取消旧草案 --target 和自动 converter 推断，没有未发布兼容层。非 SDK CPU import 是真实独立产物检查/装载路径，不是 NPU 编译模拟。
- 依赖与 build constraints 用版本+hash 锁，SDK binary/license 固定官方 commit+size+SHA256；环境准备后实际独立 import/dlopen 才有 ready 标记。CPython 3.12 及系统 C++ 工具链仍属于主机条件，不声称完整 hermetic build。只在受管目录准备，不改系统驱动。
- 从 schema v1 无损新增执行证据表到 v2；保留全部已有数据，未知版本拒绝。CPU 执行证据独立于 NPU validation。同 UID 跨模型库一个活跃方案与保守预算为本期准入策略，不假称跨用户硬件调度。
- MobileNet 分类、MiniLM 批量 embedding 与 GGUF 文本生成检验不同语义；模型特有处理留在 adapter。Jev/Laya 调查及尚未支持的 decision 明确记录于 M2-STATUS，没有空适配宣称支持。
