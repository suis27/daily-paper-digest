import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timedelta, timezone

import requests

KST = timezone(timedelta(hours=9))
API_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
DATA_FILE = Path("site/papers.json")

MAX_PER_TOPIC = int(os.getenv("MAX_PER_TOPIC", "25"))
MAX_SUMMARIES_PER_RUN = int(
    os.getenv("MAX_SUMMARIES_PER_RUN", "5")
)
MAX_ABSTRACT_REFRESH_PER_RUN = 10

OLLAMA_URL = os.getenv(
    "OLLAMA_URL", "http://localhost:11434"
).rstrip("/")
OLLAMA_MODEL = os.getenv(
    "OLLAMA_MODEL", "qwen2.5:3b"
)

TOPICS = {
    "Protein": [
        "protein engineering", "protein design",
        "protein structure", "proteomics",
        "protein therapeutics",
    ],
    "Nucleic acids": [
        "nucleic acid", "mRNA", "RNA therapeutics",
        "oligonucleotide", "siRNA",
        "antisense oligonucleotide",
    ],
    "Immunity": [
        "immunity", "immunology", "immune cell",
        "immunotherapy", "inflammation",
        "immune response",
    ],
    "Drug delivery": [
        "drug delivery", "nanoparticle", "nanomedicine",
        "lipid nanoparticle", "targeted delivery",
        "controlled release",
    ],
}

session = requests.Session()
session.headers["User-Agent"] = "BioPaperDigest/1.0"


def clean_text(value):
    """HTML 태그와 불필요한 공백을 정리한다."""
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def load_existing():
    if not DATA_FILE.exists():
        return []

    try:
        data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("papers.json의 최상위 값은 배열이어야 합니다.")
        return data
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"기존 논문 데이터를 읽지 못했습니다: {exc}"
        ) from exc


def get_abstract_from_search(query):
    """Europe PMC 검색 API에서 초록을 다시 조회한다."""
    response = session.get(
        API_URL,
        params={
            "query": query,
            "format": "json",
            "resultType": "core",
            "pageSize": 1,
        },
        timeout=45,
    )
    response.raise_for_status()

    records = (
        response.json()
        .get("resultList", {})
        .get("result", [])
    )

    if not records:
        return ""

    return clean_text(records[0].get("abstractText", ""))


def fetch_abstract(paper):
    """DOI, PMID, PMCID를 이용해 초록을 재조회한다."""
    doi = (paper.get("doi") or "").strip()
    pmid = (paper.get("pmid") or "").strip()
    pmcid = (paper.get("pmcid") or "").strip()

    # 우선 DOI 또는 PMID로 Europe PMC 검색
    queries = []

    if doi:
        queries.append(f'DOI:"{doi}"')

    if pmid:
        queries.append(f"EXT_ID:{pmid} AND SRC:MED")

    for query in queries:
        try:
            abstract = get_abstract_from_search(query)
            if abstract:
                return abstract
        except requests.RequestException as exc:
            print(f"[초록 검색 오류] {query}: {exc}")

    # 공개 전문 XML이 제공되는 PMC 논문은 전문 XML도 확인
    if pmcid:
        try:
            response = session.get(
                "https://www.ebi.ac.uk/europepmc/webservices/rest/"
                f"PMC/{pmcid}/fullTextXML",
                timeout=45,
            )

            if response.ok:
                root = ET.fromstring(response.content)
                abstract_node = root.find(".//abstract")

                if abstract_node is not None:
                    abstract = clean_text(
                        " ".join(abstract_node.itertext())
                    )
                    if abstract:
                        return abstract

        except (
            requests.RequestException,
            ET.ParseError,
            ValueError,
        ) as exc:
            print(f"[전문 XML 조회 실패] {pmcid}: {exc}")

    return ""


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

    data = response.json()
    records = data.get("resultList", {}).get("result", [])

    print(f"[{topic}] HTTP: {response.status_code}")
    print(f"[{topic}] 검색 결과: {data.get('hitCount', 0)}")

    papers = []

    for item in records:
        title = clean_text(item.get("title", ""))
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
            url = f"https://europepmc.org/articles/{pmcid}"
        elif pmid:
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        elif doi:
            url = f"https://doi.org/{doi}"
        else:
            url = (
                "https://europepmc.org/search?query="
                + requests.utils.quote(title)
            )

        abstract = clean_text(item.get("abstractText", ""))

        papers.append({
            "id": uid,
            "title": title,
            "topic": topic,
            "date": item.get("firstPublicationDate", ""),
            "journal": item.get("journalTitle", ""),
            "authors": item.get("authorString", ""),
            "doi": doi,
            "pmid": pmid,
            "pmcid": pmcid,
            "url": url,
            "abstract": abstract,
            "summary": "초록 기반 요약",
        })

    print(f"[{topic}] 반환된 논문: {len(papers)}")
    return papers


