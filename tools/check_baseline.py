#!/usr/bin/env python3
"""基线对比清单：把全书各项指标与「已验收基线」逐条比对。

用法：
    python3 tools/check_baseline.py            # 对比并写 review/baseline.md
    python3 tools/check_baseline.py --update   # 把当前值写回为本脚本里的基线

设计意图：**只看"脚本跑完了"会漏掉产物错误**（曾出现 PDF 构建静默吞掉整段附录、
退出码仍为 0 的事故）。任何一次改动后跑这个脚本，能立刻看到哪项指标漂了。

依赖：pypdf（PDF 书签）、poppler 的 pdfinfo/pdftotext（PDF 页数与文字）。
PDF 不存在时自动跳过该组，不报错。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unicodedata

REPO = pathlib.Path(__file__).resolve().parent.parent
SRC = REPO / "cn-book"
DIST = REPO / "dist"
PDF = DIST / "ai-agents-in-action-2nd-cn.pdf"

FENCE = re.compile(r"^\s*```")
LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
LINKPATH = re.compile(r"\]\([^)]*\)")
INLINE = re.compile(r"`[^`]*`")
H1 = re.compile(r"^#\s+(.*)$", re.M)
RUN = re.compile(r"(?<!\*)\*\*(?!\*)")
CJK = "\u4e00-\u9fff"
FW_PUNCT = "\u3001\u3002\uff0c\uff1a\uff1b\uff01\uff1f\uff09\u3011\u300b\u300d\u300f"

# ───────────────────────── 已验收基线 ─────────────────────────
BASELINE: dict[str, object] = {
    # 内容结构
    "文件数": 14,
    "每文件 H1 数（应为 1）": "全部 1",
    "章标题「第 N 章 」": 11,
    "附录标题「附录 X 」": 2,
    "「本章内容」H3": 11,
    "小结「## 总结」": 11,
    "二级标题数": 78,
    "三级标题数": 145,
    "围栏代码块数": 180,
    # 术语（已剔除链接路径与代码块）
    "Agent": 3271,
    "正文独立「智能体」": 0,
    "多/单智能体": 98,
    "MCP 服务器": 249,
    "MCP Server": 30,
    "MCP 客户端": 18,
    "MCP Client": 0,
    "代码清单": 261,
    "API Key": 30,
    "批评 Agent": 24,
    # 排版（全部应为 0）
    "① 中英之间缺空格": 0,
    "② MCP 术语尾空格": 0,
    "③ 中文之间空格": 0,
    "④ 全角标点后接中文": 0,
    "⑤ 全角标点后接半角": 0,
    "⑥ 加粗标记错位": 0,
    # 校验器
    "结构检查·严重": 0,
    "结构检查·中等": 0,
    "结构检查·轻微": 17,
    "行内标记配对失败": 0,
    "加粗配对成功": 1286,
    "失效内部链接": 0,
    "书稿代码块含中文": 0,
    "README 目录与 H1 不一致": 0,
    # PDF
    "PDF 页数": 384,
    "PDF 纸型": "A4",
    "PDF 书签总数": 92,
    "PDF 书签 L0": 14,
    "PDF 书签 L1": 78,
    "PDF 缺字形告警": 0,
    "PDF 目录二级缺失": 0,
    "PDF 目录三级泄漏": 0,
    "PDF 附录标题缺失": 0,
    "PDF 出现源文没有的字符": 0,
}

ZERO_EXPECTED = {k for k, v in BASELINE.items() if v == 0 and "轻微" not in k}


def files() -> list[pathlib.Path]:
    return sorted({f for pat in ("cn-book/*.md", "*.md") for f in REPO.glob(pat)})


def strip_code(text: str) -> str:
    """去掉围栏代码块。

    必须**按行**判断围栏，不能用 `re.sub(r"```.*?```")`——正文里有内联转义的
    近似写法（`（```` ``` ````）`，用四个反引号包住三个反引号，见第 2 章练习 4），
    正则会把第一个 `` ``` `` 和内容里的 `` ``` `` 配成一对，吞掉中间整段正文。
    渲染器（marked / pandoc）本身处理这种写法是正确的。
    """
    out, in_fence = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)


def count_headings() -> dict[str, int]:
    out = {"二级标题数": 0, "三级标题数": 0, "围栏代码块数": 0,
           "章标题「第 N 章 」": 0, "附录标题「附录 X 」": 0,
           "「本章内容」H3": 0, "小结「## 总结」": 0}
    for f in SRC.glob("*.md"):
        in_fence = False
        for line in f.read_text(encoding="utf-8").splitlines():
            if FENCE.match(line):
                if not in_fence:
                    out["围栏代码块数"] += 1
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if line.startswith("## "):
                out["二级标题数"] += 1
            elif re.match(r"^### \d+\.\d+\.\d+ ", line):
                out["三级标题数"] += 1
            if re.match(r"^### 本章内容", line):
                out["「本章内容」H3"] += 1
            if line.strip() == "## 总结":
                out["小结「## 总结」"] += 1
            if line.startswith("# "):
                if re.match(r"^# 第 \d+ 章 ", line):
                    out["章标题「第 N 章 」"] += 1
                elif re.match(r"^# 附录 [AB] ", line):
                    out["附录标题「附录 X 」"] += 1
    return out


def count_terms() -> dict[str, int]:
    body_all, raw_all = [], []
    for f in files():
        t = f.read_text(encoding="utf-8")
        raw_all.append(t)
        body_all.append(LINKPATH.sub("]", strip_code(t)))
    body = "\n".join(body_all)
    raw = "\n".join(raw_all)
    g = lambda p, s: len(re.findall(p, s))
    return {
        "Agent": g(r"\bAgent\b", body),
        "正文独立「智能体」": g(r"(?<![多单])智能体", body),
        "多/单智能体": g(r"[多单]智能体", body),
        "MCP 服务器": g(r"MCP\s*服务器", body),
        "MCP Server": g(r"MCP\s*[Ss]erver", body),
        "MCP 客户端": g(r"MCP\s*客户端", body),
        "MCP Client": g(r"MCP\s*[Cc]lient", body),
        "代码清单": g(r"代码清单", body),
        "API Key": g(r"API\s*Key", body),
        "批评 Agent": g(r"批评\s*Agent", body),
    }


def count_typography() -> dict[str, int]:
    checks = {
        "① 中英之间缺空格": re.compile(rf"(\*\*[^*\n]*[A-Za-z0-9])\*\*([{CJK}])|([A-Za-z0-9])\*\*([{CJK}])"),
        "② MCP 术语尾空格": re.compile(rf"(?:MCP 服务器|MCP 客户端) +(?=[{CJK}])"),
        "③ 中文之间空格": re.compile(rf"[{CJK}] +(?=[{CJK}])"),
        "④ 全角标点后接中文": re.compile(rf"[{FW_PUNCT}] +(?=[{CJK}])"),
        "⑤ 全角标点后接半角": re.compile(rf"[{FW_PUNCT}] +(?=[A-Za-z0-9`*])"),
    }
    nav = re.compile(r"^\s*\* \[")
    # 章标题体例允许「第 N 章 标题」「附录 X 标题」——扫描前先把这两个前缀里的空格去掉，
    # 否则章标题本身会被 ③ 误判（README 的列表项、**加粗项**、H1 都会中招）。
    title_space = re.compile(r"(第 \d+ 章|附录 [AB]) ")

    out = {k: 0 for k in checks}
    out["⑥ 加粗标记错位"] = 0

    def is_punct(c: str) -> bool:
        if not c:
            return False
        if c in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~":
            return True
        return unicodedata.category(c).startswith("P")

    for f in files():
        in_fence = False
        for line in f.read_text(encoding="utf-8").splitlines():
            if FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence or line.startswith("# ") or nav.match(line):
                continue
            probe = title_space.sub(lambda m: m.group(1).replace(" ", ""), line)
            for name, pat in checks.items():
                out[name] += len(pat.findall(probe))
            # ⑥：按 ** 运行序列奇偶配对（先屏蔽行内代码）
            clean = INLINE.sub(lambda m: "`" + "x" * (len(m.group(0)) - 2) + "`", probe)
            for k, m in enumerate(RUN.finditer(clean)):
                a = clean[m.start() - 1] if m.start() else ""
                b = clean[m.end()] if m.end() < len(clean) else ""
                if (k % 2 == 0 and b and b.isspace()) or (k % 2 == 1 and a and a.isspace()):
                    out["⑥ 加粗标记错位"] += 1
    return out


