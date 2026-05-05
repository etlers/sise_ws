from __future__ import annotations

# 스케줄러에서 사용하는 단순 데이터 구조를 만들기 위해 dataclass를 사용합니다.
from dataclasses import dataclass
# 날짜/시간 계산을 위해 표준 datetime 모듈의 필요한 타입만 가져옵니다.
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import csv
import json
import logging
import signal
# datetime.time과 이름이 겹치지 않도록 time 모듈은 별칭으로 import합니다.
import time as time_module

# 프로젝트 공통 설정 경로, 데이터 저장 경로, 종목 리스트 로딩 함수입니다.
from .config import CONFIG_DIR, DATA_DIR, load_stock_list
# 승인키 갱신 함수와 실제 시세 수집을 1회 수행하는 함수입니다.
from .ingest import refresh_approval_key, run_once
# 장마감 후 당일 종가 파일만 갱신하는 함수입니다.
from .preday import collect_and_save_today_close_result
# 수집된 CSV 파일을 날짜 기준으로 보관/정리하는 함수입니다.
from .storage import archive_csv_files


# 이 모듈 전용 로거입니다.
# 실제 출력 여부와 포맷은 애플리케이션의 logging 설정에 따릅니다.
logger = logging.getLogger(__name__)


# 모든 스케줄 판단은 한국 주식시장 기준이므로 UTC+9, 즉 서울 시간대로 고정합니다.
SEOUL_TZ = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class SessionWindow:
    """하나의 시장 세션 시작/종료 시간을 담는 불변 데이터 클래스입니다."""

    # 해당 세션의 시작 시각입니다. 예: 08:00:00
    start_tm: time
    # 해당 세션의 종료 시각입니다. 예: 15:30:00
    end_tm: time


@dataclass(frozen=True)
class DealWindow:
    """NXT/정규장 수집 시간과 앞뒤 여유 시간을 한 번에 담는 설정 객체입니다."""

    # NXT 또는 프리마켓 구간의 시작/종료 시간입니다.
    nxt: SessionWindow
    # KRX 정규장 구간의 시작/종료 시간입니다.
    krx: SessionWindow
    # 실제 시작 시간보다 몇 분 먼저 수집을 시작할지 결정하는 값입니다.
    pre_minute: int
    # 정규장 종료 후 몇 분 뒤 백업을 시작할지 결정하는 값입니다.
    post_minute: int


def load_deal_window(path: Path | None = None) -> DealWindow:
    """deal_tm.json에서 NXT/정규장 수집 시간 설정을 읽어 DealWindow로 변환합니다."""

    # path가 별도로 주어지면 그 파일을 사용하고, 없으면 기본 설정 파일을 사용합니다.
    target = path or (CONFIG_DIR / "deal_tm.json")

    # JSON 설정 파일을 UTF-8로 읽어 파이썬 dict로 변환합니다.
    data = json.loads(target.read_text(encoding="utf-8"))

    # 설정 파일에서 nxt/krx 블록을 꺼냅니다.
    # 값이 없을 경우 빈 dict를 사용하지만, 아래에서 필수 키 접근 시 KeyError가 발생할 수 있습니다.
    # 즉, start_tm/end_tm은 반드시 설정 파일에 존재해야 하는 값입니다.
    nxt = data.get("nxt") or {}
    krx = data.get("krx") or {}

    # 문자열 시각을 time 객체로 변환하여 스케줄러가 계산 가능한 형태로 반환합니다.
    return DealWindow(
        nxt=SessionWindow(
            start_tm=_parse_time(nxt["start_tm"]),
            end_tm=_parse_time(nxt["end_tm"]),
        ),
        krx=SessionWindow(
            start_tm=_parse_time(krx["start_tm"]),
            end_tm=_parse_time(krx["end_tm"]),
        ),
        # pre_minute/post_minute는 없으면 0으로 처리하여 추가 여유 시간을 두지 않습니다.
        pre_minute=int(data.get("pre_minute") or 0),
        post_minute=int(data.get("post_minute") or 0),
    )


