import streamlit as st
import os
import glob

st.set_page_config(
    page_title="AI CarryON",
    page_icon="🤖",
    layout="wide"
)

from agents.dashboard_sync import sync_all_channel_data
_sync_status = sync_all_channel_data()


st.markdown("""
<style>
/* ── Base ── */
[data-testid="stAppViewContainer"] { background: #0a0a0f; }
[data-testid="stSidebar"] { background: #0f0f1a; border-right: 1px solid #1e1e2e; }
section.main > div { padding-top: 1.5rem; }

/* ── Typography ── */
h1, h2, h3 { color: #e2e8f0 !important; font-family: 'Inter', sans-serif; letter-spacing: -0.5px; }
p, label, .stMarkdown { color: #94a3b8 !important; }

/* ── Tabs ── */
[data-testid="stTabs"] button {
    color: #64748b !important;
    font-weight: 600;
    border-bottom: 2px solid transparent;
    padding: 0.5rem 1.5rem;
}
[data-testid="stTabs"] button[aria-selected="true"] {
    color: #38bdf8 !important;
    border-bottom: 2px solid #38bdf8 !important;
    background: transparent !important;
}

/* ── Primary button ── */
[data-testid="stButton"] > button[kind="primary"] {
    background: linear-gradient(135deg, #0ea5e9, #6366f1) !important;
    color: white !important;
    border: none !important;
    border-radius: 8px !important;
    padding: 0.6rem 2rem !important;
    font-weight: 700 !important;
    font-size: 1rem !important;
    letter-spacing: 0.5px !important;
    box-shadow: 0 0 20px rgba(14,165,233,0.3) !important;
    transition: all 0.2s ease !important;
}
[data-testid="stButton"] > button[kind="primary"]:hover {
    box-shadow: 0 0 30px rgba(14,165,233,0.5) !important;
    transform: translateY(-1px) !important;
}

/* ── Secondary buttons ── */
[data-testid="stButton"] > button {
    background: #1e1e2e !important;
    color: #94a3b8 !important;
    border: 1px solid #2d2d3d !important;
    border-radius: 8px !important;
}

/* ── Input ── */
[data-testid="stTextInput"] input {
    background: #1e1e2e !important;
    border: 1px solid #2d2d3d !important;
    color: #e2e8f0 !important;
    border-radius: 8px !important;
    font-size: 1rem !important;
}
[data-testid="stTextInput"] input:focus {
    border-color: #38bdf8 !important;
    box-shadow: 0 0 0 2px rgba(56,189,248,0.15) !important;
}

/* ── Radio ── */
[data-testid="stRadio"] label { color: #94a3b8 !important; }
[data-testid="stRadio"] [data-testid="stMarkdownContainer"] p { color: #e2e8f0 !important; }

/* ── Toggle ── */
[data-testid="stToggle"] label { color: #94a3b8 !important; }

/* ── Info / Success / Warning boxes ── */
[data-testid="stAlert"] {
    border-radius: 8px !important;
    border-left-width: 3px !important;
}

/* ── Code blocks (prompts) ── */
[data-testid="stCode"] {
    background: #1e1e2e !important;
    border: 1px solid #2d2d3d !important;
    border-radius: 8px !important;
    white-space: pre-wrap !important;
    word-break: break-word !important;
}
[data-testid="stCode"] code {
    color: #7dd3fc !important;
    font-size: 0.85rem !important;
    line-height: 1.6 !important;
}

/* ── Divider ── */
hr { border-color: #1e1e2e !important; }

/* ── Selectbox ── */
[data-testid="stSelectbox"] > div > div {
    background: #1e1e2e !important;
    border: 1px solid #2d2d3d !important;
    color: #e2e8f0 !important;
    border-radius: 8px !important;
}

/* ── File uploader ── */
[data-testid="stFileUploader"] {
    background: #1e1e2e !important;
    border: 1px dashed #2d2d3d !important;
    border-radius: 8px !important;
    padding: 1rem !important;
}

/* ── Metrics ── */
[data-testid="stMetric"] {
    background: #1e1e2e;
    border: 1px solid #2d2d3d;
    border-radius: 10px;
    padding: 1rem;
}
[data-testid="stMetricValue"] { color: #38bdf8 !important; }

/* ── Spinner ── */
[data-testid="stSpinner"] { color: #38bdf8 !important; }
</style>
""", unsafe_allow_html=True)

def show_analytics():
    st.title("📊 YouTube Analytics Dashboard")
    col_refresh, col_auto, col_last = st.columns([1, 2, 3])
    with col_refresh:
        refresh = st.button("🔄 Refresh Now")
    with col_auto:
        auto_refresh = st.toggle("⏱️ Auto-refresh every 30 min", value=False)
    import time
    if "last_refresh" not in st.session_state:
        st.session_state["last_refresh"] = time.time()
    if auto_refresh:
        elapsed = time.time() - st.session_state["last_refresh"]
        remaining = max(0, 1800 - int(elapsed))
        mins, secs = divmod(remaining, 60)
        with col_last:
            st.caption(f"⏳ Next refresh in: {mins:02d}:{secs:02d}")
        if elapsed >= 1800:
            st.session_state["last_refresh"] = time.time()
            st.rerun()
    if refresh:
        st.session_state["last_refresh"] = time.time()
        st.rerun()
    with st.spinner("Fetching channel data..."):
        from agents.analytics_agent import get_channel_stats, get_recent_videos
        stats = get_channel_stats()
        videos = get_recent_videos(20)
    if auto_refresh:
        import streamlit as _st
        _st.markdown('<meta http-equiv="refresh" content="1800">', unsafe_allow_html=True)
    st.subheader("📡 Channel Overview")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("👤 Subscribers", f"{stats['subscribers']:,}")
    col2.metric("👁️ Total Views", f"{stats['total_views']:,}")
    col3.metric("🎬 Videos", f"{stats['video_count']:,}")
    if videos:
        avg_views = sum(v['views'] for v in videos) // len(videos)
        col4.metric("📈 Avg Views", f"{avg_views:,}")
    st.divider()
    if not videos:
        st.info("No videos found.")
        return
    st.subheader("🏆 Top Videos by Views")
    import pandas as pd
    df = pd.DataFrame(videos)
    df['short_title'] = df['title'].str[:30] + "..."
    st.bar_chart(df.set_index('short_title')['views'])
    st.divider()
    st.subheader("📋 All Videos")
    for v in videos:
        with st.container():
            c1, c2, c3, c4, c5 = st.columns([4, 1, 1, 1, 1])
            c1.markdown(f"[{v['title']}]({v['url']})")
            c2.markdown(f"👁️ **{v['views']:,}**")
            c3.markdown(f"👍 {v['likes']:,}")
            c4.markdown(f"💬 {v['comments']:,}")
            c5.markdown(f"📅 {v['published']}")
        st.divider()

@st.cache_data(ttl=300, show_spinner=False)
def _cached_channel_data(key, _get_stats, _get_videos):
    """5-min cache so idle tabs don't call the YouTube API on every rerun (errors are not cached)."""
    return _get_stats(), _get_videos(20)


