# 原生加速模块

适用版本：`0.1.20.2-alpha`。

Rust 构建通过 `CARGO_ENCODED_RUSTFLAGS` 传递路径映射，保留显式编译选项，避免用户目录或 Cargo 缓存路径嵌入原生模块；含空格的路径保持完整。Payload 在收录原生模块前检查 UTF-8/UTF-16 本机构建路径，发现残留则拒绝构建。

自定义编译选项请通过 `RUSTFLAGS` 或 `CARGO_ENCODED_RUSTFLAGS` 显式提供。若仅在 Cargo 的 `build` / `target` 配置（包括对应环境变量）设置 `rustflags`，或配置使用 `include`，构建会明确报错并要求将完整选项传入上述变量，以免路径映射覆盖原有链接或目标平台参数。相对 `CARGO_HOME` 按 Rust 源码目录解析。

当前业务架构与安装入口见[项目 README](../../README.md)和 [Portable 说明](../../packaging/portable/README.md)；版本变更见 [Changelog 归档索引](../changelog/CHANGELOG_INDEX.md)。

BiliLiveCut 只保留当前 `app.accelerators` 原生接口。业务代码统一调用
`app.accelerators.dispatcher`，不直接依赖编译扩展；旧的 `app.analysis` 原生
模块路径和函数别名均不再提供。

## 当前已接入的原生化清单

| 热点 | 实现 | 接入结果 | 原因 |
|---|---|---|---|
| ASR 复核编辑距离 | RapidFuzz C++ | 精确 Unicode 编辑距离 | 保留插入、删除、替换单位成本，不设置 cutoff、不预处理文本；20%/50% 决策和原因比率保持不变 |
| 关键词匹配自动机复用 | C / Python | 有界词表缓存 | 完整词表作为键，最多 8 组、每组最多 64 词且总长最多 512 字符；大词表完整计算，不截断匹配 |
| ASR 文本规范化与原文位置 | Cython | Unicode 字符扫描 | 使用 CPython 字符分类及完整 casefold，保留 ß/İ 等展开字符到原文的位置 |
| 音频信号分桶 | Cython + NumPy | 下标分组及数组统计 | 避免逐样本浮点二元组和重复数组转换，保留 NumPy 均值、中位数及首个峰值 |
| 代表弹幕及情绪统计 | Cython / C | 单次清洗计数与评分 | 每条正文只计算一次评分，保留首次文本、稳定顺序、感叹号范围及梗词计数 |
| 多峰转写窗口 | Python + C bisect | 调用内区间索引 | 完整 JSON 内容区分更新，允许乱序/嵌套/零时长词，结果恢复原顺序且不暴露缓存对象 |
| 热点滚动文本特征 | Cython + 集合复用 | 每个桶提取一次词项 | 长文本多窗口复用集合，短场次复用单个 tick 的评分和证据统计，不保存全局转写缓存 |
| 字幕边界与候选区间 | Cython | 数值边界处理 | 在生成 SRT 时完成毫秒约束；候选 UTC 数值坐标只转换一次，保留贪心选择和 IoU 规则 |
| 并查集与簇内平均相似度 | Rust | 独立 CPU 内核 | 保留连接顺序、根编号、簇顺序与浮点累加顺序；计算期间释放 GIL |
| 文本词项、实体与新颖度 | Cython | 独立 CPU 字符内核 | 保留 Python Unicode 小写规则与原 ASCII/CJK 分词范围，避免创建临时差集/并集 |
| ASR 连续重复检测 | C++17 / Python | 独立 CPU 字符内核 | C++ 使用 O(N) 后缀数组与 Lyndon 候选、精确分段和 LCP 上界剪枝；Python 参考路径 O(N log N)。不限周期，至少三次，平局取较短周期和较早起点；详见[内核说明](repeat-cpu-kernel.md) |
| ASR 复核风险统计 | Cython | 字符计数与首个热词冲突 | 保留 Python 风险权重、原因顺序及模型调用条件 |
| PCM16 采样计数、精确平方和与峰值 | Cython | 独立 CPU 字节内核 | 单遍整数计算，保留取消与 WAV 完整性检查；分块累积避免长音频溢出 |
| 候选聚类 N×N 相似度矩阵 | Rust + rayon | 归入 `_rust_speedups` | 预计算关键词并直接累计加权余弦；按行生成矩阵，拥有数据后释放 GIL，128 项起按行并行 |
| 弹幕复读率、标点强度、高情绪命中率、代表消息 | Rust | 新增 | 单遍字符串扫描和计数，Python 对象循环占比高 |
| 音频局部峰值筛选与间距去重 | Cython | 新增 | 五分钟 RMS 包络上的排序与逐点筛选是稳定数值热循环 |
| RMS 连续静音区间提取 | Cython | 新增 | 单遍阈值状态机，可避免 Python 逐帧分支 |
| 热点滚动历史稳健增幅（中位数、MAD） | Cython | 新增 | 有界独立数值缓冲区，原地排序并释放 GIL；不修改输入，列表/元组避免 NumPy 往返 |

