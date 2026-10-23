# agents_horror/research_agent.py
"""Optional research. Off by default (HORROR_RESEARCH=0): purely fictional stories
need none. When on AND the concept names a real place/folklore topic, it reads the
Wikipedia REST summary (free, no key) so documented facts stay separate from
fictional additions. No LLM call, no new dependency (requests is already used)."""
import re
import requests

WIKI_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{}"
FICTION_NOTE = "All characters, events and dialogue in this story are fictional."


def research_notes(cfg, concept, http_get=None):
    """Returns {"used": bool, "sources": [...], "documented_facts": [...],
    "fictional_additions": [...]}. Never raises."""
    empty = {"used": False, "sources": [], "documented_facts": [], "fictional_additions": [FICTION_NOTE]}
    topic = (concept or {}).get("research_topic") or ""
    if not (cfg.research and topic.strip()):
        return empty
    get = http_get or (lambda url: requests.get(url, timeout=15, headers={"User-Agent": "ai-carryon-horror/1.0"}))
    try:
        title = re.sub(r"\s+", "_", topic.strip())
        r = get(WIKI_SUMMARY.format(requests.utils.quote(title, safe="_")))
        if getattr(r, "status_code", 200) != 200:
            return empty
        data = r.json()
        extract = (data.get("extract") or "").strip()
        if not extract or data.get("type") == "disambiguation":
            return empty
        sentences = re.split(r"(?<=[.!?])\s+", extract)[:3]
        url = ((data.get("content_urls") or {}).get("desktop") or {}).get("page", "")
        return {"used": True,
                "sources": [{"title": data.get("title", topic), "url": url, "publisher": "Wikipedia"}],
                "documented_facts": sentences,
                "fictional_additions": [FICTION_NOTE]}
    except Exception as e:
        print(f"[horror research] skipped: {e}")
        return empty