def render_channel_analytics(key, label, emoji, get_stats, get_videos, track_fn, token_hint, peak_label):
    """Shared analytics view used by Hindi and Bhakti (same layout as English/Cricket/Gaming)."""
    import pandas as pd
    st.title(f"📊 {label} Channel Analytics")
    st.caption(f"Live stats pulled from the {label.lower()} channel's own YouTube credentials, and snapshot history from the shared database.")

    col_refresh, col_track = st.columns([1, 2])
    with col_refresh:
        if st.button("🔄 Refresh", key=f"{key}_analytics_refresh"):
            _cached_channel_data.clear()
            st.rerun()
    with col_track:
        if st.button("📸 Track views now", key=f"{key}_track_now",
                     help=f"Manually pull current view counts for {label.lower()} videos into the DB — normally happens automatically ~hourly."):
            with st.spinner("Fetching current view counts from YouTube..."):
                try:
                    tracked = track_fn()
                    _cached_channel_data.clear()
                    st.success(f"✅ Tracked {len(tracked)} {label.lower()} video(s)")
                except Exception as e:
                    st.error(f"Tracking failed: {e}")

    st.divider()

    with st.spinner(f"Fetching {label.lower()} channel data..."):
        try:
            ch_stats, ch_videos = _cached_channel_data(key, get_stats, get_videos)
            fetch_error = None
        except Exception as e:
            ch_stats, ch_videos, fetch_error = None, [], str(e)

    if fetch_error:
        st.error(f"Couldn't reach the {label.lower()} channel's YouTube API: {fetch_error}")
        st.caption(token_hint)
    else:
        st.subheader("📡 Channel Overview")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("👤 Subscribers", f"{ch_stats['subscribers']:,}")
        m2.metric("👁️ Total Views", f"{ch_stats['total_views']:,}")
        m3.metric("🎬 Videos", f"{ch_stats['video_count']:,}")
        if ch_videos:
            m4.metric("📈 Avg Views", f"{sum(v['views'] for v in ch_videos) // len(ch_videos):,}")

        st.divider()

        if not ch_videos:
            st.info(f"No {label.lower()} videos found yet.")
        else:
            st.subheader("🏆 Top Videos by Views")
            df = pd.DataFrame(ch_videos)
            df["short_title"] = df["title"].str[:30] + "..."
            st.bar_chart(df.set_index("short_title")["views"])

            st.divider()
            st.subheader(f"📋 All {label} Videos")
            for v in ch_videos:
                with st.container():
                    vc1, vc2, vc3, vc4, vc5 = st.columns([4, 1, 1, 1, 1])
                    vc1.markdown(f"[{v['title']}]({v['url']})")
                    vc2.markdown(f"👁️ **{v['views']:,}**")
                    vc3.markdown(f"👍 {v['likes']:,}")
                    vc4.markdown(f"💬 {v['comments']:,}")
                    vc5.markdown(f"📅 {v['published']}")
                st.divider()

    st.caption(f"For hour-by-hour peak upload windows, see the **Peak Hours** and **Schedule** pages in the sidebar — select \"{peak_label}\" there. "
               "Title experiments are on **A/B Titles**, competitor benchmarks on **Comparison**.")



def render_spy_rows(key, items, button_label=None, on_click=None):
    """Shared Trending Spy row layout used by all five channels.
    items: list of dicts with title, url (optional), source, views, note."""
    if not items:
        st.warning("No trending topics found right now.")
        return
    for i, t in enumerate(items):
        with st.container():
            c1, c2, c3, c4, c5 = st.columns([4, 2, 1, 2, 2])
            title = t.get("title", "")
            c1.markdown(f"[{title}]({t['url']})" if t.get("url") else f"**{title}**")
            c2.markdown(f"📺 {t.get('source', '')}")
            c3.markdown(f"👁️ {t['views']:,}" if isinstance(t.get("views"), int) else "")
            c4.markdown(f"🔥 {str(t.get('note', ''))[:30]}")
            if button_label and on_click:
                if c5.button(button_label, key=f"{key}_spy_{i}"):
                    on_click(t)
        st.divider()


def channel_nav(key, options, default=None):
    """In-tab navigation (replaces the old sidebar selectboxes, which all
    showed at once and collided across channels)."""
    return st.radio("📂 Navigation", options, horizontal=True, key=f"{key}_nav",
                    index=options.index(default) if default in options else 0)

# ── Password protection ──────────────────────────────────────────
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False

if not st.session_state["authenticated"]:
    st.title("🔐 AI CarryON - Login")
    st.info(
        "This panel is password protected — it controls live video generation and "
        "uploads to real YouTube channels using API credentials, so public access isn't allowed."
    )
    password = st.text_input("Enter Password", type="password")
    if st.button("Login"):
        if password == os.getenv("APP_PASSWORD", "aicarryon2026"):
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Wrong password!")
    st.stop()

# ── Main Tabs ────────────────────────────────────────────────────
english_tab, hindi_tab, cricket_tab, gaming_tab, bhakti_tab = st.tabs(["🇬🇧 English Channel", "🇮🇳 Hindi Channel", "🏏 Cricket Channel", "🎮 Gaming Channel", "🕉️ Bhakti Channel"])

