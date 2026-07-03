#!/bin/sh
# goinfre installer — POSIX sh, safe, idempotent
set -e

# ── Colors ────────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
CYAN='\033[0;36m'
RED='\033[0;31m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

info()    { printf "${CYAN}[...]${NC} %s\n" "$1"; }
success() { printf "${GREEN}  ✔${NC}  %s\n" "$1"; }
warn()    { printf "${RED}[WARN]${NC} %s\n" "$1"; }
die()     { printf "${RED}[ERROR]${NC} %s\n" "$1"; exit 1; }

# ── Pre-flight checks ────────────────────────────────────────────────────────
command -v curl >/dev/null 2>&1 || die "curl is required but not found. Install it first."

if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 not found — goinfre requires Python 3.11+."
    warn "Install python3 before running theSolution."
fi

# ── 1. Download/Copy goinfre.py ───────────────────────────────────────────────
INSTALL_DIR="$HOME/.local"
INSTALL_PATH="$INSTALL_DIR/goinfre.py"
DOWNLOAD_URL="https://raw.githubusercontent.com/H0MZ0/theSolution/main/goinfre.py"
CONFIG_URL="https://raw.githubusercontent.com/H0MZ0/theSolution/main/packages.conf"

mkdir -p "$INSTALL_DIR"

if [ -f "./goinfre.py" ] && [ -f "./packages.conf" ]; then
    info "Installing local goinfre.py → $INSTALL_PATH"
    cp "./goinfre.py" "$INSTALL_PATH"
    info "Installing local packages.conf → $INSTALL_DIR/packages.conf"
    cp "./packages.conf" "$INSTALL_DIR/packages.conf"
    success "Installed local goinfre.py and packages.conf"
else
    info "Downloading goinfre.py → $INSTALL_PATH"
    curl -fsSL "$DOWNLOAD_URL" -o "$INSTALL_PATH"
    info "Downloading packages.conf → $INSTALL_DIR/packages.conf"
    curl -fsSL "$CONFIG_URL" -o "$INSTALL_DIR/packages.conf"
    success "Downloaded goinfre.py and packages.conf"
fi

# ── 2. Permissions ────────────────────────────────────────────────────────────
chmod +x "$INSTALL_PATH"
success "Set executable permissions"

# ── 3. Shell alias and PATH ───────────────────────────────────────────────────
ALIAS_LINE='alias theSolution="python3 $HOME/.local/goinfre.py"'
FISH_ALIAS='alias theSolution "python3 $HOME/.local/goinfre.py"'
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
FISH_PATH='set -gx PATH $HOME/.local/bin $PATH'
RCFILE=""
ADDED=0
PATH_ADDED=0

detect_rc() {
    case "$(basename "$SHELL")" in
        bash)
            RCFILE="$HOME/.bashrc"
            ;;
        zsh)
            RCFILE="$HOME/.zshrc"
            ;;
        fish)
            RCFILE="$HOME/.config/fish/config.fish"
            ;;
        *)
            RCFILE=""
            ;;
    esac
}

detect_rc

if [ -n "$RCFILE" ]; then
    # Ensure the rc file exists
    mkdir -p "$(dirname "$RCFILE")"
    touch "$RCFILE"

    # Check for existing alias to avoid duplicates
    if grep -qF "theSolution" "$RCFILE" 2>/dev/null; then
        success "Alias theSolution already present in $RCFILE"
        ADDED=1
    else
        case "$(basename "$SHELL")" in
            fish)
                printf '\n# goinfre package manager alias\n%s\n' "$FISH_ALIAS" >> "$RCFILE"
                ;;
            *)
                printf '\n# goinfre package manager alias\n%s\n' "$ALIAS_LINE" >> "$RCFILE"
                ;;
        esac
        success "Alias theSolution added to $RCFILE"
        ADDED=1
    fi

    # Check for ~/.local/bin in PATH to avoid duplicates
    if grep -qF ".local/bin" "$RCFILE" 2>/dev/null; then
        success "~/.local/bin already in PATH configuration in $RCFILE"
        PATH_ADDED=1
    else
        case "$(basename "$SHELL")" in
            fish)
                printf '\n# goinfre package manager path\n%s\n' "$FISH_PATH" >> "$RCFILE"
                ;;
            *)
                printf '\n# goinfre package manager path\n%s\n' "$PATH_LINE" >> "$RCFILE"
                ;;
        esac
        success "~/.local/bin added to PATH in $RCFILE"
        PATH_ADDED=1
    fi
else
    warn "Unknown shell: $SHELL"
    printf "${DIM}  Add this alias manually to your shell config:${NC}\n"
    printf "    %s\n" "$ALIAS_LINE"
    printf "${DIM}  Add this path manually to your shell config:${NC}\n"
    printf "    %s\n" "$PATH_LINE"
fi

# ── 4. Done ───────────────────────────────────────────────────────────────────
printf "\n"
printf "${BOLD}${GREEN}━━━ goinfre installed ━━━${NC}\n"
printf "\n"
success "goinfre.py → $INSTALL_PATH"
if [ "$ADDED" = "1" ] && [ -n "$RCFILE" ]; then
    success "alias theSolution added to $RCFILE"
fi
if [ "$PATH_ADDED" = "1" ] && [ -n "$RCFILE" ]; then
    success "~/.local/bin added to PATH in $RCFILE"
fi
if [ -n "$RCFILE" ]; then
    printf "\n"
    printf "${CYAN}  →${NC} Restart your shell or run: ${BOLD}source $RCFILE${NC}\n"
else
    printf "\n"
    printf "${CYAN}  →${NC} Add the alias and path to your shell config manually.\n"
fi
printf "${CYAN}  →${NC} Then just type: ${BOLD}theSolution${NC}\n"
printf "\n"
