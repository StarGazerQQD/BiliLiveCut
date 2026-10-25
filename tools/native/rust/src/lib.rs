//! BiliLiveCut Rust 并行聚类与弹幕文本特征加速模块。
//!
//! - O(N**2) 聚类相似度矩阵并行计算 (PyO3 + rayon)
//! - 弹幕复读率、情绪强度和代表消息单遍汇总
//!
//! 设计:
//! - Python 端一次性提取 texts/keywords/timestamps → 传入 Rust
//! - Rust 端完成 bigram 提取、IDF 加权、余弦相似度、Jaccard、时间衰减
//! - rayon 并行处理所有 N*(N-1)/2 对
//! - 返回 N×N f64 矩阵
//!
//! 编译:
//!     cd tools/native/rust && cargo build --release
//!     或 python tools/native/build_rust.py
//!
//! 回退:
//!     无 Rust 编译环境时由 app.accelerators.dispatcher 使用 Python 参考实现

use pyo3::prelude::*;
use rayon::prelude::*;
use std::collections::{HashMap, HashSet};

// ── 字符 bigram 提取 ────────────────────────────────────────────

/// 提取中文字符级 bigram (与 Python `fast_char_bigrams` 等价的 Rust 实现)。
fn char_bigrams(text: &str) -> HashMap<u64, f64> {
    // Python 的单字符快速路径保留 ASCII 空白；多字符输入才过滤空白。
    if text.len() == 1 {
        return HashMap::from([(((u64::from(text.as_bytes()[0]) + 1) << 21), 1.0)]);
    }
    let chars: Vec<char> = text.chars().filter(|ch| *ch > ' ').collect();
    if chars.is_empty() {
        return HashMap::new();
    }
    if chars.len() == 1 {
        return HashMap::from([(((u64::from(chars[0]) + 1) << 21), 1.0)]);
    }
    let mut bigrams: HashMap<u64, f64> = HashMap::with_capacity(chars.len() - 1);
    for w in chars.windows(2) {
        // 两个码点各占 21 位；低位零保留给单字符，避免字符串分配且保持无歧义。
        let bg = ((u64::from(w[0]) + 1) << 21) | (u64::from(w[1]) + 1);
        *bigrams.entry(bg).or_insert(0.0) += 1.0;
    }
    bigrams
}

/// 在频率表上直接累加 TF-IDF 余弦，避免每对候选分配两张加权字典。
fn weighted_cosine(a: &HashMap<u64, f64>, b: &HashMap<u64, f64>) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let total_docs = a.len().max(b.len()).max(2) as f64;
    let shared = (1.0 + total_docs / 2.0).ln();
    let unique = (1.0 + total_docs / 1.5).ln();
    let (mut dot, mut na, mut nb) = (0.0, 0.0, 0.0);
    for (key, &count) in a {
        if let Some(&other) = b.get(key) {
            let va = count * shared;
            dot += va * (other * shared);
            na += va * va;
        } else {
            let va = count * unique;
            na += va * va;
        }
    }
    for (key, &count) in b {
        let vb = count * if a.contains_key(key) { shared } else { unique };
        nb += vb * vb;
    }
    if na == 0.0 || nb == 0.0 {
        0.0
    } else {
        dot / (na.sqrt() * nb.sqrt())
    }
}

/// 使用每个候选预先构建的关键词集合。
fn keyword_jaccard(a: &HashSet<String>, b: &HashSet<String>) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let inter = a.intersection(b).count();
    inter as f64 / (a.len() + b.len() - inter) as f64
}

// ── 时间接近度 ──────────────────────────────────────────────────

/// 时间接近度 (1 小时内线性衰减,与 Python 端 3600s 一致)。
fn time_proximity(ta: Option<i64>, tb: Option<i64>) -> f64 {
    match (ta, tb) {
        (Some(a), Some(b)) => {
            let micros = a.abs_diff(b);
            if micros < 3_600_000_000 {
                let diff = micros as f64 / 1_000_000.0;
                (1.0 - diff / 3600.0).max(0.0)
            } else {
                0.0
            }
        }
        _ => 0.0,
    }
}

/// 将非负相似度精确舍入到四位小数，与 Python round 的二进制输入、偶数舍入一致。
/// 使用 u128 保留乘 10000 后的全部位，避免浮点乘法先舍入改变半值判断。
fn round_similarity(score: f64) -> f64 {
    let bits = score.to_bits();
    let exponent = ((bits >> 52) & 0x7ff) as i32;
    let shift = 1075 - exponent;
    if shift >= 128 {
        return 0.0;
    }
    let mantissa = (bits & ((1u64 << 52) - 1)) | (1u64 << 52);
    let scaled = u128::from(mantissa) * 10000;
    let mut rounded = scaled >> shift;
    let remainder = scaled & ((1u128 << shift) - 1);
    let halfway = 1u128 << (shift - 1);
    if remainder > halfway || (remainder == halfway && rounded & 1 != 0) {
        rounded += 1;
    }
    rounded as f64 / 10000.0
}

