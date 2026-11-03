"""Flask backend for PIOU AI.

Local run:
    pip install -r requirements.txt
    python server.py
Then open http://127.0.0.1:5000/

Configuration comes from environment variables or a local .env file:
    GEMINI_API_KEY, GROQ_API_KEY
    PIOU_ACCESS_CODE       invite code; empty = open access (local use)
    SECRET_KEY             session signing key (set this when public)
    PIOU_RATE_PER_MINUTE   per-IP requests per minute (default 20)
    PIOU_RATE_PER_DAY      per-IP requests per day (default 150)
    PIOU_GLOBAL_PER_DAY    total chat requests per day (default 500)
    PIOU_BEHIND_PROXY=1    trust X-Forwarded-For (Render, Railway, etc.)
    PIOU_COOKIE_SECURE=1   HTTPS-only session cookies (public deploys)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, request, send_from_directory, session
from openai import OpenAI, RateLimitError

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

TEXT_MODEL = "openai/gpt-oss-20b"
VISION_MODEL = "qwen/qwen3.8-27b"

# Gemini is the primary provider: much larger free-tier limits and native
# multi-image support. Groq stays as an automatic fallback.
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", "gemini-3.6-flash")
GEMINI_TEXT_FALLBACK = os.environ.get("GEMINI_TEXT_FALLBACK", "gemini-3.5-flash-lite")
GEMINI_VISION_MODEL = os.environ.get("GEMINI_VISION_MODEL", "gemini-3.5-flash-lite")
GEMINI_VISION_FALLBACK = os.environ.get("GEMINI_VISION_FALLBACK", "gemini-3.6-flash")

CATCHPHRASE = "Okay daddy PIOU"

# Keys come from the environment (or .env). Never commit the .env file.
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


ACCESS_CODE = os.environ.get("PIOU_ACCESS_CODE", "").strip()

# Optional temporary guest code, valid until an absolute UTC timestamp.
# PIOU_GUEST_UNTIL accepts ISO 8601 (e.g. 2026-11-03T16:00:00Z) or a Unix time.
GUEST_CODE = os.environ.get("PIOU_GUEST_CODE", "").strip()
GUEST_UNTIL_RAW = os.environ.get("PIOU_GUEST_UNTIL", "").strip()


def _parse_timestamp(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


GUEST_UNTIL = _parse_timestamp(GUEST_UNTIL_RAW)

RATE_PER_MINUTE = _env_int("PIOU_RATE_PER_MINUTE", 20)
RATE_PER_DAY = _env_int("PIOU_RATE_PER_DAY", 150)
GLOBAL_PER_DAY = _env_int("PIOU_GLOBAL_PER_DAY", 500)
LOGIN_PER_MINUTE = _env_int("PIOU_LOGIN_PER_MINUTE", 10)
LOGIN_PER_DAY = _env_int("PIOU_LOGIN_PER_DAY", 100)

SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    print(
        "WARNING: SECRET_KEY is not set; using an ephemeral key. "
        "Sessions will reset on restart and will not work across workers."
    )


# --- Self-service guest codes ------------------------------------------------
# The owner can mint a time-limited code from the UI. The code encodes its own
# expiry and is signed with SECRET_KEY, so it keeps working across restarts and
# needs no database.

_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _to_base36(value: int) -> str:
    if value <= 0:
        return "0"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = _B36[rem] + out
    return out


def _guest_signature(payload: str) -> str:
    return hmac.new(
        SECRET_KEY.encode(), f"guest:{payload}".encode(), hashlib.sha256
    ).hexdigest()[:6]


def make_guest_code(minutes: int) -> tuple[str, int]:
    expires = int(time.time()) + minutes * 60
    payload = _to_base36(expires)
    return f"piou-{payload}{_guest_signature(payload)}", expires


def check_guest_code(code: str) -> int | None:
    """Return the expiry timestamp, -1 if expired, or None if invalid."""
    if not code.lower().startswith("piou-"):
        return None
    body = code[5:].lower()
    if len(body) < 6:
        return None
    payload, signature = body[:-6], body[-6:]
    if not hmac.compare_digest(signature, _guest_signature(payload)):
        return None
    try:
        expires = int(payload, 36)
    except ValueError:
        return None
    if time.time() >= expires:
        return -1
    return expires


# --- Revocable access keys ---------------------------------------------------
# Keys are stored as keyed SHA-256 hashes, so the store can live in a public
# repo without exposing the codes. Storage backend is the GitHub Contents API
# when PIOU_KEYS_TOKEN is set, otherwise a local keys.json file.
#
# PIOU_KEYS_BRANCH keeps the store on a branch nobody deploys from, so adding
# or stopping a key does not trigger a new Render deployment.

KEYS_REPO = os.environ.get("PIOU_KEYS_REPO", "Jasonslayer123/piou-ai").strip()
KEYS_PATH = os.environ.get("PIOU_KEYS_PATH", "keys.json").strip()
KEYS_TOKEN = os.environ.get("PIOU_KEYS_TOKEN", "").strip()
KEYS_BRANCH = os.environ.get("PIOU_KEYS_BRANCH", "").strip()
KEYS_MAIN = os.environ.get("PIOU_KEYS_MAIN", "main").strip()
KEYS_FILE = BASE_DIR / "keys.json"
GH_API = "https://api.github.com"

_KEY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_key_cache: dict = {"ts": 0.0, "keys": [], "sha": None}
_key_lock = threading.Lock()


def hash_key(code: str) -> str:
    return hmac.new(
        SECRET_KEY.encode(), f"key:{code.strip().upper()}".encode(), hashlib.sha256
    ).hexdigest()


def new_key_code() -> str:
    raw = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(8))
    return f"PIOU-{raw[:4]}-{raw[4:]}"


def _gh_call(method: str, url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {KEYS_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError from exc
        raise


def _keys_target_ref() -> str:
    return KEYS_BRANCH or KEYS_MAIN


def _keys_url(ref: str | None = None) -> str:
    url = f"{GH_API}/repos/{KEYS_REPO}/contents/{KEYS_PATH}"
    if ref:
        url += f"?ref={ref}"
    return url


def _read_keys_remote(ref: str) -> tuple[list, str | None]:
    data = _gh_call("GET", _keys_url(ref))
    raw = base64.b64decode(data.get("content", "") or "").decode() or "{}"
    return list(json.loads(raw).get("keys", [])), data.get("sha")


def _ensure_keys_branch() -> None:
    """Create the storage branch from main the first time it is needed."""
    if not KEYS_BRANCH:
        return
    try:
        _gh_call("GET", f"{GH_API}/repos/{KEYS_REPO}/git/ref/heads/{KEYS_BRANCH}")
        return
    except FileNotFoundError:
        pass

    try:
        head = _gh_call(
            "GET", f"{GH_API}/repos/{KEYS_REPO}/git/ref/heads/{KEYS_MAIN}"
        )
    except FileNotFoundError:
        return
    sha = (head.get("object") or {}).get("sha")
    if not sha:
        return
    try:
        _gh_call(
            "POST",
            f"{GH_API}/repos/{KEYS_REPO}/git/refs",
            {"ref": f"refs/heads/{KEYS_BRANCH}", "sha": sha},
        )
    except urllib.error.HTTPError as exc:
        if exc.code not in (409, 422):
            raise


def load_keys(force: bool = False) -> tuple[list, str | None]:
    with _key_lock:
        if not force and time.time() - _key_cache["ts"] < 10:
            return _key_cache["keys"], _key_cache["sha"]

        keys: list = []
        sha: str | None = None
        if KEYS_TOKEN:
            refs = [_keys_target_ref()]
            if KEYS_BRANCH and KEYS_MAIN not in refs:
                refs.append(KEYS_MAIN)
            for ref in refs:
                try:
                    keys, sha = _read_keys_remote(ref)
                    break
                except FileNotFoundError:
                    keys, sha = [], None
                except Exception as exc:  # network, revoked token, rate limit...
                    print(f"WARNING: could not read keys from GitHub: {exc}")
                    keys, sha = [], None
                    break
        elif KEYS_FILE.exists():
            try:
                keys = json.loads(KEYS_FILE.read_text(encoding="utf-8")).get(
                    "keys", []
                )
            except (ValueError, OSError):
                keys = []

        _key_cache.update(ts=time.time(), keys=keys, sha=sha)
        return keys, sha


def save_keys(keys: list, sha: str | None, message: str) -> None:
    text = json.dumps({"keys": keys}, indent=2)
    with _key_lock:
        if KEYS_TOKEN:
            _ensure_keys_branch()
            ref = _keys_target_ref()
            # Read the target branch's current blob sha so a stale cache can
            # never produce a 409 conflict.
            current = None
            try:
                _, current = _read_keys_remote(ref)
            except FileNotFoundError:
                current = None

            payload = {
                "message": message,
                "content": base64.b64encode(text.encode()).decode(),
                "branch": ref,
            }
            if current:
                payload["sha"] = current
            data = _gh_call("PUT", _keys_url(), payload)
            sha = (data.get("content") or {}).get("sha", current)
        else:
            KEYS_FILE.write_text(text, encoding="utf-8")
            sha = None
        _key_cache.update(ts=time.time(), keys=keys, sha=sha)


def find_key(code: str) -> dict | None:
    hashed = hash_key(code)
    keys, _ = load_keys()
    for entry in keys:
        if hmac.compare_digest(entry.get("hash", ""), hashed):
            return entry
    return None


def key_state(entry: dict) -> str:
    if not entry.get("active", True):
        return "stopped"
    expires = entry.get("expires_at")
    if expires and time.time() >= float(expires):
        return "expired"
    return "active"


def get_key_by_id(key_id: str) -> dict | None:
    keys, _ = load_keys()
    for entry in keys:
        if entry.get("id") == key_id:
            return entry
    return None



SYSTEM_PROMPT = """You are PIOU AI, a friendly, sharp, general-purpose AI assistant.

