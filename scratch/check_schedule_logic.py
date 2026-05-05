"""
거래 시간 설정 파일(config/deal_tm.json)을 읽어서
다음 거래일의 주요 작업 시간을 계산하고 출력하는 스크립트입니다.

주요 역할:
1. config/deal_tm.json 파일에서 NXT 프리마켓 시간, KRX 정규장 시간,
   시작 전 여유 시간(pre_minute), 종료 후 여유 시간(post_minute)을 읽습니다.

2. 설정된 거래 시간을 기준으로 다음 작업 시각을 계산합니다.
   - 승인키 사전 갱신 시각
   - NXT 프리마켓 시세 수집 시작/종료 시각
   - KRX 정규장 시세 수집 시작/종료 시각
   - 장 종료 후 데이터 백업 시작 시각

3. 계산된 일정을 사람이 보기 쉬운 형태로 콘솔에 출력합니다.

주의:
- 이 스크립트는 실제 스케줄러를 실행하는 로직이 아니라,
  설정값을 기준으로 예상 실행 시간을 확인하기 위한 테스트/검증용 로직입니다.
- today 값은 현재 날짜를 자동으로 가져오는 것이 아니라,
  테스트를 위해 date(2026, 4, 27)로 고정되어 있습니다.
"""

from datetime import date, time, timedelta, datetime, timezone
import json
from pathlib import Path


# 한국 시간대(KST)를 의미합니다.
# UTC 기준 +9시간이므로 timezone(timedelta(hours=9))로 정의합니다.
SEOUL_TZ = timezone(timedelta(hours=9))


def _parse_time(raw: str) -> time:
    """
    문자열로 된 시간을 datetime.time 객체로 변환합니다.

    예:
        "08:00:00" -> time(8, 0, 0)

    deal_tm.json 파일에는 시간이 문자열 형태로 저장되어 있기 때문에,
    이후 datetime 계산을 하기 위해 time 객체로 변환해야 합니다.
    """

    return datetime.strptime(raw, "%H:%M:%S").time()


def _combine(day: date, tm: time) -> datetime:
    """
    날짜(date)와 시간(time)을 합쳐서 한국 시간대가 적용된 datetime 객체를 만듭니다.

    예:
        day = 2026-04-27
        tm = 08:00:00

        결과:
        2026-04-27 08:00:00+09:00

    시세 수집 시작/종료, 승인키 갱신, 백업 시각처럼
    '특정 날짜의 특정 시간'을 계산할 때 사용합니다.
    """

    return datetime.combine(day, tm, tzinfo=SEOUL_TZ)


def _shifted_start(day: date, tm: time, pre_minute: int) -> datetime:
    """
    실제 시작 시간보다 pre_minute분 앞당긴 시작 시각을 계산합니다.

    예:
        실제 시작 시간: 08:00
        pre_minute: 5

        결과:
        07:55

    시세 수집은 장 시작과 동시에 시작하면 데이터 누락 가능성이 있으므로,
    설정값에 따라 몇 분 먼저 시작할 수 있게 처리합니다.

    max(pre_minute, 0)을 사용하는 이유:
    - pre_minute가 음수로 잘못 들어와도 시작 시간이 뒤로 밀리지 않도록 방지합니다.
    - 즉, 음수는 0분으로 취급합니다.
    """

    return _combine(day, tm) - timedelta(minutes=max(pre_minute, 0))


def load_deal_window():
    """
    config/deal_tm.json 파일을 읽어서 거래 시간 설정값을 반환합니다.

    예상 JSON 구조 예시:

    {
      "nxt": {
        "start_tm": "08:00:00",
        "end_tm": "08:50:00"
      },
      "krx": {
        "start_tm": "09:00:00",
        "end_tm": "15:30:00"
      },
      "pre_minute": 5,
      "post_minute": 10
    }

    반환값:
        {
            "nxt_start": time 객체,
            "nxt_end": time 객체,
            "krx_start": time 객체,
            "krx_end": time 객체,
            "pre_minute": int,
            "post_minute": int
        }
    """

    # 거래 시간 설정 파일 경로입니다.
    path = Path("config/deal_tm.json")

    # JSON 파일을 UTF-8로 읽은 뒤 파이썬 딕셔너리로 변환합니다.
    data = json.loads(path.read_text(encoding="utf-8"))

    # nxt 설정이 없으면 빈 딕셔너리로 처리합니다.
    # 단, 아래에서 nxt["start_tm"]처럼 필수 키를 바로 접근하므로
    # 실제로 start_tm/end_tm이 없으면 KeyError가 발생합니다.
    nxt = data.get("nxt") or {}

    # krx 설정도 동일하게 처리합니다.
    krx = data.get("krx") or {}

    return {
        # NXT 프리마켓 시작 시간 문자열을 time 객체로 변환합니다.
        "nxt_start": _parse_time(nxt["start_tm"]),

        # NXT 프리마켓 종료 시간 문자열을 time 객체로 변환합니다.
        "nxt_end": _parse_time(nxt["end_tm"]),

        # KRX 정규장 시작 시간 문자열을 time 객체로 변환합니다.
        "krx_start": _parse_time(krx["start_tm"]),

        # KRX 정규장 종료 시간 문자열을 time 객체로 변환합니다.
        "krx_end": _parse_time(krx["end_tm"]),

        # 시세 수집 시작을 몇 분 앞당길지 설정합니다.
        # 값이 없거나 None이면 0으로 처리합니다.
        "pre_minute": int(data.get("pre_minute") or 0),

        # 장 종료 후 몇 분 뒤에 백업을 시작할지 설정합니다.
        # 값이 없거나 None이면 0으로 처리합니다.
        "post_minute": int(data.get("post_minute") or 0),
    }


