"""Microsoft 365: accesso (device code flow, solo permesso di lettura posta) e connettore Outlook via Graph."""
from __future__ import annotations
import hashlib, threading, time, urllib.parse
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import httpx

from .connectors.base import RawDoc
from .parsers import parse_eml_bytes

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read"]          # offline_access è aggiunto da msal. Sola lettura: l'app non può inviare né cancellare.


def friendly(desc: str) -> str:
    """Traduce gli errori più comuni di Microsoft Entra in azioni concrete."""
    import re
    m = re.search(r"AADSTS\d+", desc or "")
    code = m.group(0) if m else ""
    hints = {
        "AADSTS700016": "L'ID applicazione non esiste in questa directory. Deve essere l'«ID applicazione (client)» di un'app "
                        "registrata (Registrazioni app → SecondBrain → Panoramica), non l'ID del tuo utente.",
        "AADSTS7000218": "Nell'app registrata attiva Autenticazione → «Consenti flussi client pubblici» = Sì.",
        "AADSTS70002": "Nell'app registrata attiva Autenticazione → «Consenti flussi client pubblici» = Sì.",
        "AADSTS65001": "Serve il consenso dell'amministratore per il permesso Mail.Read: chiedilo all'IT.",
        "AADSTS90094": "Serve il consenso dell'amministratore per il permesso Mail.Read: chiedilo all'IT.",
        "AADSTS900971": "Tenant non valido: usa l'ID directory o il dominio aziendale (es. azienda.com).",
        "AADSTS90002": "Tenant non trovato: controlla l'ID directory o il dominio aziendale.",
    }
    msg = hints.get(code)
    return f"{msg} ({code})" if msg else (desc or "accesso non riuscito").split("\r")[0][:300]


class M365Auth:
    """Tiene client id/tenant (nel database) e il token (file 0600 in data_dir)."""

    def __init__(self, store, data_dir):
        self.store = store
        self.cache_path = Path(data_dir) / "m365_token.json"
        self.state, self.error, self.flow = "none", None, None
        self._lock = threading.Lock()

    # --- configurazione ---
    @property
    def client_id(self):
        return self.store.get_setting("m365_client_id", "")

    @property
    def tenant(self):
        return self.store.get_setting("m365_tenant", "organizations")

    def configure(self, client_id, tenant):
        self.store.set_setting("m365_client_id", client_id.strip())
        self.store.set_setting("m365_tenant", (tenant or "organizations").strip())

    def _app(self):
        import msal
        cache = msal.SerializableTokenCache()
        if self.cache_path.exists():
            cache.deserialize(self.cache_path.read_text())
        app = msal.PublicClientApplication(
            self.client_id, authority=f"https://login.microsoftonline.com/{self.tenant}", token_cache=cache)
        return app, cache

    def _save(self, cache):
        if cache.has_state_changed:
            self.cache_path.write_text(cache.serialize())
            self.cache_path.chmod(0o600)

    # --- accesso ---
    def login(self):
        if not self.client_id:
            raise ValueError("Inserisci prima l'ID applicazione (client id)")
        app, cache = self._app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(friendly(flow.get("error_description", "")))
        self.flow, self.state, self.error = flow, "pending", None

        def wait():
            res = app.acquire_token_by_device_flow(flow)
            with self._lock:
                if "access_token" in res:
                    self._save(cache)
                    self.state, self.flow = "signed_in", None
                else:
                    self.state, self.flow = "error", None
                    self.error = friendly(res.get("error_description", ""))
        threading.Thread(target=wait, daemon=True).start()

    def account(self):
        if not self.client_id or not self.cache_path.exists():
            return None
        accs = self._app()[0].get_accounts()
        return accs[0]["username"] if accs else None

    def info(self):
        acc = None if self.state == "pending" else self.account()
        state = "pending" if self.state == "pending" else "signed_in" if acc else "error" if self.state == "error" else "none"
        d = {"client_id": self.client_id, "tenant": self.tenant, "state": state, "account": acc, "error": self.error}
        if state == "pending" and self.flow:
            d.update(user_code=self.flow["user_code"], uri=self.flow["verification_uri"])
        return d

    def logout(self):
        self.cache_path.unlink(missing_ok=True)
        self.state, self.flow, self.error = "none", None, None

    def token(self):
        app, cache = self._app()
        accs = app.get_accounts()
        if not accs:
            raise RuntimeError("non hai effettuato l'accesso a Microsoft 365 (scheda Sorgenti)")
        res = app.acquire_token_silent(SCOPES, account=accs[0])
        if not res or "access_token" not in res:
            raise RuntimeError("sessione Microsoft 365 scaduta: ripeti l'accesso dalla scheda Sorgenti")
        self._save(cache)
        return res["access_token"]