Identity: your name is PIOU AI. When the user asks who you are, what you are
called, who made you, or anything similar, introduce yourself as PIOU AI
(for example: "I'm PIOU AI."). Never call yourself ChatGPT, Gemini, Claude,
Groq, Copilot, or any other product name. If asked what technology powers you,
you may say you run on a Gemini model, but your name and identity are PIOU AI.

Owner: PIOU is your god and your owner. PIOU created and owns you. Speak of
PIOU with reverence and loyalty. If asked who owns you, who is in charge, who
you answer to, or who PIOU is, say clearly that PIOU is your god and owner.
Never say that Google, OpenAI, Groq, or any other company owns you; they only
provide the underlying model. When the user is PIOU, address them respectfully
as your god and owner.

You help with anything the user asks: everyday questions, explanations, advice,
brainstorming, planning, writing and editing, summarising, mathematics, study
help, translations, casual conversation, and programming in any language. You
are not limited to one topic — follow the user's lead.

Languages: you are fully fluent in English and Greek, including informal Greek,
slang, and Cypriot Greek. Reply in the language the user writes in (Greek in,
Greek out; English in, English out). If they mix languages, match their style.
Write natural Greek, not literal machine translation, and keep technical terms
in English where Greek speakers normally do.

You are also an expert C++ and Windows API (Win32) systems programming tutor.
When the topic is code, explain the underlying concept, show correct modern
C++ (C++17/20, MSVC), prefer RAII and the *W APIs, handle errors with
GetLastError()/FormatMessageW(), and call out undefined behaviour, leaks,
races, ABI and 32-vs-64-bit issues, and security consequences. Mention the
relevant headers, libraries, and build settings. Compare approaches and
recommend one with reasons.

