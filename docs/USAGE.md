# M1 使用指南

M1 提供公开来源获取、本地导入、恢复、安全删除与静态诊断。只安装选定方案的文件，不进行转换、推理或服务。文件 ready 表示摘要和安装契约通过；execution_validation 始终 unverified。尚无 rkai 已验证执行模型清单。

## 安装与统一检查

在仓库根目录运行（首次需要公开 PyPI 网络）：

```sh
uv sync --locked --python 3.12
uv run --locked rkai --help
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv run --locked pytest -q
```

Python 支持范围见 pyproject.toml，首个实测基线为 3.12。获取 CLI 在 Linux/macOS 的 POSIX 文件锁环境使用；RK3588 硬件诊断要求 Linux aarch64。无 Windows 主机支持承诺。依赖锁见 uv.lock；无需安装厂商 SDK 或接受厂商许可即可运行 M1。

全局配置参数放在命令前；`--json` 可放命令前或后。stdout 是结果，stderr 是日志。默认 release/INFO；debug/DEBUG 为同一套实现，可用 `--log-level` 覆盖。debug 输出安全的阶段时间和调用帧名称，不输出绝对输入路径、URL 查询或输入内容。

## 配置

默认读取 `$XDG_CONFIG_HOME/rkai/config.toml`（未设置时 `~/.config/rkai/config.toml`）；模型库默认 `$XDG_DATA_HOME/rkai`（未设置时 `~/.local/share/rkai`）。数据目录和 runtime 路径必须绝对；`--data-dir` 覆盖配置，不回写。服务使用不同身份时将由 M3 显式配置。

```toml
# 将路径替换为自己的绝对目录
 data_dir = "/data/rkai"
 default_registry = "hf"
 profile = "release"
 # log_level = "WARNING"

# hf/ms 已内置；配置下面的访问设置时保持公共来源身份
[registries.hf]
kind = "huggingface"
origin = "https://huggingface.co"
# mirrors = ["https://自己的兼容镜像"]

[registries.internal]
kind = "https"
origin = "https://自己的公开静态站点/rkai"

# doctor 仅检查文件/ELF；这些条目不下载安装 runtime
# [runtime_paths]
# rknn = "/data/runtime/librknnrt.so"
# rkllm = "/data/runtime/librkllmrt.so"
```

自定义 Hugging Face/ModelScope 协议端点使用单独配置名称。来源按限定 REF、`--registry`、default_registry 解析，只访问一个选定来源；限定 REF 与 `--registry` 名称不一致时报 source_conflict。公开 URI 的镜像可以改变访问端点，不能改变身份。M1 不读取 Hub token/cache 或提供私有来源认证。

## 获取与导入

下面的 `publisher/model` 需要发布者实际提供符合 [MANIFEST](MANIFEST.md) 的 manifest 和真实产物；不是现有受支持模型名称。HF/MS 仓库入口固定为 `manifest.json`；没有该文件时报 missing_manifest，不下载原始权重，也不伪装能直接运行。

```sh
uv run --locked rkai --data-dir /data/rkai pull hf://publisher/model@main --target rk3588 --alias demo
uv run --locked rkai --data-dir /data/rkai pull publisher/model@master --registry ms --target rk3588
uv run --locked rkai --data-dir /data/rkai pull internal:publisher/model@latest --target rk3588
uv run --locked rkai --data-dir /data/rkai pull ./产物目录/manifest.json --target rk3588 --alias demo
```

省略 target/plan 时只选本机可识别目标，多个相同优先级方案需 `--plan` 消歧。异机准备时用 `--target rk3588`；显式 `--plan` 也可精确选择获取方案，仍不证明能执行。RK3588 本机自动选择 NPU 方案优先；不会自动准备 CPU fallback。

静态 registry 的路径契约为 `{origin}/{repository}/{selector}/manifest.json`，无 revision 默认 main。latest 是站点实际发布的路径，固定身份为 manifest 原始 SHA256；文件必须有发布者给出的大小和摘要。HF 默认 main，MS 默认 master；latest 必须是 provider 实际 ref，不虚构最新版本。HF 固定仓库 commit，MS 固定 manifest 文件的 commit（不伪称整个分支仓库快照）。manifest 内每个 Hub source 必须已有不可变 commit。

