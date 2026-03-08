"""
anime_tools.py — Comprehensive Anime Manager for Mochi Table Assistant
Handles AniList, Kitsu, Shikimori, ANN, AnimeChan, and Anime Facts APIs.
All public methods return TTS-ready plain-English strings.
"""

import html as _html
import json
import os
import re
import threading
import time
import random
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime
from typing import Any, Optional

import requests

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

ANILIST_URL = "https://graphql.anilist.co/"
KITSU_BASE = "https://kitsu.io/api/edge"
KITSU_HEADERS = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
}
SHIKI_BASE = "https://shikimori.one/api"
SHIKI_TOKEN_URL = "https://shikimori.one/oauth/token"
ANN_BASE = "https://cdn.animenewsnetwork.com/encyclopedia"
ANIMECHAN_BASE = "https://api.animechan.io/v1"
FACTS_BASE = "https://anime-facts-rest-api.herokuapp.com/api/v1"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

FACT_ALIASES = {
    "fma": "fma_brotherhood",
    "fullmetal": "fma_brotherhood",
    "fullmetal alchemist": "fma_brotherhood",
    "aot": "attack_on_titan",
    "snk": "attack_on_titan",
    "mha": "my_hero_academia",
    "bnha": "my_hero_academia",
    "jjk": "jujutsu_kaisen",
    "demon slayer": "demon_slayer",
    "kimetsu": "demon_slayer",
    "hxh": "hunter_x_hunter",
}

TTL_ANILIST_SEARCH = 3600
TTL_ANILIST_TRENDING = 1800
TTL_ANILIST_SEASONAL = 1800
TTL_ANILIST_AIRING = 600
TTL_ANILIST_CHARACTER = 86400
TTL_ANILIST_RECOMMEND = 86400
TTL_KITSU_SEARCH = 3600
TTL_KITSU_TRENDING = 1800
TTL_SHIKI_SEARCH = 3600
TTL_SHIKI_MY_LIST = 300
TTL_ANN_NEWS = 1800
TTL_ANN_DETAILS = 604800


# ─────────────────────────────────────────────────────────────────────────────
# TTLCache
# ─────────────────────────────────────────────────────────────────────────────

