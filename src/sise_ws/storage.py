from __future__ import annotations

# dataclass: 데이터 저장용 클래스를 간결하게 만들기 위해 사용합니다.
from dataclasses import dataclass

# date/datetime/timedelta:
# - 현재 시각 기록
# - 백업 날짜 계산
# - 오래된 백업 삭제 기준 계산에 사용합니다.
from datetime import date, datetime, timedelta

# Path: 파일/디렉터리 경로를 객체 형태로 다루기 위해 사용합니다.
from pathlib import Path

# csv: 실시간 데이터를 CSV 파일로 저장하기 위해 사용합니다.
import csv

# shutil: CSV 파일 이동 및 백업 디렉터리 삭제에 사용합니다.
import shutil


@dataclass(frozen=True)
class MarketRecord:
    """
    웹소켓에서 수신한 시장 데이터를 저장하기 위한 표준 데이터 객체입니다.

    이전 웹소켓 로직에서 체결/호가 데이터를 파싱한 뒤,
    이 MarketRecord 형태로 CsvStore.append()에 전달합니다.

    frozen=True 이므로 생성 후 값이 변경되지 않습니다.
    """

    # 데이터가 수집된 시각입니다.
    captured_at: str

    # 시장 세션 이름입니다.
    # 예: kospi, kosdaq, morning, afternoon 등 프로젝트에서 정의한 값
    market: str

    # 데이터 종류입니다.
    # 현재 저장 로직에서는 "trade" 또는 "orderbook"을 처리합니다.
    kind: str

    # 한국투자증권 실시간 TR ID입니다.
    tr_id: str

    # 종목코드입니다.
    symbol: str

    # 웹소켓 payload를 컬럼명 기준으로 파싱한 원본 데이터입니다.
    payload: dict[str, str]


def now_iso() -> str:
    """
    현재 로컬 타임존 기준 시각을 ISO 문자열로 반환합니다.

    이전 웹소켓 로직에서 MarketRecord.captured_at 값을 만들 때 사용합니다.
    """

    return datetime.now().astimezone().isoformat()


