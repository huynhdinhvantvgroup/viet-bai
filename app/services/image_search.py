import httpx
import app.config as _cfg

# Module-level var so it can be hot-reloaded from settings
PEXELS_API_KEY = _cfg.PEXELS_API_KEY
PEXELS_API_KEY_2 = _cfg.PEXELS_API_KEY_2

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"


async def search_pexels(keyword: str, per_page: int = 5) -> list[dict]:
    """Search Pexels for stock photos. Returns list of {url, photographer, src}.

    Uses key 1 first; switches to key 2 when key 1 is rate-limited (429).
    """
    keys = [k for k in (PEXELS_API_KEY, PEXELS_API_KEY_2) if k]
    async with httpx.AsyncClient() as client:
        for i, key in enumerate(keys):
            resp = await client.get(
                PEXELS_SEARCH_URL,
                headers={"Authorization": key},
                params={
                    "query": keyword,
                    "per_page": per_page,
                    "orientation": "landscape",
                    "size": "medium",
                },
                timeout=15,
            )
            if resp.status_code == 429 and i < len(keys) - 1:
                print(f"[WARN] Pexels key {i + 1} rate-limited, switching to key {i + 2}")
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        else:
            raise RuntimeError("Pexels API key not configured")

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


# Generic keywords used when the article keywords don't yield enough images
FALLBACK_KEYWORDS = ["healthy food", "senior wellness", "fresh vegetables", "natural remedy"]


async def get_article_images(keywords: list[str], count: int = 3) -> list[dict]:
    """Pick `count` unique images: one per keyword, then generic fallback keywords."""
    images = []
    seen_ids = set()
    for kw in list(keywords) + FALLBACK_KEYWORDS:
        if len(images) >= count:
            break
        try:
            results = await search_pexels(kw, per_page=5)
        except Exception as e:
            print(f"[ERR] Pexels search '{kw}': {e}")
            continue
        # Take the first photo not already chosen
        for photo in results:
            if photo["id"] not in seen_ids:
                seen_ids.add(photo["id"])
                images.append(photo)
                break
    return images


async def download_image(url: str) -> bytes:
    """Download image bytes from URL."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
