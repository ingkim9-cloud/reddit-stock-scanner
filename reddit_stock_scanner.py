"""
Reddit RSS Stock Scanner
Reddit RSS 피드 수집 → Claude API 분석 → Telegram 발송
API 키 불필요, 봇 차단 없음
"""

import os
import re
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from collections import defaultdict

# ── 환경 변수 ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# ── 설정 ──────────────────────────────────────────────────────────────────────
SUBREDDITS = ["wallstreetbets", "stocks", "investing", "StockMarket"]
TOP_N        = 20
MIN_MENTIONS = 2

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; RSS Reader)",
    "Accept": "application/rss+xml, application/xml, text/xml",
}

EXCLUDE_WORDS = {
    "FOR", "ARE", "NOW", "ALL", "NEW", "USA", "CEO", "IPO", "ETF", "GDP",
    "CPI", "FED", "SEC", "FDA", "THE", "IRS", "AND", "NOT", "BUT", "YOU",
    "HAS", "ITS", "CAN", "GET", "PUT", "CALL", "EDIT", "UPDATE", "WAS",
    "THIS", "THAT", "FROM", "WITH", "HAVE", "WILL", "BEEN", "WHAT", "WHEN",
    "THEN", "THAN", "THEY", "JUST", "LONG", "SHORT", "YOLO", "GAIN", "LOSS",
    "HIGH", "OPEN", "DOWN", "HOLD", "SELL", "REAL", "GOOD", "NEXT", "WEEK",
    "YEAR", "LATE", "LAST", "BULL", "BEAR", "PUMP", "DUMP", "BACK", "INTO",
    "OVER", "MORE", "SOME", "ALSO", "VERY", "MOST", "ONLY", "EVEN", "BOTH",
    "SAME", "EACH", "SUCH", "DOES", "MUCH", "NEED", "KNOW", "LIKE", "WELL",
    "CASH", "DEBT", "LINK", "POST", "PAID", "RATE", "RISK", "PLAY", "MOVE",
    "EDIT", "TLDR", "IMHO", "IIRC", "FOMO", "HODL", "DYOR", "ATH", "ATL",
    "EPS", "TTM", "YOY", "QOQ", "TAM", "SAM", "SOM", "EBIT", "EBITDA",
    "RSS", "XML", "HTTP", "HTTPS", "WWW", "COM", "NET", "ORG",
}

TICKER_PATTERN = re.compile(r'\b([A-Z]{2,5})\b')


# ─────────────────────────────────────────────────────────────────────────────
# 1. Reddit RSS 수집
# ─────────────────────────────────────────────────────────────────────────────

def fetch_rss(subreddit: str) -> list[dict]:
    """Reddit RSS 피드로 게시글 수집 - API 키 불필요, 봇 차단 없음"""
    url = f"https://www.reddit.com/r/{subreddit}/hot.rss?limit=100"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()

        root = ET.fromstring(resp.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        posts = []
        # Atom 형식 파싱
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            content_el = entry.find("atom:content", ns)
            title = title_el.text if title_el is not None else ""
            content = content_el.text if content_el is not None else ""
            posts.append({"title": title or "", "content": content or ""})

        # RSS 2.0 형식 파싱 (fallback)
        if not posts:
            channel = root.find("channel")
            if channel:
                for item in channel.findall("item"):
                    title_el = item.find("title")
                    desc_el = item.find("description")
                    title = title_el.text if title_el is not None else ""
                    content = desc_el.text if desc_el is not None else ""
                    posts.append({"title": title or "", "content": content or ""})

        print(f"  [{subreddit}] {len(posts)}개 게시글 수집")
        return posts

    except Exception as e:
        print(f"  [WARN] {subreddit} 수집 실패: {e}")
        return []


def extract_tickers(text: str) -> list[str]:
    # HTML 태그 제거
    clean = re.sub(r'<[^>]+>', ' ', text)
    return [t for t in TICKER_PATTERN.findall(clean) if t not in EXCLUDE_WORDS]


def collect_mentions() -> dict:
    ticker_data = defaultdict(lambda: {
        "count": 0,
        "sources": set(),
        "sample_titles": [],
    })

    for sub in SUBREDDITS:
        posts = fetch_rss(sub)
        for post in posts:
            text = post["title"] + " " + post["content"]
            tickers = extract_tickers(text)
            for t in set(tickers):
                ticker_data[t]["count"]   += tickers.count(t)
                ticker_data[t]["sources"].add(sub)
                if len(ticker_data[t]["sample_titles"]) < 3:
                    ticker_data[t]["sample_titles"].append(post["title"][:120])
        time.sleep(1)

    for v in ticker_data.values():
        v["sources"] = list(v["sources"])

    return dict(ticker_data)


def filter_and_rank(ticker_data: dict) -> list[dict]:
    filtered = [
        {"ticker": k, **v}
        for k, v in ticker_data.items()
        if v["count"] >= MIN_MENTIONS
    ]
    filtered.sort(
        key=lambda x: x["count"] * len(x["sources"]),
        reverse=True,
    )
    return filtered[:TOP_N]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Claude API 분석
# ─────────────────────────────────────────────────────────────────────────────

def analyze_with_claude(ranked: list[dict]) -> str:
    lines = []
    for item in ranked:
        lines.append(
            f"- {item['ticker']}: 언급 {item['count']}회 | "
            f"서브레딧: {', '.join(item['sources'])} | "
            f"샘플 제목: {' / '.join(item['sample_titles'][:2])}"
        )

    prompt = f"""당신은 Reddit 주식 데이터 분석 전문가입니다.
아래는 오늘 Reddit에서 가장 많이 언급된 종목 목록입니다.

[데이터]
{chr(10).join(lines)}

[분석 지침]
1. 각 종목을 세 가지 기준으로 평가:
   - 모멘텀 신호: 단순 밈/반복인지, 실질적 스토리(계약·실적·숏스퀴즈 등)가 있는지
   - 감성 방향: 상승 심리 / 하락 심리 / 혼재
   - 주목 이유: 제목 샘플에서 유추되는 핵심 촉매

2. 상위 5개 종목을 "오늘의 레이더"로 선정, 각각 2~3문장 요약

3. 경고 종목(밈/과대광고 의심) 별도 표시

4. 마지막에 한 줄 총평

[출력 형식] Telegram용 (이모지 사용, 마크다운 없이 일반 텍스트)
"""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 1500,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


# ─────────────────────────────────────────────────────────────────────────────
# 3. Telegram 발송
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    now_kst = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    full_msg = f"📡 Reddit 주식 레이더 | {now_kst}\n{'─'*35}\n\n{message}"

    chunks = [full_msg[i:i+4000] for i in range(0, len(full_msg), 4000)]
    for chunk in chunks:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
            timeout=10,
        ).raise_for_status()
        time.sleep(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("▶ Reddit RSS 수집 중...")
    ticker_data = collect_mentions()
    print(f"  원시 종목 수: {len(ticker_data)}")

    ranked = filter_and_rank(ticker_data)
    print(f"  필터 후 분석 대상: {len(ranked)}개")

    if not ranked:
        send_telegram("⚠️ 오늘은 유의미한 언급 종목이 없습니다.")
        return

    print("▶ Claude 분석 중...")
    analysis = analyze_with_claude(ranked)

    print("▶ Telegram 발송 중...")
    send_telegram(analysis)
    print("✅ 완료")


if __name__ == "__main__":
    main()
