import asyncio
import base64
import re
from bisect import bisect_right

import httpx

UPLOAD_MAX_ATTEMPTS = 8
UPLOAD_RETRY_DELAY = 2  # seconds
_RETRY_STATUS_CODES = (401, 403)


def _auth_header(wp_cfg: dict) -> dict:
    token = base64.b64encode(f"{wp_cfg['username']}:{wp_cfg['password']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def upload_media_wp(
    image_bytes: bytes,
    filename: str,
    wp_cfg: dict,
    mime_type: str = "image/jpeg",
    max_attempts: int = 1,
) -> dict:
    """Upload media to WP. Retries on 401/403 or connection errors up to max_attempts."""
    for attempt in range(1, max_attempts + 1):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{wp_cfg['url']}/wp-json/wp/v2/media",
                    headers={
                        **_auth_header(wp_cfg),
                        "Content-Type": mime_type,
                        "Content-Disposition": f'attachment; filename="{filename}"',
                    },
                    content=image_bytes,
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
            return {"id": data["id"], "url": data.get("source_url", "")}
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            retryable = isinstance(e, httpx.TransportError) or e.response.status_code in _RETRY_STATUS_CODES
            if not retryable or attempt >= max_attempts:
                raise
            print(f"[RETRY] Upload {filename} attempt {attempt}/{max_attempts}: {e}")
            await asyncio.sleep(UPLOAD_RETRY_DELAY)


async def upload_media_from_url_wp(image_url: str, filename: str, wp_cfg: dict, max_attempts: int = 1) -> dict:
    async with httpx.AsyncClient() as client:
        dl = await client.get(image_url, timeout=30)
        dl.raise_for_status()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    mime = mime_map.get(ext, "image/jpeg")
    return await upload_media_wp(dl.content, filename, wp_cfg, mime, max_attempts=max_attempts)


async def upload_media_from_path_wp(file_path: str, filename: str, wp_cfg: dict) -> dict:
    from pathlib import Path
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    ext = p.suffix.lower().lstrip(".")
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    mime = mime_map.get(ext, "image/jpeg")
    return await upload_media_wp(p.read_bytes(), filename, wp_cfg, mime)


def _normalize_headings(content: str) -> str:
    """If content has no <h2> tags, convert <p><strong>short text</strong></p> to <h2>."""
    if re.search(r"<h2[^>]*>", content, re.IGNORECASE):
        return content  # Already has h2, don't touch

    def _replace(m):
        text = m.group(1)
        # Skip if contains <em> (emphasis, not heading)
        if "<em>" in text.lower():
            return m.group(0)
        # Skip if too long (>100 chars = likely a sentence, not heading)
        plain = re.sub(r"<[^>]+>", "", text)
        if len(plain) > 100:
            return m.group(0)
        return f"<h2>{text}</h2>"

    return re.sub(
        r"<p>\s*<strong>((?:(?!<em>).)+?)</strong>\s*</p>",
        _replace,
        content,
        flags=re.IGNORECASE,
    )


def _count_paragraphs_between(content: str, start: int, end: int) -> int:
    """Count <p> tags in a slice of content."""
    segment = content[start:end]
    return len(re.findall(r"<p[^>]*>", segment, re.IGNORECASE))


def _find_insert_positions(content: str, num_images: int, min_gap: int = 3, reserve_top: int = 0) -> list[int]:
    """Find positions to insert images, ensuring >=min_gap paragraphs between each.

    Strategy:
    1. First pass: spots right before <h2> tags (visually clean)
    2. Second pass: fill remaining slots with spots after </p> tags
    3. Every spot keeps >=min_gap paragraphs from the reserved top area and from other images

    Args:
        content: HTML content
        num_images: number of images to place
        min_gap: minimum paragraphs between images (default 3)
        reserve_top: skip first N paragraphs (for featured image area)
    """
    if num_images == 0:
        return []

    p_ends = [m.end() for m in re.finditer(r"</p>", content, re.IGNORECASE)]
    if not p_ends:
        return []

    # Candidates as (position, paragraphs before position)
    h2_candidates = [
        (m.start(), bisect_right(p_ends, m.start()))
        for m in re.finditer(r"<h2[^>]*>", content, re.IGNORECASE)
    ]
    p_candidates = [(pe, i + 1) for i, pe in enumerate(p_ends)]

    chosen = []  # (position, paragraphs before position)

    def fits(pc: int) -> bool:
        if pc < reserve_top + min_gap:
            return False
        return all(abs(pc - c) >= min_gap for _, c in chosen)

    for group in (h2_candidates, p_candidates):
        for pos, pc in group:
            if len(chosen) >= num_images:
                break
            if fits(pc):
                chosen.append((pos, pc))

    return sorted(pos for pos, _ in chosen)


