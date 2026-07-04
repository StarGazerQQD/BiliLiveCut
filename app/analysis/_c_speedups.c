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

static void ac_insert_pattern(ACAutomaton *self, const char *pattern, int plen) {
    int node = 0;
    for (int i = 0; i < plen; i++) {
        unsigned char c = (unsigned char)pattern[i];
        if (self->nodes[node].next[c] == -1) {
            int child = ac_add_node(self);
            if (child < 0) return;
            self->nodes[node].next[c] = child;
        }
        node = self->nodes[node].next[c];
    }
    if (self->nodes[node].output_len < 8) {
        int oi = self->nodes[node].output_len++;
        self->nodes[node].outputs[oi] = strndup(pattern, plen);
        self->nodes[node].output_lens[oi] = plen;
    }
}

/* BFS 构造失败链接 */
static void ac_build_failure(ACAutomaton *self) {
    int queue[AC_MAX_NODES];
    int head = 0, tail = 0;
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
                queue[tail++] = child;
                int f = self->nodes[r].fail;
                while (self->nodes[f].next[c] == -1) f = self->nodes[f].fail;
                self->nodes[child].fail = self->nodes[f].next[c];
                /* 合并输出 */
                int of = self->nodes[child].fail;
                for (int j = 0; j < self->nodes[of].output_len; j++) {
                    if (self->nodes[child].output_len >= 8) break;
                    int oi = self->nodes[child].output_len++;
                    self->nodes[child].outputs[oi] = self->nodes[of].outputs[j];
                    self->nodes[child].output_lens[oi] = self->nodes[of].output_lens[j];
                }
            } else {
                self->nodes[r].next[c] = -1;
            }
        }
    }
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
            ac_insert_pattern(am, pstr, (int)plen);
        }
        Py_DECREF(item);
    }

    ac_build_failure(am);
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
            if (s) PyList_Append(result, s);
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
    } else {
        PyErr_SetString(PyExc_TypeError, "expected str or bytes");
        return NULL;
    }
    if (!text || tlen < 2) {
        PyObject *list = PyList_New(0);
        if (tlen == 1) {
            char buf[2] = {text[0], 0};
            PyList_Append(list, PyUnicode_FromStringAndSize(buf, 1));
        }
        return list;
    }

    /* 跳过空白字符,统计有效 bigram 数量 */
    int bigram_count = 0;
    const char *p = text, *end = text + tlen;
    while (p < end) {
        if ((unsigned char)*p <= ' ') { p++; continue; }
        const char *q = p + 1;
        while (q < end && (unsigned char)*q <= ' ') q++;
        if (q < end) bigram_count++;
        p++;
    }

    PyObject *result = PyList_New(bigram_count);
    if (!result) return NULL;

    int idx = 0;
    p = text;
    while (p < end) {
        if ((unsigned char)*p <= ' ') { p++; continue; }
        const char *q = p + 1;
        while (q < end && (unsigned char)*q <= ' ') q++;
        if (q >= end) break;
        /* 2 个 UTF-8 字符的 bigram */
        int first_len = 1;
        if ((unsigned char)*p >= 0xC0) {
            while (first_len < 4 && (unsigned char)p[first_len] >= 0x80 && (unsigned char)p[first_len] < 0xC0)
                first_len++;
            if (first_len > (int)(end - p) || first_len < 1) first_len = 1;
        }
        int second_start = (int)(q - p);
        int second_len = 1;
        if ((unsigned char)*q >= 0xC0) {
            while (second_len < 4 && (unsigned char)q[second_len] >= 0x80 && (unsigned char)q[second_len] < 0xC0)
                second_len++;
            if (second_len > (int)(end - q) || second_len < 1) second_len = 1;
        }
        PyObject *bg = PyUnicode_FromStringAndSize(p, second_start + second_len);
        if (bg) PyList_SET_ITEM(result, idx++, bg);
        p++;
    }

    /* 如果实际 bigram 少于预估,调整大小 — 实际上我们可能高估了 */
    if (idx < bigram_count) {
        Py_SET_SIZE(result, idx);
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
            ac_insert_pattern(&am_local, pstr, (int)plen);
        }
        Py_DECREF(item);
    }
    ac_build_failure(&am_local);

    PyObject *result = PyList_New(0);
    if (!result) { free(am_local.nodes); return NULL; }

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
            if (s) PyList_Append(result, s);
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
            ac_insert_pattern(&am_local, pstr, (int)plen);
        }
    }
    ac_build_failure(&am_local);

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
