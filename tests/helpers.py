"""阶段 03 测试工具：确定性 fixture EPUB 生成与黄金文本。"""

from __future__ import annotations

import zipfile
from pathlib import Path

# (章标题, 正文) —— 含黄金专名/原句（FTS 召回测试用）
FIXTURE_CHAPTERS: list[tuple[str, str]] = [
    (
        "第一章 雨夜惊变",
        "林风在雨夜中踏入青云宗的山门，看见一块巨大的石碑，上面写着「青云不朽」四字。"
        "守门的弟子打量了他一眼，冷声道：青云宗不收来历不明之人。",
    ),
    (
        "第二章 三年之约",
        "萧炎与纳兰嫣然在乌坦城定下三年之约，围观者议论纷纷。"
        "少女回身离去时，留下一句：三年后，若你还是废物，便休要再提婚约。",
    ),
    (
        "第三章 异火榜",
        "药老提起异火榜，萧炎的目光落在青莲地心火上。"
        "异火乃是天地奇物，得之可炼万物，亦可焚尽苍穹。",
    ),
]


def make_fixture_epub(path: Path, chapters: list[tuple[str, str]], title: str = "测试小说") -> None:
    """生成一个最小合法 EPUB2（container/opf/ncx/xhtml），用于确定性测试。"""
    n = len(chapters)
    container = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        "<rootfiles>"
        '<rootfile full-path="OEBPS/content.opf"'
        ' media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    manifest = "".join(
        f'<item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(n)
    )
    spine = "".join(f'<itemref idref="ch{i}"/>' for i in range(n))
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title>"
        '<dc:identifier id="uid">fixture-001</dc:identifier>'
        "</metadata>"
        "<manifest>"
        f'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>{manifest}'
        "</manifest>"
        f'<spine toc="ncx">{spine}</spine>'
        "</package>"
    )
    nav_points = "".join(
        f'<navPoint id="n{i}" playOrder="{i + 1}"><navLabel><text>{t}</text></navLabel>'
        f'<content src="ch{i}.xhtml"/></navPoint>'
        for i, (t, _) in enumerate(chapters)
    )
    ncx = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f"<navMap>{nav_points}</navMap></ncx>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/toc.ncx", ncx)
        for i, (t, body) in enumerate(chapters):
            xhtml = (
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                f"<h1>{t}</h1><p>{body}</p>"
                "</body></html>"
            )
            z.writestr(f"OEBPS/ch{i}.xhtml", xhtml)
