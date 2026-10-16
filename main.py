import os
import json
import time
import smtplib
import html
import re
from pathlib import Path
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import quote

import requests

# --------------------------------------------------
# 설정: 환경 변수에서 읽으며 비밀번호는 코드에 넣지 않습니다.
# --------------------------------------------------
API_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SEEN_FILE = Path("seen_ids.json")
KST = timezone(timedelta(hours=9))

MAX_PER_TOPIC = int(os.getenv("MAX_PER_TOPIC", "25"))
OLLAMA_URL = os.getenv("OLLAMA_URL", "").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "")

TOPICS = {
    "Protein": [
        "protein engineering", "protein design", "proteomics",
        "protein structure", "protein therapeutics"
    ],
    "Nucleic acids": [
        "nucleic acid", "mRNA", "RNA therapeutics", "DNA",
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

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "DailyPaperDigest/1.0 (personal research tool)"
})


def load_seen():
    if not SEEN_FILE.exists():
        return []
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        raise RuntimeError(
            "seen_ids.json을 읽을 수 없습니다. "
            "중복 방지를 위해 파일을 확인하세요."
        )


def save_seen(ids):
    # 최근 20,000개만 유지
    data = sorted(set(ids))[-20000:]
    SEEN_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def search_topic(topic, terms, start_date, end_date):
    # 검색어는 제목/초록에 한정하여 관련성을 높입니다.
    term_query = " OR ".join(
        f'TITLE_ABS:"{term}"' for term in terms
    )
    query = (
        f"({term_query}) AND "
        f"FIRST_PDATE:[{start_date} TO {end_date}]"
    )

    params = {
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": MAX_PER_TOPIC,
        "sort_date": "y",
    }

    response = SESSION.get(API_URL, params=params, timeout=45)
    response.raise_for_status()
    records = response.json().get("resultList", {}).get("result", [])

    papers = []
    for item in records:
        title = (item.get("title") or "").strip()
        if not title:
            continue

        source = item.get("source", "")
        article_id = item.get("id", "")
        doi = item.get("doi", "")
        pmid = item.get("pmid", "")
        pmcid = item.get("pmcid", "")

        # PMID/DOI/Europe PMC ID 우선순위로 안정적인 식별자 생성
        uid = (
            f"doi:{doi.lower()}" if doi
            else f"pmid:{pmid}" if pmid
            else f"{source}:{article_id}"
        )

        if pmcid:
            full_text_url = (
                "https://europepmc.org/articles/" + quote(pmcid)
            )
        elif pmid:
            full_text_url = (
                "https://pubmed.ncbi.nlm.nih.gov/" + quote(pmid) + "/"
            )
        elif doi:
            full_text_url = "https://doi.org/" + quote(doi, safe="/")
        else:
            full_text_url = "https://europepmc.org/search?query=" + quote(title)

        authors = item.get("authorString", "저자 정보 없음")
        abstract = re.sub(
            r"<[^>]+>", " ",
            item.get("abstractText", "") or ""
        )
        abstract = re.sub(r"\s+", " ", abstract).strip()

        papers.append({
            "uid": uid,
            "title": title,
            "topic": topic,
            "date": item.get("firstPublicationDate", "날짜 정보 없음"),
            "journal": item.get("journalTitle", "저널 정보 없음"),
            "authors": authors,
            "doi": doi,
            "url": full_text_url,
            "abstract": abstract,
        })

    return papers


def ollama_summary(paper):
    """Ollama를 설정한 경우 로컬 LLM으로 초록을 요약합니다."""
    if not OLLAMA_URL or not OLLAMA_MODEL:
        return None

    if not paper["abstract"]:
        return None

    prompt = f"""
당신은 생명과학 분야의 논문을 검토하는 연구 보조자입니다.
아래 제목과 초록에 명시된 정보만 근거로 한국어로 작성하세요.
초록에 없는 실험 결과, 수치, 기전은 만들어내지 마세요.
확인할 수 없는 사항은 '초록만으로 확인 불가'라고 표시하세요.

제목: {paper['title']}
분야: {paper['topic']}
저널: {paper['journal']}
초록: {paper['abstract']}

다음 항목을 Markdown 없이 일반 텍스트로 상세히 작성하세요.
1. 한 문장 핵심 요약
2. 연구 배경 및 해결하려는 문제
3. 연구 목적과 가설
4. 실험 설계 및 방법론
5. 주요 결과와 정량적 근거(초록에 있는 경우)
6. 기존 연구 대비 기여점
7. 한계와 추가 검증이 필요한 사항
8. 연구 실무에 적용할 수 있는 아이디어

초록에 언급되지 않은 방법, 결과, 한계는 추측하지 마세요.
"""
    try:
        response = SESSION.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
            },
            timeout=240,
        )
        response.raise_for_status()
        return response.json().get("response", "").strip() or None
    except (requests.RequestException, ValueError) as exc:
        print(f"LLM 요약 실패: {exc}")
        return None


