# Deployment

Repository: https://github.com/GurKalra/recon
Live demo: https://recon-if0j.onrender.com

The live demo runs on Render's free plan. The service sleeps after 15 minutes without traffic, so
the first request can take 30 to 50 seconds to wake it. It has 512 MB of memory, so the live site
uses the demo dataset (3,000 orders). The 1-lakh-row timings in the design document were measured
on a laptop and in a sandbox, not on this server.

Everything below uses names only. No secret is stored in this repository; real values live in
`.env` (local) or in the host's environment settings, never in git.

## 1. Run it on your own machine (development)

You need Docker, Node 20+, and (for the tests) Python 3.12.

```bash
cp .env.example .env                    # local defaults; safe to leave as they are
docker compose up -d                    # PostgreSQL and the API (auto-reload), port 8000
docker compose exec api python -m app.users demo     # create the two demo logins
cd frontend && npm install && npm run dev            # the website, http://localhost:5173
```

Set `DEMO_EXEC_PASSWORD` and `DEMO_COFOUNDER_PASSWORD` in `.env` before creating the demo
logins to choose their passwords. If they are not set, random ones are printed once.

Sign in, then press **Load demo data** on the Overview. It loads the sample partner files in
`sample_data/small` (3,000 orders) and runs the reconciliation.

Tests:

```bash
cd backend && python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
pytest                                  # needs PostgreSQL on localhost (docker compose up -d db)
RUN_SCALE=1 pytest tests/test_scale.py -s    # the 1-lakh-row speed test (about 30 seconds)
cd ../frontend && npm test              # then: npm run typecheck
```

## 2. The live deployment (Render, free plan)

One web service and one PostgreSQL database. The API image also contains the built website
(`deploy/render.Dockerfile`), so the browser talks to a single address and there is no CORS.

1. **Database.** Create a Render PostgreSQL instance (free plan). Note its internal and external
   connection addresses.
2. **Limited database role.** Connect with the external address and create the role the API runs
   as. Choose a long random password for it.

   ```sql
   CREATE ROLE recon_app LOGIN PASSWORD '<random>' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
   GRANT CONNECT ON DATABASE <database name> TO recon_app;
   GRANT USAGE ON SCHEMA public TO recon_app;
   ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
       GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO recon_app;
   ALTER DEFAULT PRIVILEGES FOR ROLE <owner> IN SCHEMA public
       GRANT USAGE, SELECT ON SEQUENCES TO recon_app;
   ```

