"""정치 뉴스 알리미 (표준 라이브러리 전용 / 디스코드 웹훅 버전)

GitHub Actions에서 주기적으로 실행 -> RSS에서 새 기사 확인 -> 디스코드 채널로 제목+링크 전송.
외부 패키지가 필요 없어서 pip install 단계가 없습니다.
"""
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote

# ================= 설정: 여기만 고치면 됩니다 =================
GOOGLE_NEWS_QUERIES = [
    "국회 OR 대통령실 OR 여야",
    "국민의힘 OR 더불어민주당",
    "국정감사 OR 본회의 OR 법안",
]
RSS_FEEDS = {}          # 언론사 RSS 추가: {"이름": "RSS 2.0 주소"}
INCLUDE_KEYWORDS = []   # 비어 있으면 전부 통과. 값이 있으면 제목에 하나라도 포함된 기사만
EXCLUDE_KEYWORDS = []   # 제목에 하나라도 포함되면 제외
MAX_AGE_HOURS = 24      # 이보다 오래된 기사는 무시
MAX_PER_RUN = 30        # 1회 실행당 최대 전송 기사 수 (남은 건 다음 실행 때)
MAX_SEEN = 3000         # seen.json에 보관할 최대 기사 수
# ============================================================

BASE = Path(__file__).parent
SEEN_FILE = BASE / "seen.json"
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
# 디스코드는 파이썬 기본 User-Agent를 차단(403)하는 경우가 있어 직접 지정해야 합니다.
UA = "political-news-alert/1.0 (+github-actions)"
DISCORD_LIMIT = 1900    # 디스코드 메시지 한도는 2000자 -> 여유를 둠


# ---------- RSS 가져오기 ----------
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def parse_rss(content):
    try:
        return ET.fromstring(content)
    except (ET.ParseError, ValueError):
        # 일부 국내 사이트는 euc-kr 같은 다중바이트 인코딩을 선언한 RSS를 쓰는데,
        # 파이썬 XML 파서가 이를 ValueError로 거부한다 -> cp949로 직접 해석
        text = content.decode("cp949", errors="replace")
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
        return ET.fromstring(text)


def feed_sources():
    for q in GOOGLE_NEWS_QUERIES:
        url = ("https://news.google.com/rss/search?q=" + quote(f"{q} when:1d")
               + "&hl=ko&gl=KR&ceid=KR:ko")
        yield f"구글뉴스 '{q}'", url
    yield from RSS_FEEDS.items()


def fetch_items(name, url):
    try:
        root = parse_rss(http_get(url))
    except (OSError, ET.ParseError, ValueError, http.client.HTTPException) as e:
        print(f"[경고] {name}: {type(e).__name__}", file=sys.stderr)
        return []
    items = []
    for it in root.iter("item"):
        title = re.sub(r"\s+", " ", (it.findtext("title") or "")).strip()
        link = (it.findtext("link") or "").strip()
        key = (it.findtext("guid") or link).strip()
        ts = 0
        pub = it.findtext("pubDate")
        if pub:
            try:
                ts = parsedate_to_datetime(pub).timestamp()
            except (TypeError, ValueError):
                pass
        if title and link and key:
            items.append({"key": key, "title": title, "link": link, "ts": ts})
    return items


def collect(seen_set):
    now = time.time()
    inc = [k.lower() for k in INCLUDE_KEYWORDS]
    exc = [k.lower() for k in EXCLUDE_KEYWORDS]
    out, picked = [], set()
    for name, url in feed_sources():
        for it in fetch_items(name, url):
            if it["key"] in seen_set or it["key"] in picked:
                continue
            if it["ts"] and (now - it["ts"]) / 3600 > MAX_AGE_HOURS:
                continue
            low = it["title"].lower()
            if inc and not any(k in low for k in inc):
                continue
            if any(k in low for k in exc):
                continue
            picked.add(it["key"])
            out.append(it)
    return out


# ---------- 디스코드 전송 ----------
def esc(text):
    """디스코드 마크다운 기호가 제목에서 서식으로 해석되지 않게 이스케이프."""
    return re.sub(r"([\\*_~`|>\[\]])", r"\\\1", text)


def make_messages(items):
    """한도(1900자) 안에서 기사들을 메시지 여러 개로 나눈다. -> [(본문, 담긴 기사들)]"""
    header = "📰 **정치 뉴스 새 소식**"
    msgs, cur, cur_items = [], header, []
    for it in items:
        link = it["link"].replace(">", "%3E")
        line = f"• [{esc(it['title'][:200])}](<{link}>)"   # <> 로 감싸면 링크 미리보기 카드가 안 뜸
        if cur_items and len(cur) + 1 + len(line) > DISCORD_LIMIT:
            msgs.append((cur, cur_items))
            cur, cur_items = header, []
        cur += "\n" + line
        cur_items.append(it)
    if cur_items:
        msgs.append((cur, cur_items))
    return msgs


def post_discord(content):
    body = json.dumps({
        "content": content,
        "allowed_mentions": {"parse": []},   # 제목에 @everyone 등이 있어도 호출(핑)되지 않게
    }).encode("utf-8")
    req = urllib.request.Request(
        WEBHOOK, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": UA},
    )
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20):
                return                        # 성공(보통 204 No Content)
        except urllib.error.HTTPError as e:
            if e.code == 429:                 # 너무 빨리 보냄 -> 안내된 시간만큼 대기 후 재시도
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except (ValueError, TypeError, AttributeError):
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            raise RuntimeError(f"디스코드 오류 HTTP {e.code}") from None
        except OSError as e:
            # 웹훅 주소는 비밀값이라 예외 내용은 출력하지 않고 종류만 남긴다.
            raise RuntimeError(f"네트워크 오류: {type(e).__name__}") from None
    raise RuntimeError("디스코드 전송 재시도 초과 (429)")


# ---------- 상태 저장 ----------
def load_seen():
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen[-MAX_SEEN:], ensure_ascii=False, indent=0),
                         encoding="utf-8")


# ---------- 메인 ----------
def main():
    if not WEBHOOK:
        sys.exit("환경변수 DISCORD_WEBHOOK_URL 이 없습니다.")

    first_run = not SEEN_FILE.exists()
    seen = load_seen()
    items = collect(set(seen))

    if first_run:  # 첫 실행: 기존 기사는 '본 것'으로만 기록 (한꺼번에 쏟아지는 것 방지)
        if not items:
            print("[경고] 첫 실행인데 가져온 기사가 없습니다. 검색어/주소를 확인하세요.")
            return
        save_seen([i["key"] for i in items])
        post_discord("✅ 정치 뉴스 알리미가 시작되었습니다. 지금부터 올라오는 새 기사만 알려드립니다.")
        print(f"초기화 완료: {len(items)}건 기록")
        return

    items.sort(key=lambda i: i["ts"])          # 오래된 것 -> 최신
    batch = items[:MAX_PER_RUN]
    print(f"새 기사 {len(items)}건 중 {len(batch)}건 전송 시도")

    for content, sent_items in make_messages(batch):
        try:
            post_discord(content)
        except RuntimeError as e:
            print(f"[오류] {e}", file=sys.stderr)
            sys.exit(1)                        # 실패분은 기록되지 않아 다음 실행 때 재시도
        seen.extend(i["key"] for i in sent_items)
        save_seen(seen)                        # 성공한 것만 기록
        time.sleep(1)


if __name__ == "__main__":
    main()
