# 验证记录

## 2026-10-02：初始设计文档

范围：README、产品/架构/manifest/决策/路线图/来源说明、贡献指南、续接说明及 JSON 结构示例。没有生产代码、CLI、依赖环境、正式 JSON Schema 或数据库实现。

已运行：

- Python 标准库解析五份 JSON 示例。
- 四份模型示例的 file/source 引用、adapter 文件引用、plan/component id 唯一性、默认能力引用与 SHA256 字段形状检查。
- Markdown 本地文件链接目标存在性检查。
- `git diff --check` 与工作树状态检查。

这些检查仅验证文档内部一致性，不是正式 manifest 校验器，更不是模型支持验证。示例地址、摘要、大小、revision、主机条件、兼容档案及模型配置为显式占位。转换配方仅完成 JSON 语法检查，未验证工具链锁或 converter 语义。

未运行：任何模型下载、转换、CPU/NPU 推理、RK3588 驱动探测、进程通信、SQLite 恢复、systemd、分发安装或性能测试。未安装厂商 SDK，未改动 mox。未创建提交或推送。用户 review 尚未完成。

## 2026-10-02：用户 review 修订

修正通用模型平台定位；凭据与对外服务移到二期；fallback 默认关闭改为已确认；收敛工程选择；补充 latest 解析/更新与 TOML 配置、JSON 产物边界。本轮重查本地 Markdown 链接和 git diff --check；未修改或重测模型示例，无新增运行能力。

## 2026-10-02：官方仓库核查

读取官方 README、LICENSE、changelog、RKNN C 头文件和 GitHub 递归目录；记录三个 commit，详情见 ROCKCHIP。网络默认沙箱请求失败后，经只读网络授权成功取得目录。没有下载 SDK 二进制或驱动，没有接受/安装工具包，没有进行 ABI 实测或真机验证。更新设计选择与架构，检查本地文档链接和空白差异。

## 2026-10-02：HTTP 框架选择

核对 FastAPI 与 Django 官方介绍；记录 FastAPI 为 HTTP 入口选择，未安装依赖或声称完成服务实现。运行 git diff --check。

## 2026-10-02：设计细化

新增 CLI、INTERFACES、LIFECYCLE、API、ACCEPTANCE 五份文档，同步产品/架构/manifest/路线图/README/HANDOFF。检查本地 Markdown 链接、五份既有 JSON 语法与 git diff --check；人工交叉核对 ready/硬件验证、fallback、导入所有权及开工授权一致性。没有新增实现、依赖、测试脚本或环境安装；没有执行硬件/API 测试。HTTP 列表为目标子集，不是已验证兼容承诺。

## 2026-10-02：来源、诊断与里程碑细化

更新 CLI 的 registry/convert/release-debug、API 的分类请求限额、贡献与 review 原则，将计划收敛为 M1–M3。核对本地链接与 git diff --check；仅文档，无运行或性能验证。请求预算为设计默认，不是实测结论。

## 2026-10-03：M1 实现与验证

用户已明确授权编写 M1 代码。范围：公开模型获取/本地导入/恢复/别名/安全删除，配置与 release/debug，严格 manifest，SQLite，静态 doctor；未开始 M2/M3。初始 Git 状态为 README 修改、AGENTS/CONTRIBUTING/docs/examples 未跟踪；保留已有内容并同步相关规格，没有创建提交、推送或发布。

### 工程与自动验证

宿主 macOS arm64；Python 3.12.13。实际锁定 httpx 0.28.1、Pydantic 2.13.5/pydantic-core 2.46.5、pytest 9.1.1、Ruff 0.16.10。版本权威为 uv.lock，不外推厂商 SDK 兼容性。

实际执行（本次沙箱使用 `UV_CACHE_DIR=/tmp/rkai-uv-cache`；通常环境不需要该覆盖）：

```sh
uv sync --locked --python 3.12
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv run --locked pytest -q
uv build
```

最终结果：**75 passed**，Ruff 检查通过，16 个 Python 文件格式检查通过，sdist 和 `rkai-0.1.0-py3-none-any.whl` 构建成功。所有测试模型库/输入使用 pytest 临时目录，未操作用户默认模型库。

