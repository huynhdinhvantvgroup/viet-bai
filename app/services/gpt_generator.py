import base64
import json
import markdown
from pathlib import Path
from openai import AsyncOpenAI
import app.config as cfg

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

client = AsyncOpenAI(api_key=cfg.OPENAI_API_KEY)


def _get_client() -> tuple[AsyncOpenAI, str]:
    """Return the right client and model name based on current config."""
    model = cfg.GPT_MODEL
    if model.startswith("gemini-"):
        return AsyncOpenAI(api_key=cfg.GEMINI_API_KEY, base_url=GEMINI_BASE_URL), model
    return client, model


def _load_prompt() -> str:
    return cfg.PROMPT_FILE.read_text(encoding="utf-8")


def _encode_image(image_path: str) -> str | None:
    p = Path(image_path)
    if not p.exists():
        return None
    data = p.read_bytes()
    ext = p.suffix.lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
    mime_type = mime.get(ext.lstrip("."), "image/jpeg")
    return f"data:{mime_type};base64,{base64.b64encode(data).decode()}"


async def generate_article(
    screenshot_path: str = "",
    competitor_content: str = "",
    custom_prompt: str | None = None,
) -> dict:
    """Generate article using GPT. Returns {title, content, word_count}."""
    system_prompt = custom_prompt if custom_prompt else _load_prompt()

    # Append HTML output instruction
    system_prompt += """

FORMAT INSTRUCTION (override output format):
Output the article as clean HTML for WordPress. Rules:
- First line: the headline as plain text (no HTML tags, no # marks)
- Remaining content: use <h2> for subheadings, <p> for paragraphs, <strong> for bold text, <em> for italic
- Use <strong><em>text</em></strong> for bold italic pull-quotes
- Never use markdown syntax (no ##, no **, no *)
- Every paragraph must be wrapped in <p> tags
- Separate paragraphs properly
- Do NOT include <html>, <head>, <body> tags — only the article body content
"""

    # Build user message parts
    user_parts = []

    # Screenshot (vision)
    if screenshot_path:
        b64 = _encode_image(screenshot_path)
        if b64:
            user_parts.append({
                "type": "image_url",
                "image_url": {"url": b64, "detail": "high"},
            })
            user_parts.append({"type": "text", "text": "Above is the screenshot of the full Facebook post."})

    # Competitor content
    if competitor_content.strip():
        user_parts.append({
            "type": "text",
            "text": f"COMPETITOR ARTICLE CONTENT:\n\n{competitor_content.strip()}",
        })

    # Fallback if no input at all
    if not user_parts:
        user_parts.append({"type": "text", "text": "Please generate a health article based on your system prompt guidelines."})

    active_client, model = _get_client()
    response = await active_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_parts},
        ],
        max_completion_tokens=4096,
        temperature=0.7,
    )

    raw = response.choices[0].message.content or ""

    # Extract title: first line (plain text headline)
    lines = raw.strip().split("\n")
    title = "Untitled"
    content_start = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped:
            # Remove any markdown heading markers or HTML tags from title
            title = stripped.lstrip("#").strip().strip("*").strip()
            if title.startswith("<"):
                import re
                title = re.sub(r"<[^>]+>", "", title).strip()
            content_start = i + 1
            break

    # Rest is content
    content_raw = "\n".join(lines[content_start:]).strip()

    # Check if GPT returned HTML or markdown
    if "<h2" in content_raw or "<p>" in content_raw:
        # Already HTML — use as-is
        content_html = content_raw
    else:
        # Fallback: convert markdown to HTML
        content_html = markdown.markdown(
            content_raw,
            extensions=["extra", "nl2br", "sane_lists"],
        )

    # Count words from stripped text
    import re
    plain_text = re.sub(r"<[^>]+>", "", content_html)
    word_count = len(plain_text.split())

    return {"title": title, "content": content_html, "word_count": word_count}


async def generate_image_keywords(title: str, content: str) -> list[str]:
    """Generate 3 English search keywords for Pexels stock photos."""
    active_client, model = _get_client()
    response = await active_client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You generate image search keywords for health articles. "
                    "Return exactly 3 short English search phrases (2-4 words each) "
                    "that would find relevant, appealing stock photos for the article. "
                    "Focus on food, lifestyle, wellness, and senior health visuals. "
                    "Return as a JSON array of 3 strings. No explanation."
                ),
            },
            {
                "role": "user",
                "content": f"Title: {title}\n\nFirst 200 words: {content[:800]}",
            },
        ],
        max_completion_tokens=100,
        temperature=0.5,
    )

    raw = response.choices[0].message.content or "[]"
    # Clean markdown code block if present
    raw = raw.strip().strip("`").strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()
    try:
        keywords = json.loads(raw)
        if isinstance(keywords, list) and len(keywords) >= 3:
            return keywords[:3]
    except json.JSONDecodeError:
        pass
    return ["healthy food senior", "wellness lifestyle", "natural remedy"]
