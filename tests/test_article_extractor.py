"""Offline synthetic fixtures; no web requests, live articles, or credentials."""

import importlib.util
import json
from dataclasses import FrozenInstanceError, fields

import pytest

from telegram_bot.article_extractor import (
    ExtractedArticle,
    extract_article,
    is_access_challenge,
    pasted_article,
)

URL = "https://example.com/news/issue"
HAS_TRAFILATURA = importlib.util.find_spec("trafilatura") is not None
PARAGRAPHS = [
    "A small research group tested a new cooling system in its workshop. "
    "The first experiment reduced heat without increasing the machine's power demand.",
    "Engineers repeated the measurement on three separate mornings. "
    "They published the raw observations so that another team could check the result.",
    "The design uses ordinary materials and does not require a special factory. "
    "Its creators warned that larger installations may behave differently in summer.",
    "A second group will now test the device under changing outdoor conditions. "
    "Those results will determine whether the prototype is suitable for a longer trial.",
]
SHORT = (
    "The library reopened its reading room today. Visitors can now borrow tools as well as books."
)


def page(body, head=""):
    return f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"


def article(body=None, head="", attrs=""):
    if body is None:
        body = "".join(f"<p>{text}</p>" for text in PARAGRAPHS)
    return page(f"<article {attrs}><h1>A workshop experiment</h1>{body}</article>", head)


def json_page(payload, body=""):
    return page(body, f'<script type="application/ld+json">{json.dumps(payload)}</script>')


def test_frozen_contract_defaults():
    result = ExtractedArticle()
    assert [field.name for field in fields(result)] == [
        "text",
        "title",
        "author",
        "published_at",
        "canonical_url",
        "method",
        "completeness",
        "warnings",
        "needs_render",
    ]
    assert result == ExtractedArticle("", "", "", "", "", "none", "unknown", (), False)
    with pytest.raises(FrozenInstanceError):
        result.text = "changed"


def test_static_article_excludes_navigation_and_footer():
    html = (
        article()
        .replace("<body>", '<body><nav><a href="/buy">BUY OUR OTHER PRODUCTS</a></nav>')
        .replace("</body>", "<footer>Cookie settings and legal notices</footer></body>")
    )
    result = extract_article(html.encode(), URL)
    assert "research group" in result.text
    assert "longer trial" in result.text
    assert "BUY OUR OTHER PRODUCTS" not in result.text
    assert "Cookie settings" not in result.text
    assert result.title == "A workshop experiment"
    assert result.completeness == "likely_complete"
    assert not result.needs_render


@pytest.mark.skipif(not HAS_TRAFILATURA, reason="Parent installs trafilatura")
def test_real_trafilatura_primary_path():
    result = extract_article(article().encode(), URL)
    assert result.method == "trafilatura"
    assert "second group" in result.text


def test_short_article_uses_prose_and_structure_not_length_alone():
    result = extract_article(article(f"<p>{SHORT}</p>"), URL)
    assert SHORT in result.text
    assert len(result.text) < 200
    assert result.method != "none"
    assert result.completeness == "likely_complete"


@pytest.mark.parametrize(
    "body",
    [
        '<nav><a href="/">Home</a><a href="/about">About</a></nav>',
        '<main><h1>Latest stories</h1><p><a href="/a">Browse our latest interesting stories '
        "and all the other popular posts from this week.</a></p></main>",
        "<article><h1>Home</h1><p>Products Services Company Contact Privacy Careers "
        "Support Solutions Pricing Customers Partners Resources</p></article>",
        "<article><h1>Welcome</h1><p>Hello world.</p></article>",
        "<main><h1>News</h1>"
        + "".join(
            f'<article><h2><a href="/{i}">Story {i}</a></h2><p>{SHORT}</p></article>'
            for i in range(3)
        )
        + "</main>",
    ],
)
def test_navigation_and_article_listing_are_not_articles(body):
    result = extract_article(page(body), URL)
    assert not result.text
    assert result.completeness == "unknown"


def test_metadata_description_is_never_used_as_article_body():
    result = extract_article(page("", f'<meta name="description" content="{SHORT}">'), URL)
    assert not result.text
    assert not result.needs_render


