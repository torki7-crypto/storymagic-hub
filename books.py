import base64
import html
import io
import uuid
from typing import List, Optional

import httpx
from ebooklib import epub
from pydantic import BaseModel, Field
from weasyprint import HTML

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
    hero_name: Optional[str] = ""
    description: Optional[str] = Field(default="")
    dedication_text: Optional[str] = Field(default="")
    psychology_questions: List[str] = Field(default_factory=list)
    chapters: List[ChapterData]
    is_paid: Optional[bool] = False


# ---------- texts shared by ePub and print ----------

PSYCH = {
    "en": ("💡 Questions for a Heart-to-Heart Talk", "These questions will help discuss the story with your child and reinforce its therapeutic effect:"),
    "ru": ("💡 Вопросы для душевного разговора", "Эти вопросы помогут обсудить сказку с ребенком и закрепить ее терапевтический эффект:"),
    "pl": ("💡 Pytania do szczerej rozmowy", "Te pytania pomogą omówić bajkę z dzieckiem i utrwalić jej efekt terapeutyczny:"),
    "ro": ("💡 Întrebări pentru o discuție sinceră", "Aceste întrebări vă vor ajuta să discutați povestea cu copilul și să-i consolidați efectul terapeutic:"),
    "it": ("💡 Domande per una conversazione intima", "Queste domande aiuteranno a discutere la fiaba con il bambino e a consolidare il suo effetto terapeutico:"),
    "tr": ("💡 İçten Bir Sohbet İçin Sorular", "Bu sorular, masalı çocuğunuzla tartışmanıza ve terapötik etkisini pekiştirmenize yardımcı olacaktır:"),
}

LABELS = {
    "en": {"end": "The End", "made": "Made with love by SuperMeStory", "for": "A story for {name}",
           "draw": "Draw your favourite moment from the story", "love": "With love"},
    "ru": {"end": "Конец", "made": "Сделано с любовью в SuperMeStory", "for": "Сказка для {name}",
           "draw": "Нарисуй свой любимый момент из сказки", "love": "С любовью"},
    "pl": {"end": "Koniec", "made": "Stworzone z miłością w SuperMeStory", "for": "Bajka dla {name}",
           "draw": "Narysuj swój ulubiony moment z bajki", "love": "Z miłością"},
    "ro": {"end": "Sfârșit", "made": "Făcut cu dragoste de SuperMeStory", "for": "O poveste pentru {name}",
           "draw": "Desenează momentul tău preferat din poveste", "love": "Cu drag"},
    "it": {"end": "Fine", "made": "Creato con amore da SuperMeStory", "for": "Una storia per {name}",
           "draw": "Disegna il tuo momento preferito della storia", "love": "Con amore"},
    "tr": {"end": "Son", "made": "SuperMeStory tarafından sevgiyle hazırlandı", "for": "{name} için bir masal",
           "draw": "Masalın en sevdiğin anını çiz", "love": "Sevgiyle"},
}


def _labels(lang: Optional[str]) -> dict:
    return LABELS.get((lang or "en").lower(), LABELS["en"])


def _no_emoji(s: str) -> str:
    return s.replace("💡", "").strip()


# ---------- ePub ----------

EPUB_CSS = """
body { font-family: Georgia, serif; line-height: 1.6; margin: 5%; color: #2D3748; }
h1 { color: #4A5568; font-size: 1.3em; text-align: left; margin: 0.2em 0 0.6em; border-bottom: 1px solid #E2E8F0; padding-bottom: 0.3em; }
h2 { color: #4A5568; text-align: center; margin-top: 1.5em; }
.img-container { text-align: center; margin: 1em 0; page-break-inside: avoid; }
img { max-width: 100%; max-height: 55vh; width: auto; height: auto; border-radius: 12px; }
p { text-indent: 1.2em; margin-bottom: 0.8em; font-size: 1.05em; }
.dedication-page { text-align: center; font-style: italic; margin-top: 4em; padding: 2em; background: #FAF5FF; border-radius: 12px; }
.psychology-page { background: #F0FFF4; padding: 1.5em; border-radius: 12px; margin-top: 2em; }
"""


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


# ---------- Print PDFs for Lulu: A5 saddle stitch, page count always a multiple of 4 ----------
#
# Lulu SKU 0583X0827.FC.PRE.SS.080CW444.GXX  (A5, Premium Color, saddle stitch, 80# coated, glossy)
#   interior page size incl. 0.125in bleed: 6.08 x 8.52 in
#   cover size incl. bleed (spine 0):      11.91 x 8.52 in
# The usual book is: title, dedication, 8 chapters, questions for parents, closing page = 12 pages.

