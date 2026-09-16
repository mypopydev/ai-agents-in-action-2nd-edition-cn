#!/usr/bin/env python3
"""检测 markdown 行内标记（加粗 / 斜体 / 行内代码）的配对异常。

不是"数星号"——计数法查不出「星号总数没变、但空格/标点插错一侧导致
加粗失效」这类缺陷。这里用 **docsify 同源的 marked 词法分析**做地面真值：

- 成功配对的加粗 → `strong` token
- 配对失败的字面标记 → 原样留在 `text` token 的 raw 里

只要 `text` token 里出现 `*` 或反引号，就是真实渲染会出问题的地方。

输出：review/inline-markup.md
用法：python3 tools/check_inline_markup.py
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
MARKED_CANDIDATES = [
    pathlib.Path("/opt/homebrew/lib/node_modules/docsify-cli/node_modules/marked"),
    REPO / "node_modules" / "marked",
]

TARGETS = ["cn-book/*.md", "*.md"]
FENCE = re.compile(r"^\s*```")

# 在 node 侧遍历 token 树，挑出「字面标记残留」的 text token
NODE_SCRIPT = r"""
const { marked } = require(MARKED);
let raw = "";
process.stdin.on("data", d => raw += d);
process.stdin.on("end", () => {
  const blocks = JSON.parse(raw);
  const out = blocks.map(src => {
    const broken = [];
    let strong = 0, em = 0, codespan = 0;
    const walk = (t) => {
      if (t.type === "strong") strong++;
      if (t.type === "em") em++;
      if (t.type === "codespan") codespan++;
      // 只把**叶子** text 判为失效。列表项外面包了一层 text，它的 raw 是整条原文
      // （含已成功解析的 **…**），照 raw 判断会把合法加粗全误报成失效；
      // 有 .tokens 说明这一层已被成功解析，不该算失效。
      if (t.type === "text" && !t.tokens && typeof t.raw === "string" && /[*`]/.test(t.raw)) {
        broken.push(t.raw);
      }
      // marked 的 list token 用 .items 而不是 .tokens —— 只跟 .tokens 会
      // **完全跳过所有列表项**。本书大量加粗都在列表里（- **#1** …、- **专业化**：…），
      // 漏掉它们意味着这些位置的失效标记永远查不出来。
      if (t.tokens) for (const k of t.tokens) walk(k);
      if (t.items) for (const k of t.items) walk(k);
    };
    for (const t of marked.lexer(src)) walk(t);
    return { broken, strong, em, codespan };
  });
  process.stdout.write(JSON.stringify(out));
});
"""


def find_marked() -> pathlib.Path:
    for p in MARKED_CANDIDATES:
        if (p / "package.json").exists():
            return p
    sys.exit("未找到 marked：请先 `npm i -g docsify-cli`（其自带 marked，与站点渲染器同源）")


def split_blocks(lines: list[str]) -> list[tuple[int, str]]:
    """按空行切块，围栏代码块内的空行不算边界。

    行内标记不能跨空行，所以逐块分析等价于整篇分析，还能定位到行号。
    """
    blocks: list[tuple[int, str]] = []
    cur: list[str] | None = None
    start = 0
    in_fence = False

    for i, line in enumerate(lines, 1):
        if FENCE.match(line):
            in_fence = not in_fence
            if cur is None:
                cur, start = [], i
            cur.append(line)
            continue
        if in_fence:
            assert cur is not None
            cur.append(line)
            continue
        if not line.strip():
            if cur:
                blocks.append((start, "\n".join(cur)))
                cur = None
            continue
        if cur is None:
            cur, start = [], i
        cur.append(line)

    if cur:
        blocks.append((start, "\n".join(cur)))
    return blocks


def classify(raw: str) -> str:
    """把残留片段归类，便于按类型统计。"""
    if "`" in raw:
        return "反引号未配对的代码片段"
    if "**" in raw:
        return "加粗标记 ** 未配对"
    return "斜体标记 * 未配对"


def main() -> None:
    marked_dir = find_marked()
    version = json.loads((marked_dir / "package.json").read_text())["version"]

    files = sorted({f for pat in TARGETS for f in REPO.glob(pat)})

    # 汇总所有文件的块，一次性交给 node
    index: list[tuple[pathlib.Path, int, str]] = []
    for path in files:
        for start, text in split_blocks(path.read_text(encoding="utf-8").splitlines()):
            index.append((path, start, text))

    script = "const MARKED=" + json.dumps(str(marked_dir)) + ";\n" + NODE_SCRIPT
    proc = subprocess.run(
        ["node", "-e", script],
        input=json.dumps([t for _, _, t in index]),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"marked 渲染失败：\n{proc.stderr}")
    results = json.loads(proc.stdout)

    findings: list[tuple[pathlib.Path, int, str, str]] = []
    n_strong = n_em = n_code = 0
    for (path, start, text), res in zip(index, results):
        n_strong += res["strong"]
        n_em += res["em"]
        n_code += res["codespan"]
        for raw in res["broken"]:
            findings.append((path, start, classify(raw), raw))

    by_kind: dict[str, int] = {}
    for _, _, kind, _ in findings:
        by_kind[kind] = by_kind.get(kind, 0) + 1

    lines = [
        "# 行内标记配对检查（marked 词法分析）",
        "",
        f"- 渲染器：marked {version}（docsify 全局安装自带，与站点同源）",
        "- 方法：逐块 `marked.lexer()` → 配对成功的进 `strong`/`em` token，"
        "配对失败的**原样留在 `text` token**，据此定位",
        "- 说明：行内标记不能跨空行，故逐块分析等价于整篇分析，且能定位到行号",
        "",
        f"- 扫描：{len(files)} 个文件 / {len(index)} 个文本块",
        f"- 配对成功：加粗 {n_strong} 处、斜体 {n_em} 处、行内代码 {n_code} 处",
        f"- **配对失败：{len(findings)} 处**",
        "",
    ]

    if findings:
        lines += ["## 按类型", "", "| 类型 | 处数 |", "|---|---:|"]
        for kind, c in sorted(by_kind.items(), key=lambda x: -x[1]):
            lines.append(f"| {kind} | {c} |")
        lines += [
            "",
            "## 明细",
            "",
            "| 位置 | 类型 | 会原样显示的内容 |",
            "|---|---|---|",
        ]
        for path, start, kind, raw in findings:
            shown = raw.replace("|", "\\|").replace("\n", " ")
            if len(shown) > 110:
                shown = shown[:110] + "…"
            lines.append(f"| `{path.name}:{start}` | {kind} | `{shown}` |")
    else:
        lines += ["## 结果", "", "**未发现配对失败的标记。**"]

    out = REPO / "review" / "inline-markup.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"扫描 {len(files)} 个文件 / {len(index)} 个文本块")
    print(f"配对成功：加粗 {n_strong}、斜体 {n_em}、行内代码 {n_code}")
    print(f"配对失败：{len(findings)} 处")
    for kind, c in sorted(by_kind.items(), key=lambda x: -x[1]):
        print(f"    {kind}: {c}")
    print(f"明细：{out}")


if __name__ == "__main__":
    main()
