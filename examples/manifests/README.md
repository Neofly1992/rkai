# Manifest 结构示例

这些 JSON 是供设计评审的完整结构，不是可运行的模型清单。`example.invalid` 不提供下载，size=1、全零 SHA256、illustrative revision 和 unverified-example 兼容档案均是显式占位。运行校验必须拒绝未知档案，不允许由这些案例推导模型支持。

- [Qwen3](qwen3-1.7b.json)：聊天、RKLLM、共享辅助文件。
- [Laya](laya.json)：decision、RKNN 与 CPU 候选。ONNX Runtime 仅用于检验结构表达能力，尚未选为正式 CPU backend，也未验证 Laya 导出可用。
- [BGE](bge.json)：embedding、RKNN；具体 checkpoint、pooling、normalize 和导出形状未定。
- [混合组件](mixed-components.json)：同一方案中的 RKNN 视觉组件和 RKLLM 语言组件。chat 多模态请求契约待定，该文件仅检查组件结构，不声称现有 chat/1 契约已包含图像。

adapter.json/tokenizer.json 仅作为受摘要保护的资产角色展示；本目录没有实际资产。adapter 的精确配置 schema 需与选定模型一起定义。

本轮检查 JSON 语法、引用完整性、plan/component id 唯一性与默认能力引用；没有验证模型、兼容档案或远端下载。

[转换配方](conversion.recipe.json) 展示 recipe 与输出 plan 的关系；输入清单、主机架构、工具链锁与 converter 均为占位，未声称这是完整 Qwen3 转换输入。输出逻辑名称 weights/tokenizer/adapter_config 由 converter 生成后解析。parameters 留空表示具体配置尚待设计，不表示正式 converter 有可用默认值。
