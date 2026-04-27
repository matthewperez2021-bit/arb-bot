"""
kxmve_parser.py — Parse Kalshi KXMVE multi-variant market titles into individual legs.

KXMVE market titles are comma-separated lists of outcomes, e.g.:
    "yes Jaylen Brown: 25+,yes Boston,no New York Y wins by over 1.5 runs,yes Over 4.5 goals scored"

Each leg is a single yes/no proposition that can be:
    - Team win:        "yes Boston"  /  "no Boston"
    - Team spread:     "yes New York Y wins by over 1.5 runs"
    - Player over:     "yes Jaylen Brown: 25+"   (player scores/stats over threshold)
    - Total over/under:"yes Over 4.5 goals scored"  /  "no Over 3.5 goals"

Usage:
    legs = KXMVEParser.parse("yes Boston,yes Kevin Durant: 20+,no Over 4.5 goals")
    for leg in legs:
        print(leg.position, leg.leg_type, leg.subject, leg.threshold)
"""

import re
from dataclasses import dataclass
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class KXMVELeg:
    raw:         str            # original text after "yes"/"no"
    position:    str            # "yes" or "no"
    leg_type:    str            # "team_win" | "team_spread" | "player_over" | "total_over" | "unknown"
    subject:     str            # team name or player name (lowercase, normalized)
    threshold:   Optional[float]= None   # numeric threshold for player/total legs
    spread:      Optional[float]= None   # spread value for team_spread legs

    @property
    def is_team(self) -> bool:
        return self.leg_type in ("team_win", "team_spread")

    @property
    def is_player(self) -> bool:
        return self.leg_type == "player_over"

    @property
    def is_total(self) -> bool:
        return self.leg_type == "total_over"


# ─────────────────────────────────────────────────────────────────────────────
# Regex patterns (applied in order — first match wins)
# ─────────────────────────────────────────────────────────────────────────────

# "New York Y wins by over 1.5 runs"
_TEAM_SPREAD = re.compile(
    r'^(.+?)\s+wins?\s+by\s+over\s+([\d.]+)',
    re.IGNORECASE,
)

# "Jaylen Brown: 25+" or "LeBron James: 6+" or "De'Aaron Fox: 4+"
# Allows apostrophes and hyphens within name parts (De'Aaron, O'Brien, etc.)
_PLAYER_OVER = re.compile(
    r"^([A-Z][a-zA-Z''\-]+(?:\s+[A-Z][a-zA-Z''\-]+)+)\s*:\s*([\d.]+)\+",
)

# "Over 4.5 goals scored" / "Over 3.5 goals" / "Over 2.5 runs"
_TOTAL_OVER = re.compile(
    r'^[Oo]ver\s+([\d.]+)\s+\w+',
)

# Anything else that doesn't match a colon → assume team win
# (will be matched by fallback)


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────

class KXMVEParser:
    """
    Parse a KXMVE market title into a list of KXMVELeg objects.

    Algorithm:
      1. Split on commas.
      2. Each segment starts with "yes" or "no" → extract position + body.
      3. Match body against regex patterns (spread > player > total > team_win).
      4. Return list of KXMVELeg, skipping unparseable segments.
    """

    @staticmethod
    def parse(title: str) -> list:
        """
        Parse a KXMVE market title.

        Args:
            title: The market title from Kalshi, e.g.
                   "yes Boston,yes Kevin Durant: 25+,no Over 4.5 goals scored"

        Returns:
            list of KXMVELeg (may be empty if title is unparseable)
        """
        legs = []
        # Split on commas; each segment represents one leg
        segments = [s.strip() for s in title.split(",")]

        for seg in segments:
            leg = KXMVEParser._parse_segment(seg)
            if leg is not None:
                legs.append(leg)

        return legs

    @staticmethod
    def _parse_segment(seg: str) -> Optional[KXMVELeg]:
        """Parse a single segment like "yes Boston" or "no Over 3.5 goals"."""
        seg = seg.strip()

        # Extract yes/no position
        if seg.lower().startswith("yes "):
            position = "yes"
            body = seg[4:].strip()
        elif seg.lower().startswith("no "):
            position = "no"
            body = seg[3:].strip()
        else:
            return None  # malformed

        if not body:
            return None

        # Pattern 1: team spread ("New York Y wins by over 1.5 runs")
        m = _TEAM_SPREAD.match(body)
        if m:
            return KXMVELeg(
                raw=body,
                position=position,
                leg_type="team_spread",
                subject=m.group(1).strip().lower(),
                spread=float(m.group(2)),
            )

        # Pattern 2: player over threshold ("Jaylen Brown: 25+")
        m = _PLAYER_OVER.match(body)
        if m:
            return KXMVELeg(
                raw=body,
                position=position,
                leg_type="player_over",
                subject=m.group(1).strip().lower(),
                threshold=float(m.group(2)),
            )

        # Pattern 3: total over ("Over 4.5 goals scored")
        m = _TOTAL_OVER.match(body)
        if m:
            return KXMVELeg(
                raw=body,
                position=position,
                leg_type="total_over",
                subject="total",
                threshold=float(m.group(1)),
            )

        # Pattern 4: fallback → team win
        # Exclude segments that look like noise (very short, all digits, etc.)
        if len(body) >= 3 and not body[0].isdigit():
            return KXMVELeg(
                raw=body,
                position=position,
                leg_type="team_win",
                subject=body.lower(),
            )

        return None


