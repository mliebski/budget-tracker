# Code Review: budget-tracker

**Date:** 2026-10-26
**Scope:** all tracked source: `app/` (Python, templates, JS), `ocr/`, `backup/`, Docker files. Tests were read for context.
**Test status:** `pytest` → 59 passed.

Overall the codebase is in good shape. It is small, consistently styled, well commented (the comments explain *why*), and defensive in the places that matter most: upload validation, EXIF stripping, path traversal checks, escaping model output before rendering it as Markdown, and `</script>` escaping in chart JSON. Most findings below are edge cases and hardening, not structural problems.

Severity legend: **High** = data loss, security, or a likely crash. **Medium** = wrong results or a 500 on reachable input. **Low** = polish, efficiency, or cleanliness.

---

## 1. Bugs

### High

**✅ FIXED — 1.1 A failed backup is still saved as a good backup, and old backups keep getting deleted** — `backup/backup.sh:15-17, 40`
`run_backup` is called as `run_backup || echo ...`. Bash turns off `set -e` inside any function called from the left side of `||`, so if `mariadb-dump` fails (DB down, wrong password, disk full), the function keeps going:
- the truncated or empty `.part` file is `mv`'d into place as `budget-<ts>.sql.gz`,
- "backup written" is logged,
- the `find ... -delete` pruning still runs.

If dumps fail silently for longer than `BACKUP_KEEP_DAYS`, every good dump gets rotated out and only broken ones are left.
*Fix:* check each step explicitly, and prune only after a verified dump:
```bash
if ! mariadb-dump ... | gzip > "$tmp"; then rm -f "$tmp"; return 1; fi
gzip -t "$tmp" || { rm -f "$tmp"; return 1; }
mv "$tmp" "$file"
find ... -delete
```

**✅ FIXED — 1.2 Open redirect through `safe_next`** — `app/web.py:131-138`
`safe_next("/\\evil.com")` returns `/\evil.com` unchanged (verified). Browsers treat `/\` like `//`, so `?next=/\evil.com` (or the form field `next`) redirects to `evil.com`.
*Fix:* also reject backslashes, e.g. `if "\\" in url or ...: return default`.

### Medium

**✅ FIXED — 1.3 `parse_amount` crashes with a 500 on `NaN` / `Infinity`** — `app/web.py:160-169`
`Decimal("NaN")` and `Decimal("Infinity")` parse without error, then `quantize()` or the `< 0` comparison raises `InvalidOperation` *outside* the `try` block (verified). The result is an unhandled 500 instead of the friendly error. Every amount field is affected (entries, budgets, planned, assets, receipt search).
*Fix:* after summing, `if not total.is_finite(): raise UserError(...)`, and move `quantize` inside the `try`.

**✅ FIXED — 1.4 Amounts too large for the column cause a 500 on MariaDB** — `app/web.py:153`, `app/models.py:12`
`Money = Numeric(12, 2)` allows at most 9,999,999,999.99, but `parse_amount("99999999999999")` is accepted. In MariaDB strict mode the INSERT fails with a DB error (500). Exponent forms like `1e9` are also accepted.
*Fix:* add an upper bound in `parse_amount` (e.g. `abs(total) >= 10**10` → `UserError`).

**✅ FIXED — 1.5 Double-submitting the receipt confirm form creates duplicate entries** — `app/routes/receipts.py:116-133`
The GET confirm page redirects if the attachment is already filed, but the POST does not check. A double tap, or Back followed by re-submitting, creates a second `Transaction` and moves the attachment to it, leaving the first entry orphaned.
*Fix:* at the top of `confirm_save`: `if att.transaction_id: return redirect(f"/entries/{att.transaction_id}/edit", msg="This receipt is already filed.")`.

**✅ FIXED — 1.6 `save_budgets` crashes with a 500 on malformed form data** — `app/routes/budgets.py:48-68`
- `int(form["fy"])` and `int(k[2:])` raise `KeyError` / `ValueError`.
- `category_id` values are not checked against existing categories, so the FK error on MariaDB is a 500.
- `annual_month` is not range-checked (1–12). A value like `13` is saved, and that annual amount then never lands in any month.

*Fix:* validate `fy`, skip unknown category ids, and require `1 <= annual_month <= 12` or `None`.

**✅ FIXED — 1.7 Out-of-range `fy` query param causes a 500** — `app/routes/entries.py:37`, `app/routes/reports.py:59`, `app/routes/budgets.py:25`
`?fy=0` or `?fy=10000` makes `date(fy, ...)` raise `ValueError`. Clamp `fy` (e.g. 1900–2999) or catch the error and fall back to the current FY.

