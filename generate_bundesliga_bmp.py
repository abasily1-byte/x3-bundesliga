#!/usr/bin/env python3
"""Generate the Bundesliga edition of the X3 monochrome scoreboard."""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 528, 792
PACIFIC = ZoneInfo("America/Los_Angeles")
BERLIN = ZoneInfo("Europe/Berlin")
API_ROOT = "https://api.openligadb.de"
LEAGUE = "bl1"


def field(item: dict[str, Any], name: str, default: Any = None) -> Any:
    """Tolerate API capitalization differences without inventing missing data."""
    return next((value for key, value in item.items() if key.casefold() == name.casefold()), default)


def api_get(path: str) -> Any:
    url = f"{API_ROOT}/{path}"
    request = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "x3-bundesliga/1.0 (+GitHub Actions)"
    })
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == 2:
                raise RuntimeError(f"Could not fetch OpenLigaDB data from {url}: {exc}") from exc
            time.sleep(attempt + 1)


def parse_kickoff(match: dict[str, Any]) -> datetime | None:
    utc_value = field(match, "matchDateTimeUTC")
    local_value = field(match, "matchDateTime")
    for value, fallback in ((utc_value, timezone.utc), (local_value, BERLIN)):
        if not value:
            continue
        try:
            kickoff = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=fallback)
            return kickoff.astimezone(PACIFIC)
        except ValueError:
            continue
    return None


def score_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
        return result if result >= 0 and str(value).strip() == str(result) else None
    except (TypeError, ValueError):
        return None


def parse_match(match: dict[str, Any]) -> dict[str, Any]:
    def team_info(team: Any) -> dict[str, str]:
        team = team if isinstance(team, dict) else {}
        name = str(field(team, "teamName") or field(team, "shortName") or "Team TBC")
        short = str(field(team, "shortName") or name)
        return {"name": name, "short": short}

    # resultTypeID=2 is the full-time result. Never mistake the halftime result
    # or the order of the array for the final score.
    results = field(match, "matchResults") or []
    final = next((r for r in results if isinstance(r, dict) and str(field(r, "resultTypeID")) == "2"), {})
    finished = field(match, "matchIsFinished", False)
    is_final = finished is True or (isinstance(finished, str) and finished.lower() == "true")
    return {
        "id": field(match, "matchID"),
        "kickoff": parse_kickoff(match),
        "status": "C" if is_final else "U",
        "home": team_info(field(match, "team1")),
        "away": team_info(field(match, "team2")),
        "home_score": score_value(field(final, "pointsTeam1")),
        "away_score": score_value(field(final, "pointsTeam2")),
    }


def fetch_dashboard_data(now: datetime) -> dict[str, Any]:
    # Ask the source for its current matchday rather than guessing from the
    # calendar or advancing as soon as a weekend finishes. Retry once if the
    # source changes matchday between requests.
    for _ in range(2):
        current = api_get(f"getcurrentgroup/{LEAGUE}")
        matches = api_get(f"getmatchdata/{LEAGUE}")
        if not isinstance(current, dict) or not isinstance(matches, list) or not matches:
            raise RuntimeError("OpenLigaDB returned no current Bundesliga matchday")
        matchday = int(field(current, "groupOrderID", 0))
        seasons = {int(field(m, "leagueSeason", 0)) for m in matches}
        groups = {int(field(field(m, "group", {}) or {}, "groupOrderID", 0)) for m in matches}
        if groups != {matchday}:
            continue
        if matchday <= 0 or len(seasons) != 1 or next(iter(seasons)) < 2000:
            raise RuntimeError("OpenLigaDB returned inconsistent season/matchday metadata")
        season = next(iter(seasons))
        full_matches = api_get(f"getmatchdata/{LEAGUE}/{season}/{matchday}")
        if not isinstance(full_matches, list) or not full_matches:
            raise RuntimeError(f"No fixtures returned for Bundesliga matchday {matchday}")
        for match in full_matches:
            if (str(field(match, "leagueShortcut", "")).lower() != LEAGUE
                    or int(field(match, "leagueSeason", 0)) != season
                    or int(field(field(match, "group", {}) or {}, "groupOrderID", 0)) != matchday):
                raise RuntimeError("OpenLigaDB returned fixtures outside the requested Bundesliga matchday")
        ids = [field(m, "matchID") for m in full_matches]
        if len(ids) != len(set(ids)):
            raise RuntimeError("OpenLigaDB returned duplicate fixture IDs")
        parsed = [parse_match(match) for match in full_matches]
        parsed.sort(key=lambda m: (m["kickoff"] or datetime.max.replace(tzinfo=timezone.utc), str(m["id"])))
        return {
            "season": f"{season}/{str(season + 1)[-2:]}", "matchday": matchday,
            "matches": parsed, "updated": now.astimezone(PACIFIC),
        }
    raise RuntimeError("OpenLigaDB changed matchday during retrieval; retry the workflow")


