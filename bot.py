"""
Bioeconomia Bot — PubMed → Bluesky
Busca papers recentes sobre bioeconomia no PubMed e posta no Bluesky com card de preview.
"""

import os
import json
import time
import random
import requests
import re
from datetime import datetime, timedelta
from pathlib import Path
from atproto import Client, models

# ── Configurações ──────────────────────────────────────────────────────────────

BLUESKY_HANDLE = "bioeconomia.bsky.social"

# Termos amazônicos têm prioridade, globais complementam
SEARCH_TERMS_AMAZON = [
    "Amazon bioeconomy[Title/Abstract]",
    "bioeconomy Brazil[Title/Abstract]",
    "Amazonian biodiversity[Title/Abstract]",
    "sociobioeconomy[Title/Abstract]",
    "socio-bioeconomy[Title/Abstract]",
    "non-timber forest products Amazon[Title/Abstract]",
    "agroforestry Amazon[Title/Abstract]",
    "ecosystem services Amazon[Title/Abstract]",
]

SEARCH_TERMS_GLOBAL = [
    "bioeconomy[Title/Abstract]",
    "circular bioeconomy[Title/Abstract]",
    "biobased economy[Title/Abstract]",
    "biorefineries[Title/Abstract]",
    "biodiversity valuation[Title/Abstract]",
    "bioeconomy policy[Title/Abstract]",
    "forest-based economy[Title/Abstract]",
]

HASHTAGS = "#Bioeconomy #Amazon #Sustainability #CircularEconomy #OneHealth"

POSTS_PER_RUN = 2
DAYS_LOOKBACK = 3
POSTED_FILE   = "posted_ids.json"

# ── Autenticação Bluesky ───────────────────────────────────────────────────────

def get_bluesky_client():
    client = Client()
    client.login(BLUESKY_HANDLE, os.environ["BSKY_APP_PASSWORD"])
    return client

# ── PubMed ─────────────────────────────────────────────────────────────────────

PUBMED_SEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_FETCH  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
PUBMED_SUMM   = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

def search_pubmed(terms, days_back=3, max_results=20):
    date_from = (datetime.now() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    query = " OR ".join(terms)
    query += " AND (English[lang] OR Portuguese[lang])"
    query += f" AND {date_from}[PDAT]:3000[PDAT]"

    params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "pub+date",
        "tool": "bioeconomia_bluesky_bot",
        "email": os.environ.get("CONTACT_EMAIL", "bot@example.com"),
    }

    r = requests.get(PUBMED_SEARCH, params=params, timeout=15)
    r.raise_for_status()
    ids = r.json().get("esearchresult", {}).get("idlist", [])
    return ids

