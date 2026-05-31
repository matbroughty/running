# Strava → training plan automation

Auto-logs Strava activities tagged `#fav10k` into the training plan and log via a hourly GitHub Actions cron. Opens a PR for review.

## What it does

For each new Strava activity (Run, Workout, WeightTraining) whose description contains `#fav10k`:

- Matches it to the corresponding day in [`Faversham-10K-2026.md`](../Faversham-10K-2026.md).
- Ticks the day's checkbox + fills the **Actuals** line if planned and actual line up.
- Adds an _Unplanned_ marker under the day's plan line if the activity differs from what was planned.
- Inserts a new session entry at the top of [`Training-Log.md`](../Training-Log.md).
- Updates current weight (Dashboard, plan, log history, Sunday weigh-in table) if the description contains `wNN.N` (e.g. `w93.8`).
- Opens a PR you review before merging.

## What you write in Strava

In the activity description, include:

| You write     | Effect                                       |
| ------------- | -------------------------------------------- |
| `#fav10k`     | Required. Opts the activity into the sync.   |
| `RPE 6`       | Sets RPE in the log entry (also `RPE 6/7`).  |
| `w93.8`       | Updates current weight + history (in kg).    |
| `notes: ...`  | Free-text notes — everything after goes in the log entry. |

Example:
```
#fav10k RPE 6 w93.8
notes: Closing km dropped into steady. Knees fine.
```

All fields are optional except `#fav10k`. Missing fields are left blank in the log.

## One-time setup

1. **Create a Strava API app** at <https://www.strava.com/settings/api>
   - Set _Authorization Callback Domain_ to `localhost`
   - Note your **Client ID** and **Client Secret**

2. **Get a refresh token** by running the bootstrap script locally on your Mac:
   ```bash
   cd /Users/mathewbroughton/Documents/PersonalVault/personal/Running
   export STRAVA_CLIENT_ID=<your client id>
   export STRAVA_CLIENT_SECRET=<your client secret>
   pip install -r scripts/requirements.txt
   python3 scripts/bootstrap_strava.py
   ```
   Browser opens, you approve, script prints a refresh token.

3. **Add three secrets** to the GitHub repo at
   `Settings → Secrets and variables → Actions → New repository secret`:
   - `STRAVA_CLIENT_ID`
   - `STRAVA_CLIENT_SECRET`
   - `STRAVA_REFRESH_TOKEN`

4. **Verify** by running the workflow manually:
   - Go to `Actions → Sync Strava activities → Run workflow`
   - Should complete with "No actionable activities" on first run if you haven't tagged anything yet.

## Local testing

To test the script without committing or pushing:

```bash
export STRAVA_CLIENT_ID=...
export STRAVA_CLIENT_SECRET=...
export STRAVA_REFRESH_TOKEN=...
python3 scripts/sync_strava.py --dry-run
```

This will print the PR title and body it _would_ have created.

## Schedule

Cron: `0 6-20 * * *` (UTC) = hourly 07:00–21:00 BST during British Summer Time. BST is active for the full May–September training window, so this covers everything up to race day.

Manual trigger: `Actions → Sync Strava activities → Run workflow`.

## State file

`.sync-strava-state.json` (repo root) tracks already-synced activity IDs and the last sync timestamp. It's committed to the same PR as the content updates so the dedupe state is durable.

If you delete it, the bot will re-sync everything since the plan start date. Content-level dedupe (Training-Log entries matched by date) prevents duplicate log entries.

## Files this touches

- `Faversham-10K-2026.md` — tick checkboxes, fill Actuals, add Unplanned markers, update weight cell and weigh-in row
- `Training-Log.md` — insert sessions, add weight-history rows
- `Dashboard.md` — update current weight cell
- `.sync-strava-state.json` — sync state
