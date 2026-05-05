from __future__ import annotations

"""
[개요]

이 모듈은 한국투자증권 WebSocket 연결에 필요한 "Approval Key(승인키)"를 관리합니다.

주요 역할:
1. 승인키 발급 (API 요청)
2. 승인키 파일 저장 (캐싱)
3. 기존 승인키 재사용
4. 만료 시 자동 재발급

즉, "인증 토큰 관리 시스템"입니다.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import json

import requests

from .config import ApprovalKey, AppConfig


# ---------------------------------------------------
# [승인키 응답 객체]
# ---------------------------------------------------

@dataclass(frozen=True)
class ApprovalKeyResponse:
    """
    승인키 API 응답을 담는 객체

    approval_key: 실제 인증 키
    issued_at: 발급 시간 (UTC ISO)
    expires_at: 만료 시간 (없을 수도 있음)
    app_key: 어떤 app_key로 발급했는지
    source_url: 호출한 API URL
    """

    approval_key: str
    issued_at: str
    expires_at: str | None
    app_key: str
    source_url: str


# ---------------------------------------------------
# [현재 시간 ISO 문자열 생성]
# ---------------------------------------------------

def _now_iso() -> str:
    """
    현재 UTC 시간을 ISO 포맷 문자열로 반환

    예:
        2026-04-29T05:00:00.123456+00:00
    """
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------
# [승인키 파일 저장]
# ---------------------------------------------------

def save_approval_key(path: Path, record: ApprovalKeyResponse) -> None:
    """
    승인키 정보를 JSON 파일로 저장

    특징:
    - 디렉토리가 없으면 자동 생성
    - 사람이 읽기 좋게 indent=2
    """

    path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(
        json.dumps(asdict(record), ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


# ---------------------------------------------------
# [승인키 발급 요청]
# ---------------------------------------------------

def request_approval_key(app_config: AppConfig) -> ApprovalKeyResponse:
    """
    한국투자증권 API에 요청하여 승인키 발급

    흐름:
    1. app_key / app_secret 확인
    2. API 요청 생성
    3. 승인키 추출
    4. ApprovalKeyResponse 반환
    """

    # ---------------------------------------------------
    # 필수 값 검증
    # ---------------------------------------------------
    if not app_config.app_key:
        raise ValueError("APP_KEY is required for websocket approval key issuance.")
    if not app_config.app_secret:
        raise ValueError("APP_SECRET is required for websocket approval key issuance.")

    # 승인키 발급 API URL
    url = f"{app_config.api_base_url}/oauth2/Approval"

    # 요청 payload
    payload = {
        "grant_type": "client_credentials",
        "appkey": app_config.app_key,
        "secretkey": app_config.app_secret,
    }

    # 헤더 설정
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/plain",
        "charset": "UTF-8",
    }

    # ---------------------------------------------------
    # API 호출
    # ---------------------------------------------------
    response = requests.post(
        url,
        data=json.dumps(payload),
        headers=headers,
        timeout=30
    )

    # HTTP 오류 발생 시 예외
    response.raise_for_status()

    body = response.json()

    # 승인키 추출
    approval_key = body.get("approval_key")

    if not approval_key:
        raise ValueError(f"Approval key missing in response: {body}")

    # 응답 객체 생성
    return ApprovalKeyResponse(
        approval_key=approval_key,
        issued_at=_now_iso(),
        expires_at=body.get("expires_at") or body.get("expire_at"),
        app_key=app_config.app_key,
        source_url=url,
    )


# ---------------------------------------------------
# [승인키 로드 또는 갱신 (핵심 함수)]
# ---------------------------------------------------

def load_or_refresh_approval_key(
    app_config: AppConfig,
    path: Path,
    refresh: bool = False,
) -> ApprovalKey:
    """
    승인키를 로드하거나 필요 시 재발급

    핵심 로직:
    1. refresh=False이면 기존 키 사용 시도
    2. 기존 키가 있고 만료되지 않았으면 그대로 반환
    3. 아니면 새로 발급
    """

    # ---------------------------------------------------
    # 기존 키 사용 시도
    # ---------------------------------------------------
    if not refresh:
        existing = _load_cached_key(path)

        # 기존 키 있고, 만료되지 않았으면 그대로 사용
        if existing.approval_key and not _is_expired(existing):
            return existing

    # ---------------------------------------------------
    # 새 승인키 발급
    # ---------------------------------------------------
    record = request_approval_key(app_config)

    # 파일 저장
    save_approval_key(path, record)

    # 내부 객체 형태로 변환하여 반환
    return ApprovalKey(
        approval_key=record.approval_key,
        issued_at=record.issued_at,
        expires_at=record.expires_at,
        app_key=record.app_key,
        source_url=record.source_url,
    )


# ---------------------------------------------------
# [캐시된 승인키 로드]
# ---------------------------------------------------

def _load_cached_key(path: Path) -> ApprovalKey:
    """
    approval_key.json 파일에서 승인키 로드

    예외 처리:
    - 파일 없음 → 빈 키
    - 내용 없음 → 빈 키
    """

    if not path.exists():
        return ApprovalKey(approval_key="")

    raw = path.read_text(encoding="utf-8").strip()

    if not raw:
        return ApprovalKey(approval_key="")

    data = json.loads(raw)

    return ApprovalKey(
        approval_key=data.get("approval_key", ""),
        issued_at=data.get("issued_at"),
        expires_at=data.get("expires_at"),
        app_key=data.get("app_key"),
        source_url=data.get("source_url"),
    )


# ---------------------------------------------------
# [만료 여부 체크]
# ---------------------------------------------------

def _is_expired(record: ApprovalKey) -> bool:
    """
    승인키 만료 여부 판단

    True  → 만료됨
    False → 아직 유효
    """

    # 만료 시간이 없으면 무조건 유효로 판단
    if not record.expires_at:
        return False

    # ISO 문자열 → datetime 변환
    raw = record.expires_at.strip().replace("Z", "+00:00")

    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError:
        # 파싱 실패 시 안전하게 "만료 아님" 처리
        return False

    # timezone 없으면 UTC로 강제 지정
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    # 현재 시간과 비교
    return datetime.now(timezone.utc) >= expires_at.astimezone(timezone.utc)
