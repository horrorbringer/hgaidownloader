#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

library="$(gcc -print-file-name=libatomic.so.1)"
if [[ ! -f "$library" ]]; then
    echo "libatomic.so.1 is missing from the Vercel build image." >&2
    exit 1
fi

mkdir -p runtime-lib
cp -L -- "$library" runtime-lib/libatomic.so.1
