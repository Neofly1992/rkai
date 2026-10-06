# HTTP 范围与映射

状态：2026-10-05 M3 已授权并实现本文限定的 HTTP 子集；不是完整协议兼容承诺。FastAPI 单进程入口复用模型库与 Session，实际 SDK/CPU 证据见 M3-STATUS 和 VERIFICATION。

## 首期边界

监听回环地址，不开放来源凭据、用户账户或远端部署管理。请求只能选择本地 alias，不把 HTTP model 字段当作下载 URL。客户端断开触发请求取消，推理不由 HTTP 后台任务脱离所有者继续执行。

| 路径 | 设计行为 |
| --- | --- |
| GET /health | 服务状态与版本；不执行推理，不因单模型损坏使进程看似死亡 |
| GET /v1/models | 已安装且文件完整的模型列表；不是远端 registry，也不等于全部已加载 |
| POST /v1/chat/completions | OpenAI 风格文本聊天子集，映射 chat 能力 |
| POST /v1/embeddings | OpenAI 风格文本 embedding 子集，映射 embedding 能力 |
| POST /v1/messages | Anthropic 风格文本消息子集，映射同一 chat 能力 |
| POST /api/v1/infer | rkai 原生能力接口，覆盖当前已注册的 chat/embedding/classification 能力；decision 等未实现能力显式拒绝 |

外部协议使用独立 DTO/错误映射。实现前已核对官方协议；本次字段、SSE 与 SDK 验证范围见下文，不凭路径名称宣称完整兼容。首期外部聊天仅文本、单个输出候选、流式/非流式；工具执行、文件管理、远端图片 URL、Responses API 和批处理不在该子集。

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


## M3 已实现协议子集（2026-10-05）

