import base64
import html
import io
import uuid
from typing import List, Optional

import httpx
from ebooklib import epub
from pydantic import BaseModel, Field
from weasyprint import HTML, default_url_fetcher

esc = html.escape


class ChapterData(BaseModel):
    chapter_number: int
    title: str
    paragraphs: List[str]
    image_prompt: Optional[str] = ""
    image_url: Optional[str] = ""


class StoryBookData(BaseModel):
    title: str
    language_code: str = "en"
    author: str = "SuperMeStory"
    description: Optional[str] = Field(default="")
    dedication_text: Optional[str] = Field(default="")
    psychology_questions: List[str] = Field(default_factory=list)
    chapters: List[ChapterData]
    is_paid: Optional[bool] = False


# ---------- ePub ----------

EPUB_CSS = """
body { font-family: Georgia, serif; line-height: 1.6; margin: 5%; color: #2D3748; }
h1 { color: #4A5568; font-size: 1.3em; text-align: left; margin: 0.2em 0 0.6em; border-bottom: 1px solid #E2E8F0; padding-bottom: 0.3em; }
h2 { color: #4A5568; text-align: center; margin-top: 1.5em; }
.img-container { text-align: center; margin: 1.2em 0; }
img { max-width: 100%; height: auto; border-radius: 12px; }
p { text-indent: 1.2em; margin-bottom: 0.8em; font-size: 1.05em; }
.dedication-page { text-align: center; font-style: italic; margin-top: 4em; padding: 2em; background: #FAF5FF; border-radius: 12px; }
.psychology-page { background: #F0FFF4; padding: 1.5em; border-radius: 12px; margin-top: 2em; }
"""

PSYCH = {
    "en": ("💡 Questions for a Heart-to-Heart Talk", "These questions will help discuss the story with your child and reinforce its therapeutic effect:"),
    "ru": ("💡 Вопросы для душевного разговора", "Эти вопросы помогут обсудить сказку с ребенком и закрепить ее терапевтический эффект:"),
    "pl": ("💡 Pytania do szczerej rozmowy", "Te pytania pomogą omówić bajkę z dzieckiem i utrwalić jej efekt terapeutyczny:"),
    "ro": ("💡 Întrebări pentru o discuție sinceră", "Aceste întrebări vă vor ajuta să discutați povestea cu copilul și să-i consolidați efectul terapeutic:"),
    "it": ("💡 Domande per una conversazione intima", "Queste domande aiuteranno a discutere la fiaba con il bambino e a consolidare il suo effetto terapeutico:"),
    "tr": ("💡 İçten Bir Sohbet İçin Sorular", "Bu sorular, masalı çocuğunuzla tartışmanıza ve terapötik etkisini pekiştirmenize yardımcı olacaktır:"),
}


def _xhtml(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
        f'<title>{esc(title)}</title><link rel="stylesheet" href="style/main.css" type="text/css"/>'
        f"</head><body>{body}</body></html>"
    )


class EpubBuilder:
    @classmethod
    async def create_epub(cls, story: StoryBookData) -> bytes:
        book = epub.EpubBook()
        lang = (story.language_code or "en").lower()
        book.set_identifier(str(uuid.uuid4()))
        book.set_title(story.title or "SuperMeStory Book")
        book.set_language(lang)
        book.add_author(story.author)

        css = epub.EpubItem(uid="style_main", file_name="style/main.css",
                            media_type="text/css", content=EPUB_CSS.encode("utf-8"))
        book.add_item(css)
        spine, toc = ["nav"], []

        def add_page(uid: str, fname: str, title: str, body: str):
            item = epub.EpubHtml(title=title, file_name=fname, lang=lang, uid=uid)
            item.content = _xhtml(title, body).encode("utf-8")
            item.add_item(css)
            book.add_item(item)
            spine.append(item)
            toc.append(item)

        dedication = (story.dedication_text or "").strip()
        if dedication:
            add_page("dedication", "dedication.xhtml", "Dedication",
                     f'<div class="dedication-page"><h2>❤️</h2><p>{esc(dedication).replace(chr(10), "<br/>")}</p></div>')

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
            for idx, ch in enumerate(story.chapters):
                n = ch.chapter_number or (idx + 1)
                img, mime, url = None, "image/jpeg", ch.image_url or ""
                try:
                    if url.startswith("data:image"):
                        head, b64 = url.split(",", 1)
                        mime = head[5:].split(";")[0]
                        img = base64.b64decode(b64)
                    elif url.startswith("http"):
                        r = await http.get(url)
                        if r.status_code == 200:
                            img = r.content
                            mime = r.headers.get("content-type", mime).split(";")[0]
                except Exception as e:
                    print(f"ePub image error, chapter {n}: {e}")

                body = f"<h1>{esc(ch.title)}</h1>"
                if img:
                    ext = {"image/png": "png", "image/webp": "webp"}.get(mime, "jpg")
                    fname = f"images/chapter_{n}.{ext}"
                    book.add_item(epub.EpubItem(uid=f"img_{n}", file_name=fname, media_type=mime, content=img))
                    body += f'<div class="img-container"><img src="{fname}" alt=""/></div>'
                    if idx == 0 and not dedication:
                        book.set_cover(f"cover.{ext}", img)
                body += "".join(f"<p>{esc(p.strip())}</p>" for p in ch.paragraphs if p.strip())
                add_page(f"chapter_{n}", f"chapter_{n}.xhtml", ch.title, body)

        if story.psychology_questions:
            head, desc = PSYCH.get(lang, PSYCH["en"])
            items = "".join(f"<li>{esc(q)}</li>" for q in story.psychology_questions)
            add_page("psychology", "psychology_questions.xhtml", "Questions for Discussion",
                     f'<div class="psychology-page"><h2>{esc(head)}</h2><p>{esc(desc)}</p><ul>{items}</ul></div>')

        book.toc = tuple(toc)
        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = spine
        buf = io.BytesIO()
        epub.write_epub(buf, book, {})
        return buf.getvalue()