def main():
    """
    거래 시간 설정을 읽고,
    다음 거래일 기준 주요 작업 일정을 계산한 뒤 콘솔에 출력합니다.
    """

    # deal_tm.json에서 거래 시간 설정을 읽어옵니다.
    window = load_deal_window()

    # 테스트용 다음 거래일입니다.
    # 현재 날짜를 자동으로 가져오는 것이 아니라,
    # 2026년 4월 27일 월요일로 고정되어 있습니다.
    today = date(2026, 4, 27)  # Next trading day (Monday)

    # NXT 프리마켓 시세 수집 시작 시각입니다.
    # 실제 NXT 시작 시간보다 pre_minute분 먼저 시작합니다.
    nxt_start = _shifted_start(
        today,
        window["nxt_start"],
        window["pre_minute"],
    )

    # 승인키 갱신 시각입니다.
    #
    # 중요:
    # 시세 수집 시작 시각(nxt_start)이 아니라,
    # 공식 NXT 시작 시간(window["nxt_start"])을 기준으로 10분 전에 갱신합니다.
    #
    # 예:
    # 공식 NXT 시작: 08:00
    # 승인키 갱신: 07:50
    #
    # pre_minute가 5라서 시세 수집을 07:55에 시작하더라도,
    # 승인키는 공식 시작 시간 기준 10분 전인 07:50에 갱신됩니다.
    approval_refresh_at = _combine(
        today,
        window["nxt_start"],
    ) - timedelta(minutes=10)

    # NXT 프리마켓 종료 시각입니다.
    # 종료 시각은 앞당기거나 늦추지 않고 설정 파일의 값을 그대로 사용합니다.
    nxt_end = _combine(today, window["nxt_end"])

    # KRX 정규장 시세 수집 시작 시각입니다.
    # 실제 KRX 시작 시간보다 pre_minute분 먼저 시작합니다.
    krx_start = _shifted_start(
        today,
        window["krx_start"],
        window["pre_minute"],
    )

    # KRX 정규장 종료 시각입니다.
    krx_end = _combine(today, window["krx_end"])

    # 데이터 백업 시작 시각입니다.
    #
    # KRX 정규장 종료 후 post_minute분 뒤에 백업을 시작합니다.
    # max(post_minute, 0)을 사용하는 이유:
    # - post_minute가 음수로 잘못 들어와도 백업 시간이 장 종료 전으로 당겨지지 않게 하기 위해서입니다.
    archive_at = krx_end + timedelta(minutes=max(window["post_minute"], 0))

    # 아래는 계산된 일정을 콘솔에 출력하는 부분입니다.
    print(f"📅 차기 거래일: {today.isoformat()} (월요일)")
    print(f"--------------------------------------------------")
    print(f"🕒 {approval_refresh_at.strftime('%H:%M')} : 승인키 미리 갱신 (10분 전)")
    print(f"🕒 {nxt_start.strftime('%H:%M')} : 프리마켓 시세 수집 시작 (nxt)")
    print(f"🕒 {nxt_end.strftime('%H:%M')} : 프리마켓 시세 수집 종료")
    print(f"🕒 {krx_start.strftime('%H:%M')} : 정규장 시세 수집 시작 (krx)")
    print(f"🕒 {krx_end.strftime('%H:%M')} : 정규장 시세 수집 종료")
    print(f"🕒 {archive_at.strftime('%H:%M')} : 데이터 백업 시작 (nxt, krx)")
    print(f"--------------------------------------------------")


# 이 파일을 직접 실행했을 때만 main()을 실행합니다.
#
# 예:
#   python schedule_check.py
#
# 다른 파일에서 import할 경우에는 main()이 자동 실행되지 않습니다.
# 즉, 함수만 재사용할 수 있게 해주는 안전장치입니다.
if __name__ == "__main__":
    main()