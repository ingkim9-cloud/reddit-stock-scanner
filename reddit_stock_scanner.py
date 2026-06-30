"""
Reddit Stock Scanner (API 키 없이 공개 JSON 사용)
매일 아침 실행 → Claude API 분석 → Telegram 발송
"""

import os
import re
import time
import json
import requests
from datetime import datetime, timezone
from collections import defaultdict

# ── 환경 변수 ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# ── 설정 ──────────────────────────────────────────────────────────────────────
SUBREDDITS   = ["wallstreetbets", "stocks", "investing", "StockMarket"]
TOP_N        = 20
MIN_MENTIONS = 3
FETCH_LIMIT  = 100

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
}

TICKER_PATTERN = re.compile(r'\b([A-Z]{2,5})\b')


# ─────────────────────────────────────────────────────────────────────────────
# 1. Reddit 공개 JSON 수집 (API 키 불필요)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_subreddit(subreddit: str, limit: int = FETCH_LIMIT) -> list[dict]:
    """Reddit 공개 JSON 엔드포인트 사용 - 로그인/API 키 불필요"""
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()["data"]["children"]
    except Exception as e:
        print(f"[WARN] {subreddit} 수집 실패: {e}")
        return []


def extract_tickers(text: str) -> list[str]:
    return [t for t in TICKER_PATTERN.findall(text) if t not in EXCLUDE_WORDS]


def collect_mentions() -> dict:
    ticker_data = defaultdict(lambda: {
        "count": 0,
        "sources": set(),
        "sample_titles": [],
        "upvotes": 0,
    })

    for sub in SUBREDDITS:
        posts = fetch_subreddit(sub)
        for post in posts:
            data     = post["data"]
            title    = data.get("title", "")
            selftext = data.get("selftext", "")
            upvotes  = data.get("score", 0)
            tickers  = extract_tickers(title + " " + selftext)

            for t in set(tickers):
                ticker_data[t]["count"]   += tickers.count(t)
                ticker_data[t]["sources"].add(sub)
                ticker_data[t]["upvotes"] += upvotes
                if len(ticker_data[t]["sample_titles"]) < 3:
                    ticker_data[t]["sample_titles"].append(title[:120])

        print(f"  [{sub}] 게시글 {len(posts)}개 수집 완료")
        time.sleep(2)  # Reddit 요청 간격 준수

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
        key=lambda x: x["count"] * len(x["sources"]) + x["upvotes"] // 100,
        reverse=True,
    )
    return filtered[:TOP_N]


# ─────────────────────────────────────────────────────────────────────────────
# 2. Claude API 분석
# ─────────────────────────────────────────────────────────────────────────────

def build_prompt(ranked: list[dict]) -> str:
    lines = []
    for item in ranked:
        lines.append(
            f"- {item['ticker']}: 언급 {item['count']}회 | "
            f"서브레딧: {', '.join(item['sources'])} | "
            f"누적 업보트: {item['upvotes']} | "
            f"샘플 제목: {' / '.join(item['sample_titles'][:2])}"
        )
    data_block = "\n".join(lines)

    return f"""당신은 Reddit 주식 데이터 분석 전문가입니다.
아래는 오늘 Reddit에서 가장 많이 언급된 종목 목록입니다.

[데이터]
{data_block}

[분석 지침]
1. 각 종목을 세 가지 기준으로 평가하세요:
   - 모멘텀 신호: 단순 밈/반복인지, 실질적 스토리(계약·실적·숏스퀴즈 등)가 있는지
   - 감성 방향: 상승 심리 / 하락 심리 / 혼재
   - 주목 이유: 제목 샘플에서 유추되는 핵심 촉매

2. 상위 5개 종목을 "오늘의 레이더"로 선정하고, 각각 2~3문장으로 요약

3. 경고 종목(밈/과대광고 의심)이 있으면 별도 표시

4. 마지막에 한 줄 총평

[출력 형식] Telegram용 (이모지 사용, 마크다운 없이 일반 텍스트)
"""


def analyze_with_claude(ranked: list[dict]) -> str:
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
            "messages": [{"role": "user", "content": build_prompt(ranked)}],
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
    print("▶ Reddit 데이터 수집 중... (API 키 없이 공개 JSON 사용)")
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
