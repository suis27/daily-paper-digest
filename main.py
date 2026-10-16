import json
import os
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
API_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
DATA_FILE = Path("site/papers.json")
MAX_PER_TOPIC = int(os.getenv("MAX_PER_TOPIC", "25"))

TOPICS = {
    "Protein": [
        "protein engineering", "protein design",
        "protein structure", "proteomics",
        "protein therapeutics"
    ],
    "Nucleic acids": [
        "nucleic acid", "mRNA", "RNA therapeutics",
        "oligonucleotide", "siRNA", "antisense oligonucleotide"
    ],
    "Immunity": [
        "immunity", "immunology", "immune cell",
        "immunotherapy", "inflammation", "immune response"
    ],
    "Drug delivery": [
        "drug delivery", "nanoparticle", "nanomedicine",
        "lipid nanoparticle", "targeted delivery",
        "controlled release"
    ],
}

session = requests.Session()
session.headers["User-Agent"] = "BioPaperDigest/1.0"


def load_existing():
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"기존 논문 데이터를 읽지 못했습니다: {exc}")


def search_topic(topic, terms, start_date, end_date):
    terms_query = " OR ".join(
        f'TITLE_ABS:"{term}"' for term in terms
    )
    query = (
        f"({terms_query}) AND "
        f"FIRST_PDATE:[{start_date} TO {end_date}]"
    )

    response = session.get(
        API_URL,
        params={
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": MAX_PER_TOPIC,
            "sort_date": "y",
        },
        timeout=60,
    )
    response.raise_for_status()

    records = response.json().get("resultList", {}).get("result", [])
    papers = []

    for item in records:
        title = (item.get("title") or "").strip()
        if not title:
            continue

        doi = (item.get("doi") or "").strip()
        pmid = (item.get("pmid") or "").strip()
        pmcid = (item.get("pmcid") or "").strip()
        source = item.get("source", "")
        article_id = item.get("id", "")

        uid = (
            "doi:" + doi.lower() if doi
            else "pmid:" + pmid if pmid
            else f"{source}:{article_id}"
        )

        if pmcid:
            url = "https://europepmc.org/articles/" + pmcid
        elif pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        elif doi:
            url = "https://doi.org/" + doi
        else:
            url = "https://europepmc.org/search?query=" + requests.utils.quote(title)

        abstract = item.get("abstractText", "") or ""
        abstract = re.sub(r"<[^>]+>", " ", abstract)
        abstract = re.sub(r"\s+", " ", abstract).strip()

        papers.append({
            "id": uid,
            "title": title,
            "topic": topic,
            "date": item.get("firstPublicationDate", ""),
            "journal": item.get("journalTitle", ""),
            "authors": item.get("authorString", ""),
            "doi": doi,
            "url": url,
            "abstract": abstract,
            "summary": (
                "초록 기반 요약입니다. 아래 초록을 바탕으로 "
                "연구 목적, 방법, 결과 및 한계를 검토하세요. "
                "초록에 없는 세부사항은 확인할 수 없습니다."
            ),
        })

    return papers


def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(KST)
    end_date = now.date()
    start_date = end_date - timedelta(days=3)

    existing = load_existing()
    by_id = {paper["id"]: paper for paper in existing if paper.get("id")}

    for topic, terms in TOPICS.items():
        try:
            papers = search_topic(
                topic, terms,
                start_date.isoformat(),
                end_date.isoformat(),
            )

            for paper in papers:
                uid = paper["id"]

                if uid in by_id:
                    # 기존 기록에 새 분야가 발견되면 분야를 추가합니다.
                    old_topics = by_id[uid].get("topic", "").split(", ")
                    if topic not in old_topics:
                        by_id[uid]["topic"] = ", ".join(old_topics + [topic])
                    continue

                by_id[uid] = paper

            print(f"{topic}: 검색 {len(papers)}건")
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"{topic} 검색 오류: {exc}")

        time.sleep(0.4)

    # 최근 1년의 기록을 유지해 웹사이트에 누적합니다.
    cutoff = (now.date() - timedelta(days=365)).isoformat()
    all_papers = [
        p for p in by_id.values()
        if p.get("date") and p["date"] >= cutoff
    ]
    all_papers.sort(
        key=lambda p: p.get("date", ""),
        reverse=True,
    )

    DATA_FILE.write_text(
        json.dumps(all_papers, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"완료: 웹사이트에 논문 {len(all_papers)}건 저장")


if __name__ == "__main__":
    main()
