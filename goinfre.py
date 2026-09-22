#!/usr/bin/env python3
"""
goinfre — Terminal Package Manager with Textual TUI
Reads packages.conf and manages packages in a configurable install root.
"""

import argparse, os, sys, json, re, platform, shutil, subprocess, tarfile, zipfile, time
import urllib.request, urllib.error, urllib.parse
import fnmatch
import html, gzip, tempfile, fcntl, functools
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

# ── Configurable Paths ───────────────────────────────────────────────────────
DEFAULT_INSTALL_ROOT = Path.home() / "goinfre" / "bin"
CONF_FILE   = Path(__file__).parent / "packages.conf"
TMP_DIR     = Path("/tmp/goinfre_install")
CONFIG_DIR  = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "goinfre"
CONFIG_FILE = CONFIG_DIR / "config.toml"
STATE_FILE  = CONFIG_DIR / "state.json"

# Mutable global — updated at startup and by the TUI path modal
GOINFRE_BIN = DEFAULT_INSTALL_ROOT

def _read_desired_packages() -> list[str]:
    """Read the list of desired/previously installed package names."""
    if not STATE_FILE.exists():
        return []
    try:
        data = json.loads(STATE_FILE.read_text())
        return data.get("desired", [])
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Cannot read saved application list: {error}") from error

def _write_desired_packages(desired: list[str]) -> None:
    """Save the list of desired/previously installed package names."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    atomic_json(STATE_FILE, {"desired": sorted(set(desired))})

def atomic_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        json.dump(data, stream, indent=2)
        temporary = Path(stream.name)
    temporary.replace(path)

def package_lock(function):
    @functools.wraps(function)
    def locked(*args, **kwargs):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with (CONFIG_DIR / "packages.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield from function(*args, **kwargs)
    return locked

def _read_config_path() -> Path | None:
    """Read install_root from ~/.config/goinfre/config.toml."""
    if not CONFIG_FILE.exists():
        return None
    text = CONFIG_FILE.read_text()
    # Try tomllib (3.11+) first
    try:
        import tomllib
        data = tomllib.loads(text)
        p = data.get("paths", {}).get("install_root")
        if p: return Path(p)
    except ImportError:
        pass
    # Fallback: simple key=value parser
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("install_root"):
            _, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            if val: return Path(val)
    return None

def _write_config_path(path: Path) -> None:
    """Write install_root to ~/.config/goinfre/config.toml."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(f'[paths]\ninstall_root = "{path}"\n')

def resolve_install_root(cli_arg: str | None = None) -> Path:
    """CLI flag > config.toml > DEFAULT_INSTALL_ROOT."""
    if cli_arg:
        return Path(cli_arg).expanduser().resolve()
    cfg = _read_config_path()
    if cfg:
        return cfg.expanduser().resolve()
    return DEFAULT_INSTALL_ROOT

Log = Generator[str, None, None]

@dataclass
class Package:
    name: str
    url: str
    post_cmd: str | None = None
    selected: bool = False

    @property
    def source_type(self) -> str:
        u = self.url.lower()
        if github_repo_match(u):
            return "GitHub Release"
        for ext in (".appimage",):
            if u.endswith(ext): return "AppImage"
        for ext in (".tar.gz", ".tgz", ".tar.xz", ".tar.bz2"):
            if u.endswith(ext): return ext
        if u.endswith(".zip"): return ".zip"
        return "binary"

    @property
    def filename(self) -> str:
        return self.url.rstrip("/").split("/")[-1].split("?")[0][:30]

    def installed(self) -> bool:
        return find_package_executable(GOINFRE_BIN / self.name, self.name) is not None

# ── Config parser ─────────────────────────────────────────────────────────────
def parse_conf(path: Path) -> list[Package]:
    if not path.exists():
        return []
    pkgs = []
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            pkgs.append(Package(
                name=parts[0], url=parts[1],
                post_cmd=parts[2] if len(parts) == 3 else None,
            ))
    return pkgs

# ── Download Strategies ───────────────────────────────────────────────────────
def _detect_os_arch() -> tuple[str, list[str]]:
    s = platform.system().lower()
    m = platform.machine().lower()
    arch_tags = []
    if m in ("x86_64", "amd64"):
        arch_tags = ["x86_64", "amd64", "x64", "linux64"]
    elif m in ("aarch64", "arm64"):
        arch_tags = ["aarch64", "arm64"]
    else:
        arch_tags = [m]
    return s, arch_tags

class DownloadStrategy(ABC):
    @abstractmethod
    def resolve_and_download(self, url: str, name: str, dest: Path) -> Generator[str | Path, None, None]:
        """Yield log strings, final yield is the Path to downloaded file."""
        ...

def github_repo_match(url: str):
    return re.fullmatch(r"https?://github\.com/([^/]+)/([^/#?]+)/?", url.split("#", 1)[0])


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "goinfre-pm",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"Release metadata request failed for {url}: {e}") from e


def select_release_asset(release: dict, pattern: str | None = None) -> dict | None:
    os_name, arch_tags = _detect_os_arch()
    candidates = []
    for asset in release.get("assets", []):
        name = asset["name"]
        lower = name.lower()
        if not lower.endswith((".deb", ".appimage", ".tar.gz", ".tar.xz", ".tar.bz2", ".tgz", ".zip")):
            continue
        if pattern:
            if not fnmatch.fnmatchcase(name, pattern):
                continue
        else:
            # Never fall back to a different architecture or a source/debug archive.
            if not any(tag in lower for tag in arch_tags):
                continue
            if os_name not in lower and not (os_name == "linux" and lower.endswith((".deb", ".appimage"))):
                continue
            if any(tag in lower for tag in ("symbols", "debug", "source")):
                continue
        candidates.append(asset)
    if len(candidates) > 1:
        raise RuntimeError("Multiple release assets match; specify a unique #asset= filename pattern")
    return candidates[0] if candidates else None


