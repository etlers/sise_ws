"""
장마감 후 전용 종가 저장 모듈입니다.

이 모듈은 `preday_result.csv`를 갱신할 때만 사용합니다.
프리마켓(NXT) 수집 경로에서는 이 모듈을 사용하지 않으며,
프리마켓 처리 중에는 종가 파일을 읽거나 쓰지 않습니다.

주요 기능:
- 네이버 금융에서 종목별 당일 종가를 가져옵니다.
- 1차 실패 시 다른 네이버 일별 시세 페이지를 재시도합니다.
- 기존 `preday_result.csv`가 있으면 같은 날짜 행을 먼저 제거한 뒤 다시 저장합니다.
- 공유 데이터 디렉터리가 설정되어 있으면 공유 `preday_result.csv`도 함께 갱신합니다.
- 수집 실패 시 슬랙 관리자에게 알림을 보낼 수 있습니다.

주의할 점:
- 네이버 금융 HTML 구조가 바뀌면 파싱 CSS 선택자가 동작하지 않을 수 있습니다.
- 여러 프로세스가 동시에 같은 CSV를 갱신하면 덮어쓰기 충돌 가능성이 있습니다.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import csv
import logging
import os
import shutil

import requests
from bs4 import BeautifulSoup

from .config import DATA_DIR, StockItem, get_shared_data_root


# 이 모듈 전용 로거입니다.
# 수집 실패, 파싱 실패, 저장 결과 등을 기록해서 운영 중 문제를 추적할 때 사용합니다.
logger = logging.getLogger(__name__)

# 당일 종가를 저장하는 CSV 파일명입니다.
PREDAY_FILENAME = "preday_result.csv"

# preday_result.csv에 기록할 컬럼 순서입니다.
# stock_code : 6자리 종목코드
# date       : 기준 날짜, YYYYMMDD 형식
# close      : 해당 날짜의 종가
# index_name : 종목명 또는 지수명
PREDAY_COLUMNS = ["stock_code", "date", "close", "index_name"]


def _send_slack_admin_dm(message: str) -> bool:
    """
    슬랙 관리자에게 직접 메시지를 보냅니다.

    필요한 환경 변수:
    - SLACK_BOT_TOKEN: Slack Bot token
    - SLACK_ADMIN_USER_ID: DM을 받을 관리자 Slack user id

    둘 중 하나라도 없으면 알림을 보내지 않고 False를 반환합니다.
    """
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    admin_user_id = os.getenv("SLACK_ADMIN_USER_ID", "").strip()

    if not bot_token or not admin_user_id:
        logger.error(
            "slack admin dm skipped because SLACK_BOT_TOKEN or SLACK_ADMIN_USER_ID is missing"
        )
        return False

    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json; charset=utf-8",
    }

    try:
        open_response = requests.post(
            "https://slack.com/api/conversations.open",
            headers=headers,
            json={"users": admin_user_id},
            timeout=10,
        )
        open_response.raise_for_status()
        open_data = open_response.json()
        if not open_data.get("ok"):
            logger.error("slack conversations.open failed: %s", open_data)
            return False

        channel_id = str((open_data.get("channel") or {}).get("id") or "").strip()
        if not channel_id:
            logger.error("slack conversations.open returned empty channel id: %s", open_data)
            return False

        post_response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=headers,
            json={
                "channel": channel_id,
                "text": message,
                "unfurl_links": False,
                "unfurl_media": False,
            },
            timeout=10,
        )
        post_response.raise_for_status()
        post_data = post_response.json()
        if not post_data.get("ok"):
            logger.error("slack chat.postMessage failed: %s", post_data)
            return False

        return True
    except Exception:
        logger.exception("failed to send slack admin dm")
        return False


def preday_path(root: Path | None = None) -> Path:
    """
    preday_result.csv가 저장될 경로를 반환합니다.

    root가 전달되면 해당 디렉터리를 기준으로 파일 경로를 만들고,
    root가 없으면 기본 DATA_DIR 아래에 preday_result.csv를 만듭니다.

    이 함수는 파일을 직접 생성하지는 않지만, 상위 디렉터리가 없으면 미리 생성합니다.
    이후 CSV를 읽거나 쓸 때 경로가 항상 준비되어 있도록 하기 위함입니다.
    """
    # 사용자가 별도 루트 디렉터리를 넘기면 그 경로를 사용하고,
    # 아니면 설정 파일에 정의된 기본 데이터 디렉터리를 사용합니다.
    base_dir = root or DATA_DIR

    # CSV 저장 전에 디렉터리가 존재해야 하므로 없으면 생성합니다.
    # parents=True : 중간 경로까지 함께 생성
    # exist_ok=True : 이미 존재해도 오류를 내지 않음
    base_dir.mkdir(parents=True, exist_ok=True)

    # 최종 CSV 파일 경로를 반환합니다.
    return base_dir / PREDAY_FILENAME


def shared_preday_source_path() -> Path | None:
    """
    공유 데이터 루트에 이미 만들어진 preday_result.csv가 있으면 그 경로를 반환합니다.

    공유 파일은 여러 실행 환경 또는 여러 인스턴스가 같은 기준 종가 파일을 재사용하기 위한 용도입니다.
    로컬에 CSV가 없거나 새로 크롤링할 종목 수를 줄이고 싶을 때, 공유 파일을 먼저 복사해서 사용할 수 있습니다.

    반환값:
    - Path : 공유 preday_result.csv가 존재하는 경우
    - None : 공유 루트가 없거나 파일이 없는 경우
    """
    # 설정에서 공유 데이터 루트를 가져옵니다.
    # 공유 루트가 설정되어 있지 않으면 공유 파일을 사용할 수 없습니다.
    shared_root = get_shared_data_root()
    if shared_root is None:
        return None

    # 공유 루트 아래에 preday_result.csv가 있는지 확인합니다.
    candidate = shared_root / PREDAY_FILENAME
    if candidate.exists():
        return candidate

    # 공유 루트는 있지만 파일이 아직 만들어지지 않은 경우입니다.
    return None


def shared_preday_target_path() -> Path | None:
    """
    공유 preday_result.csv를 저장 대상으로 사용할 수 있는 경로를 반환합니다.

    source 함수와 달리 이 함수는 파일 존재 여부를 확인하지 않습니다.
    공유 루트가 설정되어 있다면, 이후 저장 시 해당 위치에 새 파일을 만들 수도 있기 때문입니다.

    주의:
    여러 프로세스가 동시에 같은 공유 파일에 쓰면 마지막 저장 결과가 이전 결과를 덮어쓸 수 있습니다.
    """
    shared_root = get_shared_data_root()
    if shared_root is None:
        return None
    return shared_root / PREDAY_FILENAME


def copy_shared_preday_source(target: Path | None = None) -> Path | None:
    """
    공유 preday_result.csv가 있으면 로컬 대상 경로로 복사합니다.

    이 함수는 본격적인 크롤링 전에 호출하는 초기화 성격의 함수입니다.
    공유 CSV를 먼저 복사해두면 이미 수집된 종목 데이터를 재사용할 수 있어서
    네이버 요청 횟수를 줄이고, 로컬 CSV가 비어 있는 상황에서도 일부 기준 데이터를 확보할 수 있습니다.

    반환값:
    - Path : 복사된 대상 파일 경로
    - None : 공유 원본 파일이 없어서 복사하지 않은 경우
    """
    # 공유 원본 파일이 실제로 존재하는지 확인합니다.
    source = shared_preday_source_path()
    if source is None:
        return None

    # 명시적인 target이 있으면 그 위치로 복사하고,
    # 없으면 기본 preday_result.csv 경로로 복사합니다.
    destination = target or preday_path()

    # 대상 파일의 상위 디렉터리가 없으면 생성합니다.
    destination.parent.mkdir(parents=True, exist_ok=True)

    # 메타데이터까지 가능한 보존하면서 파일을 복사합니다.
    shutil.copy2(source, destination)
    logger.info("copied shared preday file from %s to %s", source, destination)
    return destination


def _http_headers() -> dict[str, str]:
    """
    네이버 금융 요청에 공통으로 사용할 HTTP 헤더를 반환합니다.

    User-Agent를 브라우저처럼 지정해두면 단순 봇 요청으로 차단될 가능성을 줄일 수 있습니다.
    """
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }


def _daily_price_headers(stock_code: str) -> dict[str, str]:
    """
    네이버 금융 일별 시세 요청에 사용할 HTTP 헤더를 반환합니다.

    공통 헤더에 Referer와 Accept-Language를 추가합니다.
    Referer는 해당 종목의 네이버 금융 페이지에서 일별 시세를 조회한 것처럼 보이게 하며,
    Accept-Language는 한국어 페이지 기준의 응답을 받기 위한 보조 정보입니다.
    """
    # 공통 헤더를 먼저 가져온 뒤, 일별 시세 요청에 필요한 값을 추가합니다.
    headers = _http_headers()
    headers["Referer"] = f"https://finance.naver.com/item/sise.naver?code={stock_code}"
    headers["Accept-Language"] = "ko-KR,ko;q=0.9,en;q=0.8"
    return headers


def _fetch_daily_close(stock_code: str, preferred_date: str | None = None) -> dict[str, object] | None:
    """
    네이버 금융 일별 시세 페이지에서 종가를 가져옵니다.

    preferred_date가 있으면 해당 날짜의 행을 우선 찾습니다.
    preferred_date가 없거나 해당 날짜 행이 없으면 가장 최근의 유효한 시세 행을 반환합니다.

    이 함수는 collect_and_save_today_close_result()에서 1차 수집 경로로 사용됩니다.
    """
    url = f"https://finance.naver.com/item/sise_day.naver?code={stock_code}"

    # 일별 시세 전용 헤더를 사용해 요청합니다.
    response = requests.get(url, headers=_daily_price_headers(stock_code), timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # 일별 시세 테이블을 찾습니다.
    table = soup.select_one("table.type2")
    if table is None:
        logger.warning("[%s] daily price table not found", stock_code)
        return None

    rows: list[dict[str, object]] = []

    # 테이블의 각 행을 순회하면서 날짜와 종가를 추출합니다.
    for row in table.select("tr"):
        cells = row.find_all("td")

        # 네이버 일별 시세 테이블은 정상 데이터 행에 여러 개의 td가 있습니다.
        # td가 부족한 행은 헤더/구분선/빈 행일 가능성이 높으므로 건너뜁니다.
        if len(cells) < 7:
            continue

        date_text = cells[0].get_text(strip=True).replace(".", "").replace("/", "")
        close_text = cells[1].get_text(strip=True).replace(",", "")

        # 날짜 또는 종가가 비어 있으면 유효한 시세 행이 아닙니다.
        if not date_text or not close_text:
            continue

        # 날짜는 YYYYMMDD 형식만 허용합니다.
        if len(date_text) != 8 or not date_text.isdigit():
            continue

        try:
            close_value = int(close_text)
        except ValueError:
            # 종가가 숫자가 아니면 광고/빈 행/형식 변경 가능성이 있으므로 건너뜁니다.
            continue

        rows.append({
            "stock_code": stock_code.zfill(6),
            "date": date_text,
            "close": close_value,
        })

    # 파싱 가능한 유효 행이 하나도 없으면 실패 처리합니다.
    if not rows:
        logger.warning("[%s] daily price row not found", stock_code)
        return None

    # 지정한 거래일이 있으면 해당 날짜의 종가를 우선 반환합니다.
    if preferred_date:
        for row in rows:
            if str(row.get("date") or "") == preferred_date:
                return row

        # 지정 날짜 행이 없더라도 함수 전체를 실패시키지는 않습니다.
        # 아래에서 가장 최근 행을 대체값으로 반환합니다.
        logger.warning("[%s] daily price row for %s not found", stock_code, preferred_date)

    # 네이버 일별 시세 테이블은 보통 최신 날짜가 위에 있으므로 첫 번째 유효 행을 사용합니다.
    selected_row = rows[0]
    return selected_row


def _fetch_naver_daily_price(stock_code: str, preferred_date: str | None = None) -> dict[str, object] | None:
    """
    네이버 금융 sise.naver 페이지의 일별 시세 영역에서 종가를 가져오는 2차 대체 수집 함수입니다.

    _fetch_daily_close()가 실패했을 때 보조 경로로 사용합니다.
    sise_day.naver와 달리 종목 시세 페이지 안에 포함된 일별 시세 테이블을 파싱합니다.
    장후 데이터가 포함될 수 있어 일부 상황에서 더 최신 값이 잡힐 수 있습니다.
    """
    url = f"https://finance.naver.com/item/sise.naver?code={stock_code}"

    try:
        response = requests.get(url, headers=_daily_price_headers(stock_code), timeout=10)
        response.raise_for_status()
    except Exception as exc:
        # 2차 대체 경로이므로 실패해도 warning보다 낮은 debug로 남기고 None을 반환합니다.
        logger.debug("[%s] sise.naver fetch failed: %s", stock_code, exc)
        return None

    soup = BeautifulSoup(response.text, "html.parser")

    # sise.naver 페이지 안에서 일별 시세 테이블 후보를 모두 찾습니다.
    tables = soup.select("table.type2")
    if not tables:
        logger.debug("[%s] no daily price table found on sise.naver", stock_code)
        return None

    rows: list[dict[str, object]] = []

    # table.type2가 여러 개 있을 수 있으므로 모든 후보 테이블을 순회합니다.
    for table in tables:
        for row in table.select("tr"):
            cells = row.find_all("td")

            # 이 대체 파서는 1차 파서보다 조건을 느슨하게 둡니다.
            # 최소한 날짜와 종가 컬럼만 읽을 수 있으면 시도합니다.
            if len(cells) < 2:
                continue

            date_text = cells[0].get_text(strip=True).replace(".", "").replace("/", "")
            close_text = cells[1].get_text(strip=True).replace(",", "")

            if not date_text or not close_text:
                continue
            if len(date_text) != 8 or not date_text.isdigit():
                continue

            try:
                close_value = int(close_text)
            except ValueError:
                continue

            rows.append({
                "stock_code": stock_code.zfill(6),
                "date": date_text,
                "close": close_value,
            })

    if not rows:
        logger.debug("[%s] no valid daily price rows found on sise.naver", stock_code)
        return None

    # 지정 거래일이 있으면 그 날짜의 행을 우선 반환합니다.
    if preferred_date:
        for row in rows:
            if str(row.get("date") or "") == preferred_date:
                logger.debug("[%s] found daily price for %s from sise.naver", stock_code, preferred_date)
                return row

        # 지정 날짜가 없으면 가장 최근 행으로 대체합니다.
        logger.debug("[%s] daily price row for %s not found on sise.naver", stock_code, preferred_date)

    selected_row = rows[0]
    logger.debug("[%s] using most recent daily price from sise.naver: %s", stock_code, selected_row.get("date"))
    return selected_row


def _load_existing_rows(path: Path) -> list[dict[str, object]]:
    """
    기존 preday_result.csv를 읽어서 유효한 행만 리스트로 반환합니다.

    CSV가 수동 수정되었거나 일부 값이 깨져 있을 수 있으므로,
    종목코드/날짜/종가가 최소한의 형식을 만족하는 행만 살립니다.
    """
    # 파일이 없으면 기존 데이터가 없는 것으로 보고 빈 리스트를 반환합니다.
    if not path.exists():
        return []

    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            # 종목코드는 문자열로 정리한 뒤 6자리로 맞춥니다.
            stock_code = str(row.get("stock_code") or "").strip().zfill(6)

            # 날짜는 하이픈을 제거해서 YYYYMMDD 형식에 가깝게 정리합니다.
            date_text = str(row.get("date") or "").strip().replace("-", "")

            # 종가는 숫자 변환 전 문자열로 정리합니다.
            close_text = str(row.get("close") or "").strip()
            index_name = str(row.get("index_name") or "").strip()

            # 필수값이 하나라도 없으면 해당 행은 사용할 수 없습니다.
            if not stock_code or not date_text or not close_text:
                continue

            try:
                # "12,345" 또는 "12345.0" 같은 값도 최대한 정수로 복구합니다.
                close_value = int(float(close_text.replace(",", "")))
            except ValueError:
                continue

            rows.append(
                {
                    "stock_code": stock_code,
                    "date": date_text,
                    "close": close_value,
                    "index_name": index_name,
                }
            )
    return rows


def _parse_trade_file_date(path: Path, fallback_date: str | None = None) -> str | None:
    """
    거래 CSV 파일명에서 날짜를 추출합니다.

    예를 들어 파일명이 trade_data_20240620.csv처럼 끝에 8자리 날짜를 포함하면
    20240620을 반환합니다.

    파일명에서 날짜를 찾지 못했고 fallback_date가 주어지면 fallback_date를 반환합니다.
    """
    stem = path.stem

    # 파일명 마지막 언더스코어 뒤쪽을 날짜 후보로 봅니다.
    if "_" in stem:
        maybe_date = stem.rsplit("_", 1)[-1]
        if len(maybe_date) == 8 and maybe_date.isdigit():
            return maybe_date

    # 파일명에서 날짜를 찾지 못한 경우 호출자가 준 대체 날짜를 사용합니다.
    if fallback_date:
        return fallback_date
    return None


def _trade_files(root: Path) -> list[Path]:
    """
    지정한 루트 디렉터리에서 거래 데이터 CSV 파일 목록을 수집합니다.

    orderbook 파일은 호가 데이터일 가능성이 높으므로 제외합니다.
    루트 바로 아래 CSV뿐 아니라 backup/YYYYMMDD/ 같은 하위 백업 디렉터리의 CSV도 함께 탐색합니다.
    """
    files: list[Path] = []

    # 루트 디렉터리 바로 아래에 있는 일반 거래 CSV 파일을 수집합니다.
    if root.exists():
        files.extend(sorted(p for p in root.glob("*.csv") if "_orderbook" not in p.stem))

    # backup 하위 디렉터리에 날짜별로 저장된 CSV도 함께 수집합니다.
    backup_root = root / "backup"
    if backup_root.exists():
        for day_dir in sorted(p for p in backup_root.iterdir() if p.is_dir()):
            files.extend(sorted(p for p in day_dir.glob("*.csv") if "_orderbook" not in p.stem))
    return files


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    """
    종가 데이터 행 목록을 CSV 파일로 저장합니다.

    저장 전에 상위 디렉터리를 생성하고, PREDAY_COLUMNS 순서대로 헤더와 데이터를 기록합니다.
    종목코드는 항상 6자리 문자열로, 종가는 정수로 저장합니다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=PREDAY_COLUMNS, quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()

        for row in rows:
            # 외부에서 들어온 row 값의 타입이 섞여 있어도 CSV 저장 형식을 일정하게 맞춥니다.
            writer.writerow(
                {
                    "stock_code": str(row.get("stock_code") or "").zfill(6),
                    "date": str(row.get("date") or ""),
                    "close": int(row.get("close") or 0),
                    "index_name": str(row.get("index_name") or ""),
                }
            )