def test_headings_lists_code_and_tables_keep_context():
    body = "".join(f"<p>{text}</p>" for text in PARAGRAPHS)
    body += (
        "<h2>Measured results</h2><ul><li>First trial: stable temperature.</li>"
        "<li>Second trial: lower power.</li></ul>"
        "<pre><code>for trial in trials:\n    record(trial)</code></pre>"
        "<table><caption>Test readings</caption><tr><th>Trial</th><th>Watts</th></tr>"
        "<tr><td>First</td><td>42</td></tr></table>"
    )
    text = extract_article(article(body), URL).text
    for fragment in (
        "Measured results",
        "First trial",
        "Second trial",
        "for trial in trials:",
        "record(trial)",
        "Trial",
        "Watts",
        "42",
    ):
        assert fragment in text
    assert "\n" in text


def test_source_backed_metadata_only():
    head = (
        '<meta name="author" content="Morgan Example">'
        '<meta property="article:published_time" content="2026-07-01T08:00:00Z">'
        '<link rel="canonical" href="/news/canonical#details">'
    )
    result = extract_article(article(head=head), URL)
    assert result.author == "Morgan Example"
    assert result.published_at == "2026-07-01T08:00:00Z"
    assert result.canonical_url == "https://example.com/news/canonical"
    unlabelled = extract_article(article(), URL)
    assert unlabelled.author == ""
    assert unlabelled.published_at == ""


@pytest.mark.parametrize(
    "canonical, warning",
    [
        ("https://evil.example/article", "canonical_cross_host"),
        ("//evil.example/article", "canonical_cross_host"),
        ("https://example.com.evil.example/article", "canonical_cross_host"),
        ("https://user:password@example.com/article", "canonical_invalid"),
        ("javascript:alert(1)", "canonical_invalid"),
        ("file:///etc/passwd", "canonical_invalid"),
        ("http://127.0.0.1/private", "canonical_cross_host"),
        ("https://example.com:8443/admin", "canonical_invalid"),
        ("https://example.com:bad/article", "canonical_invalid"),
        ("https://example.com/&#10;evil", "canonical_invalid"),
        ("https://example.com/\\evil", "canonical_invalid"),
        ("https://example.com/%0d%0aHeader:value", "canonical_invalid"),
    ],
)
def test_untrusted_canonical_is_not_returned(canonical, warning):
    result = extract_article(article(head=f'<link rel="canonical" href="{canonical}">'), URL)
    assert result.canonical_url == ""
    assert warning in result.warnings
    assert "research group" in result.text


def test_base_href_cannot_redirect_relative_canonical():
    head = '<base href="https://evil.example/"><link rel="canonical" href="/safe">'
    assert extract_article(article(head=head), URL).canonical_url == "https://example.com/safe"


@pytest.mark.parametrize("kind", ["Article", "NewsArticle", "https://schema.org/NewsArticle"])
def test_genuine_jsonld_article_body_fallback(kind):
    payload = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": kind,
                "headline": "The library returns",
                "articleBody": SHORT,
                "author": [{"@type": "Person", "name": "Avery Example"}],
                "datePublished": "2026-07-01",
                "url": "/library",
            }
        ],
    }
    result = extract_article(json_page(payload), URL)
    assert result.text == SHORT
    assert result.method == "jsonld"
    assert result.title == "The library returns"
    assert result.author == "Avery Example"
    assert result.published_at == "2026-07-01"
    assert result.canonical_url == "https://example.com/library"
    assert result.completeness == "unknown"


@pytest.mark.parametrize(
    "payload",
    [
        {"@type": "Product", "articleBody": SHORT},
        {"@type": "Article", "description": SHORT},
        {"@type": "WebPage", "text": SHORT},
        {"@type": "Article", "articleBody": {"text": SHORT}},
        {"@type": "Article", "articleBody": "Home News About Contact"},
    ],
)
def test_jsonld_does_not_promote_descriptions_or_unrelated_schema(payload):
    assert not extract_article(json_page(payload), URL).text


def test_malformed_jsonld_does_not_hide_real_article():
    result = extract_article(article(head='<script type="application/ld+json">{bad</script>'), URL)
    assert "research group" in result.text


