"""Agent d'alertes emploi : Investment Analyst / Associate en infrastructure -> Telegram.

Sources :
  - LinkedIn (recherche publique, sans compte)
  - Adzuna   (API officielle, gratuite ; optionnel : ADZUNA_APP_ID / ADZUNA_APP_KEY)
  - Jooble   (API officielle, gratuite ; optionnel : JOOBLE_API_KEY)

Variables d'environnement requises : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Usage :
  python job_alert.py            # recherche + envoi Telegram
  python job_alert.py --dry-run  # affiche les offres sans rien envoyer ni enregistrer
  python job_alert.py --test     # envoie un message de test sur Telegram
"""

from __future__ import annotations

import argparse
import html
import json
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

try:  # Utilise le magasin de certificats du système (proxys d'entreprise, etc.)
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "seen_jobs.json"

STATE_RETENTION_DAYS = 30
MAX_MESSAGES_PER_RUN = 25
ERROR_ALERT_INTERVAL = timedelta(hours=24)


def _load_dotenv(path: Path) -> None:
    """Charge un fichier .env local (utile hors GitHub Actions). Les variables déjà définies priment."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
LINKEDIN_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"


@dataclass
class Job:
    source: str
    job_id: str
    title: str
    company: str
    location: str
    url: str
    city: str
    posted: str = ""
    description: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.job_id}"

    @property
    def fingerprint(self) -> str:
        """Clé indépendante de la source, pour éviter les doublons LinkedIn/Jooble."""
        return "fp:" + _normalize(self.title) + "|" + _normalize(self.company)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def fetch_linkedin(session: requests.Session, term: str, loc: dict, max_age_h: int) -> list[Job]:
    jobs: list[Job] = []
    for start in range(0, 50, 10):  # jusqu'à 5 pages de 10 résultats
        params = {
            "keywords": term,
            "location": loc["linkedin"],
            "f_TPR": f"r{max_age_h * 3600}",
            "sortBy": "DD",
            "start": start,
        }
        resp = _get_with_retry(session, LINKEDIN_SEARCH_URL, params=params, headers={"User-Agent": BROWSER_UA})
        cards = BeautifulSoup(resp.text, "html.parser").select("div.base-search-card")
        for card in cards:
            urn = card.get("data-entity-urn", "")
            job_id = urn.rsplit(":", 1)[-1]
            title = _text(card, ".base-search-card__title")
            if not job_id or not title:
                continue
            posted = card.select_one("time")
            jobs.append(Job(
                source="LinkedIn",
                job_id=job_id,
                title=title,
                company=_text(card, ".base-search-card__subtitle"),
                location=_text(card, ".job-search-card__location"),
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                city=loc["name"],
                posted=posted.get("datetime", "") if posted else "",
            ))
        if len(cards) < 10:
            break
        _pause()
    return jobs


def fetch_adzuna(session: requests.Session, term: str, loc: dict, max_age_h: int) -> list[Job]:
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    country = loc.get("adzuna")
    if not (app_id and app_key and country):
        return []
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what_phrase": term,
        "where": loc.get("adzuna_where", loc["name"]),
        "max_days_old": 1,
        "sort_by": "date",
        "results_per_page": 50,
        "content-type": "application/json",
    }
    data = _get_with_retry(session, url, params=params).json()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_h)
    jobs = []
    for r in data.get("results", []):
        created = _parse_iso(r.get("created"))
        if created and created < cutoff:
            continue
        jobs.append(Job(
            source="Adzuna",
            job_id=str(r.get("id")),
            title=_strip_tags(r.get("title", "")),
            company=(r.get("company") or {}).get("display_name", ""),
            location=(r.get("location") or {}).get("display_name", ""),
            url=r.get("redirect_url", ""),
            city=loc["name"],
            posted=r.get("created", ""),
            description=_strip_tags(r.get("description", "")),
        ))
    return jobs


def fetch_jooble(session: requests.Session, term: str, loc: dict, max_age_h: int) -> list[Job]:
    api_key = os.getenv("JOOBLE_API_KEY")
    if not (api_key and loc.get("jooble")):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_h)
    body = {
        "keywords": term,
        "location": loc["jooble"],
        "datecreatedfrom": (cutoff - timedelta(days=1)).strftime("%Y-%m-%d"),
        "page": "1",
    }
    resp = session.post(f"https://jooble.org/api/{api_key}", json=body, timeout=30)
    resp.raise_for_status()
    jobs = []
    for r in resp.json().get("jobs", []):
        updated = _parse_iso(r.get("updated"))
        # Jooble ne fournit souvent que la date (heure = 00:00) : on compare alors au jour près.
        if updated and updated.time() == datetime.min.time():
            if updated.date() < cutoff.date():
                continue
        elif updated and updated < cutoff:
            continue
        jobs.append(Job(
            source=f"Jooble ({r['source']})" if r.get("source") else "Jooble",
            job_id=str(r.get("id")),
            title=_strip_tags(r.get("title", "")),
            company=r.get("company", ""),
            location=r.get("location", ""),
            url=r.get("link", ""),
            city=loc["name"],
            posted=r.get("updated", ""),
            description=_strip_tags(r.get("snippet", "")),
        ))
    return jobs


SOURCES = {"LinkedIn": fetch_linkedin, "Adzuna": fetch_adzuna, "Jooble": fetch_jooble}
SOURCE_KEYS = {"Adzuna": ("ADZUNA_APP_ID", "ADZUNA_APP_KEY"), "Jooble": ("JOOBLE_API_KEY",)}


def enabled_sources() -> dict:
    """Sources utilisables : celles qui ne demandent pas de clé, ou dont les clés sont définies."""
    return {name: fetch for name, fetch in SOURCES.items()
            if all(os.getenv(k) for k in SOURCE_KEYS.get(name, ()))}


class DescriptionFetcher:
    """Récupère à la demande le texte complet d'une offre LinkedIn (quand l'intitulé ne suffit pas)."""

    def __init__(self, session: requests.Session, limit: int):
        self.session, self.limit = session, limit
        self.count, self.blocked = 0, False

    def __call__(self, job: Job) -> str | None:
        """Renvoie la description, ou None si elle est momentanément indisponible."""
        if job.source != "LinkedIn":
            return job.description
        if self.blocked or self.count >= self.limit:
            return None
        self.count += 1
        _pause()
        try:
            resp = self.session.get(LINKEDIN_POSTING_URL.format(job_id=job.job_id),
                                    headers={"User-Agent": BROWSER_UA}, timeout=30)
        except requests.RequestException as exc:
            log(f"  Description indisponible ({job.job_id}) : {exc}")
            return None
        if resp.status_code == 404:  # offre retirée entre-temps
            return ""
        if not resp.ok:
            log(f"  Description indisponible ({job.job_id}) : HTTP {resp.status_code}")
            self.blocked = resp.status_code in (403, 429)
            return None
        return _text(BeautifulSoup(resp.text, "html.parser"), ".show-more-less-html__markup")


def _get_with_retry(session, url, *, params=None, headers=None, attempts=3) -> requests.Response:
    for attempt in range(1, attempts + 1):
        resp = session.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < attempts:
                wait = 10 * attempt
                log(f"  HTTP {resp.status_code} sur {url.split('?')[0]}, nouvel essai dans {wait}s")
                time.sleep(wait)
                continue
        resp.raise_for_status()
        return resp
    raise RuntimeError("unreachable")


def _pause() -> None:
    """Pause aléatoire entre deux requêtes LinkedIn, pour ne pas être bloqué."""
    time.sleep(random.uniform(2.5, 5))


def _text(node, selector: str) -> str:
    el = node.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def _strip_tags(text: str) -> str:
    return BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))  # max 6 décimales
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Filtrage
# ---------------------------------------------------------------------------

class InfraFilter:
    """Décide si une offre est un poste d'Investment Analyst / Associate en infrastructure.

    Règles, dans l'ordre (voir config.yaml > job_filter) :
      1. l'intitulé doit contenir un rôle (analyst, associate…) et aucun terme exclu ;
      2. accepté d'office si l'intitulé est sans ambiguïté ("Project Finance Analyst",
         "Infrastructure Investment Associate"…) ou si l'employeur est un spécialiste de l'infrastructure ;
      3. sinon, si l'intitulé est partiellement pertinent ("Associate, Infrastructure",
         "Private Equity Associate", "Associate"…), on lit la description de l'offre pour trancher.
    """

    BARE_ROLE = re.compile(r"^\W*((senior|junior|sr|jr)\W+)?(analyst|associate|analyste)(\W+(i{1,3}|[1-3]))?\W*$", re.I)
    COMPANY_INFRA = re.compile(r"\binfra")

    def __init__(self, cfg: dict):
        def compile_all(key: str) -> list[re.Pattern]:
            return [re.compile(p, re.I) for p in cfg.get(key, [])]

        self.role, self.exclude = compile_all("role"), compile_all("exclude")
        self.infra_strong, self.infra = compile_all("infra_strong"), compile_all("infra")
        self.context = compile_all("investment_context")
        self.desc_infra, self.desc_investment = compile_all("description_infra"), compile_all("description_investment")
        self.specialists = [re.compile(rf"\b{re.escape(_normalize(n))}\b") for n in cfg.get("infra_specialists", [])]
        self.min_mentions = int(cfg.get("min_description_mentions", 2))

    def is_specialist(self, company: str) -> bool:
        name = _normalize(company)
        return bool(name) and (bool(self.COMPANY_INFRA.search(name)) or any(p.search(name) for p in self.specialists))

    def evaluate(self, job: Job, get_description) -> tuple[bool | None, str]:
        """(verdict, raison). verdict = None si la description était nécessaire mais indisponible."""
        title = job.title
        if not _any(self.role, title):
            return False, "pas un poste d'analyst/associate"
        if _any(self.exclude, title):
            return False, "intitulé exclu"
        infra_title, context_title = _any(self.infra, title), _any(self.context, title)
        if _any(self.infra_strong, title) or (infra_title and context_title):
            return True, "intitulé infrastructure"
        if self.is_specialist(job.company):
            return True, "employeur spécialiste de l'infrastructure"
        bare = bool(self.BARE_ROLE.match(title))
        if not (infra_title or context_title or bare):
            return False, "intitulé hors investissement / infrastructure"

        desc = job.description or get_description(job)
        if desc is None:
            return None, "description indisponible"
        # Les extraits Adzuna / Jooble sont courts : une seule mention suffit.
        needed = self.min_mentions if len(desc) > 1000 else 1
        infra_ok = _count(self.desc_infra, desc) >= needed
        invest_ok = _count(self.desc_investment, desc) >= needed
        if infra_title:
            return invest_ok, "description " + ("orientée investissement" if invest_ok else "sans dimension investissement")
        if context_title:
            return infra_ok, "description " + ("orientée infrastructure" if infra_ok else "sans infrastructure")
        return infra_ok and invest_ok, "description " + ("infra + investissement" if infra_ok and invest_ok
                                                         else "insuffisante")


def _any(patterns: list[re.Pattern], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _count(patterns: list[re.Pattern], text: str) -> int:
    return sum(len(p.findall(text)) for p in patterns)


# ---------------------------------------------------------------------------
# État, Telegram
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"seen": {}, "last_error_alert": None}


def save_state(state: dict) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)
    state["seen"] = {k: v for k, v in state["seen"].items() if _parse_iso(v) and _parse_iso(v) >= cutoff}
    STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def format_job(job: Job) -> str:
    e = html.escape
    lines = [
        f"🏗️ <b>{e(job.title)}</b>",
        f"🏢 {e(job.company or 'Entreprise non précisée')}",
        f"📍 {e(job.location or job.city)}",
        f"🌐 {e(job.source)}" + (f" · publié le {e(job.posted[:10])}" if job.posted else ""),
        f'🔗 <a href="{e(job.url, quote=True)}">Voir l\'offre</a>',
    ]
    return "\n".join(lines)


def _digest_chunks(jobs: list[Job], limit: int = 3800) -> list[str]:
    """Liste compacte d'offres, découpée pour respecter la limite de 4096 caractères de Telegram."""
    e = html.escape
    chunks, current = [], f"➕ <b>{len(jobs)} autres nouvelles offres</b>"
    for job in jobs:
        line = f'\n• <a href="{e(job.url, quote=True)}">{e(job.title)}</a> — {e(job.company)} ({e(job.city)})'
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if jobs:
        chunks.append(current)
    return chunks


