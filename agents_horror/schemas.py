# agents_horror/schemas.py
"""Story bible / scene schemas with plain-Python validation (the repo has no
pydantic dependency, so this follows the dict + validate-function style)."""
import re
from dataclasses import dataclass, field, asdict

SOURCE_TYPES = ("stock_video", "generated", "missing")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _clean_id(value, fallback):
    v = re.sub(r"[^a-z0-9_]", "_", str(value or "").strip().lower()).strip("_")
    if not v or not v[0].isalpha():
        v = fallback
    return v[:32]


def _s(d, key, default=""):
    v = d.get(key, default)
    return str(v).strip() if v is not None else default


@dataclass
class Character:
    id: str
    name: str
    age: str = ""
    appearance: str = ""
    clothing: str = ""
    features: str = ""
    role: str = ""
    relationships: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d, n=1):
        return cls(id=_clean_id(d.get("id") or d.get("name"), f"char_{n}"), name=_s(d, "name"),
                   age=_s(d, "age"), appearance=_s(d, "appearance"), clothing=_s(d, "clothing"),
                   features=_s(d, "features"), role=_s(d, "role"),
                   relationships=dict(d.get("relationships") or {}))


@dataclass
class Location:
    id: str
    name: str
    description: str = ""
    layout: str = ""
    lighting: str = ""
    palette: str = ""
    weather: str = ""
    time_of_day: str = ""

    @classmethod
    def from_dict(cls, d, n=1):
        return cls(id=_clean_id(d.get("id") or d.get("name"), f"loc_{n}"), name=_s(d, "name"),
                   description=_s(d, "description"), layout=_s(d, "layout"),
                   lighting=_s(d, "lighting"), palette=_s(d, "palette"),
                   weather=_s(d, "weather"), time_of_day=_s(d, "time_of_day"))


@dataclass
class Prop:
    id: str
    name: str
    description: str = ""

    @classmethod
    def from_dict(cls, d, n=1):
        return cls(id=_clean_id(d.get("id") or d.get("name"), f"prop_{n}"),
                   name=_s(d, "name"), description=_s(d, "description"))


@dataclass
class Entity:
    """A recurring supernatural entity / creature. `description` must stay fixed."""
    id: str
    name: str
    description: str = ""
    rules: str = ""

    @classmethod
    def from_dict(cls, d, n=1):
        return cls(id=_clean_id(d.get("id") or d.get("name"), f"entity_{n}"),
                   name=_s(d, "name"), description=_s(d, "description"), rules=_s(d, "rules"))


@dataclass
class StoryBible:
    title: str = ""
    characters: list = field(default_factory=list)
    locations: list = field(default_factory=list)
    props: list = field(default_factory=list)
    entities: list = field(default_factory=list)
    continuity_rules: list = field(default_factory=list)
    palette: str = ""
    time_of_day: str = ""
    weather: str = ""

    def all_ids(self):
        return {x.id for group in (self.characters, self.locations, self.props, self.entities)
                for x in group}

    def get(self, ref_id):
        for group in (self.characters, self.locations, self.props, self.entities):
            for x in group:
                if x.id == ref_id:
                    return x
        return None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        d = d or {}
        return cls(
            title=_s(d, "title"),
            characters=[Character.from_dict(x, i + 1) for i, x in enumerate(d.get("characters") or [])],
            locations=[Location.from_dict(x, i + 1) for i, x in enumerate(d.get("locations") or [])],
            props=[Prop.from_dict(x, i + 1) for i, x in enumerate(d.get("props") or [])],
            entities=[Entity.from_dict(x, i + 1) for i, x in enumerate(d.get("entities") or [])],
            continuity_rules=[str(x).strip() for x in (d.get("continuity_rules") or []) if str(x).strip()],
            palette=_s(d, "palette"), time_of_day=_s(d, "time_of_day"), weather=_s(d, "weather"),
        )


