/* =========================================================================
 * _c_speedups.c — BiliLiveCut C 加速扩展 (V0.1.9)
 *
 * 提供以下 Python 可调用函数:
 *   1. fast_ahocorasick_build(patterns) -> automaton
 *      构建 Aho-Corasick 多模式匹配自动机。
 *
 *   2. fast_ahocorasick_search(automaton, text) -> list[str]
 *      对文本执行一次扫描,返回所有命中的模式。
 *
 *   3. fast_char_bigrams(text) -> list[str]
 *      零拷贝风格字符级 bigram 提取。
 *
 *   4. fast_cosine_similarity(vec_a, vec_b) -> float
 *      基于 Python dict 的余弦相似度。
 *
 * 编译: python setup.py build_ext --inplace
 * ========================================================================= */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <string.h>
#include <math.h>
#include <wchar.h>

/* MSVC 兼容: strndup 是 POSIX 函数, MSVC 不提供 */
#ifdef _MSC_VER
#ifndef strndup
static inline char *strndup(const char *s, size_t n) {
    size_t len = 0;
    while (len < n && s[len]) len++;
    char *dup = (char *)malloc(len + 1);
    if (dup) {
        memcpy(dup, s, len);
        dup[len] = '\0';
    }
    return dup;
}
#endif
#endif

/* ── UTF-8 校验 (V0.1.12.5) ────────────────────────────────────────── */

/* 检查 bytes 是否为合法 UTF-8 */
static int _is_valid_utf8(const char *s, Py_ssize_t len) {
    const unsigned char *p = (const unsigned char *)s;
    const unsigned char *end = p + len;
    while (p < end) {
        if (*p < 0x80) { p++; continue; }
        int clen;
        if ((*p & 0xE0) == 0xC0) {        /* 2 字节 */
            clen = 2;
            if (p + 2 > end) return 0;
        } else if ((*p & 0xF0) == 0xE0) { /* 3 字节 */
            clen = 3;
            if (p + 3 > end) return 0;
        } else if ((*p & 0xF8) == 0xF0) { /* 4 字节 */
            clen = 4;
            if (p + 4 > end) return 0;
        } else {
            return 0;  /* 非法起始字节 */
        }
        for (int i = 1; i < clen; i++) {
            if ((p[i] & 0xC0) != 0x80) return 0;  /* 非法续字节 */
        }
        p += clen;
    }
    return 1;
}

/* ───────────────────────────────────────────────────────────────────────
 * Aho-Corasick 自动机数据结构
 * ─────────────────────────────────────────────────────────────────────── */

#define AC_ALPHABET 256
#define AC_MAX_NODES 16384

typedef struct {
    int next[AC_ALPHABET];   /* 子节点索引, -1 表示无 */
    int fail;                /* 失败链接 */
    int output_len;          /* 输出模式数量 */
    char *outputs[8];        /* 最多 8 个同节点输出 */
    int output_lens[8];      /* 对应 pattern 长度 */
} ACNode;

typedef struct {
    PyObject_HEAD
    ACNode *nodes;
    int node_count;
    int node_cap;
    PyObject *patterns;      /* 原始模式列表(保持引用防止GC) */
} ACAutomaton;

static void ac_automaton_dealloc(ACAutomaton *self) {
    if (self->nodes) {
        for (int i = 0; i < self->node_count; i++) {
            for (int j = 0; j < self->nodes[i].output_len; j++) {
                free(self->nodes[i].outputs[j]);
            }
        }
        free(self->nodes);
    }
    Py_XDECREF(self->patterns);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

static PyTypeObject ACAutomatonType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "_c_speedups.ACAutomaton",
    .tp_basicsize = sizeof(ACAutomaton),
    .tp_dealloc = (destructor)ac_automaton_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
};

