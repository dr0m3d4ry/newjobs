
job board watchlist and viewed-history tracker. pulls new postings from company
career pages, surfaces only the ones not yet seen, and marks postings viewed.

applications tracked elsewhere; this is just the "what's new" feed.

## how it works

fetching delegated to [jobhive](https://github.com/kalil0321/ats-scrapers).
"seen" tracked via local SQLite database.

## install

```
pip install -r requirements.txt
```

or:

```
pip install "jobhive-py[scrapers]"
```

## setup

add companies the watchlist:

```
python job_watcher.py add "Acme Co" greenhouse acmeco
```

`add <name> <ats> <slug>` creates `sources.json` on first use and appends to it
after. `type` is always `jobhive` (for now). run `python job_watcher.py
platforms` for the supported platforms and exact `ats` strings; `slug` is the
company's identifier on that ATS (for Greenhouse/Lever/Ashby it's the token in
the careers-page URL).

to edit file by hand instead, `python job_watcher.py init` writes a sample
`sources.json`. each entry looks like:

```json
{ "name": "Acme Co", "type": "jobhive", "ats": "greenhouse", "slug": "acmeco" }
```

## usage

manage the watchlist:

```
python job_watcher.py test greenhouse acmeco            # check a company works before adding
python job_watcher.py add "Acme Co" greenhouse acmeco   # add a company
python job_watcher.py sources                           # list configured companies
python job_watcher.py remove "Acme Co"                  # remove by name, or by number
```

check for new postings:

```
python job_watcher.py run             # pull new postings
python job_watcher.py list            # show unseen postings + links
python job_watcher.py review          # step through them, one key to dismiss
python job_watcher.py seen 5-25       # mark viewed by id / range / all
python job_watcher.py watch -i 3600   # poll on a loop (re-reads sources.json each cycle)
```

in `review`: `d` or space dismisses, `o` opens the posting, `s` skips, `b` goes
back, `q` quits.

add `--title "word1,word2"` to `run` or `watch` to keep only postings whose
title contains one of the words.

## notes

- this pulls publicly listed job postings via
  [jobhive](https://github.com/kalil0321/ats-scrapers); respect the terms of the
  sites it points at.

## license

MIT. built on [jobhive](https://github.com/kalil0321/ats-scrapers).

