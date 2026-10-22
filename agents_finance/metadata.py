"""Stage 10: titles, description, tags, thumbnail concept, source list."""
import re
from .fact_checker import find_banned
from .facts import facts_by_id, referenced_ids

_SENSEX = re.compile(r"\bSense[\s\u00a0\u202f]+x\b", re.I)


def _clean(t):
    """Displayed text only: normalise odd spaces and fix the phonetic spelling 'Sense x' -> 'Sensex'."""
    return _SENSEX.sub("Sensex", t.replace("\u202f", " ").replace("\u00a0", " "))


DISCLAIMER = ("Educational content only, not investment advice. Investing and trading involve risk, including loss of capital. "
              "Do your own research and consult a SEBI-registered adviser before making decisions.")


def build_metadata(topic, script, facts, settings):
    fmap = facts_by_id(facts)
    used = [fmap[i] for i in dict.fromkeys(referenced_ids(script.template))]
    title = next((t for t in script.title_ideas if t and len(t) <= 90 and not find_banned(t)), topic.title)
    if find_banned(title) or len(title) > 90: title = topic.title[:90]
    title = _clean(title)
    srcs, seen = [], set()
    for f in used:
        if f.source_url and f.source_url not in seen:
            seen.add(f.source_url); srcs.append(f"- {f.source_title or f.source_url}: {f.source_url} (data as of {f.as_of[:10] or 'n/a'})")
    for s in topic.sources:
        if s["url"] not in seen:
            seen.add(s["url"]); srcs.append(f"- {s['title']}: {s['url']} (published {s['published_at'][:10]})")
    hypo = any(f.kind == "hypothetical" for f in used)
    desc = [_clean(script.narration.split(". ")[0].rstrip(".")) + ".", ""]
    if hypo: desc += ["Figures marked as examples are hypothetical illustrations, not predictions.", ""]
    if srcs: desc += ["Sources:"] + srcs + [""]
    desc += [DISCLAIMER]
    tags = [settings.cat.label.lower(), "stock market", "investing basics", "finance education"]
    if settings.ticker: tags.append(settings.ticker.lower())
    hashtags = ["#finance", "#stockmarket", "#investing"] + (["#shorts"] if settings.video_format == "shorts" else [])
    return {"title": title, "description": "\n".join(desc) + "\n\n" + " ".join(hashtags), "tags": tags, "hashtags": hashtags,
            "thumbnail_concept": f"Bold 4-word text from '{topic.title}' over a dark chart background; no price promises.",
            "sources": srcs}
