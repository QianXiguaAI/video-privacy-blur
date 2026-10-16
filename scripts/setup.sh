#!/bin/bash
# Bootstrap without Python. --check is read-only; --install repairs missing tools.
set -euo pipefail

mode="${1:---check}"
if [ "$#" -gt 1 ]; then
    printf 'Usage: bash scripts/setup.sh [--check|--install]\n' >&2
    exit 1
fi
case "$mode" in
    --check|--install) ;;
    --help|-h) printf 'Usage: bash scripts/setup.sh [--check|--install]\n'; exit 0 ;;
    *) printf 'Unknown option: %s\n' "$mode" >&2; exit 1 ;;
esac

if [ "$(uname -s)" != Darwin ]; then
    printf 'Unsupported platform: Apple Vision OCR requires macOS. No packages were installed.\n' >&2
    exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
brew_path="$(command -v brew || true)"
if [ -z "$brew_path" ]; then
    case "$(uname -m)" in
        arm64) brew_candidate=/opt/homebrew/bin/brew ;;
        x86_64) brew_candidate=/usr/local/bin/brew ;;
        *) brew_candidate= ;;
    esac
    if [ -n "$brew_candidate" ] && [ -x "$brew_candidate" ]; then
        brew_path="$brew_candidate"
    fi
fi

use_brew_path() {
    local prefix
    prefix="$("$brew_path" --prefix)" || return 1
    if [ "${1:-append}" = prefer ]; then
        export PATH="$prefix/bin:$prefix/sbin:$PATH"
    else
        export PATH="$PATH:$prefix/bin:$prefix/sbin"
    fi
    hash -r
}
if [ -n "$brew_path" ]; then
    if ! use_brew_path; then
        printf 'Homebrew could not report its prefix; checking the existing tool paths first.\n' >&2
    fi
fi

# Check developer tools before invoking Apple's python3/swiftc launcher stubs.
if ! xcode-select -p >/dev/null 2>&1; then
    if [ "$mode" = --install ]; then
        xcode-select --install || true
        printf 'PENDING: Complete the Command Line Tools installation window, then rerun this command. Installation is not yet verified.\n' >&2
    else
        printf 'MISSING: Xcode Command Line Tools. Run with --install to request installation.\n' >&2
    fi
    exit 2
fi
if ! xcrun --find swiftc >/dev/null 2>&1 || ! swiftc --version >/dev/null 2>&1; then
    printf 'BROKEN: The selected developer toolchain cannot run Swift. Check xcode-select -p and xcrun --find swiftc; resolve any Xcode license or toolchain error, then retry.\n' >&2
    exit 2
fi

python_ready() {
    command -v python3 >/dev/null 2>&1 && python3 -c \
        'import argparse, hashlib, json, pathlib, subprocess, sys, tempfile; raise SystemExit(sys.version_info < (3, 9))' >/dev/null 2>&1
}
ffmpeg_ready() {
    command -v ffmpeg >/dev/null 2>&1 && ffmpeg -version >/dev/null 2>&1 && \
        command -v ffprobe >/dev/null 2>&1 && ffprobe -version >/dev/null 2>&1
}

packages=()
if ! python_ready; then packages+=(python); fi
if ! ffmpeg_ready; then packages+=(ffmpeg); fi

if [ "${#packages[@]}" -gt 0 ]; then
    if [ "$mode" = --check ]; then
        printf 'MISSING or unusable: %s\n' "${packages[*]}" >&2
        printf 'Run bash scripts/setup.sh --install to install only these dependencies.\n' >&2
        exit 2
    fi
    if [ -z "$brew_path" ]; then
        printf 'Homebrew is needed for: %s\n' "${packages[*]}" >&2
        # Use the official installer without password prompts. Do not modify shell profiles.
        setup_tmp="$(mktemp -d "${TMPDIR:-/tmp}/video-privacy-setup.XXXXXX")"
        trap 'rm -rf "$setup_tmp"' EXIT
        curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 120 \
            https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh \
            --output "$setup_tmp/homebrew-install.sh"
        /bin/bash -n "$setup_tmp/homebrew-install.sh"
        if ! NONINTERACTIVE=1 /bin/bash "$setup_tmp/homebrew-install.sh"; then
            printf 'Homebrew installation did not complete. Resolve the reported network/permission requirement, then retry; no password is read by this script.\n' >&2
            exit 1
        fi
        case "$(uname -m)" in
            arm64) brew_path=/opt/homebrew/bin/brew ;;
            x86_64) brew_path=/usr/local/bin/brew ;;
            *) printf 'Cannot locate the installed Homebrew prefix.\n' >&2; exit 1 ;;
        esac
        if [ ! -x "$brew_path" ]; then
            printf 'Homebrew installer returned success but brew was not found.\n' >&2
            exit 1
        fi
        use_brew_path
    fi
    printf 'Installing missing dependencies: %s\n' "${packages[*]}" >&2
    "$brew_path" install "${packages[@]}"
    use_brew_path prefer
    if ! python_ready || ! ffmpeg_ready; then
        printf 'Dependencies remain unusable after installation. Inspect the selected executable paths; do not continue to video processing.\n' >&2
        exit 2
    fi
fi

# Exercise the real OCR compiler and rendering features, not just command paths.
python3 "$script_dir/video_privacy.py" doctor --full
printf 'Environment verified. Continue the pending video task using the tool paths above.\n' >&2
if [ -n "$brew_path" ]; then
    printf 'This script only updates its own PATH. For later commands, use brew at %s and the reported absolute tool paths, or load its shellenv in the current task shell.\n' "$brew_path" >&2
fi
