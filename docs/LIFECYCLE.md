# 架构草案

状态：2026-10-05：M1 获取和 M2 转换/本地执行已实现，HTTP 子集已授权实现，边界见 M3-STATUS。实际范围与硬件缺口见 [M2-STATUS](M2-STATUS.md)。管理层不依赖模型名称或聊天语义。

## 0. 工程原则

正确性、清晰边界、可靠恢复与可诊断性从首个实现开始建立。不为未发布实现保存兼容层；未来已有用户数据与公开契约仍需显式迁移，不能借“无包袱”删除用户数据。优雅以理解和修改成本衡量，不以层数衡量。只围绕真实变化建立抽象，不构造任意工作流引擎。

## 1. 单一业务权威

| 部分 | 拥有的职责 |
| --- | --- |
| CLI / HTTP | 输入解析、协议映射、输出展示；不复制模型管理规则 |
| 模型库 | resolve、获取、安装、别名、删除与恢复 |
| 执行管理 | 方案选择、资源准入、session、worker 与请求生命周期 |
| 能力契约 | chat、decision、embedding 的类型化输入输出及校验 |
| 模型 adapter | tokenizer、模板、预处理、后处理、组件编排 |
| backend | 特定 runtime 的加载、执行、取消与释放 |
| converter | 源模型检查、导出、量化、编译与产物验证 |
| source provider | HF/MS/自定义协议的版本解析和文件定位 |
| runtime 管理 | 厂商库发现、版本/ABI 检查与环境准备 |
| 持久化 | SQLite 提案与文件存储实现，不反向拥有业务策略 |

HTTP 入口采用 FastAPI，负责路由、请求校验与流式响应；不通过框架后台任务承载 native 推理，不让框架拥有模型库业务。serve 的启动/关闭与共享执行层协调。选择框架不改变独立 worker 和 SQLite 的边界。

一个新模型若复用能力，只增加适配器、转换路径与产物描述；新能力增加明确类型的契约与入口映射。后端不理解 HTTP；通用模型库不理解 Laya choice head。adapter/config 有自己版本化校验，不用随意 dict 代替所有类型。插件先采用受控实现注册，不从远端 manifest 执行 Python、shell 或任意导入路径。

## 2. 运行与进程

run 与 serve 使用同一 execution layer。run 的 session 可以包含一次或多次请求；serve 可管理多个 session 和模型生命周期，不等于把 run 放到后台。run 不自动连接 serve；首期设备独占与跨进程协调见 [LIFECYCLE](LIFECYCLE.md)。

提议 native runtime 独立 worker 隔离，父进程拥有 worker。一个 worker 承载一个已选执行方案的 context，可包含多个组件；不是机械的一文件一进程，也不把所有同 backend 模型塞入一进程。多 context 常驻与硬件并行能力需用真实 SDK 验证。

提议父子 IPC 使用 AF_UNIX socketpair + SOCK_STREAM + 长度分帧，控制消息使用受限 JSON。支持 request ID、协议版本、load/infer/cancel/unload/shutdown 及 started/output/error/finished 事件；这些是消息语义草案，不是 wire spec。正文大小、队列、写超时有界；大 tensor 不预先强塞 JSON，确有需要时定义独立传输契约。stdout/stderr 专供日志。

选择依据：worker 无需独立发现或公共地址；socketpair 双向、无 socket 路径清理。pipe 可行但双通道管理更繁琐；HTTP/gRPC 暂无跨主机 worker 需求；SEQPACKET 的包长约束暂不带来必要收益。该选择需通过目标设备上的流式、取消、背压和退出试验，不为了假想切换增加 transport 框架。

socketpair 不自动杀死子进程。父端关闭需触发退出，native 卡死需终止/回收进程，描述符继承须明确关闭非所属端；systemd cgroup 是额外生命周期保障。取消必须等待实际停止，才释放资源和报告终态。加载、取消、卸载、删除互斥规则由执行管理与模型库共同定义明确门闩。

首期 HTTP 默认仅监听回环地址，不实现公网服务或 API 凭据管理。serve 前台运行，SIGINT/SIGTERM 进入统一 shutdown：拒绝新请求、取消/等待有界时间、停止 workers、持久化必要状态、退出。systemd 管整个服务，rkai 管 workers。不自 daemonize、不依赖 PID 文件猜所有权。Type=notify 可选待定；第一期无需 socket activation。

## 3. 获取与转换