def load_holiday_dates(path: Path | None = None) -> set[date]:
    """holiday.csv에서 휴장일 목록을 읽어 date 집합으로 반환합니다."""

    # path가 별도로 주어지면 해당 파일을 사용하고, 없으면 기본 휴장일 CSV를 사용합니다.
    target = path or (CONFIG_DIR / "holiday.csv")
    holidays: set[date] = set()

    # utf-8-sig를 사용해 BOM이 포함된 CSV도 안전하게 읽습니다.
    with target.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            # CSV의 date 컬럼을 읽습니다. 비어 있거나 공백이면 무시합니다.
            raw = (row.get("date") or "").strip()
            if not raw:
                continue

            # YYYY-MM-DD 형식의 문자열을 date 객체로 변환하여 휴장일 집합에 추가합니다.
            holidays.add(date.fromisoformat(raw))
    return holidays


def _parse_time(raw: str) -> time:
    """HH:MM:SS 형식 문자열을 time 객체로 변환합니다."""

    return datetime.strptime(raw, "%H:%M:%S").time()


def is_market_day(day: date, holidays: set[date]) -> bool:
    """주어진 날짜가 실제 거래일인지 판단합니다."""

    # weekday(): 월요일=0, ..., 금요일=4, 토요일=5, 일요일=6 입니다.
    # 따라서 5 미만이면 평일이고, 휴장일 목록에 없으면 거래일로 판단합니다.
    return day.weekday() < 5 and day not in holidays


def next_market_day(day: date, holidays: set[date]) -> date:
    """주어진 날짜부터 시작해 가장 가까운 다음 거래일을 찾습니다."""

    current = day

    # 현재 날짜가 주말 또는 휴장일이면 하루씩 뒤로 이동합니다.
    # 거래일을 찾을 때까지 반복합니다.
    while not is_market_day(current, holidays):
        current += timedelta(days=1)
    return current


def _combine(day: date, tm: time) -> datetime:
    """date와 time을 서울 시간대가 포함된 datetime으로 합칩니다."""

    return datetime.combine(day, tm, tzinfo=SEOUL_TZ)


def _shifted_start(day: date, tm: time, pre_minute: int) -> datetime:
    """세션 시작 시각에서 pre_minute만큼 앞당긴 실제 수집 시작 시각을 계산합니다."""

    # pre_minute가 음수로 들어오더라도 시작 시간이 뒤로 밀리지 않도록 0 이상만 허용합니다.
    return _combine(day, tm) - timedelta(minutes=max(pre_minute, 0))


def sleep_until(target: datetime, stop_flag: list[bool]) -> None:
    """목표 시각까지 대기하되, 종료 신호가 들어오면 즉시 빠져나올 수 있게 합니다."""

    # stop_flag[0]이 True가 되면 외부 종료 신호가 들어온 것이므로 대기를 중단합니다.
    while not stop_flag[0]:
        # 현재 서울 시간 기준으로 목표 시각까지 남은 초를 계산합니다.
        remaining = (target - datetime.now(SEOUL_TZ)).total_seconds()

        # 이미 목표 시각에 도달했거나 지난 경우 더 이상 잠들지 않고 반환합니다.
        if remaining <= 0:
            return

        # 너무 오래 한 번에 sleep하지 않고 최대 30초 단위로 나눠 잡니다.
        # 이렇게 하면 SIGTERM/SIGINT로 stop_flag가 바뀌었을 때 비교적 빠르게 반응할 수 있습니다.
        time_module.sleep(min(30.0, max(1.0, remaining)))


def _describe_day(day: date, holidays: set[date]) -> str:
    """로그에 표시할 날짜 상태 문구를 반환합니다."""

    # 주말 여부를 먼저 판단합니다.
    if day.weekday() >= 5:
        return "주말"

    # 평일이더라도 휴장일 CSV에 포함되어 있으면 휴장일로 표시합니다.
    if day in holidays:
        return "휴장일"

    # 주말도 휴장일도 아니면 거래일입니다.
    return "거래일"


