import asyncio

import httpx
import app.config as _cfg

# Module-level vars so they can be hot-reloaded from settings
PEXELS_API_KEY = _cfg.PEXELS_API_KEY
PIXABAY_API_KEY = _cfg.PIXABAY_API_KEY

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
PIXABAY_SEARCH_URL = "https://pixabay.com/api/"
PIXABAY_MAX_ATTEMPTS = 3
PIXABAY_RETRY_DELAY = 3  # seconds


async def search_pexels(keyword: str, per_page: int = 5) -> list[dict]:
    """Search Pexels for stock photos. Returns list of {url, photographer, src}."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            PEXELS_SEARCH_URL,
            headers={"Authorization": PEXELS_API_KEY},
            params={
                "query": keyword,
                "per_page": per_page,
                "orientation": "landscape",
                "size": "medium",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for photo in data.get("photos", []):
        results.append({
            "id": photo["id"],
            "url": photo["src"]["large"],  # 940px wide
            "medium": photo["src"]["medium"],
            "original": photo["src"]["original"],
            "photographer": photo["photographer"],
            "alt": photo.get("alt", ""),
            "pexels_url": photo["url"],
        })
    return results


def _pixabay_cdn_url(hit: dict, size: int) -> str:
    """Build a cdn.pixabay.com URL of the given width from previewURL (..._150.jpg).

    pixabay.com/get/... links (largeImageURL, webformatURL) are often blocked by
    Cloudflare (429), while cdn.pixabay.com serves the same image fine.
    """
    preview = hit.get("previewURL", "")
    if "_150." in preview:
        return preview.replace("_150.", f"_{size}.")
    return hit["largeImageURL"]


async def search_pixabay(keyword: str, per_page: int = 5) -> list[dict]:
    """Search Pixabay for stock photos. Same result shape as search_pexels.

    Retries when Cloudflare intermittently answers 429.
    """
    async with httpx.AsyncClient() as client:
        for attempt in range(1, PIXABAY_MAX_ATTEMPTS + 1):
            resp = await client.get(
                PIXABAY_SEARCH_URL,
                params={
                    "key": PIXABAY_API_KEY,
                    "q": keyword[:100],  # Pixabay max query length
                    "image_type": "photo",
                    "orientation": "horizontal",
                    "safesearch": "true",
                    "per_page": max(per_page, 3),  # Pixabay minimum is 3
                },
                timeout=15,
            )
            if resp.status_code == 429 and attempt < PIXABAY_MAX_ATTEMPTS:
                print(f"[RETRY] Pixabay search '{keyword}' got 429, attempt {attempt}/{PIXABAY_MAX_ATTEMPTS}")
                await asyncio.sleep(PIXABAY_RETRY_DELAY)
                continue
            break
        # Don't use raise_for_status: its message contains the URL with the API key
        if resp.is_error:
            raise RuntimeError(f"Pixabay HTTP {resp.status_code}")
        data = resp.json()

    results = []
    for hit in data.get("hits", []):
        results.append({
            "id": hit["id"],
            "url": _pixabay_cdn_url(hit, 1280),  # 1280px wide
            "medium": _pixabay_cdn_url(hit, 640),
            "original": _pixabay_cdn_url(hit, 1280),
            "photographer": hit.get("user", ""),
            "alt": hit.get("tags", ""),
            "pixabay_url": hit.get("pageURL", ""),
        })
    return results


async def search_stock_images(keyword: str, per_page: int = 5) -> list[dict]:
    """Search Pexels first; fall back to Pixabay when Pexels fails (e.g. 429)."""
    try:
        return await search_pexels(keyword, per_page)
    except Exception as e:
        if not PIXABAY_API_KEY:
            raise
        print(f"[WARN] Pexels search '{keyword}' failed ({e}), trying Pixabay")
    return await search_pixabay(keyword, per_page)


# Generic keywords used when the article keywords don't yield enough images
FALLBACK_KEYWORDS = ["healthy food", "senior wellness", "fresh vegetables", "natural remedy"]


async def get_article_images(keywords: list[str], count: int = 3) -> list[dict]:
    """Pick `count` unique images: one per keyword, then generic fallback keywords."""
    images = []
    seen_urls = set()
    for kw in list(keywords) + FALLBACK_KEYWORDS:
        if len(images) >= count:
            break
        try:
            results = await search_stock_images(kw, per_page=5)
        except Exception as e:
            print(f"[ERR] Stock image search '{kw}': {e}")
            continue
        # Take the first photo not already chosen
        for photo in results:
            if photo["url"] not in seen_urls:
                seen_urls.add(photo["url"])
                images.append(photo)
                break
    return images


async def download_image(url: str) -> bytes:
    """Download image bytes from URL."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