# ---------- Print PDFs (Lulu); layout is reworked for A5 in a later step ----------

def safe_fetcher(url, *args, **kwargs):
    """WeasyPrint may only load inline data: images, never files or network."""
    if url.startswith("data:"):
        return default_url_fetcher(url)
    raise ValueError("External resources are blocked")


PRINT_CSS = """
@page { size: 6.25in 9.25in; margin: 0.75in;
        @bottom-center { content: counter(page); font-family: Georgia, serif; font-size: 10pt; color: #4A5568; } }
body { font-family: Georgia, serif; font-size: 12pt; line-height: 1.6; color: #2D3748; }
.page-break { page-break-after: always; }
h1 { font-size: 16pt; color: #2D3748; margin-bottom: 0.8em; border-bottom: 1px solid #CBD5E1; padding-bottom: 0.3em; }
.img-container { text-align: center; margin: 1em 0; }
img { max-width: 100%; max-height: 3.8in; object-fit: cover; border-radius: 8px; }
p { text-indent: 1.5em; margin-bottom: 0.8em; }
.dedication-page, .psychology-page { page-break-after: always; text-align: center; padding-top: 2in; }
"""

COVER_CSS = """
@page { size: 12.25in 9.25in; margin: 0; }
body { margin: 0; font-family: Georgia, serif; background: #1e1533; color: #fff; width: 12.25in; height: 9.25in; }
.half { width: 6.125in; height: 9.25in; padding: 1in; box-sizing: border-box; text-align: center; float: left; }
"""


def generate_print_pdf_bytes(story: StoryBookData) -> bytes:
    parts = [f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>{PRINT_CSS}</style></head><body>']
    dedication = (story.dedication_text or "").strip()
    if dedication:
        parts.append('<div class="dedication-page"><h2>❤️</h2>'
                     f'<p style="font-style:italic;font-size:14pt;text-indent:0;">{esc(dedication).replace(chr(10), "<br/>")}</p></div>')
    last = len(story.chapters) - 1
    for i, ch in enumerate(story.chapters):
        brk = "" if (i == last and not story.psychology_questions) else ' class="page-break"'
        parts.append(f"<div{brk}><h1>{esc(ch.title)}</h1>")
        if ch.image_url:
            parts.append(f'<div class="img-container"><img src="{esc(ch.image_url)}" alt="" /></div>')
        parts += [f"<p>{esc(p)}</p>" for p in ch.paragraphs]
        parts.append("</div>")
    if story.psychology_questions:
        items = "".join(f"<li>{esc(q)}</li>" for q in story.psychology_questions)
        parts.append('<div class="psychology-page" style="page-break-after:avoid;"><h2>💡 Questions &amp; Discussion</h2>'
                     f'<ul style="text-align:left;display:inline-block;">{items}</ul></div>')
    parts.append("</body></html>")
    return HTML(string="".join(parts), url_fetcher=safe_fetcher).write_pdf()


def generate_cover_pdf_bytes(story: StoryBookData) -> bytes:
    img = ""
    if story.chapters and story.chapters[0].image_url:
        img = (f'<img src="{esc(story.chapters[0].image_url)}" '
               'style="width:4.2in;height:4.2in;object-fit:cover;border-radius:12px;margin-top:0.4in;" />')
    body = (
        '<div class="half"><h3 style="margin-top:2.5in;color:#fecfef;">SuperMeStory</h3>'
        '<p style="font-size:12pt;color:#cbd5e1;line-height:1.6;">A personalized therapeutic story crafted '
        'especially to inspire courage, kindness, and confidence.</p></div>'
        f'<div class="half"><h1 style="font-size:22pt;color:#ff9a9e;margin-top:0.5in;">{esc(story.title)}</h1>{img}'
        f'<p style="margin-top:0.5in;font-size:12pt;color:#f1e4ff;">{esc(story.author)}</p></div>'
    )
    doc = f'<!DOCTYPE html><html><head><meta charset="utf-8"><style>{COVER_CSS}</style></head><body>{body}</body></html>'
    return HTML(string=doc, url_fetcher=safe_fetcher).write_pdf()
