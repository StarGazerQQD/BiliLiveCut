# BiliLiveCut Engine Pack 第三方模型声明

适用产物：`BiliLiveCut-EnginePack-0.1.15.2-alpha.zip`

本 Engine Pack 包含来自下列上游项目的模型权重、配置、词表和示例文件。模型来源、固定 revision、许可证证据和验证日期同时记录在 `model_sources.lock.json` 与包内 `engine-pack-manifest.json` 中。构建器保留固定模型快照中已有的 README、NOTICE、LICENSE 和归属信息，并额外随包提供本声明及完整许可证文本。

| 组件 | 上游来源 | 固定 revision | 许可证 |
| --- | --- | --- | --- |
| Whisper large-v3-turbo（CTranslate2 转换） | https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo | `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf` | MIT |
| OpenAI Whisper 原始权重 | https://github.com/openai/whisper | `large-v3-turbo` 上游模型 | MIT |
| Paraformer-zh | https://modelscope.cn/models/iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch | `v2.0.4` | Apache-2.0 |
| FSMN-VAD | https://modelscope.cn/models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch | `v2.0.4` | Apache-2.0 |
| CT-Transformer 标点模型 | https://modelscope.cn/models/iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch | `v2.0.4` | Apache-2.0 |
| CAM++ 说话人模型 | https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common | `v1.0.0` | Apache-2.0 |
| SenseVoiceSmall | https://modelscope.cn/models/iic/SenseVoiceSmall | `7bf452403abd7353a300cd760f7adae7701c92c1` | Apache-2.0 |
| Fun-ASR-Nano-2512 | https://modelscope.cn/models/FunAudioLLM/Fun-ASR-Nano-2512 | `05201c46f1c38592b1567f857c0d56eab3d0d8ef` | Apache-2.0 |
| Qwen3-0.6B 随附组件 | https://huggingface.co/Qwen/Qwen3-0.6B | 随 Fun-ASR-Nano `05201c46f1c38592b1567f857c0d56eab3d0d8ef` 固定 | Apache-2.0 |

## 许可证文件

- MIT：`licenses/MIT.txt`
- Apache License 2.0：`licenses/Apache-2.0.txt`

许可证核验日期：2026-07-22。

本声明用于保留上游归属与许可证信息，不替代许可证原文，也不构成额外授权或法律建议。分发者和使用者仍须遵守各上游许可证及适用法律。
