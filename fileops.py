#!/usr/bin/env python3
"""
Desktroo – File-ops & PTY terminal sidecar
HTTP  → port FILEOPS_PORT   (default 8765)
WS    → port FILEOPS_WS_PORT (default 8766)

Fonctionnalités :
  - Opérations fichiers (list/delete/rename/copy/move)
  - Terminal PTY via WebSocket
  - Gestion VMs QEMU/KVM via libvirt+SSH (/libvirt2/…)
  - Proxy VNC WebSocket via SSH (/vnc-proxy)
"""
import asyncio, fcntl, json, logging, os, pty, re, struct, subprocess, sys, termios
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from threading import Thread
from urllib.parse import parse_qsl, urlparse

import websockets

try:
    import paramiko
    _HAS_PARAMIKO = True
except ImportError:
    _HAS_PARAMIKO = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

TOKEN      = os.environ.get('FILEOPS_TOKEN',    'changeme-secret-token')
PORT       = int(os.environ.get('FILEOPS_PORT',    '8765'))
WS_PORT    = int(os.environ.get('FILEOPS_WS_PORT', '8766'))

SSH_HOST   = os.environ.get('TRUENAS_SSH_HOST', '192.168.1.10')
SSH_USER   = os.environ.get('TRUENAS_SSH_USER', 'truenas_admin')
SSH_PASS   = os.environ.get('TRUENAS_SSH_PASS', '')
SSH_PORT_N = int(os.environ.get('TRUENAS_SSH_PORT', '22'))
VM_DIR     = os.environ.get('VM_DIR',  '/mnt/Truenas_Stockage/vms')
ISO_DIR    = os.environ.get('ISO_DIR', '/mnt/Truenas_Stockage')

# ── Version & mise à jour ─────────────────────────────────────────────────────
APP_VERSION = '2.8.0'
APP_DIR     = os.environ.get('APP_DIR', '')  # dossier d'install (contient fileops.py, HTML…)
GITHUB_RAW  = os.environ.get('GITHUB_RAW', 'https://raw.githubusercontent.com/Nabief/desktroo-dist/main').rstrip('/')

# MDM-ACCESS-POLICY-V1-BEGIN
import tempfile as _tempfile
import threading as _access_threading
from datetime import datetime as _access_datetime, timezone as _access_timezone

# Données de l'application (droits d'accès, partages, sites, licence, identifiant
# d'installation…) : elles vivent dans le dossier d'installation, sous « data ».
# Jusqu'à la 2.0.0, faute de réglage, ce dossier retombait sur un chemin écrit en dur
# (_LEGACY_DATA_DIR) quel que soit le NAS : une installation faite ailleurs y a rangé
# ses données. On les rapatrie une fois, au démarrage. Sans APP_DIR (aucun installeur
# ne le laisse vide), l'ancien chemin reste le repli.
_LEGACY_DATA_DIR = '/mnt/Truenas_Stockage/apps/desktop/data'
ACCESS_DATA_DIR = (os.environ.get('ACCESS_DATA_DIR')
                   or (os.path.join(APP_DIR, 'data') if APP_DIR else _LEGACY_DATA_DIR))


def _access_migrate_legacy_data(new=None, old=None):
    """Copie les données de l'ancien emplacement vers le nouveau, puis retire l'ancien.
    Ne fait rien si le nouvel emplacement contient déjà quelque chose."""
    new = new or ACCESS_DATA_DIR
    old = old or _LEGACY_DATA_DIR
    try:
        if os.path.realpath(new) == os.path.realpath(old) or not os.path.isdir(old):
            return False
        if os.path.isdir(new) and os.listdir(new):
            return False
        import shutil
        names = os.listdir(old)
        os.makedirs(new, exist_ok=True)
        for name in names:
            src, dst = os.path.join(old, name), os.path.join(new, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        # Tout est copié : l'ancien dossier ne sert plus. On remonte en retirant les
        # dossiers restés vides (un vrai pool monté là n'est jamais vide : il reste).
        shutil.rmtree(old)
        parent = os.path.dirname(old)
        while parent not in ('/mnt', '/', ''):
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
        log.info('Données rapatriées de %s vers %s (%d élément(s))', old, new, len(names))
        return True
    except Exception as exc:
        log.warning('Rapatriement des données de %s vers %s impossible : %s', old, new, exc)
        return False


_access_migrate_legacy_data()


def _remove_legacy_page():
    """Retire l'ancienne page « truenas-desktop.html » (avant le renommage) restée dans le
    dossier d'installation. On ne la retire que si la nouvelle page est bien là."""
    if not APP_DIR:
        return False
    old, new = os.path.join(APP_DIR, 'truenas-desktop.html'), os.path.join(APP_DIR, 'desktroo.html')
    try:
        if os.path.isfile(old) and os.path.isfile(new):
            os.remove(old)
            log.info('Ancienne page retirée : %s', old)
            return True
    except OSError as exc:
        log.warning('Ancienne page non retirée (%s) : %s', old, exc)
    return False


_remove_legacy_page()
ACCESS_POLICY_FILE = os.path.join(ACCESS_DATA_DIR, 'access-policy.json')
ACCESS_HISTORY_FILE = os.path.join(ACCESS_DATA_DIR, 'access-history.json')
_access_lock = _access_threading.RLock()


def _access_default_policy():
    return {'version': 1, 'subjects': {}}


def _access_read_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            value = json.load(handle)
        return value
    except FileNotFoundError:
        return default
    except Exception as exc:
        log.warning('Unable to read access data %s: %s', path, exc)
        return default


def _access_write_json(path, value):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = _tempfile.mkstemp(
        prefix='.access-', suffix='.tmp', dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def access_policy_get():
    with _access_lock:
        policy = _access_read_json(ACCESS_POLICY_FILE, _access_default_policy())
        if not isinstance(policy, dict):
            policy = _access_default_policy()
        policy.setdefault('version', 1)
        if not isinstance(policy.get('subjects'), dict):
            policy['subjects'] = {}
        return policy


def access_history_get(limit=150):
    with _access_lock:
        history = _access_read_json(ACCESS_HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        limit = max(1, min(int(limit or 150), 1000))
        return history[-limit:][::-1]


def access_history_add(event):
    if not isinstance(event, dict):
        raise ValueError('Historique invalide')
    safe = {
        'time': _access_datetime.now(_access_timezone.utc).isoformat(),
        'actor': str(event.get('actor') or 'unknown')[:128],
        'action': str(event.get('action') or 'Modification')[:256],
        'subject': str(event.get('subject') or '')[:256],
        'resource': str(event.get('resource') or '')[:512],
        'before': event.get('before'),
        'after': event.get('after'),
    }
    with _access_lock:
        history = _access_read_json(ACCESS_HISTORY_FILE, [])
        if not isinstance(history, list):
            history = []
        history.append(safe)
        history = history[-1000:]
        _access_write_json(ACCESS_HISTORY_FILE, history)
    return safe


def access_policy_set(subject, applications, actor='unknown'):
    subject = str(subject or '').strip().lower()
    if not re.fullmatch(r'(user|group):\d+', subject):
        raise ValueError('Sujet invalide')
    if not isinstance(applications, dict):
        raise ValueError('La politique applications doit être un objet')

    cleaned = {}
    for app_name, mode in applications.items():
        name = str(app_name or '').strip()
        value = str(mode or '').strip().upper()
        if not name or len(name) > 255:
            continue
        if value in ('ALLOW', 'DENY'):
            cleaned[name] = value
        elif value not in ('', 'INHERIT'):
            raise ValueError('Mode application invalide pour ' + name)

    with _access_lock:
        policy = access_policy_get()
        subjects = policy.setdefault('subjects', {})
        before = dict((subjects.get(subject) or {}).get('applications') or {})
        if cleaned:
            entry = subjects.setdefault(subject, {})
            entry['applications'] = cleaned
        else:
            subjects.pop(subject, None)
        policy['updated_at'] = _access_datetime.now(_access_timezone.utc).isoformat()
        _access_write_json(ACCESS_POLICY_FILE, policy)

    access_history_add({
        'actor': actor,
        'action': 'Politique applications modifiée',
        'subject': subject,
        'resource': 'applications',
        'before': before,
        'after': cleaned,
    })
    return policy
# MDM-ACCESS-POLICY-V1-END


# ── Threading HTTP server ─────────────────────────────────────────────────────
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── MDM-SERVER-LOGIN-V1 : connexion vérifiée par le serveur ──────────────────
# Le jeton (TOKEN) n'est plus écrit dans la page du bureau. Le navigateur l'obtient
# en présentant des identifiants TrueNAS à POST /auth/login ; fileops les vérifie
# lui-même auprès de TrueNAS (même appel JSON-RPC que le bureau) et ne remet le jeton
# qu'à un compte ayant les pleins droits d'administration.
import threading as _login_threading
import time as _login_time
import hmac as _login_hmac

TRUENAS_API_HOST = os.environ.get('TRUENAS_API_HOST') or SSH_HOST
LOGIN_MAX_FAILURES = 10      # échecs tolérés…
LOGIN_WINDOW = 300           # …sur cette durée (secondes), tous clients confondus
_login_lock = _login_threading.Lock()
_login_failures = []


def _login_blocked():
    now = _login_time.time()
    with _login_lock:
        _login_failures[:] = [t for t in _login_failures if now - t < LOGIN_WINDOW]
        return len(_login_failures) >= LOGIN_MAX_FAILURES


def _login_note_failure():
    with _login_lock:
        _login_failures.append(_login_time.time())


async def _truenas_verify_async(username, password, apikey):
    """Ouvre une session JSON-RPC sur TrueNAS avec ces identifiants.
    Rend (identifiants_valides, résultat de auth.me ou None)."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # certificat auto-signé du NAS, comme côté nginx
    ctx.verify_mode = ssl.CERT_NONE
    uri = 'wss://%s/api/current' % TRUENAS_API_HOST
    async with websockets.connect(uri, ssl=ctx, open_timeout=10, max_size=4 * 1024 * 1024) as ws:
        async def call(ident, method, params):
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 12))
                if isinstance(msg, dict) and msg.get('id') == ident:
                    return msg
        if apikey:
            r = await call(1, 'auth.login_with_api_key', [apikey])
        else:
            r = await call(1, 'auth.login', [username, password, None])
        if r.get('error') or r.get('result') is not True:
            return False, None
        me = await call(2, 'auth.me', [])
        return True, (None if me.get('error') else me.get('result'))


def _truenas_verify(username, password, apikey):
    return asyncio.run(asyncio.wait_for(_truenas_verify_async(username, password, apikey), 30))


def _truenas_is_full_admin(me):
    """Le compte a-t-il les pleins droits ? fileops donne un accès root (fichiers,
    terminal) : un compte TrueNAS aux droits restreints ne doit pas l'obtenir.
    Si TrueNAS ne renvoie aucune information de privilège, on ne bloque pas."""
    if not isinstance(me, dict) or not isinstance(me.get('privilege'), dict):
        return True
    priv = me['privilege']
    roles = priv.get('roles')
    if isinstance(roles, dict):
        roles = roles.get('$set')
    if isinstance(roles, (list, tuple)) and 'FULL_ADMIN' in roles:
        return True
    for rule in priv.get('allowlist') or []:
        if isinstance(rule, dict) and rule.get('method') == '*' and rule.get('resource') == '*':
            return True
    return False


def auth_login(body):
    """Traite POST /auth/login. Rend (code HTTP, réponse JSON)."""
    if _login_blocked():
        return 429, {'error': 'Trop de tentatives de connexion. Réessaie dans quelques minutes.'}
    body = body if isinstance(body, dict) else {}
    apikey = str(body.get('apikey') or '').strip()
    username = str(body.get('username') or '').strip()
    password = str(body.get('password') or '')
    if not apikey and not (username and password):
        return 400, {'error': 'Identifiants manquants.'}
    try:
        ok, me = _truenas_verify(username, password, apikey)
    except Exception as exc:
        log.warning('Connexion : TrueNAS injoignable (%s) : %s', TRUENAS_API_HOST, exc)
        return 502, {'error': 'TrueNAS injoignable : vérification des identifiants impossible.'}
    if not ok:
        _login_note_failure()
        log.warning('Connexion refusée : identifiants TrueNAS incorrects')
        return 401, {'error': 'Identifiants TrueNAS incorrects.'}
    if not _truenas_is_full_admin(me):
        log.warning('Connexion refusée : compte sans les pleins droits (%s)',
                    (me or {}).get('pw_name') or username or 'clé API')
        return 403, {'error': "Ce compte TrueNAS n'a pas les pleins droits d'administration."}
    return 200, {'ok': True, 'token': TOKEN}
# ── MDM-SERVER-LOGIN-V1 (fin) ────────────────────────────────────────────────


# ── SSH connection pool ───────────────────────────────────────────────────────
import threading as _threading
_ssh_pool   = []
_ssh_lock   = _threading.Lock()
_SSH_MAX    = 4   # max idle connections kept

def _ssh_get():
    """Get a live SSH client from pool or create a new one."""
    with _ssh_lock:
        while _ssh_pool:
            client = _ssh_pool.pop()
            try:
                transport = client.get_transport()
                if transport and transport.is_active():
                    return client
                client.close()
            except Exception:
                pass
    if not _HAS_PARAMIKO:
        raise RuntimeError('paramiko non disponible — relancez le container')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(SSH_HOST, port=SSH_PORT_N, username=SSH_USER, password=SSH_PASS,
                   timeout=10, auth_timeout=15, banner_timeout=15)
    return client

def _ssh_put(client):
    """Return a client to the pool (if healthy)."""
    try:
        transport = client.get_transport()
        if transport and transport.is_active():
            with _ssh_lock:
                if len(_ssh_pool) < _SSH_MAX:
                    _ssh_pool.append(client)
                    return
        client.close()
    except Exception:
        pass

# ── SSH helpers ───────────────────────────────────────────────────────────────
def ssh_exec(cmd, timeout=30):
    client = _ssh_get()
    try:
        _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out  = stdout.read().decode('utf-8', errors='replace')
        err  = stderr.read().decode('utf-8', errors='replace')
        code = stdout.channel.recv_exit_status()
        _ssh_put(client)
        return out.strip(), err.strip(), code
    except Exception:
        try: client.close()
        except: pass
        raise


def ssh_ok(cmd, timeout=30):
    out, err, code = ssh_exec(cmd, timeout)
    if code != 0:
        raw = (err or out or 'exit ' + str(code)).strip()
        # Show up to 5 lines so virsh errors are readable
        lines = raw.splitlines()
        msg = ' | '.join(lines[:5])
        raise RuntimeError(msg)
    return out


def shq(s):
    return "'" + str(s).replace("'", "'\\''") + "'"


def _privileged_remove(path):
    """Supprime un fichier/dossier. Essaie localement (conteneur root) puis, en cas
    d'échec (ACL ZFS/NFSv4 refusant la suppression), bascule sur 'sudo rm' côté host
    via SSH — le vrai root du système, qui a l'accès complet aux datasets."""
    path = str(path or '')
    if not path or not os.path.exists(path):
        return
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            _sh_shutil.rmtree(path)
        else:
            os.remove(path)
        return
    except Exception as local_err:
        try:
            out, err, code = ssh_exec("sudo -n rm -rf " + shq(path), timeout=90)
        except Exception as ssh_err:
            raise RuntimeError('Suppression impossible : ' + str(local_err) + ' / SSH: ' + str(ssh_err))
        if code != 0 or os.path.exists(path):
            raise RuntimeError('Suppression refusée (' + (err or out or str(local_err)).strip()[:200] + ')')


def _unique_path(dst):
    """Retourne un chemin libre à partir de dst : name (1).ext, name (2).ext…"""
    if not os.path.exists(dst):
        return dst
    d = os.path.dirname(dst)
    base = os.path.basename(dst.rstrip('/'))
    # Gère .tar.gz / .tar.bz2 comme extension composée.
    low = base.lower()
    for comp in ('.tar.gz', '.tar.bz2', '.tar.xz'):
        if low.endswith(comp):
            stem, ext = base[:-len(comp)], base[-len(comp):]
            break
    else:
        stem, ext = os.path.splitext(base)
    i = 1
    while True:
        cand = os.path.join(d, stem + ' (' + str(i) + ')' + ext)
        if not os.path.exists(cand):
            return cand
        i += 1


# ── libvirt / virsh helpers ───────────────────────────────────────────────────
# MDM-SUDO-VIRSH-26 : commandes libvirt exécutées via sudo non interactif.
VIRSH_URI = 'qemu:///system'

# MDM-LIBVIRT-AUTORECOVER-V1-BEGIN
_libvirt_ensure_lock = _threading.Lock()
_libvirt_last_ok = 0.0
_libvirt_cfg_ensured = False


def _ensure_libvirt_apparmor_off():
    """Après une mise à jour de TrueNAS, l'intégration AppArmor de libvirt peut
    être cassée ('cannot load AppArmor profile') et empêcher le démarrage des VMs.
    On force security_driver = "none" dans qemu.conf (idempotent) — la VM reste
    isolée par KVM et la gestion DAC des permissions de disque continue.
    Renvoie True si un changement a été appliqué."""
    inner = (
        'if ! grep -q \'^security_driver = "none"\' /etc/libvirt/qemu.conf 2>/dev/null; then '
        'cp -f /etc/libvirt/qemu.conf /etc/libvirt/qemu.conf.bak 2>/dev/null || true; '
        'sed -i \'s/^[#[:space:]]*security_driver[[:space:]]*=.*/security_driver = "none"/\' /etc/libvirt/qemu.conf 2>/dev/null || true; '
        'grep -q \'^security_driver = "none"\' /etc/libvirt/qemu.conf || echo \'security_driver = "none"\' >> /etc/libvirt/qemu.conf; '
        'systemctl restart libvirtd 2>/dev/null || systemctl restart virtqemud 2>/dev/null || true; '
        'echo CHANGED; '
        'fi'
    )
    out, _err, _code = ssh_exec("sudo -n sh -c " + shq(inner), timeout=60)
    return 'CHANGED' in (out or '')


# MDM-HOST-BOOTSTRAP-V2 : préparation du host pour la gestion des VMs, réalisée
# automatiquement via SSH+sudo. NE MODIFIE PLUS /etc (ni drop-in systemd libvirtd,
# ni règle polkit, ni 'systemctl enable') : ces écritures provoquaient un
# « ordering cycle » systemd fatal sur TrueNAS 25.x (ix-etc/middlewared en échec).
# libvirt est démarré À LA DEMANDE ; les anciennes modifs sont nettoyées.
# Idempotent. Installation 100% interface web (aucun shell requis).
_host_bootstrap_done = False


def _host_bootstrap():
    global _host_bootstrap_done
    if _host_bootstrap_done:
        return
    if not (SSH_HOST and SSH_USER and SSH_PASS):
        log.info('Host bootstrap ignoré (SSH non configuré).')
        return
    script = (
        'set +e\n'
        # -- Réparation : supprime les anciennes modifs /etc qui cassaient le boot 25.x --
        'systemctl disable truenas-desktop 2>/dev/null || true\n'
        'rm -f /etc/systemd/system/truenas-desktop.service\n'
        'rm -f /etc/systemd/system/libvirtd.service.d/notimeout.conf\n'
        'rmdir /etc/systemd/system/libvirtd.service.d 2>/dev/null || true\n'
        'rm -f /etc/tmpfiles.d/truenas-libvirt.conf\n'
        'rm -f /etc/polkit-1/rules.d/80-truenas-libvirt.rules\n'
        'systemctl daemon-reload 2>/dev/null || true\n'
        # -- libvirt À LA DEMANDE : start seulement, jamais enable ni drop-in --
        'systemctl unmask libvirtd.socket libvirtd-ro.socket libvirtd-admin.socket 2>/dev/null || true\n'
        'systemctl start libvirtd 2>/dev/null || systemctl start virtqemud 2>/dev/null || true\n'
        'for i in $(seq 1 15); do [ -S /run/libvirt/libvirt-sock ] && break; sleep 1; done\n'
        'virsh -c qemu:///system net-start default 2>/dev/null || true\n'
        'echo HOST_BOOTSTRAP_OK\n'
    )
    try:
        out, err, _code = ssh_exec("sudo -n sh -c " + shq(script), timeout=120)
        if 'HOST_BOOTSTRAP_OK' in (out or ''):
            _host_bootstrap_done = True
            log.info('Host bootstrap OK (libvirt à la demande + réseau default, sans modif /etc).')
        else:
            log.warning('Host bootstrap incomplet : %s', ((err or out) or '')[:200])
    except Exception as e:
        log.warning('Host bootstrap échec : %s', e)


# ── MDM-SELF-UPDATE-V1 : version & mise à jour depuis GitHub ──────────────────
def _fetch_text(url, timeout=15):
    import urllib.request
    req = urllib.request.Request(url, headers={'User-Agent': 'Desktroo'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace').strip()


def _truenas_ip():
    """Renvoie l'adresse IP LAN réelle du NAS, indépendante d'un nom de domaine
    configuré comme hôte SSH. On refuse toute IP publique : mieux vaut retomber
    sur le nom d'hôte que d'ouvrir une IP WAN. Priorité :
    IP littérale SSH > proxy_pass de nginx.conf (IP choisie à l'install) >
    IP privée réellement connectée en SSH > DNS si privée > hôte brut."""
    import socket as _socket, re as _re, os as _os

    def _is_ip(x):
        return bool(_re.match(r'^\d{1,3}(\.\d{1,3}){3}$', str(x or '')))

    def _is_private(x):
        m = _re.match(r'^(\d+)\.(\d+)\.(\d+)\.\d+$', str(x or ''))
        if not m:
            return False
        a, b = int(m.group(1)), int(m.group(2))
        return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or a == 127

    host = (SSH_HOST or '').strip()
    if _is_ip(host):
        return host

    # 1) IP littérale du proxy_pass de nginx.conf (celle choisie à l'installation).
    try:
        base = os.environ.get('APP_DIR', '') or (APP_DIR or '')
        conf = _os.path.join(base, 'nginx.conf') if base else ''
        if conf and _os.path.isfile(conf):
            txt = open(conf, 'r', encoding='utf-8', errors='replace').read()
            m = _re.search(r'proxy_pass\s+https?://(\d{1,3}(?:\.\d{1,3}){3})', txt)
            if m:
                return m.group(1)
    except Exception:
        pass

    # 2) IP privée réellement jointe par le sidecar en SSH.
    try:
        with _ssh_lock:
            for _c in list(_ssh_pool):
                try:
                    _tr = _c.get_transport()
                    _sock = getattr(_tr, 'sock', None)
                    if _tr and _tr.is_active() and _sock is not None:
                        _ip = _sock.getpeername()[0]
                        if _is_private(_ip):
                            return str(_ip)
                except Exception:
                    pass
    except Exception:
        pass

    # 3) résolution DNS, seulement si le résultat est une IP privée (LAN).
    try:
        _socket.setdefaulttimeout(3)
        _r = _socket.gethostbyname(host)
        if _is_private(_r):
            return _r
    except Exception:
        pass

    # 4) repli : le nom d'hôte tel quel (jamais d'IP publique).
    return host


def _version_status():
    latest = ''
    try:
        latest = _fetch_text(GITHUB_RAW + '/version.txt')[:40]
    except Exception:
        latest = ''

    def _norm(v):
        return [int(x) for x in re.findall(r'\d+', v or '')[:4]]

    upd = False
    if latest:
        try:
            upd = _norm(latest) > _norm(APP_VERSION)
        except Exception:
            upd = (latest != APP_VERSION)
    return {'version': APP_VERSION, 'latest': latest, 'update_available': bool(upd),
            'truenas_host': SSH_HOST, 'truenas_ip': _truenas_ip()}


def _do_update():
    """Télécharge la dernière version des fichiers dans APP_DIR.
    Le HTML est servi à chaud ; fileops.py nécessite un redémarrage."""
    if not APP_DIR or not os.path.isdir(APP_DIR):
        raise RuntimeError("APP_DIR introuvable — impossible de localiser l'installation.")
    import urllib.request
    updated = []
    for f in ('fileops.py', 'desktroo.html', 'vnc-viewer.html'):
        dst = os.path.join(APP_DIR, f)
        req = urllib.request.Request(GITHUB_RAW + '/' + f, headers={'User-Agent': 'Desktroo'})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        # Écriture SUR PLACE (même inode) : indispensable pour que les bind-mounts
        # de fichier unique (nginx/fileops) voient le nouveau contenu.
        with open(dst, 'wb') as fh:
            fh.write(data)
        updated.append(f)
    return updated


def _schedule_self_restart():
    def _r():
        _sh_time.sleep(3)
        try:
            # Redémarre le bureau (nginx) puis le sidecar. Avec l'écriture sur place
            # ce n'est plus strictement nécessaire pour le HTML, mais garantit la
            # prise en compte du nouveau fileops.py.
            ssh_exec("sudo -n docker restart desktroo desktroo-fileops", timeout=45)
        except Exception:
            pass
    _threading.Thread(target=_r, daemon=True).start()


def _libvirt_path_requires_daemon(path):
    """Return True only for routes that actually call virsh/libvirt."""
    path = str(path or '')
    return (
        path == '/libvirt/test'
        or path == '/libvirt/networks'
        or path.startswith('/libvirt/vms')
        or path == '/libvirt2/networks'
        or path.startswith('/libvirt2/vms')
    )


def ensure_libvirtd():
    """
    Ensure the QEMU/libvirt daemon is available on the TrueNAS host.

    Supports both monolithic libvirtd and modular virtqemud/virtproxyd.
    The health check uses virsh instead of assuming one socket filename.
    """
    global _libvirt_last_ok, _libvirt_cfg_ensured
    import time as _time

    with _libvirt_ensure_lock:
        # Une seule fois par process : garantir que l'intégration AppArmor cassée
        # (fréquente après une MAJ TrueNAS) ne bloque pas le démarrage des VMs.
        if not _libvirt_cfg_ensured:
            _libvirt_cfg_ensured = True
            try:
                _ensure_libvirt_apparmor_off()
            except Exception as _cfg_e:
                log.warning('libvirt apparmor cfg: %s', _cfg_e)

        now = _time.monotonic()
        if now - _libvirt_last_ok < 4:
            return

        probe = (
            "sudo -n virsh -c " + shq(VIRSH_URI)
            + " list --all --name >/dev/null 2>&1"
        )
        _, _, probe_code = ssh_exec(probe, timeout=15)
        if probe_code == 0:
            _libvirt_last_ok = _time.monotonic()
            return

        start_cmd = r"""
set +e

unit_loaded() {
  [ "$(sudo -n systemctl show -p LoadState --value "$1" 2>/dev/null)" = "loaded" ]
}

start_unit() {
  if unit_loaded "$1"; then
    sudo -n systemctl start "$1" >/dev/null 2>&1
  fi
}

if unit_loaded virtqemud.socket || unit_loaded virtqemud.service; then
  for unit in     virtlogd.socket     virtlockd.socket     virtqemud.socket     virtproxyd.socket     virtnetworkd.socket     virtstoraged.socket     virtinterfaced.socket     virtnodedevd.socket     virtnwfilterd.socket     virtsecretd.socket
  do
    start_unit "$unit"
  done

  if ! unit_loaded virtqemud.socket; then
    start_unit virtqemud.service
  fi
else
  # Après une mise à jour de TrueNAS, les sockets libvirt peuvent être MASQUÉS :
  # libvirtd tourne alors en --timeout sans socket -> /run/libvirt/libvirt-sock absent
  # et virsh ne peut pas se connecter. On démasque puis on repart en activation par socket.
  sudo -n systemctl unmask     virtlogd.socket     virtlockd.socket     libvirtd.socket     libvirtd-ro.socket     libvirtd-admin.socket     >/dev/null 2>&1
  sudo -n systemctl daemon-reload >/dev/null 2>&1
  # Socket principal absent alors que libvirtd tourne : arrêter le service pour
  # laisser l'activation par socket le relancer proprement (attaché au socket).
  if [ ! -S /run/libvirt/libvirt-sock ] && [ ! -S /var/run/libvirt/libvirt-sock ]; then
    sudo -n systemctl stop libvirtd.service >/dev/null 2>&1
  fi
  for unit in     virtlogd.socket     virtlockd.socket     libvirtd.socket     libvirtd-ro.socket     libvirtd-admin.socket
  do
    start_unit "$unit"
  done
  start_unit libvirtd.service
fi

for i in $(seq 1 30); do
  if sudo -n virsh -c 'qemu:///system' list --all --name >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done

echo "QEMU/libvirt indisponible après tentative d'auto-récupération." >&2
echo "--- unités libvirt ---" >&2
sudo -n systemctl --no-pager --plain --full status   libvirtd.service libvirtd.socket   virtqemud.service virtqemud.socket virtproxyd.socket   2>&1 | tail -n 60 >&2
echo "--- sockets /run/libvirt ---" >&2
ls -la /run/libvirt /var/run/libvirt 2>&1 | tail -n 60 >&2
exit 1
"""

        out, err, code = ssh_exec(start_cmd, timeout=50)
        if code != 0:
            details = (err or out or "Impossible de démarrer QEMU/libvirt").strip()
            raise RuntimeError(details)

        out, err, code = ssh_exec(probe, timeout=15)
        if code != 0:
            details = (
                err or out or
                "Le daemon libvirt a démarré mais virsh ne répond pas"
            ).strip()
            raise RuntimeError(details)

        _libvirt_last_ok = _time.monotonic()


def _libvirt_watchdog():
    """Continuously recover libvirt after boot or a daemon failure."""
    import time as _time

    delay = 3
    first_success = True

    while True:
        try:
            ensure_libvirtd()
            if first_success:
                log.info(
                    "QEMU/libvirt disponible — surveillance automatique active"
                )
                first_success = False
            delay = 60
        except Exception as error:
            log.warning("Auto-récupération QEMU/libvirt : %s", error)
            delay = min(max(delay * 2, 10), 300)

        _time.sleep(delay)
# MDM-LIBVIRT-AUTORECOVER-V1-END


def virsh(subcmd, timeout=30):
    ensure_libvirtd()
    return ssh_ok("sudo -n virsh -c '" + VIRSH_URI + "' " + subcmd, timeout)


def virsh_list_all():
    out = virsh('list --all')
    vms = []
    for line in out.splitlines():
        m = re.match(r'^\s*(\S+)\s+(\S+)\s+(.+)$', line)
        if m and m.group(1) not in ('Id', '---', '---------'):
            vid = m.group(1)
            vms.append({
                'id':    None if vid == '-' else _try_int(vid),
                'name':  m.group(2),
                'state': m.group(3).strip(),
            })
    return vms


def virsh_dominfo(name):
    out = virsh('dominfo ' + shq(name))
    info = {}
    for line in out.splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            key = re.sub(r'[^a-z0-9_]', '_', k.strip().lower())
            info[key] = v.strip()
    return info


def virsh_domdisplay(name):
    try:
        out, _, code = ssh_exec(
            "sudo -n virsh -c '" + VIRSH_URI + "' domdisplay " + shq(name))
        if code != 0 or not out:
            return None
        # virsh returns vnc://host:DISPLAY (display offset, not real port)
        # real port = 5900 + display_number
        m = re.match(r'(vnc|spice)://([^:]+):(\d+)', out.strip())
        if m:
            port = int(m.group(3))
            if port < 100:          # display number, convert to real port
                port += 5900
            return {'type': m.group(1), 'host': m.group(2), 'port': port}
    except Exception:
        pass
    return None


def virsh_domblklist(name):
    out, _, _ = ssh_exec(
        "sudo -n virsh -c '" + VIRSH_URI + "' domblklist " + shq(name) + ' --details')
    disks = []
    for line in out.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4:
            disks.append({
                'type': parts[0], 'device': parts[1],
                'target': parts[2], 'source': parts[3],
            })
    return disks


def virsh_net_list():
    out, _, _ = ssh_exec("sudo -n virsh -c '" + VIRSH_URI + "' net-list --all")
    nets = []
    for line in out.splitlines()[2:]:
        parts = line.split()
        if parts:
            nets.append({'name': parts[0], 'state': parts[1] if len(parts) > 1 else 'unknown'})
    return nets


def _try_int(s):
    try:
        return int(s)
    except Exception:
        return s


# ── libvirt XML builder ───────────────────────────────────────────────────────
def virsh_parse_devices(name):
    """Parse dumpxml to extract NICs, USB hostdevs, sound, boot order."""
    import xml.etree.ElementTree as ET
    xml_str = ssh_ok("sudo -n virsh -c '" + VIRSH_URI + "' dumpxml --inactive " + shq(name) + " 2>/dev/null || sudo -n virsh -c '" + VIRSH_URI + "' dumpxml " + shq(name))
    try:
        root = ET.fromstring(xml_str)
    except Exception:
        return {}

    devices_el = root.find('devices')
    if devices_el is None:
        return {}

    # NICs
    nics = []
    for iface in devices_el.findall('interface'):
        src_el    = iface.find('source')
        mac_el    = iface.find('mac')
        model_el  = iface.find('model')
        itype     = iface.get('type', 'network')
        source    = ''
        if src_el is not None:
            source = src_el.get('network') or src_el.get('bridge') or src_el.get('dev') or ''
        nics.append({
            'type':    'interface',
            'itype':   itype,
            'mac':     mac_el.get('address', '') if mac_el is not None else '',
            'model':   model_el.get('type', '') if model_el is not None else '',
            'source':  source,
        })

    # USB hostdevs
    usb_devs = []
    for hdev in devices_el.findall('hostdev'):
        if hdev.get('type') != 'usb': continue
        src_el  = hdev.find('source')
        vendor  = ''
        product = ''
        if src_el is not None:
            v_el = src_el.find('vendor')
            p_el = src_el.find('product')
            if v_el is not None: vendor  = v_el.get('id','').replace('0x','')
            if p_el is not None: product = p_el.get('id','').replace('0x','')
        usb_devs.append({'type': 'hostdev', 'vendor': vendor, 'product': product, 'name': vendor+':'+product})

    # Sound
    sounds = []
    for snd in devices_el.findall('sound'):
        sounds.append({'type': 'sound', 'model': snd.get('model','')})

    # Video
    video_model = 'vga'
    vid_el = devices_el.find('video')
    if vid_el is not None:
        m_el = vid_el.find('model')
        if m_el is not None:
            video_model = m_el.get('type', 'vga')

    # Boot order
    # Prefer libvirt per-device boot order:
    #   <disk device="cdrom"><boot order="1"/></disk>
    #   <disk device="disk"><boot order="2"/></disk>
    # Fallback to legacy:
    #   <os><boot dev="cdrom"/><boot dev="hd"/></os>
    boot_order = []
    boot_items = []

    for d in devices_el.findall('disk'):
        b = d.find('boot')
        if b is None or not b.get('order'):
            continue

        dev_type = d.get('device', '')
        if dev_type == 'disk':
            boot_dev = 'hd'
        elif dev_type == 'cdrom':
            boot_dev = 'cdrom'
        elif dev_type == 'floppy':
            boot_dev = 'fd'
        else:
            continue

        try:
            boot_rank = int(b.get('order'))
        except Exception:
            boot_rank = 999

        boot_items.append((boot_rank, boot_dev))

    for iface in devices_el.findall('interface'):
        b = iface.find('boot')
        if b is None or not b.get('order'):
            continue
        try:
            boot_rank = int(b.get('order'))
        except Exception:
            boot_rank = 999
        boot_items.append((boot_rank, 'network'))

    if boot_items:
        seen_boot = set()
        for _, boot_dev in sorted(boot_items, key=lambda x: x[0]):
            if boot_dev not in seen_boot:
                boot_order.append(boot_dev)
                seen_boot.add(boot_dev)
    else:
        os_el = root.find('os')
        if os_el is not None:
            for b in os_el.findall('boot'):
                dev = b.get('dev','')
                if dev and dev not in boot_order:
                    boot_order.append(dev)

    # Memory / vCPUs
    mem_el  = root.find('memory')
    vcpu_el = root.find('vcpu')
    memory_mb = int(mem_el.text or 0) // 1024 if mem_el is not None else 0
    vcpus     = int(vcpu_el.text or 1)          if vcpu_el is not None else 1

    return {
        'devices':    nics + usb_devs + sounds,
        'video_model': video_model,
        'boot_order': boot_order,
        'memory_mb':  memory_mb,
        'vcpus':      vcpus,
    }


# ── Batch-parse helpers (work on already-fetched text) ───────────────────────
def _parse_dominfo(text):
    info = {}
    for line in text.splitlines():
        if ':' in line:
            k, _, v = line.partition(':')
            key = re.sub(r'[^a-z0-9_]', '_', k.strip().lower())
            info[key] = v.strip()
    return info

def _parse_domdisplay(text):
    text = text.strip()
    if not text:
        return None
    m = re.match(r'(vnc|spice)://([^:]+):(-?\d+)', text)
    if m:
        port = int(m.group(3))
        if port < 100:
            port += 5900
        return {'type': m.group(1), 'host': m.group(2), 'port': port}
    return None

def _parse_domblklist(text):
    disks = []
    for line in text.splitlines()[2:]:
        parts = line.split()
        if len(parts) >= 4:
            disks.append({
                'type': parts[0], 'device': parts[1],
                'target': parts[2], 'source': parts[3],
            })
    return disks

def _parse_dumpxml(xml_str):
    """Same as virsh_parse_devices but takes XML string directly."""
    import xml.etree.ElementTree as ET
    if not xml_str or not xml_str.strip():
        return {}
    try:
        root = ET.fromstring(xml_str.strip())
    except Exception:
        return {}
    devices_el = root.find('devices')
    if devices_el is None:
        return {}
    nics = []
    for iface in devices_el.findall('interface'):
        src_el   = iface.find('source')
        mac_el   = iface.find('mac')
        model_el = iface.find('model')
        itype    = iface.get('type', 'network')
        source   = ''
        if src_el is not None:
            source = src_el.get('network') or src_el.get('bridge') or src_el.get('dev') or ''
        nics.append({'type': 'interface', 'itype': itype,
                     'mac':   mac_el.get('address', '')  if mac_el   is not None else '',
                     'model': model_el.get('type', '')   if model_el is not None else '',
                     'source': source})
    usb_devs = []
    for hdev in devices_el.findall('hostdev'):
        if hdev.get('type') != 'usb': continue
        src_el = hdev.find('source')
        vendor = product = ''
        if src_el is not None:
            v_el = src_el.find('vendor');  p_el = src_el.find('product')
            if v_el is not None: vendor  = v_el.get('id','').replace('0x','')
            if p_el is not None: product = p_el.get('id','').replace('0x','')
        usb_devs.append({'type': 'hostdev', 'vendor': vendor, 'product': product,
                         'name': vendor + ':' + product})
    sounds = []
    for snd in devices_el.findall('sound'):
        sounds.append({'type': 'sound', 'model': snd.get('model','')})
    video_model = 'vga'
    vid_el = devices_el.find('video')
    if vid_el is not None:
        m_el = vid_el.find('model')
        if m_el is not None:
            video_model = m_el.get('type', 'vga')
    boot_order = []
    os_el = root.find('os')
    if os_el is not None:
        for b in os_el.findall('boot'):
            boot_order.append(b.get('dev',''))
    mem_el   = root.find('memory')
    vcpu_el  = root.find('vcpu')
    mem_mb   = int(mem_el.text or 0) // 1024 if mem_el  is not None else 0
    vcpus    = int(vcpu_el.text or 1)         if vcpu_el is not None else 1
    return {'devices': nics + usb_devs + sounds,
            'video_model': video_model,
            'boot_order': boot_order, 'memory_mb': mem_mb, 'vcpus': vcpus}


def build_vm_xml(name, memory_mb, vcpus, disk_path, iso_path, uefi, secure_boot, tpm, network, disk_bus='sata', net_type='nat', bridge_iface='br0', net_model='e1000', boot_disk_first=False):
    nvram_path = disk_path.replace('.qcow2', '_VARS.fd')

    if secure_boot:
        code_fd       = '/usr/share/OVMF/OVMF_CODE_4M.ms.fd'
        vars_tpl      = '/usr/share/OVMF/OVMF_VARS_4M.ms.fd'
        smm_tag       = '<smm state="on"/>'
        loader_secure = ' secure="yes"'
    elif uefi:
        code_fd       = '/usr/share/OVMF/OVMF_CODE_4M.fd'
        vars_tpl      = '/usr/share/OVMF/OVMF_VARS_4M.fd'
        smm_tag       = ''
        loader_secure = ''
    else:
        code_fd = vars_tpl = nvram_path = None
        smm_tag = loader_secure = ''

    os_extra = ''
    if uefi or secure_boot:
        os_extra = (
            '<loader readonly="yes"' + loader_secure + ' type="pflash">' + code_fd + '</loader>'
            '<nvram template="' + vars_tpl + '">' + nvram_path + '</nvram>'
        )

    # Always include a CDROM slot so ISO can be swapped later with virsh change-media
    if iso_path:
        cdrom_tag = (
            '<disk type="file" device="cdrom">'
            '<driver name="qemu"/>'
            '<source file="' + iso_path + '"/>'
            '<target dev="sdb" bus="sata"/>'
            '<readonly/>'
            '</disk>'
        )
    else:
        cdrom_tag = (
            '<disk type="file" device="cdrom">'
            '<driver name="qemu"/>'
            '<target dev="sdb" bus="sata"/>'
            '<readonly/>'
            '</disk>'
        )

    tpm_tag = ''
    if tpm:
        tpm_tag = (
            '<tpm model="tpm-crb">'
            '<backend type="emulator" version="2.0"/>'
            '</tpm>'
        )

    boot_tags = ('<boot dev="hd"/><boot dev="cdrom"/>'
                 if boot_disk_first else
                 '<boot dev="cdrom"/><boot dev="hd"/>')

    # Interface disque : cible + machine + contrôleur selon le bus choisi.
    # IDE n'existe pas sur q35 -> on bascule la machine sur i440fx (pc) dans ce cas.
    machine = 'pc' if disk_bus == 'ide' else 'q35'
    _dev_map = {'virtio': 'vda', 'sata': 'sda', 'scsi': 'sda', 'ide': 'hda', 'usb': 'sda'}
    disk_target = _dev_map.get(disk_bus, 'sda')
    scsi_ctrl = '<controller type="scsi" model="virtio-scsi" index="0"/>' if disk_bus == 'scsi' else ''

    return (
        '<domain type="kvm">'
        '<name>' + name + '</name>'
        '<memory unit="MiB">' + str(memory_mb) + '</memory>'
        '<currentMemory unit="MiB">' + str(memory_mb) + '</currentMemory>'
        '<vcpu placement="static">' + str(vcpus) + '</vcpu>'
        '<os>'
        '<type arch="x86_64" machine="' + machine + '">hvm</type>'
        + os_extra +
        boot_tags +
        '</os>'
        '<features>'
        '<acpi/><apic/>' + smm_tag +
        '</features>'
        '<cpu mode="host-passthrough" check="none" migratable="on"/>'
        '<clock offset="localtime">'
        '<timer name="rtc" tickpolicy="catchup"/>'
        '<timer name="pit" tickpolicy="delay"/>'
        '<timer name="hpet" present="no"/>'
        '<timer name="hypervclock" present="yes"/>'
        '</clock>'
        '<devices>'
        '<disk type="file" device="disk">'
        '<driver name="qemu" type="qcow2" discard="unmap"/>'
        '<source file="' + disk_path + '"/>'
        '<target dev="' + disk_target + '" bus="' + disk_bus + '"/>'
        '</disk>'
        + cdrom_tag +
        '<controller type="sata" index="0"/>'
        + scsi_ctrl +
        ('<interface type="bridge">'
           + '<source bridge="' + (bridge_iface or 'br0') + '"/>'
           + '<model type="' + net_model + '"/>'
           + '</interface>'
           if net_type == 'bridge' else
           '<interface type="network">'
           + '<source network="' + (network or 'default') + '"/>'
           + '<model type="' + net_model + '"/>'
           + '</interface>')
        +
        '<graphics type="vnc" port="-1" autoport="yes">'
        '<listen type="address" address="0.0.0.0"/>'
        '</graphics>'
        '<video><model type="qxl" ram="65536" vram="65536" vgamem="16384" heads="1" primary="yes"/></video>'
        '<memballoon model="virtio"/>'
        + tpm_tag +
        '</devices>'
        '</domain>'
    )


# ── websockets path compat ────────────────────────────────────────────────────
def _get_ws_path(ws):
    # websockets < 10
    for attr in ('path', 'request_uri', '_path'):
        v = getattr(ws, attr, None)
        if v and isinstance(v, str):
            return v
    # websockets >= 10: ws.request
    try:
        req = ws.request
        # ws.request.path  (includes query string in most versions)
        p = getattr(req, 'path', None)
        if p and isinstance(p, str):
            return p
        # ws.request.url.path + query
        url = getattr(req, 'url', None)
        if url:
            path = str(getattr(url, 'path', '') or '/')
            query = getattr(url, 'query', '') or getattr(url, 'query_string', '') or ''
            if isinstance(query, bytes):
                query = query.decode('utf-8', errors='replace')
            return path + ('?' + str(query) if query else '')
        # websockets 14+: ws.request.headers 'path' key
        hdrs = getattr(req, 'headers', None)
        if hdrs:
            raw_line = hdrs.get(':path', '') or hdrs.get('path', '')
            if raw_line:
                return raw_line
    except Exception:
        pass
    return '/'


# ── PTY helpers ───────────────────────────────────────────────────────────────
def set_winsize(fd, rows, cols):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    except Exception:
        pass


_active_session = None


# ── WebSocket: terminal PTY ───────────────────────────────────────────────────
async def terminal_session(ws):
    global _active_session

    path   = _get_ws_path(ws)
    params = dict(parse_qsl(urlparse(path).query))
    if params.get('token', '') != TOKEN:
        log.warning('Terminal: rejected (bad token)')
        try:
            await ws.close(1008, 'Unauthorized')
        except Exception:
            pass
        return

    prev = _active_session
    if prev and not prev.done():
        prev.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(prev), timeout=3.0)
        except Exception:
            pass

    _active_session = asyncio.current_task()

    master_fd, slave_fd = pty.openpty()
    set_winsize(master_fd, 24, 80)
    env         = os.environ.copy()
    env['TERM'] = 'xterm-256color'
    env['PS1']  = r'\u@\h:\w\$ '

    proc = subprocess.Popen(
        ['/bin/sh', '-i'],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        close_fds=True, env=env,
    )
    os.close(slave_fd)

    loop = asyncio.get_event_loop()

    async def pty_to_ws():
        queue = asyncio.Queue()
        def _on_readable():
            try:
                data = os.read(master_fd, 4096)
                if data:
                    queue.put_nowait(data)
            except OSError:
                try:
                    loop.remove_reader(master_fd)
                except Exception:
                    pass
                queue.put_nowait(None)
        loop.add_reader(master_fd, _on_readable)
        try:
            while True:
                data = await queue.get()
                if data is None:
                    break
                try:
                    await ws.send(data)
                except Exception:
                    break
        finally:
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass

    async def ws_to_pty():
        async for msg in ws:
            raw = msg.encode('utf-8', errors='replace') if isinstance(msg, str) else bytes(msg)
            try:
                j = json.loads(raw)
                if 'cols' in j:
                    set_winsize(master_fd, int(j.get('rows', 24)), int(j['cols']))
                    continue
            except Exception:
                pass
            try:
                os.write(master_fd, raw)
            except OSError:
                break

    t1 = asyncio.ensure_future(pty_to_ws())
    t2 = asyncio.ensure_future(ws_to_pty())
    try:
        done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        t1.cancel()
        t2.cancel()
        await asyncio.gather(t1, t2, return_exceptions=True)
        raise
    finally:
        try:
            loop.remove_reader(master_fd)
        except Exception:
            pass
        try:
            os.close(master_fd)
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ── WebSocket: VNC proxy via SSH tunnel ───────────────────────────────────────
async def vnc_proxy_session(ws):
    path   = _get_ws_path(ws)
    params = dict(parse_qsl(urlparse(path).query))
    if params.get('token', '') != TOKEN:
        try:
            await ws.close(1008, 'Unauthorized')
        except Exception:
            pass
        return

    vnc_port = int(params.get('port', '5900'))
    loop = asyncio.get_event_loop()

    def _connect_ssh():
        transport = paramiko.Transport((SSH_HOST, SSH_PORT_N))
        transport.connect(username=SSH_USER, password=SSH_PASS)
        channel = transport.open_channel(
            'direct-tcpip', ('127.0.0.1', vnc_port), ('127.0.0.1', 0)
        )
        return transport, channel

    try:
        transport, channel = await loop.run_in_executor(None, _connect_ssh)
    except Exception as e:
        log.warning('VNC proxy connect failed: %s', e)
        try:
            await ws.close(1011, str(e))
        except Exception:
            pass
        return

    log.info('VNC proxy: tunnel open (port=%d)', vnc_port)

    async def vnc_to_ws():
        q = asyncio.Queue()
        def _readable():
            try:
                data = channel.recv(16384)
                q.put_nowait(data if data else None)
            except Exception:
                q.put_nowait(None)
        try:
            loop.add_reader(channel.fileno(), _readable)
        except Exception:
            pass
        try:
            while True:
                data = await q.get()
                if not data:
                    break
                try:
                    await ws.send(data)
                except Exception:
                    break
        finally:
            try:
                loop.remove_reader(channel.fileno())
            except Exception:
                pass

    async def ws_to_vnc():
        try:
            async for msg in ws:
                data = msg if isinstance(msg, bytes) else msg.encode()
                await loop.run_in_executor(None, channel.sendall, data)
        except Exception:
            pass

    t1 = asyncio.ensure_future(vnc_to_ws())
    t2 = asyncio.ensure_future(ws_to_vnc())
    try:
        done, pending = await asyncio.wait([t1, t2], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        try:
            channel.close()
        except Exception:
            pass
        try:
            transport.close()
        except Exception:
            pass


# ── WebSocket router ──────────────────────────────────────────────────────────
async def ws_handler(ws):
    raw  = _get_ws_path(ws)
    path = urlparse(raw).path
    log.info('WS connect: raw=%r parsed_path=%r', raw, path)
    if path == '/vnc-proxy' and _HAS_PARAMIKO:
        await vnc_proxy_session(ws)
    else:
        await terminal_session(ws)


# ── HTTP handler ──────────────────────────────────────────────────────────────
# MDM-LIBVIRT2-20260710
# Modèle VM/QEMU/libvirt V2.
# Fournit un modèle JSON cohérent aux opérations V2.
def _lv2_split_sections(text):
    sections = {}
    cur = None
    for ln in (text or '').splitlines():
        if ln.startswith('===') and ln.endswith('==='):
            cur = ln.strip('=')
            sections[cur] = []
        elif cur:
            sections[cur].append(ln)
    return sections


def _lv2_parse_key_values(text):
    data = {}
    for ln in (text or '').splitlines():
        if ':' not in ln:
            continue
        k, v = ln.split(':', 1)
        key = k.strip().lower().replace(' ', '_')
        data[key] = v.strip()
    return data


def _lv2_parse_domblklist(text):
    rows = []
    for ln in (text or '').splitlines():
        line = ln.strip()
        if not line or line.startswith('-') or line.lower().startswith('type '):
            continue
        parts = line.split(None, 3)
        if len(parts) >= 4:
            rows.append({
                'type': parts[0],
                'device': parts[1],
                'target': parts[2],
                'source': parts[3],
            })
    return rows


def _lv2_parse_domiflist(text):
    rows = []
    for ln in (text or '').splitlines():
        line = ln.strip()
        if not line or line.startswith('-') or line.lower().startswith('interface '):
            continue
        parts = line.split()
        if len(parts) >= 5:
            rows.append({
                'interface': parts[0],
                'type': parts[1],
                'source': parts[2],
                'model': parts[3],
                'mac': parts[4],
            })
    return rows


def _lv2_xml_attr(el, path, attr='', default=''):
    node = el.find(path) if el is not None else None
    if node is None:
        return default
    if attr:
        return node.get(attr, default)
    return (node.text or default).strip()


def _lv2_parse_xml(xml_str, blklist=None, iflist=None):
    import xml.etree.ElementTree as ET

    blklist = blklist or []
    iflist = iflist or []

    model = {
        'resources': {'memory_mb': 0, 'current_memory_mb': 0, 'vcpus': 1},
        'firmware': {'type': 'bios', 'loader': '', 'secure_boot': False, 'nvram': ''},
        'machine': {'arch': '', 'machine': '', 'type': ''},
        'boot_order': [],
        'boot_mode': 'unknown',
        'boot_conflict': False,
        'disks': [],
        'cdroms': [],
        'nics': [],
        'display': {},
        'video': {},
        'sound': [],
        'usb': [],
        'controllers': [],
        'raw': {'has_xml': False},
    }

    if not xml_str or not xml_str.strip():
        return model

    try:
        root = ET.fromstring(xml_str.strip())
    except Exception as e:
        model['raw']['xml_error'] = str(e)
        return model

    model['raw']['has_xml'] = True

    mem = root.find('memory')
    curmem = root.find('currentMemory')
    vcpu = root.find('vcpu')

    def mem_to_mb(node):
        if node is None or not (node.text or '').strip():
            return 0
        try:
            val = int((node.text or '0').strip())
        except Exception:
            return 0
        unit = (node.get('unit') or 'KiB').lower()
        if unit in ('kib', 'kb'):
            return val // 1024
        if unit in ('mib', 'mb'):
            return val
        if unit in ('gib', 'gb'):
            return val * 1024
        return val // 1024

    model['resources']['memory_mb'] = mem_to_mb(mem)
    model['resources']['current_memory_mb'] = mem_to_mb(curmem) or model['resources']['memory_mb']
    try:
        model['resources']['vcpus'] = int((vcpu.text or '1').strip()) if vcpu is not None else 1
    except Exception:
        model['resources']['vcpus'] = 1

    os_el = root.find('os')
    if os_el is not None:
        type_el = os_el.find('type')
        if type_el is not None:
            model['machine']['arch'] = type_el.get('arch', '')
            model['machine']['machine'] = type_el.get('machine', '')
            model['machine']['type'] = (type_el.text or '').strip()

        loader = os_el.find('loader')
        if loader is not None:
            model['firmware']['type'] = 'uefi'
            model['firmware']['loader'] = (loader.text or '').strip()
            model['firmware']['secure_boot'] = (
                loader.get('secure') in ('yes', 'on', 'true', '1')
                or 'secboot' in (loader.text or '').lower()
            )

        nvram = os_el.find('nvram')
        if nvram is not None:
            model['firmware']['nvram'] = (nvram.text or '').strip()

    devices = root.find('devices')
    if devices is None:
        return model

    blk_by_target = {d.get('target', ''): d for d in blklist}
    boot_items = []
    legacy_boot = []

    if os_el is not None:
        for b in os_el.findall('boot'):
            dev = b.get('dev', '')
            if dev:
                legacy_boot.append(dev)

    for disk in devices.findall('disk'):
        dev_type = disk.get('device', '')
        disk_type = disk.get('type', '')

        target_el = disk.find('target')
        driver_el = disk.find('driver')
        source_el = disk.find('source')
        boot_el = disk.find('boot')

        target = target_el.get('dev', '') if target_el is not None else ''
        bus = target_el.get('bus', '') if target_el is not None else ''

        source = ''
        if source_el is not None:
            source = (
                source_el.get('file')
                or source_el.get('dev')
                or source_el.get('name')
                or source_el.get('protocol')
                or ''
            )

        if not source and target in blk_by_target:
            source = blk_by_target[target].get('source', '')

        item = {
            'device': dev_type,
            'type': disk_type,
            'target': target,
            'bus': bus,
            'source': source,
            'format': driver_el.get('type', '') if driver_el is not None else '',
            'driver': driver_el.get('name', '') if driver_el is not None else '',
            'readonly': disk.find('readonly') is not None,
            'boot_order': None,
            'empty': (not source or source == '-'),
        }

        if boot_el is not None and boot_el.get('order'):
            try:
                item['boot_order'] = int(boot_el.get('order'))
            except Exception:
                item['boot_order'] = 999

            if dev_type == 'disk':
                boot_items.append((item['boot_order'], 'hd'))
            elif dev_type == 'cdrom':
                boot_items.append((item['boot_order'], 'cdrom'))
            elif dev_type == 'floppy':
                boot_items.append((item['boot_order'], 'fd'))

        if dev_type == 'cdrom':
            model['cdroms'].append(item)
        else:
            model['disks'].append(item)

    for iface in devices.findall('interface'):
        mac_el = iface.find('mac')
        src_el = iface.find('source')
        model_el = iface.find('model')
        target_el = iface.find('target')
        boot_el = iface.find('boot')

        source = ''
        if src_el is not None:
            source = src_el.get('network') or src_el.get('bridge') or src_el.get('dev') or ''

        item = {
            'type': iface.get('type', ''),
            'mac': mac_el.get('address', '') if mac_el is not None else '',
            'source': source,
            'model': model_el.get('type', '') if model_el is not None else '',
            'target': target_el.get('dev', '') if target_el is not None else '',
            'boot_order': None,
        }

        if boot_el is not None and boot_el.get('order'):
            try:
                item['boot_order'] = int(boot_el.get('order'))
            except Exception:
                item['boot_order'] = 999
            boot_items.append((item['boot_order'], 'network'))

        model['nics'].append(item)

    graphics = devices.find('graphics')
    if graphics is not None:
        model['display'] = {
            'type': graphics.get('type', ''),
            'port': graphics.get('port', ''),
            'websocket': graphics.get('websocket', ''),
            'autoport': graphics.get('autoport', ''),
            'listen': graphics.get('listen', ''),
        }

    video = devices.find('video')
    if video is not None:
        vmodel = video.find('model')
        model['video'] = {
            'model': vmodel.get('type', '') if vmodel is not None else '',
            'vram': vmodel.get('vram', '') if vmodel is not None else '',
            'heads': vmodel.get('heads', '') if vmodel is not None else '',
            'primary': vmodel.get('primary', '') if vmodel is not None else '',
        }

    for snd in devices.findall('sound'):
        model['sound'].append({'model': snd.get('model', '')})

    for hdev in devices.findall('hostdev'):
        if hdev.get('type') == 'usb':
            src = hdev.find('source')
            vendor = src.find('vendor').get('id', '') if src is not None and src.find('vendor') is not None else ''
            product = src.find('product').get('id', '') if src is not None and src.find('product') is not None else ''
            model['usb'].append({'vendor': vendor, 'product': product})

    for ctrl in devices.findall('controller'):
        model['controllers'].append({
            'type': ctrl.get('type', ''),
            'index': ctrl.get('index', ''),
            'model': ctrl.get('model', ''),
        })

    if boot_items:
        model['boot_mode'] = 'per-device'
        seen = set()
        for _, dev in sorted(boot_items, key=lambda x: x[0]):
            if dev not in seen:
                model['boot_order'].append(dev)
                seen.add(dev)
    elif legacy_boot:
        model['boot_mode'] = 'legacy-os'
        seen = set()
        for dev in legacy_boot:
            if dev not in seen:
                model['boot_order'].append(dev)
                seen.add(dev)
    else:
        model['boot_mode'] = 'none'

    # Sécurité affichage/API : l'ordre de boot ne doit jamais contenir de doublons,
    # même si un ancien XML libvirt contient plusieurs entrées héritées.
    deduped = []
    seen = set()
    for dev in model.get('boot_order', []):
        if dev not in seen:
            deduped.append(dev)
            seen.add(dev)
    model['boot_order'] = deduped

    model['boot_conflict'] = bool(boot_items and legacy_boot)

    return model


def _lv2_vm_names():
    # Auto-réimport : après une mise à jour de TrueNAS qui a pu vider
    # /etc/libvirt/qemu, on redéfinit dans libvirt toute VM dont le XML existe
    # encore dans VM_DIR (créé par cet outil) mais n'est plus connue de libvirt.
    u = "'" + VIRSH_URI + "'"
    vmdir = shq(VM_DIR.rstrip('/'))
    # Noms des VMs qui doivent redémarrer au boot (espaces exclus par la validation).
    auto_names = ' '.join(str(n) for n, v in _vm_autostart_read().items() if v)
    script = (
        'AUTO=' + shq(auto_names) + '; '
        'for f in ' + vmdir + '/*.xml; do '
        '[ -e "$f" ] || continue; '
        'n=$(basename "$f" .xml); '
        'if ! sudo -n virsh -c ' + u + ' dominfo "$n" >/dev/null 2>&1; then '
        'sudo -n virsh -c ' + u + ' define "$f" >/dev/null 2>&1 || true; '
        'for a in $AUTO; do [ "$a" = "$n" ] && sudo -n virsh -c ' + u + ' autostart "$n" >/dev/null 2>&1; done; '
        'fi; '
        'done; '
        'sudo -n virsh -c ' + u + ' list --all --name'
    )
    out, _err, _code = ssh_exec(script, timeout=60)
    return [x.strip() for x in out.splitlines() if x.strip()]


def _lv2_vm_snapshot_names(name):
    out, err, code = ssh_exec(
        "sudo -n virsh -c '" + VIRSH_URI + "' snapshot-list " + shq(name) + " --name 2>/dev/null || true",
        timeout=20
    )
    return [x.strip() for x in out.splitlines() if x.strip()]


def _lv2_get_vm(name, include_xml=False):
    cmd = (
        "echo '===DOMSTATE===' && (sudo -n virsh -c '" + VIRSH_URI + "' domstate " + shq(name) + " 2>&1 || true)"
        + " && echo '===DOMINFO===' && (sudo -n virsh -c '" + VIRSH_URI + "' dominfo " + shq(name) + " 2>&1 || true)"
        + " && echo '===DISPLAY===' && (sudo -n virsh -c '" + VIRSH_URI + "' domdisplay " + shq(name) + " 2>/dev/null || true)"
        + " && echo '===BLKLIST===' && (sudo -n virsh -c '" + VIRSH_URI + "' domblklist --details " + shq(name) + " 2>&1 || true)"
        + " && echo '===IFLIST===' && (sudo -n virsh -c '" + VIRSH_URI + "' domiflist " + shq(name) + " 2>&1 || true)"
        + " && echo '===XML===' && (sudo -n virsh -c '" + VIRSH_URI + "' dumpxml --inactive " + shq(name)
        + " 2>/dev/null || sudo -n virsh -c '" + VIRSH_URI + "' dumpxml " + shq(name) + " 2>&1 || true)"
    )

    batch = ssh_ok(cmd, timeout=45)
    sec = _lv2_split_sections(batch)

    state = '\n'.join(sec.get('DOMSTATE', [])).strip()
    info = _lv2_parse_key_values('\n'.join(sec.get('DOMINFO', [])))
    display_raw = '\n'.join(sec.get('DISPLAY', [])).strip()
    blklist = _lv2_parse_domblklist('\n'.join(sec.get('BLKLIST', [])))
    iflist = _lv2_parse_domiflist('\n'.join(sec.get('IFLIST', [])))
    xml = '\n'.join(sec.get('XML', []))

    parsed = _lv2_parse_xml(xml, blklist=blklist, iflist=iflist)

    vm = {
        'api': 'libvirt2',
        'version': 2,
        'readonly': False,
        'name': name,
        'state': state,
        'uuid': info.get('uuid', ''),
        'id': info.get('id', ''),
        'autostart': info.get('autostart', ''),
        'persistent': info.get('persistent', ''),
        'managed_save': info.get('managed_save', ''),
        'resources': parsed['resources'],
        'firmware': parsed['firmware'],
        'machine': parsed['machine'],
        'boot_order': parsed['boot_order'],
        'boot_mode': parsed['boot_mode'],
        'boot_conflict': parsed['boot_conflict'],
        'disks': parsed['disks'],
        'cdroms': parsed['cdroms'],
        'nics': parsed['nics'],
        'display': parsed['display'],
        'display_uri': display_raw,
        'video': parsed['video'],
        'sound': parsed['sound'],
        'usb': parsed['usb'],
        'controllers': parsed['controllers'],
        'snapshots': _lv2_vm_snapshot_names(name),
        'raw_sources': {
            'domblklist': blklist,
            'domiflist': iflist,
        }
    }

    if include_xml:
        vm['xml'] = xml

    return vm


# MDM-LIBVIRT2-INSTALLATION-20260710
# Actions V2 propres pour installation VM : ISO + boot.
# Fonctions dédiées aux handlers /libvirt2/vms/{name}/iso.
def _lv2_list_isos():
    cmd = (
        "find " + shq(ISO_DIR)
        + " -maxdepth 4 -type f \\( -iname '*.iso' -o -iname '*.img' \\) 2>/dev/null | sort"
    )
    out, err, code = ssh_exec(cmd, timeout=30)
    if code != 0:
        return []
    items = []
    for path in out.splitlines():
        path = path.strip()
        if not path:
            continue
        items.append({
            'path': path,
            'name': os.path.basename(path),
            'dir': os.path.dirname(path),
        })
    return items


def _lv2_state(name):
    out, err, code = ssh_exec(
        "sudo -n virsh -c '" + VIRSH_URI + "' domstate " + shq(name),
        timeout=15
    )
    return (out or err or '').strip().lower()


def _lv2_remote_file_exists(path):
    out, err, code = ssh_exec("[ -f " + shq(path) + " ]", timeout=10)
    return code == 0


def _lv2_dumpxml(name):
    return ssh_ok(
        "sudo -n virsh -c '" + VIRSH_URI + "' dumpxml --inactive " + shq(name)
        + " 2>/dev/null || sudo -n virsh -c '" + VIRSH_URI + "' dumpxml " + shq(name),
        timeout=35
    )


def _lv2_write_remote_text(path, text):
    import base64
    payload = base64.b64encode(text.encode('utf-8')).decode('ascii')
    ssh_ok(
        "printf %s " + shq(payload) + " | base64 -d > " + shq(path),
        timeout=25
    )


def _lv2_define_xml(name, xml_text):
    import re
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', name)
    tmp = "/tmp/libvirt2-" + safe + ".xml"
    _lv2_write_remote_text(tmp, xml_text)
    ssh_ok(
        "sudo -n virsh -c '" + VIRSH_URI + "' define " + shq(tmp),
        timeout=35
    )
    return tmp


def _lv2_xml_patch(name, patch_fn):
    xml = _lv2_dumpxml(name)
    new_xml = patch_fn(xml)
    tmp = _lv2_define_xml(name, new_xml)
    return tmp


def _lv2_xml_to_text(root):
    import xml.etree.ElementTree as ET
    return ET.tostring(root, encoding='unicode')


def _lv2_insert_before_alias_or_address(node, child):
    children = list(node)
    for idx, existing in enumerate(children):
        if existing.tag in ('alias', 'address'):
            node.insert(idx, child)
            return
    node.append(child)


def _lv2_set_iso(name, iso_path):
    import xml.etree.ElementTree as ET

    iso_path = str(iso_path or '').strip()

    if iso_path and not _lv2_remote_file_exists(iso_path):
        raise RuntimeError("ISO introuvable : " + iso_path)

    result = {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'mount_iso' if iso_path else 'eject_iso',
        'iso_path': iso_path,
        'config_updated': False,
        'live_updated': False,
        'warning': '',
    }

    target_ref = {'target': 'sdb'}

    def patch(xml):
        root = ET.fromstring(xml)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        cdrom = None
        for disk in devices.findall('disk'):
            if disk.get('device') == 'cdrom':
                cdrom = disk
                break

        if cdrom is None:
            cdrom = ET.Element('disk', {'type': 'file', 'device': 'cdrom'})
            cdrom.append(ET.Element('driver', {'name': 'qemu', 'type': 'raw'}))
            cdrom.append(ET.Element('target', {'dev': 'sdb', 'bus': 'sata'}))
            cdrom.append(ET.Element('readonly'))
            devices.append(cdrom)

        if not cdrom.get('type'):
            cdrom.set('type', 'file')
        cdrom.set('device', 'cdrom')

        driver = cdrom.find('driver')
        if driver is None:
            cdrom.insert(0, ET.Element('driver', {'name': 'qemu', 'type': 'raw'}))
        else:
            if not driver.get('name'):
                driver.set('name', 'qemu')
            if not driver.get('type'):
                driver.set('type', 'raw')

        target = cdrom.find('target')
        if target is None:
            target = ET.Element('target', {'dev': 'sdb', 'bus': 'sata'})
            cdrom.append(target)
        if not target.get('dev'):
            target.set('dev', 'sdb')
        if not target.get('bus'):
            target.set('bus', 'sata')

        target_ref['target'] = target.get('dev') or 'sdb'

        for source in list(cdrom.findall('source')):
            cdrom.remove(source)

        if iso_path:
            source = ET.Element('source', {'file': iso_path})
            children = list(cdrom)
            inserted = False
            for idx, child in enumerate(children):
                if child.tag == 'target':
                    cdrom.insert(idx, source)
                    inserted = True
                    break
            if not inserted:
                cdrom.append(source)

        if cdrom.find('readonly') is None:
            cdrom.append(ET.Element('readonly'))

        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)
    result['config_updated'] = True

    state = _lv2_state(name)
    target = target_ref.get('target') or 'sdb'

    if 'running' in state:
        v = "sudo -n virsh -c '" + VIRSH_URI + "'"
        if iso_path:
            cmds = [
                v + " change-media " + shq(name) + " " + shq(target) + " " + shq(iso_path) + " --insert --live",
                v + " change-media " + shq(name) + " " + shq(target) + " " + shq(iso_path) + " --update --live",
            ]
        else:
            cmds = [
                v + " change-media " + shq(name) + " " + shq(target) + " --eject --live",
                v + " change-media " + shq(name) + " " + shq(target) + " --eject",
            ]

        last_error = ''
        for cmd in cmds:
            out, err, code = ssh_exec(cmd, timeout=30)
            if code == 0:
                result['live_updated'] = True
                break
            last_error = (err or out or ('exit ' + str(code))).strip()

        if not result['live_updated']:
            result['warning'] = (
                "Configuration persistante modifiée, mais la VM est active et "
                "l'application live du CD-ROM a échoué. Redémarrer la VM si l'ISO "
                "n'est pas visible dans l'OS invité. Détail : " + last_error
            )

    return result


def _lv2_set_boot(name, order):
    import xml.etree.ElementTree as ET

    allowed = ('cdrom', 'hd', 'network', 'fd')
    clean = []
    for item in order or []:
        item = str(item).strip().lower()
        if item in allowed and item not in clean:
            clean.append(item)

    if not clean:
        raise RuntimeError('Ordre de boot vide')

    applied_ref = {'applied': []}

    def patch(xml):
        root = ET.fromstring(xml)

        os_el = root.find('os')
        if os_el is not None:
            for boot in list(os_el.findall('boot')):
                os_el.remove(boot)

        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        for node in devices.iter():
            for boot in list(node.findall('boot')):
                node.remove(boot)

        targets = {'cdrom': None, 'hd': None, 'network': None, 'fd': None}

        for disk in devices.findall('disk'):
            dev = disk.get('device', '')
            if dev == 'cdrom' and targets['cdrom'] is None:
                targets['cdrom'] = disk
            elif dev == 'disk' and targets['hd'] is None:
                targets['hd'] = disk
            elif dev == 'floppy' and targets['fd'] is None:
                targets['fd'] = disk

        for iface in devices.findall('interface'):
            if targets['network'] is None:
                targets['network'] = iface

        applied = []
        for dev in clean:
            node = targets.get(dev)
            if node is None:
                continue
            boot = ET.Element('boot', {'order': str(len(applied) + 1)})
            _lv2_insert_before_alias_or_address(node, boot)
            applied.append(dev)

        if not applied:
            raise RuntimeError('Aucun périphérique de boot correspondant trouvé')

        applied_ref['applied'] = applied
        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'set_boot',
        'order': applied_ref['applied'],
    }


# MDM-LIBVIRT2-STATE-20260710
# Actions état VM V2 isolées sur /libvirt2.
def _lv2_vm_state_action(name, action):
    action = str(action or '').strip().lower()

    commands = {
        'start': 'start',
        'shutdown': 'shutdown',
        'destroy': 'destroy',
        'reboot': 'reboot',
        'reset': 'reset',
        'suspend': 'suspend',
        'resume': 'resume',
        'wakeup': 'dompmwakeup',
    }

    if action not in commands:
        raise RuntimeError('Action état inconnue : ' + action)

    cmd = (
        "sudo -n virsh -c '" + VIRSH_URI + "' "
        + commands[action] + " " + shq(name)
    )

    out, err, code = ssh_exec(cmd, timeout=45)
    if code != 0:
        raise RuntimeError((err or out or ('exit ' + str(code))).strip())

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': action,
        'message': (out or '').strip(),
    }


# ── Persistance de l'autostart (survit à une réimportation après MAJ TrueNAS) ──
def _vm_autostart_store():
    return os.path.join(ACCESS_DATA_DIR, 'vm_autostart.json')


def _vm_autostart_read():
    d = _access_read_json(_vm_autostart_store(), {})
    return d if isinstance(d, dict) else {}


def _vm_autostart_set(name, enabled):
    try:
        d = _vm_autostart_read()
        d[str(name)] = bool(enabled)
        _access_write_json(_vm_autostart_store(), d)
    except Exception as e:
        log.warning('vm autostart persist: %s', e)


def _lv2_repair_uefi(name):
    """Régénère le NVRAM UEFI d'une VM (sauvegarde l'ancien) puis la redémarre.
    Utile si une mise à jour d'OVMF casse l'amorçage (Shell UEFI au lieu de l'OS)."""
    import xml.etree.ElementTree as ET
    xml_str = ssh_ok("sudo -n virsh -c '" + VIRSH_URI + "' dumpxml --inactive " + shq(name)
                     + " 2>/dev/null || sudo -n virsh -c '" + VIRSH_URI + "' dumpxml " + shq(name))
    nvram_path = None
    tpl_from_xml = None
    secure = False
    try:
        rootx = ET.fromstring(xml_str)
        nv = rootx.find('./os/nvram')
        if nv is not None:
            if (nv.text or '').strip():
                nvram_path = nv.text.strip()
            tpl_from_xml = nv.get('template')
        loader = rootx.find('./os/loader')
        if loader is not None and loader.get('secure') == 'yes':
            secure = True
        fw = rootx.find('./os/firmware')
        if fw is not None:
            for feat in fw.findall('feature'):
                if feat.get('name') == 'secure-boot' and feat.get('enabled') == 'yes':
                    secure = True
    except Exception:
        pass
    if tpl_from_xml and '.ms.' in tpl_from_xml:
        secure = True
    if not nvram_path:
        nvram_path = os.path.join(VM_DIR, name + '_VARS.fd')
    # On repart d'un NVRAM en « mode Setup » (variables OVMF neutres, sans clé
    # enrôlée) : Secure Boot n'est PAS imposé, donc OVMF amorce n'importe quel OS
    # (Windows, BSD, Linux) sans « Access Denied ». C'est le repli le plus fiable
    # pour rétablir le démarrage après une MAJ d'OVMF. (Le Secure Boot pourra être
    # réactivé ensuite depuis le firmware si besoin.)
    _ = (secure, tpl_from_xml)  # détection conservée pour usage futur
    src_tpl = '/usr/share/OVMF/OVMF_VARS_4M.fd'
    ts = _sh_time.strftime('%Y%m%d-%H%M%S')
    u = "'" + VIRSH_URI + "'"
    script = (
        'sudo -n virsh -c ' + u + ' destroy ' + shq(name) + ' >/dev/null 2>&1 || true; '
        'if [ -f ' + shq(nvram_path) + ' ]; then '
        'sudo -n cp -f ' + shq(nvram_path) + ' ' + shq(nvram_path + '.bak-' + ts) + ' 2>/dev/null || true; fi; '
        # Pré-amorce le NVRAM avec le bon template (clés MS si Secure Boot) ;
        # sinon on le supprime pour que libvirt le recrée depuis le template du XML.
        'if [ -f ' + shq(src_tpl) + ' ]; then sudo -n cp -f ' + shq(src_tpl) + ' ' + shq(nvram_path) + ' 2>/dev/null || true; '
        'else sudo -n rm -f ' + shq(nvram_path) + ' >/dev/null 2>&1 || true; fi; '
        'sudo -n virsh -c ' + u + ' start ' + shq(name) + ' 2>&1'
    )
    out, err, code = ssh_exec(script, timeout=90)
    combined = ((out or '') + '\n' + (err or '')).strip()
    started = ('started' in combined.lower()) or ('démarr' in combined.lower())
    # réapplique l'autostart si la VM en avait
    if started and _vm_autostart_read().get(str(name)):
        ssh_exec("sudo -n virsh -c " + u + " autostart " + shq(name), timeout=20)
    return {
        'api': 'libvirt2', 'ok': bool(started), 'name': name, 'action': 'repair-uefi',
        'nvram': nvram_path,
        'message': ('NVRAM UEFI régénéré et VM redémarrée. Rouvrez la console pour vérifier le boot.'
                    if started else ('Échec : ' + (combined[-400:] or 'raison inconnue'))),
    }


def _lv2_set_disk_bus(name, bus):
    """Change l'interface du disque principal d'une VM existante (virtio/sata/scsi).
    La VM doit être arrêtée. Ajoute un contrôleur virtio-scsi si besoin et persiste
    le XML dans VM_DIR pour survivre à une réimportation."""
    import xml.etree.ElementTree as ET
    bus = str(bus or '').strip().lower()
    if bus not in ('virtio', 'sata', 'scsi'):
        raise ValueError('Bus invalide : virtio, sata ou scsi.')
    st = _lv2_state(name)
    if 'run' in st or 'paus' in st:
        raise ValueError('Arrêtez la VM avant de changer le bus disque.')
    dev_map = {'virtio': 'vda', 'sata': 'sda', 'scsi': 'sda'}

    def patch(xml_text):
        root = ET.fromstring(xml_text)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('XML invalide (aucun <devices>).')
        disk = None
        for d in devices.findall('disk'):
            if d.get('device', 'disk') == 'disk':
                disk = d
                break
        if disk is None:
            raise RuntimeError('Disque principal introuvable.')
        tgt = disk.find('target')
        if tgt is None:
            tgt = ET.SubElement(disk, 'target')
        tgt.set('bus', bus)
        tgt.set('dev', dev_map[bus])
        addr = disk.find('address')
        if addr is not None:
            disk.remove(addr)  # bus-spécifique : laisser libvirt réattribuer
        if bus == 'scsi' and not any(c.get('type') == 'scsi' for c in devices.findall('controller')):
            c = ET.SubElement(devices, 'controller')
            c.set('type', 'scsi')
            c.set('model', 'virtio-scsi')
            c.set('index', '0')
        return ET.tostring(root, encoding='unicode')

    _lv2_xml_patch(name, patch)
    try:
        new_xml = _lv2_dumpxml(name)
        with open(os.path.join(VM_DIR, name + '.xml'), 'w', encoding='utf-8') as f:
            f.write(new_xml)
    except Exception as e:
        log.warning('persist disk bus xml: %s', e)
    return {
        'api': 'libvirt2', 'ok': True, 'name': name, 'action': 'disk-bus', 'bus': bus,
        'message': 'Bus disque changé en ' + bus + '. Prend effet au prochain démarrage.',
    }


def _lv2_set_autostart(name, enabled):
    enabled = bool(enabled)

    if enabled:
        cmd = "sudo -n virsh -c '" + VIRSH_URI + "' autostart " + shq(name)
    else:
        cmd = "sudo -n virsh -c '" + VIRSH_URI + "' autostart --disable " + shq(name)

    out, err, code = ssh_exec(cmd, timeout=30)
    if code != 0:
        raise RuntimeError((err or out or ('exit ' + str(code))).strip())

    _vm_autostart_set(name, enabled)
    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'autostart',
        'enabled': enabled,
        'message': (out or '').strip(),
    }


# MDM-LIBVIRT2-RESOURCES-20260710
# Modification persistante CPU/RAM via XML libvirt.
# Ne tente pas de hotplug live : changement appliqué au prochain démarrage si VM active.
def _lv2_set_resources(name, vcpus, memory_mb):
    import xml.etree.ElementTree as ET

    try:
        vcpus = int(vcpus)
        memory_mb = int(memory_mb)
    except Exception:
        raise RuntimeError('CPU/RAM invalides')

    if vcpus < 1 or vcpus > 256:
        raise RuntimeError('Nombre de vCPU invalide : attendu entre 1 et 256')

    if memory_mb < 256 or memory_mb > 1048576:
        raise RuntimeError('Mémoire invalide : attendu entre 256 MB et 1048576 MB')

    def _insert_after(root, after_tag, node):
        children = list(root)
        for idx, child in enumerate(children):
            if child.tag == after_tag:
                root.insert(idx + 1, node)
                return
        root.insert(0, node)

    def patch(xml):
        root = ET.fromstring(xml)

        mem = root.find('memory')
        if mem is None:
            mem = ET.Element('memory', {'unit': 'MiB'})
            _insert_after(root, 'uuid', mem)

        mem.set('unit', 'MiB')
        mem.text = str(memory_mb)

        cur = root.find('currentMemory')
        if cur is None:
            cur = ET.Element('currentMemory', {'unit': 'MiB'})
            _insert_after(root, 'memory', cur)

        cur.set('unit', 'MiB')
        cur.text = str(memory_mb)

        vcpu = root.find('vcpu')
        if vcpu is None:
            vcpu = ET.Element('vcpu', {'placement': 'static'})
            _insert_after(root, 'currentMemory', vcpu)

        if not vcpu.get('placement'):
            vcpu.set('placement', 'static')
        vcpu.text = str(vcpus)

        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'set_resources',
        'vcpus': vcpus,
        'memory_mb': memory_mb,
        'persistent': True,
        'live_applied': False,
        'warning': (
            'VM active : CPU/RAM modifiés dans la configuration persistante. '
            'Redémarrer la VM pour appliquer.'
            if running else ''
        )
    }


# MDM-LIBVIRT2-DISKS-20260710
# Disques V2 — ajout disque qcow2 persistant.
def _lv2_safe_name(value, fallback='disk'):
    import re
    value = str(value or '').strip()
    value = re.sub(r'[^A-Za-z0-9_.-]+', '-', value)
    value = value.strip('-._')
    return value or fallback


def _lv2_remote_path_available(path):
    out, err, code = ssh_exec("[ ! -e " + shq(path) + " ]", timeout=10)
    return code == 0


def _lv2_pick_disk_path(vm_name, label):
    safe_vm = _lv2_safe_name(vm_name, 'vm')
    safe_label = _lv2_safe_name(label, 'data')

    base = os.path.join(VM_DIR, safe_vm + '-' + safe_label + '.qcow2')
    if _lv2_remote_path_available(base):
        return base

    for i in range(2, 100):
        candidate = os.path.join(VM_DIR, safe_vm + '-' + safe_label + '-' + str(i) + '.qcow2')
        if _lv2_remote_path_available(candidate):
            return candidate

    raise RuntimeError('Impossible de trouver un nom de disque disponible')


def _lv2_pick_target(used, bus):
    bus = str(bus or 'virtio').lower()

    if bus == 'virtio':
        prefix = 'vd'
    else:
        prefix = 'sd'

    letters = 'abcdefghijklmnopqrstuvwxyz'
    for a in letters:
        target = prefix + a
        if target not in used:
            return target

    for a in letters:
        for b in letters:
            target = prefix + a + b
            if target not in used:
                return target

    raise RuntimeError('Aucun target disque disponible')


def _lv2_add_disk(name, size_gb, bus='virtio', label='data'):
    import xml.etree.ElementTree as ET

    try:
        size_gb = int(size_gb)
    except Exception:
        raise RuntimeError('Taille disque invalide')

    if size_gb < 1 or size_gb > 16384:
        raise RuntimeError('Taille disque invalide : attendu entre 1 GB et 16384 GB')

    bus = str(bus or 'virtio').strip().lower()
    if bus not in ('virtio', 'sata', 'scsi'):
        raise RuntimeError('Bus disque invalide : virtio, sata ou scsi attendu')

    label = _lv2_safe_name(label, 'data')
    disk_path = _lv2_pick_disk_path(name, label)

    created = False
    target_ref = {'target': ''}

    try:
        ssh_ok(
            "mkdir -p " + shq(VM_DIR) + " && qemu-img create -f qcow2 "
            + shq(disk_path) + " " + shq(str(size_gb) + "G"),
            timeout=300
        )
        created = True

        def patch(xml):
            root = ET.fromstring(xml)
            devices = root.find('devices')
            if devices is None:
                raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

            used = set()
            last_disk_index = -1

            children = list(devices)
            for idx, node in enumerate(children):
                if node.tag == 'disk':
                    last_disk_index = idx
                    target_el = node.find('target')
                    if target_el is not None and target_el.get('dev'):
                        used.add(target_el.get('dev'))

            target = _lv2_pick_target(used, bus)
            target_ref['target'] = target

            disk = ET.Element('disk', {'type': 'file', 'device': 'disk'})
            disk.append(ET.Element('driver', {
                'name': 'qemu',
                'type': 'qcow2',
                'cache': 'none',
                'discard': 'unmap'
            }))
            disk.append(ET.Element('source', {'file': disk_path}))
            disk.append(ET.Element('target', {'dev': target, 'bus': bus}))

            if last_disk_index >= 0:
                devices.insert(last_disk_index + 1, disk)
            else:
                devices.append(disk)

            return _lv2_xml_to_text(root)

        _lv2_xml_patch(name, patch)

    except Exception:
        if created:
            ssh_exec("rm -f " + shq(disk_path), timeout=20)
        raise

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'add_disk',
        'disk': {
            'path': disk_path,
            'size_gb': size_gb,
            'bus': bus,
            'target': target_ref.get('target', ''),
            'format': 'qcow2'
        },
        'persistent': True,
        'live_applied': False,
        'warning': (
            'VM active : disque ajouté à la configuration persistante. '
            'Redémarrer la VM pour voir le nouveau disque dans le système invité.'
            if running else ''
        )
    }


# MDM-LIBVIRT2-DISK-RESIZE-20260710
# Redimensionnement protégé : qcow2 uniquement, agrandissement uniquement.
def _lv2_qcow2_virtual_size_bytes(path):
    import json
    import re

    out, err, code = ssh_exec(
        "qemu-img info --output=json " + shq(path),
        timeout=30
    )

    if code == 0 and out.strip():
        try:
            data = json.loads(out)
            return int(data.get('virtual-size') or data.get('virtual_size') or 0)
        except Exception:
            pass

    out = ssh_ok("qemu-img info " + shq(path), timeout=30)
    m = re.search(r'virtual size:.*\((\d+) bytes\)', out)
    if m:
        return int(m.group(1))

    raise RuntimeError('Impossible de lire la taille virtuelle qcow2')


def _lv2_disk_in_inactive_xml(name, disk_path):
    import xml.etree.ElementTree as ET

    xml = _lv2_dumpxml(name)
    root = ET.fromstring(xml)
    devices = root.find('devices')

    if devices is None:
        return None

    for disk in devices.findall('disk'):
        if disk.get('device') != 'disk':
            continue

        src = disk.find('source')
        tgt = disk.find('target')
        drv = disk.find('driver')

        if src is not None and src.get('file') == disk_path:
            return {
                'target': tgt.get('dev', '') if tgt is not None else '',
                'bus': tgt.get('bus', '') if tgt is not None else '',
                'format': drv.get('type', '') if drv is not None else '',
                'driver': drv.get('name', '') if drv is not None else '',
            }

    return None


def _lv2_resize_disk(name, disk_path, new_size_gb):
    disk_path = str(disk_path or '').strip()

    if not disk_path:
        raise RuntimeError('Chemin disque manquant')

    if not disk_path.startswith(VM_DIR.rstrip('/') + '/'):
        raise RuntimeError('Par sécurité, seuls les disques dans VM_DIR peuvent être redimensionnés')

    if not disk_path.lower().endswith('.qcow2'):
        raise RuntimeError('Seuls les disques qcow2 sont pris en charge')

    try:
        new_size_gb = int(new_size_gb)
    except Exception:
        raise RuntimeError('Nouvelle taille invalide')

    if new_size_gb < 1 or new_size_gb > 16384:
        raise RuntimeError('Nouvelle taille invalide : attendu entre 1 GB et 16384 GB')

    info = _lv2_disk_in_inactive_xml(name, disk_path)
    if not info:
        raise RuntimeError('Ce disque n’est pas attaché à cette VM dans la configuration libvirt')

    if (info.get('format') or '').lower() not in ('qcow2', ''):
        raise RuntimeError('Format disque non pris en charge : ' + str(info.get('format')))

    current_bytes = _lv2_qcow2_virtual_size_bytes(disk_path)
    new_bytes = new_size_gb * 1024 * 1024 * 1024

    if new_bytes <= current_bytes:
        current_gb = round(current_bytes / 1024 / 1024 / 1024, 2)
        raise RuntimeError(
            'Réduction interdite. Taille actuelle : '
            + str(current_gb)
            + ' GB. Choisir une taille supérieure.'
        )

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    live_attached = False
    if running:
        for d in virsh_domblklist(name):
            if d.get('source') == disk_path:
                live_attached = True
                break

    if live_attached:
        raise RuntimeError(
            'Disque attaché live à une VM active. '
            'Arrêter la VM avant redimensionnement pour éviter tout risque.'
        )

    out, err, code = ssh_exec(
        "qemu-img resize " + shq(disk_path) + " " + shq(str(new_size_gb) + "G"),
        timeout=300
    )

    if code != 0:
        raise RuntimeError((err or out or ('exit ' + str(code))).strip())

    after_bytes = _lv2_qcow2_virtual_size_bytes(disk_path)

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'resize_disk',
        'disk': {
            'path': disk_path,
            'target': info.get('target', ''),
            'bus': info.get('bus', ''),
            'old_size_gb': round(current_bytes / 1024 / 1024 / 1024, 2),
            'new_size_gb': round(after_bytes / 1024 / 1024 / 1024, 2),
        },
        'warning': (
            'VM active : disque redimensionné côté image qcow2, mais il ne sera visible '
            'dans le système invité qu’après redémarrage si le disque n’est pas attaché live.'
            if running else ''
        )
    }


# MDM-LIBVIRT2-DISK-REMOVE-20260710
# Détachement / suppression disque protégée.
def _lv2_any_vm_references_path(path):
    refs = []
    for vm_name in _lv2_vm_names():
        try:
            xml = _lv2_dumpxml(vm_name)
            if path in xml:
                refs.append(vm_name)
        except Exception:
            pass
    return refs


def _lv2_remove_disk(name, disk_path, delete_file=False):
    import xml.etree.ElementTree as ET

    disk_path = str(disk_path or '').strip()
    delete_file = bool(delete_file)

    if not disk_path:
        raise RuntimeError('Chemin disque manquant')

    if not disk_path.startswith(VM_DIR.rstrip('/') + '/'):
        raise RuntimeError('Par sécurité, seuls les disques dans VM_DIR peuvent être détachés')

    if not disk_path.lower().endswith('.qcow2'):
        raise RuntimeError('Seuls les disques qcow2 sont pris en charge')

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    live_attached = False
    if running:
        for d in virsh_domblklist(name):
            if d.get('source') == disk_path:
                live_attached = True
                break

    if live_attached:
        raise RuntimeError(
            'Disque attaché live à une VM active. '
            'Arrêter la VM avant détachement/suppression.'
        )

    removed_ref = {'target': '', 'bus': '', 'source': disk_path, 'format': ''}

    def patch(xml):
        root = ET.fromstring(xml)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        disks = [d for d in devices.findall('disk') if d.get('device') == 'disk']
        if len(disks) <= 1:
            raise RuntimeError('Refus : impossible de supprimer le seul disque de la VM')

        found = None

        for disk in disks:
            src = disk.find('source')
            tgt = disk.find('target')
            drv = disk.find('driver')
            boot = disk.find('boot')

            if src is not None and src.get('file') == disk_path:
                target = tgt.get('dev', '') if tgt is not None else ''
                bus = tgt.get('bus', '') if tgt is not None else ''
                fmt = drv.get('type', '') if drv is not None else ''

                protected_targets = ('vda', 'sda', 'hda', 'xvda')
                if target in protected_targets:
                    raise RuntimeError('Refus : le disque ' + target + ' semble être un disque principal')

                if boot is not None:
                    raise RuntimeError('Refus : ce disque possède un ordre de boot')

                found = disk
                removed_ref['target'] = target
                removed_ref['bus'] = bus
                removed_ref['format'] = fmt
                break

        if found is None:
            raise RuntimeError('Disque introuvable dans la configuration libvirt de cette VM')

        devices.remove(found)
        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    deleted = False
    delete_warning = ''

    if delete_file:
        refs = _lv2_any_vm_references_path(disk_path)
        if refs:
            raise RuntimeError(
                'Disque détaché de la VM demandée, mais fichier non supprimé car encore référencé par : '
                + ', '.join(refs)
            )

        out, err, code = ssh_exec("rm -f " + shq(disk_path), timeout=30)
        if code != 0:
            raise RuntimeError((err or out or ('exit ' + str(code))).strip())
        deleted = True
    else:
        delete_warning = 'Fichier qcow2 conservé sur le stockage.'

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'remove_disk',
        'disk': removed_ref,
        'detached': True,
        'file_deleted': deleted,
        'warning': delete_warning,
    }


# MDM-LIBVIRT2-NETWORK-20260710
# Réseau V2 : ajout / retrait protégé de cartes réseau.
def _lv2_mac_norm(mac):
    import re
    mac = str(mac or '').strip().lower()
    if not re.match(r'^[0-9a-f]{2}(:[0-9a-f]{2}){5}$', mac):
        raise RuntimeError('Adresse MAC invalide')
    return mac


def _lv2_all_mac_addresses():
    import xml.etree.ElementTree as ET

    macs = set()

    for vm_name in _lv2_vm_names():
        try:
            xml = _lv2_dumpxml(vm_name)
            root = ET.fromstring(xml)
            devices = root.find('devices')
            if devices is None:
                continue

            for iface in devices.findall('interface'):
                mac = iface.find('mac')
                if mac is not None and mac.get('address'):
                    macs.add(mac.get('address').lower())
        except Exception:
            pass

    return macs


def _lv2_mac_usable(mac, own=None):
    """Adresse MAC choisie à la main : bien formée, adresse de carte (pas de groupe), et libre.
    « own » est l'adresse actuelle de la carte qu'on modifie : elle a le droit de rester la même."""
    mac = _lv2_mac_norm(mac)
    if int(mac[:2], 16) & 1:
        raise RuntimeError('Adresse MAC refusée : le premier octet doit être pair (adresse d’une seule carte).')
    if mac == '00:00:00:00:00:00':
        raise RuntimeError('Adresse MAC invalide')
    if mac != own and mac in _lv2_all_mac_addresses():
        raise RuntimeError('Adresse MAC déjà utilisée par une carte d’une machine virtuelle.')
    return mac


def _lv2_generate_mac():
    import random

    used = _lv2_all_mac_addresses()

    for _ in range(200):
        mac = '52:54:00:%02x:%02x:%02x' % (
            random.randint(0x00, 0xff),
            random.randint(0x00, 0xff),
            random.randint(0x00, 0xff)
        )
        if mac not in used:
            return mac

    raise RuntimeError('Impossible de générer une MAC disponible')


def _lv2_list_network_sources():
    import xml.etree.ElementTree as ET

    sources = {
        'br0': {'kind': 'bridge', 'name': 'br0', 'label': 'br0'}
    }

    # Réseaux virtuels libvirt fournissant le NAT, notamment "default".
    net_out, net_err, net_code = ssh_exec(
        "sudo -n virsh -c '" + VIRSH_URI + "' net-list --all --name",
        timeout=20
    )
    if net_code == 0:
        for line in net_out.splitlines():
            network = line.strip()
            if network:
                sources['network:' + network] = {
                    'kind': 'nat',
                    'name': network,
                    'label': network,
                }

    # Bridges détectés côté hôte si possible.
    cmd = 'for i in /sys/class/net/*; do [ -d "$i/bridge" ] && basename "$i"; done'
    out, err, code = ssh_exec("sh -lc " + shq(cmd), timeout=20)
    if code == 0:
        for line in out.splitlines():
            br = line.strip()
            if br:
                sources[br] = {'kind': 'bridge', 'name': br, 'label': br}

    # Sources déjà utilisées dans les XML libvirt.
    for vm_name in _lv2_vm_names():
        try:
            xml = _lv2_dumpxml(vm_name)
            root = ET.fromstring(xml)
            devices = root.find('devices')
            if devices is None:
                continue

            for iface in devices.findall('interface'):
                src = iface.find('source')
                if src is None:
                    continue

                bridge = src.get('bridge') or src.get('dev') or src.get('network') or ''
                if bridge and not any(item.get('name') == bridge and item.get('kind') == (iface.get('type', 'bridge') or 'bridge') for item in sources.values()):
                    sources[bridge] = {
                        'kind': iface.get('type', 'bridge') or 'bridge',
                        'name': bridge,
                        'label': bridge,
                    }
        except Exception:
            pass

    return sorted(sources.values(), key=lambda x: x.get('name', ''))


def _lv2_insert_interface_node(devices, iface):
    children = list(devices)
    for idx, node in enumerate(children):
        if node.tag in ('input', 'graphics', 'video', 'sound', 'memballoon'):
            devices.insert(idx, iface)
            return
    devices.append(iface)


def _lv2_add_nic(name, source='default', model='virtio', net_type='nat', mac=''):
    import re
    import xml.etree.ElementTree as ET

    source = str(source or '').strip()
    model = str(model or 'virtio').strip().lower()
    net_type = str(net_type or 'nat').strip().lower()

    if net_type not in ('nat', 'bridge'):
        raise RuntimeError('Type réseau invalide : utiliser nat ou bridge')

    if not re.match(r'^[A-Za-z0-9_.:-]+$', source):
        raise RuntimeError('Source réseau invalide')

    allowed_models = ('virtio', 'e1000', 'rtl8139', 'vmxnet3')
    if model not in allowed_models:
        raise RuntimeError('Modèle carte réseau invalide')

    # adresse choisie par l'utilisateur, sinon tirée au hasard parmi les adresses libres
    mac = _lv2_mac_usable(mac) if str(mac or '').strip() else _lv2_generate_mac()

    def patch(xml):
        root = ET.fromstring(xml)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        iface_type = 'network' if net_type == 'nat' else 'bridge'
        source_attr = 'network' if net_type == 'nat' else 'bridge'
        iface = ET.Element('interface', {'type': iface_type})
        iface.append(ET.Element('mac', {'address': mac}))
        iface.append(ET.Element('source', {source_attr: source}))
        iface.append(ET.Element('model', {'type': model}))

        _lv2_insert_interface_node(devices, iface)

        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'add_nic',
        'nic': {
            'type': 'network' if net_type == 'nat' else 'bridge',
            'source': source,
            'model': model,
            'mac': mac,
        },
        'persistent': True,
        'live_applied': False,
        'warning': (
            'VM active : carte réseau ajoutée à la configuration persistante. '
            'Redémarrer la VM pour qu’elle apparaisse dans le système invité.'
            if running else ''
        )
    }


def _lv2_set_nic_mac(name, mac, new_mac):
    """Remplace l'adresse MAC d'une carte dans la configuration persistante de la machine."""
    import xml.etree.ElementTree as ET

    mac = _lv2_mac_norm(mac)
    new_mac = _lv2_mac_usable(new_mac, own=mac)

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    if new_mac != mac:
        def patch(xml):
            root = ET.fromstring(xml)
            devices = root.find('devices')
            if devices is None:
                raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')
            for iface in devices.findall('interface'):
                mac_el = iface.find('mac')
                if mac_el is not None and (mac_el.get('address') or '').lower() == mac:
                    mac_el.set('address', new_mac)
                    return _lv2_xml_to_text(root)
            raise RuntimeError('Carte réseau introuvable dans la configuration libvirt')

        _lv2_xml_patch(name, patch)

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'set_nic_mac',
        'nic': {'mac': new_mac, 'previous': mac},
        'changed': new_mac != mac,
        'persistent': True,
        'warning': (
            'VM active : la nouvelle adresse MAC est enregistrée. '
            'Arrêter puis démarrer la VM pour qu’elle soit prise en compte.'
            if running and new_mac != mac else ''
        )
    }


def _lv2_remove_nic(name, mac):
    import xml.etree.ElementTree as ET

    mac = _lv2_mac_norm(mac)

    state = _lv2_state(name)
    running = 'running' in state or 'paused' in state

    if running:
        out, err, code = ssh_exec(
            "sudo -n virsh -c '" + VIRSH_URI + "' domiflist " + shq(name),
            timeout=20
        )
        if mac in (out or '').lower():
            raise RuntimeError(
                'Carte réseau attachée live à une VM active. '
                'Arrêter la VM avant suppression pour éviter toute coupure réseau.'
            )

    removed_ref = {'mac': mac}

    def patch(xml):
        root = ET.fromstring(xml)
        devices = root.find('devices')
        if devices is None:
            raise RuntimeError('Bloc <devices> introuvable dans le XML libvirt')

        ifaces = devices.findall('interface')
        if len(ifaces) <= 1:
            raise RuntimeError('Refus : impossible de supprimer la dernière carte réseau')

        found = None

        for iface in ifaces:
            mac_el = iface.find('mac')
            if mac_el is None:
                continue

            if (mac_el.get('address') or '').lower() == mac:
                boot = iface.find('boot')
                if boot is not None:
                    raise RuntimeError('Refus : cette carte réseau possède un ordre de boot')

                src = iface.find('source')
                mdl = iface.find('model')

                removed_ref['type'] = iface.get('type', '')
                removed_ref['source'] = (
                    src.get('bridge') or src.get('dev') or src.get('network') or ''
                    if src is not None else ''
                )
                removed_ref['model'] = mdl.get('type', '') if mdl is not None else ''

                found = iface
                break

        if found is None:
            raise RuntimeError('Carte réseau introuvable dans la configuration libvirt')

        devices.remove(found)
        return _lv2_xml_to_text(root)

    _lv2_xml_patch(name, patch)

    return {
        'api': 'libvirt2',
        'ok': True,
        'name': name,
        'action': 'remove_nic',
        'nic': removed_ref,
        'persistent': True,
        'warning': 'Carte réseau retirée de la configuration persistante.'
    }


# MDM-LIBVIRT2-SNAPSHOTS-20260710
# Snapshots V2 prudents : création/restauration/suppression uniquement VM arrêtée.
def _lv2_snapshot_vm_must_be_stopped(name):
    state = _lv2_state(name)
    if 'running' in state or 'paused' in state:
        raise RuntimeError(
            'Action snapshot refusée : arrêter la VM avant création/restauration/suppression.'
        )
    return state


def _lv2_snapshot_name_norm(name):
    import re

    name = str(name or '').strip()
    if not name:
        raise RuntimeError('Nom snapshot manquant')

    if not re.match(r'^[A-Za-z0-9_.:-]+$', name):
        raise RuntimeError('Nom snapshot invalide')

    return name


# ── MDM-SHARE-LINKS-V1-BEGIN ────────────────────────────────────────────────
# Liens de partage publics : partager un fichier (ou un dossier en .zip) via une
# URL /s/<token> accessible sans compte TrueNAS. Options : expiration, mot de
# passe (pbkdf2), limite de téléchargements. Stockés dans data/shares.json.
import secrets as _sh_secrets
import hashlib as _sh_hashlib
import hmac as _sh_hmac
import time as _sh_time
import zipfile as _sh_zipfile
import mimetypes as _sh_mimetypes
import shutil as _sh_shutil
import html as _sh_html
from datetime import datetime as _sh_dt, timezone as _sh_tz

SHARE_FILE = os.path.join(ACCESS_DATA_DIR, 'shares.json')
SHARE_ROOT = os.environ.get('SHARE_ROOT', '/mnt')
_share_lock = _access_threading.RLock()


def _share_read():
    with _share_lock:
        data = _access_read_json(SHARE_FILE, {})
        return data if isinstance(data, dict) else {}


def _share_write(data):
    with _share_lock:
        _access_write_json(SHARE_FILE, data)


def _share_now():
    return int(_sh_time.time())


def _share_purge():
    """Supprime les liens définitivement inutilisables (révoqués, expirés,
    limite de téléchargements atteinte). Renvoie le dictionnaire nettoyé."""
    now = _share_now()
    with _share_lock:
        data = _share_read()
        changed = False
        for tok in list(data.keys()):
            rec = data[tok]
            if not isinstance(rec, dict):
                del data[tok]
                changed = True
                continue
            exp = rec.get('expires_at')
            maxd = rec.get('max_downloads')
            expired = exp and now > int(exp)
            exhausted = maxd and int(rec.get('downloads', 0)) >= int(maxd)
            if rec.get('revoked') or expired or exhausted:
                del data[tok]
                changed = True
        if changed:
            _share_write(data)
        return data


def _share_purge_loop(interval=600):
    while True:
        try:
            _share_purge()
        except Exception as exc:
            log.warning('share purge error: %s', exc)
        _sh_time.sleep(interval)


def _share_fmt_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return '—'
    for unit in ('o', 'Ko', 'Mo', 'Go'):
        if n < 1024:
            return ('%.0f %s' if unit == 'o' else '%.1f %s') % (n, unit)
        n /= 1024
    return '%.1f To' % n


def _share_dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def _share_size(rec):
    # Pour un dossier, on NE parcourt PAS l'arborescence (trop coûteux sur un NAS,
    # peut bloquer /share/list). La taille n'est affichée que pour les fichiers.
    if rec.get('is_dir'):
        return None
    try:
        return os.path.getsize(rec.get('path', ''))
    except OSError:
        return None


def _share_hash_pw(password, salt=None):
    salt = salt or _sh_secrets.token_hex(16)
    dk = _sh_hashlib.pbkdf2_hmac(
        'sha256', (password or '').encode('utf-8'), salt.encode('utf-8'), 120000
    )
    return salt + '$' + dk.hex()


def _share_verify_pw(password, stored):
    if not stored or '$' not in str(stored):
        return False
    salt = str(stored).split('$', 1)[0]
    return _sh_hmac.compare_digest(_share_hash_pw(password or '', salt), stored)


def _share_valid(rec):
    if not rec:
        return (False, 'Lien introuvable')
    if rec.get('revoked'):
        return (False, 'Lien révoqué')
    exp = rec.get('expires_at')
    if exp and _share_now() > int(exp):
        return (False, 'Lien expiré')
    maxd = rec.get('max_downloads')
    if maxd and int(rec.get('downloads', 0)) >= int(maxd):
        return (False, 'Nombre maximum de téléchargements atteint')
    if not os.path.exists(rec.get('path', '')):
        return (False, 'Fichier introuvable sur le serveur')
    return (True, '')


def _share_create(path, expires_in=None, max_downloads=None, password=None):
    real = os.path.realpath(str(path or ''))
    root = SHARE_ROOT.rstrip('/')
    if not (real == root or real.startswith(root + '/')):
        raise PermissionError('Le partage est limité à ' + SHARE_ROOT + '/')
    if not os.path.exists(real):
        raise FileNotFoundError('Chemin introuvable : ' + str(path))
    token = _sh_secrets.token_urlsafe(18)
    rec = {
        'token': token,
        'path': real,
        'name': os.path.basename(real.rstrip('/')) or 'download',
        'is_dir': os.path.isdir(real),
        'created_at': _share_now(),
        'expires_at': (_share_now() + int(expires_in)) if expires_in else None,
        'max_downloads': int(max_downloads) if max_downloads else None,
        'downloads': 0,
        'password': _share_hash_pw(password) if password else None,
        'revoked': False,
    }
    data = _share_read()
    data[token] = rec
    _share_write(data)
    return rec


def _share_public(rec):
    valid, reason = _share_valid(rec)
    return {
        'token': rec.get('token'),
        'name': rec.get('name'),
        'is_dir': bool(rec.get('is_dir')),
        'has_password': bool(rec.get('password')),
        'created_at': rec.get('created_at'),
        'expires_at': rec.get('expires_at'),
        'max_downloads': rec.get('max_downloads'),
        'downloads': int(rec.get('downloads', 0)),
        'size': _share_size(rec),
        'valid': valid,
        'reason': reason,
    }


def _share_iso(ts):
    if not ts:
        return ''
    try:
        return _sh_dt.fromtimestamp(int(ts), _sh_tz.utc).astimezone().strftime('%Y-%m-%d %H:%M')
    except Exception:
        return ''


def _share_content_disposition(filename):
    from urllib.parse import quote
    ascii_name = (filename or 'download').encode('ascii', 'ignore').decode('ascii') or 'download'
    ascii_name = ascii_name.replace('"', '').replace('\\', '')
    return "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (ascii_name, quote(filename or 'download'))


_SHARE_CSS = (
    "<style>:root{color-scheme:dark}*{box-sizing:border-box}"
    "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
    "background:radial-gradient(1200px 600px at 50% -10%,#1b2942,#0b1424 60%);"
    "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#e7edf6;padding:24px}"
    ".card{width:100%;max-width:420px;background:rgba(18,28,48,.72);border:1px solid rgba(255,255,255,.10);"
    "border-radius:18px;padding:34px 30px;text-align:center;backdrop-filter:blur(18px);"
    "box-shadow:0 24px 60px rgba(0,0,0,.45)}"
    ".icon{font-size:52px;line-height:1;margin-bottom:14px}"
    "h1{font-size:20px;margin:0 0 10px;word-break:break-word}"
    ".muted{color:#93a2ba;font-size:13px;margin:0 0 22px;line-height:1.6}"
    ".inp{width:100%;padding:11px 14px;border-radius:11px;border:1px solid rgba(255,255,255,.16);"
    "background:rgba(255,255,255,.06);color:#fff;font-size:14px;margin-bottom:12px;outline:none}"
    ".inp:focus{border-color:#3ea6ff}"
    ".btn{display:inline-block;width:100%;padding:12px 16px;border-radius:11px;border:0;cursor:pointer;"
    "background:linear-gradient(180deg,#3ea6ff,#2b7fe0);color:#fff;font-size:15px;font-weight:600;"
    "text-decoration:none;transition:filter .15s}.btn:hover{filter:brightness(1.08)}"
    ".err{color:#ff8a8a;font-size:13px;margin:12px 0 0;min-height:16px}"
    ".foot{margin-top:20px;font-size:11px;color:#5f7characters}</style>"
).replace('#5f7characters', '#5f7597')


def _share_page(inner, title='Partage'):
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>' + _sh_html.escape(title) + '</title>' + _SHARE_CSS +
        '</head><body><div class="card">' + inner +
        '<div class="foot">Desktroo</div></div></body></html>'
    )


def _share_error_page(msg):
    return _share_page(
        '<div class="icon">&#128274;</div><h1>Lien indisponible</h1>'
        '<p class="muted">' + _sh_html.escape(msg) + '</p>',
        'Lien indisponible'
    )


def _share_landing_page(rec):
    valid, reason = _share_valid(rec)
    if not valid:
        return _share_error_page(reason)
    pub = _share_public(rec)
    name = _sh_html.escape(pub.get('name') or 'Fichier')
    size = _share_fmt_bytes(pub['size']) if pub['size'] is not None else '—'
    icon = '&#128193;' if pub['is_dir'] else '&#128196;'
    meta = []
    if pub['is_dir']:
        meta.append('Dossier — téléchargé en .zip')
    meta.append('Taille : ' + size)
    if pub['expires_at']:
        meta.append('Expire le ' + _share_iso(pub['expires_at']))
    if pub['max_downloads']:
        meta.append('Téléchargements : ' + str(pub['downloads']) + ' / ' + str(pub['max_downloads']))
    metahtml = ' &middot; '.join(_sh_html.escape(m) for m in meta)
    token = _sh_html.escape(rec.get('token', ''))
    dl = '/s/' + token + '/download'
    if pub['has_password']:
        body = (
            '<div class="icon">' + icon + '</div><h1>' + name + '</h1>'
            '<p class="muted">' + metahtml + '</p>'
            '<input id="pw" type="password" placeholder="Mot de passe" class="inp" autofocus>'
            '<button class="btn" onclick="go()">Télécharger</button>'
            '<p id="err" class="err"></p>'
            '<script>'
            'function go(){var p=document.getElementById("pw").value;'
            'var e=document.getElementById("err");e.textContent="Vérification…";'
            'fetch("' + dl + '?check=1&pw="+encodeURIComponent(p)).then(function(r){'
            'if(r.status===200){e.textContent="";window.location="' + dl + '?pw="+encodeURIComponent(p);}'
            'else{e.textContent="Mot de passe incorrect.";}})'
            '.catch(function(){e.textContent="Erreur réseau.";});}'
            'document.getElementById("pw").addEventListener("keydown",function(ev){'
            'if(ev.key==="Enter")go();});'
            '</script>'
        )
    else:
        body = (
            '<div class="icon">' + icon + '</div><h1>' + name + '</h1>'
            '<p class="muted">' + metahtml + '</p>'
            '<a class="btn" href="' + dl + '">Télécharger</a>'
        )
    return _share_page(body, rec.get('name') or 'Partage')
# ── MDM-SHARE-LINKS-V1-END ──────────────────────────────────────────────────


# ── MDM-WEBSITES-V1-BEGIN ───────────────────────────────────────────────────
# Sites web servis par le conteneur nginx dédié 'desktroo-websites'.
# Types : static | php | proxy. Accès par port (8100-8130) et/ou server_name.
WEB_FILE = os.path.join(ACCESS_DATA_DIR, 'sites.json')
WEB_CONF_DIR = os.environ.get('WEB_CONF_DIR', '/mnt/Truenas_Stockage/apps/desktop/websites/conf.d')
WEB_LOG_DIR = os.environ.get('WEB_LOG_DIR') or os.path.join(os.path.dirname(WEB_CONF_DIR.rstrip('/')), 'logs')
DB_CONTAINER = os.environ.get('DB_CONTAINER', 'desktroo-mariadb')
WEB_PHP_UPSTREAM = os.environ.get('WEB_PHP_UPSTREAM', 'desktroo-php82:9000')
# Versions PHP disponibles : {version: upstream fastcgi}. Un conteneur php-fpm par version.
_WEB_PHP_DEFAULTS = {
    '8.3': 'desktroo-php83:9000',
    '8.2': 'desktroo-php82:9000',
    '8.1': 'desktroo-php81:9000',
    '7.4': 'desktroo-php74:9000',
}
try:
    WEB_PHP_VERSIONS = json.loads(os.environ.get('WEB_PHP_VERSIONS', '') or '{}')
    if not isinstance(WEB_PHP_VERSIONS, dict) or not WEB_PHP_VERSIONS:
        WEB_PHP_VERSIONS = dict(_WEB_PHP_DEFAULTS)
except Exception:
    WEB_PHP_VERSIONS = dict(_WEB_PHP_DEFAULTS)
WEB_PHP_DEFAULT = os.environ.get('WEB_PHP_DEFAULT') or (
    '8.3' if '8.3' in WEB_PHP_VERSIONS else sorted(WEB_PHP_VERSIONS.keys(), reverse=True)[0]
)
WEB_PHP_DIR = os.environ.get('WEB_PHP_DIR', '/mnt/Truenas_Stockage/apps/desktop/websites/php')
WEB_PHP_PROFILE_FILE = os.path.join(ACCESS_DATA_DIR, 'php_profiles.json')
_PHP_INI_KEY_RE = re.compile(r'^[A-Za-z0-9_.]{1,64}$')
_PHP_EXT_RE = re.compile(r'^[A-Za-z0-9_]{1,40}$')
WEB_PORT_MIN = int(os.environ.get('WEB_PORT_MIN', '8100'))
WEB_PORT_MAX = int(os.environ.get('WEB_PORT_MAX', '8130'))
WEB_ROOT_ALLOWED = os.environ.get('WEB_ROOT_ALLOWED', '/mnt')
WEB_DATASET_POOL = os.environ.get('WEB_DATASET_POOL') or (
    VM_DIR.split('/')[2] if VM_DIR.startswith('/mnt/') and len(VM_DIR.split('/')) > 2 else 'Truenas_Stockage'
)
WEB_DATASET_NAME = os.environ.get('WEB_DATASET_NAME', 'Web')
WEB_WWW_GID = os.environ.get('WEB_WWW_GID', '82')  # gid www-data des conteneurs php-fpm alpine
WEB_PROXY_PORT = int(os.environ.get('WEB_PROXY_PORT', '8080'))  # port partagé du reverse-proxy intégré
WEB_PROXY_FILE = os.path.join(ACCESS_DATA_DIR, 'web_proxy.json')
_WEB_DS_RE = re.compile(r'^[A-Za-z0-9_.-]{1,64}$')
_web_lock = _access_threading.RLock()


def _web_proxy_get():
    d = _access_read_json(WEB_PROXY_FILE, {})
    if not isinstance(d, dict):
        d = {}
    return {'enabled': bool(d.get('enabled', True)), 'port': WEB_PROXY_PORT}


def _web_proxy_set(enabled):
    _access_write_json(WEB_PROXY_FILE, {'enabled': bool(enabled)})
    _web_regenerate()
    return _web_proxy_get()


def _npm_status():
    """Détecte un reverse-proxy NPMplus / Nginx Proxy Manager sur l'hôte.
    Matching sur le nom, l'image et le port admin 81. Utilise sudo (socket Docker root)."""
    info = {'running': False, 'containers': [], 'nas_ip': SSH_HOST}
    cmd = (
        "sudo -n docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null "
        "|| docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}' 2>/dev/null"
    )
    try:
        out, _err, _code = ssh_exec(cmd, timeout=20)
        matches = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            low = line.lower()
            name = line.split('|', 1)[0]
            if ('npmplus' in low or 'nginx-proxy-manager' in low
                    or ':81->' in low or ':81/' in low):
                if name and name not in matches:
                    matches.append(name)
        info['running'] = bool(matches)
        info['containers'] = matches
    except Exception as e:
        info['error'] = str(e)
    return info


def _web_dataset_path():
    return '/mnt/%s/%s' % (WEB_DATASET_POOL, WEB_DATASET_NAME)


def _web_create_dataset(pool=None, name=None):
    """Crée le dataset Web et applique des droits adaptés à l'hébergement :
    propriétaire root, groupe www-data (gid 82) avec setgid, group rwx, other r-x.
    nginx (lecture via 'other') et php-fpm (lecture/écriture via le groupe) accèdent
    tous deux aux fichiers."""
    pool = str(pool or WEB_DATASET_POOL).strip()
    name = str(name or WEB_DATASET_NAME).strip()
    if not _WEB_DS_RE.match(pool) or not _WEB_DS_RE.match(name):
        raise ValueError('Nom de pool ou de dataset invalide.')
    gid = str(WEB_WWW_GID)
    if not re.match(r'^\d+$', gid):
        gid = '82'
    ds = pool + '/' + name
    path = '/mnt/' + ds
    script = (
        'if sudo -n zfs list -H -o name ' + shq(ds) + ' >/dev/null 2>&1; then echo EXISTS; '
        'else sudo -n zfs create ' + shq(ds) + ' && echo CREATED; fi && '
        'sudo -n chown 0:' + gid + ' ' + shq(path) + ' && '
        'sudo -n chmod 2775 ' + shq(path) + ' && echo DONE'
    )
    out, err, code = ssh_exec(script, timeout=60)
    if code != 0 or 'DONE' not in out:
        raise RuntimeError(
            (err or out or 'Échec de création du dataset').strip()
            + ' — vérifiez que l\'utilisateur SSH a les droits sudo (zfs/chown/chmod).'
        )
    return {'path': path, 'dataset': ds, 'existed': 'EXISTS' in out}


def _web_mkdir(parent, name):
    """Crée un sous-dossier de site (dans le dataset Web) avec les bons droits.
    fileops tourne en root avec /mnt monté : création directe, sans SSH."""
    name = str(name or '').strip()
    if name in ('.', '..') or not re.match(r'^[A-Za-z0-9_.-]{1,64}$', name):
        raise ValueError('Nom de dossier invalide (lettres, chiffres, . _ - ).')
    parent_real = os.path.realpath(str(parent or '').strip() or _web_dataset_path())
    base = WEB_ROOT_ALLOWED.rstrip('/')
    if not (parent_real == base or parent_real.startswith(base + '/')):
        raise PermissionError('Le dossier doit être sous %s/.' % WEB_ROOT_ALLOWED)
    target = os.path.join(parent_real, name)
    existed = os.path.isdir(target)
    os.makedirs(target, exist_ok=True)
    gid = int(WEB_WWW_GID) if str(WEB_WWW_GID).isdigit() else 82
    try:
        os.chown(target, 0, gid)
    except OSError:
        pass
    try:
        os.chmod(target, 0o2775)
    except OSError:
        pass
    return {'path': target, 'existed': existed}


def _web_write_user_ini(root, ini):
    """Écrit un .user.ini dans le dossier du site : réglages php.ini propres au
    site (priment sur le profil de la version). Directives 'par répertoire'."""
    root = os.path.realpath(str(root or ''))
    base = WEB_ROOT_ALLOWED.rstrip('/')
    if not root.startswith(base + '/'):
        raise PermissionError('Chemin hors zone autorisée.')
    path = os.path.join(root, '.user.ini')
    if not ini:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    lines = ['; Généré par Desktroo — réglages PHP du site']
    for k, v in ini.items():
        lines.append('%s = %s' % (k, v))
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    gid = int(WEB_WWW_GID) if str(WEB_WWW_GID).isdigit() else 82
    try:
        os.chown(path, 0, gid)
        os.chmod(path, 0o664)
    except OSError:
        pass


_WEB_NAME_RE = re.compile(r'^[A-Za-z0-9 _.\-]{1,48}$')
_WEB_SN_RE = re.compile(r'^[A-Za-z0-9*._\-]{1,253}$')
_WEB_UP_RE = re.compile(r'^https?://[A-Za-z0-9._\-]+(:\d+)?(/\S*)?$')


def _web_read():
    with _web_lock:
        data = _access_read_json(WEB_FILE, {})
        return data if isinstance(data, dict) else {}


def _web_write(data):
    with _web_lock:
        _access_write_json(WEB_FILE, data)


def _web_used_ports(data, exclude=None):
    used = set()
    for sid, s in data.items():
        if sid == exclude:
            continue
        if s.get('enabled', True) and s.get('port'):
            used.add(int(s['port']))
    return used


def _web_auto_port(data):
    used = _web_used_ports(data)
    for p in range(WEB_PORT_MIN, WEB_PORT_MAX + 1):
        if p not in used:
            return p
    raise RuntimeError('Aucun port libre dans la plage %d-%d' % (WEB_PORT_MIN, WEB_PORT_MAX))


def _web_validate(site, data, sid=None):
    name = str(site.get('name', '')).strip()
    if not _WEB_NAME_RE.match(name):
        raise ValueError('Nom invalide (lettres, chiffres, espace, . _ - ; 48 max).')
    typ = site.get('type')
    if typ not in ('static', 'php', 'proxy'):
        raise ValueError('Type invalide.')
    port = site.get('port')
    if port in (None, '', 0, '0'):
        port = _web_auto_port(data)
    port = int(port)
    if not (WEB_PORT_MIN <= port <= WEB_PORT_MAX):
        raise ValueError('Port hors plage %d-%d.' % (WEB_PORT_MIN, WEB_PORT_MAX))
    sn = str(site.get('server_name', '') or '').strip()
    if sn and not _WEB_SN_RE.match(sn):
        raise ValueError('Nom de domaine invalide.')
    for other_id, o in data.items():
        if other_id == sid:
            continue
        if not o.get('enabled', True):
            continue
        if int(o.get('port', 0)) == port and str(o.get('server_name', '') or '') == sn:
            raise ValueError('Le port %d est déjà utilisé%s.' % (port, (' pour ce domaine' if sn else '')))
    out = {'name': name, 'type': typ, 'port': port, 'server_name': sn}
    if typ in ('static', 'php'):
        root = os.path.realpath(str(site.get('root', '')))
        base = WEB_ROOT_ALLOWED.rstrip('/')
        if not (root == base or root.startswith(base + '/')):
            raise ValueError('Le dossier doit être sous %s/.' % WEB_ROOT_ALLOWED)
        if not os.path.isdir(root):
            raise ValueError('Dossier introuvable : %s' % site.get('root'))
        out['root'] = root
        if typ == 'php':
            ver = str(site.get('php_version') or WEB_PHP_DEFAULT)
            if ver not in WEB_PHP_VERSIONS:
                raise ValueError('Version PHP indisponible : %s' % ver)
            out['php_version'] = ver
    else:
        up = str(site.get('upstream', '')).strip()
        if not _WEB_UP_RE.match(up):
            raise ValueError('Cible proxy invalide (ex: http://192.168.0.50:3000).')
        out['upstream'] = up
    # Conserve l'appli d'origine (installeur) pour les actions màj/SSL/clone.
    app = str(site.get('app') or '')
    if app in APP_CATALOG:
        out['app'] = app
    return out


def _web_conf(site, proxy_port=None, sid=None):
    sn = site.get('server_name') or ''
    sn_line = ('    server_name %s;\n' % sn) if sn else ''
    listen_lines = '    listen %d;\n' % int(site['port'])
    if proxy_port and sn:
        listen_lines += '    listen %d;\n' % int(proxy_port)
    log_lines = ''
    if sid:
        log_lines = (
            '    access_log %s/site-%s.access.log;\n' % (WEB_LOG_DIR, sid) +
            '    error_log %s/site-%s.error.log;\n' % (WEB_LOG_DIR, sid)
        )
    t = site['type']
    if t == 'static':
        body = (
            '    root %s;\n' % site['root'] +
            '    index index.html index.htm;\n'
            '    location / { try_files $uri $uri/ =404; }\n'
            '    autoindex off;\n'
        )
    elif t == 'php':
        body = (
            '    root %s;\n' % site['root'] +
            '    index index.php index.html;\n'
            '    location / { try_files $uri $uri/ /index.php?$query_string; }\n'
            '    location ~ \\.php$ {\n'
            '        include fastcgi_params;\n'
            '        fastcgi_pass %s;\n' % WEB_PHP_VERSIONS.get(site.get('php_version'), WEB_PHP_UPSTREAM) +
            '        fastcgi_index index.php;\n'
            '        fastcgi_param SCRIPT_FILENAME $document_root$fastcgi_script_name;\n'
            # HTTPS derrière reverse-proxy : détection locale (sans map globale, vhost autonome).
            '        set $fcgi_https "";\n'
            '        if ($http_x_forwarded_proto = "https") { set $fcgi_https "on"; }\n'
            '        fastcgi_param HTTPS $fcgi_https if_not_empty;\n'
            # Back-offices lourds (PrestaShop…) : 1er chargement long + gros en-têtes.
            '        fastcgi_read_timeout 300s;\n'
            '        fastcgi_send_timeout 300s;\n'
            '        fastcgi_connect_timeout 60s;\n'
            '        fastcgi_buffer_size 32k;\n'
            '        fastcgi_buffers 16 16k;\n'
            '        fastcgi_busy_buffers_size 64k;\n'
            '    }\n'
        )
    else:
        body = (
            '    location / {\n'
            '        proxy_pass %s;\n' % site['upstream'] +
            '        proxy_http_version 1.1;\n'
            '        proxy_set_header Host $host;\n'
            '        proxy_set_header X-Real-IP $remote_addr;\n'
            '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
            '        proxy_set_header X-Forwarded-Proto $scheme;\n'
            '        proxy_set_header Upgrade $http_upgrade;\n'
            '        proxy_set_header Connection "upgrade";\n'
            '    }\n'
        )
    return (
        'server {\n'
        + listen_lines +
        sn_line +
        log_lines +
        '    client_max_body_size 1024m;\n' +
        body +
        '}\n'
    )


def _web_regenerate():
    """Réécrit toutes les confs des sites activés et signale un reload au watcher."""
    data = _web_read()
    proxy = _web_proxy_get()
    proxy_port = proxy['port'] if proxy['enabled'] else None
    os.makedirs(WEB_CONF_DIR, exist_ok=True)
    os.makedirs(WEB_LOG_DIR, exist_ok=True)
    try:
        for fn in os.listdir(WEB_CONF_DIR):
            if (fn.startswith('site-') or fn in ('00-proxy-default.conf', '00-maps.conf')) and fn.endswith('.conf'):
                try:
                    os.unlink(os.path.join(WEB_CONF_DIR, fn))
                except OSError:
                    pass
    except FileNotFoundError:
        pass
    # (La détection HTTPS derrière proxy est désormais locale à chaque vhost PHP,
    #  via set/if — plus de map globale : chaque conf est autonome et robuste.)
    # Reverse-proxy intégré : serveur par défaut sur le port partagé (hôte inconnu -> 404)
    if proxy_port:
        try:
            with open(os.path.join(WEB_CONF_DIR, '00-proxy-default.conf'), 'w', encoding='utf-8') as f:
                f.write('server {\n    listen %d default_server;\n    server_name _;\n'
                        '    return 404;\n}\n' % proxy_port)
        except OSError as e:
            log.warning('web proxy default write error: %s', e)
    for sid, s in data.items():
        if not isinstance(s, dict) or not s.get('enabled', True):
            continue
        try:
            conf = _web_conf(s, proxy_port=proxy_port, sid=sid)
        except Exception as e:
            log.warning('web conf gen error for %s: %s', sid, e)
            continue
        try:
            with open(os.path.join(WEB_CONF_DIR, 'site-%s.conf' % sid), 'w', encoding='utf-8') as f:
                f.write(conf)
        except OSError as e:
            log.warning('web conf write error for %s: %s', sid, e)
    try:
        with open(os.path.join(WEB_CONF_DIR, '.reload'), 'w', encoding='utf-8') as f:
            f.write(str(_sh_time.time()))
    except OSError as e:
        log.warning('web reload signal error: %s', e)
    # Rechargement direct et fiable via le host (indépendant du watcher interne du
    # conteneur, qui peut être cassé selon l'échappement de la compose).
    try:
        ssh_exec("sudo -n docker exec desktroo-websites sh -c "
                 + shq("nginx -t && nginx -s reload"), timeout=30)
    except Exception as e:
        log.warning('web nginx reload (ssh) error: %s', e)


def _web_public(site, sid):
    v = dict(site)
    v['id'] = sid
    v['enabled'] = site.get('enabled', True)
    return v


def _web_create(site):
    data = _web_read()
    clean = _web_validate(site, data)
    sid = _sh_secrets.token_hex(4)
    clean['enabled'] = True
    clean['created_at'] = int(_sh_time.time())
    data[sid] = clean
    _web_write(data)
    _web_regenerate()
    return sid, clean


def _web_reimport():
    """Recrée les entrées de sites à partir des dossiers du dataset Web non encore
    enregistrés. Type 'php' si un fichier .php est présent, sinon 'static'. Port
    auto-assigné. Retourne la liste des noms importés."""
    base = _web_dataset_path()
    data = _web_read()
    existing = set()
    for s in data.values():
        if isinstance(s, dict) and s.get('root'):
            existing.add(os.path.realpath(str(s['root'])))
    try:
        entries = sorted(os.listdir(base))
    except OSError as e:
        raise RuntimeError('Dataset Web introuvable (%s) : %s' % (base, e))
    imported = []
    for name in entries:
        full = os.path.join(base, name)
        if name.startswith('.') or not os.path.isdir(full):
            continue
        real = os.path.realpath(full)
        if real in existing:
            continue
        is_php = os.path.exists(os.path.join(full, 'index.php'))
        if not is_php:
            try:
                is_php = any(f.endswith('.php') for f in os.listdir(full))
            except OSError:
                is_php = False
        site = {'name': name[:48], 'type': ('php' if is_php else 'static'),
                'root': real, 'server_name': ''}
        if is_php:
            site['php_version'] = WEB_PHP_DEFAULT
        try:
            clean = _web_validate(site, data)
            sid = _sh_secrets.token_hex(4)
            clean['enabled'] = True
            clean['created_at'] = int(_sh_time.time())
            clean['imported'] = True
            data[sid] = clean
            imported.append(clean['name'])
        except Exception as e:
            log.warning('web reimport skip %s: %s', name, e)
    if imported:
        _web_write(data)
        _web_regenerate()
    return imported


# ── Profils PHP : réglages php.ini + extensions, par version ────────────────
def _php_profiles_read():
    with _web_lock:
        data = _access_read_json(WEB_PHP_PROFILE_FILE, {})
        return data if isinstance(data, dict) else {}


def _php_profiles_write(data):
    with _web_lock:
        _access_write_json(WEB_PHP_PROFILE_FILE, data)


def _php_profile_get(ver):
    p = _php_profiles_read().get(ver, {})
    return {'ini': p.get('ini', {}) if isinstance(p.get('ini'), dict) else {},
            'extensions': p.get('extensions', []) if isinstance(p.get('extensions'), list) else []}


def _php_write_conf(ver, bump_ext=False):
    prof = _php_profile_get(ver)
    base = os.path.join(WEB_PHP_DIR, ver)
    ini_dir = os.path.join(base, 'ini')
    os.makedirs(ini_dir, exist_ok=True)
    lines = ['; Généré par Desktroo — ne pas éditer']
    for k, v in prof['ini'].items():
        lines.append('%s = %s' % (k, v))
    with open(os.path.join(ini_dir, 'zz-truenas.ini'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    exts = prof['extensions']
    with open(os.path.join(base, 'extensions.txt'), 'w', encoding='utf-8') as f:
        f.write(' '.join(exts) + ('\n' if exts else ''))
    with open(os.path.join(base, '.reload'), 'w', encoding='utf-8') as f:
        f.write(str(_sh_time.time()))
    if bump_ext:
        with open(os.path.join(base, '.extreload'), 'w', encoding='utf-8') as f:
            f.write(str(_sh_time.time()))


def _php_save(version, ini, extensions):
    version = str(version)
    if version not in WEB_PHP_VERSIONS:
        raise ValueError('Version PHP indisponible : %s' % version)
    clean_ini = {}
    for k, v in (ini or {}).items():
        k = str(k).strip()
        if not _PHP_INI_KEY_RE.match(k):
            raise ValueError('Directive ini invalide : %s' % k)
        vs = str(v).replace('\r', '').replace('\n', '').strip()[:256]
        if vs == '':
            continue
        clean_ini[k] = vs
    clean_ext = []
    for e in (extensions or []):
        e = str(e).strip()
        if not e:
            continue
        if not _PHP_EXT_RE.match(e):
            raise ValueError('Nom d\'extension invalide : %s' % e)
        if e not in clean_ext:
            clean_ext.append(e)
    data = _php_profiles_read()
    prev = data.get(version, {})
    changed_ext = set(prev.get('extensions', []) if isinstance(prev.get('extensions'), list) else []) != set(clean_ext)
    data[version] = {'ini': clean_ini, 'extensions': clean_ext}
    _php_profiles_write(data)
    _php_write_conf(version, bump_ext=changed_ext)
    # Applique directement via le host (le watcher interne du conteneur peut être
    # cassé selon l'échappement de la compose). install-php-extensions compile :
    # on lance en tâche de fond puis on recharge php-fpm (kill -USR2 1).
    cont = 'desktroo-php' + version.replace('.', '')

    def _php_apply_bg():
        try:
            if clean_ext:
                inner = 'install-php-extensions ' + ' '.join(clean_ext) + ' >/dev/null 2>&1; kill -USR2 1 2>/dev/null || true'
            else:
                inner = 'kill -USR2 1 2>/dev/null || true'
            ssh_exec('sudo -n docker exec ' + cont + ' sh -c ' + shq(inner), timeout=600)
        except Exception as e:
            log.warning('php ext apply (ssh) error: %s', e)

    _threading.Thread(target=_php_apply_bg, daemon=True).start()
    return data[version], changed_ext
# ── MDM-WEBSITES-V1-END ─────────────────────────────────────────────────────


# ── MDM-APPS-V1-BEGIN : bases de données + installeur d'applications ─────────
DB_HOST = os.environ.get('DB_HOST', 'mariadb')
DB_PORT = int(os.environ.get('DB_PORT', '3306'))
DB_ROOT_PASSWORD = os.environ.get('DB_ROOT_PASSWORD', '')
try:
    import pymysql as _pymysql
    _HAS_PYMYSQL = True
except Exception:
    _HAS_PYMYSQL = False
_DB_NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,63}$')
_DB_USER_RE = re.compile(r'^[A-Za-z0-9_]{1,32}$')
DB_CRED_FILE = os.path.join(ACCESS_DATA_DIR, 'databases.json')


def _db_creds_read():
    d = _access_read_json(DB_CRED_FILE, {})
    return d if isinstance(d, dict) else {}


def _db_creds_save(rec):
    d = _db_creds_read()
    d[rec['name']] = {
        'name': rec['name'], 'user': rec['user'], 'password': rec['password'],
        'host': rec['host'], 'port': rec['port'], 'created_at': int(_sh_time.time()),
    }
    _access_write_json(DB_CRED_FILE, d)


def _db_creds_remove(name):
    d = _db_creds_read()
    if name in d:
        del d[name]
        _access_write_json(DB_CRED_FILE, d)


def _db_conn():
    if not _HAS_PYMYSQL:
        raise RuntimeError('pymysql non disponible — relancez le conteneur fileops')
    return _pymysql.connect(host=DB_HOST, port=DB_PORT, user='root',
                            password=DB_ROOT_PASSWORD, connect_timeout=8, autocommit=True)


# ── MDM-DB-REPAIR-V1 : rétablir l'accès root quand le mot de passe enregistré ne correspond plus ──
# La base garde le mot de passe root de sa création (MARIADB_ROOT_PASSWORD n'est lu qu'à
# l'initialisation du dossier de données). Si la configuration en porte un autre — ancienne
# réinstallation par l'assistant —, fileops reçoit « 1045 Access denied » et ne peut plus gérer
# les bases. La réparation arrête MariaDB, le relance un instant sans contrôle d'accès ni réseau
# (conteneur éphémère sur le même volume), y pose le mot de passe de la configuration, puis
# redémarre le conteneur normal. Les bases et leurs comptes ne sont pas touchés.
DB_CONTAINER = os.environ.get('DB_CONTAINER', 'desktroo-mariadb')
_db_repair_lock = _threading.Lock()

# Exécuté dans le conteneur éphémère. Le SQL arrive en base64 dans DK_SQL.
_DB_REPAIR_INNER = r"""
S=/tmp/dk-repair.sock
mariadbd --user=mysql --skip-grant-tables --skip-networking --socket=$S --pid-file=/tmp/dk-repair.pid >/tmp/dk-repair.log 2>&1 &
PID=$!
ok=0; i=0
while [ $i -lt 120 ]; do
  if mariadb --socket=$S -e "SELECT 1" >/dev/null 2>&1; then ok=1; break; fi
  kill -0 $PID 2>/dev/null || break
  i=$((i+1)); sleep 1
done
if [ $ok != 1 ]; then
  tail -n 15 /tmp/dk-repair.log
  kill -TERM $PID 2>/dev/null; wait $PID 2>/dev/null
  echo DK_REPAIR_FAIL; exit 1
fi
printf %s "$DK_SQL" | base64 -d | mariadb --socket=$S; rc=$?
kill -TERM $PID 2>/dev/null; wait $PID 2>/dev/null
if [ $rc = 0 ]; then echo DK_REPAIR_OK; else echo DK_REPAIR_FAIL; fi
exit $rc
"""


def _db_is_access_denied(e):
    a = getattr(e, 'args', None) or ()
    return bool(a) and a[0] == 1045


def _db_repair_sql(password):
    p = password.replace('\\', '\\\\').replace("'", "\\'")
    return ("FLUSH PRIVILEGES;\n"
            "CREATE USER IF NOT EXISTS 'root'@'%%' IDENTIFIED BY '%(p)s';\n"
            "ALTER USER 'root'@'%%' IDENTIFIED BY '%(p)s';\n"
            "ALTER USER IF EXISTS 'root'@'localhost' IDENTIFIED BY '%(p)s';\n"
            "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%%' WITH GRANT OPTION;\n"
            "FLUSH PRIVILEGES;\n") % {'p': p}


def _db_repair_root():
    import base64 as _b64
    if not DB_ROOT_PASSWORD or '\n' in DB_ROOT_PASSWORD or '\x00' in DB_ROOT_PASSWORD:
        raise ValueError('Mot de passe MariaDB absent de la configuration (DB_ROOT_PASSWORD).')
    if not (SSH_HOST and SSH_USER and SSH_PASS):
        raise ValueError('Accès SSH au NAS non configuré : réparation impossible depuis le bureau.')
    try:
        _db_conn().close()
        return {'ok': True, 'already': True}
    except Exception as e:
        if not _db_is_access_denied(e):
            raise RuntimeError('MariaDB ne répond pas (%s) : la réparation ne concerne que le mot de passe.' % e)
    if not _db_repair_lock.acquire(False):
        raise RuntimeError('Une réparation est déjà en cours.')
    try:
        sql = _b64.b64encode(_db_repair_sql(DB_ROOT_PASSWORD).encode('utf-8')).decode('ascii')
        c = shq(DB_CONTAINER)
        script = (
            "IMG=$(docker inspect -f '{{.Config.Image}}' " + c + " 2>/dev/null)\n"
            '[ -n "$IMG" ] || { echo DK_NO_CONTAINER; exit 0; }\n'
            'docker stop -t 90 ' + c + ' >/dev/null 2>&1\n'
            'docker run --rm --network none --volumes-from ' + c + ' -e DK_SQL=' + sql
            + ' --entrypoint sh "$IMG" -c ' + shq(_DB_REPAIR_INNER) + ' 2>&1\n'
            'docker start ' + c + ' >/dev/null 2>&1 && echo DK_STARTED\n'
        )
        out, err, _code = ssh_exec('sudo -n sh -c ' + shq(script), timeout=300)
        out = out or ''
        if 'DK_NO_CONTAINER' in out:
            raise RuntimeError('Conteneur MariaDB introuvable (%s).' % DB_CONTAINER)
        if 'DK_REPAIR_OK' not in out:
            tail = ' | '.join([l for l in (out + '\n' + (err or '')).splitlines() if l.strip() and not l.startswith('DK_')][-4:])
            raise RuntimeError('Réparation non aboutie' + (' : ' + tail if tail else '') + '.')
        if 'DK_STARTED' not in out:
            raise RuntimeError('Mot de passe rétabli, mais MariaDB n\'a pas redémarré (docker start %s).' % DB_CONTAINER)
        last = None
        for _i in range(40):
            try:
                _db_conn().close()
                log.info('MariaDB : accès root rétabli.')
                return {'ok': True, 'repaired': True}
            except Exception as e:
                last = e
                _sh_time.sleep(1.5)
        raise RuntimeError('MariaDB redémarre encore (%s) — réessayez dans un instant.' % last)
    finally:
        _db_repair_lock.release()
# ── MDM-DB-REPAIR-V1-END ────────────────────────────────────────────────────


def _db_list():
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute('SHOW DATABASES')
        sysdbs = {'mysql', 'information_schema', 'performance_schema', 'sys'}
        return [r[0] for r in cur.fetchall() if r[0] not in sysdbs]
    finally:
        conn.close()


def _db_create(name=None, user=None, password=None):
    name = str(name or ('db_' + _sh_secrets.token_hex(4)))
    user = str(user or name)[:32]
    password = password or _sh_secrets.token_urlsafe(12)
    if not _DB_NAME_RE.match(name):
        raise ValueError('Nom de base invalide (lettres, chiffres, _ ).')
    if not _DB_USER_RE.match(user):
        raise ValueError('Nom d\'utilisateur invalide.')
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute('CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci' % name)
        cur.execute("CREATE USER IF NOT EXISTS %s@'%%' IDENTIFIED BY %s", (user, password))
        cur.execute("ALTER USER %s@'%%' IDENTIFIED BY %s", (user, password))
        cur.execute('GRANT ALL PRIVILEGES ON `' + name + "`.* TO %s@'%%'", (user,))
        cur.execute('FLUSH PRIVILEGES')
        rec = {'name': name, 'user': user, 'password': password, 'host': DB_HOST, 'port': DB_PORT}
        try:
            _db_creds_save(rec)
        except Exception as e:
            log.warning('db creds save error: %s', e)
        return rec
    finally:
        conn.close()


def _db_delete(name, user=None):
    if not _DB_NAME_RE.match(str(name)):
        raise ValueError('Nom de base invalide.')
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute('DROP DATABASE IF EXISTS `%s`' % name)
        if user and _DB_USER_RE.match(str(user)):
            cur.execute("DROP USER IF EXISTS %s@'%%'", (user,))
            cur.execute('FLUSH PRIVILEGES')
        try:
            _db_creds_remove(str(name))
        except Exception as e:
            log.warning('db creds remove error: %s', e)
        return True
    finally:
        conn.close()


# Catalogue d'applications (URL par défaut surchargeable côté client).
APP_CATALOG = {
    'wordpress': {'label': 'WordPress', 'url': 'https://wordpress.org/latest.zip',
                  'strip': True, 'db': True, 'php': '8.2', 'post': 'wordpress',
                  'ext': ['mysqli', 'gd', 'exif', 'zip', 'intl', 'mbstring']},
    'phpmyadmin': {'label': 'phpMyAdmin', 'url': 'https://www.phpmyadmin.net/downloads/phpMyAdmin-latest-all-languages.zip',
                   'strip': True, 'db': False, 'php': '8.2', 'post': 'phpmyadmin',
                   'ext': ['mysqli', 'mbstring', 'zip', 'gd']},
    'nextcloud': {'label': 'Nextcloud', 'url': 'https://download.nextcloud.com/server/releases/latest.zip',
                  'strip': True, 'db': True, 'php': '8.2', 'post': 'nextcloud',
                  'ext': ['pdo_mysql', 'gd', 'zip', 'intl', 'mbstring', 'bcmath', 'gmp', 'exif']},
    'grav': {'label': 'Grav', 'url': 'https://getgrav.org/download/core/grav-admin/latest',
             'strip': True, 'db': False, 'php': '8.2', 'post': None,
             'ext': ['gd', 'zip', 'mbstring', 'curl']},
    'joomla': {'label': 'Joomla', 'url': 'https://downloads.joomla.org/cms/joomla5/5-2-4/Joomla_5-2-4-Stable-Full_Package.zip?format=zip',
               'strip': False, 'db': True, 'php': '8.2', 'post': 'joomla',
               'ext': ['mysqli', 'gd', 'zip', 'intl', 'mbstring', 'curl']},
    'prestashop': {'label': 'PrestaShop', 'url': 'https://github.com/PrestaShop/PrestaShop/releases/download/8.2.0/prestashop_8.2.0.zip',
                   'strip': False, 'db': True, 'php': '8.1', 'post': 'prestashop',
                   'ext': ['pdo_mysql', 'mysqli', 'gd', 'zip', 'intl', 'mbstring', 'curl']},
}


def _app_move_into(exdir, dest, strip):
    entries = os.listdir(exdir)
    src = exdir
    if strip:
        dirs = [e for e in entries if os.path.isdir(os.path.join(exdir, e))]
        files = [e for e in entries if os.path.isfile(os.path.join(exdir, e))]
        if len(dirs) == 1 and not files:
            src = os.path.join(exdir, dirs[0])
    for item in os.listdir(src):
        s = os.path.join(src, item)
        d = os.path.join(dest, item)
        if os.path.exists(d):
            if os.path.isdir(d) and not os.path.islink(d):
                _sh_shutil.rmtree(d)
            else:
                os.remove(d)
        _sh_shutil.move(s, d)


def _app_fetch_into(url, dest, strip):
    import tempfile, urllib.request, zipfile, tarfile
    os.makedirs(dest, exist_ok=True)
    tmpd = tempfile.mkdtemp(prefix='appdl-')
    try:
        arch = os.path.join(tmpd, 'archive')
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 Desktroo'})
        with urllib.request.urlopen(req, timeout=300) as resp:
            with open(arch, 'wb') as f:
                _sh_shutil.copyfileobj(resp, f)
        exdir = os.path.join(tmpd, 'ex')
        os.makedirs(exdir)
        if zipfile.is_zipfile(arch):
            with zipfile.ZipFile(arch) as z:
                z.extractall(exdir)
        elif tarfile.is_tarfile(arch):
            with tarfile.open(arch) as t:
                t.extractall(exdir)
        else:
            raise RuntimeError('Archive téléchargée non reconnue (ni zip ni tar).')
        _app_move_into(exdir, dest, strip)
    finally:
        try:
            _sh_shutil.rmtree(tmpd)
        except OSError:
            pass


def _app_fix_perms(root, gid):
    def _set(p, mode):
        try:
            os.chown(p, 0, gid)
        except OSError:
            pass
        try:
            os.chmod(p, mode)
        except OSError:
            pass
    _set(root, 0o2775)
    for r, dirs, files in os.walk(root):
        for d in dirs:
            _set(os.path.join(r, d), 0o2775)
        for fn in files:
            _set(os.path.join(r, fn), 0o0664)


def _wp_salt(n=64):
    import string
    chars = string.ascii_letters + string.digits + '!@#$%^&*()-_ []{}<>~`+=,.;:/?|'
    return ''.join(_sh_secrets.choice(chars) for _ in range(n))


def _app_wp_config(root, db):
    sample = os.path.join(root, 'wp-config-sample.php')
    target = os.path.join(root, 'wp-config.php')
    if not os.path.exists(sample) or os.path.exists(target):
        return
    with open(sample, 'r', encoding='utf-8', errors='replace') as f:
        cfg = f.read()
    cfg = cfg.replace('database_name_here', db['name'])
    cfg = cfg.replace('username_here', db['user'])
    cfg = cfg.replace('password_here', db['password'])
    cfg = cfg.replace("'localhost'", "'%s'" % db['host'])
    while "'put your unique phrase here'" in cfg:
        cfg = cfg.replace("'put your unique phrase here'", "'" + _wp_salt() + "'", 1)
    # Derrière un reverse-proxy HTTPS (NPMplus) : faire confiance à X-Forwarded-Proto.
    # Et URL dynamiques (WP_HOME/WP_SITEURL d'après l'hôte demandé) : le site répond
    # aussi bien en IP:port qu'en domaine, sans redirection canonique (évite le timeout
    # quand on accède par IP alors que le domaine est enregistré en base).
    proxy_fix = (
        "\n/* Accès multi-hôte + reverse-proxy HTTPS (Desktroo) */\n"
        "if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {\n"
        "    $_SERVER['HTTPS'] = 'on';\n"
        "}\n"
        "if (!empty($_SERVER['HTTP_HOST'])) {\n"
        "    $_tnd_proto = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off') ? 'https' : 'http';\n"
        "    define('WP_HOME', $_tnd_proto . '://' . $_SERVER['HTTP_HOST']);\n"
        "    define('WP_SITEURL', $_tnd_proto . '://' . $_SERVER['HTTP_HOST']);\n"
        "}\n"
    )
    marker = '<?php'
    idx = cfg.find(marker)
    if idx >= 0:
        cut = idx + len(marker)
        cfg = cfg[:cut] + '\n' + proxy_fix + cfg[cut:]
    else:
        cfg = '<?php' + proxy_fix + cfg
    with open(target, 'w', encoding='utf-8') as f:
        f.write(cfg)


def _app_pma_config(root, db):
    target = os.path.join(root, 'config.inc.php')
    if os.path.exists(target):
        return
    secret = _sh_secrets.token_hex(16)
    cfg = (
        "<?php\n"
        "$cfg['blowfish_secret'] = '" + secret + "';\n"
        "$i = 0;\n"
        "$i++;\n"
        "$cfg['Servers'][$i]['host'] = '" + DB_HOST + "';\n"
        "$cfg['Servers'][$i]['port'] = '" + str(DB_PORT) + "';\n"
        "$cfg['Servers'][$i]['auth_type'] = 'cookie';\n"
        "$cfg['Servers'][$i]['AllowNoPassword'] = false;\n"
    )
    with open(target, 'w', encoding='utf-8') as f:
        f.write(cfg)


def _app_prestashop_extract(root):
    """Le paquet PrestaShop contient un zip interne (prestashop.zip) + un
    index.php d'auto-décompression. On extrait le zip interne et on nettoie."""
    import zipfile
    inner = os.path.join(root, 'prestashop.zip')
    if os.path.exists(inner):
        try:
            with zipfile.ZipFile(inner) as z:
                z.extractall(root)
            os.remove(inner)
        except Exception as e:
            log.warning('prestashop inner extract error: %s', e)
    for junk in ('Install_PrestaShop.html',):
        p = os.path.join(root, junk)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def _app_ensure_extensions(ver, exts):
    if ver not in WEB_PHP_VERSIONS or not exts:
        return
    prof = _php_profile_get(ver)
    merged = list(prof['extensions'])
    changed = False
    for e in exts:
        if e not in merged:
            merged.append(e)
            changed = True
    if changed:
        _php_save(ver, prof['ini'], merged)


# Profil de réponses (pré-remplit les installs, évite de tout ressaisir)
INSTALL_PROFILE_FILE = os.path.join(ACCESS_DATA_DIR, 'install_profile.json')
WEB_BIN_DIR = os.environ.get('WEB_BIN_DIR', os.path.join(os.path.dirname(WEB_PHP_DIR.rstrip('/')), 'bin'))


def _install_profile_read():
    d = _access_read_json(INSTALL_PROFILE_FILE, {})
    return d if isinstance(d, dict) else {}


def _install_profile_save(p):
    keep = {k: str(p.get(k, '') or '') for k in ('admin_user', 'admin_email', 'language', 'title')}
    _access_write_json(INSTALL_PROFILE_FILE, keep)
    return keep


def _php_container(ver):
    return 'desktroo-php' + str(ver).replace('.', '')


def _docker_exec(container, inner_cmd, timeout=240, user=None):
    """Exécute une commande shell dans un conteneur via SSH (docker exec, repli sudo)."""
    u = ('-u ' + shq(user) + ' ') if user else ''
    full = 'docker exec ' + u + shq(container) + ' sh -c ' + shq(inner_cmd)
    out, err, code = ssh_exec(full, timeout=timeout)
    if code != 0:
        out2, err2, code2 = ssh_exec('sudo -n ' + full, timeout=timeout)
        if code2 == 0:
            return out2, err2, 0
        return out, (err or err2), code
    return out, err, code


def _wp_cli_ensure():
    os.makedirs(WEB_BIN_DIR, exist_ok=True)
    phar = os.path.join(WEB_BIN_DIR, 'wp-cli.phar')
    if not os.path.exists(phar) or os.path.getsize(phar) < 1000000:
        import urllib.request
        url = 'https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar'
        req = urllib.request.Request(url, headers={'User-Agent': 'Desktroo'})
        with urllib.request.urlopen(req, timeout=120) as resp:
            with open(phar, 'wb') as f:
                _sh_shutil.copyfileobj(resp, f)
        try:
            os.chmod(phar, 0o644)
        except OSError:
            pass
    return phar


def _app_wp_install(root, ver, url, title, admin_user, admin_password, admin_email, language):
    """Installe WordPress sans assistant via WP-CLI (dans le conteneur php de la version)."""
    phar = _wp_cli_ensure()
    container = _php_container(ver)
    inner = (
        'install-php-extensions mysqli >/dev/null 2>&1 || true; '
        'php ' + shq(phar) + ' --allow-root --path=' + shq(root) + ' core install'
        ' --url=' + shq(url) +
        ' --title=' + shq(title) +
        ' --admin_user=' + shq(admin_user) +
        ' --admin_password=' + shq(admin_password) +
        ' --admin_email=' + shq(admin_email) +
        ' --skip-email'
    )
    out, err, code = _docker_exec(container, inner, timeout=300)
    combined = (out or '') + '\n' + (err or '')
    ok = (code == 0 and 'success' in combined.lower()) or 'already installed' in combined.lower()
    if ok and language:
        _docker_exec(container,
                     'php ' + shq(phar) + ' --allow-root --path=' + shq(root) +
                     ' language core install ' + shq(language) + ' --activate >/dev/null 2>&1 || true',
                     timeout=120)
    return ok, combined.strip()[-1500:]


def _app_nextcloud_install(root, ver, host, domain, db, admin_user, admin_password):
    """Installe Nextcloud sans assistant via occ (exécuté en www-data)."""
    container = _php_container(ver)
    occ = 'php -d memory_limit=512M ' + shq(root.rstrip('/') + '/occ')
    pre = ('install-php-extensions pdo_mysql gd zip intl mbstring bcmath gmp exif '
           '>/dev/null 2>&1 || true; ')
    install = (
        occ + ' maintenance:install --no-interaction'
        ' --database mysql'
        ' --database-host ' + shq(db['host']) +
        ' --database-name ' + shq(db['name']) +
        ' --database-user ' + shq(db['user']) +
        ' --database-pass ' + shq(db['password']) +
        ' --admin-user ' + shq(admin_user) +
        ' --admin-pass ' + shq(admin_password)
    )
    out, err, code = _docker_exec(container, pre + install, timeout=420, user='www-data')
    combined = (out or '') + '\n' + (err or '')
    ok = code == 0 and ('successfully installed' in combined.lower() or 'already been installed' in combined.lower())
    if ok:
        url = ('https://' + domain) if domain else ('http://' + host)
        cmds = []
        idx = 1
        for hn in ([domain, host] if domain else [host]):
            if hn:
                cmds.append(occ + ' config:system:set trusted_domains ' + str(idx) + ' --value=' + shq(hn))
                idx += 1
        cmds.append(occ + ' config:system:set overwrite.cli.url --value=' + shq(url))
        if domain:
            cmds.append(occ + ' config:system:set overwriteprotocol --value=https')
        _docker_exec(container, ' ; '.join(cmds) + ' ; true', timeout=120, user='www-data')
    return ok, combined.strip()[-1500:]


def _app_joomla_install(root, ver, db, admin_user, admin_password, admin_email, site_name):
    """Installe Joomla sans assistant via installation/joomla.php (en www-data)."""
    container = _php_container(ver)
    jphp = 'php -d memory_limit=512M ' + shq(root.rstrip('/') + '/installation/joomla.php')
    prefix = 'j' + _sh_secrets.token_hex(2) + '_'
    pre = 'install-php-extensions mysqli gd zip intl mbstring curl >/dev/null 2>&1 || true; '
    install = (
        jphp + ' install'
        ' --site-name=' + shq(site_name or 'Joomla') +
        ' --admin-user=' + shq(admin_user) +
        ' --admin-username=' + shq(admin_user) +
        ' --admin-password=' + shq(admin_password) +
        ' --admin-email=' + shq(admin_email) +
        ' --db-type=mysqli'
        ' --db-host=' + shq(db['host']) +
        ' --db-user=' + shq(db['user']) +
        ' --db-pass=' + shq(db['password']) +
        ' --db-name=' + shq(db['name']) +
        ' --db-prefix=' + shq(prefix) +
        ' --db-encryption=0'
    )
    out, err, code = _docker_exec(container, pre + install, timeout=420, user='www-data')
    combined = (out or '') + '\n' + (err or '')
    ok = (code == 0) and ('error' not in combined.lower() or 'successfully' in combined.lower())
    if ok:
        # Le dossier installation doit être retiré après installation
        _docker_exec(container, 'rm -rf ' + shq(root.rstrip('/') + '/installation'), timeout=60, user='www-data')
    return ok, combined.strip()[-1500:]


def _app_prestashop_install(root, ver, domain_host, db, admin_email, admin_password, name, language):
    """Installe PrestaShop sans assistant via install/index_cli.php (en www-data)."""
    container = _php_container(ver)
    cli = 'php -d memory_limit=512M ' + shq(root.rstrip('/') + '/install/index_cli.php')
    pre = 'install-php-extensions pdo_mysql mysqli gd zip intl mbstring curl >/dev/null 2>&1 || true; '
    install = (
        cli +
        ' --domain=' + shq(domain_host) +
        ' --db_server=' + shq(db['host']) +
        ' --db_name=' + shq(db['name']) +
        ' --db_user=' + shq(db['user']) +
        ' --db_password=' + shq(db['password']) +
        ' --prefix=ps_'
        ' --db_create=0'
        ' --name=' + shq(name or 'PrestaShop') +
        ' --email=' + shq(admin_email) +
        ' --password=' + shq(admin_password) +
        ' --firstname=Admin --lastname=Admin'
        ' --language=' + shq((language or 'fr').split('_')[0]) +
        ' --country=fr --newsletter=0 --send_email=0'
    )
    out, err, code = _docker_exec(container, pre + install, timeout=480, user='www-data')
    combined = (out or '') + '\n' + (err or '')
    ok = (code == 0)
    if ok:
        # Le dossier install doit être retiré après installation
        _docker_exec(container, 'rm -rf ' + shq(root.rstrip('/') + '/install'), timeout=60, user='www-data')
    return ok, combined.strip()[-1500:]


def _app_install(app_key, root, php_version=None, port=None, server_name=None,
                 name=None, url_override=None, create_db=True,
                 auto=False, site_host=None, title=None,
                 admin_user=None, admin_password=None, admin_email=None, language=None,
                 db_name=None, db_user=None, db_password=None):
    meta = APP_CATALOG.get(str(app_key))
    if not meta:
        raise ValueError('Application inconnue : %s' % app_key)
    root = os.path.realpath(str(root or ''))
    base = WEB_ROOT_ALLOWED.rstrip('/')
    if not (root == base or root.startswith(base + '/')):
        raise PermissionError('Le dossier doit être sous %s/.' % WEB_ROOT_ALLOWED)
    os.makedirs(root, exist_ok=True)
    gid = int(WEB_WWW_GID) if str(WEB_WWW_GID).isdigit() else 82
    url = str(url_override or '').strip() or meta['url']
    _app_fetch_into(url, root, meta.get('strip'))
    if meta.get('post') == 'prestashop':
        _app_prestashop_extract(root)
    db = None
    if meta.get('db') and create_db:
        db = _db_create(db_name or None, db_user or None, db_password or None)
    if meta.get('post') == 'wordpress' and db:
        _app_wp_config(root, db)
    if meta.get('post') == 'phpmyadmin':
        _app_pma_config(root, db)
    _app_fix_perms(root, gid)
    ver = str(php_version or meta.get('php') or WEB_PHP_DEFAULT)
    site = {'name': name or meta['label'], 'type': 'php', 'root': root,
            'php_version': ver if ver in WEB_PHP_VERSIONS else WEB_PHP_DEFAULT,
            'app': str(app_key)}
    if port:
        site['port'] = port
    if server_name:
        site['server_name'] = server_name
    sid, clean = _web_create(site)
    if db:
        d = _web_read()
        if sid in d:
            d[sid]['db'] = {'name': db['name'], 'user': db['user']}
            _web_write(d)
        clean['db'] = {'name': db['name'], 'user': db['user']}
    _app_ensure_extensions(clean.get('php_version'), meta.get('ext') or [])
    result = {'ok': True, 'app': app_key, 'site': _web_public(clean, sid), 'db': db,
              'ext_installing': bool(meta.get('ext'))}
    # Installation sans assistant (Softaculous-like). WordPress d'abord (WP-CLI).
    if auto and db and meta.get('post') == 'wordpress':
        try:
            apw = admin_password or _sh_secrets.token_urlsafe(10)
            auser = admin_user or 'admin'
            if clean.get('server_name'):
                aurl = 'https://' + clean['server_name']
            else:
                aurl = 'http://' + (site_host or 'localhost') + ':' + str(int(clean['port']))
            ok, msg = _app_wp_install(
                root, clean.get('php_version'), aurl,
                title or name or meta['label'], auser, apw,
                admin_email or 'admin@example.com', language or 'fr_FR')
            result['auto_installed'] = ok
            result['auto_message'] = msg
            if ok:
                result['login_url'] = aurl + '/wp-admin/'
                result['admin'] = {'user': auser, 'password': apw}
        except Exception as e:
            result['auto_installed'] = False
            result['auto_message'] = str(e)
    elif auto and db and meta.get('post') == 'nextcloud':
        try:
            apw = admin_password or _sh_secrets.token_urlsafe(10)
            auser = admin_user or 'admin'
            domain = clean.get('server_name') or ''
            host = (site_host or 'localhost') + ':' + str(int(clean['port']))
            ok, msg = _app_nextcloud_install(root, clean.get('php_version'), host, domain, db, auser, apw)
            result['auto_installed'] = ok
            result['auto_message'] = msg
            if ok:
                result['login_url'] = ('https://' + domain) if domain else ('http://' + host)
                result['admin'] = {'user': auser, 'password': apw}
        except Exception as e:
            result['auto_installed'] = False
            result['auto_message'] = str(e)
    elif auto and db and meta.get('post') == 'joomla':
        try:
            apw = admin_password or _sh_secrets.token_urlsafe(10)
            auser = admin_user or 'admin'
            ok, msg = _app_joomla_install(
                root, clean.get('php_version'), db, auser, apw,
                admin_email or 'admin@example.com', title or name or 'Joomla')
            result['auto_installed'] = ok
            result['auto_message'] = msg
            if ok:
                domain = clean.get('server_name') or ''
                result['login_url'] = (('https://' + domain) if domain
                                       else ('http://' + (site_host or 'localhost') + ':' + str(int(clean['port'])))) + '/administrator/'
                result['admin'] = {'user': auser, 'password': apw}
        except Exception as e:
            result['auto_installed'] = False
            result['auto_message'] = str(e)
    elif auto and db and meta.get('post') == 'prestashop':
        try:
            apw = admin_password or _sh_secrets.token_urlsafe(10)
            domain = clean.get('server_name') or ''
            domain_host = domain or ((site_host or 'localhost') + ':' + str(int(clean['port'])))
            ok, msg = _app_prestashop_install(
                root, clean.get('php_version'), domain_host, db,
                admin_email or 'admin@example.com', apw,
                title or name or 'PrestaShop', language or 'fr')
            result['auto_installed'] = ok
            result['auto_message'] = msg
            if ok:
                result['login_url'] = (('https://' + domain) if domain
                                       else ('http://' + domain_host)) + '/admin/'
                result['admin'] = {'user': admin_email or 'admin@example.com', 'password': apw}
        except Exception as e:
            result['auto_installed'] = False
            result['auto_message'] = str(e)
    elif auto and meta.get('db'):
        result['auto_supported'] = False
    # Persiste les identifiants pour les afficher plus tard dans la liste des sites.
    if result.get('admin') or result.get('login_url'):
        try:
            d = _web_read()
            if sid in d:
                if result.get('admin'):
                    d[sid]['admin'] = {'user': result['admin'].get('user'),
                                       'password': result['admin'].get('password')}
                if result.get('login_url'):
                    d[sid]['login_url'] = result['login_url']
                _web_write(d)
        except Exception as e:
            log.warning('persist admin creds: %s', e)
    return result
# ── MDM-APPS-V1-END ─────────────────────────────────────────────────────────


# ── MDM-WEB-MANAGE-V1 : logs, mise à jour, clone, SSL PrestaShop ─────────────
_WEB_LOG_LINES_MAX = 2000


def _web_get(sid):
    data = _web_read()
    site = data.get(sid)
    if not isinstance(site, dict):
        raise ValueError('Site introuvable.')
    return data, site


def _web_detect_app(site):
    """Clé d'appli du site ('wordpress', 'nextcloud', 'prestashop', 'joomla',
    'phpmyadmin') ou None. Utilise le champ 'app' puis une détection fichiers."""
    a = str(site.get('app') or '').strip().lower()
    if a in APP_CATALOG:
        return a
    root = site.get('root') or ''
    if not root or not os.path.isdir(root):
        return None
    def has(*parts):
        return os.path.exists(os.path.join(root, *parts))
    if has('wp-config.php') or has('wp-login.php') or has('wp-load.php'):
        return 'wordpress'
    if has('occ') and has('config', 'config.php'):
        return 'nextcloud'
    if has('configuration.php') and os.path.isdir(os.path.join(root, 'administrator')):
        return 'joomla'
    if (has('classes', 'PrestaShopAutoload.php') or has('config', 'settings.inc.php')
            or has('app', 'config', 'parameters.php')):
        return 'prestashop'
    if has('config.inc.php') and os.path.isdir(os.path.join(root, 'libraries')):
        return 'phpmyadmin'
    return None


def _web_tail_log(sid, kind='error', lines=200):
    kind = 'access' if kind == 'access' else 'error'
    lines = max(1, min(int(lines or 200), _WEB_LOG_LINES_MAX))
    path = os.path.join(WEB_LOG_DIR, 'site-%s.%s.log' % (sid, kind))
    if not os.path.isfile(path):
        return {'ok': True, 'kind': kind, 'lines': [], 'exists': False, 'path': path}
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = min(size, 256 * 1024)
            f.seek(size - block)
            data = f.read().decode('utf-8', 'replace')
        rows = data.splitlines()[-lines:]
    except OSError as e:
        raise RuntimeError('Lecture du log impossible : %s' % e)
    return {'ok': True, 'kind': kind, 'lines': rows, 'exists': True, 'path': path, 'size': size}


def _app_update(sid):
    data, site = _web_get(sid)
    app = _web_detect_app(site)
    if not app:
        raise ValueError("Type d'application non reconnu — mise à jour automatique indisponible.")
    root = (site.get('root') or '').rstrip('/')
    ver = site.get('php_version') or WEB_PHP_DEFAULT
    container = _php_container(ver)
    if app == 'wordpress':
        phar = _wp_cli_ensure()
        wp = 'php ' + shq(phar) + ' --allow-root --path=' + shq(root) + ' '
        cmd = ('install-php-extensions mysqli >/dev/null 2>&1 || true; '
               + wp + 'core update 2>&1; '
               + wp + 'core update-db 2>&1; '
               + wp + 'plugin update --all 2>&1; '
               + wp + 'theme update --all 2>&1; '
               + 'echo "--- version ---"; ' + wp + 'core version 2>&1')
        out, err, code = _docker_exec(container, cmd, timeout=600)
        combined = ((out or '') + '\n' + (err or '')).strip()
        return {'ok': code == 0, 'app': app, 'log': combined[-4000:]}
    if app == 'nextcloud':
        occ = 'php ' + shq(root + '/occ') + ' '
        updater = root + '/updater/updater.phar'
        cmd = ('if [ -f ' + shq(updater) + ' ]; then php ' + shq(updater) + ' --no-interaction 2>&1 || true; fi; '
               + occ + 'upgrade 2>&1 || true; '
               + occ + 'maintenance:mode --off 2>&1 || true; '
               + occ + 'status 2>&1')
        out, err, code = _docker_exec(container, cmd, timeout=900, user='www-data')
        combined = ((out or '') + '\n' + (err or '')).strip()
        return {'ok': True, 'app': app, 'log': combined[-4000:]}
    if app == 'prestashop':
        con = 'php ' + shq(root + '/bin/console') + ' '
        cmd = ('if [ -f ' + shq(root + '/bin/console') + ' ]; then ' + con + 'cache:clear --no-warmup 2>&1 || true; fi; echo done')
        out, err, code = _docker_exec(container, cmd, timeout=300, user='www-data')
        combined = ((out or '') + '\n' + (err or '')).strip()
        return {'ok': True, 'app': app, 'partial': True, 'log': combined[-2000:],
                'message': ("La mise à jour du cœur PrestaShop se fait via le module « Mise à niveau "
                            "en 1 clic » (autoupgrade) du back-office. Le cache a été vidé.")}
    return {'ok': False, 'app': app, 'partial': True,
            'message': "Mise à jour automatique non prise en charge pour cette application ; "
                       "utilisez son mécanisme de mise à jour intégré."}


def _app_rewrite_db_config(app, root, db):
    """Réécrit les identifiants de base dans la config de l'appli clonée."""
    import re as _re
    root = root.rstrip('/')
    def rw(path, subs):
        if not os.path.isfile(path):
            return False
        try:
            s = open(path, encoding='utf-8', errors='replace').read()
        except OSError:
            return False
        for pat, rep in subs:
            s = _re.sub(pat, rep, s)
        open(path, 'w', encoding='utf-8').write(s)
        return True
    if app == 'wordpress':
        return rw(os.path.join(root, 'wp-config.php'), [
            (r"(define\(\s*'DB_NAME'\s*,\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"(define\(\s*'DB_USER'\s*,\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"(define\(\s*'DB_PASSWORD'\s*,\s*)'[^']*'", r"\1'%s'" % db['password']),
        ])
    if app == 'nextcloud':
        return rw(os.path.join(root, 'config', 'config.php'), [
            (r"('dbname'\s*=>\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"('dbuser'\s*=>\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"('dbpassword'\s*=>\s*)'[^']*'", r"\1'%s'" % db['password']),
        ])
    if app == 'prestashop':
        done = rw(os.path.join(root, 'app', 'config', 'parameters.php'), [
            (r"('database_name'\s*=>\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"('database_user'\s*=>\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"('database_password'\s*=>\s*)'[^']*'", r"\1'%s'" % db['password']),
        ])
        done = rw(os.path.join(root, 'config', 'settings.inc.php'), [
            (r"(_DB_NAME_'\s*,\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"(_DB_USER_'\s*,\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"(_DB_PASSWD_'\s*,\s*)'[^']*'", r"\1'%s'" % db['password']),
        ]) or done
        return done
    if app == 'joomla':
        return rw(os.path.join(root, 'configuration.php'), [
            (r"(\$db\s*=\s*)'[^']*'", r"\1'%s'" % db['name']),
            (r"(\$user\s*=\s*)'[^']*'", r"\1'%s'" % db['user']),
            (r"(\$password\s*=\s*)'[^']*'", r"\1'%s'" % db['password']),
        ])
    return False


def _db_clone(src_name, dst_name):
    if not (_DB_NAME_RE.match(str(src_name)) and _DB_NAME_RE.match(str(dst_name))):
        raise ValueError('Nom de base invalide.')
    tmp = '/tmp/clone_%s.sql' % dst_name
    inner = ('export MYSQL_PWD=%s; mysqldump -uroot --no-tablespaces --single-transaction '
             '--routines --triggers %s > %s && mysql -uroot %s < %s; rc=$?; rm -f %s; exit $rc') % (
        shq(DB_ROOT_PASSWORD), shq(src_name), shq(tmp), shq(dst_name), shq(tmp), shq(tmp))
    out, err, code = _docker_exec(DB_CONTAINER, inner, timeout=600)
    if code != 0:
        inner2 = inner.replace('mysqldump ', 'mariadb-dump ').replace('mysql -uroot', 'mariadb -uroot')
        out, err, code = _docker_exec(DB_CONTAINER, inner2, timeout=600)
    if code != 0:
        raise RuntimeError('Copie de la base échouée : ' + ((err or out or '').strip()[-500:]))
    return True


def _app_clone(sid, new_name=None):
    data, site = _web_get(sid)
    src_root = (site.get('root') or '').rstrip('/')
    if not src_root or not os.path.isdir(src_root):
        raise ValueError('Ce site n\'a pas de dossier clonable.')
    base = WEB_ROOT_ALLOWED.rstrip('/')
    if not (src_root == base or src_root.startswith(base + '/')):
        raise PermissionError('Dossier hors zone autorisée.')
    parent = os.path.dirname(src_root)
    stem = os.path.basename(src_root)
    dst_root = os.path.join(parent, stem + '-clone')
    i = 2
    while os.path.exists(dst_root):
        dst_root = os.path.join(parent, '%s-clone%d' % (stem, i)); i += 1
    _sh_shutil.copytree(src_root, dst_root, symlinks=True)
    gid = int(WEB_WWW_GID) if str(WEB_WWW_GID).isdigit() else 82
    try:
        _app_fix_perms(dst_root, gid)
    except Exception as e:
        log.warning('clone fix perms: %s', e)
    app = _web_detect_app(site)
    new_db = None
    src_db = site.get('db') or {}
    if src_db.get('name'):
        new_db = _db_create()
        try:
            _db_clone(src_db['name'], new_db['name'])
        except Exception:
            try:
                _db_delete(new_db['name'], new_db['user'])
            except Exception:
                pass
            try:
                _sh_shutil.rmtree(dst_root)
            except Exception:
                pass
            raise
        if app:
            try:
                _app_rewrite_db_config(app, dst_root, new_db)
            except Exception as e:
                log.warning('clone db config rewrite: %s', e)
    nm = (new_name or (str(site.get('name', 'Site')) + ' (copie)'))[:48]
    newsite = {'name': nm, 'type': 'php', 'root': dst_root,
               'php_version': site.get('php_version') or WEB_PHP_DEFAULT}
    if app:
        newsite['app'] = app
    nsid, clean = _web_create(newsite)
    if new_db:
        d = _web_read()
        if nsid in d:
            d[nsid]['db'] = {'name': new_db['name'], 'user': new_db['user']}
            _web_write(d)
        clean['db'] = {'name': new_db['name'], 'user': new_db['user']}
    return {'ok': True, 'site': _web_public(clean, nsid), 'db': new_db,
            'src_root': src_root, 'dst_root': dst_root}


def _ps_table_prefix(root):
    import re as _re
    for rel in (('app', 'config', 'parameters.php'), ('config', 'settings.inc.php')):
        p = os.path.join((root or '').rstrip('/'), *rel)
        if os.path.isfile(p):
            try:
                s = open(p, encoding='utf-8', errors='replace').read()
            except OSError:
                continue
            m = _re.search(r"'database_prefix'\s*=>\s*'([^']*)'", s) or _re.search(r"_DB_PREFIX_'\s*,\s*'([^']*)'", s)
            if m:
                return m.group(1)
    return 'ps_'


def _joomla_prefix(root):
    import re as _re
    p = os.path.join((root or '').rstrip('/'), 'configuration.php')
    if os.path.isfile(p):
        try:
            s = open(p, encoding='utf-8', errors='replace').read()
            m = _re.search(r"\$dbprefix\s*=\s*'([^']*)'", s)
            if m:
                return m.group(1)
        except OSError:
            pass
    return 'jos_'


def _app_admin_reset(sid):
    data, site = _web_get(sid)
    app = _web_detect_app(site)
    if not app:
        raise ValueError("Application non reconnue.")
    root = (site.get('root') or '').rstrip('/')
    ver = site.get('php_version') or WEB_PHP_DEFAULT
    container = _php_container(ver)
    newpw = _sh_secrets.token_urlsafe(10)
    user = None
    if app == 'wordpress':
        phar = _wp_cli_ensure()
        wp = 'php ' + shq(phar) + ' --allow-root --path=' + shq(root) + ' '
        out, _e, _c = _docker_exec(container, wp + 'user list --role=administrator --field=user_login 2>/dev/null', timeout=180)
        lines = [l.strip() for l in (out or '').splitlines() if l.strip()]
        user = lines[0] if lines else 'admin'
        o2, e2, c2 = _docker_exec(container, wp + 'user update ' + shq(user) + ' --user_pass=' + shq(newpw) + ' 2>&1', timeout=180)
        if c2 != 0:
            raise RuntimeError((e2 or o2 or 'échec wp-cli').strip()[-400:])
    elif app == 'nextcloud':
        occ = 'php ' + shq(root + '/occ') + ' '
        out, _e, _c = _docker_exec(container, occ + 'user:list 2>/dev/null', timeout=120, user='www-data')
        for line in (out or '').splitlines():
            s = line.strip()
            if s.startswith('- '):
                user = s[2:].split(':')[0].strip()
                break
        user = user or 'admin'
        o2, e2, c2 = _docker_exec(container, 'OC_PASS=' + shq(newpw) + ' ' + occ + 'user:resetpassword --password-from-env ' + shq(user) + ' 2>&1', timeout=120, user='www-data')
        if c2 != 0:
            raise RuntimeError((e2 or o2 or 'échec occ').strip()[-400:])
    elif app in ('prestashop', 'joomla'):
        db = site.get('db') or {}
        if not db.get('name'):
            raise ValueError('Base de données du site inconnue.')
        hout, herr, hcode = _docker_exec(container, 'NP=' + shq(newpw) + ' php -r ' + shq("echo password_hash(getenv('NP'), PASSWORD_BCRYPT);"), timeout=60)
        phash = ((hout or '').strip().splitlines() or [''])[-1].strip()
        if not phash.startswith('$2'):
            raise RuntimeError('Impossible de générer le hash du mot de passe (PHP indisponible ?).')
        conn = _db_conn()
        try:
            cur = conn.cursor()
            cur.execute('USE `%s`' % db['name'])
            if app == 'prestashop':
                prefix = _ps_table_prefix(root)
                if not re.match(r'^[A-Za-z0-9_]{0,16}$', prefix or ''):
                    prefix = 'ps_'
                t = prefix + 'employee'
                cur.execute('SELECT email FROM `%s` WHERE id_profile=1 ORDER BY id_employee LIMIT 1' % t)
                r = cur.fetchone()
                if not r:
                    cur.execute('SELECT email FROM `%s` ORDER BY id_employee LIMIT 1' % t)
                    r = cur.fetchone()
                user = r[0] if r else 'admin@example.com'
                cur.execute('UPDATE `%s` SET passwd=%%s WHERE email=%%s' % t, (phash, user))
            else:
                prefix = _joomla_prefix(root)
                if not re.match(r'^[A-Za-z0-9_]{0,16}$', prefix or ''):
                    prefix = 'jos_'
                t = prefix + 'users'
                cur.execute('SELECT username FROM `%s` ORDER BY id LIMIT 1' % t)
                r = cur.fetchone()
                user = r[0] if r else 'admin'
                cur.execute('UPDATE `%s` SET password=%%s WHERE username=%%s' % t, (phash, user))
        finally:
            conn.close()
    else:
        raise ValueError('Réinitialisation non prise en charge pour cette application.')
    d = _web_read()
    if sid in d:
        d[sid]['admin'] = {'user': user, 'password': newpw}
        _web_write(d)
    return {'ok': True, 'app': app, 'admin': {'user': user, 'password': newpw},
            'login_url': site.get('login_url')}


def _web_admin_path(app, root):
    """Chemin (relatif) de l'interface d'administration selon l'appli.
    PrestaShop renomme son dossier admin -> on le détecte sur le disque."""
    root = (root or '').rstrip('/')
    if app == 'wordpress':
        return '/wp-admin/'
    if app == 'joomla':
        return '/administrator/'
    if app == 'phpmyadmin':
        return '/'
    if app == 'prestashop':
        try:
            for name in sorted(os.listdir(root)):
                if name.lower().startswith('admin') and os.path.isdir(os.path.join(root, name)) \
                        and os.path.exists(os.path.join(root, name, 'index.php')):
                    return '/' + name + '/'
        except OSError:
            pass
        return '/admin/'
    return '/'


def _web_credentials(sid):
    data, site = _web_get(sid)
    app = _web_detect_app(site)
    out = {'ok': True, 'id': sid, 'name': site.get('name'),
           'login_url': site.get('login_url'),
           'admin_path': _web_admin_path(app, site.get('root') or '') if app else '/',
           'admin': site.get('admin') or None, 'db': None,
           'app': app, 'can_reset': app in ('wordpress', 'nextcloud', 'prestashop', 'joomla')}
    dbinfo = site.get('db') or {}
    if dbinfo.get('name'):
        creds = _db_creds_read().get(dbinfo['name'])
        if creds:
            out['db'] = {'host': creds.get('host'), 'port': creds.get('port'),
                         'name': creds.get('name'), 'user': creds.get('user'),
                         'password': creds.get('password')}
        else:
            out['db'] = {'name': dbinfo['name'], 'user': dbinfo.get('user')}
    return out


def _app_prestashop_ssl(sid, enable=True):
    data, site = _web_get(sid)
    app = _web_detect_app(site)
    if app != 'prestashop':
        raise ValueError("Ce site n'est pas une boutique PrestaShop.")
    db = site.get('db') or {}
    if not db.get('name'):
        raise ValueError("Base de données du site inconnue — impossible de modifier le SSL.")
    prefix = _ps_table_prefix(site.get('root') or '')
    if not re.match(r'^[A-Za-z0-9_]{0,16}$', prefix or ''):
        prefix = 'ps_'
    table = prefix + 'configuration'
    val = '1' if enable else '0'
    conn = _db_conn()
    try:
        cur = conn.cursor()
        cur.execute('USE `%s`' % db['name'])
        updates = {'PS_SSL_ENABLED': val, 'PS_SSL_ENABLED_EVERYWHERE': val}
        sn = site.get('server_name') or ''
        if sn and enable:
            updates['PS_SHOP_DOMAIN_SSL'] = sn
        applied = []
        for k, v in updates.items():
            cur.execute('UPDATE `%s` SET value=%%s, date_upd=NOW() WHERE name=%%s' % table, (v, k))
            if cur.rowcount == 0:
                cur.execute('INSERT INTO `%s` (name, value, date_add, date_upd) VALUES (%%s,%%s,NOW(),NOW())' % table, (k, v))
            applied.append(k)
    finally:
        conn.close()
    _web_regenerate()  # s'assure que la map HTTPS + fastcgi param sont en place
    # Vide le cache PrestaShop : purge les URL mises en cache (rompt une boucle de redirection).
    root = (site.get('root') or '').rstrip('/')
    try:
        ver = site.get('php_version') or WEB_PHP_DEFAULT
        _docker_exec(_php_container(ver),
                     'rm -rf ' + shq(root + '/var/cache') + '/prod ' + shq(root + '/var/cache') + '/dev 2>/dev/null || true',
                     timeout=60)
    except Exception as e:
        log.warning('ps ssl cache clear: %s', e)
    msg = ('SSL activé' if enable else 'SSL désactivé') + ' dans PrestaShop (cache vidé).'
    if enable and sn:
        msg += ' Accédez à la boutique via https://' + sn + ' (via le reverse-proxy).'
    return {'ok': True, 'app': app, 'enabled': enable, 'db': db['name'],
            'prefix': prefix, 'applied': applied, 'message': msg}
# ── MDM-WEB-MANAGE-V1-END ────────────────────────────────────────────────────


# ── MDM-PREMIUM-V1 : comptes premium / débrideurs (résolution de liens) ─────
PREMIUM_FILE = os.path.join(ACCESS_DATA_DIR, 'premium.json')
_prem_lock = _threading.RLock()
_PREM_PROVIDERS = ('alldebrid', 'realdebrid', 'debridlink', 'premiumize', 'megadebrid',
                   'onefichier', 'rapidgator', 'nitroflare', 'ddownload')
# Débrideurs « généralistes » : résolvent des liens de n'importe quel hébergeur
# supporté (utilisés par le filet de sécurité auto ci-dessous).
_PREM_DEBRIDERS = ('alldebrid', 'realdebrid', 'debridlink', 'premiumize', 'megadebrid')
_PREM_HOSTERS = ('1fichier.com', 'rapidgator.net', 'uptobox.com', 'uptostream.com', 'turbobit.net',
                 'nitroflare.com', 'uploaded.net', 'ul.to', 'mediafire.com', 'katfile.com',
                 'fikper.com', 'ddownload.com', 'filefactory.com', 'wupfile.com', 'hexload.com',
                 'rapidgator.asia', 'mega.nz', 'usersdrive.com', 'file.al',
                 'turbobit.cc', 'trbt.cc', 'turb.cc', 'turb.pw', 'uploady.io',
                 'dailyuploads.io')


def _prem_read():
    with _prem_lock:
        d = _access_read_json(PREMIUM_FILE, {})
        return d if isinstance(d, dict) else {}


def _prem_save(d):
    with _prem_lock:
        _access_write_json(PREMIUM_FILE, d)


def _prem_set(provider, fields):
    if provider not in _PREM_PROVIDERS:
        raise ValueError('Fournisseur inconnu.')
    d = _prem_read()
    cur = d.get(provider) or {}
    for k, v in (fields or {}).items():
        if v in ('', None):
            continue
        cur[str(k)] = v
    d[provider] = cur
    _prem_save(d)
    return True


def _prem_remove(provider):
    d = _prem_read()
    if provider in d:
        del d[provider]
        _prem_save(d)
    return True


def _prem_configured(provider, cfg=None):
    c = (cfg or _prem_read()).get(provider) or {}
    return bool(c.get('apikey') or c.get('token') or (c.get('login') and c.get('password')))


def _prem_status():
    d = _prem_read()
    return {p: {'configured': _prem_configured(p, d)} for p in _PREM_PROVIDERS}


def _prem_get(provider):
    """Renvoie les champs enregistrés d'un fournisseur (pour l'édition/révélation).
    Accès protégé par le jeton fileops (NAS personnel, root)."""
    if provider not in _PREM_PROVIDERS:
        raise ValueError('Fournisseur inconnu.')
    c = _prem_read().get(provider) or {}
    return {k: c.get(k, '') for k in ('login', 'password', 'apikey', 'token')}


def _prem_http(method, url, headers=None, data=None, timeout=45):
    import urllib.request, urllib.parse
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    req.add_header('User-Agent', 'Desktroo')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace')
    except Exception as e:
        # tenter de lire le corps d'erreur JSON (HTTPError)
        body = getattr(e, 'read', None)
        if body:
            try:
                return json.loads(e.read().decode('utf-8', 'replace'))
            except Exception:
                pass
        raise
    try:
        return json.loads(raw)
    except Exception:
        return {'_raw': raw}


def _host_of(url):
    from urllib.parse import urlparse
    h = (urlparse(url).hostname or '').lower()
    return h[4:] if h.startswith('www.') else h


def _is_known_hoster(url):
    h = _host_of(url)
    return any(h == d or h.endswith('.' + d) for d in _PREM_HOSTERS)


def _resolve_alldebrid(url, cfg):
    import urllib.parse
    key = cfg.get('apikey')
    if not key:
        raise ValueError('AllDebrid non configuré.')
    j = _prem_http('GET', 'https://api.alldebrid.com/v4/link/unlock?agent=Desktroo&apikey='
                   + urllib.parse.quote(key) + '&link=' + urllib.parse.quote(url, safe=''))
    if j.get('status') == 'success':
        dd = j.get('data') or {}
        return {'link': dd.get('link'), 'filename': dd.get('filename'), 'size': dd.get('filesize') or 0}
    raise RuntimeError('AllDebrid: ' + str((j.get('error') or {}).get('message') or j))


def _resolve_realdebrid(url, cfg):
    token = cfg.get('token')
    if not token:
        raise ValueError('Real-Debrid non configuré.')
    j = _prem_http('POST', 'https://api.real-debrid.com/rest/1.0/unrestrict/link',
                   headers={'Authorization': 'Bearer ' + token}, data={'link': url})
    if j.get('download'):
        return {'link': j['download'], 'filename': j.get('filename'), 'size': j.get('filesize') or 0}
    raise RuntimeError('Real-Debrid: ' + str(j.get('error') or j))


def _resolve_debridlink(url, cfg):
    key = cfg.get('apikey')
    if not key:
        raise ValueError('Debrid-Link non configuré.')
    j = _prem_http('POST', 'https://debrid-link.com/api/v2/downloader/add',
                   headers={'Authorization': 'Bearer ' + key}, data={'url': url})
    if j.get('success') and j.get('value'):
        v = j['value']
        return {'link': v.get('downloadUrl'), 'filename': v.get('name'), 'size': v.get('size') or 0}
    raise RuntimeError('Debrid-Link: ' + str(j.get('error') or j))


def _resolve_onefichier(url, cfg):
    import urllib.request
    key = cfg.get('apikey')
    if not key:
        raise ValueError('1fichier non configuré.')
    req = urllib.request.Request('https://api.1fichier.com/v1/download/get_token.cgi',
                                 data=json.dumps({'url': url}).encode(), method='POST',
                                 headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                                          'User-Agent': 'Desktroo'})
    with urllib.request.urlopen(req, timeout=45) as r:
        j = json.loads(r.read().decode('utf-8', 'replace'))
    if j.get('status') == 'OK' and j.get('url'):
        return {'link': j['url'], 'filename': None, 'size': 0}
    raise RuntimeError('1fichier: ' + str(j.get('message') or j))


def _resolve_rapidgator(url, cfg):
    import urllib.parse
    login, pw = cfg.get('login'), cfg.get('password')
    if not (login and pw):
        raise ValueError('Rapidgator non configuré.')
    token, ts = cfg.get('token'), cfg.get('token_ts') or 0
    if not token or (_sh_time.time() - ts) > 3000:
        j = _prem_http('GET', 'https://rapidgator.net/api/v2/user/login?login='
                       + urllib.parse.quote(login) + '&password=' + urllib.parse.quote(pw))
        token = (j.get('response') or {}).get('token')
        if not token:
            raise RuntimeError('Rapidgator login: ' + str(j.get('details') or j))
        d = _prem_read()
        d.setdefault('rapidgator', {})
        d['rapidgator']['token'] = token
        d['rapidgator']['token_ts'] = int(_sh_time.time())
        _prem_save(d)
    j = _prem_http('GET', 'https://rapidgator.net/api/v2/file/download?token='
                   + urllib.parse.quote(token) + '&url=' + urllib.parse.quote(url, safe=''))
    dl = (j.get('response') or {}).get('download_url')
    if dl:
        return {'link': dl, 'filename': None, 'size': 0}
    raise RuntimeError('Rapidgator: ' + str(j.get('details') or j))


def _resolve_premiumize(url, cfg):
    key = cfg.get('apikey')
    if not key:
        raise ValueError('Premiumize non configuré.')
    j = _prem_http('POST', 'https://www.premiumize.me/api/transfer/directdl',
                   data={'apikey': key, 'src': url})
    if j.get('status') == 'success':
        if j.get('location'):
            return {'link': j['location'], 'filename': j.get('filename'), 'size': j.get('filesize') or 0}
        content = j.get('content') or []
        if content and content[0].get('link'):
            c = content[0]
            return {'link': c.get('link'), 'filename': c.get('path') or j.get('filename'), 'size': c.get('size') or 0}
    raise RuntimeError('Premiumize: ' + str(j.get('message') or j))


def _resolve_megadebrid(url, cfg):
    import urllib.parse
    login, pw = cfg.get('login'), cfg.get('password')
    if not (login and pw):
        raise ValueError('MegaDebrid non configuré.')
    token, ts = cfg.get('token'), cfg.get('token_ts') or 0
    if not token or (_sh_time.time() - ts) > 3000:
        j = _prem_http('GET', 'https://www.mega-debrid.eu/api.php?action=connectUser&login='
                       + urllib.parse.quote(login) + '&password=' + urllib.parse.quote(pw))
        if j.get('response_code') != 'ok' or not j.get('token'):
            raise RuntimeError('MegaDebrid login: ' + str(j.get('response_text') or j))
        token = j['token']
        d = _prem_read(); d.setdefault('megadebrid', {})
        d['megadebrid']['token'] = token; d['megadebrid']['token_ts'] = int(_sh_time.time()); _prem_save(d)
    j = _prem_http('POST', 'https://www.mega-debrid.eu/api.php?action=getLink&token=' + urllib.parse.quote(token),
                   data={'link': url})
    if j.get('response_code') == 'ok' and j.get('debridLink'):
        return {'link': str(j['debridLink']).strip().strip('"'), 'filename': j.get('filename'), 'size': 0}
    raise RuntimeError('MegaDebrid: ' + str(j.get('response_text') or j))


def _resolve_nitroflare(url, cfg):
    import urllib.parse, re as _re
    email, key = cfg.get('login'), cfg.get('password')
    if not (email and key):
        raise ValueError('Nitroflare non configuré.')
    m = _re.search(r'nitroflare\.com/(?:view|watch)/([A-Za-z0-9]+)', url) or _re.search(r'/([A-Za-z0-9]{10,})', url)
    fid = m.group(1) if m else url
    j = _prem_http('GET', 'https://nitroflare.com/api/v2/getDownloadLink?user='
                   + urllib.parse.quote(email) + '&premiumKey=' + urllib.parse.quote(key)
                   + '&file=' + urllib.parse.quote(fid))
    if j.get('type') == 'success':
        res = j.get('result') or {}
        if res.get('url'):
            return {'link': res['url'], 'filename': res.get('name'), 'size': res.get('size') or 0}
    raise RuntimeError('Nitroflare: ' + str(j.get('messages') or j.get('result') or j))


def _resolve_ddownload(url, cfg):
    import urllib.parse, re as _re
    key = cfg.get('apikey')
    if not key:
        raise ValueError('DDownload non configuré.')
    m = _re.search(r'ddownload\.com/(?:d/)?([A-Za-z0-9]+)', url)
    code = m.group(1) if m else url
    j = _prem_http('GET', 'https://ddownload.com/api/file/direct_link?key='
                   + urllib.parse.quote(key) + '&file_code=' + urllib.parse.quote(code))
    if str(j.get('status')) == '200':
        res = j.get('result') or {}
        if res.get('url'):
            return {'link': res['url'], 'filename': None, 'size': res.get('size') or 0}
    raise RuntimeError('DDownload: ' + str(j.get('msg') or j))


_PREM_RESOLVERS = {'alldebrid': _resolve_alldebrid, 'realdebrid': _resolve_realdebrid,
                   'debridlink': _resolve_debridlink, 'onefichier': _resolve_onefichier,
                   'rapidgator': _resolve_rapidgator, 'premiumize': _resolve_premiumize,
                   'megadebrid': _resolve_megadebrid, 'nitroflare': _resolve_nitroflare,
                   'ddownload': _resolve_ddownload}


def _resolve_premium(url, prefer=None):
    """Résout un lien via compte direct de l'hôte puis débrideurs configurés.
    Si `prefer` est un fournisseur précis (choisi par l'utilisateur), on l'utilise
    exclusivement. Retourne {'link','filename','size','via'} ou lève."""
    cfg = _prem_read()
    host = _host_of(url)
    order = []
    if prefer and prefer in _PREM_PROVIDERS:
        if not _prem_configured(prefer, cfg):
            raise RuntimeError('Le fournisseur choisi (' + prefer + ") n'est pas configuré.")
        order = [prefer]
    elif host.endswith('1fichier.com') and _prem_configured('onefichier', cfg):
        order.append('onefichier')
    if host.endswith('rapidgator.net') and _prem_configured('rapidgator', cfg):
        order.append('rapidgator')
    if host.endswith('nitroflare.com') and _prem_configured('nitroflare', cfg):
        order.append('nitroflare')
    if host.endswith('ddownload.com') and _prem_configured('ddownload', cfg):
        order.append('ddownload')
    for name in ('alldebrid', 'realdebrid', 'debridlink', 'premiumize', 'megadebrid'):
        if _prem_configured(name, cfg):
            order.append(name)
    if not order:
        raise RuntimeError('Aucun compte premium/débrideur configuré.')
    errs = []
    for name in order:
        try:
            r = _PREM_RESOLVERS[name](url, cfg.get(name) or {})
            if r.get('link'):
                r['via'] = name
                return r
            errs.append(name + ': réponse vide')
        except Exception as e:
            errs.append(str(e))
    raise RuntimeError('Résolution échouée — ' + ' | '.join(errs)[:400])


def _prem_test(provider):
    import urllib.parse, urllib.request
    cfg = _prem_read().get(provider) or {}
    if provider == 'alldebrid':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        j = _prem_http('GET', 'https://api.alldebrid.com/v4/user?agent=Desktroo&apikey=' + urllib.parse.quote(cfg['apikey']))
        if j.get('status') == 'success':
            u = (j.get('data') or {}).get('user') or {}
            return {'ok': True, 'account': u.get('username'), 'premium': bool(u.get('isPremium')), 'until': u.get('premiumUntil')}
        raise RuntimeError(str((j.get('error') or {}).get('message') or j))
    if provider == 'realdebrid':
        if not cfg.get('token'):
            raise ValueError('Token manquant.')
        j = _prem_http('GET', 'https://api.real-debrid.com/rest/1.0/user', headers={'Authorization': 'Bearer ' + cfg['token']})
        if j.get('username'):
            return {'ok': True, 'account': j.get('username'), 'premium': (j.get('type') == 'premium'), 'until': j.get('expiration')}
        raise RuntimeError(str(j.get('error') or j))
    if provider == 'debridlink':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        j = _prem_http('GET', 'https://debrid-link.com/api/v2/account/infos', headers={'Authorization': 'Bearer ' + cfg['apikey']})
        if j.get('success'):
            v = j.get('value') or {}
            return {'ok': True, 'account': v.get('email') or v.get('username'), 'premium': bool(v.get('premiumLeft')), 'until': v.get('premiumLeft')}
        raise RuntimeError(str(j.get('error') or j))
    if provider == 'onefichier':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        req = urllib.request.Request('https://api.1fichier.com/v1/user/info.cgi', data=b'{}', method='POST',
                                     headers={'Authorization': 'Bearer ' + cfg['apikey'], 'Content-Type': 'application/json', 'User-Agent': 'Desktroo'})
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode('utf-8', 'replace'))
        if j.get('email') or j.get('status') == 'OK':
            return {'ok': True, 'account': j.get('email'), 'premium': True, 'until': j.get('premium_until') or j.get('offer')}
        raise RuntimeError(str(j.get('message') or j))
    if provider == 'rapidgator':
        if not (cfg.get('login') and cfg.get('password')):
            raise ValueError('Identifiants manquants.')
        j = _prem_http('GET', 'https://rapidgator.net/api/v2/user/login?login='
                       + urllib.parse.quote(cfg['login']) + '&password=' + urllib.parse.quote(cfg['password']))
        resp = j.get('response') or {}
        if resp.get('token'):
            d = _prem_read()
            d.setdefault('rapidgator', {})
            d['rapidgator']['token'] = resp['token']
            d['rapidgator']['token_ts'] = int(_sh_time.time())
            _prem_save(d)
            u = resp.get('user') or {}
            return {'ok': True, 'account': u.get('email') or cfg['login'], 'premium': bool(u.get('is_premium')), 'until': u.get('premium_end_time')}
        raise RuntimeError(str(j.get('details') or j))
    if provider == 'premiumize':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        j = _prem_http('GET', 'https://www.premiumize.me/api/account/info?apikey=' + urllib.parse.quote(cfg['apikey']))
        if j.get('status') == 'success':
            return {'ok': True, 'account': j.get('customer_id'), 'premium': bool(j.get('premium_until')), 'until': j.get('premium_until')}
        raise RuntimeError(str(j.get('message') or j))
    if provider == 'megadebrid':
        if not (cfg.get('login') and cfg.get('password')):
            raise ValueError('Identifiants manquants.')
        j = _prem_http('GET', 'https://www.mega-debrid.eu/api.php?action=connectUser&login='
                       + urllib.parse.quote(cfg['login']) + '&password=' + urllib.parse.quote(cfg['password']))
        if j.get('response_code') == 'ok' and j.get('token'):
            d = _prem_read(); d.setdefault('megadebrid', {})
            d['megadebrid']['token'] = j['token']; d['megadebrid']['token_ts'] = int(_sh_time.time()); _prem_save(d)
            return {'ok': True, 'account': cfg['login'], 'premium': True, 'until': j.get('vip_end') or j.get('premium_left')}
        raise RuntimeError(str(j.get('response_text') or j))
    if provider == 'nitroflare':
        if not (cfg.get('login') and cfg.get('password')):
            raise ValueError('E‑mail + clé premium requis.')
        j = _prem_http('GET', 'https://nitroflare.com/api/v2/getKeyInfo?user='
                       + urllib.parse.quote(cfg['login']) + '&premiumKey=' + urllib.parse.quote(cfg['password']))
        if j.get('type') == 'success':
            res = j.get('result') or {}
            return {'ok': True, 'account': cfg['login'],
                    'premium': (str(res.get('status')).lower() in ('active', 'premium', '1', 'true')) or bool(res.get('expiryDate')),
                    'until': res.get('expiryDate')}
        raise RuntimeError(str(j.get('messages') or j))
    if provider == 'ddownload':
        if not cfg.get('apikey'):
            raise ValueError('Clé API manquante.')
        j = _prem_http('GET', 'https://ddownload.com/api/account/info?key=' + urllib.parse.quote(cfg['apikey']))
        if str(j.get('status')) == '200':
            res = j.get('result') or {}
            return {'ok': True, 'account': res.get('email'),
                    'premium': (str(res.get('premium')) in ('1', 'true')) or bool(res.get('premium_expire')),
                    'until': res.get('premium_expire')}
        raise RuntimeError(str(j.get('msg') or j))
    raise ValueError('Fournisseur inconnu.')
# ── MDM-PREMIUM-V1-END ───────────────────────────────────────────────────────


# ── MDM-DOWNLOADS-V1 : gestionnaire de téléchargements HTTP/HTTPS ────────────
DOWNLOAD_DIR = os.environ.get('DOWNLOAD_DIR', '/mnt/Truenas_Stockage/Downloads')
DOWNLOAD_ALLOWED_ROOT = os.environ.get('DOWNLOAD_ALLOWED_ROOT', '/mnt')
DOWNLOADS_FILE = os.path.join(ACCESS_DATA_DIR, 'downloads.json')
_DL_CHUNK = 256 * 1024
_DL_MAX_CONCURRENT = int(os.environ.get('DOWNLOAD_MAX_CONCURRENT', '3'))
_DL_CONNECTIONS = int(os.environ.get('DOWNLOAD_CONNECTIONS', '4'))   # sockets par lien (défaut)
_DL_MIN_SEG = 1024 * 1024   # taille min d'un segment (sinon on réduit le nb de connexions)
_dl_lock = _threading.RLock()
_dl_items = {}
_dl_threads = {}
_dl_flags = {}
class _ResizableSemaphore:
    """Sémaphore dont la capacité (nombre de jetons) est ajustable à chaud.
    Réduire la limite pendant des téléchargements en cours prend effet dès
    qu'un jeton se libère ; augmenter réveille aussitôt les threads en attente."""

    def __init__(self, value):
        self._cond = _threading.Condition(_threading.Lock())
        self._limit = int(value)
        self._in_use = 0

    def acquire(self, blocking=True, timeout=None):
        with self._cond:
            while self._in_use >= self._limit:
                if not blocking:
                    return False
                if not self._cond.wait(timeout):
                    return False
            self._in_use += 1
            return True

    def release(self):
        with self._cond:
            if self._in_use > 0:
                self._in_use -= 1
            self._cond.notify_all()

    def set_limit(self, n):
        with self._cond:
            self._limit = int(n)
            self._cond.notify_all()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


_dl_sema = _ResizableSemaphore(_DL_MAX_CONCURRENT)

# MDM-DL-SETTINGS-V1 : réglages persistants du gestionnaire de téléchargements.
# Le nombre de téléchargements simultanés est ajustable à chaud depuis l'UI
# (endpoint POST /downloads/settings) et persiste dans data/dl-settings.json.
DL_SETTINGS_FILE = os.path.join(ACCESS_DATA_DIR, 'dl-settings.json')
_DL_MAX_CONCURRENT_MIN = 1
_DL_MAX_CONCURRENT_MAX = 20


def _dl_settings_get():
    return {'max_concurrent': _DL_MAX_CONCURRENT,
            'default_connections': _DL_CONNECTIONS}


def _dl_settings_save():
    try:
        _access_write_json(DL_SETTINGS_FILE, _dl_settings_get())
    except Exception as e:
        log.warning('dl settings save: %s', e)


def _dl_set_max_concurrent(n):
    """Ajuste le nombre de téléchargements simultanés à chaud.
    Augmenter libère aussitôt des jetons du sémaphore ; diminuer prend effet
    au fur et à mesure que les téléchargements en cours se terminent."""
    global _DL_MAX_CONCURRENT
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ValueError('Nombre de téléchargements simultanés invalide.')
    n = max(_DL_MAX_CONCURRENT_MIN, min(_DL_MAX_CONCURRENT_MAX, n))
    with _dl_lock:
        _DL_MAX_CONCURRENT = n
    _dl_sema.set_limit(n)
    return n


def _dl_set_default_connections(n):
    global _DL_CONNECTIONS
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ValueError('Nombre de connexions par lien invalide.')
    _DL_CONNECTIONS = max(1, min(16, n))
    return _DL_CONNECTIONS


def _dl_settings_load():
    try:
        d = _access_read_json(DL_SETTINGS_FILE, {})
        if isinstance(d, dict):
            if d.get('max_concurrent') is not None:
                _dl_set_max_concurrent(d.get('max_concurrent'))
            if d.get('default_connections') is not None:
                _dl_set_default_connections(d.get('default_connections'))
    except Exception as e:
        log.warning('dl settings load: %s', e)


def _dl_sanitize_name(name):
    name = os.path.basename(str(name or '')).strip() or 'download'
    name = re.sub(r'[\\/:*?"<>|]+', '_', name)
    return name[:200]


def _dl_pick_path(dest_dir, filename):
    cand = os.path.join(dest_dir, filename)
    if not os.path.exists(cand) and not os.path.exists(cand + '.part'):
        return cand
    stem, ext = os.path.splitext(filename)
    i = 1
    while True:
        c = os.path.join(dest_dir, '%s-%d%s' % (stem, i, ext))
        if not os.path.exists(c) and not os.path.exists(c + '.part'):
            return c
        i += 1


def _dl_save():
    try:
        with _dl_lock:
            data = {k: dict(v) for k, v in _dl_items.items()}
        _access_write_json(DOWNLOADS_FILE, data)
    except Exception as e:
        log.warning('downloads save: %s', e)


def _dl_load():
    try:
        d = _access_read_json(DOWNLOADS_FILE, {})
        if isinstance(d, dict):
            with _dl_lock:
                for k, it in d.items():
                    if isinstance(it, dict):
                        if it.get('status') in ('downloading', 'queued'):
                            it['status'] = 'paused'  # à reprendre manuellement
                        it['speed'] = 0
                        _dl_items[k] = it
    except Exception as e:
        log.warning('downloads load: %s', e)


def _dl_probe(url):
    """Sonde l'URL (GET Range 0-0) : retourne (total, ranges_ok, filename_cd)."""
    import urllib.request
    from urllib.parse import unquote
    req = urllib.request.Request(url, headers={'User-Agent': 'Desktroo', 'Range': 'bytes=0-0'})
    with urllib.request.urlopen(req, timeout=45) as r:
        status = getattr(r, 'status', None) or r.getcode()
        cr = r.headers.get('Content-Range') or ''
        ar = (r.headers.get('Accept-Ranges') or '').lower()
        clen = r.headers.get('Content-Length')
        cd = r.headers.get('Content-Disposition') or ''
        ct = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
    fn = None
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
    if m:
        fn = _dl_sanitize_name(unquote(m.group(1)))
    if status == 206 and cr:
        mm = re.search(r'/(\d+)\s*$', cr)
        return (int(mm.group(1)) if mm else 0), True, fn, ct
    return (int(clen) if clen else 0), (ar == 'bytes'), fn, ct


_DL_CT_EXT = {
    'application/zip': '.zip', 'application/x-zip-compressed': '.zip',
    'application/x-rar-compressed': '.rar', 'application/vnd.rar': '.rar',
    'application/x-7z-compressed': '.7z', 'application/x-tar': '.tar',
    'application/gzip': '.gz', 'application/x-gzip': '.gz',
}


def _dl_has_ext(name):
    base = str(name or '')
    dot = base.rfind('.')
    if dot <= 0 or dot >= len(base) - 1:
        return False
    ext = base[dot + 1:]
    return 1 <= len(ext) <= 6 and ext.isalnum()


def _dl_maybe_rename(it, fn, ctype=None):
    """Donne un nom correct au fichier. Beaucoup d'hebergeurs resolus renvoient
    un lien direct dont le basename est un id sans extension (ex. p2211932672) :
    on prefere alors le nom fourni par Content-Disposition ; a defaut on deduit
    au moins l'extension du Content-Type, pour que l'extraction auto fonctionne."""
    cur = it.get('filename', '')
    if fn and (cur.startswith('download') or (not _dl_has_ext(cur) and _dl_has_ext(fn))):
        newpath = _dl_pick_path(it['dir'], fn)
        with _dl_lock:
            it['filename'] = os.path.basename(newpath)
            it['path'] = newpath
        cur = os.path.basename(newpath)
    if not _dl_has_ext(cur) and ctype in _DL_CT_EXT:
        newpath = _dl_pick_path(it['dir'], cur + _DL_CT_EXT[ctype])
        with _dl_lock:
            it['filename'] = os.path.basename(newpath)
            it['path'] = newpath


def _dl_single(it, fl, total):
    """Repli mono-connexion, reprise via .part."""
    import urllib.request
    path = it['path']
    part = path + '.part'
    existing = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(it['url'], headers={'User-Agent': 'Desktroo'})
    if existing > 0:
        req.add_header('Range', 'bytes=%d-' % existing)
    resp = urllib.request.urlopen(req, timeout=60)
    status = getattr(resp, 'status', None) or resp.getcode()
    clen = resp.headers.get('Content-Length')
    if status == 206 and existing > 0:
        total = existing + (int(clen) if clen else 0)
        mode = 'ab'
    else:
        total = int(clen) if clen else (total or 0)
        existing = 0
        mode = 'wb'
    with _dl_lock:
        it['total'] = total
        it['downloaded'] = existing
    downloaded = existing
    last_t = _sh_time.time()
    last_b = downloaded
    with open(part, mode) as f:
        while True:
            if fl.get('cancel'):
                with _dl_lock:
                    it['status'] = 'canceled'; it['speed'] = 0
                _dl_save()
                try:
                    f.close(); os.remove(part)
                except OSError:
                    pass
                return
            if fl.get('pause'):
                with _dl_lock:
                    it['status'] = 'paused'; it['speed'] = 0
                _dl_save()
                return
            chunk = resp.read(_DL_CHUNK)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            now = _sh_time.time()
            if now - last_t >= 0.5:
                spd = (downloaded - last_b) / (now - last_t)
                with _dl_lock:
                    it['downloaded'] = downloaded; it['speed'] = spd
                    it['eta'] = int((total - downloaded) / spd) if (spd > 0 and total) else 0
                last_t = now; last_b = downloaded
    os.replace(part, path)
    with _dl_lock:
        it['downloaded'] = downloaded
        if not total or downloaded > total:
            it['total'] = downloaded
        it['status'] = 'done'; it['speed'] = 0; it['eta'] = 0
    _dl_save()


def _dl_multi(it, fl, total, n):
    """Téléchargement segmenté : n connexions parallèles (requêtes Range) écrites
    DIRECTEMENT dans le fichier final à leur offset. Plus aucune passe de
    réassemblage en fin de téléchargement (gain majeur sur les gros fichiers)."""
    import urllib.request, glob
    path = it['path']
    meta = path + '.parts'          # progression par segment (pour la reprise)
    seg = (total + n - 1) // n
    errs = [None] * n

    # Bornes [start, end) de chaque segment.
    bounds = [(i * seg, min((i + 1) * seg, total)) for i in range(n)]

    # Reprise : réutilise le fichier existant + la progression sauvegardée.
    seg_bytes = [0] * n
    resume = False
    try:
        if os.path.exists(path) and os.path.getsize(path) == total and os.path.exists(meta):
            with open(meta, 'r') as mf:
                saved = json.load(mf)
            if isinstance(saved, list) and len(saved) == n:
                seg_bytes = [max(0, int(x)) for x in saved]
                resume = True
    except Exception:
        resume = False

    if not resume:
        # Pré-alloue le fichier final (fichier creux : instantané) pour pouvoir
        # écrire chaque segment à sa position définitive.
        with open(path, 'wb') as f:
            if total:
                f.truncate(total)
        seg_bytes = [0] * n
        # Nettoie d'éventuels anciens fragments .partN (ancien format).
        for old in glob.glob(path + '.part[0-9]*'):
            try:
                os.remove(old)
            except OSError:
                pass

    def _persist_meta():
        try:
            with open(meta, 'w') as mf:
                json.dump(seg_bytes, mf)
        except Exception:
            pass

    def _seg(i):
        start, end = bounds[i]
        done = seg_bytes[i]
        if start >= end or start + done >= end:
            return
        try:
            req = urllib.request.Request(it['url'], headers={
                'User-Agent': 'Desktroo',
                'Range': 'bytes=%d-%d' % (start + done, end - 1)})
            resp = urllib.request.urlopen(req, timeout=60)
            with open(path, 'r+b') as f:
                f.seek(start + done)
                while True:
                    if fl.get('cancel') or fl.get('pause'):
                        return
                    chunk = resp.read(_DL_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    seg_bytes[i] += len(chunk)
        except Exception as e:
            errs[i] = str(e)

    with _dl_lock:
        it['total'] = total
        it['downloaded'] = sum(seg_bytes)
    threads = []
    for i in range(n):
        t = _threading.Thread(target=_seg, args=(i,), daemon=True)
        threads.append(t)
        t.start()
    last_t = _sh_time.time()
    last_b = sum(seg_bytes)
    while any(t.is_alive() for t in threads):
        _sh_time.sleep(0.25)
        now = _sh_time.time()
        if now - last_t >= 0.5:
            downloaded = sum(seg_bytes)
            spd = (downloaded - last_b) / (now - last_t)
            with _dl_lock:
                it['downloaded'] = downloaded; it['speed'] = spd
                it['eta'] = int((total - downloaded) / spd) if (spd > 0 and total) else 0
            last_t = now
            last_b = downloaded
            _persist_meta()
    for t in threads:
        t.join()
    downloaded = sum(seg_bytes)
    with _dl_lock:
        it['downloaded'] = downloaded
        it['speed'] = 0
    if fl.get('cancel'):
        for pth in (path, meta):
            try:
                os.remove(pth)
            except OSError:
                pass
        with _dl_lock:
            it['status'] = 'canceled'
        _dl_save()
        return
    if fl.get('pause'):
        _persist_meta()
        with _dl_lock:
            it['status'] = 'paused'
        _dl_save()
        return
    if downloaded < total or any(errs):
        _persist_meta()
        msg = '; '.join([e for e in errs if e][:2]) or 'segments incomplets'
        raise RuntimeError('Téléchargement incomplet : ' + msg)
    # Terminé : le fichier final est déjà complet et à sa place — aucune
    # recopie/réassemblage. On retire simplement la métadonnée de progression.
    try:
        os.remove(meta)
    except OSError:
        pass
    with _dl_lock:
        it['status'] = 'done'
        it['eta'] = 0
    _dl_save()


def _dl_is_archive(name):
    n = str(name or '').lower()
    return n.endswith(('.zip', '.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz2',
                       '.tar.xz', '.txz', '.gz', '.bz2', '.xz', '.lzma',
                       '.rar', '.7z'))


def _dl_sniff_archive(path):
    """Detecte une archive par ses octets de tete (si l'extension manque ou ment)."""
    try:
        with open(path, 'rb') as fh:
            head = fh.read(8)
    except OSError:
        return False
    sigs = (b'PK\x03\x04', b'PK\x05\x06', b'Rar!\x1a\x07',
            b'7z\xbc\xaf\x27\x1c', b'\x1f\x8b', b'\xfd7zXZ\x00', b'BZh')
    return any(head.startswith(s) for s in sigs)


def _dl_extract_external(src, d):
    """Extrait rar/7z via un binaire externe. Essaie tous les outils disponibles
    (7z/7za/bsdtar/unrar/unar) jusqu'à réussite. Ces outils assainissent les
    chemins (anti path-traversal). Retourne le dossier de destination."""
    import shutil as _sh, subprocess as _sp
    low = src.lower()
    stem = os.path.splitext(os.path.basename(src))[0]
    dest = os.path.join(d, stem or 'extrait')
    k = 1
    while os.path.exists(dest):
        dest = os.path.join(d, (stem or 'extrait') + '.' + str(k)); k += 1
    os.makedirs(dest, exist_ok=True)

    def run(cmd):
        p = _sp.run(cmd, stdout=_sp.PIPE, stderr=_sp.STDOUT)
        return p.returncode, (p.stdout or b'').decode('utf-8', 'replace')

    cands = []
    if _sh.which('7z'):
        cands.append(['7z', 'x', '-y', '-o' + dest, src])
    if _sh.which('7za'):
        cands.append(['7za', 'x', '-y', '-o' + dest, src])
    if not low.endswith('.rar') and _sh.which('7zr'):
        cands.append(['7zr', 'x', '-y', '-o' + dest, src])
    if _sh.which('bsdtar'):
        cands.append(['bsdtar', '-x', '-f', src, '-C', dest])
    if low.endswith('.rar') and _sh.which('unrar'):
        cands.append(['unrar', 'x', '-y', '-o+', src, dest + os.sep])
    if _sh.which('unar'):
        cands.append(['unar', '-f', '-D', '-o', dest, src])

    if not cands:
        raise ValueError('Aucun outil (7z/bsdtar/unrar/unar) installé dans le conteneur.')
    last = ''
    for cmd in cands:
        try:
            rc, out = run(cmd)
        except Exception as e:
            last = cmd[0] + ': ' + str(e); continue
        if rc == 0 and os.path.isdir(dest) and os.listdir(dest):
            return dest
        last = cmd[0] + ' (code ' + str(rc) + ') : ' + out.strip()[-160:]
    raise ValueError('Échec extraction — ' + last)


def _dl_extract_archive(src):
    """Extrait une archive locale (zip/tar/gz/bz2/xz), protégé contre le zip-slip."""
    import zipfile as _zf, tarfile as _tf, shutil as _shutil
    if not os.path.isfile(src):
        raise ValueError('Fichier introuvable')
    d = os.path.dirname(src)

    def decide_dest(names):
        tops = set()
        for n in names:
            n = str(n).replace('\\', '/').lstrip('/')
            if n:
                tops.add(n.split('/')[0])
        if len(tops) == 1:
            return d
        low = src.lower()
        if low.endswith('.tar.gz'):
            stem = os.path.basename(src)[:-7]
        elif low.endswith('.tgz'):
            stem = os.path.basename(src)[:-4]
        else:
            stem = os.path.splitext(os.path.basename(src))[0]
        return os.path.join(d, stem or 'extrait')

    def do(names, extract_all):
        dest = decide_dest(names)
        os.makedirs(dest, exist_ok=True)
        dr = os.path.realpath(dest)
        for name in names:
            p = os.path.realpath(os.path.join(dr, name))
            if not (p == dr or p.startswith(dr + os.sep)):
                raise ValueError('Archive non sûre (zip-slip)')
        extract_all(dr)
        return dr

    if _zf.is_zipfile(src):
        with _zf.ZipFile(src) as z:
            return do(z.namelist(), z.extractall)
    if _tf.is_tarfile(src):
        with _tf.open(src) as t:
            return do(t.getnames(), t.extractall)
    low = src.lower()
    if low.endswith(('.rar', '.7z')):
        return _dl_extract_external(src, d)
    if low.endswith('.gz'):
        import gzip as _c; out = src[:-3]
    elif low.endswith('.bz2'):
        import bz2 as _c; out = src[:-4]
    elif low.endswith(('.xz', '.lzma')):
        import lzma as _c; out = os.path.splitext(src)[0]
    else:
        raise ValueError('Format non pris en charge (zip/tar/gz/bz2/xz).')
    cand = out or (src + '.out')
    k = 1
    while os.path.exists(cand):
        cand = out + '.' + str(k); k += 1
    with _c.open(src, 'rb') as fi, open(cand, 'wb') as fo:
        _shutil.copyfileobj(fi, fo)
    return cand


def _dl_postprocess(did):
    """Décompression et/ou retrait auto après un téléchargement terminé."""
    with _dl_lock:
        it = _dl_items.get(did)
    if not it:
        return
    if it.get('auto_extract') and (_dl_is_archive(it.get('filename', '')) or _dl_sniff_archive(it.get('path', ''))):
        with _dl_lock:
            it['status'] = 'extracting'; it['speed'] = 0
        _dl_save()
        try:
            _dl_extract_archive(it['path'])
        except Exception as e:
            with _dl_lock:
                it['error'] = ('extraction : ' + str(e))[:250]
        with _dl_lock:
            if it.get('status') == 'extracting':
                it['status'] = 'done'
        _dl_save()
    if it.get('auto_remove'):
        with _dl_lock:
            _dl_items.pop(did, None)
            _dl_flags.pop(did, None)
        _dl_save()


def _dl_worker(did):
    with _dl_sema:
        with _dl_lock:
            it = _dl_items.get(did)
            fl = _dl_flags.get(did)
        if not it or not fl or fl.get('cancel'):
            with _dl_lock:
                _dl_threads.pop(did, None)
            return
        try:
            with _dl_lock:
                it['status'] = 'downloading'
                it['error'] = ''
            _dl_save()
            total, ranges, fn, ctype = _dl_probe(it['url'])
            # Filet de sécurité : un hébergeur non reconnu répond souvent par une
            # page HTML au lieu du fichier. Si le lien n'a pas déjà été résolu et
            # qu'un débrideur généraliste est configuré (premium != off), on le
            # fait passer par le débrideur plutôt que d'enregistrer la page HTML.
            _dl_prefer = it.get('premium') if it.get('premium') in _PREM_PROVIDERS else None
            if (not it.get('via')
                    and str(it.get('premium') or 'auto') != 'off'
                    and 'html' in (ctype or '')
                    and (_dl_prefer is not None
                         or any(_prem_configured(pr) for pr in _PREM_DEBRIDERS))):
                r = _resolve_premium(it.get('source_url') or it['url'], prefer=_dl_prefer)
                newname = _dl_sanitize_name(r['filename']) if r.get('filename') else None
                with _dl_lock:
                    it['url'] = r['link']
                    it['via'] = r.get('via')
                    if r.get('size'):
                        it['total'] = r['size']
                    if newname:
                        newpath = _dl_pick_path(it['dir'], newname)
                        it['filename'] = os.path.basename(newpath)
                        it['path'] = newpath
                _dl_save()
                total, ranges, fn, ctype = _dl_probe(it['url'])
            _dl_maybe_rename(it, fn, ctype)
            n = int(it.get('connections') or _DL_CONNECTIONS)
            if ranges and total and total > _DL_MIN_SEG and n > 1:
                n = max(1, min(n, total // _DL_MIN_SEG))
                with _dl_lock:
                    it['connections'] = n
                _dl_multi(it, fl, total, n)
            else:
                with _dl_lock:
                    it['connections'] = 1
                _dl_single(it, fl, total)
            with _dl_lock:
                _st = (_dl_items.get(did) or {}).get('status')
            if _st == 'done':
                _dl_postprocess(did)
        except Exception as e:
            with _dl_lock:
                cur = _dl_items.get(did)
                if cur:
                    cur['status'] = 'error'
                    cur['error'] = str(e)[:300]
                    cur['speed'] = 0
            _dl_save()
        finally:
            with _dl_lock:
                _dl_threads.pop(did, None)


def _dl_start_thread(did):
    with _dl_lock:
        if did in _dl_threads:
            return
        _dl_flags[did] = {'pause': False, 'cancel': False}
        t = _threading.Thread(target=_dl_worker, args=(did,), daemon=True)
        _dl_threads[did] = t
    t.start()


def _dl_add(url, dest_dir=None, filename=None, premium='auto', connections=None,
            auto_extract=False, auto_remove=False):
    url = str(url or '').strip()
    if not re.match(r'^https?://', url, re.I):
        raise ValueError('URL invalide (http/https attendu).')
    dest_dir = os.path.realpath(dest_dir or DOWNLOAD_DIR)
    base = os.path.realpath(DOWNLOAD_ALLOWED_ROOT).rstrip('/')
    if not (dest_dir == base or dest_dir.startswith(base + '/')):
        raise PermissionError('Destination hors zone autorisée (%s).' % DOWNLOAD_ALLOWED_ROOT)
    os.makedirs(dest_dir, exist_ok=True)
    # Résolution premium (débrideur / compte direct) si demandé.
    source_url = url
    via = None
    resolved_name = None
    resolved_size = 0
    premium = str(premium or 'auto')
    prefer = premium if premium in _PREM_PROVIDERS else None
    if premium != 'off':
        want = (premium == 'force') or (prefer is not None) or _is_known_hoster(url)
        if prefer is not None:
            has_provider = _prem_configured(prefer)
        else:
            has_provider = any(_prem_configured(p) for p in _PREM_PROVIDERS)
        if want and has_provider:
            r = _resolve_premium(url, prefer=prefer)  # peut lever -> remonte à l'appelant
            url = r['link']
            via = r.get('via')
            resolved_name = r.get('filename')
            resolved_size = r.get('size') or 0
        elif want and prefer is not None:
            raise ValueError('Débrideur choisi (' + prefer + ") non configuré dans « Comptes premium ».")
        elif want and not has_provider:
            raise ValueError('Lien d\'hébergeur premium détecté mais aucun compte/débrideur configuré.')
    if not filename:
        from urllib.parse import urlparse, unquote
        filename = resolved_name or unquote(os.path.basename(urlparse(url).path)) or 'download'
    filename = _dl_sanitize_name(filename)
    path = _dl_pick_path(dest_dir, filename)
    did = _sh_secrets.token_hex(5)
    try:
        conn = int(connections) if connections else _DL_CONNECTIONS
    except (TypeError, ValueError):
        conn = _DL_CONNECTIONS
    conn = max(1, min(conn, 16))
    it = {'id': did, 'url': url, 'source_url': source_url, 'via': via, 'dir': dest_dir,
          'filename': os.path.basename(path), 'path': path,
          'total': resolved_size or 0, 'downloaded': 0, 'speed': 0, 'eta': 0,
          'connections': conn, 'premium': premium,
          'auto_extract': bool(auto_extract), 'auto_remove': bool(auto_remove),
          'status': 'queued', 'error': '', 'added_at': int(_sh_time.time())}
    with _dl_lock:
        _dl_items[did] = it
    _dl_save()
    _dl_start_thread(did)
    return it


def _dl_add_batch(urls, dest_dir=None, premium='auto', connections=None,
                  auto_extract=False, auto_remove=False):
    """Ajoute plusieurs URLs d'un coup. Retourne un résultat par URL (une erreur
    sur un lien n'empêche pas les autres)."""
    out = []
    seen = set()
    for u in (urls or []):
        u = str(u or '').strip()
        if not u or u in seen:
            continue
        seen.add(u)
        try:
            it = _dl_add(u, dest_dir, None, premium, connections, auto_extract, auto_remove)
            out.append({'url': u, 'ok': True, 'id': it['id']})
        except Exception as e:
            out.append({'url': u, 'ok': False, 'error': str(e)})
    return out


def _dl_pause(did):
    with _dl_lock:
        fl = _dl_flags.get(did)
        it = _dl_items.get(did)
    if not it:
        raise ValueError('Téléchargement introuvable.')
    if fl:
        fl['pause'] = True
    return {'ok': True}


def _dl_resume(did):
    with _dl_lock:
        it = _dl_items.get(did)
        if not it:
            raise ValueError('Téléchargement introuvable.')
        if it['status'] in ('downloading', 'done'):
            return {'ok': True}
        it['status'] = 'queued'
        it['error'] = ''
    _dl_start_thread(did)
    return {'ok': True}


def _dl_cancel(did, delete=True):
    with _dl_lock:
        fl = _dl_flags.get(did)
        it = _dl_items.get(did)
    if not it:
        return {'ok': True}
    if fl:
        fl['cancel'] = True
    if did not in _dl_threads:  # pas en cours -> nettoyer soi-même
        with _dl_lock:
            it['status'] = 'canceled'
            it['speed'] = 0
        if delete:
            # multi-part non terminé : fichier final partiel + métadonnée .parts.
            incomplete_multi = (it.get('status') != 'done'
                                and os.path.exists(it['path'] + '.parts'))
            for suffix in ('.part', '.parts'):
                try:
                    os.remove(it['path'] + suffix)
                except OSError:
                    pass
            if incomplete_multi:
                try:
                    os.remove(it['path'])
                except OSError:
                    pass
        _dl_save()
    return {'ok': True}


def _dl_remove(did):
    _dl_cancel(did, delete=True)
    with _dl_lock:
        _dl_items.pop(did, None)
        _dl_flags.pop(did, None)
    _dl_save()
    return {'ok': True}


def _dl_clear():
    with _dl_lock:
        rem = [k for k, v in _dl_items.items() if v.get('status') in ('done', 'error', 'canceled')]
        for k in rem:
            _dl_items.pop(k, None)
            _dl_flags.pop(k, None)
    _dl_save()
    return {'ok': True, 'removed': len(rem)}


def _dl_list():
    with _dl_lock:
        items = [dict(v) for v in _dl_items.values()]
    items.sort(key=lambda x: x.get('added_at', 0), reverse=True)
    return {'ok': True, 'downloads': items, 'default_dir': DOWNLOAD_DIR,
            'max_concurrent': _DL_MAX_CONCURRENT, 'default_connections': _DL_CONNECTIONS}


_dl_load()
_dl_settings_load()
# ── MDM-DOWNLOADS-V1-END ─────────────────────────────────────────────────────


# ── MDM-LICENSE-V1 : essai + abonnement (vérif RSA-SHA256 PKCS#1 v1.5, pur-Python) ──
import base64 as _lic_b64
import hashlib as _lic_hashlib
import hmac as _lic_hmac
import time as _lic_time

TRIAL_DAYS    = int(os.environ.get('TRIAL_DAYS', '30'))
GRACE_DAYS    = int(os.environ.get('GRACE_DAYS', '3'))   # tolérance après expiration d'un abonnement payant
LICENSE_PRICE = os.environ.get('LICENSE_PRICE', '3.99')
LICENSE_FILE  = os.path.join(ACCESS_DATA_DIR, 'license.json')
TRIAL_FILE    = os.path.join(ACCESS_DATA_DIR, 'trial.json')
ACTIVATION_KEY_FILE = os.path.join(ACCESS_DATA_DIR, 'activation-key')
LIC_REFRESH_FILE    = os.path.join(ACCESS_DATA_DIR, 'license-refresh.json')

# Serveur de licences (émission/refresh en ligne) + liens Stripe.
# SUBSCRIBE_URL (Payment Link) et MANAGE_URL (portail client) = liens LIVE.
LICENSE_SERVER = os.environ.get('LICENSE_SERVER', 'https://desktroo.fr/wp-json/desktroo/v1').rstrip('/')
SUBSCRIBE_URL  = os.environ.get('SUBSCRIBE_URL', 'https://buy.stripe.com/8x2fZgevw0LHcK33AH3840i')
MANAGE_URL     = os.environ.get('MANAGE_URL', 'https://billing.stripe.com/p/login/bJe4gy5Z0eCxbFZdbh38400')
LIC_REFRESH_INTERVAL = int(os.environ.get('LIC_REFRESH_INTERVAL', '43200'))  # 12 h entre deux refresh auto

# Clé publique de licence (RSA-2048). La clé privée reste hors dépôt chez l'éditeur.
LICENSE_PUBKEY_N_HEX = (
    "B92F624CA18A65506AE6D78AEC008F9E561C3C6BD591658303FD0892B914B2C4"
    "33D615D27A8B1775D4F2668EC8CFCAB9A4B6741253B124B7D14F0655C0C4E48C7"
    "525CA1D31417EED0E50C53F7CE562880E05AA0B04C909708BC13BA85FC424DE29"
    "8CA9FA0239CA78A2DBECCA2EC8C31CA0545B3677668163F83846C8A081510EBFB"
    "8B578000BB37F77DC584634DC23754EDB835BED0FCF4AD200142FE7C92DF5F6AF"
    "A38E21A902CF426D3930D673145C10E92FE56D66627B60A62635001F32E3C3092"
    "535A87280C87E8C406B6926C55396193DACD2D3A1DC138EB62E66EE0384E2B3D5"
    "3A68993595E969B6968B8C9617BBA1CF575886154E97EEADDD12986341")
_LIC_N = int(LICENSE_PUBKEY_N_HEX, 16)
_LIC_E = 65537
_LIC_SHA256_DI = bytes.fromhex('3031300d060960864801650304020105000420')


def _lic_b64url_dec(s):
    s = s + '=' * (-len(s) % 4)
    return _lic_b64.urlsafe_b64decode(s.encode('ascii'))


def _lic_verify(token):
    """Retourne le payload (dict) si la signature est valide, sinon None.
    Ne vérifie PAS l'expiration (l'appelant compare exp/now)."""
    try:
        p_b64, s_b64 = token.strip().split('.')
        payload = _lic_b64url_dec(p_b64)
        sig = _lic_b64url_dec(s_b64)
    except Exception:
        return None
    k = (_LIC_N.bit_length() + 7) // 8
    if len(sig) > k:
        return None
    if len(sig) < k:
        sig = b'\x00' * (k - len(sig)) + sig
    try:
        em = pow(int.from_bytes(sig, 'big'), _LIC_E, _LIC_N).to_bytes(k, 'big')
    except Exception:
        return None
    t = _LIC_SHA256_DI + _lic_hashlib.sha256(payload).digest()
    ps_len = k - 3 - len(t)
    if ps_len < 8:
        return None
    expected = b'\x00\x01' + b'\xff' * ps_len + b'\x00' + t
    if not _lic_hmac.compare_digest(em, expected):
        return None
    try:
        return json.loads(payload)
    except Exception:
        return None


INSTALL_ID_FILE = os.path.join(ACCESS_DATA_DIR, 'install-id')


def _lic_compute_install_id():
    """Ancienne logique deterministe — sert uniquement de graine initiale."""
    parts = []
    try:
        if SSH_HOST:
            parts.append(str(SSH_HOST))
    except Exception:
        pass
    for p in ('/etc/machine-id', '/data/machine-id'):
        try:
            with open(p) as fh:
                parts.append(fh.read().strip())
                break
        except Exception:
            pass
    try:
        parts.append(os.uname().nodename)
    except Exception:
        pass
    # NB : 'truenas-desktop' est une graine de hachage historique, pas un nom affiche.
    # Ne PAS la renommer : elle entre dans la derivation de l'install_id (licences liees).
    raw = '|'.join([x for x in parts if x]) or 'truenas-desktop'
    return _lic_hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def _lic_install_id():
    """Identifiant d'installation STABLE, persiste dans le volume de donnees.
    Genere une seule fois puis relu : survit a la recreation du conteneur (et
    donc aux mises a jour), contrairement au hostname du conteneur qui change
    a chaque recreation. ACCESS_DATA_DIR est un volume hote (/mnt/...)."""
    try:
        with open(INSTALL_ID_FILE, 'r', encoding='utf-8') as fh:
            v = fh.read().strip()
            if v:
                return v
    except Exception:
        pass
    v = _lic_compute_install_id()
    try:
        os.makedirs(os.path.dirname(INSTALL_ID_FILE), exist_ok=True)
        with open(INSTALL_ID_FILE, 'w', encoding='utf-8') as fh:
            fh.write(v)
    except Exception as e:
        log.warning('install-id persist failed: %s', e)
    return v


def _lic_read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception:
        return None


def _lic_write_json(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(obj, fh)
        return True
    except Exception as e:
        log.warning('license write failed %s: %s', path, e)
        return False


def _lic_trial_start():
    rec = _lic_read_json(TRIAL_FILE)
    if rec and isinstance(rec.get('start'), (int, float)):
        return int(rec['start'])
    now = int(_lic_time.time())
    _lic_write_json(TRIAL_FILE, {'start': now})
    return now


def license_status():
    now = int(_lic_time.time())
    install = _lic_install_id()
    rec = _lic_read_json(LICENSE_FILE)
    if rec and rec.get('token'):
        p = _lic_verify(rec['token'])
        if p and int(p.get('exp', 0)) > now and (not p.get('install_id') or p.get('install_id') == install):
            exp = int(p['exp'])
            return {'state': 'active', 'plan': p.get('plan', 'monthly'), 'exp': exp,
                    'days_left': max(0, (exp - now) // 86400), 'customer': p.get('customer', ''),
                    'readonly': False, 'install_id': install, 'price': LICENSE_PRICE}
        if p and int(p.get('exp', 0)) <= now:
            exp = int(p.get('exp', 0))
            grace_end = exp + GRACE_DAYS * 86400
            if GRACE_DAYS > 0 and now < grace_end:
                return {'state': 'grace', 'plan': p.get('plan', 'monthly'), 'exp': exp,
                        'grace_end': grace_end, 'days_left': max(0, (grace_end - now + 86399) // 86400),
                        'customer': p.get('customer', ''), 'readonly': False,
                        'install_id': install, 'price': LICENSE_PRICE}
            return {'state': 'expired', 'plan': p.get('plan', 'monthly'), 'exp': exp,
                    'days_left': 0, 'customer': p.get('customer', ''), 'readonly': True,
                    'install_id': install, 'price': LICENSE_PRICE}
    start = _lic_trial_start()
    exp = start + TRIAL_DAYS * 86400
    if now < exp:
        return {'state': 'trial', 'plan': 'trial', 'exp': exp,
                'days_left': max(0, (exp - now + 86399) // 86400), 'readonly': False,
                'install_id': install, 'price': LICENSE_PRICE}
    return {'state': 'expired', 'plan': 'trial', 'exp': exp, 'days_left': 0,
            'readonly': True, 'install_id': install, 'price': LICENSE_PRICE}


def license_activate(token):
    now = int(_lic_time.time())
    install = _lic_install_id()
    p = _lic_verify(token)
    if not p:
        return (400, {'ok': False, 'error': 'Jeton invalide (signature non reconnue).'})
    if int(p.get('exp', 0)) <= now:
        return (400, {'ok': False, 'error': 'Jeton expiré.'})
    if p.get('install_id') and p.get('install_id') != install:
        return (400, {'ok': False, 'error': 'Jeton lié à une autre installation.'})
    _lic_write_json(LICENSE_FILE, {'token': token})
    return (200, {'ok': True, 'status': license_status()})


def _lic_server_post(ep, payload, timeout=20):
    """POST JSON vers le serveur de licences. Retourne un dict (corps JSON) ou None."""
    import urllib.request
    url = LICENSE_SERVER + '/' + ep.lstrip('/')
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST',
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': 'Desktroo'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        body = getattr(e, 'read', None)
        if body:
            try:
                return json.loads(e.read().decode('utf-8', 'replace'))
            except Exception:
                pass
        log.warning('license server call failed (%s): %s', ep, e)
        return None


def _lic_save_activation_key(key):
    try:
        os.makedirs(os.path.dirname(ACTIVATION_KEY_FILE), exist_ok=True)
        with open(ACTIVATION_KEY_FILE, 'w', encoding='utf-8') as fh:
            fh.write((key or '').strip())
    except Exception as e:
        log.warning('activation-key persist failed: %s', e)


def _lic_read_activation_key():
    try:
        with open(ACTIVATION_KEY_FILE, 'r', encoding='utf-8') as fh:
            return fh.read().strip()
    except Exception:
        return ''


def license_activate_key(key):
    """Active via le serveur : clé d'activation -> liaison install_id -> jeton signé."""
    key = (key or '').strip().upper()
    if not key:
        return (400, {'ok': False, 'error': "Saisissez une clé d'activation."})
    install = _lic_install_id()
    resp = _lic_server_post('activate', {'activation_key': key, 'install_id': install})
    if resp is None:
        return (502, {'ok': False, 'error': 'Serveur de licences injoignable. Vérifiez la connexion et réessayez.'})
    if not resp.get('ok') or not resp.get('token'):
        return (400, {'ok': False, 'error': resp.get('error', "Échec de l'activation.")})
    code, out = license_activate(str(resp['token']))
    if code == 200 and out.get('ok'):
        _lic_save_activation_key(key)
        _lic_write_json(LIC_REFRESH_FILE, {'ts': int(_lic_time.time())})
    return (code, out)


def license_do_refresh():
    """Rafraîchit le jeton depuis le serveur si une clé d'activation est connue."""
    key = _lic_read_activation_key()
    if not key:
        return False
    install = _lic_install_id()
    resp = _lic_server_post('refresh', {'activation_key': key, 'install_id': install})
    _lic_write_json(LIC_REFRESH_FILE, {'ts': int(_lic_time.time())})
    if resp and resp.get('ok') and resp.get('token'):
        code, _out = license_activate(str(resp['token']))
        return code == 200
    return False


def license_maybe_refresh():
    """Déclenche un refresh en tâche de fond, au plus une fois par LIC_REFRESH_INTERVAL."""
    if not _lic_read_activation_key():
        return
    rec = _lic_read_json(LIC_REFRESH_FILE) or {}
    last = rec.get('ts', 0)
    last = int(last) if isinstance(last, (int, float)) else 0
    now = int(_lic_time.time())
    if now - last < LIC_REFRESH_INTERVAL:
        return
    _lic_write_json(LIC_REFRESH_FILE, {'ts': now})  # jalon posé tout de suite (évite les doublons)
    try:
        import threading
        threading.Thread(target=license_do_refresh, daemon=True).start()
    except Exception:
        try:
            license_do_refresh()
        except Exception:
            pass


def _lic_is_readonly():
    try:
        return bool(license_status().get('readonly'))
    except Exception:
        return False


# Routes POST dont l'écriture reste autorisée même en lecture seule.
_LIC_READONLY_ALLOW_POST = {'/license/activate', '/license/activate-key', '/license/refresh'}


# ── MDM-WALLPAPERS-V1 : dossier de fonds d'écran ─────────────────────────────
# Les images vivent dans <install>/wallpapers (volume hôte, donc conservées aux
# mises à jour). Le bureau les liste, les lit et en ajoute par les routes
# /wallpapers* ; on peut aussi y déposer des fichiers directement.
WALLPAPER_DIR = os.environ.get('WALLPAPER_DIR') or (os.path.join(APP_DIR, 'wallpapers') if APP_DIR else '')
WALLPAPER_MAX_BYTES = int(os.environ.get('WALLPAPER_MAX_BYTES', str(20 * 1024 * 1024)))
_WALLPAPER_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.webp': 'image/webp', '.avif': 'image/avif', '.gif': 'image/gif',
}


def _wallpaper_dir(create=False):
    """Chemin réel du dossier de fonds d'écran ; le crée à la demande."""
    if not WALLPAPER_DIR:
        raise RuntimeError("Dossier de fonds d'écran indisponible (APP_DIR non défini).")
    if create:
        os.makedirs(WALLPAPER_DIR, exist_ok=True)
    return os.path.realpath(WALLPAPER_DIR)


def _wallpaper_name(name):
    """Valide un nom de fichier : pas de sous-dossier, pas de fichier caché, image connue."""
    name = os.path.basename(str(name or '').replace('\\', '/')).strip()
    if not name or name.startswith('.') or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError('Nom de fichier invalide.')
    if os.path.splitext(name)[1].lower() not in _WALLPAPER_TYPES:
        raise ValueError('Format non pris en charge : JPG, PNG, WebP, AVIF ou GIF.')
    return name


def _wallpaper_path(name):
    """Chemin d'une image existante du dossier, sans jamais en sortir (liens compris)."""
    base = _wallpaper_dir()
    full = os.path.realpath(os.path.join(base, _wallpaper_name(name)))
    if os.path.dirname(full) != base or not os.path.isfile(full):
        raise FileNotFoundError('Image introuvable.')
    return full


def _wallpaper_item(full):
    st = os.stat(full)
    return {'name': os.path.basename(full), 'size': st.st_size, 'mtime': int(st.st_mtime)}


def _wallpaper_list():
    base = _wallpaper_dir(create=True)
    items = []
    for fn in os.listdir(base):
        full = os.path.join(base, fn)
        if fn.startswith('.') or os.path.splitext(fn)[1].lower() not in _WALLPAPER_TYPES:
            continue
        # isfile suit les liens : on écarte ceux qui pointent hors du dossier.
        if os.path.isfile(full) and os.path.dirname(os.path.realpath(full)) == base:
            items.append(_wallpaper_item(full))
    items.sort(key=lambda it: it['name'].lower())
    return {'ok': True, 'dir': WALLPAPER_DIR, 'max_bytes': WALLPAPER_MAX_BYTES, 'items': items}


def _wallpaper_free_name(base, name):
    """Évite d'écraser une image existante : photo.jpg → photo-2.jpg."""
    stem, ext = os.path.splitext(name)
    candidate, n = name, 2
    while os.path.exists(os.path.join(base, candidate)):
        candidate = '%s-%d%s' % (stem, n, ext)
        n += 1
    return candidate


class FileOpsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _auth(self):
        tok = (self.headers.get('X-Token', '')
               or self.headers.get('X-Fileops-Token', ''))
        if not _login_hmac.compare_digest(str(tok).encode('utf-8'), TOKEN.encode('utf-8')):
            self._json(403, {'error': 'Unauthorized'})
            return False
        return True

    def _auth_login(self):
        """POST /auth/login : seule route d'écriture ouverte sans jeton. La réponse ne
        porte pas d'en-tête CORS : une page d'un autre site ne peut pas la lire."""
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
            raw = self.rfile.read(min(n, 8192)) if n > 0 else b''
            code, resp = auth_login(json.loads(raw.decode('utf-8')) if raw else {})
        except Exception:
            code, resp = 400, {'error': 'Requête invalide.'}
        body = json.dumps(resp, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(n)) if n else {}

    # ── MDM-SHARE-LINKS-V1 : réponses HTML + streaming ──────────────────────
    def _html(self, code, markup):
        body = markup.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _stream_file(self, path, filename):
        try:
            size = os.path.getsize(path)
            ctype = _sh_mimetypes.guess_type(path)[0] or 'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(size))
            self.send_header('Content-Disposition', _share_content_disposition(filename))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            with open(path, 'rb') as f:
                _sh_shutil.copyfileobj(f, self.wfile, 256 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.warning('share stream error: %s', e)

    def _stream_zip(self, dirpath, name):
        tmpfd, tmpzip = _tempfile.mkstemp(suffix='.zip', prefix='share-')
        os.close(tmpfd)
        try:
            base = os.path.basename(dirpath.rstrip('/')) or 'archive'
            with _sh_zipfile.ZipFile(tmpzip, 'w', _sh_zipfile.ZIP_STORED, allowZip64=True) as zf:
                for root, _dirs, files in os.walk(dirpath):
                    for fn in files:
                        fp = os.path.join(root, fn)
                        arc = os.path.join(base, os.path.relpath(fp, dirpath))
                        try:
                            zf.write(fp, arc)
                        except OSError:
                            pass
            self._stream_file(tmpzip, (name or 'archive') + '.zip')
        finally:
            try:
                os.unlink(tmpzip)
            except OSError:
                pass

    def _share_public_get(self, pp):
        parts = [x for x in pp.path.split('/') if x]  # ['s', <token>, (download)]
        qs = dict(parse_qsl(pp.query))
        token = parts[1] if len(parts) >= 2 else ''
        action = parts[2] if len(parts) >= 3 else ''
        rec = _share_read().get(token)
        if not rec:
            return self._html(404, _share_error_page('Lien introuvable ou supprimé.'))
        if action == 'download':
            return self._serve_share_file(rec, qs.get('pw', ''), check=(qs.get('check') == '1'))
        valid, _reason = _share_valid(rec)
        return self._html(200 if valid else 410, _share_landing_page(rec))

    def _serve_share_file(self, rec, password, check=False):
        valid, reason = _share_valid(rec)
        if not valid:
            if check:
                return self._json(410, {'error': reason})
            return self._html(410, _share_error_page(reason))
        if rec.get('password') and not _share_verify_pw(password, rec['password']):
            return self._json(403, {'error': 'Mot de passe incorrect'})
        if check:
            return self._json(200, {'ok': True})
        # Incrément atomique + re-vérification de la limite sous verrou
        with _share_lock:
            data = _share_read()
            r2 = data.get(rec['token'])
            if not r2 or r2.get('revoked'):
                return self._html(410, _share_error_page('Lien révoqué.'))
            maxd = r2.get('max_downloads')
            if maxd and int(r2.get('downloads', 0)) >= int(maxd):
                return self._html(410, _share_error_page('Nombre maximum de téléchargements atteint.'))
            r2['downloads'] = int(r2.get('downloads', 0)) + 1
            data[rec['token']] = r2
            _share_write(data)
        if rec.get('is_dir'):
            self._stream_zip(rec['path'], rec.get('name', 'archive'))
        else:
            self._stream_file(rec['path'], rec.get('name', 'download'))

    def do_OPTIONS(self):
        if urlparse(self.path).path.rstrip('/') == '/auth/login':
            self.send_response(204)     # pas de CORS sur la connexion
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'X-Token, X-Fileops-Token, Content-Type')
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────────────────
    def do_GET(self):
        # MDM-SHARE-LINKS-V1 : routes publiques /s/<token> (sans token fileops)
        _pp = urlparse(self.path)
        if _pp.path == '/s' or _pp.path.startswith('/s/'):
            return self._share_public_get(_pp)

        if not self._auth():
            return
        p    = urlparse(self.path)
        path = p.path.rstrip('/')
        qs   = dict(parse_qsl(p.query))

        # MDM-LICENSE-V1 : état de la licence / essai
        if path == '/license/status':
            try:
                try:
                    license_maybe_refresh()   # refresh en ligne en tâche de fond (throttlé)
                except Exception:
                    pass
                st = license_status()
                st['subscribe_url'] = SUBSCRIBE_URL
                st['manage_url'] = MANAGE_URL
                self._json(200, st)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : liste des sites web (authentifié)
        if path == '/websites':
            try:
                data = _web_read()
                items = []
                for sid, s in data.items():
                    if not isinstance(s, dict):
                        continue
                    it = _web_public(s, sid)
                    if not it.get('app'):
                        try:
                            det = _web_detect_app(s)
                            if det:
                                it['app'] = det
                        except Exception:
                            pass
                    it['has_creds'] = bool(it.get('admin') or it.get('db') or it.get('login_url'))
                    it.pop('admin', None)  # secret : révélé seulement via /websites/credentials
                    items.append(it)
                items.sort(key=lambda x: x.get('created_at') or 0, reverse=True)
                self._json(200, {
                    'ok': True, 'sites': items,
                    'port_min': WEB_PORT_MIN, 'port_max': WEB_PORT_MAX,
                    'php_versions': sorted(WEB_PHP_VERSIONS.keys(), reverse=True),
                    'php_default': WEB_PHP_DEFAULT,
                    'web_dataset': _web_dataset_path(),
                    'proxy': _web_proxy_get(),
                })
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : logs d'un site (access/error, dernières lignes)
        if path == '/websites/logs':
            try:
                sid = qs.get('id', '')
                if not sid:
                    self._json(400, {'error': 'id requis'}); return
                _web_get(sid)  # valide l'existence
                kind = qs.get('kind', 'error')
                try:
                    n = int(qs.get('lines', '200'))
                except ValueError:
                    n = 200
                res = _web_tail_log(sid, kind, n)
                if not res.get('exists'):
                    # auto-réparation : régénère les confs pour activer les logs
                    try:
                        _web_regenerate()
                        res['regenerated'] = True
                    except Exception as e:
                        log.warning('logs regen: %s', e)
                self._json(200, res)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : identifiants d'un site (admin + base)
        if path == '/websites/credentials':
            try:
                sid = qs.get('id', '')
                if not sid:
                    self._json(400, {'error': 'id requis'}); return
                self._json(200, _web_credentials(sid))
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WALLPAPERS-V1 : liste des fonds d'écran du dossier
        if path == '/wallpapers':
            try:
                self._json(200, _wallpaper_list())
            except Exception as e:
                self._json(200, {'ok': False, 'items': [], 'error': str(e)})
            return
        # MDM-WALLPAPERS-V1 : contenu d'une image (le bureau la charge en blob)
        if path == '/wallpapers/file':
            try:
                full = _wallpaper_path(qs.get('name', ''))
                size = os.path.getsize(full)
                self.send_response(200)
                self.send_header('Content-Type', _WALLPAPER_TYPES[os.path.splitext(full)[1].lower()])
                self.send_header('Content-Length', str(size))
                self.send_header('X-Content-Type-Options', 'nosniff')
                # L'URL porte la date de modification (?v=) : le cache navigateur reste juste.
                self.send_header('Cache-Control', 'private, max-age=86400')
                self.end_headers()
                with open(full, 'rb') as fh:
                    _sh_shutil.copyfileobj(fh, self.wfile, 256 * 1024)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (FileNotFoundError, ValueError) as e:
                self._json(404, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SELF-UPDATE-V1 : version installée + dernière dispo
        if path == '/version':
            try:
                self._json(200, _version_status())
            except Exception as e:
                self._json(200, {'version': APP_VERSION, 'latest': '', 'update_available': False, 'error': str(e)})
            return

        # MDM-DOWNLOADS-V1 : liste des téléchargements
        if path == '/downloads/list':
            try:
                self._json(200, _dl_list())
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-PREMIUM-V1 : état des comptes premium configurés
        if path == '/premium/status':
            try:
                self._json(200, {'ok': True, 'providers': _prem_status()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-APPS-V1 : catalogue d'applications + bases de données
        if path == '/apps/catalog':
            cat = {}
            for k, m in APP_CATALOG.items():
                cat[k] = {'label': m['label'], 'db': bool(m.get('db')),
                          'php': m.get('php'), 'ext': m.get('ext', []), 'url': m['url']}
            self._json(200, {'ok': True, 'apps': cat, 'db_available': _HAS_PYMYSQL,
                             'install_profile': _install_profile_read()})
            return

        if path == '/db/list':
            try:
                self._json(200, {'ok': True, 'databases': _db_list()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/db/credentials':
            try:
                self._json(200, {'ok': True, 'credentials': _db_creds_read()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/proxy/npm/status':
            res = _npm_status()
            res['ok'] = True
            self._json(200, res)
            return

        # MDM-WEBSITES-V1 : profils PHP (ini + extensions)
        if path == '/php/profiles':
            try:
                data = _php_profiles_read()
                profs = {}
                for ver in WEB_PHP_VERSIONS:
                    p = data.get(ver, {})
                    profs[ver] = {
                        'ini': p.get('ini', {}) if isinstance(p.get('ini'), dict) else {},
                        'extensions': p.get('extensions', []) if isinstance(p.get('extensions'), list) else [],
                    }
                self._json(200, {
                    'ok': True,
                    'versions': sorted(WEB_PHP_VERSIONS.keys(), reverse=True),
                    'default': WEB_PHP_DEFAULT,
                    'profiles': profs,
                })
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SHARE-LINKS-V1 : liste des liens de partage (authentifié)
        if path == '/share/list':
            try:
                data = _share_purge()
                items = [_share_public(r) for r in data.values() if isinstance(r, dict)]
                items.sort(key=lambda x: x.get('created_at') or 0, reverse=True)
                self._json(200, {'ok': True, 'shares': items})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-LIBVIRT-REQUEST-GUARD-V1-BEGIN
        if _libvirt_path_requires_daemon(path):
            try:
                ensure_libvirtd()
            except Exception as error:
                self._json(503, {
                    'api': 'libvirt2' if path.startswith('/libvirt2') else 'libvirt',
                    'error': 'Service QEMU/libvirt indisponible : ' + str(error),
                    'autorecovery': 'failed',
                })
                return
        # MDM-LIBVIRT-REQUEST-GUARD-V1-END

        # MDM-ACCESS-POLICY-V1-GET-BEGIN
        if path == '/access/policy':
            try:
                self._json(200, {'ok': True, 'policy': access_policy_get()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/access/history':
            try:
                limit = int(qs.get('limit', 150) or 150)
                self._json(200, {
                    'ok': True,
                    'history': access_history_get(limit)
                })
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        # MDM-ACCESS-POLICY-V1-GET-END

        # MDM-LIBVIRT2-20260710
        # API V2 VM/QEMU/libvirt.
        # GET /libvirt2/vms
        # GET /libvirt2/vms/{name}
        # MDM-LIBVIRT2-INSTALLATION-ROUTES-20260710
        # GET /libvirt2/isos
        # MDM-LIBVIRT2-NETWORK-ROUTES-20260710
        # GET /libvirt2/networks
        if path == '/libvirt2/networks':
            try:
                self._json(200, {
                    'api': 'libvirt2',
                    'readonly': False,
                    'networks': _lv2_list_network_sources()
                })
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        if path == '/libvirt2/isos':
            try:
                self._json(200, {
                    'api': 'libvirt2',
                    'readonly': False,
                    'iso_dir': ISO_DIR,
                    'isos': _lv2_list_isos()
                })
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        if path == '/libvirt2/vms':
            try:
                detail = qs.get('detail', '0') in ('1', 'true', 'yes')
                names = _lv2_vm_names()
                if detail:
                    vms = []
                    for vm_name in names:
                        try:
                            vms.append(_lv2_get_vm(vm_name, include_xml=False))
                        except Exception as one_e:
                            vms.append({'api': 'libvirt2', 'name': vm_name, 'error': str(one_e)})
                else:
                    vms = [{'api': 'libvirt2', 'name': vm_name} for vm_name in names]
                self._json(200, {'api': 'libvirt2', 'version': 2, 'readonly': False, 'vms': vms})
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # MDM-LIBVIRT2-SNAPSHOTS-GET-FIX-20260710
        # GET /libvirt2/vms/{name}/snapshots
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                self._json(200, _lv2_snapshot_list(name))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        m = re.match(r'^/libvirt2/vms/([^/]+)$', path)
        if m:
            try:
                from urllib.parse import unquote
                vm_name = unquote(m.group(1))
                include_xml = qs.get('xml', '0') in ('1', 'true', 'yes')
                self._json(200, _lv2_get_vm(vm_name, include_xml=include_xml))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        if path == '/list':
            target = qs.get('path', '/')
            try:
                entries = []
                # os.scandir : type d'entrée sans stat (rapide sur gros dossiers,
                # ex. datasets Docker overlay). stat uniquement sur les fichiers.
                with os.scandir(target) as it:
                    for e in it:
                        try:
                            is_dir = e.is_dir(follow_symlinks=False)
                        except OSError:
                            is_dir = False
                        size = 0
                        mtime = 0
                        # Date de modification pour tout le monde (colonne « Modifié »), taille pour les fichiers.
                        try:
                            st = e.stat(follow_symlinks=False)
                            mtime = st.st_mtime
                            if not is_dir:
                                size = st.st_size
                        except OSError:
                            pass
                        entries.append({
                            'name':   e.name,
                            'is_dir': is_dir,
                            'size':   size,
                            'mtime':  mtime,
                        })
                entries.sort(key=lambda x: x['name'].lower())
                self._json(200, entries)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SEARCH-V1 : recherche de fichiers/dossiers par nom
        if path == '/search':
            base = qs.get('path', '/')
            q = (qs.get('q', '') or '').lower()
            try:
                limit = int(qs.get('limit', '500') or 500)
            except ValueError:
                limit = 500
            if not q:
                self._json(200, {'ok': True, 'results': []})
                return
            try:
                results = []
                truncated = False
                lines = None
                # 1) `find` (rapide, parcourt tout l'arbre) — recherche par nom, insensible à la casse
                try:
                    import subprocess
                    esc = ''.join(('\\' + c) if c in '*?[]\\' else c for c in q)
                    proc = subprocess.run(
                        ['find', base, '-iname', '*' + esc + '*'],
                        capture_output=True, text=True, timeout=45,
                    )
                    lines = [ln for ln in proc.stdout.splitlines() if ln]
                except Exception:
                    lines = None
                if lines is not None:
                    if len(lines) > limit:
                        truncated = True
                        lines = lines[:limit]
                    for full in lines:
                        if full == base:
                            continue
                        isd, sz, mt = False, 0, 0
                        try:
                            isd = os.path.isdir(full)
                            st = os.stat(full)
                            mt = st.st_mtime
                            sz = 0 if isd else st.st_size
                        except OSError:
                            pass
                        results.append({'name': os.path.basename(full.rstrip('/')),
                                        'path': full, 'is_dir': isd, 'size': sz, 'mtime': mt})
                else:
                    # 2) Repli Python (si `find` indisponible)
                    import time as _t
                    start = _t.time()
                    for root, dirs, files in os.walk(base):
                        for nm in list(dirs) + list(files):
                            if q in nm.lower():
                                full = os.path.join(root, nm)
                                isd, sz, mt = False, 0, 0
                                try:
                                    isd = os.path.isdir(full)
                                    st = os.stat(full)
                                    mt = st.st_mtime
                                    sz = 0 if isd else st.st_size
                                except OSError:
                                    pass
                                results.append({'name': nm, 'path': full, 'is_dir': isd, 'size': sz, 'mtime': mt})
                                if len(results) >= limit:
                                    break
                        if len(results) >= limit or (_t.time() - start) > 30:
                            truncated = len(results) >= limit
                            break
                results.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
                self._json(200, {'ok': True, 'results': results, 'truncated': truncated})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SEARCH-V1 : sommes de contrôle md5 + sha256 (un seul passage)
        if path == '/checksum':
            target = qs.get('path', '')
            try:
                if not os.path.isfile(target):
                    self._json(404, {'error': 'Fichier introuvable'})
                    return
                import hashlib
                m = hashlib.md5()
                s = hashlib.sha256()
                with open(target, 'rb') as f:
                    for chunk in iter(lambda: f.read(1048576), b''):
                        m.update(chunk); s.update(chunk)
                self._json(200, {'ok': True, 'md5': m.hexdigest(), 'sha256': s.hexdigest(),
                                 'size': os.path.getsize(target)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-PERM-V1 : lecture des permissions/propriétaire d'un élément
        if path == '/perm':
            target = qs.get('path', '')
            try:
                if not os.path.exists(target):
                    self._json(404, {'error': 'Introuvable'})
                    return
                st = os.stat(target)
                import pwd as _pwd, grp as _grp
                try:
                    owner = _pwd.getpwuid(st.st_uid).pw_name
                except Exception:
                    owner = ''
                try:
                    group = _grp.getgrgid(st.st_gid).gr_name
                except Exception:
                    group = ''
                self._json(200, {
                    'ok': True,
                    'mode': oct(st.st_mode & 0o7777)[2:].zfill(4),
                    'uid': st.st_uid, 'gid': st.st_gid,
                    'owner': owner, 'group': group,
                    'is_dir': os.path.isdir(target),
                })
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-EDITOR-V1 : lecture d'un fichier texte pour l'éditeur
        if path == '/read':
            target = qs.get('path', '')
            try:
                if not os.path.isfile(target):
                    self._json(404, {'error': 'Fichier introuvable'})
                    return
                size = os.path.getsize(target)
                if size > 5 * 1024 * 1024:
                    self._json(200, {'ok': True, 'too_large': True, 'size': size})
                    return
                with open(target, 'rb') as f:
                    raw = f.read()
                if b'\x00' in raw:
                    self._json(200, {'ok': True, 'binary': True, 'size': size})
                    return
                try:
                    content = raw.decode('utf-8')
                except UnicodeDecodeError:
                    self._json(200, {'ok': True, 'binary': True, 'size': size})
                    return
                self._json(200, {'ok': True, 'content': content, 'size': size})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/libvirt2/test':
            try:
                out = ssh_ok('echo ok && virsh --version')
                self._json(200, {'ok': True, 'message': out})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/libvirt2/bridges':
            try:
                out, _, _ = ssh_exec("ip -o link show type bridge 2>/dev/null || brctl show 2>/dev/null | awk 'NR>1{print $1}'")
                bridges = []
                for line in out.splitlines():
                    line = line.strip()
                    if not line: continue
                    # ip -o link format: "3: br0: <...>"
                    import re as _re
                    m = _re.match(r'^\d+:\s+(\S+):', line)
                    if m:
                        br = m.group(1).rstrip(':')
                        if br not in ('lo',): bridges.append(br)
                    elif line and ' ' not in line:
                        bridges.append(line)
                self._json(200, {'bridges': bridges})
            except Exception as e:
                self._json(200, {'bridges': []})
            return

        self._json(404, {'error': 'Not found: ' + path})

    # ── POST ──────────────────────────────────────────────────────────────────
    def do_POST(self):
        if urlparse(self.path).path.rstrip('/') == '/auth/login':
            return self._auth_login()
        if not self._auth():
            return
        p    = urlparse(self.path)
        path = p.path.rstrip('/')

        # MDM-LICENSE-V1 : activation + garde lecture seule
        if path == '/license/activate':
            try:
                b = self._body()
                code, resp = license_activate(str(b.get('token', '')))
                self._json(code, resp)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        # MDM-LICENSE-V1 : activation par clé (émission en ligne via le serveur)
        if path == '/license/activate-key':
            try:
                b = self._body()
                code, resp = license_activate_key(str(b.get('key', '')))
                self._json(code, resp)
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        # MDM-LICENSE-V1 : refresh manuel du jeton depuis le serveur
        if path == '/license/refresh':
            try:
                ok = license_do_refresh()
                st = license_status()
                st['subscribe_url'] = SUBSCRIBE_URL
                st['manage_url'] = MANAGE_URL
                self._json(200, {'ok': bool(ok), 'status': st})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return
        if path not in _LIC_READONLY_ALLOW_POST and _lic_is_readonly():
            self._json(402, {'error': 'license_readonly',
                             'message': 'Abonnement requis — mode lecture seule.'})
            return

        # MDM-WALLPAPERS-V1 : ajout d'une image (corps binaire, nom en paramètre)
        if path == '/wallpapers/upload':
            tmp_path = None
            try:
                qs = dict(parse_qsl(p.query, keep_blank_values=True))
                name = _wallpaper_name(qs.get('name', ''))
                total = int(self.headers.get('Content-Length') or -1)
                if total <= 0:
                    raise ValueError('Fichier vide ou taille absente.')
                if total > WALLPAPER_MAX_BYTES:
                    raise ValueError('Image trop lourde : %d Mo au plus.' % (WALLPAPER_MAX_BYTES // (1024 * 1024)))
                base = _wallpaper_dir(create=True)
                name = _wallpaper_free_name(base, name)
                target = os.path.join(base, name)
                tmp_path = os.path.join(base, '.upload-%d-%d' % (os.getpid(), _threading.get_ident()))
                remaining = total
                with open(tmp_path, 'wb') as out:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise IOError('Envoi interrompu avant la fin.')
                        out.write(chunk)
                        remaining -= len(chunk)
                os.replace(tmp_path, target)
                tmp_path = None
                try:
                    os.chmod(target, 0o644)
                except OSError:
                    pass
                self._json(200, {'ok': True, 'item': _wallpaper_item(target)})
            except ValueError as e:
                self._json(400, {'ok': False, 'error': str(e)})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            finally:
                if tmp_path:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            return
        # MDM-WALLPAPERS-V1 : suppression d'une image du dossier
        if path == '/wallpapers/delete':
            try:
                os.unlink(_wallpaper_path(self._body().get('name', '')))
                self._json(200, {'ok': True})
            except (FileNotFoundError, ValueError) as e:
                self._json(404, {'ok': False, 'error': str(e)})
            except Exception as e:
                self._json(500, {'ok': False, 'error': str(e)})
            return

        # MDM-APPS-V1 : bases de données + installation d'applications
        if path == '/db/create':
            try:
                b = self._body()
                res = _db_create(b.get('name') or None, b.get('user') or None, b.get('password') or None)
                self._json(200, {'ok': True, 'db': res})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/db/repair':
            try:
                self._json(200, _db_repair_root())
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/db/delete':
            try:
                b = self._body()
                _db_delete(b.get('name'), b.get('user'))
                self._json(200, {'ok': True})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/apps/profile':
            try:
                b = self._body()
                self._json(200, {'ok': True, 'profile': _install_profile_save(b)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/apps/install':
            try:
                b = self._body()
                res = _app_install(
                    b.get('app'), b.get('root'), b.get('php_version'),
                    b.get('port'), b.get('server_name'), b.get('name'),
                    b.get('url'), b.get('create_db', True),
                    auto=bool(b.get('auto')), site_host=b.get('site_host'),
                    title=b.get('title'), admin_user=b.get('admin_user'),
                    admin_password=b.get('admin_password'), admin_email=b.get('admin_email'),
                    language=b.get('language'),
                    db_name=b.get('db_name'), db_user=b.get('db_user'),
                    db_password=b.get('db_password'),
                )
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : activer/désactiver le reverse-proxy intégré
        if path == '/websites/proxy':
            try:
                b = self._body()
                res = _web_proxy_set(b.get('enabled', True))
                res['ok'] = True
                self._json(200, res)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : créer le dataset Web avec les bonnes permissions
        if path == '/websites/dataset':
            try:
                b = self._body()
                res = _web_create_dataset(b.get('pool'), b.get('name'))
                res['ok'] = True
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : créer un sous-dossier de site dans le dataset Web
        if path == '/websites/mkdir':
            try:
                b = self._body()
                res = _web_mkdir(b.get('parent'), b.get('name'))
                res['ok'] = True
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : réglages php.ini propres à un site (.user.ini)
        if path == '/websites/php-ini':
            try:
                b = self._body()
                sid = str(b.get('id', ''))
                data = _web_read()
                site = data.get(sid)
                if not site:
                    self._json(404, {'error': 'Site introuvable'})
                    return
                clean = {}
                for k, v in (b.get('ini') or {}).items():
                    k = str(k).strip()
                    vs = str(v).replace('\r', '').replace('\n', '').strip()[:256]
                    if _PHP_INI_KEY_RE.match(k) and vs:
                        clean[k] = vs
                site['php_ini'] = clean
                data[sid] = site
                _web_write(data)
                if site.get('root'):
                    _web_write_user_ini(site['root'], clean)
                ver = str(site.get('php_version') or '')
                if ver:
                    try:
                        os.makedirs(os.path.join(WEB_PHP_DIR, ver), exist_ok=True)
                        with open(os.path.join(WEB_PHP_DIR, ver, '.reload'), 'w', encoding='utf-8') as f:
                            f.write(str(_sh_time.time()))
                    except OSError:
                        pass
                self._json(200, {'ok': True, 'php_ini': clean})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : création / activation / suppression de sites
        if path == '/websites/create':
            try:
                b = self._body()
                sid, clean = _web_create(b)
                self._json(200, {'ok': True, 'id': sid, 'site': _web_public(clean, sid)})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/websites/reimport':
            try:
                imported = _web_reimport()
                self._json(200, {'ok': True, 'imported': imported, 'count': len(imported)})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/websites/toggle':
            try:
                b = self._body()
                sid = str(b.get('id', ''))
                data = _web_read()
                if sid not in data:
                    self._json(404, {'error': 'Site introuvable'})
                    return
                want = bool(b.get('enabled', not data[sid].get('enabled', True)))
                if want:
                    # revalider (port/domaine) avant réactivation
                    check = dict(data[sid])
                    check['enabled'] = True
                    _web_validate(check, data, sid=sid)
                data[sid]['enabled'] = want
                _web_write(data)
                _web_regenerate()
                self._json(200, {'ok': True, 'enabled': want})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/websites/delete':
            try:
                b = self._body()
                sid = str(b.get('id', ''))
                delete_root = bool(b.get('delete_root'))
                delete_db = bool(b.get('delete_db'))
                data = _web_read()
                site = data.get(sid)
                res = {'ok': True}
                if site:
                    del data[sid]
                    _web_write(data)
                    _web_regenerate()
                    # Suppression du dossier (protégé : sous /mnt, profond, pas un point de montage)
                    if delete_root and site.get('root'):
                        root = os.path.realpath(site['root'])
                        base = WEB_ROOT_ALLOWED.rstrip('/')
                        parts = [p for p in root.split('/') if p]
                        if root.startswith(base + '/') and len(parts) >= 3 and not os.path.ismount(root):
                            try:
                                _sh_shutil.rmtree(root)
                                res['root_deleted'] = True
                            except Exception as e:
                                res['root_error'] = str(e)
                        else:
                            res['root_error'] = 'dossier protégé, non supprimé'
                    # Suppression de la base + utilisateur
                    if delete_db:
                        dbinfo = site.get('db') or {}
                        dbname = b.get('db_name') or dbinfo.get('name')
                        dbuser = b.get('db_user') or dbinfo.get('user')
                        if dbname:
                            try:
                                _db_delete(dbname, dbuser)
                                res['db_deleted'] = True
                            except Exception as e:
                                res['db_error'] = str(e)
                self._json(200, res)
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : mise à jour du cœur d'une appli
        if path == '/websites/update':
            try:
                b = self._body()
                res = _app_update(str(b.get('id', '')))
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : clonage d'un site (fichiers + base)
        if path == '/websites/clone':
            try:
                b = self._body()
                res = _app_clone(str(b.get('id', '')), b.get('name'))
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : activer le SSL PrestaShop derrière le proxy
        if path == '/websites/ssl-prestashop':
            try:
                b = self._body()
                res = _app_prestashop_ssl(str(b.get('id', '')), bool(b.get('enable', True)))
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEB-MANAGE-V1 : réinitialiser le mot de passe admin d'une appli
        if path == '/websites/admin-reset':
            try:
                b = self._body()
                res = _app_admin_reset(str(b.get('id', '')))
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-DOWNLOADS-V1 : gestionnaire de téléchargements
        if path == '/downloads/add':
            try:
                b = self._body()
                self._json(200, {'ok': True, 'download': _dl_add(b.get('url'), b.get('dir'), b.get('filename'), b.get('premium', 'auto'), b.get('connections'), b.get('auto_extract'), b.get('auto_remove'))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/downloads/add-batch':
            try:
                b = self._body()
                res = _dl_add_batch(b.get('urls') or [], b.get('dir'), b.get('premium', 'auto'), b.get('connections'), b.get('auto_extract'), b.get('auto_remove'))
                self._json(200, {'ok': True, 'results': res,
                                 'added': sum(1 for x in res if x.get('ok')),
                                 'failed': sum(1 for x in res if not x.get('ok'))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path in ('/downloads/pause', '/downloads/resume', '/downloads/cancel', '/downloads/remove'):
            try:
                b = self._body()
                did = str(b.get('id', ''))
                if path == '/downloads/pause':
                    res = _dl_pause(did)
                elif path == '/downloads/resume':
                    res = _dl_resume(did)
                elif path == '/downloads/cancel':
                    res = _dl_cancel(did, delete=bool(b.get('delete', True)))
                else:
                    res = _dl_remove(did)
                self._json(200, res)
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/downloads/clear':
            try:
                self._json(200, _dl_clear())
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/downloads/settings':
            try:
                b = self._body()
                if b.get('max_concurrent') is not None:
                    _dl_set_max_concurrent(b.get('max_concurrent'))
                if b.get('default_connections') is not None:
                    _dl_set_default_connections(b.get('default_connections'))
                _dl_settings_save()
                self._json(200, dict({'ok': True}, **_dl_settings_get()))
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SELF-UPDATE-V1 : mise à jour depuis GitHub + redémarrage du sidecar
        if path == '/update':
            try:
                updated = _do_update()
                _schedule_self_restart()
                self._json(200, {'ok': True, 'updated': updated, 'restarting': True,
                                 'version': APP_VERSION})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-PREMIUM-V1 : gestion des comptes premium / débrideurs
        if path == '/premium/set':
            try:
                b = self._body()
                _prem_set(str(b.get('provider', '')),
                          {k: b.get(k) for k in ('apikey', 'token', 'login', 'password') if b.get(k) not in ('', None)})
                self._json(200, {'ok': True, 'providers': _prem_status()})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/premium/get':
            try:
                b = self._body()
                self._json(200, {'ok': True, 'fields': _prem_get(str(b.get('provider', '')))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/premium/remove':
            try:
                b = self._body()
                _prem_remove(str(b.get('provider', '')))
                self._json(200, {'ok': True, 'providers': _prem_status()})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        if path == '/premium/test':
            try:
                b = self._body()
                self._json(200, _prem_test(str(b.get('provider', ''))))
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-WEBSITES-V1 : enregistrer un profil PHP (ini + extensions)
        if path == '/php/profile':
            try:
                b = self._body()
                prof, changed_ext = _php_save(
                    b.get('version', ''), b.get('ini') or {}, b.get('extensions') or []
                )
                self._json(200, {'ok': True, 'profile': prof, 'ext_changed': changed_ext})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-SHARE-LINKS-V1 : création / révocation de liens (authentifié)
        if path == '/share/create':
            try:
                b = self._body()
                rec = _share_create(
                    b.get('path', ''),
                    expires_in=b.get('expires_in'),
                    max_downloads=b.get('max_downloads'),
                    password=(b.get('password') or None),
                )
                self._json(200, {
                    'ok': True,
                    'token': rec['token'],
                    'url_path': '/s/' + rec['token'],
                    'share': _share_public(rec),
                })
            except PermissionError as e:
                self._json(403, {'error': str(e)})
            except FileNotFoundError as e:
                self._json(404, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/share/revoke':
            try:
                b = self._body()
                tok = str(b.get('token', ''))
                data = _share_read()
                if tok in data:
                    del data[tok]
                    _share_write(data)
                self._json(200, {'ok': True})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-PERM-V1 : appliquer permissions (chmod) et/ou propriétaire (chown)
        if path == '/perm':
            try:
                body = self._body()
                target = str(body.get('path', ''))
                recursive = bool(body.get('recursive'))
                mode = body.get('mode')
                uid = body.get('uid')
                gid = body.get('gid')
                if not target or not os.path.exists(target):
                    raise ValueError('Introuvable')
                mode_int = None
                if mode not in (None, ''):
                    if not re.match(r'^[0-7]{3,4}$', str(mode)):
                        raise ValueError('Mode octal invalide (ex: 0755)')
                    mode_int = int(str(mode), 8)
                import pwd as _pwd, grp as _grp
                nu = None
                if uid not in (None, ''):
                    nu = int(uid) if str(uid).isdigit() else _pwd.getpwnam(str(uid)).pw_uid
                ng = None
                if gid not in (None, ''):
                    ng = int(gid) if str(gid).isdigit() else _grp.getgrnam(str(gid)).gr_gid

                def _apply(p):
                    if mode_int is not None:
                        os.chmod(p, mode_int)
                    if nu is not None or ng is not None:
                        stt = os.stat(p)
                        os.chown(p, nu if nu is not None else stt.st_uid,
                                 ng if ng is not None else stt.st_gid)

                _apply(target)
                if recursive and os.path.isdir(target):
                    for root, dirs, files in os.walk(target):
                        for d in dirs:
                            try:
                                _apply(os.path.join(root, d))
                            except OSError:
                                pass
                        for f in files:
                            try:
                                _apply(os.path.join(root, f))
                            except OSError:
                                pass
                self._json(200, {'ok': True})
            except (ValueError, KeyError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-EDITOR-V1 : écriture d'un fichier depuis l'éditeur (atomique)
        if path == '/write':
            try:
                body = self._body()
                target = str(body.get('path', ''))
                content = body.get('content', '')
                if not isinstance(content, str):
                    content = str(content)
                if not target:
                    raise ValueError('Chemin requis')
                parent = os.path.dirname(target)
                if not os.path.isdir(parent):
                    raise ValueError('Dossier inexistant : ' + parent)
                tmp = target + '.tmp-edit'
                with open(tmp, 'w', encoding='utf-8', newline='') as f:
                    f.write(content)
                try:
                    st = os.stat(target)
                    os.chown(tmp, st.st_uid, st.st_gid)
                    os.chmod(tmp, st.st_mode & 0o7777)
                except OSError:
                    pass
                os.replace(tmp, target)
                self._json(200, {'ok': True, 'size': len(content.encode('utf-8'))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-FILEOPS-MOVE-V1 : renommer / déplacer / copier (src -> dst)
        if path == '/rename' or path == '/move' or path == '/copy':
            import shutil as _shutil
            try:
                body = self._body()
                src = str(body.get('src', ''))
                dst = str(body.get('dst', ''))
                if not src or not dst:
                    raise ValueError('Source et destination requises')
                if not os.path.exists(src):
                    raise ValueError('Source introuvable')
                if os.path.realpath(src) == os.path.realpath(dst):
                    raise ValueError('Source et destination identiques')
                conflict = str(body.get('conflict', 'error'))
                if os.path.exists(dst):
                    if conflict == 'overwrite':
                        _privileged_remove(dst)
                    elif conflict == 'rename':
                        dst = _unique_path(dst)
                    else:
                        raise ValueError('La destination existe déjà')
                parent = os.path.dirname(dst.rstrip('/'))
                if not os.path.isdir(parent):
                    raise ValueError('Dossier de destination inexistant')
                if path == '/copy':
                    try:
                        if os.path.isdir(src):
                            _shutil.copytree(src, dst, symlinks=True)
                        else:
                            _shutil.copy2(src, dst)
                    except Exception:
                        # ACL ZFS/SMB : délègue la copie au host (vrai root).
                        _cmd = 'rm -rf ' + shq(dst) + '; cp -a ' + shq(src) + ' ' + shq(dst)
                        out, err, code = ssh_exec("sudo -n sh -c " + shq(_cmd), timeout=7200)
                        if code != 0 or not os.path.exists(dst):
                            raise RuntimeError('Copie (host) : ' + (err or out or 'échec').strip()[:200])
                else:
                    try:
                        os.rename(src, dst)
                    except OSError:
                        # Inter-dataset ou ACL ZFS/SMB : délègue au 'mv' du host
                        # (vrai root, gère les ACL et la suppression de la source).
                        out, err, code = ssh_exec("sudo -n mv -f " + shq(src) + " " + shq(dst), timeout=7200)
                        if code != 0:
                            raise RuntimeError('Déplacement (host) : ' + (err or out or 'échec').strip()[:200])
                    # Vérifie que la source a bien disparu.
                    if os.path.exists(src):
                        raise RuntimeError('Déplacement incomplet : la source subsiste (' + src + ')')
                self._json(200, {'ok': True, 'dst': dst, 'name': os.path.basename(dst.rstrip('/'))})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-ARCHIVE-V1 : compression zip / tar.gz
        if path == '/compress':
            import zipfile as _zf, tarfile as _tf
            try:
                body = self._body()
                items = body.get('items') or ([body.get('path')] if body.get('path') else [])
                items = [str(i) for i in items if i]
                dest = str(body.get('dest', ''))
                fmt = str(body.get('format', 'zip'))
                if not items or not dest:
                    raise ValueError('Éléments et destination requis')
                for it in items:
                    if not os.path.exists(it):
                        raise ValueError('Introuvable : ' + it)
                if not os.path.isdir(os.path.dirname(dest)):
                    raise ValueError('Dossier de destination inexistant')
                if fmt == 'zip':
                    with _zf.ZipFile(dest, 'w', _zf.ZIP_DEFLATED, allowZip64=True) as z:
                        for it in items:
                            base = os.path.basename(it.rstrip('/'))
                            if os.path.isdir(it):
                                for root, _d, files in os.walk(it):
                                    for fn in files:
                                        fp = os.path.join(root, fn)
                                        z.write(fp, os.path.join(base, os.path.relpath(fp, it)))
                            else:
                                z.write(it, base)
                else:
                    modes = {'tar': 'w', 'tar.gz': 'w:gz', 'tgz': 'w:gz',
                             'tar.bz2': 'w:bz2', 'tar.xz': 'w:xz'}
                    with _tf.open(dest, modes.get(fmt, 'w:gz')) as t:
                        for it in items:
                            t.add(it, arcname=os.path.basename(it.rstrip('/')))
                self._json(200, {'ok': True, 'path': dest})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-ARCHIVE-V1 : extraction zip / tar / tgz (protégée contre le zip-slip)
        if path == '/extract':
            import zipfile as _zf, tarfile as _tf
            try:
                body = self._body()
                src = str(body.get('path', ''))
                dest_given = str(body.get('dest', '') or '')
                if not os.path.isfile(src):
                    raise ValueError('Archive introuvable')

                def _decide_dest(names):
                    if dest_given:
                        return dest_given
                    d = os.path.dirname(src)
                    tops = set()
                    for n in names:
                        n = str(n).replace('\\', '/').lstrip('/')
                        if n:
                            tops.add(n.split('/')[0])
                    if len(tops) == 1:
                        return d  # l'archive a déjà son dossier racine
                    low = src.lower()
                    if low.endswith('.tar.gz'):
                        stem = os.path.basename(src)[:-7]
                    elif low.endswith('.tgz'):
                        stem = os.path.basename(src)[:-4]
                    else:
                        stem = os.path.splitext(os.path.basename(src))[0]
                    return os.path.join(d, stem or 'extrait')

                def _do_extract(names, extract_all):
                    dest = _decide_dest(names)
                    os.makedirs(dest, exist_ok=True)
                    dest_real = os.path.realpath(dest)
                    for name in names:
                        p = os.path.realpath(os.path.join(dest_real, name))
                        if not (p == dest_real or p.startswith(dest_real + os.sep)):
                            raise ValueError('Archive non sûre (chemin hors dossier)')
                    extract_all(dest_real)
                    return dest_real

                import shutil as _shutil
                if _zf.is_zipfile(src):
                    with _zf.ZipFile(src) as z:
                        dest_real = _do_extract(z.namelist(), z.extractall)
                elif _tf.is_tarfile(src):
                    with _tf.open(src) as t:
                        dest_real = _do_extract(t.getnames(), t.extractall)
                else:
                    # Fichier unique compressé (gz / bz2 / xz), pas une archive tar
                    low = src.lower()
                    if low.endswith('.gz'):
                        import gzip as _c; out = src[:-3]
                    elif low.endswith('.bz2'):
                        import bz2 as _c; out = src[:-4]
                    elif low.endswith('.xz') or low.endswith('.lzma'):
                        import lzma as _c; out = os.path.splitext(src)[0]
                    else:
                        raise ValueError('Format non reconnu (zip, tar, gz, bz2, xz)')
                    if not out or out == src:
                        out = src + '.out'
                    _cand = out
                    _n = 1
                    while os.path.exists(_cand):
                        _cand = out + '.' + str(_n)
                        _n += 1
                    out = _cand
                    with _c.open(src, 'rb') as fi, open(out, 'wb') as fo:
                        _shutil.copyfileobj(fi, fo)
                    dest_real = out
                self._json(200, {'ok': True, 'dest': dest_real})
            except (ValueError, PermissionError) as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # MDM-FILEOPS-UPLOAD-V1
        # Upload binaire direct vers /mnt, sans dépendre de l'API REST TrueNAS.
        if path == '/upload':
            tmp_path = None
            try:
                qs = dict(parse_qsl(p.query, keep_blank_values=True))
                requested_path = str(qs.get('path', '') or '').strip()

                if not requested_path.startswith('/mnt/'):
                    raise PermissionError(
                        'Destination refusée : le chemin doit commencer par /mnt/'
                    )

                filename = os.path.basename(requested_path)
                if not filename or filename in ('.', '..'):
                    raise ValueError('Nom de fichier invalide')

                requested_parent = os.path.dirname(requested_path)
                real_parent = os.path.realpath(requested_parent)

                if real_parent != '/mnt' and not real_parent.startswith('/mnt/'):
                    raise PermissionError(
                        'Destination refusée : chemin hors de /mnt'
                    )

                if not os.path.isdir(real_parent):
                    raise FileNotFoundError(
                        'Dossier destination introuvable : ' + real_parent
                    )

                target_path = os.path.join(real_parent, filename)

                if os.path.isdir(target_path):
                    raise IsADirectoryError(
                        'La destination est un dossier : ' + target_path
                    )

                content_length = self.headers.get('Content-Length')
                if content_length is None:
                    raise ValueError('Content-Length absent')

                total_size = int(content_length)
                if total_size < 0:
                    raise ValueError('Taille de fichier invalide')

                tmp_path = (
                    target_path
                    + '.uploading-'
                    + str(os.getpid())
                    + '-'
                    + str(_threading.get_ident())
                )

                remaining = total_size

                with open(tmp_path, 'wb') as output:
                    while remaining > 0:
                        chunk = self.rfile.read(min(1024 * 1024, remaining))

                        if not chunk:
                            raise IOError(
                                'Upload interrompu avant réception complète'
                            )

                        output.write(chunk)
                        remaining -= len(chunk)

                    output.flush()
                    os.fsync(output.fileno())

                received_size = os.path.getsize(tmp_path)

                if received_size != total_size:
                    raise IOError(
                        'Taille reçue incorrecte : '
                        + str(received_size)
                        + ' au lieu de '
                        + str(total_size)
                    )

                os.replace(tmp_path, target_path)
                tmp_path = None

                try:
                    os.chmod(target_path, 0o644)
                except Exception:
                    pass

                log.info(
                    'Upload terminé : %s (%s octets)',
                    target_path,
                    total_size
                )

                self._json(200, {
                    'ok': True,
                    'path': target_path,
                    'size': total_size,
                })

            except Exception as exc:
                if tmp_path:
                    try:
                        if os.path.exists(tmp_path):
                            os.remove(tmp_path)
                    except Exception:
                        pass

                log.exception('Échec upload File Station')
                self._json(500, {'error': str(exc)})

            return


        # MDM-LIBVIRT-REQUEST-GUARD-V1-BEGIN
        if _libvirt_path_requires_daemon(path):
            try:
                ensure_libvirtd()
            except Exception as error:
                self._json(503, {
                    'api': 'libvirt2' if path.startswith('/libvirt2') else 'libvirt',
                    'error': 'Service QEMU/libvirt indisponible : ' + str(error),
                    'autorecovery': 'failed',
                })
                return
        # MDM-LIBVIRT-REQUEST-GUARD-V1-END

        # MDM-ACCESS-POLICY-V1-POST-BEGIN
        if path == '/access/policy':
            try:
                body = self._body()
                policy = access_policy_set(
                    body.get('subject'),
                    body.get('applications', {}),
                    body.get('actor', 'unknown')
                )
                self._json(200, {'ok': True, 'policy': policy})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        if path == '/access/history':
            try:
                event = access_history_add(self._body())
                self._json(200, {'ok': True, 'event': event})
            except ValueError as e:
                self._json(400, {'error': str(e)})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return
        # MDM-ACCESS-POLICY-V1-POST-END

        # MDM-VM-BOOT-IMAGE-20260704: create an empty disk OR clone a selected bootable QCOW2 template.
        if path == '/libvirt2/vms':
            disk_path = None
            xml_path = None
            generated_disk = False
            try:
                body         = self._body()
                name         = re.sub(r'[^a-zA-Z0-9_-]', '-', str(body['name']).strip())
                mem_mb       = int(body.get('memory_mb', 4096))
                vcpus        = int(body.get('vcpus', 2))
                disk_gb      = int(body.get('disk_gb', 60))
                iso_path     = str(body.get('iso_path', '') or '').strip()
                source_image = str(body.get('source_image', '') or '').strip()
                uefi         = bool(body.get('uefi', True))
                secure_boot  = bool(body.get('secure_boot', False))
                tpm          = bool(body.get('tpm', False))
                network      = body.get('network', 'default')
                disk_bus     = body.get('disk_bus', 'sata')
                net_type     = body.get('net_type', 'nat')
                bridge_iface = body.get('bridge_iface', 'br0')
                net_model    = body.get('net_model', 'e1000')
                autostart   = bool(body.get('autostart', False))

                if not name:
                    raise ValueError('Nom de VM vide')
                if mem_mb < 512 or vcpus < 1:
                    raise ValueError('Ressources VM invalides')
                if disk_gb < 5:
                    raise ValueError('La taille de disque minimale est de 5 Go')
                if disk_bus not in ('sata', 'virtio', 'scsi', 'ide'):
                    raise ValueError('Bus disque invalide')

                disk_path = os.path.join(VM_DIR, name + '.qcow2')
                xml_path  = os.path.join(VM_DIR, name + '.xml')
                if os.path.exists(disk_path):
                    raise RuntimeError('Disque "' + disk_path + '" existe deja')

                # Create directory as root (container) — avoids SSH permission issues.
                os.makedirs(VM_DIR, mode=0o777, exist_ok=True)
                try:
                    os.chmod(VM_DIR, 0o777)
                except Exception:
                    pass

                import subprocess as _sp
                if source_image:
                    # A boot image is copied into VM_DIR so the original download/template remains untouched.
                    source_real = os.path.realpath(source_image)
                    allowed_roots = [os.path.realpath(ISO_DIR), os.path.realpath(VM_DIR)]
                    if not any(source_real == root or source_real.startswith(root.rstrip('/') + '/') for root in allowed_roots):
                        raise RuntimeError('L’image doit se trouver dans le stockage autorisé du NAS')
                    if not os.path.isfile(source_real):
                        raise RuntimeError('Image QCOW2 introuvable: ' + source_image)
                    if not source_real.lower().endswith('.qcow2'):
                        raise RuntimeError('Seules les images .qcow2 sont acceptées')

                    info = _sp.run(['qemu-img', 'info', '--output=json', source_real],
                                   capture_output=True, text=True, timeout=60)
                    if info.returncode != 0:
                        raise RuntimeError('qemu-img info: ' + (info.stderr or info.stdout).strip())
                    try:
                        image_info = json.loads(info.stdout)
                    except Exception:
                        raise RuntimeError('Impossible de lire les métadonnées de l’image QCOW2')
                    if str(image_info.get('format', '')).lower() != 'qcow2':
                        raise RuntimeError('Le fichier sélectionné n’est pas une image QCOW2 valide')
                    source_virtual = int(image_info.get('virtual-size') or 0)
                    requested_bytes = disk_gb * 1024 * 1024 * 1024
                    if source_virtual <= 0:
                        raise RuntimeError('Taille virtuelle de l’image QCOW2 invalide')
                    if requested_bytes < source_virtual:
                        minimum_gb = (source_virtual + 1024**3 - 1) // 1024**3
                        raise RuntimeError('La taille finale doit être au moins de ' + str(minimum_gb) + ' Go')

                    clone = _sp.run(['qemu-img', 'convert', '-p', '-O', 'qcow2', source_real, disk_path],
                                    capture_output=True, text=True, timeout=1800)
                    if clone.returncode != 0:
                        raise RuntimeError('qemu-img convert: ' + (clone.stderr or clone.stdout).strip())
                    generated_disk = True
                    if requested_bytes > source_virtual:
                        resize = _sp.run(['qemu-img', 'resize', disk_path, str(disk_gb) + 'G'],
                                         capture_output=True, text=True, timeout=180)
                        if resize.returncode != 0:
                            raise RuntimeError('qemu-img resize: ' + (resize.stderr or resize.stdout).strip())
                else:
                    # Standard VM: create a fresh empty QCOW2.
                    fresh = _sp.run(['qemu-img', 'create', '-f', 'qcow2', disk_path, str(disk_gb) + 'G'],
                                    capture_output=True, text=True, timeout=60)
                    if fresh.returncode != 0:
                        raise RuntimeError('qemu-img: ' + (fresh.stderr or fresh.stdout).strip())
                    generated_disk = True

                xml = build_vm_xml(
                    name, mem_mb, vcpus, disk_path, iso_path or None,
                    uefi, secure_boot, tpm, network, disk_bus, net_type,
                    bridge_iface, net_model,
                    boot_disk_first=bool(source_image)
                )
                with open(xml_path, 'w', encoding='utf-8') as f:
                    f.write(xml)
                log.info('VM XML written to %s:\n%s', xml_path, xml)

                out_define, err_define, code_define = ssh_exec(
                    "sudo -n virsh -c '" + VIRSH_URI + "' define " + shq(xml_path))
                if code_define != 0:
                    full = (err_define or out_define or '').strip()
                    raise RuntimeError('virsh define: ' + ' | '.join(full.splitlines()[:8]))

                # Persistent autostart after host/libvirtd restart.
                if autostart:
                    out_auto, err_auto, code_auto = ssh_exec(
                        "sudo -n virsh -c '" + VIRSH_URI + "' autostart " + shq(name), timeout=30)
                    if code_auto != 0:
                        full = (err_auto or out_auto or '').strip()
                        raise RuntimeError('virsh autostart: ' + ' | '.join(full.splitlines()[:8]))
                _vm_autostart_set(name, autostart)

                self._json(200, {
                    'ok': True,
                    'name': name,
                    'disk': disk_path,
                    'created_from_image': bool(source_image),
                    'autostart': autostart,
                })

            except Exception as e:
                # Do not leave an orphaned managed copy if VM definition fails.
                if generated_disk and disk_path and os.path.exists(disk_path):
                    try:
                        os.remove(disk_path)
                    except Exception:
                        pass
                if xml_path and os.path.exists(xml_path):
                    try:
                        os.remove(xml_path)
                    except Exception:
                        pass
                self._json(500, {'error': str(e)})
            return

        self._json(404, {'error': 'Not found: ' + path})

    def do_PUT(self):
        if not self._auth(): return
        path = self.path.split('?')[0].rstrip('/')

        # MDM-LICENSE-V1 : garde lecture seule
        if _lic_is_readonly():
            self._json(402, {'error': 'license_readonly',
                             'message': 'Abonnement requis — mode lecture seule.'})
            return

        # MDM-LIBVIRT-REQUEST-GUARD-V1-BEGIN
        if _libvirt_path_requires_daemon(path):
            try:
                ensure_libvirtd()
            except Exception as error:
                self._json(503, {
                    'api': 'libvirt2' if path.startswith('/libvirt2') else 'libvirt',
                    'error': 'Service QEMU/libvirt indisponible : ' + str(error),
                    'autorecovery': 'failed',
                })
                return
        # MDM-LIBVIRT-REQUEST-GUARD-V1-END

        # MDM-LIBVIRT2-SNAPSHOTS-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/snapshots/create
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots/create$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_snapshot_create(
                    name,
                    body.get('label', ''),
                    body.get('description', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/snapshots/revert
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots/revert$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                snapshot = _lv2_snapshot_name_norm(body.get('snapshot', ''))

                snap_data = _lv2_snapshot_list(name)
                current = str(snap_data.get('current') or '').strip()

                if current == snapshot:
                    self._json(200, {
                        'api': 'libvirt2',
                        'ok': True,
                        'name': name,
                        'action': 'snapshot_revert',
                        'snapshot': snapshot,
                        'already_current': True,
                        'warning': 'Snapshot déjà courant : aucune restauration nécessaire.',
                        'snapshots': snap_data.get('snapshots', []),
                    })
                    return

                self._json(200, _lv2_snapshot_revert(name, snapshot))
            except Exception as e:
                self._json(500, {
                    'api': 'libvirt2',
                    'error': str(e) or repr(e)
                })
            return

        # PUT /libvirt2/vms/{name}/snapshots/delete
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots/delete$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_snapshot_delete(
                    name,
                    body.get('snapshot', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-SNAPSHOTS-ROUTES-20260710
        # GET /libvirt2/vms/{name}/snapshots
        m = re.match(r'^/libvirt2/vms/([^/]+)/snapshots$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                self._json(200, _lv2_snapshot_list(name))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # MDM-LIBVIRT2-NETWORK-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/nics/add
        m = re.match(r'^/libvirt2/vms/([^/]+)/nics/add$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_add_nic(
                    name,
                    body.get('source', 'default'),
                    body.get('model', 'virtio'),
                    body.get('net_type', 'nat'),
                    body.get('mac', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/nics/mac
        m = re.match(r'^/libvirt2/vms/([^/]+)/nics/mac$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_set_nic_mac(
                    name,
                    body.get('mac', ''),
                    body.get('new_mac', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/nics/remove
        m = re.match(r'^/libvirt2/vms/([^/]+)/nics/remove$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_remove_nic(
                    name,
                    body.get('mac', '')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-DISK-REMOVE-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/disks/remove
        m = re.match(r'^/libvirt2/vms/([^/]+)/disks/remove$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_remove_disk(
                    name,
                    body.get('path'),
                    body.get('delete_file', False)
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-DISK-RESIZE-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/disks/resize
        m = re.match(r'^/libvirt2/vms/([^/]+)/disks/resize$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_resize_disk(
                    name,
                    body.get('path'),
                    body.get('new_size_gb')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-DISKS-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/disks/add
        m = re.match(r'^/libvirt2/vms/([^/]+)/disks/add$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_add_disk(
                    name,
                    body.get('size_gb'),
                    body.get('bus', 'virtio'),
                    body.get('label', 'data')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-RESOURCES-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/resources
        m = re.match(r'^/libvirt2/vms/([^/]+)/resources$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_set_resources(
                    name,
                    body.get('vcpus'),
                    body.get('memory_mb')
                ))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-STATE-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/state
        m = re.match(r'^/libvirt2/vms/([^/]+)/state$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                action = body.get('action', '')
                self._json(200, _lv2_vm_state_action(name, action))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/autostart
        m = re.match(r'^/libvirt2/vms/([^/]+)/autostart$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                enabled = bool(body.get('enabled', False))
                self._json(200, _lv2_set_autostart(name, enabled))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/repair-uefi — régénère le NVRAM puis redémarre
        m = re.match(r'^/libvirt2/vms/([^/]+)/repair-uefi$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                self._json(200, _lv2_repair_uefi(name))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/disk-bus — change l'interface du disque principal
        m = re.match(r'^/libvirt2/vms/([^/]+)/disk-bus$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                self._json(200, _lv2_set_disk_bus(name, body.get('bus', '')))
            except (ValueError, PermissionError) as e:
                self._json(400, {'api': 'libvirt2', 'error': str(e)})
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        # MDM-LIBVIRT2-INSTALLATION-ROUTES-20260710
        # PUT /libvirt2/vms/{name}/iso
        m = re.match(r'^/libvirt2/vms/([^/]+)/iso$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                iso_path = str(body.get('iso_path', '') or '').strip()
                self._json(200, _lv2_set_iso(name, iso_path))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        # PUT /libvirt2/vms/{name}/boot
        m = re.match(r'^/libvirt2/vms/([^/]+)/boot$', path)
        if m:
            try:
                from urllib.parse import unquote
                name = unquote(m.group(1))
                body = self._body()
                order = body.get('order', [])
                self._json(200, _lv2_set_boot(name, order))
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return


        self._json(404, {'error': 'Not found: ' + path})

    def do_DELETE(self):
        if not self._auth(): return
        path = self.path.split('?')[0].rstrip('/')

        # MDM-LICENSE-V1 : garde lecture seule
        if _lic_is_readonly():
            self._json(402, {'error': 'license_readonly',
                             'message': 'Abonnement requis — mode lecture seule.'})
            return

        # MDM-LIBVIRT-REQUEST-GUARD-V1-BEGIN
        if _libvirt_path_requires_daemon(path):
            try:
                ensure_libvirtd()
            except Exception as error:
                self._json(503, {
                    'api': 'libvirt2' if path.startswith('/libvirt2') else 'libvirt',
                    'error': 'Service QEMU/libvirt indisponible : ' + str(error),
                    'autorecovery': 'failed',
                })
                return
        # MDM-LIBVIRT-REQUEST-GUARD-V1-END

        # File deletion
        if path == '/delete':
            try:
                body = self._body()
                target = body.get('path', '')
                if not target:
                    raise ValueError('Chemin requis')
                _privileged_remove(target)
                self._json(200, {'ok': True})
            except Exception as e:
                self._json(500, {'error': str(e)})
            return

        # VM deletion
        m = re.match(r'^/libvirt2/vms/([^/]+)$', path)
        if m:
            from urllib.parse import unquote
            name = unquote(m.group(1))
            try:
                v = "sudo -n virsh -c '" + VIRSH_URI + "'"
                # Get disk list before undefine
                disks = virsh_domblklist(name)
                qcow2s = [d['source'] for d in disks
                          if d.get('source', '-') != '-' and d['source'].endswith('.qcow2')]
                # Force stop if running
                ssh_exec(v + ' destroy ' + shq(name))
                # Undefine (try with --nvram for UEFI VMs)
                out, err, code = ssh_exec(
                    v + ' undefine ' + shq(name) + ' --nvram --managed-save --snapshots-metadata'
                )
                if code != 0:
                    ssh_ok(v + ' undefine ' + shq(name))
                # Delete disk files
                for disk in qcow2s:
                    try:
                        os.remove(disk)
                    except Exception:
                        pass
                # Delete XML if present
                xml_path = os.path.join(VM_DIR, name + '.xml')
                try:
                    os.remove(xml_path)
                except Exception:
                    pass
                self._json(200, {'api': 'libvirt2', 'ok': True, 'name': name})
            except Exception as e:
                self._json(500, {'api': 'libvirt2', 'error': str(e)})
            return

        self._json(404, {'error': 'Not found: ' + path})


# ── WebSocket server launcher ────────────────────────────────────────────────
def ws_server_start(port):
    import asyncio
    async def _serve():
        try:
            import websockets as _ws
            async with _ws.serve(ws_handler, '0.0.0.0', port):
                await asyncio.Future()
        except Exception as e:
            log.error('WS server error: %s', e)
    asyncio.run(_serve())

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    import socketserver as _ss

    HTTP_PORT = int(os.environ.get('FILEOPS_PORT', 8765))
    WS_PORT   = int(os.environ.get('FILEOPS_WS_PORT', 8766))

    class _TCPServer(_ss.ThreadingMixIn, _ss.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    log.info('fileops HTTP :%d  WS :%d', HTTP_PORT, WS_PORT)

    import threading as _th
    t = _th.Thread(target=ws_server_start, args=(WS_PORT,), daemon=True)
    t.start()


# MDM-LIBVIRT2-SAFE-COPY-SNAPSHOTS-20260710
# Snapshots sûrs Desktroo.
# Principe : VM arrêtée uniquement, copie qcow2/XML/NVRAM dans /mnt/Truenas_Stockage/vms/_snapshots/<VM>/<snapshot-id>/
# Pas de virsh snapshot-delete, pas de qemu-img snapshot -d.

import json as _lv2_safe_json
import time as _lv2_safe_time
import re as _lv2_safe_re
import xml.etree.ElementTree as _lv2_safe_ET

_LV2_SAFE_SNAPSHOT_ROOT = "/mnt/Truenas_Stockage/vms/_snapshots"


def _lv2_safe_snapshot_id(label=""):
    raw = str(label or "").strip().lower()
    raw = raw.replace(" ", "-")
    raw = _lv2_safe_re.sub(r"[^a-z0-9_.-]+", "-", raw).strip("-._")
    if not raw:
        raw = "snapshot"
    raw = raw[:40]
    return "safe-" + _lv2_safe_time.strftime("%Y%m%d-%H%M%S") + "-" + raw


def _lv2_safe_vm_dir(name):
    safe = _lv2_safe_re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "").strip())
    if not safe:
        raise RuntimeError("Nom VM invalide")
    return _LV2_SAFE_SNAPSHOT_ROOT.rstrip("/") + "/" + safe


def _lv2_safe_sh(cmd, timeout=30):
    out, err, code = ssh_exec(cmd, timeout=timeout)
    if code != 0:
        msg = (err or out or "").strip()
        if not msg:
            msg = "Commande échouée sans message, code=" + str(code)
        raise RuntimeError(msg)
    return out


def _lv2_safe_file_basename(path):
    path = str(path or "").rstrip("/")
    if not path:
        return ""
    return path.split("/")[-1]


def _lv2_safe_vm_files_from_xml(name):
    xml = _lv2_safe_sh(
        "timeout 20s sudo -n virsh -c " + shq(VIRSH_URI) + " dumpxml " + shq(name),
        timeout=30
    )

    root = _lv2_safe_ET.fromstring(xml)

    disks = []
    for disk in root.findall("./devices/disk"):
        if disk.get("device") != "disk":
            continue
        if disk.get("type") != "file":
            continue

        source = disk.find("source")
        target = disk.find("target")
        driver = disk.find("driver")

        src = ""
        if source is not None:
            src = source.get("file") or ""

        if not src:
            continue

        tgt = ""
        bus = ""
        if target is not None:
            tgt = target.get("dev") or ""
            bus = target.get("bus") or ""

        fmt = ""
        if driver is not None:
            fmt = driver.get("type") or ""

        base = _lv2_safe_file_basename(src)
        if not base:
            base = (tgt or "disk") + ".img"

        dest_name = base
        if tgt:
            dest_name = tgt + "-" + base

        disks.append({
            "source": src,
            "target": tgt,
            "bus": bus,
            "format": fmt,
            "dest": "disks/" + dest_name,
        })

    nvram = ""
    nvram_el = root.find("./os/nvram")
    if nvram_el is not None and nvram_el.text:
        nvram = nvram_el.text.strip()

    nvram_item = None
    if nvram:
        nvram_item = {
            "source": nvram,
            "dest": "nvram/" + _lv2_safe_file_basename(nvram),
        }

    return xml, disks, nvram_item


def _lv2_safe_snapshot_meta_read(vm_dir, snap_id):
    meta_path = vm_dir.rstrip("/") + "/" + snap_id + "/metadata.json"
    cmd = "test -f " + shq(meta_path) + " && cat " + shq(meta_path) + " || true"
    out = ssh_exec(cmd, timeout=10)[0].strip()
    if not out:
        return None
    try:
        return _lv2_safe_json.loads(out)
    except Exception:
        return {
            "id": snap_id,
            "name": snap_id,
            "status": "unknown",
            "error": "metadata.json illisible",
        }


def _lv2_snapshot_list(name):
    vm_dir = _lv2_safe_vm_dir(name)

    cmd = (
        "if [ -d " + shq(vm_dir) + " ]; then "
        "find " + shq(vm_dir) + " -mindepth 1 -maxdepth 1 -type d -printf '%f\\n' 2>/dev/null | sort -r; "
        "fi"
    )

    out, err, code = ssh_exec(cmd, timeout=15)
    if code != 0:
        raise RuntimeError((err or out or "Impossible de lister les snapshots sûrs").strip())

    items = []
    for snap_id in [x.strip() for x in out.splitlines() if x.strip()]:
        meta = _lv2_safe_snapshot_meta_read(vm_dir, snap_id)
        if not meta:
            meta = {
                "id": snap_id,
                "name": snap_id,
                "status": "unknown",
            }

        items.append({
            "id": meta.get("id") or snap_id,
            "name": meta.get("name") or meta.get("id") or snap_id,
            "label": meta.get("label") or "",
            "description": meta.get("description") or "",
            "status": meta.get("status") or "unknown",
            "created_at": meta.get("created_at") or "",
            "finished_at": meta.get("finished_at") or "",
            "engine": "safe-copy",
            "current": False,
            "state": meta.get("status") or "unknown",
            "creation_time": None,
            "creation_time_iso": meta.get("created_at") or "",
            "parent": "",
            "error": meta.get("error") or "",
            "path": meta.get("path") or (vm_dir.rstrip("/") + "/" + snap_id),
            "disks": meta.get("disks") or [],
            "nvram": meta.get("nvram") or None,
        })

    return {
        "api": "libvirt2",
        "readonly": False,
        "name": name,
        "disabled": False,
        "engine": "safe-copy",
        "current": "",
        "snapshots": items,
        "root": vm_dir,
        "warning": "Snapshots sûrs par copie qcow2/XML/NVRAM. VM arrêtée obligatoire.",
    }


# MDM-LIBVIRT2-SAFE-COPY-SNAPSHOTS-SUDO-COPY-20260710
# Override safe-copy : copie des disques/NVRAM avec sudo pour gérer les permissions libvirt.

def _lv2_snapshot_create(name, label="", description=""):
    _lv2_snapshot_vm_must_be_stopped(name)

    label = str(label or "").strip()
    description = str(description or "").strip()
    snap_id = _lv2_safe_snapshot_id(label)
    vm_dir = _lv2_safe_vm_dir(name)
    snap_dir = vm_dir.rstrip("/") + "/" + snap_id

    xml, disks, nvram = _lv2_safe_vm_files_from_xml(name)

    if not disks:
        raise RuntimeError("Aucun disque fichier détecté pour cette VM")

    created_at = _lv2_safe_time.strftime("%Y-%m-%dT%H:%M:%S%z")

    meta = {
        "id": snap_id,
        "name": snap_id,
        "label": label,
        "description": description,
        "vm": name,
        "engine": "safe-copy",
        "status": "creating",
        "created_at": created_at,
        "finished_at": "",
        "path": snap_dir,
        "domain_xml": "domain.xml",
        "disks": disks,
        "nvram": nvram,
        "error": "",
    }

    meta_json = _lv2_safe_json.dumps(meta, ensure_ascii=False, indent=2)

    copy_lines = []
    copy_lines.append("set -e")
    copy_lines.append("mkdir -p " + shq(snap_dir + "/disks") + " " + shq(snap_dir + "/nvram") + " " + shq(snap_dir + "/logs"))
    copy_lines.append("cat > " + shq(snap_dir + "/metadata.json") + " <<'EOF_META'\n" + meta_json + "\nEOF_META")
    copy_lines.append("sudo -n virsh -c " + shq(VIRSH_URI) + " dumpxml " + shq(name) + " > " + shq(snap_dir + "/domain.xml"))

    for d in disks:
        dest = snap_dir + "/" + d["dest"]
        copy_lines.append("sudo -n test -f " + shq(d["source"]))
        copy_lines.append("sudo -n cp --reflink=auto --sparse=always " + shq(d["source"]) + " " + shq(dest))
        copy_lines.append("sudo -n chown $(id -u):$(id -g) " + shq(dest) + " || true")
        copy_lines.append("chmod u+rw " + shq(dest) + " || true")

    if nvram:
        dest = snap_dir + "/" + nvram["dest"]
        copy_lines.append("if sudo -n test -f " + shq(nvram["source"]) + "; then")
        copy_lines.append("  sudo -n cp --reflink=auto --sparse=always " + shq(nvram["source"]) + " " + shq(dest))
        copy_lines.append("  sudo -n chown $(id -u):$(id -g) " + shq(dest) + " || true")
        copy_lines.append("  chmod u+rw " + shq(dest) + " || true")
        copy_lines.append("fi")

    copy_lines.append(
        "python3 - <<'EOF_DONE'\n"
        "import json, time\n"
        "p=" + repr(snap_dir + "/metadata.json") + "\n"
        "m=json.load(open(p,'r',encoding='utf-8'))\n"
        "m['status']='ready'\n"
        "m['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')\n"
        "m['error']=''\n"
        "json.dump(m,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)\n"
        "EOF_DONE"
    )

    script = "\n".join(copy_lines)

    wrapped = (
        "mkdir -p " + shq(snap_dir + "/logs") + "; "
        "cat > " + shq(snap_dir + "/create.sh") + " <<'EOF_SCRIPT'\n"
        + script +
        "\nEOF_SCRIPT\n"
        "chmod +x " + shq(snap_dir + "/create.sh") + "; "
        "( " + shq(snap_dir + "/create.sh") + " > " + shq(snap_dir + "/logs/create.log") + " 2>&1 || "
        "python3 - <<'EOF_ERR'\n"
        "import json, time\n"
        "p=" + repr(snap_dir + "/metadata.json") + "\n"
        "try:\n"
        "    m=json.load(open(p,'r',encoding='utf-8'))\n"
        "except Exception:\n"
        "    m={}\n"
        "m['status']='error'\n"
        "m['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')\n"
        "try:\n"
        "    m['error']=open(" + repr(snap_dir + "/logs/create.log") + ",'r',encoding='utf-8',errors='replace').read()[-4000:]\n"
        "except Exception as e:\n"
        "    m['error']=str(e)\n"
        "json.dump(m,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)\n"
        "EOF_ERR\n"
        ") & echo $!"
    )

    out, err, code = ssh_exec(wrapped, timeout=20)
    if code != 0:
        raise RuntimeError((err or out or "Impossible de lancer la création du snapshot sûr").strip())

    return {
        "api": "libvirt2",
        "ok": True,
        "name": name,
        "action": "safe_snapshot_create",
        "engine": "safe-copy",
        "snapshot": snap_id,
        "status": "creating",
        "pid": out.strip(),
        "path": snap_dir,
        "message": "Création du snapshot sûr lancée en tâche de fond.",
        "snapshots": _lv2_snapshot_list(name).get("snapshots", []),
    }


def _lv2_snapshot_delete(name, snapshot):
    _lv2_snapshot_vm_must_be_stopped(name)

    snap_id = _lv2_snapshot_name_norm(snapshot)
    vm_dir = _lv2_safe_vm_dir(name)
    snap_dir = vm_dir.rstrip("/") + "/" + snap_id

    if not snap_dir.startswith(vm_dir.rstrip("/") + "/"):
        raise RuntimeError("Chemin snapshot invalide")

    cmd = (
        "test -d " + shq(snap_dir) + " || { echo 'Snapshot sûr introuvable'; exit 2; }; "
        "if [ -f " + shq(snap_dir + "/metadata.json") + " ]; then "
        "python3 - <<'EOF_MARK'\n"
        "import json\n"
        "p=" + repr(snap_dir + "/metadata.json") + "\n"
        "try:\n"
        "    m=json.load(open(p,'r',encoding='utf-8'))\n"
        "    m['status']='deleting'\n"
        "    m['finished_at']=''\n"
        "    json.dump(m,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)\n"
        "except Exception:\n"
        "    pass\n"
        "EOF_MARK\n"
        "fi; "
        "( sudo -n rm -rf -- " + shq(snap_dir) + " ) >/dev/null 2>&1 & echo $!"
    )

    out, err, code = ssh_exec(cmd, timeout=15)
    if code != 0:
        raise RuntimeError((err or out or "Impossible de lancer la suppression du snapshot sûr").strip())

    return {
        "api": "libvirt2",
        "ok": True,
        "name": name,
        "action": "safe_snapshot_delete",
        "engine": "safe-copy",
        "snapshot": snap_id,
        "status": "deleting",
        "pid": out.strip(),
        "message": "Suppression du snapshot sûr lancée en tâche de fond.",
        "snapshots": _lv2_snapshot_list(name).get("snapshots", []),
    }



# MDM-LIBVIRT2-SAFE-COPY-RESTORE-20260711
_LV2_SAFE_RESTORE_BACKUP_ROOT = "/mnt/Truenas_Stockage/vms/_restore_backups"


def _lv2_safe_restore_relpath(value, prefix):
    value = str(value or "").strip().replace("\\", "/")
    if not value or value.startswith("/") or ".." in value.split("/"):
        raise RuntimeError("Chemin snapshot invalide")
    if prefix and not value.startswith(prefix.rstrip("/") + "/"):
        raise RuntimeError("Chemin snapshot inattendu: " + value)
    return value


def _lv2_snapshot_revert(name, snapshot):
    _lv2_snapshot_vm_must_be_stopped(name)

    snap_id = _lv2_snapshot_name_norm(snapshot)
    vm_dir = _lv2_safe_vm_dir(name)
    snap_dir = vm_dir.rstrip("/") + "/" + snap_id
    if not snap_dir.startswith(vm_dir.rstrip("/") + "/"):
        raise RuntimeError("Chemin snapshot invalide")

    meta = _lv2_safe_snapshot_meta_read(vm_dir, snap_id)
    if not meta:
        raise RuntimeError("Snapshot sûr introuvable")
    if meta.get("engine") != "safe-copy":
        raise RuntimeError("Le snapshot n'est pas de type safe-copy")
    if meta.get("status") != "ready":
        raise RuntimeError("Snapshot non restaurable: " + str(meta.get("status") or "unknown"))
    if str(meta.get("vm") or "") != str(name):
        raise RuntimeError("Le snapshot appartient à une autre VM")

    saved_disks = meta.get("disks")
    if not isinstance(saved_disks, list) or not saved_disks:
        raise RuntimeError("metadata.json ne contient aucun disque")

    _xml, current_disks, current_nvram = _lv2_safe_vm_files_from_xml(name)
    current_by_target = {str(item.get("target") or ""): item for item in current_disks}
    if len(current_by_target) != len(current_disks):
        raise RuntimeError("Cibles de disques actuelles invalides")

    items = []
    for index, saved in enumerate(saved_disks):
        if not isinstance(saved, dict):
            raise RuntimeError("Entrée disque invalide")
        target = str(saved.get("target") or "")
        current = current_by_target.get(target)
        if not target or current is None:
            raise RuntimeError("Le disque " + (target or "?") + " n'existe plus")
        if str(saved.get("source") or "") != str(current.get("source") or ""):
            raise RuntimeError("Le chemin actif du disque " + target + " a changé")
        rel = _lv2_safe_restore_relpath(saved.get("dest"), "disks")
        items.append({
            "snapshot": snap_dir + "/" + rel,
            "active": current["source"],
            "backup": "disk-" + str(index) + "-" + _lv2_safe_file_basename(current["source"]),
        })

    if len(items) != len(current_disks):
        raise RuntimeError("Le nombre de disques du snapshot diffère de la VM actuelle")

    saved_nvram = meta.get("nvram")
    if saved_nvram:
        if not isinstance(saved_nvram, dict) or not current_nvram:
            raise RuntimeError("Configuration NVRAM incompatible")
        if str(saved_nvram.get("source") or "") != str(current_nvram.get("source") or ""):
            raise RuntimeError("Le chemin NVRAM actif a changé")
        rel = _lv2_safe_restore_relpath(saved_nvram.get("dest"), "nvram")
        items.append({
            "snapshot": snap_dir + "/" + rel,
            "active": current_nvram["source"],
            "backup": "nvram-" + _lv2_safe_file_basename(current_nvram["source"]),
        })
    elif current_nvram:
        raise RuntimeError("Le snapshot ne contient pas la NVRAM actuelle")

    domain_rel = _lv2_safe_restore_relpath(meta.get("domain_xml") or "domain.xml", "")
    domain_snapshot = snap_dir + "/" + domain_rel
    restore_id = "restore-" + _lv2_safe_time.strftime("%Y%m%d-%H%M%S")
    safe_vm = _lv2_safe_re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))
    backup_dir = _LV2_SAFE_RESTORE_BACKUP_ROOT + "/" + safe_vm + "/" + restore_id
    domain_backup = backup_dir + "/domain-before.xml"
    script_path = snap_dir + "/" + restore_id + ".sh"
    log_path = snap_dir + "/logs/" + restore_id + ".log"

    lines = [
        "#!/bin/bash",
        "set -Eeuo pipefail",
        "umask 077",
        "mkdir -p " + shq(backup_dir) + " " + shq(snap_dir + "/logs"),
        "sudo -n test -f " + shq(domain_snapshot),
        "sudo -n virsh -c " + shq(VIRSH_URI) + " dumpxml --inactive " + shq(name)
        + " > " + shq(domain_backup),
    ]

    pairs = []
    for item in items:
        backup = backup_dir + "/" + item["backup"]
        temp = item["active"] + ".truenas-" + restore_id
        pairs.append((item, backup, temp))
        lines.extend([
            "sudo -n test -f " + shq(item["snapshot"]),
            "sudo -n test -f " + shq(item["active"]),
            "sudo -n cp --reflink=auto --sparse=always --preserve=all "
            + shq(item["active"]) + " " + shq(backup),
        ])

    lines.extend(["rollback() {", "  rc=$?", "  set +e"])
    for item, backup, temp in pairs:
        lines.extend([
            "  sudo -n rm -f -- " + shq(temp),
            "  sudo -n cp --reflink=auto --sparse=always --preserve=all "
            + shq(backup) + " " + shq(item["active"]),
        ])
    lines.extend([
        "  sudo -n virsh -c " + shq(VIRSH_URI) + " define " + shq(domain_backup) + " >/dev/null 2>&1",
        "  exit $rc",
        "}",
        "trap rollback ERR",
    ])

    for item, _backup, temp in pairs:
        lines.extend([
            "sudo -n rm -f -- " + shq(temp),
            "sudo -n cp --reflink=auto --sparse=always " + shq(item["snapshot"]) + " " + shq(temp),
            "sudo -n chown --reference=" + shq(item["active"]) + " " + shq(temp),
            "sudo -n chmod --reference=" + shq(item["active"]) + " " + shq(temp),
            "sudo -n mv -f -- " + shq(temp) + " " + shq(item["active"]),
        ])

    lines.extend([
        "sudo -n virsh -c " + shq(VIRSH_URI) + " define " + shq(domain_snapshot),
        "trap - ERR",
    ])
    script = "\n".join(lines)

    marked = dict(meta)
    marked["status"] = "restoring"
    marked["error"] = ""
    marked_json = _lv2_safe_json.dumps(marked, ensure_ascii=False, indent=2)

    wrapped = (
        "mkdir -p " + shq(snap_dir + "/logs") + "; "
        "cat > " + shq(snap_dir + "/metadata.json") + " <<'EOF_META'\n"
        + marked_json + "\nEOF_META\n"
        "cat > " + shq(script_path) + " <<'EOF_SCRIPT'\n"
        + script + "\nEOF_SCRIPT\n"
        "chmod 700 " + shq(script_path) + "; "
        "( " + shq(script_path) + " > " + shq(log_path) + " 2>&1; rc=$?; "
        "python3 - " + shq(snap_dir + "/metadata.json") + " " + shq(log_path)
        + " " + shq(backup_dir) + " $rc <<'EOF_STATUS'\n"
        "import json, sys, time\n"
        "p, log, backup, rc = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])\n"
        "m=json.load(open(p,'r',encoding='utf-8'))\n"
        "m['status']='ready'\n"
        "m['last_restore']={'status':'success' if rc == 0 else 'error','finished_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'backup':backup}\n"
        "if rc:\n"
        "    m['error']='Dernière restauration échouée: '+open(log,'r',encoding='utf-8',errors='replace').read()[-4000:]\n"
        "else:\n"
        "    m['error']=''\n"
        "json.dump(m,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=2)\n"
        "EOF_STATUS\n"
        ") & echo $!"
    )

    out, err, code = ssh_exec(wrapped, timeout=20)
    if code != 0:
        raise RuntimeError((err or out or "Impossible de lancer la restauration sûre").strip())

    return {
        "api": "libvirt2",
        "ok": True,
        "name": name,
        "action": "safe_snapshot_restore",
        "engine": "safe-copy",
        "snapshot": snap_id,
        "status": "restoring",
        "pid": out.strip(),
        "backup": backup_dir,
        "log": log_path,
        "message": "Restauration sûre lancée en tâche de fond.",
    }

# MDM-LIBVIRT-WATCHDOG-START-V1-BEGIN
_threading.Thread(
    target=_libvirt_watchdog,
    name='libvirt-autorecover',
    daemon=True,
).start()
# MDM-LIBVIRT-WATCHDOG-START-V1-END

# MDM-HOST-BOOTSTRAP-START-V1 : configure le host (libvirtd/polkit/réseau) au boot.
if os.environ.get('HOST_BOOTSTRAP', '1').lower() not in ('0', 'false', 'no', 'off'):
    _threading.Thread(
        target=_host_bootstrap,
        name='host-bootstrap',
        daemon=True,
    ).start()

# MDM-SHARE-LINKS-V1 : purge périodique des liens expirés / épuisés
_threading.Thread(
    target=_share_purge_loop,
    name='share-purge',
    daemon=True,
).start()

with _TCPServer(('', HTTP_PORT), FileOpsHandler) as srv:
        srv.serve_forever()