```text
引用 → provider 固定 revision → 选择/校验 manifest → 下载计划
                                                  ↓
源模型 → converter 环境 → 导出/量化/编译 → 产物校验 → 安装
                                                  ↓
                                        模型库 → run / serve
```

镜像仅改变访问端点，不改变来源身份。HF 与 MS 上同名仓库不自动视为同一模型；只有明确映射与内容验证才能建立等价。首期只访问公开来源；凭据与私有来源支持放到二期。

下载与转换共用安装流程、磁盘预估、取消及操作记录，执行过程分别拥有自己的状态机。转换失败保留可诊断状态，不登记可运行安装。转换产物记录输入 revision/摘要、recipe、工具链锁定版本、参数、校准输入摘要和输出摘要；这提供追溯，不保证厂商编译字节级可复现。

转换主机约束与运行目标条件分开。转换依赖在独立、锁定的环境运行；服务环境不装整套训练/转换框架。Python/uv 是当前工程方向，具体 SDK 支持的 Python/OS/架构须按版本核验。容器、本机环境、远端构建的选择未定；本期不默认承诺远程构建服务。

## 4. 本地状态：SQLite

文件系统保存不可变产物和随附 manifest；SQLite 保存安装、别名、任务与来源配置。首期不实现凭据存储。

概念实体：artifacts（内容摘要及相对路径）、installations（所选 model/plan/artifact）、aliases、operations（持久阶段与计划）、sources（公开端点与镜像）。外键与唯一约束由具体 schema 评审确定，不提前提交无实现的 SQL migration。

提交顺序：持久任务计划 → staging 下载/转换 → 校验 → 写 manifest 与持久化 → 同文件系统原子目录提交 → SQLite 事务登记安装/任务终态。文件系统和 SQLite 不构成整体事务；重启恢复覆盖目录已提交但无安装记录、索引已写但任务未完成等窗口。恢复必须验证实际文件与计划，冲突不覆盖、不伪造成功。

下载支持已完成文件复用；续传须核对远端版本/验证器，不盲目拼接旧 partial。区分内容 SHA256、Git blob ID、opaque ETag；本地算出的 hash 是本地完整性证据，不冒充远端真实性证明。

删除先检查使用/准备中的引用；删除一个别名不应误删共享产物。本地 manifest 导入复制到受管目录，不删除或改写外部原文件。启动恢复、CLI 修改与服务修改需要跨进程协调；锁粒度与顺序见 [LIFECYCLE](LIFECYCLE.md)。

用户目录遵循 XDG；system service 显式配置自己的数据目录。恢复检查不应无限阻塞基本服务诊断，但未验证模型不得执行。

## 5. 设备、资源与扩展

支持矩阵按 SoC、OS/arch、工具链、runtime、驱动和模型产物组合记录。RK3588 是首期验收目标；未知组合不凭版本大小推断兼容。RKNN toolkit 的版本/代际不等于通用 backend API，具体实现可以复用接口，但必须有独立兼容规则。

fallback 在执行方案选择阶段发生，策略见产品文档。资源准入考虑加载峰值、常驻权重、KV/cache、工作区与系统余量，容量估计须带证据。进程隔离不等于 NPU 调度隔离；初期采用独占执行与单活跃方案，见生命周期规格；后续放宽必须有真机证据，不能从多 worker 推断硬件可并行。

## 6. 分发与工具链基线

采用 Python wheel + uv；首个验证环境为 Python 3.12。厂商 runtime 从官方固定来源准备到版本化私有目录，不随 rkai 主 wheel 内嵌整套 SDK，不覆盖系统库。内核驱动由 OS/BSP 提供。许可与平台依据见 [官方核查](ROCKCHIP.md)。

RKNN Toolkit2 2.3.2 提供 Linux ARM64/x86_64 转换包；RKLLM 1.3.1 目前提供 Linux x86_64 转换包。convert 在支持的主机执行并输出目标产物，不能把“一键”解释成所有模型均可板端转换。首期不自动部署远程转换服务。

CPU backend 采用 ONNX Runtime 与 llama.cpp 的相应模型路径；各自使用独立格式的产物，不直接在 CPU 执行 NPU 编译文件。实际 ABI 和模型可用性仍需执行验证。

## 7. 细化契约

[模块接口](INTERFACES.md) 定义输入输出、所有权、能力与事件；[生命周期](LIFECYCLE.md) 定义锁、状态、首次验证和故障窗口；[CLI](CLI.md) 与 [API](API.md) 定义入口行为。本文保留总体结构，不重复维护各契约。