def test_multiple_jsonld_articles_without_a_main_article_are_ambiguous():
    payload = [
        {"@type": "NewsArticle", "headline": f"Story {i}", "articleBody": SHORT} for i in range(3)
    ]
    assert not extract_article(json_page(payload), URL).text


@pytest.mark.parametrize(
    "body",
    [
        '<title>Just a moment...</title><div id="challenge-running">Checking your browser '
        'before accessing this website</div><script src="/cdn-cgi/challenge-platform/x"></script>',
        '<h1>Verify you are human</h1><div class="g-recaptcha" data-sitekey="synthetic"></div>',
        "<h1>Attention Required! | Cloudflare</h1><p>Sorry, you have been blocked</p>",
        '<h1>Sign in to continue</h1><form action="/login"><input type="password"></form>',
        '<title>Log in</title><h1>Welcome back</h1><form><input type="email">'
        '<input type="password"><button>Log in</button></form>',
        "<h1>Access denied</h1><p>Please enable cookies to continue.</p>",
        '<h1>Security verification</h1><iframe src="https://captcha.example/check"></iframe>',
    ],
)
def test_http_200_challenge_and_login_interstitials(body):
    html = page(body)
    assert is_access_challenge(html)
    result = extract_article(html, URL)
    assert not result.text
    assert result.method == "none"
    assert "access_challenge" in result.warnings
    assert not result.needs_render


def test_cloudflare_mitigated_header_is_case_insensitive_for_200_or_403():
    for status in ("200", "403"):
        assert is_access_challenge("<html></html>", {"CF-Mitigated": "Challenge", "status": status})
    assert not is_access_challenge(article(), {"Server": "cloudflare", "CF-Ray": "test"})


def test_challenge_with_fabricated_article_metadata_stays_blocked():
    payload = {"@type": "NewsArticle", "articleBody": " ".join(PARAGRAPHS)}
    html = json_page(payload, '<h1>Verify you are human</h1><div id="challenge-running"></div>')
    assert is_access_challenge(html)
    assert not extract_article(html, URL).text


def test_legitimate_article_discussing_cloudflare_is_not_a_challenge():
    html = article(
        "<p>Cloudflare serves many websites around the world. Its CAPTCHA system asks "
        "visitors to verify you are human when suspicious traffic is detected.</p>"
        "<p>The phrase checking your browser appears during some security checks. "
        "Researchers studied access denied errors and login walls in their report.</p>"
        "<pre>window._cf_chl_opt = example; /cdn-cgi/challenge-platform/</pre>"
        '<footer><a href="/login">Sign in to continue</a><div class="g-recaptcha"></div></footer>',
    )
    assert not is_access_challenge(html, {"server": "cloudflare"})
    assert "Cloudflare serves" in extract_article(html, URL).text


@pytest.mark.parametrize(
    "body",
    [
        '<div id="root"></div><script src="/static/app.js"></script>',
        '<div id="__next"><span>Loading...</span></div><script src="/_next/static/app.js"></script>',
        '<div id="app"></div><noscript>Please enable JavaScript to view this page.</noscript>'
        '<script type="module" src="/assets/main.js"></script>',
    ],
)
def test_thin_javascript_shell_requests_rendering(body):
    result = extract_article(page(body), URL)
    assert not result.text
    assert result.needs_render
    assert "javascript_shell" in result.warnings


@pytest.mark.parametrize(
    "body",
    [
        "<div></div>",
        "<p>Please enable JavaScript.</p>",
        '<div id="root">An ordinary placeholder.</div>',
        '<script src="/tracking.js"></script><p>Nothing here.</p>',
        '<div id="root"></div><script src="/app.js"></script><h1>Verify you are human</h1>'
        '<div class="cf-turnstile"></div>',
    ],
)
def test_empty_markup_and_challenges_are_not_renderable_app_shells(body):
    assert not extract_article(page(body), URL).needs_render


def test_server_rendered_root_with_article_does_not_need_rendering():
    html = (
        article()
        .replace("<body>", '<body><div id="root">')
        .replace("</body>", '</div><script src="/app.js"></script></body>')
    )
    assert not extract_article(html, URL).needs_render


