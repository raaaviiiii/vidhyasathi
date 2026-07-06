"""Layer 3: web retrieval, behind a wrapper (a swap point like the model calls).

LAST RESORT ONLY — the confidence gate calls this on a material miss, and only when
the web toggle is on. Default provider: DuckDuckGo (no key) + trafilatura extraction.
Swap web_search()/fetch_url() for a search API later without touching answer.py.

Install (Mac):  pip install ddgs trafilatura

Every returned passage is tagged  "web · <domain>"  so it is always visibly web-sourced
in citations and can never be mistaken for the student's own notes.
"""
from urllib.parse import urlparse

SEARCH_K = 3
MAX_CHARS = 1500   # per page — keep the injected context bounded


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "") or url
    except Exception:
        return url


def web_search(query: str, k: int = SEARCH_K) -> list[dict]:
    """Return [{'title','url'}]. Empty list on any failure (caller then refuses)."""
    try:
        try:
            from ddgs import DDGS               # maintained package name
        except ImportError:
            from duckduckgo_search import DDGS   # older name, same API
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=k)
        out = []
        for h in (results or []):
            url = h.get("href") or h.get("url")
            if url:
                out.append({"title": h.get("title", ""), "url": url})
        return out
    except Exception:
        return []


def fetch_url(url: str) -> str:
    """Main-text extraction from a page. '' on any failure."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ""
        text = trafilatura.extract(downloaded, include_comments=False,
                                   include_tables=False) or ""
        return text.strip()
    except Exception:
        return ""


def web_context(query: str, k: int = SEARCH_K) -> list[dict]:
    """Search -> fetch -> extract. Returns passages shaped like KB hits so the same
    build_context()/citation path works. Each is tagged [web · <domain>]."""
    passages = []
    for r in web_search(query, k):
        text = fetch_url(r["url"])
        if not text:
            continue
        passages.append({
            "source": f"web \u00b7 {_domain(r['url'])}",   # -> citation tag [web · domain]
            "loc": (r["title"][:60] or "page"),
            "url": r["url"],
            "text": text[:MAX_CHARS],
            "layer": "web",
        })
    return passages