覆盖：严格字段/类型/重复 JSON 键/引用/路径及保留文件名；来源默认/显式/URI/冲突；真实 latest 语义的固定计划（模拟 provider）；MS 每文件 commit；镜像元数据失败/坏摘要回源；强/弱 ETag、验证器变化、完整坏 partial、分块中断；SQLite close/reopen、三个安装提交窗口、两个删除窗口、真实 SIGINT 提交延后、持久暂停、共享 alias/内容、旧安装 lease 后回收、准备保护/显式 discard、已删除安装不复活、未知数据库版本保存用户表；profile 业务相同、级别覆盖和 stdout/stderr 分离；未知硬件证据与损坏安装诊断。

### 证据分类与边界

| 类别 | 实际证据 | 不能证明 |
| --- | --- | --- |
| 结构与 provider 故障注入 | 独立生成的 acquisition-only 夹具；httpx MockTransport 的 latest/镜像/摘要/续传异常 | 真实远端存在 latest 或真实镜像全部兼容 |
| 存储集成 | 真实 SQLite/WAL、目录/fsync/rename、跨进程 flock、重开恢复；阶段回调模拟进程消失；真实 SIGINT | 目标板断电、磁盘固件故障或 native worker 生命周期 |
| 回环 HTTP | 实际 TCP 响应在声明长度前关闭，续请求携带 Range/If-Range，最终 SHA256 正确 | 公网长时间断网、数 GB 权重吞吐性能 |
| 公网来源 | HF/MS 固定真实 commit 小文件，经同一 downloader/模型库安装；HTTPS 自定义文件来源；重开/共享/删除 | 可执行模型支持、厂商转换或 NPU 推理 |
| 包与 CLI | wheel 在独立虚拟环境安装；公开来源 pull、第二 alias、doctor --model、delete/purge | 系统发行版/BSP 上完整安装体验 |
| 硬件静态 | 当前 macOS 宿主真实 doctor，host fail、SoC/驱动/runtime/兼容项 unknown，文件完整性 pass | RK3588 真机检测与 ABI/加载/推理通过 |

回环端口与公开网络在默认沙箱受限，使用授权的隔离测试/只读联网执行后通过；没有把权限限制静默跳过为成功。首次完全离线新 wheel 环境因 uv 缓存缺平台依赖包失败，随后公开 PyPI 安装成功；不声称已验证全新环境离线安装。

### 真实公开来源

实际执行：

```sh
uv run --locked python scripts/verify_public_sources.py --work-dir /tmp/rkai-m1-public-final-20261003
```

两个 source 的原始文件先从公开 HTTPS 测量，再经 Library/downloader 从固定 URL 获取并校验。独立 manifest 只描述数据获取，不提供可执行 adapter/backend 或兼容档案，也没有使用 examples 的占位内容。摘要为本地完整性证据，不冒充发布者签名。可重复脚本默认使用自动清理的隔离临时目录。

- HF：`google-bert/bert-base-uncased@86b5e0934494bd15c9632b12f734a8a67f723594`，config.json，570 bytes，SHA256 `7160e1553ad2ca51d8c1cb066be533db31826e12d173824c1bb0cb1a4f187d20`。
- MS：`pengzhendong/wetext@3cda3e627e1c4acc696ff6f81abb86d4add8bd82`，configuration.json，36 bytes，SHA256 `9a9b41224519cc2cd5e993d9bd161a4165bb0f7ddc58a91a63f8b0ced41ad4de`；这是文件 commit，未称为整个分支 tip。
- 自定义 HTTPS 文件来源：HF 固定 commit 的 base_url + config.json，通过相同路径约束和摘要校验。
- 两原仓库缺运行 manifest 均得到 missing_manifest。没有转换或下载权重。

完整记录见 [公开来源 JSON](verification/m1-public-sources.json)，官方协议证据见 [SOURCES](SOURCES.md)。静态 registry 的 manifest/latest 路径与镜像故障使用模拟端点验证；本次没有发布一个公网 rkai registry 或修改上游仓库，不宣称所有镜像均经过公网验证。

### wheel / CLI 烟测

`uv build` 后实际创建 `/tmp/rkai-m1-wheel-env`，使用该环境安装构建 wheel，再运行（模型库全在隔离目录）：

