# Budget

A private budgeting and forecasting tool for your home network. It runs on a small always-on Linux box and you use it from any browser on your network (phone, tablet, laptop). The database stays on your box; the only internet access the box needs is for installing and updating the software.

- **Quick entry**: add an expense or income in a few taps from the home screen. Typing `12.50 + 3.99` adds the two amounts.
- **Categories**: two levels (e.g. *Food › Groceries*), plus free **tags** such as “Vacation 2026” that work across categories.
- **Recurring items**: rent, pay cheques, subscriptions. They are added automatically on their due date. You can pause them, and you can edit or delete a single occurrence.
- **Budgets per budget year (Oct–Sep)**: a monthly amount per category, plus yearly amounts that are either spread over the year or due in a set month.
- **Planned purchases**: a new car, a roof repair and so on. They appear in the forecast for their month; once paid, you record the real amount.
- **Reports**: month, quarter and year, showing budget vs actual vs projected, a breakdown by category, a monthly trend, and a filter by tag.
- **Forecast**: the rest of this budget year plus the next one, broken down by month and by category, with a projected bank balance.
- **Assets & net worth**: bank accounts, investments (RRSP/TFSA), property, vehicles, mortgage, loans and credit cards, with a net-worth history.
- **Nightly database backups**, kept for 30 days by default.

---

## 1. Set up the new box

Any small PC or Raspberry Pi 4/5 is enough. These steps assume **Ubuntu Server 24.04 LTS** (Debian 12 and Raspberry Pi OS work the same way).

1. **Install Ubuntu Server** and tick *Install OpenSSH server* during setup.
2. **Give the box a fixed address.** In your router's admin page, create a *DHCP reservation* (sometimes called “static lease”) for the box, e.g. `192.168.1.50`. Do **not** create any port forwarding for it; that keeps it off the internet.
3. **Copy the project to the box.** From Windows (PowerShell, in the folder that contains `budget-tracker`):
   ```powershell
   tar --exclude=.venv --exclude=__pycache__ -czf budget-tracker.tgz budget-tracker
   scp budget-tracker.tgz youruser@192.168.1.50:~
   ```
   Then on the box (`ssh youruser@192.168.1.50`):
   ```bash
   tar -xzf budget-tracker.tgz && cd budget-tracker
   ```
4. **Install Docker** (one time):
   ```bash
   bash scripts/install-docker.sh
   ```
   Log out and back in afterwards, so your user can run `docker` without `sudo`.
5. **Start the app:**
   ```bash
   bash scripts/setup.sh
   ```
   This creates `.env` with random database passwords and your time zone, builds the app and starts three containers: the web app, MariaDB, and the backup job. It prints the address to open, e.g. `http://192.168.1.50:8000`.
6. **Optional but recommended: firewall.** Allow only your home network (adjust the subnet to match your router, often `192.168.0.0/24` or `192.168.1.0/24`):
   ```bash
   sudo ufw allow OpenSSH
   sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp
   sudo ufw enable
   ```
   > Docker publishes ports through its own iptables rules, which can bypass ufw. The real protection is that your router does not forward any port to the box. To be extra strict, set `APP_PORT=192.168.1.50:8000` in `.env` so the app only listens on the LAN address.

The containers restart automatically after a reboot or power cut.

**Tip:** on your phone, open the address and use *Add to Home Screen*. The app then works like a phone app.

## 2. First steps in the app

1. **Categories**: a starter set is created on first start. Rename, hide or add categories to match your household.
2. **Recurring**: add your pay cheque(s), rent/mortgage, utilities, phone, subscriptions and insurance.
3. **Budgets**: enter monthly amounts per category and yearly amounts for once-a-year bills. The *Last year* and *Avg / month* columns help once you have some history. Next year, one click copies the budget forward.
4. **Assets**: add bank accounts, investments, vehicles and debts with their current values. Update them now and then, e.g. monthly from your statements.
5. **Planned**: add known big future expenses.
6. After that it's mostly **Quick add** on the Home page.

## 3. How the numbers are calculated

- **Budget year**: October–September. “FY 2025–26” runs from 1 Oct 2025 to 30 Sep 2026. Fiscal Q1 is Oct–Dec.
- **Month budget** = monthly amount + yearly amount ÷ 12 (if spread) or the full yearly amount in its due month. A main category's budget is the sum of its own line and its subcategories.
- **Forecast for a future month** = recurring items due that month + planned items dated that month + **typical spending**. Typical spending is the average per category over the last 6 full months (configurable in Settings), excluding recurring, planned and *one-off* entries. Tick *One-off* on unusual purchases so they don't inflate the forecast.
- **Current month projection** = actual so far + recurring items still due + open planned items (including overdue ones) + typical spending scaled to the days left.
- **Projected year** = actual for months already closed + forecast for the rest.
- **Projected bank & cash balance** starts from the latest values of your *Bank account* and *Cash* assets.

## 4. Backups and restore

- A dump of the database is written every night at `BACKUP_TIME` (default 02:30) to `BACKUP_PATH` (default `./backups` in the project folder) and kept for `BACKUP_KEEP_DAYS` days. The Settings page lists the latest files.
- **Point `BACKUP_PATH` at a different disk** (USB drive or NAS mount) in `.env`, then run `docker compose up -d`. A backup on the same disk won't survive a disk failure.
- Take a backup right now:
  ```bash
  docker compose exec backup bash /backup.sh now
  ```
- Restore a backup. This **replaces** the current data:
  ```bash
  set -a; . ./.env; set +a
  gunzip -c backups/budget-2026-09-24_0230.sql.gz | docker compose exec -T db mariadb -u budget -p"$DB_PASSWORD" budget
  ```

## 5. Updating

Copy the new version over the old folder, keeping your `.env` and `backups`, then run:

```bash
docker compose up -d --build
```

Your data lives in the Docker volume `db_data` and is not affected. Take a backup first to be safe.

## 6. Everyday commands

```bash
docker compose ps                 # status of the containers
docker compose logs -f app        # app log
docker compose logs backup        # backup log
docker compose restart app        # restart the web app
docker compose down               # stop everything (data is kept)
```

## 7. Local development (Windows/Mac/Linux)

The app also runs on SQLite, so no database server is needed for development:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt pytest httpx   # Linux/Mac: .venv/bin/pip
.venv/Scripts/python -m uvicorn app.main:app --reload         # http://127.0.0.1:8000, data in ./budget.db
.venv/Scripts/python -m pytest
```

## Project layout

```
app/
  main.py          FastAPI app, startup (creates tables, seeds categories)
  models.py        database tables
  fiscal.py        month / budget-year date helpers
  recurring.py     schedules and auto-creation of recurring entries
  services.py      budget, actuals, forecast, net-worth calculations
  web.py           templates, form parsing, shared helpers
  routes/          one module per page
  templates/       HTML (Jinja2)
  static/          CSS, JS, Chart.js (bundled; no internet needed)
backup/backup.sh   nightly dump job (runs in the backup container)
scripts/           Docker install and first-time setup
tests/             pytest suite
```

Tables are created automatically on start. If a future change modifies existing tables, it should ship with a migration step.
