#!/bin/bash
python3 -m pip install --break-system-packages -q pandas numpy 2>/dev/null || pip install -q pandas numpy
WORKDIR=. python3 collx_cloud.py > status.txt 2>&1
RC=$?
echo "exit: $RC" >> status.txt
if [ $RC -ne 0 ]; then
  # failed run: discard all half-written state, report status only
  cp status.txt /tmp/status.txt
  git checkout -- . 2>/dev/null
  git clean -fd --exclude=.git 2>/dev/null
  cp /tmp/status.txt status.txt
fi
git config user.name "collx-bot"
git config user.email "bot@users.noreply.github.com"
git add -A
git diff --cached --quiet || git commit -m "hourly update"
git push