# ════════════════════════════════════════════════════════════════
# ENGLISH CHANNEL
# ════════════════════════════════════════════════════════════════
with english_tab:
    if st.session_state.get("go_generate"):
        st.session_state["go_generate"] = False
        st.session_state["english_nav"] = "🎬 Generate Video"
    page = channel_nav("english", ["🎬 Generate Video", "📊 Analytics", "🕵️ Trending Spy"])

    if page == "🕵️ Trending Spy":
        st.title("🕵️ Trending Spy")
        st.markdown("Top performing Shorts from leading AI/Tech channels — click to generate your own version!")
        with st.spinner("Fetching trending topics from top channels..."):
            from agents.spy_agent import get_trending_topics
            topics = get_trending_topics()
        if not topics:
            st.warning("No topics found.")
        for t in topics or []:
            with st.container():
                c1, c2, c3, c4, c5 = st.columns([4, 1, 1, 1, 2])
                c1.markdown(f"[{t['title']}]({t['url']})")
                c2.markdown(f"📺 **{t['channel']}**")
                c3.markdown(f"👁️ {t['views']:,}")
                c4.markdown(f"👍 {t['likes']:,}")
                if c5.button("🎬 Make This Video", key=t['url']):
                    st.session_state["trending_topic"] = t["topic"]
                    st.session_state["go_generate"] = True
                    st.rerun()
            st.divider()

    elif page == "📊 Analytics":
        show_analytics()

    else:
        # Generate Video page
        from agents.research_agent import research
        from agents.script_agent import create_script
        from agents.seo_agent import generate_seo
        from agents.thumbnail_agent import generate_thumbnail_text
        from agents.thumbnail_generator import generate_thumbnail
        from agents.image_agent import generate_backgrounds
        from agents.video_clip_agent import generate_background_clips
        from agents.voice_agent import generate_voice
        from agents.caption_agent import create_srt
        from agents.video_agent import create_video
        from agents.manim_agent import render_manim_animation
        from agents.upload_agent import upload_video

        st.title("🤖 AI CarryON")
        st.markdown("Generate AI-powered YouTube Shorts automatically")

        topic = st.text_input(
            "Enter Topic",
            placeholder="What is LangChain?",
            value=st.session_state.get("trending_topic", ""),
            key="english_topic_input"
        )

        if st.button("🔥 Use Trending Topic Instead"):
            with st.spinner("Fetching trending topic..."):
                from agents.trending_agent import get_trending_topic
                try:
                    trending = get_trending_topic()
                    st.session_state["trending_topic"] = trending
                    st.rerun()
                except Exception as e:
                    st.error(str(e))

        if "trending_topic" in st.session_state:
            topic = st.session_state["trending_topic"]
            st.success(f"Trending topic: **{topic}**")

        auto_upload = st.toggle("Auto-upload to YouTube after generation", value=False)

        st.markdown("---")
        st.markdown("**🎬 Video Background Mode**")
        video_mode_pre = st.radio(
            "Video Background Mode",
            ["🎥 Flow clips — cinematic AI video (recommended)", "🖼️ Auto image backgrounds — no upload needed"],
            index=0,
            label_visibility="collapsed",
            key="video_mode_radio",
            horizontal=True
        )
        if "Flow clips" in video_mode_pre:
            st.info("📋 After generating, copy the Veo prompts → go to [labs.google/flow](https://labs.google/flow) → generate VIDEO clips → download MP4s → upload them back here")
        else:
            st.info("🖼️ Pexels backgrounds will be generated automatically — no upload needed")
        st.markdown("---")

        col_gen, col_clear = st.columns([3, 1])
        with col_gen:
            generate_clicked = st.button("Generate", type="primary")
        with col_clear:
            if st.button("🔄 New Topic"):
                for k in ["eng_research","eng_script","eng_seo","eng_prompts","eng_thumb_text","eng_thumb_img","eng_topic_done"]:
                    st.session_state.pop(k, None)
                st.rerun()

        if generate_clicked:
            if not topic.strip():
                st.warning("Please enter a topic.")
                st.stop()
            st.session_state["eng_topic_done"] = topic
            try:
                with st.spinner("🔍 Researching..."):
                    st.session_state["eng_research"] = research(topic)
                with st.spinner("✍️ Generating Script..."):
                    st.session_state["eng_script"] = create_script(st.session_state.get("eng_research", ""))
                with st.spinner("📈 Generating SEO..."):
                    st.session_state["eng_seo"] = generate_seo(topic, st.session_state.get("eng_script", ""))
                with st.spinner("🎬 Generating Flow/Veo Prompts..."):
                    from agents.flow_prompt_agent import generate_flow_prompts
                    st.session_state["eng_prompts"] = generate_flow_prompts(topic, st.session_state.get("eng_script", ""), num_clips=3)
                with st.spinner("🎯 Generating Thumbnail Text..."):
                    st.session_state["eng_thumb_text"] = generate_thumbnail_text(topic)
                with st.spinner("🖼️ Generating Thumbnail Image..."):
                    st.session_state["eng_thumb_img"] = generate_thumbnail(st.session_state.get("eng_seo", {}).get("title", ""), topic)

            except Exception as e:
                st.error(f"Generation failed: {e}")
                st.stop()

        if st.session_state.get("eng_topic_done"):
            research_data = st.session_state.get("eng_research", "")
            script = st.session_state.get("eng_script", "")
            seo = st.session_state.get("eng_seo", {})
            flow_prompts = st.session_state.get("eng_prompts", [])
            thumbnail_text = st.session_state.get("eng_thumb_text", "")
            thumbnail_image = st.session_state.get("eng_thumb_img", "")

            try:
                st.subheader("📚 Research")
                st.write(research_data)
                st.subheader("📝 YouTube Script")
                st.write(script)
                st.subheader("📈 SEO")
                st.markdown(f"**Title:** {seo['title']}")
                st.markdown(f"**Description:** {seo['description']}")
                st.markdown(f"**Hashtags:** {seo['hashtags']}")

                use_flow_clips = "Flow clips" in st.session_state.get("video_mode_radio", "")

                # ── Flow Prompts ──────────────────────────────────
                st.divider()
                st.subheader("🎬 Flow / Veo 3 Cinematic Prompts")
                if use_flow_clips:
                    st.info("📋 **Step 1:** Copy prompt → **Step 2:** Paste in [labs.google/flow](https://labs.google/flow) → Generate VIDEO → **Step 3:** Download MP4 → **Step 4:** Upload below")
                else:
                    st.caption("💡 Reference prompts — auto image mode is active")

                for i, fp in enumerate(flow_prompts):
                    st.markdown(f"**Clip {i+1}**")
                    st.code(fp, language=None)

                if use_flow_clips:
                    st.subheader("📤 Upload Your Flow Clips")
                    uploaded_clips = st.file_uploader(
                        "Upload all MP4 clips here, then scroll down to Generate Video",
                        type=["mp4", "mov"],
                        accept_multiple_files=True,
                        key="flow_clips_uploader"
                    )
                    if uploaded_clips:
                        os.makedirs("assets/flow_clips", exist_ok=True)
                        for f in glob.glob("assets/flow_clips/*.mp4"):
                            os.remove(f)
                        for i, clip in enumerate(uploaded_clips):
                            clip_path = f"assets/flow_clips/clip_{i:02d}.mp4"
                            with open(clip_path, "wb") as f:
                                f.write(clip.read())
                        st.success(f"✅ {len(uploaded_clips)} clip(s) ready — cinematic video mode active")
                    else:
                        if os.path.isdir("assets/flow_clips"):
                            for f in glob.glob("assets/flow_clips/*.mp4"):
                                os.remove(f)
                        st.warning("⬆️ Upload your downloaded Flow clips above to proceed with cinematic mode")
                else:
                    if os.path.isdir("assets/flow_clips"):
                        for f in glob.glob("assets/flow_clips/*.mp4"):
                            os.remove(f)

                st.subheader("🖼️ Thumbnail Text")
                st.success(thumbnail_text)
                st.subheader("🖼️ Thumbnail Image")
                st.image(thumbnail_image, width="stretch")

                # Skip Pexels if Flow clips mode is active
                _flow_clips_exist = bool(glob.glob("assets/flow_clips/*.mp4"))
                _use_flow = "Flow clips" in st.session_state.get("video_mode_radio", "")
                if _use_flow and not _flow_clips_exist:
                    st.warning("⏸️ Flow clips mode is ON — upload your 3 clips above first, then click Generate Video below")
                    st.stop()
                elif _flow_clips_exist:
                    image_paths, image_errors = [], []
                    st.success("🎥 Flow clips detected — using cinematic clips")
                else:
                    with st.spinner("🎨 Generating Background Images..."):
                        image_paths, image_errors = generate_background_clips(topic, script, num_clips=4)
                        if len(image_paths) < 2:
                            image_paths, image_errors = generate_backgrounds(topic, script, num_images=4)
                            st.session_state["use_pexels"] = False
                        else:
                            st.session_state["use_pexels"] = True
                if image_errors:
                    st.warning("Some images failed to generate:")
                    for err in image_errors:
                        st.text(err)
                if image_paths:
                    st.subheader("🖼️ Generated Backgrounds")
                    cols = st.columns(len(image_paths))
                    for col, img_path in zip(cols, image_paths):
                        if str(img_path).lower().endswith(".mp4"):
                            col.video(img_path)
                        else:
                            col.image(img_path)
                elif not (_use_flow or _flow_clips_exist):
                    st.error("No background images were generated. Cannot continue.")
                    st.stop()

                with st.spinner("🎤 Generating Voiceover..."):
                    voice_file = generate_voice(script)
                st.subheader("🔊 Voiceover")
                st.audio(voice_file)

                with st.spinner("✏️ Generating Captions..."):
                    caption_file = create_srt(script, voice_file)
                st.subheader("📄 Captions")
                st.code(open(caption_file).read())

                if st.button("🎬 Generate Final Video", key="eng_make_video_btn", type="primary"):
                    with st.spinner("🎬 Creating Final Video..."):
                        video_file = create_video(manim_path=None, use_flow_clips=_flow_clips_exist, use_pexels_clips=st.session_state.get("use_pexels", False))
                    st.session_state["eng_video_file"] = video_file

                from moviepy import AudioFileClip as AFC
                duration = AFC("output/voice.mp3").duration
                if duration > 60:
                    st.warning(f"⚠️ Video is {duration:.1f}s — over 60s Shorts limit!")
                else:
                    st.success(f"✅ Shorts-ready! Duration: {duration:.1f}s")

                st.subheader("🎥 Generated Video")
                st.video(video_file)
                st.success("✅ Video Created Successfully!")

                with open(video_file, "rb") as f:
                    st.download_button(
                        label="📱 Download for Instagram Reels",
                        data=f,
                        file_name="reel.mp4",
                        mime="video/mp4"
                    )

                st.info("""**📱 Post to Instagram Reels manually:**
1. Download the video above
2. Open Instagram on your phone
3. Tap **+** → **Reel**
4. Select the downloaded video
5. Add caption and post!""")

                if auto_upload:
                    with st.spinner("📤 Uploading to YouTube..."):
                        video_id, video_url = upload_video(
                            video_path=video_file,
                            title=seo["title"],
                            description=seo["description"],
                            hashtags=seo["hashtags"],
                            thumbnail_path=thumbnail_image
                        )
                    st.subheader("📤 YouTube Upload")
                    st.success("Uploaded successfully!")
                    st.markdown(f"**Watch here:** [{video_url}]({video_url})")
                else:
                    st.info("Auto-upload is OFF. Toggle it on to upload directly to YouTube.")

            except Exception as e:
                st.error(str(e))

