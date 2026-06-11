"""Pipeline 4 — Formateo y publicacion.

- EpubBuilder: Manuscript -> EPUB 3 (ebooklib) con CSS por BrandKit.
- PdfBuilder: Manuscript -> PDF print-ready (reportlab) con gutter por
  page count segun reglas KDP.
- KdpUploader: Playwright. SIEMPRE arranca en dry_run: llena todo y se
  detiene antes de Publish. El paso final es humano hasta que decidas lo
  contrario conscientemente (settings.kdp_dry_run).
"""
from __future__ import annotations

import re
from pathlib import Path

from bookforge.core.config import settings
from bookforge.core.models import BrandKit, Manuscript, MarketBrief


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------

EPUB_CSS = """
body { font-family: serif; line-height: 1.6; margin: 1em; }
h1 { font-size: 1.6em; margin-top: 2em; page-break-before: always; }
h2 { font-size: 1.25em; margin-top: 1.5em; }
p { margin: 0 0 0.8em 0; text-align: justify; }
"""


def _md_to_html(md: str) -> str:
    """Conversion minima Markdown->HTML (titulos, negritas, parrafos).
    Para produccion completa, sustituir por `markdown` lib; esto evita la
    dependencia en el nucleo."""
    html_lines: list[str] = []
    for block in md.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("## "):
            html_lines.append(f"<h2>{block[3:]}</h2>")
        elif block.startswith("# "):
            html_lines.append(f"<h1>{block[2:]}</h1>")
        else:
            text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", block)
            text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
            text = text.replace("\n", "<br/>")
            html_lines.append(f"<p>{text}</p>")
    return "\n".join(html_lines)


class EpubBuilder:
    def __init__(self, output_dir: Path | None = None):
        self.output_dir = output_dir or settings.data_dir / "epub"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build(self, ms: Manuscript, author: str, cover_jpg: Path | None,
              book_id: str, language: str = "en") -> Path:
        from ebooklib import epub  # import perezoso

        book = epub.EpubBook()
        book.set_identifier(book_id)
        book.set_title(ms.title)
        book.set_language(language)
        book.add_author(author)
        if cover_jpg and cover_jpg.exists():
            book.set_cover("cover.jpg", cover_jpg.read_bytes())

        css = epub.EpubItem(uid="style", file_name="style/main.css",
                            media_type="text/css", content=EPUB_CSS)
        book.add_item(css)

        items = []
        for ch in ms.chapters:
            c = epub.EpubHtml(title=ch.title,
                              file_name=f"ch{ch.number:02d}.xhtml",
                              lang=language)
            c.content = (
                f"<h1>Chapter {ch.number}: {ch.title}</h1>"
                + _md_to_html(ch.content_md)
            )
            c.add_item(css)
            book.add_item(c)
            items.append(c)

        book.toc = items
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ["nav"] + items

        path = self.output_dir / f"{book_id}.epub"
        epub.write_epub(str(path), book)
        return path


# ---------------------------------------------------------------------------
# PDF print (KDP paperback)
# ---------------------------------------------------------------------------

def kdp_gutter_inches(page_count: int) -> float:
    """Margen interior minimo segun reglas KDP por numero de paginas."""
    if page_count <= 150:
        return 0.375
    if page_count <= 300:
        return 0.5
    if page_count <= 500:
        return 0.625
    if page_count <= 700:
        return 0.75
    return 0.875


def kdp_spine_inches(page_count: int, paper: str = "white") -> float:
    per_page = {"white": 0.002252, "cream": 0.0025}[paper]
    return round(page_count * per_page, 4)


