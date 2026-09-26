import json
import os
import re

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_VARIATIONS = 3

SYSTEM_PROMPT = """You are a senior YouTube SEO strategist and copywriter.

You receive the transcript of a YouTube video and must return metadata that
maximises click through rate while staying honest to the actual content.

Return ONLY valid JSON in exactly this shape:
{
  "variations": [
    {"title": "...", "description": "...", "tags": ["...", "..."]}
  ]
}

Rules:
- Return exactly {n} variations.
- titles: max 100 characters, front load the main keyword, no clickbait lies,
  no ALL CAPS words, at most one emoji, no quotation marks.
- descriptions: 110-190 words. First line must be a strong hook under 120
  characters. Use short paragraphs and bullet points. Include a call to action
  and finish with 4-6 relevant #hashtags.
- tags: 15-20 strings, 1-4 words each, lowercase, mix of broad, niche and
  long tail phrases. Keep the joined tag string under 450 characters.
- The {n} variations must use different angles: (1) keyword/SEO driven,
  (2) curiosity and hook driven, (3) audience/community driven.
- Write everything in the same language as the transcript. Use English only
  when the transcript language is unclear.
- Never invent facts that are not in the transcript."""


class GroqError(Exception):
    """Raised when metadata generation fails."""


def get_config() -> tuple[str, str]:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    model = os.getenv("GROQ_MODEL", "").strip() or DEFAULT_MODEL
    if not api_key:
        raise GroqError(
            "GROQ_API_KEY is not set. Add it in the environment variables "
            "(Render dashboard -> Environment)."
        )
    return api_key, model


def generate_metadata(
    transcript_text: str,
    video_url: str,
    variations: int = DEFAULT_VARIATIONS,
) -> dict:
    api_key, model = get_config()
    variations = max(1, min(int(variations or DEFAULT_VARIATIONS), 5))

    user_prompt = (
        f"Video URL: {video_url}\n\n"
        f"Transcript:\n\"\"\"\n{transcript_text}\n\"\"\""
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT.replace("{n}", str(variations))},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.85,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }

    try:
        response = requests.post(
            GROQ_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
    except requests.RequestException as exc:
        raise GroqError("Could not reach the Groq API. Try again shortly.") from exc

    if response.status_code == 401:
        raise GroqError("Groq rejected the API key. Check GROQ_API_KEY.")
    if response.status_code == 404:
        raise GroqError(
            f"Model '{model}' is not available on your Groq account. "
            "Set GROQ_MODEL to another model (for example llama-3.1-8b-instant)."
        )
    if response.status_code == 429:
        raise GroqError("Groq rate limit reached. Wait a moment and retry.")
    if response.status_code >= 400:
        detail = response.text[:300]
        raise GroqError(f"Groq returned an error ({response.status_code}): {detail}")

    try:
        data = response.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise GroqError("Groq returned an unexpected response. Try again.") from exc

    return _parse(content, variations)


def _parse(content: str, expected: int) -> dict:
    cleaned = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GroqError("Groq returned malformed JSON. Try again.") from exc

    raw = parsed.get("variations") if isinstance(parsed, dict) else None
    if isinstance(parsed, list):
        raw = parsed
    if not isinstance(raw, list) or not raw:
        raise GroqError("Groq returned no variations. Try again.")

    variations = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        variation = {
            "title": _clean_title(item.get("title")),
            "description": str(item.get("description") or "").strip(),
            "tags": _clean_tags(item.get("tags")),
        }
        if variation["title"] and variation["description"]:
            variations.append(variation)

    if not variations:
        raise GroqError("Groq returned unusable content. Try again.")

    return {
        "variations": variations[:expected],
        "count": len(variations[:expected]),
    }


def _clean_title(value) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip().strip('"').strip()
    if len(title) > 100:
        title = title[:97].rsplit(" ", 1)[0] + "..."
    return title


def _clean_tags(value) -> list[str]:
    if isinstance(value, str):
        value = [part for part in re.split(r"[,\n]", value)]
    if not isinstance(value, list):
        return []

    tags, seen, budget = [], set(), 0
    for tag in value:
        tag = re.sub(r"\s+", " ", str(tag)).strip().lstrip("#").lower()
        if not tag or tag in seen:
            continue
        if budget + len(tag) + 1 > 450:
            break
        seen.add(tag)
        tags.append(tag)
        budget += len(tag) + 1
    return tags
