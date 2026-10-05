# 设计依据与待核验事项

查阅日期：2026-10-02。在线默认分支会变化；以下是设计依据，不是锁定的构建输入。

- [Rockchip RKLLM 官方仓库](https://github.com/airockchip/rknn-llm)：区分转换工具与板端 C/C++ runtime；官方组合示例可同时使用 RKNN 编码器和 RKLLM 语言组件。由此支持“组件级 backend”和“转换主机/运行目标分开”的设计。
- [Rockchip RKNN Toolkit2 官方仓库](https://github.com/airockchip/rknn-toolkit2)：转换与部署工具入口，具体 Python、系统、SoC 支持必须按选定 release 核验。
- [Laya 上游仓库](https://github.com/NandhaKishorM/laya)：决策模型及其结构化输出的背景；本轮没有证明其可转换或运行于 RK3588。
- [Python socketpair 文档](https://docs.python.org/3/library/socket.html#socket.socketpair)：父子双向 socket 候选机制；不会自动建立进程退出保证。

已找到第三代正式仓库 [rknn3-toolkit](https://github.com/airockchip/rknn3-toolkit)，已完成官方目录、许可和工具链平台调查，见 [核查结论](ROCKCHIP.md)。

参考本地 mox 的 CONTRIBUTING.md、docs/SOURCES.md、docs/ARCHITECTURE.md，以及 ModelArtifact、ModelLibraryPorts、ArtifactStore：采用来源身份与镜像分离、固定 revision、暂存/校验/提交、崩溃恢复和真实验证原则。没有复制 Swift、GUI、Keychain、MLX 特定实现或 mox 的发布状态。

待核验：精确 checkpoint/revision；Laya 导出图和输出处理；BGE pooling/normalize/tokenizer 配置；转换主机要求；量化/校准数据；RK3588 驱动/runtime 组合；CPU backend；厂商库再分发许可；toolkit 代际支持范围。

- [PyPA pyproject.toml 指南](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)：TOML 用于 Python 工程配置，不要求业务数据也使用 TOML。
- [HF 下载指南](https://huggingface.co/docs/huggingface_hub/guides/download)：revision 可接受分支、标签或 commit；rkai 将用户 selector 与解析后的安装快照区分。

## 2026-10-03 M1 协议核对

- [HF 官方 Hub API 文档](https://huggingface.co/docs/hub/api) 与 [实时 OpenAPI](https://huggingface.co/.well-known/openapi.json)：model revision 返回 sha；文件通过 resolve/固定 commit 访问。实现直接使用公开 HTTP，不依赖训练/转换 SDK，不读 token 缓存。
- [ModelScope 官方 Hub 客户端固定源码](https://github.com/modelscope/modelscope_hub/blob/0b2a3bacef4cbeccfd9a64545149d56cb1bb98a1/src/modelscope_hub/_legacy_api.py)：repo/files 的 Revision/Root 参数、Files 的每文件 Revision 和文件下载 repo?Revision=&FilePath=。本次公开响应已核对每文件 commit；不从 LatestCommitter.ShortId 伪造仓库全长 commit。
- [RKNN 官方头文件固定源码](https://github.com/airockchip/rknn-toolkit2/blob/59a913d172e7f5ff03c9076e2ec7b1b1288ffd08/rknpu2/runtime/Linux/librknn_api/include/rknn_api.h)：复核 RKNN_QUERY_SDK_VERSION=5 与 api_version/drv_version 字段。需要有效运行上下文的查询留在 M2，M1 不调用 native constructor。

ModelScope 核对源码 commit：`0b2a3bacef4cbeccfd9a64545149d56cb1bb98a1`。SDK 历史平台/版本调查仍见 ROCKCHIP；此次没有下载动态库、安装 SDK、替换驱动或推理。

## 2026-10-04 文件安全边界核对

- [Python 3.12 文件与目录接口](https://docs.python.org/3.12/library/os.html#files-and-directories)：dir_fd、follow_symlinks 与 POSIX O_NOFOLLOW 用于锚定目录和文件访问。
- [Python 3.12 shutil.rmtree](https://docs.python.org/3.12/library/shutil.html#shutil.rmtree)：dir_fd 与 avoids_symlink_attacks 标志；递归清理仅使用支持描述符的防符号链接实现，无不安全回退。

HF/MS 响应顶层和使用到的嵌套字段按官方协议形状校验；数组、null 或错类型响应作为 invalid_source_metadata，镜像故障继续尝试原站。真实成功响应与故障注入分开记录，不把 mock 当作上游稳定性证明。

## 2026-10-04 403 实际调查补充

核对 [当前官方 ModelScope repo/files 实现](https://github.com/modelscope/modelscope_hub/blob/main/src/modelscope_hub/_legacy_api.py)：Revision/Recursive/Root 参数仍用于文件树查询，未发现本次 URL 因接口废弃而必然失效的证据。[官方错误处理](https://github.com/modelscope/modelscope_hub/blob/main/src/modelscope_hub/errors.py) 区分 HTTP 层错误和业务正文；不能仅凭 403 证明限流。

本次相同 URL 返回过 200 与 403，最终完整原脚本成功，但持续稳定性与全部 403 的统一原因仍未证明。实际请求状态、前次失败与最终通过分开保留在 VERIFICATION 中；未变更客户端身份或网络端点规避限制。

## 2026-10-05 M2 来源与模型调查

官方 SDK 实物对应 ROCKCHIP 的两个固定 commit；大小/摘要与原始 URL 为 src/rkai/toolchains/rockchip-assets.json。Toolkit2/RKLLM 的许可原文实际读取并区分。构建依赖约束依据 [uv build constraints](https://docs.astral.sh/uv/pip/compile/#adding-build-constraints)，带 hash 的构建约束与运行依赖锁分别管理。

公开代表权重为 [MobileNetV2 ONNX](https://huggingface.co/onnxmodelzoo/mobilenetv2-12/tree/23a2a25eaaa6fcd61865414c8a1acf13dda66b7a)、[MiniLM](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/tree/1110a243fdf4706b3f48f1d95db1a4f5529b4d41)、[ggml stories260K](https://huggingface.co/ggml-org/models/tree/499bc8821c6b12b4e53c5bffcb21ec206f212d81)。固定文件 size/SHA256、实际 provider commit 与 CPU 输出在公开验证证据中；MobileNet 图片/labels 取自固定 Toolkit2 resnet18 示例。只解析 labels 字面量，不执行下载的 Python。

[Jev 官方能力文档](https://docs.typesafe.ai/introduction) 为 Choice/Score/Noul，公开服务接口不等于本地可下载权重/SDK。当前没有取得其可验证本地转换材料，不提供 Jev adapter。[Laya 原作者仓库](https://github.com/NandhaKishorM/laya) 与 [ONNXAgent 文档](https://github.com/NandhaKishorM/laya/blob/main/docs/reference/agent.md) 表明存在 CPU ONNX 方向；专用 decision head、option 标记与 calibration 不能由 embedding pooling 代替。RKNN 支持和完整导出尚未证明，待固定完整材料和参考结果再适配。第三方 Jev 仿站、网关或“兼容”包不作为 TypeSafe 官方本地 SDK 证据。
