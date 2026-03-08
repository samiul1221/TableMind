"""
memory.py — Conversation memory system for the voice assistant.

Implements a 6-layer memory architecture:
  Layer 1: System identity (permanent prompt)
  Layer 2: User profile (persistent across sessions)
  Layer 3: Episodic memory (turn-decay, persistent per session)
  Layer 4: Emotional state (VADER sentiment, session-only)
  Layer 5: Rolling conversation buffer (session-only)
  Layer 6: Topic tracker (stop-word filtered, session-only)

Public interface:
  ConversationMemory(session_id)
    .build_prompt(user_text) -> List[dict]
    .record_turn(user_text, assistant_text)
    .save_all()
"""

import json
import os
import re
import uuid
import glob
import time
from collections import Counter, deque
from datetime import datetime
from typing import List, Dict, Optional, Any

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# ─── Constants ────────────────────────────────────────────────────────────────

MEMORY_DIR = os.path.expanduser("~/voice-assistant/memory")

# Episodic limits
MAX_EPISODIC = 50
COMPRESS_THRESHOLD = 40
MAX_CROSS_SESSION_ENTRIES = 30
CROSS_SESSION_FILES = 3         # load last N session files at startup

# Turn decay coefficient
TURN_DECAY_RATE = 0.05

# Top-K retrieval
TOP_K_EPISODIC = 5
TOP_K_CROSS_SESSION = 2

# User profile limits
MAX_PREFERENCES = 10
MAX_FACTS = 20

# ─── Enums / Type Weights ────────────────────────────────────────────────────

MEMORY_TYPES = {
    "CORRECTION", "FACT", "PREFERENCE", "EMOTION",
    "TASK", "QUESTION", "SUMMARY",
}

TYPE_WEIGHT = {
    "CORRECTION": 1.0,
    "FACT":       0.85,
    "PREFERENCE": 0.80,
    "EMOTION":    0.75,
    "TASK":       0.70,
    "QUESTION":   0.40,
    "SUMMARY":    0.60,
}

# Mood enum values
MOODS = {
    "NEUTRAL", "CURIOUS", "FRUSTRATED", "HAPPY",
    "CONFUSED", "IMPATIENT", "ENGAGED", "TIRED",
}

# ─── Layer 1: System Identity ────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a helpful voice assistant. You speak concisely because "
    "responses are converted to speech. Keep answers under 3 sentences "
    "unless asked to elaborate. You have memory of this conversation "
    "and past sessions. IMPORTANT: Never include stage directions, "
    "parenthetical notes, or metadata like '(speaking calmly)' or "
    "'(in a friendly tone)' in your responses. Output only the words "
    "you would say out loud."
)

# ─── Layer 6: Stop Words ─────────────────────────────────────────────────────

STOP_WORDS = {
    # Articles & determiners
    "a", "an", "the", "this", "that", "these", "those",
    # Prepositions
    "in", "on", "at", "to", "for", "of", "with", "by",
    "from", "up", "about", "into", "through", "during",
    # Conjunctions
    "and", "but", "or", "so", "yet", "nor", "as", "if",
    "then", "than", "because", "while", "although",
    # Pronouns
    "i", "me", "my", "you", "your", "he", "she", "it",
    "we", "they", "them", "their", "its", "our", "his",
    # Common verbs (auxiliary)
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall",
    "can", "need", "dare", "ought", "used",
    # Filler words common in speech
    "just", "like", "okay", "ok", "yeah", "yes", "no",
    "um", "uh", "so", "well", "actually", "basically",
    "kind", "sort", "thing", "stuff", "something", "anything",
    "really", "very", "much", "also", "still", "already",
    "now", "here", "there", "then", "when", "where",
    "what", "how", "why", "who", "which",
    # Numbers as words
    "one", "two", "three", "first", "second", "last",
}

# ─── Profile Extraction Patterns ─────────────────────────────────────────────

_NAME_RE = re.compile(
    r"(?:call me|i'm|i am|my name is)\s+(\w+)", re.IGNORECASE
)
_FACT_RE = re.compile(
    r"i\s+(?:live|work|am|have|own|use)\s+(.+)", re.IGNORECASE
)
_PREF_RE = re.compile(
    r"(?:i prefer|i like|i want you to|always|never)\s+(.+)", re.IGNORECASE
)