static int ac_add_node(ACAutomaton *self) {
    if (self->node_count >= self->node_cap) {
        int new_cap = self->node_cap * 2;
        ACNode *new_nodes = realloc(self->nodes, new_cap * sizeof(ACNode));
        if (!new_nodes) return -1;
        self->nodes = new_nodes;
        /* 初始化新分配的节点 */
        for (int i = self->node_cap; i < new_cap; i++) {
            memset(&self->nodes[i], 0, sizeof(ACNode));
            for (int j = 0; j < AC_ALPHABET; j++) self->nodes[i].next[j] = -1;
            self->nodes[i].fail = 0;
        }
        self->node_cap = new_cap;
    }
    int idx = self->node_count++;
    memset(&self->nodes[idx], 0, sizeof(ACNode));
    for (int j = 0; j < AC_ALPHABET; j++) self->nodes[idx].next[j] = -1;
    return idx;
}

static int ac_insert_pattern(ACAutomaton *self, const char *pattern, int plen) {
    int node = 0;
    for (int i = 0; i < plen; i++) {
        unsigned char c = (unsigned char)pattern[i];
        if (self->nodes[node].next[c] == -1) {
            int child = ac_add_node(self);
            if (child < 0) return -1;  /* OOM: 通知调用方 */
            self->nodes[node].next[c] = child;
        }
        node = self->nodes[node].next[c];
    }
    if (self->nodes[node].output_len < 8) {
        int oi = self->nodes[node].output_len++;
        self->nodes[node].outputs[oi] = strndup(pattern, plen);
        if (!self->nodes[node].outputs[oi]) { self->nodes[node].output_len--; return -1; }
        self->nodes[node].output_lens[oi] = plen;
    }
    return 0;
}

/* BFS 构造失败链接 — V0.1.9.1: 使用堆分配队列防止节点数超 AC_MAX_NODES 时栈溢出。 */
static void ac_build_failure(ACAutomaton *self) {
    int *queue = malloc(self->node_cap * sizeof(int));
    if (!queue) {
        PyErr_NoMemory();
        return;
    }
    int head = 0, tail = 0;
    int qcap = self->node_cap;
    for (int c = 0; c < AC_ALPHABET; c++) {
        int child = self->nodes[0].next[c];
        if (child != -1) {
            self->nodes[child].fail = 0;
            queue[tail++] = child;
        } else {
            self->nodes[0].next[c] = 0;
        }
    }
    while (head < tail) {
        int r = queue[head++];
        for (int c = 0; c < AC_ALPHABET; c++) {
            int child = self->nodes[r].next[c];
            if (child != -1) {
                /* 队列扩容 (节点数可能超过初始 cap) */
                if (tail >= qcap) {
                    int new_cap = qcap * 2;
                    int *nq = realloc(queue, new_cap * sizeof(int));
                    if (!nq) { free(queue); PyErr_NoMemory(); return; }
                    queue = nq;
                    qcap = new_cap;
                }
                queue[tail++] = child;
                int f = self->nodes[r].fail;
                while (self->nodes[f].next[c] == -1) f = self->nodes[f].fail;
                self->nodes[child].fail = self->nodes[f].next[c];
                /* 合并输出 — V0.1.9.2: strndup 独立副本,避免 double-free。
                   原代码直接复制指针,导致 child 和 of 持有同一堆地址,
                   dealloc 时对同一指针多次 free()。 */
                int of = self->nodes[child].fail;
                for (int j = 0; j < self->nodes[of].output_len; j++) {
                    if (self->nodes[child].output_len >= 8) break;
                    int oi = self->nodes[child].output_len++;
                    char *dup = strndup(self->nodes[of].outputs[j], self->nodes[of].output_lens[j]);
                    if (!dup) { self->nodes[child].output_len--; continue; }
                    self->nodes[child].outputs[oi] = dup;
                    self->nodes[child].output_lens[oi] = self->nodes[of].output_lens[j];
                }
            } else {
                self->nodes[r].next[c] = -1;
            }
        }
    }
    free(queue);
}

/* ───────────────────────────────────────────────────────────────────────
 * fast_ahocorasick_build(patterns) -> ACAutomaton
 * ─────────────────────────────────────────────────────────────────────── */
