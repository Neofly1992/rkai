# HTTP 范围与映射

状态：接口设计，不是已实现的兼容承诺。FastAPI 为单进程协议入口，业务调用与 CLI 共用应用用例。

## 首期边界

监听回环地址，不开放来源凭据、用户账户或远端部署管理。请求只能选择本地 alias，不把 HTTP model 字段当作下载 URL。客户端断开触发请求取消，推理不由 HTTP 后台任务脱离所有者继续执行。

| 路径 | 设计行为 |
| --- | --- |
| GET /health | 服务状态与版本；不执行推理，不因单模型损坏使进程看似死亡 |
| GET /v1/models | 已安装且文件完整的模型列表；不是远端 registry，也不等于全部已加载 |
| POST /v1/chat/completions | OpenAI 风格文本聊天子集，映射 chat 能力 |
| POST /v1/embeddings | OpenAI 风格文本 embedding 子集，映射 embedding 能力 |
| POST /v1/messages | Anthropic 风格文本消息子集，映射同一 chat 能力 |
| POST /api/v1/infer | rkai 原生能力接口，覆盖 decision/classification 等无统一外部协议的能力 |

外部协议使用独立 DTO/错误映射。以上是目标范围；进入实现前按各官方协议锁定请求、响应、SSE 事件和 SDK 验收样例，不凭路径名称宣称完整兼容。首期外部聊天仅文本、单个输出候选、流式/非流式；工具执行、文件管理、远端图片 URL、Responses API 和批处理不在该子集。

原生请求包含 model、capability、input、parameters、stream；fallback 策略取服务配置，不由任意请求改变主机策略。响应关联 request_id、实际 plan/device 和类型化结果。流式使用 SSE，事件语义来自 INTERFACES；不声称这与 OpenAI/Anthropic wire 格式相同。

### 请求大小与多模态

取消统一 1 MiB 限制，按类型区分并通过 config.toml 集中配置：

| 配置项 | 默认值 | 范围 |
| --- | --- | --- |
| http.limits.json_bytes | 16 MiB | 普通 JSON，包括支持的内嵌 base64 媒体表示 |
| http.limits.multipart_bytes | 64 MiB | 整个 multipart 请求，含元数据与所有媒体 |
| http.limits.media_file_bytes | 32 MiB | 单个上传文件或单个解码后的媒体资产 |
| http.limits.metadata_bytes | 1 MiB | multipart 的 JSON 元数据部分，不是整个请求 |
| http.limits.media_count | 8 | 一次请求中的媒体资产数 |

这些是可调的工程默认，不是模型支持的输入上限。base64 编码约增加三分之一大小，JSON 上限按传输字节计算；需要更大媒体时原生接口优先 multipart，不把全部请求转成 base64。协议适配器按其正式字段处理媒体，不能为了允许大 body 就宣称尚未实现的多模态聊天可用。

首期原生 classification 的 multipart 包含一个 JSON 元数据部分和命名媒体部分；不接受服务端任意文件路径，不增加永久上传资产库。逐块计数，不能只信 Content-Length；超限返回 413，已知长度提前拒绝，未知长度也有界读取。multipart 流式暂存，单次与全局磁盘/内存准入由资源管理器约束，取消时清理；排队请求同样计入预算。

传输限制之外，媒体解码后还有像素/帧数/采样数和模型 tensor 预算；压缩文件很小不意味着可安全解码。具体上限由能力和 adapter 的真实输入契约给出，无预算依据不加载。提高 HTTP 限额不绕过模型上下文或内存检查。父子 IPC 继续限制控制帧，媒体通过受管文件引用传递，不把 64 MiB 上传直接塞进 8 MiB IPC 帧。

未知字段原则上拒绝；明确允许忽略的协议字段须列出，不默默接受并不生效。不能映射的参数报 unsupported_parameter，缺能力报 unsupported_capability，模型不存在报 model_not_found。前置错误使用对应 HTTP 错误；流开始后以协议错误事件或连接终止表达，不能再伪造普通 JSON 成功。

模型不存在/不完整不自动 pull；正在加载的同模型请求共享加载结果，失败各自获得明确终态。忙和资源不足与输入错误分开；不自动重试推理。原生 request_id 用于关联日志，不作为未定义的幂等重放键。

## 验收依据

至少覆盖每个已承诺入口的合法请求、参数拒绝、流终态、取消、断连及 usage 来源。CLI 成功不能替代 HTTP 协议验证。OpenAI/Anthropic SDK 验收通过的具体版本、字段和范围写入验证记录，未测字段不宣称支持。