PRINT_CSS = """
@page { size: 6.08in 8.52in; margin: 0.75in 0.7in 0.8in 0.7in; background: #fffdf7; }
html { font-family: 'Georgia', 'DejaVu Serif', 'Liberation Serif', serif; color: #2d3748; }
body { margin: 0; font-size: __FONT__pt; line-height: 1.42; }
section.pg { page-break-after: always; break-after: page; }
section.pg:last-child { page-break-after: auto; break-after: auto; }
h2.ch { text-align: center; font-size: __HEAD__pt; margin: 0 0 0.16in; color: #4a3b6b; line-height: 1.2; }
.pic { text-align: center; margin: 0 0 0.18in; }
.pic img { height: __IMG__in; width: auto; max-width: 100%; border-radius: 8px; }
p { margin: 0 0 0.09in; text-align: left; }
.rule { width: 0.9in; height: 0; border-top: 1.5px solid #c9b8e8; margin: 0.3in auto; }
.title { padding-top: 2.1in; text-align: center; }
.title h1 { font-size: 24pt; line-height: 1.2; margin: 0 0 0.2in; color: #4a3b6b; }
.title .sub { text-align: center; font-size: 13pt; font-style: italic; color: #6b5b8c; }
.title .brand { text-align: center; font-size: 11pt; letter-spacing: 0.12em; color: #8d7fae; }
.ded { padding-top: 2.4in; text-align: center; }
.ded p { text-align: center; font-style: italic; font-size: 13pt; line-height: 1.5; }
.qs h2 { font-size: 14pt; text-align: center; color: #4a3b6b; margin: 0 0 0.15in; }
.qs .intro { font-size: 10pt; font-style: italic; color: #5a6475; }
.qs ol { margin: 0.1in 0 0 0.2in; padding: 0; font-size: 11pt; }
.qs li { margin-bottom: 0.16in; }
.draw { text-align: center; }
.draw p { text-align: center; font-style: italic; font-size: 12pt; color: #6b5b8c; }
.draw .box { height: 5.4in; border: 1.5px dashed #b8a9d9; border-radius: 12px; margin-top: 0.25in; }
.end { padding-top: 2.7in; text-align: center; }
.end h2 { font-size: 22pt; color: #4a3b6b; margin: 0; }
.end p { text-align: center; font-size: 10.5pt; color: #8d7fae; }
.blank { height: 1px; }
"""

# (illustration height in inches, body font in pt): the first step that keeps the book on its page plan wins
PRINT_STEPS = ((3.5, 10.5), (3.2, 10.0), (2.9, 9.5), (2.6, 9.0), (2.3, 8.5), (2.0, 8.0))


def inline_image(url: Optional[str]) -> str:
    """Only inline data: images are allowed in PDFs, never files or network URLs."""
    return url if (url or "").startswith("data:image") else ""


def _build_print_html(story: StoryBookData, img_h: float, font_pt: float, extra_blank: int = 0):
    """Returns (html, planned_page_count)."""
    L = _labels(story.language_code)
    lang = (story.language_code or "en").lower()
    hero = (story.hero_name or "").strip()
    secs: List[str] = []

    sub = f'<p class="sub">{esc(L["for"].format(name=hero))}</p>' if hero else ""
    secs.append(f'<section class="pg title"><h1>{esc(story.title)}</h1>{sub}'
                '<div class="rule"></div><p class="brand">SuperMeStory</p></section>')

    ded = (story.dedication_text or "").strip()
    ded_html = esc(ded).replace("\n", "<br/>") if ded else esc(L["love"])
    secs.append(f'<section class="pg ded"><div class="rule"></div><p>{ded_html}</p><div class="rule"></div></section>')

    for ch in story.chapters:
        pic = ""
        if inline_image(ch.image_url):
            pic = f'<div class="pic"><img src="{esc(ch.image_url)}" alt="" /></div>'
        paras = "".join(f"<p>{esc(p.strip())}</p>" for p in ch.paragraphs if p.strip())
        secs.append(f'<section class="pg chapter"><h2 class="ch">{esc(ch.title)}</h2>{pic}{paras}</section>')

    if story.psychology_questions:
        head, desc = PSYCH.get(lang, PSYCH["en"])
        items = "".join(f"<li>{esc(q)}</li>" for q in story.psychology_questions)
        secs.append(f'<section class="pg qs"><h2>{esc(_no_emoji(head))}</h2><p class="intro">{esc(desc)}</p>'
                    f"<ol>{items}</ol></section>")

    draw_pages = (-(len(secs) + 1)) % 4  # +1 for the closing page
    for _ in range(draw_pages):
        secs.append(f'<section class="pg draw"><p>{esc(L["draw"])}</p><div class="box"></div></section>')

    secs.append(f'<section class="pg end"><div class="rule"></div><h2>{esc(L["end"])}</h2>'
                f'<div class="rule"></div><p>{esc(L["made"])}</p><p>supermestory.com</p></section>')
    for _ in range(extra_blank):
        secs.append('<section class="pg"><div class="blank"></div></section>')

    css = (PRINT_CSS.replace("__FONT__", str(font_pt)).replace("__HEAD__", str(round(font_pt + 4, 1)))
           .replace("__IMG__", str(img_h)))
    doc = (f'<!DOCTYPE html><html lang="{esc(lang)}"><head><meta charset="utf-8"><style>{css}</style></head>'
           f'<body>{"".join(secs)}</body></html>')
    return doc, len(secs)


