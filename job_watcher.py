#!/usr/bin/env python3
"""
job_watcher.py
--------------
job board watchlist+viewed history tracker.
surfaces postings not viewed yet; mark ones already viewed to only grab ones that are fresh.
(applications tracked elsewhere. this is just the "what's new" feed.)

fetching via jobhive (kalil0321/ats-scrapers).
each source = ats + company slug. `python job_watcher.py platforms` lists them.

deps: pip install jobhive-py

usage:
    python job_watcher.py platforms                # list supported ATS platforms
    python job_watcher.py test <ats> <slug>        # check a company before adding
    python job_watcher.py add "Acme" <ats> <slug>  # add a company
    python job_watcher.py sources                  # list configured companies
    python job_watcher.py remove <name-or-number>  # remove a company
    python job_watcher.py init                     # write a sample sources.json
    python job_watcher.py run                      # pull new postings (--title to filter)
    python job_watcher.py list                     # show unseen postings
    python job_watcher.py review                   # step through, mark viewed
    python job_watcher.py seen 5-25                # mark viewed: id / range / all
    python job_watcher.py watch -i 3600            # poll on a loop
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# --------------------------------------------------------------------------- #
# posting model
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    source: str
    external_id: str
    title: str
    company: str = ""
    location: str = ""
    url: str = ""


# --------------------------------------------------------------------------- #
# fetching via jobhive (kalil0321/ats-scrapers)
# --------------------------------------------------------------------------- #
def _field(record: object, *names: str) -> str:
    """first non-empty field by name; handles object attrs or dict keys."""
    for n in names:
        v = getattr(record, n, None)
        if v is None and isinstance(record, dict):
            v = record.get(n)
        if v:
            return str(v)
    return ""


def fetch_jobhive(cfg: dict) -> list[Job]:
    """one company's postings, via jobhive.

    config = {"type": "jobhive", "ats": "<platform>", "slug": "<company>"}.
    """
    try:
        from jobhive.scrapers import get_scraper
    except ImportError as exc:
        raise RuntimeError("pip install jobhive-py to use jobhive sources") from exc

    ats, slug = cfg["ats"], cfg["slug"]
    out: list[Job] = []
    for j in get_scraper(ats, slug).fetch():
        title = _field(j, "title")
        url = _field(j, "url", "apply_url")
        # stable id, never blank
        gid = _field(j, "global_id")
        rid = _field(j, "ats_id", "id", "requisition_id")
        if gid:
            ext = gid
        elif rid:
            ext = f"{ats}:{rid}"
        elif url:
            ext = url
        else:
            ext = f"{ats}:{hashlib.sha1(title.encode('utf-8')).hexdigest()}"
        out.append(
            Job(
                source=cfg["name"],
                external_id=ext,
                title=title,
                company=_field(j, "company") or slug,
                location=_field(j, "location"),
                url=url,
            )
        )
    return out


FETCHERS = {"jobhive": fetch_jobhive}


# --------------------------------------------------------------------------- #
# storage: sqlite, new + viewed status
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    pk          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title       TEXT,
    company     TEXT,
    location    TEXT,
    url         TEXT,
    first_seen  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'new',
    UNIQUE(source, external_id)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.executescript(SCHEMA)
    return con


def upsert_jobs(con: sqlite3.Connection, jobs: list[Job]) -> list[sqlite3.Row]:
    """insert unseens, return the new ones.

    already-stored rows (viewed or not) skipped, so dismissed stays gone.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_rows: list[sqlite3.Row] = []
    for j in jobs:
        cur = con.execute(
            "INSERT OR IGNORE INTO jobs"
            "(source, external_id, title, company, location, url, first_seen) "
            "VALUES (?,?,?,?,?,?,?)",
            (j.source, j.external_id, j.title, j.company, j.location, j.url, now),
        )
        if cur.rowcount:
            new_rows.append(
                con.execute(
                    "SELECT * FROM jobs WHERE source=? AND external_id=?",
                    (j.source, j.external_id),
                ).fetchone()
            )
    con.commit()
    return new_rows


def unseen(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM jobs WHERE status='new' ORDER BY first_seen DESC, pk DESC"
    ).fetchall()


def mark_seen(con: sqlite3.Connection, pk: int) -> bool:
    cur = con.execute("UPDATE jobs SET status='seen' WHERE pk=? AND status='new'", (pk,))
    con.commit()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# display
