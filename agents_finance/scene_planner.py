"""Stage 7: scene planning. Charts only for verified series; stock footage only with generic queries."""
import re
from dataclasses import dataclass, field, asdict

from .facts import PLACEHOLDER, facts_by_id, referenced_ids, render_template

SENT = re.compile(r"(?<=[.!?])\s+")


class SceneError(ValueError):
    pass


@dataclass
class Scene:
    index: int
    narration: str
    duration_sec: float
    visual_type: str                      # "chart" | "stock"
    search_terms: list = field(default_factory=list)
    chart: dict = field(default_factory=dict)   # {"fact_id", "title", "series", "as_of", "source"}
    on_screen_text: str = ""
    source_attribution: list = field(default_factory=list)
    fact_ids: list = field(default_factory=list)

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, d): return cls(**d)


ABSTRACT = {"animated", "animation", "animations", "icon", "icons", "flowchart", "infographic",
            "infographics", "diagram", "diagrams", "representing", "concept", "abstract", "illustration"}


HINT_BLOCK = {"graphic", "graphics", "showing", "highlighting", "simple", "visual", "visuals"}


EXTRA_TERMS = ("business district skyline", "office workers laptop", "stock ticker display",
               "calculator and notes", "city skyline night", "people walking city street")


def is_generic(term, forbidden):
    toks = set(re.findall(r"[a-z0-9]+", term.lower()))
    bad = {t for f in forbidden for t in re.findall(r"[a-z0-9]+", str(f).lower())}
    return bool(toks) and not (toks & bad) and not (toks & ABSTRACT) and not re.search(r"\d", term)


def _group(sentences, n):
    words = [len(PLACEHOLDER.sub("x", s).split()) for s in sentences]
    target, groups, cur, cw = sum(words) / max(n, 1), [], [], 0
    for s, w in zip(sentences, words):
        if cur and cw + w / 2 > target and len(groups) < n - 1:
            groups.append(cur); cur, cw = [], 0
        cur.append(s); cw += w
    if cur: groups.append(cur)
    return [" ".join(g) for g in groups]


def plan_scenes(template, facts, topic, settings, visual_hints=()):
    fmap = facts_by_id(facts)
    cat = settings.cat
    sentences = [s for s in SENT.split(template.strip()) if s]
    n = max(3, round(settings.duration_sec / (6 if settings.video_format == "shorts" else 12)))
    groups = _group(sentences, min(n, len(sentences)))
    forbidden = list(topic.entity_terms) + [topic.ticker]
    hints = [h for h in visual_hints
             if is_generic(h, forbidden) and len(h.split()) <= 4 and not (set(h.lower().split()) & HINT_BLOCK)]
    pool = list(dict.fromkeys(list(cat.stock_terms) + hints + list(EXTRA_TERMS)))
    used_terms = set()

    scenes, total_words = [], 0
    for g in groups:
        total_words += len(render_template(g, fmap).split())
    for i, g in enumerate(groups):
        ids = list(dict.fromkeys(referenced_ids(g)))
        used = [fmap[x] for x in ids]
        text = render_template(g, fmap)
        dur = round(settings.duration_sec * len(text.split()) / max(total_words, 1), 1)
        series_fact = next((f for f in used if len(f.series) >= 2), None)
        attrib = []
        for f in used:
            if f.kind == "hypothetical": attrib.append("Hypothetical example, not a real investment")
            elif f.kind == "forecast": attrib.append(f"Forecast by {f.attribution}")
            if f.source_url: attrib.append(f"{f.source_title or f.source_url} ({f.source_url})")
        stat = next((f for f in used if f.kind in ("current", "computed") and f.unit != "text"), None)
        sc = Scene(index=i, narration=text, duration_sec=dur, visual_type="chart" if series_fact else "stock",
                   on_screen_text=f"{stat.label}: {stat.render()}" if stat else "",
                   source_attribution=list(dict.fromkeys(attrib)), fact_ids=ids)
        if series_fact:
            sc.chart = {"fact_id": series_fact.id, "title": series_fact.label, "series": series_fact.series,
                        "as_of": series_fact.as_of, "source": series_fact.source_title or series_fact.source_url}
        else:
            fresh = [t for t in pool if t not in used_terms] or pool   # distinct footage per scene
            sc.search_terms = fresh[:2]
            used_terms.add(sc.search_terms[0])
        scenes.append(sc)
    if scenes:  # make estimated durations sum exactly to the target
        scenes[-1].duration_sec = round(scenes[-1].duration_sec + settings.duration_sec - sum(s.duration_sec for s in scenes), 1)
    validate_scenes(scenes, settings, forbidden)
    return scenes


def validate_scenes(scenes, settings, forbidden=()):
    if not scenes: raise SceneError("no scenes")
    for s in scenes:
        if not s.narration.strip() or s.duration_sec <= 0: raise SceneError(f"scene {s.index}: empty narration or duration")
        if s.visual_type == "chart" and len(s.chart.get("series", [])) < 2: raise SceneError(f"scene {s.index}: chart without data")
        if s.visual_type == "stock":
            if not s.search_terms: raise SceneError(f"scene {s.index}: no search terms")
            for t in s.search_terms:
                if not is_generic(t, forbidden): raise SceneError(f"scene {s.index}: non-generic footage query {t!r}")
        if s.visual_type not in ("chart", "stock"): raise SceneError(f"scene {s.index}: bad visual_type")
    if abs(sum(s.duration_sec for s in scenes) - settings.duration_sec) > 1.0:
        raise SceneError("scene durations do not sum to target")
