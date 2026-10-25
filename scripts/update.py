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
LOOKUP_V = 4
FB_V = 1
DZ_V = 1
RB_V = 1  # bump when the fallback sources change, so misses get retried  # bump when matching improves, so missing tracks get retried once
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
    return re.sub(r"[\W_]", "", s)  # keeps letters of any script (e.g. Cyrillic)


def plain(s):
    """Drop accents and bracketed extras: 'Durdu Dünya (Extended)' -> 'Durdu Dunya'. Other scripts are kept."""
    import unicodedata
    s = re.sub(r"\(.*?\)|\[.*?\]", "", s or "")
    s = s.replace("ı", "i")
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).strip()


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


class StopLookups(Exception):
    pass


LOG = []


def log(msg):
    print(msg)
    LOG.append(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}")


def save(releases, note=""):
    releases = sorted(releases, key=lambda r: (r["artist"].lower(), r.get("year") or 0))
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="minutes")
    try:  # keep the old timestamp when nothing changed, so quiet runs don't create a commit
        with open(DATA, encoding="utf-8") as fh:
            prev = json.load(fh)
        if prev.get("releases") == json.loads(json.dumps(releases, ensure_ascii=False)):
            stamp = prev.get("updated", stamp)
    except (OSError, ValueError):
        pass
    with open(DATA, "w", encoding="utf-8") as fh:
        json.dump({"updated": stamp,
                   "source": "Discogs + GetSongBPM.com", "releases": releases},
                  fh, ensure_ascii=False, indent=1)
    with open(os.path.join(ROOT, "data", "last_run.txt"), "w", encoding="utf-8") as fh:
        fh.write(note + "\n" + "\n".join(LOG[-80:]) + "\n")


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
        if r.status_code in (401, 429):
            raise StopLookups(f"GetSongBPM said {r.status_code}: {r.text[:120]} (hourly limit or key problem)")
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
    # "Закладка = The Stash": try each side of the "=" (original script / English)
    if " = " in title or " = " in artist:
        for tv in [x.strip() for x in title.split(" = ")]:
            for av in [x.strip() for x in artist.split(" = ")]:
                attempts.insert(-1, ("both", f"song:{plain(tv) or tv} artist:{plain(av) or av}"))
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
            titles = {norm(plain(x)) for x in [title, *title.split(" = ")]} | {norm(t_plain)}
            if norm(plain(h.get("title"))) in titles and \
                    ha and (ha == norm(a_plain) or (norm(short_artist) and norm(short_artist) in ha)
                            or any(norm(plain(x)) == ha for x in artist.split(" = "))):
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


# ---------- fallbacks for tracks GetSongBPM doesn't know ----------
FB_UA = "Setwright/1.0 ( https://github.com/abrimmer/vinyl-dj )"


