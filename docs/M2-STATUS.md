# M2 实现与验证边界

2026-10-05。用户已授权实现 M2；M1 真机静态诊断后置。M3 已另获授权并实现本地服务子集，边界见 M3-STATUS。本文记录当前执行范围，概念规格不等于已经支持的模型。

## 实施顺序与当前结果

先固定官方 SDK/许可及工具链，再复用 M1 输入快照与提交实现 convert；随后实现能力 DTO、adapter/backend、socketpair worker、session 与退出清理；最后验证公开代表模型、故障路径、doctor 和分发。所有安装仍由 `Library.acquire_manifest` 提交；没有第二套转换安装权威。

| 路径 | 已实现 | 实际证据 |
| --- | --- | --- |
| ONNX → 独立 CPU ONNX | checker、拒绝 external tensor data、ORT 加载、输入快照、安装/导出 | MobileNet、MiniLM 真实转换/加载/推理通过 |
| GGUF → 独立 CPU GGUF | 格式检查、真实 llama.cpp 加载、安装/导出 | stories260K 真实流式文本续写、输出后取消通过 |
| ONNX → RKNN | Toolkit2 2.3.2 实际 API、固定 SDK、校准文件、build/export/release | macOS 主机拒绝通过；Linux SDK 安装与编译尚未运行 |
| HF 权重 → RKLLM | Toolkit 1.3.1 实际 API、只读本地快照、拒绝 remote custom code、build/export | 需要 Linux x86_64；实际 SDK 编译尚未运行 |
| RKNN / RKLLM runtime | 显式受管 C API / 按官方头编译的 C++ bridge | RKNN 宿主结构布局比对、RKLLM 头文件语法检查通过；目标链接、动态加载、ABI/驱动与 NPU 推理均未运行 |
| 管理与退出 | lease、单活跃方案、内存准入、session、事件、取消、背压、崩溃、父退出 guardian | 真实 CPU 与 native 卡死 ONNX 故障测试通过；不是硬件证据 |

非 LLM 分类和批量 embedding 使用统一 Session；自回归流式文本走同一管理层。CPU 实测文件、固定来源及输出见 [公开证据](verification/m2-public-cpu-20261005.json)。stories260K 是微型文本续写模型，不代表可用的指令聊天质量。

## 当前支持范围与限制

- 能力：classification、embedding、chat。严格 DTO 拒绝 bool 代整数、非有限概率/向量和输出形状错配；classification 输入 image/top_k，embedding 输入 texts，chat 输入 messages/max_tokens/temperature。decision、音频、检测和混合 RKNN/RKLLM 多环境编排尚未实现，明确拒绝。
- adapter：image-classification、sentence-embedding、gguf-chat、prompt-chat。模型名称不进入 CLI/存储/session 分支。当前 adapter 操作一个模型组件；扩展编排归 adapter。
- converter 是受控注册名称，recipe 必须显式提供实际输入 size/SHA256 和 output_plan。原始目录没有统一可信的标签、模板、pooling、内存估算；本期不猜测这些语义。现成真实 CPU 配方与未真机验证的 RKNN 配方见 `examples/recipes`；它们不是已发布运行 manifest。
- 支持本地目录/原始文件及 HF/MS 原始来源引用；HF/MS 文件固定为 provider 返回的 commit。custom HTTPS 发布运行 manifest 仍走 M1 pull；custom HTTPS 原始转换来源尚未定义元数据协议，convert 明确拒绝。ONNX 外部 tensor 文件暂不支持，不绕过快照规则从任意路径读取。
- run 不准备环境、不下载；缺环境提示 `runtime prepare`，缺 CPU 快照明确报错。CPU 默认关闭，只能显式开启；NPU 不可用时先按本机 OS/arch 与请求能力过滤，再选择同一 manifest 已安装的唯一 CPU plan，能力一致并重验文件。权限/坏文件/加载/推理错误不降级，输出后不重跑。
- 准入按计划声明的 native 加载/请求总峰值，加上 adapter 的图像解码/张量工作区，再与系统余量检查；图像压缩文件最多 16 MiB，解码前限制 8 Mi 像素，不能用压缩体积代替内存峰值。默认预算 2048 MiB、Linux 可用内存余量 256 MiB。macOS 仅采用显式预算，不能冒称测得空闲内存。示例 512 MiB 是保守准入额度，未测 NPU 峰值；没有强制 RSS/cgroup 限额。
- 当前保守地限制同一 UID 的主机仅一个活跃执行方案，跨 data_dir 共享锁；不宣称不同 UID 的 NPU 全局调度隔离。锁目录在系统临时目录的真实路径下，私有且检查所有者。
- session 历史只留内存；成功完成才追加助手消息。取消/崩溃退役 native context；交互下一请求重新打开。模型 tokenizer/模板需要配方明确，RKLLM SDK 未提供明确停止原因时返回 unknown，不伪造 stop。

## 可重复入口

