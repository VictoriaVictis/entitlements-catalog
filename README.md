# entitlements-catalog

Data repo for digital-store entitlement/DLC catalog JSON.

- Output: `catalogs/steam/v1/dlc.json`
- Manual additions/overrides: `manual/steam/extra-dlc.json`
- Updater: `scripts/update_catalog.py`
- State: `state/steam/`

The generated catalog includes games whose current storefront DLC list is at least 64 entries.

Local test:

```powershell
Copy-Item .env.example .env
# Put STEAM_API_KEY in .env
python scripts/update_catalog.py --storefront steam --mode full
python scripts/update_catalog.py --storefront steam --mode incremental
```
