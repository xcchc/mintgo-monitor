"""轻量 YAML 读取（不引入 pyyaml 依赖）。

只支持本项目 config.yaml 用到的子集：顶层 key: value、一层嵌套 map、以及
"- item" 形式的字符串列表。够用即可，遇到不支持的结构会抛错而不是静默忽略。
"""
from __future__ import annotations

import re
from pathlib import Path


def _strip_inline_comment(s: str) -> str:
    """去掉行内注释。只在 # 前面是空白、且不在引号内时才截断。"""
    in_double = in_single = False
    for i, ch in enumerate(s):
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == "#" and not in_double and not in_single:
            if i == 0 or s[i - 1] in " \t":
                return s[:i].rstrip()
    return s


def _parse_scalar(raw: str):
    s = _strip_inline_comment(raw.strip())
    if not s:
        return None
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        # 只还原项目里会用到的转义，避免踩 \\ 的处理差异
        return s[1:-1].replace("\\\\", "\\").replace('\\"', '"')
    if s.startswith("'") and s.endswith("'") and len(s) >= 2:
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d*\.\d+", s):
        return float(s)
    return s


def load(path: str | Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    root: dict = {}

    cur: dict = root
    cur_indent = -1
    pending: tuple[dict, str, int] | None = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        line = stripped

        # ---- 列表项 ----
        if line.startswith("- "):
            if pending is None:
                raise ValueError(f"列表项没有归属的 key: {raw_line!r}")
            parent, key, _ = pending
            if not isinstance(parent.get(key), list):
                parent[key] = []
            parent[key].append(_parse_scalar(line[2:]))
            continue

        if ":" not in line:
            raise ValueError(f"无法解析的行: {raw_line!r}")

        # 值待定的 key 下面出现缩进内容 —— 它是嵌套 map
        if pending is not None and indent > pending[2]:
            cur, cur_indent = pending[0][pending[1]], pending[2]
            pending = None

        # 缩进回退，目前只支持一层嵌套，直接退回根
        if cur_indent >= 0 and indent <= cur_indent:
            cur, cur_indent = root, -1

        key, _, value = line.partition(":")
        key = key.strip()
        value = _strip_inline_comment(value.strip())

        if value == "":
            pending = (cur, key, indent)
            cur.setdefault(key, {})
        else:
            cur[key] = _parse_scalar(value)
            pending = None

    return root
