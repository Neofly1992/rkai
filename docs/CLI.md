# 命令与使用流程规格

状态：2026-10-05：doctor/pull/delete/convert/run/runtime prepare 已实现；serve 已实现（服务身份/systemd 见 SERVICE）。实际命令与支持限制见 [USAGE](USAGE.md)、[M2-STATUS](M2-STATUS.md)。

## 1. 公共约定

命令集维持 doctor/pull/convert/delete/run/serve。配置优先级为显式命令参数 > 指定或默认 config.toml > 内置默认。命令参数不回写配置。`--config PATH` 指定配置，`--data-dir PATH` 显式覆盖模型库；路径不随当前工作目录隐式改变。用户目录采用 XDG，systemd 显式传入服务配置。

`--json` 输出机器可读结果；有事件流时使用逐行 JSON，每行完整对象。stdout 只放结果，stderr 放诊断和进度，非 TTY 不输出颜色/动态进度。退出码：0 成功；1 操作失败；2 参数/输入无效；3 环境或模型不支持；4 资源忙/准入失败；130 用户中断。机器错误 code 提供更细原因，不能靠匹配错误文案处理。HTTP 来源错误可附 error.details（http_status/source_host/数值 upstream_code），保留诊断事实但不转发原始远端正文或凭据；403 本身不能自动归因为限流。

模型引用规则：本地 run/delete 默认解释为已安装 alias；pull 的短名由配置中的默认 registry 解析，不遍历全网猜测。显式源使用 `hf://组织/仓库@引用`、`ms://组织/仓库@引用`，自定义 registry 使用 `registry-name:模型名@引用`。本地 manifest 使用路径参数。引用中的保留字符按 provider 规则编码；无 revision 使用该源默认引用，latest 仅由实际源或 registry 提供。源仓库没有 rkai manifest 时，pull 明确返回 missing_manifest，不猜一个目录就是可运行模型，提示 convert 路径。

不新增隐式下载：run/serve 使用本地已安装快照。pull/convert 才能获取网络文件。已有 alias 只在新安装成功后更新；失败仍指向旧快照。相同内容重复获取可复用，正在使用的旧安装保留到 lease 释放。

## 2. doctor

```text
rkai doctor [--model ALIAS] [--probe] [--json]
```

默认静态检查系统/SoC、NPU 节点权限、驱动、runtime 文件与加载条件、数据目录及配置。`--model` 加文件/方案兼容检查。`--probe` 执行有界主动运行检测，必须使用用户已安装且适配器提供探测输入的模型；没有输入或模型则 skipped，不自动下载探针模型。若探测需占用 NPU，遵守与推理相同的设备锁。

每项含 status、evidence、impact、remedy；整体 ready/degraded/unavailable。必需项 unknown 不能判 ready；CPU 可用不掩盖 NPU 失败。主动探测成功仅证明该测试路径，不证明所有模型支持。doctor 不修驱动、不改权限、不接受许可、不安装 SDK。

## 3. pull

```text
rkai pull REF [--registry NAME] [--target rk3588] [--plan PLAN] [--alias NAME]
rkai pull --resume OPERATION_ID
rkai pull --discard OPERATION_ID
```

### registry 选择

三种写法共用一个解析器：

```text
rkai pull org/model --registry hf
rkai pull hf://org/model@main
rkai pull org/model
```

前两者分别是显式配置名称与显式 provider URI；第三种使用 config.toml 的 default_registry。内置配置名称 hf/ms 分别指公开 Hugging Face/ModelScope；出厂默认 hf，不假设已经存在 rkai 官方托管 registry。自定义来源需先在配置中登记名称、kind 和 origin；镜像只属于该来源的访问设置。

优先级：完整限定 REF 或 --registry 指定来源；两者同时出现时必须指向同一配置来源，否则参数错误，不静默覆盖。未限定 REF 才采用 default_registry。只有唯一来源被访问，不按顺序跨源搜同名模型。hf:// 与 ms:// 指公开源；自定义同协议端点使用 --registry 自定义名，或自定义名:模型引用。操作计划保存实际来源身份与固定 revision。

```toml
# 用户配置示例；不是已部署的 registry
 default_registry = "hf"

[registries.hf]
kind = "huggingface"
origin = "https://huggingface.co"

[registries.ms]
kind = "modelscope"
origin = "https://modelscope.cn"
```

pull 获取的是已发布运行 manifest/产物；选择 HF 并不意味着任意 HF 权重可直接运行。源仓库缺运行 manifest 时明确提示 convert，不悄悄转换。

