"""
Fantano-in-DC: find DMV-area shows by artists Fantano loves.

Sources:
  - https://theneedledrop.com/loved-list/2025/   (cached for 24h)
  - https://theneedledrop.com/loved-list/2026/   (cached for 24h; updated all year)
  - rym_10s.txt                                  (manual; RYM blocks bots)
  - https://www.songkick.com/metro-areas/1409-us-washington (fresh every run)

Both Fantano and Songkick sit behind Cloudflare. We use Playwright (headless
Chromium) which gets through fine from a normal residential IP. From a
datacenter IP you may see 403s — that's a Cloudflare behavior, not a bug.

Run:
    python run.py                # uses cached loved-lists if < 1 day old
    python run.py --refresh      # re-fetch loved-lists even if cached
    python run.py --max-pages 5  # limit Songkick pagination (faster for testing)

First-time setup:
    pip install beautifulsoup4 rapidfuzz playwright
    python -m playwright install chromium
"""

from __future__ import annotations
import argparse
import re
import time
import html
import json
import pathlib
import unicodedata
import datetime as dt
from dataclasses import dataclass, field

from bs4 import BeautifulSoup
from rapidfuzz import fuzz, process

HERE = pathlib.Path(__file__).parent
CACHE_DIR = HERE / "cache"
CACHE_DIR.mkdir(exist_ok=True)
SLEEP_BETWEEN_PAGES = 1.5

FANTANO_URLS = [
    "https://theneedledrop.com/loved-list/2025/",
    "https://theneedledrop.com/loved-list/2026/",
]

# Songkick sources. The metro page caps at ~1001 upcoming events, which in a
# busy market like DC only covers the next ~2 months. Per-venue pages don't
# have that cap and reach further into the future. We scrape both: the metro
# page catches venues we haven't enumerated, and the venue pages catch later
# dates at the major rooms. Shows from both sources get deduped by URL.
SONGKICK_SOURCES = [
    # Wide-net metro page (~1001 cap)
    ("metro", "https://www.songkick.com/metro-areas/1409-us-washington"),
    # Per-venue pages (no cap; reach further into the future)
    ("venue", "https://www.songkick.com/venues/922-930-club/calendar"),
    ("venue", "https://www.songkick.com/venues/1038-black-cat/calendar"),
    ("venue", "https://www.songkick.com/venues/3552789-anthem/calendar"),
    ("venue", "https://www.songkick.com/venues/4498657-atlantis/calendar"),
    ("venue", "https://www.songkick.com/venues/20843-dc9-nightclub/calendar"),
    ("venue", "https://www.songkick.com/venues/2826-lincoln-theatre/calendar"),
    ("venue", "https://www.songkick.com/venues/4420812-songbyrd-music-house/calendar"),
    ("venue", "https://www.songkick.com/venues/1448428-fillmore-silver-spring/calendar"),
    ("venue", "https://www.songkick.com/venues/93446-merriweather-post-pavilion/calendar"),
]
RYM_FILE = HERE / "rym_10s.txt"

# Names too generic / likely to collide.
BLOCKLIST = {"u", "the", "her", "him", "love", "home"}


# -------- fetching --------

class Fetcher:
    """Stateful fetcher that keeps a Playwright browser open across calls."""
    def __init__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._ctx = self._browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "Version/17.0 Safari/605.1.15"),
            viewport={"width": 1280, "height": 800},
        )

    def get(self, url: str) -> str:
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if resp and resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status} for {url}")
            page.wait_for_timeout(800)
            return page.content()
        finally:
            page.close()

    def close(self):
        self._ctx.close()
        self._browser.close()
        self._pw.stop()


# -------- artist list extraction --------

@dataclass
class ListedAlbum:
    artist: str
    album: str
    source: str

REVIEW_PATTERNS = (
    "/album-reviews/", "bandcamp.com", "qobuz.com", "boomkat.com",
    "erstwhilerecords.com",
)

