#!/bin/bash
# Mac side is now a thin client: pull the cloud-built targets list for the walker.
TOKEN=$(cat ~/collx_pipeline/pages.conf)
curl -sL -H "Accept: application/vnd.github.v3.raw" -H "Authorization: token $TOKEN" \
  https://api.github.com/repos/asweinstein-lab/collx-targets/contents/targets.txt \
  -o ~/collx_targets.txt.tmp && \
  [ -s ~/collx_targets.txt.tmp ] && mv ~/collx_targets.txt.tmp ~/collx_targets.txt && \
  echo "$(date): targets synced $(grep -c . ~/collx_targets.txt) lines" >> ~/collx_data/fetch.log