def render_print_document(story: StoryBookData):
    """Renders the interior and makes sure the page count is a multiple of 4."""
    doc, n, planned, img_h, font_pt = None, 0, 0, PRINT_STEPS[-1][0], PRINT_STEPS[-1][1]
    for img_h, font_pt in PRINT_STEPS:
        doc_html, planned = _build_print_html(story, img_h, font_pt)
        doc = HTML(string=doc_html).render()
        n = len(doc.pages)
        if n <= planned:
            break  # nothing spilled onto an extra page
    if n % 4:
        doc_html, planned = _build_print_html(story, img_h, font_pt, extra_blank=(-n) % 4)
        doc = HTML(string=doc_html).render()
        n = len(doc.pages)
    print(f"Print interior: {n} pages (planned {planned}), illustrations {img_h}in, text {font_pt}pt")
    return doc, n


def generate_print_pdf_bytes(story: StoryBookData) -> bytes:
    doc, _ = render_print_document(story)
    return doc.write_pdf()


COVER_CSS = """
@page { size: 11.91in 8.52in; margin: 0; }
html { font-family: 'Georgia', 'DejaVu Serif', 'Liberation Serif', serif; }
body { margin: 0; width: 11.91in; height: 8.52in; background: #1e1533; color: #fff; position: relative; }
.half { position: absolute; top: 0; width: 5.955in; height: 8.52in; box-sizing: border-box; text-align: center; }
.back { left: 0; padding: 2.9in 0.95in 0 0.95in; }
.front { left: 5.955in; padding: 0.85in 0.8in 0 0.8in; }
.front img { width: 3.5in; height: 3.5in; object-fit: cover; border-radius: 12px; margin-top: 0.3in; }
"""


def generate_cover_pdf_bytes(story: StoryBookData) -> bytes:
    L = _labels(story.language_code)
    hero = (story.hero_name or "").strip()
    size = 22 if len(story.title) <= 30 else 18 if len(story.title) <= 55 else 15
    img = ""
    if story.chapters and inline_image(story.chapters[0].image_url):
        img = f'<img src="{esc(story.chapters[0].image_url)}" alt="" />'
    sub = (f'<p style="margin-top:0.3in;font-size:12pt;font-style:italic;color:#f1e4ff;">'
           f'{esc(L["for"].format(name=hero))}</p>') if hero else ""
    body = (
        '<div class="half back"><h3 style="font-size:16pt;color:#fecfef;margin:0 0 0.2in;">SuperMeStory</h3>'
        f'<p style="font-size:11pt;color:#cbd5e1;line-height:1.6;">{esc(L["made"])}</p>'
        '<p style="font-size:10pt;color:#94a3b8;margin-top:0.4in;">supermestory.com</p></div>'
        f'<div class="half front"><h1 style="font-size:{size}pt;line-height:1.2;color:#ff9a9e;margin:0;">'
        f'{esc(story.title)}</h1>{img}{sub}</div>'
    )
    doc = (f'<!DOCTYPE html><html lang="{esc((story.language_code or "en").lower())}"><head><meta charset="utf-8">'
           f"<style>{COVER_CSS}</style></head><body>{body}</body></html>")
    return HTML(string=doc).write_pdf()