def parse_fantano_html(html_text: str, year: str) -> list[ListedAlbum]:
    soup = BeautifulSoup(html_text, "html.parser")
    out: list[ListedAlbum] = []
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if not any(p in href for p in REVIEW_PATTERNS):
            continue
        text = a.get_text(strip=True)
        if not text or len(text) < 5:
            continue
        if " - " not in text and " – " not in text:
            continue
        lower = text.lower()
        if any(s in lower for s in ["loved list", "next post", "prev post", "full"]):
            continue
        sep = " - " if " - " in text else " – "
        artist, _, album = text.partition(sep)
        artist, album = artist.strip(), album.strip()
        if not artist or not album:
            continue
        if len(artist) > 80 or len(album) > 120:
            continue
        out.append(ListedAlbum(artist=artist, album=album, source=f"loved-{year}"))
    return out

def scrape_fantano(fetcher: Fetcher, url: str, refresh: bool) -> list[ListedAlbum]:
    year = "2026" if "2026" in url else "2025"
    cache_path = CACHE_DIR / f"fantano-{year}.html"
    fresh = cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < 86400
    if not refresh and fresh:
        print(f"    cache hit: {cache_path.name}")
        html_text = cache_path.read_text()
    else:
        print(f"    fetching {url}")
        html_text = fetcher.get(url)
        cache_path.write_text(html_text)
    return parse_fantano_html(html_text, year)

def load_rym() -> list[ListedAlbum]:
    if not RYM_FILE.exists():
        RYM_FILE.write_text(
            "# Anthony Fantano 10/10 albums from RYM, one per line as 'Artist - Album'.\n"
            "# RateYourMusic blocks scrapers, so you maintain this manually.\n"
            "# Source: https://rateyourmusic.com/list/nickzadr/"
            "anthony-fantano-theneedledrop-10_10-albums/\n"
            "# Lines starting with # are ignored.\n"
            "#\n"
            "# Example entries (delete and replace):\n"
            "# Death Grips - The Money Store\n"
            "# Swans - To Be Kind\n"
            "# Kendrick Lamar - To Pimp a Butterfly\n"
        )
        print(f"    created starter {RYM_FILE.name}")
        return []
    out: list[ListedAlbum] = []
    for line in RYM_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        sep = " - " if " - " in line else (" – " if " – " in line else None)
        if not sep:
            continue
        artist, _, album = line.partition(sep)
        out.append(ListedAlbum(artist=artist.strip(), album=album.strip(),
                               source="rym-10"))
    return out

def gather_loved(fetcher: Fetcher, refresh: bool) -> dict[str, ListedAlbum]:
    all_items: list[ListedAlbum] = []
    for url in FANTANO_URLS:
        all_items.extend(scrape_fantano(fetcher, url, refresh))
    rym = load_rym()
    print(f"    RYM 10/10s: {len(rym)}")
    all_items.extend(rym)
    canonical: dict[str, ListedAlbum] = {}
    sources: dict[str, set[str]] = {}
    for item in all_items:
        n = normalize(item.artist)
        if not n or n in BLOCKLIST:
            continue
        sources.setdefault(n, set()).add(item.source)
        if n not in canonical:
            canonical[n] = item
    return {n: ListedAlbum(artist=v.artist, album=v.album,
                           source=",".join(sorted(sources[n])))
            for n, v in canonical.items()}


# -------- Songkick scraping --------

@dataclass
class Show:
    date: str
    headliner: str
    supporting: list[str] = field(default_factory=list)
    venue: str = ""
    city: str = ""
    url: str = ""

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June",
     "July", "August", "September", "October", "November", "December"], start=1)}

def _parse_date(text: str) -> str:
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return text
    day = int(m.group(1))
    month = MONTHS.get(m.group(2))
    year = int(m.group(3))
    if not month:
        return text
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return text

