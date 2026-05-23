#!/bin/sh
set -e

# Restore SSL fragments for any certs already in the named volume.
# This handles container restarts where certs exist but fragments need regenerating.
/app/entrypoint/generate-ssl-frags.sh

exec /usr/bin/supervisord -c /etc/supervisor/conf.d/anginx.conf
