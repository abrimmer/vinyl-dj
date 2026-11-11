# Setwright versions

Each version is a saved snapshot of the app's code (website + update script). Ask Claude to
"revert to version 0.0X" to bring that version back. Your data (collection, edits, setlists) is
kept separately and is never rolled back by a revert.

| Version | Date | Saved snapshot | What changed |
|---|---|---|---|
| 0.01 | 22 Oct 2026 | `4d66245` | Nightly data update (Discogs + GetSongBPM), 30 test records |
| 0.02 | 23 Oct 2026 | `064dff8` | First website: album grid, track details, "what mixes with this" |
| 0.03 | 23 Oct 2026 | `546067a` | Setlists, style filters, Spotify/YouTube links, missing-data page, manual entry |
| 0.04 | 23 Oct 2026 | `5399111` | Branded play buttons, Tunebat lookup, Guide page |
| 0.05 | 23 Oct 2026 | `17f7352` | Home link, sorting, clearer add-to-setlist |
| 0.06 | 23 Oct 2026 | `85de360` | Renamed Setwright, setlist style modes, full collection |
| 0.07 | 24 Oct 2026 | `6f2a1d5` | GitHub sync across devices, style editing, genre/random sort, separators, shortcuts |
| 0.08 | 24 Oct 2026 | `7770fef` | Album listen buttons, non-Latin title matching |
| 0.09 | 25 Oct 2026 | `e2df814` | Rules: tempo slider, key steps, style steps, Tight / Evolve |
| 0.10 | 25 Oct 2026 | `1e83c8a` | Style moves shown between tracks |
| 0.11 | 25 Oct 2026 | `06b4363` | Data-status dots, version numbers in the app |
| 0.12 | 26 Oct 2026 | `b9b2ce0` | Swipe cards for filling slots, song previews (Apple Music / Deezer / Spotify player), Spotify playlist export |
| 0.13 | 2 Nov 2026 | `010482f` | New look: Bricolage + Archivo fonts, compact setlist screen (rules bar, drag to reorder, swipe right to lock / left to clear, BPM + key tiles, play on cover), bento-style track picker. Revert to 0.12 if it misbehaves. |
| 0.14 | 11 Nov 2026 | `6441e06` | Stacks + crates: pick records for the table, save them as named crates, build setlists from just those records; Fill / Re-shuffle picks a random first track when slot 1 is empty |
| 0.15 | 11 Nov 2026 | `2c4968b` | Popularity: world-scale score 0–100 (Last.fm listeners + Deezer rank), Banger / Known / Deep cut tiers, Popularity rule (Bangers only / Deep cuts only / Mix 1-in-N, steady or wave), tick a tier on any empty slot before filling |
