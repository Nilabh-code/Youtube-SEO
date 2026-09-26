import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests

try:
    from youtube_transcript_api import (
        AgeRestricted,
        IpBlocked,
        NoTranscriptFound,
        PoTokenRequired,
        RequestBlocked,
        TranscriptsDisabled,
        VideoUnavailable,
        VideoUnplayable,
        YouTubeTranscriptApi,
    )
except ImportError:  # pragma: no cover
    from youtube_transcript_api._errors import (
        AgeRestricted,
        IpBlocked,
        NoTranscriptFound,
        PoTokenRequired,
        RequestBlocked,
        TranscriptsDisabled,
        VideoUnavailable,
        VideoUnplayable,
    )
    from youtube_transcript_api import YouTubeTranscriptApi

try:
    from youtube_transcript_api.proxies import GenericProxyConfig
except ImportError:  # pragma: no cover
    GenericProxyConfig = None

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CAPTION_TRACKS_RE = re.compile(r'"captionTracks"\s*:\s*(\[[^\]]*\])')
PLAYER_RESPONSE_RE = re.compile(r"ytInitialPlayerResponse\s*=\s*(\{.*?\})\s*;", re.DOTALL)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

CONSENT_COOKIES = {"CONSENT": "YES+cb", "SOCS": "CAI"}
PROMPT_CHAR_LIMIT = 12000
WATCH_RETRIES = 2

SUPADATA_URL = "https://api.supadata.ai/v1/transcript"
SUPADATA_JOB_POLLS = 6
SUPADATA_POLL_WAIT = 5


class TranscriptError(Exception):
    """Raised when a transcript cannot be retrieved.

    `kind` is one of:
      - "blocked"      YouTube blocked this server's IP (retry later / paste manually)
      - "no_captions"  the video simply has no captions
      - "unavailable"  private / deleted / region locked / age restricted / bad id
    """

    def __init__(self, message: str, kind: str = "fetch_failed"):
        super().__init__(message)
        self.kind = kind


@dataclass
class Transcript:
    text: str
    language: str
    source: str

    @property
    def prompt_text(self) -> str:
        if len(self.text) <= PROMPT_CHAR_LIMIT:
            return self.text
        return self.text[:PROMPT_CHAR_LIMIT].rsplit(" ", 1)[0] + " ..."