def count_links() -> dict[str, int]:
    names = {p.name for p in SRC.glob("*.md")}
    bad = 0
    for f in files():
        for m in LINK.finditer(f.read_text(encoding="utf-8")):
            href = m.group(1).split("#")[0].strip()
            if not href or href.startswith(("http://", "https://", "mailto:")):
                continue
            if href.split("/")[-1] not in names and not (REPO / href).exists():
                bad += 1
    return {"失效内部链接": bad}


def count_code_cjk() -> dict[str, int]:
    bad = 0
    for f in SRC.glob("*.md"):
        in_fence, buf = False, []
        for line in f.read_text(encoding="utf-8").splitlines():
            if FENCE.match(line):
                if in_fence and re.search(rf"[{CJK}]", "\n".join(buf)):
                    bad += 1
                buf = []
                in_fence = not in_fence
            elif in_fence:
                buf.append(line)
    return {"书稿代码块含中文": bad}


def count_readme_drift() -> dict[str, int]:
    h1 = {}
    for f in SRC.glob("*.md"):
        m = H1.search(f.read_text(encoding="utf-8"))
        if m:
            h1[f.name] = m.group(1).strip()
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    bad = 0
    for tag, title, fn in re.findall(r"\[(第 \d+ 章|附录 [AB]) ([^\]]+)\]\(cn-book/([^)]+)\)", readme):
        if h1.get(fn) != f"{tag} {title}":
            bad += 1
    return {"README 目录与 H1 不一致": bad}


