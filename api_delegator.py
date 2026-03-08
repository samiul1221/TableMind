"""
api_delegator.py — Two-phase intent router + API dispatcher for Mochi Table Assistant.

Sits between Whisper STT output and AnimeManager / WeatherManager.
Converts natural language into [CMD:ANIME:*] or [CMD:WEATHER:*] tags,
calls the appropriate manager, and returns a TTS-ready plain string.

No LLM is invoked unless keyword scan is ambiguous.
"""

import re
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Domain keyword sets — used for Phase 1 tier-1 (zero-latency) routing
# ─────────────────────────────────────────────────────────────────────────────

_ANIME_KEYWORDS: list[str] = [
    "anime", "manga", "character", "episode", "airing",
    "trending anime", "seasonal anime", "recommend anime",
    "anime quote", "anime fact", "anime news",
    "anilist", "kitsu", "shikimori",
    "opening", "ending", "studio", "voice actor",
    "watchlist", "plan to watch", "dropped",
    "what anime", "which anime",
    # Bare single-word triggers so "what's trending" / "give me a quote" etc.
    # hit tier-1 without needing the full multi-word phrase
    "recommend", "recommendation", "similar to", "something like",
    "trending", "seasonal", "synopsis", "my list", "my anime list",
    "quote", "trivia", "top rated", "popular",
    # Manhwa / Manhua / Webtoon / reading terms
    "manhwa", "manhua", "webtoon", "comic",
    "chapter", "chapters", "volume",
    "author", "artist", "illustrator", "hiatus", "raws", "scans",
    "reading list", "plan to read", "currently reading", "finished reading",
]

_WEATHER_KEYWORDS: list[str] = [
    "weather", "temperature", "forecast", "rain", "precipitation",
    "humidity", "humid", "wind", "breeze", "gust",
    "uv", "uv index", "sunscreen", "sun protection",
    "air quality", "aqi", "pollution", "pm2.5", "pm10",
    "pollen", "allergy", "hayfever", "hay fever",
    "sunrise", "sunset", "daylight",
    "sensor", "station", "monitor",
    "hot outside", "cold outside", "is it raining",
    "how hot", "how cold", "dew point", "dewpoint",
]

# ─────────────────────────────────────────────────────────────────────────────
# Action mapping — ordered longest-first so specific phrases take priority
# Each entry: (tuple of trigger phrases, action tag)
# ─────────────────────────────────────────────────────────────────────────────

_WEATHER_ACTION_MAP: list[tuple[tuple[str, ...], str]] = [
    # Multi-word specific phrases first
    (("air quality", "aqi", "pollution index"),                          "AQI"),
    (("pollution", "pm2", "pm10", "nitrogen dioxide", "ozone level",
      "sulfur dioxide", "carbon monoxide"),                              "POLLUTION"),
    (("pollen", "allergy", "hayfever", "hay fever"),                     "POLLEN"),
    (("sunrise", "sunset", "daylight", "golden hour"),                   "SUNRISE"),
    (("sensor", "station", "monitor", "openaq"),                         "SENSORS"),
    (("real time", "realtime", "live reading", "live sensor"),           "REALTIME"),
    (("hourly", "next few hours", "hour by hour"),                       "HOURLY"),
    (("forecast", "tomorrow", "this week", "next week",
      "coming days", "5 day", "weekly"),                                 "FORECAST"),
    (("rain", "precipitation", "snow", "umbrella",
      "will it rain", "is it raining", "chance of rain"),                "RAIN"),
    (("wind", "breeze", "gust", "storm", "windy"),                       "WIND"),
    (("humidity", "humid", "dry", "dewpoint", "dew point",
      "muggy", "moisture"),                                              "HUMIDITY"),
    (("uv", "sunscreen", "sun protection", "uv index"),                 "UV"),
    # Broadest last — catches "weather", "temperature", "how hot", etc.
    (("weather", "temperature", "hot", "cold", "conditions",
      "how hot", "how cold", "what's it like outside", "outside"),       "CURRENT"),
]