默认目标为本机可识别设备；异机准备产物时显式 target（或精确指定 plan）。解析引用和不可变快照 → 选择方案 → 检查空间 → 下载引用闭包 → 校验 → 提交安装 → 更新 alias。自动选择只在唯一匹配或已有偏好时成立；多个量化方案无偏好时列出候选，要求显式 plan，不按文件顺序选择。

latest 每次 pull 重新解析，断点恢复则沿用原计划；不会续传到另一个 revision。下载中 Ctrl+C 持久暂停，保留已校验文件；恢复按操作 ID，不依赖内存任务。远端内容变化使 partial 失效时重新取对应文件，不拼接不同内容。磁盘不足在写入前尽量发现，实际写满仍按失败恢复。

公开源与镜像采用同协议访问。镜像故障可回到源站，摘要不符必须记录端点错误并丢弃该文件临时字节；最终安装仍以固定摘要为准。

## 4. convert 与 runtime prepare

```text
rkai convert SOURCE --recipe PATH [--registry NAME] [--output NEW_DIR] [--alias NAME]
rkai runtime prepare {onnx-cpu,llama-cpu,rknn-convert,rknn-runtime,rkllm-convert,rkllm-runtime}
```

本地 SOURCE 为目录或配方 model.path 对应的单文件；本地资产全部由 recipe.inputs 声明实际 size/SHA256。HF/MS 来源引用复用 pull 的 registry/默认来源解析和固定文件 revision；不需要仓库已有运行 manifest。自定义 HTTPS 发布 manifest 继续使用 pull；原始 HTTPS 转换缺元数据协议，明确拒绝。配方必选，目标在 output_plan.target 中，不存在独立 --target 参数或自动猜测 adapter/模型语义。真实配方示例见 examples/recipes；结构见 MANIFEST。

先校验配方、主机和支持条件，再准备独立工具链、获取不可变源快照及 lease、转换和验证产物。准备/download 属于此显式命令；run 和 doctor 不自动准备。环境不污染主环境；许可从固定官方来源先取得并保留，厂商二进制不进入 rkai wheel，不替换系统库/驱动。

RKNN 2.3.2 转换需 Linux ARM64/x86_64 CPython 3.12；RKLLM 1.3.1 转换需 Linux x86_64。目标运行均为 RK3588 Linux ARM64，转换主机不等于运行目标。macOS 请求厂商转换在下载权重前拒绝，提示在支持的 Linux 主机运行同一命令及 --output，无隐式远程编排。

默认通过共用模型库提交，并且仅成功后更新 alias。--output 导出可搬移 manifest、产物和受摘要保护的 conversion-record.json；目录必须不存在，不覆盖现有目录。板端 pull 导出目录内 manifest，仍由 M1 只读校验并复制。原始输入不修改/删除，绝对原始路径不写入导出记录。

转换的 completed 只表示产物已提交/导出，execution_validation 仍 unverified。记录有配方/实现/工具链摘要、固定源快照、主机、SDK/图检查证据和真实输出摘要。重开只恢复可证明的提交结果；未完成编译标记失败，不恢复半个 native 文件或自动联网重编译。取消/失败先证明进程停止再释放源 lease；详情见 LIFECYCLE。

## 5. run

```text
rkai run -m ALIAS [--capability NAME] [--input TEXT | --input-file PATH] [--json]
rkai run -m ALIAS --interactive [--allow-cpu-fallback]
```

无输入选项时，非 TTY 从 stdin 读取一个请求；TTY 提示使用明确输入或 interactive，不猜用户想进入聊天。纯文本是 chat 的便捷输入；其他能力使用有 schema 的 JSON 输入文件或 stdin。媒体输入引用本地文件；由 CLI 解析为受控 media 对象，adapter 不任意打开用户字符串路径。

一次 run 创建一个 session，执行结束后释放。interactive 仅在能力声明支持交互时可用，首期实现聊天交互；退出会话时释放模型租约。聊天历史只存在当前 session，不自动写磁盘；`/reset` 清空历史，下一次请求从清空的 native cache 开始，`/exit` 或 EOF 退出。生成过程中 Ctrl+C 取消当前请求，停止完成后回到提示符；空闲时 Ctrl+C 退出。被取消的半条助手回复不进入下一轮上下文。

默认 NPU，fallback 默认关闭；`--allow-cpu-fallback` 可开启，另设 `--no-cpu-fallback` 显式覆盖配置，二者互斥。CPU 选择发生在执行前，仅用于确定无 NPU 可用方案/运行条件的情况；数据损坏、输入错误、权限错误和执行中崩溃不自动重跑。需要但未安装的 CPU 文件明确报错。输出报告实际方案/设备和选择原因。

