#!/usr/bin/env python3
"""
jf_alt_titles.py - make anime findable in Jellyfin by ANY of its names.

Jellyfin 12.x built-in search (SqlSearchProvider) matches:
    CleanName CONTAINS term   OR   OriginalTitle LIKE %term%
So we store every useful alias (English, romaji, native Japanese, common
short names like "AoT", "Oregairu") in OriginalTitle, joined by " · ".
Searching "Attack on Titan", "Shingeki", "進撃" or "AoT" then all hit.

Sources
  * ID bridge : Fribb/anime-lists  (AniDB/TVDB/TMDB/IMDb/MAL -> AniList id)
  * Titles    : AniList GraphQL     (romaji, english, native, synonyms)
  * Fallback  : AniList search by the item's Name (only inside anime libraries)

Safe by design
  * Only touches Series/Movies that map to a known anime (or live in a library
    whose name contains "anime").
  * Only OriginalTitle changes; the full item DTO is round-tripped.
  * Idempotent - re-running changes nothing unless AniList data changed.
  * --dry-run shows what would change.

Stdlib only (runs on the TrueNAS host's python3).

Env / flags
  JELLYFIN_URL      default http://127.0.0.1:8096
  JELLYFIN_API_KEY  required (Dashboard -> API Keys)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SEP = " · "
FRIBB_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
ANILIST_URL = "https://graphql.anilist.co"
UA = "jf-alt-titles/1.0 (+self-hosted Jellyfin)"
MAX_SYNONYMS = 8
MAX_ALIAS_LEN = 80

# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def http_json(url, *, method="GET", data=None, headers=None, timeout=60, retries=4):
    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:  # AniList rate limit
                wait = int(e.headers.get("Retry-After", "60"))
                log(f"  rate-limited, sleeping {wait}s")
                time.sleep(wait + 1)
                continue
            if e.code >= 500 and attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise


_LOG_FN = None


def log(msg):
    if _LOG_FN:
        _LOG_FN(msg)
    else:
        print(msg, flush=True)


# --------------------------------------------------------------------------- #
# Jellyfin
# --------------------------------------------------------------------------- #

class Jellyfin:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.h = {"Authorization": f'MediaBrowser Token="{key}"'}

    def get(self, path, **params):
        q = ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
        return http_json(self.url + path + q, headers=self.h)

    def post(self, path, data):
        return http_json(self.url + path, method="POST", data=data, headers=self.h)

    def libraries(self):
        return self.get("/Library/VirtualFolders") or []

    def items(self, parent_id=None):
        params = dict(
            Recursive="true",
            IncludeItemTypes="Series,Movie",
            Fields="ProviderIds,OriginalTitle,Path",
            EnableImages="false",
            EnableUserData="false",
        )
        if parent_id:
            params["ParentId"] = parent_id
        return (self.get("/Items", **params) or {}).get("Items", [])

    def any_user_id(self):
        users = self.get("/Users") or []
        admins = [u for u in users if u.get("Policy", {}).get("IsAdministrator")]
        return (admins or users)[0]["Id"]

    def full_item(self, item_id, user_id):
        return self.get(f"/Items/{item_id}", userId=user_id)

    def set_original_title(self, item_id, user_id, value):
        dto = self.full_item(item_id, user_id)
        dto["OriginalTitle"] = value
        self.post(f"/Items/{item_id}", dto)


# --------------------------------------------------------------------------- #
# ID mapping (Fribb anime-lists)
# --------------------------------------------------------------------------- #

def load_fribb(cache: Path, max_age_days=7):
    if not cache.exists() or time.time() - cache.stat().st_mtime > max_age_days * 86400:
        log("Downloading Fribb anime-lists mapping ...")
        try:
            req = urllib.request.Request(FRIBB_URL, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(raw)
        except Exception as e:  # keep using a stale cache if we have one
            if not cache.exists():
                raise
            log(f"  download failed ({e}); using cached copy")
    data = json.loads(cache.read_text(encoding="utf-8"))

    m = {"anidb": {}, "mal": {}, "tvdb": {}, "tmdb_tv": {}, "tmdb_movie": {}, "imdb": {}}
    first_seen = set()
    for e in data:
        al = e.get("anilist_id")
        if not al:
            continue
        if e.get("anidb_id"):
            m["anidb"].setdefault(str(e["anidb_id"]), al)
        if e.get("mal_id"):
            m["mal"].setdefault(str(e["mal_id"]), al)
        # For TVDB/TMDB one show id covers many AniList entries (one per
        # season/cour). Prefer the entry that is season 1 / a TV type.
        tv_first = (e.get("season") or {}).get("tvdb") in (None, 1) and e.get("type") in ("TV", "ONA", None)

        def put(table, key):
            # first season-1 TV entry wins; anything else only fills a gap
            tag = (id(table), key)
            if key not in table or (tv_first and tag not in first_seen):
                table[key] = al
                if tv_first:
                    first_seen.add(tag)

        if e.get("tvdb_id"):
            put(m["tvdb"], str(e["tvdb_id"]))
        tmdb = e.get("themoviedb_id")
        if isinstance(tmdb, dict):
            if tmdb.get("tv"):
                put(m["tmdb_tv"], str(tmdb["tv"]))
            if tmdb.get("movie"):
                m["tmdb_movie"].setdefault(str(tmdb["movie"]), al)
        for imdb in (e.get("imdb_id") or []) if isinstance(e.get("imdb_id"), list) else [e.get("imdb_id")]:
            if imdb:
                m["imdb"].setdefault(imdb, al)
    return m


def anilist_id_for(item, fribb):
    p = {k.lower(): str(v) for k, v in (item.get("ProviderIds") or {}).items() if v}
    if p.get("anilist"):
        return int(p["anilist"]), "anilist"
    order = [("anidb", "anidb"), ("mal", "mal"), ("myanimelist", "mal")]
    if item.get("Type") == "Movie":
        order += [("tmdb", "tmdb_movie"), ("imdb", "imdb"), ("tvdb", "tvdb")]
    else:
        order += [("tvdb", "tvdb"), ("tmdb", "tmdb_tv"), ("imdb", "imdb")]
    for pk, mk in order:
        v = p.get(pk)
        if v and v in fribb[mk]:
            return int(fribb[mk][v]), pk
    return None, None


# --------------------------------------------------------------------------- #
# AniList
# --------------------------------------------------------------------------- #

Q_BY_IDS = """query($ids:[Int],$page:Int){Page(page:$page,perPage:50){
  media(id_in:$ids,type:ANIME){id title{romaji english native} synonyms}}}"""
Q_SEARCH = """query($s:String){Media(search:$s,type:ANIME){
  id title{romaji english native} synonyms}}"""


def anilist(query, variables):
    time.sleep(2.1)  # stay under AniList's ~30 req/min degraded limit
    res = http_json(ANILIST_URL, method="POST", data={"query": query, "variables": variables})
    if res.get("errors"):
        raise RuntimeError(res["errors"])
    return res["data"]


def anilist_fetch(ids):
    out = {}
    ids = sorted(set(ids))
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        for m in anilist(Q_BY_IDS, {"ids": chunk, "page": 1})["Page"]["media"]:
            out[m["id"]] = m
        log(f"  AniList: {min(i + 50, len(ids))}/{len(ids)}")
    return out


def anilist_search(name):
    try:
        return anilist(Q_SEARCH, {"s": name})["Media"]
    except Exception:
        return None


def looks_like_anime(it):
    p = {k.lower() for k, v in (it.get("ProviderIds") or {}).items() if v}
    ot = it.get("OriginalTitle") or ""
    return it.get("_anime_lib") or "anidb" in p or "anilist" in p or bool(_CJK.search(ot.split(SEP)[0]))


def search_verified(it):
    """Search AniList by native title, then by Name; accept only if a title matches exactly."""
    ot = (it.get("OriginalTitle") or "").split(SEP)[0].strip()
    wanted = {norm(it["Name"])} | ({norm(ot)} if ot else set())
    for term in [ot, it["Name"]]:
        if not term:
            continue
        m = anilist_search(term)
        if not m:
            continue
        t = m.get("title") or {}
        names = {norm(x) for x in [t.get("romaji"), t.get("english"), t.get("native"), *(m.get("synonyms") or [])] if x}
        if wanted & names:
            return m
    return None


# --------------------------------------------------------------------------- #
# Alias building
# --------------------------------------------------------------------------- #

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


def fold(s):
    """strip diacritics (Jellyfin strips them from the search term)."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s):
    s = fold(s).casefold().replace("’", "'")
    return re.sub(r"[\s\W_]+", " ", s).strip()