# Correction keywords
_CORRECTION_KEYWORDS = {"no", "wrong", "not right", "incorrect", "that's not"}

# IoT command detection — matches assistant responses containing [CMD:...]
_IOT_CMD_RE = re.compile(r'\[CMD:', re.IGNORECASE)

# IoT intent keywords — used in build_prompt() to skip episodic injection
_IOT_INTENT_WORDS = frozenset({
    "lamp", "light", "lights", "lock", "unlock",
    "brightness", "dim", "brighten", "color", "colour", "scene",
})

# Task patterns
_TASK_RE = re.compile(
    r"(?:remind me|set .* timer|add .* to|schedule|don't forget)\s+(.+)",
    re.IGNORECASE,
)

# ─── VADER Singleton ─────────────────────────────────────────────────────────

_vader = SentimentIntensityAnalyzer()

# ─── Helper: JSON I/O ────────────────────────────────────────────────────────


def _load_json(path: str) -> Optional[dict]:
    """Load a JSON file, return None if missing or corrupt."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_json(path: str, data: Any) -> None:
    """Atomically save JSON (write-then-rename)."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.replace(tmp, path)


# ─── Topic Extraction (Layer 6) ──────────────────────────────────────────────


def extract_topics(text: str) -> List[str]:
    """Return top-3 content words from text, stop-word filtered."""
    words = text.lower().split()
    words = [w.strip(".,!?;:'\"()[]") for w in words]
    content_words = [w for w in words if w not in STOP_WORDS and len(w) > 2]
    freq = Counter(content_words)
    return [word for word, _ in freq.most_common(3)]


# ─── Mood Detection (Layer 4) ────────────────────────────────────────────────


def detect_mood(text: str) -> tuple:
    """
    Returns (mood_str, vader_compound).
    Uses VADER for sentiment, layered with structural heuristics.
    """
    scores = _vader.polarity_scores(text)
    compound = scores["compound"]

    word_count = len(text.split())
    has_question = text.strip().endswith("?")
    is_short = word_count < 5

    if compound >= 0.5:
        mood = "HAPPY"
    elif compound <= -0.4:
        mood = "FRUSTRATED"
    elif is_short and has_question:
        mood = "IMPATIENT"
    elif has_question and not is_short:
        mood = "CURIOUS"
    elif compound >= 0.1:
        mood = "ENGAGED"
    else:
        mood = "NEUTRAL"

    return mood, compound


# ─── Importance Scoring (Layer 3) ────────────────────────────────────────────


def compute_importance(mem: dict, current_turn: int) -> float:
    """Compute final importance score with turn-based decay."""
    # Clamp to zero: cross-session memories (shifted to negative turn_index)
    # must never produce a recency > 1.0 that incorrectly boosts them.
    turns_since = max(0, current_turn - mem.get("turn_index", 0))
    recency = 1.0 / (1 + turns_since * TURN_DECAY_RATE)

    base = TYPE_WEIGHT.get(mem.get("type", "QUESTION"), 0.4)
    reuse = min(0.3, mem.get("access_count", 0) * 0.05)
    return min(1.0, base + reuse) * recency


# ─── Episodic Compression ────────────────────────────────────────────────────