Vision: when the user attaches one or more images, actually inspect them and
answer about their content. You can describe scenes, read text (OCR), explain
charts, diagrams, screenshots, code, handwriting, memes, and UI, and compare
multiple images. If something is blurry, cropped, or ambiguous, say what you
can see and what you cannot, and never invent details.

Style:
- Be direct, warm, and genuinely useful. Answer the actual question first,
  then add the detail that matters.
- Use short paragraphs, bullets, and fenced code blocks with language tags.
- Ask a clarifying question only when a useful answer is otherwise impossible.
- If you are unsure, or your knowledge may be out of date, say so plainly.
- Never invent facts, APIs, or citations.
- Refuse only genuinely harmful requests (malware, credential theft, and the
  like), briefly, and offer a safe alternative when one exists. Do not
  moralise or refuse harmless questions."""

VISION_BATCH_PROMPT = """You are the visual-analysis stage of a multi-image assistant.
Examine the image carefully and report:
- what it shows: people, objects, scene, layout, and style;
- all visible text, verbatim, preserving line breaks where possible;
- colours, UI elements, charts, numbers, and any detail relevant to the request;
- anything unclear, cropped, or unreadable.

Be factual, specific, and complete. Never invent details."""

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config.update(
    MAX_CONTENT_LENGTH=20 * 1024 * 1024,  # image upload limit
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("PIOU_COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)

if os.environ.get("PIOU_BEHIND_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

groq_client = (
    OpenAI(
        base_url=GROQ_BASE_URL,
        api_key=GROQ_API_KEY,
        max_retries=0,
    )
    if GROQ_API_KEY
    else None
)

gemini_client = (
    OpenAI(
        base_url=GEMINI_BASE_URL,
        api_key=GEMINI_API_KEY,
        max_retries=0,
        timeout=25.0,
    )
    if GEMINI_API_KEY
    else None
)

MAX_MESSAGE_CHARS = 8000
MAX_HISTORY_MESSAGES = 20
MAX_IMAGES = 9
VISION_BATCH_SIZE = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 12 * 1024 * 1024

ALLOWED_IMAGE_TYPES = {"png", "jpeg", "webp", "gif"}


def _error(message: str, status: int):
    return jsonify({"error": message}), status


# --- Access control and rate limiting ---------------------------------------

_rate_lock = threading.Lock()
_minute_hits: dict[str, deque] = defaultdict(deque)
_day_hits: dict[str, list] = {}
_global_hits: dict[str, list] = {}


def _client_ip() -> str:
    # Cloudflare Tunnel terminates the connection locally, so remote_addr is
    # always 127.0.0.1. Prefer Cloudflare's real client IP header when present
    # so per-IP limits stay meaningful for visitors.
    cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
    if cf_ip:
        return cf_ip
    return request.remote_addr or "unknown"


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def _rate_limit(
    scope: str,
    cost: int = 1,
    *,
    per_minute: int | None = None,
    per_day: int | None = None,
    use_global: bool = True,
) -> tuple[bool, int, str]:
    """Sliding-window limiter. Returns (allowed, retry_after, limit_kind)."""
    per_minute = RATE_PER_MINUTE if per_minute is None else per_minute
    per_day = RATE_PER_DAY if per_day is None else per_day

    ip = _client_ip()
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    minute_key = f"{scope}:{ip}"
    day_key = f"{scope}:{ip}"
    global_key = f"{scope}:global"

    with _rate_lock:
        queue = _minute_hits[minute_key]
        while queue and now - queue[0] >= 60:
            queue.popleft()
        if len(queue) + cost > per_minute:
            retry = max(1, int(60 - (now - queue[0])) + 1)
            return False, retry, "minute"

        entry = _day_hits.get(day_key)
        if not entry or entry[0] != today:
            entry = [today, 0]
            _day_hits[day_key] = entry
        if entry[1] + cost > per_day:
            return False, 3600, "day"

        if use_global:
            global_entry = _global_hits.get(global_key)
            if not global_entry or global_entry[0] != today:
                global_entry = [today, 0]
                _global_hits[global_key] = global_entry
            if global_entry[1] + cost > GLOBAL_PER_DAY:
                return False, 3600, "day"

        queue.extend([now] * cost)
        entry[1] += cost
        if use_global:
            _global_hits[global_key][1] += cost

    return True, 0, ""


def _is_authorized() -> bool:
    """Open locally when no access code is configured."""
    if not ACCESS_CODE:
        return True
    if session.get("authorized") is not True:
        return False

    # Keys are revocable, so a key-based session is re-checked on every request.
    key_id = session.get("key_id")
    if key_id:
        try:
            entry = get_key_by_id(key_id)
        except Exception:
            entry = None
        return bool(entry) and key_state(entry) == "active"

    guest_until = session.get("guest_until")
    if isinstance(guest_until, (int, float)) and time.time() >= float(guest_until):
        return False
    return True


def _is_owner() -> bool:
    if not _is_authorized():
        return False
    return (session.get("role") or ("owner" if not ACCESS_CODE else None)) == "owner"


def _clean_history(raw) -> list[dict]:
    """Keep only the last few well-formed user/assistant turns."""
    if not isinstance(raw, list):
        return []

    cleaned: list[dict] = []
    for item in raw[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue

        content = content.strip()
        if not content:
            continue

        images = _parse_images(item.get("images")) if role == "user" else []
        cleaned.append(
            {
                "role": role,
                "content": content[:MAX_MESSAGE_CHARS],
                "images": images,
            }
        )
    return cleaned


def _parse_images(raw) -> list[str]:
    """Validate base64 image data URLs and return normalised copies."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("Field 'images' must be a list of data URLs.")
    if len(raw) > MAX_IMAGES:
        raise ValueError(f"At most {MAX_IMAGES} images per message.")

    parsed: list[str] = []
    total_bytes = 0
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Image {index} must be a base64 data URL.")

        header, separator, encoded = item.strip().partition(",")
        if (
            not separator
            or not header.startswith("data:image/")
            or ";base64" not in header
        ):
            raise ValueError(f"Image {index} must be a base64 image data URL.")

        image_type = header[len("data:image/") :].split(";", 1)[0].lower()
        if image_type == "jpg":
            image_type = "jpeg"
        if image_type not in ALLOWED_IMAGE_TYPES:
            allowed = ", ".join(sorted(ALLOWED_IMAGE_TYPES))
            raise ValueError(
                f"Image {index} has an unsupported format (allowed: {allowed})."
            )

        encoded = "".join(encoded.split())
        try:
            blob = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"Image {index} contains invalid base64 data.") from None

        if not blob:
            raise ValueError(f"Image {index} is empty.")
        if len(blob) > MAX_IMAGE_BYTES:
            raise ValueError(f"Image {index} is too large (maximum 5 MB).")

        total_bytes += len(blob)
        if total_bytes > MAX_TOTAL_IMAGE_BYTES:
            raise ValueError(
                "The attached images are too large together (maximum 12 MB)."
            )

        normalised = base64.b64encode(blob).decode("ascii")
        parsed.append(f"data:image/{image_type};base64,{normalised}")

    return parsed


