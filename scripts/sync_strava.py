"""Sync Strava activities tagged #fav10k into the training plan and log.

Runs unattended on GitHub Actions cron. For each new activity:
- Fetches detail from Strava
- Parses #fav10k tag, RPE (e.g. "RPE 6"), weight (e.g. "w93.8"), and notes
- Matches to the planned day in Faversham-10K-2026.md
- Edits Faversham-10K-2026.md (checkbox + actuals) and Training-Log.md
- Opens a PR for review

Idempotency:
- Activity IDs already in .sync-strava-state.json are skipped
- A Training-Log content scan blocks duplicate logging of manually-logged sessions

Run locally with --dry-run to test without committing or pushing.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# --- Config -----------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
PLAN_FILE = REPO_ROOT / "Faversham-10K-2026.md"
LOG_FILE = REPO_ROOT / "Training-Log.md"
DASH_FILE = REPO_ROOT / "Dashboard.md"
STATE_FILE = REPO_ROOT / ".sync-strava-state.json"

PLAN_START = date(2026, 5, 31)
RACE_DAY = date(2026, 9, 27)
PLAN_YEAR = 2026

ACTIVITY_TAG = "#fav10k"
ALLOWED_TYPES = {"Run", "Workout", "WeightTraining"}
FETCH_BUFFER_HOURS = 48

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync_strava")


# --- Data types -------------------------------------------------------------


@dataclass
class ParsedActivity:
    id: int
    type: str
    name: str
    description: str
    distance_m: float
    moving_time_s: int
    elapsed_time_s: int
    start_date_local: datetime
    avg_hr: float | None
    max_hr: float | None
    splits_km: list[dict]
    perceived_exertion: float | None
    rpe: str | None
    weight_kg: float | None
    notes: str | None
    strava_url: str

    @property
    def activity_date(self) -> date:
        return self.start_date_local.date()


@dataclass
class WeekSection:
    label: str
    start_date: date
    end_date: date
    start_offset: int
    end_offset: int


@dataclass
class LogResult:
    activity: ParsedActivity
    week: WeekSection | None
    planned_summary: str
    match_status: str  # match | mismatch_rest | mismatch_session | race | out_of_plan
    match_note: str
    skipped: bool = False
    skip_reason: str = ""


# --- State -------------------------------------------------------------------


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"synced_ids": [], "last_synced_at": None}
    return json.loads(STATE_FILE.read_text())


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


# --- Strava API -------------------------------------------------------------


class Strava:
    def __init__(self) -> None:
        self.client_id = os.environ["STRAVA_CLIENT_ID"]
        self.client_secret = os.environ["STRAVA_CLIENT_SECRET"]
        self.refresh_token = os.environ["STRAVA_REFRESH_TOKEN"]
        self._access_token: str | None = None

    def _refresh(self) -> None:
        r = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30,
        )
        r.raise_for_status()
        self._access_token = r.json()["access_token"]

    def _get(self, path: str, **params) -> Any:
        if not self._access_token:
            self._refresh()
        r = requests.get(
            f"https://www.strava.com/api/v3{path}",
            headers={"Authorization": f"Bearer {self._access_token}"},
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def list_activities(self, after_epoch: int) -> list[dict]:
        return self._get("/athlete/activities", after=after_epoch, per_page=50)

    def get_activity(self, activity_id: int) -> dict:
        return self._get(f"/activities/{activity_id}")


# --- Description parsing ----------------------------------------------------

RPE_RE = re.compile(r"\brpe\s*:?\s*(\d{1,2}(?:[/-]\d{1,2})?)\b", re.IGNORECASE)
WEIGHT_RE = re.compile(r"\bw(\d{2,3}(?:\.\d)?)\b", re.IGNORECASE)
NOTES_RE = re.compile(r"\bnotes\s*:\s*(.+)$", re.IGNORECASE | re.DOTALL)


def parse_description(desc: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not desc:
        return out
    m = RPE_RE.search(desc)
    if m:
        out["rpe"] = m.group(1).replace("/", "–").replace("-", "–")
    m = WEIGHT_RE.search(desc)
    if m:
        out["weight_kg"] = float(m.group(1))
    m = NOTES_RE.search(desc)
    if m:
        out["notes"] = m.group(1).strip()
    return out


# --- Formatting helpers -----------------------------------------------------


def fmt_duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_pace(distance_m: float, time_s: int) -> str:
    if distance_m <= 0 or time_s <= 0:
        return "—"
    sec_per_km = time_s / (distance_m / 1000)
    m, s = divmod(int(round(sec_per_km)), 60)
    return f"{m}:{s:02d}/km"


def fmt_distance(distance_m: float) -> str:
    return f"{distance_m / 1000:.2f} km"


def fmt_short_date(d: date) -> str:
    return d.strftime("%a %-d %b")


def fmt_iso_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def fmt_history_date(d: date) -> str:
    return d.strftime("%-d %b %Y")


# --- Plan parsing -----------------------------------------------------------

WEEK_HEADER_RE = re.compile(r"^### (Week \S+|Race Week)\s+—\s+(.+)$", re.MULTILINE)
WEEK_DATES_RE = re.compile(
    r"\*\*Dates:\*\*\s+(.+?)\s+[–-]\s+(.+?)\s*$", re.MULTILINE
)


def parse_short_date(s: str, year: int = PLAN_YEAR) -> date | None:
    s = s.strip()
    for fmt in ("%a %d %b %Y", "%d %b %Y"):
        try:
            return datetime.strptime(f"{s} {year}", fmt).date()
        except ValueError:
            continue
    return None


def find_week_sections(plan_text: str) -> list[WeekSection]:
    headers = list(WEEK_HEADER_RE.finditer(plan_text))
    sections: list[WeekSection] = []
    for i, h in enumerate(headers):
        section_start = h.start()
        if i + 1 < len(headers):
            section_end = headers[i + 1].start()
        else:
            # Search from end of this header (h.end()), not section_start + 1 —
            # otherwise the "## " inside "### " of the current header matches.
            nxt = re.search(r"^## ", plan_text[h.end() :], re.MULTILINE)
            section_end = h.end() + nxt.start() if nxt else len(plan_text)

        section_text = plan_text[section_start:section_end]
        dates_match = WEEK_DATES_RE.search(section_text)
        if not dates_match:
            continue
        sd = parse_short_date(dates_match.group(1))
        ed = parse_short_date(dates_match.group(2))
        if not sd or not ed:
            continue
        sections.append(
            WeekSection(
                label=h.group(0)[4:].rstrip(),
                start_date=sd,
                end_date=ed,
                start_offset=section_start,
                end_offset=section_end,
            )
        )
    return sections


def find_week_for_date(plan_text: str, target: date) -> WeekSection | None:
    for w in find_week_sections(plan_text):
        if w.start_date <= target <= w.end_date:
            return w
    return None


DAY_LINE_RE = re.compile(
    r"^- \[[ x]\]\s+(\S+\s+\d{1,2}\s+\S+)\s*—\s*(.+)$", re.MULTILINE
)


def extract_planned_session(plan_text: str, week: WeekSection, target: date) -> str:
    section = plan_text[week.start_offset : week.end_offset]
    for m in DAY_LINE_RE.finditer(section):
        if parse_short_date(m.group(1)) == target:
            return m.group(2).strip()
    return ""


# --- Plan editing -----------------------------------------------------------


def tick_checkbox(
    plan_text: str, week: WeekSection, target: date
) -> tuple[str, bool]:
    """Tick the day's '- [ ]'. Returns (new_plan_text, already_ticked)."""
    section = plan_text[week.start_offset : week.end_offset]
    out_lines: list[str] = []
    already = False
    changed = False
    box_re = re.compile(
        r"^(- \[)([ x])(\]\s+)(\S+\s+\d{1,2}\s+\S+)(\s*—\s*.+)$"
    )
    for line in section.splitlines(keepends=True):
        m = box_re.match(line.rstrip("\n"))
        if m and parse_short_date(m.group(4)) == target:
            if m.group(2) == "x":
                already = True
                out_lines.append(line)
            else:
                out_lines.append(
                    f"{m.group(1)}x{m.group(3)}{m.group(4)}{m.group(5)}\n"
                )
                changed = True
        else:
            out_lines.append(line)
    new_section = "".join(out_lines)
    if not changed and not already:
        log.warning("Checkbox not found for %s", target)
        return plan_text, False
    return (
        plan_text[: week.start_offset] + new_section + plan_text[week.end_offset :],
        already,
    )


