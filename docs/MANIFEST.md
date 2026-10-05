# Model manifest v0 草案

状态：2026-10-05：rkai.model/v0 和 rkai.recipe/v0 已实现；通用结构以 manifest.py 为权威，能力/adapter/backend/recipe 分别由所属严格类型校验。未发布，无旧草案兼容层。已支持范围与尚未验证的 SDK 路径见 [M2-STATUS](M2-STATUS.md)。

## 1. 描述对象与边界

平台不限定模型类别。以下 LLM、decision、embedding 示例是检验抽象的样本，不是产品围绕它们构建的理由；视觉、音频等可通过新增能力与适配器接入，不要求伪装成 chat。

工程配置使用 pyproject.toml，用户设置使用 config.toml；机器生成、下载交换的 model manifest 和 recipe 采用 JSON。不同时维护多种等价格式，也不将格式选择列为用户阻塞项。

一份 model manifest 描述一个具体模型变体及其可选执行方案。同一模型家族的不同结构/规模是不同 model id；量化/目标设备差异通常是不同 plan。模型身份、发布 revision、内容摘要、用户别名互不替代。

| 对象 | 内容 | 不包含 |
| --- | --- | --- |
| model manifest | 身份、方案、能力、adapter、组件、文件与来源 | 凭据、绝对安装路径、下载进度、默认 fallback 策略 |
| conversion recipe | 锁定源输入、converter、工具链、目标方案、参数与校准输入 | 任意 shell/Python、用户秘密 |
| conversion record | 实际工具版本、输入/输出摘要、recipe 摘要、验证结果 | 未执行的验证承诺 |
| 本地数据库 | 安装、别名、来源设置引用、任务和恢复状态 | 用数据库字段覆盖不可变产物内容 |
| 用户配置 | 来源端点/镜像、数据目录、资源与 fallback 策略 | 模型结构的重复副本 |

运行 manifest 是“可获取产物目录”，转换配方是“如何生成新产物”。只有源权重而没有运行产物的仓库不应伪装成可运行 manifest；convert 可独立接受源引用与内置配方，完成后生成 manifest。

## 2. 顶层字段

| 字段 | 类型/约束 | 语义 |
| --- | --- | --- |
| schema | 固定字符串 | `rkai.model/v0` |
| model | 对象 | 模型身份与说明 |
| model.id | 非空命名空间字符串 | 逻辑名，如 `publisher/model-name`，不自动等于 HF repo |
| model.revision | 非空发布标识 | 本发布 revision，区别于上游权重 revision；同 revision 不可静默换内容 |
| model.description | 可选字符串 | 人类说明，不参与能力判定 |
| model.license | 可选字符串 | 许可证标识/说明；不是依赖或权重再分发许可证明 |
| sources | id → source 对象 | 被 files 引用的不可变远端快照 |
| files | id → file 对象 | 所有方案引用的文件清单，可共享 |
| plans | 非空对象数组 | 备选执行方案，id 唯一 |

所有对象拒绝重复键。通用层未知字段报错，避免拼写错误静默失效；自由扩展仅存在于有版本的 adapter/backend/converter 配置，其拥有者仍需校验。未知 schema/实现/API 版本明确不支持，不忽略后继续运行。

## 3. 来源和文件

### latest 与固定内容

允许用户使用 `latest`、branch 或 tag 选择模型。`latest` 是可变标签，不表示“对所有源按时间找最大版本”：rkai registry 可维护 latest 指针；直接访问 HF/MS 时按 provider 的真实 ref 解析，源不存在 latest 就明确报错，不伪造通用标签。

每次 pull 开始时，将请求 selector（例如 latest）解析为 resolved revision 和 manifest 内容摘要，整次下载/续传使用同一固定快照。两者都保存在操作与安装记录中。已有安装的 run 使用本地快照，不每次重新查询 latest；用户再次 pull latest 才解析更新，成功安装后更新本地指针，失败保留原安装。