**✅ FIXED — 1.8 AI chat stream errors other than `AiError` break the stream** — `app/ai.py:95-119`, `app/routes/ai.py:149-169`
`_open` wraps connection errors, but errors raised *while iterating* the response (a socket timeout `OSError`, or `json.loads` hitting a partial line) are not converted to `AiError`. `events()` only catches `AiError`, so the stream ends without a `{"t":"error"}` or `{"t":"done"}` event, and the browser shows "No answer came back" or a JSON parse error.
*Fix:* wrap the loop in `chat_stream` with `except (OSError, ValueError) as e: raise AiError(...)`.

**✅ FIXED — 1.9 The AI snapshot hard-codes "October to September"** — `app/ai_data.py:94`
`FY_START_MONTH` is configurable (`app/fiscal.py:10`), but the prompt always tells the model the year runs October–September. Build the sentence from `fiscal.fy_months(fy)`.

**✅ FIXED — 1.10 One-off *income* is missing from the AI snapshot** — `app/ai_data.py:85-107`
One-off entries are taken out of the monthly tables, but the one-off list is filtered to `EXPENSE` only. One-off income (a bonus, an inheritance, a tax refund marked one-off) therefore appears nowhere in what the model sees. Either keep one-off income in the income table or add an "Unusual income" section.

### Low

**✅ FIXED — 1.11 `variable_average` counts the first, partial month as a full month** — `app/services.py:162-178`
If the first entry is dated 25 Oct, October counts as one of the `n` averaging months, which pulls the average (and so the forecast) down while history is short. Consider starting from the first *full* month: `months_between(add_months(month_start(first), 1), month)` when `first.day > 1`.

**✅ FIXED — 1.12 The `undo` query parameter goes into a form action unvalidated** — `app/templates/base.html:27-29`
`/?undo=../../categories/5` renders an "Undo" button that posts to `/entries/../../categories/5/delete`, which the browser normalizes to `/categories/5/delete`. It needs a user click, but it is easy to harden: render the button only when `undo` is all digits (`{% if undo and undo.isdigit() %}`).

**✅ FIXED — 1.13 A conflict in `materialize` drops all pending occurrences until tomorrow** — `app/recurring.py:92-98, 109-112`
On `IntegrityError` everything is rolled back and `_last_run` is still set to today. If the conflict was not the race it is meant to handle, the other due entries silently wait a day. Consider retrying once with fresh `existing` sets, or not setting `_last_run` after a rollback.

**✅ FIXED — 1.14 SQLite foreign keys are never enabled** — `app/db.py`
SQLite ignores `ondelete="CASCADE"/"SET NULL"` unless `PRAGMA foreign_keys=ON` is set on each connection. Production (MariaDB) honours these rules, but dev and the test suite don't, so tests can't catch FK-related regressions. One visible effect: `tag_delete` (`app/routes/categories.py:110-117`) clears `transaction_tags` by hand but not `recurring_tags`, which leaves orphan rows on SQLite.
*Fix:* add an `@event.listens_for(engine, "connect")` hook that runs `PRAGMA foreign_keys=ON` for SQLite.

**✅ FIXED — 1.15 Other small issues**
- `list_entries` search: `%` and `_` in `q` act as LIKE wildcards. Use `.contains(..., autoescape=True)` (`app/routes/entries.py:54`).
- `tag_rename` flashes "Tag renamed." even when the tag doesn't exist or the name is empty (`app/routes/categories.py:97-107`).
- `chat_stream` drops any text that comes *before* `<think>` in the same chunk (`split("<think>", 1)[1]`) (`app/ai.py:107-108`).
- `files.ocr_image` opens the image without a `with` block, so the file handle stays open until GC (`app/files.py:113`).
- `files.store` accepts any format Pillow can decode (GIF, TIFF, BMP, …) although the error message says JPEG, PNG and WebP only (`app/files.py:73-85`). This is harmless, since everything is re-encoded to JPEG, but the message is inaccurate.
- `settings_save` takes `int` form fields, so a non-numeric value returns FastAPI's raw 422 JSON instead of a flash message (`app/routes/settings.py:39`).
- Timestamps are mixed: some models use `server_default=func.now()` (UTC on SQLite, server TZ on MariaDB), others `default=dt.datetime.now` (app TZ). `Attachment.display_date` can therefore be off by a day in dev. Pick one approach.

