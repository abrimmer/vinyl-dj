"""Nightly update: Discogs collection -> track lengths, BPM, Camelot key -> data/collection.json

Runs on GitHub Actions. Keys come from GitHub Secrets (DISCOGS_TOKEN, GETSONGBPM_KEY).
Your own edits live in data/edits.json and are never touched by this script.
BPM & key data: GetSongBPM.com
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

USERNAME = os.environ.get("DISCOGS_USER", "abrimmer")
DISCOGS = "https://api.discogs.com"
GSB = "https://api.getsong.co"
UA = "VinylDJ/1.0 +https://github.com/abrimmer/vinyl-dj"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data", "collection.json")
RECORDS = os.path.join(ROOT, "records.txt")
STARTED = time.time()
LOOKUP_V = 3  # bump when matching improves, so missing tracks get retried once
MAX_NEW_LOOKUPS = int(os.environ.get("MAX_NEW_LOOKUPS", "1500"))  # stay well under GetSongBPM's 3000/hour

MINOR = {"Ab": 1, "G#": 1, "Eb": 2, "D#": 2, "Bb": 3, "A#": 3, "F": 4, "C": 5, "G": 6,
         "D": 7, "A": 8, "E": 9, "B": 10, "F#": 11, "Gb": 11, "Db": 12, "C#": 12}
MAJOR = {"B": 1, "Cb": 1, "F#": 2, "Gb": 2, "Db": 3, "C#": 3, "Ab": 4, "G#": 4, "Eb": 5,
         "D#": 5, "Bb": 6, "A#": 6, "F": 7, "C": 8, "G": 9, "D": 10, "A": 11, "E": 12}


def camelot(open_key="", key_of=""):
    m = re.fullmatch(r"(\d{1,2})([md])", (open_key or "").strip())
    if m:
        return f"{(int(m.group(1)) + 6) % 12 + 1}{'A' if m.group(2) == 'm' else 'B'}"
    k = (key_of or "").replace("♯", "#").replace("♭", "b").strip()
    if not k:
        return ""
    if k.endswith("m"):
        n = MINOR.get(k[:-1])
        return f"{n}A" if n else ""
    n = MAJOR.get(k)
    return f"{n}B" if n else ""


def norm(s):
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s or "").lower()
    return re.sub(r"[^a-z0-9]", "", s)


def plain(s):
    """Drop accents and bracketed extras: 'Durdu Dünya (Extended)' -> 'Durdu Dunya'."""
    import unicodedata
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s or "")
    s = s.replace("ı", "i").replace("ş", "s").replace("ğ", "g")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().strip()


def clean_artist(name):
    return re.sub(r"\s*\(\d+\)$", "", name or "").replace("*", "").strip()


def secs(t):
    try:
        m, s = (t or "").split(":")[-2:]
        return int(m) * 60 + int(s)
    except ValueError:
        return None


def mmss(n):
    return f"{n // 60}:{n % 60:02d}"


class Api:
    def __init__(self, dtoken, gkey):
        self.d = requests.Session()
        self.d.headers.update({"Authorization": f"Discogs token={dtoken}", "User-Agent": UA})
        self.g = requests.Session()
        self.g.headers.update({"User-Agent": UA, "X-API-KEY": gkey})
        self.gkey = gkey
        self.lookups = 0

    def discogs(self, path, **params):
        for _ in range(5):
            r = self.d.get(f"{DISCOGS}{path}", params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(30)
                continue
            r.raise_for_status()
            time.sleep(1.1)  # 60 requests/minute limit
            return r.json()
        raise RuntimeError(f"Discogs kept rate-limiting {path}")

    def gsb(self, path, **params):
        self.lookups += 1
        r = self.g.get(f"{GSB}{path}", params={"api_key": self.gkey, **params}, timeout=30)
        time.sleep(0.4)
        if r.status_code == 401:
            sys.exit("GetSongBPM key rejected (401). Check the GETSONGBPM_KEY secret.")
        try:
            return r.json()
        except ValueError:
            return {}


def lookup(api, title, artist):
    """Return dict with bpm, camelot, exact (bool) — or found=False."""
    t_plain, a_plain = plain(title), plain(artist)
    short_artist = re.sub(r"^the ", "", re.split(r" & | and ", a_plain, flags=re.I)[0], flags=re.I)
    # Try several ways: full search, simplified names, then title only filtered by artist
    attempts = [("both", f"song:{title} artist:{artist}"),
                ("both", f"song:{t_plain} artist:{short_artist}"),
                ("song", t_plain)]
    best, exact, tried = None, False, []
    for kind, q in attempts:
        hits = api.gsb("/search/", type=kind, lookup=q, limit=10).get("search")
        if not isinstance(hits, list) or not hits:
            tried.append({"search": q, "found": []})
            continue
        tried.append({"search": q, "found": [f"{h.get('title')} - {(h.get('artist') or {}).get('name')}"
                                             for h in hits[:3]]})
        for h in hits:
            ha = norm(plain(clean_artist((h.get("artist") or {}).get("name"))))
            if norm(plain(h.get("title"))) == norm(t_plain) and \
                    (ha == norm(a_plain) or norm(short_artist) in ha):
                best, exact = h, True
                break
        if exact:
            break
        if best is None and kind == "both":
            best = hits[0]
    if best is None:
        return {"found": False, "tried": tried}
    tempo, ok, ko = best.get("tempo"), best.get("open_key"), best.get("key_of")
    if not tempo or not (ok or ko):
        song = api.gsb("/song/", id=best.get("id")).get("song") or {}
        tempo, ok, ko = song.get("tempo", tempo), song.get("open_key", ok), song.get("key_of", ko)
    try:
        bpm = round(float(tempo)) if tempo else None
    except (TypeError, ValueError):
        bpm = None
    return {"found": bool(bpm), "bpm": bpm, "camelot": camelot(ok, ko), "exact": exact,
            "gsb_title": best.get("title"), "tried": tried}


def master_title(api, cache, mid, track):
    """Title of the matching track on the original album: same title minus extras, or same position."""
    if mid not in cache:
        try:
            cache[mid] = [x for x in api.discogs(f"/masters/{mid}").get("tracklist", [])
                          if x.get("type_", "track") == "track"]
        except requests.HTTPError:
            cache[mid] = []
    mine = norm(plain(track["title"]))
    for x in cache[mid]:
        theirs = norm(plain(x.get("title")))
        if theirs and (theirs in mine or mine in theirs):
            return plain(x.get("title"))
    for x in cache[mid]:
        if x.get("position") == track.get("pos"):
            return plain(x.get("title"))
    return None


def wanted_ids():
    ids, everything = set(), False
    with open(RECORDS, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if line.upper() == "ALL":
                everything = True
            elif line.isdigit():
                ids.add(int(line))
    return ids, everything


def main():
    dtoken, gkey = os.environ.get("DISCOGS_TOKEN"), os.environ.get("GETSONGBPM_KEY")
    if not dtoken or not gkey:
        sys.exit("Missing DISCOGS_TOKEN or GETSONGBPM_KEY (add them in GitHub > Settings > Secrets).")
    api = Api(dtoken, gkey)

    old = {}
    if os.path.exists(DATA):
        with open(DATA, encoding="utf-8") as fh:
            old = {r["id"]: r for r in json.load(fh).get("releases", [])}

    ids, everything = wanted_ids()

    # 1. Collection (folder 0 = all)
    coll, page = [], 1
    while True:
        res = api.discogs(f"/users/{USERNAME}/collection/folders/0/releases", page=page, per_page=100)
        coll += res.get("releases", [])
        if page >= res.get("pagination", {}).get("pages", 1):
            break
        page += 1
    folders = {f["id"]: f["name"] for f in api.discogs(f"/users/{USERNAME}/collection/folders")["folders"]}
    print(f"Collection: {len(coll)} items")

    releases = []
    for item in coll:
        rid = item["id"]
        if not everything and rid not in ids:
            continue
        b = item["basic_information"]
        rel = old.get(rid) or {}
        rel.update({
            "id": rid,
            "title": b.get("title"),
            "artist": clean_artist(b["artists"][0]["name"]) if b.get("artists") else "",
            "year": b.get("year") or None,
            "genres": b.get("genres", []),
            "styles": b.get("styles", []),
            "label": (b.get("labels") or [{}])[0].get("name", ""),
            "format": ", ".join(f.get("name", "") for f in b.get("formats", [])),
            "cover": b.get("cover_image"),
            "thumb": b.get("thumb"),
            "folder": folders.get(item.get("folder_id"), ""),
            "added": item.get("date_added"),
            "url": f"https://www.discogs.com/release/{rid}",
            "master_id": b.get("master_id") or None,
        })
        # Original release year (from the master release), fetched once
        if "orig_year" not in rel:
            mid = b.get("master_id")
            if mid:
                try:
                    rel["orig_year"] = api.discogs(f"/masters/{mid}").get("year") or rel["year"]
                except requests.HTTPError:
                    rel["orig_year"] = rel["year"]
            else:
                rel["orig_year"] = rel["year"]
        # 2. Tracklist (once per release)
        if not rel.get("tracks"):
            full = api.discogs(f"/releases/{rid}")
            rel["tracks"] = []
            for t in full.get("tracklist", []):
                if t.get("type_", "track") != "track":
                    continue
                n = secs(t.get("duration"))
                rel["tracks"].append({
                    "pos": t.get("position", ""),
                    "title": t.get("title", ""),
                    "artist": clean_artist(t["artists"][0]["name"]) if t.get("artists") else rel["artist"],
                    "duration": t.get("duration", ""),
                    "cue_in": "0:00*",
                    "cue_out": (mmss(max(n - 20, 0)) + "*") if n else "",
                })
        releases.append(rel)

    # 3. BPM + key (once per track, capped per run; the rest happens on the next run)
    pending, masters = 0, {}
    for rel in releases:
        for t in rel["tracks"]:
            # Skip tracks already looked up; retry misses once each time the matching improves
            if "looked_up" in t and (t.get("bpm") or t.get("lookup_v", 1) >= LOOKUP_V):
                continue
            if api.lookups >= MAX_NEW_LOOKUPS or time.time() - STARTED > 85 * 60:  # save before the 2h job limit
                pending += 1
                continue
            info = lookup(api, t["title"], t["artist"])
            # Fallback: try the track title as it appears on the original album (Discogs master)
            if not info["found"] and rel.get("master_id"):
                alt = master_title(api, masters, rel["master_id"], t)
                if alt and norm(alt) != norm(t["title"]):
                    alt_info = lookup(api, alt, t["artist"])
                    alt_info["tried"] = info["tried"] + alt_info["tried"]
                    info = alt_info
                    if info["found"]:
                        info["exact"] = False
                        t["matched_as"] = alt
            t["looked_up"] = datetime.now(timezone.utc).date().isoformat()
            t["lookup_v"] = LOOKUP_V
            if info["found"]:
                star = "" if info["exact"] else "*"
                t["bpm"] = f"{info['bpm']}{star}"
                t["camelot"] = f"{info['camelot']}{star}" if info["camelot"] else ""
                t.pop("tried", None)
            else:
                t["bpm"], t["camelot"] = "", ""
                t["tried"] = info["tried"]  # shown on the site's "Missing data" page
            print(f"  {rel['artist']} - {t['title']}: {t.get('bpm') or '-'} {t.get('camelot') or ''}")

    releases.sort(key=lambda r: (r["artist"].lower(), r.get("year") or 0))
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA, "w", encoding="utf-8") as fh:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                   "source": "Discogs + GetSongBPM.com", "releases": releases},
                  fh, ensure_ascii=False, indent=1)
    print(f"Saved {len(releases)} records. Lookups this run: {api.lookups}. Tracks still pending: {pending}.")


if __name__ == "__main__":
    main()