def pdf_text(path: pathlib.Path) -> str:
    """抽取 PDF 文本。

    必须写临时文件，**不能用 `pdftotext <pdf> -`**：stdin 不是终端时（heredoc、
    DEVNULL）poppler 会去读 stdin，输出结果间歇性为空，导致校验静默通过。
    """
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        tmp = f.name
    try:
        subprocess.run(["pdftotext", str(path), tmp], check=True, capture_output=True)
        return pathlib.Path(tmp).read_text(encoding="utf-8", errors="replace")
    finally:
        pathlib.Path(tmp).unlink(missing_ok=True)


def count_pdf_chars() -> dict[str, int]:
    """PDF 里出现、但源文件里从未出现过的字符数（排除构建时已知的归一化）。

    这是「拷贝乱码」的兜底指标：字形显示正常但 ToUnicode 反查错码位时，
    页面上看不出来，只有把它抽成文本才发现多出了源文没有的字符。
    实例：`……`→`⋯`（宋体 ToUnicode 反查错）、`↪`（折页标记被复制进代码）。
    """
    if not PDF.exists():
        return {}
    src = "".join(f.read_text(encoding="utf-8") for f in SRC.glob("*.md"))
    # 构建时按设计归一化的字符（见 build_pdf.py 的 GLYPH_MAP）不算异常
    allowed = set("\u21d2\u25cf\u25b6") | set(GLYPH_FIXES)
    t = pdf_text(PDF)
    extra = {c for c in t if c not in src and c not in allowed and not c.isspace()}
    return {"PDF 出现源文没有的字符": len(extra)}


GLYPH_FIXES = "\u27f9\U0001f7e2\U0001f680\ufe0f"


def run_checker(script: str) -> str:
    p = subprocess.run([sys.executable, str(REPO / "tools" / script)],
                       capture_output=True, text=True)
    return p.stdout + p.stderr


def count_checkers() -> dict[str, object]:
    out: dict[str, object] = {}
    s = run_checker("check_structure.py")
    for k in ("严重", "中等", "轻微"):
        out[f"结构检查·{k}"] = sum(int(x) for x in re.findall(rf"{k}=\s*(\d+)", s))
    m = run_checker("check_inline_markup.py")
    mm = re.search(r"配对成功：加粗 (\d+)", m)
    out["加粗配对成功"] = int(mm.group(1)) if mm else -1
    fm = re.search(r"配对失败：(\d+) 处", m)
    out["行内标记配对失败"] = int(fm.group(1)) if fm else -1
    return out