def extract_video_id(raw: str) -> str | None:
    """Pull the 11 character video id out of any common YouTube URL (or raw id)."""
    value = (raw or "").strip()
    if not value:
        return None
    if VIDEO_ID_RE.match(value):
        return value

    if "://" not in value:
        value = "https://" + value

    try:
        parsed = urlparse(value)
    except ValueError:
        return None

    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]

    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/")[0]
        return candidate if VIDEO_ID_RE.match(candidate) else None

    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        query = parse_qs(parsed.query)
        video_param = query.get("v", [""])[0]
        if VIDEO_ID_RE.match(video_param):
            return video_param
        match = re.match(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", parsed.path)
        if match:
            return match.group(1)
    return None


def fetch_transcript(video_id: str) -> Transcript:
    """Return the transcript, chaining free sources until one works.

    Order: direct library -> Supadata API (only if a key is configured) ->
    watch page scrape. Retries only happen for blocking style failures;
    missing captions will not appear by retrying.
    """
    attempts: list[TranscriptError] = []

    for step in (_from_library, _from_supadata, _from_watch_page):
        try:
            transcript = step(video_id)
        except TranscriptError as exc:
            attempts.append(exc)
            if exc.kind in ("no_captions", "unavailable"):
                break
            continue
        if transcript and transcript.text.strip():
            return transcript
        attempts.append(
            TranscriptError("The transcript for this video came back empty.", "no_captions")
        )
        break

    raise _pick_error(attempts)


def _pick_error(attempts: list[TranscriptError]) -> TranscriptError:
    if not attempts:
        return TranscriptError("No transcript is available for this video.", "no_captions")
    # Prefer "unavailable" and "no_captions" verdicts over generic blocking noise,
    # because they describe the video itself rather than our server.
    priority = {"unavailable": 0, "no_captions": 1, "blocked": 2, "fetch_failed": 3}
    return sorted(attempts, key=lambda err: priority.get(err.kind, 3))[0]


def _proxy_config():
    raw = os.getenv("TRANSCRIPT_PROXY", "").strip()
    if not raw or GenericProxyConfig is None:
        return None
    try:
        return GenericProxyConfig(http_url=raw, https_url=raw)
    except Exception:
        return None


def _from_library(video_id: str) -> Transcript:
    try:
        api = YouTubeTranscriptApi(proxy_config=_proxy_config())
    except TypeError:  # very old library without proxy support
        api = YouTubeTranscriptApi()

    try:
        listed = api.list(video_id)
    except (RequestBlocked, IpBlocked, PoTokenRequired) as exc:
        raise TranscriptError(
            "YouTube is blocking transcript requests from this server's IP address "
            "(cloud hosting IPs are routinely blocked). Wait a few minutes and retry, "
            "or paste the transcript manually below.",
            "blocked",
        ) from exc
    except (VideoUnavailable, VideoUnplayable) as exc:
        raise TranscriptError(
            "That video is unavailable, private, deleted, or region locked.",
            "unavailable",
        ) from exc
    except AgeRestricted as exc:
        raise TranscriptError(
            "That video is age restricted, so its transcript cannot be fetched "
            "automatically. Paste the transcript manually below.",
            "unavailable",
        ) from exc
    except TranscriptsDisabled as exc:
        raise TranscriptError(
            "Captions are turned off for this video, so there is no transcript to read.",
            "no_captions",
        ) from exc
    except NoTranscriptFound as exc:
        raise TranscriptError(
            "This video has no captions in any language.",
            "no_captions",
        ) from exc
    except Exception as exc:
        if "429" in str(exc) or "Too Many Requests" in str(exc):
            raise TranscriptError(
                "YouTube is rate limiting this server right now. Wait a minute and retry, "
                "or paste the transcript manually below.",
                "blocked",
            ) from exc
        raise TranscriptError(
            f"Could not read the transcript ({type(exc).__name__}).",
            "fetch_failed",
        ) from exc

    try:
        preferred = listed.find_transcript(["en"])
        rows = preferred.fetch()
        return Transcript(_flatten(rows), preferred.language_code, "youtube")
    except NoTranscriptFound:
        pass
    except Exception:
        pass

    try:
        any_transcript = next(iter(listed))
    except StopIteration:
        raise TranscriptError(
            "This video has no captions in any language.", "no_captions"
        ) from None
    try:
        rows = any_transcript.fetch()
    except Exception as exc:
        raise TranscriptError(
            f"Could not download the captions ({type(exc).__name__}).",
            "fetch_failed",
        ) from exc
    return Transcript(_flatten(rows), any_transcript.language_code, "youtube")


def _from_supadata(video_id: str) -> Transcript:
    """Supadata transcript API (free plan). Only runs when SUPADATA_API_KEY is set."""
    api_key = os.getenv("SUPADATA_API_KEY", "").strip()
    if not api_key:
        raise TranscriptError("Supadata API key not configured.", "fetch_failed")

    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    params = {
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "lang": "en",
        "text": "true",
        "mode": "native",
    }
    try:
        response = requests.get(SUPADATA_URL, headers=headers, params=params, timeout=30)
    except requests.RequestException as exc:
        raise TranscriptError(
            "Could not reach the Supadata API.", "fetch_failed"
        ) from exc

    if response.status_code in (401, 403):
        raise TranscriptError(
            "Supadata rejected the API key. Check SUPADATA_API_KEY.", "fetch_failed"
        )
    if response.status_code in (402, 429):
        raise TranscriptError(
            "Supadata quota is exhausted right now. Try again later.", "fetch_failed"
        )
    if response.status_code == 202:
        return _poll_supadata_job(response, headers)
    if response.status_code in (206, 404):
        raise TranscriptError(
            "This video has no captions available.", "no_captions"
        )
    if response.status_code >= 400:
        raise TranscriptError(
            f"Supadata returned an error ({response.status_code}).", "fetch_failed"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise TranscriptError("Supadata returned an unreadable response.", "fetch_failed") from exc
    return _supadata_transcript(payload)


def _poll_supadata_job(response: requests.Response, headers: dict) -> Transcript:
    try:
        job_id = response.json().get("jobId")
    except ValueError:
        job_id = None
    if not job_id:
        raise TranscriptError("Supadata did not return a transcript.", "fetch_failed")

    last_error: TranscriptError | None = None
    for _ in range(SUPADATA_JOB_POLLS):
        time.sleep(SUPADATA_POLL_WAIT)
        try:
            poll = requests.get(
                f"{SUPADATA_URL}/{job_id}", headers=headers, timeout=30
            )
        except requests.RequestException as exc:
            last_error = TranscriptError(
                "Could not reach the Supadata API.", "fetch_failed"
            )
            continue
        if poll.status_code != 200:
            last_error = TranscriptError(
                "Supadata is still working on that transcript. Try again shortly.",
                "fetch_failed",
            )
            continue
        try:
            payload = poll.json()
        except ValueError:
            last_error = TranscriptError(
                "Supadata returned an unreadable response.", "fetch_failed"
            )
            continue
        if payload.get("status") == "completed":
            return _supadata_transcript(payload)
        if payload.get("status") == "failed":
            raise TranscriptError(
                "Supadata could not read that video.", "fetch_failed"
            )
        last_error = TranscriptError(
            "Supadata is still working on that transcript. Try again shortly.",
            "fetch_failed",
        )
    raise last_error or TranscriptError(
        "Supadata took too long. Try again shortly.", "fetch_failed"
    )


def _supadata_transcript(payload: dict) -> Transcript:
    content = payload.get("content")
    if isinstance(content, list):
        text = " ".join(
            str(chunk.get("text", "")).strip()
            for chunk in content
            if isinstance(chunk, dict) and chunk.get("text")
        )
    else:
        text = str(content or "").strip()
    if not text:
        raise TranscriptError(
            "The transcript for this video came back empty.", "no_captions"
        )
    return Transcript(text, str(payload.get("lang") or "unknown"), "supadata")


def extract_track_urls(video_id: str) -> list[dict]:
    """Fetch the watch page and return caption track URLs without downloading them.

    Used by the browser fallback: the server (blocked IP) extracts the URLs,
    the visitor's browser (home IP) downloads the captions. Track URLs are not
    IP bound, so this works across machines.
    """
    last_error: TranscriptError | None = None
    for user_agent in (DESKTOP_UA, MOBILE_UA):
        try:
            tracks = _fetch_watch_tracks(video_id, user_agent)
        except TranscriptError as exc:
            last_error = exc
            continue
        if tracks:
            return [
                {
                    "languageCode": track.get("languageCode", "unknown"),
                    "label": _track_label(track),
                    "is_generated": track.get("kind") == "asr",
                    "url": track.get("baseUrl", "").replace("&fmt=srv3", ""),
                }
                for track in tracks
                if track.get("baseUrl")
            ]
        last_error = TranscriptError(
            "This video has no captions available.", "no_captions"
        )
    raise last_error or TranscriptError(
        "Could not read the video page from YouTube.", "blocked"
    )


def _track_label(track: dict) -> str:
    try:
        return track.get("name", {}).get("runs", [{}])[0].get("text", "")
    except (AttributeError, IndexError, KeyError):
        return ""


def _from_watch_page(video_id: str) -> Transcript:
    """Fallback: scrape captionTracks from the watch page (helps when the API is throttled)."""
    last_error: TranscriptError | None = None

    for attempt in range(WATCH_RETRIES):
        user_agent = DESKTOP_UA if attempt % 2 == 0 else MOBILE_UA
        try:
            tracks = _fetch_watch_tracks(video_id, user_agent)
        except TranscriptError as exc:
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
            continue
        if not tracks:
            last_error = TranscriptError(
                "This video has no captions available.", "no_captions"
            )
            break
        try:
            return _download_track(tracks, user_agent)
        except TranscriptError as exc:
            last_error = exc
            time.sleep(1.2 * (attempt + 1))
            continue

    raise last_error or TranscriptError(
        "Could not read the transcript from YouTube.", "blocked"
    )


def _fetch_watch_tracks(video_id: str, user_agent: str) -> list:
    headers = {"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"}
    try:
        response = requests.get(
            f"https://www.youtube.com/watch?v={video_id}&hl=en&gl=US",
            headers=headers,
            cookies=CONSENT_COOKIES,
            timeout=25,
        )
    except requests.RequestException as exc:
        raise TranscriptError(
            "Could not reach YouTube. Check your connection and retry.", "blocked"
        ) from exc

    if response.status_code == 429:
        raise TranscriptError(
            "YouTube is rate limiting this server right now. Wait a minute and retry, "
            "or paste the transcript manually below.",
            "blocked",
        )
    if response.status_code != 200:
        raise TranscriptError(
            "YouTube refused the request. Try again shortly.", "blocked"
        )

    lowered = response.text.lower()
    if "confirm you're not a bot" in lowered or "sign in to confirm" in lowered:
        raise TranscriptError(
            "YouTube showed a bot check to this server instead of the video "
            "(common on cloud hosting IPs). Wait a few minutes and retry, "
            "or paste the transcript manually below.",
            "blocked",
        )

    match = CAPTION_TRACKS_RE.search(response.text)
    if not match:
        if "ytinitialplayerresponse" not in lowered:
            raise TranscriptError(
                "YouTube did not return video data to this server "
                "(it may be blocking this IP). Wait a bit and retry, "
                "or paste the transcript manually below.",
                "blocked",
            )
        raise TranscriptError(
            "This video has no captions available.", "no_captions"
        )

    try:
        tracks = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise TranscriptError(
            "Could not read the caption data from YouTube.", "blocked"
        ) from exc
    if not tracks:
        raise TranscriptError("This video has no captions available.", "no_captions")
    return tracks


def _download_track(tracks: list, user_agent: str) -> Transcript:
    headers = {"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"}
    track = _pick_track(tracks)
    base_url = track.get("baseUrl")
    if not base_url:
        raise TranscriptError("This video has no captions available.", "no_captions")
    if "&exp=xpe" in base_url:
        raise TranscriptError(
            "YouTube is demanding extra verification from this server "
            "(common on cloud hosting IPs). Wait a bit and retry, "
            "or paste the transcript manually below.",
            "blocked",
        )

    # Fetch the track URL exactly as YouTube serves it (srv3 XML) and parse it.
    # Appending another fmt parameter makes YouTube return an empty response.
    track_url = base_url.replace("&fmt=srv3", "")
    try:
        caption_response = requests.get(
            track_url,
            headers=headers,
            cookies=CONSENT_COOKIES,
            timeout=25,
        )
        caption_response.raise_for_status()
        body = caption_response.text
    except requests.RequestException as exc:
        raise TranscriptError(
            "Could not download the captions from YouTube.", "blocked"
        ) from exc

    text = _flatten_xml(body)
    if not text.strip():
        raise TranscriptError(
            "The transcript for this video came back empty.", "no_captions"
        )
    return Transcript(text, track.get("languageCode", "unknown"), "watch page")


def _pick_track(tracks: list) -> dict:
    for track in tracks:
        if track.get("kind") == "asr":
            continue
        return track
    return tracks[0]


def _flatten(rows) -> str:
    pieces = []
    for row in rows:
        text = row.get("text", "") if isinstance(row, dict) else getattr(row, "text", "")
        if text:
            pieces.append(str(text).strip())
    return _join(pieces)


def _flatten_xml(body: str) -> str:
    import html as html_module
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return ""
    pieces = []
    for node in root.iter("text"):
        if node.text:
            pieces.append(html_module.unescape(node.text).strip())
    return _join(pieces)


def _join(pieces: list) -> str:
    text = ""
    for piece in pieces:
        if not piece:
            continue
        if text.endswith("\n") or not text:
            text += piece
        else:
            text += " " + piece
    return re.sub(r"[ \t]+", " ", text).strip()