```sh
/tmp/rkai-m1-wheel-env/bin/rkai --help
/tmp/rkai-m1-wheel-env/bin/rkai --data-dir /tmp/rkai-m1-wheel-library --profile debug pull /tmp/rkai-m1-public-final-20261003/manifest.json --target rk3588 --alias evidence --json
/tmp/rkai-m1-wheel-env/bin/rkai --data-dir /tmp/rkai-m1-wheel-library pull /tmp/rkai-m1-public-final-20261003/manifest.json --target rk3588 --alias shared --json
/tmp/rkai-m1-wheel-env/bin/rkai --data-dir /tmp/rkai-m1-wheel-library doctor --model shared --json
/tmp/rkai-m1-wheel-env/bin/rkai --data-dir /tmp/rkai-m1-wheel-library delete evidence --json
/tmp/rkai-m1-wheel-env/bin/rkai --data-dir /tmp/rkai-m1-wheel-library delete shared --purge --json
```

pull ready/unverified、共享安装、两步删除成功；doctor 的 model_files pass，宿主硬件 unavailable（退出 3）是预期。CLI help 仅 doctor/pull/delete，不提供伪装可用的 convert/run/serve。构建文件留在被忽略的 dist，没有发布。

### 未运行与人工验收

真实 RK3588 板卡/OS/BSP/设备节点、NPU 权限与驱动版本读取、Linux readelf/ELF 动态依赖条件、厂商 runtime 加载及 ABI、内存预算和主动 probe **未运行**。未安装厂商 SDK，未改内核/权限，未进行任何转换/推理/HTTP 服务。Python 3.13/3.14 和目标 Linux 分发环境未实测。M1 代码和非硬件证据已落地，真机静态诊断与用户验收仍待完成，不代签完整硬件验收。

人工步骤见 [USAGE](USAGE.md) 末节：公开来源演示、隔离双别名/删除流程、实际 RK3588 上的静态 doctor。用户结论尚未提供。

## 2026-10-04 评审修复复验

修复三项评审问题：恢复任务的暂存父目录符号链接在写入前受控拒绝；blob 完整哈希移出全局 SQLite 写事务；HF/MS 非对象或嵌套错类型元数据返回统一错误，允许镜像回源。文件读写、原子替换、目录移动和清理共享描述符安全边界，可写硬链接拒绝截断；未改变模型库格式或 M1 范围。

实际运行（uv 缓存使用隔离 /tmp/rkai-uv-cache）：

```sh
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv run --locked pytest -q
uv build --offline
uv run --locked python scripts/verify_public_sources.py
```

最终自动结果：**100 passed**，Ruff 通过，18 个 Python 文件格式检查通过；sdist/wheel 构建成功。所有测试数据和模型库使用临时目录，未操作用户默认模型库。

| 证据类别 | 此次新增或重跑的实际检查 | 边界 |
| --- | --- | --- |
| 来源 mock | HF/MS 顶层与嵌套错类型、坏镜像回源、CLI JSON 错误 | 不证明公网镜像可靠性 |
| 真实文件系统/SQLite + 故障注入 | 网络续传/本地复制/blob 复用的暂存父目录链接；打开前目录替换、请求中目录替换、叶符号链接/硬链接、原子写临时链接、移动/清理父目录链接；外部文件未改变；哈希期间另一连接可写；登记前 pending 计划保护共享 blob；重跑安装/删除恢复窗口 | 不证明板卡断电行为或多 GB 吞吐；模拟并发钩子不是性能基准 |
| 真实公开 HTTPS | 三项修复后 HF/MS 固定 commit 小文件、自定义 HTTPS、安装重开、共享别名与删除通过；目录移动/清理加固后的两次完整重跑均在 MS 元数据 HTTP 403 处失败 | 最终公网端到端复验未完成；403 原因未知；仅获取证据，不是可执行模型/NPU 验证 |
| 构建 | 最终源码构建 sdist 和 wheel | 本次未重跑独立 wheel 环境安装；此前记录保留 |
| RK3588 硬件 | 未运行，当前无设备 | 未验证驱动、runtime、ABI、加载或推理；不把 mock 标为硬件 pass |