def fetch_paper_summary(pmid):
    params = {
        "db": "pubmed",
        "id": pmid,
        "retmode": "json",
        "tool": "bioeconomia_bluesky_bot",
        "email": os.environ.get("CONTACT_EMAIL", "bot@example.com"),
    }
    r = requests.get(PUBMED_SUMM, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("result", {}).get(pmid, {})

def get_doi_abstract_and_url(pmid):
    """Extrai DOI, abstract e tenta obter URL direta do periódico."""
    params = {
        "db": "pubmed",
        "id": pmid,
        "rettype": "xml",
        "retmode": "xml",
        "tool": "bioeconomia_bluesky_bot",
        "email": os.environ.get("CONTACT_EMAIL", "bot@example.com"),
    }
    r = requests.get(PUBMED_FETCH, params=params, timeout=15)
    r.raise_for_status()

    # DOI
    doi_match = re.search(r'<ArticleId IdType="doi">(.*?)</ArticleId>', r.text)
    doi = doi_match.group(1).strip() if doi_match else None

    # Abstract
    abstract_match = re.search(r'<AbstractText[^>]*>(.*?)</AbstractText>', r.text, re.DOTALL)
    abstract = re.sub(r'<[^>]+>', '', abstract_match.group(1)).strip() if abstract_match else ""
    if len(abstract) > 200:
        abstract = abstract[:197] + "..."

    # URL direta: segue o redirecionamento do DOI para obter link do periódico
    direct_url = None
    if doi:
        try:
            doi_url = f"https://doi.org/{doi}"
            resp = requests.get(doi_url, timeout=10, allow_redirects=True)
            direct_url = resp.url
            # Se caiu em doi.org ou unpaywall, usa o DOI mesmo
            if "doi.org" in direct_url:
                direct_url = None
        except Exception:
            direct_url = None

    # URL final: direta se conseguiu, senão DOI, senão PubMed
    if direct_url:
        link = direct_url
    elif doi:
        link = f"https://doi.org/{doi}"
    else:
        link = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"

    return doi, abstract, link

# ── Controle de IDs já postados ────────────────────────────────────────────────

def load_posted():
    if Path(POSTED_FILE).exists():
        with open(POSTED_FILE) as f:
            return set(json.load(f))
    return set()

def save_posted(posted: set):
    with open(POSTED_FILE, "w") as f:
        json.dump(list(posted), f)

# ── Postagem com card embed ────────────────────────────────────────────────────

def post_with_card(client, title, journal, abstract, link):
    """Posta no Bluesky com card de preview usando link direto do periódico."""

    text = f"{title}\n\n📰 {journal}\n\n{HASHTAGS}"

    if len(text) > 300:
        max_title = 300 - len(f"\n\n📰 {journal}\n\n{HASHTAGS}") - 1
        text = f"{title[:max_title]}…\n\n📰 {journal}\n\n{HASHTAGS}"

    card_description = abstract if abstract else f"Published in {journal}"

    embed = models.AppBskyEmbedExternal.Main(
        external=models.AppBskyEmbedExternal.External(
            uri=link,
            title=title[:300],
            description=card_description,
        )
    )

    client.send_post(text=text, embed=embed)
    print(f"[Bot] Postado: {link}")

# ── Loop principal ─────────────────────────────────────────────────────────────

def run():
    print(f"[Bot] Iniciando — {datetime.now().isoformat()}")

    posted = load_posted()

    # Busca primeiro amazônico, depois global para complementar
    amazon_ids = search_pubmed(SEARCH_TERMS_AMAZON, days_back=DAYS_LOOKBACK)
    global_ids = search_pubmed(SEARCH_TERMS_GLOBAL, days_back=DAYS_LOOKBACK)

    # Amazônico tem prioridade, global complementa sem repetir
    all_ids = list(dict.fromkeys(amazon_ids + global_ids))
    new_pmids = [p for p in all_ids if p not in posted]

    print(f"[Bot] {len(amazon_ids)} amazônicos + {len(global_ids)} globais = {len(new_pmids)} novos.")

    if not new_pmids:
        print("[Bot] Nada novo. Encerrando.")
        return

    # Prioriza amazônicos na seleção
    amazon_new = [p for p in new_pmids if p in amazon_ids]
    global_new = [p for p in new_pmids if p not in amazon_ids]

    priority = amazon_new + global_new
    to_post = priority[:POSTS_PER_RUN]

    client = get_bluesky_client()

    for pmid in to_post:
        try:
            summary = fetch_paper_summary(pmid)
            title   = summary.get("title", "").rstrip(".")
            journal = summary.get("fulljournalname", summary.get("source", ""))

            if not title:
                print(f"[Bot] PMID {pmid} sem título, pulando.")
                continue

            doi, abstract, link = get_doi_abstract_and_url(pmid)
            post_with_card(client, title, journal, abstract, link)

            posted.add(pmid)
            save_posted(posted)

            time.sleep(random.randint(60, 180))

        except Exception as e:
            print(f"[Erro] PMID {pmid}: {e}")

    print(f"[Bot] Concluído. {len(to_post)} posts enviados.")

if __name__ == "__main__":
    run()
