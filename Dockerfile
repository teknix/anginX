FROM nginx:1.27-alpine

RUN apk add --no-cache python3 py3-pip supervisor curl && \
    pip3 install --no-cache-dir --break-system-packages flask gunicorn

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY nginx.conf /etc/nginx/nginx.conf
COPY conf.base/  /etc/nginx/conf.base/
COPY supervisord.conf /etc/supervisor/conf.d/anginx.conf
COPY entrypoint/ entrypoint/
RUN chmod +x entrypoint/start-gunicorn.sh

COPY app.py .
COPY templates/ templates/

RUN mkdir -p /etc/nginx/conf.d /etc/nginx/conf.d/ssl /etc/nginx/certs /var/log/supervisor

# conf.d is the named volume — generate an empty default so nginx starts cleanly
RUN echo "# anginx managed conf.d" > /etc/nginx/conf.d/.keep

EXPOSE 80 443

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost/health || exit 1

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/anginx.conf"]
