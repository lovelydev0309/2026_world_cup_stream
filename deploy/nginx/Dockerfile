# Copyright (c) 2026 lovelydev0309. All rights reserved.
# Proprietary and confidential. See LICENSE. Unauthorized copying, modification or use is prohibited.
FROM alfg/nginx-rtmp:latest

# Install curl for healthcheck
RUN apk add --no-cache curl

# Copy custom nginx config
COPY nginx.conf /etc/nginx/nginx.conf

# HLS segment output directory
RUN mkdir -p /var/www/hls /var/www/player && \
    chmod 755 /var/www/hls /var/www/player

EXPOSE 1935 8080

CMD ["nginx", "-g", "daemon off;"]