# ════════════════════════════════════════════════════════════════
# HINDI CHANNEL
# ════════════════════════════════════════════════════════════════
with hindi_tab:
    # Handle redirect from Banao button (must set widget state before the widget exists)
    if st.session_state.get("hindi_nav_override"):
        st.session_state["hindi_nav_nav"] = st.session_state["hindi_nav_override"]
        st.session_state["hindi_nav_override"] = None
    hindi_page = channel_nav("hindi_nav", ["🎬 Video Banao", "📊 Analytics", "🕵️ Trending Spy"])

    # ── Trending Spy ─────────────────────────────────────────────
    if hindi_page == "🕵️ Trending Spy":
        st.title("🕵️ Hindi Trending Spy")
        st.markdown("Top Hindi Tech channels ke viral topics — click karo apna version banane ke liye!")

        with st.spinner("Hindi channels ke trending topics fetch ho rahe hain..."):
            try:
                from agents_hindi.spy_agent import get_hindi_trending_topics
                topics = get_hindi_trending_topics()
            except Exception as e:
                st.error(str(e))
                topics = []

        if not topics:
            st.warning("Koi topics nahi mile.")
        else:
            for i, t in enumerate(topics):
                with st.container():
                    c1, c2, c3, c4, c5 = st.columns([4, 1, 1, 1, 2])
                    c1.markdown(f"**{t['title']}**")
                    c2.markdown(f"📺 {t['channel']}")
                    c3.markdown(f"👁️ {t['views']:,}")
                    c4.markdown(f"🔥 {t.get('why_trending', 'Viral')[:30]}")
                    if c5.button("🎬 Banao", key=f"hindi_{i}_{t['topic'][:20]}"):
                        st.session_state["hindi_topic"] = t["topic"]
                        st.session_state["hindi_competitor"] = t
                        st.session_state["hindi_nav_override"] = "🎬 Video Banao"
                        st.rerun()
                st.divider()

    # ── Analytics ────────────────────────────────────────────────
    elif hindi_page == "📊 Analytics":
        from agents_hindi.analytics_agent import get_channel_stats as _hi_stats, get_recent_videos as _hi_videos
        from agents_hindi.view_tracker_agent import track_views_hindi as _hi_track
        render_channel_analytics(
            key="hindi", label="Hindi", emoji="🇮🇳",
            get_stats=_hi_stats, get_videos=_hi_videos, track_fn=_hi_track,
            token_hint="Check that the Hindi token (HINDI_TOKEN_JSON) is set and includes the youtube.readonly scope.",
            peak_label="Hindi AI CarryON",
        )

    # ── Generate Video ───────────────────────────────────────────
    else:
        from agents_hindi.script_agent import create_script as hindi_create_script
        from agents_hindi.seo_agent import generate_seo as hindi_generate_seo
        from agents_hindi.voice_agent import generate_voice as hindi_generate_voice
        from agents_hindi.trending_agent import get_trending_topic as hindi_get_trending

        st.title("🇮🇳 AI CarryON - Hindi Channel")
        st.markdown("Hindi YouTube Shorts ke liye AI-powered video banao!")

        if st.session_state.get("hindi_go_generate"):
            st.session_state["hindi_go_generate"] = False

        # Auto-fill from trending spy or trending button
        prefilled = st.session_state.get("hindi_topic", "")
        hindi_topic = st.text_input(
            "Topic daalo (Hindi ya English mein)",
            placeholder="Artificial Intelligence kya hai?",
            value=prefilled,
            key="hindi_topic_input"
        )

        col_trend, col_clear = st.columns([2, 1])
        with col_trend:
            if st.button("🔥 Trending Topic Lo (India)", key="hindi_trending_btn"):
                with st.spinner("India ka trending topic fetch ho raha hai..."):
                    try:
                        t = hindi_get_trending(region_code="IN")
                        st.session_state["hindi_topic"] = t
                        st.session_state["hindi_competitor"] = None
                        st.rerun()
                    except Exception as e:
                        st.error(str(e))
        with col_clear:
            if st.button("🗑️ Clear", key="hindi_clear_btn"):
                st.session_state.pop("hindi_topic", None)
                st.session_state.pop("hindi_competitor", None)
                st.rerun()

        if st.session_state.get("hindi_topic"):
            st.success(f"✅ Topic ready: **{st.session_state['hindi_topic']}**")
            if st.session_state.get("hindi_competitor"):
                st.caption(f"📺 Source: {st.session_state['hindi_competitor'].get('channel', '')} — {st.session_state['hindi_competitor'].get('why_trending', '')}")

        hindi_upload = st.toggle(
            "Auto-upload to Hindi YouTube Channel",
            value=False,
            key="hindi_upload_toggle"
        )

        if st.button("🎬 Hindi Video Banao", key="hindi_generate_btn"):
            if not hindi_topic.strip():
                st.warning("Pehle topic daalo!")
            else:
                try:
                    with st.spinner("🔍 Research ho raha hai..."):
                        from agents.research_agent import research
                        st.session_state["hindi_research"] = research(hindi_topic)
                    with st.spinner("✍️ Hindi script likh raha hai..."):
                        st.session_state["hindi_script"] = hindi_create_script(st.session_state["hindi_research"])
                    with st.spinner("📈 Hindi SEO generate ho raha hai..."):
                        competitor_data = st.session_state.get("hindi_competitor", None)
                        st.session_state["hindi_seo"] = hindi_generate_seo(hindi_topic, st.session_state["hindi_script"], competitor_data=competitor_data)
                    with st.spinner("🖼️ Thumbnail ban raha hai..."):
                        from agents.thumbnail_generator import generate_thumbnail
                        st.session_state["hindi_thumb"] = generate_thumbnail(st.session_state["hindi_seo"]["title"], hindi_topic)
                    with st.spinner("🌆 Background images fetch ho rahi hain..."):
                        from agents.image_agent import generate_backgrounds
                        from agents.video_clip_agent import generate_background_clips
                        imgs, errs = generate_background_clips(hindi_topic, st.session_state["hindi_script"], num_clips=4)
                        if len(imgs) < 2:
                            imgs, errs = generate_backgrounds(hindi_topic, st.session_state["hindi_script"], num_images=4)
                            st.session_state["hindi_use_pexels"] = False
                        else:
                            st.session_state["hindi_use_pexels"] = True
                        st.session_state["hindi_images"] = imgs
                        st.session_state["hindi_img_errors"] = errs
                    with st.spinner("🎙️ Hindi awaaz generate ho rahi hai..."):
                        st.session_state["hindi_voice"] = hindi_generate_voice(st.session_state["hindi_script"])
                    with st.spinner("💬 Captions ban rahe hain..."):
                        from agents.caption_agent import create_srt
                        st.session_state["hindi_captions"] = create_srt(st.session_state["hindi_script"], st.session_state["hindi_voice"])
                    with st.spinner("🎬 Flow/Veo Prompts generate ho rahe hain..."):
                        from agents_hindi.flow_prompt_agent import generate_flow_prompts_hindi
                        st.session_state["hindi_prompts"] = generate_flow_prompts_hindi(
                            st.session_state.get("hindi_topic", ""), st.session_state["hindi_script"]
                        )
                    st.session_state["hindi_generated"] = True
                    st.session_state["hindi_video_file"] = None
                except Exception as e:
                    st.error(str(e))
                    import traceback
                    st.code(traceback.format_exc())

        # ── Display all generated content from session state ──
        if st.session_state.get("hindi_generated"):
                research_data = st.session_state.get("hindi_research", "")
                script = st.session_state.get("hindi_script", "")
                seo = st.session_state.get("hindi_seo", {})

                st.subheader("📚 Research")
                st.write(research_data)
                st.subheader("📝 Hindi Script")
                st.write(script)
                st.subheader("📈 SEO")
                st.markdown(f"**Title:** {seo.get('title','')}")
                st.markdown(f"**Description:** {seo.get('description','')}")
                st.markdown(f"**Hashtags:** {seo.get('hashtags','')}")

                st.subheader("🖼️ Thumbnail")
                st.image(st.session_state.get("hindi_thumb"), width="stretch")

                image_paths = st.session_state.get("hindi_images", [])
                image_errors = st.session_state.get("hindi_img_errors", [])
                if image_errors:
                    for err in image_errors:
                        st.warning(err)
                if image_paths:
                    st.subheader("🖼️ Background Images")
                    cols = st.columns(len(image_paths))
                    for col, img_path in zip(cols, image_paths):
                        if str(img_path).lower().endswith(".mp4"):
                            col.video(img_path)
                        else:
                            col.image(img_path)

                voice = st.session_state.get("hindi_voice")
                st.subheader("🔊 Hindi Awaaz")
                if voice:
                    st.audio(voice)

                caption_file = st.session_state.get("hindi_captions")
                st.subheader("📄 Captions")
                if caption_file:
                    st.code(open(caption_file).read())

                hindi_flow_prompts = st.session_state.get("hindi_prompts", [])

                st.subheader("🎬 Flow Video Prompts (Google Flow / Veo 3)")
                st.info("Har prompt copy karo → labs.google/flow mein paste karo → clip download karo → neeche upload karo")

                for i, fp in enumerate(hindi_flow_prompts):
                    st.code(fp, language=None)

                st.divider()
                st.subheader("🎬 Video Mode Chuno")

                col_auto, col_flow = st.columns(2)
                with col_auto:
                    if st.button("🤖 Auto (Pexels images)", key="hindi_mode_auto",
                                 type="primary" if st.session_state.get("hindi_mode") != "flow" else "secondary"):
                        st.session_state["hindi_mode"] = "auto"
                        if os.path.isdir("assets/flow_clips"):
                            for f in glob.glob("assets/flow_clips/*.mp4"):
                                os.remove(f)
                with col_flow:
                    if st.button("🎬 Flow clips use karo", key="hindi_mode_flow",
                                 type="primary" if st.session_state.get("hindi_mode") == "flow" else "secondary"):
                        st.session_state["hindi_mode"] = "flow"

                hindi_mode = st.session_state.get("hindi_mode", "auto")

                if hindi_mode == "flow":
                    st.success("✅ Flow clips mode active hai")
                    st.markdown("**📤 Teeno clips upload karo (MP4):**")
                    hindi_uploaded_clips = st.file_uploader(
                        "Flow clips upload karo",
                        type=["mp4", "mov"],
                        accept_multiple_files=True,
                        key="hindi_flow_clips_uploader"
                    )
                    if hindi_uploaded_clips:
                        os.makedirs("assets/flow_clips", exist_ok=True)
                        for f in glob.glob("assets/flow_clips/*.mp4"):
                            os.remove(f)
                        for i, clip in enumerate(hindi_uploaded_clips):
                            clip_path = f"assets/flow_clips/clip_{i:02d}.mp4"
                            with open(clip_path, "wb") as f:
                                f.write(clip.read())
                        st.success(f"✅ {len(hindi_uploaded_clips)} clip(s) upload ho gaye!")
                    else:
                        st.warning("⚠️ Abhi tak koi clip upload nahi hua — clips upload karo phir Video Banao click karo")
                else:
                    st.info("🤖 Auto mode — Pexels se background images use honge")
                    if os.path.isdir("assets/flow_clips"):
                        for f in glob.glob("assets/flow_clips/*.mp4"):
                            os.remove(f)

                _hindi_clips_exist = bool(glob.glob("assets/flow_clips/*.mp4"))
                _hindi_use_flow = hindi_mode == "flow"

                if _hindi_use_flow and not _hindi_clips_exist:
                    st.warning("⬆️ Pehle clips upload karo, phir Video Banao click karo")
                else:
                    if st.button("🎬 Video Banao (Final)", key="hindi_make_video_btn", type="primary"):
                        with st.spinner("🎬 Video ban raha hai..."):
                            from agents.video_agent import create_video
                            st.session_state["hindi_video_file"] = create_video(use_flow_clips=_hindi_clips_exist, use_pexels_clips=st.session_state.get("hindi_use_pexels", False))
                            for _f in glob.glob("assets/flow_clips/*.mp4"):
                                os.remove(_f)

                video_file = st.session_state.get("hindi_video_file")
                if video_file:
                    from moviepy import AudioFileClip as AFC
                    duration = AFC("output/voice.mp3").duration
                    if duration > 60:
                        st.warning(f"⚠️ Video {duration:.1f}s ka hai — 60s Shorts limit se zyada!")
                    else:
                        st.success(f"✅ Shorts-ready! Duration: {duration:.1f}s")

                    st.subheader("🎥 Generated Hindi Video")
                    st.video(video_file)
                    st.success("✅ Video ban gaya!")

                    with open(video_file, "rb") as f:
                        st.download_button(
                            label="📱 Instagram Reels ke liye Download karo",
                            data=f,
                            file_name="hindi_reel.mp4",
                            mime="video/mp4",
                            key="hindi_download"
                        )

                    if hindi_upload:
                        with st.spinner("📤 YouTube Hindi channel par upload ho raha hai..."):
                            from agents_hindi.upload_agent import upload_video as hindi_upload_fn
                            video_id, video_url = hindi_upload_fn(
                                video_path=video_file,
                                title=seo.get("title",""),
                                description=seo.get("description",""),
                                hashtags=seo.get("hashtags",""),
                                thumbnail_path=st.session_state.get("hindi_thumb")
                            )
                        st.success("✅ YouTube par upload ho gaya!")
                        st.balloons()
                        st.markdown(f"**▶️ Yahan dekho:** [{video_url}]({video_url})")
                        st.session_state.pop("hindi_topic", None)
                        st.session_state.pop("hindi_competitor", None)
                    else:
                        st.info("💡 Auto-upload OFF hai. Toggle on karo YouTube par seedha upload karne ke liye.")

