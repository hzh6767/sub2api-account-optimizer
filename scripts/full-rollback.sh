#!/bin/sh
set -eu

ROOT=${SUB2API_ROOT:-/opt/sub2api}
OPTIMIZER_DIR=${OPTIMIZER_DIR:-$ROOT/account-optimizer}
: "${ACTIVATION_BACKUP:?Set ACTIVATION_BACKUP to the immutable deployment backup directory}"
SUB2API_ADMIN_BASE_URL=${SUB2API_ADMIN_BASE_URL:-http://127.0.0.1:8080}
SUB2API_CONTAINER=${SUB2API_CONTAINER:-sub2api}
IMAGE_RECORD="$ACTIVATION_BACKUP/sub2api-image.txt"
KEY_FILE="$OPTIMIZER_DIR/secrets/admin-api-key"

test -f "$IMAGE_RECORD"
test -f "$KEY_FILE"

read -r ORIGINAL_IMAGE_ID ORIGINAL_IMAGE_REF < "$IMAGE_RECORD"

docker image inspect "$ORIGINAL_IMAGE_ID" >/dev/null

docker compose --env-file "$ROOT/.env" \
  --env-file "$OPTIMIZER_DIR/activation.env" \
  -f "$OPTIMIZER_DIR/compose.yml" --profile optimizer stop optimizer

docker compose --env-file "$ROOT/.env" \
  --env-file "$OPTIMIZER_DIR/activation.env" \
  -f "$OPTIMIZER_DIR/compose.yml" run --rm optimizer --rollback

API_KEY=$(cat "$KEY_FILE")
curl --fail --silent --show-error \
  -X PUT "$SUB2API_ADMIN_BASE_URL/api/v1/admin/settings" \
  -H "Content-Type: application/json" \
  -H "x-api-key: $API_KEY" \
  -d '{"openai_advanced_scheduler_enabled":false}' >/dev/null

SETTINGS=$(curl --fail --silent --show-error \
  -H "x-api-key: $API_KEY" \
  "$SUB2API_ADMIN_BASE_URL/api/v1/admin/settings")
printf '%s' "$SETTINGS" | grep -Eq '"openai_advanced_scheduler_enabled"[[:space:]]*:[[:space:]]*false'
unset SETTINGS

docker tag "$ORIGINAL_IMAGE_ID" "$ORIGINAL_IMAGE_REF"
docker compose --env-file "$ROOT/.env" \
  -f "$ROOT/docker-compose.yml" \
  -f "$ROOT/docker-compose.override.yml" \
  up -d --no-deps --pull never sub2api

attempt=0
until test "$(docker inspect -f '{{.State.Health.Status}}' "$SUB2API_CONTAINER" 2>/dev/null || true)" = healthy; do
  attempt=$((attempt + 1))
  test "$attempt" -lt 90
  sleep 1
done

curl --fail --silent --show-error \
  -X DELETE "$SUB2API_ADMIN_BASE_URL/api/v1/admin/settings/admin-api-key" \
  -H "x-api-key: $API_KEY" >/dev/null
unset API_KEY
rm -f "$KEY_FILE"

EXPECTED_IMAGE=$(printf '%s' "$ORIGINAL_IMAGE_ID" | sed 's/^sha256://')
RUNNING_IMAGE=$(docker inspect -f '{{.Image}}' "$SUB2API_CONTAINER" | sed 's/^sha256://')
test "$RUNNING_IMAGE" = "$EXPECTED_IMAGE"
test "$(docker inspect -f '{{.State.Health.Status}}' "$SUB2API_CONTAINER")" = healthy

printf '%s\n' "Full account-optimizer rollback completed."