def _log_day_schedule(day: date, holidays: set[date], window: DealWindow) -> None:
    """하루에 한 번, 해당 날짜의 실행 예정 스케줄을 로그로 출력합니다."""

    # 오늘이 거래일인지, 주말/휴장일인지 사람이 읽기 쉬운 문구로 변환합니다.
    status = _describe_day(day, holidays)
    market_day = is_market_day(day, holidays)
    logger.info("today schedule day=%s status=%s", day.isoformat(), status)

    # 주요 분기점 1: 오늘이 거래일이 아닌 경우
    # - 시세 수집, 승인키 갱신, 백업을 수행하지 않습니다.
    # - 다음 거래일만 로그로 안내하고 함수 실행을 종료합니다.
    if not market_day:
        next_day = next_market_day(day + timedelta(days=1), holidays)
        logger.info("  - next market day: %s", next_day.isoformat())
        return

    # 오늘이 거래일이면 각 단계별 실행 예정 시각을 계산합니다.
    # NXT 수집 시작은 설정된 시작 시각보다 pre_minute만큼 앞당겨집니다.
    nxt_start = _shifted_start(day, window.nxt.start_tm, window.pre_minute)
    # 승인키는 NXT 시작 10분 전에 미리 갱신합니다.
    approval_refresh_at = _combine(day, window.nxt.start_tm) - timedelta(minutes=10)
    # NXT 수집 종료 시각입니다.
    nxt_end = _combine(day, window.nxt.end_tm)
    # KRX 정규장 수집 시작도 설정된 시작 시각보다 pre_minute만큼 앞당겨집니다.
    krx_start = _shifted_start(day, window.krx.start_tm, window.pre_minute)
    # KRX 정규장 수집 종료 시각입니다.
    krx_end = _combine(day, window.krx.end_tm)
    # 백업은 정규장 종료 후 post_minute만큼 지난 시각에 시작합니다.
    archive_at = krx_end + timedelta(minutes=max(window.post_minute, 0))

    # 계산된 하루 실행 계획을 로그에 남깁니다.
    logger.info("  - %s 승인키 미리 갱신 (10분 전)", approval_refresh_at.strftime("%H:%M"))
    logger.info("  - %s 프리마켓 시세 추출 시작 (nxt)", nxt_start.strftime("%H:%M"))
    logger.info("    * 종료: %s", nxt_end.strftime("%H:%M"))
    logger.info("  - %s 정규장 시세 추출 시작 (krx)", krx_start.strftime("%H:%M"))
    logger.info("    * 종료: %s", krx_end.strftime("%H:%M"))
    logger.info("  - %s 백업 시작 (nxt, krx)", archive_at.strftime("%H:%M"))


def _archive_market_data(day: date) -> dict[str, int]:
    """KRX/NXT CSV 파일을 백업하고, 백업된 파일 개수를 반환합니다."""

    # 정규장 데이터 폴더를 대상으로 archive_date 기준 백업을 수행합니다.
    # keep_days=20이므로 보관 정책은 최근 20일 기준으로 관리됩니다.
    archived_krx = archive_csv_files(DATA_DIR / "krx", archive_date=day, keep_days=20)

    # NXT 데이터 폴더도 동일한 기준으로 백업합니다.
    archived_nxt = archive_csv_files(DATA_DIR / "nxt", archive_date=day, keep_days=20)

    # 백업 결과를 로그로 남깁니다.
    logger.info(
        "archived files for %s (krx=%s, nxt=%s)",
        day.isoformat(),
        len(archived_krx),
        len(archived_nxt),
    )

    # 후속 로그에서 쓰기 쉽도록 시장별/전체 백업 파일 수를 dict로 반환합니다.
    return {
        "krx": len(archived_krx),
        "nxt": len(archived_nxt),
        "total": len(archived_krx) + len(archived_nxt),
    }


def _log_stage_done(stage: str, day: date, finished_at: datetime, summary: str = "") -> None:
    """각 처리 단계가 끝났을 때 공통 형식으로 종료 로그를 남깁니다."""

    # summary가 있으면 앞에 공백을 붙여 로그 문장 뒤에 자연스럽게 이어 붙입니다.
    suffix = f" {summary}" if summary else ""
    logger.info(
        "%s 종료 - %s %s%s",
        stage,
        day.isoformat(),
        finished_at.strftime("%H:%M:%S"),
        suffix,
    )


