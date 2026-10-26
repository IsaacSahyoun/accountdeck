#!/bin/bash
# Keeps accounts and settings unless --delete-data is explicitly requested.
cd -- "$(dirname -- "${BASH_SOURCE[0]}")" || exit 1
printf 'Quit AccountDeck 2 from its menu bar menu before continuing.\n'
bash ./uninstall.sh "$@"
status=$?
printf '\n'
if [ "$status" -ne 0 ]; then
    printf 'Uninstall stopped. Read the message above for help.\n'
fi
if [ -t 0 ]; then
    read -r -p 'Press Return to close this window. ' || true
fi
exit "$status"
