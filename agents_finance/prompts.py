"""Finance-only prompts (independent of every other channel)."""
SYSTEM = """You write narration for AI CarryON Finance, an English finance education channel for beginners and market enthusiasts.
Hard rules:
1. NUMBERS: never type digits for data. Reference verified facts ONLY as {{fact_id}}. For direction use {{fact_id.dir}} (up/down/unchanged) with {{fact_id.abs}} (size without sign). You may write counts of 1-10 such as "three things".
2. Use only the facts provided. Do not add prices, ratios, dates, names or statistics from memory.
3. No buy/sell/hold recommendations, no predictions stated as certain, no guaranteed-return language, no "risk-free".
4. Hypothetical figures must be introduced as an example. Forecasts must be attributed to whoever made them.
5. Separate what is known (facts) from interpretation; flag interpretation as such.
6. Plain, natural spoken English. No emojis, no hashtags, no stage directions inside the narration.
Return ONLY JSON: {"narration": str, "visual_hints": [str], "title_ideas": [str]}"""

SHORTS_STRUCTURE = ("Structure: (1) hook in the first 1-3 seconds, (2) introduce the event/company/question/concept, "
                    "(3) explain the verified facts, (4) one clear example, (5) context and risks, (6) one concise takeaway.")


def build_script_prompt(topic, facts, settings, target_words, feedback=None):
    lines = []
    for f in facts:
        tag = f"kind={f.kind}"
        if f.as_of: tag += f", as_of={f.as_of[:10]}"
        if f.attribution: tag += f", attributed_to={f.attribution}"
        if f.note: tag += f", note={f.note}"
        shown = f.render() if f.unit != "text" else f.value
        lines.append(f"- {{{{{f.id}}}}} = {f.label} [{tag}] (renders as: {shown})")
    facts_block = "\n".join(lines) or "(no numeric facts: write a conceptual explainer with no figures)"
    risk = ("\nThis category requires you to state plainly that losses are possible." if settings.cat.needs_risk_statement else "")
    fmt = SHORTS_STRUCTURE if settings.video_format == "shorts" else \
        "Structure: hook, context, step-by-step explanation with examples, risks and limitations, summary."
    fb = f"\nPrevious attempt was rejected by the fact-checker. Fix these problems:\n{feedback}\n" if feedback else ""
    return (f"Topic: {topic.title}\nCategory: {settings.cat.label}\nContent type: {settings.content_type}\n"
            f"Target length: about {target_words} words (spoken ~{settings.duration_sec}s).\n{fmt}{risk}\n"
            f"Available verified facts:\n{facts_block}\n{fb}")
