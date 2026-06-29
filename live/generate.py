#!/usr/bin/env python3
"""
generate.py

Fetches the DLHD / Dami-TV channel list from BuddyChewChew/buddylive
(live/dlhd_damitv.txt) and rewrites it into TiviMate-friendly M3U
playlists with VLC HTTP option headers (referrer, origin, user-agent)
injected before every #EXTINF entry.

By default this writes TWO files:
  1. <output>            - headers fixed only, no tvg-id (always written)
  2. <output>_with_ids.m3u - same, plus tvg-id="..." where a CONFIDENT
     match against BuddyChewChew/epg-viewer's data/epg_data.json was found

Only exact normalized-name matches (and anything in MANUAL_OVERRIDES) are
written as a tvg-id. Anything uncertain is left blank rather than guessed -
a wrong tvg-id is worse than no tvg-id for a 24/7 linear EPG feed. A CSV
match log lists every channel plus, for unmatched ones, a best-effort fuzzy
"suggested_id" for you to review and promote into MANUAL_OVERRIDES if it's
correct.

Source format (one block per channel):
    #EXTINF:-1 group-title="DLHD 24/7",<Channel Name>
    https://dami-tv.pro/papi/tv/dlhd/<id>/playlist.m3u8

Output format (with tvg-id):
    #EXTVLCOPT:http-referrer=https://dami-tv.pro/
    #EXTVLCOPT:http-origin=https://dami-tv.pro
    #EXTVLCOPT:http-user-agent=Mozilla/5.0 ...
    #EXTINF:-1 tvg-id="Some.Channel.us2" group-title="DLHD 24/7",<Channel Name>
    https://dami-tv.pro/papi/tv/dlhd/<id>/playlist.m3u8

Usage:
    python3 generate.py
    python3 generate.py --output dlhd_damitv.m3u
    python3 generate.py --source <url or local path>
    python3 generate.py --no-epg                     # skip EPG matching entirely
    python3 generate.py --output-with-ids custom.m3u
    python3 generate.py --score 0.85                 # tune the LOG-ONLY suggestion threshold
    python3 generate.py --log match_log.csv          # custom log path

To fix a wrong/missing match, add an entry to MANUAL_OVERRIDES below using
the exact channel name from dlhd_damitv.txt and the correct epgshare01
tvg-id (check the match log's "suggested_id" column first).
"""

import argparse
import csv
import json
import re
import sys
import unicodedata
import urllib.request
from difflib import SequenceMatcher

SOURCE_URL = (
    "https://raw.githubusercontent.com/BuddyChewChew/buddylive/"
    "refs/heads/main/live/dlhd_damitv.txt"
)

EPG_DATA_URL = (
    "https://raw.githubusercontent.com/BuddyChewChew/epg-viewer/"
    "refs/heads/main/data/epg_data.json"
)

REFERRER = "https://dami-tv.pro/"
ORIGIN = "https://dami-tv.pro"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

VLC_HEADER_BLOCK = (
    f"#EXTVLCOPT:http-referrer={REFERRER}\n"
    f"#EXTVLCOPT:http-origin={ORIGIN}\n"
    f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n"
)

# Matches an #EXTINF line followed by its stream URL on the next line.
ENTRY_RE = re.compile(
    r'^#EXTINF:-1\s+group-title="([^"]*)"\s*,\s*(.*?)\s*\n(\S+)\s*$',
    re.MULTILINE,
)

# Default fuzzy-match acceptance threshold (0.0-1.0). Same spirit as the
# --score option in epg_match.py for the locals.m3u8 project.
DEFAULT_SCORE = 0.80

# Country/region hints that may appear at the end of a dami-tv channel name,
# mapped to the epgshare01 ID suffix(es) that represent that region. Order
# matters only for readability; lookup is by exact key match against the
# last word(s) stripped from the channel name.
NAME_COUNTRY_HINTS = {
    "usa": ["us2", "us", "us_locals1", "plex", "distro"],
    "us": ["us2", "us", "us_locals1", "plex", "distro"],
    "uk": ["uk"],
    "ca": ["ca2", "ca"],
    "canada": ["ca2", "ca"],
    "au": ["au"],
    "australia": ["au"],
    "nz": ["nz"],
    "ireland": ["ie"],
    "de": ["de"],
    "germany": ["de"],
    "fr": ["fr"],
    "france": ["fr"],
    "es": ["es"],
    "spain": ["es"],
    "it": ["it"],
    "italy": ["it"],
    "pt": ["pt"],
    "portugal": ["pt"],
    "nl": ["nl"],
    "poland": ["pl"],
    "pl": ["pl"],
    "greece": ["gr"],
    "bulgaria": ["bg"],
    "serbia": ["rs"],
    "croatia": ["hr"],
    "czech": ["cz"],
    "cz": ["cz"],
    "slovakia": ["sk"],
    "sk": ["sk"],
    "hungary": ["hu"],
    "romania": ["ro"],
    "ro": ["ro"],
    "turkey": ["tr"],
    "russia": ["ru"],
    "israel": ["il"],
    "uae": ["ae"],
    "qatar": ["ae", "bein"],
    "mena": ["ae", "bein"],
    "mx": ["mx"],
    "brasil": ["br"],
    "brazil": ["br"],
    "argentina": ["ar"],
    "bih": ["ba"],
    "denmark": ["dk"],
    "sweden": ["se"],
    "sw": ["se"],
    "india": ["in", "in2"],
    "malaysia": ["my"],
    "indonesia": ["id"],
    "philippines": ["ph"],
    "south africa": ["za"],
    "cyprus": ["cy"],
}

