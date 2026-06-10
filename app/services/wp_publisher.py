import base64
import re
import httpx


def _auth_header(wp_cfg: dict) -> dict:
    token = base64.b64encode(f"{wp_cfg['username']}:{wp_cfg['password']}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def upload_media_wp(image_bytes: bytes, filename: str, wp_cfg: dict, mime_type: str = "image/jpeg") -> dict:
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


async def upload_media_from_url_wp(image_url: str, filename: str, wp_cfg: dict) -> dict:
    async with httpx.AsyncClient() as client:
        dl = await client.get(image_url, timeout=30)
        dl.raise_for_status()
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg"
    mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    mime = mime_map.get(ext, "image/jpeg")
    return await upload_media_wp(dl.content, filename, wp_cfg, mime)


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
    1. Prefer inserting before <h2> tags (visually clean)
    2. If no suitable <h2>, fallback to after </p> tags
    3. Always ensure min_gap paragraphs from previous image / start

    Args:
        content: HTML content
        num_images: number of images to place
        min_gap: minimum paragraphs between images (default 3)
        reserve_top: skip first N paragraphs (for featured image area)
    """
    if num_images == 0:
        return []

    # Build list of all candidate positions: (position, type, p_count_before)
    # p_count_before = total <p> tags from start to this position
    p_pattern = re.compile(r"</p>", re.IGNORECASE)
    h2_pattern = re.compile(r"(<h2[^>]*>)", re.IGNORECASE)

    all_p = list(p_pattern.finditer(content))
    all_h2 = list(h2_pattern.finditer(content))

    if not all_p:
        return []

    # Build a function: how many <p> before position X
    p_ends = [m.end() for m in all_p]

    def p_count_at(pos):
        count = 0
        for pe in p_ends:
            if pe <= pos:
                count += 1
            else:
                break
        return count

    # Candidate positions: before each H2 (preferred) + after each </p> (fallback)
    candidates = []
    for m in all_h2:
        candidates.append((m.start(), "h2", p_count_at(m.start())))
    for m in all_p:
        candidates.append((m.end(), "p", p_count_at(m.end())))

    # Sort by position
    candidates.sort(key=lambda x: x[0])

    # Remove candidates in the reserved top area
    candidates = [c for c in candidates if c[2] >= reserve_top + min_gap]

    # Greedy selection: pick positions with enough gap
    chosen = []
    last_p_count = reserve_top  # treat reserved area as "last image position"

    # Prefer H2 positions first pass, then fill with P positions
    h2_candidates = [c for c in candidates if c[0] == "h2" or c[1] == "h2"]
    p_candidates = [c for c in candidates if c[1] == "p"]

    # Try H2 first
    for pos, typ, pc in candidates:
        if len(chosen) >= num_images:
            break
        if pc - last_p_count >= min_gap:
            # Prefer H2: if this is an H2 position, take it
            # If this is a P position, check if there's an H2 nearby (within 1 paragraph)
            if typ == "h2":
                chosen.append(pos)
                last_p_count = pc
            elif typ == "p":
                # Only use P if no H2 was available at similar distance
                # Check if next candidate is H2 and close enough
                chosen.append(pos)
                last_p_count = pc

    return chosen


def insert_featured_image_into_content(content: str, featured_url: str) -> str:
    if not featured_url:
        return content

    content = _normalize_headings(content)

    img_html = f'\n\n<figure class="wp-block-image size-large"><img src="{featured_url}" alt="" /></figure>\n\n'

    positions = _find_insert_positions(content, 1, min_gap=3, reserve_top=0)
    if positions:
        pos = positions[0]
        return content[:pos] + img_html + content[pos:]

    # Final fallback: append at end
    return content + img_html


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