此次公网记录见 [修复后公开来源 JSON](verification/m1-review-fixes-public-sources.json)，与 2026-10-03 历史记录分开保存。此次成功运行中两真实仓库的固定 commit、文件大小和完整摘要与前次一致；缺少运行 manifest 正确返回 missing_manifest。随后两次重跑的 MS 元数据 403 也已记录，没有将远端失败写成通过。目录移动/清理最终代码以真实本地文件系统集成和全部 100 项测试验证，最终公网端到端仍未完成。人工步骤仍使用 [USAGE](USAGE.md) 的隔离演示与 RK3588 静态 doctor；没有提交、推送、发布或开始 M2。

## 2026-10-04：独立全面审查

完整审查结论、问题影响及修复见 [M1-REVIEW](M1-REVIEW.md)。本轮新增 27 个回归用例，修复数据库链接预检、本地复制大小边界、续传状态损坏、安全且有界的 manifest 读取、递归解析错误、无效镜像 manifest 回源、解码后的 REF 校验、doctor 符号/FIFO/设备节点异常以及构造失败连接释放。

最终执行（缓存 `/tmp/rkai-uv-cache`）：

```sh
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv run --locked pytest -q
uv build --offline
uv pip install --offline --reinstall-package rkai --python /tmp/rkai-independent-review-wheel-env/bin/python dist/rkai-0.1.0-py3-none-any.whl
uv run --locked python scripts/verify_public_sources.py
```

完整测试 **127 passed**（真实回环端口经授权运行）；Ruff/18 文件格式通过，sdist/wheel 成功。独立 Python 3.12.13 wheel 环境执行 release/debug 本地导入、共享 alias、doctor --model、delete/purge 成功；外部输入保留，宿主硬件 unavailable 为预期。初次 wheel 烟测暴露 macOS /tmp 路径别名过度拒绝，规范化显式导入根后重建、重新安装及整套烟测通过。

最终公开来源脚本失败：ModelScope repo/files 元数据 HTTP 403。另行只读响应检查读到业务码 10013201001、“操作过于频繁，请稍后再试!”；不能外推所有历史 403 的原因，亦未证明解除。历史公开下载成功记录保留，不能替代最终复验。未持续重试或变更访问方式规避限流。

RK3588 静态诊断仍未运行，用户验收未代签；本次不能宣布达到 M2 入口。未改默认模型库、未装厂商库或改系统，未提交/推送/发布或开始 M2。

## 2026-10-04：403 与真机缺口实际复核

重新核对生产 sources.py、公开验证脚本、ModelScope 官方客户端源码和实际接口。当前官方接口仍使用 repo/files、Revision/Recursive/Root；本轮没有更换来源、伪装客户端、添加账号凭据或修改代理来规避限制。

实际观察：同一 MS 元数据 URL 的单独 GET 和生产形式 streaming GET 返回 200；直接运行完整脚本的两次观察仍返回 403。仅加 response hook 记录状态的同一完整流程曾通过，钩子没有改 URL/headers/重试策略，失败时只读取有界错误正文。不能据此证明“钩子解决 403”或全部 403 来自同一限流原因。此前记录的明确限流业务响应仍只是该响应的证据。

发现原 read_url 在读取错误正文前抛异常，丢失上游业务码。现在 HTTP 错误保持原 code/退出码，可附 error.details 的 http_status/source_host/数值 upstream_code；只读最多 16 KiB 错误正文，禁止转发原始 Message、HTML、cookies、完整 URL/query 或凭据。错误正文中断/错类型/超限时仍保留 HTTP 状态，不将普通 403 自动认定为限流，也没有加入盲目重试。

修改后实际直接运行：

```sh
uv run --locked python scripts/verify_public_sources.py
uv run --locked pytest -q
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv build --offline
```

**最终原脚本退出 0**：HF/MS 两真实固定 commit 小文件、完整摘要、custom HTTPS、SQLite 安装/重开、共享别名、最终删除、外部 manifest 保留全部通过。此次结果为最终代码的新实证，不沿用历史成功，也不保证公网持续稳定。原脚本 stderr 的 metadata endpoint failed 来自预期的无 manifest 验证；两来源均得到 missing_manifest。