static PyObject *fast_ahocorasick_build(PyObject *self, PyObject *args) {
    PyObject *patterns;
    if (!PyArg_ParseTuple(args, "O", &patterns)) return NULL;
    if (!PySequence_Check(patterns)) {
        PyErr_SetString(PyExc_TypeError, "patterns must be a sequence of strings");
        return NULL;
    }

    ACAutomaton *am = PyObject_New(ACAutomaton, &ACAutomatonType);
    if (!am) return NULL;
    am->node_cap = 1024;
    am->nodes = calloc(am->node_cap, sizeof(ACNode));
    if (!am->nodes) { Py_DECREF(am); return PyErr_NoMemory(); }
    am->node_count = 0;
    am->patterns = patterns; Py_INCREF(patterns);

    /* 初始化根节点 */
    for (int i = 0; i < AC_ALPHABET; i++) am->nodes[0].next[i] = -1;
    am->node_count = 1;

    Py_ssize_t n = PySequence_Length(patterns);
    for (Py_ssize_t i = 0; i < n; i++) {
        PyObject *item = PySequence_GetItem(patterns, i);
        if (!item) { Py_DECREF(am); return NULL; }
        const char *pstr = NULL; Py_ssize_t plen = 0;
        if (PyUnicode_Check(item)) {
            pstr = PyUnicode_AsUTF8AndSize(item, &plen);
        } else if (PyBytes_Check(item)) {
            PyBytes_AsStringAndSize(item, (char **)&pstr, &plen);
        }
        if (pstr && plen > 0 && plen < 256) {
            if (ac_insert_pattern(am, pstr, (int)plen) < 0) {
                Py_DECREF(item);
                Py_DECREF(am);
                return PyErr_NoMemory();
            }
        }
        Py_DECREF(item);
    }

    ac_build_failure(am);
    if (PyErr_Occurred()) {
        Py_DECREF(am);
        return NULL;
    }
    return (PyObject *)am;
}

/* ───────────────────────────────────────────────────────────────────────
 * fast_ahocorasick_search(automaton, text) -> list[str]
 * ─────────────────────────────────────────────────────────────────────── */
static PyObject *fast_ahocorasick_search(PyObject *self, PyObject *args) {
    PyObject *am_obj, *text_obj;
    if (!PyArg_ParseTuple(args, "OO", &am_obj, &text_obj)) return NULL;
    if (!PyObject_TypeCheck(am_obj, &ACAutomatonType)) {
        PyErr_SetString(PyExc_TypeError, "first arg must be an ACAutomaton");
        return NULL;
    }
    ACAutomaton *am = (ACAutomaton *)am_obj;

    const char *text; Py_ssize_t tlen;
    if (PyUnicode_Check(text_obj)) {
        text = PyUnicode_AsUTF8AndSize(text_obj, &tlen);
    } else if (PyBytes_Check(text_obj)) {
        PyBytes_AsStringAndSize(text_obj, (char **)&text, &tlen);
    } else {
        PyErr_SetString(PyExc_TypeError, "text must be str or bytes");
        return NULL;
    }
    if (!text || tlen <= 0) return PyList_New(0);

    PyObject *result = PyList_New(0);
    if (!result) return NULL;

    int node = 0;
    for (Py_ssize_t i = 0; i < tlen; i++) {
        unsigned char c = (unsigned char)text[i];
        while (node != 0 && am->nodes[node].next[c] == -1)
            node = am->nodes[node].fail;
        int next = am->nodes[node].next[c];
        if (next != -1) node = next;

        for (int j = 0; j < am->nodes[node].output_len; j++) {
            PyObject *s = PyUnicode_FromStringAndSize(
                am->nodes[node].outputs[j], am->nodes[node].output_lens[j]);
            if (s) {
                if (PyList_Append(result, s) < 0) {
                    Py_DECREF(s);
                    Py_DECREF(result);
                    return NULL;
                }
                Py_DECREF(s);
            }
        }
    }
    return result;
}

/* ───────────────────────────────────────────────────────────────────────
 * fast_char_bigrams(text) -> list[str]
 * ─────────────────────────────────────────────────────────────────────── */
