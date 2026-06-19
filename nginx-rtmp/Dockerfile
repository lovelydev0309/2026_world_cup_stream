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
