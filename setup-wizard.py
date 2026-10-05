#!/usr/bin/env python3
# =============================================================
#  TrueNAS Desktop — Wizard d'installation web
#
#  Usage depuis le shell TrueNAS :
#    python3 /mnt/<pool>/apps/desktop/setup-wizard.py
#
#  Puis ouvrir dans le navigateur :
#    http://IP_TRUENAS:8099/setup
# =============================================================

import http.server
import json
import os
import re
import subprocess
import threading
import socket
import sys
import secrets
import shutil
import queue
import time
import urllib.parse

PORT = 8090
GITHUB_RAW_DEFAULT = 'https://raw.githubusercontent.com/Nabief/truenas-desktop-dist/main'
INSTALL_EVENTS = queue.Queue()
INSTALL_RUNNING = False
INSTALL_DONE = False

# ── Auto-détection IP ─────────────────────────────────────────
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'

# ── Détection des pools ZFS montés sous /mnt ──────────────────
def list_pools():
    pools = []
    _SKIP = {'ix-apps', 'ix-applications', 'ix-virt', 'lost+found'}
    try:
        for name in sorted(os.listdir('/mnt')):
            if name.startswith('.') or name in _SKIP:
                continue
            if os.path.isdir(os.path.join('/mnt', name)):
                pools.append(name)
    except Exception:
        pass
    return pools

# ── Vérification prérequis ────────────────────────────────────
def check_prerequisites():
    results = {}
    results['root'] = os.geteuid() == 0
    results['docker'] = shutil.which('docker') is not None
    results['python'] = sys.version_info >= (3, 6)
    results['openssl'] = shutil.which('openssl') is not None
    return results

# ── Génération token ──────────────────────────────────────────
def generate_token():
    return secrets.token_urlsafe(24)

# ── Installation ──────────────────────────────────────────────
INSTALL_LOG = '/tmp/tnd-install.log'


def emit(msg, level='info', secret=False):
    # secret=True : le message s'affiche à l'écran mais n'est PAS écrit dans le
    # journal persistant (évite d'y laisser un mot de passe en clair).
    INSTALL_EVENTS.put({'msg': msg, 'level': level})
    try:
        import time as _t
        _logmsg = '(masqué — non journalisé)' if secret else msg
        with open(INSTALL_LOG, 'a', encoding='utf-8') as f:
            f.write('%s [%s] %s\n' % (_t.strftime('%H:%M:%S'), level, _logmsg))
    except Exception:
        pass

def run_cmd(cmd, shell=True):
    proc = subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:
        emit(line.rstrip())
    proc.wait()
    return proc.returncode

