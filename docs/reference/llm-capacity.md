# 大模型容量与请求预算

核查日期：2026-10-29。单一能力来源为 `app/models/capacity.py`，管理界面从后端读取。只有准确的官方 HTTPS 端点和型号匹配时才采用目录；代理及自定义型号不会继承同名原厂能力。官方仅写 K/M 且没有精确整数时，以下使用十进制保守取值。

| 预设型号 | 共享上下文 | 独立输入 | 最大输出参数 | 官方依据 |
| --- | ---: | ---: | ---: | --- |
| DeepSeek `deepseek-flash` | 1000000 | 未单列 | 393216 | [规格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[API](https://api-docs.deepseek.com/api/create-chat-completion/) |
| 百炼 `qwen-plus` | 1000000 | 997952 | 32768 | [默认非思考规格](https://help.aliyun.com/zh/model-studio/qwen-plus) |
| 豆包 `doubao-seed-2-1-pro-260915` | 1024000 | 1024000 | 256000 | [具体型号表](https://docs.volcengine.com/docs/ark/model-list?lang=zh)、[上下文](https://docs.volcengine.com/docs/ark/context-management?lang=zh) |
| Kimi `kimi-k3` | 1048576 | 受共享窗口限制 | 1048576 | [API](https://platform.kimi.com/docs/api/chat)、[剩余窗口规则](https://www.kimi.com/help/kimi-api/api-troubleshooting) |
| 智谱 `glm-5.3` | 1000000 | 未单列 | 131072 | [规格](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3)、[参数](https://docs.bigmodel.cn/cn/guide/start/concept-param) |
| 小米 `mimo-v2.6-pro` | 1048576 | 未单列 | 131072 | [精确配置](https://mimo.mi.com/docs/zh-CN/tokenplan/integration/opencode)、[API](https://mimo.mi.com/docs/en-US/api/chat/openai-api) |
| 硅基流动 `deepseek-ai/DeepSeek-V3.2` | 163840 | 未单列 | 未核实 | [模型](https://www.siliconflow.cn/models)、[API](https://docs.siliconflow.cn/docs/api/chat-completions-post) |
| `MiniMax-M3.1-Flash` | 1000000 | 未单列 | 524288 | [上下文](https://platform.minimax.cn/docs/api-reference/text-openai-api)、[输出](https://platform.minimax.cn/docs/api-reference/text-chat-openai) |
| TokenHub `hy4-preview` | 1000000 | 960000 | 64000 | [型号表](https://cloud.tencent.com/document/product/1823/130051) |
| 星火 `4.0Ultra` | 未单列 | 32000 | 32768 | [HTTP 文档](https://www.xfyun.cn/doc/spark/HTTP调用文档.html) |
| LKEAP `deepseek-r1-0528` | 128000 | 96000 | 16000 | [旧接口](https://cloud.tencent.com/document/product/1772/115969) |
| 旧混元 `hunyuan-turbos-latest` | 未核实 | 未核实 | 未核实 | [已下线及旧平台停服公告](https://cloud.tencent.com/document/product/1729/131925) |

Kimi、MiMo、MiniMax 发送 `max_completion_tokens`，其余已核实型号使用 `max_tokens`。输出是否包含思考以服务契约为准；豆包、LKEAP 回答预算之外分别预留 256000、32000 思考 token，硅基流动按通用思考上界预留 32768。豆包通用 API 页仍有旧的 65536 参数范围，目录使用具体新型号表并保留该文档差异，服务返回的真实错误仍会报告。

千帆、ModelScope、StepFun、Infini 预设需要填写账号实际模型 ID，不能给整个平台指定统一容量。千帆 [models API](https://cloud.baidu.com/doc/qianfan-api/s/Dmba8k71y) 提供可为空的容量字段；StepFun [模型对象](https://platform.stepfun.com/docs/zh/api-reference/models/object.md) 和 Infini [说明](https://docs.infini-ai.com/shared/gen-studio/coding-tools/gs-use-cline-n-roocode.html) 不提供完整容量，ModelScope 的容量接口本次未核实。依据当前服务模型卡手动填写即可，无需修改源代码。

## 实际生效规则

- 共享上下文、独立输入和输出均可手动配置，最高 2097152 token。已核实型号仍受官方上限约束；手动小值保留，清空后恢复自动额度。未知输出留空沿用原 65536 本地预算，这不是供应商能力承诺。
- 转写整理、高光分析、热点解释默认业务预算为 1048576；总结、投稿文案和热点采集也使用扩大的业务预算。实际输出取业务预算、路由限制、型号限制及剩余上下文的较小值。连接测试仍仅用 4096。
- 所有系统/用户消息、修复说明及 `response_format` 完整序列化后，以 UTF-8 字节数加 512 协议余量作保守 token 估算。该估算会提前拒绝部分实际 tokenizer 可能容纳的输入，不宣称精确计数。超过限制时保留完整来源，记录未发送原因并尝试后续路由，不在调用层静默裁剪。未发送的请求不消耗实际请求次数。服务端实际 usage 与校验为最终依据。
- 热点默认前后文各 180 秒，补充上下文扩至指定方向 600 秒；最多 120000 字符证据、单摘录 20000 字符。完整来源 512 KiB、输出 JSON 16 MiB、HTTP 响应 32 MiB，响应中思考文本计入传输上限。产品标题/摘要长度与事实核验不变。
- 单次超时默认 600 秒（最大 3600），编辑总期限默认 1800 秒（最大 14400）。已有数据库和环境变量的显式设置仍生效，扩大 token 额度不会绕过时间与每日费用限制。更长思考可能增加费用和等待时间。

本次变更不自动修改正在运行的 Portable、账户模型配置或历史素材；新代码需正常更新运行版本后生效。