@pytest.mark.parametrize(
    "notice",
    [
        '<a href="/full">Read the full article</a>',
        '<a class="read-more" href="/full">Read more</a>',
        "<p>Subscribe to continue reading this article.</p>",
        '<div class="paywall">This article is for subscribers only.</div>',
        "<p>This is an excerpt. The full story is available to members.</p>",
    ],
)
def test_explicit_excerpt_or_paywall_marks_partial(notice):
    result = extract_article(article(f"<p>{SHORT}</p>{notice}"), URL)
    assert "library reopened" in result.text
    assert result.completeness == "partial"
    assert "excerpt_or_paywall" in result.warnings


def test_generic_newsletter_signup_and_related_links_do_not_imply_excerpt():
    html = article().replace(
        "</body>",
        '<footer>Subscribe to our newsletter. <a href="/related">Read more</a></footer></body>',
    )
    result = extract_article(html, URL)
    assert result.completeness == "likely_complete"
    assert "excerpt_or_paywall" not in result.warnings


def test_schema_access_restriction_marks_partial_not_full_factuality():
    payload = {
        "@type": "Article",
        "headline": "Library",
        "articleBody": SHORT,
        "isAccessibleForFree": False,
    }
    result = extract_article(json_page(payload), URL)
    assert result.completeness == "partial"
    assert "excerpt_or_paywall" in result.warnings


def test_truncation_is_reported():
    result = extract_article(article(), URL, max_text_chars=125)
    assert 0 < len(result.text) <= 125
    assert result.completeness == "partial"
    assert "text_truncated" in result.warnings


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_invalid_limits_are_rejected(limit):
    with pytest.raises(ValueError):
        extract_article(article(), URL, max_text_chars=limit)
    with pytest.raises(ValueError):
        pasted_article(SHORT, max_text_chars=limit)


@pytest.mark.parametrize("encoding", ["utf-8", "windows-1252", "utf-16"])
def test_declared_encoding_and_bom(encoding):
    html = article(
        "<p>Cafés opened their doors early this morning. The mayor’s report described "
        "a successful trial and promised more public spaces.</p>",
        head=f'<meta charset="{encoding}">',
    )
    result = extract_article(html.encode(encoding), URL)
    assert "Cafés" in result.text
    assert "mayor’s" in result.text
    assert "�" not in result.text


def test_malformed_html_is_tolerated():
    html = "<html><head><title>Library</title></head><body><article><h1>Library</h1><p>" + SHORT
    assert "library reopened" in extract_article(html, URL).text


@pytest.mark.parametrize("value", ["", b"", b"\x89PNG\r\n\x1a\n\0binary", "%PDF-1.7\0data"])
def test_empty_or_binary_html_is_not_content(value):
    result = extract_article(value, URL)
    assert not result.text
    assert not result.needs_render


def test_weak_main_fallback_is_unknown():
    result = extract_article(page(f"<main><p>{SHORT}</p></main>"), URL)
    assert SHORT in result.text
    assert result.completeness == "unknown"


def test_paste_preserves_paragraphs_and_strips_bom():
    text = "\ufeff  First paragraph.\r\n\r\nSecond paragraph.\r\n    code()  "
    result = pasted_article(text)
    assert result.text == "First paragraph.\n\nSecond paragraph.\n    code()"
    assert result.method == "pasted"
    assert result.completeness == "unknown"
    assert result.title == result.author == result.published_at == result.canonical_url == ""
    assert not result.needs_render


@pytest.mark.parametrize(
    "text", ["", " \n\t", "\ufeff", "\0binary", "\x01\x02bad", "�" * 20, "%PDF-1.7 binary stream"]
)
def test_paste_rejects_empty_control_heavy_or_binary(text):
    with pytest.raises(ValueError):
        pasted_article(text)


def test_paste_limit_is_rejected_not_silently_truncated():
    assert pasted_article(SHORT, max_text_chars=len(SHORT)).text == SHORT
    with pytest.raises(ValueError, match="max_text_chars"):
        pasted_article(SHORT, max_text_chars=len(SHORT) - 1)


@pytest.mark.parametrize("value", [None, 42, b"bytes"])
def test_paste_requires_text(value):
    with pytest.raises(TypeError):
        pasted_article(value)


