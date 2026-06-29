#!/usr/bin/env python3
"""
generate.py

Fetches the DLHD / Dami-TV channel list from BuddyChewChew/buddylive
(live/dlhd_damitv.txt) and rewrites it into a TiviMate-friendly M3U
playlist with VLC HTTP option headers (referrer, origin, user-agent)
injected before every #EXTINF entry.

Source format (one block per channel):
    #EXTINF:-1 group-title="DLHD 24/7",<Channel Name>
    https://dami-tv.pro/papi/tv/dlhd/<id>/playlist.m3u8

Output format (per channel):
    #EXTVLCOPT:http-referrer=https://dami-tv.pro/
    #EXTVLCOPT:http-origin=https://dami-tv.pro
    #EXTVLCOPT:http-user-agent=Mozilla/5.0 ...
    #EXTINF:-1 group-title="DLHD 24/7",<Channel Name>
    https://dami-tv.pro/papi/tv/dlhd/<id>/playlist.m3u8

Usage:
    python3 generate.py
    python3 generate.py --output dlhd_damitv.m3u
    python3 generate.py --source <url or local path>
"""

import argparse
import re
import sys
import urllib.request

SOURCE_URL = (
    "https://raw.githubusercontent.com/BuddyChewChew/buddylive/"
    "refs/heads/main/live/dlhd_damitv.txt"
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
    r'^(#EXTINF:.*?,.*?)\s*\n(\S+)\s*$',
    re.MULTILINE,
)


def fetch_source(source: str) -> str:
    """Read the source list from a URL or a local file path."""
    if source.startswith("http://") or source.startswith("https://"):
        req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    with open(source, "r", encoding="utf-8") as f:
        return f.read()


def build_playlist(raw_text: str) -> tuple[str, int]:
    """Parse raw EXTINF/URL pairs and rebuild with VLC header blocks."""
    lines = []
    count = 0

    for match in ENTRY_RE.finditer(raw_text):
        extinf_line = match.group(1).strip()
        stream_url = match.group(2).strip()

        lines.append(VLC_HEADER_BLOCK.rstrip("\n"))
        lines.append(extinf_line)
        lines.append(stream_url)
        count += 1

    body = "\n".join(lines)
    playlist = "#EXTM3U\n" + body + ("\n" if body else "")
    return playlist, count


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
        help="Output .m3u file path (default: dlhd_damitv.m3u)",
    )
    args = parser.parse_args()

    try:
        raw_text = fetch_source(args.source)
    except Exception as exc:
        print(f"Error fetching source '{args.source}': {exc}", file=sys.stderr)
        sys.exit(1)

    playlist, count = build_playlist(raw_text)

    if count == 0:
        print("Warning: no channel entries were parsed from the source.", file=sys.stderr)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(playlist)

    print(f"Wrote {count} channels to {args.output}")


if __name__ == "__main__":
    main()