# --------------------------------------------------------------------------- #
def print_rows(rows: list[sqlite3.Row]) -> None:
    """one card per job: id + title, meta line, url. blank line between."""
    for i, r in enumerate(rows):
        if i:
            print()
        print(f"  {r['pk']:>4}  {r['title']}")
        meta = "  ".join(
            x for x in (r["company"], r["location"], (r["first_seen"] or "")[:10]) if x
        )
        if meta:
            print(f"        {meta}")
        if r["url"]:
            print(f"        {r['url']}")


def print_unseen(rows: list[sqlite3.Row]) -> None:
    if not rows:
        print("  (nothing new, you're caught up)")
        return
    print_rows(rows)
    print(f"\n  {len(rows)} unseen.")


# --------------------------------------------------------------------------- #
# keypress + id parsing
# --------------------------------------------------------------------------- #
def read_key() -> str:
    """single keypress, lowercased. line-input fallback if no tty."""
    try:
        import msvcrt  # windows
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # arrow/fn-key prefix
            msvcrt.getwch()
            return ""
        return ch.lower()
    except ImportError:
        pass
    try:
        import termios
        import tty  # posix
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return ch.lower()
    except Exception:
        return (sys.stdin.readline().strip().lower() or " ")[:1]


def expand_ids(con: sqlite3.Connection, tokens: list[str]) -> list[int]:
    """['5-8', '3', 'all'] -> deduped pks."""
    if tokens == ["all"]:
        return [r["pk"] for r in unseen(con)]
    ids: list[int] = []
    for tok in tokens:
        tok = tok.strip()
        parts = tok.split("-", 1)
        if len(parts) == 2 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
            a, b = int(parts[0]), int(parts[1])
            ids.extend(range(min(a, b), max(a, b) + 1))
        elif tok.isdigit():
            ids.append(int(tok))
        else:
            print(f"  ! ignoring bad id {tok!r}", file=sys.stderr)
    seen_set, out = set(), []
    for x in ids:
        if x not in seen_set:
            seen_set.add(x)
            out.append(x)
    return out


def _clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def load_sources(path: str, *, missing_ok: bool = False) -> list[dict]:
    """read sources.json. missing_ok=True returns [] instead of erroring."""
    fp = Path(path)
    if not fp.exists():
        if missing_ok:
            return []
        sys.exit(f"no config at {path!r}. run:  python job_watcher.py init")
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")


def _match_title(title: str, keywords: list[str]) -> bool:
    """true if the title contains any keyword (case-insensitive)."""
    t = title.lower()
    return any(k in t for k in keywords)


