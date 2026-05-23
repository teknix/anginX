FROM alpine:3.21

RUN apk add --no-cache nginx python3 py3-pip supervisor curl && \
    pip3 install --no-cache-dir --break-system-packages flask gunicorn certbot

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY nginx.conf /etc/nginx/nginx.conf
COPY conf.base/  /etc/nginx/conf.base/
COPY supervisord.conf /etc/supervisor/conf.d/anginx.conf
COPY entrypoint/ entrypoint/
RUN chmod +x entrypoint/*.sh

COPY app.py .
COPY templates/ templates/

RUN mkdir -p \
    /etc/nginx/conf.d \
    /etc/nginx/conf.d/ssl \
    /etc/nginx/certs \
    /var/www/acme/.well-known/acme-challenge \
    /var/lib/certbot \
    /var/log/certbot \
    /var/log/supervisor

EXPOSE 80 443

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -sf http://localhost/health || exit 1

ENTRYPOINT ["/app/entrypoint/docker-entrypoint.sh"]
