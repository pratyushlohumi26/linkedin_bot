"""Extract supplied HTML only; completeness signals are not factual verification."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

try:
    from trafilatura import bare_extraction
except ImportError:
    bare_extraction = None


@dataclass(frozen=True)
class ExtractedArticle:
    text: str = ""
    title: str = ""
    author: str = ""
    published_at: str = ""
    canonical_url: str = ""
    method: str = "none"
    completeness: str = "unknown"
    warnings: tuple[str, ...] = ()
    needs_render: bool = False


_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
_SENTENCE = re.compile(r"[.!?。！？](?:\s|$)")
_NOISE = re.compile(
    r"(?:^|[-_\s])(?:nav|menu|sidebar|related|share|sharing|social|cookie|"
    r"comments?|advertisement|advert|breadcrumb|newsletter-signup)(?:$|[-_\s])",
    re.I,
)
_EXCERPT = re.compile(
    r"\b(?:subscribe|sign in|log in|register|become a member) to "
    r"(?:continue reading|read (?:the (?:full|rest of)|this))\b|"
    r"\b(?:read|view|unlock) the (?:full|rest of the) (?:article|story|post)\b|"
    r"\b(?:this (?:is an? |article is an? )excerpt|subscribers? only|"
    r"for (?:paid )?(?:subscribers|members) only|you(?:'ve| have) reached your .*limit)\b",
    re.I,
)
_INTERSTITIAL = re.compile(
    r"\b(?:just a moment|attention required|verify (?:that )?you are (?:a )?human|"
    r"checking your browser|security (?:check|verification)|access denied|"
    r"sorry,? you have been blocked|sign in to continue|log in to continue|"
    r"enable (?:javascript and )?cookies to continue)\b",
    re.I,
)


def _space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("max_text_chars must be a positive integer")


def _binary(text: str) -> bool:
    if "\0" in text or text.lstrip("\ufeff \r\n").startswith(
        ("%PDF-", "\x89PNG", "GIF89a", "PK\x03\x04")
    ):
        return True
    bad = sum(unicodedata.category(c) == "Cc" and c not in "\n\r\t" for c in text)
    return (bad + text.count("\ufffd")) / max(len(text), 1) > 0.02


def _prose(text: str) -> bool:
    words = _WORD.findall(text)
    # Sentence structure and lexical variety keep short dispatches, not menus.
    if len(words) >= 12 and len({w.casefold() for w in words}) >= 8:
        return bool(_SENTENCE.search(text)) and sum(map(len, words)) / max(len(text), 1) > 0.45
    cjk = len(re.findall(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text))
    return cjk >= 24 and bool(_SENTENCE.search(text))


def _clean(root: Tag) -> Tag:
    clean = BeautifulSoup(str(root), "html.parser")
    for comment in clean.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()
    for node in list(clean.find_all(True)):
        if node.parent is None:
            continue
        attributes = " ".join([str(node.get("id", "")), " ".join(node.get("class", []))])
        style = re.sub(r"\s+", "", str(node.get("style", ""))).lower()
        if (
            node.name
            in {
                "script",
                "style",
                "noscript",
                "nav",
                "footer",
                "aside",
                "form",
                "button",
                "template",
            }
            or node.has_attr("hidden")
            or node.get("aria-hidden") == "true"
            or node.get("role") in {"navigation", "banner", "contentinfo"}
            or "display:none" in style
            or "visibility:hidden" in style
            or _NOISE.search(attributes)
            or (
                node.name == "article"
                and node.find("a")
                and not _prose(node.get_text(" ", strip=True))
            )
        ):
            node.decompose()
    return clean


def _plain(root: Tag) -> str:
    def render(node):
        if isinstance(node, NavigableString):
            return re.sub(r"\s+", " ", str(node))
        if not isinstance(node, Tag):
            return ""
        if node.name in {"pre", "code"}:
            text = node.get_text().strip("\n")
            return f"\n\n{text}\n\n" if node.name == "pre" or "\n" in text else text
        text = "".join(render(child) for child in node.children)
        if node.name in {"td", "th", "cell"}:
            return text.strip() + " | "
        if node.name in {"li", "item"}:
            return "\n- " + text.strip() + "\n"
        if node.name == "br":
            return "\n"
        if node.name in {
            "p",
            "div",
            "section",
            "article",
            "main",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "head",
            "tr",
            "row",
            "caption",
            "table",
            "ul",
            "ol",
            "list",
        }:
            return "\n\n" + text.strip() + "\n\n"
        return text

    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", render(root)).strip()


def _article_roots(soup: BeautifulSoup) -> list[Tag]:
    return [
        node
        for node in soup.find_all("article")
        if not node.find_parent("article") and _prose(node.get_text(" ", strip=True))
    ]


def _container(soup: BeautifulSoup) -> tuple[Tag | None, str, bool]:
    articles = _article_roots(soup)
    if len(articles) > 1:
        return None, "", False
    roots = articles or soup.find_all("main") or soup.find_all(attrs={"role": "main"})
    candidates = []
    for root in roots:
        clean = _clean(root)
        text = _plain(clean)
        linked = sum(len(a.get_text(" ", strip=True)) for a in clean.find_all("a"))
        prose = [
            p for p in clean.find_all(["p", "blockquote"]) if _prose(p.get_text(" ", strip=True))
        ]
        if linked / max(len(text), 1) > 0.35 or not _prose(text):
            continue
        if not prose and not (root.name == "article" and clean.find(["h1", "h2"])):
            continue
        strong = bool(prose and (root.name == "article" or clean.find("h1")))
        candidates.append((clean, text, strong))
    return max(candidates, key=lambda candidate: len(candidate[1]), default=(None, "", False))


def _challenge(soup: BeautifulSoup) -> bool:
    clean, article_text, _ = _container(soup)
    substantial = clean is not None and _prose(article_text)
    title = _space(soup.title.get_text()) if soup.title else ""
    headings = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2"], limit=3))
    visible = _space(_clean(soup).get_text(" ", strip=True))
    marker = bool(
        soup.select(
            "#challenge-running, #challenge-form, #cf-challenge-running, .cf-challenge, "
            ".g-recaptcha, .h-captcha, .cf-turnstile, #captcha"
        )
    )
    for node in soup.find_all(["script", "iframe"]):
        src = str(node.get("src", "")).lower()
        if any(
            part in src
            for part in ("/cdn-cgi/challenge-platform/", "captcha", "challenges.cloudflare.com")
        ):
            marker = True
        if node.name == "script" and "_cf_chl_opt" in node.get_text():
            marker = True
    prompt = bool(_INTERSTITIAL.search(title + " " + headings))
    direct_prompt = any(
        _INTERSTITIAL.fullmatch(
            re.sub(r"\s*\|\s*Cloudflare.*$", "", label, flags=re.I).strip(".! …")
        )
        for label in [title, *(h.get_text(" ", strip=True) for h in soup.find_all("h1"))]
    )
    if direct_prompt and (
        marker or not soup.find("article") or len(_WORD.findall(article_text)) < 40
    ):
        return True
    if not substantial and (prompt or (marker and len(visible.split()) < 250)):
        return True
    password = soup.find("input", attrs={"type": re.compile("^password$", re.I)})
    login_heading = re.search(
        r"\b(?:log ?in|sign ?in|authentication required)\b", title + " " + headings, re.I
    )
    return bool(password and login_heading and (not substantial or not soup.find("article")))


def is_access_challenge(html: bytes | str, headers: Mapping[str, str] | None = None) -> bool:
    """Recognize interstitials independent of HTTP status; no requests are made."""
    if headers and any(
        str(key).lower() == "cf-mitigated" and str(value).strip().lower() == "challenge"
        for key, value in headers.items()
    ):
        return True
    if not isinstance(html, (str, bytes)):
        raise TypeError("html must be bytes or str")
    return _challenge(BeautifulSoup(html, "html.parser"))


def _json_articles(soup: BeautifulSoup) -> list[dict]:
    articles = []
    for script in soup.find_all(
        "script", attrs={"type": re.compile(r"^application/ld\+json$", re.I)}
    ):
        try:
            data = json.loads(script.string or "")
        except (ValueError, RecursionError):
            continue
        queue = [(data, 0)]
        while queue:
            node, depth = queue.pop()
            if depth > 20:
                continue
            if isinstance(node, list):
                queue.extend((item, depth + 1) for item in node)
            elif isinstance(node, dict):
                types = node.get("@type", [])
                types = [types] if isinstance(types, str) else types
                if isinstance(types, list) and any(
                    isinstance(t, str)
                    and re.fullmatch(r"(?:(?:https?://schema\.org/))?(?:Article|NewsArticle)", t)
                    for t in types
                ):
                    articles.append(node)
                for key in ("@graph", "mainEntity"):
                    if key in node:
                        queue.append((node[key], depth + 1))
    return articles


def _string(value) -> str:
    return _space(value) if isinstance(value, str) else ""


def _metadata(soup: BeautifulSoup, schema: dict, url: str) -> tuple[dict, list[str]]:
    def meta(*names):
        for name in names:
            for node in soup.find_all("meta"):
                if node.get("name", "").lower() == name or node.get("property", "").lower() == name:
                    value = _string(node.get("content"))
                    if value:
                        return value
        return ""

    title = meta("og:title", "twitter:title") or _string(schema.get("headline"))
    h1 = soup.find("h1")
    title = title or (h1.get_text(" ", strip=True) if h1 else "")
    title = title or (soup.title.get_text(" ", strip=True) if soup.title else "")
    authors = schema.get("author", [])
    if not isinstance(authors, list):
        authors = [authors]
    author = meta("author", "article:author") or ", ".join(
        name
        for entry in authors
        if (name := _string(entry.get("name") if isinstance(entry, dict) else entry))
    )
    if not author:
        byline = soup.select_one('[rel="author"], [itemprop="author"]')
        author = byline.get_text(" ", strip=True) if byline else ""
    published = meta("article:published_time", "datepublished") or _string(
        schema.get("datePublished")
    )
    if not published:
        time = soup.select_one('[itemprop="datePublished"]')
        if time:
            published = _string(
                time.get("datetime") or time.get("content") or time.get_text(" ", strip=True)
            )
    canonical = ""
    for link in soup.find_all("link", href=True):
        if "canonical" in [str(rel).lower() for rel in link.get("rel", [])]:
            canonical = link["href"]
            break
    if not canonical:
        canonical = schema.get("url", "")
        if not canonical:
            main = schema.get("mainEntityOfPage", "")
            canonical = main.get("@id", "") if isinstance(main, dict) else main
    canonical, warnings = _canonical(canonical, url)
    return {
        "title": title,
        "author": author,
        "published_at": published,
        "canonical_url": canonical,
    }, warnings


def _canonical(value, source: str) -> tuple[str, list[str]]:
    if not value:
        return "", []
    if (
        not isinstance(value, str)
        or any(c.isspace() or ord(c) < 32 for c in value)
        or "\\" in value
        or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", value, re.I)
    ):
        return "", ["canonical_invalid"]
    try:
        base = urlsplit(source)
        target = urlsplit(urldefrag(urljoin(source, value))[0])
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
        ):
            return "", ["canonical_invalid"]
        if target.hostname.lower() != (base.hostname or "").lower():
            return "", ["canonical_cross_host"]
        if target.port not in {None, 80 if target.scheme == "http" else 443}:
            return "", ["canonical_invalid"]
        if base.scheme not in {"http", "https"}:
            return "", ["canonical_invalid"]
        return urlunsplit(target), []
    except ValueError:
        return "", ["canonical_invalid"]


def _excerpt(soup: BeautifulSoup, schema: dict) -> bool:
    if schema.get("isAccessibleForFree") in (False, "false", "False"):
        return True
    roots = soup.find_all("article") or soup.find_all("main")
    for root in roots:
        clean = _clean(root)
        for node in clean.find_all(["p", "a", "button", "div", "span"]):
            text = node.get_text(" ", strip=True)
            if len(text) < 350 and _EXCERPT.search(text):
                return True
            if node.name == "a" and re.fullmatch(
                r"(?:read|continue reading|show|view)\s*(?:more)?[ .…]*", text, re.I
            ):
                return True
    for node in soup.find_all(["p", "div", "span", "a", "button"]):
        text = node.get_text(" ", strip=True)
        if (
            len(text) < 350
            and _EXCERPT.search(text)
            and not node.find_parent(["nav", "footer", "aside"])
            and not node.has_attr("hidden")
        ):
            return True
        marker = " ".join([str(node.get("id", "")), " ".join(node.get("class", []))])
        if re.search(
            r"(?:^|[-_\s])(?:paywall|subscription-wall|article-excerpt)(?:$|[-_\s])", marker, re.I
        ):
            return True
    return False


def _js_shell(soup: BeautifulSoup) -> bool:
    root = soup.select_one("#root, #app, #__next, #__nuxt, app-root, [data-reactroot]")
    if root is None:
        return False
    scripts = soup.find_all("script")
    bootstrap = any(
        node.get("src")
        or node.get("type") == "module"
        or re.search(
            r"(?:createRoot|hydrateRoot|createApp|__NEXT_DATA__|__NUXT__)", node.get_text()
        )
        for node in scripts
    )
    visible = _space(_clean(soup).get_text(" ", strip=True))
    return bool(bootstrap and len(visible.split()) < 60 and not _prose(visible))


def extract_article(
    html: bytes | str, url: str, *, max_text_chars: int = 60000
) -> ExtractedArticle:
    """Extract main prose without network access or inferred author/date metadata.

    Canonical URLs are same-host hints only, not SSRF approval. A fetcher must
    validate DNS, redirects, and destinations again before following any URL.
    """
    _limit(max_text_chars)
    if not isinstance(html, (bytes, str)):
        raise TypeError("html must be bytes or str")
    if not html.strip():
        return ExtractedArticle(warnings=("no_article_content",))
    soup = BeautifulSoup(html, "html.parser")
    if _binary(str(soup)):
        return ExtractedArticle(warnings=("invalid_content",))
    if _challenge(soup):
        return ExtractedArticle(warnings=("access_challenge",))
    schemas = _json_articles(soup)
    schema = schemas[0] if len(schemas) == 1 else {}
    metadata, warnings = _metadata(soup, schema, url)
    container, fallback, strong = _container(soup)
    text, method, completeness = "", "none", "unknown"
    listing = len(_article_roots(soup)) > 1
    if bare_extraction is None:
        warnings.append("trafilatura_unavailable")
    elif not listing:
        try:
            result = bare_extraction(
                html,
                url=url,
                include_comments=False,
                include_tables=True,
                include_formatting=True,
                favor_precision=True,
                output_format="xml",
            )
            if result:
                body = result.body
                if body is not None:
                    text = _plain(
                        BeautifulSoup(ElementTree.tostring(body, encoding="unicode"), "html.parser")
                    )
                else:
                    text = _string(result.raw_text)
                visible_text = fallback or _plain(_clean(soup))
                source_words = Counter(word.casefold() for word in _WORD.findall(visible_text))
                extracted_words = Counter(word.casefold() for word in _WORD.findall(text))
                if not _prose(text) or extracted_words - source_words:
                    text = ""
                if text:
                    method = "trafilatura"
                    completeness = "likely_complete" if strong else "unknown"
        except Exception:
            # Parser/library errors must not discard a usable semantic container.
            warnings.append("trafilatura_failed")
    if text and container is not None:
        context = container.find_all(["pre", "table"])
        if any(
            _space(node.get_text(" ", strip=True)) not in _space(text.replace("|", " "))
            for node in context
        ):
            text = ""
    if not text and fallback and not listing:
        text, method = fallback, "beautifulsoup"
        completeness = "likely_complete" if strong else "unknown"
    if not text and not listing and schema:
        body = schema.get("articleBody")
        if isinstance(body, str):
            candidate = (
                _plain(_clean(BeautifulSoup(body, "html.parser")))
                if re.search(r"</?[a-z][^>]*>", body, re.I)
                else body.strip()
            )
            if not _binary(candidate) and _prose(candidate):
                text, method = candidate, "jsonld"
                warnings.append("jsonld_unverified_completeness")
    if text and _excerpt(soup, schema):
        completeness = "partial"
        warnings.append("excerpt_or_paywall")
    if len(text) > max_text_chars:
        text = text[:max_text_chars].rstrip()
        completeness = "partial"
        warnings.append("text_truncated")
    needs_render = not text and _js_shell(soup)
    if needs_render:
        warnings.append("javascript_shell")
    if not text:
        warnings.append("no_article_content")
    return ExtractedArticle(
        text=text,
        method=method,
        completeness=completeness,
        warnings=tuple(dict.fromkeys(warnings)),
        needs_render=needs_render,
        **metadata,
    )


def pasted_article(text: str, *, max_text_chars: int = 60000) -> ExtractedArticle:
    """Validate a user-supplied article; reject oversize pastes rather than trim."""
    _limit(max_text_chars)
    if not isinstance(text, str):
        raise TypeError("text must be str")
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or _binary(text):
        raise ValueError("Pasted article must contain non-binary, readable text")
    if len(text) > max_text_chars:
        raise ValueError("Pasted article exceeds max_text_chars")
    warnings = ()
    cleaned = "".join(c for c in text if unicodedata.category(c) != "Cc" or c in "\n\t")
    if cleaned != text:
        warnings = ("control_characters_removed",)
    return ExtractedArticle(text=cleaned, method="pasted", warnings=warnings)