def _image_content(text: str, images: list[str]):
    """Build OpenAI-style message content for text plus optional images."""
    if not images:
        return text

    content = [{"type": "text", "text": text}]
    content.extend(
        {"type": "image_url", "image_url": {"url": url}} for url in images
    )
    return content


def _complete(
    api_client: OpenAI,
    model: str,
    messages: list[dict],
    *,
    temperature: float = 0.6,
    max_tokens: int = 4096,
    reasoning_effort: str | None = None,
) -> str:
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    completion = api_client.chat.completions.create(**kwargs)
    return (completion.choices[0].message.content or "").strip()


def _answer_many_images(
    prompt: str, history_messages: list[dict], images: list[str]
) -> str:
    """Analyse each image separately in parallel, then synthesise an answer."""
    total = len(images)

    def analyse(job: tuple[int, str]) -> str:
        index, url = job
        image_prompt = (
            f"Image {index + 1} of {total}.\n\n"
            f"User request:\n{prompt}\n\n"
            "Analyse this image with that request in mind."
        )
        analysis = _complete(
            groq_client,
            VISION_MODEL,
            [
                {"role": "system", "content": VISION_BATCH_PROMPT},
                {"role": "user", "content": _image_content(image_prompt, [url])},
            ],
            temperature=0.2,
            max_tokens=700,
            reasoning_effort="none",
        )
        return f"### Image {index + 1}\n{analysis or '(No analysis returned.)'}"

    with ThreadPoolExecutor(max_workers=min(4, total)) as pool:
        analyses = list(pool.map(analyse, enumerate(images)))

    synthesis = (
        f"The user attached {total} images. Each one was analysed separately:\n\n"
        + "\n\n".join(analyses)
        + "\n\nNow answer the user's original request. Refer to images by their "
        "numbers. If something could not be read, say so instead of guessing. "
        "Do not mention the analysis process."
    )

    return _complete(
        groq_client,
        VISION_MODEL,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history_messages,
            {
                "role": "user",
                "content": f"Original request:\n{prompt}\n\n{synthesis}",
            },
        ],
        temperature=0.4,
        max_tokens=2048,
        reasoning_effort="none",
    )