@dataclass
class Scene:
    scene_id: str
    order: int
    narration: str
    start: float = 0.0
    duration: float = 0.0
    characters: list = field(default_factory=list)
    location: str = ""
    action: str = ""
    camera: str = ""
    lighting: str = ""
    atmosphere: str = ""
    bible_refs: list = field(default_factory=list)
    search_keywords: list = field(default_factory=list)
    source_type: str = "stock_video"
    sfx: list = field(default_factory=list)
    ambience: str = ""
    transition: str = "cut"
    continuity: list = field(default_factory=list)
    pause_after: float = 0.3

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(
            scene_id=_s(d, "scene_id"), order=int(d.get("order") or 0), narration=_s(d, "narration"),
            start=float(d.get("start") or 0), duration=float(d.get("duration") or 0),
            characters=list(d.get("characters") or []), location=_s(d, "location"),
            action=_s(d, "action"), camera=_s(d, "camera"), lighting=_s(d, "lighting"),
            atmosphere=_s(d, "atmosphere"), bible_refs=list(d.get("bible_refs") or []),
            search_keywords=list(d.get("search_keywords") or []),
            source_type=_s(d, "source_type", "stock_video") or "stock_video",
            sfx=list(d.get("sfx") or []), ambience=_s(d, "ambience"),
            transition=_s(d, "transition", "cut") or "cut",
            continuity=list(d.get("continuity") or []),
            pause_after=float(d.get("pause_after") if d.get("pause_after") is not None else 0.3),
        )


# ── validation (each returns a list of error strings; empty list = valid) ──

def validate_bible(bible):
    errs = []
    if not bible.characters:
        errs.append("bible has no characters")
    if not bible.locations:
        errs.append("bible has no locations")
    seen = set()
    for group in (bible.characters, bible.locations, bible.props, bible.entities):
        for x in group:
            if not _ID_RE.match(x.id):
                errs.append(f"bad id '{x.id}'")
            if x.id in seen:
                errs.append(f"duplicate id '{x.id}'")
            seen.add(x.id)
            if not x.name:
                errs.append(f"'{x.id}' has no name")
    for c in bible.characters:
        if not (c.appearance and c.clothing):
            errs.append(f"character '{c.id}' needs appearance and clothing for continuity")
    for loc in bible.locations:
        if not loc.description:
            errs.append(f"location '{loc.id}' needs a description")
    for e in bible.entities:
        if not e.description:
            errs.append(f"entity '{e.id}' needs a fixed description")
    return errs


def validate_scenes(scenes, bible, total_sec=None, tolerance=1.0):
    errs = []
    if not scenes:
        return ["no scenes"]
    ids = set()
    valid_ids = bible.all_ids()
    cursor = 0.0
    for i, sc in enumerate(scenes, 1):
        tag = sc.scene_id or f"#{i}"
        if not sc.scene_id:
            errs.append(f"scene #{i} has no scene_id")
        elif sc.scene_id in ids:
            errs.append(f"duplicate scene_id '{sc.scene_id}'")
        ids.add(sc.scene_id)
        if sc.order != i:
            errs.append(f"scene {tag} order {sc.order} != position {i}")
        if not sc.narration:
            errs.append(f"scene {tag} has empty narration")
        if sc.duration <= 0:
            errs.append(f"scene {tag} has non-positive duration")
        if abs(sc.start - cursor) > tolerance:
            errs.append(f"scene {tag} start {sc.start:.1f}s does not follow previous scene ({cursor:.1f}s)")
        cursor = sc.start + sc.duration
        if sc.source_type not in SOURCE_TYPES:
            errs.append(f"scene {tag} has unknown source_type '{sc.source_type}'")
        refs = set(sc.bible_refs) | set(sc.characters) | ({sc.location} if sc.location else set())
        unknown = sorted(r for r in refs if r not in valid_ids)
        if unknown:
            errs.append(f"scene {tag} references unknown bible ids {unknown}")
        if not sc.location:
            errs.append(f"scene {tag} has no location")
        if sc.source_type == "stock_video":
            kws = [k for k in sc.search_keywords if str(k).strip()]
            if not kws:
                errs.append(f"scene {tag} has no search_keywords")
            for k in kws:
                if not 1 <= len(str(k).split()) <= 5:
                    errs.append(f"scene {tag} keyword '{k}' should be 1-5 words")
    if total_sec and abs(cursor - total_sec) > max(3.0, total_sec * 0.15):
        errs.append(f"scenes cover {cursor:.1f}s but target is {total_sec}s")
    return errs
