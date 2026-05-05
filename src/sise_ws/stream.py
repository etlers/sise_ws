from __future__ import annotations

# dataclass: 데이터만 담는 클래스를 간결하게 정의하기 위해 사용합니다.
from dataclasses import dataclass

# asyncio: 웹소켓 수신처럼 비동기 I/O 작업을 처리하기 위해 사용합니다.
import asyncio
import json
import logging

# 한국투자증권 웹소켓에서 내려오는 암호화 데이터를 복호화하기 위해 사용합니다.
from base64 import b64decode
from pathlib import Path
from datetime import datetime

import websockets
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from .approval import load_or_refresh_approval_key
from .config import AppConfig, ApprovalKey
from .market import MarketSession
from .storage import CsvStore, MarketRecord, now_iso


# 현재 모듈 전용 로거를 생성합니다.
logger = logging.getLogger(__name__)


# 실시간 체결 데이터 TR ID 목록입니다.
# KIS 웹소켓에서 체결 데이터를 구분할 때 사용합니다.
TRADE_TR_IDS = {"H0STCNT0", "H0NXCNT0"}

# 실시간 호가 데이터 TR ID 목록입니다.
# KIS 웹소켓에서 호가 데이터를 구분할 때 사용합니다.
ORDERBOOK_TR_IDS = {"H0STASP0", "H0NXASP0"}


@dataclass(frozen=True)
class SubscriptionSpec:
    """
    웹소켓 구독 요청에 필요한 정보를 담는 설정 객체입니다.

    frozen=True 이므로 생성 후 값 변경이 불가능합니다.
    즉, 구독 스펙은 한 번 만들어지면 중간에 변경되지 않도록 고정됩니다.
    """

    # 시장 구분값입니다. 예: kospi, kosdaq 등
    market: str

    # 데이터 종류입니다. 예: trade, orderbook
    kind: str

    # 한국투자증권 웹소켓 TR ID입니다.
    tr_id: str

    # 구독/해제 구분값입니다.
    # 보통 "1"은 등록, "2"는 해제 의미로 사용됩니다.
    tr_type: str

    # 구독 대상 키입니다.
    # 보통 종목코드가 들어갑니다.
    tr_key: str

    # 내부에서 사용할 종목 코드입니다.
    symbol: str

    # 수신 payload를 파싱할 때 사용할 컬럼명 목록입니다.
    # 웹소켓 데이터는 "^" 로 구분된 문자열이므로,
    # 각 위치별 값에 컬럼명을 매핑하기 위해 필요합니다.
    columns: list[str]


def _build_message(approval_key: str, spec: SubscriptionSpec) -> dict:
    """
    KIS 웹소켓 서버로 보낼 구독 요청 메시지를 생성합니다.

    approval_key:
        웹소켓 접속/구독에 필요한 승인키입니다.

    spec:
        어떤 TR ID와 종목을 구독할지에 대한 설정입니다.
    """

    return {
        "header": {
            # 한국투자증권에서 발급받은 웹소켓 승인키입니다.
            "approval_key": approval_key,

            # 고객 타입입니다.
            # "P"는 개인 고객을 의미합니다.
            "custtype": "P",

            # 구독 등록/해제 구분입니다.
            "tr_type": spec.tr_type,

            # 메시지 인코딩 타입입니다.
            "content-type": "utf-8",
        },
        "body": {
            "input": {
                # 구독할 실시간 데이터의 TR ID입니다.
                "tr_id": spec.tr_id,

                # 구독 대상 키입니다.
                # 일반적으로 종목코드가 들어갑니다.
                "tr_key": spec.tr_key,
            }
        },
    }


def _parse_csv_payload(columns: list[str], payload: str) -> dict[str, str]:
    """
    KIS 웹소켓에서 받은 "^" 구분 문자열 payload를
    컬럼명 기반 dict로 변환합니다.

    예:
        columns = ["A", "B", "C"]
        payload = "1^2^3"

        결과:
        {
            "A": "1",
            "B": "2",
            "C": "3",
        }

    payload 값 개수가 columns보다 부족한 경우에는 빈 문자열을 넣습니다.
    """

    # KIS 실시간 데이터는 CSV처럼 보이지만 콤마가 아니라 "^" 로 구분됩니다.
    values = payload.split("^")

    data: dict[str, str] = {}

    # 컬럼 순서와 payload 값 순서를 맞춰 dict로 변환합니다.
    for idx, column in enumerate(columns):
        # payload 값이 존재하면 해당 값을 넣고,
        # 값이 부족하면 빈 문자열을 넣어 컬럼 누락을 방지합니다.
        data[column] = values[idx] if idx < len(values) else ""

    return data


