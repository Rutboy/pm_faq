#!/usr/bin/env python3
"""Generate checked-in static article pages from the PM FAQ source document.

The script intentionally uses only the Python standard library. The resulting
HTML has no runtime dependency on Telegram, Markdown renderers, or a build step.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


SITE_URL = "https://rutboy.github.io/pm_faq"
SPLIT_POSTS = {13: 14, 18: 19, 52: 53, 92: 93, 125: 126}
SUMMARY_HEADINGS = {
    27: "Итог первой главы",
    45: "Итог второй главы",
    62: "Итог третьей главы",
    78: "Итог четвёртой главы",
    96: "Итог пятой главы",
    111: "Итог шестой главы",
    122: "Итог седьмой главы",
}
TITLE_OVERRIDES = {129: "Яндекс Что-то"}
TELEGRAM_ONLY_IDS = {125, 129}
MONTHS_RU = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


@dataclasses.dataclass(frozen=True)
class Post:
    telegram_id: int
    title: str
    href: str
    telegram_ids: tuple[int, ...]
    chapter: str
    subchapter: str


@dataclasses.dataclass
class MarkdownSection:
    level: int
    title: str
    body: list[str]


@dataclasses.dataclass
class TelegramPost:
    telegram_id: int
    body_fragment: str
    published_at: dt.datetime


@dataclasses.dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str]
    children: list[HtmlNode | str]


def decode_js_string(value: str) -> str:
    return json.loads(f'"{value}"')


def parse_toc(index_html: str) -> list[Post]:
    toc_match = re.search(r"const tocData\s*=\s*(\[[\s\S]*?\n\s*\]);", index_html)
    if not toc_match:
        raise ValueError("Не удалось найти tocData в index.html")

    structural_title = re.compile(r'^\s*title:\s*"((?:[^"\\]|\\.)*)",?\s*$')
    old_post = re.compile(
        r'\{\s*title:\s*"((?:[^"\\]|\\.)*)",\s*'
        r'link:\s*"https://t\.me/pm_faq/(\d+)"\s*\}'
    )
    new_post = re.compile(
        r'\{\s*id:\s*(\d+),\s*title:\s*"((?:[^"\\]|\\.)*)",\s*'
        r'href:\s*"([^"]+)",\s*telegram:\s*\[([^]]+)]\s*\}'
    )

    pending_title: str | None = None
    chapter = ""
    subchapter = ""
    posts: list[Post] = []

    for line in toc_match.group(1).splitlines():
        title_match = structural_title.match(line)
        if title_match:
            pending_title = decode_js_string(title_match.group(1))
            continue

        chapter_match = re.search(r'id:\s*"chapter\d+"', line)
        if chapter_match:
            chapter = pending_title or ""
            subchapter = ""
            pending_title = None
            continue

        subchapter_match = re.search(r'id:\s*"sub\d+_\d+"', line)
        if subchapter_match:
            subchapter = pending_title or ""
            pending_title = None
            continue

        match = new_post.search(line)
        if match:
            telegram_id = int(match.group(1))
            title = decode_js_string(match.group(2))
            source_ids = tuple(
                int(value)
                for value in re.findall(r'https://t\.me/pm_faq/(\d+)', match.group(4))
            )
            posts.append(
                Post(
                    telegram_id=telegram_id,
                    title=TITLE_OVERRIDES.get(telegram_id, title),
                    href=match.group(3),
                    telegram_ids=source_ids or (telegram_id,),
                    chapter=chapter,
                    subchapter=subchapter,
                )
            )
            continue

        match = old_post.search(line)
        if match:
            title = decode_js_string(match.group(1))
            telegram_id = int(match.group(2))
            source_ids = (telegram_id,)
            if telegram_id in SPLIT_POSTS:
                source_ids += (SPLIT_POSTS[telegram_id],)
            posts.append(
                Post(
                    telegram_id=telegram_id,
                    title=TITLE_OVERRIDES.get(telegram_id, title),
                    href=f"posts/{telegram_id}/",
                    telegram_ids=source_ids,
                    chapter=chapter,
                    subchapter=subchapter,
                )
            )

    if not posts:
        raise ValueError("В tocData не найдено ни одного материала")
    if len({post.telegram_id for post in posts}) != len(posts):
        raise ValueError("В tocData обнаружены дублирующиеся Telegram ID")
    if any(not post.chapter or not post.subchapter for post in posts):
        raise ValueError("Не удалось определить главу или подраздел для части материалов")
    invalid_hrefs = [
        post.telegram_id
        for post in posts
        if post.href != f"posts/{post.telegram_id}/"
    ]
    if invalid_hrefs:
        raise ValueError(f"Некорректные локальные ссылки материалов: {invalid_hrefs}")
    source_ids = [telegram_id for post in posts for telegram_id in post.telegram_ids]
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("В tocData обнаружены дублирующиеся ссылки на Telegram-публикации")
    return posts


def strip_heading_markup(value: str) -> str:
    value = value.strip()
    while (value.startswith("**") and value.endswith("**")) or (
        value.startswith("__") and value.endswith("__")
    ):
        value = value[2:-2].strip()
    value = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~])", r"\1", value)
    return value


def normalize_title(value: str) -> str:
    value = strip_heading_markup(value).casefold().replace("ё", "е")
    value = value.translate(str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"', "—": "-", "–": "-"}))
    return re.sub(r"[^a-zа-я0-9]+", " ", value).strip()


def parse_markdown_sections(source: str) -> list[MarkdownSection]:
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
    sections: list[MarkdownSection] = []
    current: MarkdownSection | None = None

    for line in source.splitlines():
        match = heading_pattern.match(line)
        if match and len(match.group(1)) <= 3:
            if current:
                sections.append(current)
            current = MarkdownSection(
                level=len(match.group(1)),
                title=strip_heading_markup(match.group(2)),
                body=[],
            )
        elif current:
            current.body.append(line)

    if current:
        sections.append(current)
    return sections


def build_markdown_content_map(sections: list[MarkdownSection], posts: list[Post]) -> dict[int, list[str]]:
    h3_sections: dict[str, list[MarkdownSection]] = {}
    h2_sections: dict[str, MarkdownSection] = {}

    for index, section in enumerate(sections):
        if section.level == 2:
            h2_sections[normalize_title(section.title)] = section
        if section.level != 3:
            continue

        body = list(section.body)
        if index > 0 and sections[index - 1].level == 2 and any(
            line.strip() for line in sections[index - 1].body
        ):
            body = list(sections[index - 1].body) + [""] + body
        enriched = MarkdownSection(level=3, title=section.title, body=body)
        h3_sections.setdefault(normalize_title(section.title), []).append(enriched)

    result: dict[int, list[str]] = {}
    used_h3: set[int] = set()

    for post in posts:
        if post.telegram_id in TELEGRAM_ONLY_IDS:
            continue
        if post.telegram_id in SUMMARY_HEADINGS:
            summary_key = normalize_title(SUMMARY_HEADINGS[post.telegram_id])
            section = h2_sections.get(summary_key)
            if not section:
                raise ValueError(f"В Markdown не найден раздел «{SUMMARY_HEADINGS[post.telegram_id]}»")
            result[post.telegram_id] = list(section.body)
            continue

        candidates = h3_sections.get(normalize_title(post.title), [])
        if len(candidates) != 1:
            raise ValueError(
                f"Для материала {post.telegram_id} «{post.title}» найдено разделов Markdown: {len(candidates)}"
            )
        section = candidates[0]
        used_h3.add(id(section))
        body = list(section.body)

        if post.telegram_id == 74:
            continuation = h3_sections.get(normalize_title("Как понять, что гипотеза прошла?"), [])
            if len(continuation) != 1:
                raise ValueError("Не найдена вторая часть Markdown-раздела для материала 74")
            used_h3.add(id(continuation[0]))
            body += [""] + continuation[0].body

        result[post.telegram_id] = body

    expected_count = len(posts) - len({post.telegram_id for post in posts} & TELEGRAM_ONLY_IDS)
    if len(result) != expected_count:
        raise ValueError(
            f"Ожидалось {expected_count} материалов из Markdown, сопоставлено {len(result)}"
        )

    all_h3 = {
        id(section): section.title
        for candidates in h3_sections.values()
        for section in candidates
    }
    unused_h3 = sorted(title for section_id, title in all_h3.items() if section_id not in used_h3)
    if unused_h3:
        preview = "; ".join(unused_h3[:5])
        suffix = "…" if len(unused_h3) > 5 else ""
        raise ValueError(f"В Markdown остались несопоставленные материалы: {preview}{suffix}")
    return result


def safe_href(raw_href: str, current_id: int) -> tuple[str, bool]:
    href = html.unescape(raw_href).strip()
    parsed = urlparse(href)

    if parsed.netloc in {"www.google.com", "google.com"} and parsed.path == "/url":
        href = unquote(parse_qs(parsed.query).get("q", [href])[0])
        parsed = urlparse(href)

    if parsed.netloc in {"t.me", "telegram.me"}:
        if parsed.path.rstrip("/") in {"/pm_faq_bot/contents", "/pm_faq_bot"}:
            return "../../", False
        post_match = re.fullmatch(r"/pm_faq/(\d+)/?", parsed.path)
        if post_match:
            return f"../{int(post_match.group(1))}/", False

    if parsed.scheme in {"http", "https", "mailto"}:
        return href, True
    if not parsed.scheme and not parsed.netloc and not href.lower().startswith(("javascript:", "data:")):
        return href, False
    return "#", False


def find_unescaped(value: str, needle: str, start: int) -> int:
    position = start
    while True:
        position = value.find(needle, position)
        if position < 0:
            return -1
        slash_count = 0
        cursor = position - 1
        while cursor >= 0 and value[cursor] == "\\":
            slash_count += 1
            cursor -= 1
        if slash_count % 2 == 0:
            return position
        position += len(needle)


def render_inline(value: str, current_id: int) -> str:
    output: list[str] = []
    index = 0

    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            output.append(html.escape(value[index + 1]))
            index += 2
            continue

        if value.startswith("**", index):
            end = find_unescaped(value, "**", index + 2)
            if end >= 0:
                output.append(f"<strong>{render_inline(value[index + 2:end], current_id)}</strong>")
                index = end + 2
                continue

        if value[index] == "*":
            end = find_unescaped(value, "*", index + 1)
            if end >= 0:
                output.append(f"<em>{render_inline(value[index + 1:end], current_id)}</em>")
                index = end + 1
                continue

        if value[index] == "[":
            label_end = find_unescaped(value, "](", index + 1)
            if label_end >= 0:
                href_end = find_unescaped(value, ")", label_end + 2)
                if href_end >= 0:
                    label = render_inline(value[index + 1:label_end], current_id)
                    href, external = safe_href(value[label_end + 2:href_end], current_id)
                    attributes = f'href="{html.escape(href, quote=True)}"'
                    if external:
                        attributes += ' target="_blank" rel="noopener noreferrer"'
                    output.append(f"<a {attributes}>{label}</a>")
                    index = href_end + 1
                    continue

        output.append(html.escape(value[index]))
        index += 1

    return "".join(output)


def is_list_line(line: str) -> bool:
    return bool(re.match(r"^(\s*)[*+-]\s+", line) or re.match(r"^(\s*)\d+[.)]\s+", line))


def render_list(lines: list[str], current_id: int) -> str:
    items: list[dict[str, object]] = []
    for line in lines:
        unordered = re.match(r"^(\s*)[*+-]\s+(.+?)\s*$", line)
        ordered = re.match(r"^(\s*)(\d+)[.)]\s+(.+?)\s*$", line)
        if unordered:
            items.append(
                {
                    "indent": len(unordered.group(1).expandtabs(2)),
                    "kind": "ul",
                    "number": None,
                    "content": unordered.group(2),
                    "details": [],
                }
            )
        elif ordered:
            items.append(
                {
                    "indent": len(ordered.group(1).expandtabs(2)),
                    "kind": "ol",
                    "number": int(ordered.group(2)),
                    "content": ordered.group(3),
                    "details": [],
                }
            )
        elif items and line[:1].isspace():
            details = items[-1]["details"]
            assert isinstance(details, list)
            details.append(line.strip())

    output: list[str] = []
    stack: list[dict[str, int | str | bool]] = []

    for item in items:
        indent = int(item["indent"])
        kind = str(item["kind"])
        number = item["number"]
        content = str(item["content"])
        details = item["details"]
        assert number is None or isinstance(number, int)
        assert isinstance(details, list)
        while stack and indent < int(stack[-1]["indent"]):
            output.append(f"</li></{stack[-1]['kind']}>")
            stack.pop()

        if stack and indent == int(stack[-1]["indent"]) and kind != stack[-1]["kind"]:
            output.append(f"</li></{stack[-1]['kind']}>")
            stack.pop()

        if not stack or indent > int(stack[-1]["indent"]):
            start = f' start="{number}"' if kind == "ol" and number not in {None, 1} else ""
            output.append(f"<{kind}{start}>")
            stack.append({"indent": indent, "kind": kind})
        else:
            output.append("</li>")

        output.append(f"<li>{render_inline(content.rstrip(), current_id)}")
        output.extend(
            f'<p class="list-detail">{render_inline(str(detail), current_id)}</p>'
            for detail in details
        )

    while stack:
        output.append(f"</li></{stack[-1]['kind']}>")
        stack.pop()
    return "".join(output)


def split_table_row(line: str) -> list[str]:
    value = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", value)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def render_markdown(lines: list[str], current_id: int) -> str:
    output: list[str] = []
    index = 0
    source_heading_levels = [
        len(match.group(1))
        for line in lines
        if (match := re.match(r"^(#{4,6})\s+", line))
    ]
    base_heading_level = min(source_heading_levels, default=4)

    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        heading = re.match(r"^(#{4,6})\s+(.+?)\s*$", line)
        if heading:
            level = min(3, 2 + len(heading.group(1)) - base_heading_level)
            title = strip_heading_markup(heading.group(2))
            output.append(f"<h{level}>{render_inline(title, current_id)}</h{level}>")
            index += 1
            continue

        if (
            line.lstrip().startswith("|")
            and index + 1 < len(lines)
            and re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1])
        ):
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and lines[index].lstrip().startswith("|"):
                table_lines.append(lines[index])
                index += 1
            headers = split_table_row(table_lines[0])
            rows = [split_table_row(row) for row in table_lines[2:]]
            output.append("<table><thead><tr>")
            output.extend(f"<th>{render_inline(cell, current_id)}</th>" for cell in headers)
            output.append("</tr></thead><tbody>")
            for row in rows:
                output.append("<tr>")
                output.extend(f"<td>{render_inline(cell, current_id)}</td>" for cell in row)
                output.append("</tr>")
            output.append("</tbody></table>")
            continue

        if is_list_line(line):
            list_lines: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if is_list_line(candidate) or (candidate.strip() and candidate[:1].isspace()):
                    list_lines.append(candidate)
                    index += 1
                    continue
                break
            output.append(render_list(list_lines, current_id))
            continue

        if line.lstrip().startswith(">"):
            quote_lines: list[str] = []
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                quote_lines.append(re.sub(r"^\s*>\s?", "", lines[index]))
                index += 1
            output.append(f"<blockquote><p>{render_inline(' '.join(quote_lines), current_id)}</p></blockquote>")
            continue

        paragraph_lines = [line.rstrip()]
        hard_boundary = line.endswith("  ")
        index += 1
        while not hard_boundary and index < len(lines):
            candidate = lines[index]
            if (
                not candidate.strip()
                or re.match(r"^#{4,6}\s+", candidate)
                or is_list_line(candidate)
                or candidate.lstrip().startswith((">", "|"))
            ):
                break
            paragraph_lines.append(candidate.rstrip())
            hard_boundary = candidate.endswith("  ")
            index += 1
        paragraph = " ".join(part.strip() for part in paragraph_lines if part.strip())
        output.append(f"<p>{render_inline(paragraph, current_id)}</p>")

    rendered = "\n        ".join(output).strip()
    if not rendered:
        raise ValueError(f"Пустое Markdown-содержимое для материала {current_id}")
    return rendered


class FragmentParser(HTMLParser):
    VOID_TAGS = {"br", "img", "hr", "meta", "link", "input"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("root", {}, [])
        self.stack = [self.root]
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_attrs = [(key.lower(), value or "") for key, value in attrs]
        attr_names = [key for key, _ in normalized_attrs]
        if len(attr_names) != len(set(attr_names)):
            self.errors.append(f"Повторяющийся атрибут у <{tag.lower()}>")
        node = HtmlNode(tag.lower(), dict(normalized_attrs), [])
        self.stack[-1].children.append(node)
        if tag.lower() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for position in range(len(self.stack) - 1, 0, -1):
            if self.stack[position].tag == tag.lower():
                if position != len(self.stack) - 1:
                    self.errors.append(f"Нарушена вложенность перед </{tag.lower()}>")
                del self.stack[position:]
                return
        self.errors.append(f"Закрывающий тег </{tag.lower()}> без открывающего")

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def load_telegram_post(telegram_dir: Path, telegram_id: int) -> TelegramPost:
    source_path = telegram_dir / f"pmfaq-{telegram_id}.html"
    if not source_path.exists():
        raise FileNotFoundError(f"Не найден сохранённый Telegram HTML: {source_path}")
    source = source_path.read_text(encoding="utf-8", errors="replace")
    if f'data-post="pm_faq/{telegram_id}"' not in source:
        raise ValueError(f"Telegram HTML {source_path} не соответствует посту {telegram_id}")

    body_match = re.search(
        r'<div class="tgme_widget_message_text js-message_text"[^>]*>([\s\S]*?)</div>',
        source,
    )
    if not body_match:
        raise ValueError(f"Не найден основной текст Telegram-поста {telegram_id}")
    time_match = re.search(r'<time datetime="([^"]+)"', source)
    if not time_match:
        raise ValueError(f"Не найдена дата Telegram-поста {telegram_id}")
    published_at = dt.datetime.fromisoformat(time_match.group(1).replace("Z", "+00:00"))
    return TelegramPost(telegram_id, body_match.group(1), published_at)


def wrap_inline(value: str, wrappers: tuple[tuple[str, str], ...]) -> str:
    rendered = html.escape(value)
    for opening, closing in reversed(wrappers):
        rendered = f"{opening}{rendered}{closing}"
    return rendered


def telegram_lines(fragment: str, current_id: int) -> list[tuple[str, str]]:
    parser = FragmentParser()
    parser.feed(fragment)
    lines: list[tuple[list[str], list[str]]] = [([], [])]

    def new_line() -> None:
        lines.append(([], []))

    def walk(node: HtmlNode | str, wrappers: tuple[tuple[str, str], ...] = ()) -> None:
        if isinstance(node, str):
            if node:
                lines[-1][0].append(wrap_inline(node, wrappers))
                lines[-1][1].append(node)
            return
        if node.tag == "br":
            new_line()
            return

        next_wrappers = wrappers
        if node.tag in {"b", "strong"} and not any(opening == "<strong>" for opening, _ in wrappers):
            next_wrappers += (("<strong>", "</strong>"),)
        elif node.tag in {"i", "em"} and "emoji" not in node.attrs.get("class", "").split():
            if not any(opening == "<em>" for opening, _ in wrappers):
                next_wrappers += (("<em>", "</em>"),)
        elif node.tag == "a":
            href, external = safe_href(node.attrs.get("href", ""), current_id)
            opening = f'<a href="{html.escape(href, quote=True)}"'
            if external:
                opening += ' target="_blank" rel="noopener noreferrer"'
            opening += ">"
            next_wrappers += ((opening, "</a>"),)

        for child in node.children:
            walk(child, next_wrappers)

    for child in parser.root.children:
        walk(child)
    return [("".join(parts).strip(), "".join(text).strip()) for parts, text in lines]


def render_telegram_fragments(fragments: list[str], title: str, current_id: int) -> str:
    all_lines: list[tuple[str, str]] = []
    for fragment in fragments:
        lines = telegram_lines(fragment, current_id)
        if all_lines and all_lines[-1][1]:
            all_lines.append(("", ""))
        all_lines.extend(lines)

    title_key = normalize_title(title)
    cleaned: list[tuple[str, str]] = []
    for rendered, plain in all_lines:
        without_part = re.sub(r"\s*\[[12]/2]\s*$", "", plain).strip()
        normalized = normalize_title(without_part)
        if normalized == title_key or re.match(r"^Глава\s+\d+:", plain, re.IGNORECASE):
            continue
        if plain.startswith("#") and "@pm_faq" in plain:
            continue
        cleaned.append((rendered, plain))

    output: list[str] = []
    index = 0
    while index < len(cleaned):
        if not cleaned[index][1]:
            index += 1
            continue

        group: list[tuple[str, str]] = []
        while index < len(cleaned) and cleaned[index][1]:
            group.append(cleaned[index])
            index += 1

        cursor = 0
        while cursor < len(group):
            rendered, plain = group[cursor]
            if re.fullmatch(r"(?:<strong>[\s\S]*?</strong>)+[»”\"']?", rendered):
                output.append(f"<h2>{html.escape(plain)}</h2>")
                cursor += 1
                continue
            if re.match(r"^\s*[•·]\s+", plain):
                items: list[str] = []
                while cursor < len(group) and re.match(r"^\s*[•·]\s+", group[cursor][1]):
                    item_html = re.sub(r"^\s*[•·]\s+", "", group[cursor][0], count=1)
                    items.append(f"<li>{item_html}</li>")
                    cursor += 1
                output.append(f"<ul>{''.join(items)}</ul>")
                continue
            if re.match(r"^\s*\d+[.)]\s+", plain):
                items = []
                while cursor < len(group) and re.match(r"^\s*\d+[.)]\s+", group[cursor][1]):
                    item_html = re.sub(r"^\s*\d+[.)]\s+", "", group[cursor][0], count=1)
                    items.append(f"<li>{item_html}</li>")
                    cursor += 1
                output.append(f"<ol>{''.join(items)}</ol>")
                continue

            normal_lines: list[tuple[str, str]] = []
            while cursor < len(group) and not re.match(r"^\s*(?:[•·]|\d+[.)])\s+", group[cursor][1]):
                normal_lines.append(group[cursor])
                cursor += 1
            if len(normal_lines) == 1 and re.fullmatch(r"<strong>[\s\S]+</strong>", normal_lines[0][0]):
                heading = re.sub(r"^<strong>|</strong>$", "", normal_lines[0][0])
                output.append(f"<h2>{heading}</h2>")
            else:
                output.append(f"<p>{'<br>'.join(item[0] for item in normal_lines)}</p>")

    rendered = "\n        ".join(output).strip()
    if not rendered:
        raise ValueError(f"Пустое Telegram-содержимое для материала {current_id}")
    return rendered


def validate_extra_article(article_html: str, current_id: int, source: Path) -> None:
    parser = FragmentParser()
    parser.feed(article_html)
    parser.close()
    if len(parser.stack) != 1:
        parser.errors.append(f"Не закрыт тег <{parser.stack[-1].tag}>")
    if parser.errors:
        raise ValueError(f"Некорректный HTML в {source}: {'; '.join(parser.errors)}")
    allowed_tags = {
        "p",
        "h2",
        "h3",
        "ul",
        "ol",
        "li",
        "strong",
        "em",
        "a",
        "br",
        "blockquote",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
    }
    allowed_attrs = {"a": {"href", "target", "rel"}, "p": {"class"}, "ol": {"start"}}

    def walk(node: HtmlNode | str) -> None:
        if isinstance(node, str):
            return
        if node.tag not in allowed_tags:
            raise ValueError(f"Недопустимый тег <{node.tag}> в {source}")
        unexpected_attrs = set(node.attrs) - allowed_attrs.get(node.tag, set())
        if unexpected_attrs:
            raise ValueError(
                f"Недопустимые атрибуты {sorted(unexpected_attrs)} у <{node.tag}> в {source}"
            )
        if node.tag == "p" and node.attrs.get("class") not in {None, "", "list-detail"}:
            raise ValueError(f"Недопустимый class у <p> в {source}")
        if node.tag == "ol" and node.attrs.get("start", "1").isdigit() is False:
            raise ValueError(f"Некорректный start у <ol> в {source}")
        if node.tag == "a":
            href = node.attrs.get("href", "")
            safe, external = safe_href(href, current_id)
            if not href or safe != href:
                raise ValueError(f"Небезопасная ссылка {href!r} в {source}")
            if external:
                rel = set(node.attrs.get("rel", "").split())
                if node.attrs.get("target") != "_blank" or not {"noopener", "noreferrer"} <= rel:
                    raise ValueError(f"Внешняя ссылка без безопасных атрибутов в {source}")
            elif node.attrs.get("target") or node.attrs.get("rel"):
                raise ValueError(f"Лишние атрибуты у локальной ссылки в {source}")
        for child in node.children:
            walk(child)

    for child in parser.root.children:
        walk(child)


def write_telegram_snapshot(
    metadata_path: Path,
    extras_dir: Path,
    telegram: dict[int, TelegramPost],
    extra_articles: dict[int, str],
    site_last_modified: str,
) -> None:
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": 1,
        "channel": "https://t.me/pm_faq",
        "siteLastModified": site_last_modified,
        "telegramPosts": {
            str(telegram_id): {"publishedAt": post.published_at.isoformat()}
            for telegram_id, post in sorted(telegram.items())
        },
    }
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    extras_dir.mkdir(parents=True, exist_ok=True)
    for telegram_id, article_html in sorted(extra_articles.items()):
        source_path = extras_dir / f"{telegram_id}.html"
        validate_extra_article(article_html, telegram_id, source_path)
        source_path.write_text(
            article_html.strip() + "\n",
            encoding="utf-8",
        )


def load_telegram_metadata(
    metadata_path: Path,
    expected_ids: set[int],
) -> dict[int, TelegramPost]:
    if not metadata_path.exists():
        raise FileNotFoundError(
            f"Не найден файл дат Telegram-публикаций: {metadata_path}. "
            "Передайте --telegram-dir, чтобы создать его."
        )
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    raw_posts = payload.get("telegramPosts")
    if not isinstance(raw_posts, dict):
        raise ValueError(f"Некорректный формат метаданных: {metadata_path}")

    missing_ids = expected_ids - {int(value) for value in raw_posts}
    if missing_ids:
        raise ValueError(f"В метаданных отсутствуют Telegram ID: {sorted(missing_ids)}")

    telegram: dict[int, TelegramPost] = {}
    for telegram_id in sorted(expected_ids):
        entry = raw_posts.get(str(telegram_id))
        if not isinstance(entry, dict) or not isinstance(entry.get("publishedAt"), str):
            raise ValueError(f"Некорректная дата Telegram-публикации {telegram_id}")
        published_at = dt.datetime.fromisoformat(entry["publishedAt"].replace("Z", "+00:00"))
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=dt.timezone.utc)
        telegram[telegram_id] = TelegramPost(telegram_id, "", published_at)
    return telegram


def load_extra_articles(extras_dir: Path, expected_ids: set[int]) -> dict[int, str]:
    articles: dict[int, str] = {}
    for telegram_id in sorted(expected_ids):
        source_path = extras_dir / f"{telegram_id}.html"
        if not source_path.exists():
            raise FileNotFoundError(f"Не найден дополнительный материал: {source_path}")
        article_html = source_path.read_text(encoding="utf-8").strip()
        if not article_html:
            raise ValueError(f"Пустой дополнительный материал: {source_path}")
        validate_extra_article(article_html, telegram_id, source_path)
        articles[telegram_id] = article_html
    return articles


def read_site_last_modified(metadata_path: Path) -> str | None:
    if not metadata_path.exists():
        return None
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    value = payload.get("siteLastModified")
    return value if isinstance(value, str) else None


def write_site_last_modified(metadata_path: Path, lastmod: str) -> None:
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["siteLastModified"] = lastmod
    metadata_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def strip_html(value: str) -> str:
    value = re.sub(
        r"</?(?:p|h[1-6]|li|ul|ol|blockquote|table|thead|tbody|tr|th|td|br)\b[^>]*>",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def make_description(article_html: str, limit: int = 170) -> str:
    text = strip_html(article_html)
    if len(text) <= limit:
        return text
    shortened = text[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{shortened}…"


def format_date_ru(value: dt.datetime) -> str:
    return f"{value.day} {MONTHS_RU[value.month]} {value.year}"


def source_card(post: Post) -> str:
    if len(post.telegram_ids) == 1:
        links = (
            f'<a href="https://t.me/pm_faq/{post.telegram_ids[0]}" target="_blank" '
            'rel="noopener noreferrer">Открыть публикацию</a>'
        )
        copy = "Исходная публикация сохранена для контекста и обсуждения в канале."
    else:
        links = "\n              ".join(
            f'<a href="https://t.me/pm_faq/{telegram_id}" target="_blank" '
            f'rel="noopener noreferrer">Часть {part}</a>'
            for part, telegram_id in enumerate(post.telegram_ids, start=1)
        )
        copy = "Материал выходил в двух сообщениях. На этой странице обе части объединены."
    return f"""<section class="source-card" aria-labelledby="source-title">
        <div class="source-card-inner">
          <h2 id="source-title">Оригинал в Telegram</h2>
          <p>{copy}</p>
          <div class="source-links">
            {links}
          </div>
        </div>
      </section>"""


def navigation_link(post: Post | None, direction: str) -> str:
    if not post:
        return '<span class="placeholder" aria-hidden="true"></span>'
    label = "Предыдущий материал" if direction == "prev" else "Следующий материал"
    return (
        f'<a href="../{post.telegram_id}/" rel="{direction}">'
        f"<small>{label}</small><strong>{html.escape(post.title)}</strong></a>"
    )


def render_page(
    post: Post,
    article_html: str,
    telegram_posts: list[TelegramPost],
    previous_post: Post | None,
    next_post: Post | None,
    page_modified_at: dt.datetime,
) -> str:
    published_at = telegram_posts[0].published_at
    modified_at = max([page_modified_at] + [item.published_at for item in telegram_posts])
    description = make_description(article_html)
    word_count = len(re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", strip_html(article_html)))
    reading_minutes = max(1, round(word_count / 180))
    canonical = f"{SITE_URL}/posts/{post.telegram_id}/"
    title_text = f"{post.title} | PM FAQ"
    kicker = re.sub(r"^Глава\s+\d+:\s*", "", post.chapter, flags=re.IGNORECASE)

    schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": post.title,
        "description": description,
        "inLanguage": "ru-RU",
        "datePublished": published_at.isoformat(),
        "dateModified": modified_at.isoformat(),
        "mainEntityOfPage": canonical,
        "image": f"{SITE_URL}/channel-avatar.jpg",
        "author": {"@type": "Organization", "name": "PM FAQ", "url": SITE_URL + "/"},
        "publisher": {
            "@type": "Organization",
            "name": "PM FAQ",
            "url": SITE_URL + "/",
            "logo": {"@type": "ImageObject", "url": f"{SITE_URL}/channel-avatar.jpg"},
        },
        "isPartOf": {"@type": "CollectionPage", "url": SITE_URL + "/"},
    }
    schema_json = json.dumps(schema, ensure_ascii=False, indent=6).replace("</", "<\\/")
    previous_head = (
        f'  <link rel="prev" href="../{previous_post.telegram_id}/" />\n' if previous_post else ""
    )
    next_head = f'  <link rel="next" href="../{next_post.telegram_id}/" />\n' if next_post else ""

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="theme-color" content="#172235" />
  <meta name="robots" content="index, follow, max-image-preview:large" />
  <meta name="description" content="{html.escape(description, quote=True)}" />
  <link rel="canonical" href="{canonical}" />
  <link rel="icon" type="image/jpeg" href="../../channel-avatar.jpg" />
  <link rel="stylesheet" href="../../assets/post.css" />
{previous_head}{next_head}
  <meta property="og:type" content="article" />
  <meta property="og:locale" content="ru_RU" />
  <meta property="og:site_name" content="PM FAQ" />
  <meta property="og:title" content="{html.escape(post.title, quote=True)}" />
  <meta property="og:description" content="{html.escape(description, quote=True)}" />
  <meta property="og:url" content="{canonical}" />
  <meta property="og:image" content="{SITE_URL}/channel-avatar.jpg" />
  <meta property="article:published_time" content="{published_at.isoformat()}" />
  <meta property="article:modified_time" content="{modified_at.isoformat()}" />

  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="{html.escape(post.title, quote=True)}" />
  <meta name="twitter:description" content="{html.escape(description, quote=True)}" />
  <meta name="twitter:image" content="{SITE_URL}/channel-avatar.jpg" />

  <title>{html.escape(title_text)}</title>
  <script type="application/ld+json">
{schema_json}
  </script>
</head>
<body>
  <main class="post-shell">
    <header class="site-header">
      <a class="brand" href="../../" aria-label="PM FAQ — оглавление">
        <img src="../../channel-avatar.jpg" alt="" width="42" height="42" />
        <span>PM FAQ</span>
      </a>
      <a class="header-channel" href="https://t.me/pm_faq" target="_blank" rel="noopener noreferrer">
        Канал в Telegram
      </a>
    </header>

    <nav class="breadcrumbs" aria-label="Хлебные крошки">
      <a href="../../">Оглавление</a>
      <span class="separator" aria-hidden="true">/</span>
      <span>{html.escape(post.chapter)}</span>
      <span class="separator" aria-hidden="true">/</span>
      <span>{html.escape(post.subchapter)}</span>
    </nav>

    <article class="article-card">
      <header class="article-header">
        <p class="article-kicker">{html.escape(kicker)} · {html.escape(post.subchapter)}</p>
        <h1>{html.escape(post.title)}</h1>
        <p class="article-meta">
          <span><time datetime="{published_at.date().isoformat()}">{format_date_ru(published_at)}</time></span>
          <span>{reading_minutes} мин чтения</span>
        </p>
      </header>

      <div class="article-body">
        {article_html}
      </div>

      {source_card(post)}
    </article>

    <nav class="post-navigation" aria-label="Навигация между материалами">
      {navigation_link(previous_post, "prev")}
      {navigation_link(next_post, "next")}
    </nav>

    <footer class="page-footer">
      <span>PM FAQ · Честно о работе продуктового менеджера</span>
      <a href="../../">Вернуться к оглавлению</a>
    </footer>
  </main>
</body>
</html>
"""


