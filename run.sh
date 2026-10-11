#!/bin/bash
python3 -m pip install --break-system-packages -q pandas numpy 2>/dev/null || pip install -q pandas numpy
WORKDIR=. python3 collx_cloud.py > status.txt 2>&1
echo "exit: $?" >> status.txt
git config user.name "collx-bot"
git config user.email "bot@users.noreply.github.com"
git add -A
git diff --cached --quiet || git commit -m "hourly update"
git push