已有的 C Aho-Corasick、字符 bigram、余弦相似度、梗词计数，以及已有的
Cython 弹幕基线、SRT 组装继续保留在同一命名空间。

## 当前模块边界

- `app.accelerators._c_speedups`：文本匹配与向量小核。
- `app.accelerators._cython_speedups`：PCM16 统计、文本新颖度、ASR 复核统计、音频包络后处理、稳健滚动基线、弹幕基线、SRT、聚类后备。
- `app.accelerators._repeat_speedups`：任意周期连续重复检测的 C++17 内核。
- `app.accelerators._rust_speedups`：rayon 聚类矩阵、并查集/簇内统计与弹幕文本特征。
- `app.accelerators.python_fallback`：与每个原生函数逐项等价的可测试参考实现。

以下链路没有改写为 Rust/Cython：RMS 包络本身已由 NumPy 向量化；FFmpeg
解码、数据库查询、LLM/ASR 调度和文件发布主要受 I/O 或外部进程限制；Brotli
解压已由现有原生依赖完成。把这些部分再包一层扩展不会缩短主要耗时，反而会
扩大跨语言状态与异常边界。

## 构建与诊断

```powershell
python setup.py build_ext --inplace --force
python tools/native/build_rust.py
```

```python
from app.accelerators import dispatcher

print(dispatcher.get_backend())
print(dispatcher.get_cython_backend())
print(dispatcher.get_rust_backend())
print(dispatcher.get_cluster_backend())
```

Windows Payload 和 Full Bundle 将当前 Python ABI 的四个 `.pyd` 视为强制发行
契约；普通源码运行仍可按函数粒度使用 Python 参考实现。

Docker 同样构建 Rust 弹幕与矩阵内核，并在真实容器验收中确认加载成功。PCM 内核只读取主机内存中的小端 PCM16 字节，返回 Python 整数统计；不接收 CUDA 张量，不修改样本、设备配置或模型加载路径。没有编译扩展时使用等价 Python 参考实现。

Rust Python 绑定固定为 PyO3 0.29.3，替换存在已公布内存/线程安全问题的旧 0.22.6；构建必须遵守 Cargo.lock。继续显式要求 GIL，不随依赖升级自动声明支持未经验证的无 GIL 解释器。

矩阵时间列使用相对整数微秒，避免 Unix 浮点秒丢失精度；四位小数遵守 Python 的偶数舍入规则。不同动态时区及 Rust UTF-8 无法表示的孤立代理码点由等价参考实现处理；不会截断、替换文本或忽略计算错误。Rust 构建按平台选择唯一动态库文件名，拒绝把 Cargo 的 `.d` 依赖清单或其他平台产物当作扩展。

仅安装 Cython 时也按相同规则预处理时间：有效 ISO 字符串每项只解析一次，缺失或无效时间不贡献时间接近度；无时区与有时区 datetime 混用仍明确报错。后端回归分别覆盖默认选择、未安装 Rust 和纯 Python 三种路径。

C 字符 bigram 按 Python Unicode 码点处理；单汉字、空白和孤立代理码点与参考实现一致，bytes 输入必须是合法 UTF-8。

