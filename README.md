# entitlements-catalog

Machine-readable exceptions for entitlement discovery across digital stores.

## Steam

The generated `catalogs/steam/v1/unlisted-dlc.json` contains DLC IDs found
through anonymous Steam product info or curated evidence that are absent from
the client's normal discovery sources: the Steam Store app list, per-game Store
DLC pages, and the base game's PICS `listofdlc`/depot references. Its format is
`{base_app_id: {"dlcs": {dlc_id: name}}}`.

The catalog contains only DLC the client cannot obtain through these sources.

## Discovery

`tools/PicsScanner` queries anonymous Steam PICS product info in consecutive
app-ID ranges, and `scripts/unlisted_catalog.py` accumulates the DLC records it
finds. The scan ceiling is the current Store maximum plus 100,000 IDs.

The twice-daily GitHub Action rescans only the newest 200,000 IDs below the
ceiling, where new DLC appear, and commits once if the records or catalog
changed. Running the Action manually with `segments` set to 1-60 resumes the
full pass from its cursor, pushing a checkpoint after each 100,000-ID range so
a failed run resumes where it stopped; after the ceiling it starts over from
app ID 1.

Publishing removes IDs found by `IStoreService/GetAppList(include_dlc=true)`,
the base game's anonymous PICS data, or its per-game Store DLC page.
`manual/steam/extra-dlc.json` remains a curated fallback for DLC whose
anonymous PICS metadata is unavailable. Entries are removed from this fallback
automatically once the scan finds the same ID and parent with a useful name.
The catalog cannot be guaranteed exhaustive: Steam may withhold product info,
and hidden IDs may exist above the current scan ceiling.

## Local use

Set `STEAM_API_KEY` in `.env` or the environment. The key only reads Steam's
Store app list; the PICS scanner logs in anonymously.

```powershell
dotnet build tools/PicsScanner/PicsScanner.csproj -c Release
python scripts/unlisted_catalog.py ceiling
python scripts/unlisted_catalog.py scan [--frontier] --count 100000 --ceiling <value> --scanner tools/PicsScanner/bin/Release/net10.0/PicsScanner.dll
python scripts/unlisted_catalog.py publish --scanner tools/PicsScanner/bin/Release/net10.0/PicsScanner.dll
```

The Action uses GitHub's scoped `GITHUB_TOKEN` to commit generated state and
catalog changes; no personal access token is needed in Actions.