def _decode_aes_cbc_base64(key: str, iv: str, cipher_text: str) -> str:
    """
    AES-CBC 방식으로 암호화되어 있고 base64로 인코딩된 데이터를 복호화합니다.

    KIS 웹소켓은 일부 실시간 데이터를 암호화해서 내려줄 수 있습니다.
    이 경우 시스템 응답에서 받은 key, iv를 저장해두었다가
    실제 데이터 수신 시 이 함수로 복호화합니다.
    """

    # 문자열 key, iv를 bytes로 변환하여 AES-CBC cipher를 생성합니다.
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))

    # base64 디코딩 → AES 복호화 → PKCS 패딩 제거 → UTF-8 문자열 변환 순서로 처리합니다.
    return unpad(
        cipher.decrypt(b64decode(cipher_text)),
        AES.block_size,
    ).decode("utf-8")


class KISWebSocketClient:
    """
    한국투자증권 KIS 웹소켓 클라이언트입니다.

    주요 역할:
    1. 웹소켓 서버 접속
    2. 실시간 체결/호가 데이터 구독
    3. 수신 메시지 파싱
    4. 암호화 데이터 복호화
    5. CSV 저장소에 MarketRecord 형태로 저장
    6. 연결 실패 시 재시도
    """

    def __init__(
        self,
        app_config: AppConfig,
        approval_key: ApprovalKey,
        session: MarketSession,
        store: CsvStore,
        max_retries: int = 3,
    ) -> None:
        # 앱 전체 설정입니다.
        # 웹소켓 URL 등 환경 설정이 들어있습니다.
        self.app_config = app_config

        # KIS 웹소켓 승인키 정보입니다.
        self.approval_key = approval_key

        # 현재 실행 중인 시장 세션 정보입니다.
        # 예: 어떤 시장 이름으로 데이터를 저장할지 결정할 때 사용됩니다.
        self.session = session

        # 수신한 실시간 데이터를 저장할 CSV 저장소입니다.
        self.store = store

        # 웹소켓 연결 실패 시 최대 재시도 횟수입니다.
        self.max_retries = max_retries

        # TR ID별 수신 데이터 파싱 정보를 저장합니다.
        #
        # 예:
        # {
        #   "H0STCNT0": {
        #       "columns": [...],
        #       "encrypt": "Y",
        #       "key": "...",
        #       "iv": "...",
        #   }
        # }
        #
        # 실제 데이터 프레임은 TR ID만 포함해서 내려오기 때문에,
        # 어떤 컬럼 구조로 파싱해야 하는지 여기서 찾아옵니다.
        self._data_map: dict[str, dict[str, object]] = {}

        # 웹소켓 연결 후 등록할 구독 목록입니다.
        self._open_specs: list[SubscriptionSpec] = []

    def subscribe(self, specs: list[SubscriptionSpec]) -> None:
        """
        웹소켓 접속 후 구독할 SubscriptionSpec 목록을 추가합니다.

        여기서는 실제 구독 요청을 보내지 않고,
        내부 목록에만 저장합니다.

        실제 구독 요청은 run()에서 웹소켓 연결이 성공한 뒤
        _send_subscription()을 통해 전송됩니다.
        """

        self._open_specs.extend(specs)

    def _add_data_map(
        self,
        tr_id: str,
        columns: list[str] | None = None,
        encrypt: str | None = None,
        key: str | None = None,
        iv: str | None = None,
    ) -> None:
        """
        TR ID별 데이터 파싱/복호화 정보를 등록하거나 갱신합니다.

        이 함수는 두 상황에서 사용됩니다.

        1. 구독 요청을 보낼 때:
            TR ID별 columns 정보를 저장합니다.

        2. 시스템 응답을 받을 때:
            암호화 여부, key, iv 정보를 저장합니다.

        기존 entry가 있으면 필요한 값만 갱신하고,
        없으면 기본값으로 새 entry를 생성합니다.
        """

        # tr_id에 해당하는 설정이 없으면 기본 구조를 생성합니다.
        entry = self._data_map.setdefault(
            tr_id,
            {
                "columns": [],
                "encrypt": False,
                "key": None,
                "iv": None,
            },
        )

        # 컬럼 정보가 전달된 경우에만 갱신합니다.
        if columns is not None:
            entry["columns"] = columns

        # 암호화 여부가 전달된 경우에만 갱신합니다.
        if encrypt is not None:
            entry["encrypt"] = encrypt

        # 복호화 key가 전달된 경우에만 갱신합니다.
        if key is not None:
            entry["key"] = key

        # 복호화 iv가 전달된 경우에만 갱신합니다.
        if iv is not None:
            entry["iv"] = iv

    def _system_resp(self, data: str) -> dict[str, object]:
        """
        JSON 형태의 시스템 응답 메시지를 파싱합니다.

        KIS 웹소켓에서 내려오는 메시지는 크게 두 종류입니다.

        1. 실시간 데이터 메시지:
            문자열이 "0" 또는 "1"로 시작합니다.

        2. 시스템 응답 메시지:
            JSON 문자열입니다.
            구독 성공/실패, PINGPONG, 암호화 key/iv 등이 포함됩니다.

        이 함수는 2번 시스템 응답 메시지를 해석합니다.
        """

        # JSON 문자열을 dict로 변환합니다.
        rdic = json.loads(data)

        # 응답 header에서 TR ID를 가져옵니다.
        tr_id = rdic["header"]["tr_id"]

        # 구독 대상 키입니다.
        # 응답 종류에 따라 없을 수도 있으므로 get()을 사용합니다.
        tr_key = rdic["header"].get("tr_key")

        # 암호화 여부입니다.
        # "Y"이면 실제 데이터 payload가 암호화되어 내려올 수 있습니다.
        encrypt = rdic["header"].get("encrypt")

        # 서버가 연결 유지를 위해 보내는 PINGPONG 메시지인지 확인합니다.
        is_pingpong = tr_id == "PINGPONG"

        # 구독 해제 응답 여부입니다.
        is_unsub = False

        # 응답 메시지 본문에 포함된 안내 메시지입니다.
        tr_msg = None

        # 암호화 데이터 복호화에 필요한 iv/key입니다.
        iv = None
        ekey = None

        # PINGPONG이 아니고 body가 있는 경우에만 body를 분석합니다.
        if not is_pingpong and rdic.get("body") is not None:
            tr_msg = rdic["body"].get("msg1")

            # output 안에 암호화 key/iv가 들어올 수 있습니다.
            output = rdic["body"].get("output") or {}
            iv = output.get("iv")
            ekey = output.get("key")

            # 메시지가 "UNSUB"으로 시작하면 구독 해제 응답으로 판단합니다.
            is_unsub = isinstance(tr_msg, str) and tr_msg.startswith("UNSUB")

        return {
            # rt_cd가 "0"이면 KIS 기준 정상 응답입니다.
            "is_ok": rdic.get("body", {}).get("rt_cd") == "0",

            # 응답 TR ID입니다.
            "tr_id": tr_id,

            # 응답 대상 키입니다.
            "tr_key": tr_key,

            # 구독 해제 응답 여부입니다.
            "is_unsub": is_unsub,

            # PINGPONG 메시지 여부입니다.
            "is_pingpong": is_pingpong,

            # KIS 응답 메시지입니다.
            "tr_msg": tr_msg,

            # 암호화 IV입니다.
            "iv": iv,

            # 암호화 KEY입니다.
            "ekey": ekey,

            # 암호화 여부입니다.
            "encrypt": encrypt,
        }

    async def _send_subscription(
        self,
        ws: websockets.ClientConnection,
        spec: SubscriptionSpec,
    ) -> None:
        """
        웹소켓 서버에 구독 요청을 전송합니다.

        전송 전에 TR ID별 컬럼 정보를 _data_map에 저장합니다.
        그래야 이후 실시간 데이터가 들어왔을 때
        payload를 어떤 컬럼명으로 파싱해야 하는지 알 수 있습니다.
        """

        # KIS 웹소켓 구독 요청 메시지를 생성합니다.
        message = _build_message(self.approval_key.approval_key, spec)

        # 이후 수신 데이터 파싱을 위해 TR ID별 컬럼 정보를 저장합니다.
        self._add_data_map(spec.tr_id, columns=spec.columns)

        # 웹소켓 서버로 구독 메시지를 JSON 문자열로 전송합니다.
        await ws.send(json.dumps(message))

        logger.debug(
            "subscribed market=%s kind=%s symbol=%s tr_id=%s",
            spec.market,
            spec.kind,
            spec.symbol,
            spec.tr_id,
        )

    async def _handle_raw(self, raw: str) -> None:
        """
        웹소켓에서 수신한 원본 메시지 1건을 처리합니다.

        처리 흐름:

        1. raw가 "0" 또는 "1"로 시작하면 실시간 데이터로 판단
            - "|" 기준으로 분리
            - TR ID 확인
            - 암호화 여부 확인
            - 필요 시 복호화
            - "^" 기준 payload 파싱
            - 체결/호가 종류에 따라 CSV 저장

        2. 그 외에는 시스템 응답 JSON으로 판단
            - PINGPONG이면 무시
            - key/iv가 있으면 _data_map에 저장
        """

        # KIS 실시간 데이터는 일반적으로 "0" 또는 "1"로 시작합니다.
        # 예: 0|H0STCNT0|...|payload
        if raw and raw[0] in {"0", "1"}:
            # 실시간 데이터 프레임은 "|" 로 구분됩니다.
            parts = raw.split("|")

            # 최소한 TR ID와 payload 위치까지는 있어야 합니다.
            # 구조가 기대와 다르면 잘못된 데이터로 보고 예외를 발생시킵니다.
            if len(parts) < 4:
                raise ValueError(f"invalid market payload: {raw}")

            # parts[1] 위치에 TR ID가 들어있습니다.
            tr_id = parts[1]

            # 해당 TR ID의 컬럼/암호화 정보를 가져옵니다.
            data_map = self._data_map.get(tr_id)

            # 구독한 적 없는 TR ID라면 파싱 방법을 알 수 없으므로 예외 처리합니다.
            if data_map is None:
                raise KeyError(f"unknown tr_id {tr_id}")

            # parts[3] 위치에 실제 데이터 payload가 들어있습니다.
            payload = parts[3]

            # 해당 TR ID가 암호화 데이터로 표시되어 있으면 복호화합니다.
            if data_map.get("encrypt") == "Y":
                payload = _decode_aes_cbc_base64(
                    str(data_map["key"]),
                    str(data_map["iv"]),
                    payload,
                )

            # "^" 구분 payload를 컬럼명 기반 dict로 변환합니다.
            record = _parse_csv_payload(
                list(data_map["columns"]),
                payload,
            )

            # TR ID가 체결 데이터 목록에 있으면 체결 데이터로 저장합니다.
            if tr_id in TRADE_TR_IDS:
                self.store.append(
                    MarketRecord(
                        # 데이터 수집 시각입니다.
                        captured_at=now_iso(),

                        # 현재 시장 세션 이름입니다.
                        market=self.session.name,

                        # 데이터 종류는 체결입니다.
                        kind="trade",

                        # 수신한 TR ID입니다.
                        tr_id=tr_id,

                        # 종목코드입니다.
                        # payload에서 MKSC_SHRN_ISCD 값을 사용합니다.
                        symbol=record.get("MKSC_SHRN_ISCD", ""),

                        # 전체 파싱 결과를 저장합니다.
                        payload=record,
                    )
                )

            # TR ID가 호가 데이터 목록에 있으면 호가 데이터로 저장합니다.
            elif tr_id in ORDERBOOK_TR_IDS:
                self.store.append(
                    MarketRecord(
                        captured_at=now_iso(),
                        market=self.session.name,
                        kind="orderbook",
                        tr_id=tr_id,
                        symbol=record.get("MKSC_SHRN_ISCD", ""),
                        payload=record,
                    )
                )

            # 실시간 데이터 처리가 끝났으므로 여기서 종료합니다.
            return

        # 여기까지 왔다면 raw는 실시간 데이터가 아니라 시스템 응답 JSON입니다.
        resp = self._system_resp(raw)

        # PINGPONG은 연결 유지를 위한 메시지이므로 별도 처리하지 않고 무시합니다.
        if resp["is_pingpong"]:
            return

        # 시스템 응답에 암호화 key와 iv가 포함되어 있으면 저장합니다.
        # 이후 해당 TR ID의 실시간 데이터가 암호화되어 내려올 때 복호화에 사용됩니다.
        if resp["ekey"] and resp["iv"]:
            self._add_data_map(
                resp["tr_id"],
                encrypt=resp["encrypt"],
                key=resp["ekey"],
                iv=resp["iv"],
            )

    async def run(self, stop_at: datetime | None = None) -> None:
        """
        웹소켓 클라이언트를 실행합니다.

        주요 흐름:

        1. 구독 개수 제한 확인
        2. 웹소켓 서버 접속
        3. 등록된 구독 요청 전송
        4. 메시지 반복 수신
        5. stop_at 시간이 되면 종료
        6. 연결 실패 시 max_retries 횟수만큼 재시도

        stop_at:
            지정된 시간이 되면 웹소켓 연결을 종료합니다.
            None이면 시간 제한 없이 실행됩니다.
        """

        # KIS 웹소켓은 연결당 구독 가능 개수가 제한되어 있습니다.
        # 현재 로직에서는 40개를 초과하면 실행하지 않고 예외를 발생시킵니다.
        if len(self._open_specs) > 40:
            raise ValueError("KIS websocket subscriptions are capped at 40 per connection.")

        # 웹소켓 서버 URL입니다.
        url = self.app_config.ws_base_url

        # 현재까지 재시도한 횟수입니다.
        retries = 0

        # 최대 재시도 횟수에 도달할 때까지 연결을 시도합니다.
        while retries < self.max_retries:
            # 재연결 전에 이미 종료 시간이 지났다면 더 이상 연결하지 않습니다.
            if stop_at is not None and datetime.now().astimezone() >= stop_at.astimezone():
                logger.info("stop time reached before websocket reconnect")
                return

            try:
                # 웹소켓 서버에 연결합니다.
                async with websockets.connect(url) as ws:
                    # 연결 성공 후 등록된 모든 구독 요청을 전송합니다.
                    for spec in self._open_specs:
                        await self._send_subscription(ws, spec)

                    # 연결이 유지되는 동안 계속 메시지를 수신합니다.
                    while True:
                        # 종료 시간이 지정되어 있고 현재 시간이 종료 시간을 넘었으면 종료합니다.
                        if stop_at is not None and datetime.now().astimezone() >= stop_at.astimezone():
                            logger.info("stop time reached, closing websocket")
                            return

                        # 기본 수신 timeout은 30초입니다.
                        timeout = 30.0

                        # stop_at이 지정되어 있으면 남은 시간보다 오래 기다리지 않도록 timeout을 조정합니다.
                        if stop_at is not None:
                            remaining = (
                                stop_at.astimezone() - datetime.now().astimezone()
                            ).total_seconds()

                            # 남은 시간이 없으면 종료합니다.
                            if remaining <= 0:
                                logger.info("stop time reached, closing websocket")
                                return

                            # 기본 timeout 30초와 남은 시간 중 더 짧은 값을 사용합니다.
                            timeout = min(timeout, remaining)

                        try:
                            # 지정된 timeout 동안 웹소켓 메시지를 기다립니다.
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)

                        except asyncio.TimeoutError:
                            # timeout 동안 메시지가 없더라도 오류로 보지 않습니다.
                            # 다시 루프를 돌면서 stop_at 확인 후 계속 수신합니다.
                            continue

                        logger.debug("received websocket frame")

                        # 수신한 원본 메시지를 처리합니다.
                        await self._handle_raw(raw)

                # async with 블록이 정상 종료되면 웹소켓 세션이 닫힌 것입니다.
                logger.info("%s websocket session closed", self.session.name)
                return

            except Exception:
                # 연결 실패 또는 수신/처리 중 예외가 발생하면 재시도합니다.
                retries += 1

                logger.exception(
                    "websocket connection failed (attempt %s/%s)",
                    retries,
                    self.max_retries,
                )

                # 너무 빠른 재시도를 방지하기 위해 1초 대기합니다.
                await asyncio.sleep(1)


def build_client(
    app_config: AppConfig,
    approval_path: Path,
    session: MarketSession,
    store: CsvStore,
    refresh_approval: bool = False,
) -> KISWebSocketClient:
    """
    KISWebSocketClient를 생성하는 팩토리 함수입니다.

    역할:
    1. approval_path에서 기존 approval key를 불러오거나
    2. refresh_approval=True이면 새로 발급/갱신한 뒤
    3. KISWebSocketClient 인스턴스를 생성해 반환합니다.
    """

    # 웹소켓 승인키를 로드하거나 필요 시 갱신합니다.
    approval_key = load_or_refresh_approval_key(
        app_config,
        approval_path,
        refresh=refresh_approval,
    )

    # 준비된 설정값들을 이용해 웹소켓 클라이언트를 생성합니다.
    return KISWebSocketClient(
        app_config,
        approval_key,
        session,
        store,
    )