#!/usr/bin/env python3
"""Mechanical EN<->CN consistency checks for the translated book.

Reads the extracted English markdown (review/en/*.md, produced by
extract_epub.py) and the Chinese markdown under cn-book/, then writes
review/structure.md with every structural discrepancy it can find.

The checks are deliberately mechanical: they catch omissions, numbering
breaks, broken image references, untranslated leftovers and typography slips.
Semantic accuracy is handled separately by the per-chapter review agents.

Usage:
    python3 tools/check_structure.py [--review DIR]
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
from collections import Counter, defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
NUM_PREFIX = re.compile(r"^\s*(\d+(?:\.\d+)+)\s")
IMG = re.compile(r"!\[[^\]]*\]\(\.?/?images/([^)\s]+\.(?:png|jpg|jpeg|gif|svg))\)", re.I)
SEC_NUM = re.compile(r"(?<![\d.])(\d+(?:\.\d+)+)(?![\d.])")

EN_CAPTION = re.compile(r"^#{1,6}\s+(Figure|Table|Listing)\s+(\d+\.\d+)\b")
CN_CAPTION = re.compile(
    r"^\s*(?:#{1,6}\s+|>\s*)?\*{0,2}\s*"
    r"(?:代码)?\s*(图|表|清单|列表|示例|Listing|Figure|Table)\s*(\d+\.\d+)\b")
CN_CAPTION_INLINE = re.compile(
    r"\*{0,2}(?:代码|示例)?\s*(图|表|清单)\*{0,2}\s*(\d+\.\d+)")

BOLD_HEADING = re.compile(r"^\*\*(.{2,60})\*\*\s*$")
BOLD_PSEUDO_SEC = re.compile(r"^\*\*\s*((?:练习|示例|图|表|代码清单|清单)\s*[\dA-Z][^\*]{0,50})\*\*\s*$")

CROSSREF_EN = [
    ("chapter", re.compile(r"\b[Cc]hapter\s+(\d+)\b")),
    ("listing", re.compile(r"\b[Ll]isting\s+(\d+\.\d+)\b")),
    ("figure", re.compile(r"\b[Ff]igure\s+(\d+\.\d+)\b")),
    ("table", re.compile(r"\b[Tt]able\s+(\d+\.\d+)\b")),
]
CROSSREF_CN = [
    ("chapter", re.compile(r"第\s*(\d+)\s*章")),
    ("listing", re.compile(r"清单\s*(\d+\.\d+)")),
    ("figure", re.compile(r"图\s*(\d+\.\d+)")),
    ("table", re.compile(r"表\s*(\d+\.\d+)")),
]

# English term -> candidate Chinese renderings (regex, so variants are counted)
TERMS = {
    "agent": ["智能体", "代理", "智能代理", "Agent"],
    "agentic": ["智能体化", "代理式", "智能体的", "Agentic"],
    "orchestration": ["编排", "编配", "协调", "Orchestration"],
    "guardrail": ["护栏", "防护栏", "安全护栏", "Guardrail"],
    "handoff": ["交接", "移交", "转交", "Handoff"],
    "grounding": ["事实校验", "落地", "接地", "基础化", "Grounding"],
    "prompt": ["提示词", "提示", "Prompt"],
    "context": ["上下文", "语境"],
    "evaluation": ["评估", "评测", "评价"],
    "reasoning": ["推理", "Reasoning"],
    "token": ["令牌", "词元", "Token"],
    "embedding": ["嵌入", "向量化", "Embedding"],
    "tool": ["工具", "Tool"],
    "memory": ["记忆", "Memory"],
    "metacognition": ["元认知", "Metacognition"],
    "termination": ["终止", "结束", "Termination"],
    "stagnation": ["停滞", "僵滞", "Stagnation"],
    "checkpoint": ["检查点", "检查站", "Checkpoint"],
    "listing(caption)": ["代码清单", "清单", "列表", "Listing", "示例"],
}

TYPO_PATTERNS = [
    ("重复句号", re.compile(r"。{2,}")),
    ("重复逗号", re.compile(r"，{2,}")),
    ("重复冒号", re.compile(r"：{2,}")),
    ("重复分号", re.compile(r"；{2,}")),
    ("重复问号/叹号", re.compile(r"[？！]{2,}")),
    ("半角逗号(中文语境)", re.compile(r"[\u4e00-\u9fff],[\u4e00-\u9fff]")),
    ("半角句号(中文语境)", re.compile(r"[\u4e00-\u9fff]\.[\u4e00-\u9fff]")),
    ("半角分号(中文语境)", re.compile(r"[\u4e00-\u9fff];")),
    ("半角冒号(中文语境)", re.compile(r"[\u4e00-\u9fff]:")),
    ("半角问号/叹号(中文语境)", re.compile(r"[\u4e00-\u9fff][?!]")),
    ("中英文之间缺空格", re.compile(r"[\u4e00-\u9fff][A-Za-z0-9]|[A-Za-z0-9][\u4e00-\u9fff]")),
    ("孤立右括号", re.compile(r"[\u4e00-\u9fff]\)")),
    ("括号不匹配", re.compile(r"（[^）]*$")),
]

MAX_ITEMS = 40


def split_code(text: str):
    """Return (prose_lines_with_lineno, code_blocks) with fenced code removed."""
    prose = []
    blocks = []
    in_fence = False
    cur: list[str] | None = None
    start = 0
    for i, line in enumerate(text.splitlines(), start=1):
        m = FENCE.match(line)
        if m:
            if not in_fence:
                in_fence = True
                cur = []
                start = i
            else:
                in_fence = False
                blocks.append((start, "".join(f"{b}\n" for b in cur or [])))
                cur = None
            continue
        if in_fence and cur is not None:
            cur.append(line)
        else:
            prose.append((i, line))
    return prose, blocks


def headings(text: str):
    """Headings outside code blocks, including bold pseudo-headings."""
    prose, _ = split_code(text)
    out = []
    for lineno, line in prose:
        m = HEADING.match(line)
        if m:
            out.append((lineno, len(m.group(1)), m.group(2).strip()))
            continue
        m = BOLD_HEADING.match(line)
        if m and NUM_PREFIX.match(m.group(1)):
            out.append((lineno, 0, m.group(1).strip()))
    return out


def section_numbers(text: str):
    """Section numbers (x.y / x.y.z) that head a block, in document order."""
    nums = []
    seen = set()
    for lineno, level, text_h in headings(text):
        m = NUM_PREFIX.match(text_h)
        if m:
            n = m.group(1)
            if n not in seen:
                seen.add(n)
                nums.append((n, lineno, text_h[:60]))
    return nums


def captions(text: str, is_en: bool):
    prose, _ = split_code(text)
    out = []
    for lineno, line in prose:
        if is_en:
            m = EN_CAPTION.match(line)
            if m:
                out.append((m.group(1).lower(), m.group(2), lineno))
        else:
            m = CN_CAPTION.match(line)
            if m:
                kind = m.group(1)
                if kind in ("图", "Figure"):
                    kind = "figure"
                elif kind in ("表", "Table"):
                    kind = "table"
                else:
                    kind = "listing"
                out.append((kind, m.group(2), lineno))
    return out


SEP_ROW = re.compile(r"^\|[\s\-:|]+\|$")


def _plain(row: str) -> str:
    return row.strip().strip("|").replace("**", "").replace("|", " / ").strip()


def table_shapes(text: str):
    """[(data_rows, columns, header_row), ...]; separator rows are excluded
    from the row count but keep a table in one piece."""
    prose, _ = split_code(text)
    groups, cur = [], []
    for _, line in prose:
        s = line.strip()
        if s.startswith("|") and s.endswith("|"):
            cur.append(s)
        else:
            if cur:
                groups.append(cur)
                cur = []
    if cur:
        groups.append(cur)
    shapes = []
    for g in groups:
        rows = [r for r in g if not SEP_ROW.match(r)]
        header = rows[0] if rows else g[0]
        shapes.append((len(rows), max(r.count("|") - 1 for r in g), header))
    return shapes


def list_items(text: str):
    prose, _ = split_code(text)
    n = 0
    for _, line in prose:
        s = line.strip()
        if re.match(r"^[-*+]\s+\S", s) or re.match(r"^\d+\.\s+\S", s):
            n += 1
    return n


def crossrefs(text: str, is_en: bool):
    prose, _ = split_code(text)
    body = "\n".join(l for _, l in prose)
    res = {}
    for name, pat in (CROSSREF_EN if is_en else CROSSREF_CN):
        res[name] = Counter(pat.findall(body))
    return res


def leftovers(text: str, min_words: int = 8):
    """Prose lines that are still essentially English."""
    prose, _ = split_code(text)
    out = []
    for lineno, line in prose:
        s = line.strip()
        if not s or s.startswith(("#", ">", "|", "!")):
            continue
        if re.search(r"[\u4e00-\u9fff]", s):
            continue
        if len(re.findall(r"[A-Za-z][A-Za-z\-']+", s)) < min_words:
            continue
        out.append((lineno, s))
    return out


def term_usage(text: str):
    counts = {}
    for term, variants in TERMS.items():
        counts[term] = {v: text.count(v) for v in variants}
    return counts


INLINE_CODE = re.compile(r"`[^`]*`")


def typo_hits(text: str):
    prose, _ = split_code(text)
    hits = defaultdict(list)
    for lineno, line in prose:
        if line.lstrip().startswith("|"):
            continue
        # inline code follows code conventions, not Chinese punctuation rules
        line = INLINE_CODE.sub("``", line)
        # "领域.角色.版本" mirrors the dotted identifier it documents, so a
        # half-width dot is intentional on lines that carry one
        dotted_id = re.search(r"\b\w+(?:\.\w+){2,}", line) is not None
        for name, pat in TYPO_PATTERNS:
            if name == "半角句号(中文语境)" and dotted_id:
                continue
            for m in pat.finditer(line):
                hits[name].append((lineno, m.group(0)))
    return hits


def images(text: str):
    return [m.group(1) for m in IMG.finditer(text)]


def diff_nums(en_nums, cn_nums):
    en_seq = [n for n, _, _ in en_nums]
    cn_seq = [n for n, _, _ in cn_nums]
    sm = difflib.SequenceMatcher(a=en_seq, b=cn_seq)
    only_en, only_cn = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace"):
            only_en.extend(en_nums[i1:i2])
        if tag in ("insert", "replace"):
            only_cn.extend(cn_nums[j1:j2])
    return only_en, only_cn


def check_pair(en_text: str, cn_text: str, cn_rel: str, repo: str):
    issues: list[tuple[str, str, str]] = []  # (severity, category, detail)
    add = lambda sev, cat, det: issues.append((sev, cat, det))

    # 1. section numbering
    en_nums = section_numbers(en_text)
    cn_nums = section_numbers(cn_text)
    only_en, only_cn = diff_nums(en_nums, cn_nums)
    for n, ln, t in only_en:
        add("严重", "小节缺失", f"英文 `{n}` ({t}) 在中文中无对应小节 (en line {ln})")
    for n, ln, t in only_cn:
        # the epub sometimes merges an h4 into a code comment, so the number
        # can exist in the English text without being a heading of its own
        if n not in en_text:
            add("中等", "小节多余", f"中文出现英文没有的小节 `{n}` ({t}) (cn line {ln})")
    # numbering continuity
    for nums, label in ((en_nums, "英文"), (cn_nums, "中文")):
        prev = None
        for n, ln, _ in nums:
            if prev and not _is_next(prev, n):
                add("轻微", "编号不连续", f"{label} 小节编号 {prev} -> {n} (line {ln})")
            prev = n

    # 2. figures / tables / listings captions
    en_cap = Counter((k, v) for k, v, _ in captions(en_text, True))
    cn_cap = Counter((k, v) for k, v, _ in captions(cn_text, False))
    for (kind, num), cnt in sorted(en_cap.items()):
        if (kind, num) not in cn_cap:
            add("严重", "图表/清单缺失",
                f"英文 {kind} {num} 在中文中找不到对应图注/表题/清单标题")
    for (kind, num), cnt in sorted(cn_cap.items()):
        if (kind, num) not in en_cap:
            add("中等", "图表/清单多余", f"中文 {kind} {num} 在英文原文中不存在")

    # 2b. English caption keywords left untranslated in Chinese headings
    for lineno, level, text_h in headings(cn_text):
        m = re.match(r"\s*(Listing|Figure|Table|Exercise|Exercises|Summary|Chapter)\b", text_h)
        if m:
            add("严重", "标题未翻译",
                f"cn:{lineno} 标题残留英文 `{m.group(1)}` -> {text_h[:60]}")

    # 3. images
    en_imgs, cn_imgs = images(en_text), images(cn_text)
    for name in set(cn_imgs):
        if not os.path.exists(os.path.join(repo, "images", name)):
            add("严重", "图片缺失", f"中文引用 images/{name}，但文件不存在")
    only_en_img = [i for i in en_imgs if i not in cn_imgs]
    only_en_real = [i for i in only_en_img if not i.lower().startswith("emoji")]
    if only_en_real:
        add("严重", "图片未引用", f"英文有但中文未引用: {only_en_real[:8]}")
    if len(only_en_img) != len(only_en_real):
        add("轻微", "图片未引用",
            f"{len(only_en_img) - len(only_en_real)} 张装饰性 Emoji 图标在中文中未引用（通常可忽略）")

    # 4. tables
    en_t, cn_t = table_shapes(en_text), table_shapes(cn_text)
    used = [False] * len(cn_t)
    for rows, cols, header in en_t:
        best, best_d = None, None
        for j, (r, c, _) in enumerate(cn_t):
            if used[j]:
                continue
            d = abs(r - rows) * 3 + abs(c - cols) * 100
            if best_d is None or d < best_d:
                best, best_d = j, d
        if best is None or best_d >= 100:
            add("严重", "表格缺失",
                f"英文表格「{_plain(header)[:50]}」在中文中找不到形状匹配的表格")
            continue
        used[best] = True
        r, c, _ = cn_t[best]
        if (r, c) != (rows, cols):
            add("中等", "表格形状",
                f"表「{_plain(header)[:40]}」: 英文 {rows}行x{cols}列 vs 中文 {r}行x{c}列")
    extra = [t for j, t in enumerate(cn_t) if not used[j]]
    if extra:
        add("轻微", "表格增补",
            f"中文有 {len(extra)} 个英文中没有的表格（可能为译者补充）："
            + "; ".join(_plain(h)[:30] for _, _, h in extra[:3]))

    # 5. code blocks
    _, en_code = split_code(en_text)
    _, cn_code = split_code(cn_text)
    # Chinese routinely splits one English block into several (output samples,
    # continued listings), so compare total code lines rather than block count
    en_lines = sum(len(c.strip().splitlines()) for _, c in en_code)
    cn_lines = sum(len(c.strip().splitlines()) for _, c in cn_code)
    if en_lines and cn_lines / en_lines < 0.85:
        add("中等", "代码行数偏少",
            f"英文 {en_lines} 行代码 vs 中文 {cn_lines} 行（少 "
            f"{100 * (1 - cn_lines / en_lines):.0f}%，需确认不是漏译）")
    missing_ids = sorted(_python_ids(en_text) - _python_ids(cn_text))
    if missing_ids:
        add("中等", "代码标识符改动",
            f"{len(missing_ids)} 个英文代码标识符在中文代码中找不到 -> {missing_ids[:15]}")

    # 6. list items (soft signal only)
    en_li, cn_li = list_items(en_text), list_items(cn_text)
    if abs(en_li - cn_li) > max(8, 0.40 * en_li):
        add("轻微", "列表项数量", f"英文 {en_li} 项 vs 中文 {cn_li} 项（中文常拆条，仅供参考）")

    # 6b. bold pseudo-headings used where a real heading is expected
    prose, _ = split_code(cn_text)
    for lineno, line in prose:
        m = BOLD_PSEUDO_SEC.match(line)
        if m:
            add("轻微", "标题层级", f"cn:{lineno} 用加粗代替标题：{m.group(1)[:40]}")

    # 7. cross references
    en_x = crossrefs(en_text, True)
    cn_x = crossrefs(cn_text, False)
    for kind in ("chapter", "listing", "figure", "table"):
        e, c = en_x[kind], cn_x[kind]
        for k, v in e.items():
            if c.get(k, 0) == 0 and v >= 1:
                add("轻微", "交叉引用", f"英文引用 {kind} {k} 共 {v} 次，中文未见对应引用")

    # 8. untranslated leftovers
    for ln, s in leftovers(cn_text)[:MAX_ITEMS]:
        add("严重", "疑似未翻译", f"cn:{ln} {s[:120]}")

    # 9. typography
    for name, hits in sorted(typo_hits(cn_text).items()):
        if not hits:
            continue
        sample = ", ".join(f"L{ln}:{repr(t)}" for ln, t in hits[:6])
        add("轻微" if "空格" in name else "中等", "排版", f"{name} x{len(hits)} -> {sample}")

    # 10. length ratio (soft signal)
    ratio = len(cn_text) / len(en_text) if en_text else 0
    if ratio < 0.42:
        add("轻微", "篇幅异常", f"中文/英文字符比 {ratio:.2f}，低于全书均值，建议抽查是否漏译")

    return issues


def _is_next(prev: str, cur: str) -> bool:
    """Very loose 'is cur a plausible successor of prev' check."""
    p, c = prev.split("."), cur.split(".")
    if len(c) > len(p) + 1:
        return False
    for i in range(min(len(p), len(c)) - 1):
        if p[i] != c[i]:
            return False
    return True


_STRING_LITERAL = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'|"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')


def _python_ids(text: str) -> set:
    """Real Python identifiers inside fenced code, with comments and string
    literals stripped so that translated prose/examples do not raise false
    alarms. Only names a reader would have to match while typing the code."""
    ids: set = set()
    in_fence, buf = False, []
    for line in text.splitlines():
        if FENCE.match(line):
            if in_fence:
                code = _STRING_LITERAL.sub('""', "\n".join(buf))
                code = re.sub(r"#.*", "", code)
                for m in re.finditer(r"\bdef\s+(\w+)\s*\(([^)]*)\)", code):
                    ids.add("def:" + m.group(1))
                    for p in m.group(2).split(","):
                        p = p.split(":")[0].split("=")[0].strip().lstrip("*")
                        if p and p not in ("self", "cls"):
                            ids.add("arg:" + p)
                for m in re.finditer(r"\bclass\s+(\w+)", code):
                    ids.add("class:" + m.group(1))
                for m in re.finditer(r"^\s*(\w+)\s*=[^=]", code, re.M):
                    if m.group(1) not in ("if", "for", "while", "return", "print"):
                        ids.add("var:" + m.group(1))
                for m in re.finditer(r"@(\w+)", code):
                    ids.add("dec:" + m.group(1))
                for m in re.finditer(r"\b(\w+)\.(\w+)", code):
                    ids.add("attr:" + m.group(2))
                buf = []
            in_fence = not in_fence
            continue
        if in_fence:
            buf.append(line)
    return ids


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", default=os.path.join(REPO, "review"))
    args = ap.parse_args()

    en_dir = os.path.join(args.review, "en")
    with open(os.path.join(en_dir, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)

    out = ["# 结构比对报告", "",
           "由 `tools/check_structure.py` 自动生成。严重度：严重 > 中等 > 轻微。",
           "（篇幅/列表项等为软信号，需人工复核。）", ""]

    summary = []
    all_terms = defaultdict(lambda: defaultdict(Counter))

    for entry in manifest:
        en_path = os.path.join(REPO, entry["en_path"])
        cn_path = os.path.join(REPO, entry["cn_path"])
        en_text = open(en_path, encoding="utf-8").read()
        cn_text = open(cn_path, encoding="utf-8").read()

        issues = check_pair(en_text, cn_text, entry["cn_path"], REPO)
        by_sev = Counter(s for s, _, _ in issues)
        summary.append((entry["en_name"], entry["cn_path"],
                        by_sev["严重"], by_sev["中等"], by_sev["轻微"]))

        out.append(f"## {entry['en_name']} ↔ `{entry['cn_path']}`")
        out.append("")
        if not issues:
            out.append("无结构性问题。")
            out.append("")
            continue
        for sev in ("严重", "中等", "轻微"):
            group = [i for i in issues if i[0] == sev]
            if not group:
                continue
            out.append(f"### {sev} ({len(group)})")
            out.append("")
            for _, cat, det in group[:MAX_ITEMS]:
                out.append(f"- **{cat}**：{det}")
            if len(group) > MAX_ITEMS:
                out.append(f"- …… 另有 {len(group) - MAX_ITEMS} 条同类问题")
            out.append("")

        tu = term_usage(cn_text)
        for term, variants in tu.items():
            for v, c in variants.items():
                if c:
                    all_terms[term][v][entry["en_name"]] += c

    out.append("## 总览")
    out.append("")
    out.append("| 章节 | 中文文件 | 严重 | 中等 | 轻微 |")
    out.append("|---|---|---:|---:|---:|")
    for name, cn, a, b, c in summary:
        out.append(f"| {name} | `{cn}` | {a} | {b} | {c} |")
    out.append(f"| **合计** | | {sum(s[2] for s in summary)} | "
               f"{sum(s[3] for s in summary)} | {sum(s[4] for s in summary)} |")
    out.append("")

    out.append("## 术语译法分布")
    out.append("")
    out.append("同一英文术语出现多种中文译法时列出；括号内为各章出现次数。")
    out.append("")
    out.append("| 英文 | 中文译法 | 出现章节（次数） |")
    out.append("|---|---|---|")
    for term in sorted(all_terms):
        variants = {v: sum(c.values()) for v, c in all_terms[term].items()}
        variants = {v: n for v, n in variants.items() if n}
        if len(variants) <= 1:
            continue
        for v, _ in sorted(variants.items(), key=lambda kv: -kv[1]):
            per = ", ".join(f"{k}:{n}" for k, n in sorted(all_terms[term][v].items()))
            out.append(f"| {term} | {v} | {per} |")
    out.append("")

    os.makedirs(args.review, exist_ok=True)
    path = os.path.join(args.review, "structure.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print(f"wrote {path}")
    for name, cn, a, b, c in summary:
        print(f"{name:18s} 严重={a:3d} 中等={b:3d} 轻微={c:3d}")


if __name__ == "__main__":
    main()
