FROM python:3.12-alpine3.21

# https://stackoverflow.com/questions/58701233/docker-logs-erroneously-appears-empty-until-container-stops
ENV PYTHONUNBUFFERED=1

# Define build argument with default value
ARG VERSION=dev
# Set it as an environment variable
ENV VERSION=$VERSION

WORKDIR /yamtrack

# System dependencies — cached until alpine or nginx version changes
RUN apk add --no-cache nginx shadow \
    && mkdir -p /var/log/nginx /var/lib/nginx/body \
    && useradd -U -M -s /bin/sh abc

# Python dependencies — cached until requirements.txt changes
COPY ./requirements.txt /requirements.txt
RUN pip install -r /requirements.txt \
    && pip install supervisor==4.3.0

# Config files — cached until configs change
COPY ./entrypoint.sh /entrypoint.sh
COPY ./supervisord.conf /etc/supervisord.conf
COPY ./nginx.conf /etc/nginx/nginx.conf
RUN sed 's/listen 8000;/listen 8000; listen [::]:8000;/' /etc/nginx/nginx.conf > /etc/nginx/nginx.ipv6.conf \
    && chmod +x /entrypoint.sh

# Django app — changes every build, but COPY is fast
COPY src ./
RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["/entrypoint.sh"]

HEALTHCHECK --interval=45s --timeout=15s --start-period=30s --retries=5 \
  CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:8000/health/ || exit 1