# ════════════════════════════════════════════════════════════════
# GAMING CHANNEL (fully automated — Twitch clips -> Shorts -> YouTube,
# runs on GitHub Actions cron via scheduler_gaming.py)
# ════════════════════════════════════════════════════════════════
with gaming_tab:
    gaming_page = channel_nav("gaming", ["🎬 Run Pipeline", "📊 Analytics", "🕵️ Trending Spy"])

    # ── Trending Spy (Twitch clips) ──────────────────────────────
    if gaming_page == "🕵️ Trending Spy":
        st.title("🕵️ Gaming Trending Spy")
        st.markdown("Top Twitch clips from followed streamers and trending game categories (last 24h) — the same pool the scheduler picks from.")
        with st.spinner("Fetching trending Twitch clips..."):
            try:
                from agents_gaming.trending_agent import get_all_topics as _gm_topics
                from agents_gaming.clip_finder import rank_clips as _gm_rank
                _clips = _gm_rank(_gm_topics())[:15]
            except Exception as e:
                st.error(f"Couldn't fetch Twitch clips: {e}")
                _clips = []
        render_spy_rows("gaming", [
            {"title": c.get("title", ""), "url": c.get("url"),
             "source": c.get("broadcaster_name") or c.get("_source_streamer") or c.get("_source_game", ""),
             "views": int(c.get("view_count", 0) or 0),
             "note": c.get("_source_game") or c.get("_topic_type", "")}
            for c in _clips
        ])

    # ── Analytics ────────────────────────────────────────────────
    elif gaming_page == "📊 Analytics":
        from agents_gaming.analytics_agent import get_channel_stats as _gm_stats, get_recent_videos as _gm_videos
        from agents_gaming.view_tracker_agent import track_views_gaming as _gm_track
        render_channel_analytics(
            key="gaming", label="Gaming", emoji="🎮",
            get_stats=_gm_stats, get_videos=_gm_videos, track_fn=_gm_track,
            token_hint="Check that YOUTUBE_GAMING_TOKEN_B64 is set and has the youtube.readonly scope "
                       "(re-run generate_gaming_token.py if the token predates that scope).",
            peak_label="Gaming AI CarryON",
        )
        st.subheader("🕐 Clip Post History")
        try:
            from agents_gaming.database import db as _gdb, db_init_error as _gdb_err
            if _gdb is None:
                st.info(f"Clip history unavailable here: {_gdb_err}")
            else:
                st.metric("Clips posted (all-time)", len(_gdb.get_all_posted_clip_ids()))
        except Exception as e:
            st.info(f"Clip history unavailable: {e}")

    # ── Run Pipeline ─────────────────────────────────────────────
    else:
        st.title("🎮 AI CarryON - Gaming Channel")
        st.markdown("Finds a trending Twitch clip, analyses the moment, writes commentary, renders a Short and uploads it. "
                    "Runs automatically from GitHub Actions via scheduler_gaming.py.")
        try:
            from agents_gaming.database import db as _gdb2
            _gm_posted = len(_gdb2.get_all_posted_clip_ids()) if _gdb2 is not None else None
        except Exception:
            _gm_posted = None
        _gm_today = None
        try:
            from scheduler_gaming import _check_daily_cap as _gm_cap
            _gm_today = _gm_cap()
        except Exception:
            pass
        g1, g2 = st.columns(2)
        g1.metric("Uploaded today (UTC)", _gm_today if _gm_today is not None else "—")
        g2.metric("Clips posted (all-time)", _gm_posted if _gm_posted is not None else "—")
        st.divider()
        st.markdown("**Quick mode — run one full cycle now**")
        st.caption("Runs the same cycle the scheduler fires. This **publishes to the live Gaming YouTube channel** "
                   "and ignores the adaptive-hour check (the daily cap still applies).")
        _gm_confirm = st.checkbox("I understand this will upload a real video now", key="gaming_confirm_run")
        if st.button("🔥 Generate + Upload Gaming Video", type="primary", key="gaming_run_btn", disabled=not _gm_confirm):
            with st.spinner("Running full Gaming pipeline... this can take a few minutes"):
                try:
                    from scheduler_gaming import run_gaming_cycle
                    _res = run_gaming_cycle(force=True)
                    if _res.get("status") == "uploaded":
                        st.success(f"✅ Uploaded: {_res.get('title')} — {_res.get('video_url')}")
                    else:
                        st.warning(f"Pipeline finished without an upload: {_res}")
                except Exception as e:
                    st.error(f"Pipeline error: {e}")
        _gm_log = "output/scheduler_gaming_log.txt"
        if os.path.exists(_gm_log):
            with st.expander("📜 Latest scheduler log"):
                st.code("".join(open(_gm_log, encoding="utf-8", errors="ignore").readlines()[-40:]), language="text")

