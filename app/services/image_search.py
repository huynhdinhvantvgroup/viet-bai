import httpx
import app.config as _cfg

# Module-level var so it can be hot-reloaded from settings
PEXELS_API_KEY = _cfg.PEXELS_API_KEY

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"


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


async def get_article_images(keywords: list[str]) -> list[dict]:
    """Get one best image per keyword. Returns list of 3 image dicts."""
    images = []
    for kw in keywords[:3]:
        try:
            results = await search_pexels(kw, per_page=3)
            if results:
                images.append(results[0])
            else:
                # Fallback with broader keyword
                fallback = await search_pexels("healthy lifestyle senior", per_page=3)
                if fallback:
                    images.append(fallback[0])
        except Exception:
            pass
    return images


async def download_image(url: str) -> bytes:
    """Download image bytes from URL."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
