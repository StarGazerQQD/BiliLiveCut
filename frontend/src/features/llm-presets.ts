export interface LlmPreset {
  id: string;
  name: string;
  baseUrl: string;
  model: string;
  docsUrl: string;
  note: string;
}

// OpenAI Chat Completions 接入信息，2026-10-22 按各平台官方文档核对。
// model 仅为公开示例；账号专属模型／接入点留空，不能推测或自动开通。
export const llmPresets: readonly LlmPreset[] = [
  {
    id: "deepseek",
    name: "DeepSeek",
    baseUrl: "https://api.deepseek.com",
    model: "deepseek-flash",
    docsUrl: "https://api-docs.deepseek.com/zh-cn/",
    note: "使用 DeepSeek 开放平台的 API Key。",
  },
  {
    id: "bailian",
    name: "阿里云百炼（Qwen）",
    baseUrl: "https://dashscope.aliyuncs.com/compatible-mode/v1",
    model: "qwen-plus",
    docsUrl:
      "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
    note: "预填北京地域通用地址；可改为控制台的业务空间专属地址。API Key 与地域必须匹配，Coding Plan 使用不同的地址和密钥。",
  },
  {
    id: "volcengine",
    name: "火山方舟（豆包）",
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3",
    model: "doubao-seed-2-1-pro-260915",
    docsUrl:
      "https://docs.volcengine.com/docs/ark/compatible-with-openai-sdk?lang=zh",
    note: "使用方舟 API Key 和已开通的 Model ID；自定义推理接入点请将模型改为自己的 ep-… ID。Coding Plan 地址不同。",
  },
  {
    id: "moonshot",
    name: "Kimi（Moonshot）",
    baseUrl: "https://api.moonshot.cn/v1",
    model: "kimi-k3",
    docsUrl: "https://platform.kimi.com/docs/get-api-key",
    note: "使用 Kimi API 开放平台的 API Key。",
  },
  {
    id: "bigmodel",
    name: "智谱 GLM",
    baseUrl: "https://open.bigmodel.cn/api/paas/v4",
    model: "glm-5.3",
    docsUrl: "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction",
    note: "预填通用按量 API 地址；GLM Coding Plan 使用不同的专用地址。",
  },
  {
    id: "mimo",
    name: "小米 MiMo",
    baseUrl: "https://api.xiaomimimo.com/v1",
    model: "mimo-v2.6-pro",
    docsUrl:
      "https://mimo.mi.com/docs/zh-CN/quick-start/summary/first-api-call",
    note: "预填实时按量 API 地址；Token Plan 和批量推理使用各自的地址及凭据。",
  },
  {
    id: "siliconflow",
    name: "硅基流动",
    baseUrl: "https://api.siliconflow.cn/v1",
    model: "deepseek-ai/DeepSeek-V3.2",
    docsUrl:
      "https://docs.siliconflow.cn/docs/userguide/capabilities/text-generation",
    note: "使用国内平台 API Key，模型需填写完整 ID，包括组织名及可能存在的 Pro/ 前缀。",
  },
  {
    id: "minimax",
    name: "MiniMax",
    baseUrl: "https://api.minimax.cn/v1",
    model: "MiniMax-M3.1-Flash",
    docsUrl: "https://platform.minimax.cn/docs/api-reference/text-openai-api",
    note: "预填国内平台 OpenAI 兼容地址；使用对应平台的 API Key。",
  },
  {
    id: "qianfan",
    name: "百度千帆",
    baseUrl: "https://qianfan.baidubce.com/v2",
    model: "",
    docsUrl: "https://cloud.baidu.com/doc/qianfan-docs/s/Fm9l6ocai",
    note: "使用千帆通用 API Key，并从控制台复制已开通的模型 ID 或推理接入点 ID。",
  },
  {
    id: "tokenhub",
    name: "腾讯云 TokenHub（混元等）",
    baseUrl: "https://tokenhub.tencentmaas.com/v1",
    model: "hy4-preview",
    docsUrl: "https://cloud.tencent.com/document/product/1823/132252",
    note: "使用 TokenHub API Key；混元与原 LKEAP 服务已向此平台迁移，新账号优先使用此入口。",
  },
  {
    id: "spark",
    name: "讯飞星火",
    baseUrl: "https://spark-api-open.xf-yun.com/v1",
    model: "4.0Ultra",
    docsUrl: "https://www.xfyun.cn/doc/spark/HTTP调用文档.html",
    note: "API 密钥填写对应模型版本的 APIPassword，不是 WebSocket 的 APIKey 或 APISecret。",
  },
  {
    id: "modelscope",
    name: "魔搭 ModelScope",
    baseUrl: "https://api-inference.modelscope.cn/v1",
    model: "",
    docsUrl: "https://www.modelscope.cn/docs/model-service/API-Inference/intro",
    note: "使用 ModelScope Token，并从支持 API 推理的模型页面复制完整 Model ID。",
  },
  {
    id: "stepfun",
    name: "阶跃星辰 StepFun",
    baseUrl: "https://api.stepfun.com/v1",
    model: "",
    docsUrl: "https://platform.stepfun.com/",
    note: "预填国内平台地址；请从平台模型列表复制当前账号可调用的文本模型 ID。",
  },
  {
    id: "infini",
    name: "无问芯穹",
    baseUrl: "https://cloud.infini-ai.com/maas/v1",
    model: "",
    docsUrl: "https://docs.infini-ai.com/support/faq.html",
    note: "使用 GenStudio API Key，并从服务列表复制当前账号可调用的模型 ID。",
  },
  {
    id: "hunyuan-legacy",
    name: "腾讯混元（旧平台）",
    baseUrl: "https://api.hunyuan.cloud.tencent.com/v1",
    model: "hunyuan-turbos-latest",
    docsUrl: "https://cloud.tencent.com/document/product/1729/111007",
    note: "官方公告旧平台已于 2026-09-30 停服；保留旧配置供核对，请迁移到 TokenHub 并使用新模型及专属密钥。",
  },
  {
    id: "lkeap-legacy",
    name: "腾讯云 LKEAP（旧平台）",
    baseUrl: "https://api.lkeap.cloud.tencent.com/v1",
    model: "deepseek-r1-0528",
    docsUrl: "https://cloud.tencent.com/document/product/1772/130549",
    note: "原 DeepSeek 接口已迁移至 TokenHub；仅供原账号核对旧配置，使用 LKEAP 专属 API Key。",
  },
];

export function createLlmProvider(preset?: LlmPreset) {
  return {
    id: "",
    name: preset?.name ?? "",
    base_url: preset?.baseUrl ?? "",
    model: preset?.model ?? "",
    api_key: "",
    clear_api_key: false,
    web_search_param: "",
    enabled: true,
    priority: 100,
    price_input_per_m: 0,
    price_output_per_m: 0,
  };
}
