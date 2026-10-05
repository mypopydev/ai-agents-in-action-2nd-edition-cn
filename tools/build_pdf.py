#!/usr/bin/env python3
"""把 cn-book/ 下的 Markdown 构建成一本中文 PDF。

用法：
    python3 tools/build_pdf.py                 # 输出到 dist/
    python3 tools/build_pdf.py -o /tmp/x.pdf   # 指定输出
    python3 tools/build_pdf.py --keep          # 保留中间产物 dist/build/

依赖（本机已具备）：pandoc、XeLaTeX（TeX Live + ctex）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import shutil
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "cn-book"

BOOK_TITLE = "AI Agents in Action（第二版）"
BOOK_SUBTITLE = "中文译本 · Intelligent workflows with LLMs, MCP, A2A, and more"
BOOK_AUTHOR = "Michael Lanham 著"

# 章序（与 sidebar.md 一致）
FRONT = ["0.关于本书.md"]
CHAPTERS = [
    "1.AI智能体的崛起.md",
    "2.核心组件.md",
    "3.AI智能体的MCP操作.md",
    "4.架构与构建多智能体系统.md",
    "5.智能体推理与规划.md",
    "6.为智能体处理记忆与知识RAG.md",
    "7.通过评估与反馈构建稳健的智能体.md",
    "8.部署智能体与智能体系统.md",
    "9.理解智能体循环.md",
    "10.探索会思考、监控和适应的认知智能体.md",
    "11.构建智能体系统的实用技巧.md",
]
APPENDIX = [
    "附录A-设置示例代码仓库.md",
    "附录B-为本地MCP服务器设置Node.js.md",
]

H1 = re.compile(r"^#\s+(.*)$", re.M)
FENCE = re.compile(r"^\s*```")
# 「第 10 章 标题」「10 标题」「第 3 章：标题」等前缀
TITLE_PREFIX = re.compile(r"^第?\s*\d+\s*章?[：:、.\s]+")
# 附录标题里自带的「附录 A 」，交给 LaTeX 自己渲染
APPENDIX_PREFIX = re.compile(r"^附录\s*[A-Z]\s*[：:、.\s]*")

# 这些字符没有任何已装字体能渲染，构建时替换为等价且可渲染的字符。
# 只作用于合并后的临时文件，**不写回仓库**；网站显示保持原样。
GLYPH_MAP = {
    "\u27f9": "\u21d2",        # ⟹ → ⇒     Menlo 无 U+27F9
    "\U0001f7e2": "\u25cf",    # 🟢 → ●     emoji，XeTeX 无法渲染
    "\U0001f680": "\u25b6",    # 🚀 → ▶     同上
    "\ufe0f": "",              # 变体选择符 U+FE0F，去掉即可（⚠️ → ⚠）
}

HEADER_TEX = r"""
% ============ 封面 ============
\usepackage{graphicx}
% 图片不超过版心宽度（pandoc 默认按原始尺寸插入，截图会溢出）
\makeatletter
\def\maxwidth{\ifdim\Gin@nat@width>\linewidth\linewidth\else\Gin@nat@width\fi}
\makeatother
\setkeys{Gin}{width=\maxwidth,keepaspectratio}