因此 latest 在用户入口完全可用；下文 source.revision 和 model.revision 表达已经解析的快照/发布身份，不用 latest 作为唯一缓存键或断点续传依据。固定标识可以是 provider commit 或发布内容摘要，不强迫用户手填版本号。


source 是带 kind 的联合类型：

- `huggingface` / `modelscope`：`origin`（规范 HTTPS 源站）、`repository`、`revision`（provider 解析后的不可变版本）。branch/tag 仅在用户请求中出现，解析结果不保留浮动引用。
- `https`：`base_url`，每个文件固定内容摘要；即使服务器替换字节也会校验失败。路径解析后不得逃出 base_url 路径。

HF/MS 兼容自定义端点使用对应 kind 与自定义 origin；静态自定义源使用 https。镜像配置在用户配置中匹配来源身份，不写进 manifest。不同 provider 同名仓库不隐式等价。凭据不得嵌入 URL。

file 字段：

| 字段 | 约束 |
| --- | --- |
| path | 产物目录内 POSIX 相对路径；唯一；禁止绝对路径、空段、`.`、`..`、反斜杠及路径穿越 |
| size | 非负整数字节数；角色要求非空时由 adapter 加强校验 |
| sha256 | 64 位小写十六进制，内容摘要 |
| location | 可选 `{source, path}`，引用 sources；path 同样受相对路径约束 |

发布到 registry 的可下载方案，其文件必须有 location。本地 convert 结果可省略 location，文件已在受管产物目录中；分享前需发布文件并补来源，不能泄漏本机路径。没有可验证内容 SHA256 的上游元数据由获取流程生成本地完整性记录，并明确证据来源；不能伪称是发布者给出的摘要。要成为可分发 manifest，发布者须提供完整文件摘要。

运行时拒绝 symlink 逃逸，下载不自动执行文件或解压未声明归档。只下载选中 plan 的引用闭包，不顺带获取所有 CPU/NPU 版本。tokenizer、模板、adapter 配置文件都进入相同清单和摘要验证。

## 4. 执行方案

plan 必需字段：`id`、`target`、`capabilities`、`default_capability`、`adapter`、`components`。

- `target`：`os`、`arch`、`accelerator`；首期 NPU 为 `linux/aarch64/rockchip-npu`，并必须给 `soc: rk3588`。CPU 为 `accelerator: cpu`，不要求 soc。无通配符、隐式 CPU 支持或由工具链版本推导设备支持。
- `capabilities`：数组，每项 `{id, version, features?}`，如 `chat/1`、`decision/1`、`embedding/1`。id 在本方案唯一，实际输入输出契约另行定义；能力名称不能证明模板、工具调用或所有操作均已支持。features 按能力契约声明具体支持；最小基线和扩展规则见 [INTERFACES](INTERFACES.md)，不凭名称猜测。
- `default_capability`：必须出现在本方案内。只有需要自动选择时采用；显式请求不被默认值改写。
- `adapter`：`id`、整数 `api`、`config` 对象。id 解析到已安装受控实现，api 表示配置与调用契约版本。config 可以含 `file:<id>` 文件引用，所有此类引用必须存在并加入依赖闭包；不是可执行导入路径。
- `components`：非空数组，组件 `id` 唯一。每个组件包含 `backend: {id, api}`、`compatibility`、`files`（角色 → file id）和可选 `config`。角色由 adapter/backend 共同契约校验。

`compatibility` 使用受控兼容档案的不可变标识，描述该产物与 runtime/驱动/ABI 的已验证条件。档案由 rkai 发布的支持矩阵提供，记录官方支持条件与本项目验证证据，二者分开；不能是未经验证的 `>=版本` 猜测，也不要求每份自定义产物事先获得 rkai 认证。未知档案阻止执行并说明原因。具体档案结构需在锁定 SDK 和真机验证后确定；本文示例用明确的未验证标识，不能据此运行。

组件顺序不定义数据流。adapter 按组件 id 编排，必要时组合 RKNN/RKLLM；通用 manifest 不建立任意 DAG 执行语言。量化、tensor layout 等字段放入所属 backend/config，经版本化校验；不能仅靠 `.rknn` 后缀判定兼容。

