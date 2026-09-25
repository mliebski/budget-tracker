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

## 1. Put the code on GitHub (one time, on your Windows PC)

The code is kept in a **private** GitHub repository, and the box downloads it from there. Only the program code goes to GitHub. Your financial data stays in the database on the box, and `.env` (passwords) and `backups/` are excluded by `.gitignore`.

1. On github.com, create a new repository named `budget-tracker`. Set it to **Private**, and don't add a README, .gitignore or license (the project already has them).
2. In PowerShell, in the `budget-tracker` folder (replace `YOUR-GITHUB-NAME`):
   ```powershell
   git remote add origin https://github.com/YOUR-GITHUB-NAME/budget-tracker.git
   git push -u origin main
   ```
   The first push opens a browser window to sign in to GitHub.

## 2. Set up the new box

Any small PC or Raspberry Pi 4/5 is enough. These steps assume **Ubuntu Server 24.04 LTS** (Debian 12 and Raspberry Pi OS work the same way).

1. **Install Ubuntu Server** and tick *Install OpenSSH server* during setup.
2. **Give the box a fixed address.** In your router's admin page, create a *DHCP reservation* (sometimes called “static lease”) for the box, e.g. `192.168.1.50`. Do **not** create any port forwarding for it; that keeps it off the internet.
3. **Give the box read-only access to the repository.** Log in to the box (`ssh youruser@192.168.1.50`) and create a key that is used only for this:
   ```bash
   sudo apt-get install -y git
   ssh-keygen -t ed25519 -f ~/.ssh/budget_deploy -N "" -C "budget box"
   printf 'Host github.com\n  IdentityFile ~/.ssh/budget_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
   cat ~/.ssh/budget_deploy.pub
   ```
   On GitHub, open the repository → **Settings → Deploy keys → Add deploy key**. Paste the line printed by `cat`, name it “budget box”, and leave *Allow write access* **unticked**. The box can then download the code but can't change it, and the key gives no access to your other repositories.
4. **Download the code:**
   ```bash
   git clone git@github.com:YOUR-GITHUB-NAME/budget-tracker.git
   cd budget-tracker
   ```
   Answer `yes` when asked to trust github.com's fingerprint.
5. **Install Docker** (one time):
   ```bash
   bash scripts/install-docker.sh
   ```
   Log out and back in afterwards, so your user can run `docker` without `sudo`.
6. **Start the app:**
   ```bash
   bash scripts/setup.sh
   ```
   This creates `.env` with random database passwords and your time zone, builds the app and starts three containers: the web app, MariaDB, and the backup job. It prints the address to open, e.g. `http://192.168.1.50:8000`.
7. **Optional but recommended: firewall.** Allow only your home network (adjust the subnet to match your router, often `192.168.0.0/24` or `192.168.1.0/24`):
   ```bash
   sudo ufw allow OpenSSH
   sudo ufw allow from 192.168.1.0/24 to any port 8000 proto tcp
   sudo ufw enable
   ```
   > Docker publishes ports through its own iptables rules, which can bypass ufw. The real protection is that your router does not forward any port to the box. To be extra strict, set `APP_PORT=192.168.1.50:8000` in `.env` so the app only listens on the LAN address.

The containers restart automatically after a reboot or power cut.

**Tip:** on your phone, open the address and use *Add to Home Screen*. The app then works like a phone app.

## 3. First steps in the app

1. **Categories**: a starter set is created on first start. Rename, hide or add categories to match your household.
2. **Recurring**: add your pay cheque(s), rent/mortgage, utilities, phone, subscriptions and insurance.
3. **Budgets**: enter monthly amounts per category and yearly amounts for once-a-year bills. The *Last year* and *Avg / month* columns help once you have some history. Next year, one click copies the budget forward.
4. **Assets**: add bank accounts, investments, vehicles and debts with their current values. Update them now and then, e.g. monthly from your statements.
5. **Planned**: add known big future expenses.
6. After that it's mostly **Quick add** on the Home page.

## 4. How the numbers are calculated