static PyObject *fast_char_bigrams(PyObject *self, PyObject *arg) {
    const char *text; Py_ssize_t tlen;
    if (PyUnicode_Check(arg)) {
        text = PyUnicode_AsUTF8AndSize(arg, &tlen);
    } else if (PyBytes_Check(arg)) {
        PyBytes_AsStringAndSize(arg, (char **)&text, &tlen);
        /* V0.1.12.5: 验证 bytes 为合法 UTF-8 */
        if (text && tlen > 0 && !_is_valid_utf8(text, tlen)) {
            PyErr_SetString(PyExc_UnicodeDecodeError, "bytes is not valid UTF-8");
            return NULL;
        }
    } else {
        PyErr_SetString(PyExc_TypeError, "expected str or bytes");
        return NULL;
    }
    if (!text || tlen < 2) {
        PyObject *list = PyList_New(0);
        if (!list) return NULL;
        if (tlen == 1 && text) {
            int clen = 1;
            if ((unsigned char)text[0] >= 0xC0) {
                while (clen < 4 && (unsigned char)text[clen] >= 0x80 && (unsigned char)text[clen] < 0xC0)
                    clen++;
            }
            PyObject *bg = PyUnicode_FromStringAndSize(text, clen);
            if (bg) {
                if (PyList_Append(list, bg) < 0) {
                    Py_DECREF(bg);
                    Py_DECREF(list);
                    return NULL;
                }
                Py_DECREF(bg);
            }
        }
        return list;
    }

    PyObject *result = PyList_New(0);
    if (!result) return NULL;

    const char *p = text, *end = text + tlen;
    while (p < end) {
        /* 跳过空白 */
        if ((unsigned char)*p <= ' ') { p++; continue; }

        /* 第一个字符的字节长度 */
        int first_len = 1;
        if ((unsigned char)*p >= 0xC0) {
            while (first_len < 4 && (unsigned char)p[first_len] >= 0x80 && (unsigned char)p[first_len] < 0xC0)
                first_len++;
            if (first_len > (int)(end - p)) first_len = 1;
        }

        /* 跳过第一个字符,找到下一个非空白字符开头 */
        const char *q = p + first_len;
        while (q < end && (unsigned char)*q <= ' ') q++;
        if (q >= end) { p += first_len; continue; }

        /* 第二个字符的字节长度 */
        int second_len = 1;
        if ((unsigned char)*q >= 0xC0) {
            while (second_len < 4 && (unsigned char)q[second_len] >= 0x80 && (unsigned char)q[second_len] < 0xC0)
                second_len++;
            if (second_len > (int)(end - q)) second_len = 1;
        }

        /* p 到 q+second_len 组成一个有效 bigram (不含空白, V0.1.12.5) */
        char bigram_buf[16];  /* 最坏情况: 2 个 4 字节 UTF-8 字符 = 8 字节 */
        memcpy(bigram_buf, p, first_len);
        memcpy(bigram_buf + first_len, q, second_len);
        PyObject *bg = PyUnicode_FromStringAndSize(bigram_buf, first_len + second_len);
        if (!bg) {
            Py_DECREF(result);
            return NULL;
        }
        if (PyList_Append(result, bg) < 0) {
            Py_DECREF(bg);
            Py_DECREF(result);
            return NULL;
        }
        Py_DECREF(bg);

        p += first_len;
    }
    return result;
}

/* ───────────────────────────────────────────────────────────────────────
 * fast_cosine_similarity(vec_a, vec_b) -> float
 * ─────────────────────────────────────────────────────────────────────── */
static PyObject *fast_cosine_similarity(PyObject *self, PyObject *args) {
    PyObject *va, *vb;
    if (!PyArg_ParseTuple(args, "OO", &va, &vb)) return NULL;
    if (!PyDict_Check(va) || !PyDict_Check(vb)) {
        PyErr_SetString(PyExc_TypeError, "expected two dicts");
        return NULL;
    }

    double dot = 0.0, na = 0.0, nb = 0.0;
    PyObject *key, *value;
    Py_ssize_t pos = 0;

    /* 遍历 vec_a,累积 a 的 norm 和 dot(当 key 在 b 中时) */
    while (PyDict_Next(va, &pos, &key, &value)) {
        double va_val = PyFloat_AsDouble(value);
        if (PyErr_Occurred()) return NULL;
        na += va_val * va_val;

        PyObject *vb_val = PyDict_GetItem(vb, key);  /* borrowed ref */
        if (vb_val) {
            double vb_val_d = PyFloat_AsDouble(vb_val);
            if (PyErr_Occurred()) return NULL;
            dot += va_val * vb_val_d;
        }
    }

    /* 遍历 vec_b 累积 b 的 norm */
    pos = 0;
    while (PyDict_Next(vb, &pos, &key, &value)) {
        double val = PyFloat_AsDouble(value);
        if (PyErr_Occurred()) return NULL;
        nb += val * val;
    }

    if (na == 0.0 || nb == 0.0) return PyFloat_FromDouble(0.0);
    double sim = dot / (sqrt(na) * sqrt(nb));
    return PyFloat_FromDouble(sim < 1.0 ? sim : 1.0);
}