ACTUALS_BLOCK_RE = re.compile(r"\*\*Actuals\*\*\s*\n((?:- .+\n)+)")


def fill_actuals_line(
    plan_text: str, week: WeekSection, target: date, summary: str
) -> str:
    section = plan_text[week.start_offset : week.end_offset]
    block_match = ACTUALS_BLOCK_RE.search(section)
    if not block_match:
        return plan_text
    block_start = block_match.start(1)
    block = block_match.group(1)

    new_lines: list[str] = []
    matched = False
    for line in block.splitlines(keepends=True):
        m = re.match(
            r"^(- )(\S+\s+\d{1,2}\s+\S+)(\s*\([^)]+\))?\s*:\s*$",
            line.rstrip("\n"),
        )
        if m and parse_short_date(m.group(2)) == target:
            suffix = m.group(3) or ""
            new_lines.append(f"{m.group(1)}{m.group(2)}{suffix}: {summary}\n")
            matched = True
        else:
            new_lines.append(line)
    if not matched:
        return plan_text
    new_block = "".join(new_lines)
    new_section = section[:block_start] + new_block + section[block_start + len(block) :]
    return (
        plan_text[: week.start_offset] + new_section + plan_text[week.end_offset :]
    )


def add_unplanned_marker(
    plan_text: str, week: WeekSection, target: date, summary: str
) -> str:
    """For activities that don't match the planned session, add a sub-bullet
    under the day's plan line noting what was actually done."""
    section = plan_text[week.start_offset : week.end_offset]
    out_lines: list[str] = []
    inserted = False
    for line in section.splitlines(keepends=True):
        out_lines.append(line)
        m = DAY_LINE_RE.match(line.rstrip("\n"))
        if (
            m
            and not inserted
            and parse_short_date(m.group(1)) == target
        ):
            out_lines.append(f"  - *Unplanned: {summary}*\n")
            inserted = True
    if not inserted:
        return plan_text
    new_section = "".join(out_lines)
    return (
        plan_text[: week.start_offset] + new_section + plan_text[week.end_offset :]
    )


