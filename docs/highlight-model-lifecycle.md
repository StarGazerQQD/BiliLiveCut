# 高光模型训练与生命周期

高光模型默认使用 NumPy Logistic，安装主项目现有依赖即可训练和推理。需要比较 XGBoost 时安装可选 extra：

```bash
pip install -e ".[highlight-ml]"
```

项目支持 Python 3.11，因此 XGBoost 固定在 `>=3.2,<3.3`；XGBoost 3.3 已将 Python 下限提升到 3.12。缺少可选依赖时，比较结果会把 XGBoost 标记为 unavailable，不会伪装成训练成功，也不会影响规则和 Logistic 基线。

## 训练和评估

`train_candidate_models()` 按录制会话分组，并按片段时间把最新会话留作最终测试集；同一会话不会跨训练、校准和测试折。中位数填充、均值和标准差只在训练折拟合，availability 列保持 0/1，不参与 Z-score。概率校准和 F1 阈值只使用独立校准折，最终指标只在测试折计算。

候选模型使用同一留出集比较：

- `rules`：不读取标签的现有可解释信号基线；
- `logistic`：类别平衡、L2 正则的 NumPy Logistic；
- `xgboost`：可选的 CPU XGBoost。

主指标为 PR-AUC；同时记录 ROC-AUC、Brier、log loss、期望校准误差、固定阈值 Precision/Recall/F1、`Recall@审核比例`，以及房间宏平均 PR-AUC/Recall。产物是单个规范 JSON，包含特征 Schema 指纹、预处理参数、模型参数、校准器、阈值、评估和训练折摘要；不使用 pickle。

## 原子注册和热加载

`ModelRegistry` 的发布顺序为：

1. 在 `versions/` 下创建 staging 目录；
2. fsync 模型 JSON 和 Manifest，并计算 SHA-256；
3. 原子重命名为完整版本目录；
4. 最后原子替换 `registry.json` 的 generation 与角色指针。

首个版本自动成为 Champion，后续版本可设为 Shadow。`promote_shadow()` 和 `rollback(version)` 都切换到已经通过校验的真实产物并递增 generation。`HotReloadingPredictor` 每次预测前比较 generation，只在变化时加载并校验新 Champion/Shadow；因此回滚不会继续使用旧内存缓存。

## 漂移

`DriftBaseline.fit()` 从真实训练矩阵和训练预测生成概率/逐特征分位箱、均值、标准差和缺失率，并原子保存完整可复算数据。`DriftDetector` 对真实近期样本计算 PSI、标准化均值偏移和缺失率变化。Schema 不一致或近期样本不足会明确报错，不返回虚假的“正常”。

训练命令会把 `drift-baseline.json` 与 `blind-review.json` 作为版本附件，和模型 JSON 一起写入 staging、记录 SHA-256 后再发布版本目录。附件损坏或路径越界会拒绝读取。盲审文件只列出未审核片段，不赋标签；完成盲审并在主程序产生明确反馈后，下一次训练才会把它作为监督样本。

## 主程序在线接入

在线推理与训练共用 `DEFAULT_FEATURE_SCHEMA`。规则评分完成音频解码后，适配层直接把同一个 `AudioFeatures` 聚合为模型音频特征；不会为了模型再次执行 FFmpeg。在线 Schema 指纹与 Champion 不一致、注册表损坏、缺少 XGBoost 可选依赖或模型加载失败时，系统记录具体错误并回退原规则分，不中断分析任务。

全局配置位于 `.env`：

```dotenv
HIGHLIGHT_ML_MODE=off
HIGHLIGHT_ML_REGISTRY_ROOT=./storage/highlight_models
```

模式语义：

- `off`：完全保持原规则评分行为，也是升级后的默认值；
- `shadow`：执行 Champion 和可选 Shadow 推理并记录概率，但筛选、LLM 融合和候选分仍使用规则分；
- `champion`：用校准后的 Champion 概率代替规则分参与初筛和 LLM 融合；推理失败时显式回退规则分。

房间配置 `room_config_json.highlight_ml_mode` 可设为 `inherit/off/shadow/champion`。`inherit` 使用全局值。模型概率不会改变房间的 `highlight_threshold`、`auto_approve_threshold` 和 `review_threshold`，因此现有审核策略仍是唯一运行时阈值来源；产物阈值作为训练评估元数据保留并在预测审计中展示。

每次非 `off` 推理都写入现有 `system_logs` 表，事件为 `highlight_ml_prediction` 或 `highlight_ml_fallback`；候选的 `features_json.highlight_ml` 同时留存模式、Schema、Champion/Shadow 版本和概率。这里刻意复用现有表，不改变严格数据库 Schema，也不要求用户重建 Alpha 数据库。

运行状态可通过以下入口查看：

```powershell
python -m app.cli highlight-model-status
# GET /api/highlight-ml/status
# GET /api/highlight-ml/predictions?limit=50
```

## 训练、部署与回滚操作

训练使用当前数据库中的明确人工反馈。默认比较规则、NumPy Logistic 和可用的 XGBoost；数据不足、只有一个会话、正样本不足或文件/Schema 异常都会以非零退出码停止，不会注册半成品。

```powershell
# 训练；已有 Champion 时新版本默认成为 Shadow
python -m app.cli highlight-model-train

# 不比较 XGBoost，并导出最多 200 条盲审项
python -m app.cli highlight-model-train --no-xgboost --blind-review-limit 200

# 查看角色与历史版本
python -m app.cli highlight-model-status

# 设置/清空 Shadow
python -m app.cli highlight-model-shadow 3
python -m app.cli highlight-model-shadow 0

# 人工确认后提升 Shadow；或回滚到已校验的真实历史版本
python -m app.cli highlight-model-promote
python -m app.cli highlight-model-rollback 2
```

每次角色变化都会递增 `generation`，在线 Worker 在下一次预测前热加载；无需重启。系统不自动晋升 Shadow，因为在线预测日志本身不是真值。

在线成功预测会记录 35 个命名特征、可用性语义、模型版本和概率。漂移命令只读取与当前 Champion 和 Schema 匹配的近期日志，并使用该版本原子附件中的训练基线：

```powershell
python -m app.cli highlight-model-drift --limit 500 --min-recent-samples 20
# GET /api/highlight-ml/drift?limit=500&min_recent_samples=20
```

样本不足、Champion 缺失或基线损坏会明确失败；Web 运维面板显示同一报告。`warning` 表示需要观察，`alert` 表示达到漂移阈值，但系统不会未经人工确认自动重训或切换模型。
