# META_IMPORT_STATUS -- FB/IG Messaging Data (Pass MM)

**Date:** 2026-04-16
**Status:** STALE -- no new FB/IG rows since 2026-01-28 (78 days)

---

## 1. Finding: What the Pipeline IS

FB and Instagram data in `messaging_messages` was loaded by a **one-shot manual migration**, not a scheduled importer.

| Item | Detail |
|------|--------|
| Project | `~/Projects/messaging-migration/` |
| FB script | `scripts/03_migrate_facebook.py` |
| IG script | `scripts/04_migrate_instagram.py` |
| Source for FB | `~/Downloads/extracted_social_data/your_facebook_activity/messages/` (directory export) |
| Source for IG | `~/Downloads/instagram-youngmoneyinvestments-2026-01-28-3IlR0and.zip` (zip export) |
| Config | `config/migration_config.yaml` (hardcodes the above paths) |
| Schedule | None -- these scripts were run once in early February 2026 after a Meta data export on 2026-01-28 |

No launchd job, cron, or daemon loads new FB/IG messages continuously. The 6h cron (`cron_social_embed.sh` + `social_embed_pipeline.py`) only reads from PostgreSQL and embeds new rows -- it does not pull from Meta. It correctly reports "0 new messages" every run because PostgreSQL itself has not been updated.

**Current data state (queried 2026-04-16):**

| Platform | Last sent_at | Row count | Age |
|----------|-------------|-----------|-----|
| facebook | 2026-01-28 | 33,251 | 78 days |
| instagram | 2026-01-28 | 95,767 | 78 days |
| imessage | 2026-02-05 | 601,981 | live (chat.db) |

---

## 2. Root Cause: Why It Stopped

**This is a manual data-export pipeline. There is no automation to continue.**

Meta does not expose a real-time API for personal message history. The only way to get new messages into PostgreSQL is:

1. Cameron logs into Meta Accounts Center and requests a new data export (takes hours to days).
2. The resulting zip/directory is downloaded to Mac.
3. The migration scripts are re-run manually.

The last export was requested 2026-01-28. No subsequent export has been downloaded. The IG zip file is missing from disk; the FB directory export still exists.

| File | Status |
|------|--------|
| `~/Downloads/extracted_social_data/your_facebook_activity/messages/` | Present |
| `~/Downloads/instagram-youngmoneyinvestments-2026-01-28-3IlR0and.zip` | MISSING |

---

## 3. Automatic Fix Applied

None. This pipeline is intentionally manual (Meta makes it so). No automated fix is possible without violating Meta's Terms of Service or attempting scraping.

The staleness monitor described in Section 5 was deployed and has already written its first alert observation to cortex (id: `kg-manual-20260417-011030`).

---

## 4. Manual Steps Required (Cameron)

### Step A: Request a new Meta data export

1. Go to https://accountscenter.facebook.com/
2. Navigate: Your information and permissions -> Download your information -> Download or transfer information
3. Select profiles: your Facebook profile AND your Instagram account (@youngmoneyinvestments)
4. Choose: **Specific types of information** -> **Messages**
5. Format: **JSON** (not HTML)
6. Date range: from 2026-01-28 to today
7. Submit. Meta will email you when the export is ready (can take hours to several days).

### Step B: Download and place the files

For **Instagram**: download the zip. Rename it or update the path constant in the migration script.

- Script constant (line 29 of `scripts/04_migrate_instagram.py`):
  ```python
  INSTAGRAM_ZIP_PATH = os.path.expanduser(
      "~/Downloads/instagram-youngmoneyinvestments-2026-01-28-3IlR0and.zip"
  )
  ```
- Either place the new zip at the same path OR update this line (and the matching entry in `config/migration_config.yaml`) to the new filename.

For **Facebook**: the directory export lands at the same path -- just download the new export folder. No path change needed unless Meta changes the folder structure.

### Step C: Run the migration scripts

```bash
source ~/venv/bin/activate
cd ~/Projects/messaging-migration

# Facebook
python scripts/03_migrate_facebook.py

# Instagram
python scripts/04_migrate_instagram.py

# Verify
python scripts/06_verify_migration.py
```

The scripts use `ON CONFLICT ... DO UPDATE` / dedup logic, so re-running is safe and will only insert new rows.

### Step D: Trigger embedding refresh

The 6h cron will pick up new rows automatically. To force it immediately:

```bash
~/clawd/venv/bin/python3 ~/clawd/scripts/social_embed_pipeline.py --platform all
```

---

## 5. Hardening: Staleness Monitor

**Script:** `/Users/cameronbennion/Projects/llm-cortex/scripts/check_meta_staleness.py`

Queries `MAX(sent_at)` per platform from the Storage VPS. If either platform is stale beyond the threshold (default 14 days), saves an alert observation to the cortex memory worker tagged `alert,meta,facebook,instagram,messaging,staleness`.

**How to wire it to the weekly maintenance cron:**

The maintenance plist (`~/Library/LaunchAgents/com.cortex.maintenance.plist`) runs `scripts/maintenance.py` weekly on Sunday at 04:00. To add the staleness check without modifying `maintenance.py`, update the plist to run both scripts sequentially. Alternatively, add a one-liner to whatever weekly wrapper drives maintenance.

Simplest additive approach -- add a second `ProgramArguments` entry is not possible in a single plist. Instead, create a thin wrapper or add a PostStop hook. The recommended pattern:

```bash
# Run maintenance, then staleness check (failures in staleness check don't break maintenance)
/Users/cameronbennion/clawd/venv/bin/python3 /Users/cameronbennion/Projects/llm-cortex/scripts/maintenance.py
CORTEX_WORKER_API_KEY=cortex-local-2026 /Users/cameronbennion/clawd/venv/bin/python3 \
    /Users/cameronbennion/Projects/llm-cortex/scripts/check_meta_staleness.py || true
```

**First-run output (2026-04-16, verifying alert fires for 78-day-stale state):**

```
2026-04-16T20:10:29 [INFO] === Meta (FB/IG) staleness check START ===
2026-04-16T20:10:29 [INFO] stale_threshold=14 days  dry_run=False
2026-04-16T20:10:29 [INFO]   facebook: last_sent=2026-01-28 10:26:27  status=STALE (78d)
2026-04-16T20:10:29 [INFO]   instagram: last_sent=2026-01-28 11:27:55  status=STALE (78d)
2026-04-16T20:10:29 [WARNING] STALE PLATFORMS: ['facebook', 'instagram']
2026-04-16T20:10:30 [INFO] Alert saved to cortex: id=kg-manual-20260417-011030
2026-04-16T20:10:30 [INFO] === Meta staleness check COMPLETE (stale) ===
```

Alert correctly fires. Cortex observation written.

---

*Generated by Pass MM of the adversarial improvement loop.*