# --- Log editing ------------------------------------------------------------

LOG_INSERT_MARKER = "<!-- Add new sessions here. Newest first. -->"


def classify_type(act: ParsedActivity) -> str:
    """Map a Strava activity to a workout-library name."""
    if act.type in ("Workout", "WeightTraining"):
        return "HIIT"
    name_low = (act.name or "").lower()
    if "400" in name_low or "interval" in name_low:
        return "400m repeats"
    if "800" in name_low:
        return "800m repeats"
    if "tempo" in name_low or "threshold" in name_low:
        return "tempo"
    if "ladder" in name_low:
        return "ladder"
    if "hill" in name_low:
        return "hills"
    if "parkrun" in name_low:
        return "parkrun"
    if act.distance_m >= 10000:
        return "long run"
    return "easy"


def build_log_entry(act: ParsedActivity, planned_summary: str) -> str:
    split_strs = []
    for s in (act.splits_km or [])[:15]:
        d = s.get("distance", 0)
        t = s.get("moving_time", 0) or s.get("elapsed_time", 0)
        if d > 0 and t > 0:
            split_strs.append(fmt_pace(d, t))
    splits = ", ".join(split_strs) if split_strs else ""

    hr = ""
    if act.avg_hr or act.max_hr:
        avg = int(act.avg_hr) if act.avg_hr else "—"
        mx = int(act.max_hr) if act.max_hr else "—"
        hr = f"{avg} / {mx}"

    rpe = act.rpe or (
        f"{int(act.perceived_exertion)}" if act.perceived_exertion else ""
    )

    return (
        f"### {fmt_iso_date(act.activity_date)} — {act.name}\n"
        f"- **Type:** {classify_type(act)}\n"
        f"- **Planned:** {planned_summary or '—'}\n"
        f"- **Actual:** {fmt_distance(act.distance_m)} in "
        f"{fmt_duration(act.moving_time_s)}\n"
        f"- **Distance:** {fmt_distance(act.distance_m)}\n"
        f"- **Total time:** {fmt_duration(act.moving_time_s)}\n"
        f"- **Average pace:** {fmt_pace(act.distance_m, act.moving_time_s)}\n"
        f"- **Splits / reps:** {splits}\n"
        f"- **Weight (am):** {f'{act.weight_kg:.1f} kg' if act.weight_kg else ''}\n"
        f"- **RPE (1–10):** {rpe}\n"
        f"- **How it felt:**\n"
        f"- **Heart rate avg / max:** {hr}\n"
        f"- **Weather / conditions:**\n"
        f"- **Notes:** {act.notes or ''}\n"
        f"- **Strava:** {act.strava_url}\n"
    )


