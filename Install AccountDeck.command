#!/bin/bash
# Finder opens this in Terminal. Resolve the package folder even when its name contains spaces.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
bash ./install.sh "$@"
status=$?
printf '\n'
if [ "$status" -ne 0 ]; then
    printf 'Installation stopped. Read the message above or START HERE.txt for help.\n'
fi
if [ -t 0 ]; then
    read -r -p 'Press Return to close this window. ' || true
fi
exit "$status"
