#!/usr/bin/env python3
"""Extract the original English text from the source epub into markdown.

Usage:
    python3 tools/extract_epub.py [--epub PATH] [--out DIR]

Output layout: <out>/en/<name>.md  plus a <out>/en/manifest.json mapping each
English file to the corresponding Chinese file under cn-book/.

The extracted markdown keeps paragraph anchors (<!--p123-->) so that review
findings can point at an exact location in the original.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import zipfile
from html.parser import HTMLParser

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# English epub file -> Chinese markdown file (relative to repo root)
CN_MAP = {
    "about-this-book": "cn-book/0.关于本书.md",
    "chapter-1": "cn-book/1.AI智能体的崛起.md",
    "chapter-2": "cn-book/2.核心组件.md",
    "chapter-3": "cn-book/3.AI智能体的MCP操作.md",
    "chapter-4": "cn-book/4.架构与构建多智能体系统.md",
    "chapter-5": "cn-book/5.智能体推理与规划.md",
    "chapter-6": "cn-book/6.为智能体处理记忆与知识RAG.md",
    "chapter-7": "cn-book/7.通过评估与反馈构建稳健的智能体.md",
    "chapter-8": "cn-book/8.部署智能体与智能体系统.md",
    "chapter-9": "cn-book/9.理解智能体循环.md",
    "chapter-10": "cn-book/10.探索会思考、监控和适应的认知智能体.md",
    "chapter-11": "cn-book/11.构建智能体系统的实用技巧.md",
    "appendix-a": "cn-book/附录A-设置示例代码仓库.md",
    "appendix-b": "cn-book/附录B-为本地MCP服务器设置Node.js.md",
}

INLINE_SKIP = {"span", "a", "sub", "sup", "small"}


class Extractor(HTMLParser):
    """Convert an InDesign-generated epub XHTML chapter into markdown."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.buf: list[str] = []
        self.div_classes: list[str] = []
        self.heading_level: int | None = None
        self.in_pre = False
        self.pre_lines: list[str] = []
        self.list_stack: list[dict] = []
        self.table_rows: list[list[str]] | None = None
        self.cur_row: list[str] | None = None
        self.cell: list[str] | None = None
        self.pending_anchor: str | None = None

    # ---------- helpers ----------
    def _flush_buf(self) -> str:
        text = "".join(self.buf)
        self.buf = []
        return re.sub(r"\s+", " ", text).strip()

    def _container(self) -> str:
        return self.div_classes[-1] if self.div_classes else ""

    def _emit(self, text: str = "") -> None:
        self.lines.append(text)

    def _emit_anchor(self) -> None:
        if self.pending_anchor:
            self.lines.append(f"<!--{self.pending_anchor}-->")
            self.pending_anchor = None

    @staticmethod
    def _attr(attrs, name):
        for k, v in attrs:
            if k == name:
                return v or ""
        return ""

    # ---------- tag handling ----------
    def handle_starttag(self, tag, attrs):
        if self.in_pre:
            # inside <pre> the InDesign markup wraps code fragments in
            # <strong>/<span>; keep the raw text and only honour <br>
            if tag == "br":
                self.pre_lines.append("\n")
            return
        if tag == "div":
            self.div_classes.append(self._attr(attrs, "class"))
            anchor = self._attr(attrs, "id")
            if re.fullmatch(r"p\d+", anchor):
                self.pending_anchor = anchor
            return
        if tag == "img":
            src = self._attr(attrs, "src")
            name = os.path.basename(src)
            if name:
                self._emit_anchor()
                self.buf.append(f"![](images/{name})")
                self._flush_buf_to_line()
            return
        if tag == "pre":
            self.in_pre = True
            self.pre_lines = []
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.heading_level = int(tag[1])
            return
        if tag in ("p", "li", "dt", "dd"):
            # nested <p> inside a table cell is handled by the cell buffer
            return
        if tag in ("ul", "ol"):
            self.list_stack.append({"ordered": tag == "ol", "index": 0})
            return
        if tag == "table":
            self.table_rows = []
            return
        if tag == "tr":
            self.cur_row = []
            return
        if tag in ("td", "th"):
            self.cell = []
            return
        if tag in ("strong", "b"):
            (self.cell if self.cell is not None else self.buf).append("**")
            return
        if tag in ("em", "i"):
            (self.cell if self.cell is not None else self.buf).append("*")
            return
        if tag == "code":
            (self.cell if self.cell is not None else self.buf).append("`")
            return
        if tag == "br":
            self.buf.append(" ")
            return

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_pre = False
            code = "".join(self.pre_lines).rstrip("\n")
            self._emit_anchor()
            self.lines.append("```")
            self.lines.extend(code.split("\n"))
            self.lines.append("```")
            self._emit()
            self.pre_lines = []
            return
        if self.in_pre:
            return
        if tag == "div":
            if self.div_classes:
                self.div_classes.pop()
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = self._flush_buf()
            if text:
                level = self.heading_level or 1
                # h5 is used for figure/table/listing captions and callouts
                self._emit_anchor()
                self.lines.append("#" * level + " " + text)
                self._emit()
            self.heading_level = None
            return
        if tag == "p":
            if self.cell is not None:
                text = self._flush_buf()
                if text and self.cell and self.cell[-1] not in ("", " "):
                    self.cell.append(" / ")
                self.cell.append(text)
                return
            text = self._flush_buf()
            if text:
                self._emit_anchor()
                self.lines.append(text)
                self._emit()
            return
        if tag == "li":
            text = self._flush_buf()
            if text and self.list_stack:
                frame = self.list_stack[-1]
                frame["index"] += 1
                marker = f"{frame['index']}. " if frame["ordered"] else "- "
                self._emit_anchor()
                self.lines.append(marker + text)
            return
        if tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
                self._emit()
            return
        if tag in ("td", "th"):
            if self.cell is not None:
                extra = self._flush_buf()
                if extra:
                    self.cell.append(" " + extra)
                text = re.sub(r"\s+", " ", "".join(self.cell)).strip()
                if self.cur_row is not None:
                    self.cur_row.append(text)
                self.cell = None
            return
        if tag == "tr":
            if self.cur_row is not None and self.table_rows is not None:
                self.table_rows.append(self.cur_row)
            self.cur_row = None
            return
        if tag == "table":
            self._emit_anchor()
            self.lines.extend(self._render_table(self.table_rows or []))
            self._emit()
            self.table_rows = None
            return
        if tag in ("strong", "b"):
            self._close_inline("**")
            return
        if tag in ("em", "i"):
            self._close_inline("*")
            return
        if tag == "code":
            self._close_inline("`")
            return

    def _close_inline(self, marker: str) -> None:
        (self.cell if self.cell is not None else self.buf).append(marker)

    def _flush_buf_to_line(self) -> None:
        text = self._flush_buf()
        if text:
            self.lines.append(text)
            self._emit()

    @staticmethod
    def _render_table(rows: list[list[str]]) -> list[str]:
        if not rows:
            return []
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = ["| " + " | ".join(rows[0]) + " |",
               "|" + "|".join(["---"] * width) + "|"]
        for r in rows[1:]:
            out.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
        return out

    def handle_data(self, data):
        if self.in_pre:
            self.pre_lines.append(data)
            return
        if self.heading_level is not None:
            self.buf.append(data)
        elif self.cell is not None:
            self.cell.append(data)
        elif self.list_stack:
            self.buf.append(data)
        else:
            self.buf.append(data)

    def result(self) -> str:
        return "\n".join(self.lines).strip() + "\n"