---

## 2. Security / deployment hardening

The app is meant for a home network, so these are recommendations, not urgent fixes.

**2.1 No authentication and no CSRF protection.** Port 8000 is published on all interfaces (`docker-compose.yml:37-38`), and every state-changing route is a plain form POST. Any web page the user visits can submit a hidden cross-origin form to `http://<box-ip>:8000/entries/123/delete`, `/budgets/copy`, `/ai/settings` and so on. Browsers allow simple cross-origin form POSTs, and there is no cookie for SameSite to protect.
Cheap mitigations, in order of effort:
1. Reject POSTs whose `Origin` (or `Referer`) host doesn't match `Host`. This is about 10 lines of middleware, needs no template changes, and blocks drive-by CSRF.
2. Bind to a specific LAN interface, or put the app behind a reverse proxy with basic auth.

**2.2 Add `X-Content-Type-Options: nosniff`** to `/files/{aid}` responses (`app/routes/receipts.py:70-72`). Stored files are always JPEG or PDF, so this is defence in depth only.

**2.3 `ocr/server.py:109`**: a non-numeric `Content-Length` raises inside the handler. This only affects the internal service, but `int(... or 0)` should be wrapped.

---

## 3. Efficiency

None of these matter much at household data volumes, but they are cheap to fix and some affect how the app feels on a mini PC.

**✅ FIXED — 3.1 The Settings page makes up to three blocking network calls on every load** — `app/routes/settings.py:28-35`
`ocr.status()` (5 s timeout) plus `ai.status()` (two calls, 5 s each). If Ollama or the OCR container is down, the page can take about 15 s to render. Also, `/health` on the OCR side spawns `tesseract` twice per call.
*Options:* cache the status for 30–60 s, use shorter timeouts (1–2 s), or load the status asynchronously from the page with `fetch`.

**✅ FIXED — 3.2 Uploads run OCR synchronously, one file at a time** — `app/routes/receipts.py:42-53, 165-166`
`add_attachment` calls `run_ocr` inline with a 180 s timeout. A multi-file upload to `/attachments` waits for OCR of *every* file before redirecting, and each request holds a threadpool worker. Also, files are written to disk *before* the DB commit, so if file 3 of 5 fails validation, files 1–2 stay on disk with no DB row (orphans).
*Options:* only run OCR on the scan flow (where the result is needed right away) and queue it for bulk uploads (a background thread, like AI reports); validate every file before writing any of them, or delete written files on failure.

**✅ FIXED — 3.3 `/reports` computes the forecast twice** — `app/routes/reports.py:76 and 110`
`_projection()` and the trend chart both call `forecast(db, today, <same open months>)`. Compute it once and pass it to both.

**✅ FIXED — 3.4 `forecast()` does work before its early return** — `app/services.py:213-216`
`variable_average()` (two queries) runs even when `months` is empty, for example on every past-FY report. Move `if not months: return ...` to the top.

**✅ FIXED — 3.5 Lazy-load N+1 queries in loops**
- `forecast()`, `upcoming()`, `ai_data.snapshot()`: `r.skips` and `r.category` per recurring item → add `.options(selectinload(Recurring.skips), selectinload(Recurring.category))`.
- `planned_list`: `p.category.kind` per item (`app/routes/planned.py:30`).
- `recurring_list`: `r.category` and `r.skips` per row (`app/routes/recurring.py:44-46`).
- `assets_page`, `liquid_balance`, `app_asset_projection`: `a.values` per asset → `selectinload(Asset.values)`.
- `materialize()`: two queries per active recurring item. One grouped query for all `(recurring_id, occurrence_date)` pairs plus one for all skips would replace them.

