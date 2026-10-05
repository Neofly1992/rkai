# Rockchip 官方仓库核查与工程结论

核查日期：2026-10-02。以下区分官方已发布事实、rkai 工程选择和必须实际运行的验证。不需要用户代查或逐项决定。

## 1. 固定调查来源

GitHub 官方递归目录 API 返回的快照：

| 仓库 | commit | 本轮确认版本 |
| --- | --- | --- |
| [rknn-toolkit2](https://github.com/airockchip/rknn-toolkit2/tree/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08) | `59a913d172e7f5ff03c9076e2ec7b1b1288ffd08` | 2.3.2 |
| [rknn-llm](https://github.com/airockchip/rknn-llm/tree/f7390530443bf84f0394255a449d7cbe81e69d1c) | `f7390530443bf84f0394255a449d7cbe81e69d1c` | 1.3.1 |
| [rknn3-toolkit](https://github.com/airockchip/rknn3-toolkit/tree/2136cc54fa79ad289c43e56ed9cf7e63ef56439b) | `2136cc54fa79ad289c43e56ed9cf7e63ef56439b` | 1.1.0 |

这是设计调查基线，不代表三个 SDK 都要安装，也不代表已完成 ABI/真机验收。

## 2. 工具链与设备结论

| 路径 | 官方事实 | rkai 采用方式 |
| --- | --- | --- |
| RK3588 通用模型 | Toolkit2 明确支持 RK3588；提供 Lite2 Python API 和 RKNN C API | 首期通用 NPU backend 用 RKNN2 路径，能力按模型适配 |
| RK3588 LLM | RKLLM 明确支持 RK3588，并提供 C/C++ runtime | LLM 使用 RKLLM worker binding，不以某个 LLM 家族设计通用层 |
| RKNN 转换 | 2.3.2 目录存在 Linux x86_64 和 ARM64 cp312 wheel | 可在这两类 Linux 主机执行受支持转换；板端仍受内存等条件约束 |
| RKLLM 转换 | 1.3.1 目录只有 Linux x86_64 cp310/cp311/cp312 wheel | RKLLM convert 需要 Linux x86_64 转换主机，不承诺板端原生转换 |
| 第三代 | 实际仓库名为 rknn3-toolkit；README 列 RK1820/RK1828/RK3572，明确与 Toolkit1/2 不兼容 | 后续独立 backend/转换路径，不作为 RK3588 内置 NPU 的升级替代 |

第三代 README 中出现 RK3588 EVB10 固件入口，不足以证明 RK3588 内置 NPU 使用 RKNN3；不能把主机板名称与加速器类型混淆。

包目录证据：[Toolkit2 ARM64](https://github.com/airockchip/rknn-toolkit2/tree/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08/rknn-toolkit2/packages/arm64)、[x86_64](https://github.com/airockchip/rknn-toolkit2/tree/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08/rknn-toolkit2/packages/x86_64)、[Lite2](https://github.com/airockchip/rknn-toolkit2/tree/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08/rknn-toolkit-lite2/packages)、[RKLLM](https://github.com/airockchip/rknn-llm/tree/f7390530443bf84f0394255a449d7cbe81e69d1c/rkllm-toolkit/packages)。

工程选择：Python 3.12 作为首个验证基线，转换环境各自隔离。三类相关 wheel 均有 cp312，选择有实物依据；不是声称任意依赖版本都能共存。RKLLM 官方还说明 RWKV 转换仅支持 3.12，并有独立 requirements。

convert 是同一 CLI 在合适主机上执行的用例。首期提供本机能力检查与明确的目标产物输出；RK3588 上请求不支持的 RKLLM 转换时说明需要 x86_64，不能偷偷仿真或宣称容器会改变 CPU 架构。远程编排不是当前已实现能力。

## 3. 分发与动态库

**采用 Python wheel 分发 rkai，通过 uv 管理安装/环境；首期不冻结成单文件二进制。** 这是基于 Python SDK wheel 和独立 native runtime 的工程选择，不是厂商要求。编译 Python 不会消除厂商库、C ABI 与内核驱动条件。

采用产品管理的版本化 runtime 目录，按官方来源与固定摘要准备依赖，worker 显式加载对应库。用户不必手工把 .so 复制进 /usr/lib；不覆盖系统库。允许显式使用已安装库，但仍执行版本/架构检测。下载/准备前展示适用的厂商条款，不把依赖伪装成 rkai 的 MIT 代码。具体下载执行尚未实现。

许可核查结论：

- [RKLLM LICENSE](https://github.com/airockchip/rknn-llm/blob/f7390530443bf84f0394255a449d7cbe81e69d1c/LICENSE) 明文允许满足条件的源码与二进制再分发，要求保留声明及第三方许可。这为携带相应 runtime 提供依据，不能外推到仓库内每个第三方文件。
- [Toolkit2 LICENSE](https://github.com/airockchip/rknn-toolkit2/blob/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08/LICENSE) 是 RKNN SDK 专用协议，有用途及第三方条款；不能仅凭 GitHub 公开就认定整个 SDK 是 MIT/BSD 或允许无条件镜像所有二进制。

因此首期 rkai wheel 不内嵌/重新托管整套厂商 SDK，依赖从官方固定来源准备并保留许可。RKLLM 可再分发不等于必须把它塞进主包。内核驱动仍由板卡 OS/BSP 提供，doctor 检查，rkai 不擅自替换内核模块。

## 4. ABI 与驱动：已经能确定的行为

官方分别提供 Linux aarch64 的 [RKNN runtime](https://github.com/airockchip/rknn-toolkit2/tree/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08/rknpu2/runtime/Linux/librknn_api) 和 [RKLLM runtime](https://github.com/airockchip/rknn-llm/tree/f7390530443bf84f0394255a449d7cbe81e69d1c/rkllm-runtime/Linux/librkllm_api)。RKNN wheel 的 manylinux_2_17 标记约束该 wheel，不能外推为每个独立 .so 的完整系统兼容证明。

[RKNN 头文件](https://github.com/airockchip/rknn-toolkit2/blob/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08/rknpu2/runtime/Linux/librknn_api/include/rknn_api.h) 提供 RKNN_QUERY_SDK_VERSION，返回 api_version 与 drv_version，并定义模型优化级别与驱动不兼容错误。doctor 应结合无模型环境检查与单独的可加载模型探测使用这些证据，不只检查文件是否存在。

RKLLM 快照携带 [0.9.8 驱动归档](https://github.com/airockchip/rknn-llm/tree/f7390530443bf84f0394255a449d7cbe81e69d1c/rknpu-driver)。这确认了官方交付版本，不证明 0.9.8 是所有模型/SDK 的统一最低要求。不得将 issue 用户日志当成完整官方兼容矩阵。

尚需执行的验证：读取选定二进制 ELF 依赖与符号版本、RK3588 实际加载、设备权限与驱动查询、真实推理/取消。当前仅核查源码/目录/许可，没有安装或运行 SDK；这些是工程验证任务，不是问用户“支持不支持”。

## 5. 通用模型与 CPU 路径

[官方 Model Zoo](https://github.com/airockchip/rknn_model_zoo) 已有分类、检测、分割及其他类别的逐模型平台表，说明首期验证不应只选 LLM。采用代表性图像分类/检测案例验证通用输入输出，再覆盖 LLM、embedding、decision；某模型列入官方示例仍不等于 rkai 已适配。

CPU 不是把 .rknn/.rkllm 文件交给 CPU 就能运行。采用独立 CPU 产物：ONNX 类模型优先接 [ONNX Runtime](https://github.com/microsoft/onnxruntime)，支持的 GGUF LLM 接 [llama.cpp](https://github.com/ggml-org/llama.cpp)。这是工程方向，具体模型必须验证，不能声称两个引擎覆盖任意模型。CPU fallback 默认关闭，用户开启后按已安装方案选择。

Laya/BGE 具体导出图、量化精度及后处理仍由适配测试确认；不让用户先研究 SDK 再告知结论，也不把“通用平台”表述成所有模型无需适配即可运行。

## 2026-10-03 M1 静态诊断实现

doctor 读取 device-tree、识别 rknpu 驱动关联的 render 节点（不把所有 GPU render 节点都当 NPU）、检查权限和可读 sysfs/debugfs 版本。显式库路径的 ELF64 little-endian/AArch64 只证明结构架构，静态符号可用时仅报告 API 存在；不声称依赖加载或 ABI 通过。没有兼容实测档案，因此兼容性 unknown。RKNN_QUERY_SDK_VERSION 等有上下文 API 留待 M2 隔离 worker。

本次宿主为 macOS arm64，未连接 RK3588。真设备节点、驱动版本读取、Linux ELF 依赖与厂商库实际加载未运行。宿主静态结果见 VERIFICATION；未新增 SDK 支持版本或最低驱动版本承诺。

## 2026-10-05 实物与接入进展

本轮从上述固定 commit 实际取得 cp312 Toolkit2（ARM64/x86_64）、Lite2、RKLLM Toolkit wheel、两类 Linux ARM64 runtime、RKLLM header 和 LICENSE，测量 size/SHA256。完整固定输入见 src/rkai/toolchains/rockchip-assets.json；厂商实物仅放临时目录，没有进入 Git、主环境或系统库。两 SDK wheel metadata 与官方 requirements 用于生成各自独立依赖锁，runtime 只需要实际调用依赖，不安装整套编译 SDK。

RKNN 采用官方 C API absolute-path binding，支持 float32/int32/int64 输入、输出拷贝与 release、版本查询及 destroy；Lite2 并未用于实际执行（包内系统库查找不满足受管路径要求）。RKLLM bridge 使用实际固定 header，模型 init/按请求 clear KV/run/callback/abort/destroy。编译目标需 Linux ARM64 与 C++ 编译器，运行时 rpath 为自身受管目录，不覆盖系统库。

宿主 C 头结构 sizeof 与 ctypes 对比为 8/376/32/24/512；RKLLM bridge 在同一固定 header 下通过 C++17 syntax-only。这只是静态绑定检查，**不代表目标 ELF 链接、ABI、设备加载或 NPU 执行**。官方 runtime 文件已固定且读取实物不等于已运行。当前 macOS ARM64 不能安装 Linux SDK，recipe_lock 在获取权重前拒绝。

受管 prepare 固定下载适用 LICENSE 并保留在 assets：Toolkit2 是 SDK 专用条款，RKLLM 为附带第三方声明的再分发条款，不能统一称为 MIT。动态库不会打进 rkai wheel；准备记录保存许可来源和摘要。主机 Python/C++/libc 与驱动兼容仍要实测；不把 wheel manylinux tag 或仓库携带的驱动版本当作所有组合已通过。

Linux 转换与 RK3588 重复验证入口见 [M2-STATUS](M2-STATUS.md)。尚未运行 Linux SDK import/build/export、目标 bridge 链接、dlopen/ABI、driver 查询、实际 NPU 分类/文本推理与中断。因此没有新增硬件验证通过的兼容档案。

本期受管解释器精确固定 CPython 3.12.13，SDK wheel 的 ABI 为 cp312；主项目仍按 pyproject 声明支持范围。Python 版本也参与环境身份，避免后来 uv 下载不同 patch 却复用旧 receipt。