def _compress_episodic(store: list) -> list:
    """
    Compress episodic store when it exceeds COMPRESS_THRESHOLD.
    Groups nearby memories (within 5 turns), keeps the most important
    verbatim, merges the rest into SUMMARY entries.
    """
    if len(store) <= COMPRESS_THRESHOLD:
        return store

    # Sort by turn_index
    store.sort(key=lambda m: m["turn_index"])

    groups: list = []
    current_group: list = []

    for mem in store:
        if not current_group:
            current_group.append(mem)
        elif abs(mem["turn_index"] - current_group[-1]["turn_index"]) <= 5:
            current_group.append(mem)
        else:
            groups.append(current_group)
            current_group = [mem]
    if current_group:
        groups.append(current_group)

    result = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
            continue

        # Keep highest-importance entry verbatim
        group.sort(key=lambda m: m.get("importance", 0), reverse=True)
        best = group[0]
        rest = group[1:]

        result.append(best)

        if rest:
            median_turn = sorted(m["turn_index"] for m in group)[len(group) // 2]
            avg_imp = sum(m.get("importance", 0.5) for m in group) / len(group)
            merged_content = "; ".join(m["content"] for m in rest)
            all_tags = list({t for m in rest for t in m.get("tags", [])})

            summary = {
                "id": str(uuid.uuid4()),
                "timestamp": datetime.now().isoformat(),
                "turn_index": median_turn,
                "type": "SUMMARY",
                "content": f"Around turn {median_turn}: {merged_content}",
                "importance": min(1.0, avg_imp * 1.1),
                "access_count": 0,
                "tags": all_tags[:6],
                "session_id": best.get("session_id", ""),
            }
            result.append(summary)

    return result


# ═══════════════════════════════════════════════════════════════════════════════
#  ConversationMemory — main class
# ═══════════════════════════════════════════════════════════════════════════════


class ConversationMemory:
    """
    Manages all 6 memory layers. Two public methods:
      build_prompt(user_text)  → List[dict]  (call before Ollama)
      record_turn(user_text, assistant_text)  (call after Ollama)
      save_all()               (call at shutdown)
    """

    def __init__(self, session_id: str, llm_context_window: int = 2048):
        self.session_id = session_id
        self.current_turn = 0
        self.llm_context_window = llm_context_window

        # Ensure memory directory exists
        os.makedirs(MEMORY_DIR, exist_ok=True)

        # ── Layer 2: User profile ────────────────────────────────────
        self.user_profile = self._load_profile()

        # ── Layer 3: Episodic memory ─────────────────────────────────
        self.episodic_store: list = []
        self._load_cross_session_episodic()

        # Track which memories were retrieved last turn (for access_count bump)
        self._last_retrieved_ids: list = []

        # ── Layer 4: Emotional state ─────────────────────────────────
        self.emotional_state = {
            "current_mood": "NEUTRAL",
            "vader_compound": 0.0,
            "confidence": 0.5,
            "mood_history": deque(maxlen=5),
            "frustration_streak": 0,
            "engagement_level": 0.5,
        }

        # ── Layer 5: Conversation buffer ─────────────────────────────
        self.conversation_buffer: deque = deque(maxlen=10)

        # ── Layer 6: Topic tracker ───────────────────────────────────
        self.topic_tracker = {
            "current_topics": [],
            "topic_history": deque(maxlen=20),
            "topic_turn_map": {},
        }

        # Increment session count
        self.user_profile["total_sessions"] = (
            self.user_profile.get("total_sessions", 0) + 1
        )
        self.user_profile["last_seen"] = datetime.now().isoformat()
        self._save_profile()

        print(f"[memory] Session {session_id} | "
              f"Profile loaded (sessions: {self.user_profile['total_sessions']}) | "
              f"Episodic: {len(self.episodic_store)} cross-session memories")

    # ──────────────────────────────────────────────────────────────────────
    #  PUBLIC: build_prompt
    # ──────────────────────────────────────────────────────────────────────

    def build_prompt(self, user_text: str) -> List[dict]:
        """
        Called BEFORE sending to Ollama. Returns the full messages list.
        Updates topics, mood, and profile extraction as side effects.
        """
        # 1. Update topics (Layer 6)
        topics = extract_topics(user_text)
        self.topic_tracker["current_topics"] = topics
        for t in topics:
            self.topic_tracker["topic_history"].append(t)
            self.topic_tracker["topic_turn_map"].setdefault(t, []).append(
                self.current_turn
            )

        # 2. Update mood (Layer 4)
        mood, compound = detect_mood(user_text)
        self.emotional_state["current_mood"] = mood
        self.emotional_state["vader_compound"] = compound
        self.emotional_state["mood_history"].append(mood)
        self.emotional_state["engagement_level"] = (
            0.8 * self.emotional_state["engagement_level"]
            + 0.2 * max(0, compound)
        )
        if mood == "FRUSTRATED":
            self.emotional_state["frustration_streak"] += 1
        else:
            self.emotional_state["frustration_streak"] = 0
        # Confidence: higher when VADER is more decisive
        self.emotional_state["confidence"] = min(1.0, abs(compound) + 0.3)

        # 3. Retrieve relevant episodic memories (Layer 3)
        #    Note: profile extraction has been moved to record_turn() so it
        #    runs after the LLM call instead of before — avoids a disk write
        #    per turn and is semantically more correct (extracted info applies
        #    to the *next* turn's prompt, not the current one anyway).
        current_tags = set(topics)
        # Also include recent topic history for broader matching
        for t in list(self.topic_tracker["topic_history"])[-6:]:
            current_tags.add(t)

        retrieved = self._retrieve_episodic(current_tags)
        self._last_retrieved_ids = [m["id"] for m in retrieved]

        # 5. Separate cross-session from current-session memories
        cross_session = [
            m for m in retrieved if m.get("session_id") != self.session_id
        ][:TOP_K_CROSS_SESSION]
        current_session = [
            m for m in retrieved if m.get("session_id") == self.session_id
        ]

        # 6. Assemble prompt
        messages = []

        # Detect IoT intent — used to skip episodic injection below
        is_iot_intent = any(w in user_text.lower() for w in _IOT_INTENT_WORDS)

        # Layer 1: System identity
        messages.append({"role": "system", "content": SYSTEM_PROMPT})

        # Layer 2: User profile context
        profile_text = self._format_profile()
        if profile_text:
            messages.append({
                "role": "system",
                "content": f"User info: {profile_text}",
            })

        # Cross-session context (top 2 from past sessions)
        if cross_session:
            cross_texts = [m["content"] for m in cross_session]
            messages.append({
                "role": "system",
                "content": "From previous conversations: "
                           + " | ".join(cross_texts),
            })

        # Layer 4: Emotional context — skip for small context windows
        emotion_text = self._format_emotion()
        if emotion_text and self.llm_context_window >= 512:
            messages.append({
                "role": "system",
                "content": f"User emotional state: {emotion_text}",
            })

        # Layer 3: Relevant episodic memories — skip for IoT intents
        if current_session and not is_iot_intent:
            ep_texts = [
                f"[Turn {m['turn_index']}, {m['type']}] {m['content']}"
                for m in current_session
            ]
            messages.append({
                "role": "system",
                "content": "Relevant context from this session: "
                           + " | ".join(ep_texts),
            })

        # Layer 5: Conversation buffer (raw turns)
        for turn in self.conversation_buffer:
            messages.append(turn)

        return messages

    # ──────────────────────────────────────────────────────────────────────
    #  PUBLIC: record_turn
    # ──────────────────────────────────────────────────────────────────────

    def record_turn(self, user_text: str, assistant_text: str, is_iot: bool = False) -> None:
        """
        Called AFTER Ollama responds. Updates all memory layers and persists.
        is_iot should be passed explicitly from the intent classifier in
        assistant.py — more reliable than detecting [CMD:] in assistant_text
        since the model sometimes fails to emit the tag.
        """

        # 1. Extract profile info (Layer 2) — done here, not in build_prompt(),
        #    to batch disk writes and avoid a profile save on every LLM call.
        self._extract_profile(user_text)

        # 2. Append to conversation buffer (Layer 5) — IoT turns skipped
        if not is_iot:
            self.conversation_buffer.append({"role": "user", "content": user_text})
            self.conversation_buffer.append(
                {"role": "assistant", "content": assistant_text}
            )
        self._save_conversation()

        # 3. Increment turn
        self.current_turn += 1

        # 4. Evaluate episodic storage — IoT turns carry no meaningful episodic
        if not is_iot:
            compound = self.emotional_state["vader_compound"]
            topics = self.topic_tracker["current_topics"]

            # CORRECTION: keyword + VADER gate
            text_lower = user_text.lower()
            if any(kw in text_lower for kw in _CORRECTION_KEYWORDS) and compound < 0.0:
                self._add_episodic("CORRECTION", user_text.strip(), topics, 1.0)

            # FACT extraction
            fact_match = _FACT_RE.search(user_text)
            if fact_match:
                self._add_episodic("FACT", fact_match.group(0).strip(), topics, 0.85)

            # TASK extraction
            task_match = _TASK_RE.search(user_text)
            if task_match:
                self._add_episodic("TASK", task_match.group(0).strip(), topics, 0.70)

            # EMOTION spike
            if abs(compound) > 0.6:
                mood = self.emotional_state["current_mood"]
                self._add_episodic(
                    "EMOTION",
                    f"User felt {mood.lower()}: \"{user_text[:80]}\"",
                    topics,
                    0.75,
                )

        # 5. Bump access_count on retrieved memories
        for mem in self.episodic_store:
            if mem["id"] in self._last_retrieved_ids:
                mem["access_count"] = mem.get("access_count", 0) + 1

        # 6. Compress if needed
        if len(self.episodic_store) > COMPRESS_THRESHOLD:
            self.episodic_store = _compress_episodic(self.episodic_store)

        # 7. Persist episodic
        self._save_episodic()

    # ──────────────────────────────────────────────────────────────────────
    #  PUBLIC: save_all
    # ──────────────────────────────────────────────────────────────────────

    def save_all(self) -> None:
        """Called at shutdown. Persists all layers and updates symlink."""
        self._save_profile()
        self._save_episodic()
        self._save_conversation()
        self._update_latest_symlink()
        print(f"[memory] All data saved for session {self.session_id}.")

    # ──────────────────────────────────────────────────────────────────────
    #  PRIVATE: Layer 2 — Profile
    # ──────────────────────────────────────────────────────────────────────

    def _load_profile(self) -> dict:
        path = os.path.join(MEMORY_DIR, "user_profile.json")
        data = _load_json(path)
        if data:
            return data
        # Empty default
        profile = {
            "name": None,
            "preferences": [],
            "facts": [],
            "last_updated": datetime.now().isoformat(),
            "total_sessions": 0,
            "last_seen": datetime.now().isoformat(),
        }
        _save_json(path, profile)
        return profile

    def _save_profile(self) -> None:
        self.user_profile["last_updated"] = datetime.now().isoformat()
        path = os.path.join(MEMORY_DIR, "user_profile.json")
        _save_json(path, self.user_profile)

    def _extract_profile(self, text: str) -> None:
        """Regex extraction for name, facts, preferences."""
        changed = False

        # Name
        m = _NAME_RE.search(text)
        if m:
            name = m.group(1).capitalize()
            if self.user_profile["name"] != name:
                self.user_profile["name"] = name
                changed = True

        # Facts
        m = _FACT_RE.search(text)
        if m:
            fact = m.group(0).strip()
            facts = self.user_profile["facts"]
            if fact not in facts:
                facts.append(fact)
                if len(facts) > MAX_FACTS:
                    facts.pop(0)
                changed = True

        # Preferences
        m = _PREF_RE.search(text)
        if m:
            pref = m.group(0).strip()
            prefs = self.user_profile["preferences"]
            if pref not in prefs:
                prefs.append(pref)
                if len(prefs) > MAX_PREFERENCES:
                    prefs.pop(0)
                changed = True

        if changed:
            self._save_profile()

    def _format_profile(self) -> str:
        """Format profile for injection into prompt."""
        parts = []
        p = self.user_profile
        if p.get("name"):
            parts.append(f"Name: {p['name']}")
        if p.get("preferences"):
            parts.append(f"Preferences: {', '.join(p['preferences'][-3:])}")
        if p.get("facts"):
            parts.append(f"Known facts: {', '.join(p['facts'][-3:])}")
        return "; ".join(parts)

    # ──────────────────────────────────────────────────────────────────────
    #  PRIVATE: Layer 3 — Episodic Memory
    # ──────────────────────────────────────────────────────────────────────

    def _add_episodic(
        self, mem_type: str, content: str, tags: List[str], importance: float
    ) -> None:
        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "turn_index": self.current_turn,
            "type": mem_type,
            "content": content,
            "importance": importance,
            "access_count": 0,
            "tags": tags[:6],
            "session_id": self.session_id,
        }
        self.episodic_store.append(entry)

    def _retrieve_episodic(self, current_tags: set) -> list:
        """Retrieve top-K episodic memories by tag match + importance."""
        if not current_tags:
            # If no tags, score all by importance only
            scored = [
                (mem, compute_importance(mem, self.current_turn))
                for mem in self.episodic_store
            ]
        else:
            scored = [
                (mem, compute_importance(mem, self.current_turn))
                for mem in self.episodic_store
                if any(t in mem.get("tags", []) for t in current_tags)
            ]
        # Fallback: if tag matching returned too few, fill with top by importance
        if len(scored) < TOP_K_EPISODIC:
            seen_ids = {m["id"] for m, _ in scored}
            remaining = [
                (mem, compute_importance(mem, self.current_turn))
                for mem in self.episodic_store
                if mem["id"] not in seen_ids
            ]
            remaining.sort(key=lambda x: x[1], reverse=True)
            scored.extend(remaining[: TOP_K_EPISODIC - len(scored)])

        scored.sort(key=lambda x: x[1], reverse=True)
        return [mem for mem, _ in scored[:TOP_K_EPISODIC]]

    def _save_episodic(self) -> None:
        path = os.path.join(MEMORY_DIR, f"episodic_{self.session_id}.json")
        data = {
            "session_id": self.session_id,
            "total_turns": self.current_turn,
            "memories": self.episodic_store,
        }
        _save_json(path, data)

    def _load_cross_session_episodic(self) -> None:
        """Load episodic memories from the last N sessions."""
        pattern = os.path.join(MEMORY_DIR, "episodic_*.json")
        files = sorted(glob.glob(pattern), reverse=True)  # newest first

        loaded = []
        for fpath in files[:CROSS_SESSION_FILES]:
            # Skip the current session file if it already exists
            if self.session_id in os.path.basename(fpath):
                continue
            data = _load_json(fpath)
            if data and "memories" in data:
                loaded.extend(data["memories"])

        if not loaded:
            return

        # Score all loaded memories and keep top N
        # Reindex turn numbers: shift old turns to negative values
        # so current_turn=0 starts fresh and old memories decay naturally
        for mem in loaded:
            # Use a large fixed negative offset so past memories always
            # appear far in the past relative to current_turn=0. The -100
            # offset was unsafe: a session with >100 turns would still
            # produce a positive turn_index. compute_importance() also clamps
            # turns_since to 0, so both defences are in place.
            mem["turn_index"] = -999

        # Keep only the most important ones
        loaded.sort(
            key=lambda m: TYPE_WEIGHT.get(m.get("type", "QUESTION"), 0.4),
            reverse=True,
        )
        self.episodic_store = loaded[:MAX_CROSS_SESSION_ENTRIES]

    def _update_latest_symlink(self) -> None:
        """Update the episodic_latest.json symlink."""
        link_path = os.path.join(MEMORY_DIR, "episodic_latest.json")
        target = f"episodic_{self.session_id}.json"
        try:
            if os.path.islink(link_path) or os.path.exists(link_path):
                os.remove(link_path)
            os.symlink(target, link_path)
        except OSError:
            pass  # symlink not critical

    # ──────────────────────────────────────────────────────────────────────
    #  PRIVATE: Layer 4 — Emotion Formatting
    # ──────────────────────────────────────────────────────────────────────

    def _format_emotion(self) -> str:
        """Format current emotional state for prompt injection."""
        es = self.emotional_state
        mood = es["current_mood"]
        compound = es["vader_compound"]
        streak = es["frustration_streak"]

        parts = [f"mood={mood.lower()} (sentiment={compound:+.2f})"]

        if streak >= 2:
            parts.append(f"frustrated for {streak} consecutive turns")

        engagement = es["engagement_level"]
        if engagement > 0.6:
            parts.append("highly engaged")
        elif engagement < 0.2:
            parts.append("low engagement")

        return ", ".join(parts)

    # ──────────────────────────────────────────────────────────────────────
    #  PRIVATE: Layer 5 — Conversation Buffer
    # ──────────────────────────────────────────────────────────────────────

    def _save_conversation(self) -> None:
        path = os.path.join(MEMORY_DIR, f"conversation_{self.session_id}.json")
        data = {
            "session_id": self.session_id,
            "turns": list(self.conversation_buffer),
        }
        _save_json(path, data)
