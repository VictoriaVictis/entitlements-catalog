"""Discover DLC via Steam product info and publish only Store-unlisted IDs."""

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "state" / "steam"
CURSOR = STATE / "pics_scan.json"
RECORDS = STATE / "pics_dlcs.json"
MANUAL = ROOT / "manual" / "steam" / "extra-dlc.json"
OUTPUT = ROOT / "catalogs" / "steam" / "v1" / "unlisted-dlc.json"
STORE_API = "https://api.steampowered.com/IStoreService/GetAppList/v1/"


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def steam_key():
    load_env()
    key = os.environ.get("STEAM_API_KEY", "").strip()
    if not key:
        raise RuntimeError("STEAM_API_KEY is required for Store app-list requests")
    return key


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent,
        prefix=path.name + ".", suffix=".tmp", delete=False
    ) as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
        temporary = Path(file.name)
    temporary.replace(path)


def store_app_ids(*, include_all=False):
    """Return the complete Store-visible ID set, or its maximum when include_all."""
    key = steam_key()
    last_id = 0
    result = set()
    while True:
        params = {
            "key": key,
            "include_games": "true" if include_all else "false",
            "include_dlc": "true",
            "include_software": "true" if include_all else "false",
            "include_videos": "true" if include_all else "false",
            "include_hardware": "true" if include_all else "false",
            "max_results": 50000,
        }
        if last_id:
            params["last_appid"] = last_id
        url = STORE_API + "?" + urllib.parse.urlencode(params)
        for attempt in range(4):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "entitlements-catalog/2"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    page = json.load(response)["response"]
                break
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
                if attempt == 3:
                    raise RuntimeError("Steam Store app-list request failed") from None
                time.sleep(2 ** attempt)
        batch = page.get("apps", [])
        if not batch:
            if page.get("have_more_results"):
                raise RuntimeError("Steam Store returned an empty nonterminal page")
            break
        ids = [int(item["appid"]) for item in batch]
        next_id = int(page.get("last_appid") or ids[-1])
        if next_id <= last_id:
            raise RuntimeError("Steam Store pagination did not advance")
        result.update(ids)
        last_id = next_id
        if not page.get("have_more_results"):
            break
    if not result:
        raise RuntimeError("Steam Store returned no app IDs")
    return result


def scan(count, ceiling, scanner):
    if not 1 <= count <= 100000:
        raise ValueError("count must be between 1 and 100000")
    if ceiling < 1:
        raise ValueError("ceiling must be positive")
    cursor = read_json(CURSOR, {"next_app_id": 1, "completed_passes": 0})
    start = int(cursor["next_app_id"])
    if start < 1 or start > ceiling:
        raise ValueError("scan cursor lies outside the current ceiling")
    size = min(count, ceiling - start + 1)
    with tempfile.TemporaryDirectory() as temp:
        segment = Path(temp) / "segment.json"
        subprocess.run(["dotnet", str(scanner), str(start), str(size), str(segment)], check=True)
        data = read_json(segment, None)
    if data is None or data.get("start") != start or data.get("count") != size:
        raise RuntimeError("scanner output does not match requested interval")
    records = read_json(RECORDS, {})
    for item in data["dlcs"]:
        dlc_id = int(item["id"])
        parent = int(item["parent"])
        if not start <= dlc_id < start + size or parent <= 0 or parent == dlc_id:
            raise RuntimeError("scanner output contains an invalid DLC record")
        records[str(dlc_id)] = {"parent": parent, "name": str(item["name"]).strip()}
    # Write records first: an interrupted run repeats the segment rather than losing it.
    write_json(RECORDS, dict(sorted(records.items(), key=lambda pair: int(pair[0]))))
    wrapped = start + size > ceiling
    write_json(CURSOR, {
        "next_app_id": 1 if wrapped else start + size,
        "completed_passes": int(cursor["completed_passes"]) + int(wrapped),
    })
    print(f"Scanned {start}..{start + size - 1}; {len(data['dlcs'])} DLC records; "
          f"next {1 if wrapped else start + size}")


