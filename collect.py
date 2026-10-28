"""
전기차 충전기 상태 수집기 (한국환경공단 getChargerInfo)

사용법
    EV_API_KEY=발급받은_일반인증키(Decoding) python collect.py daegu gumi

저장 구조 (지역별)
    data/<지역>/status/<YYYY-MM-DD>/<HHMM>.csv.gz   매 회차 상태 열만 (작게 저장)
    data/<지역>/static/<YYYY-MM-DD>.csv.gz          하루 1번 전체 열 (위치·출력·운영기관 등)
    logs/<지역>/<YYYY-MM>.csv                       회차별 수집 로그 (성공·실패, 페이지 수, 행 수)

API 키는 코드나 저장소에 넣지 말고 GitHub Secrets(EV_API_KEY)로만 전달합니다.
"""
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_URL = "https://apis.data.go.kr/B552584/EvCharger/getChargerInfo"
KST = timezone(timedelta(hours=9))
NUM_OF_ROWS = 9999          # 한 페이지 최대 행 수
MAX_RETRY = 3
TIMEOUT_SEC = 60

# 지역 설정: zcode = 시도 코드(대구 27, 경북 47). addr = 주소 필터(없으면 전체)
REGIONS = {
    "daegu": {"zcode": "27", "addr": None},
    "gumi": {"zcode": "47", "addr": "구미시"},
}

# 매 회차 상태 파일에는 급속 충전기(출력 50kW 이상)만 저장 (저장소 용량 절약).
# 정적 파일(하루 1번)에는 전체 충전기를 저장합니다. 전체를 저장하려면 0으로 바꾸세요.
MIN_STATUS_KW = 50

# 매 회차 저장하는 상태 열 (예측에 필요한 시간·상태 정보)
STATUS_COLS = ["statId", "chgerId", "stat", "statUpdDt", "lastTsdt", "lastTedt", "nowTsdt"]


def to_kw(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def now_kst():
    return datetime.now(KST)


def fetch_page(key, zcode, page):
    params = {"serviceKey": key, "pageNo": page, "numOfRows": NUM_OF_ROWS,
              "zcode": zcode, "dataType": "JSON"}
    url = API_URL + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_SEC) as r:
                body = r.read().decode("utf-8")
            return parse_body(body)
        except Exception as e:                      # 네트워크·형식 오류 → 재시도
            last_err = e
            time.sleep(5 * attempt)
    raise RuntimeError(f"page {page} 실패: {last_err}")


def parse_body(body):
    """JSON 응답에서 (전체 건수, 항목 목록) 추출. 응답 구조 차이를 흡수."""
    try:
        obj = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError("JSON이 아닌 응답: " + body[:200])
    if "response" in obj:                           # {"response": {"header":..., "body":...}}
        head, obj = obj["response"].get("header", {}), obj["response"].get("body", {})
        code = head.get("resultCode", "00")
    else:
        code = obj.get("resultCode", "00")
    if str(code) not in ("00", "0"):
        raise RuntimeError(f"API 오류 코드 {code}: {str(obj)[:200]}")
    total = int(obj.get("totalCount", 0) or 0)
    items = obj.get("items") or {}
    item = items.get("item", []) if isinstance(items, dict) else items
    if isinstance(item, dict):
        item = [item]
    return total, item


def write_csv_gz(path, rows, cols):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())


def append_log(region, rec):
    path = f"logs/{region}/{rec['run_started_kst'][:7]}.csv"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rec))
        if new:
            w.writeheader()
        w.writerow(rec)


def collect(region, key):
    cfg = REGIONS[region]
    started = now_kst()
    rec = {"run_started_kst": started.isoformat(timespec="seconds"), "run_finished_kst": "",
           "region": region, "zcode": cfg["zcode"], "pages": 0, "total_count": 0,
           "rows_saved": 0, "status": "ok", "error": ""}
    rows = []
    try:
        page = 1
        while True:
            page_time = now_kst().isoformat(timespec="seconds")
            total, items = fetch_page(key, cfg["zcode"], page)
            rec["pages"], rec["total_count"] = page, total
            for it in items:
                if cfg["addr"] and cfg["addr"] not in str(it.get("addr", "")):
                    continue
                it["collected_at_kst"] = page_time
                it["source_page_no"] = page
                rows.append(it)
            if page * NUM_OF_ROWS >= total or not items:
                break
            page += 1
        if not rows:
            raise RuntimeError("저장할 행이 없음")
        day, hhmm = started.strftime("%Y-%m-%d"), started.strftime("%H%M")
        status_rows = [r for r in rows if to_kw(r.get("output")) >= MIN_STATUS_KW]
        write_csv_gz(f"data/{region}/status/{day}/{hhmm}.csv.gz", status_rows,
                     STATUS_COLS + ["collected_at_kst", "source_page_no"])
        static_path = f"data/{region}/static/{day}.csv.gz"
        if not os.path.exists(static_path):        # 정적 정보는 하루 1번만
            cols = sorted({k for r in rows for k in r})
            write_csv_gz(static_path, rows, cols)
        rec["rows_saved"] = len(status_rows)
    except Exception as e:
        rec["status"], rec["error"] = "fail", str(e)[:300]
    rec["run_finished_kst"] = now_kst().isoformat(timespec="seconds")
    append_log(region, rec)
    print(json.dumps(rec, ensure_ascii=False))
    return rec["status"] == "ok"


if __name__ == "__main__":
    key = os.environ.get("EV_API_KEY")
    if not key:
        sys.exit("EV_API_KEY 환경변수가 없습니다.")
    regions = sys.argv[1:] or ["daegu"]
    results = [collect(r, key) for r in regions]
    sys.exit(0 if any(results) else 1)            # 모든 지역 실패 시에만 실패 처리