# ════════════════════════════════════════════════════════════════
# BHAKTI CHANNEL (fully automated — devotional topics -> Pexels footage +
# bhajan music -> Shorts -> YouTube, runs on GitHub Actions via scheduler_bhakti.py)
# ════════════════════════════════════════════════════════════════
with bhakti_tab:
    bhakti_page = channel_nav("bhakti", ["🎬 Run Pipeline", "📊 Analytics", "🕵️ Trending Spy"])

    # ── Trending Spy (devotional topics) ─────────────────────────
    if bhakti_page == "🕵️ Trending Spy":
        st.title("🕉️ Bhakti Trending Spy")
        st.markdown("Devotional topic pool, with a live check for festival/devotional content trending in India right now. "
                    "Topics already used or uploaded are marked.")
        try:
            from agents_bhakti import trending_agent as _bh_ta
            _recent = _bh_ta._load_recent() + _bh_ta._load_uploaded_titles()
            _items = [{"title": t, "source": "Topic pool",
                       "note": "✅ used" if _bh_ta._is_repeat(t, _recent) else "🆕 available"}
                      for t in _bh_ta.TOPIC_POOL]
            _items.sort(key=lambda x: x["note"].startswith("✅"))
            _live = None
            if st.button("🔎 Check live devotional trend now", key="bhakti_live_trend_btn"):
                with st.spinner("Checking YouTube trending (IN)..."):
                    _live = _bh_ta._fetch_live_devotional_trend("IN", _recent)
                if _live:
                    st.success(f"🔥 Live trend: {_live}")
                else:
                    st.info("No devotional topic trending live right now.")
            render_spy_rows("bhakti", _items)
        except Exception as e:
            st.error(f"Couldn't load Bhakti topics: {e}")

    # ── Analytics ────────────────────────────────────────────────
    elif bhakti_page == "📊 Analytics":
        from agents_bhakti.analytics_agent import get_channel_stats as _bh_stats, get_recent_videos as _bh_videos
        from agents_bhakti.view_tracker_agent import track_views_bhakti as _bh_track
        render_channel_analytics(
            key="bhakti", label="Bhakti", emoji="🕉️",
            get_stats=_bh_stats, get_videos=_bh_videos, track_fn=_bh_track,
            token_hint="Check that BHAKTI_TOKEN_JSON is set and includes youtube.upload, youtube.readonly and youtube.force-ssl "
                       "(re-run generate_bhakti_token.py if the token predates those scopes).",
            peak_label="Bhakti AI CarryON",
        )

    # ── Run Pipeline ─────────────────────────────────────────────
    else:
        st.title("🕉️ AI CarryON - Bhakti Channel")
        st.markdown("Hindi devotional Shorts: mythological stories, mantra meanings and festival significance — "
                    "stock temple/diya/aarti footage with a bhajan-style track. Runs automatically from GitHub Actions.")

        _bh_mode = os.getenv("BHAKTI_MODE", "narrated")
        _bh_target = os.getenv("BHAKTI_VIDEOS_PER_DAY", "6")
        try:
            from scheduler_bhakti import get_posted_count_today as _bh_posted_today
            _bh_count = _bh_posted_today()
        except Exception as e:
            _bh_count = None
            st.info(f"Post history unavailable here: {e}")

        b1, b2, b3 = st.columns(3)
        b1.metric("Uploaded in last 24h", _bh_count if _bh_count is not None else "—")
        b2.metric("Daily target", _bh_target)
        b3.metric("Mode", _bh_mode)

        st.divider()
        st.markdown("**Quick mode — run one full cycle now**")
        st.caption("Runs the same pipeline the scheduler fires (topic → saturation → comparison → research → script → "
                   "A/B title → SEO → voice/music → render → upload). This **publishes to the live Bhakti YouTube channel** "
                   "and ignores the adaptive-hour check.")
        _bh_confirm = st.checkbox("I understand this will upload a real video now", key="bhakti_confirm_run")
        if st.button("🔥 Generate + Upload Bhakti Video", type="primary", key="bhakti_run_btn", disabled=not _bh_confirm):
            with st.spinner("Running full Bhakti pipeline... this can take a few minutes"):
                try:
                    from scheduler_bhakti import generate_and_upload_bhakti
                    generate_and_upload_bhakti(force=True)
                    st.success("✅ Pipeline finished — check the Analytics tab or the scheduler log for the result.")
                except Exception as e:
                    st.error(f"Pipeline error: {e}")

        _bh_log = "output/scheduler_bhakti_log.txt"
        if os.path.exists(_bh_log):
            with st.expander("📜 Latest scheduler log"):
                st.code("".join(open(_bh_log).readlines()[-40:]), language="text")

