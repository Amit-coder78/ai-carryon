# agents_bhakti/image_agent.py
"""
Devotional background visuals for the Bhakti channel.

Deliberately scoped to real-world devotional scenery (temples, diyas,
aarti flames, incense smoke, marigold garlands, rivers, sunrise/sunset over
temples, folded hands in prayer) rather than asking any image model to
depict a deity's face/form — that's both a quality risk (AI image models
render deities inconsistently/oddly) and something devotional audiences
are understandably sensitive about. Pexels stock photography of real
temples/rituals is the primary and preferred source; the same-style
prompts are reused for both the static-image and video-clip pipelines.

Reuses the existing, generic download machinery from agents/image_agent.py
and agents/video_clip_agent.py (Pexels search + download) — only the
prompt generation here is devotional-specific.
"""

import os
import re

from agents_bhakti.model_invoke_agent_bhakti import safe_invoke
from agents.image_agent import download_generated_image
from agents.video_clip_agent import download_video_clip

FALLBACK_VISUAL_PROMPTS = [
    "ancient Hindu temple at sunrise, golden light, peaceful",
    "diya oil lamps glowing at temple steps, warm light",
    "hands folded in prayer namaste silhouette, temple background",
    "aarti flame close up, brass lamp, incense smoke rising",
    "marigold flower garlands at temple entrance",
    "Ganges river ghat at dusk, devotees, calm water",
    "temple bells and incense smoke, soft focus",
    "sunset over Hindu temple silhouette, dramatic sky",
]


def generate_bhakti_visual_prompts(topic, script, num=4):
    """Ask the LLM for devotional B-roll ideas, constrained to real-world
    scenery (no deity depictions) so Pexels searches return usable stock
    footage and any AI-image fallback stays respectful and tasteful."""
    prompt = f"""You are a visual director for a Hindi devotional (Bhakti) YouTube Shorts video.

Topic: {topic}
Script excerpt: {script[:300]}

Generate {num} short visual search phrases for REAL-WORLD, non-figurative
devotional scenery that would suit this topic as background footage.

STRICT RULES:
- Do NOT describe a deity's face, body, or form — only temples, diyas,
  aarti flames, incense smoke, flowers/garlands, rivers, temple bells,
  sunrise/sunset over temples, devotees praying (from behind/silhouette),
  folded hands, or similar real, photographable scenery
- Each phrase should be 4-8 words, suitable as a stock-footage search query
- Calm, reverent, cinematic mood — golden hour lighting preferred

Return ONLY a numbered list:
1. <phrase>
2. <phrase>
...
"""
    try:
        response = safe_invoke(prompt).content
        lines = []
        for line in response.split("\n"):
            line = line.strip()
            match = re.match(r"^\d+\.\s*(.+)", line)
            if match:
                lines.append(match.group(1).strip())
        if lines:
            return lines[:num]
    except Exception as e:
        print(f"Bhakti visual prompt generation failed: {e}")

    # Fall back to the static devotional scenery pool
    import random
    return random.sample(FALLBACK_VISUAL_PROMPTS, min(num, len(FALLBACK_VISUAL_PROMPTS)))


def generate_background_clips(topic, script, num_clips=4):
    """Pexels video clips of devotional scenery."""
    folder = "assets/pexels_clips"
    os.makedirs(folder, exist_ok=True)
    for f in os.listdir(folder):
        os.remove(os.path.join(folder, f))

    clean_topic = topic.split("||PATTERN:")[0].strip()
    prompts = generate_bhakti_visual_prompts(clean_topic, script, num_clips)

    print(f"\nSearching Pexels video clips (devotional) for: {clean_topic}")
    for i, p in enumerate(prompts):
        print(f"  {i+1}: {p}")

    clip_paths = []
    errors = []
    used_ids = set()  # no clip repeated inside one video
    for i, prompt in enumerate(prompts):
        output_path = os.path.join(folder, f"{i+1}.mp4")
        try:
            download_video_clip(prompt, output_path, channel="bhakti", used_ids=used_ids)
            clip_paths.append(output_path)
            print(f"Clip {i+1}: downloaded")
        except Exception as e:
            errors.append(f"Clip {i+1}: {e}")
            print(f"Clip {i+1} failed: {e}")

    return clip_paths, errors


def generate_backgrounds(topic, script, num_images=4):
    """Static devotional images fallback (Pexels first, then a tasteful
    scenery-only AI-image fallback)."""
    folder = "assets/backgrounds"
    os.makedirs(folder, exist_ok=True)
    for f in os.listdir(folder):
        os.remove(os.path.join(folder, f))

    clean_topic = topic.split("||PATTERN:")[0].strip()
    prompts = generate_bhakti_visual_prompts(clean_topic, script, num_images)

    print(f"\nGenerated devotional image prompts for: {clean_topic}")
    for i, p in enumerate(prompts):
        print(f"  {i+1}: {p}")

    image_paths = []
    errors = []
    for i, prompt in enumerate(prompts):
        output_path = os.path.join(folder, f"{i+1}.jpg")
        try:
            download_generated_image(prompt, output_path)
            image_paths.append(output_path)
            print(f"Image {i+1}: generated and saved")
        except Exception as e:
            errors.append(f"Image {i+1}: {e}")
            print(f"Image {i+1} failed: {e}")

    return image_paths, errors