def summarize_paper(paper):
    """제목과 초록을 근거로 한국어 상세 요약을 생성한다."""
    abstract = paper.get("abstract", "").strip()

    if not abstract:
        print(f"[요약 건너뜀] 초록 없음: {paper['title']}")
        return None

    prompt = f"""
당신은 단백질, 핵산, 면역학, 약물 전달 분야의 논문 분석가입니다.

아래 제목과 초록만 근거로 한국어 상세 분석을 작성하세요.

제목: {paper.get('title', '')}
분야: {paper.get('topic', '')}
학술지: {paper.get('journal', '')}
발표일: {paper.get('date', '')}
저자: {paper.get('authors', '')}

초록:
{abstract}

JSON 객체 하나만 반환하세요. 모든 값은 한국어 문자열이어야 합니다.

{{
  "research_purpose": "연구 배경과 목적",
  "methods": "실험 설계, 물질, 모델, 분석법",
  "quantitative_results": "보고된 수치와 대조군 비교",
  "novelty": "기존 연구 대비 차별점",
  "limitations": "저자가 명시한 한계와 확인 불가능한 정보",
  "future_research": "후속 연구 제안"
}}

규칙:
- 초록에 없는 수치, 결과, 실험 조건을 만들지 마세요.
- 확인할 수 없는 사항은 초록에서 확인할 수 없다고 쓰세요.
- 논문에서 실제로 보고한 한계와 분석자의 추론을 구분하세요.
- 후속 연구 제안은 제안이라고 명시하세요.
- 각 항목을 2~5문장으로 작성하세요.
"""

    response = session.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
        },
        timeout=300,
    )
    response.raise_for_status()

    result = response.json().get("response", "")
    summary = json.loads(result)

    required_keys = [
        "research_purpose",
        "methods",
        "quantitative_results",
        "novelty",
        "limitations",
        "future_research",
    ]

    if not isinstance(summary, dict):
        raise ValueError("AI 응답이 JSON 객체가 아닙니다.")

    for key in required_keys:
        if not isinstance(summary.get(key), str):
            raise ValueError(f"AI 요약 필드 누락: {key}")

    return summary


def main():
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(KST)
    end_date = now.date()
    start_date = end_date - timedelta(days=30)

    existing = load_existing()
    by_id = {
        paper["id"]: paper
        for paper in existing
        if paper.get("id")
    }

    # 새 논문 검색 및 기존 데이터 갱신
    for topic, terms in TOPICS.items():
        try:
            papers = search_topic(
                topic,
                terms,
                start_date.isoformat(),
                end_date.isoformat(),
            )

            for paper in papers:
                uid = paper["id"]

                if uid not in by_id:
                    by_id[uid] = paper
                    continue

                old = by_id[uid]

                # 기존 AI 요약과 데이터를 유지하면서 빈 메타데이터 보완
                for key, value in paper.items():
                    if key == "ai_summary":
                        continue

                    if key == "topic":
                        topics = old.get("topic", "").split(", ")
                        if value not in topics:
                            old["topic"] = ", ".join(
                                topics + [value]
                            )
                    elif value and (
                        not old.get(key) or key == "abstract"
                    ):
                        old[key] = value

            print(f"{topic}: 검색 {len(papers)}건")

        except (
            requests.RequestException,
            ValueError,
            KeyError,
        ) as exc:
            print(f"{topic} 검색 오류: {exc}")

        time.sleep(0.4)

    # 최근 1년 기록 유지
    cutoff = (
        now.date() - timedelta(days=365)
    ).isoformat()

    all_papers = [
        p for p in by_id.values()
        if p.get("date") and p["date"] >= cutoff
    ]

    all_papers.sort(
        key=lambda p: p.get("date", ""),
        reverse=True,
    )

    # 기존 논문 중 초록이 없는 항목도 일부 재조회
    missing_abstracts = [
        p for p in all_papers
        if not p.get("abstract", "").strip()
    ]

    for paper in missing_abstracts[:MAX_ABSTRACT_REFRESH_PER_RUN]:
        try:
            print(f"[초록 재조회] {paper['title']}")
            abstract = fetch_abstract(paper)

            if abstract:
                paper["abstract"] = abstract
                print(f"[초록 확보 완료] {paper['title']}")
            else:
                print(f"[초록 미제공] {paper['title']}")

        except requests.RequestException as exc:
            print(f"[초록 재조회 실패] {paper['title']}: {exc}")

        time.sleep(0.3)

    # 초록이 확보된 미요약 논문부터 AI 분석
    pending = [
        p for p in all_papers
        if not p.get("ai_summary")
        and p.get("abstract", "").strip()
    ]

    success_count = 0

    for paper in pending[:MAX_SUMMARIES_PER_RUN]:
        try:
            print(f"[AI 요약 시작] {paper['title']}")

            summary = summarize_paper(paper)

            if summary:
                paper["ai_summary"] = summary
                success_count += 1
                print(f"[AI 요약 완료] {paper['title']}")

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            print(f"[AI 요약 실패] {paper['title']}: {exc}")

    # 요약이 실패해도 기존 논문 데이터는 저장
    DATA_FILE.write_text(
        json.dumps(all_papers, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"전체 논문: {len(all_papers)}건")
    print(f"이번 실행 AI 요약 성공: {success_count}건")
    print(f"완료: {DATA_FILE} 저장")


if __name__ == "__main__":
    main()
