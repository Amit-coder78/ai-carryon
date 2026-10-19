"""
agents_hindi/ab_title_agent.py — Phase 3 for Hindi Channel

Generates 3 Hindi title variations using different psychological
patterns, scores each, picks the winner. Logs to SQLite with
channel="hindi" — kept completely separate from English learning.
"""

import os
import re
from datetime import datetime, timezone

from agents_hindi.model_invoke_agent_hindi import safe_invoke

PATTERNS_HINDI = {
    "curiosity":   "Shuru karo 'Kya Aapko Pata Hai', 'Yeh Kyun Hota Hai' se — curiosity jagao",
    "urgency":     "Urgency use karo: 'Yeh Sab Badal Dega', 'Abhi Dekho Warna', 'Turant Karo'",
    "revelation":  "Secret reveal karo: 'Koi Nahi Batata', 'Chhupa Hua Sach', 'Yeh Nahi Jaante Tum'",
    "number":      "Number use karo: '5 Karan', '3 Baatein', 'Top 7'",
    "question":    "Seedha sawal pucho: 'Kya Yeh Future Hai?', 'Kya AI Sach Mein...?'",
    "warning":     "Warning do: 'Savdhan:', 'Try Karne Se Pehle', 'Yeh Galti Mat Karo'",
    "contrarian":  "Common belief ko challenge karo: 'Sab Galat Hain Iske Baare Mein'",
    "personal":    "Personal banao: 'Yeh Tumhe Affect Karta Hai', 'Tumhara Phone Yeh Kar Raha Hai'",
}

PATTERN_EXAMPLES_HINDI = {
    "curiosity":  "क्या आपको पता है AI अब यह कर सकता है? 🤯",
    "urgency":    "यह अपडेट सब कुछ बदल देगा अभी देखो ⚡",
    "revelation": "कोई नहीं बताता AI के बारे में यह सच 🔥",
    "number":     "3 कारण जो बताते हैं AI क्यों जीत रहा है 🏆",
    "question":   "क्या सच में AI इंसानों से बेहतर है? 🤔",
    "warning":    "सावधान: यह गलती मत करना AI के साथ 🚨",
    "contrarian": "सब गलत हैं इस AI टेक्नोलॉजी के बारे में 💡",
    "personal":   "तुम्हारा फोन अभी यह कर रहा है तुम्हें पता भी नहीं 👀",
}


def generate_title_variation_hindi(topic, script, pattern_name, pattern_instruction):
    example = PATTERN_EXAMPLES_HINDI.get(pattern_name, "")
    prompt = f"""Aap ek Hindi YouTube Shorts title expert hain tech/AI channel ke liye.

Topic: {topic}
Pattern: {pattern_name}
Pattern instruction: {pattern_instruction}
Is style ka example (apne topic ke liye adapt karo, copy mat karo): "{example}"

Topic "{topic}" ke liye ek title likho {pattern_name} pattern follow karte hue.
Rules:
- 60 characters se kam, Hindi/Hinglish mein
- Actual topic concept mention hona chahiye
- Pattern STRICTLY follow karo
- End mein 1 relevant emoji
- Topic ko as-is repeat mat karo

Sirf title return karo, kuch aur nahi."""

    try:
        response = safe_invoke(prompt).content.strip()
        title = response.strip('"\'').strip()
        if len(title) > 70:
            title = title[:67] + "..."
        return title
    except Exception:
        return f"{topic[:50]} 🤖"


def score_title_hindi(title, topic):
    prompt = f"""Topic: {topic}
Title to score: "{title}"

Score this Hindi YouTube Shorts title 1-10 based on:
1. Curiosity gap — worth 3 points
2. Specificity — worth 2 points
3. Emotional trigger — worth 2 points
4. Click-worthiness for Indian tech/AI audience — worth 2 points
5. Length (under 60 chars ideal) — worth 1 point

Return ONLY a number 1-10."""

    try:
        response = safe_invoke(prompt).content.strip()
        match = re.search(r'\b([1-9]|10)\b', response)
        if match:
            return int(match.group())
    except Exception:
        pass
    return 5


def get_best_title_hindi(topic, script, num_variations=3):
    import random

    selected_patterns = random.sample(list(PATTERNS_HINDI.items()), min(num_variations, len(PATTERNS_HINDI)))

    variations = []
    for pattern_name, pattern_instruction in selected_patterns:
        title = generate_title_variation_hindi(topic, script, pattern_name, pattern_instruction)
        score = score_title_hindi(title, topic)
        variations.append({"title": title, "pattern": pattern_name, "score": score})
        print(f"  [Hindi/{pattern_name}] score={score} — {title}")

    variations.sort(key=lambda x: x["score"], reverse=True)
    winner = variations[0]

    result = {
        "winner": winner,
        "variations": variations,
        "topic": topic,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    _log_result_hindi(result)
    return result


def _log_result_hindi(result):
    """Log to SQLite with channel='hindi' — separate from English."""
    try:
        from agents.database import db
        winner = result.get("winner", {})
        # Tag topic with [HI] prefix so it's distinguishable, or use a channel param
        db.log_ab_test(
            topic=f"[HI] {result.get('topic', '')}",
            winner_title=winner.get("title", ""),
            winner_pattern=winner.get("pattern", ""),
            winner_score=winner.get("score", 0),
            all_variations=result.get("variations", []),
            generated_at=result.get("generated_at"),
            channel="hindi",
        )
    except Exception as e:
        print(f"Hindi DB log error: {e}")


if __name__ == "__main__":
    import sys
    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "AI ka naya update"
    script = "Yeh ek naya AI update hai jo sab kuch badal dega..."

    print(f"\nGenerating Hindi title variations for: {topic}\n")
    result = get_best_title_hindi(topic, script)

    print(f"\n🏆 Winner: {result['winner']['title']}")
    print(f"   Pattern: {result['winner']['pattern']} | Score: {result['winner']['score']}/10")
