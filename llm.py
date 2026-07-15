"""
LLM provider abstraction for relevance-checking + summarizing articles.

Supports:
  - "gemini"  (Google Gemini API, e.g. gemini-2.5-flash) - default
  - "typhoon" (opentyphoon.ai, Thai-specialized, OpenAI-compatible API)

Both providers are called with the same prompt and expected to return
strict JSON: {"is_relevant": bool, "matched_topics": [...], "summary": "..."}
"""

import json
import os
import re
import sys

import requests

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TYPHOON_API_KEY = os.environ.get("TYPHOON_API_KEY")

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

TYPHOON_BASE_URL = "https://api.opentyphoon.ai/v1"
TYPHOON_MODEL = "typhoon-v2.1-12b-instruct"


def build_prompt(article_title, article_text, candidate_topics, summary_language):
    lang_instruction = {
        "th": "Write the summary in Thai.",
        "en": "Write the summary in English.",
        "match": "Write the summary in the same language as the article.",
    }.get(summary_language, "Write the summary in Thai.")

    # Truncate very long articles to keep prompts small/cheap.
    truncated_text = article_text[:6000]

    return f"""You are filtering a news alert bot. The article below was flagged because \
it contained keywords matching these topics: {candidate_topics}.

Your job: read the actual article content and decide whether it is genuinely \
about any of these topics (not just a coincidental keyword mention), then summarize it.

Article title: {article_title}
Article text:
\"\"\"
{truncated_text}
\"\"\"

Candidate topics: {candidate_topics}

{lang_instruction} Keep the summary to 2-3 sentences, factual, no commentary.

Respond with ONLY a JSON object, no markdown fences, no preamble, in exactly this shape:
{{"is_relevant": true or false, "matched_topics": ["topic1", "topic2"], "summary": "..."}}

If the article is not genuinely about any candidate topic, set "is_relevant" to false, \
"matched_topics" to [], and "summary" to "".
"""


def _extract_json(text):
    # Strip markdown code fences if the model added them despite instructions.
    text = text.strip()
    text = re.sub(r"^```(json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY environment variable not set.")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }
    resp = requests.post(
        GEMINI_URL,
        params={"key": GEMINI_API_KEY},
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return _extract_json(text)


def call_typhoon(prompt):
    if not TYPHOON_API_KEY:
        raise RuntimeError("TYPHOON_API_KEY environment variable not set.")

    resp = requests.post(
        f"{TYPHOON_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {TYPHOON_API_KEY}"},
        json={
            "model": TYPHOON_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    return _extract_json(text)


def check_relevance_and_summarize(
    article_title, article_text, candidate_topics, provider="gemini", summary_language="th"
):
    """
    Returns dict: {"is_relevant": bool, "matched_topics": [...], "summary": str}
    On any failure (API error, bad JSON), falls back to treating the article as
    relevant with the keyword-matched topics and no summary, so a transient LLM
    issue never silently swallows a real match.
    """
    prompt = build_prompt(article_title, article_text, candidate_topics, summary_language)

    try:
        if provider == "gemini":
            result = call_gemini(prompt)
        elif provider == "typhoon":
            result = call_typhoon(prompt)
        else:
            raise ValueError(f"Unknown llm_provider: {provider}")

        # Basic shape validation
        if "is_relevant" not in result:
            raise ValueError("Malformed LLM response, missing 'is_relevant'")
        return result

    except Exception as e:
        print(f"LLM relevance check failed ({provider}): {e}. Falling back to keyword match.", file=sys.stderr)
        return {
            "is_relevant": True,
            "matched_topics": candidate_topics,
            "summary": "",
        }