CLI 流式输出按能力呈现：chat 增量文本，embedding/classification 输出结构化结果；decision 尚未实现。--json 输出 NDJSON 事件，提示及日志走 stderr。公共事件流不强制叫 token。

## 6. serve 与 delete（已实现）

```text
rkai serve [--port PORT] [--preload ALIAS]
rkai delete ALIAS [--purge]
```

serve 默认绑定 127.0.0.1:8080，仅本地访问；端口占用明确失败，不自动换端口。启动不必加载模型；只列出已安装的能力，按请求延迟加载。preload 明确指定启动时加载的模型，任一必需 preload 失败则启动失败。HTTP ready 与具体模型 ready 分开。systemd 管服务进程，不让 worker 独立注册服务。

delete 默认移除 alias；若没有其他 alias/安装引用，再移除不被使用的该安装及其专属文件。`--purge` 表示删除所指安装的所有 alias，显示影响范围；活动 session/正在准备的安装始终返回 busy，purge 不强杀请求。共享文件仅在引用为零且无 lease 时清理。删除流程使用持久删除记录与 trash 目录恢复；用户外部原文件永不删除。

## 7. 典型路径

- 现成模型：doctor → pull → run；全过程不要求用户选择 RKNN/RKLLM。
- 自行转换：合适主机 convert --output → 搬移目录 → 板端 pull 本地 manifest → doctor --model --probe → run。
- 本地应用调用：pull → serve → 客户端请求；无需先开交互聊天，也不隐式获取模型。
- 新模型发布：实现/注册必要 adapter 与 converter → 生成产物/manifest → 上传公开源 → registry 建立名称映射；增加模型不修改通用下载/会话流程。

## 8. Release / Debug 与日志

提供 release 与 debug 两种运行配置，使用同一业务实现和数据格式。release 为正常分发配置，默认 INFO；debug 默认 DEBUG，增加阶段耗时、模块和调用栈等诊断。Python 不靠复制一套源码或 python -O 定义 release，也不靠关闭校验取得“优化”。

全局 `--profile release|debug` 选择配置，`--log-level DEBUG|INFO|WARNING|ERROR` 单独覆盖级别，具体优先级为显式 log-level > 用户日志配置 > profile 默认。默认 profile 为 release；`rkai --profile debug ...` 可在正式安装上复现诊断。两种配置都必须保留取消/资源/输入校验，并运行相同业务正确性测试。

日志写 stderr，机器结果写 stdout；serve 可交给 journald。按 source/storage/conversion/runtime/worker/http 分类，关联 operation_id/request_id/session_id。debug 不记录原始 prompt、媒体、凭据和未脱敏绝对路径；native 库日志单独适配捕获，避免污染协议。异常栈只在诊断输出中展示，HTTP 响应不泄露内部栈。

## M1 实现补充

- 通用 manifest 结构/引用与文件校验由一个严格模型维护；M1 不加载 adapter/backend。可获取声明未知实现的产物，但不声称能执行；doctor 的执行契约与兼容性检查是 unknown。配置暂不提供方案偏好，歧义用 --plan。
- HF/MS 入口文件为 manifest.json。MS 使用该文件元数据的固定 commit，不将每文件 Revision 误称整个仓库的 branch tip。静态 registry 路径为 origin/repository/selector/manifest.json，摘要固定快照。详细格式与真实来源验证见 USAGE。
- paused/failed 任务可显式 --discard：记录 cancelled、释放准备引用、清理任务暂存，不改变 alias；已完成任务应使用 delete。committing 的真实 Ctrl+C 延后至一致性完成，返回明确 completed/cancellation_deferred，而不是谎称 alias 未更新。
- 全局配置/profile/data-dir/log-level 放在子命令前；--json 同时支持前后。日志只在 stderr，未知配置/字段报错；缺 runtime 路径或未经实测 ABI 如实 unknown。

M2 当前实现的错误对象同时包含 exit_code。run 的 failed 事件与进程退出码一致，worker 内发现的上下文/图像无效输入同样返回 2。SDK/构建失败仅附安全阶段与诊断类别；native 原始 stdout/stderr 不转发到 debug 日志。

## M3 serve 实现

`rkai [--config PATH] [--data-dir PATH] serve [--port PORT] [--preload ALIAS]`。
服务配置使用严格 [http]/[http.limits]，显式 CLI port/preload 覆盖配置、不回写。
preload 仅一个 alias（单活跃方案）；不能提供多个模型列表绕过设备锁。
HTTP 入参、队列/模型状态及协议边界见 API；systemd 分发与身份见 SERVICE。