_ANIME_ACTION_MAP: list[tuple[tuple[str, ...], str]] = [
    # Specific multi-word phrases first
    (("trending anime", "what's trending", "whats trending", "trending manhwa",
      "trending webtoon", "what anime is trending",
      "popular anime", "whats hot"),                                     "TRENDING"),
    (("seasonal anime", "this season", "airing this season",
      "current season"),                                                 "SEASONAL"),
    (("when does", "next episode", "next chapter", "airing schedule",
      "when is the next", "currently airing", "what's airing",
      "whats airing", "airing right now", "airing", "update"),          "AIRING"),
    (("recommend", "suggest", "suggestion", "similar to",
      "if i liked", "something like", "give me something"),             "RECOMMEND"),
    (("top anime", "best anime", "highest rated", "top manhwa",
      "all time", "top rated"),                                          "TOP"),
    (("anime quote", "quote from", "give me a quote",
      "say something from", "memorable line"),                           "QUOTE"),
    (("anime fact", "fact about", "trivia", "did you know"),            "FACT"),
    (("anime news", "latest anime", "announcement"),                    "NEWS"),
    (("character", "who is", "tell me about the character",
      "voice actor"),                                                    "CHARACTER"),
    (("add to", "plan to watch", "plan to read", "add anime", "add manhwa",
      "watchlist", "put on my list"),                                   "ADD"),
    (("watching", "currently watching", "reading", "currently reading",
      "mark as watching", "started watching", "started reading"),        "WATCHING"),
    (("finished", "completed", "done watching", "done reading",
      "finished reading", "mark as done", "mark as completed"),          "DONE"),
    (("dropped", "drop", "stopped watching", "stopped reading"),         "DROP"),
    (("on hold", "paused", "put on hold"),                               "HOLD"),
    (("my list", "my anime list", "what am i watching",
      "what am i reading", "show my list"),                              "LIST"),
    (("kitsu trending",),                                                "KITSUTRENDING"),
    (("what facts", "list facts", "available facts", "fact list"),       "LISTFACT"),
    # Broadest last — catches "what is <title>", "tell me about <anime/manhwa>"
    (("about", "synopsis", "what is", "tell me about",
      "search", "look up", "info on", "information",
      "author", "artist", "how many chapters", "chapter"),               "SEARCH"),
]

# ─────────────────────────────────────────────────────────────────────────────
# Entity extraction patterns — "in <city>", "about <title>", etc.
# ─────────────────────────────────────────────────────────────────────────────

# Weather: extract city from common phrasing
_WEATHER_ENTITY_PATTERNS: list[re.Pattern] = [
    # Bug 1: added negative lookahead to block domain words from being treated as cities
    re.compile(
        r'\bin\s+(?!(?:anime|manga|general|this|my|the|a\b)\b)(.+?)'
        r'(?:\s+(?:today|tomorrow|this week|right now|currently))?$',
        re.I,
    ),
    re.compile(r'\bfor\s+(?!(?:the\s+next|next\s+\d|going\s+out|a\s+while)\b)(.+?)(?:\s+(?:today|tomorrow|this week))?$', re.I),
    re.compile(r'^(?:weather|temperature|forecast|rain|wind|humidity|uv|aqi|air quality|pollution|pollen|sunrise|sunset)\s+(?:in\s+|for\s+)?(.+)', re.I),
]

# Anime: extract title/name from common phrasing
_ANIME_ENTITY_PATTERNS: list[re.Pattern] = [
    re.compile(r'\babout\s+(.+?)$', re.I),
    re.compile(r'\bfor\s+(.+?)$', re.I),
    re.compile(r'\bcalled\s+(.+?)$', re.I),
    re.compile(r'\bnamed\s+(.+?)$', re.I),
    re.compile(r'\b(?:something|anime|shows|manga|manhwa)\s+like\s+(.+?)$', re.I),
    re.compile(r'\bfrom\s+(.+?)$', re.I),
    re.compile(r'\brelated to\s+(.+?)$', re.I),
    re.compile(r'\bsimilar to\s+(.+?)$', re.I),
    # "search <title>", "look up <title>"
    re.compile(r'\b(?:search|look up|info on)\s+(.+?)$', re.I),
    # "what is <title>" — guarded: reject if captured text is ≤2 words of
    # common descriptive filler (e.g. "what's the best anime" → "the best anime"
    # is junk).  Only fire when the captured group looks like a proper noun/title.
    re.compile(r'\bwhat(?:\s+is|\'s)\s+(?:the\s+)?(?:anime\s+)?(.+?)(?:\s+about)?$', re.I),
    # "recommend something like <title>"
    re.compile(r'\bsomething like\s+(.+?)$', re.I),
    # "when does <title> air"
    re.compile(r'\bwhen does\s+(.+?)\s+(?:air|come|release)', re.I),
    # "fact about <title>"
    re.compile(r'\bfact(?:s)?\s+(?:about|on|for)\s+(.+?)$', re.I),
    # "quote from <title>"
    re.compile(r'\bquote(?:s)?\s+(?:from|about)\s+(.+?)$', re.I),
    # Manhwa/Webtoon-specific extraction patterns
    re.compile(r'\bauthor\s+(?:of|for)\s+(.+?)$', re.I),
    re.compile(r'\bartist\s+(?:of|for)\s+(.+?)$', re.I),
    re.compile(r'\bchapters?\s+(?:in|for|of)\s+(.+?)$', re.I),
    re.compile(r'\bseason\s+\d+\s+(?:of|for)\s+(.+?)$', re.I),
    re.compile(r'\b(?:read|reading)\s+(.+?)$', re.I),
]