# ════════════════════════════════════════════════════════════════
# CRICKET CHANNEL (fully automated — same pipeline as Render)
# ════════════════════════════════════════════════════════════════
with cricket_tab:
    cricket_page = channel_nav("cricket", ["🎬 Generate + Upload", "📊 Analytics", "🕵️ Trending Spy"])

if cricket_page == "🕵️ Trending Spy":
    with cricket_tab:
        st.title("🕵️ Cricket Trending Spy")
        st.markdown("Breaking news, live, upcoming and recently finished Indian cricket matches — the same pool the scheduler picks from.")
        with st.spinner("Fetching cricket topics..."):
            try:
                from agents_cricket.trending_agent import get_all_topics as _ck_topics
                _ck = _ck_topics(limit=10)
            except Exception as e:
                st.error(f"Couldn't fetch cricket topics: {e}")
                _ck = []
        render_spy_rows("cricket", [
            {"title": t.get("name") or t.get("title", ""), "url": t.get("link") or t.get("url"),
             "source": t.get("source") or t.get("series") or "CricAPI",
             "note": t.get("topic_type", "")}
            for t in _ck
        ])
    st.stop()

if cricket_page == "📊 Analytics":
    with cricket_tab:
        st.title("📊 Cricket Channel Analytics")
        st.caption("Live stats pulled from the cricket channel's own YouTube credentials, and snapshot history from its own Firestore collections.")

        col_refresh, col_track = st.columns([1, 2])
        with col_refresh:
            if st.button("🔄 Refresh", key="cricket_analytics_refresh"):
                st.rerun()
        with col_track:
            if st.button("📸 Track views now", key="cricket_track_now",
                         help="Manually pull current view counts for cricket videos into Postgres — normally happens automatically ~hourly."):
                with st.spinner("Fetching current view counts from YouTube..."):
                    try:
                        from agents_cricket.view_tracker_agent import track_views_cricket
                        tracked = track_views_cricket()
                        st.success(f"✅ Tracked {len(tracked)} cricket video(s)")
                    except Exception as e:
                        st.error(f"Tracking failed: {e}")

        st.divider()

        with st.spinner("Fetching cricket channel data..."):
            try:
                from agents_cricket.analytics_agent import get_channel_stats, get_recent_videos
                cricket_stats = get_channel_stats()
                cricket_videos = get_recent_videos(20)
                cricket_fetch_error = None
            except Exception as e:
                cricket_stats, cricket_videos = None, []
                cricket_fetch_error = str(e)

        if cricket_fetch_error:
            st.error(f"Couldn't reach the cricket channel's YouTube API: {cricket_fetch_error}")
            st.caption("Check that CRICKET_YOUTUBE_TOKEN_B64 is set for this environment and has youtube.readonly scope.")
        else:
            st.subheader("📡 Channel Overview")
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("👤 Subscribers", f"{cricket_stats['subscribers']:,}")
            c2.metric("👁️ Total Views", f"{cricket_stats['total_views']:,}")
            c3.metric("🎬 Videos", f"{cricket_stats['video_count']:,}")
            if cricket_videos:
                avg_views = sum(v["views"] for v in cricket_videos) // len(cricket_videos)
                c4.metric("📈 Avg Views", f"{avg_views:,}")

            st.divider()

            if not cricket_videos:
                st.info("No cricket videos found yet.")
            else:
                st.subheader("🏆 Top Videos by Views")
                import pandas as pd
                df = pd.DataFrame(cricket_videos)
                df["short_title"] = df["title"].str[:30] + "..."
                st.bar_chart(df.set_index("short_title")["views"])

                st.divider()
                st.subheader("📋 All Cricket Videos")
                for v in cricket_videos:
                    with st.container():
                        vc1, vc2, vc3, vc4, vc5 = st.columns([4, 1, 1, 1, 1])
                        vc1.markdown(f"[{v['title']}]({v['url']})")
                        vc2.markdown(f"👁️ **{v['views']:,}**")
                        vc3.markdown(f"👍 {v['likes']:,}")
                        vc4.markdown(f"💬 {v['comments']:,}")
                        vc5.markdown(f"📅 {v['published']}")
                    st.divider()

        st.divider()
        st.subheader("🕐 Match Post History")
        try:
            from agents_cricket.database import db as _cdb, db_init_error as _cdb_err
            if _cdb is None:
                st.info(f"Match history unavailable here: {_cdb_err}")
            else:
                posted_ids = _cdb.get_all_posted_match_ids()
                st.metric("Matches posted (all-time)", len(posted_ids))
        except Exception as e:
            st.info(f"Match history unavailable: {e}")

        st.caption("For hour-by-hour peak upload windows, see the **Peak Hours** and **Schedule** pages in the sidebar — select \"Cricket AI CarryON\" there.")

    st.stop()