def resolve_download_url(url: str) -> str:
    """Resolve a GitHub release to an exact asset; leave direct URLs unchanged."""
    vendor = urllib.parse.parse_qs(urllib.parse.urlsplit(url).fragment).get("resolve")
    if vendor:
        return resolve_vendor_url(url.split("#", 1)[0], vendor[0])
    match = github_repo_match(url)
    if not match:
        return url
    options = urllib.parse.parse_qs(urllib.parse.urlsplit(url).fragment)
    pattern = options.get("asset", [None])[0]
    channel = options.get("channel", ["stable"])[0]
    if channel not in ("stable", "nightly"):
        raise RuntimeError(f"Unsupported release channel: {channel}")
    if channel == "nightly" and (not pattern or "nightly" not in pattern):
        raise RuntimeError("Nightly releases require an explicit nightly asset pattern")
    base = f"https://api.github.com/repos/{match[1]}/{match[2]}/releases"
    if channel == "stable":
        release = fetch_json(base + "/latest")
        if not release.get("draft") and not release.get("prerelease"):
            asset = select_release_asset(release, pattern)
            if asset:
                return asset["browser_download_url"]
    # Some projects publish mobile-only releases, or multiple channels in one repo.
    # Search recent releases for the newest matching desktop/channel asset.
    for page in range(1, 4):
        releases = fetch_json(f"{base}?per_page=30&page={page}")
        for release in releases:
            if release.get("draft") or (channel == "stable" and release.get("prerelease")):
                continue
            asset = select_release_asset(release, pattern)
            if asset:
                return asset["browser_download_url"]
        if len(releases) < 30:
            break
    raise RuntimeError(f"No matching {channel} release asset for {url}")


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "goinfre-pm"})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
        if response.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        return html.unescape(data.decode("utf-8"))


def resolve_vendor_url(url: str, vendor: str) -> str:
    """Read current Linux x86-64 installers from official vendor metadata/pages."""
    if vendor == "tor":
        return fetch_json(url)["binary"]
    if vendor == "go":
        for release in fetch_json(url):
            if release.get("stable"):
                for file in release["files"]:
                    if (file["os"], file["arch"], file["kind"]) == ("linux", "amd64", "archive"):
                        return "https://go.dev/dl/" + file["filename"]
        raise RuntimeError("No stable Go Linux amd64 archive")
    page = fetch_text(url)
    if vendor in ("opera", "warp"):
        package, base = {
            "opera": ("opera-stable", "https://deb.opera.com/opera-stable/"),
            "warp": ("warp-terminal", "https://releases.warp.dev/linux/deb/"),
        }[vendor]
        candidates = []
        for paragraph in page.split("\n\n"):
            fields = dict(line.split(": ", 1) for line in paragraph.splitlines()
                          if ": " in line and not line.startswith(" "))
            if fields.get("Package") == package and fields.get("Architecture") == "amd64":
                candidates.append(fields)
        if not candidates:
            raise RuntimeError(f"No {package} amd64 entry in vendor repository")
        # These vendor versions use dotted numeric components.
        latest = max(candidates, key=lambda entry: tuple(map(int, re.findall(r"\d+", entry["Version"]))))
        return urllib.parse.urljoin(base, latest["Filename"])
    patterns = {
        "antigravity": r'https://edgedl[^"\s<>]+/antigravity/stable/[^"\s<>]+/linux-x64/[^"\s<>]+\.tar\.gz',
        "waterfox": r'https://cdn\.waterfox\.com/[^"\s<>]+/Linux_x86_64/[^"\s<>]+\.tar\.bz2',
        "seamonkey": r'https://archive\.seamonkey-project\.org/releases/[^"\s<>]+/linux-x86_64/en-US/[^"\s<>]+\.tar\.bz2',
        "sublime": r'https://download\.sublimetext\.com/sublime-text_build-\d+_amd64\.deb',
        "blender": r'https://www\.blender\.org/download/release/Blender[\d.]+/blender-[\d.]+-linux-x64\.tar\.xz/',
        "slack": r'https://downloads\.slack-edge\.com/[^"\s<>]+/slack-desktop-[^"\s<>]+-amd64\.deb',
        "android": r'https://edgedl[^"\s<>]+/android/studio/ide-zips/[^"\s<>]+linux\.tar\.gz',
    }
    if vendor not in patterns:
        raise RuntimeError(f"Unknown release resolver: {vendor}")
    matches = list(dict.fromkeys(re.findall(patterns[vendor], page)))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one current {vendor} installer, found {len(matches)}; vendor page may have changed")
    result = matches[0]
    if vendor == "blender":
        result = result.replace("https://www.blender.org/download/release/", "https://download.blender.org/release/").rstrip("/")
    return result


def download_metadata(url: str) -> dict:
    """Fingerprint the installer without retaining expiring signed redirect URLs."""
    resolved = resolve_download_url(url)
    request = urllib.request.Request(resolved, headers={"User-Agent": "goinfre-pm", "Range": "bytes=0-511"})
    with urllib.request.urlopen(request, timeout=30) as response:
        prefix = response.read(512)
        if not prefix.startswith((b"!<arch>\n", b"\x1f\x8b", b"\xfd7zXZ\x00", b"BZh", b"PK\x03\x04", b"\x7fELF")):
            raise RuntimeError("Source did not return an installer/archive")
        return {
            "source": url, "resolved_url": resolved,
            "filename": get_response_filename(response, resolved),
            "etag": response.headers.get("ETag", ""),
            "modified": response.headers.get("Last-Modified", ""),
            "size": response.headers.get("Content-Range", "").split("/")[-1]
                    or response.headers.get("Content-Length", ""),
        }


def same_release(old: dict, current: dict) -> bool:
    # All current catalog sources provide versioned filenames or HTTP validators.
    return all(old.get(key) == current.get(key) for key in ("source", "filename", "etag", "modified", "size"))


class GitHubReleaseStrategy(DownloadStrategy):
    def resolve_and_download(self, url: str, name: str, dest: Path):
        yield "[INFO] Resolving current GitHub release asset..."
        dl_url = resolve_download_url(url)
        yield f"[INFO] Selected asset: {dl_url.rsplit('/', 1)[-1]}"
        yield from DirectURLStrategy().resolve_and_download(dl_url, name, dest)

def get_response_filename(resp, url: str) -> str:
    # 1. Try Content-Disposition header
    cd = resp.headers.get("Content-Disposition")
    if cd:
        m = re.search(r'filename=["\']?([^"\';]+)["\']?', cd)
        if m:
            return Path(urllib.parse.unquote(m.group(1))).name
    # 2. Try the final redirected URL
    final_url = resp.geturl()
    path = urllib.parse.urlparse(final_url).path
    fname = Path(urllib.parse.unquote(path)).name
    if fname:
        return fname
    # 3. Fallback to original URL
    path = urllib.parse.urlparse(url).path
    fname = Path(path).name
    return fname or "downloaded_package"

