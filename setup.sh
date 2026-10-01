#!/bin/sh
# Fills in your GitHub username and name. Works on macOS and Linux.
# Usage: sh setup.sh <github-username> "Your Name"
set -e
if [ $# -ne 2 ]; then
  echo 'Usage: sh setup.sh <github-username> "Your Name"' >&2
  exit 1
fi
cd "$(dirname "$0")"
U=$(printf '%s' "$1" | tr 'A-Z' 'a-z')   # container registry names must be lowercase
N=$2
export U N
for f in hangar-pilot-logbook/umbrel-app.yml hangar-pilot-logbook/docker-compose.yml README.md; do
  perl -pi -e 'BEGIN { $u = $ENV{U}; $n = $ENV{N}; } s/YOUR-GITHUB-USER/$u/g; s/Your Name/$n/g' "$f"
done
echo "Done. Repo URL will be https://github.com/$U/pilot-logbook-umbrel"
grep -rn "YOUR-GITHUB-USER\|Your Name" hangar-pilot-logbook README.md && echo "Some placeholders are left above." || echo "No placeholders left."