def find_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    names = (["DejaVuSansCondensed-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"] if bold else
             ["DejaVuSansCondensed.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
              "/System/Library/Fonts/Supplemental/Arial.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def center_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str,
                font: ImageFont.ImageFont, fill: str) -> None:
    draw.text(xy, text, font=font, fill=fill, anchor="mm")


def fitting_team(draw: ImageDraw.ImageDraw, team: dict[str, str],
                 max_width: int, size: int) -> tuple[str, ImageFont.ImageFont]:
    # Keep the full recognizable name whenever it fits; use the source's own
    # familiar short name before reducing type size.
    for font_size in range(size, 10, -1):
        font = find_font(font_size, bold=True)
        for candidate in dict.fromkeys((team["name"], team["short"])):
            if text_width(draw, candidate.upper(), font) <= max_width:
                return candidate.upper(), font
    raise RuntimeError(f"Team name will not fit safely: {team['name']}")


def time_label(kickoff: datetime | None) -> str:
    if kickoff is None:
        return "KICKOFF TBC"
    return kickoff.strftime("%a %b %d  •  %I:%M %p PT").upper().replace(" 0", " ")


def draw_match_row(draw: ImageDraw.ImageDraw, match: dict[str, Any], y: int,
                   height: int, team_size: int, meta_size: int) -> None:
    meta_font = find_font(meta_size)
    center_text(draw, (WIDTH // 2, y + 11), time_label(match["kickoff"]), meta_font, "black")
    score_font = find_font(team_size, bold=True)
    if match["status"] == "C":
        home_score = match["home_score"] if match["home_score"] is not None else "?"
        away_score = match["away_score"] if match["away_score"] is not None else "?"
        middle = f"{home_score}–{away_score} FT"
    else:
        middle = "v"
    # Match the PL's 216 / 312 team anchors, widening the central score area
    # only if necessary so unusually large scores cannot touch team text.
    half_gap = max(48, (text_width(draw, middle, score_font) + 1) // 2 + 8)
    left_edge, right_edge = WIDTH // 2 - half_gap, WIDTH // 2 + half_gap
    team_width = left_edge - 27
    home, home_font = fitting_team(draw, match["home"], team_width, team_size)
    away, away_font = fitting_team(draw, match["away"], team_width, team_size)
    team_y = y + min(31, height - 12)
    draw.text((left_edge, team_y), home, font=home_font, fill="black", anchor="rm")
    draw.text((right_edge, team_y), away, font=away_font, fill="black", anchor="lm")
    center_text(draw, (WIDTH // 2, team_y), middle, score_font, "black")
    draw.line((22, y + height - 1, WIDTH - 23, y + height - 1), fill="black", width=1)


def render_dashboard(data: dict[str, Any], output: Path, preview: Path | None = None) -> None:
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((6, 6, WIDTH - 7, HEIGHT - 7), outline="black", width=3)
    draw.rectangle((17, 17, WIDTH - 18, 94), fill="black")
    center_text(draw, (WIDTH // 2, 48), "BUNDESLIGA", find_font(37, True), "white")
    center_text(draw, (WIDTH // 2, 78), f"{data['season']}  •  PACIFIC TIME", find_font(17, True), "white")

    matches = data["matches"]
    if not matches:
        raise RuntimeError("Cannot render an empty matchday")
    completed = [m for m in matches if m["status"] == "C"]
    upcoming = [m for m in matches if m["status"] != "C"]
    draw.line((17, 105, WIDTH - 18, 105), fill="black", width=2)
    center_text(draw, (WIDTH // 4, 127), f"MATCHDAY {data['matchday']}", find_font(19, True), "black")
    center_text(draw, (WIDTH * 3 // 4, 127), f"{len(completed)} OF {len(matches)} FINAL", find_font(19, True), "black")
    draw.line((17, 149, WIDTH - 18, 149), fill="black", width=2)

    groups = [(label, items) for label, items in (("COMPLETED", completed), ("UPCOMING", upcoming)) if items]
    top, footer_top, header_height = 158, 744, 27
    available_rows = footer_top - top - header_height * len(groups) - 4
    row_height = min(54, available_rows // len(matches))
    # Do not silently clip or omit matches if unexpectedly large data arrives.
    if row_height < 38:
        raise RuntimeError("Too many matches to render legibly on the X3; output not overwritten")
    team_size = 18 if row_height >= 44 else 16
    meta_size = 12 if row_height >= 44 else 11
    y = top
    for label, group_matches in groups:
        draw.rectangle((17, y, WIDTH - 18, y + header_height - 1), fill="black")
        draw.text((25, y + header_height // 2), label, font=find_font(17, True), fill="white", anchor="lm")
        draw.text((WIDTH - 25, y + header_height // 2), str(len(group_matches)),
                  font=find_font(17, True), fill="white", anchor="rm")
        y += header_height
        for match in group_matches:
            draw_match_row(draw, match, y, row_height, team_size, meta_size)
            y += row_height
    if y >= footer_top:
        raise RuntimeError("Content would overlap the footer; output not overwritten")
    draw.line((17, footer_top, WIDTH - 18, footer_top), fill="black", width=2)
    center_text(draw, (WIDTH // 2, 760), f"UPDATED {time_label(data['updated'])}", find_font(12, True), "black")
    center_text(draw, (WIDTH // 2, 777), "DATA: OPENLIGADB", find_font(12), "black")

    # Same threshold and RGB BMP encoding as the Premier League edition.
    image = image.convert("L").point(lambda pixel: 255 if pixel >= 128 else 0, mode="1").convert("RGB")
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, format="BMP", compression="raw")
    if preview is not None:
        preview.parent.mkdir(parents=True, exist_ok=True)
        image.save(preview, format="PNG")


def validate_bmp(path: Path, preview: Path | None = None) -> None:
    if not path.exists():
        raise RuntimeError(f"Missing output file: {path}")
    header = path.read_bytes()[:54]
    if len(header) < 54 or header[:2] != b"BM":
        raise RuntimeError("Output is not a BMP file")
    if struct.unpack_from("<H", header, 28)[0] != 24 or struct.unpack_from("<I", header, 30)[0] != 0:
        raise RuntimeError("Expected uncompressed 24-bit BMP")
    with Image.open(path) as image:
        image.load()
        if image.format != "BMP" or image.size != (WIDTH, HEIGHT) or image.mode != "RGB":
            raise RuntimeError(f"Expected RGB {WIDTH}x{HEIGHT} BMP; found {image.format}, {image.size}, {image.mode}")
        colors = set(image.getdata())
        if not colors or not colors.issubset({(0, 0, 0), (255, 255, 255)}):
            raise RuntimeError("Output contains pixels other than pure black and white")
        if preview is not None:
            with Image.open(preview) as png:
                png.load()
                if png.mode != "RGB" or png.size != image.size or png.tobytes() != image.tobytes():
                    raise RuntimeError("Preview does not match the BMP pixels")
    print(f"Validated {path}: {WIDTH}x{HEIGHT}, RGB, 24-bit uncompressed BMP, {len(colors)} pure B/W colors")


def print_source_summary(data: dict[str, Any]) -> None:
    finals = sum(m["status"] == "C" for m in data["matches"])
    print(f"Season {data['season']} — Matchday {data['matchday']} — {finals} of {len(data['matches'])} final")
    for match in data["matches"]:
        when = match["kickoff"].strftime("%Y-%m-%d %H:%M %Z") if match["kickoff"] else "kickoff TBC"
        detail = f"{match['home_score']}-{match['away_score']} FT" if match["status"] == "C" else "upcoming / not final"
        print(f"  {match['id']} | {when} | {match['home']['name']} {detail} {match['away']['name']}")
    print(f"Updated {data['updated'].isoformat()}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("Bundesliga.bmp"))
    parser.add_argument("--preview", type=Path, default=Path("Bundesliga_preview.png"))
    parser.add_argument("--validate", action="store_true", help="validate both generated artifacts")
    parser.add_argument("--validate-only", action="store_true", help="validate an existing BMP and preview")
    args = parser.parse_args()
    if not args.validate_only:
        data = fetch_dashboard_data(datetime.now(timezone.utc))
        print_source_summary(data)
        render_dashboard(data, args.output, args.preview)
        print(f"Wrote {args.output} and {args.preview}")
    if args.validate or args.validate_only:
        validate_bmp(args.output, args.preview)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
