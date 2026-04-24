# Adding a new app to Hack2skill Central Data Intelligence

**Already have a Flask app?** Use the repo root guide **[MODULE_INTEGRATION.md](../MODULE_INTEGRATION.md)** and run `python scripts/copy_cdi_auth_into_module.py <your-app-dir>` from the CDI repo to drop in `h2s_cdi_auth.py` + `jarvis_auth.py`.

---

## 5-step checklist (greenfield from this template)

### Step 1 — Copy this folder
```
cp -r module_template  my_new_app
cd my_new_app
```

---

### Step 2 — Configure `.env`
```
cp .env.example .env
```
Open `.env` and fill in **5 values** (the rest are already set):

| Variable | What to put |
|---|---|
| `H2S_CDI_MODULE_ID` | A short slug, e.g. `hr-portal` (lowercase, no spaces) |
| `MODULE_NAME` | Display name, e.g. `HR Portal` |
| `PORT` | A free port, e.g. `5100` |
| `H2S_CDI_JWT_SECRET` | Copy from the portal `.env` (same as `JARVIS_JWT_SECRET` if you still use that name) |
| `H2S_CDI_REGISTRATION_SECRET` | Copy `MODULE_REGISTRATION_SECRET` from the portal `.env` |

Also update `APPLICATION_ROOT` and `BASE_URL` to match your slug and port:
```
APPLICATION_ROOT=/hr-portal
BASE_URL=http://localhost:5100
```

---

### Step 3 — Define your pages in `app.py`
Open `app.py` and find **CUSTOMIZE #1** and **CUSTOMIZE #2**.

**CUSTOMIZE #1** — edit `MODULE_PAGES`:
```python
MODULE_PAGES = [
    {"pageId": "home",    "label": "Home",    "path": "/home"},
    {"pageId": "reports", "label": "Reports", "path": "/reports"},
]
```

**CUSTOMIZE #2** — add a route per page:
```python
@app.route("/reports")
@h2s_cdi_auth_required(page="reports")
def reports():
    return render_template("reports.html")
```

Add a matching `templates/reports.html` (copy `templates/home.html` as a starting point).

---

### Step 4 — Register in the portal UI
In the portal → **Nginx Config**:
- Add service: Name = `HR Portal`, Slug = `hr-portal`, Port = `5100`, Host = your app host
- Click **Reload Nginx**

Also add `MODULE_URL_HR-PORTAL=http://h2s.tech/hr-portal/` to the portal `.env`
and restart the portal (so the dashboard "Open" button links correctly).

---

### Step 5 — Install & run
```bash
pip install -r requirements.txt
python app.py
```

On startup the app registers itself with the portal automatically.
Open it via **Dashboard → Open HR Portal** (not directly via URL).

---

## Granting user access
1. Portal → **Users** → grant user access to the service.
2. Portal → **Modules** → configure which pages each group/user can see.

---

## File structure
```
my_new_app/
├── app.py            ← your routes (edit CUSTOMIZE #1 and #2)
├── h2s_cdi_auth.py   ← portal auth middleware (don't edit)
├── requirements.txt
├── .env.example      ← copy to .env and fill in
├── .env              ← never commit this
└── templates/
    ├── base.html     ← nav + layout (customise styling here)
    └── home.html     ← one file per page in MODULE_PAGES
```

## Troubleshooting
| Symptom | Fix |
|---|---|
| 502 Bad Gateway | App not running, or wrong host/port in the portal service config |
| 404 | Service not enabled or Nginx not reloaded |
| Redirect loop | Always open via **Dashboard** button, not directly by URL |
| `[h2s_cdi_auth] Registration failed` | Check `H2S_CDI_REGISTRATION_SECRET` matches portal `.env` |
| Pages not showing in Modules | Restart the app — pages register on startup |