def write_sitemap(output_root: Path, posts: list[Post], lastmod: str) -> None:
    locations = [f"{SITE_URL}/"] + [f"{SITE_URL}/posts/{post.telegram_id}/" for post in posts]
    entries = "\n".join(
        f"  <url>\n    <loc>{location}</loc>\n    <lastmod>{lastmod}</lastmod>\n  </url>"
        for location in locations
    )
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{entries}\n"
        "</urlset>\n"
    )
    (output_root / "sitemap.xml").write_text(sitemap, encoding="utf-8")


def validate_post_directories(output_root: Path, posts: list[Post]) -> None:
    posts_root = output_root / "posts"
    if not posts_root.exists():
        return
    expected_ids = {post.telegram_id for post in posts}
    existing_ids = {
        int(path.name)
        for path in posts_root.iterdir()
        if path.is_dir() and path.name.isdigit()
    }
    stale_ids = sorted(existing_ids - expected_ids)
    if stale_ids:
        raise ValueError(
            "В posts/ найдены страницы, которых нет в tocData: "
            f"{stale_ids}. Удалите их осознанно и повторите генерацию."
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--source-md",
        type=Path,
        help="Markdown-источник (по умолчанию content/pm-faq.md)",
    )
    parser.add_argument(
        "--telegram-dir",
        type=Path,
        help="Каталог pmfaq-<id>.html для обновления метаданных и дополнительных текстов",
    )
    parser.add_argument(
        "--metadata",
        type=Path,
        help="JSON с датами публикаций (по умолчанию content/post-metadata.json)",
    )
    parser.add_argument(
        "--extras-dir",
        type=Path,
        help="Каталог дополнительных HTML-текстов (по умолчанию content/extras)",
    )
    parser.add_argument(
        "--lastmod",
        help="Дата изменения контента YYYY-MM-DD; без флага берётся из metadata",
    )
    args = parser.parse_args()

    output_root = args.output_root.resolve()
    source_path = (args.source_md or output_root / "content" / "pm-faq.md").resolve()
    metadata_path = (args.metadata or output_root / "content" / "post-metadata.json").resolve()
    extras_dir = (args.extras_dir or output_root / "content" / "extras").resolve()
    lastmod = args.lastmod or read_site_last_modified(metadata_path)
    if not lastmod and args.telegram_dir:
        lastmod = dt.date.today().isoformat()
    if not lastmod:
        raise ValueError(
            "В metadata нет siteLastModified. Передайте --lastmod YYYY-MM-DD один раз."
        )
    try:
        lastmod_date = dt.date.fromisoformat(lastmod)
    except ValueError as error:
        raise ValueError("--lastmod должен быть датой в формате YYYY-MM-DD") from error
    page_modified_at = dt.datetime.combine(
        lastmod_date,
        dt.time.min,
        tzinfo=dt.timezone.utc,
    )

    index_html = (output_root / "index.html").read_text(encoding="utf-8")
    posts = parse_toc(index_html)
    validate_post_directories(output_root, posts)
    sections = parse_markdown_sections(source_path.read_text(encoding="utf-8"))
    markdown_content = build_markdown_content_map(sections, posts)

    all_source_ids = {telegram_id for post in posts for telegram_id in post.telegram_ids}
    extra_ids = {post.telegram_id for post in posts} & TELEGRAM_ONLY_IDS
    if args.telegram_dir:
        telegram_dir = args.telegram_dir.resolve()
        telegram = {
            telegram_id: load_telegram_post(telegram_dir, telegram_id)
            for telegram_id in sorted(all_source_ids)
        }
        post_by_id = {post.telegram_id: post for post in posts}
        extra_articles = {
            telegram_id: render_telegram_fragments(
                [
                    telegram[source_id].body_fragment
                    for source_id in post_by_id[telegram_id].telegram_ids
                ],
                post_by_id[telegram_id].title,
                telegram_id,
            )
            for telegram_id in sorted(extra_ids)
        }
        write_telegram_snapshot(
            metadata_path,
            extras_dir,
            telegram,
            extra_articles,
            lastmod,
        )
    else:
        telegram = load_telegram_metadata(metadata_path, all_source_ids)
        extra_articles = load_extra_articles(extras_dir, extra_ids)

    source_counts = {"markdown": 0, "telegram": 0}
    for index, post in enumerate(posts):
        if post.telegram_id in TELEGRAM_ONLY_IDS:
            article_html = extra_articles[post.telegram_id]
            source_counts["telegram"] += 1
        else:
            article_html = render_markdown(markdown_content[post.telegram_id], post.telegram_id)
            source_counts["markdown"] += 1

        if len(strip_html(article_html)) < 80:
            raise ValueError(f"Подозрительно короткий материал {post.telegram_id}")

        page = render_page(
            post=post,
            article_html=article_html,
            telegram_posts=[telegram[telegram_id] for telegram_id in post.telegram_ids],
            previous_post=posts[index - 1] if index > 0 else None,
            next_post=posts[index + 1] if index + 1 < len(posts) else None,
            page_modified_at=page_modified_at,
        )
        page_dir = output_root / "posts" / str(post.telegram_id)
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(page, encoding="utf-8")

    write_sitemap(output_root, posts, lastmod)
    if args.lastmod and not args.telegram_dir:
        write_site_last_modified(metadata_path, lastmod)
    validate_post_directories(output_root, posts)
    print(
        f"Создано {len(posts)} страниц: {source_counts['markdown']} из Markdown, "
        f"{source_counts['telegram']} из сохранённых дополнений; "
        f"источников Telegram: {len(all_source_ids)}."
    )


if __name__ == "__main__":
    main()