def has_existing_log_entry(log_text: str, on_date: date) -> bool:
    pattern = re.compile(
        rf"^### {re.escape(fmt_iso_date(on_date))}\b", re.MULTILINE
    )
    return bool(pattern.search(log_text))


def insert_log_entry(log_text: str, entry: str) -> str:
    if LOG_INSERT_MARKER not in log_text:
        log.error("Log insert marker missing")
        return log_text
    return log_text.replace(
        LOG_INSERT_MARKER, f"{LOG_INSERT_MARKER}\n\n{entry}", 1
    )


# --- Weight editing ---------------------------------------------------------

WEIGHT_HISTORY_RE = re.compile(
    r"(## Weight history\s*\n\|[^\n]+\n\|[-:\s|]+\n)", re.MULTILINE
)


def update_weight_history(
    log_text: str, on_date: date, weight_kg: float
) -> str:
    m = WEIGHT_HISTORY_RE.search(log_text)
    if not m:
        log.warning("Weight history table not found in log")
        return log_text
    insertion = m.end()
    row = (
        f"| {fmt_history_date(on_date)}  | {weight_kg:.1f}kg "
        f"| (auto from Strava) |\n"
    )
    return log_text[:insertion] + row + log_text[insertion:]


def update_weigh_in_table(
    plan_text: str, on_date: date, weight_kg: float
) -> str:
    label = fmt_history_date(on_date)
    pattern = re.compile(
        rf"^(\|\s*\S+\s*\|\s*{re.escape(label)}\s*\|)\s*(\|.*)$",
        re.MULTILINE,
    )
    return pattern.sub(rf"\1 {weight_kg:.1f}   \2", plan_text, count=1)


WEIGHT_ROW_DASH_RE = re.compile(
    r"(\|\s*Weight\s*\|\s*)\d+(?:\.\d)?kg(\s*\|)", re.IGNORECASE
)
WEIGHT_ROW_PLAN_RE = re.compile(
    r"(\|\s*Weight\s*\|\s*)\d+(?:\.\d)?\s*kg(\s*\|)", re.IGNORECASE
)


def update_current_weight(
    dashboard_text: str, plan_text: str, weight_kg: float
) -> tuple[str, str]:
    new_d = WEIGHT_ROW_DASH_RE.sub(
        rf"\g<1>{weight_kg:.1f}kg\g<2>", dashboard_text, count=1
    )
    new_p = WEIGHT_ROW_PLAN_RE.sub(
        rf"\g<1>{weight_kg:.1f} kg\g<2>", plan_text, count=1
    )
    return new_d, new_p


