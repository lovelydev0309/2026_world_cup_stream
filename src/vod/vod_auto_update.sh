#!/bin/bash
# Copyright (c) 2026 lovelydev0309. All rights reserved.
# Proprietary and confidential. See LICENSE. Unauthorized copying, modification or use is prohibited.
# Auto-update program (client rule): pull NEW 2026+ movies rated >=7 into the catalog.
# Idempotent (skips titles already present), Spanish/Latino first, gentle (sequential,
# one provider connection, 15GB disk floor). Runs daily via cron; flock prevents overlap.
cd /opt/streaming-stack/src/vod || exit 1
export VOD_QUALITY=1 VOD_ONLY_NEW=1
flock -n /tmp/vod_autoupdate.lock /usr/bin/python3 vod_ingest2.py 15 15 \
  >> /opt/streaming-stack/vod-disk/autoupdate.log 2>&1