def cmd_run(con: sqlite3.Connection, sources: list[dict], delay: float,
            title_filter: list[str] | None = None) -> None:
    total_new = 0
    all_new: list[sqlite3.Row] = []
    for i, cfg in enumerate(sources):
        name = cfg.get("name", "?")
        fetch = FETCHERS.get(cfg.get("type", ""))
        if fetch is None:
            print(f"  ! {name}: unknown type {cfg.get('type')!r}", file=sys.stderr)
            continue
        if i:  # gap between sources
            time.sleep(delay)
        try:
            jobs = fetch(cfg)
        except Exception as exc:  # a bad source never kills the run
            print(f"  ! {name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if title_filter:
            kept = [j for j in jobs if _match_title(j.title, title_filter)]
        else:
            kept = jobs
        new_rows = upsert_jobs(con, kept)
        all_new.extend(new_rows)
        total_new += len(new_rows)
        if title_filter:
            print(f"  {name}: {len(jobs)} listings, {len(kept)} match, {len(new_rows)} new")
        else:
            print(f"  {name}: {len(jobs)} listings, {len(new_rows)} new")
    print(f"\n{total_new} new posting(s).")
    if all_new:
        print()
        print_rows(all_new)


def cmd_watch(con: sqlite3.Connection, config_path: str, interval: int, delay: float,
               title_filter: list[str] | None = None) -> None:
    print(f"watching every {interval}s (re-reads {config_path} each cycle). ctrl-c to stop.")
    sources: list[dict] = []
    try:
        while True:
            # reload the source list every cycle, so edits apply without a restart
            try:
                sources = json.loads(Path(config_path).read_text(encoding="utf-8"))
            except FileNotFoundError:
                print(f"  ! {config_path} not found; retrying next cycle", file=sys.stderr)
            except json.JSONDecodeError as exc:
                print(f"  ! {config_path} is not valid JSON ({exc}); using last good list",
                      file=sys.stderr)
            print(f"\n[{datetime.now():%Y-%m-%d %H:%M}] {len(sources)} source(s), polling...")
            cmd_run(con, sources, delay, title_filter)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopped.")


def cmd_review(con: sqlite3.Connection) -> None:
    """step through unseens + viewed markers."""
    rows = unseen(con)
    if not rows:
        print("  (nothing new, you're caught up)")
        return
    dismissed = 0
    i, n = 0, len(rows)
    while 0 <= i < n:
        r = rows[i]
        _clear_screen()
        print(f"[ {i + 1} / {n} ]\n")
        print(f"  {r['title']}")
        meta = "  ".join(x for x in (r["company"], r["location"]) if x)
        if meta:
            print(f"  {meta}")
        if r["url"]:
            print(f"  {r['url']}")
        print("\n  [d]/space = seen (dismiss)   [o]pen   [s]kip   [b]ack   [q]uit")
        try:
            key = read_key()
        except KeyboardInterrupt:
            break
        if key in ("d", " "):
            if mark_seen(con, r["pk"]):
                dismissed += 1
            i += 1
        elif key == "o":
            if r["url"]:
                webbrowser.open(r["url"])
        elif key in ("s", "\r", "\n"):
            i += 1
        elif key == "b":
            i = max(0, i - 1)
        elif key in ("q", "\x1b", "\x03"):
            break
        # anything else redraws
    _clear_screen()
    print(f"dismissed {dismissed}. {len(unseen(con))} still unseen.")


def cmd_seen(con: sqlite3.Connection, tokens: list[str]) -> None:
    ids = expand_ids(con, tokens)
    done = sum(1 for pk in ids if mark_seen(con, pk))
    msg = f"dismissed {done} posting(s)."
    if done != len(ids):
        msg += f" ({len(ids) - done} not found or already seen)"
    print(msg)


def _write_sources(path: str, sources: list[dict]) -> None:
    Path(path).write_text(json.dumps(sources, indent=2) + "\n", encoding="utf-8")


def cmd_add(path: str, name: str, ats: str, slug: str) -> None:
    sources = load_sources(path, missing_ok=True)
    if any(s.get("name") == name for s in sources):
        sys.exit(f"a source named {name!r} already exists (names must be unique).")
    dup = next((s for s in sources if s.get("ats") == ats and s.get("slug") == slug), None)
    if dup is not None:
        sys.exit(f"already added as {dup.get('name')!r} [{ats}:{slug}].")
    sources.append({"name": name, "type": "jobhive", "ats": ats, "slug": slug})
    _write_sources(path, sources)
    print(f"added {name}  [{ats}:{slug}]. {len(sources)} source(s) total.")


def cmd_remove(path: str, key: str) -> None:
    sources = load_sources(path, missing_ok=True)
    idx = None
    if key.isdigit():  # remove by the number shown in 'sources'
        i = int(key) - 1
        if 0 <= i < len(sources):
            idx = i
    if idx is None:  # else remove by exact name
        idx = next((i for i, s in enumerate(sources) if s.get("name") == key), None)
    if idx is None:
        sys.exit(f"no source matching {key!r}. run 'sources' to see the list.")
    removed = sources.pop(idx)
    _write_sources(path, sources)
    print(f"removed {removed.get('name', '?')}. {len(sources)} source(s) left.")


def cmd_test(ats: str, slug: str) -> None:
    """fetch one company by ats+slug without adding it or touching the db."""
    cfg = {"name": f"{ats}:{slug}", "type": "jobhive", "ats": ats, "slug": slug}
    try:
        jobs = fetch_jobhive(cfg)
    except Exception as exc:
        print(f"  {ats}:{slug} - failed: {type(exc).__name__}: {exc}")
        return
    print(f"  {ats}:{slug} - {len(jobs)} posting(s).")
    for j in jobs[:5]:
        print(f"    - {j.title}")
    if len(jobs) > 5:
        print(f"    ... and {len(jobs) - 5} more")
    if jobs:
        print(f'\n  works. add it with:  python job_watcher.py add "NAME" {ats} {slug}')
    else:
        print("\n  0 postings. platform/slug may be wrong, or no open roles right now.")


def cmd_platforms() -> None:
    """print the ats names jobhive currently supports (fetched live)."""
    import urllib.request
    url = "https://storage.stapply.ai/jobhive/v1/manifest.json"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as exc:
        sys.exit(f"couldn't fetch platform list: {exc}")
    names = sorted((data.get("by_ats") or data).keys())
    for n in names:
        print(f"  {n}")
    print(f"\n  {len(names)} platforms. use the name as the \"ats\" when adding a company.")


def cmd_sources(path: str) -> None:
    sources = load_sources(path, missing_ok=True)
    if not sources:
        print('  no sources yet. add one:  python job_watcher.py add "Name" <ats> <slug>')
        return
    for i, src in enumerate(sources, 1):
        print(f"  {i:>3}  {src.get('name', '?'):<28} [{src.get('ats', '?')}:{src.get('slug', '?')}]")
    print(f"\n  {len(sources)} source(s).")


SAMPLE = [
    {"_comment": "type is always 'jobhive'. Set 'ats' to the platform (run 'python job_watcher.py platforms' for the full list) and 'slug' to the company on that ATS.",
     "name": "Example", "type": "jobhive", "ats": "greenhouse", "slug": "examplecompany"},
    {"name": "Company 1", "type": "jobhive", "ats": "workday", "slug": "company1"},
    {"name": "Company 2", "type": "jobhive", "ats": "phenom", "slug": "company2"},
    {"name": "Company 3", "type": "jobhive", "ats": "successfactors", "slug": "company3"},
]


def cmd_init(path: str) -> None:
    p = Path(path)
    if p.exists():
        sys.exit(f"{path!r} already exists; not overwriting.")
    p.write_text(json.dumps(SAMPLE, indent=2) + "\n", encoding="utf-8")
    print(f"wrote sample {path}. edit it, then: python job_watcher.py run")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="show job postings you haven't seen yet.")
    p.add_argument("--db", default="jobs.db", help="SQLite file (default: jobs.db)")
    p.add_argument("--config", default="sources.json", help="sources file (default: sources.json)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="write a sample sources.json")

    pr = sub.add_parser("run", help="poll all sources once")
    pr.add_argument("--delay", type=float, default=8.0, help="seconds between sources")
    pr.add_argument("--title", help="only keep postings whose title contains any of these (comma-separated)")

    pw = sub.add_parser("watch", help="poll on a loop")
    pw.add_argument("-i", "--interval", type=int, default=3600, help="seconds between polls")
    pw.add_argument("--delay", type=float, default=8.0, help="seconds between sources")
    pw.add_argument("--title", help="only keep postings whose title contains any of these (comma-separated)")

    sub.add_parser("list", help="show unseen postings")
    sub.add_parser("review", help="step through unseen postings, one key to dismiss")

    ps = sub.add_parser("seen", help="dismiss posting(s) by id / range / all")
    ps.add_argument("ids", nargs="+", help="IDs, ranges like 5-25, or 'all'")

    pa = sub.add_parser("add", help="add a company to sources.json")
    pa.add_argument("name", help='display name, e.g. "Acme Co"')
    pa.add_argument("ats", help="platform, e.g. greenhouse (see: platforms command)")
    pa.add_argument("slug", help="company slug on that ATS")

    prm = sub.add_parser("remove", help="remove a source by name or number")
    prm.add_argument("key", help="the name, or the number shown by 'sources'")

    sub.add_parser("sources", help="list configured sources")
    pt = sub.add_parser("test", help="fetch one company by ats+slug without adding it")
    pt.add_argument("ats", help="platform, e.g. greenhouse")
    pt.add_argument("slug", help="company slug on that ATS")
    sub.add_parser("platforms", help="list ATS platforms jobhive supports")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "init":
        cmd_init(args.config)
        return 0
    if args.cmd == "add":
        cmd_add(args.config, args.name, args.ats, args.slug)
        return 0
    if args.cmd == "remove":
        cmd_remove(args.config, args.key)
        return 0
    if args.cmd == "sources":
        cmd_sources(args.config)
        return 0
    if args.cmd == "platforms":
        cmd_platforms()
        return 0
    if args.cmd == "test":
        cmd_test(args.ats, args.slug)
        return 0

    con = connect(args.db)
    title_filter = None
    if getattr(args, "title", None):
        title_filter = [w.strip().lower() for w in args.title.split(",") if w.strip()]
    if args.cmd == "run":
        cmd_run(con, load_sources(args.config), args.delay, title_filter)
    elif args.cmd == "watch":
        cmd_watch(con, args.config, args.interval, args.delay, title_filter)
    elif args.cmd == "list":
        print_unseen(unseen(con))
    elif args.cmd == "review":
        cmd_review(con)
    elif args.cmd == "seen":
        cmd_seen(con, args.ids)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