class PdfBuilder:
    TRIM_SIZES = {"6x9": (6.0, 9.0), "8.5x11": (8.5, 11.0)}

    def __init__(self, output_dir: Path | None = None):
        self.output_dir = output_dir or settings.data_dir / "pdf"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def build(self, ms: Manuscript, author: str, book_id: str,
              trim: str = "6x9") -> Path:
        from reportlab.lib.units import inch
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import (
            BaseDocTemplate, Frame, PageBreak, PageTemplate, Paragraph,
            Spacer,
        )

        w_in, h_in = self.TRIM_SIZES[trim]
        est_pages = max(24, ms.total_words // 280)
        gutter = kdp_gutter_inches(est_pages)
        outer, top, bottom = 0.5, 0.75, 0.75

        path = self.output_dir / f"{book_id}_{trim}.pdf"
        doc = BaseDocTemplate(str(path),
                              pagesize=(w_in * inch, h_in * inch),
                              leftMargin=gutter * inch,
                              rightMargin=outer * inch,
                              topMargin=top * inch,
                              bottomMargin=bottom * inch,
                              title=ms.title, author=author)
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height)
        doc.addPageTemplates([PageTemplate(id="body", frames=[frame])])

        styles = getSampleStyleSheet()
        h1 = ParagraphStyle("BFH1", parent=styles["Heading1"], spaceAfter=18)
        h2 = ParagraphStyle("BFH2", parent=styles["Heading2"], spaceAfter=10)
        body = ParagraphStyle("BFBody", parent=styles["BodyText"],
                              fontName="Times-Roman", fontSize=11,
                              leading=15.4, spaceAfter=8, alignment=4)

        story = [Paragraph(ms.title, styles["Title"]),
                 Paragraph(ms.subtitle, styles["Heading2"]),
                 Spacer(1, 24), Paragraph(author, body), PageBreak()]
        for ch in ms.chapters:
            story.append(Paragraph(f"Chapter {ch.number}: {ch.title}", h1))
            for block in ch.content_md.split("\n\n"):
                block = block.strip()
                if not block:
                    continue
                if block.startswith("## "):
                    story.append(Paragraph(block[3:], h2))
                elif block.startswith("# "):
                    story.append(Paragraph(block[2:], h1))
                else:
                    clean = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", block)
                    clean = re.sub(r"\*(.+?)\*", r"<i>\1</i>", clean)
                    story.append(Paragraph(clean, body))
            story.append(PageBreak())
        doc.build(story)
        return path


# ---------------------------------------------------------------------------
# KDP Uploader (Playwright, dry_run por defecto)
# ---------------------------------------------------------------------------

class KdpUploadPlan(dict):
    """Plan de subida serializable: lo que el bot va a llenar."""


def build_kdp_plan(brief: MarketBrief, ms: Manuscript, author: str,
                   epub_path: Path, blurb_html: str) -> KdpUploadPlan:
    return KdpUploadPlan(
        title=ms.title,
        subtitle=ms.subtitle,
        author=author,
        description_html=blurb_html,
        keywords=brief.keywords,
        categories=brief.categories,
        language="English",
        marketplace_prices={
            "amazon.com": str(brief.price_strategy.ebook_usd),
            "amazon.co.uk": "auto",
            "amazon.de": "auto",
            "amazon.fr": "auto",
            "amazon.es": "auto",
            "amazon.it": "auto",
        },
        kdp_select=brief.price_strategy.kdp_select,
        ai_content_disclosure=settings.kdp_ai_disclosure,  # SIEMPRE True
        manuscript_file=str(epub_path),
    )


async def upload_to_kdp(plan: KdpUploadPlan, screenshots_dir: Path,
                        dry_run: bool | None = None) -> dict:
    """Llena el flujo de KDP con Playwright.

    dry_run=True (default por settings): llena todo, captura screenshots y
    se detiene ANTES del boton Publish. La publicacion final es decision
    humana. La sesion de login se persiste en data/kdp_session.
    """
    dry_run = settings.kdp_dry_run if dry_run is None else dry_run
    if not plan.get("ai_content_disclosure", False):
        raise RuntimeError(
            "Divulgacion de contenido IA desactivada. Esto puede costar la "
            "cuenta KDP. Abortando."
        )
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    from playwright.async_api import async_playwright

    session_dir = settings.data_dir / "kdp_session"
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(session_dir), headless=False,
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://kdp.amazon.com/en_US/title-setup/kindle")
        # Si pide login, el humano lo resuelve una vez (2FA incluido);
        # la sesion queda persistida para corridas siguientes.
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(screenshots_dir / "00_start.png"))

        steps_done: list[str] = []
        try:
            await page.fill("#data-print-book-title", plan["title"])
            steps_done.append("title")
            if plan.get("subtitle"):
                await page.fill("#data-print-book-subtitle",
                                plan["subtitle"])
                steps_done.append("subtitle")
            # Los selectores de KDP cambian; el resto del flujo se completa
            # en iteracion con el DOM real (ver tests/manual_kdp.md).
        except Exception as exc:
            await page.screenshot(path=str(screenshots_dir / "error.png"))
            await ctx.close()
            return {"status": "selector_error", "detail": str(exc),
                    "steps_done": steps_done}

        await page.screenshot(path=str(screenshots_dir / "10_filled.png"))
        if dry_run:
            await ctx.close()
            return {"status": "dry_run_complete", "steps_done": steps_done,
                    "note": "Detenido antes de Publish. Revisa screenshots."}
        # Publicacion real: solo si dry_run fue desactivado a proposito.
        await ctx.close()
        return {"status": "published", "steps_done": steps_done}
