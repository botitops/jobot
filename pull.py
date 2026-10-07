#!/usr/bin/env python3
"""Jobot pull: baja TODOS los avisos de los portales, los normaliza y deduplica.

Cero LLM y cero filtros de fit: lo unico que se descarta es lo que ya se vio
(por URL o por titulo+empresa). El match contra el perfil lo hace el
agente despues (modelo barato por titulo, modelo mejor por detalle).

Salida (data/):
  seen.json              estado de dedupe {clave: fecha_primera_vez}
  new/<run>.jsonl        lote de avisos nuevos de ESA corrida (run = AAAAMMDD-HHMM UTC), con descripcion si la fuente la da
  new/<run>.tsv          una linea por aviso: id, titulo, empresa, ubicacion, fuente, fecha
  runs.jsonl             una linea por corrida: run, crudos, nuevos, detalle por fuente (para el embudo)
  report.md              por fuente: crudos / nuevos / error (una fuente en 0 = algo se rompio)
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
NEWDIR = os.path.join(DATA, "new")
SEEN_PATH = os.path.join(DATA, "seen.json")
NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")
FC_KEY = os.environ.get("FIRECRAWL_API_KEY", "")
FC_DAILY_MAX = int(os.environ.get("FC_DAILY_MAX", "0"))  # ~900 creditos/mes
FC_USED = 0
MAXDESC = 3000
KEEP_SEEN_DAYS = 90
KEEP_NEW_DAYS = 14

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en,es;q=0.8",
})

# Roles del perfil (slugs). Se usan en portales con listado por rol.
ROLES = [
    "account-manager", "customer-success-manager", "client-success-manager",
    "community-manager", "project-manager", "operations-manager",
    "partnerships-manager", "events-manager", "content-writer",
]


# ----------------------------------------------------------------- utilidades
def get(url, **kw):
    r = None
    for i in range(3):
        try:
            r = S.get(url, timeout=30, **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429, 503):
                time.sleep(2 * (i + 1))
                continue
            return r
        except requests.RequestException:
            time.sleep(2 * (i + 1))
    return r


def must(url):
    r = get(url)
    if r is None or r.status_code != 200:
        raise RuntimeError(f"HTTP {getattr(r, 'status_code', 'sin respuesta')} en {url}")
    return r


def jget(url):
    return must(url).json()


def clean(t, n=MAXDESC):
    t = re.sub(r"<[^>]+>", " ", str(t or ""))
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()[:n]


def J(source, title, url, company="", location="", posted="", desc="", salary=""):
    title, url = clean(title, 200), (url or "").strip()
    if not title or not url.startswith("http"):
        return None
    return dict(source=source, title=title, company=clean(company, 100), location=clean(location, 200),
                url=url, posted_at=str(posted or ""), salary=clean(salary, 60), description=clean(desc))


def norm_url(u):
    p = urlparse(u.strip())
    q = [(k, v) for k, v in parse_qsl(p.query) if not re.match(r"(utm_|ref$|source$|fbclid|gclid)", k)]
    return urlunparse((p.scheme, p.netloc.lower(), p.path.rstrip("/"), "", urlencode(q), ""))


def k_url(j):
    return "u:" + hashlib.sha1(norm_url(j["url"]).encode()).hexdigest()[:16]


def k_tc(j):
    if not j["company"]:
        return None
    return "t:" + hashlib.sha1(f'{j["title"].lower()}|{j["company"].lower()}'.encode()).hexdigest()[:16]


# ------------------------------------------------------------------ fuentes API
def remoteok(is_seen):
    d = jget("https://remoteok.com/api")
    return [J("remoteok", j.get("position"), j.get("url") or j.get("apply_url"), j.get("company"),
              j.get("location") or "Worldwide", j.get("date"), j.get("description"),
              f'{j.get("salary_min") or ""}-{j.get("salary_max") or ""}'.strip("-"))
            for j in d if isinstance(j, dict) and j.get("position")]


def workingnomads(is_seen):
    d = jget("https://www.workingnomads.com/api/exposed_jobs/")
    d = d if isinstance(d, list) else d.get("jobs", [])
    return [J("workingnomads", j.get("title"), j.get("url"), j.get("company_name"), j.get("location"),
              j.get("pub_date"), j.get("description")) for j in d]


def himalayas(is_seen):
    out = []
    for off in range(0, 3000, 100):
        jobs = (jget(f"https://himalayas.app/jobs/api?limit=100&offset={off}") or {}).get("jobs") or []
        if not jobs:
            break
        page = [J("himalayas", j.get("title"), j.get("applicationLink") or j.get("guid"), j.get("companyName"),
                  ", ".join(j.get("locationRestrictions") or []) or "Worldwide", j.get("pubDate"),
                  j.get("description") or j.get("excerpt"),
                  f'{j.get("minSalary") or ""}-{j.get("maxSalary") or ""} {j.get("currency") or ""}'.strip("- "))
                for j in jobs]
        page = [x for x in page if x]
        out += page
        if page and all(is_seen(x) for x in page):  # pagina entera ya vista: cortar
            break
    return out


def remotive(is_seen):
    d = jget("https://remotive.com/api/remote-jobs")
    return [J("remotive", j.get("title"), j.get("url"), j.get("company_name"), j.get("candidate_required_location"),
              j.get("publication_date"), j.get("description"), j.get("salary")) for j in d.get("jobs", [])]


def jobicy(is_seen):
    d = jget("https://jobicy.com/api/v2/remote-jobs?count=100")
    return [J("jobicy", j.get("jobTitle"), j.get("url"), j.get("companyName"), j.get("jobGeo"),
              j.get("pubDate"), j.get("jobExcerpt")) for j in d.get("jobs", [])]


def arbeitnow(is_seen):  # solo remotos (campo estructurado de la API)
    out = []
    for page in range(1, 6):
        d = jget(f"https://www.arbeitnow.com/api/job-board-api?page={page}").get("data") or []
        if not d:
            break
        out += [J("arbeitnow", j.get("title"), j.get("url"), j.get("company_name"), j.get("location") or "Remote",
                  j.get("created_at"), j.get("description")) for j in d if j.get("remote")]
    return out


def rss(source, url, split_company=False):
    root = ET.fromstring(must(url).content)
    out = []
    for it in root.iter("item"):
        title, company = (it.findtext("title") or ""), ""
        if split_company and ": " in title:
            company, title = title.split(": ", 1)
        out.append(J(source, title, it.findtext("link"), company, it.findtext("region") or "",
                     it.findtext("pubDate"), it.findtext("description")))
    return out


WWR_FEEDS = ["remote-jobs", "categories/remote-customer-support-jobs", "categories/remote-management-and-finance-jobs",
             "categories/remote-sales-and-marketing-jobs", "categories/remote-product-jobs",
             "categories/remote-all-other-jobs"]


def jobspresso(is_seen):  # WP Job Manager expone RSS de avisos
    return rss("jobspresso", "https://jobspresso.co/?feed=job_feed")


def weworkremotely(is_seen):
    out, errs = [], []
    for f in WWR_FEEDS:
        try:
            out += rss("weworkremotely", f"https://weworkremotely.com/{f}.rss", split_company=True)
        except Exception as e:
            errs.append(repr(e)[:80])
    if not out:
        raise RuntimeError("; ".join(errs))
    return out


# ------------------------------------------------------------ fuentes HTML (links)
ANCHOR = re.compile(r'<a\s[^>]*?href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.I | re.S)
GENERIC = re.compile(r"^(apply|view|read more|see|details|learn more|new|featured|remote|save|share)\b", re.I)
NAV = re.compile(r"/(blog|about|login|signup|sign-up|pricing|category|categories|tag|tags|page|companies|company-profile|"
                 r"privacy|terms|contact|jobs/?$|remote-jobs/?$)", re.I)


def fc_links(url):
    """Fallback para paginas con JS / anti-bot: 1 credito de Firecrawl, devuelve solo links."""
    global FC_USED
    if not FC_KEY or FC_USED >= FC_DAILY_MAX:
        return []
    FC_USED += 1
    try:
        r = requests.post("https://api.firecrawl.dev/v1/scrape",
                          headers={"Authorization": f"Bearer {FC_KEY}"},
                          json={"url": url, "formats": ["links"], "waitFor": 3000}, timeout=90)
        return [(u, "") for u in (r.json().get("data") or {}).get("links", [])]
    except Exception:
        return []


def page_links(url, link_re):
    r = get(url)
    pairs = []
    if r is not None and r.status_code == 200:
        best = {}
        for href, inner in ANCHOR.findall(r.text):
            u, t = urljoin(url, html.unescape(href)), clean(inner, 200)
            if len(t) > len(best.get(u, "")):
                best[u] = t
        pairs = list(best.items())
    pick = lambda ps: pick_jobs(ps, url, link_re)  # noqa: E731
    got = pick(pairs)
    if len(got) < 3:  # sospechoso (JS o bloqueo): reintentar con Firecrawl
        fc = fc_links(url)
        if fc:
            pairs = fc
        got = pick(fc) or got
    return got, (r.status_code if r is not None else None), pairs


def pick_jobs(pairs, page_url, link_re):
    host, ppath = urlparse(page_url).netloc, urlparse(page_url).path.rstrip("/")
    out = []
    for u, t in pairs:
        p = urlparse(u)
        if link_re:
            if not re.search(link_re, u):
                continue
        else:  # generico: mismo sitio, ruta profunda, ultimo tramo tipo slug
            seg = p.path.rstrip("/").split("/")[-1]
            if p.netloc != host or p.path.rstrip("/") == ppath or p.path.count("/") < 2 or NAV.search(p.path) \
                    or "-" not in seg or len(seg) < 12:
                continue
        out.append((u, t))
    return out


def html_source(name, pages, link_re=None):
    def fn(is_seen):
        found, errs, dbg = {}, [], []
        for page in pages:
            got, status, pairs = page_links(page, link_re)
            if not got:
                errs.append(f"{status or 'sin respuesta'} {page}")
                dbg.append(f"## {page} status={status} anchors={len(pairs)}")
                dbg += [f"{u}\t{t[:80]}" for u, t in pairs[:80]]
            for u, t in got:
                slug = urlparse(u).path.rstrip("/").split("/")[-1].replace("-", " ")
                title = t if len(t) >= 5 and not GENERIC.match(t) else slug
                if u not in found or len(title) > len(found[u]):
                    found[u] = title
        if not found:
            os.makedirs(os.path.join(DATA, "debug"), exist_ok=True)
            open(os.path.join(DATA, "debug", f"{name}.txt"), "w").write("\n".join(dbg))
            raise RuntimeError("0 avisos; " + " | ".join(errs[:3]))
        return [J(name, t, u) for u, t in found.items()]
    return fn


SOURCES = {
    # APIs / RSS (confiables, con descripcion y fecha)
    "remoteok": remoteok,
    "workingnomads": workingnomads,
    "himalayas": himalayas,
    "remotive": remotive,
    "jobicy": jobicy,
    "arbeitnow": arbeitnow,
    "weworkremotely": weworkremotely,
    # HTML por links (los patrones de URL se afinan mirando report.md tras las primeras corridas)
    "remoterocketship": html_source(
        "remoterocketship",
        [f"https://www.remoterocketship.com/jobs/{r}/" for r in ROLES]
        + [f"https://www.remoterocketship.com/country/europe/jobs/{r}/" for r in ROLES],
        r"/company/[^/]+/jobs/[^/?#]+"),
    "jobgether": html_source("jobgether", [f"https://jobgether.com/remote-jobs/emea/{r}" for r in ROLES], r"/offer/"),
    "dailyremote": html_source(
        "dailyremote",
        [f"https://dailyremote.com/remote-{c}-jobs" for c in
         ("community-manager", "account-manager", "customer-success", "project-manager", "operations",
          "content-writer", "copywriter", "partnerships", "events")], r"/remote-job/"),
    "nodesk": html_source("nodesk", ["https://nodesk.co/remote-jobs/", "https://nodesk.co/remote-jobs/europe/"]),
    "euremotejobs": html_source("euremotejobs", ["https://euremotejobs.com/"], r"/job/"),
    "4dayweek": html_source("4dayweek", ["https://4dayweek.io/remote-jobs"], r"/(remote-)?job/"),
    "jobspresso": jobspresso,
    "remotifyeurope": html_source("remotifyeurope", ["https://remotifyeurope.com/"]),
    "topcsjobs": html_source("topcsjobs", ["https://topcsjobs.com/"]),
    "remotefirstjobs": html_source("remotefirstjobs", ["https://remotefirstjobs.com/"]),
    "trulyremotework": html_source("trulyremotework", ["https://trulyremotework.com/"]),
    "startupjobs": html_source("startupjobs", ["https://startup.jobs/?remote=true"]),
    "dynamitejobs": html_source("dynamitejobs", ["https://dynamitejobs.com/remote-jobs"]),
}


# ------------------------------------------------------------------------ main
def load_seen():
    try:
        return json.load(open(SEEN_PATH))
    except (OSError, ValueError):
        return {}


def main():
    os.makedirs(NEWDIR, exist_ok=True)
    seen = load_seen()
    batch, new, report = set(), [], {}
    is_seen = lambda j: k_url(j) in seen  # noqa: E731

    for name, fn in SOURCES.items():
        try:
            items = [j for j in fn(is_seen) if j]
            err = ""
        except Exception as e:  # una fuente rota no frena a las demas
            items, err = [], repr(e)[:200]
        n_new = 0
        for j in items:
            keys = [k for k in (k_url(j), k_tc(j)) if k]
            if any(k in seen or k in batch for k in keys):
                continue
            batch.update(keys)
            j["id"] = keys[0][2:]
            j["first_seen"] = TODAY
            new.append(j)
            n_new += 1
        report[name] = (len(items), n_new, err)
        print(f"{name}: crudos={len(items)} nuevos={n_new} {err}", flush=True)

    # persistir estado (primera vez = hoy; podar viejos)
    cutoff = (NOW - timedelta(days=KEEP_SEEN_DAYS)).strftime("%Y-%m-%d")
    for k in batch:
        seen[k] = TODAY
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    json.dump(seen, open(SEEN_PATH, "w"), separators=(",", ":"))

    run = NOW.strftime("%Y%m%d-%H%M")
    if new:  # un lote por corrida: nunca se pisa ni se solapa con otro
        with open(os.path.join(NEWDIR, f"{run}.jsonl"), "w") as f:
            for j in new:
                f.write(json.dumps(j, ensure_ascii=False) + "\n")
        with open(os.path.join(NEWDIR, f"{run}.tsv"), "w") as f:
            for j in new:
                row = [j["id"], j["title"], j["company"], j["location"], j["source"], j["posted_at"]]
                f.write("\t".join(re.sub(r"[\t\r\n]+", " ", c) for c in row) + "\n")
    cutoff_new = (NOW - timedelta(days=KEEP_NEW_DAYS)).strftime("%Y%m%d")
    for fn_ in os.listdir(NEWDIR):  # podar lotes viejos
        if fn_[:8].isdigit() and fn_[:8] < cutoff_new:
            os.remove(os.path.join(NEWDIR, fn_))

    total_raw = sum(v[0] for v in report.values())
    with open(os.path.join(DATA, "runs.jsonl"), "a") as f:
        f.write(json.dumps({"run": run, "raw": total_raw, "new": len(new), "fc_credits": FC_USED,
                            "sources": {k: list(v) for k, v in report.items()}}, ensure_ascii=False) + "\n")
    lines = [f"# Pull {run} UTC", "", f"Crudos: {total_raw} | Nuevos: {len(new)} | Creditos Firecrawl: {FC_USED}", "",
             "| fuente | crudos | nuevos | error |", "|---|---|---|---|"]
    for name, (raw, n, err) in report.items():
        flag = " ⚠️" if raw == 0 else ""
        lines.append(f"| {name}{flag} | {raw} | {n} | {err} |")
    open(os.path.join(DATA, "report.md"), "w").write("\n".join(lines) + "\n")
    for name, (raw, _, err) in report.items():
        if raw == 0:
            print(f"::warning::{name} devolvio 0 avisos {err}")
    return 0 if total_raw else 1


if __name__ == "__main__":
    sys.exit(main())
# run con Firecrawl 2026-10-07