with cricket_tab:
    st.title("🏏 AI CarryON - Cricket Channel")
    st.markdown("Finds a live or recently finished Indian cricket match, writes a one-moment highlight recap, generates voice/video, and uploads to YouTube.")

    cricket_auto_upload = st.toggle(
        "Auto-upload to YouTube after generation", value=False, key="cricket_auto_upload_toggle"
    )

    st.markdown("---")
    st.markdown("**Step 1 — Find a live or recently finished match**")

    col_find, col_quick = st.columns(2)
    with col_find:
        cricket_find_clicked = st.button("🔍 Find Recently Finished Matches", key="cricket_find_matches_btn")
    with col_quick:
        cricket_quick_clicked = st.button(
            "🔥 Quick Mode: Auto Generate + Upload (no preview)", key="cricket_auto_generate_btn"
        )

    if cricket_find_clicked:
        with st.spinner("Checking CricAPI for live or recently finished matches..."):
            try:
                from agents_cricket.trending_agent import get_all_topics
                from agents_cricket.database import db as _cdb_find, db_init_error as _cdb_find_err
                cricket_matches = get_all_topics(limit=8)
                cricket_posted_ids = _cdb_find.get_all_posted_match_ids() if _cdb_find else set()
                st.session_state["cricket_matches"] = cricket_matches
                st.session_state["cricket_posted_ids"] = cricket_posted_ids
                if _cdb_find is None:
                    st.warning(f"Couldn't check post history: {_cdb_find_err}")
            except Exception as e:
                st.error(f"Error fetching matches: {e}")
                st.session_state["cricket_matches"] = []

    cricket_matches = st.session_state.get("cricket_matches", [])
    cricket_posted_ids = st.session_state.get("cricket_posted_ids", set())

    if cricket_find_clicked and not cricket_matches:
        st.warning("No live or recently finished Indian T20/ODI/Test matches found right now. Try again later.")

    if cricket_matches:
        cricket_match_options = {}
        for m in cricket_matches:
            m_label = m.get("name") or m.get("title", "")
            m_status = m.get("status") or m.get("topic_type", "")
            label = f"[{m.get('topic_type','?')}] {m_label} — {m_status}"
            if m["id"] in cricket_posted_ids:
                label += " ✅ already posted"
            cricket_match_options[label] = m

        cricket_selected_label = st.selectbox(
            "Pick a match to generate a recap for",
            list(cricket_match_options.keys()),
            key="cricket_match_select",
        )
        cricket_selected_match = cricket_match_options[cricket_selected_label]

        if cricket_selected_match["id"] in cricket_posted_ids:
            st.warning("This match has already been posted to the cricket channel.")

        st.markdown("**Step 2 — Generate the recap**")
        if st.button("✍️ Generate Recap for This Match", type="primary", key="cricket_generate_pipeline_btn"):
            st.session_state["cricket_selected_match"] = cricket_selected_match
            for k in ["cricket_summary", "cricket_script", "cricket_seo", "cricket_images",
                      "cricket_img_errors", "cricket_voice", "cricket_captions",
                      "cricket_video_file", "cricket_match_done"]:
                st.session_state.pop(k, None)
            try:
                with st.spinner("📚 Researching topic..."):
                    from agents_cricket.research_agent import get_summary_for_topic
                    cricket_summary, cricket_structured = get_summary_for_topic(cricket_selected_match)
                if not cricket_summary:
                    st.error("Could not fetch details for this topic. Try another one from the list above.")
                    st.stop()
                st.session_state["cricket_summary"] = cricket_summary
                st.session_state["cricket_structured"] = cricket_structured

                with st.spinner("✍️ Writing recap script..."):
                    from agents_cricket.script_agent import create_cricket_script
                    st.session_state["cricket_script"] = create_cricket_script(
                        cricket_summary, standout_player=cricket_structured.get("standout_player")
                    )

                with st.spinner("📈 Generating SEO..."):
                    from agents_cricket.seo_agent import generate_cricket_seo
                    st.session_state["cricket_seo"] = generate_cricket_seo(
                        cricket_summary, st.session_state["cricket_script"]
                    )

                with st.spinner("🎨 Generating background images..."):
                    from agents_cricket.image_agent import generate_backgrounds
                    img_paths, img_errors = generate_backgrounds(
                        cricket_summary, num_images=4, structured=cricket_structured
                    )
                    st.session_state["cricket_images"] = img_paths
                    st.session_state["cricket_img_errors"] = img_errors

                with st.spinner("🎤 Generating voiceover..."):
                    from agents_cricket.voice_agent import generate_voice
                    st.session_state["cricket_voice"] = generate_voice(
                        st.session_state["cricket_script"], output_path="output/voice.mp3"
                    )

                with st.spinner("✏️ Generating captions..."):
                    from agents.caption_agent import create_srt
                    st.session_state["cricket_captions"] = create_srt(
                        st.session_state["cricket_script"], audio_path="output/voice.mp3"
                    )

                st.session_state["cricket_match_done"] = True
            except Exception as e:
                st.error(f"Generation failed: {e}")

    # ── Display generated pipeline output ──────────────────────────────
    if st.session_state.get("cricket_match_done"):
        cmatch = st.session_state.get("cricket_selected_match", {})
        csummary = st.session_state.get("cricket_summary", "")
        cscript = st.session_state.get("cricket_script", "")
        cseo = st.session_state.get("cricket_seo", {})
        cimages = st.session_state.get("cricket_images", [])
        cimg_errors = st.session_state.get("cricket_img_errors", [])
        cvoice = st.session_state.get("cricket_voice")
        ccaptions = st.session_state.get("cricket_captions")

        st.divider()
        st.subheader(f"🏏 {cmatch.get('name', '')}")
        st.subheader("📚 Match Research")
        st.text(csummary)
        st.subheader("📝 Recap Script")
        st.write(cscript)
        st.subheader("📈 SEO")
        st.markdown(f"**Title:** {cseo.get('title', '')}")
        st.markdown(f"**Description:** {cseo.get('description', '')}")
        st.markdown(f"**Hashtags:** {' '.join(cseo.get('hashtags', []))}")

        if cimg_errors:
            for err in cimg_errors:
                st.warning(err)
        if cimages:
            st.subheader("🖼️ Background Images")
            cols = st.columns(len(cimages))
            for col, img_path in zip(cols, cimages):
                if str(img_path).lower().endswith(".mp4"):
                    col.video(img_path)
                else:
                    col.image(img_path)

        st.subheader("🔊 Voiceover")
        if cvoice:
            st.audio(cvoice)

        st.subheader("📄 Captions")
        if ccaptions and os.path.exists(ccaptions):
            st.code(open(ccaptions).read())

        st.divider()
        st.markdown("**Step 3 — Render and upload**")
        if st.button("🎬 Generate Final Video", key="cricket_make_video_btn", type="primary"):
            with st.spinner("🎬 Rendering video..."):
                from agents_cricket.video_agent import create_video
                st.session_state["cricket_video_file"] = create_video()

        cvideo_file = st.session_state.get("cricket_video_file")
        if cvideo_file:
            from moviepy import AudioFileClip as AFC
            cduration = AFC("output/voice.mp3").duration
            if cduration > 60:
                st.warning(f"⚠️ Video is {cduration:.1f}s — over 60s Shorts limit!")
            else:
                st.success(f"✅ Shorts-ready! Duration: {cduration:.1f}s")

            st.subheader("🎥 Generated Video")
            st.video(cvideo_file)

            with open(cvideo_file, "rb") as f:
                st.download_button(
                    label="📱 Download for Instagram Reels",
                    data=f,
                    file_name="cricket_reel.mp4",
                    mime="video/mp4",
                    key="cricket_download",
                )

            cricket_already_posted = cmatch.get("id") in st.session_state.get("cricket_posted_ids", set())

            def _do_cricket_upload():
                with st.spinner("📤 Uploading to YouTube..."):
                    try:
                        from agents_cricket.upload_agent import upload_video
                        from agents_cricket.database import db as _cdb_up
                        from datetime import datetime, timezone
                        video_id, video_url = upload_video(
                            cvideo_file, cseo["title"], cseo["description"], cseo["hashtags"]
                        )
                        if _cdb_up:
                            _cdb_up.mark_posted(cmatch["id"], cmatch.get("name", ""))
                            _cdb_up.upsert_video(
                                video_id, cseo["title"],
                                datetime.now(timezone.utc).isoformat(),
                                match_id=cmatch["id"],
                            )
                        st.success("✅ Uploaded to Cricket channel!")
                        st.balloons()
                        st.markdown(f"**▶️ Watch here:** [{video_url}]({video_url})")
                        st.session_state.pop("cricket_matches", None)
                        st.session_state.pop("cricket_match_done", None)
                    except Exception as e:
                        st.error(f"Upload failed: {e}")

            if cricket_already_posted:
                st.warning("This match was already posted — pick a different match above to avoid a duplicate upload.")
            elif cricket_auto_upload:
                _do_cricket_upload()
            else:
                if st.button("📤 Upload to YouTube", key="cricket_manual_upload_btn", type="primary"):
                    _do_cricket_upload()
                st.info("💡 Auto-upload is OFF. Toggle it on above, or click the button to upload manually.")

    st.divider()
    st.markdown("**Or — quick mode**")
    st.caption("Runs the exact same pipeline that fires automatically on Render every ~20 min, with no preview step.")

    if cricket_quick_clicked:
        with st.spinner("Checking for live or recently finished matches..."):
            try:
                from agents_cricket.trending_agent import get_all_topics
                quick_matches = get_all_topics(limit=8)
            except Exception as e:
                st.error(f"Error fetching topics: {e}")
                quick_matches = []

        if not quick_matches:
            st.warning("No live or recently finished Indian T20/ODI/Test matches found right now. Try again later.")
        else:
            st.info(f"Found {len(quick_matches)} finished match(es) — processing the newest one not already posted...")

            with st.spinner("Running full pipeline: research → script → SEO → voice → video → upload... this can take 1-2 minutes"):
                try:
                    from scheduler_cricket import run_cricket_cycle
                    result = run_cricket_cycle()
                except Exception as e:
                    st.error(f"Pipeline error: {e}")
                    result = None

            if result:
                status = result.get("status")
                if status == "uploaded":
                    st.success("✅ Uploaded to Cricket channel!")
                    st.balloons()
                    st.markdown(f"**Title:** {result.get('title','')}")
                    video_url = result.get("video_url", "")
                    st.markdown(f"**▶️ Watch here:** [{video_url}]({video_url})")
                elif status == "no_new_match":
                    st.info("All recent topics have already been posted. Nothing new right now.")
                elif status == "summary_fetch_failed":
                    st.warning(f"Could not fetch details for {result.get('topic','')}. Try again shortly.")
                else:
                    st.info(f"Result: {result}")