自动测试 **134 passed**，新增 7 项错误正文/安全证据回归属于 mock；Ruff/18 文件格式及 sdist/wheel 构建通过。所有模型库在临时目录，没有操作默认库。本次未重新安装独立 wheel 环境，此前 wheel 使用流程证据保留。

记录分开保存：[请求调查](verification/m1-ms-403-investigation-20261004.json)、[最终原脚本公网通过](verification/m1-public-recheck-20261004.json)、[当前真实宿主 doctor](verification/m1-doctor-recheck-20261004.json)。宿主是 darwin/arm64，整体 unavailable，SoC/NPU/driver/runtime 为 unknown；这不证明 RK3588 通过。尚未获得目标板连接信息，真机静态诊断仍未运行，用户验收未代签，M2 未开始。

## 2026-10-04：回复复核与用户调整

核对新增 sources.py/errors.py 的错误证据处理与 m1-public-recheck-20261004.json，独立运行 `uv run --locked pytest -q tests/test_sources.py`：38 passed。本次未重新运行公网、全套 134 项测试或构建；相应结果依据上节留存记录。用户明确真机问题后续解决，因此不再作为开始 M2 的阻塞项；仍保留真机未验证，不代签硬件通过。同步 HANDOFF/M1-REVIEW/ROADMAP，未开始 M2 实现。

## 2026-10-05：M2 转换与本地执行

用户明确授权 M2，并将真 RK3588 静态诊断后置。本轮保护启动时全部未提交内容；没有提交、推送、发布，未实现 M3 HTTP/systemd、凭据/私有来源或 chat/decide 别名。

实施已落地：复用 M1 来源/固定快照/安装/校验/lease 的 convert；受管精确 CPython 3.12.13、运行/构建依赖 hash 锁与官方 SDK assets；严格 classification/embedding/chat、adapter/backend、run 单次/连续交互；跨模型库准入、session、socketpair 帧、输出事件、取消/背压/崩溃/父退出；doctor 静态/动态加载/主动 probe 分开。具体范围与未支持条件见 [M2-STATUS](M2-STATUS.md)。静态执行契约共用于 doctor/convert/run，禁止把 RKNN/RKLLM backend 声明为 CPU 绕过检查或误记证据。

实际执行：

```sh
UV_CACHE_DIR=/tmp/rkai-uv-cache uv run --locked ruff check src tests scripts
UV_CACHE_DIR=/tmp/rkai-uv-cache uv run --locked ruff format --check src tests scripts
UV_CACHE_DIR=/tmp/rkai-uv-cache uv run --locked pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning
UV_CACHE_DIR=/tmp/rkai-uv-cache uv build --offline
```

**158 passed**，Ruff 与 **34 文件**格式通过，sdist/wheel 构建成功。包含 M1 原 134 项与 24 项新增执行/转换验证：真实 ONNX 分类与 batch embedding、转换安装/导出、旧 alias 失败保护、v1→v2 无损迁移、CPU 明确许可和独立产物选择、损坏/内存拒绝、分帧/大帧/写截止时间、native ONNX Loop 卡死取消与超时、父 SIGKILL 与 guardian 崩溃回收。HF 可变分支推进竞态用故障注入证明只解析一次，不能当公网或 NPU 证据。[检查与源摘要](verification/m2-checks-20261005.json)。

父退出测试额外暂停 guardian：父进程被杀后安装仍 busy，恢复 guardian 并确认 native 进程组退出后才能删除。为解决父死亡先释放 lease 的窗口，锁描述符继承给 guardian/native，guardian 与 native 使用独立进程组，握手报告 native PGID；关闭最后描述符才释放 flock，不显式解开继承描述符。准备命令也归 guardian 管理。慢 socket 整帧阻塞问题改为非阻塞描述符+整体截止时间，保留现有 socketpair，不引入 transport 框架。

真实公开模型脚本：

```sh
UV_CACHE_DIR=/tmp/rkai-uv-cache uv run --locked python scripts/verify_m2_public.py \
  --work-dir /tmp/rkai-m2-public --data-dir /tmp/rkai-m2-final-preparation-library \
  --evidence docs/verification/m2-public-cpu-20261005.json
```