def send_telegram(text: str) -> None:
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    for _ in range(3):
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 429:
            time.sleep(resp.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not resp.ok:
            raise RuntimeError(f"Telegram a refusé le message : {resp.status_code} {resp.text}")
        return
    raise RuntimeError("Telegram : trop de tentatives (429)")


# ---------------------------------------------------------------------------
# Programme principal
# ---------------------------------------------------------------------------

def collect(session: requests.Session, cfg: dict) -> tuple[list[Job], list[str]]:
    """Interroge toutes les sources. Renvoie les offres brutes et la liste des sources bloquées."""
    max_age_h = int(cfg.get("max_age_hours", 3))
    found: dict[str, Job] = {}
    sources = enabled_sources()
    stats = {name: {"ok": 0, "failed": 0} for name in sources}
    blocked: set[str] = set()

    for loc in cfg["locations"]:
        terms = cfg["search_terms"] + loc.get("extra_terms", [])
        for name, fetch in sources.items():
            if name in blocked:
                continue
            # LinkedIn : une seule requête combinée par ville, pour limiter le nombre d'appels.
            queries = [" OR ".join(f'"{t}"' for t in terms)] if name == "LinkedIn" else terms
            for query in queries:
                try:
                    jobs = fetch(session, query, loc, max_age_h)
                    stats[name]["ok"] += 1
                except Exception as exc:  # une source en panne ne bloque pas les autres
                    stats[name]["failed"] += 1
                    log(f"  ERREUR {name} / {loc['name']} / {query}: {exc}")
                    status = exc.response.status_code if isinstance(exc, requests.HTTPError) else None
                    if status in (401, 403, 429):
                        blocked.add(name)  # inutile d'insister pendant ce passage
                        log(f"  {name} désactivé pour ce passage.")
                        break
                    continue
                for j in jobs:
                    found.setdefault(j.key, j)
                if jobs:
                    log(f"{name:8} | {loc['name']:10} | {len(jobs):3} offres | {query[:60]}")
                if name == "LinkedIn":
                    _pause()

    if not any(st["ok"] for st in stats.values()):
        raise RuntimeError("Toutes les sources ont échoué.")
    failing = sorted(n for n, st in stats.items() if (st["failed"] and not st["ok"]) or n in blocked)
    return list(found.values()), failing


def _alert_once(state: dict, now: str, dry_run: bool, text: str) -> None:
    """Envoie un avertissement technique, au maximum une fois par ERROR_ALERT_INTERVAL."""
    log(text)
    last = _parse_iso(state.get("last_error_alert"))
    if dry_run or (last and datetime.now(timezone.utc) - last < ERROR_ALERT_INTERVAL):
        return
    send_telegram(text)
    state["last_error_alert"] = now
    save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="n'envoie rien et n'enregistre pas l'état")
    parser.add_argument("--test", action="store_true", help="envoie un message de test sur Telegram")
    parser.add_argument("--hours", type=int, help="remplace max_age_hours de config.yaml")
    parser.add_argument("--verbose", action="store_true", help="affiche la décision prise pour chaque offre")
    args = parser.parse_args()

    if args.test:
        send_telegram("✅ L'agent d'alertes emploi est bien connecté à Telegram.")
        log("Message de test envoyé.")
        return 0

    if not args.dry_run and not (os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")):
        log("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être définis (ou utilisez --dry-run).")
        return 2

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    if args.hours:
        cfg["max_age_hours"] = args.hours
    state = load_state()
    seen = state["seen"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    session = requests.Session()

    try:
        jobs, failing = collect(session, cfg)
    except RuntimeError as exc:
        log(str(exc))
        _alert_once(state, now, args.dry_run, "⚠️ Agent d'alertes emploi : aucune source n'a répondu "
                    "lors du dernier passage. Consultez les logs si cela persiste.")
        return 1
    if failing:
        _alert_once(state, now, args.dry_run, f"⚠️ Agent d'alertes emploi : source(s) bloquée(s) ou en "
                    f"erreur lors du dernier passage : {', '.join(failing)}. Les autres sources continuent.")

    # Chaque offre n'est évaluée qu'une fois : acceptée ou rejetée, elle est mémorisée.
    job_filter = InfraFilter(cfg.get("job_filter", {}))
    fetcher = DescriptionFetcher(session, int(cfg.get("max_description_fetches", 30)))
    unseen = [j for j in jobs if j.key not in seen]
    decided: list[Job] = []
    accepted: dict[str, Job] = {}
    for job in sorted(unseen, key=lambda j: j.source != "LinkedIn"):
        verdict, reason = job_filter.evaluate(job, fetcher)
        if args.verbose or (args.dry_run and verdict):
            mark = {True: "OK ", False: "-- ", None: "?? "}[verdict]
            log(f"{mark}{job.title[:70]:70} | {job.company[:30]:30} | {reason}")
        if verdict is None:
            continue  # réévaluée au prochain passage
        decided.append(job)
        if verdict and job.fingerprint not in seen:
            accepted.setdefault(job.fingerprint, job)
    new_jobs = list(accepted.values())
    log(f"{len(unseen)} offres non vues, {len(decided)} évaluées "
        f"({fetcher.count} descriptions lues), {len(new_jobs)} retenues.")

    to_send, overflow = new_jobs[:MAX_MESSAGES_PER_RUN], new_jobs[MAX_MESSAGES_PER_RUN:]
    for job in to_send:
        if args.dry_run:
            print("\n" + format_job(job))
            continue
        send_telegram(format_job(job))
        time.sleep(1.1)
    # Au-delà de MAX_MESSAGES_PER_RUN, on regroupe les offres dans des messages récapitulatifs.
    for chunk in _digest_chunks(overflow):
        if args.dry_run:
            print("\n" + chunk)
        else:
            send_telegram(chunk)
            time.sleep(1.1)

    if not args.dry_run:
        for job in decided:
            seen[job.key] = now
        for job in new_jobs:
            seen[job.fingerprint] = now
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
