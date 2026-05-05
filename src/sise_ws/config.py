from __future__ import annotations

"""
[개요]

이 모듈은 시스템 전반에서 사용하는 "설정(config) 및 데이터 로딩"을 담당합니다.

주요 역할:
1. 프로젝트 루트 및 기본 경로 정의
2. 환경 변수(.env) 로드
3. 앱 설정(AppConfig) 생성
4. 승인키(ApprovalKey) 로드
5. 종목 리스트 CSV 로드
6. 공유 데이터 경로 탐색

즉, 다른 모든 모듈이 의존하는 "기초 설정 레이어"입니다.
"""

from dataclasses import dataclass
from pathlib import Path
import csv
import json
import os


# ---------------------------------------------------
# [프로젝트 경로 정의]
# ---------------------------------------------------

# 현재 파일 기준으로 2단계 위 = 프로젝트 루트
ROOT_DIR = Path(__file__).resolve().parents[2]

# 설정 파일 위치 (config/)
CONFIG_DIR = ROOT_DIR / "config"

# 데이터 저장 위치 (data/)
DATA_DIR = ROOT_DIR / "data"

# 공유 데이터 기본 경로 (도커/외부 시스템 연동용)
DEFAULT_SHARED_DATA_PATH = "/shared_sise_data/data"

# ---------------------------------------------------
# [API / WebSocket 기본 URL]
# ---------------------------------------------------

# 실서버
DEFAULT_API_BASE_URL = "https://openapi.koreainvestment.com:9443"
DEFAULT_WS_BASE_URL = "ws://ops.koreainvestment.com:21000"

# 모의투자(VPS)
DEFAULT_VPS_API_BASE_URL = "https://openapivts.koreainvestment.com:29443"
DEFAULT_VPS_WS_BASE_URL = "ws://ops.koreainvestment.com:31000"


# ---------------------------------------------------
# [앱 설정 객체]
# ---------------------------------------------------

@dataclass(frozen=True)
class AppConfig:
    """
    애플리케이션 설정 객체 (불변)

    필드:
        app_key: API 키
        app_secret: API 시크릿
        env_name: 실행 환경 (prod / demo / vps 등)
        api_base_url: REST API 주소
        ws_base_url: WebSocket 주소
    """

    app_key: str
    app_secret: str
    env_name: str = "prod"
    api_base_url: str = DEFAULT_API_BASE_URL
    ws_base_url: str = DEFAULT_WS_BASE_URL

    @property
    def is_paper(self) -> bool:
        """
        모의투자 환경 여부 판단

        demo / vps / paper → True
        """
        return self.env_name.lower() in {"demo", "vps", "paper"}


# ---------------------------------------------------
# [승인키 객체]
# ---------------------------------------------------

@dataclass(frozen=True)
class ApprovalKey:
    """
    웹소켓 연결에 필요한 승인키 정보

    approval_key: 실제 인증 키
    issued_at: 발급 시간
    expires_at: 만료 시간
    app_key: 어떤 app_key로 발급했는지
    source_url: 발급 API URL
    """

    approval_key: str
    issued_at: str | None = None
    expires_at: str | None = None
    app_key: str | None = None
    source_url: str | None = None


# ---------------------------------------------------
# [종목 정보 객체]
# ---------------------------------------------------

@dataclass(frozen=True)
class StockItem:
    """
    종목 정보

    code: 종목 코드
    name: 종목명
    short_nm: 축약명
    div: 데이터 구분 (orderbook 포함 여부 등)
    """

    code: str
    name: str
    short_nm: str
    div: str

    @property
    def is_orderbook_enabled(self) -> bool:
        """
        호가(orderbook) 데이터 수집 여부

        div == "all" → 호가 포함
        """
        return self.div.lower() == "all"


# ---------------------------------------------------
# [.env 파일 로드]
# ---------------------------------------------------

def _load_env_file(path: Path) -> None:
    """
    .env 파일을 읽어서 환경 변수로 설정

    특징:
    - 이미 존재하는 환경 변수는 덮어쓰지 않음
    - # 주석 라인 무시
    - KEY=VALUE 형식만 처리
    """

    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        # 빈 줄 / 주석 / 잘못된 형식 제외
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")

        # 환경 변수에 없는 경우만 설정
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------
# [환경 변수 조회 헬퍼]
# ---------------------------------------------------