% ============ 页眉页脚 ============
\usepackage{fancyhdr}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[LE,RO]{\thepage}
\fancyhead[LO]{\nouppercase{\rightmark}}
\fancyhead[RE]{\nouppercase{\leftmark}}
\renewcommand{\headrulewidth}{0.4pt}
% 章节号由脚本写进标题（源文件里两种写法混用），LaTeX 不再自动编号，
% 页眉直接取标题。不能用 \CTEXthechapter —— ctex 的 \chapter 在自增计数器
% 之前就调用 \chaptermark，取到的永远是「第零章」。
\renewcommand{\chaptermark}[1]{\markboth{#1}{}}
\fancypagestyle{plain}{\fancyhf{}\fancyhead[LE,RO]{\thepage}\renewcommand{\headrulewidth}{0pt}}
% 保留「第 3 章 使用 MCP…」里章号后的空格（xeCJK 默认会吃掉中文之间的空格）
\xeCJKsetup{CJKspace=true}

% ============ 字体 ============
% 等宽（代码块）：Menlo 覆盖 ≤ ≈ ≥ ⚠ ➥ → ⇒ ● ▶
\setmonofont{Menlo}[Scale=0.92]
% 正文拉丁字体（Latin Modern）缺 数学运算符 / 杂项符号 / 补充箭头 区，
% 回退到 Apple Symbols。注意 fontspec 的区间回退只生效一个字体，必须是最后一个。
\setmainfont{Apple Symbols}[Range={mathematical-operators,miscellaneous-symbols,supplemental-arrows-a},Scale=MatchLowercase]
% ➥ 在 Dingbats 区而 Apple Symbols 没有 —— 正文里定点修补（verbatim 内不生效，
% 但代码块用的是 Menlo，本就有该字形）
\usepackage{newunicodechar}
\newfontfamily\dingfont{Zapf Dingbats}
\newunicodechar{➥}{{\dingfont ➥}}

% ============ 强调字体：中文用楷体 ============
% 中文加粗改用楷体（Kaiti SC 有真实 Bold 字重，无需合成加粗）。
% xeCJK 会自动分流字符：CJK 走 CJK 字体、拉丁走拉丁字体，
% 所以 \kaiemph 只影响中文，150 处纯英文加粗（**Agent**/**MCP**）保持原样。
% 必须显式声明 BoldFont —— 否则 \bfseries 在楷体家族下不生效，中文会静默掉字重。
\newCJKfontfamily\kaiemph{Kaiti SC}[BoldFont={Kaiti SC Bold}]
\renewcommand{\textbf}[1]{{\kaiemph\bfseries #1}}
% 省略号：宋体一族（Songti SC / Songti TC / STSong / Kaiti SC）的 ToUnicode 会把
% U+2026（…）反查成 U+22EF（⋯）——**复制出来与源文不是同一个码位**。代码块用的
% Menlo 映射正确，只有中文正文受影响。这里把省略号换到一个映射正确的 CJK 字体。
% 注意必须用 newCJKfontfamily（保持该字符仍在 CJK 标点类里）——
% 直接用 newfontfamily + newunicodechar 会被 xeCJK 的标点处理绕过去，不生效。
% 选 STFangsong 而不是 PingFang SC：后者 fc-match 解析虽正确（苹方-简），
% 但 fontspec 从 .ttc 取 face 时实际拿到的是 **PingFangHK**（港区字形）。
% STFangsong 是 macold 的等宽 CJK 字体，本书本来就在用，不额外引入字体。
\newCJKfontfamily\ellipsisfont{STFangsong}
\newunicodechar{…}{{\ellipsisfont …}}
% 代码块超过版心的长行自动折行。
% breaksymbolleft/right 必须清空：fvextra 默认在折行处插一个 ↪，它会**打印在页面上
% 并被复制进读者的代码里**（实测 29 处），是拷贝乱码的来源之一。
% 改用 breakindent 缩进续行来提示折行，复制出来只是多几个空格。
\usepackage{fvextra}
\fvset{breaklines=true,breakanywhere=true,breaksymbolleft={},breaksymbolright={},breakindent=1.5em,fontsize=\footnotesize}
% 上面这条只作用于 fvextra 的 Verbatim —— 也就是**标了语言**的代码块（pandoc 会输出
% Shaded/Highlighting，里面是 Verbatim）。没标语言的块 pandoc 输出的是标准
% \begin{verbatim}：既不折行、也不吃 \footnotesize，超过版心的长行**直接顶出纸面被裁掉**。
% 实测第 8 章 8 行被切（页 267/283/284/285），页面上会看到 "to confirm reac"
% （reaction 被切）、"what remains unkn"（unknown 被切）。
% 注意这个缺陷骗过了所有原有指标：字形都在（缺字形 0）、没多出字符（字符差集 0）、
% 页数正常，只有量每行的 xMax 才看得见。已固化为基线项「PDF 越界裁切行」。
% 这里把 verbatim 也换成 Verbatim，两类代码块行为一致。
\RecustomVerbatimEnvironment{verbatim}{Verbatim}{breaklines=true,breakanywhere=true,breaksymbolleft={},breaksymbolright={},breakindent=1.5em,fontsize=\footnotesize}
"""


def load(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


extra_h1: list[str] = []


def demote_extra_h1(text: str, source: str) -> str:
    """章文件里除第一个之外的一级标题降为二级。

    每章只应有一个 H1（章标题）。正文里若混进 H1（如第 6 章的 `# 总结`），
    pandoc 会把它当成新的一章，PDF 里会凭空多出一章。这里降级并记录，供最后提示。
    """
    out: list[str] = []
    in_fence = False
    seen = 0
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and line.startswith("# "):
            seen += 1
            if seen > 1:
                extra_h1.append(f"{source}  {line.strip()}")
                out.append("#" + line)
                continue
        out.append(line)
    return "\n".join(out)


def strip_title(text: str) -> tuple[str, str]:
    """返回 (正文原样, 剥掉章号前缀的纯标题)。

    只解析标题，不改正文；改 H1 由调用方用 set_h1() 完成。
    """
    m = H1.search(text)
    if not m:
        return text, ""
    raw = m.group(1).strip()
    return text, (TITLE_PREFIX.sub("", raw).strip() or raw)


def set_h1(text: str, new_title: str) -> str:
    m = H1.search(text)
    if not m:
        return f"# {new_title}\n\n{text}"
    return text[: m.start(1)] + new_title + text[m.end(1) :]


def normalize_glyphs(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for bad, good in GLYPH_MAP.items():
        n = text.count(bad)
        if n:
            counts[f"{bad!r} → {good!r}" if good else f"{bad!r} → (删除)"] = n
            text = text.replace(bad, good)
    return text, counts


def build_merged() -> tuple[str, dict[str, int], list[tuple[str, float]]]:
    """按书序合并各章。

    章号由脚本按 `CHAPTERS` 的顺序重新写入标题，而不交给 LaTeX 计数器
    （ctex 的 \\chapter 在自增计数器之前就调用 \\chaptermark，用 \\CTEXthechapter
    取到的永远是「第零章」，见 header.tex 注释）。这样也保证章节号与文件顺序永远一致。

    返回值第三项是每个文件的「输出/输入」字符比，用于兜住"正文被吞"这类事故
    （改写标题时若不小心复用变量，可能把整段正文替换成标题字符串，而构建仍成功）。
    """
    parts: list[str] = []
    ratios: list[tuple[str, float]] = []

    def take(name: str, new_title: str) -> str:
        src = load(name)
        text = demote_extra_h1(src, name)
        _, title = strip_title(text)
        out = set_h1(text, new_title(title))
        ratios.append((name, len(out) / max(len(src), 1)))
        return out

    for name in FRONT:
        parts.append(take(name, lambda _t: "关于本书 {-}"))

    for i, name in enumerate(CHAPTERS, 1):
        if not (SRC / name).exists():
            sys.exit(f"缺少章节文件：{name}")
        parts.append(take(name, lambda t, i=i: f"第 {i} 章 {t}"))

    for letter, name in zip("AB", APPENDIX):
        parts.append(
            take(name, lambda t, l=letter: f"附录 {l} {APPENDIX_PREFIX.sub('', t).strip() or t} {{-}}")
        )

    merged = "\n\n\\newpage\n\n".join(p.strip() for p in parts)
    merged, counts = normalize_glyphs(merged)
    return merged, counts, ratios


def run_pandoc(src: pathlib.Path, out: pathlib.Path, head: pathlib.Path, date: str) -> int:
    cmd = [
        "pandoc", str(src),
        "-o", str(out),
        "--pdf-engine=xelatex",
        f"--resource-path={REPO}",
        "--toc", "--toc-depth=2",
        "-H", str(head),
        "-V", "documentclass=ctexbook",
        # 钉住 fontset。不钉的话 ctex 每次按机器自动判定：它看
        # /System/Library/Fonts/PingFang.ttc 在不在，不在就退回 macold。
        # 本机该路径不存在（PingFang 装了，只是不在 ctex 硬编码的位置）→ 现在取 macold；
        # 换台机器或系统更新后可能翻到 macnew，等宽/斜体/无衬线的 CJK 字体会跟着变，
        # 13 处含中文的行内代码会重新排版。钉住即可跨机器复现。
        "-V", "classoption=fontset=macold",
        "-V", "CJKmainfont=Songti SC",
        "-V", "papersize=a4",
        "-V", "geometry:margin=2.3cm",
        "-V", "fontsize=11pt",
        "-V", "linestretch=1.15",
        "-V", f"title={BOOK_TITLE}",
        "-V", f"subtitle={BOOK_SUBTITLE}",
        "-V", f"author={BOOK_AUTHOR}",
        "-V", f"date={date}",
        "-V", "titlepage=true",
        "-V", "colorlinks=true",
        "-V", "linkcolor=black",
        "-V", "toccolor=black",
        "--highlight-style=tango",
    ]
    print("  " + " ".join(cmd[:6]) + " …")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    warns = [l for l in proc.stderr.splitlines() if "Missing character" in l]
    if warns:
        print(f"  ⚠ 缺字形 {len(warns)} 条：")
        for w in sorted(set(warns))[:10]:
            print("     " + w.replace("[WARNING] ", ""))
    if proc.returncode != 0:
        print(proc.stderr[-3000:], file=sys.stderr)
        sys.exit("pandoc 构建失败")
    return len(warns)


def main() -> None:
    ap = argparse.ArgumentParser(description="构建中文版 PDF")
    ap.add_argument("-o", "--output", type=pathlib.Path)
    ap.add_argument("--keep", action="store_true", help="保留中间产物")
    args = ap.parse_args()

    dist = REPO / "dist"
    dist.mkdir(exist_ok=True)
    out = args.output or dist / "ai-agents-in-action-2nd-cn.pdf"

    merged, counts, ratios = build_merged()

    # 兜底：改写标题时若误吞正文，构建仍会「成功」，只有页数会悄悄变少。
    thin = [(n, r) for n, r in ratios if r < 0.95]
    if thin:
        print("  ⚠ 以下文件合并后内容明显变短，疑似正文被吞：")
        for n, r in thin:
            print(f"     {n}  输出/输入 = {r:.2%}")

    work = dist / "build"
    work.mkdir(exist_ok=True)
    src = work / "book.md"
    src.write_text(merged, encoding="utf-8")
    head = work / "header.tex"
    head.write_text(HEADER_TEX, encoding="utf-8")

    if extra_h1:
        print("  ⚠ 章文件里出现了额外的一级标题（已在本 PDF 中降为二级，建议改源文件）：")
        for w in extra_h1:
            print(f"     {w}")

    if counts:
        print("  字符归一化（仅 PDF 构建，不改仓库）：")
        for k, v in counts.items():
            print(f"     {k}  ×{v}")

    date = dt.date.today().isoformat()
    glyph_warns = run_pandoc(src, out, head, date)

    # 机器可读的构建报告，供 tools/check_baseline.py 比对（缺字形等只在构建期可知）
    report = {
        "built": date,
        "size_bytes": out.stat().st_size,
        "glyph_warnings": glyph_warns,
        "extra_h1": extra_h1,
        "normalized": counts,
        "min_content_ratio": min((r for _, r in ratios), default=1.0),
    }
    (dist / "build-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    size = out.stat().st_size / 1e6
    print(f"\n✅ {out}  ({size:.1f} MB)")


if __name__ == "__main__":
    main()