依据：[OpenAI Chat](https://developers.openai.com/api/reference/resources/chat)、[Chat SSE](https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events)、[Embeddings](https://developers.openai.com/api/reference/resources/embeddings/methods/create)、[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)、[Anthropic streaming](https://platform.claude.com/docs/en/build-with-claude/streaming)。核对日期 2026-10-05；没有调用外部付费推理服务。

| 接口 | 接受字段与结果 |
| --- | --- |
| OpenAI chat | model、messages（system/user/assistant + 字符串 content）、max_tokens 或 max_completion_tokens（互斥）、temperature、stream、n=1；结果为单 choices，SSE role/content delta → finish_reason chunk → [DONE] |
| OpenAI embedding | model、input（字符串或字符串数组）、encoding_format=float；data 按输入顺序带 index/embedding；不接受 token ID 输入、dimensions 或 base64 输出。官方 Python SDK 调用必须显式 encoding_format="float"（其默认发 base64） |
| Anthropic messages | model、messages（user/assistant，字符串或 type=text/text 内容块数组）、max_tokens 必填、system（字符串/文本块数组）、temperature、stream；结果为单 text block |
| Native | model、capability、input、parameters、stream；input 与 parameters 不可有重复键；共同映射严格能力请求，不允许请求修改 fallback/运行环境 |

能力自身的数量/文本长度/上下文参数校验仍由 capabilities/adapters 执行。HTTP 不扩大模型能力；工具、developer role、图片聊天、stop、seed、采样扩展、批处理、Responses、持久 session 均未支持。未知字段与未知 enum 值拒绝；没有列出可静默忽略的请求 JSON 字段。SDK 带来的认证/版本头在无鉴权本机范围内不作凭据认证，也不记录它们。

Anthropic SSE 顺序为 message_start → content_block_start → content_block_delta(text_delta) → content_block_stop → message_delta → message_stop；事件名与 JSON type 相同。OpenAI 成功流只有一个 finish_reason chunk 和 [DONE]。流开始后的错误为对应协议 error 事件/对象后结束，失败不再发送成功停止事件或 [DONE]。原生 SSE 使用 accepted/started/output/一个 completed、failed 或 cancelled，并保持递增 sequence/request_id。

usage：当前 backend 未提供可信 token 计数；OpenAI chat/embedding **省略 usage**，Anthropic usage 的 input_tokens/output_tokens 为 **null**，message_delta 的 output_tokens 也为 null。这是明确的局部协议偏差（官方计数字段通常为整数），不能据 SDK 宽松解析宣称完整兼容。RKLLM unknown finish_reason 在外部协议映射为 null，原生仍为 unknown；不能假造 end_turn/stop。llama.cpp 的已知 stop/length 分别映射 Anthropic end_turn/max_tokens。没有根据输出字符数估算 token 或填 0。

SDK 实验范围：OpenAI Python **2.54.0** create/chat SSE/embedding float；Anthropic Python **0.125.0** messages.create 和 messages.stream 的 text_stream/get_final_message。SDK 重试在验证中关闭；仅以上调用通过，不承诺 strict response validation、token 计费、全部 SDK 或未知字段。

前置错误：无 alias 404/model_not_found；HTTP 超限 413/input_too_large；字段/能力请求无效 400；能力、环境、平台不支持 422；队列/预算/设备忙 429；关闭中 503；请求 deadline 504。错误仅含受控 code/type/message，不返回原始输入、内部路径、native 原文、数据库异常或栈。/health 不因某模型 load failed 假报进程死亡；ready 表示 actor 活着并接受请求，不保证所有模型可执行。/v1/models 文件重验在独立线程自有 SQLite 连接中完成，返回完整 alias；created=null（数据库未记录创建时间），loaded 与 execution_validation 分开。

classification multipart：唯一 metadata JSON part 和唯一 image part，input.image 固定 "media:image"。media_count 是通用传输预算，但当前 classification 契约只接受一个 image；不支持的多媒体/重复 part 直接拒绝，不为了凑到 8 注册空多模态能力。JSON classification、远端 URL、任意服务端路径、内嵌 base64 均拒绝。文件名不用于路径，实际存入 data_dir/http-staging 的随机私有目录；失败/断连/关闭回收，父崩溃遗留在下一次服务启动取得上传 owner lock 后回收。上传与 Session 复制各计磁盘预算，模型输入继续限制压缩图像 16 MiB、解码前 8 Mi 像素与工作区内存。

[http] 默认 queue_capacity=8（共 9 个上传/排队/执行槽），memory_bytes=128 MiB、staging_bytes=256 MiB、request_seconds=300、drain_seconds=10。单输出通道限 64 个事件/1 MiB；传输、解析与响应临时对象采用保守内存计费，另在 Session native 准入预留整个 HTTP memory_bytes 与 IPC 缓冲。不是强制 RSS 上限。资源预算可能先于传输尺寸限额拒绝；不能提高 http.limits 来绕过模型或全局预算。

请求期限从接收入口开始，覆盖上传、等待、加载、执行和输出。模型按 FIFO 串行；同 alias/能力共享已加载 context，已等待同一次 load 的请求共享成功或失败；不同 alias 切换先停止旧 worker、释放 lease，再走同一个 Session 选择/锁/准入。加载失败不会自动重试已等待该加载的请求，后来的新请求可明确重试。preload 仅一个 alias；同 UID 跨库设备锁保持，忙不自动切换 CPU。输出后不重放。

共享失败仅指模型/runtime 加载失败。发起加载的请求自身超时或断连取消不得传播给其他排队请求；停止该次加载并释放资源后，仍有效的下一请求可独立加载，这不是重放已执行的推理。


### 2026-10-06 生命周期审查修复

取消绑定 accepted 的实际 request_id；共享 Session 的迟到取消不能影响下一请求。
尚未出队的请求在超时/断连/关闭时原子移出队列，不等待其他请求，也不退役其他请求的 Session。
执行中的取消仍等待实际 native 停止；失败终态在 owner 完成清理后发送，不能以终态代替停止证明。

流式和非流式响应都持有请求槽、媒体与响应预算直到发送结束或断连/写入超时后的清理完成。
正常输出写入受入口绝对期限和单次 drain_seconds 共同约束；不得因有缓冲事件就继续超期输出成功。
超期后的 native 回收必须完成：若连接可写，只允许在证明停止后发送一个失败诊断终态，诊断写入仍限 drain_seconds；
发送已阻塞/部分响应时可以终止连接，不继续补发成功或伪造完整 JSON。
因此回收和安全错误诊断可能晚于执行期限，这不允许继续 native 工作或提前释放资源。

加载使用同一剩余期限覆盖启动握手、load 控制帧发送及 loaded 等待；这些 IPC 阶段都观察取消。
尚未 loaded 的 worker 直接进入受管进程组停止证明，不等待无法保证会被处理的软关闭命令。
上传创建、恢复和 owner 删除由一个稳定注册锁协调；UUID 临时锁与媒体一起回收，
启动也回收旧 M3 的 UUID 孤儿锁，仅匹配生成身份且 owner 空闲的对象，保留活动上传和其他文件。