# --- Match classification ---------------------------------------------------


def classify_match(
    activity: ParsedActivity, planned_summary: str
) -> tuple[str, str]:
    if activity.activity_date == RACE_DAY:
        return (
            "race",
            "RACE DAY — review and fill the race result table manually.",
        )

    if not planned_summary:
        return "out_of_plan", "No planned session line found for this date."

    planned_lower = planned_summary.lower()

    if "rest" in planned_lower:
        return (
            "mismatch_rest",
            "Planned: Rest. Activity logged, no checkbox to tick.",
        )

    if "hiit" in planned_lower:
        if activity.type in {"Workout", "WeightTraining"}:
            return "match", ""
        return (
            "mismatch_session",
            f"Planned: HIIT class. Strava activity is a {activity.type}.",
        )

    if activity.type == "Run":
        return "match", ""

    return (
        "mismatch_session",
        f"Planned: running session. Strava activity type: {activity.type}.",
    )


# --- Strava → ParsedActivity ------------------------------------------------


def to_parsed(detail: dict) -> ParsedActivity:
    parsed = parse_description(detail.get("description") or "")
    start_str = detail.get("start_date_local") or detail.get("start_date")
    sdl = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
    return ParsedActivity(
        id=int(detail["id"]),
        type=detail.get("type", "Run"),
        name=detail.get("name", "") or "Unnamed",
        description=detail.get("description") or "",
        distance_m=float(detail.get("distance", 0) or 0),
        moving_time_s=int(detail.get("moving_time", 0) or 0),
        elapsed_time_s=int(detail.get("elapsed_time", 0) or 0),
        start_date_local=sdl,
        avg_hr=detail.get("average_heartrate"),
        max_hr=detail.get("max_heartrate"),
        splits_km=detail.get("splits_metric") or [],
        perceived_exertion=detail.get("perceived_exertion"),
        rpe=parsed.get("rpe"),
        weight_kg=parsed.get("weight_kg"),
        notes=parsed.get("notes"),
        strava_url=f"https://www.strava.com/activities/{detail['id']}",
    )


# --- Summary lines for actuals + PR body ------------------------------------


def actuals_summary(act: ParsedActivity) -> str:
    rpe_part = ""
    if act.rpe:
        rpe_part = f", RPE {act.rpe}"
    elif act.perceived_exertion:
        rpe_part = f", RPE {int(act.perceived_exertion)}"
    return (
        f"{fmt_distance(act.distance_m)} in "
        f"{fmt_duration(act.moving_time_s)} "
        f"({fmt_pace(act.distance_m, act.moving_time_s)} avg){rpe_part}"
    )


# --- Git --------------------------------------------------------------------


def run_cmd(cmd: list[str], cwd: Path = REPO_ROOT) -> str:
    log.info("$ %s", " ".join(cmd))
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        log.error("Command failed: %s\nstdout: %s\nstderr: %s", cmd, res.stdout, res.stderr)
        raise RuntimeError(f"Command failed: {cmd}")
    return res.stdout.strip()


def configure_git() -> None:
    run_cmd(["git", "config", "user.name", "github-actions[bot]"])
    run_cmd([
        "git", "config", "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
    ])


def create_branch_commit_push(
    branch: str, files: list[Path], message: str
) -> bool:
    run_cmd(["git", "fetch", "origin", "main"])
    run_cmd(["git", "checkout", "-B", branch, "origin/main"])
    for f in files:
        run_cmd(["git", "add", str(f.relative_to(REPO_ROOT))])
    if not run_cmd(["git", "status", "--porcelain"]):
        log.info("No changes after staging.")
        return False
    run_cmd(["git", "commit", "-m", message])
    run_cmd(["git", "push", "-f", "origin", branch])
    return True


