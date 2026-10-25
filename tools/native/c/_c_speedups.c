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
#include <limits.h>
#include <string.h>
#include <math.h>
#include <wchar.h>

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
    char *text;
    Py_ssize_t length;
} ACOutput;

typedef struct {
    int next[AC_ALPHABET];   /* 子节点索引, -1 表示无 */
    int fail;                /* 失败链接 */
    int output_len;          /* 输出模式数量 */
    int output_cap;
    ACOutput *outputs;
} ACNode;

typedef struct {
    PyObject_HEAD
    ACNode *nodes;
    int node_count;
    int node_cap;
    PyObject *patterns;      /* 原始模式列表(保持引用防止GC) */
} ACAutomaton;

static void ac_clear(ACAutomaton *self) {
    if (self->nodes) {
        for (int i = 0; i < self->node_count; i++) {
            for (int j = 0; j < self->nodes[i].output_len; j++) {
                free(self->nodes[i].outputs[j].text);
            }
            free(self->nodes[i].outputs);
        }
        free(self->nodes);
        self->nodes = NULL;
    }
}

static void ac_automaton_dealloc(ACAutomaton *self) {
    ac_clear(self);
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
        if (self->node_cap > INT_MAX / 2 || (size_t)self->node_cap > SIZE_MAX / (2 * sizeof(ACNode))) {
            PyErr_NoMemory();
            return -1;
        }
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

static int ac_add_output(ACNode *node, const char *pattern, Py_ssize_t length) {
    if (node->output_len == node->output_cap) {
        if (node->output_cap > INT_MAX / 2 || (size_t)node->output_cap > SIZE_MAX / (2 * sizeof(ACOutput))) {
            PyErr_NoMemory();
            return -1;
        }
        int capacity = node->output_cap ? node->output_cap * 2 : 4;
        ACOutput *outputs = realloc(node->outputs, (size_t)capacity * sizeof(ACOutput));
        if (!outputs) { PyErr_NoMemory(); return -1; }
        node->outputs = outputs;
        node->output_cap = capacity;
    }
    /* 使用显式长度复制，保留内嵌 NUL，禁止按 C 字符串截断。 */
    char *copy = malloc((size_t)length + 1);
    if (!copy) { PyErr_NoMemory(); return -1; }
    memcpy(copy, pattern, (size_t)length);
    copy[length] = '\0';
    node->outputs[node->output_len++] = (ACOutput){copy, length};
    return 0;
}

static int ac_insert_pattern(ACAutomaton *self, const char *pattern, Py_ssize_t plen) {
    int node = 0;
    for (Py_ssize_t i = 0; i < plen; i++) {
        unsigned char c = (unsigned char)pattern[i];
        if (self->nodes[node].next[c] == -1) {
            int child = ac_add_node(self);
            if (child < 0) return -1;  /* OOM: 通知调用方 */
            self->nodes[node].next[c] = child;
        }
        node = self->nodes[node].next[c];
    }
    return ac_add_output(&self->nodes[node], pattern, plen);
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
                /* 保留全部后缀命中及原遍历顺序，各节点独立持有输出。 */
                int of = self->nodes[child].fail;
                for (int j = 0; j < self->nodes[of].output_len; j++) {
                    ACOutput output = self->nodes[of].outputs[j];
                    if (ac_add_output(&self->nodes[child], output.text, output.length) < 0) {
                        free(queue);
                        return;
                    }
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
    am->patterns = NULL;
    am->node_count = 0;
    am->node_cap = 16;
    am->nodes = calloc(am->node_cap, sizeof(ACNode));
    if (!am->nodes) { Py_DECREF(am); return PyErr_NoMemory(); }
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
        if (PyErr_Occurred()) { Py_DECREF(item); Py_DECREF(am); return NULL; }
        if (pstr && plen > 0) {
            if (ac_insert_pattern(am, pstr, plen) < 0) {
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
    if (!text) return NULL;
    if (tlen <= 0) return PyList_New(0);

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
                am->nodes[node].outputs[j].text, am->nodes[node].outputs[j].length);
            if (!s) { Py_DECREF(result); return NULL; }
            {
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
    PyObject *text;
    if (PyUnicode_Check(arg)) {
        text = Py_NewRef(arg);
    } else if (PyBytes_Check(arg)) {
        /* 验证 bytes 为合法 UTF-8；Unicode 输入按码点处理，包含孤立代理码点。 */
        text = PyUnicode_DecodeUTF8(PyBytes_AS_STRING(arg), PyBytes_GET_SIZE(arg), "strict");
        if (!text) return NULL;
    } else {
        PyErr_SetString(PyExc_TypeError, "expected str or bytes");
        return NULL;
    }
    PyObject *result = PyList_New(0);
    if (!result) { Py_DECREF(text); return NULL; }
    Py_ssize_t size = PyUnicode_GET_LENGTH(text);
    if (size < 2) {
        if (size == 1 && PyList_Append(result, text) < 0) goto error;
        Py_DECREF(text);
        return result;
    }
    int kind = PyUnicode_KIND(text);
    void *data = PyUnicode_DATA(text);
    Py_UCS4 previous = 0x110000;
    for (Py_ssize_t index = 0; index < size; index++) {
        Py_UCS4 current = PyUnicode_READ(kind, data, index);
        /* 跳过 ASCII 空白，与 Python 参考实现逐码点比较一致。 */
        if (current <= ' ') continue;
        if (previous != 0x110000) {
            PyObject *pair = PyUnicode_New(2, previous > current ? previous : current);
            if (!pair) goto error;
            int pair_kind = PyUnicode_KIND(pair);
            void *pair_data = PyUnicode_DATA(pair);
            PyUnicode_WRITE(pair_kind, pair_data, 0, previous);
            PyUnicode_WRITE(pair_kind, pair_data, 1, current);
            int status = PyList_Append(result, pair);
            Py_DECREF(pair);
            if (status < 0) goto error;
        }
        previous = current;
    }
    if (PyList_GET_SIZE(result) == 0 && previous != 0x110000) {
        PyObject *single = PyUnicode_FromOrdinal(previous);
        if (!single) goto error;
        int status = PyList_Append(result, single);
        Py_DECREF(single);
        if (status < 0) goto error;
    }
    Py_DECREF(text);
    return result;
error:
    Py_DECREF(text);
    Py_DECREF(result);
    return NULL;
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
    if (!text) return NULL;
    if (tlen <= 0) return PyBool_FromLong(0);

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
    if (!text) return NULL;
    if (tlen <= 0) return PyList_New(0);

    if (!PyTuple_Check(patterns_obj) && !PyList_Check(patterns_obj)) {
        PyErr_SetString(PyExc_TypeError, "patterns must be tuple or list");
        return NULL;
    }

    Py_ssize_t np = PySequence_Length(patterns_obj);
    /* 构建自动机 */
    ACAutomaton am_local;
    memset(&am_local, 0, sizeof(am_local));
    am_local.node_cap = 16;
    am_local.nodes = calloc(am_local.node_cap, sizeof(ACNode));
    if (!am_local.nodes) return PyErr_NoMemory();
    for (int i = 0; i < AC_ALPHABET; i++) am_local.nodes[0].next[i] = -1;
    am_local.node_count = 1;
    am_local.patterns = NULL;

    for (Py_ssize_t i = 0; i < np; i++) {
        PyObject *item = PySequence_GetItem(patterns_obj, i);
        if (!item) { ac_clear(&am_local); return NULL; }
        const char *pstr = NULL; Py_ssize_t plen = 0;
        if (PyUnicode_Check(item)) {
            pstr = PyUnicode_AsUTF8AndSize(item, &plen);
        }
        if (PyErr_Occurred()) { Py_DECREF(item); ac_clear(&am_local); return NULL; }
        if (pstr && plen > 0) {
            if (ac_insert_pattern(&am_local, pstr, plen) < 0) {
                Py_DECREF(item);
                ac_clear(&am_local);
                return PyErr_NoMemory();
            }
        }
        Py_DECREF(item);
    }
    ac_build_failure(&am_local);
    if (PyErr_Occurred()) {
        ac_clear(&am_local);
        return NULL;
    }

    PyObject *result = PyList_New(0);
    if (!result) {
        ac_clear(&am_local);
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
                am_local.nodes[node].outputs[j].text, am_local.nodes[node].outputs[j].length);
            if (!s) { Py_DECREF(result); goto error_cleanup; }
            {
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
    ac_clear(&am_local);
    return result;

error_cleanup:
    ac_clear(&am_local);
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
    am_local.node_cap = 16;
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
        if (PyErr_Occurred()) { ac_clear(&am_local); return NULL; }
        if (pstr && plen > 0 && ac_insert_pattern(&am_local, pstr, plen) < 0) {
            ac_clear(&am_local);
            return PyErr_NoMemory();
        }
    }
    ac_build_failure(&am_local);
    if (PyErr_Occurred()) {
        ac_clear(&am_local);
        return NULL;
    }

    long count = 0;
    Py_ssize_t nt = PyList_GET_SIZE(texts_obj);
    for (Py_ssize_t i = 0; i < nt; i++) {
        PyObject *t = PyList_GET_ITEM(texts_obj, i);
        const char *text; Py_ssize_t tlen;
        if (!PyUnicode_Check(t)) continue;
        text = PyUnicode_AsUTF8AndSize(t, &tlen);
        if (!text) { ac_clear(&am_local); return NULL; }
        if (tlen <= 0) continue;

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

    ac_clear(&am_local);
    return PyLong_FromLong(count);
}

/* ───────────────────────────────────────────────────────────────────────
 * 模块方法列表
 * ─────────────────────────────────────────────────────────────────────── */
static PyObject *danmaku_sentiment_statistics(PyObject *self, PyObject *args) {
    PyObject *texts_obj, *automaton_obj;
    if (!PyArg_ParseTuple(args, "OO", &texts_obj, &automaton_obj)) return NULL;
    if (!PyList_Check(texts_obj) || !PyObject_TypeCheck(automaton_obj, &ACAutomatonType)) {
        PyErr_SetString(PyExc_TypeError, "expected (list[str], ACAutomaton)");
        return NULL;
    }
    /* 固定元素引用，避免字符串子类的哈希回调改变调用方列表。 */
    PyObject *texts = PySequence_Tuple(texts_obj);
    if (!texts) return NULL;
    PyObject *counts = PyDict_New();
    if (!counts) { Py_DECREF(texts); return NULL; }
    ACAutomaton *am = (ACAutomaton *)automaton_obj;
    Py_ssize_t max_dup = 0, exclamations = 0, hits = 0;
    for (Py_ssize_t i = 0; i < PyTuple_GET_SIZE(texts); i++) {
        PyObject *item = PyTuple_GET_ITEM(texts, i);
        if (!PyUnicode_Check(item)) {
            PyErr_SetString(PyExc_TypeError, "texts must contain str");
            goto sentiment_error;
        }
        PyObject *previous = PyDict_GetItemWithError(counts, item);
        if (!previous && PyErr_Occurred()) goto sentiment_error;
        Py_ssize_t count = previous ? PyLong_AsSsize_t(previous) + 1 : 1;
        if (count > max_dup) max_dup = count;
        PyObject *updated = PyLong_FromSsize_t(count);
        if (!updated) goto sentiment_error;
        int result = PyDict_SetItem(counts, item, updated);
        Py_DECREF(updated);
        if (result < 0) goto sentiment_error;
        Py_ssize_t size = PyUnicode_GET_LENGTH(item);
        Py_ssize_t bang = PyUnicode_FindChar(item, '!', 0, size, 1);
        if (bang == -2) goto sentiment_error;
        if (bang < 0) bang = PyUnicode_FindChar(item, 0xff01, 0, size, 1);
        if (bang == -2) goto sentiment_error;
        if (bang >= 0) exclamations++;
        Py_ssize_t length;
        const char *text = PyUnicode_AsUTF8AndSize(item, &length);
        if (!text) goto sentiment_error;
        int node = 0;
        for (Py_ssize_t j = 0; j < length; j++) {
            unsigned char c = (unsigned char)text[j];
            while (node != 0 && am->nodes[node].next[c] == -1) node = am->nodes[node].fail;
            int next = am->nodes[node].next[c];
            if (next != -1) node = next;
            if (am->nodes[node].output_len > 0) { hits++; break; }
        }
    }
    Py_DECREF(counts);
    Py_DECREF(texts);
    return Py_BuildValue("(nnn)", max_dup, exclamations, hits);
sentiment_error:
    Py_DECREF(counts);
    Py_DECREF(texts);
    return NULL;
}

static PyMethodDef speedups_methods[] = {
    {"danmaku_sentiment_statistics", danmaku_sentiment_statistics, METH_VARARGS,
     "一次汇总最大重复次数、感叹号条数和梗命中条数。"},
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