def acceptable(s):
    """Keep Latin-script (English/romaji) and Japanese-script aliases only."""
    if not s or len(s) > MAX_ALIAS_LEN:
        return False
    if _CJK.search(s):
        # Japanese/Chinese: allow CJK + kana + latin + punctuation, nothing else
        return all(_CJK.match(c) or ord(c) < 0x250 or not c.isalpha() for c in s)
    # Latin-ish: every letter must be in Latin / Latin-extended blocks
    return all(ord(c) < 0x250 or not c.isalpha() for c in s)


def build_aliases(name, provider_original, media):
    t = media.get("title") or {}
    primary = [provider_original, t.get("native"), t.get("romaji"), t.get("english")]
    syns = [s for s in (media.get("synonyms") or []) if acceptable(s)][:MAX_SYNONYMS]

    kept, keys = [], []
    name_key = norm(name or "")
    for cand in primary + syns:
        if not cand or not acceptable(cand):
            continue
        k = norm(cand)
        if not k or k == name_key:
            continue
        # skip if an already-covered string is a substring (LIKE %x% hits it anyway)
        if any(prev and prev in k for prev in keys + [name_key]):
            continue
        kept.append(cand.strip())
        keys.append(k)

    # Jellyfin removes diacritics from the typed term, but not from OriginalTitle
    extra = []
    for a in kept:
        f = fold(a)
        if f != a and not _CJK.search(a):
            extra.append(f)
    return kept + extra


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def run(url, api_key, *, dry_run=False, libraries=None, cache=None, only_new=False, log_fn=None):
    """Alias pass over the library. Returns (changed, up_to_date).

    only_new=True skips items that already carry our alias list, so a
    post-download run only costs AniList calls for newly added shows.
    """
    global _LOG_FN
    if log_fn:
        _LOG_FN = log_fn
    cache = Path(cache or os.environ.get("ALT_TITLES_CACHE")
                 or Path(tempfile.gettempdir()) / "jf-alt-titles-fribb.json")

    jf = Jellyfin(url, api_key)
    fribb = load_fribb(cache)
    user_id = jf.any_user_id()

    libs = [l for l in jf.libraries() if l.get("CollectionType") in ("tvshows", "movies", None, "mixed")]
    if libraries:
        wanted = {n.casefold() for n in libraries}
        libs = [l for l in libs if l["Name"].casefold() in wanted]

    items = {}
    for lib in libs:
        anime_lib = "anime" in lib["Name"].casefold()
        for it in jf.items(lib["ItemId"]):
            if only_new and SEP in (it.get("OriginalTitle") or ""):
                continue
            it["_anime_lib"] = anime_lib
            items[it["Id"]] = it
    log(f"{len(items)} {'new ' if only_new else ''}series/movies in {len(libs)} libraries")

    matched, unmatched = {}, []
    for it in items.values():
        al, _via = anilist_id_for(it, fribb)
        if al:
            matched[it["Id"]] = al
        elif looks_like_anime(it):
            unmatched.append(it)

    log(f"{len(matched)} mapped to AniList by id; {len(unmatched)} look like anime -> verified name search")
    media = anilist_fetch(matched.values()) if matched else {}

    for it in unmatched:
        m = search_verified(it)
        if m:
            media[m["id"]] = m
            matched[it["Id"]] = m["id"]
            log(f"  name-search: {it['Name']!r} -> {m['title'].get('romaji')!r}")
        else:
            log(f"  no confident AniList match: {it['Name']!r} (skipped)")

    changed = skipped = 0
    for iid, al in matched.items():
        it, m = items[iid], media.get(al)
        if not m:
            continue
        cur = it.get("OriginalTitle") or ""
        provider_original = (cur.split(SEP)[0].strip() if SEP in cur else cur) or None  # first segment = provider original
        aliases = build_aliases(it["Name"], provider_original, m)
        new = SEP.join(aliases)
        if new == cur or (not new and not cur):
            skipped += 1
            continue
        log(f"{'[dry] ' if dry_run else ''}{it['Name']}\n    was: {cur or '-'}\n    now: {new or '-'}")
        if not dry_run:
            try:
                jf.set_original_title(iid, user_id, new or None)
            except Exception as e:
                log(f"    ! update failed: {e}")
                continue
        changed += 1

    log(f"Done: {changed} {'would change' if dry_run else 'updated'}, {skipped} already up to date.")
    return changed, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096"))
    ap.add_argument("--api-key", default=os.environ.get("JELLYFIN_API_KEY"))
    ap.add_argument("--libraries", nargs="*",
                    help="library names to process (default: all; name-search fallback only for items that look like anime)")
    ap.add_argument("--cache", default=str(Path(__file__).with_name(".cache") / "fribb.json"))
    ap.add_argument("--only-new", action="store_true", help="skip items that already have aliases")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.api_key:
        sys.exit("JELLYFIN_API_KEY missing")
    run(args.url, args.api_key, dry_run=args.dry_run, libraries=args.libraries,
        cache=args.cache, only_new=args.only_new)


if __name__ == "__main__":
    main()