def parse_songkick_page(soup: BeautifulSoup) -> list[Show]:
    candidate_uls = soup.find_all("ul")
    target_ul = None
    best = 0
    for ul in candidate_uls:
        count = len(ul.find_all("a", href=re.compile(r"^/(concerts|festivals)/")))
        if count > best:
            best, target_ul = count, ul
    if not target_ul or best == 0:
        return []
    out: list[Show] = []
    current_date = ""
    for child in target_ul.find_all("li", recursive=False):
        text = child.get_text(" ", strip=True)
        if re.match(r"^(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\s+\d{1,2}\s+"
                    r"[A-Za-z]+\s+\d{4}$", text):
            current_date = _parse_date(text)
            continue
        # Each event <li> contains multiple <a> links to /concerts/: a thumbnail
        # wrapper (just an <img> inside, no text), the main artist link (with
        # <strong>headliner</strong> and trailing support text inside a <p
        # class="artists">), and a "Buy tickets" button. We want the artist
        # link — find the <p class="artists"> first and look inside it.
        artists_p = child.find("p", class_=re.compile(r"\bartists\b"))
        if artists_p:
            a = artists_p.find("a", href=re.compile(r"^/(concerts|festivals)/"))
        else:
            # Fallback for older / different structures: pick the concert link
            # that actually contains a <strong>.
            a = None
            for cand in child.find_all("a", href=re.compile(r"^/(concerts|festivals)/")):
                if cand.find("strong"):
                    a = cand
                    break
            if a is None:
                a = child.find("a", href=re.compile(r"^/(concerts|festivals)/"))
        if not a:
            continue
        href = a.get("href", "")
        strong = a.find("strong")
        headliner = (strong.get_text(strip=True) if strong
                     else a.get_text(strip=True).split("\n")[0])
        supporting: list[str] = []
        if strong:
            tail = strong.next_sibling
            tail_text = ""
            while tail:
                if hasattr(tail, "get_text"):
                    tail_text += " " + tail.get_text(" ", strip=True)
                elif isinstance(tail, str):
                    tail_text += " " + tail
                tail = tail.next_sibling
            tail_text = tail_text.strip()
            if tail_text:
                parts = re.split(r",\s*|\s+and\s+", tail_text)
                supporting = [p.strip() for p in parts if p.strip()]
        venue, city = "", ""
        # Songkick wraps location in <p class="location">. Scope our search
        # there so we don't accidentally pull venue text from elsewhere.
        location_p = child.find("p", class_=re.compile(r"\blocation\b"))
        search_root = location_p if location_p else child
        venue_a = search_root.find("a", href=re.compile(r"^/venues/"))
        if venue_a:
            venue = venue_a.get_text(strip=True)
            full_loc_text = search_root.get_text(" ", strip=True)
            m = re.search(r",\s*([^,]+),\s*[A-Z]{2}", full_loc_text)
            if m:
                city = m.group(1).strip()
        out.append(Show(date=current_date, headliner=headliner,
                        supporting=supporting, venue=venue, city=city,
                        url="https://www.songkick.com" + href))
    return out

def scrape_songkick_metro(fetcher: Fetcher, max_pages: int) -> list[Show]:
    """Scrape both the metro page (broad coverage, ~1001 cap) and per-venue
    pages (narrower, but reach further into the future). Dedupe by concert URL."""
    seen_urls: set[str] = set()
    all_shows: list[Show] = []

    for kind, base_url in SONGKICK_SOURCES:
        print(f"  source: {kind} {base_url}")
        source_shows: list[Show] = []
        for page_num in range(1, max_pages + 1):
            sep = "&" if "?" in base_url else "?"
            url = base_url if page_num == 1 else f"{base_url}{sep}page={page_num}"
            print(f"    page {page_num}: {url}")
            try:
                html_text = fetcher.get(url)
            except Exception as e:
                print(f"      error: {e}; stopping this source")
                break
            soup = BeautifulSoup(html_text, "html.parser")
            page_shows = parse_songkick_page(soup)
            if not page_shows:
                print("      0 shows on this page; stopping this source")
                break
            new_shows = [s for s in page_shows if s.url not in seen_urls]
            for s in new_shows:
                seen_urls.add(s.url)
            source_shows.extend(new_shows)
            print(f"      +{len(new_shows)} new ({len(page_shows) - len(new_shows)} dupes)")
            next_n = page_num + 1
            if not soup.find("a", href=re.compile(rf"page={next_n}\b")):
                print("      no next page; done with this source")
                break
            time.sleep(SLEEP_BETWEEN_PAGES)
        all_shows.extend(source_shows)
        print(f"    → {len(source_shows)} shows from this source")
    return all_shows