本地无 location 的文件从 manifest 目录复制。用户显式选择的入口父目录先规范化（允许 /tmp 等系统路径别名），manifest 叶及根内产物拒绝符号链接/越界；有 location 的文件按其公开来源下载。外部文件只读，之后删除受管副本不涉及外部原件。examples/manifests 中的占位内容禁止用于真实获取。可重复的真实下载演示使用独立验证夹具：

```sh
uv run --locked python scripts/verify_public_sources.py
```

该脚本自动创建、删除隔离临时目录，只获取两份真实配置文件和同一 HF 配置的 HTTPS 来源副本，验证固定来源、缺 manifest 错误、安装、共享别名、SQLite 重开与删除；不是权重或推理验收。`--work-dir /绝对/空目录` 可保留演示 manifest（数据用途、不可执行）。网络异常直接失败，不自动跳过或标成功。

## 暂停、恢复与清理

pull 在开始下载前记录 operation_id，日志及最终结果都有该 ID；Ctrl+C 在下载阶段记录 paused。网络、摘要或空间失败记录 failed，旧 alias 不变。对同一任务恢复时不再查询 latest，不接受新 REF/plan/alias。

```sh
uv run --locked rkai --data-dir /data/rkai pull --resume OPERATION_ID --json
uv run --locked rkai --data-dir /data/rkai pull --discard OPERATION_ID --json
```

实际运行时将 OPERATION_ID 换成上次输出的 ID。续传仅在同端点强 ETag/If-Range/Content-Range 相符时追加；无验证器、弱 ETag、服务器返回完整 200 或验证器变化时重新下载该文件。损坏或无法识别的续传记录从零下载，不信任旧偏移；最终始终检验 size/SHA256。镜像损坏丢弃临时字节再尝试源站；有限重试，失败保留固定计划。

进入 committing 后 Ctrl+C 延后处理，完成一致性并返回 completed 和 `cancellation_deferred: true`。进程直接消失时，下次打开库自动恢复 committing；downloading/paused/failed 只通过显式 resume 获取网络。重命名、数据库提交和 alias 更新的中断窗口有故障注入覆盖。

`--discard` 锁住该任务，持久记录 cancelled 并清除其 staging，释放准备引用，不改任何 alias。已完成任务不能 discard；同 ID 可重复清理未完成任务。需要删除损坏安装时，先丢弃指向它的失败任务，再删除别名并重新 pull。历史任务不恢复已删除安装。库 schema 不识别时保留数据并拒绝打开。

## 删除与诊断

```sh
uv run --locked rkai --data-dir /data/rkai delete demo --json
uv run --locked rkai --data-dir /data/rkai delete demo --purge --json
uv run --locked rkai --data-dir /data/rkai doctor --json
uv run --locked rkai --data-dir /data/rkai doctor --model demo --json
```

delete 默认删除一个 alias，无其他 alias 才删除安装；purge 删除该安装的全部 alias，结果列出影响名称。活动 lease 或未完成准备任务返回 busy；不强杀使用者。共享内容有其他安装/准备引用时保留；trash 与持久删除记录用于崩溃恢复。alias 更新后的旧安装等 lease 释放后回收，必要时下次打开库完成。

doctor 检查配置、主机/SoC、能正面识别的 NPU 节点、权限、可读驱动版本、显式 runtime 的 ELF 架构/静态符号、数据目录、可测内存及选定安装摘要。读取版本只证明存在，不证明兼容；M1 没有硬件验证兼容档案，ABI/动态加载/模型执行如实 unknown。`--probe` 返回 skipped，未开展主动运行。

配置解析失败时 doctor 给出 configuration=fail、整体 unavailable，退出 2。

退出码：0 操作成功；1 获取/存储失败；2 参数/结构不合法；3 不支持或 doctor 未 ready；4 busy；130 下载被用户中断。doctor 在非 RK3588 主机输出 unavailable 是预期证据，不能作为获取能力失败或假装成硬件通过。