- **Budget year**: October–September. “FY 2025–26” runs from 1 Oct 2025 to 30 Sep 2026. Fiscal Q1 is Oct–Dec.
- **Month budget** = monthly amount + yearly amount ÷ 12 (if spread) or the full yearly amount in its due month. A main category's budget is the sum of its own line and its subcategories.
- **Forecast for a future month** = recurring items due that month + planned items dated that month + **typical spending**. Typical spending is the average per category over the last 6 full months (configurable in Settings), excluding recurring, planned and *one-off* entries. Tick *One-off* on unusual purchases so they don't inflate the forecast.
- **Current month projection** = actual so far + recurring items still due + open planned items (including overdue ones) + typical spending scaled to the days left.
- **Projected year** = actual for months already closed + forecast for the rest.
- **Projected bank & cash balance** starts from the latest values of your *Bank account* and *Cash* assets.

## 5. Backups and restore

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

## 6. Updating

Changes are made on your Windows PC, committed and pushed to GitHub:

```powershell
git add -A
git commit -m "Describe the change"
git push
```

Then on the box, in the `budget-tracker` folder:

```bash
bash scripts/update.sh
```

This takes a database backup, downloads the latest code (`git pull`) and rebuilds and restarts the app. Your data lives in the Docker volume `db_data`, and `.env` and `backups/` are never touched by git.

To go back to an earlier version if an update causes trouble:

```bash
git log --oneline                # find the version you want
git checkout <commit-id>
docker compose up -d --build
```

Run `git checkout main` and `bash scripts/update.sh` to return to the latest version.

## 7. Everyday commands

```bash
docker compose ps                 # status of the containers
docker compose logs -f app        # app log
docker compose logs backup        # backup log
docker compose restart app        # restart the web app
docker compose down               # stop everything (data is kept)
```

## 8. Local development (Windows/Mac/Linux)

The app also runs on SQLite, so no database server is needed for development:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt pytest httpx   # Linux/Mac: .venv/bin/pip
.venv/Scripts/python -m uvicorn app.main:app --reload         # http://127.0.0.1:8000, data in ./budget.db
.venv/Scripts/python -m pytest
```

## Ideas for the next release

### 1. Receipts and invoices: photo upload and OCR
- Attach photos or PDFs to any entry or planned item. Big purchases would carry their invoice, warranty card and serial number, e.g. appliances, electronics, the car or the roof.
- Read receipts automatically (OCR) to fill in the amount, date and store. You would only confirm the result.
- Add **warranty tracking**: a warranty end date on an item, a list of items still under warranty, and a reminder on the Home page before a warranty runs out.
- Search receipts by store, amount or text.
- *Implementation notes:* files would be stored on the box in a volume next to the database and included in the nightly backup. OCR would run locally, e.g. Tesseract or PaddleOCR in a separate container, so receipts never leave the house. Phones can take a photo directly from the upload field.

### 2. AI forecasting and recommendations
- Smarter forecasts than a plain 6-month average: they would account for seasonality (heating in winter, travel in summer), trends and irregular annual costs.
- Written recommendations, e.g. “Restaurants are 40% over budget for 3 months in a row”, “Your subscriptions went up $28/month since spring”, or “At this pace you'll be $1,200 short for the roof in May”.
- Detection of unusual entries, such as duplicates, typos (like $850 instead of $85.00) and one-off spikes.
- Suggested budgets for the next budget year, based on history and planned purchases.
- Questions in plain language: “How much did we spend on the kids last year?”
- *Implementation notes:* the statistical part (seasonality, anomalies) can run locally in Python. For written recommendations and questions, a **local model** (e.g. via Ollama on the box) keeps all data private. A cloud AI API would be more capable but would send financial data out of the house. That should be an explicit opt-in, sending only summarized numbers.

### 3. Overall wealth tracker with forecasts
- Project net worth years ahead, not just the next budget year.
- Expected growth rate per asset (investments, property) and depreciation (vehicles).
- Regular contributions (RRSP, TFSA, RESP, savings), linked to the monthly budget surplus.
- Loan and mortgage amortization: interest rate, payment and term give the payoff date and the interest paid, with a “what if I pay $200 more” comparison.
- Scenarios such as buying a car in 2027, changing jobs or retiring at 60, compared side by side.
- Goals like an emergency fund, down payment or retirement target, with progress and a projected date for reaching each.
- Canadian specifics such as TFSA/RRSP contribution room and RESP grants (CESG).

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
scripts/           Docker install, first-time setup, updates
tests/             pytest suite
```

Tables are created automatically on start. If a future change modifies existing tables, it should ship with a migration step.
