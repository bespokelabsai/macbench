#!/bin/bash
# Starter: a compressor that only copies bytes. Replace it.
set -e
cd "$(dirname "$0")"
mkdir -p bin
printf '#!/bin/sh\nexec cp "$1" "$2"\n' > bin/compress
printf '#!/bin/sh\nexec cp "$1" "$2"\n' > bin/decompress
chmod +x bin/compress bin/decompress
