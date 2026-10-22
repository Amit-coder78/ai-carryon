"""Fact registry. The LLM writes {{id}} placeholders; numbers are substituted from verified facts."""
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

KINDS = ("current", "historical", "hypothetical", "forecast", "computed", "concept")
SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_]+)(?:\.(dir|abs))?\s*\}\}")
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?:\s?%|x\b)?")


class UnknownFact(KeyError):
    pass


def _indian_group(n: int) -> str:
    s = str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:]); head = head[:-2]
        if head: parts.insert(0, head)
        s = ",".join(parts + [tail])
    return ("-" if n < 0 else "") + s


@dataclass
class Fact:
    id: str
    label: str
    value: object                      # float for numeric facts, str for text facts
    kind: str
    unit: str = "count"                # pct | x | money | count | text
    currency: str = ""
    decimals: int = 2
    as_of: str = ""                    # ISO-8601 datetime (UTC) of the data point or period end
    source_url: str = ""
    source_title: str = ""
    attribution: str = ""              # required for forecasts ("RBI projects ...")
    note: str = ""                     # required for hypotheticals (the assumptions)
    derived_from: list = field(default_factory=list)
    series: list = field(default_factory=list)   # [[label, value], ...] for charts

    def as_of_dt(self):
        if not self.as_of: return None
        d = datetime.fromisoformat(self.as_of)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

    def _num(self, v):
        if self.unit == "money":
            sym = SYMBOLS.get(self.currency, self.currency + " " if self.currency else "")
            if self.currency == "INR":
                whole = _indian_group(int(abs(v) // 1))
                frac = f"{abs(v) - int(abs(v)):.{self.decimals}f}"[1:] if self.decimals else ""
                return f"{'-' if v < 0 else ''}{sym}{whole}{frac}"
            return f"{'-' if v < 0 else ''}{sym}{abs(v):,.{self.decimals}f}"
        if self.unit == "pct": return f"{abs(v):.{self.decimals}f}%"
        if self.unit == "x": return f"{v:.{self.decimals}f}x"
        return f"{v:,.{self.decimals}f}" if self.decimals else f"{int(round(v)):,}"

    def render(self, mode=None):
        if self.unit == "text": return str(self.value)
        v = float(self.value)
        if mode == "dir": return "up" if v > 0 else "down" if v < 0 else "unchanged"
        if mode == "abs": return self._num(abs(v))
        if self.unit == "pct": return f"{'+' if v > 0 else '-' if v < 0 else ''}{abs(v):.{self.decimals}f}%"
        return self._num(v)

    def to_dict(self): return asdict(self)

    @classmethod
    def from_dict(cls, d): return cls(**d)


def facts_by_id(facts):
    out = {}
    for f in facts:
        if f.id in out: raise ValueError(f"duplicate fact id {f.id}")
        out[f.id] = f
    return out


def referenced_ids(template):
    return [m.group(1) for m in PLACEHOLDER.finditer(template)]


def render_template(template, facts):
    def sub(m):
        fid, mode = m.group(1), m.group(2)
        if fid not in facts: raise UnknownFact(fid)
        return facts[fid].render(mode)
    return PLACEHOLDER.sub(sub, template)


def ungrounded_numbers(template, allowed_literals=()):
    """Digits typed by the model outside placeholders. Small counts (1-10) are tolerated."""
    stripped = PLACEHOLDER.sub(" ", template)
    allowed = {str(a) for a in allowed_literals}
    bad = []
    for m in NUMBER.finditer(stripped):
        tok = m.group(0).strip()
        if tok in allowed: continue
        if re.fullmatch(r"\d{1,2}", tok) and 1 <= int(tok) <= 10: continue
        bad.append(tok)
    return bad