```sh
uv sync --locked --python 3.12
uv run --locked python scripts/verify_m2_public.py
uv run --locked pytest -q
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv build --offline
```

公开脚本默认在隔离临时库准备两个 CPU 环境并读取固定公开源；会下载约 106 MiB 权重及环境依赖。`--work-dir`、`--data-dir` 可显式复用私有验证目录，`--evidence PATH` 保存摘要与公共输出，不保存私人输入。不使用默认模型库，不把权重/动态库写进仓库。

有 Linux ARM64/x86_64 转换主机后，用公开脚本准备的 MobileNet 原始目录与对应标签：

```sh
rkai --data-dir /data/rkai-convert convert /data/mobilenet-source \
  --recipe examples/recipes/mobilenet-rk3588.json --output /data/mobilenet-rk3588
```

输出目录必须不存在；不要使用原始输入目录作为输出。RKNN 配方为固定 1×3×224×224、不量化版本，adapter 承担 RGB 归一化。量化需另外声明校准资产及验证预处理/精度，不能复用没有证据的配置。

有 RK3588 Linux ARM64 板卡、可访问的 rknpu 字符设备、BSP 驱动及相应 ELF 依赖后：

```sh
rkai --data-dir /data/rkai runtime prepare rknn-runtime
rkai --data-dir /data/rkai pull /data/mobilenet-rk3588/manifest.json --alias mobilenet
rkai --data-dir /data/rkai doctor --model mobilenet --json
rkai --data-dir /data/rkai run -m mobilenet \
  --input '{"image":"/data/space_shuttle_224.jpg","top_k":5}' --json
```

必须保存板卡/OS/内核/驱动/API 版本、实际 NPU 加载与输出，比较 CPU 参考 top-1 812 和约定精度，再实测取消/父退出/故障恢复。静态文件检查、dlopen、CPU 或 mock 均不能替代这些证据。`doctor --probe` 仅运行 manifest adapter config 中明确声明的 probe；未声明时 skipped，不凭空选择输入。

RKLLM 另需 Linux x86_64 CPython 3.12.13 转换主机，完整 HF 权重/tokenizer/config 的固定快照、官方实际支持的架构及模板、CPU 参考与 SDK 校准集；板端 `runtime prepare rkllm-runtime` 还需 C++ 编译器。受管桥在目标架构上链接官方 1.3.1 库。本文不提供未经实际编译的假运行 manifest，也不声称任意 HF 模型能成功转换。

## Jev / Laya 调查结果

[Jev 官方文档](https://docs.typesafe.ai/introduction) 给出 Choice/Score/Noul 与服务接口，是模型产品的能力描述。本次官方资料中未取得可固定下载的权重、导出图或 RK3588 运行 SDK，因此不能建立本地可验证适配；没有把它的 API 当作 rkai 内部协议。

[Laya 上游](https://github.com/NandhaKishorM/laya) 提供 ModernBERT/mmBERT 编码器、专用 decision head 和 [CPU ONNXAgent](https://github.com/NandhaKishorM/laya/blob/main/docs/reference/agent.md)。这说明存在 CPU 导出方向，不证明 Rockchip 支持。落实需要固定完整 checkpoint/导出图、option marker/tokenizer、head 与校准资产，并对 choice/score/noul 做参考比较；现有 sentence-embedding adapter 的 mean pooling 无法承担这些语义。未运行其完整导出与 SDK 算子/形状检验，未注册空 decision adapter；当前不声称支持 Laya，也不武断宣称其不能转换。

M2 的非硬件实现与 CPU 证据已经落地，但 Linux SDK 实际编译、目标 ELF 加载/ABI 和真 RK3588 验收仍缺证据；**不能宣布 M2 全部验收完成**。

## 2026-10-05 审查修复

9 项审查问题已修复，最新全量回归 173 项通过；详见 [M2-REVIEW](M2-REVIEW.md)。RKLLM 转换环境单独设置 BUILD_CUDA_EXT=0 与 UV_NO_CACHE=true，避免 CPython 3.12 上 AutoGPTQ 0.7.1 的 CUDA 源码构建失败及共享构建缓存混用；设置进入环境身份与 ready 记录。固定摘要的 AutoGPTQ 临时源码安装已实测通过，仅证明依赖打包，不证明 Linux SDK 能导入、转换或 NPU 执行。

RKNN 校准现在按图输入分组，并复用运行预处理；W4A16 在 RK3588 量化路径明确拒绝，W8A8 显式传给 SDK。量化编译和精度仍需真实 Linux SDK/板卡验证。doctor 动态加载复用现有 guardian，父进程退出或超时后先停止 native 再返回。native stdout/stderr 连续有界读取，仅保留固定诊断类别，不保留原始 prompt/路径/URL。

后续独立复审另修复 3 项 P2，最终 180 项测试通过，公开 CPU 流程独立复跑通过；详见 [M2-INDEPENDENT-REVIEW](M2-INDEPENDENT-REVIEW.md)。这不补齐 Linux SDK/NPU 或 decision/混合组件的实际支持证据。