# -------- matching --------

def normalize(name: str) -> str:
    """Normalize an artist name for matching.

    Steps: NFKD-strip diacritics ("Björk" -> "bjork", "Snõõper" -> "snooper"),
    drop parenthetical disambiguators ("Maggie Lindemann (US)" -> "maggie lindemann"),
    lowercase, drop punctuation, collapse whitespace, drop leading "the".
    """
    if name in _ALIAS_MAP:
        name = _ALIAS_MAP[name]
    # Strip parenthetical region/disambiguator tags Songkick adds: "Foo (UK)", "Foo (US)"
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)
    # Decompose unicode (é -> e + ◌́) and drop combining marks.
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    s = name.lower()
    # Convert anything non-alphanumeric (except &) to space, then collapse.
    s = re.sub(r"[^\w\s&]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s


# Alias map: maps display names that won't normalize correctly to a canonical form.
# Add entries here when you find a mismatch you want to teach the script.
# The LHS is what Songkick (or Fantano) calls the artist, the RHS is the canonical name.
_ALIAS_MAP: dict[str, str] = {
    # Songkick listings of common abbreviations / alternate spellings:
    # (none needed at startup — add as you discover mismatches)
}

@dataclass
class Match:
    show: Show
    matched_artist_role: str
    matched_artist_name: str
    listed_album: ListedAlbum
    score: int

def match_shows(shows: list[Show], loved: dict[str, ListedAlbum],
                fuzzy_threshold: int = 92
                ) -> tuple[list[Match], list[Match]]:
    norm_keys = list(loved.keys())
    exact: list[Match] = []
    fuzzy: list[Match] = []
    seen_exact: set[tuple[str, str, str]] = set()

    def _try_exact(show: Show, name: str, role: str) -> bool:
        n = normalize(name)
        if not n or n in BLOCKLIST or len(n) < 2:
            return False
        if n in loved:
            key = (show.date, show.headliner, name)
            if key not in seen_exact:
                seen_exact.add(key)
                exact.append(Match(show=show, matched_artist_role=role,
                                   matched_artist_name=name,
                                   listed_album=loved[n], score=100))
            return True
        return False

    def _try_fuzzy(show: Show, name: str, role: str) -> None:
        n = normalize(name)
        if not n or n in BLOCKLIST or len(n) < 4:
            return
        # For short normalized names (≤8 chars), require a higher score because
        # small edit distances are more likely to be different artists than typos.
        # "the hive" vs "the hives" scores ~94 but they're different bands.
        effective_threshold = 98 if len(n) <= 8 else fuzzy_threshold
        result = process.extractOne(n, norm_keys, scorer=fuzz.ratio,
                                    score_cutoff=effective_threshold)
        if result:
            matched_key, score, _ = result
            fuzzy.append(Match(show=show, matched_artist_role=role,
                               matched_artist_name=name,
                               listed_album=loved[matched_key],
                               score=int(score)))

    for show in shows:
        # The headliner string might be a single artist whose name contains commas
        # (e.g. "Black Country, New Road") or a list of artists ("BENEE and bayli").
        # Try the whole string as an exact match first; if that fails, split and
        # try each part. This avoids breaking bands with commas in their names.
        if not _try_exact(show, show.headliner, "headliner"):
            parts = re.split(r",\s*|\s+and\s+|\s+&\s+", show.headliner)
            parts = [p.strip() for p in parts if p.strip()]
            # Only treat as multi-artist if splitting actually yielded multiple parts.
            if len(parts) > 1:
                for part in parts:
                    if not _try_exact(show, part, "headliner"):
                        _try_fuzzy(show, part, "headliner")
            else:
                _try_fuzzy(show, show.headliner, "headliner")

        for s in show.supporting:
            if not _try_exact(show, s, "support"):
                parts = re.split(r",\s*|\s+and\s+|\s+&\s+", s)
                parts = [p.strip() for p in parts if p.strip()]
                if len(parts) > 1:
                    for part in parts:
                        if not _try_exact(show, part, "support"):
                            _try_fuzzy(show, part, "support")
                else:
                    _try_fuzzy(show, s, "support")
    return exact, fuzzy


# -------- HTML output --------

HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Fantano artists in DC</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {{
    --bg: #fafaf7; --fg: #1a1a1a; --muted: #666;
    --accent: #c0392b; --border: #e3e1d8;
    --going: #2a7f3e; --skip: #888;
  }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         max-width: 760px; margin: 2rem auto; padding: 0 1.2rem;
         background: var(--bg); color: var(--fg); line-height: 1.5; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 0.2rem 0; }}
  .sub {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 1.5rem; }}
  h2 {{ font-size: 1.15rem; margin-top: 2rem; padding-bottom: 0.3rem;
        border-bottom: 1px solid var(--border); }}

  .filters {{ background: #fff; border: 1px solid var(--border); border-radius: 6px;
              padding: 0.6rem 0.8rem; margin: 1rem 0; font-size: 0.88rem;
              display: flex; flex-wrap: wrap; gap: 0.6rem 1.2rem; align-items: center; }}
  .filters label {{ cursor: pointer; user-select: none; }}
  .filters input[type=checkbox] {{ vertical-align: middle; }}
  .filter-counts {{ color: var(--muted); margin-left: auto; }}

  .show {{ border-bottom: 1px solid var(--border); padding: 0.8rem 0;
           transition: opacity 0.15s; }}
  .show.hidden {{ display: none; }}
  .show.dim {{ opacity: 0.4; }}
  .show .date {{ font-weight: 600; }}
  .show .artist {{ font-weight: 600; color: var(--accent); }}
  .show.going .artist {{ color: var(--going); }}
  .show .meta {{ color: var(--muted); font-size: 0.88rem; }}
  .show .album {{ font-style: italic; color: var(--muted); font-size: 0.88rem; }}
  .source-tag {{ display: inline-block; font-size: 0.7rem; padding: 1px 6px;
                 background: #eee; border-radius: 3px; color: #555;
                 margin-left: 0.3rem; }}
  .score {{ font-size: 0.75rem; color: var(--muted); }}
  a {{ color: var(--accent); }}
  .empty {{ color: var(--muted); font-style: italic; }}

  .labels {{ margin-top: 0.4rem; font-size: 0.82rem; }}
  .labels button {{ font: inherit; padding: 2px 10px; margin-right: 4px;
                    border: 1px solid var(--border); background: #fff;
                    border-radius: 4px; cursor: pointer; color: var(--muted); }}
  .labels button:hover {{ background: #f3f1ea; }}
  .labels button.active[data-label=going] {{ background: var(--going); color: #fff; border-color: var(--going); }}
  .labels button.active[data-label=skip] {{ background: var(--skip); color: #fff; border-color: var(--skip); }}
  .status {{ margin-left: 0.5rem; color: var(--muted); font-size: 0.78rem; }}
</style>
</head>
<body>
<h1>Fantano artists with upcoming DMV shows</h1>
<p class="sub">Generated {generated}. {n_loved} unique loved artists across the
three lists, checked against {n_shows} upcoming DMV shows on Songkick.</p>

<div class="filters">
  <label><input type="checkbox" id="hide-skipped" checked> Hide skipped</label>
  <label><input type="checkbox" id="only-unlabeled"> Only show new (unlabeled)</label>
  <label><input type="checkbox" id="hide-fuzzy"> Hide fuzzy section</label>
  <span class="filter-counts" id="filter-counts"></span>
</div>

<h2>Confirmed matches (<span data-section-count="exact">{n_exact}</span>)</h2>
<div id="exact-section">
{exact_html}
</div>

<h2 id="fuzzy-header">Probable matches — eyeball these (<span data-section-count="fuzzy">{n_fuzzy}</span>)</h2>
<p class="sub" id="fuzzy-sub">Fuzzy name matches. May include false positives where two unrelated
artists happen to have similar names.</p>
<div id="fuzzy-section">
{fuzzy_html}
</div>

<script>
(function() {{
  const STORAGE_KEY = 'fantano-dc-labels';
  function loadLabels() {{
    try {{ return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{{}}'); }}
    catch (e) {{ return {{}}; }}
  }}
  function saveLabels(labels) {{
    localStorage.setItem(STORAGE_KEY, JSON.stringify(labels));
  }}
  let labels = loadLabels();

  function applyLabel(showEl, label) {{
    showEl.classList.toggle('going', label === 'going');
    showEl.classList.toggle('skipped', label === 'skip');
    showEl.querySelectorAll('.labels button').forEach(btn => {{
      btn.classList.toggle('active', btn.dataset.label === label);
    }});
    const status = showEl.querySelector('.status');
    if (status) {{
      status.textContent = label === 'going' ? '✓ going'
                          : label === 'skip' ? 'skipped'
                          : '';
    }}
  }}

  // Initialize each show from localStorage and wire up buttons.
  document.querySelectorAll('.show').forEach(showEl => {{
    const key = showEl.dataset.key;
    if (!key) return;
    if (labels[key]) applyLabel(showEl, labels[key]);
    showEl.querySelectorAll('.labels button').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const label = btn.dataset.label;
        if (labels[key] === label) {{
          // Toggle off
          delete labels[key];
          applyLabel(showEl, null);
        }} else {{
          labels[key] = label;
          applyLabel(showEl, label);
        }}
        saveLabels(labels);
        refilter();
      }});
    }});
  }});

  // Filter logic
  const hideSkipped = document.getElementById('hide-skipped');
  const onlyUnlabeled = document.getElementById('only-unlabeled');
  const hideFuzzy = document.getElementById('hide-fuzzy');
  const counts = document.getElementById('filter-counts');

  // Persist filter preferences too.
  const FILTER_KEY = 'fantano-dc-filters';
  try {{
    const f = JSON.parse(localStorage.getItem(FILTER_KEY) || '{{}}');
    if (typeof f.hideSkipped === 'boolean') hideSkipped.checked = f.hideSkipped;
    if (typeof f.onlyUnlabeled === 'boolean') onlyUnlabeled.checked = f.onlyUnlabeled;
    if (typeof f.hideFuzzy === 'boolean') hideFuzzy.checked = f.hideFuzzy;
  }} catch (e) {{}}

  function saveFilters() {{
    localStorage.setItem(FILTER_KEY, JSON.stringify({{
      hideSkipped: hideSkipped.checked,
      onlyUnlabeled: onlyUnlabeled.checked,
      hideFuzzy: hideFuzzy.checked,
    }}));
  }}

  function refilter() {{
    let visible = 0, going = 0, skipped = 0;
    document.querySelectorAll('.show').forEach(showEl => {{
      const label = labels[showEl.dataset.key];
      let hide = false;
      if (hideSkipped.checked && label === 'skip') hide = true;
      if (onlyUnlabeled.checked && label) hide = true;
      showEl.classList.toggle('hidden', hide);
      if (label === 'going') going++;
      if (label === 'skip') skipped++;
      if (!hide) visible++;
    }});
    // Fuzzy section visibility
    const fuzzyEls = [document.getElementById('fuzzy-header'),
                     document.getElementById('fuzzy-sub'),
                     document.getElementById('fuzzy-section')];
    fuzzyEls.forEach(el => {{ if (el) el.style.display = hideFuzzy.checked ? 'none' : ''; }});
    counts.textContent = `${{going}} going · ${{skipped}} skipped · ${{visible}} shown`;
    saveFilters();
  }}

  [hideSkipped, onlyUnlabeled, hideFuzzy].forEach(el => {{
    el.addEventListener('change', refilter);
  }});
  refilter();
}})();
</script>
</body>
</html>
"""

def render_match(m: Match) -> str:
    date = html.escape(m.show.date or "TBD")
    matched = html.escape(m.matched_artist_name)
    if m.matched_artist_role == "headliner":
        role = "headlining"
    else:
        role = f"opening for {html.escape(m.show.headliner)}"
    venue = html.escape(m.show.venue) if m.show.venue else "venue tbd"
    city_str = f", {html.escape(m.show.city)}" if m.show.city else ""
    album = html.escape(m.listed_album.album)
    src = html.escape(m.listed_album.source)
    score_html = (f' <span class="score">(match score {m.score})</span>'
                  if m.score < 100 else "")
    # Use Songkick URL as stable storage key. It survives page regeneration.
    key = html.escape(m.show.url, quote=True)
    return f"""
    <div class="show" data-key="{key}">
      <div><span class="date">{date}</span> &middot;
           <span class="artist">{matched}</span>
           <span class="source-tag">{src}</span>{score_html}</div>
      <div class="meta">{role} &middot; {venue}{city_str}
           &middot; <a href="{html.escape(m.show.url)}">tickets</a></div>
      <div class="album">listed for: {album}</div>
      <div class="labels">
        <button data-label="going" type="button">going</button>
        <button data-label="skip" type="button">skip</button>
        <span class="status"></span>
      </div>
    </div>"""

def _dedupe_matches(matches: list[Match]) -> list[Match]:
    """Collapse duplicate matches where the same artist plays the same venue on
    the same date but Songkick has the gig listed under multiple URLs (e.g. a
    festival entry plus an individual artist entry for the same night).
    
    When duplicates exist, prefer the "headliner" version over the "support"
    version, since the headliner billing carries more information.
    """
    by_key: dict[tuple[str, str, str], Match] = {}
    for m in matches:
        n = normalize(m.matched_artist_name)
        key = (m.show.date, n, m.show.venue)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = m
        else:
            # Prefer headliner billings over support billings.
            if existing.matched_artist_role == "support" and m.matched_artist_role == "headliner":
                by_key[key] = m
    return list(by_key.values())


def write_html(exact: list[Match], fuzzy: list[Match], n_loved: int,
               n_shows: int, out_path: pathlib.Path) -> None:
    exact = _dedupe_matches(exact)
    fuzzy = _dedupe_matches(fuzzy)
    exact_sorted = sorted(exact, key=lambda m: (m.show.date or "9999",
                                                m.show.headliner))
    fuzzy_sorted = sorted(fuzzy, key=lambda m: (-m.score,
                                                m.show.date or "9999"))
    exact_html = ("\n".join(render_match(m) for m in exact_sorted)
                  or '<p class="empty">No exact matches right now.</p>')
    fuzzy_html = ("\n".join(render_match(m) for m in fuzzy_sorted)
                  or '<p class="empty">No fuzzy matches.</p>')
    out_path.write_text(HTML_TEMPLATE.format(
        generated=dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        n_loved=n_loved, n_shows=n_shows,
        n_exact=len(exact_sorted), n_fuzzy=len(fuzzy_sorted),
        exact_html=exact_html, fuzzy_html=fuzzy_html,
    ))


# -------- main --------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true",
                    help="Re-fetch Fantano loved lists even if cached")
    ap.add_argument("--max-pages", type=int, default=30,
                    help="Max Songkick pages to scrape (default 30 ≈ 750 shows)")
    args = ap.parse_args()

    fetcher = Fetcher()
    try:
        print("\n[1/3] Gathering Fantano-loved artists…")
        loved = gather_loved(fetcher, refresh=args.refresh)
        print(f"    → {len(loved)} unique loved artists")

        print("\n[2/3] Scraping Songkick DC metro calendar…")
        shows = scrape_songkick_metro(fetcher, max_pages=args.max_pages)
        print(f"    → {len(shows)} shows total")

        print("\n[3/3] Matching…")
        exact, fuzzy = match_shows(shows, loved)
        print(f"    → {len(exact)} exact, {len(fuzzy)} fuzzy")

        out = HERE / "dc_shows.html"
        write_html(exact, fuzzy, n_loved=len(loved), n_shows=len(shows),
                   out_path=out)
        print(f"\nWrote {out}. Open it in your browser.")
    finally:
        fetcher.close()

if __name__ == "__main__":
    main()