/* ───────────────────────────────────────────────────────────────────────
 * fast_aho_match(text, automaton) -> int  (快速判断是否有匹配)
 * ─────────────────────────────────────────────────────────────────────── */
static PyObject *fast_aho_has_match(PyObject *self, PyObject *args) {
    PyObject *am_obj, *text_obj;
    if (!PyArg_ParseTuple(args, "OO", &am_obj, &text_obj)) return NULL;
    if (!PyObject_TypeCheck(am_obj, &ACAutomatonType)) {
        PyErr_SetString(PyExc_TypeError, "first arg must be ACAutomaton");
        return NULL;
    }
    ACAutomaton *am = (ACAutomaton *)am_obj;

    const char *text; Py_ssize_t tlen;
    if (PyUnicode_Check(text_obj)) {
        text = PyUnicode_AsUTF8AndSize(text_obj, &tlen);
    } else if (PyBytes_Check(text_obj)) {
        PyBytes_AsStringAndSize(text_obj, (char **)&text, &tlen);
    } else {
        return PyBool_FromLong(0);
    }
    if (!text || tlen <= 0) return PyBool_FromLong(0);

    int node = 0;
    for (Py_ssize_t i = 0; i < tlen; i++) {
        unsigned char c = (unsigned char)text[i];
        while (node != 0 && am->nodes[node].next[c] == -1)
            node = am->nodes[node].fail;
        int next = am->nodes[node].next[c];
        if (next != -1) node = next;
        if (am->nodes[node].output_len > 0) {
            Py_RETURN_TRUE;
        }
    }
    Py_RETURN_FALSE;
}

/* ───────────────────────────────────────────────────────────────────────
 * fast_match_keywords(text, patterns_tuple) -> list[str]
 * 比 Python 版快 20-50x:一次构建自动机,一次扫描。
 * 用于 keywords.py match_keywords() 替换。
 * ─────────────────────────────────────────────────────────────────────── */