def open_pr(branch: str, title: str, body: str) -> str:
    try:
        return run_cmd([
            "gh", "pr", "create",
            "--base", "main",
            "--head", branch,
            "--title", title,
            "--body", body,
        ])
    except RuntimeError:
        return run_cmd([
            "gh", "pr", "view", branch, "--json", "url", "-q", ".url",
        ])


# --- PR body ----------------------------------------------------------------


STATUS_LABELS = {
    "match": "✓ Exact match",
    "mismatch_rest": "⚠ Unplanned activity on rest day",
    "mismatch_session": "⚠ Activity type differs from planned session",
    "race": "🏁 RACE DAY",
    "out_of_plan": "⚠ Date outside the plan",
}


def compose_pr_body(results: list[LogResult]) -> str:
    lines = ["Auto-synced Strava activities. Review and merge to log.", ""]
    for r in results:
        a = r.activity
        lines.append(f"## {fmt_iso_date(a.activity_date)} — {a.name}")
        lines.append("")
        lines.append(f"- Distance: {fmt_distance(a.distance_m)}")
        lines.append(
            f"- Time: {fmt_duration(a.moving_time_s)} "
            f"({fmt_pace(a.distance_m, a.moving_time_s)} avg)"
        )
        if a.avg_hr:
            mx = int(a.max_hr) if a.max_hr else "—"
            lines.append(f"- HR avg / max: {int(a.avg_hr)} / {mx}")
        if a.rpe:
            lines.append(f"- RPE (from description): {a.rpe}")
        if a.weight_kg:
            lines.append(f"- Weight (from description): {a.weight_kg:.1f} kg")
        lines.append(f"- Strava: {a.strava_url}")
        lines.append("")
        lines.append(f"**Match:** {STATUS_LABELS.get(r.match_status, r.match_status)}")
        if r.planned_summary:
            lines.append(f"**Planned:** {r.planned_summary}")
        if r.match_note:
            lines.append(f"**Note:** {r.match_note}")
        if r.skipped:
            lines.append(f"**Skipped:** {r.skip_reason}")
        lines.append("")
        if r.match_status == "mismatch_rest":
            lines.append("**Decide at merge:**")
            lines.append("- [ ] Accept as bonus mileage (default — plan unchanged)")
            lines.append("- [ ] Swap a planned session to a different day (follow-up edit)")
            lines.append("- [ ] Revert this PR if the activity was a duplicate")
            lines.append("")
        lines.append("---")
        lines.append("")
    lines.append(
        f"Generated by `.github/workflows/sync-strava.yml`. Activities are "
        f"matched by description containing `{ACTIVITY_TAG}`."
    )
    return "\n".join(lines)


# --- Main -------------------------------------------------------------------