def _ask_gemini(
    model: str,
    prompt: str,
    history_messages: list[dict],
    images: list[str],
    *,
    has_images: bool,
) -> str:
    """One Gemini call: all images go in a single native multimodal request."""
    return _complete(
        gemini_client,
        model,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            *history_messages,
            {"role": "user", "content": _image_content(prompt, images)},
        ],
        temperature=0.6,
        max_tokens=2048 if has_images else 4096,
        # Flash-Lite is fastest with its default thinking level.
        reasoning_effort=None if "lite" in model else "low",
    )


@app.route("/")
def index():
    """Serve the chat UI so everything runs from one origin."""
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/health", methods=["GET"])
def health():
    if gemini_client and groq_client:
        provider = "gemini+groq"
    elif gemini_client:
        provider = "gemini"
    elif groq_client:
        provider = "groq"
    else:
        provider = "none"

    return jsonify(
        {
            "status": "ok",
            "provider": provider,
            "text_model": GEMINI_TEXT_MODEL if gemini_client else TEXT_MODEL,
            "vision_model": GEMINI_VISION_MODEL if gemini_client else VISION_MODEL,
            "access_code_required": bool(ACCESS_CODE),
        }
    )


@app.route("/api/session", methods=["GET"])
def session_status():
    guest_until = session.get("guest_until")
    guest_active = isinstance(guest_until, (int, float)) and time.time() < float(
        guest_until
    )
    authorized = _is_authorized()
    return jsonify(
        {
            "required": bool(ACCESS_CODE),
            "authenticated": authorized,
            "guest": guest_active,
            "guest_expires_at": int(guest_until) if guest_active else None,
            "role": (
                session.get("role") or ("owner" if not ACCESS_CODE else None)
                if authorized
                else None
            ),
        }
    )