# ─────────────────────────────────────────────────────────────────────────────
# Team name variant builder
# ─────────────────────────────────────────────────────────────────────────────

def build_team_variants(events: list) -> dict:
    """
    Build a lookup {variant_string: (canonical_team_name, event_dict)}
    from a flat list of Odds API event dicts.

    For each team name (e.g. "New York Yankees") we generate multiple variants:
      - Full name:              "new york yankees"
      - Last word (nickname):   "yankees"
      - City / first word:      "new york"
      - City + first letter:    "new york y"       ← Kalshi's common abbreviation
      - All words except last:  "new york"
      - Common abbrevs:         "ny", "nyy"  (for well-known teams)

    Returns dict keyed by lowercased variant, value is (full_name, event).
    If two teams share a variant, the later one wins (acceptable for our use).
    """
    variants: dict = {}

    for event in events:
        for team in (event.get("home_team", ""), event.get("away_team", "")):
            if not team:
                continue
            _register_team(team, event, variants)

    return variants


def _register_team(team: str, event: dict, variants: dict):
    """Register all name variants for one team into the variants dict."""
    words = team.split()
    base  = (team, event)

    def add(key: str):
        k = key.lower().strip()
        if k and len(k) >= 2:
            variants[k] = base

    # Full name
    add(team)

    if len(words) == 1:
        add(words[0])
        return

    # Last word (team nickname) — "Yankees", "Celtics", "Oilers"
    add(words[-1])

    # First word (city) — "Boston", "New", "Los"
    add(words[0])

    # All words except last (city / metro) — "New York", "Los Angeles"
    add(" ".join(words[:-1]))

    # City + first letter of nickname — "New York Y" for Yankees
    add(" ".join(words[:-1]) + " " + words[-1][0])

    # First two letters of nickname — "New York Ya"
    if len(words[-1]) >= 2:
        add(" ".join(words[:-1]) + " " + words[-1][:2])

    # Initials of all words — "NYY", "LAL", "GSW"
    initials = "".join(w[0] for w in words)
    add(initials)

    # Initials of city words — "NY" for New York, "LA" for Los Angeles
    if len(words) > 2:
        city_initials = "".join(w[0] for w in words[:-1])
        add(city_initials)

    # Common known abbreviations (hardcoded for top teams)
    _KNOWN_ABBREVS = {
        # MLB
        "new york yankees":     ["nyy", "ny yankees", "yankees"],
        "new york mets":        ["nym", "ny mets", "mets"],
        "los angeles dodgers":  ["lad", "la dodgers", "dodgers"],
        "los angeles angels":   ["laa", "la angels", "angels"],
        "chicago white sox":    ["cws", "white sox"],
        "chicago cubs":         ["chc", "cubs"],
        "boston red sox":       ["bos", "red sox"],
        "houston astros":       ["hou", "astros"],
        "san francisco giants": ["sfg", "sf giants", "giants"],
        "new york giants":      ["nyg"],   # NFL — different from SF Giants
        # NBA
        "los angeles lakers":   ["lal", "la lakers", "lakers"],
        "los angeles clippers": ["lac", "la clippers", "clippers"],
        "golden state warriors":["gsw", "golden state", "warriors"],
        "san antonio spurs":    ["sas", "spurs"],
        "oklahoma city thunder":["okc", "thunder"],
        "new york knicks":      ["nyk", "knicks"],
        "brooklyn nets":        ["bkn", "nets"],
        # NHL
        "edmonton oilers":      ["edm", "edm oilers"],
        "montreal canadiens":   ["mtl", "mtl canadiens", "canadiens", "habs"],
        "toronto maple leafs":  ["tor", "maple leafs", "leafs"],
        "new york rangers":     ["nyr", "rangers"],
        "new york islanders":   ["nyi", "islanders"],
        # NFL
        "kansas city chiefs":   ["kc", "chiefs"],
        "san francisco 49ers":  ["sf", "49ers", "niners"],
        "new england patriots": ["ne", "patriots", "pats"],
        "green bay packers":    ["gb", "packers"],
        "dallas cowboys":       ["dal", "cowboys"],
        # Soccer
        "rb leipzig":           ["leipzig"],
        "bayer leverkusen":     ["leverkusen"],
        "nottingham forest":    ["nottingham", "forest"],
        "stade brest":          ["brest"],
    }

    team_lower = team.lower()
    for abbrevs in _KNOWN_ABBREVS.get(team_lower, []):
        add(abbrevs)


# ─────────────────────────────────────────────────────────────────────────────
# Quick test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    samples = [
        "yes Jaylen Brown: 25+,yes Boston,no New York Y wins by over 1.5 runs,yes Over 4.5 goals scored",
        "yes Tyrese Maxey: 6+,yes De'Aaron Fox: 4+,yes Jaylen Brown: 30+,yes Jayson Tatum: 20+",
        "yes New York Y,yes Boston,yes Houston,yes San Antonio,yes Jessica Pegula",
        "yes Leipzig wins by over 2.5 goals,yes Over 3.5 goals scored,no Over 4.5 goals scored",
        "yes EDM Oilers,yes MTL Canadiens,yes Over 5.5 goals scored",
    ]
    for s in samples:
        legs = KXMVEParser.parse(s)
        print(f"\nTitle: {s[:80]}")
        for leg in legs:
            print(f"  {leg.position:3} | {leg.leg_type:12} | {leg.subject:30} | thresh={leg.threshold}")
