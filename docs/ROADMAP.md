# 路线图

2026-10-05：M1 实现及本地复验已完成，独立审查见 [M1-REVIEW](M1-REVIEW.md)；10-04 公网复验曾通过，10-05 MS 403 复验失败如实保留在 VERIFICATION；用户明确将 RK3588 静态诊断后置，不再阻塞开始 M2，真机证据仍标为未验证。M2 当前注册路径与真实 CPU 模型验证已落地；decision/Jev/Laya 与混合组件编排尚未实现，Linux SDK 实际编译/链接及 RK3588 验收尚未完成；M3 本地服务子集已授权实现，HTTP/CPU/SDK/wheel 验证见 M3-STATUS；Linux systemd/真机完整验收未完成。实际范围见 [M2-STATUS](M2-STATUS.md)。完整验收见 [ACCEPTANCE](ACCEPTANCE.md)。

1. **M1 模型获取与诊断**：工程、release/debug、配置、manifest、来源/镜像、可靠获取与安装、删除、静态 doctor。
2. **M2 转换与本地执行**：真实 SDK 接入、受管工具链、convert/run、会话与 worker、各类模型适配、可控 CPU fallback、真机诊断。
3. **M3 本地服务与首版交付**：FastAPI 协议、systemd、请求/媒体限额、wheel 分发与端到端验收。

里程碑内按小任务推进，但不需要为每个普通技术步骤重新审批。每期可验收、错误恢复和工程质量从开始成立；convert 属于首期产品的 M2，不能后移到“以后再说”。

二期及后续：来源凭据/私有下载、API 鉴权及对外服务、更多 RK SoC/工具链和模型能力；chat/decide 语法糖按实际需要增加。其他 RK 设备按官方矩阵和实测推进，不以一个 backend 名称承诺兼容全部硬件。