def convert(html_text: str) -> str:
    parser = Extractor()
    parser.feed(html_text)
    return parser.result()


def find_epub(explicit: str | None) -> str:
    if explicit:
        return explicit
    matches = glob.glob(os.path.join(REPO, "*.epub"))
    if not matches:
        raise SystemExit("No .epub found in the repository root")
    return matches[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--epub", default=None)
    ap.add_argument("--out", default=os.path.join(REPO, "review"))
    args = ap.parse_args()

    epub = find_epub(args.epub)
    out_en = os.path.join(args.out, "en")
    os.makedirs(out_en, exist_ok=True)

    manifest = []
    with zipfile.ZipFile(epub) as z:
        for name in sorted(z.namelist()):
            if not name.startswith("OEBPS/Text/"):
                continue
            stem = os.path.splitext(os.path.basename(name))[0]
            if stem not in CN_MAP:
                continue
            md = convert(z.read(name).decode("utf-8", errors="replace"))
            path = os.path.join(out_en, stem + ".md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(md)
            cn = CN_MAP[stem]
            manifest.append({
                "en_name": stem,
                "en_path": os.path.relpath(path, REPO),
                "cn_path": cn,
                "en_chars": len(md),
                "cn_chars": len(open(os.path.join(REPO, cn), encoding="utf-8").read())
                if os.path.exists(os.path.join(REPO, cn)) else 0,
            })

    with open(os.path.join(out_en, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    for m in manifest:
        ratio = (m["cn_chars"] / m["en_chars"]) if m["en_chars"] else 0
        print(f"{m['en_name']:18s} en={m['en_chars']:7d} cn={m['cn_chars']:7d} "
              f"ratio={ratio:.2f}  -> {m['cn_path']}")


if __name__ == "__main__":
    main()