两个 CPU 环境在新隔离目录实际 prepare/import 成功，包版本及锁已留证。MobileNetV2：固定 ONNX 权重与官方图片/标签，实际 top-1 为 812/space shuttle，概率约 0.999680。MiniLM：真实 HF 固定 commit 原始来源 → 输入快照 → ONNX 检查/安装 → 3 文本批量推理，384 维单位向量，相近/不同语义句子 cosine 分别约 0.973298 / 0.046355。stories260K：实际 llama-cpp-python 0.3.16 CPU 流式文本续写、成功历史/reset、输出后取消、不重跑；真实 native 流的慢消费者回收通过（测试缩放父队列为 4、drain deadline=0.1s，生产仍 64/10s）。这些是明确的 **CPU** 证据，所有安装 NPU execution_validation 仍 unverified。[完整公共来源/工具链/转换/输出证据](verification/m2-public-cpu-20261005.json)。不代表通用聊天质量或完整模型精度。

独立 wheel 环境只安装主依赖，不借开发环境 SDK：release/debug 实际分类、三轮真实连续交互与 /reset、NDJSON stdout/提示 stderr 均通过。[wheel 使用证据](verification/m2-wheel-smoke-20261005.json)。wheel 检查包含工具链锁、assets 元数据与 bridge 源码，不含权重/厂商 .so/SDK wheel。最初独立安装位置使用 macOS /tmp 别名时发现受管代码源路径过度拒绝，规范化应用模块路径后重建并通过；不放宽受管文件的 no-follow 边界。

官方 SDK 实物已按固定 commit 取得并测量 size/SHA256、读取 wheel metadata/requirements/许可。RKNN 宿主 C sizeof 与 ctypes 为 8/376/32/24/512；最终 RKLLM bridge 对固定头 C++17 syntax-only 通过。这是 [静态绑定证据](verification/m2-native-static-20261005.json)，**不是目标链接/ABI/dlopen/推理**。宿主 darwin/aarch64；只读检查发现 Docker CLI/app 存在但本地 daemon socket 不可用，没有已验证的 Linux 执行主机。本轮未运行 Linux Toolkit2/RKLLM import/build/export、目标 bridge 链接、设备权限/driver 查询或真实 NPU 取消与精度比较；要求条件和可重复入口已列于 M2-STATUS。Jev/Laya 不注册空 adapter，不宣称支持；调查限制及官方来源已记录。

另实际运行一次 `uv run python scripts/verify_public_sources.py` 复核 M1 公网闭环：**失败于 ModelScope repo/files HTTP 403，upstream_code=10013201001**。本次没有完整公网通过，不能沿用 10-04 成功代替它；历史成功和本次失败同时保留。[本次公网失败记录](verification/m1-during-m2-public-recheck-20261005.json)。没有改身份/端点/凭据、盲目重试或外推所有 403 同因。M1 的本地/协议回归通过，实际外部请求失败单独报告。

真 RK3588 静态诊断与 M2 真机验收仍未完成，用户验收未代签。非硬件实现和真实 CPU 闭环已完成，但 **不能宣布 M2 全部验收完成**。

## 2026-10-05 M2 审查修复复验

本轮修复 9 项问题，具体触发条件、改动与证据范围见 [M2-REVIEW](M2-REVIEW.md)。保留上面的历史 158 项记录，不将旧源码摘要当作修复后证据。

```sh
UV_CACHE_DIR=/tmp/rkai-uv-cache uv run --locked pytest -q
UV_CACHE_DIR=/tmp/rkai-uv-cache uv run --locked ruff check src tests scripts
UV_CACHE_DIR=/tmp/rkai-uv-cache uv run --locked ruff format --check src tests scripts
UV_CACHE_DIR=/tmp/rkai-uv-cache uv build --offline
```

**173 passed in 12.85s**；Ruff、**36 文件**格式和离线构建通过。新增 15 项覆盖真实/注入提交中断、超限解码前拒绝、图像工作区准入、共用校准预处理、多输入样本及危险 NPY、受管构建设置、日志背压与敏感内容隔离、实际编译 native constructor 的父 SIGKILL/超时回收。完整回归包含 M1 与原 M2 生命周期测试。[检查与当前源码摘要](verification/m2-review-fixes-20261005.json)。

