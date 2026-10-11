#!/bin/bash
set -e
pip install -q pandas numpy
WORKDIR=. python3 collx_cloud.py
git config user.name "collx-bot"
git config user.email "bot@users.noreply.github.com"
git add -A
git diff --cached --quiet || git commit -m "hourly update"
git push