def filter_relevant(summaries: list[dict], synced_ids: set[int]) -> list[int]:
    relevant: list[int] = []
    for s in summaries:
        aid = int(s["id"])
        if aid in synced_ids:
            continue
        if s.get("type") not in ALLOWED_TYPES:
            continue
        relevant.append(aid)
    return relevant


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Do everything except commit, push, and open PR",
    )
    args = parser.parse_args()

    state = load_state()
    synced_ids: set[int] = set(state.get("synced_ids", []))

    last_synced_at_str = state.get("last_synced_at")
    if last_synced_at_str:
        last_synced_at = datetime.fromisoformat(last_synced_at_str)
    else:
        last_synced_at = datetime.combine(
            PLAN_START, datetime.min.time(), tzinfo=timezone.utc
        )
    fetch_after = last_synced_at - timedelta(hours=FETCH_BUFFER_HOURS)
    log.info("Fetching activities after %s", fetch_after.isoformat())

    strava = Strava()
    summaries = strava.list_activities(int(fetch_after.timestamp()))
    log.info("Got %d activity summaries", len(summaries))

    candidate_ids = filter_relevant(summaries, synced_ids)
    log.info("%d candidates to fetch detail for", len(candidate_ids))

    plan_text = PLAN_FILE.read_text()
    log_text = LOG_FILE.read_text()
    dash_text = DASH_FILE.read_text()

    results: list[LogResult] = []
    files_changed: set[Path] = set()

    for aid in candidate_ids:
        try:
            detail = strava.get_activity(aid)
        except requests.HTTPError as e:
            log.warning("Failed to fetch activity %d: %s", aid, e)
            continue

        desc = detail.get("description") or ""
        if ACTIVITY_TAG.lower() not in desc.lower():
            log.info("Activity %d skipped (no %s tag)", aid, ACTIVITY_TAG)
            synced_ids.add(aid)
            continue

        act = to_parsed(detail)

        # Content-level dedupe (catches manually-logged sessions)
        if has_existing_log_entry(log_text, act.activity_date):
            results.append(LogResult(
                activity=act, week=None, planned_summary="",
                match_status="out_of_plan", match_note="",
                skipped=True,
                skip_reason=(
                    f"Training-Log already has an entry for "
                    f"{fmt_iso_date(act.activity_date)} (likely logged manually)"
                ),
            ))
            synced_ids.add(aid)
            continue

        week = find_week_for_date(plan_text, act.activity_date)
        if not week:
            planned_summary = ""
            status, note = "out_of_plan", "Activity date is outside the plan window."
        else:
            planned_summary = extract_planned_session(
                plan_text, week, act.activity_date
            )
            status, note = classify_match(act, planned_summary)

        result = LogResult(
            activity=act, week=week,
            planned_summary=planned_summary,
            match_status=status, match_note=note,
        )

        summary = actuals_summary(act)

        if week and status == "match":
            plan_text, _ = tick_checkbox(plan_text, week, act.activity_date)
            plan_text = fill_actuals_line(
                plan_text, week, act.activity_date, summary
            )
            files_changed.add(PLAN_FILE)
        elif week and status in {"mismatch_rest", "mismatch_session"}:
            plan_text = add_unplanned_marker(
                plan_text, week, act.activity_date, summary
            )
            files_changed.add(PLAN_FILE)

        log_text = insert_log_entry(
            log_text, build_log_entry(act, planned_summary)
        )
        files_changed.add(LOG_FILE)

        if act.weight_kg:
            log_text = update_weight_history(
                log_text, act.activity_date, act.weight_kg
            )
            dash_text, plan_text = update_current_weight(
                dash_text, plan_text, act.weight_kg
            )
            files_changed.add(DASH_FILE)
            files_changed.add(PLAN_FILE)
            if act.activity_date.weekday() == 6:  # Sunday
                plan_text = update_weigh_in_table(
                    plan_text, act.activity_date, act.weight_kg
                )

        synced_ids.add(act.id)
        results.append(result)

    state["synced_ids"] = sorted(synced_ids)
    state["last_synced_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    actionable = [r for r in results if not r.skipped]
    if not actionable:
        log.info("No actionable activities. Done.")
        return 0

    files_changed.add(STATE_FILE)
    if PLAN_FILE in files_changed:
        PLAN_FILE.write_text(plan_text)
    if LOG_FILE in files_changed:
        LOG_FILE.write_text(log_text)
    if DASH_FILE in files_changed:
        DASH_FILE.write_text(dash_text)

    if len(actionable) == 1:
        a = actionable[0].activity
        title = f"Log {fmt_short_date(a.activity_date)}: {a.name}"
    else:
        title = (
            f"Log {len(actionable)} Strava sessions "
            f"({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"
        )

    body = compose_pr_body(results)

    if args.dry_run:
        log.info("--dry-run: skipping commit/push/PR.")
        print("=== PR TITLE ===")
        print(title)
        print("=== PR BODY ===")
        print(body)
        return 0

    branch = f"sync-strava-{int(time.time())}"
    configure_git()
    committed = create_branch_commit_push(
        branch, sorted(files_changed), f"Auto-sync: {title}"
    )
    if not committed:
        log.info("Nothing to commit.")
        return 0
    pr_url = open_pr(branch, title, body)
    log.info("PR: %s", pr_url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
