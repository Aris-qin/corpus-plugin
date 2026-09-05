#!/usr/bin/env python3
"""
jats_to_md.py — JATS XML → Markdown 转换器

researcher 抓全文工具链核心：把 EDirect `efetch -db pmc -format xml` 的 PMC XML 全文
转成干净 markdown，让 corpus-curator 的 V2 heading-aware chunker 拿到完整嵌套树。

为什么不用 web HTML：
- PMC 7-22 加了 Proof of Work 反爬，浏览器路径 pmc/articles/PMC... 只能拿到 1.8KB
  拦截页
- HTML 把 h4 写成 `####` 但缺 `###` 父级 → chunker 把多个 h4 坍缩成 h3_orphan
  → 同 path 多 sibling 撞 chunk_id 主键（参考 bug fix 2026-07-22）
- JATS XML 用 `<sec>` 嵌套结构，跟 heading 层级一一对应 → 转换后 tree 完整

输入: raw/<pmid>.xml (PMC EDirect efetch 输出)
输出: raw/<pmid>.md (V2 heading-aware compatible)

用法:
  python3 scripts/corpus/jats_to_md.py <input.xml> <output.md>
  python3 scripts/corpus/jats_to_md.py raw/36850500.xml raw/36850500.md

2026-07-22 立
"""
from __future__ import annotations
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

JATS = "http://www.ncbi.nlm.nih.gov/JATS1"


def _text(elem) -> str:
    """拼接 elem 内所有 text（含子元素）"""
    return "".join(elem.itertext()).strip()


def render_article_title(root) -> str:
    """找 article-title（顶级，不在 abstract/back 里）"""
    for t in root.iter("article-title"):
        return _text(t)
    return ""


def render_abstract(root) -> str:
    """abstract 段所有 <p> 拼成 markdown"""
    parts = []
    for ab in root.iter("abstract"):
        for p in ab.iter("p"):
            text = _text(p)
            if text:
                parts.append(text)
    return "\n\n".join(parts)


def render_sec(sec, level: int) -> str:
    """递归 sec 元素：title → #、子 p → 段落、嵌套 sec → level+1

    Bug fix 2026-07-22 关联：JATS XML 的 sec 嵌套是 heading 真实结构，递归 level
    能避免 HTML 路径的 h3_orphan 坍陷。
    """
    parts = []
    title_el = sec.find("title")
    if title_el is not None:
        title_text = _text(title_el)
        if title_text:
            parts.append(f'\n{"#" * level} {title_text}\n')
    for child in sec:
        tag = child.tag.split("}")[-1]  # strip namespace
        if tag == "p":
            text = _text(child)
            if text:
                parts.append(f"\n{text}\n")
        elif tag == "sec":
            parts.append(render_sec(child, level + 1))
        # 其他标签（图、表、公式）暂不处理；corpus 不依赖这些结构
    return "".join(parts)


def render_body(root) -> str:
    """body 下所有顶级 sec 拼成 markdown（顶级 sec 是 h2）"""
    body_md = ""
    for body in root.iter("body"):
        for sec in body.findall("sec"):
            body_md += render_sec(sec, 2)
    return body_md


def convert(xml_path: Path) -> str:
    """主入口：xml → markdown 字符串"""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    title = render_article_title(root)
    abstract = render_abstract(root)
    body = render_body(root)

    md = f"# {title}\n\n" if title else ""
    if abstract:
        md += f"## Abstract\n\n{abstract}\n"
    md += body
    return md


def heading_stats(md: str) -> dict:
    """统计 markdown 各级 heading 数（V2 chunker 友好性指标）"""
    counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}
    for line in md.split("\n"):
        m = re.match(r"^(#+) ", line)
        if m:
            level = len(m.group(1))
            if level in counts:
                counts[level] += 1
    return counts


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: jats_to_md.py <input.xml> <output.md>", file=sys.stderr)
        return 1

    xml_path = Path(argv[1])
    md_path = Path(argv[2])

    if not xml_path.exists():
        print(f"ERROR: {xml_path} not found", file=sys.stderr)
        return 1

    md = convert(xml_path)
    md_path.write_text(md, encoding="utf-8")

    stats = heading_stats(md)
    char_count = len(md)
    word_count = len(md.split())
    print(f"wrote {md_path} ({char_count} chars, {word_count} words)")
    print(f"  headings: {stats}")

    # sanity check: V2 chunker 需要至少 h1 才有完整 tree
    if stats[1] == 0:
        print(f"WARN: no h1 heading (article-title missing?) — V2 chunker will produce h1_orphan", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))