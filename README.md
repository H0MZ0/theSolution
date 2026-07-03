# theSolution — Professional Package Manager for 1337 and 42 Schools

`theSolution` is a terminal-based package manager designed to solve storage limit issues on 1337 and 42 school computers by installing large applications (like browsers, IDEs, and tools) in `/goinfre` while integrating them seamlessly into the user's home directory.

---

## Acknowledgments

This project was inspired by the work of:
* **Mohamed El Mouhib**: Creator of [Fixgoinfre](https://github.com/Mohamed-El-Mouhib/Fixgoinfre.git).
* **Achraf Ennadiri**: Creator of the *zero-tow* project ([GitHub Profile](https://github.com/ac-ennadi)).

## 🌟 Features

- **Textual TUI (Terminal User Interface)**: Interactive dashboard to search, install, and remove applications.
- **FUSE-Free AppImage Extraction**: Automatically extracts AppImages using `--appimage-extract` and runs them via their `AppRun` binary to bypass 42 mount restrictions.
- **Native Debian Extraction**: Supports downloading and extracting `.deb` packages via `dpkg -x` to `/goinfre`.
- **System Integration**:
  - Automatically creates symlinks under `~/.local/bin/` so you can run tools from any terminal.
  - Automatically copies icons and creates `.desktop` launchers under `~/.local/share/applications/` so apps appear in your system applications menu.
  - Runs `update-desktop-database` to refresh launchers instantly.
- **Space Pruning**: Removes configurations/caches under `~/.config` and `~/.cache` when uninstalling packages.
- **Auto-Restore on Login**: Can automatically reinstall all previously chosen packages if you change your place (or delete).

---

## 🚀 the ultimat setup

To install `theSolution` on your account, run:

```bash
git clone https://github.com/H0MZ0/theSolution.git
cd theSolution
chmod +x install.sh
./install.sh
source ~/.zshrc
mkdir -p ~/.config/autostart && cat << 'EOF' > ~/.config/autostart/goinfre-auto.desktop
[Desktop Entry]
Type=Application
Exec=python3 /home/hakader/.local/goinfre.py --auto
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Name=Goinfre Auto Installer
Comment=Reinstall missing goinfre packages on startup
EOF
```

This installer copies the scripts to `~/.local/` and automatically configures your shell (`.zshrc` / `.bashrc` / `config.fish`):
1. Registers the alias `theSolution` to launch the manager.
2. Appends `~/.local/bin` to your `PATH` so your installed apps are runnable directly.
3. Creates an auto-start entry to restore your packages on login.


---

## 🎮 How to Use (TUI Controls)

To open the package manager, simply type:

```bash
theSolution
```

### Keyboard Shortcuts:
| Key | Action |
|---|---|
| `Space` | Select / Deselect package |
| `i` | Install selected package under cursor |
| `Shift + I` (or `I`) | Install all selected packages (or all missing packages if none selected) |
| `r` | Uninstall package under cursor (fully prunes storage and links) |
| `Shift + R` (or `R`) | Uninstall all selected packages |
| `/` | Open filter bar (search/filter packages dynamically) |
| `p` | Change `/goinfre` installation root path |
| `q` or `Esc` | Quit the package manager |

---

## ⚙️ Configuration (`packages.conf`)

You can add or update applications in `~/.local/packages.conf`. The format is:

```text
# PackageName          URL
vscode                 https://code.visualstudio.com/sha/download?build=stable&os=linux-deb-x64
brave-browser-nightly  https://github.com/brave/brave-browser/releases/download/v1.83.72/brave-browser-nightly_1.83.72_amd64.deb
```

- **Executables Lookup**: The manager uses a smart, self-healing recursive search to find executable binaries and link them. It also matches common package name aliases automatically.

---

## 🔄 Automatic Restore on Startup/Login

Since 1337 and 42 school computers regularly wipe local `/goinfre` storage, you can automate re-installation of your desired packages at login.
