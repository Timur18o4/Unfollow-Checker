#!/usr/bin/env python3
"""Unfollow Checker: Twitch follow monitor and LAN setup web interface."""
import argparse
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "Unfollow Checker"
DEFAULT_CLIENT_ID = "9bm0ou31egoqeobo3sl8a9cvpldcb3"
API = "https://api.twitch.tv/helix"
AUTH = "https://id.twitch.tv/oauth2"
LOG = logging.getLogger("unfollow-checker")
POLL_SECONDS = 600


class AppError(Exception):
    pass


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.config_path = self.directory / "config.json"
        self.credentials_path = self.directory / "credentials.json"
        self.pin_path = self.directory / "web.pin"
        self.db_path = self.directory / "state.sqlite3"
        self.lock = threading.RLock()
        self.auth_flows = {}
        self.last_result = "Not configured"
        self.last_check = None
        self.last_error = None
        self.next_check = None
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS followers(user_id TEXT PRIMARY KEY, login TEXT NOT NULL, display_name TEXT NOT NULL, followed_at TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    def read(self, path, default=None):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default

    def write(self, path, value):
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(path)

    def config(self):
        return self.read(self.config_path, {})

    def credentials(self):
        return self.read(self.credentials_path, {})

    def pin(self):
        try:
            return self.pin_path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return ""

    def status(self):
        config, creds = self.config(), self.credentials()
        mode = config.get("mode")
        roles = ["broadcaster"] + (["bot"] if mode == "separate" else [])
        ready = bool(config.get("client_id") and config.get("channel_login")) and all(
            creds.get(role, {}).get("access_token") for role in roles
        )
        with sqlite3.connect(self.db_path) as db:
            count = db.execute("SELECT COUNT(*) FROM followers").fetchone()[0]
            baseline = db.execute("SELECT value FROM meta WHERE key='initialized'").fetchone()
        auth = {}
        for role in roles:
            flow = self.auth_flows.get(role, {})
            auth[role] = {
                "authorized": bool(creds.get(role, {}).get("access_token")),
                "pending": bool(flow.get("device_code")),
                "user_code": flow.get("user_code"),
                "verification_uri": flow.get("verification_uri"),
                "error": flow.get("error"),
            }
        return {
            "configured": bool(config.get("channel_login")),
            "ready": ready,
            "mode": mode,
            "channel_login": config.get("channel_login"),
            "bot_login": config.get("bot_login"),
            "auth": auth,
            "follower_count": count if baseline else None,
            "last_check": self.last_check,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "next_check": self.next_check,
            "poll_minutes": POLL_SECONDS // 60,
        }


def request_json(url, method="GET", headers=None, data=None, form=False, return_http_errors=False):
    req_headers = dict(headers or {})
    payload = None
    if data is not None:
        if form:
            payload = urllib.parse.urlencode(data).encode("utf-8")
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            payload = json.dumps(data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if return_http_errors:
            try:
                return json.loads(detail)
            except ValueError:
                return {"status": exc.code, "message": detail}
        raise AppError(f"Twitch HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AppError(f"Twitch connection failed: {exc}") from exc


class Twitch:
    def __init__(self, store):
        self.store = store

    def role(self, role):
        config = self.store.config()
        return "bot" if role == "bot" and config.get("mode") == "separate" else "broadcaster"

    def token(self, role):
        config = self.store.config()
        actual_role = self.role(role)
        creds = self.store.credentials()
        item = creds.get(actual_role)
        if not item or not item.get("access_token"):
            raise AppError(f"OAuth login missing for {actual_role}.")
        if item.get("expires_at", 0) < time.time() + 120:
            result = request_json(AUTH + "/token", "POST", data={
                "grant_type": "refresh_token",
                "refresh_token": item["refresh_token"],
                "client_id": config["client_id"],
            }, form=True)
            item.update(
                access_token=result["access_token"],
                refresh_token=result.get("refresh_token", item["refresh_token"]),
                expires_at=time.time() + result.get("expires_in", 14400),
                validated_at=0,
            )
            self.store.write(self.store.credentials_path, creds)
        if item.get("validated_at", 0) < time.time() - 3600:
            validation = request_json(AUTH + "/validate", headers={"Authorization": "OAuth " + item["access_token"]})
            if validation.get("client_id") != config["client_id"]:
                raise AppError("OAuth token does not belong to the configured Twitch app.")
            item["validated_at"] = time.time()
            item["scopes"] = validation.get("scopes", item.get("scopes", []))
            item["login"] = validation.get("login", item.get("login"))
            item["user_id"] = validation.get("user_id", item.get("user_id"))
            self.store.write(self.store.credentials_path, creds)
        return item["access_token"]

    def api(self, role, path, params=None, method="GET", body=None):
        config = self.store.config()
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Authorization": "Bearer " + self.token(role), "Client-Id": config["client_id"]}
        return request_json(url, method=method, headers=headers, data=body)

    def me(self, role):
        users = self.api(role, "/users").get("data", [])
        if len(users) != 1:
            raise AppError("Could not validate the connected Twitch account.")
        return users[0]

    def start_device_flow(self, role):
        config = self.store.config()
        if role not in ("broadcaster", "bot"):
            raise AppError("Invalid account role.")
        if role == "bot" and config.get("mode") != "separate":
            raise AppError("A second account is not enabled in this setup.")
        scopes = "moderator:read:followers user:write:chat" if role == "broadcaster" and config.get("mode") == "same" else (
            "moderator:read:followers" if role == "broadcaster" else "user:write:chat"
        )
        flow = request_json(AUTH + "/device", method="POST", data={
            "client_id": config["client_id"], "scopes": scopes,
        }, form=True)
        if not flow.get("device_code"):
            raise AppError("Could not start Twitch device authorization.")
        self.store.auth_flows[role] = {
            "device_code": flow["device_code"],
            "expires_at": time.time() + int(flow.get("expires_in", 1800)),
            "interval": max(5, int(flow.get("interval", 5))),
            "verification_uri": flow.get("verification_uri", "https://www.twitch.tv/activate"),
            "user_code": flow.get("user_code"),
            "scope": scopes,
            "next_poll": time.time(),
        }
        return {k: self.store.auth_flows[role][k] for k in ("verification_uri", "user_code")}

    def poll_device_flow(self, role):
        flow = self.store.auth_flows.get(role)
        if not flow or not flow.get("device_code") or time.time() < flow.get("next_poll", 0):
            return
        if time.time() >= flow["expires_at"]:
            flow.update(device_code=None, error="Code expired. Start authorization again.")
            return
        config = self.store.config()
        result = request_json(AUTH + "/token", method="POST", data={
            "client_id": config["client_id"],
            "scopes": flow["scope"],
            "device_code": flow["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }, form=True, return_http_errors=True)
        status = result.get("error") or result.get("message")
        if result.get("access_token"):
            item = {
                "access_token": result["access_token"],
                "refresh_token": result.get("refresh_token"),
                "expires_at": time.time() + result.get("expires_in", 14400),
                "scopes": result.get("scope", []),
            }
            creds = self.store.credentials()
            creds[role] = item
            self.store.write(self.store.credentials_path, creds)
            flow.update(device_code=None, error=None)
            try:
                who = self.me(role)
                creds = self.store.credentials()
                creds[role].update(user_id=who["id"], login=who["login"])
                self.store.write(self.store.credentials_path, creds)
            except Exception as exc:
                flow["error"] = str(exc)
            return
        if status not in ("authorization_pending", "slow_down"):
            flow.update(device_code=None, error=str(result.get("message", status or "Authorization failed")))
            return
        if status == "slow_down":
            flow["interval"] += 5
        flow["next_poll"] = time.time() + flow["interval"]

    def validate_setup(self):
        config = self.store.config()
        creds = self.store.credentials()
        channel = self.me("broadcaster")
        if channel["login"].lower() != config["channel_login"].lower():
            raise AppError(f"The broadcaster login is {channel['login']}, expected {config['channel_login']}.")
        if config.get("mode") == "separate":
            bot = self.me("bot")
            if bot["login"].lower() != config.get("bot_login", "").lower():
                raise AppError(f"The chat account login is {bot['login']}, expected {config.get('bot_login')}.")
        else:
            config["bot_login"] = channel["login"]
        config["broadcaster_id"] = channel["id"]
        self.store.write(self.store.config_path, config)
        # A real API read confirms broadcaster access before the background worker starts.
        snapshot = self.followers()
        return snapshot

    def followers(self):
        config = self.store.config()
        rows, cursor, total = [], None, None
        for _ in range(10000):
            params = {"broadcaster_id": config["broadcaster_id"], "first": 100}
            if cursor:
                params["after"] = cursor
            page = self.api("broadcaster", "/channels/followers", params)
            rows.extend(page.get("data", []))
            total = page.get("total")
            cursor = page.get("pagination", {}).get("cursor")
            if not cursor:
                break
        else:
            raise AppError("Follower pagination reached its safety limit.")
        if total is None or total != len(rows):
            raise AppError(f"Follower data was inconsistent (API total {total}, loaded {len(rows)}); no comparison performed.")
        ids = [row["user_id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise AppError("Follower response contained duplicate IDs; comparison aborted.")
        return {row["user_id"]: row for row in rows}

    def lookup_users(self, ids):
        found = {}
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            if chunk:
                params = [("id", user_id) for user_id in chunk]
                page = self.api("broadcaster", "/users", params)
                found.update({user["id"]: user for user in page.get("data", [])})
        return found

    def send_chat(self, message):
        config = self.store.config()
        sender = self.me("bot")
        payload = {"broadcaster_id": config["broadcaster_id"], "sender_id": sender["id"], "message": message}
        result = self.api("bot", "/chat/messages", method="POST", body=payload).get("data", [{}])[0]
        if result.get("is_sent") is False:
            raise AppError("Twitch did not send the chat message: " + str(result.get("drop_reason")))

    def poll_once(self, current=None):
        if current is None:
            current = self.followers()
        with sqlite3.connect(self.store.db_path) as db:
            previous = {row[0]: {"user_id": row[0], "user_login": row[1], "user_name": row[2], "followed_at": row[3]}
                        for row in db.execute("SELECT user_id,login,display_name,followed_at FROM followers")}
            initialized = db.execute("SELECT value FROM meta WHERE key='initialized'").fetchone()
            if not initialized:
                db.execute("DELETE FROM followers")
                db.executemany("INSERT INTO followers VALUES(?,?,?,?)", [
                    (row["user_id"], row["user_login"], row["user_name"], row.get("followed_at")) for row in current.values()
                ])
                db.execute("INSERT OR REPLACE INTO meta VALUES('initialized',?)", (datetime.now(timezone.utc).isoformat(),))
                db.commit()
                self.store.last_result = f"Baseline saved: {len(current)} followers. No chat message sent."
                LOG.info(self.store.last_result)
                return
            removed = [previous[key] for key in previous.keys() - current.keys()]
            added = [current[key] for key in current.keys() - previous.keys()]
            accessible = self.lookup_users([row["user_id"] for row in removed])
            unavailable = [row for row in removed if row["user_id"] not in accessible]
            unfollowed = [row for row in removed if row["user_id"] in accessible]
            events = [("unfollow", row) for row in unfollowed] + [("unavailable", row) for row in unavailable]
            if len(events) > 3:
                self.send_chat(f"Since the last check: {len(unfollowed)} unfollows; {len(unavailable)} accounts are no longer reachable (possibly deactivated).")
            elif events:
                parts = []
                for kind, row in events:
                    name = row.get("user_name") or row.get("user_login") or row["user_id"]
                    if kind == "unfollow":
                        parts.append(f"{name} no longer follows {config_channel(self.store)}.")
                    else:
                        parts.append(f"{name} is no longer reachable (possibly deactivated).")
                self.send_chat(" | ".join(parts)[:480])
            # Advance only after every required notification has been accepted by Twitch.
            db.execute("DELETE FROM followers")
            db.executemany("INSERT INTO followers VALUES(?,?,?,?)", [
                (row["user_id"], row["user_login"], row["user_name"], row.get("followed_at")) for row in current.values()
            ])
            db.commit()
        self.store.last_result = f"Check complete: {len(current)} followers; {len(added)} new; {len(unfollowed)} unfollows; {len(unavailable)} unavailable."
        LOG.info(self.store.last_result)


def config_channel(store):
    return store.config().get("channel_login", "the channel")


def worker(store, twitch):
    while True:
        try:
            state = store.status()
            if state["ready"]:
                snapshot = twitch.validate_setup()
                twitch.poll_once(snapshot)
                store.last_error = None
                store.last_check = datetime.now(timezone.utc).isoformat()
            else:
                store.last_result = "Waiting for Twitch setup."
            store.next_check = datetime.now(timezone.utc).timestamp() + POLL_SECONDS
        except Exception as exc:
            store.last_error = str(exc)
            LOG.exception("Check failed; follower state was left unchanged.")
        time.sleep(POLL_SECONDS)


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Unfollow Checker</title><style>
:root{font:16px system-ui;color:#222;background:#f3f5f7}body{margin:0}.wrap{max-width:760px;margin:30px auto;padding:16px}.card{background:white;border-radius:14px;padding:24px;margin:16px 0;box-shadow:0 3px 16px #0001}h1{margin:.2em 0}h2{margin-top:0}label{display:block;margin:12px 0 4px}input[type=text],input[type=password]{width:100%;box-sizing:border-box;padding:11px;border:1px solid #bbc2ca;border-radius:8px;font:inherit}button{padding:10px 16px;border:0;border-radius:8px;background:#6b35c8;color:white;font:inherit;cursor:pointer}button:disabled{opacity:.45;cursor:wait}.muted{color:#626b75}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.status{padding:12px;border-radius:8px;background:#eef1f4}.hidden{display:none}.error{color:#a22}.ok{color:#176b37}code{word-break:break-all}.step{border-top:1px solid #e6e8eb;padding-top:16px;margin-top:16px}
</style></head><body><div class="wrap"><header><h1>Unfollow Checker</h1><p class="muted">Twitch follow monitoring for Raspberry Pi</p></header>
<section class="card" id="login"><h2>Zugriff auf die Einrichtung</h2><p>Gib die PIN ein, die der Installer auf dem Raspberry Pi angezeigt hat.</p><label for="pin">Einrichtungs-PIN</label><input id="pin" type="password" autocomplete="current-password"><p><button onclick="login()">Weiter</button></p><p id="loginError" class="error"></p></section>
<main id="main" class="hidden">
<section class="card"><h2>1. Konten auswählen</h2><label>Zu überwachender Kanal</label><input id="channel" type="text" value="" placeholder="Twitch-Kanalname"><label><input type="radio" name="mode" value="same" checked> Mit diesem Konto prüfen und im Chat schreiben</label><label><input type="radio" name="mode" value="separate"> Mit einem zweiten Konto im Chat schreiben</label><div id="botWrap" class="hidden"><label for="bot">Zweites Chatkonto</label><input id="bot" type="text" placeholder="Twitch-Kontoname"></div><p class="muted">Der Abgleich läuft alle 10 Minuten. Der erste erfolgreiche Abruf speichert den Ausgangsstand und sendet keine Nachricht.</p><button onclick="saveSetup()">Speichern und weiter</button><p id="setupError" class="error"></p></section>
<section class="card"><h2>2. Twitch-Konten freigeben</h2><p class="muted">Der Pi erhält die Tokens direkt von Twitch. Füge niemals Tokens in diese Webseite ein.</p><div id="authSteps"></div></section>
<section class="card"><h2>3. Status</h2><div id="status" class="status">Wird geladen…</div></section></main></div>
<script>
let pin=sessionStorage.getItem('ufc-pin')||'';let currentMode='';let loadedConfig=false;
document.querySelectorAll('input[name=mode]').forEach(x=>x.onchange=()=>document.getElementById('botWrap').classList.toggle('hidden',document.querySelector('input[name=mode]:checked').value!=='separate'));
async function api(path,body){let r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json','Authorization':'Bearer '+pin},body:body?JSON.stringify(body):undefined});let d=await r.json();if(!r.ok)throw Error(d.error||'Request failed');return d}
async function login(){pin=document.getElementById('pin').value;try{await api('/api/state');sessionStorage.setItem('ufc-pin',pin);showMain()}catch(e){document.getElementById('loginError').textContent='PIN ungültig oder Pi-Weboberfläche nicht erreichbar.'}}
function showMain(){document.getElementById('login').classList.add('hidden');document.getElementById('main').classList.remove('hidden');refresh()}
async function saveSetup(){let mode=document.querySelector('input[name=mode]:checked').value;try{await api('/api/setup',{channel_login:document.getElementById('channel').value.trim(),mode,bot_login:mode==='separate'?document.getElementById('bot').value.trim():''});document.getElementById('setupError').textContent='Gespeichert. Bitte die Konten erneut freigeben.';document.getElementById('setupError').className='ok';refresh()}catch(e){document.getElementById('setupError').textContent='Bitte Kanalnamen und gegebenenfalls zweiten Kontonamen prüfen.'}}
async function start(role){try{let d=await api('/api/auth/start',{role});renderCode(role,d)}catch(e){alert(e.message)}}
function esc(v){return String(v||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;')}
function renderCode(role,d){let el=document.getElementById('auth-'+role);if(!el)return;let u;try{u=new URL(d.verification_uri);if(u.protocol!=='https:'||!['twitch.tv','www.twitch.tv'].includes(u.hostname))throw Error()}catch(e){u=new URL('https://www.twitch.tv/activate')}el.innerHTML=`<p>Melde dich beim passenden Twitch-Konto an und erlaube den Zugriff:</p><p><a href="${esc(u.href)}" target="_blank" rel="noopener">Twitch-Geräteaktivierung öffnen</a></p><p>Code: <strong><code>${esc(d.user_code)}</code></strong></p><p class="muted">Der Code läuft nach etwa 30 Minuten ab.</p>`}
function renderAuth(s){let host=document.getElementById('authSteps');host.innerHTML='';if(!s.configured){host.innerHTML='<p>Bitte zuerst Konten auswählen und speichern.</p>';return}let roles=[['broadcaster',s.channel_login]];if(s.mode==='separate')roles.push(['bot',s.bot_login]);for(let [role,label] of roles){let a=s.auth[role]||{};let div=document.createElement('div');div.className='step';div.id='auth-'+role;div.innerHTML=`<h3>${esc(label||role)}</h3>${a.authorized?'<p class="ok">Freigegeben</p>':a.pending?'<p class="muted">Warte auf Bestätigung bei Twitch…</p>':`<button onclick="start('${role}')">${esc(label||role)} freigeben</button>`}${a.error?`<p class="error">${esc(a.error)}</p>`:''}`;host.appendChild(div);if(a.pending)renderCode(role,a)}}
async function refresh(){try{let s=await api('/api/state');currentMode=s.mode;if(!loadedConfig){if(s.configured){document.getElementById('channel').value=s.channel_login||'';document.getElementById('bot').value=s.bot_login||'';document.querySelector(`input[name=mode][value="${s.mode}"]`).checked=true;document.getElementById('botWrap').classList.toggle('hidden',s.mode!=='separate')}loadedConfig=true}renderAuth(s);document.getElementById('status').textContent=`${s.ready?'Läuft':'Einrichtung nötig'} · ${s.follower_count===null?'Noch kein Ausgangsstand':s.follower_count+' Follower'} · ${s.last_result}${s.last_error?' · Fehler: '+s.last_error:''}`;if(s.last_check)document.getElementById('status').title='Letzter Abruf: '+s.last_check}catch(e){}setTimeout(refresh,4000)}
if(pin){document.getElementById('pin').value=pin;login()}
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "UnfollowChecker/1.0"

    def log_message(self, fmt, *args):
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def send_json(self, code, value):
        payload = json.dumps(value).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def body(self):
        length = min(int(self.headers.get("Content-Length", 0)), 16384)
        return json.loads(self.rfile.read(length) or b"{}")

    def authorized(self):
        expected = self.server.store.pin().encode()
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ").encode()
        return bool(expected) and hmac.compare_digest(expected, supplied)

    def do_GET(self):
        if self.path == "/":
            page = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if self.path == "/api/state":
            if not self.authorized():
                return self.send_json(401, {"error": "Invalid setup PIN."})
            try:
                for role in ("broadcaster", "bot"):
                    self.server.twitch.poll_device_flow(role)
                return self.send_json(200, self.server.store.status())
            except Exception as exc:
                self.server.store.last_error = str(exc)
                return self.send_json(503, {"error": "Twitch ist gerade nicht erreichbar. Bitte erneut versuchen."})
        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self.authorized():
            return self.send_json(401, {"error": "Invalid setup PIN."})
        try:
            payload = self.body()
            if self.path == "/api/setup":
                channel = str(payload.get("channel_login", "")).strip().lower()
                mode = payload.get("mode")
                bot = str(payload.get("bot_login", "")).strip().lower()
                if not channel or len(channel) > 25 or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in channel):
                    raise AppError("Enter a valid Twitch channel login.")
                if mode not in ("same", "separate"):
                    raise AppError("Choose whether to use one account or two.")
                if mode == "separate" and (not bot or len(bot) > 25 or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_" for ch in bot)):
                    raise AppError("Enter a valid second Twitch account login.")
                config = self.server.store.config()
                config.update(client_id=DEFAULT_CLIENT_ID, channel_login=channel, bot_login=bot or channel, mode=mode)
                self.server.store.write(self.server.store.config_path, config)
                # Reauthorizing clears only tokens for account roles whose scopes/identity may have changed.
                self.server.store.auth_flows.clear()
                self.server.store.write(self.server.store.credentials_path, {})
                return self.send_json(200, {"ok": True})
            if self.path == "/api/auth/start":
                result = self.server.twitch.start_device_flow(payload.get("role"))
                return self.send_json(200, result)
            if self.path == "/api/validate":
                self.server.twitch.validate_setup()
                return self.send_json(200, {"ok": True})
            return self.send_json(404, {"error": "Not found"})
        except Exception as exc:
            return self.send_json(400, {"error": str(exc)})

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.environ.get("DATA_DIR", str(Path.home() / ".config/unfollow-checker")))
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(args.data_dir)
    if not store.pin():
        pin = secrets.token_urlsafe(15)
        store.pin_path.write_text(pin + "\n", encoding="ascii")
        os.chmod(store.pin_path, 0o600)
        LOG.warning("No setup PIN found. Generated one-time local PIN: %s", pin)
    twitch = Twitch(store)
    thread = threading.Thread(target=worker, args=(store, twitch), daemon=True)
    thread.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.store, server.twitch = store, twitch
    LOG.info("Web setup listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