def count_pdf() -> dict[str, object]:
    if not PDF.exists():
        return {}
    out: dict[str, object] = {}

    info = subprocess.run(["pdfinfo", str(PDF)], capture_output=True, text=True).stdout
    out["PDF 页数"] = int(re.search(r"Pages:\s+(\d+)", info).group(1))
    size = re.search(r"Page size:\s+([\d.]+) x ([\d.]+)", info)
    w, h = float(size.group(1)), float(size.group(2))
    out["PDF 纸型"] = "A4" if abs(w - 595.28) < 1 and abs(h - 841.89) < 1 else f"{w:.0f}x{h:.0f}"

    try:
        from pypdf import PdfReader

        rows: list[tuple[int, str]] = []

        def walk(node, depth):
            for it in node:
                if isinstance(it, list):
                    walk(it, depth + 1)
                else:
                    rows.append((depth, it.title))

        walk(PdfReader(str(PDF)).outline, 0)
        out["PDF 书签总数"] = len(rows)
        out["PDF 书签 L0"] = sum(1 for d, _ in rows if d == 0)
        out["PDF 书签 L1"] = sum(1 for d, _ in rows if d == 1)
    except Exception as e:  # noqa: BLE001
        print(f"  （书签读取失败：{e}）")

    # 缺字形只在构建期可知，读 tools/build_pdf.py 落下的构建报告
    rep = DIST / "build-report.json"
    if rep.exists():
        out["PDF 缺字形告警"] = json.loads(rep.read_text(encoding="utf-8")).get("glyph_warnings")

    txt = pdf_text(PDF)
    flat = re.sub(r"[.\s]+", "", txt)
    toc = "".join(txt.split("\f")[:8])
    toc_flat = re.sub(r"[.\s]+", "", toc)

    expect2, deep, app = [], set(), []
    for f in SRC.glob("*.md"):
        in_fence = False
        for line in f.read_text(encoding="utf-8").splitlines():
            if FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if re.match(r"^## (\d+\.\d+|[AB]\.\d+) ", line) or line.strip() == "## 总结":
                expect2.append(re.sub(r"^##\s*", "", line).strip())
            elif re.match(r"^### \d+\.\d+\.\d+ ", line):
                deep.add(line[4:].strip())
            elif re.match(r"^#{2,3} (A|B)\.\d", line):
                app.append(line.lstrip("# ").strip())
    out["PDF 目录二级缺失"] = sum(
        1 for t in dict.fromkeys(expect2) if re.sub(r"[.\s]+", "", t) not in toc_flat
    )
    out["PDF 目录三级泄漏"] = sum(
        1 for t in deep if re.sub(r"[.\s]+", "", t) in toc_flat
    )
    out["PDF 附录标题缺失"] = sum(
        1 for t in app if re.sub(r"[.\s]+", "", t) not in flat
    )
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="基线对比清单")
    ap.add_argument("--update", action="store_true", help="把当前值写回为本脚本的基线")
    args = ap.parse_args()

    cur: dict[str, object] = {
        "文件数": len(list(SRC.glob("*.md"))),
        "每文件 H1 数（应为 1）": "全部 1"
        if all(len(re.findall(r"^# ", strip_code(f.read_text(encoding='utf-8')), re.M)) == 1
               for f in SRC.glob("*.md"))
        else "存在异常",
    }
    cur.update(count_headings())
    cur.update(count_terms())
    cur.update(count_typography())
    cur.update(count_links())
    cur.update(count_code_cjk())
    cur.update(count_readme_drift())
    cur.update(count_checkers())
    cur.update(count_pdf())
    cur.update(count_pdf_chars())

    if args.update:
        changed = {k: v for k, v in cur.items() if BASELINE.get(k) != v}
        src_text = pathlib.Path(__file__).read_text(encoding="utf-8")
        for k, v in cur.items():
            src_text = re.sub(
                rf'("{re.escape(k)}": )(".*?"|[-\d.]+)',
                lambda m, v=v: m.group(1) + json.dumps(v, ensure_ascii=False),
                src_text,
                count=1,
            )
        pathlib.Path(__file__).write_text(src_text, encoding="utf-8")
        print(f"已写回基线；变动 {len(changed)} 项")
        for k, v in changed.items():
            print(f"   {k}: {BASELINE.get(k)} → {v}")
        return

    # ── 对比 ──
    rows, drift, skipped = [], [], 0
    for k, base in BASELINE.items():
        if k not in cur:
            skipped += 1
            continue
        got = cur[k]
        ok = got == base
        if not ok:
            drift.append((k, base, got))
        rows.append((k, base, got, ok))

    lines = [
        "# 基线对比清单",
        "",
        "由 `python3 tools/check_baseline.py` 生成。**任何一次改动后跑一遍**，",
        "比对全书指标与已验收基线。改动前后页数、书签数、术语计数等一旦漂移即会标出。",
        "",
        f"- 对比项：{len(rows)} 项（另有 {skipped} 项未测——通常是没有 PDF）",
        f"- 与基线一致：**{len(rows) - len(drift)}** 项",
        f"- **与基线不一致：{len(drift)}** 项",
        "",
        "## 明细",
        "",
        "| 指标 | 基线 | 当前 | |",
        "|---|---:|---:|:--:|",
    ]
    for k, base, got, ok in rows:
        lines.append(f"| {k} | {base} | {got} | {'✅' if ok else '⚠️'} |")

    if drift:
        lines += ["", "## 漂移项", "", "| 指标 | 基线 | 当前 |", "|---|---:|---:|"]
        for k, base, got in drift:
            lines.append(f"| {k} | {base} | {got} |")
        lines += ["", "确认漂移是有意为之，执行 `python3 tools/check_baseline.py --update` 更新基线。"]

    (REPO / "review").mkdir(exist_ok=True)
    (REPO / "review" / "baseline.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"对比 {len(rows)} 项：一致 {len(rows) - len(drift)}，不一致 {len(drift)}")
    for k, base, got in drift:
        print(f"   ⚠️ {k}: 基线 {base} → 当前 {got}")
    print("明细：review/baseline.md")
    if drift:
        sys.exit(1)


if __name__ == "__main__":
    main()
