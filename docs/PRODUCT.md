# 产品需求

状态：2026-10-05：M1 已实现；用户授权的 M2 已落地当前注册的转换与本地执行路径；decision/Jev/Laya 与混合多组件编排尚未实现。真实 CPU 代表模型和管理层验证见 [M2-STATUS](M2-STATUS.md)。Linux SDK 编译与 RK3588 真机证据未完成，M3 本地服务子集已授权实现（边界见 M3-STATUS）。以下需求基线与实际支持范围分开。

## 已确认

- 提供面向 RK3588 的通用模型运行平台，不围绕 Qwen3 或 LLM 设计。LLM、决策、embedding 及后续视觉/音频等模型通过相应能力扩展；举例不代表支持清单。日常使用屏蔽 RKNN/RKLLM 差异，长期逐步覆盖更多 RK 设备。
- 模型类型可扩展，覆盖聊天、决策、embedding 等需求。Jev 是决策模型，Laya 是可考虑部署的开源竞品；模型本身不等于外部协议或适配器。
- 核心命令：doctor、pull、convert、delete、run、serve。未来可增加 chat/decide 语法糖，当前不实现。
- run 支持单次请求和持续交互；serve 前台常驻，可由 systemd 托管。两者共享执行能力。
- convert 是明确需要的产品能力：对已支持的源模型提供一键转换，不让用户手动串联脚本。不承诺任意模型、任意格式都可转换。
- pull 支持 Hugging Face、ModelScope、自定义来源与镜像，借鉴 registry 概念。
- doctor 同时检查自身与系统环境，尤其是 NPU 驱动、设备权限、runtime 和兼容性。
- CPU fallback 默认关闭，用户可手动开启；只有实际适配的 CPU 方案才可使用。这是已确认策略，不再列为待决策。
- 首期面向 SBC 本地使用；服务默认仅监听回环地址，不提供对外服务配置。API 鉴权、来源凭据管理及私有/需授权模型下载放到二期；首期使用公开来源。
- 使用 uv 管理 Python 工程依赖和虚拟环境；Typer/Rich 按需采用。Python 是当前主选方向，具体 runtime binding、分发方式仍待设计验证。
- 正确性优先，不接受为了赶进度建立的临时方案、永久过渡层或未发布旧实现兼容包袱。

## 继承方向与未决范围

旧会话末尾摘要提出优先行业 HTTP 协议，如 OpenAI/Anthropic，不以 Ollama 兼容为目标。首期接口目标范围见 [API](API.md)，具体外部协议字段在实现前按官方规范锁定。Jev 兼容 API 如有需求，是单独的协议适配决策。

SQLite、socketpair 与格式等由工程设计选择合理默认，不要求用户逐项审批。分发及厂商动态库已完成官方调查，采用方案见 [设计选择](DECISIONS.md)。

## 可观察行为草案

以下仅说明交互意图，参数拼写尚未冻结：

```text
rkai doctor
rkai doctor --model <model>
rkai pull <model-ref>
rkai convert <source-ref-or-path> --recipe <recipe.json>
rkai run -m <model> --input <text-or-file>
rkai run -m <chat-model> --interactive
rkai serve
rkai delete <model>
```

输入由所选能力解释；decision 不被强行包装成聊天文本。机器可读结果与日志分开，进度仅在合适的终端显示。持续聊天的历史由 session 持有；首期不跨进程持久保存。

pull 获取现成产物；convert 获取或读取源模型并生成新产物。pull 不隐式启动昂贵转换，缺少适配产物时给出具体转换路径。转换完成后与下载产物进入相同模型库。

服务用户与交互用户的模型库位置显式配置；不能在不同身份之间偷偷猜路径。请求不自动下载；预加载与模型列表语义见 [CLI](CLI.md) 和 [API](API.md)。

## doctor

结果逐项包含 pass / fail / unknown / skipped、检测依据、影响和修复建议。检查系统架构/SoC、设备节点权限、驱动、动态库、兼容组合、数据目录、模型文件与内存条件。库能加载不等于模型能推理；主动运行探测与静态检查分开报告。缺证据不能写 pass，探测不得自动改驱动、权限或系统配置。

## fallback 策略

默认关闭，可由用户在配置与单次命令中手动开启。开启后仅在执行前选择具备同一请求能力的 CPU 方案；说明选择原因和性能影响。不因任意异常切换：输入错误、损坏、权限错误不能一概掩盖。输出已经开始后不静默重跑。缺 CPU 文件明确提示，不因开启 fallback 自动下载。CPU 与 NPU 数值可有差异，必须满足同一能力契约，不宣称逐字一致。

## 细化规格入口

命令行为见 [CLI](CLI.md)，模块与能力见 [INTERFACES](INTERFACES.md)，状态和恢复见 [LIFECYCLE](LIFECYCLE.md)，实施与验收见 [ACCEPTANCE](ACCEPTANCE.md)。M1/M2 已明确授权实现，实际代码/验证入口见 USAGE/VERIFICATION；服务已实现的子集与验证见 M3-STATUS。
