"""
Фрагмент начала текста новости для ленты (главная, индекс) и служебного поля title.
На публичных страницах отдельный заголовок не показываем — только тело и этот фрагмент в списках.
"""

from __future__ import annotations


def first_paragraph_one_line(text: str) -> str:
    """Первый абзац: до пустой строки; внутри абзаца — первая непустая строка, склеенная в одну линию для превью."""
    t = (text or "").strip()
    if not t:
        return ""
    block = t.split("\n\n", 1)[0].strip()
    line = block.split("\n", 1)[0].strip()
    core = line or block
    return " ".join(core.split())


def excerpt_for_list(text: str, *, max_chars: int = 200) -> str:
    """Первый абзац, до max_chars (для блока новостей на главной и индекса сайта)."""
    para = first_paragraph_one_line(text)
    if not para:
        return ""
    if len(para) <= max_chars:
        return para
    cut = para[: max_chars - 1]
    sp = cut.rfind(" ")
    if sp > int(max_chars * 0.55):
        cut = cut[:sp]
    return cut.rstrip() + "…"


def title_fallback_from_site_text(site_text: str, *, max_chars: int = 100) -> str:
    """Короткая строка для поля ContentItem.title и индекса (без отдельного заголовка на сайте)."""
    ex = excerpt_for_list(site_text, max_chars=max_chars)
    if ex:
        return ex.replace("\n", " ").strip()
    return "Новость"