@pytest.mark.parametrize(
    "heading, extra",
    [
        ("Just a moment...", '<div id="challenge-running"></div>'),
        ("Sign in to continue", '<form><input type="password"></form>'),
        ("Access denied", ""),
    ],
)
def test_wordy_interstitial_inside_main_is_not_article(heading, extra):
    html = page(
        f"<main><h1>{heading}</h1><p>We need to check your browser before you can "
        "access this website. This automatic process will complete shortly.</p>"
        "<p>Please enable JavaScript and cookies to continue browsing the website. "
        "Your browser will redirect when this check has completed.</p>"
        f"{extra}</main>"
    )
    assert is_access_challenge(html)
    assert not extract_article(html, URL).text


def test_library_failure_uses_real_semantic_fallback(monkeypatch):
    # Only the third-party failure boundary is substituted; parsing stays real.
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic parser failure")

    monkeypatch.setattr("telegram_bot.article_extractor.bare_extraction", fail)
    html = article(
        "<p><span>The library </span><span>reopened its reading room today. </span>"
        "<span>Visitors can now borrow tools as well as books.</span></p>"
        "<!--HIDDEN COMMENT MUST NOT LEAK.--><p hidden>HIDDEN NAVIGATION MUST NOT LEAK.</p>"
        '<p aria-hidden="true">ANOTHER HIDDEN PARAGRAPH.</p>'
        "<pre>for item in items:\n    process(item)</pre>"
    )
    result = extract_article(html, URL)
    assert SHORT in result.text
    assert "\n    process(item)" in result.text
    assert "HIDDEN" not in result.text
    assert result.method == "beautifulsoup"
    assert "trafilatura_failed" in result.warnings


def test_foreign_schema_type_is_not_a_genuine_article():
    html = json_page({"@type": "https://evil.example/Article", "articleBody": SHORT})
    assert not extract_article(html, URL).text


def test_jsonld_body_with_markup_is_cleaned():
    html = json_page(
        {"@type": "Article", "articleBody": f"<section><span>{SHORT}</span></section>"}
    )
    result = extract_article(html, URL)
    assert result.text == SHORT
    assert result.method == "jsonld"


def test_plain_nonsemantic_paragraph_article_can_use_primary():
    if not HAS_TRAFILATURA:
        pytest.skip("Parent installs trafilatura")
    result = extract_article(
        page("<div>" + "".join(f"<p>{p}</p>" for p in PARAGRAPHS) + "</div>"), URL
    )
    assert result.method == "trafilatura"
    assert "research group" in result.text


def test_long_navigation_with_sentence_like_links_is_not_content():
    html = page(
        "<nav>"
        + "".join(f'<p><a href="/{i}">{p}</a></p>' for i, p in enumerate(PARAGRAPHS))
        + "</nav>"
    )
    assert not extract_article(html, URL).text


def test_no_network_even_for_canonical_and_external_script(monkeypatch):
    attempts = []

    def forbidden(*args, **kwargs):
        attempts.append(args)
        raise AssertionError("Extraction must never connect to a network")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    html = article(
        head='<link rel="canonical" href="https://evil.example/private">'
        '<script src="https://evil.example/app.js"></script>'
    )
    assert "research group" in extract_article(html.encode(), URL).text
    assert not attempts


def test_hidden_prose_is_not_extracted_by_primary():
    html = article(
        f"<p>{SHORT}</p><p hidden>This secret draft is not public. "
        "The company would prefer that the hidden draft was not published here.</p>"
    )
    result = extract_article(html, URL)
    assert SHORT in result.text
    assert "secret draft" not in result.text


def test_paywall_notice_outside_article_marks_excerpt():
    html = article(f"<p>{SHORT}</p>").replace(
        "</body>", "<div>Subscribe to continue reading this article.</div></body>"
    )
    result = extract_article(html, URL)
    assert result.completeness == "partial"
    assert "excerpt_or_paywall" in result.warnings


def test_main_article_is_not_confused_with_embedded_repository_cards():
    body = "<main><h1>A workshop experiment</h1>" + "".join(f"<p>{p}</p>" for p in PARAGRAPHS)
    body += '<article><a href="/repo-one">Unrelated repository card alpha</a></article>'
    body += '<article><a href="/repo-two">Unrelated repository card beta</a></article></main>'
    result = extract_article(page(body), URL)
    assert "research group" in result.text
    assert "longer trial" in result.text
    assert "Unrelated repository card" not in result.text