def _log_stage_files(stage: str, report: dict[str, object]) -> None:
    """run_once 실행 결과에 포함된 파일별 요약 정보를 로그로 출력합니다."""

    # run_once가 반환한 report에서 파일별 요약 목록을 꺼냅니다.
    file_summaries = report.get("file_summaries") or []

    # 주요 분기점: 파일 요약이 없거나 리스트 형식이 아니면 상세 로그를 생략합니다.
    if not isinstance(file_summaries, list) or not file_summaries:
        logger.info("%s file details: none", stage)
        return

    logger.info("%s file details:", stage)
    for item in file_summaries:
        # 방어 코드: 리스트 안에 dict가 아닌 값이 섞여 있으면 무시합니다.
        if not isinstance(item, dict):
            continue

        # 파일명, 최초 시각, 마지막 시각, 행 수를 문자열/정수로 정리합니다.
        filename = str(item.get("filename") or "")
        first_time = str(item.get("first_time") or "")
        last_time = str(item.get("last_time") or "")
        rows = int(item.get("rows") or 0)
        logger.info("  - %s first=%s last=%s rows=%s", filename, first_time, last_time, rows)


def run_scheduler() -> None:
    """거래일 스케줄에 맞춰 승인키 갱신, NXT 수집, KRX 수집, 백업을 반복 실행합니다."""

    # 휴장일 목록을 읽어 거래일 판단에 사용합니다.
    holidays = load_holiday_dates()
    # NXT/정규장 시작·종료 시각 및 앞뒤 여유 시간을 읽습니다.
    window = load_deal_window()
    # 종가 결과 생성 단계에서 사용할 종목 리스트를 미리 로드합니다.
    stock_items = load_stock_list()

    # signal handler 내부에서 값을 바꿀 수 있도록 list로 감싼 종료 플래그입니다.
    # bool 변수만 쓰면 중첩 함수에서 재할당 처리가 번거로워지므로 list[bool] 형태를 사용합니다.
    stop_flag = [False]

    # 같은 날짜의 스케줄 안내 로그가 반복 출력되지 않도록 마지막 로그 출력 날짜를 기억합니다.
    last_logged_day: date | None = None

    # 스케줄러 시작 시 전체 설정을 한 번 로그로 남깁니다.
    logger.info(
        "scheduler started nxt=%s~%s krx=%s~%s pre_minute=%s post_minute=%s holidays=%s",
        window.nxt.start_tm.isoformat(),
        window.nxt.end_tm.isoformat(),
        window.krx.start_tm.isoformat(),
        window.krx.end_tm.isoformat(),
        window.pre_minute,
        window.post_minute,
        len(holidays),
    )

    def _stop_handler(signum, frame) -> None:  # type: ignore[unused-argument]
        """SIGTERM/SIGINT를 받으면 스케줄러 루프가 안전하게 종료되도록 표시합니다."""

        # Docker, systemd, 터미널 Ctrl+C 등으로 종료 신호가 들어오면 이 함수가 호출됩니다.
        # 즉시 프로세스를 강제 종료하지 않고 stop_flag를 바꿔 현재 대기/루프가 자연스럽게 끝나도록 합니다.
        logger.info("received signal %s, stopping scheduler", signum)
        stop_flag[0] = True

    # 컨테이너/운영체제 종료 신호와 Ctrl+C 인터럽트를 모두 처리합니다.
    signal.signal(signal.SIGTERM, _stop_handler)
    signal.signal(signal.SIGINT, _stop_handler)

    # 메인 루프입니다.
    # stop_flag가 True가 되기 전까지 매일 스케줄을 계산하고 필요한 단계만 실행합니다.
    while not stop_flag[0]:
        # 모든 기준 날짜는 서울 시간 기준의 오늘 날짜입니다.
        today = datetime.now(SEOUL_TZ).date()

        # 주요 분기점: 날짜가 바뀌었을 때만 오늘의 실행 계획을 로그로 출력합니다.
        # 이 처리가 없으면 루프가 돌 때마다 같은 스케줄 로그가 계속 찍힐 수 있습니다.
        if last_logged_day != today:
            _log_day_schedule(today, holidays, window)
            last_logged_day = today

        # 오늘 또는 오늘 이후의 가장 가까운 거래일을 찾습니다.
        market_day = next_market_day(today, holidays)

        # 주요 분기점: 오늘이 거래일이 아닌 경우
        # - 오늘은 아무 수집도 하지 않습니다.
        # - 다음 거래일 00:00까지 대기한 뒤 루프를 다시 돌면서 새 날짜 기준으로 판단합니다.
        if market_day > today:
            logger.info("today is not a market day, sleeping until %s", market_day.isoformat())
            sleep_until(_combine(market_day, time(0, 0)), stop_flag)
            continue

        # 오늘이 거래일이면 오늘 날짜 기준으로 각 단계별 기준 시각을 계산합니다.
        nxt_start = _shifted_start(today, window.nxt.start_tm, window.pre_minute)
        approval_refresh_at = _combine(today, window.nxt.start_tm) - timedelta(minutes=10)
        nxt_end = _combine(today, window.nxt.end_tm)
        krx_start = _shifted_start(today, window.krx.start_tm, window.pre_minute)
        krx_end = _combine(today, window.krx.end_tm)
        now = datetime.now(SEOUL_TZ)

        # 주요 분기점: 승인키 갱신 예정 시각보다 아직 이른 경우
        # - 아직 할 일이 없으므로 승인키 갱신 시각까지 잠듭니다.
        # - 깨어난 뒤 continue로 루프 처음으로 돌아가 현재 시각을 다시 계산합니다.
        if now < approval_refresh_at:
            logger.info("waiting for approval key refresh at %s", approval_refresh_at.isoformat())
            sleep_until(approval_refresh_at, stop_flag)
            continue

        # 주요 분기점: 승인키 갱신 시각은 지났지만 NXT 수집 시작 전인 경우
        # - NXT 수집 전에 승인키를 미리 갱신합니다.
        # - 승인키 갱신 실패가 발생해도 스케줄러 전체를 죽이지 않고 로그만 남긴 뒤 계속 진행합니다.
        if now < nxt_start:
            logger.info("starting approval key refresh at %s", now.isoformat())
            try:
                refresh_approval_key()
            except Exception:
                logger.exception("approval key refresh failed")

            # 승인키 갱신 시도 후 NXT 수집 시작 시각까지 대기합니다.
            logger.info("waiting for nxt start at %s", nxt_start.isoformat())
            sleep_until(nxt_start, stop_flag)
            continue

        # 주요 분기점: 현재 시각이 NXT 수집 구간 안에 있는 경우
        # - session_name="nxt"로 프리마켓/NXT 데이터를 수집합니다.
        # - 이 구간에서는 preday_result.csv를 읽거나 갱신하지 않습니다.
        # - run_once 내부에서 stop_at까지 반복 수집하는 구조로 보입니다.
        if now < nxt_end:
            logger.info(
                "starting nxt ingest for %s window=%s~%s",
                today.isoformat(),
                nxt_start.isoformat(),
                nxt_end.isoformat(),
            )
            try:
                # 승인키는 앞 단계에서 이미 갱신했으므로 refresh_approval=False로 실행합니다.
                report = run_once(refresh_approval=False, stop_at=nxt_end, session_name="nxt")

                # 수집된 파일별 요약과 전체 처리 결과를 로그로 남깁니다.
                _log_stage_files("프리마켓", report)
                _log_stage_done(
                    "프리마켓 시세 추출",
                    today,
                    datetime.now(SEOUL_TZ),
                    (
                        f"files={report.get('files_written', 0)} "
                        f"rows={report.get('rows_written', 0)} "
                        f"orderbook_rows={report.get('orderbook_rows_written', 0)}"
                    ),
                )
                logger.info("프리마켓 종료, 정규장 시작 대기 at %s", krx_start.strftime("%H:%M"))
            except Exception:
                # NXT 수집 중 예외가 발생해도 프로세스를 종료하지 않습니다.
                # 10초 대기 후 루프를 다시 돌며 현재 시간 기준으로 다음 행동을 결정합니다.
                logger.exception("nxt ingest failed")
                time_module.sleep(10)
                continue

        # 백업 시작 시각은 정규장 종료 시각에 post_minute를 더해 계산합니다.
        archive_at = krx_end + timedelta(minutes=max(window.post_minute, 0))
        now = datetime.now(SEOUL_TZ)

        # 주요 분기점: NXT 수집이 끝났거나 NXT 시간이 지난 뒤, 아직 KRX 시작 전인 경우
        # - 정규장 수집 시작 시각까지 대기합니다.
        if now < krx_start:
            logger.info("waiting for krx start at %s", krx_start.isoformat())
            sleep_until(krx_start, stop_flag)
            continue

        now = datetime.now(SEOUL_TZ)

        # 주요 분기점: 현재 시각이 KRX 정규장 수집 구간 안에 있는 경우
        # - session_name="krx"로 정규장 데이터를 수집합니다.
        if now < krx_end:
            logger.info(
                "starting krx ingest for %s window=%s~%s",
                today.isoformat(),
                krx_start.isoformat(),
                krx_end.isoformat(),
            )
            try:
                # 정규장 수집도 승인키 재갱신 없이 현재 승인키를 사용합니다.
                report = run_once(refresh_approval=False, stop_at=krx_end, session_name="krx")

                # 정규장 수집 결과는 체결 데이터와 호가 데이터 행 수를 함께 기록합니다.
                _log_stage_files("정규장", report)
                _log_stage_done(
                    "정규장 시세 추출",
                    today,
                    datetime.now(SEOUL_TZ),
                    (
                        f"files={report.get('files_written', 0)} "
                        f"rows={report.get('rows_written', 0)} "
                        f"trade_rows={report.get('trade_rows_written', 0)} "
                        f"orderbook_rows={report.get('orderbook_rows_written', 0)}"
                    ),
                )
                logger.info("정규장 종료, 백업 시작 대기 at %s", archive_at.strftime("%H:%M"))
            except Exception:
                # KRX 수집 중 예외가 발생해도 스케줄러는 계속 유지합니다.
                # 10초 후 루프를 다시 돌며 남은 수집 시간 또는 백업 시점을 재판단합니다.
                logger.exception("krx ingest failed")
                time_module.sleep(10)
                continue

        now = datetime.now(SEOUL_TZ)

        # 주요 분기점: 정규장 수집은 끝났지만 백업 시작 시각 전인 경우
        # - post_minute로 지정된 여유 시간이 끝날 때까지 대기합니다.
        # - 이 여유 시간은 장 종료 직후 파일 저장 지연 등을 고려한 완충 시간으로 볼 수 있습니다.
        if now < archive_at:
            logger.info("waiting for backup at %s", archive_at.isoformat())
            sleep_until(archive_at, stop_flag)
            continue

        # 주요 분기점: 백업 시각에 도달한 경우
        # - 당일 종가 결과 파일을 먼저 생성/갱신합니다.
        # - 이 단계는 프리마켓과 무관하고 장마감 후에만 실행됩니다.
        # - 이후 KRX/NXT CSV 파일을 백업/정리합니다.
        logger.info("starting backup for %s at %s", today.isoformat(), datetime.now(SEOUL_TZ).isoformat())

        # 당일 종가 기반 결과 파일을 생성합니다.
        # stock_items: 대상 종목 목록
        # today: 기준 거래일
        preday_report = collect_and_save_today_close_result(
            stock_items,
            today,
            DATA_DIR / "preday_result.csv",
        )

        # 종가 결과 파일 생성 결과를 로그로 기록합니다.
        logger.info(
            "preday_result.csv updated path=%s shared=%s rows=%s fetched=%s failed=%s",
            preday_report.get("path"),
            preday_report.get("shared_path") or "",
            preday_report.get("rows", 0),
            preday_report.get("fetched_rows", 0),
            preday_report.get("failed_rows", 0),
        )

        # 수집된 시장 데이터를 백업하고 백업 파일 수를 집계합니다.
        archived = _archive_market_data(today)
        _log_stage_done(
            "백업",
            today,
            datetime.now(SEOUL_TZ),
            f"archived_files={archived.get('total', 0)} krx={archived.get('krx', 0)} nxt={archived.get('nxt', 0)} keep_days=20",
        )

        # 하루치 작업이 모두 끝났으므로 다음 날 00:00까지 대기합니다.
        # 다음 루프에서 다시 거래일 여부, 휴장일 여부, 각 단계별 시간을 새로 계산합니다.
        sleep_until(_combine(today + timedelta(days=1), time(0, 0)), stop_flag)