def _merge_existing_rows(targets: list[Path]) -> dict[tuple[str, str], dict[str, object]]:
    """
    여러 CSV 대상 파일에 이미 들어 있는 데이터를 하나의 딕셔너리로 병합합니다.

    키는 (stock_code, date)입니다.
    같은 종목/같은 날짜 데이터가 여러 파일에 있으면 나중에 읽은 값이 앞의 값을 덮어씁니다.

    이 구조를 사용하면 새로 수집한 데이터도 같은 키로 덮어써서 중복 행을 방지할 수 있습니다.
    """
    merged: dict[tuple[str, str], dict[str, object]] = {}
    for target in targets:
        for row in _load_existing_rows(target):
            key = (str(row["stock_code"]), str(row["date"]))
            merged[key] = row
    return merged


def _drop_rows_for_date(rows: dict[tuple[str, str], dict[str, object]], target_date: str) -> int:
    """
    병합된 row dict에서 특정 날짜의 행을 모두 제거합니다.

    같은 날짜가 이미 들어 있는 상태에서 다시 저장할 때 기존 행을 먼저 비워두면
    당일 종가가 중복되거나 잘못 누적되는 일을 막을 수 있습니다.
    """
    removed = 0
    for key, row in list(rows.items()):
        if str(row.get("date") or "") == target_date:
            del rows[key]
            removed += 1
    return removed