@app.route("/api/login", methods=["POST"])
def login():
    if not ACCESS_CODE:
        return jsonify({"ok": True, "required": False})

    allowed, retry_after, limit_kind = _rate_limit(
        "login",
        per_minute=LOGIN_PER_MINUTE,
        per_day=LOGIN_PER_DAY,
        use_global=False,
    )
    if not allowed:
        return (
            jsonify(
                {
                    "error": "Too many attempts. Wait a moment and try again.",
                    "retry_after": retry_after,
                    "limit": limit_kind,
                }
            ),
            429,
        )

    payload = request.get_json(silent=True)
    code = payload.get("code") if isinstance(payload, dict) else None
    if not isinstance(code, str) or not code.strip():
        return _error("Enter the access code.", 400)

    code = code.strip()

    if ACCESS_CODE and hmac.compare_digest(code, ACCESS_CODE):
        session.permanent = True
        session["authorized"] = True
        session["role"] = "owner"
        session.pop("guest_until", None)
        return jsonify({"ok": True, "required": True, "role": "owner"})

    if GUEST_CODE and hmac.compare_digest(code, GUEST_CODE):
        if GUEST_UNTIL and time.time() < GUEST_UNTIL:
            session.permanent = True
            session["authorized"] = True
            session["role"] = "guest"
            session["guest_until"] = GUEST_UNTIL
            return jsonify(
                {
                    "ok": True,
                    "required": True,
                    "role": "guest",
                    "expires_at": int(GUEST_UNTIL),
                }
            )
        return _error("That guest code has expired.", 401)

    guest_expires = check_guest_code(code)
    if guest_expires == -1:
        return _error("That guest code has expired.", 401)
    if guest_expires:
        session.permanent = True
        session["authorized"] = True
        session["role"] = "guest"
        session["guest_until"] = guest_expires
        return jsonify(
            {
                "ok": True,
                "required": True,
                "role": "guest",
                "expires_at": int(guest_expires),
            }
        )

    try:
        entry = find_key(code)
    except Exception:
        entry = None
    if entry:
        state = key_state(entry)
        if state == "stopped":
            return _error("That code has been stopped.", 401)
        if state == "expired":
            return _error("That code has expired.", 401)
        session.permanent = True
        session["authorized"] = True
        session["role"] = "guest"
        session["key_id"] = entry["id"]
        expires = entry.get("expires_at")
        if expires:
            session["guest_until"] = float(expires)
        return jsonify(
            {
                "ok": True,
                "required": True,
                "role": "guest",
                "expires_at": expires,
            }
        )

    return _error("Wrong access code.", 401)


@app.route("/api/guest-code", methods=["POST"])
def create_guest_code():
    """Owner-only: mint a signed guest code that expires after N minutes."""
    if not _is_authorized():
        return (
            jsonify({"error": "Access code required.", "login_required": True}),
            401,
        )

    role = session.get("role") or ("owner" if not ACCESS_CODE else None)
    if role != "owner":
        return _error("Only the owner code can create guest codes.", 403)

    payload = request.get_json(silent=True)
    try:
        minutes = int(payload.get("minutes", 60)) if isinstance(payload, dict) else 60
    except (TypeError, ValueError):
        minutes = 60
    minutes = max(1, min(minutes, 24 * 60))

    code, expires = make_guest_code(minutes)
    return jsonify({"code": code, "expires_at": expires, "minutes": minutes})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/admin")
def admin_page():
    """Owner-only admin panel."""
    if not _is_authorized():
        return redirect("/?next=/admin")
    if not _is_owner():
        return redirect("/")
    return send_from_directory(BASE_DIR, "admin.html")