def build_catalog(records, manual, visible, base_references=None, store_references=None):
    base_references = base_references or {}
    store_references = store_references or {}
    combined = {}
    for dlc_id, item in records.items():
        combined[int(dlc_id)] = (int(item["parent"]), str(item["name"]).strip())
    # Keep curated entries as a safety net for DLC whose anonymous PICS data is absent.
    for parent_id, entry in manual.items():
        for dlc_id, name in entry["dlcs"].items():
            combined[int(dlc_id)] = (int(parent_id), str(name).strip())
    result = {}
    for dlc_id, (parent_id, name) in sorted(combined.items()):
        if (dlc_id in visible or dlc_id in base_references.get(parent_id, ())
                or dlc_id in store_references.get(parent_id, ())):
            continue
        if dlc_id <= 0 or parent_id <= 0 or not name:
            raise ValueError(f"Invalid DLC record: {dlc_id}")
        result.setdefault(parent_id, {})[dlc_id] = name
    return {
        str(parent): {"dlcs": {str(dlc): name for dlc, name in sorted(dlcs.items())}}
        for parent, dlcs in sorted(result.items())
    }


def base_dlc_references(parents, scanner):
    with tempfile.TemporaryDirectory() as temp:
        input_path = Path(temp) / "parents.json"
        output_path = Path(temp) / "references.json"
        input_path.write_text(json.dumps(sorted(parents)), encoding="utf-8")
        subprocess.run(["dotnet", str(scanner), "--references", str(input_path), str(output_path)], check=True)
        data = read_json(output_path, None)
    if data is None or not isinstance(data.get("references"), dict):
        raise RuntimeError("scanner did not return base DLC references")
    return {int(parent): set(map(int, ids)) for parent, ids in data["references"].items()}


def store_dlc_references(parents):
    def fetch(parent):
        url = f"https://store.steampowered.com/dlc/{parent}/ajaxgetdlclist?cc=us&l=english"
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "entitlements-catalog/2"})
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = json.load(response)
                if int(data.get("success", 0)) != 1:
                    return parent, None
                return parent, {int(item["appid"]) for item in data.get("dlcs", [])}
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
                if attempt < 2:
                    time.sleep(1 << attempt)
        return parent, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = dict(pool.map(fetch, parents))
    failures = sum(ids is None for ids in results.values())
    if failures:
        print(f"Store DLC pages unavailable for {failures} games; retaining their candidates", file=sys.stderr)
    return {parent: ids for parent, ids in results.items() if ids is not None}


def publish(scanner):
    records = read_json(RECORDS, {})
    manual = read_json(MANUAL, {})
    visible = store_app_ids()
    parents = {int(item["parent"]) for item in records.values()} | set(map(int, manual))
    base_references = base_dlc_references(parents, scanner)
    candidates = build_catalog(records, manual, visible, base_references)
    store_references = store_dlc_references(map(int, candidates))
    output = build_catalog(records, manual, visible, base_references, store_references)
    write_json(OUTPUT, output)
    print(f"Published {sum(len(item['dlcs']) for item in output.values())} "
          f"unlisted DLC IDs across {len(output)} games")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ceiling_cmd = commands.add_parser("ceiling", help="print Store maximum ID plus scan margin")
    ceiling_cmd.add_argument("--margin", type=int, default=100000)
    scan_cmd = commands.add_parser("scan", help="scan one checkpointed ID range")
    scan_cmd.add_argument("--count", type=int, default=100000)
    scan_cmd.add_argument("--ceiling", type=int, required=True)
    scan_cmd.add_argument("--scanner", type=Path, required=True)
    publish_cmd = commands.add_parser("publish", help="publish DLC absent from client discovery sources")
    publish_cmd.add_argument("--scanner", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "ceiling":
        ids = store_app_ids(include_all=True)
        if args.margin < 0:
            raise ValueError("margin must be nonnegative")
        cursor = read_json(CURSOR, {"next_app_id": 1})
        print(max(max(ids) + args.margin, int(cursor["next_app_id"])))
    elif args.command == "scan":
        scan(args.count, args.ceiling, args.scanner)
    else:
        publish(args.scanner)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1) from None