def _write_rows_to_targets(targets: list[Path], rows: list[dict[str, object]]) -> None:
    """
    동일한 행 목록을 여러 대상 CSV 파일에 저장합니다.

    로컬 preday_result.csv와 공유 preday_result.csv를 동시에 갱신할 때 사용합니다.
    """
    for target in targets:
        _write_rows(target, rows)


def collect_and_save_today_close_result(
    stock_items: list[StockItem],
    trade_date: date,
    target: Path | None = None,
) -> dict[str, object]:
    """
    지정한 거래일의 당일 종가를 종목별로 수집해서 preday_result.csv에 저장합니다.

    이 함수는 '오늘 장이 끝난 뒤 오늘 종가를 저장'하는 목적입니다.

    수집 우선순위:
    1. 네이버 sise_day.naver 일별 시세 페이지에서 지정 거래일 종가 조회
    2. 실패하면 네이버 sise.naver 페이지의 일별 시세 영역에서 재시도
    3. 둘 다 실패하면 즉시 오류를 올리고 슬랙 관리자에게 알립니다.

    저장 방식:
    - 기존 로컬/공유 CSV를 먼저 읽어 병합합니다.
    - 새로 수집한 행은 (종목코드, 날짜) 키 기준으로 기존 값을 덮어씁니다.
    - 수집 중 실패가 하나라도 있으면 저장하지 않고 오류를 올립니다.
    """
    # 기본 저장 대상입니다.
    # target이 주어지면 그 위치에 저장하고, 없으면 기본 preday_result.csv 경로에 저장합니다.
    primary_target = target or preday_path()
    targets = [primary_target]

    # 공유 저장 경로가 설정되어 있고, 로컬 저장 경로와 다르면 저장 대상에 추가합니다.
    # 성공했을 때만 최종 저장하도록, 기존 데이터는 메모리에서만 병합합니다.
    shared_target = shared_preday_target_path()
    if shared_target is not None and shared_target != primary_target:
        targets.append(shared_target)

    # 기존 CSV 데이터들을 먼저 메모리에서 합칩니다.
    # 이후 새로 가져온 데이터가 같은 종목/날짜 키로 덮어쓰게 됩니다.
    existing_targets = [primary_target]
    if shared_target is not None and shared_target != primary_target:
        existing_targets.append(shared_target)
    merged = _merge_existing_rows(existing_targets)

    # 같은 거래일 데이터가 이미 있으면 먼저 제거한 뒤 새 종가를 저장합니다.
    # 이렇게 하면 장마감 후 재실행되거나 이전 실행의 잔재가 남아 있어도
    # 당일 행이 누적되지 않고 항상 최신 값으로 교체됩니다.
    trade_date_text = trade_date.strftime("%Y%m%d")
    removed_rows = _drop_rows_for_date(merged, trade_date_text)
    if removed_rows:
        logger.info("removed %s existing rows for trade date %s", removed_rows, trade_date_text)

    # 실제로 이번 실행에서 새로 수집에 성공한 행 수입니다.
    fetched_rows = 0

    # 최종적으로 어떤 종목이 실패했는지 호출자에게 알려주기 위한 목록입니다.
    failed_codes: list[str] = []

    # 종목코드로 종목명을 빠르게 찾기 위한 매핑입니다.
    # CSV 저장 시 index_name 컬럼을 채우는 데 사용합니다.
    stock_name_by_code = {
        str(item.code or "").strip().zfill(6): item.name
        for item in stock_items
        if str(item.code or "").strip()
    }

    for item in stock_items:
        # 종목코드는 항상 6자리 문자열로 정규화합니다.
        code = str(item.code or "").strip().zfill(6)
        if not code:
            # 코드가 비어 있으면 수집할 수 없으므로 조용히 건너뜁니다.
            continue

        row = None

        # 1차 시도: 네이버 일별 시세 페이지에서 지정 거래일 종가를 가져옵니다.
        try:
            row = _fetch_daily_close(code, trade_date.strftime("%Y%m%d"))
        except Exception as exc:
            # 네트워크 오류, HTTP 오류, HTML 파싱 중 예외가 모두 여기로 올 수 있습니다.
            # 다음 대체 경로를 계속 시도해야 하므로 예외를 다시 던지지 않습니다.
            logger.warning("[%s] daily price crawl failed: %s", code, exc)

        # 주요 분기점 1:
        # 1차 수집이 실패하면 sise.naver 페이지의 일별 시세 섹션을 2차로 확인합니다.
        # 이 경로는 HTML 구조가 조금 다르거나 장후 데이터가 반영된 경우에 도움이 됩니다.
        if row is None:
            row = _fetch_naver_daily_price(code, trade_date.strftime("%Y%m%d"))

        # 주요 분기점 2:
        # 네이버의 두 경로가 모두 실패하면 이 종목은 실패 목록에 기록하고 다음 종목으로 넘어갑니다.
        if row is None:
            failed_codes.append(code)
            continue

        # 수집 성공 행에 종목명을 채웁니다.
        # 매핑에 없으면 현재 StockItem의 이름을 사용합니다.
        row["index_name"] = stock_name_by_code.get(code, item.name)

        # 같은 종목/날짜 조합은 하나만 유지합니다.
        # 새로 수집한 데이터가 기존 CSV 데이터보다 우선합니다.
        key = (str(row["stock_code"]), str(row["date"]))
        merged[key] = row
        fetched_rows += 1

    # 저장 전 정렬합니다.
    # 날짜 오름차순, 같은 날짜 안에서는 종목코드 오름차순으로 정렬해 CSV diff와 확인이 쉬워집니다.
    ordered_rows = sorted(
        merged.values(),
        key=lambda item: (str(item.get("date") or ""), str(item.get("stock_code") or "")),
    )

    if failed_codes:
        failure_message = (
            "preday today-close crawl failed "
            f"date={trade_date.isoformat()} "
            f"failed_rows={len(failed_codes)} "
            f"failed_codes={','.join(failed_codes)}"
        )
        _send_slack_admin_dm(failure_message)
        raise RuntimeError(failure_message)

    if not ordered_rows:
        failure_message = (
            "preday today-close crawl returned no rows "
            f"date={trade_date.isoformat()}"
        )
        _send_slack_admin_dm(failure_message)
        raise RuntimeError(failure_message)

    _write_rows_to_targets(targets, ordered_rows)
    logger.info(
        "today close synced to preday_result.csv path=%s shared=%s rows=%s fetched=%s failed=%s",
        primary_target,
        shared_target if shared_target is not None and shared_target != primary_target else "",
        len(ordered_rows),
        fetched_rows,
        len(failed_codes),
    )

    # 호출자가 저장 결과와 실패 종목을 확인할 수 있도록 요약 정보를 반환합니다.
    return {
        "path": str(primary_target),
        "shared_path": str(shared_target) if shared_target is not None and shared_target != primary_target else "",
        "rows": len(ordered_rows),
        "fetched_rows": fetched_rows,
        "failed_rows": len(failed_codes),
        "failed_codes": failed_codes,
    }