static PyObject *fast_match_keywords(PyObject *self, PyObject *args) {
    PyObject *text_obj, *patterns_obj;
    if (!PyArg_ParseTuple(args, "OO", &text_obj, &patterns_obj)) return NULL;

    const char *text; Py_ssize_t tlen;
    if (PyUnicode_Check(text_obj)) {
        text = PyUnicode_AsUTF8AndSize(text_obj, &tlen);
    } else {
        PyErr_SetString(PyExc_TypeError, "text must be str");
        return NULL;
    }
    if (!text || tlen <= 0) return PyList_New(0);

    if (!PyTuple_Check(patterns_obj) && !PyList_Check(patterns_obj)) {
        PyErr_SetString(PyExc_TypeError, "patterns must be tuple or list");
        return NULL;
    }

    Py_ssize_t np = PySequence_Length(patterns_obj);
    /* 构建自动机 */
    ACAutomaton am_local;
    memset(&am_local, 0, sizeof(am_local));
    am_local.node_cap = 1024;
    am_local.nodes = calloc(am_local.node_cap, sizeof(ACNode));
    if (!am_local.nodes) return PyErr_NoMemory();
    for (int i = 0; i < AC_ALPHABET; i++) am_local.nodes[0].next[i] = -1;
    am_local.node_count = 1;
    am_local.patterns = NULL;

    for (Py_ssize_t i = 0; i < np; i++) {
        PyObject *item = PySequence_GetItem(patterns_obj, i);
        if (!item) { free(am_local.nodes); return NULL; }
        const char *pstr = NULL; Py_ssize_t plen = 0;
        if (PyUnicode_Check(item)) {
            pstr = PyUnicode_AsUTF8AndSize(item, &plen);
        }
        if (pstr && plen > 0 && plen < 256) {
            if (ac_insert_pattern(&am_local, pstr, (int)plen) < 0) {
                Py_DECREF(item);
                for (int k = 0; k < am_local.node_count; k++)
                    for (int j = 0; j < am_local.nodes[k].output_len; j++)
                        free(am_local.nodes[k].outputs[j]);
                free(am_local.nodes);
                return PyErr_NoMemory();
            }
        }
        Py_DECREF(item);
    }
    ac_build_failure(&am_local);
    if (PyErr_Occurred()) {
        for (int i = 0; i < am_local.node_count; i++)
            for (int j = 0; j < am_local.nodes[i].output_len; j++)
                free(am_local.nodes[i].outputs[j]);
        free(am_local.nodes);
        return NULL;
    }

    PyObject *result = PyList_New(0);
    if (!result) {
        for (int i = 0; i < am_local.node_count; i++)
            for (int j = 0; j < am_local.nodes[i].output_len; j++)
                free(am_local.nodes[i].outputs[j]);
        free(am_local.nodes);
        return NULL;
    }

    int node = 0;
    for (Py_ssize_t i = 0; i < tlen; i++) {
        unsigned char c = (unsigned char)text[i];
        while (node != 0 && am_local.nodes[node].next[c] == -1)
            node = am_local.nodes[node].fail;
        int next = am_local.nodes[node].next[c];
        if (next != -1) node = next;
        for (int j = 0; j < am_local.nodes[node].output_len; j++) {
            PyObject *s = PyUnicode_FromStringAndSize(
                am_local.nodes[node].outputs[j], am_local.nodes[node].output_lens[j]);
            if (s) {
                if (PyList_Append(result, s) < 0) {
                    Py_DECREF(s);
                    Py_DECREF(result);
                    goto error_cleanup;
                }
                Py_DECREF(s);
            }
        }
    }

    /* 清理本地自动机 */
    for (int i = 0; i < am_local.node_count; i++) {
        for (int j = 0; j < am_local.nodes[i].output_len; j++) {
            free(am_local.nodes[i].outputs[j]);
        }
    }
    free(am_local.nodes);
    return result;

error_cleanup:
    for (int i = 0; i < am_local.node_count; i++) {
        for (int j = 0; j < am_local.nodes[i].output_len; j++) {
            free(am_local.nodes[i].outputs[j]);
        }
    }
    free(am_local.nodes);
    return NULL;
}

/* ───────────────────────────────────────────────────────────────────────
 * fast_meme_count(texts_list, memes_tuple) -> int
 * 统计弹幕列表中命中梗词的条数(用于 highlight.py 弹幕情绪)。
 * ─────────────────────────────────────────────────────────────────────── */