def fallback_summary(paper):
    """LLM 미설정 시 초록 기반 요약. AI 분석으로 표시하지 않습니다."""
    abstract = paper["abstract"]
    if not abstract:
        return (
            "초록을 확보하지 못했습니다. 원문 링크에서 내용을 확인하세요."
        )
    return (
        "초록 기반 자동 정리입니다. AI의 상세 분석은 수행되지 않았습니다.\n\n"
        + abstract
    )


def make_email(papers, run_date):
    msg = EmailMessage()
    msg["Subject"] = (
        f"[Daily Paper Digest] {run_date} | 신규 논문 {len(papers)}편"
    )
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]

    # 일반 텍스트 대체 본문
    plain = [
        f"Daily Paper Digest — {run_date}",
        f"신규 논문: {len(papers)}편",
        "",
    ]
    for paper in papers:
        plain.extend([
            f"[{paper['topic']}] {paper['title']}",
            f"출판일: {paper['date']} | 저널: {paper['journal']}",
            f"저자: {paper['authors']}",
            f"원문: {paper['url']}",
            "",
            paper["summary"],
            "",
            "-" * 60,
            "",
        ])
    if not papers:
        plain.append(
            "이번 검색 범위에서 새 논문을 찾지 못했습니다."
        )
    msg.set_content("\n".join(plain))

    # HTML 본문
    cards = []
    for paper in papers:
        safe = lambda x: html.escape(str(x or "정보 없음"))
        cards.append(f"""
        <article style="padding:16px 0;border-bottom:1px solid #ddd">
          <p><b>{safe(paper['topic'])}</b> · {safe(paper['date'])}</p>
          <h2 style="font-size:18px">{safe(paper['title'])}</h2>
          <p>{safe(paper['journal'])} · {safe(paper['authors'])}</p>
          <p><a href="{html.escape(paper['url'], quote=True)}">
             논문 페이지 열기</a></p>
          <pre style="white-space:pre-wrap;font:14px sans-serif">
{safe(paper['summary'])}</pre>
        </article>
        """)

    body = "".join(cards) or "<p>이번 검색 범위에서 새 논문이 없습니다.</p>"
    msg.add_alternative(f"""
    <html><body style="font-family:Arial,sans-serif;line-height:1.6;
                       max-width:800px;margin:auto;padding:20px">
      <h1>Daily Paper Digest</h1>
      <p>{html.escape(run_date)} · 신규 논문 {len(papers)}편</p>
      {body}
      <p style="color:#666;font-size:12px">
      요약은 논문 초록을 기반으로 생성됩니다. 중요한 결과와
      실험 조건은 원문에서 확인하세요.</p>
    </body></html>
    """, subtype="html")
    return msg


def send_email(msg):
    email_from = os.environ["EMAIL_FROM"]
    password = os.environ["EMAIL_APP_PASSWORD"]

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(email_from, password)
        smtp.send_message(msg)


def main():
    now = datetime.now(KST)
    run_date = now.strftime("%Y-%m-%d")

    # 3일간 검색하여 색인 지연을 보완하고, 기존 논문은 제외
    start_date = (now.date() - timedelta(days=3)).isoformat()
    end_date = now.date().isoformat()

    seen = set(load_seen())
    found = {}

    for topic, terms in TOPICS.items():
        try:
            for paper in search_topic(
                topic, terms, start_date, end_date
            ):
                # 동일 논문이 여러 주제에 걸리면 분야를 합칩니다.
                uid = paper["uid"]
                if uid in found:
                    if topic not in found[uid]["topic"].split(", "):
                        found[uid]["topic"] += ", " + topic
                else:
                    found[uid] = paper
            time.sleep(0.4)
        except (requests.RequestException, ValueError, KeyError) as exc:
            print(f"{topic} 검색 실패: {exc}")

    new_papers = [
        p for uid, p in found.items()
        if uid not in seen
    ]

    # 초록이 있는 논문을 우선하고, 요약 결과를 각 논문에 기록
    for paper in new_papers:
        summary = ollama_summary(paper)
        paper["summary"] = summary or fallback_summary(paper)

    new_papers.sort(
        key=lambda p: p["date"], reverse=True
    )

    # 검색에 실패한 경우 빈 리포트를 정상 결과로 오인하지 않도록 함
    if not found:
        raise RuntimeError(
            "모든 주제에서 검색 결과가 없습니다. "
            "API 연결 또는 검색 쿼리를 확인하세요."
        )

    msg = make_email(new_papers, run_date)
    send_email(msg)

    # 이메일 발송에 성공한 뒤에만 중복 기록을 갱신
    save_seen(seen | set(found.keys()))
    print(f"완료: 신규 논문 {len(new_papers)}편 이메일 발송")


if __name__ == "__main__":
    main()