固定摘要的 AutoGPTQ 0.7.1 在 CPython 3.12.13 下，通过正式受管命令路径、BUILD_CUDA_EXT=0 与 UV_NO_CACHE=true 临时构建安装，无 CUDA 动态扩展。构建/安装通过；没有导入完整 Linux RKLLM SDK或运行模型，不能记作 SDK 转换/NPU 证据。[依赖源码构建证据](verification/m2-autogptq-build-fix-20261005.json)。

修复后复用隔离验证库运行 `scripts/verify_m2_public.py`，公开 MobileNet、MiniLM、stories260K 的真实 CPU 转换/执行、输出后取消和慢消费者回收重新通过。[CPU 证据](verification/m2-public-cpu-20261005.json)。独立 wheel 重新安装，release/debug 分类与三轮交互/reset 通过；额外使用真实 MiniLM 超长文本验证 context_limit 的事件及 CLI 退出码均为 2。[wheel 证据](verification/m2-wheel-smoke-20261005.json)、[退出码证据](verification/m2-input-status-fix-20261005.json)。未保存私人输入。

Linux SDK import/build/export、量化数据集真实接受与精度、目标动态加载/ABI/驱动和 RK3588 NPU执行仍为 **not_run**。宿主 constructor 清理、NPY 校准数组及 CPU 执行均不能替代它们。缺硬件与 Linux 转换主机的条件及复验入口仍按 M2-STATUS 记录；M3 未实现，不能宣布 M2 全部验收完成。

## 2026-10-05：独立 M2 复审

完整结论见 [M2-INDEPENDENT-REVIEW](M2-INDEPENDENT-REVIEW.md)。独立基线 173 passed；随后复现并修复 CPU fallback 将异机方案计入歧义、环境记录非对象的未受控异常、CLI 终态之后 Ctrl+C 改写退出码三项 P2 问题。新增 7 个针对性回归。

最终运行：

```sh
uv run --locked pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv build --offline
uv run --locked python scripts/verify_m2_public.py --work-dir /tmp/rkai-m2-public --data-dir /tmp/rkai-m2-final-preparation-library --evidence docs/verification/m2-independent-public-cpu-20261005.json
```

最终串行全量 **180 passed**，Ruff/36 文件格式和 sdist/wheel 构建通过；公开 CPU 分类、embedding、流式文本/取消/慢消费者流程通过，新证据单独保存。复用了已有隔离下载目录和受管环境，未重建新环境、未重装最终 wheel，未操作默认模型库。

验证过程中曾将公开执行与测试并行，竞争设计中的同 UID 主机独占锁，造成 8 failed/172 passed；公开执行结束后串行重跑通过。没有通过放宽锁规避失败。新增 late-interrupt 和环境记录测试是故障注入，fallback 用例的 NPU 不可用也是注入，不能作为硬件证据。

Linux SDK/真 RK3588 仍未验证；未重跑 M1 公网脚本；未提交、推送、发布或开始 M3。

## 2026-10-05 独立复审报告续接核对

阅读报告并核对 execution/environments/run_cli 及新增测试。针对性运行：

```sh
UV_CACHE_DIR=/tmp/rkai-uv-cache uv run --locked pytest -q tests/test_execution.py::test_fallback_filters_foreign_cpu_targets tests/test_execution.py::test_corrupt_environment_receipt_is_controlled tests/test_m2_review.py::test_late_cli_interrupt_preserves_terminal_status -W error::pytest.PytestUnhandledThreadExceptionWarning
```

**7 passed in 0.91s**。本轮没有生产代码修改，没有重新运行 180 项全量、公开 CPU 或独立 wheel；上节为独立复审的实际证据。只修正文档中 M2 未开始和功能覆盖状态的过时措辞；未测 Linux SDK/真 RK3588，未新增 decision 或组件适配。

## 2026-10-05：提交前检查

用户授权提交当前成果。检查 95 个待纳入版本管理的文件（约 1.19 MB），未发现二进制内容、超大文件、模型权重/动态库/私钥文件或常见凭据模式；git diff --check 通过。依赖环境、缓存、dist 仍由 .gitignore 排除。本轮仅续接文档变化，沿用最近 180 项回归及公开 CPU 实测，不重复声称运行测试；M2 硬件/功能边界不变，M3 未实现。