def _env(*keys: str, default: str = "") -> str:
    """
    여러 키를 순차적으로 조회하여 첫 번째 값 반환

    예:
        _env("APP_KEY", "KIS_APP_KEY")

    → 둘 중 하나라도 있으면 사용
    """

    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value

    return default


# ---------------------------------------------------
# [앱 설정 로드]
# ---------------------------------------------------

def load_app_config() -> AppConfig:
    """
    AppConfig 생성

    흐름:
    1. .env 로드
    2. 환경 이름 결정
    3. API/WS URL 선택
    4. AppConfig 객체 생성
    """

    # .env 파일 로드
    _load_env_file(ROOT_DIR / ".env")

    # 환경 결정
    env_name = _env("KIS_ENV", "ENV_NAME", default="prod").lower()

    # ---------------------------------------------------
    # [중요 분기] 실서버 vs 모의투자
    # ---------------------------------------------------
    if env_name in {"demo", "vps", "paper"}:
        # 모의투자 서버
        api_base_url = _env("KIS_API_BASE_URL", default=DEFAULT_VPS_API_BASE_URL)
        ws_base_url = _env("KIS_WS_BASE_URL", default=DEFAULT_VPS_WS_BASE_URL)
    else:
        # 실서버
        api_base_url = _env("KIS_API_BASE_URL", default=DEFAULT_API_BASE_URL)
        ws_base_url = _env("KIS_WS_BASE_URL", default=DEFAULT_WS_BASE_URL)

    return AppConfig(
        app_key=_env("APP_KEY", "APP_EKY", "APPKEY", "KIS_APP_KEY"),
        app_secret=_env("APP_SECRET", "SECRET_EKY", "APP.SECRET", "app.secret", "KIS_APP_SECRET"),
        env_name=env_name,
        api_base_url=api_base_url,
        ws_base_url=ws_base_url,
    )


# ---------------------------------------------------
# [공유 데이터 경로 탐색]
# ---------------------------------------------------

def get_shared_data_root() -> Path | None:
    """
    외부 공유 데이터 경로 찾기

    우선순위:
    1. 환경 변수 (SISE_SHARED_DATA_PATH)
    2. 기본 호스트 경로 (../sise_data/data)

    반환:
        Path 또는 None
    """

    shared_data_path = _env("SISE_SHARED_DATA_PATH", default=DEFAULT_SHARED_DATA_PATH)

    if shared_data_path:
        candidate = Path(shared_data_path).expanduser()
        if candidate.exists():
            return candidate

    # fallback 경로
    host_default = ROOT_DIR.parent / "sise_data" / "data"
    if host_default.exists():
        return host_default

    return None


# ---------------------------------------------------
# [승인키 로드]
# ---------------------------------------------------

def load_approval_key(path: Path | None = None) -> ApprovalKey:
    """
    approval_key.json 파일을 읽어서 ApprovalKey 객체 생성

    예외 처리:
    - 파일 없음 → 빈 승인키 반환
    - 내용 없음 → 빈 승인키 반환
    """

    target = path or (CONFIG_DIR / "approval_key.json")

    if not target.exists():
        return ApprovalKey(approval_key="")

    raw_text = target.read_text(encoding="utf-8").strip()

    if not raw_text:
        return ApprovalKey(approval_key="")

    data = json.loads(raw_text)

    return ApprovalKey(
        approval_key=data["approval_key"],
        issued_at=data.get("issued_at"),
        expires_at=data.get("expires_at"),
        app_key=data.get("app_key"),
        source_url=data.get("source_url"),
    )


# ---------------------------------------------------
# [종목 리스트 로드]
# ---------------------------------------------------

def load_stock_list(path: Path | None = None) -> list[StockItem]:
    """
    sise_stock_list.csv 파일을 읽어서 종목 리스트 생성

    CSV 구조:
        code,name,short_nm,div

    특징:
    - utf-8-sig → BOM 제거 대응
    - 각 row를 StockItem 객체로 변환
    """

    target = path or (CONFIG_DIR / "sise_stock_list.csv")

    with target.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)

        items: list[StockItem] = []

        for row in reader:
            items.append(
                StockItem(
                    code=row["code"].strip(),
                    name=row["name"].strip(),
                    short_nm=row["short_nm"].strip(),
                    div=row.get("div", "").strip(),
                )
            )

        return items
