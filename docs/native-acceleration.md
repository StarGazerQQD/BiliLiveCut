# 原生加速模块

适用版本：`0.1.18.4-alpha`。

Rust 构建通过 `CARGO_ENCODED_RUSTFLAGS` 传递路径映射，保留显式编译选项，避免用户目录或 Cargo 缓存路径嵌入原生模块；含空格的路径保持完整。Payload 在收录原生模块前检查 UTF-8/UTF-16 本机构建路径，发现残留则拒绝构建。

当前业务架构与安装入口见[项目 README](../README.md)和 [Portable 说明](../packaging/portable/README.md)；版本变更见 [Changelog 归档索引](changelog/CHANGELOG_INDEX.md)。

BiliLiveCut 只保留当前 `app.accelerators` 原生接口。业务代码统一调用
`app.accelerators.dispatcher`，不直接依赖编译扩展；旧的 `app.analysis` 原生
模块路径和函数别名均不再提供。

## V0.1.18.4 已完成的原生化清单

| 热点 | 实现 | 接入结果 | 原因 |
|---|---|---|---|
| 候选聚类 N×N 相似度矩阵 | Rust + rayon | 归入 `_rust_speedups` | 两两计算独立，适合 rayon 并行 |
| 弹幕复读率、标点强度、高情绪命中率、代表消息 | Rust | 新增 | 单遍字符串扫描和计数，Python 对象循环占比高 |
| 音频局部峰值筛选与间距去重 | Cython | 新增 | 五分钟 RMS 包络上的排序与逐点筛选是稳定数值热循环 |
| RMS 连续静音区间提取 | Cython | 新增 | 单遍阈值状态机，可避免 Python 逐帧分支 |
| 热点滚动历史稳健增幅（中位数、MAD） | Cython | 新增 | 检测器每个 tick 对多个模态重复执行相同数值流程 |

已有的 C Aho-Corasick、字符 bigram、余弦相似度、梗词计数，以及已有的
Cython 弹幕基线、SRT 组装继续保留在同一命名空间。

## 当前模块边界

- `app.accelerators._c_speedups`：文本匹配与向量小核。
- `app.accelerators._cython_speedups`：音频包络后处理、稳健滚动基线、弹幕基线、SRT、聚类后备。
- `app.accelerators._rust_speedups`：rayon 聚类矩阵与弹幕文本特征。
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

Windows Payload 和 Full Bundle 将当前 Python ABI 的三个 `.pyd` 视为强制发行
契约；普通源码运行仍可按函数粒度使用 Python 参考实现。