class CsvStore:
    """
    실시간 시장 데이터를 CSV 파일로 저장하는 저장소 클래스입니다.

    주요 역할:
    1. 체결 데이터 저장
        - {종목코드}.csv 파일에 저장
        - 컬럼: time, price, millisec

    2. 호가 데이터 저장
        - {종목코드}_orderbook.csv 파일에 저장
        - 컬럼: time, millisec, 기타 호가 payload 컬럼들

    3. 중복 체결가 필터링
        - 같은 종목의 직전 체결가와 동일하면 저장하지 않음

    4. 같은 초 안에서 들어온 여러 데이터를 구분하기 위한 millisec 생성
        - 실제 밀리초라기보다는 같은 HHMMSS 안의 순번입니다.

    5. 실행 중 저장 통계 관리
        - 저장된 파일 수
        - 저장된 row 수
        - 체결/호가 row 수
        - 파일별 first_time, last_time
    """

    def __init__(self, root_dir: Path):
        # CSV 파일을 저장할 루트 디렉터리입니다.
        self.root_dir = root_dir

        # 저장 디렉터리가 없으면 생성합니다.
        self.root_dir.mkdir(parents=True, exist_ok=True)

        # target_key별 마지막 저장 time 값을 기억합니다.
        # target_key 예: "trade:005930", "orderbook:005930"
        self._last_time_by_target: dict[str, str] = {}

        # target_key별 마지막 millisec 순번을 기억합니다.
        # 같은 time_text가 반복되면 이 값을 증가시켜 0000, 0001, 0002처럼 저장합니다.
        self._last_seq_by_target: dict[str, int] = {}

        # 종목별 마지막 체결가를 기억합니다.
        # 같은 가격이 연속으로 들어오면 중복 저장하지 않기 위해 사용합니다.
        self._last_price_by_symbol: dict[str, int] = {}

        # 이번 실행 중 한 번이라도 기록된 파일 경로 집합입니다.
        self._written_targets: set[Path] = set()

        # 이번 실행 중 전체 저장 row 수입니다.
        self._written_rows = 0

        # 이번 실행 중 체결 데이터 저장 row 수입니다.
        self._written_trade_rows = 0

        # 이번 실행 중 호가 데이터 저장 row 수입니다.
        self._written_orderbook_rows = 0

        # target_key별 이번 실행 중 처음 저장된 time입니다.
        self._first_time_by_target: dict[str, str] = {}

        # target_key별 이번 실행 중 마지막으로 저장된 time입니다.
        self._last_written_time_by_target: dict[str, str] = {}

        # target_key별 이번 실행 중 저장된 row 수입니다.
        self._rows_by_target: dict[str, int] = {}

    def _trade_target(self, symbol: str) -> Path:
        """
        체결 데이터 저장 파일 경로를 반환합니다.

        예:
            symbol = "005930"
            결과 = root_dir / "005930.csv"
        """

        return self.root_dir / f"{symbol}.csv"

    def _orderbook_target(self, symbol: str) -> Path:
        """
        호가 데이터 저장 파일 경로를 반환합니다.

        예:
            symbol = "005930"
            결과 = root_dir / "005930_orderbook.csv"
        """

        return self.root_dir / f"{symbol}_orderbook.csv"

    def _target_key(self, kind: str, symbol: str) -> str:
        """
        내부 상태 관리를 위한 고유 key를 생성합니다.

        체결과 호가는 같은 종목이어도 별도 파일에 저장되므로,
        kind와 symbol을 함께 묶어서 구분합니다.

        예:
            kind = "trade", symbol = "005930"
            결과 = "trade:005930"
        """

        return f"{kind}:{symbol}"

    def _load_last_state(self, target_key: str, target: Path) -> None:
        """
        기존 CSV 파일의 마지막 저장 상태를 메모리에 로드합니다.

        필요한 이유:
        프로그램이 재시작되더라도 기존 CSV 마지막 row를 읽어서
        같은 초(time)에 이어지는 millisec 순번을 자연스럽게 이어가기 위함입니다.

        단, 이미 한 번 로드한 target_key는 다시 읽지 않습니다.
        """

        # 이미 로드된 target_key라면 중복으로 파일을 읽지 않습니다.
        if target_key in self._last_time_by_target:
            return

        # 저장 대상 파일이 아직 없으면 초기 상태로 설정합니다.
        if not target.exists():
            self._last_time_by_target[target_key] = ""
            self._last_seq_by_target[target_key] = -1
            return

        # 기존 CSV 파일을 읽어서 마지막 row를 찾습니다.
        with target.open("r", encoding="utf-8", newline="") as fp:
            reader = csv.DictReader(fp)

            last_row: dict[str, str] | None = None

            # 전체 row를 순회하면서 마지막 row만 남깁니다.
            for row in reader:
                last_row = row

        # 파일은 있지만 데이터 row가 없다면 초기 상태로 설정합니다.
        if not last_row:
            self._last_time_by_target[target_key] = ""
            self._last_seq_by_target[target_key] = -1
            return

        # 마지막 row의 time 값을 읽습니다.
        last_time = str(last_row.get("time", "")).strip()

        # 마지막 row의 millisec 값을 읽습니다.
        last_seq_text = str(last_row.get("millisec", "0")).strip()

        try:
            # millisec은 문자열로 저장되어 있으므로 int로 변환합니다.
            last_seq = int(last_seq_text)

        except ValueError:
            # millisec 값이 숫자가 아니면 이어가기 어렵기 때문에 -1로 초기화합니다.
            last_seq = -1

        # 마지막 time과 millisec 순번을 메모리에 저장합니다.
        self._last_time_by_target[target_key] = last_time
        self._last_seq_by_target[target_key] = last_seq

    def _next_millisec(
        self,
        kind: str,
        symbol: str,
        target: Path,
        time_text: str,
    ) -> str:
        """
        저장할 row의 millisec 값을 생성합니다.

        주의:
        여기서 millisec은 실제 밀리초가 아닙니다.
        같은 HHMMSS 시간값 안에서 여러 데이터가 들어왔을 때
        순서를 구분하기 위한 4자리 순번입니다.

        예:
            같은 time_text = "093000" 데이터가 연속으로 들어오면
            0000, 0001, 0002 ... 로 증가합니다.
        """

        target_key = self._target_key(kind, symbol)

        # 기존 파일이 있다면 마지막 time/millisec 상태를 먼저 로드합니다.
        self._load_last_state(target_key, target)

        last_time = self._last_time_by_target.get(target_key, "")
        last_seq = self._last_seq_by_target.get(target_key, -1)

        # 직전 저장 time과 현재 time이 같으면 같은 초 안의 다음 순번으로 처리합니다.
        if last_time == time_text:
            seq = last_seq + 1

        # 시간이 바뀌었으면 해당 초의 첫 데이터이므로 0부터 시작합니다.
        else:
            seq = 0

        # 다음 호출에서 이어서 사용할 수 있도록 상태를 갱신합니다.
        self._last_time_by_target[target_key] = time_text
        self._last_seq_by_target[target_key] = seq

        # 4자리 문자열로 반환합니다.
        # 예: 0 -> "0000", 12 -> "0012"
        return f"{seq:04d}"

    def _parse_time(self, record: MarketRecord) -> str:
        """
        MarketRecord payload에서 시간값을 추출합니다.

        우선순위:
        1. STCK_CNTG_HOUR : 체결 시간
        2. BSOP_HOUR      : 영업 시간/호가 시간
        3. time           : 이미 정규화된 시간값
        4. 없으면 빈 문자열

        반환값은 HHMMSS 형태의 6자리 문자열입니다.
        """

        time_text = str(
            record.payload.get("STCK_CNTG_HOUR")
            or record.payload.get("BSOP_HOUR")
            or record.payload.get("time")
            or ""
        ).strip()

        # 시간값이 없으면 저장할 수 없으므로 빈 문자열 반환
        if not time_text:
            return ""

        # ":" 문자가 있으면 제거하고,
        # 앞 6자리만 사용한 뒤,
        # 길이가 부족하면 왼쪽을 0으로 채웁니다.
        #
        # 예:
        # "09:30:01" -> "093001"
        # "93001"    -> "093001"
        return time_text.replace(":", "")[:6].zfill(6)

    def _parse_price(self, record: MarketRecord) -> int | None:
        """
        MarketRecord payload에서 현재가/체결가를 정수로 추출합니다.

        우선순위:
        1. STCK_PRPR : KIS 현재가 필드
        2. price     : 이미 정규화된 가격 필드

        가격이 없거나 숫자로 변환할 수 없으면 None을 반환합니다.
        """

        price_text = str(
            record.payload.get("STCK_PRPR")
            or record.payload.get("price")
            or ""
        ).strip()

        # 가격값이 없으면 저장하지 않습니다.
        if not price_text:
            return None

        try:
            # 콤마가 포함된 가격 문자열도 처리합니다.
            # float을 거친 뒤 int로 바꾸는 이유는
            # "1234.0" 같은 문자열도 처리하기 위함입니다.
            return int(float(price_text.replace(",", "")))

        except ValueError:
            # 숫자로 변환할 수 없는 값이면 저장하지 않습니다.
            return None

    def append(self, record: MarketRecord) -> Path | None:
        """
        외부에서 호출하는 저장 진입점입니다.

        이전 웹소켓 로직은 수신 데이터를 MarketRecord로 만든 뒤
        이 append() 메서드에 전달합니다.

        record.kind 값에 따라 체결/호가 저장 로직으로 분기합니다.

        반환값:
            저장에 성공하면 저장된 파일 Path
            저장하지 않았거나 저장할 수 없으면 None
        """

        # 종목코드를 정리합니다.
        symbol = str(record.symbol or "").strip()

        # 종목코드가 없으면 어떤 파일에 저장할지 알 수 없으므로 저장하지 않습니다.
        if not symbol:
            return None

        # kind 값을 소문자로 정규화합니다.
        kind = str(record.kind or "").strip().lower()

        # 체결 데이터 저장
        if kind == "trade":
            return self._append_trade(record, symbol)

        # 호가 데이터 저장
        if kind == "orderbook":
            return self._append_orderbook(record, symbol)

        # 지원하지 않는 kind이면 저장하지 않습니다.
        return None

    def _append_trade(self, record: MarketRecord, symbol: str) -> Path | None:
        """
        체결 데이터를 CSV 파일에 저장합니다.

        저장 파일:
            {symbol}.csv

        저장 컬럼:
            time, price, millisec

        주요 필터:
        - time이 없으면 저장하지 않음
        - price가 없거나 숫자가 아니면 저장하지 않음
        - 직전 저장 가격과 같으면 중복으로 보고 저장하지 않음
        """

        target = self._trade_target(symbol)

        # payload에서 시간값을 추출합니다.
        time_text = self._parse_time(record)

        # 시간값이 없으면 저장하지 않습니다.
        if not time_text:
            return None

        # payload에서 가격값을 추출합니다.
        price_value = self._parse_price(record)

        # 가격값이 없거나 변환에 실패하면 저장하지 않습니다.
        if price_value is None:
            return None

        # 기존 CSV 파일이 있다면 마지막 저장 상태를 로드합니다.
        self._load_last_state(self._target_key("trade", symbol), target)

        # 같은 종목에서 직전 저장 가격과 현재 가격이 같으면 저장하지 않습니다.
        # 즉, 가격 변화가 있는 체결만 기록하려는 의도입니다.
        if self._last_price_by_symbol.get(symbol) == price_value:
            return None

        # 같은 초 안에서 들어온 데이터 순서를 구분하기 위한 순번을 생성합니다.
        millisec = self._next_millisec("trade", symbol, target, time_text)

        # 파일이 없으면 이번 write에서 헤더를 함께 씁니다.
        write_header = not target.exists()

        # CSV 파일에 append 모드로 저장합니다.
        with target.open("a", encoding="utf-8", newline="") as fp:
            writer = csv.writer(fp, quoting=csv.QUOTE_NONNUMERIC)

            # 신규 파일이면 헤더 작성
            if write_header:
                writer.writerow(["time", "price", "millisec"])

            # 실제 체결 데이터 row 작성
            writer.writerow([time_text, price_value, millisec])

        # 종목별 마지막 저장 가격을 갱신합니다.
        self._last_price_by_symbol[symbol] = price_value

        # 저장 통계를 갱신합니다.
        self._mark_written("trade", symbol, target, time_text)

        return target

    def _append_orderbook(self, record: MarketRecord, symbol: str) -> Path | None:
        """
        호가 데이터를 CSV 파일에 저장합니다.

        저장 파일:
            {symbol}_orderbook.csv

        저장 컬럼:
            time, millisec, payload의 나머지 컬럼들

        체결 데이터와 달리 현재 로직에서는
        같은 값 중복 여부를 검사하지 않고 수신될 때마다 저장합니다.
        """

        target = self._orderbook_target(symbol)

        # payload에서 시간값을 추출합니다.
        time_text = self._parse_time(record)

        # 시간값이 없으면 저장하지 않습니다.
        if not time_text:
            return None

        # 같은 초 안에서의 순번을 생성합니다.
        millisec = self._next_millisec("orderbook", symbol, target, time_text)

        # 원본 payload를 복사합니다.
        # 원본 record.payload를 직접 수정하지 않기 위함입니다.
        payload = dict(record.payload)

        # 파일명/공통 컬럼으로 이미 따로 쓰거나 불필요한 필드는 제거합니다.
        payload.pop("MKSC_SHRN_ISCD", None)   # 종목코드
        payload.pop("BSOP_HOUR", None)        # 호가/영업 시간
        payload.pop("STCK_CNTG_HOUR", None)   # 체결 시간
        payload.pop("STCK_PRPR", None)        # 현재가

        # 최종 CSV row를 구성합니다.
        # time, millisec을 앞에 두고 나머지 payload 필드를 뒤에 붙입니다.
        row = {
            "time": time_text,
            "millisec": millisec,
            **payload,
        }

        # CSV 헤더 순서입니다.
        fieldnames = ["time", "millisec", *payload.keys()]

        # 파일이 없으면 헤더를 작성합니다.
        write_header = not target.exists()

        # DictWriter를 사용해 dict 형태의 row를 CSV로 저장합니다.
        with target.open("a", encoding="utf-8", newline="") as fp:
            writer = csv.DictWriter(
                fp,
                fieldnames=fieldnames,
                quoting=csv.QUOTE_NONNUMERIC,
            )

            # 신규 파일이면 헤더 작성
            if write_header:
                writer.writeheader()

            # 호가 데이터 row 작성
            writer.writerow(row)

        # 저장 통계를 갱신합니다.
        self._mark_written("orderbook", symbol, target, time_text)

        return target

    def _mark_written(
        self,
        kind: str,
        symbol: str,
        target: Path,
        time_text: str,
    ) -> None:
        """
        실제 CSV 저장이 완료된 뒤 내부 통계를 갱신합니다.

        snapshot()에서 현재 실행 중 저장 현황을 보여주기 위해 사용됩니다.
        """

        target_key = self._target_key(kind, symbol)

        # 이번 실행 중 저장된 파일 경로를 기록합니다.
        self._written_targets.add(target)

        # 전체 저장 row 수 증가
        self._written_rows += 1

        # target_key별 저장 row 수 증가
        self._rows_by_target[target_key] = self._rows_by_target.get(target_key, 0) + 1

        # target_key별 마지막 저장 time 갱신
        self._last_written_time_by_target[target_key] = time_text

        # target_key별 첫 저장 time은 최초 1회만 기록합니다.
        self._first_time_by_target.setdefault(target_key, time_text)

        # kind별 저장 row 수 증가
        if kind == "trade":
            self._written_trade_rows += 1
        elif kind == "orderbook":
            self._written_orderbook_rows += 1

    def snapshot(self) -> dict[str, object]:
        """
        현재 CsvStore 실행 중 저장 통계를 dict로 반환합니다.

        예:
        {
            "files_written": 2,
            "rows_written": 100,
            "trade_rows_written": 80,
            "orderbook_rows_written": 20,
            "file_summaries": [...]
        }

        이 값은 로그 출력, 실행 결과 요약, 알림 메시지 등에 사용할 수 있습니다.
        """

        return {
            # 이번 실행 중 실제로 기록된 파일 개수입니다.
            "files_written": len(self._written_targets),

            # 전체 저장 row 수입니다.
            "rows_written": self._written_rows,

            # 체결 데이터 row 수입니다.
            "trade_rows_written": self._written_trade_rows,

            # 호가 데이터 row 수입니다.
            "orderbook_rows_written": self._written_orderbook_rows,

            # 파일별 상세 저장 요약입니다.
            "file_summaries": [
                {
                    # 내부 식별자입니다.
                    # 예: "trade:005930"
                    "target": str(target_key),

                    # 실제 CSV 파일명입니다.
                    "filename": self._target_filename(target_key),

                    # 해당 파일에 이번 실행 중 저장한 row 수입니다.
                    "rows": self._rows_by_target.get(target_key, 0),

                    # 이번 실행 중 첫 저장 시간입니다.
                    "first_time": self._first_time_by_target.get(target_key, ""),

                    # 이번 실행 중 마지막 저장 시간입니다.
                    "last_time": self._last_written_time_by_target.get(target_key, ""),
                }
                for target_key in sorted(self._rows_by_target)
            ],
        }

    def _target_filename(self, target_key: str) -> str:
        """
        target_key를 실제 저장 파일명으로 변환합니다.

        예:
            "trade:005930"     -> "005930.csv"
            "orderbook:005930" -> "005930_orderbook.csv"
        """

        # "kind:symbol" 구조를 kind와 symbol로 분리합니다.
        kind, symbol = target_key.split(":", 1)

        # 체결 데이터 파일명
        if kind == "trade":
            return f"{symbol}.csv"

        # 호가 데이터 파일명
        if kind == "orderbook":
            return f"{symbol}_orderbook.csv"

        # 혹시 알 수 없는 kind가 들어오면 기본 파일명 형태로 반환합니다.
        return f"{symbol}.csv"