**✅ FIXED — 3.6 Categories are loaded several times per request**
The dashboard calls `category_map`, `build_rows` → `category_tree`, `category_choices` → `category_tree` ×2, and `recent_categories` → `category_map`, which comes to five `SELECT * FROM categories` per page. Load once per request (e.g. a small cache on the session's `info` dict, or pass the list through).

**✅ FIXED — 3.7 `net_worth_series` is O(months × assets × values)** — `app/services.py:314-345`
`asset_values_at` scans every value of every asset for each month. Since `values` is sorted, a single forward pass (or `bisect`) per asset is linear. It's fine today and would only matter after years of monthly values.

**✅ FIXED — 3.8 Chat rendering re-parses all the Markdown on every token** — `app/static/app.js:264-266`
`answer.innerHTML = markdown(text)` runs once per streamed chunk, which is O(n²) for long answers and noticeable on phones. Throttle it with `requestAnimationFrame`, so it renders at most once per frame.

**✅ FIXED — 3.9 `save_budgets` is `async def` but uses the sync SQLAlchemy session** — `app/routes/budgets.py:48`
DB calls inside an `async def` handler block the event loop. Make it a plain `def` and read the form via `Form` parameters, or parse `request.form()` in a small async dependency and keep the handler sync.

---

## 4. Cleanliness / maintainability

**✅ FIXED — 4.1 Duplicated helpers.** `_category()` exists in `routes/entries.py:18`, `routes/planned.py:14` and inline in `routes/recurring.py:18-20`. The "next non-skipped occurrence" logic is duplicated in `recurring.next_occurrence` and `ai_data.snapshot:114-116`. Move each into one shared place (`services.py` / `recurring.py`).

**✅ FIXED — 4.2 Repeated "all tags" query.** `list(db.scalars(select(Tag).order_by(Tag.name)))` appears in 7 routes. Add a `services.all_tags(db)` helper.

**✅ FIXED — 4.3 Magic status strings.** `"planned" | "done" | "cancelled"`, `"running" | "done" | "failed"` and `"none" | "done" | "failed"` are spread across routes and templates. Module-level constants (like `EXPENSE`/`INCOME`) or `StrEnum`s would prevent typos.

**◐ PARTLY FIXED — 4.4 GET handlers that write.** `report_page` (`app/routes/ai.py:221-224`) marks stale reports as failed and commits during a GET, and `get_db` materializes recurring entries on any request, including `/files/{id}`. This is pragmatic, but worth knowing about. A periodic background tick for both would keep GETs read-only.
*Done:* the AI report page no longer writes; interrupted runs are marked failed at startup instead. *Kept on purpose:* `get_db` still creates due recurring entries (at most once a day) on the first request. It needs no scheduler and is safe against races, so a background tick isn't worth it yet.

**✅ FIXED — 4.5 `asset_value` uses `next == "detail"` as a flag** (`app/routes/assets.py:100`), while other routes use `next` as a URL. For consistency, pass a real path and use `safe_next`.

**✅ FIXED — 4.6 Long lines.** Several lines pass 120 characters (`web.py:109`, `receipts.py:35`, `routes/receipts.py:227-230`, `routes/ai.py:207`). A formatter or linter (`ruff format` + `ruff check`) would keep this consistent and catch unused imports as the code changes.
*Done:* the long lines in `app/` are wrapped and unused imports are removed. ruff itself is not set up.

**⏸ NOT DONE (plan for later) — 4.7 Schema migrations.** `migrate.py` handles added columns only. That's fine for now, but the first index, type change or rename will need Alembic or a numbered-steps table. Plan for that before the schema changes again.
*Status:* no code change needed until the next schema change.

**✅ FIXED — 4.8 Pinned dependencies are good. Add a lock or update routine.** `requirements.txt` is fully pinned. Consider Dependabot or Renovate, or a periodic `pip list --outdated` step in `scripts/update.sh`, so FastAPI and Starlette security fixes aren't missed. The test run shows a Starlette/AnyIO deprecation warning, which is a sign it's time for a bump.
*Done:* `.github/dependabot.yml` (pip, Dockerfiles, compose; monthly). The versions themselves were not bumped.

---

## 5. Test coverage suggestions

The 59 tests cover the core flows well. Additions worth considering, mostly as regression tests for the bugs above:
- `parse_amount` with `NaN`, `Infinity`, `1e3` and over-limit values (1.3, 1.4).
- `safe_next` with `/\host`, `\\host` and `//host` (1.2).
- Double POST to `/receipts/{id}/confirm` (1.5).
- `/budgets` POST with bad `fy` / `annual_month` (1.6).
- Running the suite with SQLite foreign keys on (1.14).
- A shell test for `backup.sh` with a failing `mariadb-dump` stub (1.1).

---

## Suggested order of work

1. **1.1** backup script (protects your data).
2. **1.2**, **1.3**, **1.4**, **1.5** (small, contained fixes for crashes, a duplicate-entry bug and an open redirect).
3. **2.1** Origin-check middleware (about 10 lines, closes CSRF).
4. **3.1**, **3.2** (most noticeable speed-ups).
5. Remaining bugs, then cleanliness items as you touch those files.