## 5. 解析与选择

1. 用户 model ref 通过显式 provider/registry 配置解析，固定 manifest revision 和原始字节摘要。短名/alias 的查找优先级属于 CLI/registry 规格，存在歧义时不猜。
2. 严格结构和引用检查。M1 获取只校验通用结构及 API/version=1 的字段形状；实际 adapter/backend/能力契约识别由 M2 执行准入负责，未识别实现不能执行。
3. 按请求能力、设备、兼容档案和资源条件过滤方案。所有组件均可运行才算方案可用。
4. 优先 NPU；CPU 只有策略允许且方案与请求兼容时可选。多个同优先级方案必须由已配置偏好/显式选择消歧，不按数组顺序任意选。
5. pull 生成固定的下载计划；run 默认仅选择已安装内容，不隐式下载/转换。显式方案选择失败不偷偷换方案；fallback 行为需要明确策略。
6. 安装前校验全部依赖；执行前核验环境及产物状态。给出被排除方案与原因，方便 doctor 诊断。

CPU fallback 默认关闭，由用户手动开启，不再作为待决策问题。CPU fallback 不保证相同文本/数值，只保证所请求能力语义。已开始执行后不自动重放。多个 plan 可有不同 default_capability；若用户未指定且候选默认能力不同，要求消歧，避免不经意切换任务类型。

## 6. 转换配方（当前实现）

独立严格 JSON 对象，schema 为 rkai.recipe/v0，权威是 conversion.py 的 Recipe。字段为 id、model、converter、inputs、parameters、output_plan；不接受 shell 命令或任意 Python。

| 字段 | 当前语义 |
| --- | --- |
| id | 配方名称，原始 JSON 完整摘要进入记录 |
| model | 输出模型 id 与原始变体标签；最终发布 revision 由转换记录 SHA256 生成 |
| converter | onnx-import / gguf-import / onnx-rknn / hf-rkllm 四个受控名称 |
| inputs | file id → path/size/sha256；必有 model，不能带 location，位置由 SOURCE 与共用 provider 解析 |
| parameters | quantize、calibration 样本行、input_names/input_shapes、quantized_dtype；RKNN/RK3588 量化仅 W8A8，RKLLM 可声明 W8A8/W4A16；不接受任意配置 |
| output_plan | 完整 Plan，单模型组件 model role 指向生成的 model file id，adapter 辅助资产用 file:id |

工具链由 converter 的受管环境注册固定，不让配方另选未经支持的 SDK。相应依赖与 build constraints 带固定版本和下载摘要，官方二进制与许可有固定 commit/size/SHA256。实际环境 ready 记录只在安装及独立 import/load 检查后产生；它不是 NPU 执行证据。

calibration 为二维 file id 数组，每行一个样本，各列按 output_plan 组件的 input_names 排列；多输入样本不能拆成多行。RKNN 图像校准使用与运行相同的 RGB/缩放/归一化/layout，生成 float32 NPY，SDK 不重复归一化；embedding 校准必须提供预先分词的 NPY，各列按图输入检查 dtype/shape/有限值。每个 NPY 最多 64 MiB，先检查头部再分配；SDK dataset 路径含空白时明确拒绝。RKLLM 校准声明形如 [["dataset"]]，该资产为 SDK JSON 校准集。旧的一维 calibration 示例不属于支持契约。

源输入通过 M1 的 acquire_manifest/固定快照/文件闭包/校验/lease 提交；校准集必须是命名输入，不能引用可变目录。转换完成后测量真实输出并生成 rkai.model/v0，安装仍通过同一权威。conversion-record.json 被 adapter 的 file:conversion-record 引用，参与文件闭包与安装校验。记录保存配方、转换实现摘要、工具链、主机、固定源与阶段证据；导出不含本地源绝对路径。

每次转换操作的记录不同，最终 model.revision 以记录摘要为身份，避免同一用户标签的不同编译产物冲突；原始 model.revision 标签保留在 record.recipe。编译、动态加载、NPU 推理、语义比较分开记录。未真机验证仍 converted-unverified / execution_validation=unverified，不能把文件 ready 写成硬件 pass。