class Fallback:
    """MusicBrainz -> AcousticBrainz (BPM + key, measured from audio), then Deezer (BPM only)."""
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": FB_UA, "Accept": "application/json"})
        self.last_mb = 0
        self.dz_logged = 0

    def get(self, url, **params):
        for _ in range(3):
            try:
                r = self.s.get(url, params=params, timeout=30)
            except requests.RequestException:
                time.sleep(5); continue
            if r.status_code in (429, 503):
                time.sleep(int(r.headers.get("X-RateLimit-Reset-In", 0) or 5) + 1); continue
            try:
                return r.json() if r.ok else {}
            except ValueError:
                return {}
        return {}

    def mb_ids(self, title, artist):
        ids = []
        q_esc = lambda x: x.replace("\\", "").replace('"', "")
        for tv in dict.fromkeys([title, *[x.strip() for x in title.split(" = ")]]):
            for av in dict.fromkeys([artist, *[x.strip() for x in artist.split(" = ")]]):
                wait = 1.1 - (time.time() - self.last_mb)
                if wait > 0:
                    time.sleep(wait)  # MusicBrainz allows 1 request per second
                self.last_mb = time.time()
                res = self.get("https://musicbrainz.org/ws/2/recording",
                               query=f'recording:"{q_esc(tv)}" AND artist:"{q_esc(av)}"', fmt="json", limit=10)
                ids += [r["id"] for r in res.get("recordings", []) if int(r.get("score", 0)) >= 85]
                if ids:
                    return list(dict.fromkeys(ids))[:25]
        return []

    def acousticbrainz(self, ids):
        if not ids:
            return None
        res = self.get("https://acousticbrainz.org/api/v1/low-level", recording_ids=";".join(ids),
                       features="rhythm.bpm;tonal.key_key;tonal.key_scale")
        time.sleep(1)
        for mbid in ids:  # keep MusicBrainz's ranking
            for doc in (res.get(mbid) or {}).values():
                bpm = ((doc or {}).get("rhythm") or {}).get("bpm")
                if bpm:
                    tonal = doc.get("tonal") or {}
                    k = tonal.get("key_key") or ""
                    key_of = (k + ("m" if tonal.get("key_scale") == "minor" else "")) if k else ""
                    return {"bpm": round(float(bpm)), "camelot": camelot("", key_of)}
        return None

    def deezer(self, title, artist):
        for tv in dict.fromkeys([title, *[x.strip() for x in title.split(" = ")]]):
            res = self.get("https://api.deezer.com/search", q=f'artist:"{artist.split(" = ")[0]}" track:"{tv}"', limit=5)
            if not res.get("data"):  # fall back to a plain text search
                res = self.get("https://api.deezer.com/search", q=f'{artist.split(" = ")[0]} {tv}', limit=5)
            time.sleep(0.2)
            if self.dz_logged < 5:
                self.dz_logged += 1
                log(f"Deezer check '{artist} - {tv}': {str(res)[:300]}")
            for hit in res.get("data", []) or []:
                ha = norm(plain((hit.get("artist") or {}).get("name")))
                mine = [norm(plain(x)) for x in [artist, *artist.split(" = ")]]
                if ha and any(m and (m == ha or m in ha or ha in m) for m in mine):
                    tr = self.get(f"https://api.deezer.com/track/{hit['id']}")
                    time.sleep(0.2)
                    if self.dz_logged < 8:
                        self.dz_logged += 1
                        log(f"Deezer track {hit['id']} bpm={tr.get('bpm')} error={tr.get('error')}")
                    if tr.get("bpm") and float(tr["bpm"]) > 0:
                        return {"bpm": round(float(tr["bpm"])), "camelot": ""}
                    break
        return None


PITCH = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