## M1 实现映射

`src/rkai/cli.py` 只解析/展示；`config.py` 管严格 TOML 与优先级；`manifest.py` 为通用校验/闭包/获取方案选择的单一来源；`sources.py` 管来源身份、provider 固定文件和镜像 URL；`files.py` 统一 POSIX 目录描述符与 no-follow 文件访问、移动和清理；`download.py` 管有限传输、验证器与 fsync/SHA256；`storage.py` 管唯一模型库规则、SQLite、lease、安装和恢复；`doctor.py` 区分静态检查、显式隔离动态加载和已安装模型主动 probe；文件检查委托模型库。HTTP 入口复用此模型库与 Session。

SQLite schema version=2（从 v1 无损新增 execution_evidence，不重建安装/alias/文件表），WAL/foreign_keys/synchronous=FULL；拒绝未知版本或无版本但有用户表的数据库，不清库。installation 为原始 manifest 摘要与 plan 的身份，保留 model/revision/snapshot；artifacts 维护安装对摘要的引用，blobs 用已校验安装文件建立受管内容复用索引。每任务 tree 与 transfers 分离，发布者文件名不会覆盖续传状态。下载及 blob 的完整摘要校验不持有模型库写锁；操作/安装锁是稳定 inode 的 fail-fast flock，busy 返回 4。提交的目录和 SQLite 不是一个事务，因此计划、目录提交和 alias/完成事务均有重开恢复测试。

更新 alias 时记录旧安装的持久回收任务，获取其独占 lease 后才清理；共享摘要仍有安装或未完成操作引用时保留。discard 与 delete 的职责不同，前者不删除 alias。所有模型相关示例/测试内容保持执行 unverified。

## M2 实现映射

capabilities.py 是能力 DTO 权威；adapters.py 注册预处理/模板/pooling/后处理，backends.py、rknn.py、rkllm.py 拥有 native context。execution.py 拥有 lease、实际方案、准入、session 与有界事件流。worker.py 仅承载 adapter/backend 调用；supervisor.py 用 EOF 专用管道处理父进程死亡，数据仍用 ipc.py 的 socketpair 帧，不引入 transport 框架。processes.py 证明整个受管进程组停止后才能释放。conversion.py 编排配方/源快照/转换记录，environments.py 负责固定版本环境，安装权威仍在 storage.py。运行与转换复用 manifest/source/下载/文件校验规则。

当前混合 runtime 多环境组件编排不支持；未知支持条件显式失败。Linux SDK/目标 ABI 尚未通过，不能把注册实现写成硬件兼容档案。实际限制见 M2-STATUS。

## M2 已实现的进程和恢复细节

安装 lease → 重验快照 → CPU 许可/主机/资源准入 → 同 UID 跨模型库独占锁 → 受管环境 worker → native load。失败按逆序清理，资源未证明停止则保留 lease，不报已回收。环境缺失不触发 run 下载。启动 native 前要求 ps 能读取 PGID/state；精简 Linux 需 procps 兼容工具，不在清理时才发现无法证明停止。转换在编译退出后重新委托模型库校验输入快照，SDK 修改输入则拒绝发布。

socketpair 固定 4 字节长度+UTF-8 JSON，8 MiB 帧；非阻塞读写与整体截止时间，不能把 select 可写解释成整帧不会阻塞。输出缓冲最多 64 事件且 8 MiB，10 秒不消费触发清理；请求默认 300 秒，加载 120 秒，取消宽限 5 秒。这些是保守安全预算，不是模型性能承诺。测试以更短期限注入，生产值不受测试更改。

请求 accepted/started/output/恰好一个终态，序号由父进程生成。输入拒绝无 accepted。取消幂等，迟到取消不改已落定结果；异常/取消退役 session，先 stop/reap 受管进程组再释放锁/媒体/lease、报告终态。不重放已输出请求。正常 completed 表示本次 native 调用结束，交互 session 仍持有 context/lease，退出后回收。

独立 guardian 持有 worker，父进程专用管道 EOF 后停止独立 native 进程组并等待其所有非 zombie 成员退出；即使 native 卡死、worker 不能回到 Python，清理不依赖其线程。父进程与 guardian 分别检查 native 进程组，不仅检查 guardian PID。安装/设备/转换/环境锁描述符显式继承给 guardian/native，父进程死亡不提前释放；锁靠最后描述符关闭释放，不显式 LOCK_UN 解开继承者。guardian 正常退出前证明 native 停止；guardian 自身崩溃由父进程监听并停止 native。握手报告 native PGID，父进程可证明两组退出。准备命令复用同一 guardian，EOF 管道仍只承担死亡通知。运行管理限同 UID 单活跃方案，保守内存预算不能冒充硬件调度或强制 RSS 上限。

