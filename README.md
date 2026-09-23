# entitlements-catalog

Public exceptions for Steam DLC discovery. The generated
`catalogs/steam/v1/unlisted-dlc.json` contains DLC IDs found through anonymous
Steam product info or curated evidence that are absent from the client's normal
discovery sources: the Steam Store app list, per-game Store DLC pages, and the
base game's PICS `listofdlc`/depot references. It uses the same
`{base_app_id: {"dlcs": {dlc_id: name}}}` format as the previous catalog.

Store-visible DLC, including games with more than 64 DLC, is intentionally
excluded: clients can retrieve it from the Store app list. The old full
`dlc.json` mirrored that public data and is no longer maintained.

## Discovery

`tools/PicsScanner` queries anonymous Steam PICS product info in consecutive
app-ID ranges. `scripts/unlisted_catalog.py` commits a cursor and discovered
DLC records after each range. The twice-daily GitHub Action scans 100,000 IDs
per run; its `segments` input can run up to 60 ranges for a full backfill. Each
range is pushed separately so a failed run resumes at its last checkpoint.
When the scan reaches the current Store maximum plus 100,000 IDs, it starts
another pass from app ID 1.

Publishing removes IDs found by `IStoreService/GetAppList(include_dlc=true)`,
the base game's anonymous PICS data, or its per-game Store DLC page.
`manual/steam/extra-dlc.json` remains a curated fallback for DLC whose
anonymous PICS metadata is unavailable. This data cannot be guaranteed
exhaustive: Steam may withhold product info and hidden IDs may exist above the
current scan ceiling.

## Local use

Set `STEAM_API_KEY` in `.env` or the environment. The key only reads Steam's
Store app list; the PICS scanner logs in anonymously.

```powershell
dotnet build tools/PicsScanner/PicsScanner.csproj -c Release
python scripts/unlisted_catalog.py ceiling
python scripts/unlisted_catalog.py scan --count 100000 --ceiling <value> --scanner tools/PicsScanner/bin/Release/net10.0/PicsScanner.dll
python scripts/unlisted_catalog.py publish --scanner tools/PicsScanner/bin/Release/net10.0/PicsScanner.dll
```

The Action uses GitHub's scoped `GITHUB_TOKEN` to commit generated state and
catalog changes; no personal access token is needed in Actions.