class Recco:
    """Spotify search (to get a track ID) -> ReccoBeats audio features (tempo, key, mode)."""
    def __init__(self, cid, secret):
        self.s = requests.Session(); self.s.headers.update({"User-Agent": FB_UA})
        r = self.s.post("https://accounts.spotify.com/api/token", data={"grant_type": "client_credentials"},
                        auth=(cid, secret), timeout=30)
        r.raise_for_status()
        self.token, self.logged = r.json()["access_token"], 0

    def spotify_id(self, title, artist):
        for tv in dict.fromkeys([title, *[x.strip() for x in title.split(" = ")]]):
            for av in dict.fromkeys([artist, *[x.strip() for x in artist.split(" = ")]]):
                for _ in range(3):
                    r = self.s.get("https://api.spotify.com/v1/search", timeout=30,
                                   headers={"Authorization": f"Bearer {self.token}"},
                                   params={"q": f'track:"{tv}" artist:"{av}"', "type": "track", "limit": 5})
                    if r.status_code == 429:
                        time.sleep(int(r.headers.get("Retry-After", 5)) + 1); continue
                    break
                time.sleep(0.15)
                if not r.ok:
                    if self.logged < 5:
                        self.logged += 1; log(f"Spotify search {r.status_code}: {r.text[:200]}")
                    continue
                mine = [norm(plain(x)) for x in [artist, *artist.split(" = ")]]
                for it in r.json().get("tracks", {}).get("items", []):
                    names = [norm(plain(a.get("name"))) for a in it.get("artists", [])]
                    if any(m and (m == n or m in n or n in m) for m in mine for n in names if n):
                        return it["id"]
        return None

    def features(self, ids):
        """Return {spotify_id: (bpm, camelot)} for up to 40 ids."""
        out = {}
        r = self.s.get("https://api.reccobeats.com/v1/audio-features", params={"ids": ",".join(ids)}, timeout=30)
        time.sleep(0.5)
        try:
            j = r.json()
        except ValueError:
            j = {}
        items = j.get("content") or j.get("audio_features") or (j if isinstance(j, list) else [])
        if self.logged < 8:
            self.logged += 1; log(f"ReccoBeats {r.status_code}: {str(j)[:300]}")
        for i, it in enumerate(items or []):
            if not it:
                continue
            sid = next((x for x in ids if x in str(it.get("href", "")) or x == it.get("id")), None)
            if sid is None and len(items) == len(ids):
                sid = ids[i]
            tempo = it.get("tempo")
            if not sid or not tempo:
                continue
            key, mode = it.get("key"), it.get("mode")
            cam = ""
            if isinstance(key, int) and 0 <= key < 12:
                cam = camelot("", PITCH[key] + ("m" if mode == 0 else ""))
            out[sid] = (round(float(tempo)), cam)
        return out


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


