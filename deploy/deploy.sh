#!/usr/bin/env bash
# Deploy the Film Buffs bot: bring /srv/film-buffs-bot up to origin/main and restart it.
#
#   deploy/deploy.sh            # deploy origin/main
#   deploy/deploy.sh <ref>      # deploy a branch, tag or commit instead (e.g. to roll back)
#
# Run it from anywhere, as dev. Push your commits first: the live copy pulls from GitHub,
# never from ~/src. The live copy must have no local edits; the script stops if it does.
set -euo pipefail

LIVE=/srv/film-buffs-bot
UNIT=film-buffs-bot.service
REF=${1:-origin/main}

cd "$LIVE"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Live copy has local changes; refusing to deploy over them:" >&2
  git status --short >&2
  exit 1
fi

before=$(git rev-parse --short HEAD)
git fetch --quiet origin
git checkout --quiet --detach "$REF"
after=$(git rev-parse --short HEAD)
echo "Code: $before -> $after ($(git log -1 --format=%s))"

uv sync --frozen --quiet

if ! cmp -s "deploy/$UNIT" "/etc/systemd/system/$UNIT"; then
  echo "Unit file changed; installing it."
  sudo install -m 644 "deploy/$UNIT" /etc/systemd/system/
  sudo systemctl daemon-reload
fi

sudo systemctl restart "$UNIT"
sleep 8
if systemctl is-active --quiet "$UNIT"; then
  echo "Bot is running."
else
  echo "Bot is NOT running after restart:" >&2
fi
journalctl -u "$UNIT" -n 10 --no-pager -q || true
systemctl is-active --quiet "$UNIT"