def archive_csv_files(
    root_dir: Path,
    archive_date: date | None = None,
    keep_days: int = 20,
) -> list[Path]:
    """
    root_dir에 있는 CSV 파일들을 날짜별 backup 디렉터리로 이동합니다.

    백업 경로:
        root_dir / "backup" / YYYY-MM-DD

    백업 파일명:
        기존파일명_YYYYMMDD.csv

    예:
        data/005930.csv
        → data/backup/2026-04-29/005930_20260429.csv

    keep_days 기준보다 오래된 백업 디렉터리는 삭제합니다.
    """

    # archive_date가 지정되어 있으면 해당 날짜를 사용하고,
    # 없으면 오늘 날짜를 사용합니다.
    target_date = archive_date or datetime.now().date()

    # 날짜별 백업 디렉터리를 생성합니다.
    backup_root = root_dir / "backup" / target_date.isoformat()
    backup_root.mkdir(parents=True, exist_ok=True)

    # 실제 이동된 파일 경로 목록입니다.
    moved: list[Path] = []

    # root_dir 바로 아래의 CSV 파일만 백업 대상으로 봅니다.
    # backup 디렉터리 내부 CSV는 여기서 직접 대상으로 잡지 않습니다.
    for item in root_dir.glob("*.csv"):
        # 백업 파일명에 날짜를 붙입니다.
        destination = backup_root / f"{item.stem}_{target_date.strftime('%Y%m%d')}{item.suffix}"

        # 같은 이름의 백업 파일이 이미 있으면 삭제 후 다시 이동합니다.
        # 즉, 같은 날짜에 백업을 다시 수행하면 최신 파일로 덮어쓰는 방식입니다.
        if destination.exists():
            destination.unlink()

        # 원본 CSV 파일을 백업 디렉터리로 이동합니다.
        shutil.move(str(item), str(destination))

        # 이동된 백업 파일 경로를 결과 목록에 추가합니다.
        moved.append(destination)

    # 오래된 백업 디렉터리를 정리합니다.
    _prune_old_backups(root_dir / "backup", keep_days=keep_days)

    return moved


