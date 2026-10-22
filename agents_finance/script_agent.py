"""Stage 5: scriptwriter with a fact-check retry loop. The model only ever sees placeholders for data."""
import json, re
from dataclasses import dataclass, field

from . import fact_checker as fc
from .facts import render_template
from .prompts import SYSTEM, build_script_prompt
from .topics import requires_current_data


class ScriptRejected(RuntimeError):
    pass


@dataclass
class ScriptResult:
    template: str
    narration: str
    visual_hints: list = field(default_factory=list)
    title_ideas: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self): return self.__dict__.copy()


def parse_json(text):
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", t, re.S)
    if not m: raise ValueError("no JSON object in model output")
    return json.loads(m.group(0))


def generate_script(topic, facts, settings, invoke, now=None):
    target_words = round(settings.duration_sec * settings.words_per_second)
    feedback, last = None, None
    for attempt in range(settings.max_retries + 1):
        prompt = build_script_prompt(topic, facts, settings, target_words, feedback)
        try:
            data = parse_json(invoke(prompt, system=SYSTEM))
            template = str(data["narration"]).strip()
        except Exception as e:
            feedback, last = f"Output was not valid JSON with a 'narration' string ({e}).", f"parse error: {e}"
            continue
        rep = fc.check_script(template, facts, settings, requires_current_data=requires_current_data(settings), now=now)
        if rep.passed:
            return ScriptResult(template=template, narration=render_template(template, {f.id: f for f in facts}),
                                visual_hints=[str(h) for h in data.get("visual_hints", [])][:8],
                                title_ideas=[str(t) for t in data.get("title_ideas", [])][:5],
                                warnings=[f"{w.code}: {w.message}" for w in rep.warnings])
        feedback = "\n".join(f"- {f.code}: {f.message}" for f in rep.errors)
        last = rep.summary()
    raise ScriptRejected(f"script failed fact-check after {settings.max_retries + 1} attempts: {last}")
