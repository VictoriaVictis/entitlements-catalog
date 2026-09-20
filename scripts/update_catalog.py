import argparse
import ast
import concurrent.futures
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = ROOT / "state"
ENV_FILE = ROOT / ".env"

STORE_API = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
APP_DETAILS = "https://store.steampowered.com/api/appdetails"
AJAX_DLC = "https://store.steampowered.com/dlc/{app_id}/ajaxgetdlclist?cc=us&l=english"
USER_AGENT = "entitlements-catalog/1.0 (+digital-store-catalog)"


class CatalogHTTPError(RuntimeError):
    pass


def decode_body(raw, headers):
    if headers is not None and headers.get("Content-Encoding") == "gzip":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8")


def log(message):
    print(message, flush=True)


def build_paths(storefront):
    if storefront != "steam":
        raise SystemExit(f"Unsupported storefront: {storefront}")
    state_dir = STATE_ROOT / storefront
    return {
        "output": ROOT / "catalogs" / storefront / "v1" / "dlc.json",
        "manual": ROOT / "manual" / storefront / "extra-dlc.json",
        "state_dir": state_dir,
        "last_poll": state_dir / "last_poll.txt",
        "dlc_to_app": state_dir / "dlc_to_app.json",
        "queue": state_dir / "queue.json",
    }


def load_env():
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_api_key():
    return os.environ.get("STEAM_API_KEY", "").strip()


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)


def get_json(url, data=None, retries=4):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = data
    last_error = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(decode_body(raw, response.headers))
        except urllib.error.HTTPError as error:
            last_error = CatalogHTTPError(f"Steam request failed: HTTP {error.code}")
            if error.code in (403, 429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_error from None
        except Exception as error:
            last_error = CatalogHTTPError("Steam request failed")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise last_error from None
    raise last_error


def call_get_app_list(params, api_key):
    values = {"key": api_key}
    for key, value in params.items():
        if isinstance(value, bool):
            values[key] = "true" if value else "false"
        else:
            values[key] = value
    query = urllib.parse.urlencode(values)
    return get_json(f"{STORE_API}?{query}")


def list_apps(api_key, include_games, include_dlc, if_modified_since=None, limit=50000):
    apps = []
    last_app_id = 0
    page_size = 50000
    while len(apps) < limit:
        params = {
            "include_games": include_games,
            "include_dlc": include_dlc,
            "include_software": False,
            "include_videos": False,
            "include_hardware": False,
            "max_results": page_size,
        }
        if if_modified_since is not None:
            params["if_modified_since"] = if_modified_since
        if last_app_id:
            params["last_appid"] = last_app_id
        data = call_get_app_list(params, api_key)
        batch = data.get("response", {}).get("apps", [])
        if not batch:
            batch = data.get("applist", {}).get("apps", {}).get("app", [])
        if not batch:
            break
        for item in batch:
            app_id = int(item.get("appid", 0) or 0)
            if not app_id:
                continue
            apps.append(app_id)
            last_app_id = app_id
        if len(batch) < page_size:
            break
    return apps[:limit]


def get_app_details(app_ids, concurrency=8):
    details = {}
    app_ids = list(app_ids)
    if not app_ids:
        return details

    def fetch_one(app_id):
        url = f"{APP_DETAILS}?appids={app_id}&cc=us&l=english"
        try:
            data = get_json(url)
        except Exception:
            return str(app_id), None
        entry = data.get(str(app_id), {})
        payload = entry.get("data")
        if entry.get("success") and isinstance(payload, dict):
            return str(app_id), payload
        return str(app_id), None

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(fetch_one, app_id): app_id for app_id in app_ids}
        for future in concurrent.futures.as_completed(futures):
            app_key, payload = future.result()
            details[app_key] = payload
    return details


def get_dlc_list(app_id):
    try:
        data = get_json(AJAX_DLC.format(app_id=app_id))
    except Exception:
        return None
    if int(data.get("success", 0) or 0) != 1:
        return None
    dlcs = data.get("dlcs", [])
    return dlcs if isinstance(dlcs, list) else None


