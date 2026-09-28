"""HTTP client for the app's own endpoints -- the same ones the front-end JS
calls (datasets.js / quick-access.js / downloads.js)."""
import threading

import requests


class Api:
    def __init__(self, server):
        self.server = server
        self._local = threading.local()
        self.errors = []            # transport-level failures and 5xx responses seen by this client
        self.calls = 0

    @property
    def base(self) -> str:
        return self.server.base_url

    def _session(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.trust_env = False     # never pick up proxies from the environment
            self._local.s = s
        return s

    def request(self, method, path, **kw):
        kw.setdefault("timeout", 30)
        self.calls += 1
        try:
            r = self._session().request(method, self.base + path, **kw)
        except requests.RequestException as e:
            self.errors.append((method, path, type(e).__name__ + ": " + str(e)[:200]))
            raise
        if r.status_code >= 500:
            self.errors.append((method, path, f"HTTP {r.status_code}: {r.text[:200]}"))
        return r

    # --- downloads (routes in api/routes/downloads.py) -----------------------
    def start_download(self, name, destination, paths, sizes=None):
        body = {"name": name, "destination": str(destination), "paths": list(paths)}
        if sizes is not None:
            body["sizes"] = sizes
        return self.request("POST", "/datasets/download/start", json=body)

    def status(self, job_id):
        return self.request("GET", f"/datasets/download/status/{job_id}", headers={"Cache-Control": "no-store"})

    def history(self):
        r = self.request("GET", "/downloads/history")
        r.raise_for_status()
        return r.json()

    def delete_record(self, history_id):
        return self.request("DELETE", f"/downloads/history/{history_id}")

    def restart_record(self, history_id):
        return self.request("POST", f"/downloads/history/{history_id}/restart")

    def restart_file(self, history_id, path):
        return self.request("POST", f"/downloads/history/{history_id}/restart-file", json={"path": path})

    # --- browsing / misc -----------------------------------------------------
    def list_path(self, path):
        return self.request("GET", "/datasets/category/list-path", params={"path": path}, timeout=60)

    def token_status(self, namespace):
        return self.request("GET", "/auth/token/status", params={"namespace": namespace})

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def history_row(self, history_id):
        for row in self.history():
            if row["id"] == history_id:
                return row
        return None
