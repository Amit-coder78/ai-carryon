"""
agents_bhakti/ab_title_agent.py — Phase 3 for the Bhakti Channel

Generates 3 Hindi devotional title variations using different (respectful)
psychological patterns, scores each, picks the winner. Logs to SQLite with
channel="bhakti" — kept separate from the other channels' learning data.
"""

import os
import re
from datetime import datetime, timezone

from agents_bhakti.model_invoke_agent_bhakti import safe_invoke

PATTERNS_BHAKTI = {
    "blessing":   "Blessing/jaikara se shuru karo: 'Jai Shri Ram', 'Har Har Mahadev', 'Radhe Radhe'",
    "story_hook": "Kahani ka hook do: 'Yeh Kahani Aapko Rula Degi', 'Kam Log Jaante Hain Yeh Baat'",
    "revelation": "Chhupa gyaan reveal karo: 'Shastron Mein Likha Hai Yeh Rahasya', 'Yeh Sach Kam Logon Ko Pata Hai'",
    "devotion":   "Bhakti bhaav jagao: 'Ek Baar Zaroor Suniye', 'Bhagwan Ki Yeh Leela Anokhi Hai'",
    "question":   "Seedha sawal pucho: 'Kya Aapko Pata Hai...?', 'Kyun Manate Hain Yeh Vrat?'",
    "significance": "Mahatva batao: '5 Minute Mein Jaano Iska Mahatva', 'Iska Arth Jaan Kar Aap Chounk Jayenge'",
    "blessing_promise": "Aashirwad ka vaada: 'Yeh Sunne Se Milta Hai Aashirwad', 'Roz Suno Yeh Katha'",
    "personal":   "Personal banao: 'Aapke Jeevan Mein Yeh Kaam Aayega', 'Har Bhakt Ko Yeh Pata Hona Chahiye'",
}

PATTERN_EXAMPLES_BHAKTI = {
    "blessing":         "जय श्री राम 🙏 यह कथा जरूर सुनें",
    "story_hook":        "यह कहानी आपको रुला देगी 😢🙏",
    "revelation":        "शास्त्रों में लिखा है यह रहस्य 📖",
    "devotion":          "भगवान की यह लीला अनोखी है 🛕",
    "question":          "क्या आपको पता है हनुमान जी क्यों उड़ सकते थे? 🤔",
    "significance":      "5 मिनट में जानो नवरात्रि का महत्व 🔱",
    "blessing_promise":  "रोज सुनो यह कथा, मिलेगा आशीर्वाद 🙏",
    "personal":          "हर भक्त को यह पता होना चाहिए 🕉️",
}


def generate_title_variation_bhakti(topic, script, pattern_name, pattern_instruction):
    example = PATTERN_EXAMPLES_BHAKTI.get(pattern_name, "")
    prompt = f"""Aap ek Hindi devotional (Bhakti) YouTube Shorts title expert hain.

Topic: {topic}
Pattern: {pattern_name}
Pattern instruction: {pattern_instruction}
Is style ka example (apne topic ke liye adapt karo, copy mat karo): "{example}"

Topic "{topic}" ke liye ek title likho {pattern_name} pattern follow karte hue.
Rules:
- 60 characters se kam, Hindi/Hinglish mein
- Respectful aur devotional tone — koi mazak ya galat baat NAHI
- Actual topic/devta/prasang mention hona chahiye
- Pattern STRICTLY follow karo
- End mein 1 relevant emoji (🙏 🛕 🔱 🕉️ etc.)
- Topic ko as-is repeat mat karo

Sirf title return karo, kuch aur nahi."""

    try:
        response = safe_invoke(prompt).content.strip()
        title = response.strip('"\'').strip()
        if len(title) > 70:
            title = title[:67] + "..."
        return title
    except Exception:
        return f"{topic[:50]} 🙏"


def score_title_bhakti(title, topic):
    prompt = f"""Topic: {topic}
Title to score: "{title}"

Score this Hindi devotional YouTube Shorts title 1-10 based on:
1. Curiosity/emotional pull — worth 3 points
2. Respectfulness towards the deity/topic — worth 2 points (0 if disrespectful)
3. Specificity — worth 2 points
4. Click-worthiness for Indian devotional audience — worth 2 points
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


def get_best_title_bhakti(topic, script, num_variations=3):
    import random

    selected_patterns = random.sample(list(PATTERNS_BHAKTI.items()), min(num_variations, len(PATTERNS_BHAKTI)))

    variations = []
    for pattern_name, pattern_instruction in selected_patterns:
        title = generate_title_variation_bhakti(topic, script, pattern_name, pattern_instruction)
        score = score_title_bhakti(title, topic)
        variations.append({"title": title, "pattern": pattern_name, "score": score})
        print(f"  [Bhakti/{pattern_name}] score={score} — {title}")

    variations.sort(key=lambda x: x["score"], reverse=True)
    winner = variations[0]

    result = {
        "winner": winner,
        "variations": variations,
        "topic": topic,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    _log_result_bhakti(result)
    return result


def _log_result_bhakti(result):
    try:
        from agents.database import db
        winner = result.get("winner", {})
        db.log_ab_test(
            topic=f"[BHAKTI] {result.get('topic', '')}",
            winner_title=winner.get("title", ""),
            winner_pattern=winner.get("pattern", ""),
            winner_score=winner.get("score", 0),
            all_variations=result.get("variations", []),
            generated_at=result.get("generated_at"),
            channel="bhakti",
        )
    except Exception as e:
        print(f"Bhakti DB log error: {e}")


if __name__ == "__main__":
    import sys
    topic = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Hanuman ji ka Sanjeevani booti laane ka prasang"
    script = "Ek baar ki baat hai, Lakshman ji moorchit ho gaye the..."

    print(f"\nGenerating Bhakti title variations for: {topic}\n")
    result = get_best_title_bhakti(topic, script)

    print(f"\n🏆 Winner: {result['winner']['title']}")
    print(f"   Pattern: {result['winner']['pattern']} | Score: {result['winner']['score']}/10")