class DirectURLStrategy(DownloadStrategy):
    def resolve_and_download(self, url: str, name: str, dest: Path) -> Generator[str | tuple | Path, None, None]:
        yield f"[...] Connecting to download source..."
        try:
            url = resolve_download_url(url)
            import urllib.parse
            req = urllib.request.Request(url, headers={"User-Agent": "goinfre-pm"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.headers.get_content_type() in ("text/html", "application/xhtml+xml", "application/json"):
                    raise RuntimeError("Download URL returned a web page or metadata instead of an installer")
                fname = get_response_filename(resp, url)
                out = dest / fname
                yield f"[...] Downloading {fname}..."
                
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 64 * 1024
                with open(out, "wb") as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            pct = min(downloaded / total * 100, 100)
                            yield ("PROGRESS", pct)
                if not downloaded or (total and downloaded != total):
                    out.unlink(missing_ok=True)
                    raise RuntimeError("Download is empty or incomplete")
        except Exception as e:
            raise RuntimeError(f"Download failed: {e}")
        yield ("PROGRESS", 100.0)
        yield f"[OK] Downloaded {fname}"
        yield out  # final yield = path

def pick_strategy(url: str) -> DownloadStrategy:
    if github_repo_match(url):
        return GitHubReleaseStrategy()
    return DirectURLStrategy()

# ── Extract ───────────────────────────────────────────────────────────────────
USER_BIN_DIR = Path(os.environ.get("THESOLUTION_BIN_DIR", Path.home() / ".local/bin"))
USER_DESKTOP_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications"
USER_ICON_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "icons"

EXEC_FILES = {
    "Motasafi7i": "opt/brave.com/brave-nightly/brave-browser-nightly",
    "steam": "usr/bin/steam",
    "stremio": "opt/stremio/stremio",
    "firefox": "firefox-bin",
    "chromium": "chrome",
    "tor": "Browser/firefox.real",
    "edge": "opt/microsoft/msedge/microsoft-edge",
    "vivaldi": "opt/vivaldi/vivaldi-bin",
    "google-chrome": "opt/google/chrome/google-chrome",
    "codium": "usr/share/codium/bin/codium",
    "atom": "usr/bin/atom",
    "discord": "usr/share/discord/Discord",
    "telegram": "Telegram",
    "pycharm": "bin/pycharm.sh",
    "midori": "opt/midori/midori",
    "waterfox": "waterfox",
    "seamonkey": "seamonkey-bin",
    "acennadicode": "usr/share/code/bin/code",
    "opera": "usr/lib/x86_64-linux-gnu/opera/opera",
    "postman": "app/Postman",
    "sublime": "opt/sublime_text/sublime_text",
    "blender": "blender-launcher",
    "zed": "bin/zed",
    "min": "opt/Min/min",
    "floorp": "floorp-bin",
    "mullvad": "Browser/start-mullvad-browser",
    "go": "bin/go",
    "clion": "bin/clion",
    "idea": "bin/idea",
    "phpstorm": "bin/phpstorm",
    "webstorm": "bin/webstorm",
    "rider": "bin/rider",
    "waveterm": "opt/Wave/waveterm",
    "android-studio": "bin/studio",
    "thunderbird": "thunderbird",
    "image-magick": "AppRun",
    "warp": "opt/warpdotdev/warp-terminal/warp",
    "nvim": "bin/nvim",
    "slack": "usr/lib/slack/slack",
    "zen": "zen",
    "wezterm": "usr/bin/wezterm",
    "obsidian": "opt/Obsidian/obsidian",
    "antigravity": "opt/google/antigravity/antigravity",
    "rpg": "rg",
}

NON_GRAPHICAL_PACKAGES = {"image-magick", "go", "nvim", "rpg"}
TERMINAL_PACKAGES = {"nvim", "rpg"}

def is_elf_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except Exception:
        return False

def find_icon(pkg_dir: Path, name: str) -> Path | None:
    icon_files = list(pkg_dir.glob("**/*.png")) + list(pkg_dir.glob("**/*.svg"))
    if not icon_files:
        return None
    patterns = [
        rf"product.*128.*\.png",
        rf"{re.escape(name)}\.png",
        rf"{re.escape(name)}.*\.png",
        rf"{re.escape(name)}\.svg",
        rf"default.*128.*\.png",
        rf".*icon.*128.*\.png"
    ]
    for pat in patterns:
        try:
            regex = re.compile(pat, re.IGNORECASE)
            for f in icon_files:
                if regex.search(f.name):
                    return f
        except Exception:
            continue
    return icon_files[0]

def find_package_executable(pkg_dir: Path, name: str) -> Path | None:
    # 1. Try our known map with key normalization (to handle user renames)
    aliases = {
        "motasafi7i": "opt/brave.com/brave-nightly/brave-browser-nightly",
        "brave": "opt/brave.com/brave/brave-browser",
        "brave-browser": "opt/brave.com/brave/brave-browser",
        "brave-browser-nightly": "opt/brave.com/brave-nightly/brave-browser-nightly",
        "acennadicode": "usr/share/code/bin/code",
        "vscode": "usr/share/code/bin/code",
        "code": "usr/share/code/bin/code",
        "rpg": "rg",
        "ripgrep": "rg",
    }
    
    norm_name = name.lower().strip()
    rel_path = aliases.get(norm_name) or EXEC_FILES.get(name)
    if rel_path:
        p = pkg_dir / rel_path.lstrip("/")
        if p.exists():
            return p
    if (pkg_dir / "AppRun").is_file():
        return pkg_dir / "AppRun"

    # 2. Try the default base search
    b = find_binary(pkg_dir, name)
    if b:
        return b

    # 3. Fallback: recursively search for an executable binary matching the package name
    candidates = []
    for root, dirs, files in os.walk(pkg_dir):
        for f in files:
            fp = Path(root) / f
            if fp.suffix in (".png", ".svg", ".desktop", ".json", ".txt", ".md", ".conf", ".css", ".html", ".js"):
                continue
            f_lower = f.lower()
            if norm_name in f_lower or f_lower in norm_name:
                try:
                    is_exe = os.access(fp, os.X_OK) or is_elf_file(fp)
                    if is_exe and fp.is_file() and not fp.is_symlink():
                        score = 0
                        if f_lower == norm_name:
                            score += 10
                        if "bin" in fp.parts:
                            score += 5
                        candidates.append((score, fp))
                except Exception:
                    continue

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    return None

def desktop_arg(value) -> str:
    value = ''.join('\\' + c if c in '\\"`$' else c for c in str(value))
    return '"' + value.replace('\\', '\\\\').replace('%', '%%') + '"'


def integrate_package(pkg: Package) -> Log:
    pkg_dir = GOINFRE_BIN / pkg.name
    exe = find_package_executable(pkg_dir, pkg.name)
    if not exe:
        yield f"[WARN] Could not find executable for integration: {pkg.name}"
        return

    exe.chmod(exe.stat().st_mode | 0o111)

    USER_BIN_DIR.mkdir(parents=True, exist_ok=True)
    sym = USER_BIN_DIR / pkg.name
    if sym.exists() or sym.is_symlink():
        sym.unlink()
    try:
        sym.symlink_to(exe)
        yield f"[OK] Created symlink: {sym} -> {exe}"
    except Exception as e:
        raise RuntimeError(f"Failed to create symlink: {e}") from e

    if pkg.name not in NON_GRAPHICAL_PACKAGES:
        icon_file = find_icon(pkg_dir, pkg.name)
        icon_name = pkg.name
        if icon_file:
            USER_ICON_DIR.mkdir(parents=True, exist_ok=True)
            dest_icon = USER_ICON_DIR / f"{pkg.name}{icon_file.suffix}"
            if dest_icon.exists() or dest_icon.is_symlink():
                dest_icon.unlink()
            try:
                shutil.copy2(icon_file, dest_icon)
                yield f"[OK] Copied icon to {dest_icon}"
            except Exception as e:
                yield f"[WARN] Failed to copy icon: {e}"
        else:
            yield f"[WARN] No icon found for {pkg.name}"

        USER_DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
        desktop_file = USER_DESKTOP_DIR / f"{pkg.name}.desktop"
        if desktop_file.exists():
            desktop_file.unlink()

        is_term = "true" if pkg.name in TERMINAL_PACKAGES else "false"
        content = f"[Desktop Entry]\nName={pkg.name}\nComment={pkg.name}\nExec={desktop_arg(exe)}\nTerminal={is_term}\nIcon={icon_name}\nType=Application\n"
        try:
            desktop_file.write_text(content)
            desktop_file.chmod(0o755)
            yield f"[OK] Created desktop launcher: {desktop_file}"
        except Exception as e:
            raise RuntimeError(f"Failed to create desktop launcher: {e}") from e

        try:
            subprocess.run(["update-desktop-database", str(USER_DESKTOP_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

def deintegrate_package(pkg: Package) -> Log:
    sym = USER_BIN_DIR / pkg.name
    if sym.exists() or sym.is_symlink():
        sym.unlink()
        yield f"[OK] Removed symlink: {sym}"
    
    dt = USER_DESKTOP_DIR / f"{pkg.name}.desktop"
    if dt.exists():
        dt.unlink()
        yield f"[OK] Removed desktop launcher: {dt}"

    for ext in (".png", ".svg"):
        icon = USER_ICON_DIR / f"{pkg.name}{ext}"
        if icon.exists() or icon.is_symlink():
            icon.unlink()
            yield f"[OK] Removed icon: {icon}"

    try:
        subprocess.run(["update-desktop-database", str(USER_DESKTOP_DIR)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    config_paths = [
        Path.home() / ".config" / pkg.name,
        Path.home() / ".cache" / pkg.name,
        Path.home() / f".{pkg.name}",
    ]
    for path in config_paths:
        if path.exists():
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                yield f"[OK] Cleaned user config/cache: {path}"
            except Exception as e:
                yield f"[WARN] Could not remove {path}: {e}"

def extract_archive(archive: Path, name: str, target: Path | None = None) -> Log:
    target = target if target is not None else GOINFRE_BIN / name
    target.mkdir(parents=True, exist_ok=True)
    fn = archive.name.lower()
    yield f"[...] Extracting {archive.name}..."
    try:
        if fn.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")):
            with tarfile.open(archive) as tf:
                members = tf.getmembers()
                top = {m.name.split("/")[0] for m in members if "/" in m.name}
                if len(top) == 1:
                    t = top.pop()
                    for m in members:
                        m.path = m.path[len(t):].lstrip("/")
                        if m.path:
                            tf.extract(m, path=target)
                else:
                    tf.extractall(path=target)
        elif fn.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
                top = {n.split("/")[0] for n in names if "/" in n}
                if len(top) == 1:
                    t = top.pop() + "/"
                    for item in zf.infolist():
                        if item.filename.startswith(t):
                            item.filename = item.filename[len(t):]
                            if item.filename:
                                zf.extract(item, path=target)
                else:
                    zf.extractall(path=target)
        elif fn.endswith(".deb"):
            yield f"[...] Extracting Debian package {archive.name}..."
            try:
                subprocess.run(["dpkg", "-x", str(archive), str(target)], check=True)
            except Exception as e:
                raise RuntimeError(f"dpkg -x failed: {e}")
        elif fn.endswith(".appimage") or is_elf_file(archive):
            yield f"[...] Checking if {archive.name} is an AppImage..."
            is_extracted = False
            archive.chmod(0o755)
            temp_extract_dir = TMP_DIR / f"{name}_extract"
            temp_extract_dir.mkdir(parents=True, exist_ok=True)
            try:
                subprocess.run(
                    [str(archive), "--appimage-extract"],
                    cwd=temp_extract_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    check=True
                )
                squashfs_root = temp_extract_dir / "squashfs-root"
                if squashfs_root.exists():
                    for item in squashfs_root.iterdir():
                        shutil.move(str(item), str(target / item.name))
                    is_extracted = True
                    yield f"[OK] Extracted AppImage contents to {target}"
            except Exception:
                pass
            finally:
                shutil.rmtree(temp_extract_dir, ignore_errors=True)

            if not is_extracted:
                dest = target / name
                shutil.copy2(archive, dest)
                dest.chmod(0o755)
                yield f"[OK] Binary copied: {dest.name}"
        else:
            dest = target / archive.name
            shutil.copy2(archive, dest)
            dest.chmod(0o755)
            yield f"[OK] Binary copied: {dest.name}"
            return
        yield f"[OK] Extracted to {target}"
    except Exception as e:
        raise RuntimeError(f"Extraction failed: {e}")

def find_binary(pkg_dir: Path, name: str) -> Path | None:
    for c in (pkg_dir / name, pkg_dir / "bin" / name):
        if c.exists(): return c
    for d in (pkg_dir, pkg_dir / "bin"):
        if d.is_dir():
            for f in d.iterdir():
                if f.name.lower() == name.lower() and f.is_file():
                    return f
    return None

def auto_chmod(pkg_dir: Path, name: str) -> Log:
    b = find_package_executable(pkg_dir, name)
    if b:
        b.chmod(b.stat().st_mode | 0o111)
        yield f"[OK] chmod +x {b.relative_to(GOINFRE_BIN)}"
    else:
        yield f"[WARN] Could not locate binary '{name}' — may need post_cmd"

def run_post_cmd(cmd: str, pkg_dir: Path) -> Log:
    yield f"[...] Running post-install: {cmd}"
    env = os.environ.copy()
    env["PACKAGE_DIR"] = str(pkg_dir)
    env["GOINFRE_BIN"] = str(GOINFRE_BIN)
    result = subprocess.run(cmd, shell=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in result.stdout.splitlines():
        yield f"    │ {line}"
    if result.returncode != 0:
        raise RuntimeError(f"Post-install exited with code {result.returncode}")
    yield "[OK] Post-install done."

# ── Install / Remove orchestrators ────────────────────────────────────────────
@package_lock
def install_package(pkg: Package, metadata: dict | None = None):
    """Download and extract separately, then replace with rollback on failure."""
    GOINFRE_BIN.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    yield f"[INFO] ━━━ Installing {pkg.name} ━━━"
    target = GOINFRE_BIN / pkg.name
    stage = Path(tempfile.mkdtemp(prefix=f".{pkg.name}-stage-", dir=GOINFRE_BIN))
    backup = stage.with_name(stage.name.replace("-stage-", "-backup-"))
    switched = False
    try:
        record = target / ".thesolution-release.json"
        if metadata is not None and pkg.installed() and record.exists():
            if same_release(json.loads(record.read_text()), metadata):
                yield f"[OK] {pkg.name} was already updated by another worker."
                return
        metadata = metadata or download_metadata(pkg.url)
        with tempfile.TemporaryDirectory(prefix=f"{pkg.name}-", dir=TMP_DIR) as temporary:
            downloaded = None
            for message in DirectURLStrategy().resolve_and_download(metadata["resolved_url"], pkg.name, Path(temporary)):
                if isinstance(message, Path):
                    downloaded = message
                else:
                    yield message
            if downloaded is None:
                raise RuntimeError("Download did not produce a file")
            yield from extract_archive(downloaded, pkg.name, stage)
        if not find_package_executable(stage, pkg.name) and not pkg.post_cmd:
            raise RuntimeError(f"No executable found in {pkg.name}; keeping previous installation")
        if target.exists():
            target.rename(backup)
        stage.rename(target)
        switched = True
        if pkg.post_cmd:
            yield from run_post_cmd(pkg.post_cmd, target)
        if not find_package_executable(target, pkg.name):
            raise RuntimeError("Post-install did not produce an executable")
        yield from auto_chmod(target, pkg.name)
        yield from integrate_package(pkg)
        atomic_json(target / ".thesolution-release.json", metadata)
        desired = _read_desired_packages()
        if pkg.name not in desired:
            _write_desired_packages(desired + [pkg.name])
        shutil.rmtree(backup, ignore_errors=True)
        yield f"[OK] {pkg.name} installed successfully!"
    except Exception as error:
        if switched:
            shutil.rmtree(target, ignore_errors=True)
        if backup.exists():
            backup.rename(target)
            yield from integrate_package(pkg)
        elif switched:
            # Remove launchers for a failed first installation, not user settings.
            for link in (USER_BIN_DIR / pkg.name, USER_DESKTOP_DIR / f"{pkg.name}.desktop"):
                link.unlink(missing_ok=True)
        yield f"[ERROR] {error}"
        raise RuntimeError(str(error)) from error
    finally:
        shutil.rmtree(stage, ignore_errors=True)

@package_lock
def remove_package(pkg: Package) -> Log:
    d = GOINFRE_BIN / pkg.name
    if d.exists():
        yield f"[...] Removing {pkg.name}..."
        shutil.rmtree(d)
        yield f"[OK] {pkg.name} removed."
    else:
        yield f"[WARN] {pkg.name} is not installed."
    yield from deintegrate_package(pkg)
    # Forget desired packages even after /goinfre has been wiped.
    desired = _read_desired_packages()
    if pkg.name in desired:
        desired.remove(pkg.name)
        _write_desired_packages(desired)


# ══════════════════════════════════════════════════════════════════════════════
# ── Textual TUI ──────────────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════════

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical, Container, Center
    from textual.widgets import Static, RichLog, Input, ListItem, ListView, Button
    from textual.screen import ModalScreen
    from textual.reactive import reactive
    from textual.message import Message
    from textual.worker import Worker
    from textual import work
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False

if HAS_TEXTUAL:
    from rich.text import Text

    APP_CSS = """
    Screen {
        background: #0d1117;
        align: center middle;
    }
    #wrapper {
        width: auto;
        height: auto;
        align: center middle;
    }
    #app-title {
        width: 100%;
        text-align: center;
        color: #58a6ff;
        text-style: bold;
        margin: 0 0 1 0;
    }
    #box {
        width: 80;
        height: 40;
        min-width: 60;
        min-height: 30;
        max-width: 120;
        max-height: 60;
        border: round #58a6ff;
        background: #0d1117;
    }
    #panels {
        height: 1fr;
    }
    #left-panel {
        width: 1fr;
        border-right: solid #30363d;
    }
    #left-title {
        dock: top;
        height: 1;
        background: #1f6feb;
        color: #ffffff;
        text-align: center;
        text-style: bold;
    }
    #pkg-list {
        height: 1fr;
        background: #0d1117;
    }
    #pkg-list > ListItem {
        height: 1;
        padding: 0 0 0 1;
        background: #0d1117;
    }
    #pkg-list > ListItem.--highlight {
        background: #1a2740;
        color: #e6edf3;
    }
    #pkg-list:focus > ListItem.--highlight {
        background: #1f3058;
        color: #ffffff;
    }
    #right-panel {
        width: 1fr;
    }
    #right-title {
        dock: top;
        height: 1;
        background: #1f6feb;
        color: #ffffff;
        text-align: center;
        text-style: bold;
    }
    #detail-top {
        height: 1fr;
        border-bottom: solid #30363d;
    }
    #detail-info {
        height: 1fr;
        padding: 0 1;
        color: #c9d1d9;
    }
    #log-bottom {
        height: 1fr;
    }
    #log-separator {
        height: 1;
        background: #21262d;
        color: #8b949e;
        padding: 0 1;
    }
    #progress-bar {
        height: 1;
        padding: 0 1;
        background: #0d1117;
        color: #58a6ff;
        display: none;
    }
    #progress-bar.visible {
        display: block;
    }
    #log-area {
        height: 1fr;
        padding: 0 1;
        background: #0d1117;
        scrollbar-size: 1 1;
    }
    #bottom-bar {
        height: 1;
        width: 100%;
        margin: 1 0 0 0;
        text-align: center;
        color: #6e7681;
        background: transparent;
    }
    #filter-bar {
        dock: bottom;
        height: 1;
        display: none;
        background: #21262d;
    }
    #filter-bar.visible {
        display: block;
    }
    #filter-input {
        width: 1fr;
        background: #21262d;
        color: #c9d1d9;
        border: none;
    }
    PathModal {
        align: center middle;
    }
    #path-modal-box {
        width: 60;
        height: 12;
        border: round #58a6ff;
        background: #161b22;
        padding: 1 2;
    }
    #path-modal-title {
        height: 1;
        text-align: center;
        text-style: bold;
        color: #58a6ff;
    }
    #path-input {
        width: 1fr;
        margin: 1 0;
        background: #0d1117;
        color: #c9d1d9;
    }
    #path-preview {
        height: 1;
        color: #8b949e;
    }
    #path-error {
        height: 1;
        color: #f85149;
    }
    #path-buttons {
        height: 1;
        align: center middle;
        margin: 1 0 0 0;
    }
    #path-buttons Button {
        margin: 0 1;
        min-width: 10;
    }
    """

    class PkgLabel(Static):
        """A single package row in the list."""
        pass

    class PathModal(ModalScreen):
        """Modal popup to change the install root path."""
        BINDINGS = [("escape", "cancel", "Cancel")]

        def __init__(self, current_path: Path) -> None:
            super().__init__()
            self._current = current_path

        def compose(self) -> ComposeResult:
            with Vertical(id="path-modal-box"):
                yield Static("Change Install Path", id="path-modal-title")
                yield Input(value=str(self._current), id="path-input")
                yield Static(f"→ {self._current}", id="path-preview")
                yield Static("", id="path-error")
                with Center(id="path-buttons"):
                    yield Button("Save", variant="primary", id="path-save")
                    yield Button("Cancel", id="path-cancel")

        def on_mount(self) -> None:
            self.query_one("#path-input", Input).focus()

        def on_input_changed(self, event: Input.Changed) -> None:
            try:
                p = Path(event.value).expanduser().resolve()
                self.query_one("#path-preview", Static).update(f"→ {p}")
                self.query_one("#path-error", Static).update("")
            except Exception:
                self.query_one("#path-preview", Static).update("")
                self.query_one("#path-error", Static).update("Invalid path")

        def on_button_pressed(self, event: Button.Pressed) -> None:
            if event.button.id == "path-save":
                self._try_save()
            else:
                self.dismiss(None)

        def on_key(self, event) -> None:
            if event.key == "enter":
                self._try_save()
                event.prevent_default()
            elif event.key in ("escape", "esc"):
                self.dismiss(None)
                event.prevent_default()

        def action_cancel(self) -> None:
            self.dismiss(None)

        def _try_save(self) -> None:
            raw = self.query_one("#path-input", Input).value.strip()
            if not raw:
                self.query_one("#path-error", Static).update("Path cannot be empty")
                return
            try:
                p = Path(raw).expanduser().resolve()
                p.mkdir(parents=True, exist_ok=True)
                if not os.access(p, os.W_OK):
                    self.query_one("#path-error", Static).update("Path is not writable")
                    return
                self.dismiss(p)
            except Exception as e:
                self.query_one("#path-error", Static).update(f"Error: {e}")

    class GoinfreApp(App):
        TITLE = "goinfre"
        CSS = APP_CSS
        BINDINGS = [("ctrl+c", "quit", "Quit")]

        _pkg_logs: dict[str, list[str]] = {}

        packages: list[Package] = []
        filtered: list[int] = []  # indices into packages
        cursor: reactive[int] = reactive(0)
        g_pending: bool = False
        filter_text: str = ""
        busy: bool = False

        def compose(self) -> ComposeResult:
            with Vertical(id="wrapper"):
                yield Static("H0MZ0 •<(>_-)>", id="app-title")
                with Vertical(id="box"):
                    with Horizontal(id="panels"):
                        with Vertical(id="left-panel"):
                            yield Static(" Packages", id="left-title")
                            yield ListView(id="pkg-list")
                        with Vertical(id="right-panel"):
                            yield Static(" Details", id="right-title")
                            with Vertical(id="detail-top"):
                                yield Static("", id="detail-info")
                            with Vertical(id="log-bottom"):
                                yield Static(" Log", id="log-separator")
                                yield Static("", id="progress-bar")
                                yield RichLog(id="log-area", highlight=True, markup=True)
                    with Container(id="filter-bar"):
                        yield Input(placeholder=" Search...", id="filter-input")
                yield Static(
                    "[▲/▼]Move  [space]Select  [/]Search  [P]ath  [I]nstall  [R]emove  [A]ll  [Q]uit",
                    id="bottom-bar",
                    markup=False,
                )

        def on_mount(self) -> None:
            self.packages = parse_conf(CONF_FILE)
            self._pkg_logs = {p.name: [] for p in self.packages}
            self.filtered = list(range(len(self.packages)))
            self._rebuild_list()
            lv = self.query_one("#pkg-list", ListView)
            lv.focus()
            if self.filtered:
                self._update_detail()

        def _rebuild_list(self) -> None:
            lv = self.query_one("#pkg-list", ListView)
            lv.clear()
            for li, idx in enumerate(self.filtered):
                pkg = self.packages[idx]
                active = (li == self.cursor)
                lv.append(ListItem(PkgLabel(self._pkg_row_text(pkg, idx, active))))
            if self.filtered:
                safe = min(self.cursor, len(self.filtered) - 1)
                self.cursor = max(0, safe)
                lv.index = self.cursor

        def _pkg_row_text(self, pkg: Package, idx: int, is_active: bool = False) -> Text:
            t = Text()
            # Cursor indicator
            if is_active:
                t.append("▶ ", style="bold #58a6ff")
            else:
                t.append("  ")
            if pkg.selected:
                t.append("● ", style="bold yellow")
            else:
                t.append("  ")
            if pkg.installed():
                t.append("✔ ", style="bold green")
            else:
                t.append("✘ ", style="bold red")
            name_style = "bold #ffffff" if is_active else "bold #c9d1d9"
            t.append(f"{pkg.name:<14s}", style=name_style)
            t.append(f" {pkg.filename}", style="#8b949e")
            return t

        def _update_row(self, list_idx: int, is_active: bool = False) -> None:
            lv = self.query_one("#pkg-list", ListView)
            if 0 <= list_idx < len(lv.children):
                pkg_idx = self.filtered[list_idx]
                pkg = self.packages[pkg_idx]
                item = lv.children[list_idx]
                label = item.query_one(PkgLabel)
                label.update(self._pkg_row_text(pkg, pkg_idx, is_active))

        def _update_detail(self) -> None:
            if not self.filtered:
                self.query_one("#detail-info", Static).update("No packages.")
                return
            idx = self.filtered[self.cursor]
            pkg = self.packages[idx]
            inst = "[green]Installed ✔[/green]" if pkg.installed() else "[red]Not installed ✘[/red]"
            sel = "[yellow]● Selected[/yellow]" if pkg.selected else "[dim]Not selected[/dim]"
            stored = GOINFRE_BIN / pkg.name
            stored_style = "[green]" if pkg.installed() else "[dim]"
            stored_end = "[/green]" if pkg.installed() else "[/dim]"
            info_text = (
                f"[bold #58a6ff]Name:[/]      {pkg.name}\n"
                f"[bold #58a6ff]Source:[/]    [dim]{pkg.url[:55]}{'…' if len(pkg.url)>55 else ''}[/dim]\n"
                f"[bold #58a6ff]Type:[/]      {pkg.source_type}\n"
                f"[bold #58a6ff]Post cmd:[/]  {pkg.post_cmd or '—'}\n"
                f"[bold #58a6ff]Status:[/]    {inst}   {sel}\n"
                f"[bold #58a6ff]Stored at:[/] {stored_style}{stored}{stored_end}"
            )
            self.query_one("#detail-info", Static).update(info_text)

        def watch_cursor(self, old_value: int, value: int) -> None:
            lv = self.query_one("#pkg-list", ListView)
            if self.filtered and 0 <= old_value < len(self.filtered):
                self._update_row(old_value, is_active=False)
            if self.filtered and 0 <= value < len(self.filtered):
                lv.index = value
                self._update_row(value, is_active=True)
            self._update_detail()
            self._show_pkg_logs()

        def _move_cursor(self, delta: int) -> None:
            if not self.filtered:
                return
            self.cursor = max(0, min(len(self.filtered) - 1, self.cursor + delta))

        def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
            lv = self.query_one("#pkg-list", ListView)
            if lv.index is not None and 0 <= lv.index < len(self.filtered):
                self.cursor = lv.index
                self._update_detail()

        def _styled_text(self, msg: str) -> Text:
            if "[OK]" in msg:
                return Text(msg, style="green")
            elif "[ERROR]" in msg:
                return Text(msg, style="bold red")
            elif "[WARN]" in msg:
                return Text(msg, style="yellow")
            elif "[INFO]" in msg:
                return Text(msg, style="bold #58a6ff")
            elif "[...]" in msg:
                return Text(msg, style="cyan")
            return Text(msg, style="#8b949e")

        def _log(self, msg: str, pkg_name: str = "") -> None:
            """Store log line for a package and display if that pkg is active."""
            if pkg_name:
                if pkg_name not in self._pkg_logs:
                    self._pkg_logs[pkg_name] = []
                self._pkg_logs[pkg_name].append(msg)
            # Show only if the current package matches
            cur_name = self._current_pkg_name()
            if not pkg_name or pkg_name == cur_name:
                self.query_one("#log-area", RichLog).write(self._styled_text(msg))

        def _progress(self, pct: float, pkg_name: str = "") -> None:
            """Update the progress bar for a download."""
            bar_w = 30
            filled = int(pct / 100 * bar_w)
            bar = "█" * filled + "░" * (bar_w - filled)
            label = f" {bar}  {pct:5.1f}%"
            pbar = self.query_one("#progress-bar", Static)
            cur_name = self._current_pkg_name()
            if not pkg_name or pkg_name == cur_name:
                pbar.update(Text(label, style="bold #58a6ff"))
                if not pbar.has_class("visible"):
                    pbar.add_class("visible")
            if pct >= 100:
                pbar.remove_class("visible")

        def _current_pkg_name(self) -> str:
            if self.filtered and 0 <= self.cursor < len(self.filtered):
                return self.packages[self.filtered[self.cursor]].name
            return ""

        def _show_pkg_logs(self) -> None:
            """Replay stored logs for the currently highlighted package."""
            log_area = self.query_one("#log-area", RichLog)
            log_area.clear()
            name = self._current_pkg_name()
            for msg in self._pkg_logs.get(name, []):
                log_area.write(self._styled_text(msg))
            # Hide progress bar when switching packages
            self.query_one("#progress-bar", Static).remove_class("visible")

        def _apply_filter(self) -> None:
            ft = self.filter_text.lower().strip()
            if ft:
                self.filtered = [i for i, p in enumerate(self.packages) if ft in p.name.lower()]
            else:
                self.filtered = list(range(len(self.packages)))
            self.cursor = 0
            self._rebuild_list()

        # ── Key handling ──────────────────────────────────────────────────
        def on_key(self, event) -> None:
            fbar = self.query_one("#filter-bar", Container)
            finput = self.query_one("#filter-input", Input)

            # If filter bar is open, let Input handle keys except Escape/Enter
            if fbar.has_class("visible"):
                if event.key == "escape":
                    fbar.remove_class("visible")
                    self.filter_text = ""
                    self._apply_filter()
                    self.query_one("#pkg-list", ListView).focus()
                    event.prevent_default()
                    return
                if event.key == "enter":
                    fbar.remove_class("visible")
                    self.filter_text = finput.value
                    self._apply_filter()
                    self.query_one("#pkg-list", ListView).focus()
                    event.prevent_default()
                    return
                return  # let Input handle typing

            key = event.key
            event.prevent_default()

            # gg combo
            if self.g_pending:
                self.g_pending = False
                if key == "g":
                    self.cursor = 0
                    return

            if key == "down":
                self._move_cursor(1)
            elif key == "up":
                self._move_cursor(-1)
            elif key == "g":
                self.g_pending = True
            elif key == "G" or key == "shift+g":
                if self.filtered:
                    self.cursor = len(self.filtered) - 1
            elif key == "space":
                if self.filtered:
                    idx = self.filtered[self.cursor]
                    self.packages[idx].selected = not self.packages[idx].selected
                    self._update_row(self.cursor)
                    self._update_detail()
            elif key == "a":
                any_sel = any(self.packages[i].selected for i in self.filtered)
                for i in self.filtered:
                    self.packages[i].selected = not any_sel
                self._rebuild_list()
                self._update_detail()
            elif key == "i":
                if self.filtered and not self.busy:
                    idx = self.filtered[self.cursor]
                    self._install_packages([self.packages[idx]])
            elif key == "I" or key == "shift+i":
                if not self.busy:
                    selected = [self.packages[i] for i in self.filtered if self.packages[i].selected]
                    if not selected:
                        selected = [self.packages[i] for i in self.filtered if not self.packages[i].installed()]
                    if selected:
                        self._install_packages(selected)
            elif key == "r":
                if self.filtered and not self.busy:
                    idx = self.filtered[self.cursor]
                    self._remove_packages([self.packages[idx]])
            elif key == "R" or key == "shift+r":
                if not self.busy:
                    selected = [self.packages[i] for i in self.filtered if self.packages[i].selected]
                    if selected:
                        self._remove_packages(selected)
            elif key == "slash":
                fbar.add_class("visible")
                finput.value = ""
                finput.focus()
            elif key == "p":
                self.push_screen(PathModal(GOINFRE_BIN), self._on_path_changed)
            elif key == "q" or key in ("escape", "esc"):
                self.exit()

        def _on_path_changed(self, new_path: Path | None) -> None:
            """Callback from PathModal — update global path and refresh."""
            global GOINFRE_BIN
            if new_path is None:
                return
            GOINFRE_BIN = new_path
            _write_config_path(new_path)
            self._rebuild_list()
            self._update_detail()
            self.notify(f"Install root → {new_path}", timeout=3)

        # ── Workers ───────────────────────────────────────────────────────
        @work(thread=True, exclusive=True)
        def _install_packages(self, pkgs: list[Package]) -> None:
            self.busy = True
            for pkg in pkgs:
                try:
                    for msg in install_package(pkg):
                        if isinstance(msg, tuple) and msg[0] == "PROGRESS":
                            self.call_from_thread(self._progress, msg[1], pkg.name)
                        else:
                            self.call_from_thread(self._log, msg, pkg.name)
                except RuntimeError:
                    pass  # error already logged
                self.call_from_thread(self._refresh_pkg, pkg)
            self.busy = False

        @work(thread=True, exclusive=True)
        def _remove_packages(self, pkgs: list[Package]) -> None:
            self.busy = True
            for pkg in pkgs:
                try:
                    for msg in remove_package(pkg):
                        self.call_from_thread(self._log, msg, pkg.name)
                except RuntimeError:
                    pass
                self.call_from_thread(self._refresh_pkg, pkg)
            self.busy = False

        def _refresh_pkg(self, pkg: Package) -> None:
            for li, idx in enumerate(self.filtered):
                if self.packages[idx] is pkg:
                    self._update_row(li, is_active=(li == self.cursor))
                    break
            self._update_detail()


# ── Entry point ───────────────────────────────────────────────────────────────
def wait_for_internet(timeout: int = 5) -> bool:
    """Wait for an active internet connection by attempting HTTPS requests."""
    print(f"[INFO] Waiting for internet connection (timeout up to {timeout} seconds)...")
    start_time = time.time()
    urls = [
        "https://clients3.google.com/generate_204",
        "https://github.com"
    ]
    while time.time() - start_time < timeout:
        for url in urls:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "goinfre-pm"})
                with urllib.request.urlopen(req, timeout=4) as resp:
                    if resp.status in (200, 204):
                        print("[OK] Internet connection is active!")
                        return True
            except Exception:
                pass
        time.sleep(2)
    print("[WARN] Internet connection check timed out. Proceeding anyway.")
    return False

def run_auto_mode(update: bool = False) -> int:
    """Restore desired apps, optionally updating installed apps to current releases."""
    desired = set(_read_desired_packages())
    if update:
        desired.update(pkg.name for pkg in parse_conf(CONF_FILE) if pkg.installed())
    if not desired:
        print("[OK] No applications selected for restore or update.")
        return 0
    packages = {pkg.name: pkg for pkg in parse_conf(CONF_FILE)}
    failures = 0
    for name in sorted(desired):
        pkg = packages.get(name)
        if pkg is None:
            print(f"[WARN] {name} is missing from the catalog.")
            continue
        try:
            metadata = None
            if pkg.installed():
                if not update:
                    for line in integrate_package(pkg):
                        print(line)
                    continue
                metadata = download_metadata(pkg.url)
                record = GOINFRE_BIN / name / ".thesolution-release.json"
                previous = json.loads(record.read_text()) if record.exists() else {}
                if same_release(previous, metadata):
                    print(f"[OK] {name} is current.")
                    for line in integrate_package(pkg):
                        print(line)
                    continue
                print(f"[INFO] Updating {name}...")
            for message in install_package(pkg, metadata):
                if isinstance(message, str):
                    print(message)
        except Exception as error:
            failures += 1
            print(f"[ERROR] {name}: {error}")
    return 1 if failures else 0

def main():
    global GOINFRE_BIN, CONF_FILE
    parser = argparse.ArgumentParser(description="goinfre — Terminal Package Manager")
    parser.add_argument("--install-root", type=str, default=None,
                        help="Override install root path (default: ~/goinfre/bin)")
    parser.add_argument("--config", type=Path, default=CONF_FILE,
                        help="Use a custom packages.conf (default: bundled catalog)")
    parser.add_argument("-a", "--auto", action="store_true",
                        help="Automatically check and install previously selected packages")
    parser.add_argument("--update", action="store_true", help="Restore missing apps and update installed apps")
    args = parser.parse_args()
    CONF_FILE = args.config.expanduser().resolve()
    if not CONF_FILE.is_file():
        parser.error(f"Catalog not found: {CONF_FILE}")
    GOINFRE_BIN = resolve_install_root(args.install_root)
    if args.install_root:
        _write_config_path(GOINFRE_BIN)

    if args.auto or args.update:
        sys.exit(run_auto_mode(update=args.update))
    else:
        if not HAS_TEXTUAL:
            print("Install textual first:  pip install textual")
            sys.exit(1)
        app = GoinfreApp()
        app.run()


if __name__ == "__main__":
    main()