conversion record 存 conversions/<id>/record.json，转换 op flock 在创建记录前持有。恢复只处理已停止 owner，活动转换返回 busy 不篡改。若 committing 的导出/安装文件摘要及登记完整，标 completed；否则 failed，绝不自动重新编译。中断保留原始诊断与下载任务，可用现有 pull --resume/--discard 处理明确 operation；部分编译文件不复用。转换工作区保留记录和产物供诊断，尚无独立自动过期清理策略，不擅自删用户数据。

SQLite v1→v2 加表迁移保留安装/alias/操作与所有文件；未来未知版本继续拒绝。execution_evidence 记录实际设备/运行库/能力与 semantic_reference=not_run，CPU 成功不将 installation.execution_validation 改为 passed。NPU 成功记录执行事实，也不等于所有模型或精度已验收。

## M2 审查修复后的提交与加载清理

安装/导出提交与 conversion completed 记录位于同一个中断延后区。即使安装成功后出现 KeyboardInterrupt，也使用与恢复相同的 committed_output 证明文件/摘要/登记已完成，再返回 completed/cancellation_deferred；无法证明才标取消，不把已发布结果误报取消。

doctor 显式动态加载复用同一 guardian 与专用 EOF 死亡通知，10 秒加载截止时间或父进程 SIGKILL 均先停止 native 进程组。临时编译的卡死 native constructor 已验证此清理路径；这是宿主进程证据，不是 Rockchip ABI 或 NPU 证据。输出管道由有界 reader 持续排空，避免大量厂商日志堵塞；终态诊断在资源停止后收集，原始日志不保存。


## M3 应用所有权与生命周期

service.py 用固定执行线程创建并关闭 Library、Session 与 infer 生成器；SQLite 不关闭线程校验、不交给任意线程池，生成器只由该线程推进和关闭。HTTP 只做 DTO/协议转换，经有界 Job 输出通道收取复制事件。单线程 FIFO 序列化模型请求与切换；Session 仍是执行方案/fallback/native 生命周期的唯一权威。models 查询在短时独立连接的所属线程中完成，遵守模型库 lease 和文件校验。

取消控制只设置 Job 标志并在独立控制线程调用 Session 的锁保护 cancel；控制锁和 native 停止等待不能阻塞 ASGI 事件循环。加载读取也观察取消标志与加载期限，先回收 worker 再报告失败。HTTP 响应迭代器同时监测断连和 send 截止时间，不依赖 ASGI 版本的 send-only 断连机制。停止信号在 Uvicorn 等待连接前停止应用准入；在途取消、排队退出、actor 停止 worker/释放 lease，最后退出。进程停止无法证明时停止准入、保留 owner/lease/预算并重试证明，不宣称资源已释放；systemd cgroup 是外部兜底。

上传解析逐块统计实际字节、metadata/file/total 限额与全局预算；文件操作的取消等待实际文件操作返回，避免清理与线程池写入竞态。私有 http-staging 使用随机身份和 flock owner，正常退出清理，重启只回收可获得 owner lock 的遗留上传，不删除其他进程活动内容或用户模型。数据库 schema 仍为 v2，没有迁移或重建已有安装。


2026-10-06 审查修复：Session.cancel 可携带 request_id，锁内核对当前请求，防止共享 context 的迟到取消串扰。
队列移除与 actor 出队使用同一 mutex；尚未执行的取消可直接完成，已出队的请求仍只由 actor 证明停止。
JSON/SSE 的 Delivery 持有响应预算到 ASGI 发送退出，并在迭代器尚未启动时也执行清理。
成功输出按入口绝对期限停止；超时错误诊断只在 native 停止后进行有界写入，发送阻塞则结束连接。
启动握手、load 写入和 loaded 读取共同观察加载取消/剩余期限，未 loaded 的进程不等待软关闭。
稳定 http-uploads 注册锁协调 UUID owner 创建、恢复、删除，防止 unlink 后出现两个 flock inode；
正常清理和恢复回收临时锁，旧孤儿锁只在证明 owner 空闲后删除，不改用户模型或数据库格式。