// ── 单对相似度 ──────────────────────────────────────────────────

/// 计算两个候选事件的全维度综合相似度。
fn pairwise_sim(
    t_a: &str,
    bg_a: &HashMap<u64, f64>,
    kw_a: &HashSet<String>,
    ts_a: Option<i64>,
    t_b: &str,
    bg_b: &HashMap<u64, f64>,
    kw_b: &HashSet<String>,
    ts_b: Option<i64>,
) -> f64 {
    // 文本相似度 (TF-IDF 余弦)
    let sim_text = if !t_a.is_empty() && !t_b.is_empty() && !bg_a.is_empty() && !bg_b.is_empty() {
        weighted_cosine(bg_a, bg_b)
    } else {
        0.0
    };

    // 关键词重叠
    let sim_kw = keyword_jaccard(kw_a, kw_b);

    // 时间接近度
    let time_sim = time_proximity(ts_a, ts_b);

    let score = sim_text * 0.55 + sim_kw * 0.25 + time_sim * 0.20;
    // round to 4 decimal places (match Python `round(score, 4)`)
    round_similarity(score)
}

// ── PyO3 入口 ────────────────────────────────────────────────────

/// 并行计算 N×N 聚类相似度矩阵 (对称,对角 = 1.0)。
///
/// :param texts: 每个候选的 ASR 文本列表。
/// :param keywords: 每个候选的关键词列表 (list[list[str]])。
/// :param timestamps: 每个候选的 相对于同一基点的整数微秒 (list[Optional[int]])。
/// :returns: N×N 矩阵 list[list[float]]。
#[pyfunction]
fn cluster_similarity_matrix(
    py: Python<'_>,
    texts: Vec<String>,
    keywords: Vec<Vec<String>>,
    timestamps: Vec<Option<i64>>,
) -> PyResult<Vec<Vec<f64>>> {
    let n = texts.len();
    if keywords.len() != n || timestamps.len() != n {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "texts, keywords and timestamps must have equal lengths",
        ));
    }

    Ok(py.detach(move || similarity_matrix(&texts, keywords, &timestamps)))
}

/// 计算和消费矩阵均留在 Rust 内，避免为聚类创建 N² 个 Python 浮点对象。
fn similarity_matrix(
    texts: &[String],
    keywords: Vec<Vec<String>>,
    timestamps: &[Option<i64>],
) -> Vec<Vec<f64>> {
    let n = texts.len();
    // 快速路径: 0 或 1 个元素
    if n < 2 {
        return vec![vec![0.0; n]; n];
    }

    let bigrams: Vec<HashMap<u64, f64>> = texts.iter().map(|t| char_bigrams(t)).collect();
    let keywords: Vec<HashSet<String>> = keywords
        .into_iter()
        .map(|v| v.into_iter().collect())
        .collect();
    let mut matrix = vec![vec![0.0; n]; n];
    let compute = |(i, row): (usize, &mut Vec<f64>)| {
        row[i] = 1.0;
        for (j, value) in row.iter_mut().enumerate().skip(i + 1) {
            *value = pairwise_sim(
                &texts[i],
                &bigrams[i],
                &keywords[i],
                timestamps[i],
                &texts[j],
                &bigrams[j],
                &keywords[j],
                timestamps[j],
            );
        }
    };
    // 更轻的成对运算提高并行分界，避免小矩阵线程调度回退。
    if n < 128 {
        matrix.iter_mut().enumerate().for_each(compute);
    } else {
        matrix.par_iter_mut().enumerate().for_each(compute);
    }
    for i in 0..n {
        for j in (i + 1)..n {
            matrix[j][i] = matrix[i][j];
        }
    }
    matrix
}

/// 一次完成相似度、并查集和簇内统计，仅返回最终分组。
#[pyfunction]
fn cluster_candidate_groups(
    py: Python<'_>,
    texts: Vec<String>,
    keywords: Vec<Vec<String>>,
    timestamps: Vec<Option<i64>>,
    threshold: f64,
) -> PyResult<Vec<(usize, Vec<usize>, f64)>> {
    if keywords.len() != texts.len() || timestamps.len() != texts.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "texts, keywords and timestamps must have equal lengths",
        ));
    }
    if !threshold.is_finite() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "threshold must be finite",
        ));
    }
    Ok(py
        .detach(move || group_matrix(&similarity_matrix(&texts, keywords, &timestamps), threshold)))
}