def _prune_old_backups(backup_root: Path, keep_days: int) -> None:
    """
    keep_days 기준보다 오래된 백업 디렉터리를 삭제합니다.

    백업 디렉터리 구조는 다음과 같다고 가정합니다.

        backup/
            2026-04-29/
            2026-04-28/
            2026-04-01/

    keep_days=20이면 최근 20일치만 남기고,
    그보다 오래된 날짜 디렉터리는 삭제합니다.
    """

    # keep_days가 0 이하이면 백업 보관 기능을 사용하지 않는 것으로 보고 종료합니다.
    if keep_days <= 0 or not backup_root.exists():
        return

    # 보관 기준 날짜를 계산합니다.
    #
    # keep_days=20이면
    # 오늘을 포함해서 최근 20일치를 남겨야 하므로
    # cutoff = 오늘 - 19일 입니다.
    cutoff = datetime.now().date() - timedelta(days=keep_days - 1)

    # backup_root 아래의 항목들을 순회합니다.
    for child in backup_root.iterdir():
        # 디렉터리가 아니면 무시합니다.
        if not child.is_dir():
            continue

        try:
            # 디렉터리명이 YYYY-MM-DD 형식이라고 가정하고 날짜로 변환합니다.
            child_date = date.fromisoformat(child.name)

        except ValueError:
            # 날짜 형식이 아닌 디렉터리는 삭제 대상에서 제외합니다.
            continue

        # cutoff보다 오래된 백업 디렉터리는 삭제합니다.
        if child_date < cutoff:
            shutil.rmtree(child, ignore_errors=True)