def build_release(api, item, old, folders):
    rid = item["id"]
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
    return rel


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
    log(f"Collection: {len(coll)} items")

    releases, seen = [], set()
    for item in coll:
        rid = item["id"]
        if (not everything and rid not in ids) or rid in seen:
            continue
        seen.add(rid)
        try:
            releases.append(build_release(api, item, old, folders))
        except Exception as e:  # one bad record shouldn't stop the rest
            log(f"Skipped release {rid}: {e}")
            if rid in old:
                releases.append(old[rid])
    log(f"Records: {len(releases)}")
    save(releases, "Records saved, starting BPM lookups")

    # 3. BPM + key (once per track, capped per run; the rest happens on the next run)
    pending, masters, stop, last_save = 0, {}, False, 0
    for rel in releases:
        if stop:
            break
        if api.lookups - last_save >= 300:  # save progress as we go
            save(releases, "In progress"); last_save = api.lookups
        for t in rel["tracks"]:
            # Skip tracks already looked up; retry misses once each time the matching improves
            if "looked_up" in t and (t.get("bpm") or t.get("lookup_v", 1) >= LOOKUP_V):
                continue
            if api.lookups >= MAX_NEW_LOOKUPS or time.time() - STARTED > 85 * 60:  # save before the 2h job limit
                pending += 1
                continue
            try:
                info = lookup(api, t["title"], t["artist"])
            except StopLookups as e:
                log(str(e)); stop = True; break
            except Exception as e:
                log(f"Lookup error for {rel['artist']} - {t['title']}: {e}"); continue
            # Fallback: try the track title as it appears on the original album (Discogs master)
            if not info["found"] and rel.get("master_id"):
                alt = master_title(api, masters, rel["master_id"], t)
                if alt and norm(alt) != norm(t["title"]):
                    try:
                        alt_info = lookup(api, alt, t["artist"])
                    except StopLookups as e:
                        log(str(e)); stop = True; break
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

    # 4. Fallbacks for tracks GetSongBPM didn't have (once per track, within the time budget)
    if not stop:
        fb, done = Fallback(), 0
        try:
            for rel in releases:
                for t in rel["tracks"]:
                    if t.get("bpm") or t.get("fb_v") == FB_V or t.get("lookup_v", 1) < LOOKUP_V:
                        continue
                    if time.time() - STARTED > 85 * 60:
                        raise StopLookups("Time budget used; fallbacks continue next run")
                    found, src = None, ""
                    ab = fb.acousticbrainz(fb.mb_ids(t["title"], t["artist"]))
                    if ab:
                        found, src = ab, "AcousticBrainz"
                    t["fb_v"] = FB_V
                    if found:
                        t["bpm"] = f"{found['bpm']}*"
                        t["camelot"] = f"{found['camelot']}*" if found["camelot"] else ""
                        t["source"] = src
                        t.pop("tried", None)
                    done += 1
                    if done % 150 == 0:
                        save(releases, "In progress (fallbacks)")
        except StopLookups as e:
            log(str(e))
        log(f"Fallback checks this run: {done}")
        # Deezer pass (BPM only) for anything still missing
        dz_done = dz_found = 0
        try:
            for rel in releases:
                for t in rel["tracks"]:
                    if t.get("bpm") or t.get("dz_v") == DZ_V or t.get("fb_v") != FB_V:
                        continue
                    if time.time() - STARTED > 85 * 60:
                        raise StopLookups("Time budget used; Deezer checks continue next run")
                    dz = fb.deezer(t["title"], t["artist"])
                    t["dz_v"] = DZ_V; dz_done += 1
                    if dz:
                        t["bpm"], t["camelot"], t["source"] = f"{dz['bpm']}*", "", "Deezer"; dz_found += 1
        except StopLookups as e:
            log(str(e))
        log(f"Deezer checks this run: {dz_done}, found: {dz_found}")

    # 5. ReccoBeats via Spotify IDs: fills tracks still missing, and adds keys to BPM-only (Deezer) tracks
    cid, csec = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not stop and cid and csec:
        rb_done = rb_found = 0
        try:
            rc = Recco(cid, csec)
            todo = [t for rel in releases for t in rel["tracks"]
                    if (not t.get("bpm") or not t.get("camelot")) and t.get("rb_v") != RB_V and t.get("fb_v") == FB_V]
            for i in range(0, len(todo), 40):
                if time.time() - STARTED > 85 * 60:
                    raise StopLookups("Time budget used; ReccoBeats checks continue next run")
                batch, ids = todo[i:i + 40], {}
                for t in batch:
                    sid = rc.spotify_id(t["title"], t["artist"])
                    if sid:
                        ids[sid] = t; t["spotify_id"] = sid
                feats = rc.features(list(ids)) if ids else {}
                for sid, (bpm, cam) in feats.items():
                    t = ids[sid]
                    if not t.get("bpm"):
                        t["bpm"], t["source"] = f"{bpm}*", "ReccoBeats"
                    if cam and not t.get("camelot"):
                        t["camelot"] = f"{cam}*"
                        if t.get("source") != "ReccoBeats":
                            t["source"] = (t.get("source") or "GetSongBPM") + " + ReccoBeats key"
                    rb_found += 1
                for t in batch:
                    t["rb_v"] = RB_V; rb_done += 1
                save(releases, "In progress (ReccoBeats)")
        except StopLookups as e:
            log(str(e))
        except Exception as e:
            log(f"ReccoBeats/Spotify problem: {e}")
        log(f"ReccoBeats checks this run: {rb_done}, found: {rb_found}")

    left = sum(1 for r in releases for t in r["tracks"] if not t.get("bpm") and
               (t.get("lookup_v", 1) < LOOKUP_V or t.get("fb_v") != FB_V or t.get("dz_v") != DZ_V))
    if cid and csec:
        left += sum(1 for r in releases for t in r["tracks"]
                    if (not t.get("bpm") or not t.get("camelot")) and t.get("rb_v") != RB_V and t.get("fb_v") == FB_V)
    log(f"Lookups this run: {api.lookups}. Tracks still to look up: {left}.")
    save(releases, "Finished")
    if os.environ.get("GITHUB_OUTPUT"):  # tells the workflow whether to start another run
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"remaining={left}\ngsb_used={api.lookups}\n")


if __name__ == "__main__":
    main()
