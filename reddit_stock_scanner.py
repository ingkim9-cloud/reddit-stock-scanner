"""
Reddit RSS Stock Scanner
Reddit RSS 피드 수집 → Claude API 분석 → Telegram 발송
"""

import os
import re
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from collections import defaultdict

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

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
    "EPS", "TTM", "YOY", "QOQ", "TAM", "SAM", "SOM", "EBIT",
    "RSS", "XML", "HTTP", "WWW", "COM", "NET", "ORG",
    # 일반 단어 추가
    "OF", "TO", "IS", "BE", "IN", "IT", "AT", "ON", "BY", "AS",
    "IF", "OR", "AN", "UP", "SO", "DO", "GO", "NO", "MY", "HE",
    "WE", "US", "HI", "OK", "OH", "OW", "OX", "RE", "PE",
    "RATIO", "STOCK", "STOCKS", "PRICE", "TRADE", "MARKET",
    "SHARE", "SHARES", "MONEY", "FUND", "FUNDS", "LOSS", "GAINS",
}

TICKER_PATTERN = re.compile(r'\b([A-Z]{2,5})\b')


def fetch_rss(subreddit: str) -> list:
    url = f"https://www.reddit.com/r/{subreddit}/hot.rss?limit=100"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        posts = []
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            content_el = entry.find("atom:content", ns)
            title = title_el.text if title_el is not None else ""
            content = content_el.text if content_el is not None else ""
            posts.append({"title": title or "", "content": content or ""})
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


def extract_tickers(text: str) -> list:
    clean = re.sub(r'<[^>]+>', ' ', text)
    return [t for t in TICKER_PATTERN.findall(clean) if t not in EXCLUDE_WORDS]


def collect_mentions() -> dict:
    ticker_data = defaultdict(lambda: {"count": 0, "sources": set(), "sample_titles": []})
    for sub in SUBREDDITS:
        posts = fetch_rss(sub)
        for post in posts:
            text = post["title"] + " " + post["content"]
            tickers = extract_tickers(text)
            for t in set(tickers):
                ticker_data[t]["count"] += tickers.count(t)
                ticker_data[t]["sources"].add(sub)
                if len(ticker_data[t]["sample_titles"]) < 3:
                    ticker_data[t]["sample_titles"].append(post["title"][:120])
        time.sleep(1)
    for v in ticker_data.values():
        v["sources"] = list(v["sources"])
    return dict(ticker_data)


def filter_and_rank(ticker_data: dict) -> list:
    filtered = [
        {"ticker": k, **v}
        for k, v in ticker_data.items()
        if v["count"] >= MIN_MENTIONS
    ]
    filtered.sort(key=lambda x: x["count"] * len(x["sources"]), reverse=True)
    return filtered[:TOP_N]


def analyze_with_claude(ranked: list) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = []
    for item in ranked:
        lines.append(
            f"- {item['ticker']}: 언급 {item['count']}회 | "
            f"서브레딧: {', '.join(item['sources'])} | "
            f"샘플 제목: {' / '.join(item['sample_titles'][:2])}"
        )

    prompt = f"""당신은 Reddit 주식 데이터 분석 전문가입니다.
아래는 오늘({today}) Reddit에서 가장 많이 언급된 종목 목록입니다.

[데이터]
{chr(10).join(lines)}

[분석 지침]
상위 5개 종목을 선정하고 아래 형식으로 출력하세요.

각 종목 평가 기준:
- 모멘텀 점수(★1~5): 뉴스기반=5, 펀더멘털기반=4, 내러티브중심=3, 단일게시물=2, 투기성=1
- 신뢰도 유형: 뉴스기반 / 펀더멘털기반 / 내러티브중심 / 단일게시물영향 / 투기성
- 투자 심리: 강한상승 / 상승 / 중립 / 혼재 / 하락
- 핵심 이슈: 1~2문장
- 시사점: 1문장

[출력 형식 - 반드시 이 순서로]

📊 Reddit Hot Stocks | {today}
━━━━━━━━━━━━━━━━━━━━━━━━━━

[순위이모지] [N]위 [티커] ([N]회)
종목: [회사명]
모멘텀: [★표시] | 신뢰도: [유형]
심리: [심리방향]
이슈: [핵심이슈]
시사점: [시사점]

(5개 종목 반복)

━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 종목 요약표
티커 | 언급 | 모멘텀 | 신뢰도
[티커] | [N]회 | [★표시] | [유형]
(5개 종목)

⚠️ 경고 종목: [밈/과대광고 의심 종목]
💡 오늘의 핵심: [한 줄 총평]

주의: OF, TO, IS, BE 등 일반 단어는 종목으로 포함하지 말 것
Telegram용이므로 마크다운 없이 일반 텍스트로 작성
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
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["content"][0]["text"]


def send_telegram(message: str) -> None:
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk},
            timeout=10,
        ).raise_for_status()
        time.sleep(0.5)


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