实际配方见 examples/recipes。原始示例 manifest 仍仅用于结构审查，不代表能直接运行。

## 7. 身份与摘要

`model.id + model.revision` 是发布标识；source 身份由 kind/origin/repository/不可变 revision 构成。用户 alias 是可修改指针。plan.id 只在该 manifest 内唯一。

文件以 SHA256 识别内容；manifest/recipe 的锁定摘要计算原始 UTF-8 字节，避免引入未定义的 JSON canonicalization。空白修改会产生新 manifest 摘要，这是有意的。组件共享内容不意味着来源和许可记录可以丢弃。不能只凭模型显示名称覆盖安装。

## 8. 示例与反例

[示例目录](../examples/manifests/README.md) 提供四份完整结构示例：Qwen3/RKLLM、Laya/RKNN + CPU 候选、BGE/RKNN、视觉+语言混合组件。地址、hash、版本和兼容档案是占位；不用于实际下载或支持承诺。

| 场景 | 结果 |
| --- | --- |
| Laya 的输出处理变化 | 修改 laya adapter/config 契约，不增加顶层 choice_head |
| BGE pooling/tokenizer 不同 | adapter 配置与文件变化，不套用 chat 模板 |
| 新设备有不同编译产物 | 新 plan/target/兼容档案，不改命令名称 |
| CPU 与 NPU 支持能力不同 | 各自声明 capabilities，不复制模型顶层全集 |
| 一模型需要 RKNN 与 RKLLM | 同 plan 多组件，整体准入 |
| 文件摘要不符 | 安装失败，不 fallback 掩盖损坏 |
| 缺 tokenizer 引用 | 结构或 adapter 校验失败，不等推理崩溃 |
| 使用 main 作为 HF 固定 revision | 拒绝发布解析结果，先固定不可变 revision |
| 未知兼容档案或插件 id | M1 可存储但执行验证 unverified；M2 执行准入须 unsupported，不执行远端代码 |

## 9. 本轮 review 重点

优先评审 model/plan/component 是否足以表达各种模型，以及下载和转换是否能进入同一运行路径。CPU fallback 和格式等工程默认不需要反复确认。精确能力 DTO、兼容档案格式、来源解析优先级、转换锁和数据库 schema 是后续规格，不在当前文档中伪造完整实现。

## M1 校验与获取边界

权威校验器在 src/rkai/manifest.py：严格类型（布尔值不是整数）、所有对象拒绝重复 JSON 键、未知通用字段、非有限 JSON 数值、未知 schema/API/version、路径穿越/文件目录冲突、缺失引用、重复 id 与默认能力错误。安装根 manifest.json 与其原子写临时名 manifest.json.tmp 保留，不可用作文件路径或目录。所有 adapter 与 component config 中嵌套 file: 引用均加入闭包。4 MiB 是 manifest 解析预算，不是模型文件上限。

M1 不建立虚假的通用 adapter 或兼容档案来让结构夹具成为运行模型；自由配置的所属实现语义、能力细节、角色约束留给 M2。产物可被获取为 ready/unverified；doctor 静态检查不得将未知实现、未经验证的 ABI/质量写成 pass。recipe 章节仍是 M2 规格，M1 不解析/执行转换配方。

HF/MS 发布 source.revision 要求完整 40 位小写 commit，https 用每文件摘要固定内容。用户 selector 和 source 的固定 commit 分别记录；本次真实验证的 SHA256 为本地 HTTPS 完整性测量，证据类型不冒充发布者签名或支持认证。

实际文件访问由 src/rkai/files.py 统一执行：目录逐层通过 dir_fd 与 O_NOFOLLOW 打开，叶文件先确认普通文件再读写；可写文件拒绝多硬链接，验证前不截断。续传、临时原子写、本地复制与内容复用均使用此边界；目录移动与递归清理同样锚定父目录描述符。路径安全在 I/O 时保障，不能只依赖安装完成后的摘要或符号链接检查。