# Noise words/phrases stripped from both sides before comparison. Helps
# collapse "ESPN USA" and "ESPN HD US2" down to a comparable core token.
NOISE_WORDS = {
    "hd", "sd", "fhd", "uhd", "4k", "tv", "channel", "network", "the",
    "feed", "stream", "live", "east", "west", "national", "plus",
    "alternate", "backup", "dummy",
}

# Hand-curated fixes for channels the automatic matcher gets wrong or can't
# find. Key = exact dami-tv channel name (as it appears in dlhd_damitv.txt),
# value = the correct epgshare01 tvg-id. These always take priority over
# automatic matching - add to this as you spot issues in the match log.
# Set a value to "" to force a channel to stay unmatched (no tvg-id at all).
MANUAL_OVERRIDES = {
    "A&E USA": "A.and.E.HD.East.us2",
    "Antenna TV USA": "Antenna.TV.us2",
}


def fetch_source(source: str) -> str:
    """Read the source list from a URL or a local file path."""
    if source.startswith("http://") or source.startswith("https://"):
        req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    with open(source, "r", encoding="utf-8") as f:
        return f.read()


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# Build a flat set of every hint word for stripping during normalization.
_ALL_HINT_WORDS = set()
for _hint in NAME_COUNTRY_HINTS:
    _ALL_HINT_WORDS.update(_hint.split())


