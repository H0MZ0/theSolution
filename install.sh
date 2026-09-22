#!/bin/sh
# Install a persistent user copy, launchers, and login maintenance. No sudo.
set -eu
command -v python3 >/dev/null 2>&1 || { echo 'Python 3.11+ with venv is required.' >&2; exit 1; }
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$script_dir/bootstrap.py" ]; then
    exec python3 "$script_dir/bootstrap.py" --setup "$@"
fi
command -v curl >/dev/null 2>&1 || { echo 'curl is required.' >&2; exit 1; }
install_tmp=$(mktemp -d)
trap 'rm -rf "$install_tmp"' EXIT HUP INT TERM
# Resolve one commit so all downloaded files belong to the same release.
curl -fsSL https://api.github.com/repos/H0MZ0/theSolution/commits/main -o "$install_tmp/commit.json"
install_commit=$(python3 -c 'import json,sys,re; s=json.load(open(sys.argv[1]))["sha"]; assert re.fullmatch("[a-f0-9]{40}",s); print(s)' "$install_tmp/commit.json")
for file in bootstrap.py goinfre.py packages.conf check_links.py requirements.txt package.json; do
    curl -fsSL "https://raw.githubusercontent.com/H0MZ0/theSolution/$install_commit/$file" -o "$install_tmp/$file"
done
python3 "$install_tmp/bootstrap.py" --setup "$@"
