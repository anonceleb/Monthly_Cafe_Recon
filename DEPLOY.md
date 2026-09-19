# Deploy on Render

Result: `https://kredo-recon.onrender.com` (or your own domain), behind a shared password, backed by managed Postgres.
Time: about 20 minutes. Cost: roughly **$7/month web (Starter) + $6/month Postgres (Basic-256MB)** — check Render's pricing page, plans change.

## 1. Deploy

1. Push this repo to GitHub (done: `anonceleb/Monthly_Cafe_Recon`, private).
2. Render dashboard → **New → Blueprint** → connect GitHub, choose the repo. Render reads `render.yaml` and proposes a web service and a database.
3. When asked for **RECON_PASSWORD**, choose a passphrase of 10+ characters (this is what the founder types). Apply.
4. First build takes a few minutes. When the service shows **Live**, open its URL. You should land on the sign-in page.

The database schema is created automatically the first time the app runs; there is nothing to migrate by hand.

## 2. Load April

Sign in → **Upload** → drag in the seven April files (ICICI statement, Petpooja order summary and online sales, Zomato delivery and dining, Swiggy orders and adjustments). The findings appear when it finishes.
Your source spreadsheets stay on your laptop; only the parsed rows live in the cloud database.

## 3. Show the founder

- Send them the URL and the password (through a different channel from each other, e.g. link by email, password by phone).
- They only need a browser. Nothing to install.
- **Export Excel** on any page gives them the findings to keep.

## Settings (all environment variables; change under Service → Environment)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | Set by the Blueprint from the database. |
| `RECON_PASSWORD` | Shared password. The app **refuses to start** in the cloud if this is missing or under 10 characters. |
| `RECON_SECRET` | Signs login cookies. Generated for you. Changing it signs everyone out. |
| `PORT` | Injected by Render. |

To change the password: edit `RECON_PASSWORD`, save (Render restarts the service). Existing sessions stay valid for up to 12 hours; change `RECON_SECRET` too to sign everyone out immediately.

## Changing rates or rules

`config/rates.toml` and `recon/rules/` are part of the image. Edit, commit, push to `main`; Render redeploys (auto-deploy is on). Rates in the repo were read off the April reports, so confirm them against your agreements before the demo.

## Operations

- **Backups:** paid Render Postgres plans include point-in-time recovery for a limited window (check the plan). For a permanent copy, use Render's **Database → Backups** export or take a `pg_dump` from a machine you have allowed in the database's access list.
- **Reading the database yourself:** the database is closed to the internet on purpose (`ipAllowList: []`). To connect from your laptop, temporarily add your IP under Database → Access Control, use the *External* connection string, then remove it.
- **Free tier:** don't use it for this. Free Postgres expires after 30 days and free web services sleep, so the demo would open slowly or lose data.
- **Logs:** Service → Logs. Failed sign-ins and parse errors show there.
- **Cold starts:** Starter is always on. If you drop to a free web plan, the first request after idle takes about 30 seconds.

## Security notes

- Single shared password, no per-user accounts or audit trail. Fine for a founder demo; move to per-user sign-in (for example Google login) before more people use it.
- Five wrong passwords from one address lock sign-in for five minutes.
- HTTPS is provided by Render; the session cookie is marked Secure and HTTP-only.
- The data is trading and bank information. Keep the GitHub repo private, and only invite people who should see the rates in `config/`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Deploy fails with `RECON_PASSWORD must be set` | The password variable is empty. Set it (10+ characters). |
| `could not connect to server` in logs | Web service and database are in different regions, or `DATABASE_URL` was overwritten. Use the Blueprint's value; both are in `singapore`. |
| Upload works but page shows old numbers | Uploads re-run the rules automatically; press **Re-run rules** on Overview if you edited config. |
| Sign-in says too many attempts | Wait five minutes, or restart the service to clear the counter. |

## Same image elsewhere

```bash
docker build -t kredo-recon .
docker run -p 8790:8790 -e DATABASE_URL=postgresql://... -e RECON_PASSWORD=... -e RECON_SECRET=$(openssl rand -hex 24) kredo-recon
```
(Behind plain http on localhost the Secure cookie will not stick; add `-e RECON_COOKIE_SECURE=0` for a local test.)