def fetch_dlc_lists(app_ids, concurrency):
    results = {}
    app_ids = list(app_ids)
    if not app_ids:
        return results
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(get_dlc_list, app_id): app_id for app_id in app_ids}
        for future in concurrent.futures.as_completed(futures):
            app_id = futures[future]
            try:
                results[app_id] = future.result()
            except Exception:
                results[app_id] = None
    return results


def normalize_dlc_map(dlcs):
    result = {}
    if not dlcs:
        return result
    for dlc in dlcs:
        dlc_id = str(dlc.get("appid", ""))
        name = str(dlc.get("name", ""))
        if dlc_id:
            result[dlc_id] = name
    return result


def update_dlc_to_app(mapping, app_id, dlcs):
    if not dlcs:
        return
    app_key = str(app_id)
    for dlc in dlcs:
        dlc_id = str(dlc.get("appid", ""))
        if not dlc_id:
            continue
        parents = mapping.setdefault(dlc_id, [])
        if app_key not in parents:
            parents.append(app_key)


def merge_manual(output, manual):
    for app_id, app in manual.items():
        dlcs = app.get("dlcs", {}) if isinstance(app, dict) else app
        if isinstance(dlcs, str):
            try:
                dlcs = json.loads(dlcs)
            except Exception:
                try:
                    dlcs = ast.literal_eval(dlcs)
                except Exception:
                    continue
        if not isinstance(dlcs, dict):
            continue
        entry = output.setdefault(str(app_id), {"dlcs": {}})
        for dlc_id, name in dlcs.items():
            name = str(name)
            if name:
                entry["dlcs"][str(dlc_id)] = name


def write_output(generated, manual, output_path):
    output = {}
    for app_id, entry in generated.items():
        dlc_map = entry.get("dlcs", {}) if isinstance(entry, dict) else entry
        if not isinstance(dlc_map, dict) or not dlc_map:
            continue
        output[str(app_id)] = {"dlcs": {str(k): str(v) for k, v in dlc_map.items()}}
    merge_manual(output, manual)
    output = {app_id: entry for app_id, entry in output.items() if entry.get("dlcs")}
    save_json(output_path, output)
    return output


def require_api_key():
    api_key = get_api_key()
    if not api_key:
        log("STEAM_API_KEY is required. Copy .env.example to .env and fill it in.")
        sys.exit(1)
    return api_key


def run_full(args, paths):
    api_key = require_api_key()
    manual = load_json(paths["manual"], {})
    mapping = load_json(paths["dlc_to_app"], {})
    generated = load_json(paths["output"], {})
    failed = []

    app_ids = list_apps(api_key, include_games=True, include_dlc=False, limit=args.max_games)
    log(f"Full scan: {len(app_ids)} games")

    for offset in range(0, len(app_ids), args.batch_size):
        batch = app_ids[offset : offset + args.batch_size]
        results = fetch_dlc_lists(batch, args.concurrency)
        for app_id, dlcs in results.items():
            app_key = str(app_id)
            if dlcs is None:
                failed.append(app_key)
                continue
            dlc_map = normalize_dlc_map(dlcs)
            if len(dlc_map) >= args.min_dlc:
                generated[app_key] = {"dlcs": dlc_map}
            elif app_key in generated and app_key not in manual:
                generated.pop(app_key, None)
            update_dlc_to_app(mapping, app_id, dlcs)
        log(f"Full scan: {min(offset + args.batch_size, len(app_ids))}/{len(app_ids)}")

    save_json(paths["dlc_to_app"], mapping)
    save_json(paths["queue"], sorted(set(failed), key=lambda value: int(value)))
    paths["last_poll"].parent.mkdir(parents=True, exist_ok=True)
    paths["last_poll"].write_text(str(int(time.time())), encoding="utf-8", newline="\n")
    output = write_output(generated, manual, paths["output"])
    log(f"Full scan complete: {len(output)} apps in output, {len(failed)} failed app fetches")