def normalize(text: str, strip_hints: bool = True) -> str:
    """Lowercase, strip accents/punctuation, collapse whitespace, drop noise
    words and (optionally) country/region hint words like 'usa' or 'uk'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[()&/_.,'\-]", " ", text)
    text = re.sub(r"[^a-z0-9 ]", "", text)
    drop = NOISE_WORDS | (_ALL_HINT_WORDS if strip_hints else set())
    words = [w for w in text.split() if w not in drop]
    return " ".join(words).strip()


def id_suffix(tvg_id: str) -> str:
    parts = tvg_id.rsplit(".", 1)
    return parts[1] if len(parts) == 2 else ""


def detect_country_hint(channel_name: str):
    """Look for a trailing country/region word in the channel name."""
    norm = channel_name.lower()
    # Check multi-word hints first (e.g. "south africa"), then single words.
    for hint in sorted(NAME_COUNTRY_HINTS, key=len, reverse=True):
        if re.search(r"\b" + re.escape(hint) + r"\b", norm):
            return NAME_COUNTRY_HINTS[hint]
    return None


def build_epg_index(epg_data: dict):
    """Build normalized-name -> [(tvg_id, suffix)] index from epg_data.json."""
    channels = epg_data.get("source", {}).get("channels", {})
    index = {}
    for tvg_id, info in channels.items():
        name = info.get("name") or ""
        norm = normalize(name)
        if not norm:
            continue
        index.setdefault(norm, []).append((tvg_id, id_suffix(tvg_id)))
    return index, channels


def best_match(channel_name: str, epg_index: dict, allow_cross_region: bool = False):
    """
    Find a CONFIDENT epgshare tvg-id for a dami-tv channel name - exact
    normalized-name match only. Returns (tvg_id, epg_name) or (None, None).

    Fuzzy/approximate matches are intentionally not returned here: a wrong
    tvg-id is worse than no tvg-id for a 24/7 linear EPG feed, so anything
    short of an exact match is left for manual review (see suggest_match
    and MANUAL_OVERRIDES) rather than auto-applied.
    """
    norm_name = normalize(channel_name)
    preferred_suffixes = detect_country_hint(channel_name)
    region_locked = bool(preferred_suffixes) and not allow_cross_region

    if norm_name not in epg_index:
        return None, None

    candidates = epg_index[norm_name]
    if preferred_suffixes:
        for suf in preferred_suffixes:
            for tvg_id, cand_suf in candidates:
                if cand_suf == suf:
                    return tvg_id, tvg_id
        if region_locked:
            # Region hint present but no candidate matches it - do not
            # silently pick a different country's channel.
            return None, None
        tvg_id = candidates[0][0]
        return tvg_id, tvg_id

    tvg_id = candidates[0][0]
    return tvg_id, tvg_id


def suggest_match(channel_name: str, epg_index: dict, score_threshold: float,
                   allow_cross_region: bool = False):
    """
    Best-effort fuzzy suggestion for the match log only - never auto-applied
    as a tvg-id. Returns (suggested_tvg_id_or_None, score).
    """
    norm_name = normalize(channel_name)
    preferred_suffixes = detect_country_hint(channel_name)
    region_locked = bool(preferred_suffixes) and not allow_cross_region

    # Short names (e.g. 2-3 letter acronyms) are unreliable for ratio-based
    # fuzzy matching - "fox" vs "fx" scores 0.8 despite being different
    # channels - so require a stricter score for short strings.
    effective_threshold = max(score_threshold, 0.92)

    best_id = None
    best_score = 0.0
    best_in_region = False

    for cand_norm, candidates in epg_index.items():
        if abs(len(cand_norm) - len(norm_name)) > max(len(norm_name), len(cand_norm)) * 0.6:
            continue  # quick length-disparity skip before the expensive ratio() call
        score = SequenceMatcher(None, norm_name, cand_norm).ratio()
        threshold_for_pair = effective_threshold if min(len(norm_name), len(cand_norm)) <= 4 else score_threshold
        if score < threshold_for_pair:
            continue
        for tvg_id, cand_suf in candidates:
            in_region = bool(preferred_suffixes) and cand_suf in preferred_suffixes
            if region_locked and not in_region:
                continue  # skip out-of-region candidates entirely
            if in_region and not best_in_region:
                best_id, best_score, best_in_region = tvg_id, score, True
            elif in_region == best_in_region and score > best_score:
                best_id, best_score, best_in_region = tvg_id, score, in_region

    return best_id, best_score


def parse_entries(raw_text: str):
    """Parse raw EXTINF/URL pairs into a list of dicts:
    {group_title, channel_name, stream_url}."""
    entries = []
    for match in ENTRY_RE.finditer(raw_text):
        entries.append({
            "group_title": match.group(1).strip(),
            "channel_name": match.group(2).strip(),
            "stream_url": match.group(3).strip(),
        })
    return entries


def match_entries(entries: list, epg_index, score_threshold=DEFAULT_SCORE,
                   allow_cross_region=False):
    """
    Determine a tvg-id (if any) for each entry. Only exact matches and
    MANUAL_OVERRIDES are returned as a usable tvg_id; everything else is
    left blank so a possibly-wrong ID is never silently injected. A fuzzy
    "suggested_id"/"suggested_score" is still recorded for the match log so
    close-but-unconfirmed matches are visible for you to review and promote
    to MANUAL_OVERRIDES if correct.

    Returns a list of dicts (one per entry):
    {channel_name, tvg_id, match_type, suggested_id, suggested_score}
    """
    results = []
    for entry in entries:
        name = entry["channel_name"]

        if name in MANUAL_OVERRIDES:
            override_id = MANUAL_OVERRIDES[name]
            results.append({
                "channel_name": name,
                "tvg_id": override_id,  # "" is valid - forces no tvg-id
                "match_type": "manual_override" if override_id else "manual_blank",
                "suggested_id": "",
                "suggested_score": "",
            })
            continue

        tvg_id, _ = best_match(name, epg_index, allow_cross_region)
        suggested_id, suggested_score = (None, 0.0)
        if not tvg_id:
            # Only bother computing a fuzzy suggestion when there is no
            # confident exact match - it's purely informational here.
            suggested_id, suggested_score = suggest_match(
                name, epg_index, score_threshold, allow_cross_region
            )

        results.append({
            "channel_name": name,
            "tvg_id": tvg_id or "",
            "match_type": "exact" if tvg_id else "",
            "suggested_id": suggested_id or "",
            "suggested_score": f"{suggested_score:.3f}" if suggested_id else "",
        })
    return results


def render_playlist(entries: list, tvg_ids: dict = None) -> str:
    """
    Render entries (from parse_entries) into M3U text with VLC headers.
    If tvg_ids is given (channel_name -> tvg_id), inject tvg-id="..." for
    any channel with a non-empty id; otherwise omit tvg-id entirely.
    """
    lines = []
    for entry in entries:
        tvg_attr = ""
        if tvg_ids is not None:
            tvg_id = tvg_ids.get(entry["channel_name"], "")
            if tvg_id:
                tvg_attr = f'tvg-id="{tvg_id}" '

        extinf_line = (
            f'#EXTINF:-1 {tvg_attr}group-title="{entry["group_title"]}",'
            f'{entry["channel_name"]}'
        )

        lines.append(VLC_HEADER_BLOCK.rstrip("\n"))
        lines.append(extinf_line)
        lines.append(entry["stream_url"])

    body = "\n".join(lines)
    return "#EXTM3U\n" + body + ("\n" if body else "")


def write_match_log(path: str, match_results: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["channel_name", "tvg_id", "match_type", "suggested_id", "suggested_score"])
        for r in match_results:
            writer.writerow([
                r["channel_name"], r["tvg_id"], r["match_type"],
                r["suggested_id"], r["suggested_score"],
            ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default=SOURCE_URL,
        help="URL or local path to the source channel list "
        "(default: dlhd_damitv.txt on GitHub)",
    )
    parser.add_argument(
        "--output",
        default="dlhd_damitv.m3u",
        help="Output path for the plain (no tvg-id) playlist "
        "(default: dlhd_damitv.m3u)",
    )
    parser.add_argument(
        "--output-with-ids",
        default=None,
        help="Output path for the tvg-id-enriched playlist "
        "(default: <output> with '_with_ids' inserted before the extension)",
    )
    parser.add_argument(
        "--no-epg",
        action="store_true",
        help="Skip tvg-id matching entirely and only write the plain playlist "
        "(no second file, no match log).",
    )
    parser.add_argument(
        "--epg-url",
        default=EPG_DATA_URL,
        help="URL or local path to epg_data.json (default: epg-viewer repo on GitHub)",
    )
    parser.add_argument(
        "--score",
        type=float,
        default=DEFAULT_SCORE,
        help=f"Fuzzy SUGGESTION threshold for the match log only, 0.0-1.0 "
        f"(default: {DEFAULT_SCORE}). This never affects which tvg-ids are "
        f"actually written - only exact matches and MANUAL_OVERRIDES are.",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Path to write the CSV match log (default: <output>.match_log.csv)",
    )
    parser.add_argument(
        "--allow-cross-region",
        action="store_true",
        help="Allow matching a channel to a different country's EPG entry "
        "when no same-region exact candidate is found (default: off, "
        "leaves such channels blank instead of guessing wrong).",
    )
    args = parser.parse_args()

    try:
        raw_text = fetch_source(args.source)
    except Exception as exc:
        print(f"Error fetching source '{args.source}': {exc}", file=sys.stderr)
        sys.exit(1)

    entries = parse_entries(raw_text)
    if not entries:
        print("Warning: no channel entries were parsed from the source.", file=sys.stderr)

    # Always write the plain (no tvg-id) playlist.
    plain_playlist = render_playlist(entries, tvg_ids=None)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(plain_playlist)
    print(f"Wrote {len(entries)} channels to {args.output} (no tvg-id)")

    if args.no_epg:
        return

    epg_index = None
    try:
        print(f"Fetching EPG data from {args.epg_url} ...")
        if args.epg_url.startswith("http://") or args.epg_url.startswith("https://"):
            epg_data = fetch_json(args.epg_url)
        else:
            with open(args.epg_url, "r", encoding="utf-8") as f:
                epg_data = json.load(f)
        epg_index, _ = build_epg_index(epg_data)
        num_channels = sum(len(v) for v in epg_index.values())
        print(f"Loaded {num_channels} EPG channels ({len(epg_index)} unique normalized names).")
    except Exception as exc:
        print(f"Warning: failed to load EPG data ({exc}); skipping the tvg-id playlist.",
              file=sys.stderr)
        return

    match_results = match_entries(entries, epg_index, args.score, args.allow_cross_region)
    tvg_ids = {r["channel_name"]: r["tvg_id"] for r in match_results}

    enriched_playlist = render_playlist(entries, tvg_ids=tvg_ids)
    output_with_ids = args.output_with_ids
    if not output_with_ids:
        if "." in args.output:
            base, ext = args.output.rsplit(".", 1)
            output_with_ids = f"{base}_with_ids.{ext}"
        else:
            output_with_ids = f"{args.output}_with_ids"

    with open(output_with_ids, "w", encoding="utf-8") as f:
        f.write(enriched_playlist)

    matched = sum(1 for r in match_results if r["tvg_id"])
    blanked = sum(1 for r in match_results if r["match_type"] == "manual_blank")
    suggested = sum(1 for r in match_results if not r["tvg_id"] and r["suggested_id"])
    print(f"Wrote {len(entries)} channels to {output_with_ids} "
          f"({matched} with tvg-id, {blanked} manually blanked, "
          f"{suggested} have an unconfirmed fuzzy suggestion in the log)")

    log_path = args.log or f"{args.output}.match_log.csv"
    write_match_log(log_path, match_results)
    print(f"Match log written to {log_path}")


if __name__ == "__main__":
    main()
