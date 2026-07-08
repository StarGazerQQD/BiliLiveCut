//! BiliLiveCut Rust 加速模块 — V0.1.10
//!
//! O(N**2) 聚类相似度矩阵并行计算 (PyO3 + rayon)。
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
//!     无 Rust 编译环境时自动使用 _speedups_round2_py.py

use pyo3::prelude::*;
use rayon::prelude::*;
use std::collections::{HashMap, HashSet};

// ── 字符 bigram 提取 ────────────────────────────────────────────

/// 提取中文字符级 bigram (与 Python `fast_char_bigrams` 等价的 Rust 实现)。
fn char_bigrams(text: &str) -> HashMap<String, f64> {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() < 2 {
        return HashMap::new();
    }
    let mut bigrams: HashMap<String, f64> = HashMap::with_capacity(chars.len() - 1);
    for w in chars.windows(2) {
        let bg: String = w.iter().collect();
        *bigrams.entry(bg).or_insert(0.0) += 1.0;
    }
    bigrams
}

// ── 余弦相似度 ──────────────────────────────────────────────────

/// 两个加权向量 (String → f64) 的余弦相似度。
fn cosine_sim(a: &HashMap<String, f64>, b: &HashMap<String, f64>) -> f64 {
    let mut dot = 0.0f64;
    let mut na = 0.0f64;

    for (k, va) in a {
        na += va * va;
        if let Some(vb) = b.get(k) {
            dot += va * vb;
        }
    }
    if na == 0.0 {
        return 0.0;
    }
    let nb: f64 = b.values().map(|v| v * v).sum();
    if nb == 0.0 {
        return 0.0;
    }
    dot / (na.sqrt() * nb.sqrt())
}

// ── IDF 加权 ─────────────────────────────────────────────────────

/// 对 bigram 频率向量做 IDF 加权 (TF-IDF 风格)。
///
/// 对 freq 中每个 key,检查它是否同时出现在 other 中。
/// df=1.0 (两边都有) → 低惩罚; df=0.5 (仅自己) → 高惩罚。
fn idf_weight(
    freq: &HashMap<String, f64>,
    other: &HashMap<String, f64>,
    total_docs: f64,
) -> HashMap<String, f64> {
    let mut weighted = HashMap::with_capacity(freq.len());
    for (k, &v) in freq {
        let df: f64 = if other.contains_key(k) { 1.0 } else { 0.5 };
        let idf = (1.0_f64 + total_docs / (df + 1.0)).ln();
        weighted.insert(k.clone(), v * idf);
    }
    weighted
}

// ── 关键词 Jaccard ──────────────────────────────────────────────

/// 关键词集合 Jaccard 重叠率。
fn keyword_jaccard(a: &[String], b: &[String]) -> f64 {
    if a.is_empty() || b.is_empty() {
        return 0.0;
    }
    let sa: HashSet<&str> = a.iter().map(|s| s.as_str()).collect();
    let sb: HashSet<&str> = b.iter().map(|s| s.as_str()).collect();
    let inter = sa.intersection(&sb).count();
    let union = sa.union(&sb).count();
    if union == 0 {
        0.0
    } else {
        inter as f64 / union as f64
    }
}

// ── 时间接近度 ──────────────────────────────────────────────────

/// 时间接近度 (1 小时内线性衰减,与 Python 端 3600s 一致)。
fn time_proximity(ta: Option<f64>, tb: Option<f64>) -> f64 {
    match (ta, tb) {
        (Some(a), Some(b)) => {
            let diff = (a - b).abs();
            if diff < 3600.0 {
                (1.0 - diff / 3600.0).max(0.0)
            } else {
                0.0
            }
        }
        _ => 0.0,
    }
}

// ── 单对相似度 ──────────────────────────────────────────────────

/// 计算两个候选事件的全维度综合相似度。
fn pairwise_sim(
    t_a: &str,
    bg_a: &HashMap<String, f64>,
    kw_a: &[String],
    ts_a: Option<f64>,
    t_b: &str,
    bg_b: &HashMap<String, f64>,
    kw_b: &[String],
    ts_b: Option<f64>,
) -> f64 {
    // 文本相似度 (TF-IDF 余弦)
    let sim_text = if !t_a.is_empty()
        && !t_b.is_empty()
        && !bg_a.is_empty()
        && !bg_b.is_empty()
    {
        let total_docs = bg_a.len().max(bg_b.len()).max(2) as f64;
        let wa = idf_weight(bg_a, bg_b, total_docs);
        let wb = idf_weight(bg_b, bg_a, total_docs);
        cosine_sim(&wa, &wb)
    } else {
        0.0
    };

    // 关键词重叠
    let sim_kw = keyword_jaccard(kw_a, kw_b);

    // 时间接近度
    let time_sim = time_proximity(ts_a, ts_b);

    let score = sim_text * 0.55 + sim_kw * 0.25 + time_sim * 0.20;
    // round to 4 decimal places (match Python `round(score, 4)`)
    (score * 10000.0).round() / 10000.0
}

// ── PyO3 入口 ────────────────────────────────────────────────────

/// 并行计算 N×N 聚类相似度矩阵 (对称,对角 = 1.0)。
///
/// :param texts: 每个候选的 ASR 文本列表。
/// :param keywords: 每个候选的关键词列表 (list[list[str]])。
/// :param timestamps: 每个候选的 Unix 时间戳 (list[Optional[float]])。
/// :returns: N×N 矩阵 list[list[float]]。
#[pyfunction]
fn cluster_similarity_matrix_rust(
    texts: Vec<String>,
    keywords: Vec<Vec<String>>,
    timestamps: Vec<Option<f64>>,
) -> PyResult<Vec<Vec<f64>>> {
    let n = texts.len();

    // 快速路径: 0 或 1 个元素
    if n < 2 {
        return Ok(vec![vec![0.0; n]; n]);
    }

    // ── 阶段 1: 预计算 bigram 频率 (串行,但 O(N * avg_text_len),可接受) ──
    let bigrams: Vec<HashMap<String, f64>> =
        texts.iter().map(|t| char_bigrams(t)).collect();

    // ── 阶段 2: 并行计算所有 (i<j) 对的相似度 ──
    // 收集下标对
    let pairs: Vec<(usize, usize)> = (0..n)
        .flat_map(|i| ((i + 1)..n).map(move |j| (i, j)))
        .collect();

    // rayon 并行计算
    let results: Vec<(usize, usize, f64)> = pairs
        .par_iter()
        .map(|&(i, j)| {
            let sim = pairwise_sim(
                &texts[i],
                &bigrams[i],
                &keywords[i],
                timestamps[i],
                &texts[j],
                &bigrams[j],
                &keywords[j],
                timestamps[j],
            );
            (i, j, sim)
        })
        .collect();

    // ── 阶段 3: 构建矩阵 ──
    let mut matrix = vec![vec![0.0f64; n]; n];
    for i in 0..n {
        matrix[i][i] = 1.0;
    }
    for (i, j, sim) in results {
        matrix[i][j] = sim;
        matrix[j][i] = sim;
    }

    Ok(matrix)
}

// ── 模块注册 ─────────────────────────────────────────────────────

#[pymodule]
fn _rust_cluster(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cluster_similarity_matrix_rust, m)?)?;
    Ok(())
}