def _midclt(args, timeout=60):
    """Appelle le middleware TrueNAS. Retourne (code, stdout, stderr)."""
    try:
        p = subprocess.run(['midclt'] + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()
    except Exception as e:
        return 1, '', str(e)


def configure_truenas(ssh_user):
    """Automatise les prérequis TrueNAS via midclt : SSH + auth mot de passe,
    et sudo NOPASSWD pour l'utilisateur. Non bloquant (avertit si échec)."""
    if not shutil.which('midclt'):
        emit('⚠ midclt introuvable — configure SSH/sudo manuellement.', 'warn')
        return
    emit('▸ Configuration TrueNAS (SSH + sudo) via middleware...', 'step')

    # 1. SSH : autoriser l'authentification par mot de passe
    rc, out, err = _midclt(['call', 'ssh.update', '{"passwordauth": true}'])
    emit('✓ SSH : auth par mot de passe activée' if rc == 0
         else f'⚠ ssh.update a échoué : {err or out}', 'ok' if rc == 0 else 'warn')

    # 2. SSH : activer le service au boot + démarrer
    _midclt(['call', 'service.update', 'ssh', '{"enable": true}'])
    rc, out, err = _midclt(['call', 'service.start', 'ssh'])
    emit('✓ Service SSH démarré' if rc == 0
         else f'⚠ Démarrage SSH : {err or out}', 'ok' if rc == 0 else 'warn')

    # 3. sudo NOPASSWD pour l'utilisateur SSH
    rc, out, err = _midclt(['call', 'user.query', f'[["username","=","{ssh_user}"]]'])
    uid = None
    if rc == 0 and out:
        try:
            data = json.loads(out)
            if data:
                uid = data[0].get('id')
        except Exception:
            pass
    if uid is not None:
        payload = '{"sudo_commands": ["ALL"], "sudo_commands_nopasswd": ["ALL"]}'
        rc, out, err = _midclt(['call', 'user.update', str(uid), payload])
        emit(f'✓ sudo sans mot de passe activé pour {ssh_user}' if rc == 0
             else f'⚠ user.update a échoué : {err or out}', 'ok' if rc == 0 else 'warn')
    else:
        emit(f'⚠ Utilisateur {ssh_user} introuvable — active le sudo NOPASSWD manuellement.', 'warn')

    # 4. SSH 25.x : autoriser le login par mot de passe pour le groupe de l'utilisateur.
    #    Sur TrueNAS 25.x, passwordauth=true ne suffit pas : le login mot de passe
    #    est verrouille par 'password_login_groups' (vide => personne ; sshd met
    #    PasswordAuthentication no hors bloc Match Group). On ajoute le groupe
    #    principal de l'utilisateur SSH. Non bloquant (champ absent en < 25.x).
    try:
        grp_name = None
        rc, out, _ = _midclt(['call', 'user.query', f'[["username","=","{ssh_user}"]]'])
        u = (json.loads(out) or [{}])[0] if (rc == 0 and out) else {}
        g = u.get('group') or {}
        grp_name = g.get('bsdgrp_group') or g.get('group') or g.get('name')
        if not grp_name and g.get('id') is not None:
            rc, out, _ = _midclt(['call', 'group.query', json.dumps([["id", "=", g.get('id')]])])
            gg = (json.loads(out) or [{}])[0] if out else {}
            grp_name = gg.get('group') or gg.get('name')
        if grp_name:
            rc, out, _ = _midclt(['call', 'ssh.config'])
            cur = json.loads(out) if out else {}
            groups = list(cur.get('password_login_groups') or [])
            if grp_name not in groups:
                groups.append(grp_name)
                rc, out, err = _midclt(['call', 'ssh.update', json.dumps({"password_login_groups": groups})])
                if rc == 0:
                    _midclt(['call', 'service.restart', 'ssh'])
                    emit(f'✓ SSH : login mot de passe autorise pour le groupe {grp_name}', 'ok')
                else:
                    emit(f'⚠ password_login_groups non applique ({err or out}) — a regler en UI si besoin.', 'warn')
            else:
                emit(f'✓ SSH : groupe {grp_name} deja autorise (mot de passe)', 'ok')
    except Exception as _e:
        emit(f'⚠ Reglage password_login_groups ignore ({_e}).', 'warn')


def _ensure_dataset(mount_path):
    """Crée un vrai dataset ZFS (+ ses ancêtres) pour un chemin sous /mnt via
    midclt, afin qu'il apparaisse dans Storage. Retourne True si dataset(s) en
    place, False si on doit retomber sur un simple dossier."""
    if not shutil.which('midclt') or not mount_path.startswith('/mnt/'):
        return False
    ds = mount_path[len('/mnt/'):].strip('/')
    parts = ds.split('/')
    if len(parts) < 2:
        return False  # c'est le pool lui-même
    for i in range(2, len(parts) + 1):
        name = '/'.join(parts[:i])
        rc, out, _ = _midclt(['call', 'pool.dataset.query', json.dumps([["id", "=", name]])])
        exists = False
        try:
            exists = bool(json.loads(out))
        except Exception:
            exists = False
        if exists:
            continue
        # Ne pas écraser un dossier déjà rempli (installation existante)
        full = '/mnt/' + name
        if os.path.isdir(full) and os.listdir(full):
            emit(f'⚠ {full} existe déjà en dossier — conservé tel quel.', 'warn')
            return False
        rc, out, err = _midclt(['call', 'pool.dataset.create', json.dumps({"name": name})], timeout=120)
        if rc != 0:
            emit(f'⚠ Dataset {name} non créé ({err or out}) — dossier simple utilisé.', 'warn')
            return False
        emit(f'✓ Dataset créé : {name}', 'ok')
    return True


def run_install(config):
    global INSTALL_RUNNING, INSTALL_DONE, INSTALL_LOG
    INSTALL_RUNNING = True
    INSTALL_DONE = False
    try:
        open(INSTALL_LOG, 'w', encoding='utf-8').close()  # log neuf par install
    except Exception:
        pass

    try:
        install_dir  = config['install_dir']
        vm_dir       = config['vm_dir']
        iso_dir      = config['iso_dir']
        port         = config['port']
        truenas_ip   = config['truenas_ip']
        truenas_host = config['truenas_host']
        ssh_user     = config['ssh_user']
        ssh_pass     = config['ssh_pass']
        token        = config['token'] or generate_token()
        db_pass      = config.get('db_pass') or generate_token()

        # ── Sécurité : identifiants du bureau + 2FA optionnelle ──
        desk_user   = config.get('desk_user') or 'admin'
        desk_pass   = config.get('desk_pass') or secrets.token_urlsafe(12)
        enable_2fa  = bool(config.get('enable_2fa'))
        domain_desktop = (config.get('domain_desktop') or '').strip().lower()
        domain_auth    = (config.get('domain_auth') or '').strip().lower()
        admin_email    = (config.get('admin_email') or 'admin@example.com').strip()
        npm_ip      = (config.get('npm_ip') or '').strip()
        smtp_host   = (config.get('smtp_host') or '').strip()
        smtp_port   = (config.get('smtp_port') or '465').strip()
        smtp_user   = (config.get('smtp_user') or '').strip()
        smtp_pass   = (config.get('smtp_pass') or '').strip()
        # ── 2FA : Authelia DERRIÈRE Nginx Proxy Manager (NPM) ──
        #   Authelia 4.39 exige des URLs HTTPS ; c'est NPM (Let's Encrypt) qui
        #   les fournit. L'assistant fait TOUT le côté NAS (secrets, hash,
        #   conteneur Authelia, nginx bureau sans barrière). Il reste à créer
        #   2 hôtes proxy dans NPM + les redirections DNS (affichés à la fin).
        #   Les 2 domaines doivent partager le même domaine parent.
        if enable_2fa and (not domain_desktop or not domain_auth or '.' not in domain_desktop):
            emit('⚠ 2FA demandée mais domaines manquants/invalides — 2FA désactivée (barrière simple conservée).', 'warn')
            enable_2fa = False
        domain_parent = domain_desktop.split('.', 1)[1] if (enable_2fa and '.' in domain_desktop) else ''
        if enable_2fa and domain_parent and not domain_auth.endswith(domain_parent):
            emit('⚠ Les 2 domaines doivent partager le même domaine parent (%s) — 2FA désactivée.' % domain_parent, 'warn')
            enable_2fa = False

        if enable_2fa:
            # Bureau nginx SANS barrière locale : NPM + Authelia (en amont)
            # assurent l'authentification. Authelia est publié sur 9091 pour
            # que NPM (sur un autre hôte) puisse l'atteindre.
            _srv_auth = "    # Auth deleguee a NPM + Authelia (2FA) en amont."
            _s_exempt = ""
            _server_name = "_"
            _extra_server = ""
            _authelia_service = (
                "\n  authelia:\n"
                "    image: authelia/authelia:4.39\n"
                "    container_name: truenas-authelia\n"
                "    restart: unless-stopped\n"
                "    ports:\n"
                "      - \"9091:9091\"\n"
                "    volumes:\n"
                "      - " + install_dir + "/authelia:/config\n"
                "    env_file:\n"
                "      - " + install_dir + "/authelia/secrets.env\n"
                "    environment:\n"
                "      TZ: \"Europe/Paris\"\n"
                "    healthcheck:\n"
                "      disable: true\n"
            )
        else:
            # Basic auth retirée (doublon avec Authelia) — protection via Authelia 2FA, optionnelle.
            _srv_auth = ""
            _s_exempt = ""
            _server_name = "_"
            _extra_server = ""
            _authelia_service = ""

        script_dir = os.path.dirname(os.path.abspath(__file__))

        # ── 1. Datasets / répertoires ─────────────────────────
        emit('▸ Création des datasets ZFS...', 'step')
        for d in (install_dir, vm_dir):
            if not _ensure_dataset(d):
                os.makedirs(d, exist_ok=True)  # repli : simple dossier
        try:
            os.chmod(vm_dir, 0o777)
        except Exception:
            pass
        # Sous-dossiers applicatifs (à l'intérieur du dataset d'install)
        for sub in ('websites/conf.d', 'websites/php/8.3/ini', 'websites/php/8.2/ini',
                    'websites/php/8.1/ini', 'websites/php/7.4/ini', 'mariadb'):
            os.makedirs(os.path.join(install_dir, sub), exist_ok=True)
        emit(f'✓ {install_dir}', 'ok')
        emit(f'✓ {vm_dir}', 'ok')

        # ── Journal persistant : /tmp → <install_dir>/install.log ──
        # (rapport d'install durable et découvrable ; on y recopie les 1res lignes)
        try:
            _persist = os.path.join(install_dir, 'install.log')
            _prev = ''
            try:
                with open(INSTALL_LOG, encoding='utf-8') as _lf:
                    _prev = _lf.read()
            except Exception:
                pass
            with open(_persist, 'w', encoding='utf-8') as _lf:
                _lf.write("===== TrueNAS Desktop — journal d'installation =====\n")
                _lf.write(_prev)
            INSTALL_LOG = _persist
            emit('✓ Journal d\'installation : %s' % _persist, 'ok')
        except Exception:
            pass

        # ── 1b. Prérequis TrueNAS automatisés (SSH + sudo) ────
        configure_truenas(ssh_user)

        # ── 2. Sauvegarde config ──────────────────────────────
        emit('▸ Sauvegarde de la configuration...', 'step')
        os.makedirs('/etc/truenas-desktop', exist_ok=True)
        config_content = f"""INSTALL_DIR={install_dir}
VM_DIR={vm_dir}
ISO_DIR={iso_dir}
PORT={port}
TRUENAS_IP={truenas_ip}
TRUENAS_HOST={truenas_host}
SSH_USER={ssh_user}
SSH_PASS={ssh_pass}
FILEOPS_TOKEN={token}
GITHUB_RAW={(config.get('github_raw') or GITHUB_RAW_DEFAULT).rstrip('/')}
"""
        with open('/etc/truenas-desktop/config.env', 'w') as f:
            f.write(config_content)
        os.chmod('/etc/truenas-desktop/config.env', 0o600)
        emit('✓ /etc/truenas-desktop/config.env', 'ok')

        # ── 3. Récupération des fichiers (GitHub, sinon copie locale) ──
        emit('▸ Récupération des fichiers applicatifs...', 'step')
        # Un démarrage Docker précédent (avant que les fichiers existent) a pu
        # créer des DOSSIERS à la place des fichiers montés (bind-mount) → l'écriture
        # échouerait ensuite avec « Is a directory ». On retire ces dossiers parasites.
        for _bad in ('truenas-desktop.html', 'vnc-viewer.html', 'fileops.py',
                     'nginx.conf', '.htpasswd', 'docker-compose.yml'):
            _bp = os.path.join(install_dir, _bad)
            if os.path.isdir(_bp):
                try:
                    shutil.rmtree(_bp)
                    emit('⚠ %s était un dossier (mount Docker d\'un essai précédent) — supprimé.' % _bad, 'warn')
                except Exception as _e:
                    emit('⚠ Impossible de supprimer le dossier parasite %s : %s' % (_bad, _e), 'warn')
        github_raw = (config.get('github_raw') or GITHUB_RAW_DEFAULT).rstrip('/')
        import urllib.request as _u
        for fname in ['fileops.py', 'truenas-desktop.html', 'vnc-viewer.html']:
            dst = os.path.join(install_dir, fname)
            src = os.path.join(script_dir, fname)
            got = False
            if os.path.exists(src) and src != dst:
                try:
                    shutil.copy2(src, dst); got = True
                    emit(f'✓ {fname} (copié)', 'ok')
                except Exception:
                    pass
            if not got:
                try:
                    _u.urlretrieve(f'{github_raw}/{fname}', dst)
                    emit(f'✓ {fname} (téléchargé)', 'ok')
                except Exception as e:
                    emit(f'✗ Échec récupération {fname} : {e}', 'error')
                    raise RuntimeError(f'Impossible de récupérer {fname} depuis {github_raw}')

        # ── 4. Injection token dans le HTML ───────────────────
        html_path = os.path.join(install_dir, 'truenas-desktop.html')
        if os.path.exists(html_path):
            with open(html_path, 'r') as f:
                html = f.read()
            # Remplace le token quel que soit sa valeur actuelle (placeholder ou ancien token)
            html, n = re.subn(
                r"(FILEOPS_TOKEN_DEFAULT\s*=\s*')[^']*(')",
                lambda m: m.group(1) + token + m.group(2),
                html
            )
            if n:
                with open(html_path, 'w') as f:
                    f.write(html)
                emit('✓ Token injecté dans truenas-desktop.html', 'ok')
            else:
                emit('⚠ Token non trouvé dans le HTML (variable FILEOPS_TOKEN_DEFAULT absente)', 'warn')

        # ── 5. Génération docker-compose.yml ──────────────────
        emit('▸ Génération de docker-compose.yml...', 'step')
        # NB : '$$' dans ces commandes → docker compose écrit un '$' littéral dans le
        # YAML généré (sinon il substitue $v/$e/$last comme variables → watcher cassé).
        php_reload = (
            "apk add --no-cache curl >/dev/null 2>&1 || true; "
            "[ -x /usr/local/bin/install-php-extensions ] || { curl -sSLf https://github.com/mlocati/docker-php-extension-installer/releases/latest/download/install-php-extensions -o /usr/local/bin/install-php-extensions && chmod +x /usr/local/bin/install-php-extensions; }; "
            "[ -s /conf/extensions.txt ] && install-php-extensions $$(cat /conf/extensions.txt) || true; "
            "( last=''; lastext=''; while true; do e=$$(cat /conf/.extreload 2>/dev/null); if [ \"$$e\" != \"$$lastext\" ]; then lastext=\"$$e\"; { [ -s /conf/extensions.txt ] && install-php-extensions $$(cat /conf/extensions.txt) >/dev/null 2>&1; } || true; kill -USR2 1 2>/dev/null || true; fi; v=$$(cat /conf/.reload 2>/dev/null); if [ \"$$v\" != \"$$last\" ]; then last=\"$$v\"; kill -USR2 1 2>/dev/null || true; fi; sleep 3; done ) & exec php-fpm"
        )
        web_reload = "last=''; ( while true; do v=$$(cat /etc/nginx/conf.d/.reload 2>/dev/null); if [ \"$$v\" != \"$$last\" ]; then last=\"$$v\"; nginx -t && nginx -s reload; fi; sleep 3; done ) & exec nginx -g 'daemon off;'"

        def php_service(ver):
            return f"""  php{ver.replace('.','')}:
    image: php:{ver}-fpm-alpine
    container_name: truenas-php{ver.replace('.','')}
    restart: unless-stopped
    environment:
      PHP_INI_SCAN_DIR: ":/conf/ini"
    volumes:
      - /mnt:/mnt
      - {install_dir}/websites/php/{ver}:/conf
    command: {json.dumps(["sh","-c",php_reload])}
    depends_on:
      - fileops
"""

        compose = f"""services:
  truenas-desktop:
    image: nginx:alpine
    container_name: truenas-desktop
    restart: unless-stopped
    user: root
    ports:
      - "{port}:80"
    volumes:
      - {install_dir}/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - {install_dir}/truenas-desktop.html:/usr/share/nginx/html/index.html:ro
      - {install_dir}/vnc-viewer.html:/usr/share/nginx/html/vnc-viewer.html:ro
      - {install_dir}/.htpasswd:/etc/nginx/.htpasswd:ro

    depends_on:
      - fileops

  fileops:
    image: python:3.11-alpine
    container_name: truenas-fileops
    restart: unless-stopped
    user: root
    stdin_open: true
    tty: true
    environment:
      FILEOPS_TOKEN: "{token}"
      FILEOPS_PORT: "8765"
      FILEOPS_WS_PORT: "8766"
      HOST_BOOTSTRAP: "1"
      APP_DIR: "{install_dir}"
      GITHUB_RAW: "{github_raw}"
      TRUENAS_SSH_HOST: "{truenas_ip}"
      TRUENAS_SSH_USER: "{ssh_user}"
      TRUENAS_SSH_PASS: "{ssh_pass}"
      TRUENAS_SSH_PORT: "22"
      VM_DIR: "{vm_dir}"
      ISO_DIR: "{iso_dir}"
      WEB_CONF_DIR: "{install_dir}/websites/conf.d"
      WEB_PHP_VERSIONS: '{{"8.3":"truenas-php83:9000","8.2":"truenas-php82:9000","8.1":"truenas-php81:9000","7.4":"truenas-php74:9000"}}'
      WEB_PHP_DEFAULT: "8.3"
      WEB_PHP_DIR: "{install_dir}/websites/php"
      WEB_PROXY_PORT: "8080"
      DB_HOST: "mariadb"
      DB_PORT: "3306"
      DB_ROOT_PASSWORD: "{db_pass}"
    volumes:
      - /mnt:/mnt
      - {install_dir}/fileops.py:/app/fileops.py:ro
    command: sh -c "apk add --no-cache qemu-img ca-certificates && {{ apk add --no-cache p7zip libarchive-tools 2>/dev/null || true; apk add --no-cache unrar 2>/dev/null || true; }} && pip install websockets paramiko pymysql --break-system-packages -q && python /app/fileops.py"
    expose:
      - "8765"
      - "8766"

  websites:
    image: nginx:alpine
    container_name: truenas-websites
    restart: unless-stopped
    ports:
      - "8080:8080"
      - "8100-8130:8100-8130"
    volumes:
      - /mnt:/mnt
      - {install_dir}/websites/conf.d:/etc/nginx/conf.d
    command: {json.dumps(["sh","-c",web_reload])}
    depends_on:
      - php83
      - php82
      - php81
      - php74

{php_service('8.3')}
{php_service('8.2')}
{php_service('8.1')}
{php_service('7.4')}
  mariadb:
    image: mariadb:11
    container_name: truenas-mariadb
    restart: unless-stopped
    environment:
      MARIADB_ROOT_PASSWORD: "{db_pass}"
      MARIADB_AUTO_UPGRADE: "1"
    expose:
      - "3306"
    volumes:
      - {install_dir}/mariadb:/var/lib/mysql
{_authelia_service}"""
        with open(os.path.join(install_dir, 'docker-compose.yml'), 'w') as f:
            f.write(compose)
        # Sauvegarde du mot de passe DB dans la config
        try:
            with open('/etc/truenas-desktop/config.env', 'a') as f:
                f.write(f'DB_ROOT_PASSWORD={db_pass}\n')
        except Exception:
            pass
        emit('✓ docker-compose.yml (stack complète)', 'ok')

        # ── 6. Génération nginx.conf ──────────────────────────
        emit('▸ Génération de nginx.conf...', 'step')
        nginx = f"""server {{
    listen 80;
    server_name {_server_name};
    client_max_body_size 20g;
    client_body_timeout 3600s;
    root /usr/share/nginx/html;
    index index.html;
    # ── Barrière d'authentification devant tout le bureau ────────────
    # Login exigé avant d'accéder à la page (qui contient le token) et aux
    # endpoints fileops / terminal / VNC. /s/ (partages publics) est exempté.
    # 2FA : déléguer à un portail (Authelia / authentik) via auth_request.
{_srv_auth}

    location / {{
        try_files $uri /index.html;
    }}

    location = /api/current {{
        proxy_pass            https://{truenas_ip}/api/current;
        proxy_http_version    1.1;
        proxy_set_header      Upgrade           $http_upgrade;
        proxy_set_header      Connection        "upgrade";
        proxy_set_header      Host              {truenas_host};
        proxy_ssl_verify      off;
        proxy_ssl_server_name off;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
    }}

    location /s/ {{
        {_s_exempt}
        proxy_pass            http://fileops:8765/s/;
        proxy_http_version    1.1;
        proxy_set_header      Host $host;
        proxy_buffering       off;
        proxy_max_temp_file_size 0;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
        proxy_connect_timeout 30s;
    }}

    location /api/ {{
        proxy_pass          https://{truenas_ip}/api/;
        proxy_http_version  1.1;
        proxy_ssl_verify    off;
        proxy_ssl_server_name off;
        proxy_set_header    Host              {truenas_host};
        proxy_set_header    Authorization     $http_authorization;
        proxy_pass_header   Authorization;
        proxy_set_header    Cookie            $http_cookie;
        proxy_pass_header   Set-Cookie;
        proxy_connect_timeout 10s;
        proxy_read_timeout    30s;
    }}

    location /_download/ {{
        proxy_pass            https://{truenas_ip}/_download/;
        proxy_http_version    1.1;
        proxy_ssl_verify      off;
        proxy_ssl_server_name off;
        proxy_set_header      Host {truenas_host};
        proxy_read_timeout    120s;
    }}

    location /fileops/ {{
        proxy_pass         http://fileops:8765/;
        proxy_http_version 1.1;
        proxy_set_header   Host $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 30s;
    }}

    location /truenas-shell {{
        proxy_pass         http://fileops:8766;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}

    location /vnc-proxy {{
        proxy_pass         http://fileops:8766;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host       $host;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }}

    location = /vnc-viewer {{
        alias /usr/share/nginx/html/vnc-viewer.html;
        default_type text/html;
        add_header Cache-Control "no-cache";
    }}

    location /websocket {{
        proxy_pass            https://{truenas_ip}/websocket;
        proxy_http_version    1.1;
        proxy_set_header      Upgrade           $http_upgrade;
        proxy_set_header      Connection        "upgrade";
        proxy_set_header      Host              {truenas_host};
        proxy_set_header      Authorization     $http_authorization;
        proxy_pass_header     Authorization;
        proxy_ssl_verify      off;
        proxy_ssl_server_name off;
        proxy_read_timeout    3600s;
        proxy_send_timeout    3600s;
    }}
}}{_extra_server}"""
        with open(os.path.join(install_dir, 'nginx.conf'), 'w') as f:
            f.write(nginx)
        emit('✓ nginx.conf', 'ok')

        # ── .htpasswd (barrière d'auth du bureau) ──────────────
        emit('▸ Génération de .htpasswd (barrière d\'auth)...', 'step')
        _htpasswd_ok = False
        _htp = os.path.join(install_dir, '.htpasswd')
        try:
            _h = subprocess.check_output(['openssl', 'passwd', '-apr1', desk_pass]).decode().strip()
            if not _h:
                raise RuntimeError('openssl a renvoyé un hash vide')
            with open(_htp, 'w') as f:
                f.write('%s:%s\n' % (desk_user, _h))
            try:
                os.chmod(_htp, 0o644)  # lisible par nginx (worker non-root du conteneur) — hash uniquement
            except OSError:
                pass  # chmod refusé sur ZFS (ACL) — non bloquant
            _htpasswd_ok = os.path.getsize(_htp) > 0
            emit('✓ Accès bureau — utilisateur: %s  mot de passe: %s' % (desk_user, desk_pass), 'ok', secret=True)
        except Exception as e:
            emit('⚠ .htpasswd non généré: %s' % e, 'warn')

        # Filet de sécurité : barrière simple demandée mais .htpasswd absent.
        # On RETIRE la barrière du nginx.conf déjà écrit, sinon nginx renvoie
        # un 500 (fichier d'auth introuvable) et le bureau est inaccessible.
        if not enable_2fa and not _htpasswd_ok:
            try:
                _np = os.path.join(install_dir, 'nginx.conf')
                _nc = open(_np).read()
                _nc = _nc.replace(_srv_auth, "    # Barriere login desactivee : .htpasswd non genere (voir SECURITE.md)")
                with open(_np, 'w') as f:
                    f.write(_nc)
            except Exception:
                pass
            emit('⚠ BARRIÈRE LOGIN NON POSÉE : le bureau sera accessible SANS mot de passe. '
                 'Génère le .htpasswd à la main (openssl passwd -apr1) dans %s, remets auth_basic '
                 'dans nginx.conf, puis « docker restart truenas-desktop » — voir SECURITE.md.' % install_dir, 'warn')

        # ── Authelia (2FA) : secrets + hash + fichiers de config ──
        if enable_2fa:
            emit('▸ Configuration Authelia (2FA)...', 'step')
            adir = os.path.join(install_dir, 'authelia')
            os.makedirs(adir, exist_ok=True)
            # Secrets : on PRÉSERVE ceux déjà présents. Régénérer la clé de
            # chiffrement casserait la base Authelia (db.sqlite3) à chaque
            # réinstallation → conteneur en boucle. On ne génère que le manquant.
            _sfile = os.path.join(adir, 'secrets.env')
            _sec = {}
            if os.path.exists(_sfile):
                for _l in open(_sfile):
                    if '=' in _l and not _l.lstrip().startswith('#'):
                        _k, _v = _l.split('=', 1)
                        _sec[_k.strip()] = _v.rstrip('\n')
            for _k in ('AUTHELIA_SESSION_SECRET',
                       'AUTHELIA_STORAGE_ENCRYPTION_KEY',
                       'AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET'):
                if not _sec.get(_k):
                    _sec[_k] = secrets.token_hex(32)
            if smtp_pass:
                _sec['AUTHELIA_NOTIFIER_SMTP_PASSWORD'] = smtp_pass
            with open(_sfile, 'w') as f:
                for _k, _v in _sec.items():
                    f.write('%s=%s\n' % (_k, _v))
            try:
                os.chmod(_sfile, 0o600)
            except Exception:
                pass
            pw_hash = ''
            try:
                _out = subprocess.check_output(
                    ['docker', 'run', '--rm', 'authelia/authelia:4.39',
                     'authelia', 'crypto', 'hash', 'generate', 'argon2',
                     '--password', desk_pass],
                    stderr=subprocess.STDOUT, timeout=240).decode()
                _m = re.search(r'\$argon2id\$[^\s]+', _out)
                pw_hash = _m.group(0) if _m else ''
            except Exception as e:
                emit('⚠ Hash Authelia non généré (%s) — à compléter dans users_database.yml' % e, 'warn')
            with open(os.path.join(adir, 'users_database.yml'), 'w') as f:
                f.write('users:\n')
                f.write('  %s:\n' % desk_user)
                f.write('    disabled: false\n')
                f.write("    displayname: '%s'\n" % desk_user)
                f.write("    password: '%s'\n" % (pw_hash or 'REMPLACE_PAR_LE_HASH_ARGON2ID'))
                f.write("    email: '%s'\n" % admin_email)
                f.write('    groups:\n      - admins\n')
            # Notifier : SMTP (email réel) si renseigné, sinon fichier local.
            if smtp_host and smtp_user and smtp_pass:
                _scheme = 'submissions' if str(smtp_port) == '465' else 'submission'
                _notifier_yaml = (
                    "notifier:\n  smtp:\n"
                    "    address: '%s://%s:%s'\n" % (_scheme, smtp_host, smtp_port) +
                    "    username: '%s'\n" % smtp_user +
                    "    sender: 'TrueNAS Desktop <%s>'\n" % smtp_user +
                    "    subject: '[Authelia] {title}'\n"
                )
                emit('✓ Email (SMTP) configuré : %s via %s:%s' % (smtp_user, smtp_host, smtp_port), 'ok')
            else:
                _notifier_yaml = "notifier:\n  filesystem:\n    filename: '/config/notification.txt'\n"
            cfg = (
                "theme: 'dark'\n"
                "log:\n  level: 'info'\n"
                "server:\n  address: 'tcp://:9091'\n"
                "totp:\n  issuer: 'TrueNAS Desktop'\n  period: 30\n"
                "authentication_backend:\n  file:\n    path: '/config/users_database.yml'\n"
                "access_control:\n  default_policy: 'deny'\n  rules:\n"
                "    - domain: '%s'\n      policy: 'two_factor'\n" % domain_desktop +
                "session:\n  cookies:\n"
                "    - name: 'authelia_session'\n"
                "      domain: '%s'\n" % domain_parent +
                "      authelia_url: 'https://%s'\n" % domain_auth +
                "      default_redirection_url: 'https://%s'\n" % domain_desktop +
                "      expiration: '1h'\n      inactivity: '15m'\n"
                "storage:\n  local:\n    path: '/config/db.sqlite3'\n" +
                _notifier_yaml
            )
            with open(os.path.join(adir, 'configuration.yml'), 'w') as f:
                f.write(cfg)
            # ── Snippets NPM (à monter dans le conteneur NPM sous /snippets) ──
            npmd = os.path.join(adir, 'npm')
            os.makedirs(npmd, exist_ok=True)
            with open(os.path.join(npmd, 'authelia-location.conf'), 'w') as f:
                f.write(
                    "## Authelia - endpoint interne d'autorisation (niveau server)\n"
                    "set $upstream_authelia http://%s:9091/api/authz/auth-request;\n" % truenas_ip +
                    "location /internal/authelia/authz {\n"
                    "    internal;\n"
                    "    proxy_pass $upstream_authelia;\n"
                    "    proxy_set_header X-Original-Method $request_method;\n"
                    "    proxy_set_header X-Original-URL $scheme://$http_host$request_uri;\n"
                    "    proxy_set_header X-Forwarded-For $remote_addr;\n"
                    "    proxy_set_header Content-Length \"\";\n"
                    "    proxy_set_header Connection \"\";\n"
                    "    proxy_pass_request_body off;\n"
                    "    proxy_next_upstream error timeout invalid_header http_500 http_502 http_503;\n"
                    "    proxy_redirect http:// $scheme://;\n"
                    "    proxy_http_version 1.1;\n"
                    "    proxy_cache_bypass $cookie_session;\n"
                    "    proxy_no_cache $cookie_session;\n"
                    "    proxy_buffers 4 32k;\n"
                    "    client_body_buffer_size 128k;\n"
                    "    send_timeout 5m;\n"
                    "    proxy_read_timeout 240;\n"
                    "    proxy_send_timeout 240;\n"
                    "    proxy_connect_timeout 240;\n"
                    "}\n"
                )
            with open(os.path.join(npmd, 'authelia-authrequest.conf'), 'w') as f:
                f.write(
                    "## Authelia - protege la location (a inclure DANS location /)\n"
                    "auth_request /internal/authelia/authz;\n"
                    "auth_request_set $user   $upstream_http_remote_user;\n"
                    "auth_request_set $groups $upstream_http_remote_groups;\n"
                    "auth_request_set $name   $upstream_http_remote_name;\n"
                    "auth_request_set $email  $upstream_http_remote_email;\n"
                    "proxy_set_header Remote-User   $user;\n"
                    "proxy_set_header Remote-Groups $groups;\n"
                    "proxy_set_header Remote-Name   $name;\n"
                    "proxy_set_header Remote-Email  $email;\n"
                    "auth_request_set $redirection_url $upstream_http_location;\n"
                    "error_page 401 =302 $redirection_url;\n"
                )
            with open(os.path.join(npmd, 'proxy.conf'), 'w') as f:
                f.write(
                    "## En-tetes proxy standard (a inclure DANS location /)\n"
                    "proxy_set_header Host              $host;\n"
                    "proxy_set_header X-Real-IP         $remote_addr;\n"
                    "proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;\n"
                    "proxy_set_header X-Forwarded-Proto $scheme;\n"
                    "proxy_set_header X-Forwarded-Host  $http_host;\n"
                    "proxy_set_header X-Forwarded-Uri   $request_uri;\n"
                    "proxy_set_header Upgrade           $http_upgrade;\n"
                    "proxy_set_header Connection        $connection_upgrade;\n"
                    "proxy_http_version 1.1;\n"
                    "proxy_read_timeout 3600;\n"
                    "proxy_send_timeout 3600;\n"
                    "send_timeout 3600;\n"
                    "client_max_body_size 20g;\n"
                )
            emit('✓ Authelia configuré (utilisateur: %s) — conteneur publié sur le port 9091' % desk_user, 'ok')
            emit('✓ Snippets NPM écrits dans %s' % npmd, 'ok')
            _npm = npm_ip or '<IP_de_NPM>'
            emit('===== A FAIRE DANS NPM (une seule fois) =====', 'step')
            emit("RECOMMANDE : NPMplus (fork de NPM) integre Authelia nativement -- plus simple que NPM standard.", 'step')
            emit("- Hote proxy PORTAIL : %s -> http %s port 9091 -- SSL Let's Encrypt + Force SSL + Websockets. Rien dans Advanced." % (domain_auth, truenas_ip), 'step')
            emit("- Hote proxy BUREAU : %s -> http %s port %s -- SSL + Force SSL + Websockets." % (domain_desktop, truenas_ip, port), 'step')
            emit("    NPMplus (recommande) : Auth Request = 'authelia (modern)', Auth Request Upstream = http://%s:9091 (sans chemin). Rien d'autre a monter." % truenas_ip, 'step')
            emit("    NPM standard : monte %s sous /snippets dans NPM, puis onglet Advanced :" % npmd, 'step')
            emit("      include /snippets/authelia-location.conf;", 'step')
            emit("      location / { include /snippets/proxy.conf; include /snippets/authelia-authrequest.conf; proxy_pass $forward_scheme://$server:$port; }", 'step')
            emit('━━━━━ DNS — pointer les 2 domaines vers l\'accès (comme tes autres services) ━━━━━', 'step')
            emit("   %s  →  %s      %s  →  %s" % (domain_desktop, _npm, domain_auth, _npm), 'step')
            if smtp_host and smtp_user and smtp_pass:
                emit("➤ Enrôlement TOTP : ouvre https://%s , connecte-toi (%s) ; le code de vérification est envoyé par email à %s." % (domain_desktop, desk_user, admin_email), 'step')
            else:
                emit("➤ Enrôlement TOTP : ouvre https://%s , connecte-toi (%s), scanne le QR — le code est dans %s/authelia/notification.txt" % (domain_desktop, desk_user, install_dir), 'step')

        # ── 7. Nettoyage des anciennes modifs /etc (réparation boot 25.x) ──
        emit('▸ Nettoyage des anciennes modifications systemd/libvirt...', 'step')
        _cleanup = (
            'systemctl disable truenas-desktop 2>/dev/null || true; '
            'rm -f /etc/systemd/system/truenas-desktop.service; '
            'rm -f /etc/systemd/system/libvirtd.service.d/notimeout.conf; '
            'rmdir /etc/systemd/system/libvirtd.service.d 2>/dev/null || true; '
            'rm -f /etc/tmpfiles.d/truenas-libvirt.conf; '
            'rm -f /etc/polkit-1/rules.d/80-truenas-libvirt.rules; '
            'systemctl daemon-reload 2>/dev/null || true'
        )
        run_cmd(_cleanup)

        # ── 8. Démarrage auto via Init/Shutdown Script TrueNAS (POSTINIT) ──
        # Remplace l'ancien service systemd (qui provoquait un « ordering cycle »
        # fatal sur TrueNAS 25.x). Un POSTINIT s'exécute APRÈS le middleware, hors
        # du chemin critique de boot : aucun impact sur ix-etc/middlewared.
        emit('▸ Configuration du démarrage automatique (POSTINIT)...', 'step')
        autostart = f"""#!/bin/bash
# TrueNAS Desktop — demarrage POSTINIT (v1.4.0). Enregistre via midclt
# (initshutdownscript, when=POSTINIT). N'altere JAMAIS l'ordonnancement systemd.
set +e
INSTALL_DIR="{install_dir}"
LOG="$INSTALL_DIR/autostart.log"
echo "=== $(date '+%F %T') POSTINIT ===" >> "$LOG"
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
systemctl start libvirtd 2>/dev/null || systemctl start virtqemud 2>/dev/null || true
virsh -c qemu:///system net-start default >/dev/null 2>&1 || true
cd "$INSTALL_DIR" && /usr/bin/docker compose up -d >> "$LOG" 2>&1
echo "exit docker compose: $?" >> "$LOG"
"""
        autostart_path = os.path.join(install_dir, 'autostart.sh')
        # Étape non bloquante + tolérante aux ACL NFSv4 (aclmode restreint) : un
        # échec ici ne doit pas interrompre l'install (le bureau démarre quand même).
        try:
            try:
                if os.path.exists(autostart_path):
                    try:
                        os.remove(autostart_path)
                    except Exception:
                        pass
                with open(autostart_path, 'w') as f:
                    f.write(autostart)
            except OSError:
                # Dataset en ACL restreinte : on écrit via un fichier temporaire.
                import tempfile as _tf
                _fd, _tmp = _tf.mkstemp()
                with os.fdopen(_fd, 'w') as _tfh:
                    _tfh.write(autostart)
                shutil.move(_tmp, autostart_path)
            try:
                os.chmod(autostart_path, 0o755)  # inutile (lancé via 'bash'), ignoré si ACL refuse
            except OSError:
                pass
            if shutil.which('midclt'):
                _cmd = f'bash {autostart_path}'
                rc, out, err = _midclt(['call', 'initshutdownscript.query',
                                        json.dumps([["comment", "=", "TrueNAS Desktop autostart"]])])
                try:
                    for _e in json.loads(out or '[]'):
                        _midclt(['call', 'initshutdownscript.delete', str(_e.get('id'))])
                except Exception:
                    pass
                payload = json.dumps({"type": "COMMAND", "command": _cmd, "when": "POSTINIT",
                                      "enabled": True, "timeout": 300,
                                      "comment": "TrueNAS Desktop autostart"})
                rc, out, err = _midclt(['call', 'initshutdownscript.create', payload])
                if rc == 0:
                    emit('✓ Démarrage auto configuré (Init/Shutdown Script POSTINIT)', 'ok')
                else:
                    emit(f'⚠ POSTINIT non créé ({err or out}) — à configurer en UI si besoin.', 'warn')
            else:
                emit('⚠ midclt introuvable — démarrage auto non configuré.', 'warn')
        except Exception as _pe:
            emit(f'⚠ Démarrage auto non configuré ({_pe}) — non bloquant, le bureau démarre quand même.', 'warn')

        # ── 9. Démarrage Docker ───────────────────────────────
        emit('▸ Démarrage de la stack Docker...', 'step')
        rc = run_cmd(f'cd {install_dir} && docker compose up -d --force-recreate')
        if rc == 0:
            emit('✓ Stack Docker démarrée', 'ok')
        else:
            emit('✗ Erreur démarrage Docker', 'error')
            emit('➤ Rapport d\'installation complet : %s' % INSTALL_LOG, 'error')
            INSTALL_RUNNING = False
            return

        emit('✓ Journal d\'installation : %s' % INSTALL_LOG, 'ok')
        emit(f'__DONE__{truenas_ip}:{port}', 'done')

    except Exception as e:
        import traceback as _tb
        emit('✗ Erreur : %s' % e, 'error')
        try:
            with open(INSTALL_LOG, 'a', encoding='utf-8') as _lf:
                _lf.write('\n----- TRACEBACK -----\n' + _tb.format_exc() + '\n')
        except Exception:
            pass
        emit('➤ Rapport d\'installation complet (à envoyer en cas de souci) : %s' % INSTALL_LOG, 'error')

    finally:
        INSTALL_RUNNING = False
        INSTALL_DONE = True


# ── HTML du wizard ────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TrueNAS Desktop — Installation</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0f1117; --surface: #1a1d27; --surface2: #22263a;
    --border: #2d3148; --accent: #5b7fff; --success: #4caf87;
    --warn: #f0a500; --error: #e05555; --text: #e8eaf0; --dim: #8892b0;
  }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif;
         min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
          width: 100%; max-width: 600px; overflow: hidden; box-shadow: 0 24px 64px rgba(0,0,0,0.5); }
  .header { background: linear-gradient(135deg, #1e2340 0%, #2a3060 100%);
            padding: 32px; text-align: center; border-bottom: 1px solid var(--border); }
  .logo { font-size: 40px; margin-bottom: 12px; }
  .header h1 { font-size: 22px; font-weight: 700; color: #fff; }
  .header p  { color: var(--dim); font-size: 13px; margin-top: 6px; }
  .steps { display: flex; padding: 24px 32px 20px; gap: 0; border-bottom: 1px solid var(--border); }
  .step  { flex: 1; text-align: center; font-size: 11px; color: var(--dim); position: relative;
            display: flex; flex-direction: column; align-items: center; gap: 10px; }
  .step::after { content: ''; position: absolute; bottom: 11px; left: calc(50% + 14px); right: calc(-50% + 14px);
                  height: 1px; background: var(--border); z-index: 0; }
  .step:last-child::after { display: none; }
  .step-label { font-size: 11px; letter-spacing: 0.2px; }
  .step-dot { width: 24px; height: 24px; border-radius: 50%; background: var(--surface2);
               border: 2px solid var(--border); display: inline-flex; align-items: center;
               justify-content: center; font-size: 10px; font-weight: 700;
               position: relative; z-index: 1; flex-shrink: 0; }
  .step.active .step-dot  { background: var(--accent); border-color: var(--accent); color: #fff; }
  .step.done   .step-dot  { background: var(--success); border-color: var(--success); color: #fff; }
  .step.active { color: var(--text); }
  .body { padding: 32px; }

  /* Prérequis */
  .prereq { display: flex; align-items: center; gap: 12px; padding: 10px 0;
             border-bottom: 1px solid var(--border); }
  .prereq:last-child { border-bottom: none; }
  .prereq-icon { font-size: 18px; width: 24px; text-align: center; }
  .prereq-label { flex: 1; font-size: 14px; }
  .badge { font-size: 11px; padding: 3px 10px; border-radius: 20px; font-weight: 600; }
  .badge.ok   { background: rgba(76,175,135,.15); color: var(--success); }
  .badge.fail { background: rgba(224,85,85,.15);  color: var(--error); }
  .badge.warn { background: rgba(240,165,0,.15);  color: var(--warn); }

  /* Formulaire */
  .section-title { font-size: 11px; font-weight: 700; color: var(--dim); text-transform: uppercase;
                    letter-spacing: 1px; margin: 20px 0 12px; }
  .section-title:first-child { margin-top: 0; }
  .form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .form-group { margin-bottom: 14px; }
  .form-group label { display: block; font-size: 12px; color: var(--dim); margin-bottom: 6px; }
  .form-group input { width: 100%; background: var(--surface2); border: 1px solid var(--border);
                       border-radius: 8px; padding: 10px 12px; color: var(--text); font-size: 13px;
                       transition: border-color .2s; outline: none; }
  .form-group input:focus { border-color: var(--accent); }
  .hint { font-size: 11px; color: var(--dim); margin-top: 4px; }

  /* Log */
  .log { background: #0a0c12; border: 1px solid var(--border); border-radius: 10px;
          padding: 16px; font-family: 'Cascadia Code', 'Fira Code', monospace;
          font-size: 12px; height: 320px; overflow-y: auto; line-height: 1.7; }
  .log::-webkit-scrollbar { display: none; }
  .log { scrollbar-width: none; }
  .log .step  { color: #7b9fff; }
  .log .ok    { color: var(--success); }
  .log .warn  { color: var(--warn); }
  .log .error { color: var(--error); }
  .log .info  { color: #b0bec5; }

  /* Succès */
  .success-box { text-align: center; padding: 20px 0; }
  .success-box .big-icon { font-size: 56px; margin-bottom: 16px; }
  .success-box h2 { font-size: 20px; margin-bottom: 8px; }
  .success-box p  { color: var(--dim); font-size: 14px; }
  .open-btn { display: inline-block; margin-top: 20px; background: var(--accent);
               color: #fff; padding: 12px 32px; border-radius: 10px; text-decoration: none;
               font-weight: 600; font-size: 15px; transition: opacity .2s; }
  .open-btn:hover { opacity: .85; }

  /* Boutons */
  .actions { display: flex; gap: 12px; justify-content: flex-end; margin-top: 24px; }
  .btn { padding: 11px 28px; border-radius: 10px; border: none; cursor: pointer;
          font-size: 14px; font-weight: 600; transition: opacity .2s; }
  .btn:hover { opacity: .85; }
  .btn-primary  { background: var(--accent); color: #fff; }
  .btn-secondary{ background: var(--surface2); color: var(--dim); border: 1px solid var(--border); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }

  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.3);
              border-top-color: #fff; border-radius: 50%; animation: spin .7s linear infinite;
              margin-right: 8px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Pilule / interrupteur (toggle) */
  .form-group label.switch { display: flex; align-items: center; gap: 10px; cursor: pointer; user-select: none; margin-bottom: 0; }
  .switch input { position: absolute; opacity: 0; width: 0; height: 0; }
  .switch .slider { display: inline-block; vertical-align: middle; width: 42px; height: 23px;
                    background: var(--surface2); border: 1px solid var(--border);
                    border-radius: 999px; position: relative; transition: .2s; flex-shrink: 0; }
  .switch .slider::before { content: ''; position: absolute; width: 17px; height: 17px; border-radius: 50%;
                    background: var(--dim); top: 2px; left: 2px; transition: .2s; }
  .switch input:checked + .slider { background: var(--accent); border-color: var(--accent); }
  .switch input:checked + .slider::before { transform: translateX(19px); background: #fff; }
  .switch input:focus-visible + .slider { box-shadow: 0 0 0 3px rgba(91,127,255,.35); }
  .switch-label { display: inline-block; vertical-align: middle; font-size: 13px; color: var(--text); font-weight: 500; }

  [hidden] { display: none !important; }

  /* Input avec bouton picker */
  .input-browse { display: flex; gap: 6px; }
  .input-browse input { flex: 1; }
  .btn-browse { background: var(--surface2); border: 1px solid var(--border); color: var(--dim);
                 border-radius: 8px; padding: 0 12px; cursor: pointer; font-size: 16px;
                 transition: border-color .2s; flex-shrink: 0; }
  .btn-browse:hover { border-color: var(--accent); color: var(--text); }

  /* Modale navigateur */
  .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,.7);
                    display: flex; align-items: center; justify-content: center; z-index: 100; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
            width: 480px; max-width: 95vw; max-height: 80vh; display: flex; flex-direction: column;
            box-shadow: 0 24px 64px rgba(0,0,0,.6); }
  .modal-header { padding: 16px 20px; border-bottom: 1px solid var(--border);
                   display: flex; align-items: center; gap: 10px; }
  .modal-header h3 { flex: 1; font-size: 14px; font-weight: 600; }
  .modal-close { background: none; border: none; color: var(--dim); cursor: pointer;
                  font-size: 18px; padding: 2px 6px; border-radius: 4px; }
  .modal-close:hover { color: var(--text); }
  .modal-path { padding: 10px 20px; background: var(--surface2); font-size: 12px;
                 color: var(--dim); font-family: monospace; border-bottom: 1px solid var(--border); }
  .modal-list { flex: 1; overflow-y: auto; padding: 8px; scrollbar-width: none; }
  .modal-list::-webkit-scrollbar { display: none; }
  .modal-entry { display: flex; align-items: center; gap: 10px; padding: 9px 12px;
                  border-radius: 8px; cursor: pointer; font-size: 13px; }
  .modal-entry:hover { background: var(--surface2); }
  .modal-entry .icon { font-size: 16px; width: 20px; text-align: center; }
  .modal-footer { padding: 14px 20px; border-top: 1px solid var(--border);
                   display: flex; justify-content: flex-end; gap: 10px; }
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="logo"><svg width="54" height="54" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" style="display:block;margin:0 auto;"><defs><linearGradient id="tndg" x1="3" y1="3" x2="21" y2="18" gradientUnits="userSpaceOnUse"><stop stop-color="#8fa9ff"/><stop offset="1" stop-color="#5b7fff"/></linearGradient></defs><rect x="2.4" y="3.4" width="19.2" height="13.2" rx="2.4" stroke="url(#tndg)" stroke-width="1.6"/><path d="M8 20.6h8M12 16.6v4" stroke="url(#tndg)" stroke-width="1.6" stroke-linecap="round"/><circle cx="6.4" cy="7.4" r="1.05" fill="#4caf87"/><path d="M9.6 7.4h8.2" stroke="#8fa9ff" stroke-width="1.4" stroke-linecap="round" opacity="0.85"/><path d="M5.4 10.6h13" stroke="#5b7fff" stroke-width="1.3" stroke-linecap="round" opacity="0.5"/><path d="M5.4 13.2h9" stroke="#5b7fff" stroke-width="1.3" stroke-linecap="round" opacity="0.35"/></svg></div>
    <h1>TrueNAS Desktop</h1>
    <p>Assistant d'installation</p>
  </div>

  <!-- Indicateur étapes -->
  <div class="steps">
    <div class="step active" id="s1"><span class="step-label">Prérequis</span><div class="step-dot" id="d1">1</div></div>
    <div class="step"        id="s2"><span class="step-label">Configuration</span><div class="step-dot" id="d2">2</div></div>
    <div class="step"        id="s3"><span class="step-label">Installation</span><div class="step-dot" id="d3">3</div></div>
    <div class="step"        id="s4"><span class="step-label">Terminé</span><div class="step-dot" id="d4">4</div></div>
  </div>

  <div class="body">

    <!-- Étape 1 : Prérequis -->
    <div id="page1">
      <div id="prereq-list">
        <div style="color:var(--dim);font-size:13px;">Vérification en cours...</div>
      </div>
      <div class="actions">
        <button class="btn btn-primary" id="btn-next1" disabled onclick="goTo(2)">Continuer →</button>
      </div>
    </div>

    <!-- Étape 2 : Configuration -->
    <div id="page2" hidden>
      <div class="section-title">📁 Chemins</div>
      <div class="form-group">
        <label>Répertoire d'installation</label>
        <div class="input-browse">
          <input id="install_dir" value="/mnt/pool/apps/desktop" placeholder="/mnt/&lt;votre-pool&gt;/apps/desktop" />
          <button class="btn-browse" onclick="openBrowser('install_dir')" title="Parcourir">📁</button>
        </div>
        <div id="pool-hint" style="font-size:12px;color:#8a9bbf;margin-top:4px;"></div>
      </div>
      <div class="form-row">
        <div class="form-group">
          <label>Dossier VMs</label>
          <div class="input-browse">
            <input id="vm_dir" value="/mnt/pool/vms" />
            <button class="btn-browse" onclick="openBrowser('vm_dir')" title="Parcourir">📁</button>
          </div>
        </div>
        <div class="form-group">
          <label>Dossier ISOs (racine)</label>
          <div class="input-browse">
            <input id="iso_dir" value="/mnt/pool" />
            <button class="btn-browse" onclick="openBrowser('iso_dir')" title="Parcourir">📁</button>
          </div>
        </div>
      </div>

      <div class="section-title">🌐 Réseau</div>
      <div class="form-row">
        <div class="form-group">
          <label>IP du TrueNAS</label>
          <input id="truenas_ip" placeholder="192.168.1.x" />
        </div>
        <div class="form-group">
          <label>Port du bureau</label>
          <input id="port" value="8099" />
        </div>
      </div>
      <div class="form-group">
        <label>Hostname / FQDN <span style="color:var(--dim)">(optionnel)</span></label>
        <input id="truenas_host" placeholder="même que l'IP si vide" />
        <div class="hint">Utilisé dans les en-têtes nginx. Laissez vide pour utiliser l'IP.</div>
      </div>

      <div class="section-title">🔐 Accès SSH</div>
      <div class="form-row">
        <div class="form-group">
          <label>Utilisateur SSH</label>
          <input id="ssh_user" value="truenas_admin" />
        </div>
        <div class="form-group">
          <label>Mot de passe SSH</label>
          <input id="ssh_pass" type="password" placeholder="••••••••" />
        </div>
      </div>

      <div class="section-title">🔑 Sécurité</div>
      <div class="form-group">
        <label>Token sidecar</label>
        <input id="token" placeholder="Laissez vide pour générer automatiquement" />
        <div class="hint">Clé secrète entre le navigateur et le service fileops.</div>
      </div>

      <div class="form-group">
        <label>Accès au bureau — identifiant</label>
        <input id="desk_user" value="admin" />
        <div class="hint">Login exigé avant d'accéder au bureau (barrière serveur).</div>
      </div>
      <div class="form-group">
        <label>Accès au bureau — mot de passe</label>
        <input id="desk_pass" type="password" placeholder="Laissez vide pour générer" />
      </div>
      <div class="form-group">
        <label class="switch"><input type="checkbox" id="enable_2fa" onchange="document.getElementById('twofa').hidden=!this.checked;updateInstallBtn()" /><span class="slider"></span><span class="switch-label">Activer la double authentification (2FA / Authelia)</span></label>
        <div class="hint">Ajoute un code TOTP. Nécessite 2 domaines locaux (ci-dessous).</div>
      </div>
      <div id="twofa" hidden>
        <div class="form-group">
          <label>Domaine du bureau</label>
          <input id="domain_desktop" placeholder="desktop.exemple.fr" />
        </div>
        <div class="form-group">
          <label>Domaine du portail 2FA</label>
          <input id="domain_auth" placeholder="auth.exemple.fr" />
        </div>
        <div class="form-group">
          <label>E-mail administrateur</label>
          <input id="admin_email" placeholder="admin@exemple.fr" />
        </div>
        <div class="form-group">
          <label>IP de Nginx Proxy Manager (NPM)</label>
          <input id="npm_ip" placeholder="192.168.1.2" />
          <div class="hint">Le HTTPS de la 2FA passe par NPM. Les 2 domaines pointeront vers cette IP.</div>
        </div>
        <div class="form-group" style="margin-top:10px;">
          <label class="switch"><input type="checkbox" id="enable_email" onchange="document.getElementById('emailfields').hidden=!this.checked;updateInstallBtn()" /><span class="slider"></span><span class="switch-label">Envoyer les codes 2FA par email (SMTP)</span></label>
          <div class="hint">Décoché : les codes 2FA sont écrits dans un fichier local (authelia/notification.txt) — le plus simple. Coché : renseigne et teste le SMTP ci-dessous.</div>
        </div>
        <div id="emailfields" hidden>
        <div class="form-row">
          <div class="form-group">
            <label>Serveur SMTP</label>
            <input id="smtp_host" placeholder="mail.exemple.fr" oninput="smtpChanged()" />
          </div>
          <div class="form-group">
            <label>Port</label>
            <input id="smtp_port" value="465" placeholder="465" oninput="smtpChanged()" />
          </div>
        </div>
        <div class="form-group">
          <label>Identifiant SMTP (adresse d'envoi)</label>
          <input id="smtp_user" placeholder="noreply@exemple.fr" oninput="smtpChanged()" />
        </div>
        <div class="form-group">
          <label>Mot de passe SMTP</label>
          <input id="smtp_pass" type="password" placeholder="Laisse vide pour envoyer les codes dans un fichier local" oninput="smtpChanged()" />
          <div class="hint">Si rempli : Authelia envoie les codes par email (port 465 = SSL, 587 = STARTTLS). Si vide : les codes sont écrits dans authelia/notification.txt.</div>
        </div>
        <div class="form-group">
          <button type="button" class="btn btn-secondary" id="btn-smtp-test" onclick="testSmtp()" style="width:100%;justify-content:center;">Tester le SMTP</button>
          <div id="smtp-status" class="hint" style="margin-top:6px;"></div>
          <div class="hint">Le test doit réussir avant de pouvoir installer (sinon l'enrôlement 2FA par email serait impossible).</div>
        </div>
        </div><!-- /emailfields -->
        <div class="hint">L'assistant configure tout le côté NAS. Il reste ensuite à créer 2 hôtes proxy dans NPM + les redirections DNS vers l'IP de NPM — l'assistant affiche les valeurs exactes à la fin. L'enrôlement TOTP se fait après l'installation.</div>
      </div>

      <div class="actions">
        <button class="btn btn-secondary" onclick="goTo(1)">← Retour</button>
        <button class="btn btn-primary" id="btn-install" onclick="startInstall()">Installer →</button>
      </div>
    </div>

    <!-- Étape 3 : Installation -->
    <div id="page3" hidden>
      <div class="log" id="log"></div>
      <div class="actions" style="margin-top:16px;">
        <button class="btn btn-secondary" id="btn-cancel" onclick="window.close()">Fermer</button>
        <button class="btn btn-primary" id="btn-finish" hidden onclick="goTo(4)">Continuer →</button>
      </div>
    </div>

    <!-- Étape 4 : Succès -->
    <div id="page4" hidden>
      <div class="success-box">
        <div class="big-icon">🎉</div>
        <h2>Installation réussie !</h2>
        <p>TrueNAS Desktop est prêt.</p>
        <a id="open-link" href="#" class="open-btn" target="_blank">Ouvrir le bureau →</a>
      </div>
    </div>

  </div>
</div>

<!-- Modale navigateur de dossiers -->
<div class="modal-overlay" id="browser-modal" hidden>
  <div class="modal">
    <div class="modal-header">
      <h3>📁 Choisir un dossier</h3>
      <button class="modal-close" onclick="closeBrowser()">✕</button>
    </div>
    <div class="modal-path" id="browser-path">/mnt</div>
    <div class="modal-list" id="browser-list"></div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeBrowser()">Annuler</button>
      <button class="btn btn-primary"   onclick="selectCurrent()">Choisir ce dossier</button>
    </div>
  </div>
</div>

<script>
let currentPage = 1;

function goTo(n) {
  document.getElementById('page' + currentPage).hidden = true;
  document.getElementById('s'    + currentPage).classList.remove('active');
  if (n > currentPage) document.getElementById('s' + currentPage).classList.add('done');
  currentPage = n;
  document.getElementById('page' + n).hidden = false;
  document.getElementById('s'    + n).classList.add('active');
  if (n === 2) updateInstallBtn();
}

// ── Vérification SMTP avant install (2FA par email) ───────────
var smtpState = 'untested';
function _v(id){ var e = document.getElementById(id); return e ? e.value.trim() : ''; }
function _emailOn(){ var e = document.getElementById('enable_email'); return !!(e && e.checked); }
function smtpUsesEmail(){
  return _emailOn() && !!(_v('smtp_host') && _v('smtp_user') && document.getElementById('smtp_pass').value);
}
function smtpChanged(){
  smtpState = 'untested';
  var s = document.getElementById('smtp-status');
  if (s){ s.textContent = ''; s.style.color = ''; }
  updateInstallBtn();
}
function styleSmtpBtn(){
  var b = document.getElementById('btn-smtp-test');
  if (!b) return;
  var has = !!(_v('smtp_host') || _v('smtp_user') || document.getElementById('smtp_pass').value);
  if (smtpState === 'ok'){
    b.style.background = 'rgba(76,175,135,.18)'; b.style.borderColor = 'var(--success)'; b.style.color = 'var(--success)';
  } else if (has){
    b.style.background = 'rgba(240,165,0,.18)'; b.style.borderColor = 'var(--warn)'; b.style.color = 'var(--warn)';
  } else {
    b.style.background = ''; b.style.borderColor = ''; b.style.color = '';
  }
}
function updateInstallBtn(){
  styleSmtpBtn();
  var btn = document.getElementById('btn-install');
  if (!btn) return;
  var twofa = document.getElementById('enable_2fa').checked;
  var block = twofa && smtpUsesEmail() && smtpState !== 'ok';
  btn.disabled = block;
  btn.style.opacity = block ? '0.5' : '';
  btn.style.cursor  = block ? 'not-allowed' : '';
  btn.title = block ? 'Teste le SMTP (il doit réussir), ou laisse le mot de passe SMTP vide.' : '';
}
async function testSmtp(){
  var btn = document.getElementById('btn-smtp-test');
  var s   = document.getElementById('smtp-status');
  var host = _v('smtp_host'), port = _v('smtp_port') || '465', user = _v('smtp_user');
  var pass = document.getElementById('smtp_pass').value;
  if (!host || !user || !pass){
    s.textContent = 'Renseigne serveur, identifiant et mot de passe SMTP.';
    s.style.color = '#f0b429';
    return;
  }
  btn.disabled = true; var old = btn.textContent; btn.textContent = 'Test en cours…';
  s.textContent = ''; s.style.color = '';
  try {
    var r = await fetch('/smtp-test', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ host: host, port: port, user: user, pass: pass }) });
    var j = await r.json();
    if (j.ok){ smtpState = 'ok';
      s.textContent = '✓ Connexion SMTP réussie (authentification OK).'; s.style.color = '#3ecf8e'; }
    else { smtpState = 'fail';
      s.textContent = '✗ Échec SMTP : ' + (j.error || 'inconnu'); s.style.color = '#ff6b6b'; }
  } catch(e){ smtpState = 'fail';
    s.textContent = '✗ Échec SMTP : ' + e; s.style.color = '#ff6b6b'; }
  btn.disabled = false; btn.textContent = old;
  updateInstallBtn();
}

// ── Navigateur de dossiers ────────────────────────────────────
let _browserTarget = null;
let _browserPath   = '/mnt';

function openBrowser(inputId) {
  _browserTarget = inputId;
  const cur = document.getElementById(inputId).value.trim();
  browseTo(cur || '/mnt');
  document.getElementById('browser-modal').hidden = false;
}

function closeBrowser() {
  document.getElementById('browser-modal').hidden = true;
}

function selectCurrent() {
  if (_browserTarget) document.getElementById(_browserTarget).value = _browserPath;
  closeBrowser();
}

function browseTo(path) {
  _browserPath = path;
  document.getElementById('browser-path').textContent = path;
  const list = document.getElementById('browser-list');
  list.innerHTML = '<div style="padding:20px;color:var(--dim);text-align:center">Chargement...</div>';
  fetch('/browse?path=' + encodeURIComponent(path))
    .then(r => r.json())
    .then(data => {
      _browserPath = data.path;
      document.getElementById('browser-path').textContent = data.path;
      if (!data.entries.length) {
        list.innerHTML = '<div style="padding:20px;color:var(--dim);text-align:center">Dossier vide</div>';
        return;
      }
      list.innerHTML = '';
      data.entries.forEach(e => {
        const div = document.createElement('div');
        div.className = 'modal-entry';
        div.innerHTML = `<span class="icon">${e.type === 'parent' ? '↩' : '📁'}</span><span>${e.name}</span>`;
        div.onclick = () => browseTo(e.path);
        list.appendChild(div);
      });
    })
    .catch(function(err){
      list.innerHTML = '<div style="padding:20px;color:var(--error);text-align:center">Erreur : ' + err + '</div>';
    });
}

// ── Étape 1 : prérequis ───────────────────────────────────────
fetch('/check').then(r => r.json()).then(data => {
  const icons = { root: '👤', docker: '🐳', python: '🐍', openssl: '🔑' };
  const labels = { root: 'Exécuté en root', docker: 'Docker disponible',
                   python: 'Python 3.6+', openssl: 'OpenSSL (génération token)' };
  let allOk = true;
  let html = '';
  for (const [k, ok] of Object.entries(data)) {
    const critical = k !== 'openssl';
    if (!ok && critical) allOk = false;
    const badge = ok ? '<span class="badge ok">✓ OK</span>'
                     : critical ? '<span class="badge fail">✗ Manquant</span>'
                                : '<span class="badge warn">⚠ Optionnel</span>';
    html += `<div class="prereq">
      <span class="prereq-icon">${icons[k]}</span>
      <span class="prereq-label">${labels[k]}</span>
      ${badge}
    </div>`;
  }
  document.getElementById('prereq-list').innerHTML = html;
  if (allOk) document.getElementById('btn-next1').disabled = false;

  // Auto-remplir l'IP
  fetch('/ip').then(r => r.text()).then(ip => {
    document.getElementById('truenas_ip').value = ip.trim();
  });

  // Auto-détecter le pool et pré-remplir les chemins
  fetch('/pools').then(r => r.json()).then(pools => {
    if (pools && pools.length) {
      const p = '/mnt/' + pools[0];
      document.getElementById('install_dir').value = p + '/apps/desktop';
      document.getElementById('vm_dir').value = p + '/vms';
      document.getElementById('iso_dir').value = p;
      if (pools.length > 1) {
        const hint = document.getElementById('pool-hint');
        if (hint) hint.textContent = 'Pools détectés : ' + pools.join(', ') + ' — utilise 📁 pour en choisir un autre.';
      }
    }
  }).catch(() => {});
});

// ── Étape 3 : installation ─────────────────────────────────────
function startInstall() {
  const ip   = document.getElementById('truenas_ip').value.trim();
  const pass = document.getElementById('ssh_pass').value.trim();
  if (!ip)   { alert('IP TrueNAS obligatoire'); return; }
  if (!pass) { alert('Mot de passe SSH obligatoire'); return; }
  if (document.getElementById('enable_2fa').checked && smtpUsesEmail() && smtpState !== 'ok') {
    alert('Teste le SMTP (il doit réussir) avant de lancer, ou laisse le mot de passe SMTP vide pour utiliser le fichier local.');
    return;
  }

  const config = {
    install_dir:  document.getElementById('install_dir').value.trim(),
    vm_dir:       document.getElementById('vm_dir').value.trim(),
    iso_dir:      document.getElementById('iso_dir').value.trim(),
    port:         document.getElementById('port').value.trim(),
    truenas_ip:   ip,
    truenas_host: document.getElementById('truenas_host').value.trim() || ip,
    ssh_user:     document.getElementById('ssh_user').value.trim(),
    ssh_pass:     pass,
    token:        document.getElementById('token').value.trim(),
    desk_user:      document.getElementById('desk_user').value.trim(),
    desk_pass:      document.getElementById('desk_pass').value.trim(),
    enable_2fa:     document.getElementById('enable_2fa').checked,
    domain_desktop: document.getElementById('domain_desktop').value.trim(),
    domain_auth:    document.getElementById('domain_auth').value.trim(),
    admin_email:    document.getElementById('admin_email').value.trim(),
    npm_ip:         document.getElementById('npm_ip').value.trim(),
    smtp_host:      _emailOn() ? document.getElementById('smtp_host').value.trim() : '',
    smtp_port:      document.getElementById('smtp_port').value.trim(),
    smtp_user:      _emailOn() ? document.getElementById('smtp_user').value.trim() : '',
    smtp_pass:      _emailOn() ? document.getElementById('smtp_pass').value : '',
  };

  goTo(3);

  fetch('/install', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(config)
  });

  // SSE pour la progression
  const log = document.getElementById('log');
  const es  = new EventSource('/events');
  es.onmessage = function(e) {
    const data = JSON.parse(e.data);
    if (data.msg.startsWith('__DONE__')) {
      es.close();
      const addr = data.msg.replace('__DONE__', '');
      document.getElementById('open-link').href = 'http://' + addr;
      const done = document.createElement('div');
      done.className = 'ok';
      done.textContent = '✅ Installation terminée — vérifie le journal ci-dessus, puis clique « Continuer ».';
      log.appendChild(done);
      log.scrollTop = log.scrollHeight;
      document.getElementById('btn-finish').hidden = false;
      return;
    }
    const cls = data.level === 'step' ? 'step' : data.level;
    const line = document.createElement('div');
    line.className = cls;
    line.textContent = data.msg;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
  };
  es.onerror = function() { es.close(); };
}
</script>
</body>
</html>"""


# ── Serveur HTTP ──────────────────────────────────────────────
def _smtp_test(host, port, user, password):
    """Teste la connexion + authentification SMTP telle qu'Authelia l'utilisera
    (465 = SSL implicite, sinon STARTTLS). Retourne {'ok': bool, 'error': str}."""
    import smtplib, ssl
    host = (host or '').strip()
    user = (user or '').strip()
    password = password or ''
    try:
        port = int(str(port or '465').strip())
    except Exception:
        port = 465
    if not host or not user or not password:
        return {'ok': False, 'error': 'Renseigne serveur, identifiant et mot de passe SMTP.'}
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) as s:
                s.login(user, password)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.ehlo()
                try:
                    s.starttls(context=ctx)
                    s.ehlo()
                except smtplib.SMTPException:
                    pass  # certains serveurs 587 acceptent sans STARTTLS
                s.login(user, password)
        return {'ok': True, 'error': ''}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


class WizardHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # Silencieux

    def do_GET(self):
        if self.path in ('/', '/setup'):
            self._html()
        elif self.path == '/check':
            self._json(check_prerequisites())
        elif self.path == '/ip':
            self._text(get_local_ip())
        elif self.path == '/pools':
            self._json(list_pools())
        elif self.path == '/events':
            self._sse()
        elif self.path.startswith('/browse'):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            path   = params.get('path', ['/mnt'])[0]
            self._browse(path)
        else:
            self.send_error(404)

    def _browse(self, path):
        try:
            path = os.path.realpath(path)
            entries = []
            if path != '/':
                entries.append({'name': '..', 'path': str(os.path.dirname(path)), 'type': 'parent'})
            for name in sorted(os.listdir(path)):
                full = os.path.join(path, name)
                if os.path.isdir(full) and not name.startswith('.'):
                    entries.append({'name': name, 'path': full, 'type': 'dir'})
            self._json({'path': path, 'entries': entries})
        except Exception as e:
            self._json({'path': path, 'entries': [], 'error': str(e)})

    def do_POST(self):
        if self.path == '/install':
            length = int(self.headers.get('Content-Length', 0))
            body   = self.rfile.read(length)
            config = json.loads(body)
            if not INSTALL_RUNNING:
                t = threading.Thread(target=run_install, args=(config,), daemon=True)
                t.start()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'ok')
        elif self.path == '/smtp-test':
            length = int(self.headers.get('Content-Length', 0))
            try:
                cfg = json.loads(self.rfile.read(length) or b'{}')
            except Exception:
                cfg = {}
            self._json(_smtp_test(cfg.get('host'), cfg.get('port'),
                                  cfg.get('user'), cfg.get('pass')))
        else:
            self.send_error(404)

    def _html(self):
        data = HTML.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def _text(self, txt):
        data = txt.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', len(data))
        self.end_headers()
        self.wfile.write(data)

    def _sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
        try:
            while True:
                try:
                    item = INSTALL_EVENTS.get(timeout=30)
                    msg  = json.dumps(item)
                    self.wfile.write(f'data: {msg}\n\n'.encode())
                    self.wfile.flush()
                    if item.get('level') == 'done':
                        break
                except queue.Empty:
                    # Keepalive
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
        except Exception:
            pass


# ── Main ──────────────────────────────────────────────────────
if __name__ == '__main__':
    ip = get_local_ip()
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), WizardHandler)
    print()
    print('╔══════════════════════════════════════════╗')
    print('║      TrueNAS Desktop  —  Wizard          ║')
    print('╚══════════════════════════════════════════╝')
    print()
    print(f'  Ouvrez dans votre navigateur :')
    print(f'  ➜  http://{ip}:{PORT}/setup')
    print()
    print('  Ctrl+C pour arrêter le wizard.')
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nWizard arrêté.')