class TTLCache:
    """Simple in-memory key-value store with per-key TTL. Thread-safe."""

    def __init__(self):
        self._store: dict = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at is not None and time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[int]) -> None:
        with self._lock:
            expires_at = (time.time() + ttl) if ttl is not None else None
            self._store[key] = (value, expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    if not text:
        return ""
    # Remove tags first, then decode entities (e.g. &amp; &mdash; &#8220;)
    no_tags = re.sub(r"<[^>]+>", "", text)
    return _html.unescape(no_tags).strip()


def _truncate(text: str, max_len: int = 220) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:") + "..."


def _safe(value: Any, fallback: str = "unknown") -> str:
    if value is None:
        return fallback
    return str(value)


def _current_season() -> str:
    m = datetime.now().month
    if m in (1, 2, 3):
        return "WINTER"
    if m in (4, 5, 6):
        return "SPRING"
    if m in (7, 8, 9):
        return "SUMMER"
    return "FALL"


def _seconds_to_human(seconds: int) -> str:
    days = seconds // 86400
    remaining = seconds % 86400
    hours = remaining // 3600
    if days > 0:
        return f"{days} day{'s' if days != 1 else ''} and {hours} hour{'s' if hours != 1 else ''}"
    minutes = remaining // 60
    return f"{hours} hour{'s' if hours != 1 else ''} and {minutes} minute{'s' if minutes != 1 else ''}"


def _prefer_english(title_obj: Optional[dict]) -> str:
    if not title_obj:
        return "Unknown"
    return title_obj.get("english") or title_obj.get("romaji") or "Unknown"


# ─────────────────────────────────────────────────────────────────────────────
# AnimeManager
# ─────────────────────────────────────────────────────────────────────────────

class AnimeManager:
    """Main anime/manga manager. All public methods return TTS-ready strings."""

    def __init__(self):
        self._cache = TTLCache()
        self._config: dict = {}
        self._quote_cache: deque = deque(maxlen=30)
        self._facts_cache: dict = {}          # anime_name → list of fact strings
        self._facts_anime_list: dict = {}     # lower_name → canonical_name
        self._facts_available: bool = False   # False until warmup confirms API is live
        self._animechan_available_anime: dict = {}  # lower → canonical
        self._lock = threading.Lock()
        self._load_config()

    # ── Config ────────────────────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r") as f:
                    self._config = json.load(f)
            else:
                self._config = {}
        except Exception as e:
            print(f"[anime_tools] config load error: {e}", flush=True)
            self._config = {}

    def _save_config(self) -> None:
        try:
            with open(CONFIG_PATH, "w") as f:
                json.dump(self._config, f, indent=2)
        except Exception as e:
            print(f"[anime_tools] config save error: {e}", flush=True)

    def _cfg(self, *keys: str, default=None):
        """Safely navigate nested config keys."""
        obj = self._config
        for k in keys:
            if not isinstance(obj, dict):
                return default
            obj = obj.get(k, default)
            if obj is None:
                return default
        return obj

    # ── Warmup ────────────────────────────────────────────────────────────────

    def warmup(self) -> None:
        """Non-blocking warmup — spawns a background thread."""
        t = threading.Thread(target=self._warmup_bg, daemon=True)
        t.start()

    def _warmup_bg(self) -> None:
        self._warmup_anime_facts()
        time.sleep(0.5)
        self._warmup_animechan_available()
        time.sleep(0.5)
        self._refill_quote_cache()

    def _warmup_anime_facts(self) -> None:
        # Heroku free dynos were eliminated Nov 2022 — endpoint is permanently dead.
        self._facts_available = False
        return
        try:  # dead code kept for reference
            r = requests.get(f"{FACTS_BASE}/", timeout=20)
            if r.status_code == 200:
                data = r.json().get("data", [])
                self._facts_anime_list = {
                    a["anime_name"].lower(): a["anime_name"]
                    for a in data
                    if "anime_name" in a
                }
                if self._facts_anime_list:
                    self._facts_available = True
                    print(f"[anime_tools] Anime Facts API online, {len(self._facts_anime_list)} anime available.", flush=True)
                else:
                    print("[anime_tools] WARNING: Anime Facts API returned empty data. Facts feature disabled.", flush=True)
            else:
                print(
                    f"[anime_tools] WARNING: Anime Facts API returned HTTP {r.status_code}. "
                    f"This Heroku-hosted endpoint may be permanently unavailable. "
                    f"Facts feature disabled.",
                    flush=True,
                )
        except Exception as e:
            print(
                f"[anime_tools] WARNING: Anime Facts API unreachable ({e}). "
                f"The host (anime-facts-rest-api.herokuapp.com) was on Heroku free tier, "
                f"which was discontinued Nov 2022 and may be permanently gone. "
                f"Facts feature disabled.",
                flush=True,
            )

    def _warmup_animechan_available(self) -> None:
        # /available/anime does not exist in AnimeChan v1.
        # Anime names are passed directly to /quotes/random?anime= and 404s
        # are handled gracefully in random_quote().
        pass

    def _refill_quote_cache(self) -> None:
        needed = min(1, 30 - len(self._quote_cache))
        for _ in range(needed):
            try:
                r = requests.get(f"{ANIMECHAN_BASE}/quotes/random",
                                 headers=self._animechan_headers(), timeout=5)
                if r.status_code == 200:
                    data = r.json().get("data")
                    if data:
                        self._quote_cache.append(data)
            except Exception:
                break
            time.sleep(0.5)

    # ── AnimeChan headers ─────────────────────────────────────────────────────

    def _animechan_headers(self) -> dict:
        key = self._cfg("animechan", "api_key")
        if key:
            return {"X-Api-Key": key}
        return {}

    # ── AniList ───────────────────────────────────────────────────────────────

    def _anilist_query(self, query: str, variables: dict, token: str = None) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(2):
            try:
                resp = requests.post(
                    ANILIST_URL,
                    json={"query": query, "variables": variables},
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 429:
                    time.sleep(60)
                    continue
                resp.raise_for_status()
                return resp.json().get("data", {})
            except requests.HTTPError as e:
                if resp.status_code == 429 and attempt == 0:
                    time.sleep(60)
                    continue
                raise
        return {}

    def search(self, title: str, media_type: str = "ANIME") -> str:
        if not title:
            return "Please tell me what anime or manga you'd like to search for."
        cache_key = f"anilist_search:{title.lower()}:{media_type}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        query = """
        query ($search: String, $type: MediaType) {
          Media(search: $search, type: $type, sort: SEARCH_MATCH) {
            id title { romaji english }
            type episodes chapters status averageScore
            description(asHtml: false)
            genres
            startDate { year }
            studios(isMain: true) { nodes { name } }
          }
        }
        """
        try:
            data = self._anilist_query(query, {"search": title, "type": media_type.upper()})
            media = data.get("Media")
            if not media:
                return f"I couldn't find anything for {title}."

            name = _prefer_english(media.get("title"))
            status = media.get("status", "").replace("_", " ").lower()
            score_raw = media.get("averageScore")
            score_str = f"{score_raw / 10:.1f} out of 10" if score_raw else "no score yet"
            episodes = media.get("episodes") or media.get("chapters")
            ep_str = f"with {episodes} episodes" if episodes else ""
            studio_nodes = (media.get("studios") or {}).get("nodes", [])
            studio = studio_nodes[0]["name"] if studio_nodes else ""
            genres = (media.get("genres") or [])[:3]
            genre_str = ", ".join(genres) if genres else ""
            desc = _truncate(_strip_html(media.get("description") or ""))
            year = (media.get("startDate") or {}).get("year")

            parts = [f"{name}"]
            if year:
                parts[0] += f" ({year})"
            parts.append(f"is a {status} {media_type.lower()}")
            if ep_str:
                parts[-1] += f" {ep_str}"
            parts.append(f"rated {score_str}")
            if studio:
                parts.append(f"produced by {studio}")
            if genre_str:
                parts.append(f"genres include {genre_str}")
            if desc:
                parts.append(f"The story follows {desc}")

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_ANILIST_SEARCH)
            return result
        except Exception as e:
            print(f"[anime_tools] search error: {e}", flush=True)
            return "I couldn't get that info right now, try again in a moment."

    def trending(self) -> str:
        cache_key = "anilist_trending"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        season = _current_season()
        year = datetime.now().year
        query = """
        query ($season: MediaSeason, $year: Int, $page: Int) {
          Page(page: $page, perPage: 5) {
            media(type: ANIME, season: $season, seasonYear: $year,
                  sort: [TRENDING_DESC, POPULARITY_DESC], status: RELEASING) {
              title { romaji english }
              averageScore
              nextAiringEpisode { episode }
            }
          }
        }
        """
        try:
            data = self._anilist_query(query, {"season": season, "year": year, "page": 1})
            items = (data.get("Page") or {}).get("media", [])
            if not items:
                return "I couldn't find trending anime right now."

            entries = []
            for i, m in enumerate(items[:5], 1):
                name = _prefer_english(m.get("title"))
                score = m.get("averageScore")
                score_str = f"rated {score / 10:.1f}" if score else ""
                ep = (m.get("nextAiringEpisode") or {}).get("episode")
                ep_str = f"on episode {ep}" if ep else ""
                entry = f"number {i}, {name}"
                if score_str:
                    entry += f" {score_str}"
                if ep_str:
                    entry += f" {ep_str}"
                entries.append(entry)

            result = f"Top trending anime this {season.lower()} season: {', '.join(entries)}."
            self._cache.set(cache_key, result, TTL_ANILIST_TRENDING)
            return result
        except Exception as e:
            print(f"[anime_tools] trending error: {e}", flush=True)
            return "I couldn't get trending anime right now, try again in a moment."

    def seasonal(self) -> str:
        cache_key = "anilist_seasonal"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        season = _current_season()
        year = datetime.now().year
        query = """
        query ($season: MediaSeason, $year: Int) {
          Page(page: 1, perPage: 8) {
            media(type: ANIME, season: $season, seasonYear: $year,
                  sort: [POPULARITY_DESC]) {
              title { romaji english }
              averageScore
              episodes
              status
              genres
            }
          }
        }
        """
        try:
            data = self._anilist_query(query, {"season": season, "year": year})
            items = (data.get("Page") or {}).get("media", [])
            if not items:
                return f"I couldn't find anime for {season.lower()} {year}."

            entries = []
            for i, m in enumerate(items[:5], 1):
                name = _prefer_english(m.get("title"))
                score = m.get("averageScore")
                score_str = f"rated {score / 10:.1f}" if score else ""
                entry = f"number {i}, {name}"
                if score_str:
                    entry += f" {score_str}"
                entries.append(entry)

            result = (
                f"This {season.lower()} {year} season has some great shows. "
                f"Top picks: {', '.join(entries)}."
            )
            self._cache.set(cache_key, result, TTL_ANILIST_SEASONAL)
            return result
        except Exception as e:
            print(f"[anime_tools] seasonal error: {e}", flush=True)
            return "I couldn't get the seasonal lineup right now, try again in a moment."

    def airing_schedule(self, title: str) -> str:
        if not title:
            return "I don't have a specific show, but " + self.seasonal().lower()
        cache_key = f"anilist_airing:{title.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        # Step 1: get media ID
        search_query = """
        query ($search: String) {
          Media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
            id title { romaji english }
            status episodes
            nextAiringEpisode { episode airingAt timeUntilAiring }
          }
        }
        """
        try:
            data = self._anilist_query(search_query, {"search": title})
            media = data.get("Media")
            if not media:
                return f"I couldn't find {title} on AniList."

            name = _prefer_english(media.get("title"))
            status = media.get("status", "")
            nae = media.get("nextAiringEpisode")

            if not nae:
                if status == "FINISHED":
                    eps = media.get("episodes", "all")
                    return f"{name} has finished airing with {eps} episodes total."
                elif status == "NOT_YET_RELEASED":
                    return f"{name} hasn't started airing yet."
                else:
                    return f"{name} doesn't have a scheduled next episode right now."

            ep = nae.get("episode")
            airing_at = nae.get("airingAt")
            time_until = nae.get("timeUntilAiring")

            time_str = _seconds_to_human(time_until) if time_until else "soon"
            date_str = ""
            if airing_at:
                dt = datetime.fromtimestamp(airing_at)
                date_str = f" on {dt.strftime('%A %B %d')}"

            result = (
                f"{name} episode {ep} airs in {time_str}{date_str}."
            )
            self._cache.set(cache_key, result, TTL_ANILIST_AIRING)
            return result
        except Exception as e:
            print(f"[anime_tools] airing_schedule error: {e}", flush=True)
            return "I couldn't get the airing schedule right now, try again in a moment."

    def character_info(self, name: str) -> str:
        if not name:
            return "Please tell me which character you'd like to know about."
        cache_key = f"anilist_character:{name.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        # voiceActors lives on Media.characters.edges, not Character.media.edges.
        # Fetching VAs would require a second query via Media — not worth the complexity.
        # We report name, description, and appearances only.
        query = """
        query ($search: String) {
          Character(search: $search) {
            name { full }
            description(asHtml: false)
            gender age
            media(sort: POPULARITY_DESC, perPage: 3) {
              nodes { title { romaji english } type }
            }
          }
        }
        """
        try:
            data = self._anilist_query(query, {"search": name})
            char = data.get("Character")
            if not char:
                return f"I couldn't find a character named {name}."

            full_name = (char.get("name") or {}).get("full") or name
            desc = _truncate(_strip_html(char.get("description") or ""), 160)
            media_data = char.get("media") or {}
            nodes = media_data.get("nodes", [])

            appearances = []
            for n in nodes[:2]:
                t = _prefer_english(n.get("title"))
                if t and t != "Unknown":
                    appearances.append(t)

            parts = [f"{full_name}"]
            if appearances:
                parts.append(f"is a character from {appearances[0]}")
            if desc:
                parts.append(desc)
            if len(appearances) > 1:
                parts.append(f"also appears in {appearances[1]}")

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_ANILIST_CHARACTER)
            return result
        except Exception as e:
            print(f"[anime_tools] character_info error: {e}", flush=True)
            return "I couldn't get that character info right now, try again in a moment."

    def recommend(self, title: str) -> str:
        if not title:
            return self.trending()
        cache_key = f"anilist_recommend:{title.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        # Step 1: get ID
        id_query = """
        query ($search: String) {
          Media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
            id title { romaji english }
          }
        }
        """
        try:
            id_data = self._anilist_query(id_query, {"search": title})
            media = id_data.get("Media")
            if not media:
                return f"I couldn't find {title} to base recommendations on."

            media_id = media["id"]
            source_name = _prefer_english(media.get("title"))

            rec_query = """
            query ($id: Int) {
              Media(id: $id) {
                recommendations(sort: RATING_DESC, perPage: 5) {
                  nodes {
                    rating
                    mediaRecommendation {
                      title { romaji english }
                      averageScore genres episodes status
                    }
                  }
                }
              }
            }
            """
            rec_data = self._anilist_query(rec_query, {"id": media_id})
            recs = ((rec_data.get("Media") or {})
                    .get("recommendations", {})
                    .get("nodes", []))

            if not recs:
                return f"I couldn't find recommendations for {source_name} right now."

            entries = []
            for node in recs[:4]:
                mr = node.get("mediaRecommendation")
                if not mr:
                    continue
                rec_name = _prefer_english(mr.get("title"))
                score = mr.get("averageScore")
                score_str = f"rated {score / 10:.1f}" if score else ""
                entry = rec_name
                if score_str:
                    entry += f" {score_str}"
                entries.append(entry)

            if not entries:
                return f"I couldn't find recommendations for {source_name} right now."

            result = f"If you liked {source_name}, you might enjoy: {', '.join(entries)}."
            self._cache.set(cache_key, result, TTL_ANILIST_RECOMMEND)
            return result
        except Exception as e:
            print(f"[anime_tools] recommend error: {e}", flush=True)
            return "I couldn't get recommendations right now, try again in a moment."

    def top_all_time(self, media_type: str = "ANIME") -> str:
        cache_key = f"anilist_top:{media_type}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        query = """
        query ($type: MediaType) {
          Page(page: 1, perPage: 5) {
            media(type: $type, sort: SCORE_DESC) {
              title { romaji english }
              averageScore episodes chapters status
            }
          }
        }
        """
        try:
            data = self._anilist_query(query, {"type": media_type.upper()})
            items = (data.get("Page") or {}).get("media", [])
            if not items:
                return f"I couldn't get the top {media_type.lower()} list right now."

            entries = []
            for i, m in enumerate(items, 1):
                name = _prefer_english(m.get("title"))
                score = m.get("averageScore")
                score_str = f"scored {score / 10:.1f}" if score else ""
                entry = f"number {i}, {name}"
                if score_str:
                    entry += f" {score_str}"
                entries.append(entry)

            media_label = "anime" if media_type.upper() == "ANIME" else "manga"
            result = f"Top {media_label} of all time: {', '.join(entries)}."
            self._cache.set(cache_key, result, TTL_ANILIST_TRENDING)
            return result
        except Exception as e:
            print(f"[anime_tools] top_all_time error: {e}", flush=True)
            return "I couldn't get the top list right now, try again in a moment."

    def anilist_add_to_list(self, title: str, status: str = "PLANNING") -> str:
        token = self._cfg("anilist", "access_token")
        if not token:
            return "I'm not connected to AniList right now. Please set up your access token."

        id_query = """
        query ($search: String) {
          Media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
            id title { romaji english }
          }
        }
        """
        try:
            id_data = self._anilist_query(id_query, {"search": title})
            media = id_data.get("Media")
            if not media:
                return f"I couldn't find {title} on AniList."

            media_id = media["id"]
            name = _prefer_english(media.get("title"))

            mutation = """
            mutation ($mediaId: Int, $status: MediaListStatus) {
              SaveMediaListEntry(mediaId: $mediaId, status: $status) {
                id status
                media { title { romaji english } }
              }
            }
            """
            result = self._anilist_query(mutation, {
                "mediaId": media_id,
                "status": status.upper()
            }, token=token)
            entry = result.get("SaveMediaListEntry", {})
            entry_status = (entry.get("status") or status).lower().replace("_", " ")
            return f"Added {name} to your AniList as {entry_status}."
        except Exception as e:
            print(f"[anime_tools] anilist_add error: {e}", flush=True)
            return "I couldn't update your AniList right now, try again in a moment."

    def anilist_update_progress(self, title: str, episode: int) -> str:
        token = self._cfg("anilist", "access_token")
        if not token:
            return "I'm not connected to AniList right now. Please set up your access token."

        id_query = """
        query ($search: String) {
          Media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
            id title { romaji english }
          }
        }
        """
        try:
            id_data = self._anilist_query(id_query, {"search": title})
            media = id_data.get("Media")
            if not media:
                return f"I couldn't find {title} on AniList."

            media_id = media["id"]
            name = _prefer_english(media.get("title"))

            mutation = """
            mutation ($mediaId: Int, $progress: Int, $status: MediaListStatus) {
              SaveMediaListEntry(mediaId: $mediaId, progress: $progress, status: $status) {
                id progress
              }
            }
            """
            self._anilist_query(mutation, {
                "mediaId": media_id,
                "progress": episode,
                "status": "CURRENT"
            }, token=token)
            return f"Updated {name} progress to episode {episode} on AniList."
        except Exception as e:
            print(f"[anime_tools] anilist_update_progress error: {e}", flush=True)
            return "I couldn't update your AniList progress right now, try again in a moment."

    # ── Kitsu ─────────────────────────────────────────────────────────────────

    def _kitsu_get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(
            f"{KITSU_BASE}/{path}",
            headers=KITSU_HEADERS,
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def kitsu_trending(self) -> str:
        cache_key = "kitsu_trending"
        cached = self._cache.get(cache_key)
        if cached:
            return cached
        try:
            data = self._kitsu_get("trending/anime")
            items = data.get("data", [])
            if not items:
                return "I couldn't get Kitsu trending anime right now."

            entries = []
            for i, item in enumerate(items[:5], 1):
                attr = item.get("attributes", {})
                titles = attr.get("titles", {})
                name = (titles.get("en") or titles.get("en_jp") or "Unknown")
                rating = attr.get("averageRating")
                score_str = f"rated {float(rating) / 10:.1f}" if rating else ""
                entry = f"number {i}, {name}"
                if score_str:
                    entry += f" {score_str}"
                entries.append(entry)

            result = f"Trending on Kitsu: {', '.join(entries)}."
            self._cache.set(cache_key, result, TTL_KITSU_TRENDING)
            return result
        except Exception as e:
            print(f"[anime_tools] kitsu_trending error: {e}", flush=True)
            return "I couldn't get Kitsu trending right now, try again in a moment."

    def kitsu_search(self, title: str) -> str:
        if not title:
            return "Please tell me what to search for on Kitsu."
        cache_key = f"kitsu_search:{title.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached
        try:
            data = self._kitsu_get("anime", params={
                "filter[text]": title,
                "page[limit]": 1,
                "fields[anime]": "titles,episodeCount,status,averageRating,synopsis,subtype",
            })
            items = data.get("data", [])
            if not items:
                return f"I couldn't find {title} on Kitsu."

            attr = items[0].get("attributes", {})
            titles = attr.get("titles", {})
            name = titles.get("en") or titles.get("en_jp") or title
            status = attr.get("status", "unknown")
            episodes = attr.get("episodeCount")
            rating = attr.get("averageRating")
            score_str = f"rated {float(rating) / 10:.1f} out of 10" if rating else "unrated"
            ep_str = f"with {episodes} episodes" if episodes else ""
            subtype = attr.get("subtype", "anime")
            synopsis = _truncate(_strip_html(attr.get("synopsis") or ""), 160)

            parts = [f"{name} is a {status} {subtype}"]
            if ep_str:
                parts[-1] += f" {ep_str}"
            parts.append(score_str)
            if synopsis:
                parts.append(synopsis)

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_KITSU_SEARCH)
            return result
        except Exception as e:
            print(f"[anime_tools] kitsu_search error: {e}", flush=True)
            return "I couldn't search Kitsu right now, try again in a moment."

    def kitsu_episodes(self, title: str) -> str:
        if not title:
            return "Please tell me which anime's episodes you want."
        try:
            # First find the anime ID
            data = self._kitsu_get("anime", params={
                "filter[text]": title, "page[limit]": 1,
                "fields[anime]": "titles,episodeCount,slug",
            })
            items = data.get("data", [])
            if not items:
                return f"I couldn't find {title} on Kitsu."

            anime_id = items[0]["id"]
            attr = items[0].get("attributes", {})
            titles = attr.get("titles", {})
            name = titles.get("en") or titles.get("en_jp") or title
            total = attr.get("episodeCount")

            ep_data = self._kitsu_get(f"anime/{anime_id}/episodes", params={
                "sort": "number", "page[limit]": 5,
            })
            episodes = ep_data.get("data", [])
            if not episodes:
                return f"{name} has {total or 'an unknown number of'} episodes but episode details are unavailable."

            ep_list = []
            for ep in episodes[:3]:
                ea = ep.get("attributes", {})
                num = ea.get("number")
                ep_titles = ea.get("titles", {})
                ep_name = ep_titles.get("en_us") or f"Episode {num}"
                airdate = ea.get("airdate", "")
                ep_list.append(f"episode {num}: {ep_name}" + (f" aired {airdate}" if airdate else ""))

            total_str = f"{total} episodes total" if total else "episode count unknown"
            result = f"{name} has {total_str}. First few: {', '.join(ep_list)}."
            return result
        except Exception as e:
            print(f"[anime_tools] kitsu_episodes error: {e}", flush=True)
            return "I couldn't get episode info right now, try again in a moment."

    # ── Shikimori ─────────────────────────────────────────────────────────────

    def _shiki_headers(self, token: str = None) -> dict:
        app_name = self._cfg("shikimori", "app_name") or "MochiAssistant"
        h = {
            "User-Agent": f"{app_name}/1.0",
            "Content-Type": "application/json",
        }
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _shiki_token(self) -> Optional[str]:
        return self._cfg("shikimori", "access_token")

    def _shiki_refresh_token(self) -> bool:
        """Refresh Shikimori access token. Returns True on success."""
        client_id = self._cfg("shikimori", "client_id")
        client_secret = self._cfg("shikimori", "client_secret")
        refresh_token = self._cfg("shikimori", "refresh_token")
        if not all([client_id, client_secret, refresh_token]):
            return False
        try:
            app_name = self._cfg("shikimori", "app_name") or "MochiAssistant"
            resp = requests.post(SHIKI_TOKEN_URL,
                headers={"User-Agent": f"{app_name}/1.0"},
                data={   # Shikimori OAuth requires form-encoded, not JSON
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                }, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if "shikimori" not in self._config:
                    self._config["shikimori"] = {}
                self._config["shikimori"]["access_token"] = data.get("access_token")
                self._config["shikimori"]["refresh_token"] = data.get("refresh_token")
                self._save_config()
                return True
        except Exception as e:
            print(f"[anime_tools] shiki token refresh error: {e}", flush=True)
        return False

    def _shiki_get(self, path: str, params: dict = None, token: str = None) -> Any:
        for attempt in range(2):
            try:
                resp = requests.get(
                    f"{SHIKI_BASE}{path}",
                    headers=self._shiki_headers(token),
                    params=params,
                    timeout=10,
                )
                if resp.status_code == 401 and attempt == 0 and token:
                    if self._shiki_refresh_token():
                        token = self._shiki_token()
                        continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError:
                raise
        return None

    def _shiki_post(self, path: str, body: dict, token: str) -> Any:
        for attempt in range(2):
            try:
                resp = requests.post(
                    f"{SHIKI_BASE}{path}",
                    headers=self._shiki_headers(token),
                    json=body,
                    timeout=10,
                )
                if resp.status_code == 401 and attempt == 0:
                    if self._shiki_refresh_token():
                        token = self._shiki_token()
                        continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError:
                raise
        return None

    def _shiki_patch(self, path: str, body: dict, token: str) -> Any:
        for attempt in range(2):
            try:
                resp = requests.patch(
                    f"{SHIKI_BASE}{path}",
                    headers=self._shiki_headers(token),
                    json=body,
                    timeout=10,
                )
                if resp.status_code == 401 and attempt == 0:
                    if self._shiki_refresh_token():
                        token = self._shiki_token()
                        continue
                resp.raise_for_status()
                return resp.json()
            except requests.HTTPError:
                raise
        return None

    def shiki_search(self, title: str) -> str:
        if not title:
            return "Please tell me what to search for on Shikimori."
        cache_key = f"shiki_search:{title.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached
        try:
            results = self._shiki_get("/animes", params={"search": title, "limit": 1})
            if not results:
                return f"I couldn't find {title} on Shikimori."
            item = results[0]
            name = item.get("name") or title
            score = item.get("score", "0")
            episodes = item.get("episodes") or item.get("episodes_aired", "unknown")
            status = item.get("status", "unknown")
            kind = item.get("kind", "anime")
            result = (
                f"{name} is a {status} {kind} with {episodes} episodes, "
                f"scored {score} on Shikimori."
            )
            self._cache.set(cache_key, result, TTL_SHIKI_SEARCH)
            return result
        except Exception as e:
            print(f"[anime_tools] shiki_search error: {e}", flush=True)
            return "I couldn't search Shikimori right now, try again in a moment."

    def _shiki_find_anime_id(self, title: str) -> Optional[int]:
        try:
            results = self._shiki_get("/animes", params={"search": title, "limit": 1})
            if results:
                return results[0].get("id")
        except Exception:
            pass
        return None

    def _shiki_find_rate_id(self, anime_id: int, user_id: int, token: str) -> Optional[int]:
        # Path is /v2/user_rates (not /api/v2/...) because SHIKI_BASE already ends in /api
        try:
            rates = self._shiki_get("/v2/user_rates", params={
                "user_id": user_id,
                "target_id": anime_id,
                "target_type": "Anime",
            }, token=token)
            if rates:
                return rates[0].get("id")
        except Exception:
            pass
        return None

    def shiki_add(self, title: str, status: str = "planned") -> str:
        token = self._shiki_token()
        if not token:
            return "I'm not connected to Shikimori. Please set up your access token."
        user_id = self._cfg("shikimori", "user_id")
        if not user_id:
            return "Shikimori user ID is not configured."

        anime_id = self._shiki_find_anime_id(title)
        if not anime_id:
            return f"I couldn't find {title} on Shikimori."
        try:
            result = self._shiki_post("/v2/user_rates", {
                "user_rate": {
                    "user_id": user_id,
                    "target_id": anime_id,
                    "target_type": "Anime",
                    "status": status,
                }
            }, token=token)
            if result:
                return f"Added {title} to your Shikimori list as {status.replace('_', ' ')}."
            return f"I couldn't add {title} to Shikimori right now."
        except Exception as e:
            print(f"[anime_tools] shiki_add error: {e}", flush=True)
            return "I couldn't update Shikimori right now, try again in a moment."

    def shiki_update_status(self, title: str, status: str, episodes: int = None) -> str:
        token = self._shiki_token()
        if not token:
            return "I'm not connected to Shikimori. Please set up your access token."
        user_id = self._cfg("shikimori", "user_id")
        if not user_id:
            return "Shikimori user ID is not configured."

        anime_id = self._shiki_find_anime_id(title)
        if not anime_id:
            return f"I couldn't find {title} on Shikimori."

        rate_id = self._shiki_find_rate_id(anime_id, user_id, token)
        body = {"user_rate": {"status": status}}
        if episodes is not None:
            body["user_rate"]["episodes"] = episodes

        try:
            if rate_id:
                self._shiki_patch(f"/v2/user_rates/{rate_id}", body, token)
            else:
                body["user_rate"].update({"user_id": user_id, "target_id": anime_id, "target_type": "Anime"})
                self._shiki_post("/v2/user_rates", body, token)
            self._cache.delete(f"shiki_my_list:{status}")
            label = status.replace("_", " ")
            if episodes is not None:
                return f"Updated {title} to episode {episodes} and status {label} on Shikimori."
            return f"Marked {title} as {label} on Shikimori."
        except Exception as e:
            print(f"[anime_tools] shiki_update_status error: {e}", flush=True)
            return "I couldn't update Shikimori right now, try again in a moment."

    def shiki_set_episode(self, title: str, episode: int) -> str:
        return self.shiki_update_status(title, "watching", episodes=episode)

    def shiki_increment_episode(self, title: str) -> str:
        token = self._shiki_token()
        if not token:
            return "I'm not connected to Shikimori. Please set up your access token."
        user_id = self._cfg("shikimori", "user_id")
        if not user_id:
            return "Shikimori user ID is not configured."

        anime_id = self._shiki_find_anime_id(title)
        if not anime_id:
            return f"I couldn't find {title} on Shikimori."

        rate_id = self._shiki_find_rate_id(anime_id, user_id, token)
        if not rate_id:
            return f"You don't have {title} in your Shikimori list yet. Add it first."
        try:
            self._shiki_post(f"/v2/user_rates/{rate_id}/increment", {}, token)
            return f"Incremented episode count for {title} on Shikimori."
        except Exception as e:
            print(f"[anime_tools] shiki_increment error: {e}", flush=True)
            return "I couldn't update your episode count right now, try again in a moment."

    def shiki_my_list(self, status_filter: str = "watching") -> str:
        token = self._shiki_token()
        if not token:
            return "I'm not connected to Shikimori. Please set up your access token."
        user_id = self._cfg("shikimori", "user_id")
        if not user_id:
            return "Shikimori user ID is not configured."

        cache_key = f"shiki_my_list:{status_filter}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached

        try:
            rates = self._shiki_get("/v2/user_rates", params={
                "user_id": user_id,
                "target_type": "Anime",
                "status": status_filter,
                "limit": 10,
            }, token=token)
            if not rates:
                return f"Your {status_filter.replace('_', ' ')} list is empty."

            # Fetch anime names for each rate
            names = []
            for rate in rates[:8]:
                target_id = rate.get("target_id")
                ep = rate.get("episodes", 0)
                try:
                    details = self._shiki_get(f"/animes/{target_id}")
                    name = (details or {}).get("name") or f"Anime {target_id}"
                except Exception:
                    name = f"Anime {target_id}"
                entry = name
                if ep:
                    entry += f" at episode {ep}"
                names.append(entry)
                time.sleep(0.2)  # respect rate limit

            label = status_filter.replace("_", " ")
            result = f"Your {label} list: {', '.join(names)}."
            self._cache.set(cache_key, result, TTL_SHIKI_MY_LIST)
            return result
        except Exception as e:
            print(f"[anime_tools] shiki_my_list error: {e}", flush=True)
            return "I couldn't get your list right now, try again in a moment."

    # ── ANN ───────────────────────────────────────────────────────────────────

    def _ann_get(self, params: dict, endpoint: str = "api.xml") -> ET.Element:
        resp = requests.get(
            f"{ANN_BASE}/{endpoint}",
            params=params,
            headers={"User-Agent": "MochiAssistant/1.0"},
            timeout=15,
        )
        resp.raise_for_status()
        return ET.fromstring(resp.text)

    def ann_news(self) -> str:
        cache_key = "ann_news"
        cached = self._cache.get(cache_key)
        if cached:
            return cached
        try:
            r = requests.get(
                f"{ANN_BASE}/reports.xml",
                params={"id": 169, "type": "anime", "nlist": 5},
                headers={"User-Agent": "MochiAssistant/1.0"},
                timeout=15,
            )
            r.raise_for_status()

            # ANN sometimes returns multiple concatenated XML roots (malformed).
            # Wrap in a synthetic root to tolerate this.
            safe_xml = f"<root>{r.text}</root>"
            try:
                root = ET.fromstring(safe_xml)
            except ET.ParseError:
                # Last resort: pull text between <name> tags via regex
                names = re.findall(r"<name[^>]*>([^<]+)</name>", r.text)
                if names:
                    result = f"Recent additions on Anime News Network: {', '.join(names[:5])}."
                    self._cache.set(cache_key, result, TTL_ANN_NEWS)
                    return result
                return "I couldn't get anime news right now."

            items = []
            for item in root.iter("item"):
                name_el = item.find("name") or item.find("title")
                if name_el is not None and name_el.text:
                    items.append(name_el.text)
            if not items:
                return "I couldn't find recent anime news right now."
            result = f"Recent additions on Anime News Network: {', '.join(items[:5])}."
            self._cache.set(cache_key, result, TTL_ANN_NEWS)
            return result
        except Exception as e:
            print(f"[anime_tools] ann_news error: {e}", flush=True)
            return "I couldn't get anime news right now, try again in a moment."

    def ann_details(self, title: str) -> str:
        if not title:
            return "Please tell me which anime you want details on."
        cache_key = f"ann_details:{title.lower()}"
        cached = self._cache.get(cache_key)
        if cached:
            return cached
        try:
            root = self._ann_get({"title": title})
            anime_el = root.find("anime")
            if anime_el is None:
                return f"I couldn't find {title} on Anime News Network."

            time.sleep(1)  # rate limit before second call
            ann_id = anime_el.get("id")
            if ann_id:
                root2 = self._ann_get({"anime": ann_id})
                anime_el = root2.find("anime") or anime_el

            ann_title = anime_el.get("name") or title
            infos = {el.get("type"): el.text for el in anime_el.findall("info") if el.get("type")}
            plot = _truncate(infos.get("Plot Summary") or "", 180)
            genres = infos.get("Genres") or ""
            episodes = infos.get("Number of episodes") or "unknown"
            vintage = infos.get("Vintage") or ""
            credits_raw = {}
            for el in anime_el.findall("credit"):
                ctype = el.get("type")
                person_el = el.find("person")
                if ctype and person_el is not None and person_el.text:
                    credits_raw[ctype] = person_el.text
            director = credits_raw.get("Director", "")

            parts = [f"{ann_title}"]
            if vintage:
                parts.append(f"aired {vintage}")
            parts.append(f"{episodes} episodes")
            if genres:
                parts.append(f"genres: {genres}")
            if director:
                parts.append(f"directed by {director}")
            if plot:
                parts.append(plot)

            result = ". ".join(parts) + "."
            self._cache.set(cache_key, result, TTL_ANN_DETAILS)
            return result
        except Exception as e:
            print(f"[anime_tools] ann_details error: {e}", flush=True)
            return "I couldn't get that info from Anime News Network right now, try again in a moment."

    # ── AnimeChan ─────────────────────────────────────────────────────────────

    def random_quote(self, anime: str = None, character: str = None) -> str:
        # If specific anime or character requested, try API directly
        if anime or character:
            params = {}
            if anime:
                params["anime"] = anime
            if character:
                params["character"] = character
            try:
                r = requests.get(
                    f"{ANIMECHAN_BASE}/quotes/random",
                    headers=self._animechan_headers(),
                    params=params,
                    timeout=8,
                )
                if r.status_code == 200:
                    data = r.json().get("data")
                    if data:
                        return self._format_quote(data)
                elif r.status_code == 404:
                    src = anime or character
                    return f"I couldn't find a quote from {src}."
            except Exception as e:
                print(f"[anime_tools] random_quote error: {e}", flush=True)

        # Serve from local cache
        if self._quote_cache:
            quote = self._quote_cache.popleft()
            # Refill if running low
            if len(self._quote_cache) < 5:
                t = threading.Thread(target=self._refill_quote_cache, daemon=True)
                t.start()
            return self._format_quote(quote)

        # Cache empty — try API one more time
        try:
            r = requests.get(
                f"{ANIMECHAN_BASE}/quotes/random",
                headers=self._animechan_headers(),
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json().get("data")
                if data:
                    return self._format_quote(data)
        except Exception as e:
            print(f"[anime_tools] random_quote fallback error: {e}", flush=True)

        return "I couldn't get a quote right now, try again in a moment."

    def _format_quote(self, data: dict) -> str:
        content = data.get("content") or data.get("quote") or ""
        anime_info = data.get("anime") or {}
        char_info = data.get("character") or {}
        anime_name = anime_info.get("name") or ""
        char_name = char_info.get("name") or ""

        if char_name and anime_name:
            return f'Here\'s a quote from {char_name} in {anime_name}: "{content}"'
        elif anime_name:
            return f'Here\'s a quote from {anime_name}: "{content}"'
        elif content:
            return f'Here\'s an anime quote: "{content}"'
        return "I couldn't get a quote right now."

    # ── Anime Facts ───────────────────────────────────────────────────────────

    def _resolve_anime_name(self, name: str) -> str:
        lower = name.lower().strip()
        # Check aliases first
        if lower in FACT_ALIASES:
            return FACT_ALIASES[lower]
        # Check exact match in facts list
        if lower in self._facts_anime_list:
            return self._facts_anime_list[lower]
        # Check if it starts with any known name
        for key, canonical in self._facts_anime_list.items():
            if lower.replace(" ", "_") == key:
                return canonical
        # Return as-is (lowercased, underscored)
        return lower.replace(" ", "_")

    def anime_fact(self, anime_name: str) -> str:
        if not anime_name:
            return "Please tell me which anime you want a fact about."
        if not self._facts_available and not self._facts_anime_list:
            return "The anime facts service isn't available right now. It may be offline."
        resolved = self._resolve_anime_name(anime_name)
        cache_key = f"facts:{resolved}"
        facts = self._cache.get(cache_key)

        if not facts:
            try:
                r = requests.get(f"{FACTS_BASE}/{resolved}", timeout=20)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("success"):
                        facts = [f["fact"] for f in data.get("data", []) if "fact" in f]
                        if facts:
                            self._cache.set(cache_key, facts, None)  # cache forever
                elif r.status_code == 404:
                    # Try original name with spaces as underscores
                    alt = anime_name.lower().replace(" ", "_")
                    r2 = requests.get(f"{FACTS_BASE}/{alt}", timeout=20)
                    if r2.status_code == 200:
                        data2 = r2.json()
                        if data2.get("success"):
                            facts = [f["fact"] for f in data2.get("data", []) if "fact" in f]
                            if facts:
                                self._cache.set(f"facts:{alt}", facts, None)
            except Exception as e:
                print(f"[anime_tools] anime_fact error: {e}", flush=True)

        if not facts:
            return f"I couldn't find any facts about {anime_name} right now."

        fact = random.choice(facts)
        display_name = resolved.replace("_", " ").title()
        return f"Here's a fact about {display_name}: {fact}"

    def list_fact_anime(self) -> str:
        if self._facts_anime_list:
            names = [n.replace("_", " ").title() for n in list(self._facts_anime_list.keys())[:10]]
            return f"I have facts about: {', '.join(names)}."
        return "I can share facts about Bleach, Naruto, One Piece, Fullmetal Alchemist Brotherhood, Attack on Titan, Demon Slayer, and more."

    # ── Dispatcher ────────────────────────────────────────────────────────────

    def dispatch(self, tag: str) -> str:
        """
        Parse a [CMD:ANIME:*] tag and call the appropriate method.
        Returns a TTS-ready string. Never raises.
        """
        try:
            inner = tag.strip()
            if inner.startswith("[") and inner.endswith("]"):
                inner = inner[1:-1]
            parts = inner.split(":")

            if len(parts) < 3 or parts[0] != "CMD" or parts[1] != "ANIME":
                return "I didn't understand that anime command."

            action = parts[2].upper()
            arg = ":".join(parts[3:]).strip() if len(parts) > 3 else ""

            if action == "SEARCH":
                title, _, mtype = arg.partition(":")
                return self.search(title.strip(), mtype.strip() or "ANIME")

            elif action == "TRENDING":
                return self.trending()

            elif action == "SEASONAL":
                return self.seasonal()

            elif action == "AIRING":
                return self.airing_schedule(arg)

            elif action == "CHARACTER":
                return self.character_info(arg)

            elif action == "RECOMMEND":
                return self.recommend(arg)

            elif action == "TOP":
                return self.top_all_time(arg or "ANIME")

            elif action == "QUOTE":
                a_parts = arg.split(":", 1)
                anime_q = a_parts[0].strip() or None
                char_q = a_parts[1].strip() if len(a_parts) > 1 else None
                return self.random_quote(anime=anime_q, character=char_q)

            elif action == "FACT":
                return self.anime_fact(arg)

            elif action == "NEWS":
                return self.ann_news()

            elif action == "ADD":
                return self.shiki_add(arg, "planned")

            elif action == "WATCHING":
                return self.shiki_add(arg, "watching")

            elif action == "DONE":
                return self.shiki_update_status(arg, "completed")

            elif action == "DROP":
                return self.shiki_update_status(arg, "dropped")

            elif action == "HOLD":
                return self.shiki_update_status(arg, "on_hold")

            elif action == "EPISODE":
                title, _, ep = arg.partition(":")
                title = title.strip()
                ep = ep.strip()
                if ep.isdigit():
                    return self.shiki_set_episode(title, int(ep))
                return self.shiki_increment_episode(title)

            elif action == "LIST":
                return self.shiki_my_list(arg or "watching")

            elif action == "KITSUTRENDING":
                return self.kitsu_trending()

            elif action == "KITSUSEARCH":
                return self.kitsu_search(arg)

            elif action == "ANNDETAILS":
                return self.ann_details(arg)

            elif action == "LISTFACT":
                return self.list_fact_anime()

            else:
                return f"I don't know how to handle the anime command {action}."

        except Exception as e:
            print(f"[anime_tools] dispatch error: {e}", flush=True)
            return "I ran into a problem with that anime request."


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    manager = AnimeManager()
    manager.warmup()
    time.sleep(3)  # let warmup complete

    tests = [
        ("search Frieren",                  lambda: manager.search("Frieren")),
        ("trending",                         lambda: manager.trending()),
        ("random quote",                     lambda: manager.random_quote()),
        ("quote from Naruto",                lambda: manager.random_quote(anime="Naruto")),
        ("fact about bleach",                lambda: manager.anime_fact("bleach")),
        ("ann news",                         lambda: manager.ann_news()),
        ("character Levi",                   lambda: manager.character_info("Levi")),
        ("dispatch SEARCH One Piece",        lambda: manager.dispatch("[CMD:ANIME:SEARCH:One Piece]")),
        ("dispatch QUOTE Bleach",            lambda: manager.dispatch("[CMD:ANIME:QUOTE:Bleach]")),
        ("dispatch FACT naruto",             lambda: manager.dispatch("[CMD:ANIME:FACT:naruto]")),
        ("dispatch TRENDING",                lambda: manager.dispatch("[CMD:ANIME:TRENDING]")),
        ("dispatch TOP",                     lambda: manager.dispatch("[CMD:ANIME:TOP]")),
        ("recommend Re Zero",                lambda: manager.recommend("Re Zero")),
        ("kitsu trending",                   lambda: manager.kitsu_trending()),
        ("kitsu search SAO",                 lambda: manager.kitsu_search("Sword Art Online")),
        ("dispatch AIRING Solo Leveling",    lambda: manager.dispatch("[CMD:ANIME:AIRING:Solo Leveling]")),
        ("fact aliases - fma",               lambda: manager.anime_fact("fma")),
        ("list fact anime",                  lambda: manager.list_fact_anime()),
    ]

    for label, test in tests:
        print(f"\n--- {label} ---")
        try:
            result = test()
            print(result)
        except Exception as e:
            print(f"FAILED: {e}")
        time.sleep(0.8)  # be gentle with APIs