def insert_featured_image_into_content(content: str, featured_url: str) -> str:
    """Insert featured image right after the intro.

    Before the first <h2> that follows the first paragraph; fallback after the
    first paragraph; final fallback at the top of the content.
    """
    if not featured_url:
        return content

    content = _normalize_headings(content)

    img_html = f'\n\n<figure class="wp-block-image size-large"><img src="{featured_url}" alt="" /></figure>\n\n'

    first_p = re.search(r"</p>", content, re.IGNORECASE)
    if not first_p:
        return img_html + content

    h2 = re.compile(r"<h2[^>]*>", re.IGNORECASE).search(content, first_p.end())
    pos = h2.start() if h2 else first_p.end()
    return content[:pos] + img_html + content[pos:]


def insert_images_into_content(content: str, image_urls: list[str]) -> str:
    if not image_urls:
        return content

    content = _normalize_headings(content)

    # Detect if featured image was already inserted (figure tag in first portion)
    first_figure = re.search(r"<figure[^>]*>", content, re.IGNORECASE)
    if first_figure:
        # Count paragraphs before first figure = reserve area
        reserve = _count_paragraphs_between(content, 0, first_figure.start())
        reserve = max(reserve, 2)  # at least skip 2 paragraphs past featured
    else:
        reserve = 0

    positions = _find_insert_positions(content, len(image_urls), min_gap=3, reserve_top=reserve)

    # Insert in reverse order to preserve positions
    result = content
    for i, pos in enumerate(reversed(positions)):
        img_idx = len(positions) - 1 - i
        if img_idx < len(image_urls):
            img_html = f'\n\n<figure class="wp-block-image size-large"><img src="{image_urls[img_idx]}" alt="" /></figure>\n\n'
            result = result[:pos] + img_html + result[pos:]

    return result


async def create_post_wp(
    title: str,
    content: str,
    wp_cfg: dict,
    featured_image_id: int | None = None,
    category: str = "Health",
    tags: str = "",
    status: str = "publish",
    scheduled_at: str | None = None,
) -> dict:
    category_id = await _get_or_create_category(category, wp_cfg)

    tag_ids = []
    if tags.strip():
        for tag_name in tags.split(","):
            tag_name = tag_name.strip()
            if tag_name:
                tag_id = await _get_or_create_tag(tag_name, wp_cfg)
                tag_ids.append(tag_id)

    post_data = {
        "title": title,
        "content": content,
        "status": status if not scheduled_at else "future",
        "categories": [category_id] if category_id else [],
        "tags": tag_ids,
    }
    if featured_image_id:
        post_data["featured_media"] = featured_image_id
    if scheduled_at:
        post_data["date"] = scheduled_at

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{wp_cfg['url']}/wp-json/wp/v2/posts",
            headers=_auth_header(wp_cfg),
            json=post_data,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

    return {"id": data["id"], "url": data.get("link", "")}


async def _get_or_create_category(name: str, wp_cfg: dict) -> int | None:
    if not name.strip():
        return None
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{wp_cfg['url']}/wp-json/wp/v2/categories",
            headers=_auth_header(wp_cfg),
            params={"search": name, "per_page": 5},
            timeout=15,
        )
        resp.raise_for_status()
        cats = resp.json()
        for c in cats:
            if c["name"].lower() == name.lower():
                return c["id"]
        resp = await client.post(
            f"{wp_cfg['url']}/wp-json/wp/v2/categories",
            headers=_auth_header(wp_cfg),
            json={"name": name},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["id"]


async def _get_or_create_tag(name: str, wp_cfg: dict) -> int:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{wp_cfg['url']}/wp-json/wp/v2/tags",
            headers=_auth_header(wp_cfg),
            params={"search": name, "per_page": 5},
            timeout=15,
        )
        resp.raise_for_status()
        tags = resp.json()
        for t in tags:
            if t["name"].lower() == name.lower():
                return t["id"]
        resp = await client.post(
            f"{wp_cfg['url']}/wp-json/wp/v2/tags",
            headers=_auth_header(wp_cfg),
            json={"name": name},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["id"]
