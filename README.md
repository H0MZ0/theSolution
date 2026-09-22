# theSolution

A terminal package manager for 1337/42 Linux x86-64 computers. Install apps into `~/goinfre/bin` or a directory you choose, with application-menu launchers, automatic restore, and updates.

## Install

From this checkout:

```sh
npm install --global .
```

Without Node/npm:

```sh
./install.sh
```

Both install a persistent copy into `~/.local/share/thesolution-pm/app`, npm-managed commands (or shell-install symlinks) in `~/.local/bin`, a **theSolution** applications-menu shortcut, and login maintenance. No sudo is needed. Open the menu shortcut or run:

```sh
~/.local/bin/thesolution-pm
```

The desktop shortcut opens the terminal interface. It is not a separate graphical interface. Add `~/.local/bin` to PATH if you want to type `thesolution-pm` or `theSolution` directly. Remove an older `theSolution` shell alias if it still points to the old installer.

Requires **Python 3.11+ with venv/pip**, Linux x86-64, and `dpkg` for DEBs. npm installation additionally needs Node.js 20+. First interactive launch installs Textual into a separate environment for each machine under `~/goinfre/.thesolution-pm`, `/goinfre/<login>/.thesolution-pm`, or a user-specific directory in `/tmp` if goinfre is unavailable. These environments are recreated if local storage is wiped; they do not consume persistent home quota. Python and system libraries must already be available; extraction does not install system dependencies.

## Give it to another person

Create the distributable npm package:

```sh
npm pack
```

Send `thesolution-pm-1.1.1.tgz`. The recipient installs with one command:

```sh
npm install --global ./thesolution-pm-1.1.1.tgz
```

The package is available on npm. Anyone can use:

```sh
npx --yes thesolution-pm@latest
# Or install permanently:
npm install --global thesolution-pm
```

`goinfre-pm` belongs to a different project. Maintainers publish new versions of this package with `npm login` and `npm publish`.

The no-npm one-command installer is also available:

```sh
curl -fsSL https://raw.githubusercontent.com/H0MZ0/theSolution/main/install.sh | sh
```

The remote installer and self-update use published files from GitHub `main`; they cannot fetch unpublished local changes.

## Restore and update automatically

Installation enables graphical-login maintenance and starts it for the current session. It:

1. Restores applications recorded in `~/.config/goinfre/state.json` if their executable is missing.
2. Checks installed apps against current upstream downloads; unchanged releases are skipped.
3. Repairs app launchers and symlinks.
4. Checks again every 24 hours while logged in; failed app checks/installations retry after 15 minutes.

Applications are downloaded and extracted in a separate directory before replacement. Failed downloads, extraction, or post-install steps preserve the previous installation. Updates need temporary room for the archive, replacement, and previous version. Newer system-library requirements can still prevent an updated app from running.

The manager also checks this project's GitHub `main` branch for a higher package version, downloads all runtime files from one commit, and replaces its persistent copy. Maintainers must increment `package.json` and push the complete release for self-update to see it. Failed manager update checks leave the local copy intact.

Across school PCs, your home directory must follow your login, the chosen goinfre path must work there, and Python must be available. A persistent copy means deleting an `npx` cache does not remove the launcher. Python environments are rebuilt per machine as needed. Autostart uses the desktop session; it does not run at an SSH-only login.

For another account or a computer without a shared home, transfer the application list:

```sh
thesolution-pm --export-profile apps.json
# On the other computer, after installing the tool:
thesolution-pm --import-profile apps.json
thesolution-pm --update
```

Profiles contain application names, not application settings, files, or custom catalogs. Import merges the list with existing selections. Copy custom catalogs separately when needed.

Useful commands:

```sh
thesolution-pm --auto                    # Restore missing apps only
thesolution-pm --update                  # Restore and update now
thesolution-pm --update-self             # Check the manager now
thesolution-pm --status                  # Preferences and log location
thesolution-pm --disable-auto-update     # Restore without updating apps
thesolution-pm --enable-auto-update
thesolution-pm --disable-self-update
thesolution-pm --enable-self-update
thesolution-pm --disable-autostart        # Stop login/background maintenance
thesolution-pm --enable-autostart
```