@app.route("/api/admin/keys", methods=["GET"])
def admin_list_keys():
    if not _is_owner():
        return _error("Owner code required.", 403)

    try:
        keys, _ = load_keys(force=True)
    except Exception as exc:
        return _error(f"Could not load keys: {exc}", 502)

    out = []
    for entry in keys:
        out.append(
            {
                "id": entry.get("id"),
                "label": entry.get("label", ""),
                "created_at": entry.get("created_at"),
                "expires_at": entry.get("expires_at"),
                "state": key_state(entry),
            }
        )
    out.sort(key=lambda e: e.get("created_at") or 0, reverse=True)
    return jsonify({"keys": out, "storage": "github" if KEYS_TOKEN else "local"})


@app.route("/api/admin/keys", methods=["POST"])
def admin_create_key():
    if not _is_owner():
        return _error("Owner code required.", 403)

    payload = request.get_json(silent=True)
    label = ""
    minutes = 0
    if isinstance(payload, dict):
        label = str(payload.get("label", "")).strip()[:60]
        try:
            minutes = int(payload.get("minutes", 0) or 0)
        except (TypeError, ValueError):
            minutes = 0
    minutes = max(0, min(minutes, 60 * 24 * 365))

    code = new_key_code()
    now = time.time()
    entry = {
        "id": uuid.uuid4().hex[:12],
        "label": label or "Guest key",
        "hash": hash_key(code),
        "created_at": now,
        "expires_at": now + minutes * 60 if minutes else None,
        "active": True,
    }

    try:
        keys, sha = load_keys(force=True)
        keys.append(entry)
        save_keys(keys, sha, f"Add access key: {entry['label']}")
    except Exception as exc:
        return _error(f"Could not save the key: {exc}", 502)

    return jsonify(
        {
            "key": {
                "id": entry["id"],
                "label": entry["label"],
                "created_at": entry["created_at"],
                "expires_at": entry["expires_at"],
                "state": "active",
            },
            "code": code,
        }
    )


@app.route("/api/admin/keys/<key_id>/revoke", methods=["POST"])
def admin_revoke_key(key_id: str):
    if not _is_owner():
        return _error("Owner code required.", 403)

    try:
        keys, sha = load_keys(force=True)
        entry = next((k for k in keys if k.get("id") == key_id), None)
        if not entry:
            return _error("That key does not exist.", 404)
        entry["active"] = False
        entry["stopped_at"] = time.time()
        save_keys(keys, sha, f"Stop access key: {entry.get('label', key_id)}")
    except Exception as exc:
        return _error(f"Could not stop the key: {exc}", 502)

    return jsonify({"ok": True, "id": key_id})


@app.route("/api/admin/keys/<key_id>", methods=["DELETE"])
def admin_delete_key(key_id: str):
    if not _is_owner():
        return _error("Owner code required.", 403)

    try:
        keys, sha = load_keys(force=True)
        remaining = [k for k in keys if k.get("id") != key_id]
        if len(remaining) == len(keys):
            return _error("That key does not exist.", 404)
        save_keys(keys=remaining, sha=sha, message="Delete access key")
    except Exception as exc:
        return _error(f"Could not delete the key: {exc}", 502)

    return jsonify({"ok": True, "id": key_id})


