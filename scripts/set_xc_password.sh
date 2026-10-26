#!/bin/bash
# Ask for the XC API certificate password in a macOS dialog and store it in the repo-root .env
# (XC_API_P12_PASSWORD). The password never appears on screen, in shell history or in a log.
# Usage (from the repo root, also as a Claude Code `!` command): scripts/set_xc_password.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "no .env here: copy .env.example to .env first" >&2; exit 1; }

P=$(osascript -e 'text returned of (display dialog "XC API certificate password:" default answer "" with hidden answer with title "Copperline")') \
  || { echo "cancelled; nothing changed" >&2; exit 1; }
[ -n "$P" ] || { echo "empty password; nothing changed" >&2; exit 1; }

# Python does the replacement so any character in the password (| & $ \ /) is written as-is.
XC_P12_PASSWORD="$P" python3 - <<'EOF'
import os, re
text = open(".env").read()
new, count = re.subn(r"(?m)^XC_API_P12_PASSWORD=.*$", lambda m: "XC_API_P12_PASSWORD=" + os.environ["XC_P12_PASSWORD"], text)
if count == 0:
    new = text.rstrip("\n") + "\nXC_API_P12_PASSWORD=" + os.environ["XC_P12_PASSWORD"] + "\n"
open(".env", "w").write(new)
EOF
unset P
echo "saved XC_API_P12_PASSWORD in .env"