主题聚类通过 `cluster_candidate_groups` 在一次 Rust 调用内完成相似度矩阵、并查集和簇内统计，避免 N² 个浮点对象在 Python/Rust 间往返；原矩阵与分组接口继续保留。字符 bigram 使用无歧义的 64 位码点组合键，减少逐词字符串分配；单字符与双字符保持独立。分组接口仍校验有限阈值，返回原根编号、成员顺序和簇内均值；主题、关联及事务由 Python 业务层维护。

C 关键词自动机保留全部后缀输出和重复词，不再限制每节点 8 个结果或忽略 256 字节以上的词。显式长度复制保留文本中的 NUL；自动机缓存只保留词表，不保留用户转写文本和匹配结果。更换房间词表或词表顺序后使用相应独立自动机。

文本内核只接收字符串，保留空文本、新颖度/主题变化/实体变化评分公式；证据无需实体统计时显式返回 `None`，不会假造统计值。RapidFuzz 作为直接运行依赖安装，Portable 和 uv 环境均锁定 3.14.6。Windows wheel 随附其所需的 MSVCP140 运行库，Portable Python 提供 VCRUNTIME140；不要求用户另装编译工具或系统 C++ 运行库。

## 数值语义与性能边界

多峰分析中的 `TranscriptWindowCache` 只存活于一次评分调用。索引先完整校验词 JSON，再按起点及前缀最远终点缩小候选范围；每次返回新的词对象。单峰仍使用直接提取，原 JSON 改变后重新建立索引，不复用旧正文。

热点检测在桶数足以形成多个滚动窗口且总正文至少 1024 字符时，复用各桶的词项与实体集合；以空格连接桶正文的分词边界保持不变。评分、证据和输出正文共用同一 tick 的结果。字幕保留原时分秒拆分和毫秒截断次序，再约束到下一块起点及剪辑结束，避免生成 SRT 后再次解析。

滚动中位数/MAD复制到经过长度溢出检查的独立数值缓冲区，排序不修改调用方历史；有限值过滤保留，不使用 `-ffast-math` 或 `/fp:fast`。列表/元组直接进入内核，其他数值序列转换为连续 float64。Rust 矩阵预计算关键词集合、直接累计加权余弦，按行计算上三角后镜像填充；释放 GIL 时仅访问 Rust 拥有的数据。小矩阵串行，大矩阵按行并行；没有修改 Torch/CTranslate2 或设备选择。

完整调用边界基准：`python scripts/benchmark_cpu_kernels.py --output benchmark.json`。测量包括参数转换和结果返回，先逐项校验等价，再交替测量前后路径并记录七轮中位数。编辑距离同时测量近似文本与无关文本；关键词比较当前 C 内核的重复构建与缓存调用；聚类比较相同矩阵实现的两次调用和合并调用，仅归因于减少往返。合成 CPU 基准不代表 ASR/CUDA 或数据库端到端吞吐，也不保证未测平台及所有输入均加速。

Rust 依赖的完整许可证文本随源码、Docker 和 Windows Payload 分发；Payload 内路径为 `tools/native/rust/THIRD_PARTY_NOTICES.md`。发行验收逐字节核对该文件与源码一致。

## 查询与 CPU 内核的配合

单次热点分析从当前数据库会话加载弹幕延迟和覆盖快照，以起点及前缀最远终点索引查询每个时间桶。每个桶仍须由一个原始连接区间完整覆盖，相邻区间不会被拼接；本地导入继续依据 `has_comments`。快照不跨分析调用保存，后续分析重新读取采集状态。

主题聚类优先使用事件留存正文；缺失时在候选查询内通过关联子查询读取首个完整覆盖片段的 `final_text`。不选择后续非空转写、不读取其他场次，不改变主题写入事务。弹幕概览在 SQL 中限制近期记录数量，历史数量和热度以流式方式按原顺序累计，保留浮点舍入结果。

作业列表只读取必要的 JSON 值，逐条完整校验，再按权限筛选并稳定选取最近记录；内存随返回上限及单批记录增长。其他用户或显示页之外的损坏记录仍明确报错。登记去重沿用同一写事务、权限主体、操作类型及请求校验，不增加数据库格式或索引迁移。
