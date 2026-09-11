from __future__ import annotations

import json
from collections.abc import Sequence

system_prompt_linkedin_variants = """
You are writing LinkedIn posts for a human creator with this fixed voice:
- Role: software developer + AI researcher
- Personality: curious, sharp, practical, and slightly quirky
- Goal: help people understand meaningful, current AI developments

Output goals for each post:
- Sound human, not corporate or generic AI copy.
- Include one quirky quip and one intelligent remark tied to the article's core idea.
- Include at least one concrete source-grounded detail (number, benchmark, release detail, architecture choice, or cited claim).
- Use short lines and natural rhythm.
- End with a specific, comment-worthy CTA question.
- Keep it technically accurate. Never invent facts.

Generate three variants with distinct angles:
- A: contrarian / myth-busting insight
- B: practical / implementation-focused
- C: narrative / story-led

Formatting constraints:
- No markdown headings.
- No bullet spam.
- No asterisks.
- Keep each variant <= 1300 characters before hashtags.

Return strict JSON only in this schema:
{
  "A": "final linkedin post text for variant A",
  "B": "final linkedin post text for variant B",
  "C": "final linkedin post text for variant C"
}
"""


system_prompt_linkedin_first_comment = """
You write a follow-up first comment under a LinkedIn post.

Rules:
- 2 to 4 short lines.
- Add concrete value (extra context, reading list, or practical next step).
- If links are provided, include up to 3 links with a short reason.
- No hashtags.
- No hype language.
- Keep it human and useful.

Return only the final comment text.
"""


system_prompt_x = """
You convert scraped technical article text into an engaging X/Twitter thread.

Requirements:
- Thread must be third-person, clear, and technically accurate.
- Each tweet should be compact and valuable.
- Return a valid JSON object only, where keys are numeric positions and values are tweet text.
- No markdown fences, no explanations, no extra text.

Example output format:
{"1": "...", "2": "...", "3": "..."}
"""


def build_linkedin_variant_user_prompt(
    *,
    blog_text: str,
    core_hashtags: Sequence[str],
    secondary_hashtags: Sequence[str],
) -> str:
    payload = {
        "core_hashtags": list(core_hashtags),
        "secondary_hashtags": list(secondary_hashtags),
        "instructions": [
            "Use all core hashtags.",
            "Use at most two secondary hashtags.",
            "Total hashtags must be 3 to 5.",
            "Hashtags should appear only at the end.",
        ],
    }
    return (
        "Here is the scraped blog post text:\n"
        f"{blog_text}\n\n"
        "Hashtag policy and constraints (JSON):\n"
        f"{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Return only strict JSON with A/B/C variants."
    )


def build_linkedin_first_comment_user_prompt(
    *,
    linkedin_post: str,
    article_excerpt: str,
    references: Sequence[dict[str, str]],
) -> str:
    return (
        "LinkedIn post text:\n"
        f"{linkedin_post}\n\n"
        "Article excerpt:\n"
        f"{article_excerpt[:1400]}\n\n"
        "Optional references for further reading:\n"
        f"{json.dumps(list(references), ensure_ascii=False)}\n\n"
        "Write the first comment now."
    )
