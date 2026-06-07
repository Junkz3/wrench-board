#!/usr/bin/env bash
# Mirror CDN dependencies into web/vendor/ for an offline-resilient demo.
#
# Use case: the demo venue's wifi blocks d3js.org / cdn.jsdelivr.net /
# fonts.googleapis.com. Without this fallback the frontend silently
# breaks (boardview won't render, chat markdown won't sanitise).
#
# Usage : bash scripts/pin_cdn.sh
# Result: web/vendor/{d3.v7.min.js, marked.min.js, purify.min.js}
#         (gitignored, regenerated on demand)
#
# To activate the offline assets, swap the CDN URLs in web/index.html for
# /vendor/<filename> — left as a manual step so we don't pay the offline
# cost in normal demos. Run this script first, then rewrite the <script>
# tags during the demo prep checklist.
set -euo pipefail

VENDOR="web/vendor"
mkdir -p "$VENDOR"

curl -sSL https://d3js.org/d3.v7.min.js                                -o "$VENDOR/d3.v7.min.js"
curl -sSL https://cdn.jsdelivr.net/npm/marked/marked.min.js            -o "$VENDOR/marked.min.js"
curl -sSL https://cdn.jsdelivr.net/npm/dompurify@3/dist/purify.min.js  -o "$VENDOR/purify.min.js"

echo "✓ CDN pinned to $VENDOR/"
ls -lh "$VENDOR" | tail -n +2