## 人工验收（结论由用户给出）

1. 运行真实来源验证脚本，检查两条固定 revision、摘要以及 execution_validation=unverified；确认未触及默认模型库。
2. 使用脚本保留的 manifest 和另一个隔离数据目录执行 pull（`--target rk3588`），为同一安装设两个 alias，删除其中一个后另一份 `doctor --model` 的 model_files 应 pass；最后 purge 应列出剩余别名，外部 manifest 仍存在。
3. 在实际 RK3588 上用隔离目录运行 doctor；记录板卡/OS/驱动/runtime 路径和逐项状态。unknown 不作为 pass。M1 不进行推理，不能以该步骤声称模型执行成功。

模型库属于当前用户的私人目录。预置数据库/附属文件链接会被拒绝；文件保护不构成针对同一系统用户恶意并发替换目录的安全隔离。不要让不可信用户共同写入模型库。

## M2 转换与运行

实际支持与 Linux/RK3588 待验证入口见 [M2-STATUS](M2-STATUS.md)。这里的 CPU 命令需用户显式开启 CPU；不会执行 RKNN/RKLLM 编译文件。

```sh
uv run --locked python scripts/verify_m2_public.py
rkai --data-dir /data/rkai convert /data/mobilenet-source \
  --recipe examples/recipes/mobilenet-cpu.json --alias mobilenet
rkai --data-dir /data/rkai run -m mobilenet --allow-cpu-fallback \
  --input '{"image":"/data/space_shuttle_224.jpg","top_k":5}' --json
rkai --data-dir /data/rkai runtime prepare onnx-cpu
rkai --data-dir /data/rkai run -m embedding --allow-cpu-fallback \
  --input '{"texts":["第一句","第二句"]}' --json
rkai --data-dir /data/rkai run -m chat --allow-cpu-fallback --interactive
```

示例 CPU recipe 的 target 固定实际验证宿主 darwin/aarch64；在其他主机按 host 改 target 再转换，CPU profile/资产仍需真实验证。后两条 run 需要先安装相应 snapshot/alias，不能凭命令建立支持。公开验证脚本会生成完整配方并在自己的临时库运行三个模型，成功不改变默认库。

JSON 请求：chat 的 messages 为 role=system/user/assistant 与 content，max_tokens 1..4096（默认 256），temperature 0..2；classification 为 image 和 top_k 1..100；embedding 为 texts（1..32）。输入还受大小/上下文预算限制。chat --input 支持纯文本，其 JSON 对象形式使用同一严格 DTO。JSON 结果是 NDJSON 事件，日志/交互提示在 stderr。取消不会把半条助手消息纳入历史，/reset 重置当前会话。

config.toml 可增加 allow_cpu_fallback=false、memory_budget_mb=2048、memory_reserve_mb=256。配置 CPU 开启可被单次 --no-cpu-fallback 覆盖；--allow-cpu-fallback 不下载缺少的 CPU plan，也不掩盖坏文件、权限和 native 失败。

`doctor --load-runtime` 只在隔离进程加载 runtime_paths 中显式的库；不是推理证明。`doctor --model ALIAS --probe` 仅执行安装 manifest 中 adapter.config.probe，未声明则 skipped；CPU 许可仍受同一配置约束。NPU compatibility 在缺真实板卡组合时保持 unknown。

图像文件压缩体积最多 16 MiB，解码前还检查最多 8 Mi 像素。内存准入额外预留两张解码表面及张量工作区，配置预算不足会拒绝加载。RKNN 量化配方的 calibration 是二维样本行；图像直接声明图片 file id，embedding 声明与图输入顺序一致的预分词 NPY。具体格式见 MANIFEST 的转换配方。

请求失败事件包含 error.exit_code；CLI 返回相同分类，输入/上下文超限返回 2。环境准备错误包含失败 stage/program/exit_code 与安全诊断类别，如 missing_module:torch。厂商输出在内存中有界读取，release/debug 都不保存原始 SDK 输出，因此日志不会直接转发路径、URL 或输入内容。