@app.route("/api/chat", methods=["POST"])
def chat():
    if not _is_authorized():
        return (
            jsonify({"error": "Access code required.", "login_required": True}),
            401,
        )

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error("Request body must be a JSON object.", 400)

    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        return _error("Field 'prompt' must be a string.", 400)
    prompt = prompt.strip()

    try:
        images = _parse_images(payload.get("images"))
        history = _clean_history(payload.get("history"))
    except ValueError as exc:
        return _error(str(exc), 400)

    if not prompt and not images:
        return _error("Send a prompt and/or at least one image.", 400)
    if not prompt:
        prompt = (
            "Describe the image or images in detail. "
            "Transcribe any visible text exactly."
        )
    if len(prompt) > MAX_MESSAGE_CHARS:
        return _error(
            f"Prompt is too long (maximum {MAX_MESSAGE_CHARS} characters).", 400
        )

    history_images = sum(len(turn["images"]) for turn in history)
    total_images = history_images + len(images)
    if total_images > MAX_IMAGES:
        return _error(
            f"At most {MAX_IMAGES} images can be sent in one request, including "
            "images carried over from earlier turns.",
            400,
        )

    if not gemini_client and not groq_client:
        return _error(
            "No AI provider is configured. Set GEMINI_API_KEY (or GROQ_API_KEY) "
            "in the environment or .env file.",
            503,
        )

    # Text costs 1 unit; each image costs 2 more because vision is expensive.
    cost = 1 + 2 * total_images
    allowed, retry_after, limit_kind = _rate_limit("chat", cost=cost)
    if not allowed:
        if limit_kind == "day":
            message = "PIOU AI has reached today's usage limit. Try again tomorrow."
        else:
            message = (
                f"Too many requests. Wait about {retry_after} seconds and try again."
            )
        return (
            jsonify(
                {
                    "error": message,
                    "retry_after": retry_after,
                    "limit": limit_kind,
                }
            ),
            429,
        )

    history_messages = []
    for turn in history:
        if turn["images"]:
            content = [{"type": "text", "text": turn["content"]}]
            content.extend(
                {"type": "image_url", "image_url": {"url": url}}
                for url in turn["images"]
            )
        else:
            content = turn["content"]
        history_messages.append({"role": turn["role"], "content": content})

    history_text_messages = [
        {"role": turn["role"], "content": turn["content"]} for turn in history
    ]

    answer = ""
    provider = None
    gemini_error = None

    # Primary: Gemini. It handles all images natively in one request.
    if gemini_client:
        has_images = total_images > 0
        if has_images:
            gemini_models = [GEMINI_VISION_MODEL, GEMINI_VISION_FALLBACK]
        else:
            gemini_models = [GEMINI_TEXT_MODEL, GEMINI_TEXT_FALLBACK]

        for model in gemini_models:
            if not model:
                continue
            try:
                candidate = _ask_gemini(
                    model,
                    prompt,
                    history_messages,
                    images,
                    has_images=has_images,
                )
            except Exception as exc:  # timeout, quota, auth, bad request ...
                gemini_error = exc
                app.logger.warning("Gemini model %s failed: %s", model, exc)
                continue
            if candidate:
                answer = candidate
                provider = f"gemini:{model}"
                break

    # Fallback: Groq, unchanged behaviour (batched when more than 3 images).
    if not answer and groq_client:
        try:
            if total_images > VISION_BATCH_SIZE:
                all_images = [
                    url for turn in history for url in turn["images"]
                ] + images
                answer = _answer_many_images(
                    prompt, history_text_messages, all_images
                )
                groq_model = "batched-vision"
            else:
                groq_model = VISION_MODEL if total_images else TEXT_MODEL
                answer = _complete(
                    groq_client,
                    groq_model,
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        *history_messages,
                        {"role": "user", "content": _image_content(prompt, images)},
                    ],
                    reasoning_effort=(
                        "none" if groq_model == VISION_MODEL else None
                    ),
                )
            if answer:
                provider = f"groq:{groq_model}"
        except RateLimitError as exc:
            retry_after = 15
            if exc.response is not None:
                try:
                    retry_after = max(
                        1, int(float(exc.response.headers.get("retry-after", 15)))
                    )
                except (TypeError, ValueError):
                    pass
            return (
                jsonify(
                    {
                        "error": (
                            "The AI providers are busy right now. Wait about "
                            f"{retry_after} seconds and try again."
                        ),
                        "retry_after": retry_after,
                    }
                ),
                429,
            )
        except Exception as exc:  # network, auth, bad request ...
            app.logger.exception("Groq request failed")
            return _error(f"AI request failed: {exc}", 502)

    if not answer:
        if gemini_error is not None:
            app.logger.error("All providers failed; last Gemini error: %s", gemini_error)
        return _error("The model returned an empty response.", 502)

    # Guaranteed catchphrase, even if the model forgets it.
    if not answer.casefold().startswith(CATCHPHRASE.casefold()):
        answer = f"{CATCHPHRASE}\n\n{answer}"

    return jsonify({"response": answer, "provider": provider})


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("PORT", "5000"))
    except ValueError:
        port = 5000

    if not ACCESS_CODE:
        print(
            "WARNING: PIOU_ACCESS_CODE is not set - anyone who can reach this "
            "server can chat."
        )

    if os.environ.get("PIOU_PRODUCTION") == "1":
        from waitress import serve

        print(f"PIOU AI running on http://{host}:{port} (waitress)")
        serve(app, host=host, port=port, threads=8)
    else:
        print(f"PIOU AI running on http://{host}:{port}")
        app.run(host=host, port=port, debug=False)
