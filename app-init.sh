#!/bin/sh
# ============================================================
#  Desktroo — init container (déploiement 100% web)
#  Téléchargé et exécuté par le service "app-init" du compose
#  Custom App. Télécharge les fichiers applicatifs depuis GitHub,
#  injecte le token et génère nginx.conf depuis les variables.
#  Idempotent : sans danger à relancer.
# ============================================================
set -e

: "${GITHUB_RAW:?GITHUB_RAW manquant}"
: "${FILEOPS_TOKEN:?FILEOPS_TOKEN manquant}"
: "${TRUENAS_IP:?TRUENAS_IP manquant}"
TRUENAS_HOST="${TRUENAS_HOST:-$TRUENAS_IP}"
TRUENAS_UI_URL="${TRUENAS_UI_URL:-http://$TRUENAS_IP}"
D="${DATA_DIR:-/data}"

echo "▸ Préparation de l'arborescence dans $D"
mkdir -p "$D" \
  "$D/websites/conf.d" \
  "$D/websites/php/8.3/ini" "$D/websites/php/8.2/ini" \
  "$D/websites/php/8.1/ini" "$D/websites/php/7.4/ini" \
  "$D/mariadb"

# Install antérieure au renommage : les confs nginx des sites pointent encore sur
# les anciens conteneurs PHP (truenas-phpNN) → on les réécrit, sinon nginx ne démarre pas.
for c in "$D"/websites/conf.d/*.conf; do
  if [ -f "$c" ] && grep -q 'truenas-php' "$c"; then
    sed -i 's/truenas-php/desktroo-php/g' "$c"
    echo "  conf de site mise à jour : $(basename "$c")"
  fi
done

# wget (busybox) présent dans alpine ; sinon on installe curl.
fetch() { wget -qO "$2" "$1" 2>/dev/null || curl -fsSL "$1" -o "$2"; }

echo "▸ Téléchargement des fichiers depuis $GITHUB_RAW"
for f in fileops.py desktroo.html vnc-viewer.html; do
  fetch "$GITHUB_RAW/$f" "$D/$f" || { echo "✗ Échec téléchargement $f"; exit 1; }
  echo "  + $f"
done

echo "▸ Injection du token et de la configuration locale"
sed -i "s|FILEOPS_TOKEN_PLACEHOLDER|${FILEOPS_TOKEN}|g" "$D/desktroo.html"
sed -i "s|const NAS_URL *= *'[^']*';|const NAS_URL = '';|" "$D/desktroo.html"
sed -i "s|const TRUENAS_UI *= *'[^']*';|const TRUENAS_UI = '${TRUENAS_UI_URL}';|" "$D/desktroo.html"

echo "▸ Génération de nginx.conf (NAS=$TRUENAS_IP host=$TRUENAS_HOST)"
cat > "$D/nginx.conf" <<NGINX
server {
    listen 80;
    server_name _;
    client_max_body_size 20g;
    client_body_timeout 3600s;
    root /usr/share/nginx/html;
    index index.html;
    # ── Barrière d'authentification devant tout le bureau ────────────
    # Login exigé avant d'accéder à la page (qui contient le token) et aux
    # endpoints fileops / terminal / VNC. /s/ (partages publics) est exempté.
    # 2FA : déléguer à un portail (Authelia / authentik) via auth_request.
    auth_basic           "Desktroo";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / { try_files \$uri /index.html; }

    location = /api/current {
        proxy_pass            https://${TRUENAS_IP}/api/current;
        proxy_http_version    1.1;
        proxy_set_header      Upgrade           \$http_upgrade;
        proxy_set_header      Connection        "upgrade";
        proxy_set_header      Host              ${TRUENAS_HOST};
        proxy_ssl_verify      off;
        proxy_ssl_server_name off;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
    }

    location /api/ {
        proxy_pass          https://${TRUENAS_IP}/api/;
        proxy_http_version  1.1;
        proxy_ssl_verify    off;
        proxy_ssl_server_name off;
        proxy_set_header    Host              ${TRUENAS_HOST};
        proxy_set_header    Authorization     \$http_authorization;
        proxy_pass_header   Authorization;
        proxy_set_header    Cookie            \$http_cookie;
        proxy_pass_header   Set-Cookie;
        proxy_connect_timeout 10s;
        proxy_read_timeout    30s;
    }

    location /_download/ {
        proxy_pass            https://${TRUENAS_IP}/_download/;
        proxy_http_version    1.1;
        proxy_ssl_verify      off;
        proxy_ssl_server_name off;
        proxy_set_header      Host ${TRUENAS_HOST};
        proxy_read_timeout    120s;
    }

    location /s/ {
        auth_basic off;
        proxy_pass            http://fileops:8765/s/;
        proxy_http_version    1.1;
        proxy_set_header      Host \$host;
        proxy_buffering       off;
        proxy_max_temp_file_size 0;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
        proxy_connect_timeout 30s;
    }

    location /fileops/ {
        proxy_pass         http://fileops:8765/;
        proxy_http_version 1.1;
        proxy_set_header   Host \$host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 30s;
    }

    location /truenas-shell {
        proxy_pass         http://fileops:8766;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    \$http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       \$host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location /vnc-proxy {
        proxy_pass         http://fileops:8766;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    \$http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       \$host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }

    location = /vnc-viewer {
        alias /usr/share/nginx/html/vnc-viewer.html;
        default_type text/html;
        add_header Cache-Control "no-cache";
    }

    location /websocket {
        proxy_pass            https://${TRUENAS_IP}/websocket;
        proxy_http_version    1.1;
        proxy_set_header      Upgrade           \$http_upgrade;
        proxy_set_header      Connection        "upgrade";
        proxy_set_header      Host              ${TRUENAS_HOST};
        proxy_set_header      Authorization     \$http_authorization;
        proxy_pass_header     Authorization;
        proxy_ssl_verify      off;
        proxy_ssl_server_name off;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
    }
}
NGINX

echo "▸ Génération de .htpasswd (barrière d'auth du bureau)"
DESK_AUTH_USER="${DESK_AUTH_USER:-admin}"
if [ -z "${DESK_AUTH_PASS:-}" ]; then
  DESK_AUTH_PASS="$(head -c 64 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 16)"
  echo "  Mot de passe bureau généré : $DESK_AUTH_PASS"
fi
if apk add --no-cache apache2-utils >/dev/null 2>&1; then
  htpasswd -bc "$D/.htpasswd" "$DESK_AUTH_USER" "$DESK_AUTH_PASS" || : > "$D/.htpasswd"
elif apk add --no-cache openssl >/dev/null 2>&1; then
  printf '%s:%s\n' "$DESK_AUTH_USER" "$(openssl passwd -apr1 "$DESK_AUTH_PASS")" > "$D/.htpasswd"
else
  echo "  ⚠ ni apache2-utils ni openssl — .htpasswd vide"; : > "$D/.htpasswd"
fi
chmod 600 "$D/.htpasswd" 2>/dev/null || true

echo "✓ Init terminé — fichiers prêts dans $D"