/// 单遍汇总弹幕文本特征。
///
/// :param texts: 当前时间桶内的弹幕正文。
/// :param high_emotion_tokens: 当前版本定义的高情绪词表。
/// :returns: (复读率, 情绪强度, 高情绪命中率, 高频代表消息)。
#[pyfunction]
fn danmaku_text_features(
    texts: Vec<String>,
    high_emotion_tokens: Vec<String>,
) -> (f64, f64, f64, Vec<String>) {
    if texts.is_empty() {
        return (0.0, 0.0, 0.0, Vec::new());
    }

    let mut counts: HashMap<&str, (usize, usize)> = HashMap::with_capacity(texts.len());
    let mut punctuation_hits = 0usize;
    let mut emotion_hits = 0usize;
    for (index, text) in texts.iter().enumerate() {
        let entry = counts.entry(text.as_str()).or_insert((0, index));
        entry.0 += 1;
        if text.contains('!') || text.contains('！') || text.contains('?') || text.contains('？')
        {
            punctuation_hits += 1;
        }
        if high_emotion_tokens.iter().any(|token| text.contains(token)) {
            emotion_hits += 1;
        }
    }

    let total = texts.len() as f64;
    let repetition = counts
        .values()
        .map(|(count, _first_index)| *count)
        .max()
        .unwrap_or(0) as f64
        / total;
    let punctuation_rate = punctuation_hits as f64 / total;
    let high_emotion = emotion_hits as f64 / total;
    let intensity = (punctuation_rate * 0.45 + high_emotion * 0.55).clamp(0.0, 1.0);

    let mut ranked: Vec<(&str, usize, usize)> = counts
        .into_iter()
        .map(|(text, (count, first_index))| (text, count, first_index))
        .collect();
    let order = |left: &(&str, usize, usize), right: &(&str, usize, usize)| {
        right.1.cmp(&left.1).then_with(|| left.2.cmp(&right.2))
    };
    if ranked.len() > 32 {
        ranked.select_nth_unstable_by(2, order);
        ranked.truncate(3);
    }
    ranked.sort_unstable_by(order);
    let representatives = ranked
        .into_iter()
        .take(3)
        .map(|(text, _count, _first_index)| text.to_owned())
        .collect();

    (
        repetition.clamp(0.0, 1.0),
        intensity,
        high_emotion.clamp(0.0, 1.0),
        representatives,
    )
}

/// 路径折半并保持原有“右根连接到左根”规则，根编号用于默认主题标题。
fn find_root(parent: &mut [usize], mut index: usize) -> usize {
    while parent[index] != index {
        parent[index] = parent[parent[index]];
        index = parent[index];
    }
    index
}

/// 不访问 Python/数据库：按原遍历顺序连接，保留簇顺序并统计全部簇内边。
#[pyfunction]
fn cluster_groups(
    py: Python<'_>,
    matrix: Vec<Vec<f64>>,
    threshold: f64,
) -> PyResult<Vec<(usize, Vec<usize>, f64)>> {
    let n = matrix.len();
    if !threshold.is_finite()
        || matrix
            .iter()
            .any(|row| row.len() != n || row.iter().any(|v| !v.is_finite()))
    {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "cluster matrix must be square and finite; threshold must be finite",
        ));
    }
    Ok(py.detach(move || group_matrix(&matrix, threshold)))
}

fn group_matrix(matrix: &[Vec<f64>], threshold: f64) -> Vec<(usize, Vec<usize>, f64)> {
    let n = matrix.len();
    let mut parent: Vec<usize> = (0..n).collect();
    for (i, row) in matrix.iter().enumerate() {
        for (j, &similarity) in row.iter().enumerate().skip(i + 1) {
            if similarity >= threshold {
                let a = find_root(&mut parent, i);
                let b = find_root(&mut parent, j);
                if a != b {
                    parent[b] = a;
                }
            }
        }
    }
    let mut positions = vec![usize::MAX; n];
    let mut groups: Vec<(usize, Vec<usize>, f64)> = Vec::new();
    for i in 0..n {
        let root = find_root(&mut parent, i);
        if positions[root] == usize::MAX {
            positions[root] = groups.len();
            groups.push((root, Vec::new(), 0.0));
        }
        groups[positions[root]].1.push(i);
    }
    groups.retain(|(_, members, _)| members.len() >= 2);
    for (_, members, average) in &mut groups {
        let mut total = 0.0;
        let mut count = 0usize;
        for (position, &a) in members.iter().enumerate() {
            for &b in &members[position + 1..] {
                total += matrix[a][b];
                count += 1;
            }
        }
        *average = total / count as f64;
    }
    groups
}

// ── 模块注册 ─────────────────────────────────────────────────────

#[pymodule(gil_used = true)]
fn _rust_speedups(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cluster_similarity_matrix, m)?)?;
    m.add_function(wrap_pyfunction!(danmaku_text_features, m)?)?;
    m.add_function(wrap_pyfunction!(cluster_groups, m)?)?;
    m.add_function(wrap_pyfunction!(cluster_candidate_groups, m)?)?;
    Ok(())
}