def run_incremental(args, paths):
    api_key = require_api_key()
    manual = load_json(paths["manual"], {})
    mapping = load_json(paths["dlc_to_app"], {})
    queue = [str(app_id) for app_id in load_json(paths["queue"], [])]
    output = load_json(paths["output"], {})

    if paths["last_poll"].exists():
        try:
            last_poll = int(paths["last_poll"].read_text(encoding="utf-8").strip() or 0)
        except Exception:
            last_poll = 0
    else:
        last_poll = 0
    if last_poll <= 0:
        last_poll = int(time.time()) - 7 * 86400
        log("Incremental: no last_poll found, using 7-day lookback")

    poll_start = int(time.time())
    modified_games = list_apps(
        api_key,
        include_games=True,
        include_dlc=False,
        if_modified_since=last_poll,
        limit=50000,
    )
    modified_dlcs = list_apps(
        api_key,
        include_games=False,
        include_dlc=True,
        if_modified_since=last_poll,
        limit=50000,
    )
    log(
        f"Incremental: {len(modified_games)} modified games and "
        f"{len(modified_dlcs)} modified DLC app ids since {last_poll}"
    )

    recheck = set(queue)
    recheck.update(str(app_id) for app_id in modified_games)
    unknown_dlcs = []

    for dlc_id in modified_dlcs:
        dlc_key = str(dlc_id)
        parents = mapping.get(dlc_key)
        if isinstance(parents, list) and parents:
            recheck.update(str(parent) for parent in parents)
        else:
            unknown_dlcs.append(dlc_key)

    if unknown_dlcs:
        unknown_dlcs = sorted(set(unknown_dlcs), key=lambda value: int(value))
        if len(unknown_dlcs) > args.max_rechecks:
            log(f"Incremental: resolving {args.max_rechecks} of {len(unknown_dlcs)} unknown modified DLC parents")
        details = get_app_details(unknown_dlcs[: args.max_rechecks], args.concurrency)
        for dlc_key, detail in details.items():
            if not detail:
                continue
            if detail.get("type") == "dlc":
                full_game = detail.get("fullgame", {})
                parent = full_game.get("appid")
                if parent:
                    recheck.add(str(parent))
                    mapping[dlc_key] = [str(parent)]

    recheck.discard("")
    recheck.discard("0")
    recheck = sorted(recheck, key=lambda value: int(value))

    if len(recheck) > args.max_rechecks:
        selected = recheck[: args.max_rechecks]
        queue = recheck[args.max_rechecks :]
    else:
        selected = recheck
        queue = []

    log(f"Incremental: rechecking {len(selected)} base apps")
    results = fetch_dlc_lists(selected, args.concurrency)
    failed = []
    for app_id, dlcs in results.items():
        app_key = str(app_id)
        if dlcs is None:
            failed.append(app_key)
            continue
        dlc_map = normalize_dlc_map(dlcs)
        if len(dlc_map) >= args.min_dlc:
            output[app_key] = {"dlcs": dlc_map}
        elif app_key in output and app_key not in manual:
            output.pop(app_key, None)
        update_dlc_to_app(mapping, app_id, dlcs)

    queue = sorted(set(queue) | set(failed), key=lambda value: int(value))
    save_json(paths["dlc_to_app"], mapping)
    save_json(paths["queue"], queue)
    next_poll = poll_start - args.overlap_seconds
    paths["last_poll"].parent.mkdir(parents=True, exist_ok=True)
    paths["last_poll"].write_text(str(next_poll), encoding="utf-8", newline="\n")
    output = write_output(output, manual, paths["output"])
    log(f"Incremental complete: {len(output)} apps in output, {len(failed)} failed app fetches")


def main():
    load_env()
    parser = argparse.ArgumentParser()
    parser.add_argument("--storefront", choices=["steam"], default="steam")
    parser.add_argument("--mode", choices=["incremental", "full"], required=True)
    parser.add_argument("--min-dlc", type=int, default=64)
    parser.add_argument("--max-games", type=int, default=1000000)
    parser.add_argument("--max-rechecks", type=int, default=500)
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--overlap-seconds", type=int, default=300)
    args = parser.parse_args()
    paths = build_paths(args.storefront)

    if args.mode == "full":
        run_full(args, paths)
    else:
        run_incremental(args, paths)


if __name__ == "__main__":
    main()