3. **Web service.** New Web Service from this repository: language Docker, Dockerfile path
   `deploy/render.Dockerfile`, build context `.`, instance type Free, health check path
   `/api/health`. Environment variables (names only):

   | Name                                            | Value                                             |
   | ----------------------------------------------- | ------------------------------------------------- |
   | `APP_ENV`                                       | `production`                                      |
   | `TRUSTED_PROXIES`                               | `*` (Render's proxy is the only way in)           |
   | `DATABASE_URL`                                  | internal address, signed in as `recon_app`        |
   | `MIGRATE_DATABASE_URL`                          | internal address, signed in as the database owner |
   | `GROQ_API_KEY`                                  | the AI key (optional)                             |
   | `DEMO_EXEC_PASSWORD`, `DEMO_COFOUNDER_PASSWORD` | passwords for the two demo logins                 |

   Migrations run on every start as the owner; the running API uses `recon_app`.

4. **Demo logins.** The free plan has no shell, so create them from a machine that can reach the
   database (use the external address and the owner login, with the two passwords set):

   ```bash
   cd backend
   DATABASE_URL=<external address> DEMO_EXEC_PASSWORD=... DEMO_COFOUNDER_PASSWORD=... \
       python -m app.users demo
   ```

5. **Close the database.** In the database's networking settings remove the "everywhere"
   (`0.0.0.0/0`) inbound rule so only Render services can connect.
6. **Check it.**

   ```bash
   cd backend
   BASE_URL=https://recon-if0j.onrender.com DEMO_EXEC_PASSWORD=... DEMO_COFOUNDER_PASSWORD=... \
       python scripts/smoke.py
   ```

   Two checks need data, so sign in and press **Load demo data** first.

Update: push to the repository; Render rebuilds and migrations run by themselves on start.

## 3. Deploy on any server that runs Docker

`docker-compose.prod.yml` runs three containers: the database, the API, and the website (nginx,
which also forwards `/api` to the API). Only the website is reachable from outside; the database
and API are not published.

```bash
cp .env.example .env
# edit .env and set (letters and digits only):
#   POSTGRES_PASSWORD, APP_DB_PASSWORD, DEMO_EXEC_PASSWORD, DEMO_COFOUNDER_PASSWORD
#   optional: GROQ_API_KEY (AI features), WEB_BIND, WEB_PORT, DOMAIN
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec api python -m app.users demo
```

Open `http://<server>:8080` (set `WEB_BIND=0.0.0.0` to reach it from other machines).

**HTTPS is required for a public site** (the session cookie is sent over HTTPS only outside
development). Either put your host's HTTPS in front of port 8080, or let the bundled Caddy do it:
set `DOMAIN` in `.env` to a name that points at the server and add `--profile tls` to the
`up` command. Caddy obtains and renews the certificate.

Update: `git pull && docker compose -f docker-compose.prod.yml up -d --build` (migrations run
by themselves on start). Back up the database with
`docker compose -f docker-compose.prod.yml exec db pg_dump -U recon_owner recon > backup.sql`.

## 4. Design points that matter when deploying

- **Two database users.** Migrations run as the owner. The API runs as `recon_app`, which can
  read and write rows but cannot create or drop tables, cannot `TRUNCATE`, and so cannot erase the
  audit trail. (The audit table also rejects edits with a trigger.)
- **One API worker, on purpose.** Imports and reconciliation runs hold an in-process lock so they
  never overlap. Do not raise the worker count without moving that lock into the database.
- **Uploads** wait on the server's disk until imported or discarded; stale ones are swept. On
  Render the disk is temporary, so an upload that has not been imported is lost on a restart.
- **Personal data is never stored.** Columns that look personal are refused at import, and the AI
  only ever sees masked shapes.

## 5. Environment variables (names only)

| Name                                                                                                                                                      | Used by      | Meaning                                                                                           |
| --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------- |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`                                                                                                       | dev compose  | Local database login                                                                              |
| `POSTGRES_PASSWORD`                                                                                                                                       | prod compose | Database owner (migrations)                                                                       |
| `APP_DB_PASSWORD`                                                                                                                                         | prod compose | The limited role the API uses                                                                     |
| `DATABASE_URL`                                                                                                                                            | API          | Connection string the API uses                                                                    |
| `MIGRATE_DATABASE_URL`                                                                                                                                    | migrations   | Optional. A more privileged address used only to migrate                                          |
| `APP_ENV`                                                                                                                                                 | API          | `development` or `production` (production: secure cookies, no API explorer)                       |
| `WEB_DIST`                                                                                                                                                | API          | Folder of the built website. Set in `deploy/render.Dockerfile`; when set, the API serves the site |
| `CORS_ORIGINS`                                                                                                                                            | API          | Allowed browser origins (empty when the site and API share an address)                            |
| `TRUSTED_PROXIES`                                                                                                                                         | API          | Proxy addresses whose `X-Forwarded-For` can be believed                                           |
| `UPLOAD_DIR`, `MAX_UPLOAD_MB`, `MAX_UNZIPPED_MB`                                                                                                          | API          | Where uploads wait and the limits on them                                                         |
| `SAMPLE_DATA_DIR`                                                                                                                                         | API          | Folder of sample partner files for the demo data                                                  |
| `DEMO_EXEC_PASSWORD`, `DEMO_COFOUNDER_PASSWORD`                                                                                                           | user script  | Passwords for the two demo logins                                                                 |
| `GROQ_API_KEY`                                                                                                                                            | API          | Turns on the AI features. Empty means rules only; everything still works                          |
| `GROQ_MODEL`, `GROQ_BASE_URL`, `GROQ_REASONING_FORMAT`, `GROQ_REASONING_EFFORT`, `GROQ_TEMPERATURE`, `GROQ_MAX_TOKENS`, `GROQ_TPM_LIMIT`, `LLM_TIMEOUT_S` | API          | AI model settings and pacing                                                                      |
| `AI_SUGGESTIONS_PER_10MIN`, `AI_RERANKS_PER_10MIN`                                                                                                        | API          | Per-person limits on AI requests                                                                  |
| `DB_HOST_PORT`, `API_HOST_PORT`                                                                                                                           | dev compose  | Host ports, if 5432 or 8000 are taken                                                             |
| `WEB_BIND`, `WEB_PORT`, `DOMAIN`                                                                                                                          | prod compose | Where the website listens, and the name for HTTPS                                                 |

## 6. Demo logins

| Role              | Email                    | What they can do                                                                |
| ----------------- | ------------------------ | ------------------------------------------------------------------------------- |
| Finance executive | `exec@isolabs.demo`      | Import, review, create and track claims, add rate cards, run the reconciliation |
| Co-founder        | `cofounder@isolabs.demo` | Read everything, write off findings, open the audit trail                       |

Passwords are whatever `DEMO_EXEC_PASSWORD` and `DEMO_COFOUNDER_PASSWORD` were set to. They are
given to reviewers separately and are never committed. Rotate or remove these accounts after the
review.
