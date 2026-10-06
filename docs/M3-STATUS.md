# M3 实现与验证边界

2026-10-05：用户明确授权 M3「本地服务与首版交付」，优先于旧设计状态；M2 硬件/模型覆盖缺项独立跟踪，不作为 M3 的新增前置条件。没有提交、推送、发布或修改宿主系统配置。

## 已实现

- FastAPI `rkai serve` 前台回环服务；默认 127.0.0.1:8080，端口占用明确失败。延迟加载与一个显式 preload，健康、应用 ready、busy/排队和模型 loaded 状态分开。
- 六个指定 HTTP 入口；OpenAI/Anthropic 文本子集与原生 chat/embedding/classification。严格拒绝未知参数和未实现 decision，不提供空 adapter。支持字段、null/省略 usage 与 unknown 停止原因的协议偏差见 API。
- 固定线程拥有 SQLite/Session/生成器，HTTP 不复制安装/转换/方案选择/fallback/native 规则；单 UID 跨库锁保留，同模型串行与复用加载，不同 alias 先退役旧 context。已等待的同模型加载失败共享失败，不自动重试。
- 有界入口槽、JSON/metadata 内存、multipart/单媒体/数量/暂存磁盘、输出通道和写入背压；上传/排队/加载/执行/输出共用入口 deadline。压缩媒体、解码与模型 tensor 预算另由原能力/adapter 执行。
- 分类只接受受管 multipart，随机私有文件名，不接受服务端路径/远端 URL；实际字节计数，未知长度也有界。取消等实际 native 停止后回收预算/暂存；崩溃遗留由 owner flock 恢复，活动上传保留。无数据库 schema 变化，未删除用户模型。
- 断连与 SIGINT/SIGTERM 停止准入、取消在途/排队、退役 native、释放 lease 后退出；输出后不重放。取消控制不阻塞 ASGI，不能证明停止时保留 owner/lease/预算并停止准入。
- 可安装 wheel，内置 systemd unit/config 与安装/卸载/诊断说明。主环境仅服务/管理依赖；厂商 SDK 与推理依赖继续在受管环境，wheel 没有权重/vendor 动态库/整套 SDK。

## 实际验证通过

全量结果与命令见 VERIFICATION（含原 180 项 M1/M2 和新增 23 项 HTTP，共 203 项）。新增测试使用真实回环 TCP、FastAPI、SQLite、guardian 与 native ONNX；环境定位复用开发环境仅是测试设置，不冒称受管环境安装或 NPU。真实公开 CPU 则单独走受管环境/HTTP/官方 SDK，证据独立保存。

公开 MobileNet top-1=812；MiniLM SDK 批量向量 384 维；stories260K OpenAI create/SSE、Anthropic create/text_stream/get_final_message 通过。服务占用时 CLI 被设备锁拒绝，退出后 CLI 再推理通过。SIGINT/SIGTERM 都在实际文本输出后触发，原生只发一个 cancelled 终态，没有重放，服务退出及 CLI 重新准入通过。独立主 wheel 环境同样运行这些流程，不借主环境开发依赖执行 native。

SDK 版本 OpenAI 2.54.0、Anthropic 0.125.0；FastAPI 0.142.2、Uvicorn 0.54.0、python-multipart 0.0.32；精确依赖见 uv.lock。无可靠 token usage 的字段按 API 留 null/省略，SDK 宽松解析通过不等于正式完整兼容。

## 未实现与未验证

- 未实现：decision/Jev/Laya、混合多环境组件编排、工具/多模态聊天及未列出的外部协议字段；明确拒绝。凭据、私有来源、公网服务、多用户与新 SoC 不在本期扩张。
- 未验证：Linux systemd 实际启动/停止/cgroup、Linux SDK 转换、RK3588 ABI/驱动/NPU 加载、精度与生命周期；M2-STATUS/SERVICE 提供重复入口。macOS CPU/宿主进程测试不能替代目标证据。
- 未代签用户验收；**M3 已完成当前宿主可执行的实现与验证，不表示首版完整目标平台验收完成**。M1 公网本轮未重跑，历史 MS 403 不改写成通过。

## 重复入口

```sh
uv run --locked pytest -q -W error::pytest.PytestUnhandledThreadExceptionWarning
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv build --offline
```

先用 `scripts/verify_m2_public.py --work-dir /absolute/public --data-dir /absolute/isolated-library` 显式准备 CPU 环境和三个公开 alias；然后：

```sh
uv run --locked python scripts/verify_m3_http.py \
  --cli /absolute/venv/bin/rkai --data-dir /absolute/isolated-library \
  --image /absolute/public/mobilenet/space_shuttle_224.jpg --evidence /absolute/http-evidence.json
```

脚本不隐式下载/转换/prepare、不使用默认库，官方 SDK 重试关闭；不得与占用同 UID 设备锁的测试并行。wheel 验证将 --cli 指向只安装主 wheel 的独立环境；SDK 测试程序自身运行在含 SDK 的验证环境。systemd/硬件重复入口见 SERVICE/M2-STATUS。


## 2026-10-06 审查修复

8 项审查问题已修复：取消请求隔离、native 停止后的终态、非流式发送预算、输出总期限、
排队取消独立回收、加载握手取消、临时上传锁回收，以及 systemd 服务解释器位置。
新增 8 项真实 HTTP/native 回归；完整 **211 passed**，Ruff/格式/构建通过。
说明和证据见 VERIFICATION；Linux systemd/cgroup、SDK 转换与 RK3588 仍未验证，功能覆盖缺项不变。

2026-10-06 独立复核另复现并修复加载期间超时传播给同模型排队请求的问题。
共享模型加载失败与单请求取消已分开；新增真实 HTTP/guardian 回归，完整 212 项通过。
独立审查范围、最终验证及限制见 M3-INDEPENDENT-REVIEW.md、VERIFICATION。