static PyObject *fast_meme_count(PyObject *self, PyObject *args) {
    PyObject *texts_obj, *memes_obj;
    if (!PyArg_ParseTuple(args, "OO", &texts_obj, &memes_obj)) return NULL;
    if (!PyList_Check(texts_obj) || !PyTuple_Check(memes_obj)) {
        PyErr_SetString(PyExc_TypeError, "expected (list, tuple)");
        return NULL;
    }

    Py_ssize_t nm = PyTuple_GET_SIZE(memes_obj);
    /* 构建梗词自动机 */
    ACAutomaton am_local;
    memset(&am_local, 0, sizeof(am_local));
    am_local.node_cap = 1024;
    am_local.nodes = calloc(am_local.node_cap, sizeof(ACNode));
    if (!am_local.nodes) return PyErr_NoMemory();
    for (int i = 0; i < AC_ALPHABET; i++) am_local.nodes[0].next[i] = -1;
    am_local.node_count = 1;

    for (Py_ssize_t i = 0; i < nm; i++) {
        PyObject *item = PyTuple_GET_ITEM(memes_obj, i);
        const char *pstr = NULL; Py_ssize_t plen = 0;
        if (PyUnicode_Check(item)) {
            pstr = PyUnicode_AsUTF8AndSize(item, &plen);
        }
        if (pstr && plen > 0) {
            if (ac_insert_pattern(&am_local, pstr, (int)plen) < 0) {
                /* 梗词 OOM: 静默跳过个别模式,构建失败则返回 0 */
                continue;
            }
        }
    }
    ac_build_failure(&am_local);
    if (PyErr_Occurred()) {
        for (int i = 0; i < am_local.node_count; i++)
            for (int j = 0; j < am_local.nodes[i].output_len; j++)
                free(am_local.nodes[i].outputs[j]);
        free(am_local.nodes);
        return NULL;
    }

    long count = 0;
    Py_ssize_t nt = PyList_GET_SIZE(texts_obj);
    for (Py_ssize_t i = 0; i < nt; i++) {
        PyObject *t = PyList_GET_ITEM(texts_obj, i);
        const char *text; Py_ssize_t tlen;
        if (!PyUnicode_Check(t)) continue;
        text = PyUnicode_AsUTF8AndSize(t, &tlen);
        if (!text || tlen <= 0) continue;

        int node = 0, found = 0;
        for (Py_ssize_t j = 0; j < tlen && !found; j++) {
            unsigned char c = (unsigned char)text[j];
            while (node != 0 && am_local.nodes[node].next[c] == -1)
                node = am_local.nodes[node].fail;
            int next = am_local.nodes[node].next[c];
            if (next != -1) node = next;
            if (am_local.nodes[node].output_len > 0) found = 1;
        }
        if (found) count++;
    }

    for (int i = 0; i < am_local.node_count; i++)
        for (int j = 0; j < am_local.nodes[i].output_len; j++)
            free(am_local.nodes[i].outputs[j]);
    free(am_local.nodes);
    return PyLong_FromLong(count);
}

/* ───────────────────────────────────────────────────────────────────────
 * 模块方法列表
 * ─────────────────────────────────────────────────────────────────────── */
static PyMethodDef speedups_methods[] = {
    {"fast_ahocorasick_build", fast_ahocorasick_build, METH_VARARGS,
     "构建 Aho-Corasick 多模式匹配自动机。\n\n:param patterns: 模式字符串列表。\n:returns: ACAutomaton 对象。"},
    {"fast_ahocorasick_search", fast_ahocorasick_search, METH_VARARGS,
     "用自动机搜索文本,返回所有命中的模式。\n\n:param automaton: ACAutomaton。\n:param text: 待搜索文本。\n:returns: 命中模式列表。"},
    {"fast_char_bigrams", fast_char_bigrams, METH_O,
     "字符级 bigram 提取(零拷贝风格)。\n\n:param text: 文本。\n:returns: bigram 字符串列表。"},
    {"fast_cosine_similarity", fast_cosine_similarity, METH_VARARGS,
     "基于 Python dict 的余弦相似度。\n\n:param vec_a: {str: float}。\n:param vec_b: {str: float}。\n:returns: 0-1 相似度。"},
    {"fast_aho_has_match", fast_aho_has_match, METH_VARARGS,
     "快速判断文本中是否有模式匹配(提前终止)。\n\n:param automaton: ACAutomaton。\n:param text: 文本。\n:returns: bool。"},
    {"fast_match_keywords", fast_match_keywords, METH_VARARGS,
     "一次构建自动机+扫描,返回命中的关键词列表。\n\n:param text: 文本。\n:param patterns: 关键词元组。\n:returns: 命中关键词列表。"},
    {"fast_meme_count", fast_meme_count, METH_VARARGS,
     "统计弹幕列表中命中梗词的条数。\n\n:param texts: 弹幕文本列表。\n:param memes: 梗词元组。\n:returns: 命中条数。"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef speedups_module = {
    PyModuleDef_HEAD_INIT,
    "_c_speedups",
    "BiliLiveCut C 加速模块 — Aho-Corasick + 文本相似度",
    -1,
    speedups_methods,
};

PyMODINIT_FUNC PyInit__c_speedups(void) {
    PyObject *m;
    if (PyType_Ready(&ACAutomatonType) < 0) return NULL;
    m = PyModule_Create(&speedups_module);
    if (m == NULL) return NULL;
    Py_INCREF(&ACAutomatonType);
    PyModule_AddObject(m, "ACAutomaton", (PyObject *)&ACAutomatonType);
    return m;
}
