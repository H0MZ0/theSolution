#!/usr/bin/env python3
"""Probe configured downloads without installing them or fetching entire archives."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.request

from goinfre import CONF_FILE, get_response_filename, parse_conf, resolve_download_url


def check_package(pkg):
    result = {"name": pkg.name, "source": pkg.url}
    try:
        url = resolve_download_url(pkg.url)
        if not url.startswith("https://"):
            raise RuntimeError("Expected an HTTPS download URL")
        result["resolved_url"] = url
        request = urllib.request.Request(url, headers={
            "User-Agent": "goinfre-pm", "Range": "bytes=0-511",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            result["http_status"] = response.status
            result["filename"] = get_response_filename(response, url)
            result["content_type"] = response.headers.get_content_type()
            prefix = response.read(512)
        signatures = (b"!<arch>\n", b"\x1f\x8b", b"\xfd7zXZ\x00", b"BZh", b"PK\x03\x04", b"\x7fELF")
        if not prefix.startswith(signatures):
            raise RuntimeError("Response is not a recognized installer/archive (possibly an HTML page)")
        result["status"] = "ok"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = str(exc)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONF_FILE)
    parser.add_argument("--json", type=Path, help="Write detailed results to this path")
    args = parser.parse_args()
    if not args.config.is_file():
        parser.error(f"Configuration not found: {args.config}")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(check_package, parse_conf(args.config)))
    for result in results:
        detail = result.get("error", result.get("filename", ""))
        print(f"{result['status'].upper():5} {result['name']:<24} {detail}")
    if args.json:
        args.json.write_text(json.dumps({
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "results": results,
        }, indent=2) + "\n")
    print("Checks cover reachability and archive headers, not full installation or freshness of pinned URLs.")
    return 1 if any(r["status"] == "error" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