Preferences are in `~/.config/goinfre/automation.json`; logs are in the runtime directory under `logs/<hostname>/maintenance.log` (use `--status` for the exact path). An active installation finishes before a disabled worker exits. Standard XDG directory overrides are supported.

## App sources and verification

On **2026-09-22**, all **45 configured sources** passed actual HTTP GET checks and archive-header validation. **43 follow upstream releases automatically**, using official vendor metadata/download pages, release APIs, or rolling endpoints.

Two explicit exceptions remain because upstream no longer supplies current compatible packages:

- **Atom:** discontinued; the final stable build is retained.
- **Stremio:** the DEB is legacy 4.4.168. Current official Linux releases use Flatpak, which this archive/DEB manager does not install. It is not presented as the current Stremio release.

WezTerm follows its latest stable Ubuntu 22.04 build, which is still dated 2024. Brave nightly follows nightly releases; Chromium follows development snapshots; Thunderbird follows ESR. Other release resolvers use stable desktop builds. If a vendor changes its page layout or GitHub rate-limits requests, checks fail visibly rather than silently choosing an unrelated installer.

Recheck at any time:

```sh
thesolution-pm --check-links --json /tmp/thesolution-links.json
# Or from the checkout:
python3 check_links.py
```

This verifies reachability and the beginning of each archive, not full application compatibility. Verification also covered a real ripgrep installation, executable launch, unchanged-update skipping, and restoration after deleting its installation. Rollback, profile transfer, launcher creation, and background-worker behavior were checked separately. The test files and dated audit artifacts were removed from the project as requested; the reusable link checker remains.

## Controls and configuration

| Key | Action |
|---|---|
| Up / Down | Move |
| Space | Select |
| `i` / `I` | Install current / selected apps |
| `r` / `R` | Remove current / selected apps and matching settings/cache |
| `a` | Select/deselect all filtered apps |
| `/` | Search |
| `p` | Set the installation directory |
| `q` / Escape | Quit |

```sh
thesolution-pm --install-root /goinfre/your-login/bin
thesolution-pm --config /path/to/packages.conf
```

These choices persist for maintenance. The default catalog is updated with the manager; custom catalogs are not overwritten. Each catalog line is a package name, URL, and optional post-install shell command. Commands receive `$PACKAGE_DIR` and `$GOINFRE_BIN`.

```text
brave-browser https://github.com/brave/brave-browser#asset=brave-browser_*_amd64.deb
tor https://aus1.torproject.org/torbrowser/update_3/release/download-linux-x86_64.json#resolve=tor
```

Advanced installations can set `GOINFRE_PYTHON` to a Python executable, `THESOLUTION_BIN_DIR` to a different command directory, `THESOLUTION_RUNTIME_DIR` to a writable location outside your home quota, or `THESOLUTION_NO_START=1` to register login maintenance without starting a worker immediately. `./install.sh --no-start` does the same for shell installation.

## Credits

Inspired by [Mohamed El Mouhib's Fixgoinfre](https://github.com/Mohamed-El-Mouhib/Fixgoinfre.git) and Achraf Ennadiri's zero-tow project ([GitHub profile](https://github.com/ac-ennadi)). The owner has not selected a license; npm metadata remains `UNLICENSED`.

## Full home storage / npm cache

On school machines, place npm's download cache in goinfre before using npm or npx:

```sh
mkdir -p "$HOME/goinfre/.cache/npm"
npm config set cache "$HOME/goinfre/.cache/npm" --location=user
```

The tool's Python environments and logs already use goinfre (or `/tmp`). npm downloads happen before the tool runs, so npm's own cache location is configured separately. Application lists and the small persistent launcher/runtime remain in your home directory.

Version 1.1.1 also fixes the old installer overwriting npm's executable symlink. If upgrading from 1.1.0 reports `EEXIST`, remove only the old tool-created regular launcher at `~/.local/bin/thesolution-pm` before reinstalling; do not use `--force` to overwrite unrelated commands.