# Actions that don't need an entity (no title/city required)
_NO_ENTITY_WEATHER = {"CURRENT"}  # CURRENT defaults to a configured home city
_NO_ENTITY_ANIME = {"TRENDING", "SEASONAL", "TOP", "QUOTE", "FACT", "NEWS",
                     "LIST", "KITSUTRENDING", "AIRING", "LISTFACT"}


# ─────────────────────────────────────────────────────────────────────────────
# APIDelegator
# ─────────────────────────────────────────────────────────────────────────────

class APIDelegator:
    """
    Converts natural language into CMD tags and dispatches to the correct
    manager. Returns TTS-ready strings. Never raises.

    Usage:
        delegator = APIDelegator(anime_manager, weather_manager)
        domain = delegator.classify_domain(text)  # "anime" | "weather" | None
        if domain:
            result = delegator.handle(text, domain)
            stream_tts_to_esp32(result)
    """

    def __init__(self, anime_manager=None, weather_manager=None,
                 llm_classify_fn=None, default_city: str = "Delhi"):
        """
        anime_manager:   AnimeManager instance (or None to disable)
        weather_manager: WeatherManager instance (or None to disable)
        llm_classify_fn: optional callback(text) -> "ANIME"|"WEATHER"|"IOT"|"CHAT"
                         used as tier-2 fallback when keywords are ambiguous.
        default_city:    fallback city for weather queries without a city name.
        """
        self._anime = anime_manager
        self._weather = weather_manager
        self._llm_classify = llm_classify_fn
        self._default_city = default_city

    # ── Phase 1: Domain Classification ────────────────────────────────────

    def classify_domain(self, text: str) -> Optional[str]:
        """
        Tier 1: keyword scan → instant routing.
        Tier 2: LLM fallback (only if ambiguous and callback provided).
        Returns "anime", "weather", or None (means route to chat LLM).
        """
        low = text.lower()
        anime_hit = any(re.search(r'\b' + re.escape(kw) + r'\b', low) for kw in _ANIME_KEYWORDS)
        weather_hit = any(re.search(r'\b' + re.escape(kw) + r'\b', low) for kw in _WEATHER_KEYWORDS)

        if anime_hit and not weather_hit:
            return "anime"
        if weather_hit and not anime_hit:
            return "weather"

        # Both or neither → try LLM classify if available
        if self._llm_classify:
            try:
                result = self._llm_classify(text)
                if result:
                    r = result.strip().upper()
                    if r == "ANIME":
                        return "anime"
                    if r == "WEATHER":
                        return "weather"
            except Exception:
                pass

        # Ambiguous or no LLM → fall through to chat
        return None

    # ── Phase 2: Action + Entity → CMD tag → Manager call ────────────────

    def handle(self, text: str, domain: str) -> str:
        """
        Build CMD tag from text, dispatch to the right manager, return string.
        domain must be "anime" or "weather".
        """
        if domain == "anime":
            return self._handle_anime(text)
        if domain == "weather":
            return self._handle_weather(text)
        return "I'm not sure how to handle that."

    def _handle_weather(self, text: str) -> str:
        if not self._weather:
            return "Weather features aren't available right now."
        action = self._extract_action(text, _WEATHER_ACTION_MAP)
        if action in _NO_ENTITY_WEATHER:
            entity = self._extract_entity(text, _WEATHER_ENTITY_PATTERNS)
            tag = f"[CMD:WEATHER:{action}:{entity}]" if entity else f"[CMD:WEATHER:{action}:{self._default_city}]"
        else:
            entity = self._extract_entity(text, _WEATHER_ENTITY_PATTERNS)
            if not entity:
                entity = self._default_city
            tag = f"[CMD:WEATHER:{action}:{entity}]"
        print(f"[delegator] {tag}", flush=True)
        return self._weather.dispatch(tag)

    # ── Media-type words that are NOT valid titles ─────────────────────────
    _MEDIA_TYPE_WORDS = frozenset({
        "anime", "manga", "manhwa", "manhua", "webtoon", "comic",
        "show", "series", "something", "anything",
    })

    def _handle_anime(self, text: str) -> str:
        if not self._anime:
            return "Anime features aren't available right now."
        action = self._extract_action(text, _ANIME_ACTION_MAP)
        entity = self._extract_entity(text, _ANIME_ENTITY_PATTERNS)

        # Strip trailing punctuation that leaks from STT (e.g. "manhwa?")
        if entity:
            entity = entity.strip("?!.,;:'\"").strip()

        # If entity is just a generic media-type word, it's not a real title
        if entity and entity.lower() in self._MEDIA_TYPE_WORDS:
            entity = None

        # Some actions don't need an entity
        if action in _NO_ENTITY_ANIME:
            tag = f"[CMD:ANIME:{action}]"
            if entity:
                tag = f"[CMD:ANIME:{action}:{entity}]"
        elif not entity:
            # For entity-requiring actions with no entity found,
            # try using the whole text minus known keywords as the entity
            fallback = self._fallback_entity(text)
            # If fallback is also just a media type word, leave entity empty
            # so recommend() falls back to trending()
            if fallback and fallback.lower() not in self._MEDIA_TYPE_WORDS:
                entity = fallback
                entity = entity.strip("?!.,;:'\"").strip()
            tag = f"[CMD:ANIME:{action}:{entity}]" if entity else f"[CMD:ANIME:{action}]"
        else:
            tag = f"[CMD:ANIME:{action}:{entity}]"

        print(f"[delegator] {tag}", flush=True)
        return self._anime.dispatch(tag)

    # ── Action extraction (keyword scan against action maps) ──────────────

    @staticmethod
    def _extract_action(text: str,
                        action_map: list[tuple[tuple[str, ...], str]]) -> str:
        """Scan text for the first matching action keyword group."""
        low = text.lower()
        for keywords, action in action_map:
            for kw in keywords:
                if kw in low:
                    return action
        # Default: first entry's action (broadest)
        return action_map[-1][1] if action_map else "SEARCH"

    # ── Entity extraction (regex patterns) ────────────────────────────────

    @staticmethod
    def _extract_entity(text: str,
                        patterns: list[re.Pattern]) -> Optional[str]:
        """Try each regex pattern, return the first captured group."""
        for pat in patterns:
            m = pat.search(text)
            if m:
                entity = m.group(1).strip()
                # Clean up trailing noise words
                entity = re.sub(
                    r'\s+(?:please|right now|today|tomorrow|currently|now)$',
                    '', entity, flags=re.I
                ).strip()
                if entity and len(entity) > 1:
                    return entity
        return None

    @staticmethod
    def _fallback_entity(text: str) -> Optional[str]:
        """
        Last resort: strip only generic functional words and the action verbs
        that triggered routing. Does NOT strip domain keywords (anime, weather,
        rain, wind, etc.) because they can appear legitimately in titles/cities
        (e.g. "Weathering With You", "Rain", "Wind Breaker").
        """
        cleaned = text.lower()
        _STOP_WORDS = [
            "what", "what's", "whats", "is", "the", "a", "an",
            "tell", "me", "give", "can", "you", "please",
            "about", "do", "does", "any", "some", "get",
            "search", "look", "up", "info", "on", "for",
            "recommend", "suggest", "show", "find", "check",
            "to", "related", "similar",
        ]
        for word in _STOP_WORDS:
            cleaned = re.sub(r'\b' + re.escape(word) + r'\b', '', cleaned)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        return cleaned if cleaned else None
