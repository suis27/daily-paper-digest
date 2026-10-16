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
        f'"{term}"' for term in terms
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

    print(f"[{topic}] HTTP: {response.status_code}")
    print(f"[{topic}] 검색식: {query}")
    print(f"[{topic}] 전체 검색 결과: {data.get('hitCount', '미확인')}")

    records = data.get("resultList", {}).get("result", [])
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
            url = (
                "https://europepmc.org/search?query="
                + requests.utils.quote(title)
            )

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
                "초록 기반 요약입니다. 연구 목적, 방법, "
                "결과와 한계는 제공된 초록에서 확인하세요."
            ),
        })

    print(f"[{topic}] 반환된 논문: {len(papers)}")
    return papers

import json
import os
import requests

OLLAMA_URL = os.getenv(
    "OLLAMA_URL", "http://localhost:11434"
)
OLLAMA_MODEL = os.getenv(
    "OLLAMA_MODEL", "qwen2.5:3b"
)
MAX_SUMMARIES_PER_RUN = int(
    os.getenv("MAX_SUMMARIES_PER_RUN", "5")
)


def summarize_paper(paper):
    """논문의 제목과 초록을 바탕으로 한국어 상세 요약을 생성한다."""

    abstract = paper.get("abstract", "").strip()

    if not abstract:
        print(f"[요약 건너뜀] 초록 없음: {paper['title']}")
        return None

    prompt = f"""
당신은 단백질, 핵산, 면역학, 약물 전달 분야의 연구를 분석하는
과학 논문 분석가입니다.

아래에 제공된 논문 제목과 초록만 근거로 한국어 분석을 작성하세요.

논문 제목: {paper.get('title', '')}
연구 분야: {paper.get('topic', '')}
학술지: {paper.get('journal', '')}
발표일: {paper.get('date', '')}
초록:
{abstract}

반드시 유효한 JSON 객체 하나만 출력하세요.
다음 6개 키의 값은 모두 한국어 문자열이어야 합니다.

{{
  "research_purpose": "연구 배경과 해결하려는 문제, 연구 목적",
  "methods": "실험 설계, 사용한 물질·모델·분석법",
  "quantitative_results": "보고된 수치, 대조군 비교, 통계적 결과",
  "novelty": "기존 연구와 비교한 차별점",
  "limitations": "저자가 명시한 한계와 초록만으로 확인할 수 없는 정보",
  "future_research": "후속 연구에서 검증할 수 있는 제안"
}}

중요한 규칙:
- 초록에 없는 수치, 실험 조건, 인과관계를 만들어내지 마세요.
- 확인할 수 없는 정보는 '초록에서 확인할 수 없음'이라고 쓰세요.
- 실제 논문에 명시된 한계와 당신의 추론을 구분하세요.
- 후속 연구는 제안임을 명확히 표시하세요.
- 각 항목은 핵심 내용을 담은 2~5문장으로 작성하세요.
"""

    response = requests.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0.2
            }
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
     # 아직 AI 요약이 없는 최신 논문부터 순서대로 처리한다.
    pending = [
        paper for paper in all_papers
        if not paper.get("ai_summary")
    ]

    for paper in pending[:MAX_SUMMARIES_PER_RUN]:
        try:
            print(f"[AI 요약 시작] {paper['title']}")

            summary = summarize_paper(paper)

            if summary:
                paper["ai_summary"] = summary
                print(f"[AI 요약 완료] {paper['title']}")

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            print(
                f"[AI 요약 실패] {paper.get('title', '')}: {exc}"
            )

    DATA_FILE.write_text(
        json.dumps(all_papers, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"완료: 웹사이트에 논문 {len(all_papers)}건 저장")


if __name__ == "__main__":
    main()