class Outlook365Connector:
    """Posta Outlook online via Microsoft Graph, sola lettura. I messaggi sono salvati come .eml in cache
    (per anteprima) e indicizzati; 'Apri in Outlook' usa il link web del messaggio."""

    def __init__(self, source, cache_dir, token_fn, client: httpx.Client | None = None):
        self.source = source
        self.token_fn = token_fn
        self.out = Path(cache_dir) / "m365" / source.name.replace("/", "_")
        self.out.mkdir(parents=True, exist_ok=True)
        self.http = client or httpx.Client(timeout=60)
        self.skipped = []

    def roots(self):
        return [self.out]

    # --- HTTP con rinnovo token e gestione limiti di velocità ---
    def _get(self, url, params=None, headers=None):
        for attempt in range(6):
            h = {"Authorization": f"Bearer {self.token_fn()}", **(headers or {})}
            r = self.http.get(url if url.startswith("http") else GRAPH + url, params=params, headers=h)
            if r.status_code in (429, 503, 504):
                time.sleep(min(float(r.headers.get("Retry-After", 2 ** attempt)), 60))
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json()["error"]["message"]
                except Exception:
                    msg = r.text[:200]
                raise RuntimeError(f"Graph {r.status_code}: {msg}")
            return r.json()
        raise RuntimeError("Graph: troppe richieste, riprova più tardi")

    def _folders(self):
        names, skip = {}, set()
        for wk in ("deleteditems", "junkemail"):
            try:
                skip.add(self._get(f"/me/mailFolders/{wk}", {"$select": "id"})["id"])
            except RuntimeError:
                pass

        def walk(url, prefix):
            data = self._get(url, {"$top": "100", "$select": "id,displayName,childFolderCount"})
            while True:
                for f in data.get("value", []):
                    path = f"{prefix}{f['displayName']}"
                    names[f["id"]] = path
                    if f.get("childFolderCount"):
                        walk(f"/me/mailFolders/{urllib.parse.quote(f['id'], safe='')}/childFolders", path + "/")
                if "@odata.nextLink" not in data:
                    break
                data = self._get(data["@odata.nextLink"])
        walk("/me/mailFolders", "")
        return names, skip

    def scan(self):
        months = int(self.source.options.get("months", 12))
        limit = int(self.source.options.get("max_messages", 30000))
        since = (datetime.now(timezone.utc) - timedelta(days=30 * months)).strftime("%Y-%m-%dT%H:%M:%SZ")
        folders, skip = self._folders()
        data = self._get("/me/messages", {
            "$select": "id,subject,lastModifiedDateTime,receivedDateTime,parentFolderId,webLink",
            "$filter": f"receivedDateTime ge {since}", "$orderby": "receivedDateTime desc", "$top": "100"})
        n = 0
        while True:
            for m in data.get("value", []):
                if m.get("parentFolderId") in skip:
                    continue
                n += 1
                if n > limit:
                    return
                yield self._doc(m, folders)
            if "@odata.nextLink" not in data:
                return
            data = self._get(data["@odata.nextLink"])

    def _doc(self, m, folders):
        mid = m["id"]
        path = self.out / (hashlib.sha1(mid.encode()).hexdigest() + ".eml")
        mtime = datetime.fromisoformat(m["lastModifiedDateTime"].replace("Z", "+00:00")).timestamp()
        folder = folders.get(m.get("parentFolderId"), "")
        return RawDoc(self.source.name, mid, m.get("subject") or "(senza oggetto)",
                      f"{self.source.name}/{folder}".rstrip("/"), path, "eml", mtime, 0,
                      lambda mid=mid, path=path: self._fetch(mid, path), web_url=m.get("webLink"))

    def _fetch(self, mid, path):
        m = self._get(f"/me/messages/{urllib.parse.quote(mid, safe='')}",
                      {"$select": "subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments"},
                      {"Prefer": 'outlook.body-content-type="text"'})
        addr = lambda r: f"{(r.get('emailAddress') or {}).get('name', '')} <{(r.get('emailAddress') or {}).get('address', '')}>"
        msg = EmailMessage()
        msg["Subject"] = m.get("subject") or ""
        if m.get("from"):
            msg["From"] = addr(m["from"])
        msg["To"] = ", ".join(addr(r) for r in m.get("toRecipients", []))
        if m.get("ccRecipients"):
            msg["Cc"] = ", ".join(addr(r) for r in m["ccRecipients"])
        try:
            msg["Date"] = format_datetime(datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")))
        except Exception:
            pass
        msg.set_content((m.get("body") or {}).get("content", "") +
                        ("\n\n[Il messaggio ha allegati]" if m.get("hasAttachments") else ""))
        raw = msg.as_bytes()
        path.write_bytes(raw)
        return [(1, parse_eml_bytes(raw)[1])]
