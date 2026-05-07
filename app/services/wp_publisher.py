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


def insert_featured_image_into_content(content: str, featured_url: str) -> str:
    if not featured_url:
        return content

    content = _normalize_headings(content)

    img_html = f'\n\n<figure class="wp-block-image size-large"><img src="{featured_url}" alt="" /></figure>\n\n'

    h2_pattern = re.compile(r"(<h2[^>]*>)", re.IGNORECASE)
    h2_matches = list(h2_pattern.finditer(content))

    if not h2_matches:
        return content + img_html

    def count_text_blocks(text):
        p_count = len(re.findall(r"<p[^>]*>", text, re.IGNORECASE))
        if p_count >= 3:
            return p_count
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        text_lines = [l for l in lines if not re.match(r"^</?(?:div|figure|img|br|hr)[^>]*>$", l, re.IGNORECASE)]
        return max(p_count, len(text_lines))

    for m in h2_matches:
        text_above = content[:m.start()]
        if count_text_blocks(text_above) >= 3:
            return content[:m.start()] + img_html + content[m.start():]

    pos = h2_matches[0].start()
    return content[:pos] + img_html + content[pos:]


def insert_images_into_content(content: str, image_urls: list[str]) -> str:
    content = _normalize_headings(content)

    h2_pattern = re.compile(r"(<h2[^>]*>.*?</h2>)", re.IGNORECASE)
    h2_matches = list(h2_pattern.finditer(content))

    if not h2_matches or not image_urls:
        return content

    total_h2 = len(h2_matches)
    available = list(range(1, total_h2))
    if not available:
        return content

    step = max(1, len(available) // len(image_urls))
    chosen = []
    for i in range(len(image_urls)):
        idx = i * step
        if idx < len(available):
            chosen.append(available[idx])

    insert_positions = []
    for i, h2_idx in enumerate(chosen):
        if i < len(image_urls) and h2_idx < total_h2:
            insert_positions.append((h2_matches[h2_idx].end(), image_urls[i]))

    result = content
    for pos, url in reversed(insert_positions):
        img_html = f'\n\n<figure class="wp-block-image size-large"><img src="{url}" alt="" /></figure>\n\n'
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
