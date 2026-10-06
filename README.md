# rkai

面向 RK3588 的通用本地 AI 模型运行平台，统一模型获取、转换、命令行调用与常驻服务。LLM 只是支持的模型类别之一；决策、embedding、视觉、音频等按各自能力适配，具体支持以验证矩阵为准。长期扩展至更多 Rockchip 设备。

**当前状态：M1 获取与 M2 转换/本地执行已实现；公开 CPU 分类、embedding、流式文本验证通过。** Linux SDK 实际编译、RK3588 加载/ABI/NPU 推理仍未验证，M2 尚未全部验收；M3 本地服务子集与 systemd 交付资料已实现，公开 CPU HTTP/官方 SDK 与独立 wheel 实测通过；Linux systemd 尚未实测。实际范围见 [M2 实现与验证边界](docs/M2-STATUS.md)，旧设计 manifest 仍是占位材料。

```sh
uv sync --locked --python 3.12
uv run --locked rkai --help
uv run --locked rkai --data-dir /tmp/rkai-doctor-demo doctor --json
uv run --locked python scripts/verify_public_sources.py
```

doctor 在非 RK3588 主机返回 unavailable/退出码 3 是正常诊断结果。完整配置、获取/恢复/删除流程和人工验收见 [使用指南](docs/USAGE.md)，证据见 [验证记录](docs/VERIFICATION.md)。

## Review 入口

1. [产品需求](docs/PRODUCT.md)：已确认需求与待定范围。
2. [Manifest v0](docs/MANIFEST.md)：本轮主要评审对象，字段、规则与完整示例入口。
3. [架构](docs/ARCHITECTURE.md)：职责、进程、获取、转换和本地状态。
4. [设计选择](docs/DECISIONS.md)：已采用的工程默认与少量需要验证的问题。
5. [命令与流程](docs/CLI.md)、[模块接口](docs/INTERFACES.md)、[生命周期](docs/LIFECYCLE.md)、[HTTP 范围](docs/API.md)：本轮细化。
6. [实施验收](docs/ACCEPTANCE.md)：各阶段的交付与证据；本次已授权 M1/M2/M3 实现，真机验收仍待完成。
7. [路线图](docs/ROADMAP.md)：实施顺序与完成条件。

工程协作见 [CONTRIBUTING](CONTRIBUTING.md)，续接见 [HANDOFF](docs/HANDOFF.md)，证据边界见 [验证记录](docs/VERIFICATION.md)。源码采用 [MIT License](LICENSE)，模型及厂商工具另受各自许可约束。
