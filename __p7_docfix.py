import os, re

# Fix specific files with damaged docstrings
fixes = {
    r"D:\Vibe\BiliLiveCut\app\analysis\_speedups_py.py": (
        '"""纯 Python 回退 — Aho-Corasick + 文本相似度 (兼容门面).',
        '"""纯 Python 回退 — Aho-Corasick + 文本相似度 (兼容门面)。"""'
    ),
    r"D:\Vibe\BiliLiveCut\app\analysis\_speedups_round2_py.py": (
        '"""纯 Python 回退 — 第二轮加速 (兼容门面).',
        '"""纯 Python 回退 — 第二轮加速 (兼容门面)。"""'
    ),
    r"D:\Vibe\BiliLiveCut\app\analysis\speedups.py": (
        '"""BiliLiveCut 加速模块分派层 (兼容门面).',
        '"""BiliLiveCut 加速模块分派层 (兼容门面)。'
    ),
    r"D:\Vibe\BiliLiveCut\app\db\migrate.py": (
        '"""数据库管理命令。',
        '"""数据库管理命令。'
    ),
    r"D:\Vibe\BiliLiveCut\app\pipeline\orchestrator.py": (
        '"""片段处理编排。状态机重构):',
        '"""片段处理编排 (状态机重构):'
    ),
}

for fp, (old, new) in fixes.items():
    with open(fp, encoding="utf-8") as f:
        content = f.read()
    content = content.replace(old, new, 1)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"Fixed {os.path.basename(fp)}")

print("Done")
