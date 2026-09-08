#!/bin/sh
# Install Sailfish Phone Control for the current Linux user. No root access or
# third-party Python packages are required.
set -eu

show_usage() {
    printf '%s\n' "Usage: $0 [--enable-listener]"
    printf '%s\n' "  --enable-listener  also install and start the optional background discovery service"
}

enable_listener=false
case "${1:-}" in
    "") ;;
    --enable-listener) enable_listener=true ;;
    -h|--help) show_usage; exit 0 ;;
    *) show_usage >&2; exit 2 ;;
esac

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_bin="${HOME}/.local/bin"
install_apps="${HOME}/.local/share/applications"
install_systemd="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
program_target="${install_bin}/sailfish-mother-pc-helper"
desktop_target="${install_apps}/sailfish-phone-control.desktop"

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "Python 3 is required but was not found." >&2
    exit 1
fi

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else "Python 3.9 or newer is required.")'

if [ ! -f "${source_dir}/sailfish_mother_pc_helper.py" ] || [ ! -f "${source_dir}/sailfish-phone-control.desktop" ]; then
    printf '%s\n' "Run this installer from the Sailfish Mother-PC Helper source directory." >&2
    exit 1
fi

mkdir -p "${install_bin}" "${install_apps}"
install -m 755 "${source_dir}/sailfish_mother_pc_helper.py" "${program_target}"
install -m 644 "${source_dir}/sailfish-phone-control.desktop" "${desktop_target}"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "${desktop_target}"
fi

if [ "${enable_listener}" = true ]; then
    mkdir -p "${install_systemd}"
    install -m 644 "${source_dir}/sailfish-mother-pc-helper.service" \
        "${install_systemd}/sailfish-mother-pc-helper.service"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload
        systemctl --user enable --now sailfish-mother-pc-helper.service
    else
        printf '%s\n' "The listener service was installed but systemctl is unavailable; start it manually." >&2
    fi
fi

printf '%s\n' "Installed Sailfish Phone Control."
printf '%s\n' "Open it from the desktop menu, or run: ${program_target} gui"
if [ "${enable_listener}" = true ]; then
    printf '%s\n' "The background listener is enabled. The GUI will use its state with: sailfish-mother-pc-helper gui --no-udp"
fi
