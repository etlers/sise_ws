# SISE WS

이 저장소는 도커 컨테이너 기반으로 한국투자증권 시세를 수집하고 적재하는 모듈을 구현하기 위한 작업 공간이다.

## 목적

- `config` 폴더의 설정을 바탕으로 한국투자증권 웹소켓 시세를 구독한다.
- `sise_stock_list.csv`에 정의된 종목을 대상으로 시세를 수집한다.
- `div` 값이 `all`인 종목은 오더북(order book) 정보도 받을 수 있도록 구독 구성을 둔다.
- 수집된 실제 시세 데이터는 종목별 `CSV`로 누적 저장한다.
- 당일 종가는 장마감 후에만 `data/preday_result.csv`에 저장한다.
- 장중 시세는 `data/krx`에 적재한다.
- 프리마켓 시세는 `data/nxt`에 적재하고, 종가 파일은 건드리지 않는다.

## 디렉터리 구조

```text
config/
  approval_key.json
  nxt_component_stock.csv
  sise_stock_list.csv
data/
  preday_result.csv
  krx/
  nxt/
src/
```

## 설정 파일

### `config/approval_key.json`

웹소켓으로 시세를 받아오기 위한 승인키를 저장한다.

저장 정보 예시:

- 승인키 값
- 생성일
- 만료일
- 관련 메타데이터

### `config/sise_stock_list.csv`

수집 대상 종목 목록을 저장한다.

기본적으로 다음과 같은 기준으로 활용한다.

- 종목 코드
- 종목명
- 구분값 `div`
- `div` 값이 `all`이면 오더북 정보도 함께 수집

### `config/deal_tm.json`

장 운영 시간은 `nxt`와 `krx`로 분리해서 관리한다.

- `nxt.start_tm`, `nxt.end_tm`
- `krx.start_tm`, `krx.end_tm`
- `pre_minute`: 시작 시각을 당기는 분 단위
- `post_minute`: `krx.end_tm` 이후 백업을 시작하기까지의 대기 분 단위

### `config/nxt_component_stock.csv`

프리마켓(`nxt`)에서 사용할 네이버 구성종목 목록을 저장한다.

- `source_code`: 원본 ETF 코드
- `source_name`: 원본 ETF 이름
- `rank`: 구성종목 순위
- `stock_cd`: 실제 수집할 종목 코드
- `stock_nm`: 종목명
- `weight_rt`: 구성비중

### `data/preday_result.csv`

장마감 후 네이버에서 가져온 당일 종가를 저장한다.
프리마켓(NXT) 수집과는 분리되어 있으며, 프리마켓 단계에서는 이 파일을 읽거나 쓰지 않는다.

저장 컬럼:

- `stock_code`
- `date`
- `close`
- `index_name`

장마감 후 `krx.end_tm + post_minute` 시점에 네이버 일별시세를 다시 수집해 이 파일을 갱신한다.

### `.env`

승인키를 받아오기 위한 `app.key`와 `app.secret` 값을 저장한다.

예시:

```env
APP_KEY=your_app_key_value
APP_SECRET=your_app_secret_value
KIS_ENV=prod
```

기존 환경변수를 그대로 쓰는 경우 `APP_EKY`와 `SECRET_EKY`도 호환된다.
당일 종가 파일을 다른 컨테이너와 공유하려면 `SISE_SHARED_DATA_PATH=/shared_sise_data/data`를 사용한다.

## 설치

가상환경에서 필요한 패키지를 설치한다.

```bash
pip install -e .
```

## 실행

현재 설정 상태를 확인한다.

```bash
python -m sise_ws bootstrap
```

웹소켓 수집을 시작한다.

```bash
python -m sise_ws run
```

스케줄러를 시작한다.

```bash
python -m sise_ws scheduler
```

컨테이너로 실행할 때는 `sise-scheduler` 서비스와 `sise-scheduler:latest` 이미지를 사용한다.

```bash
docker compose up -d --build sise-scheduler
```

이미지 빌드 및 컨테이너 재생성

```bash
docker compose up -d --build --force-recreate sise-scheduler
```

## 데이터 적재 규칙

- 장중 데이터는 `data/krx/<종목코드>.csv`에 저장한다.
- 장중 오더북 데이터는 `data/krx/<종목코드>_orderbook.csv`에 저장한다.
- 프리마켓 데이터는 `data/nxt/<종목코드>.csv`에 저장한다.
- 프리마켓 오더북 데이터는 `data/nxt/<종목코드>_orderbook.csv`에 저장한다.
- trade CSV 컬럼은 `time`, `price`, `millisec`만 사용한다.
- `millisec`는 같은 `time`이 연속해서 들어오면 `0000`, `0001`, `0002`처럼 순번으로 저장한다.
- trade는 직전 저장 가격과 같으면 저장하지 않는다.
- orderbook CSV는 `time`, `millisec`와 원본 호가 필드를 저장한다.
- 원본 수집 데이터는 종목별로 하나의 CSV 파일에 누적한다.

## 스케줄 규칙

- `config/holiday.csv`에 있는 날짜는 휴장일로 처리한다.
- 토요일과 일요일은 시세를 수집하지 않는다.
- `config/deal_tm.json`의 `nxt.start_tm`과 `krx.start_tm`은 각각 `pre_minute`만큼 당겨서 시작한다.
- `nxt.end_tm`이 되면 프리마켓 수집을 멈춘다.
- `krx.end_tm`이 되면 수집을 종료하고, `post_minute`가 지난 뒤 `nxt`와 `krx`를 모두 백업한다.
- 프리마켓 데이터는 `data/nxt`에 적재한다.
- 장중 데이터는 `data/krx`에 적재한다.
- `krx.end_tm` 이후 `post_minute`가 지난 시점에 `data/krx`와 `data/nxt`의 활성 CSV 파일을 각 폴더의 `backup/YYYY-MM-DD/`로 이동하고, 백업 파일명에는 날짜를 붙인다.
- 각 `backup`에는 최근 20일치만 보관하고, 더 오래된 백업은 자동 삭제한다.
- 프리마켓 `nxt` 수집은 `config/nxt_component_stock.csv`의 구성종목을 읽어서 개별 종목으로 구독하고, trade는 받지 않고 오더북만 받는다.
- 현재 `sise_stock_list.csv` 기준으로 `div=all`은 `069500`, `229200` 두 종목이고, 이 둘만 orderbook을 구독한다.
- 스케줄러가 시작되면 당일이 `주말`, `휴장일`, `거래일`인지와 함께 오늘의 시간표를 로그로 출력한다.

### 당일 종가 저장

- `preday_result.csv`는 장마감 후 `krx.end_tm + post_minute` 시점에만 저장한다.
- 저장 함수는 `scheduler.py`의 `collect_and_save_today_close_result()`이다.
- 이 함수는 네이버 `sise_day.naver`와 `sise.naver`의 일별 시세 영역에서 당일 종가를 읽는다.
- 같은 날짜의 기존 행이 있으면 먼저 지우고, 새 당일 종가로 다시 저장한다.
- 프리마켓 `nxt` 구간에서는 `preday_result.csv`를 읽거나 갱신하지 않는다.
- 네이버 수집에 실패하면 파일을 갱신하지 않고 오류를 올리며 슬랙 관리자에게 알린다.

## 로직 위치

실제 수집 및 가공 로직은 `src` 폴더 아래에 추가한다.

권장 구성 예시:

- 승인키 발급 및 갱신 처리
- 웹소켓 연결 및 재연결 처리
- 종목별 시세 구독 처리
- `div=all` 대상 오더북 수집 처리
- 시장 구분에 따른 `krx` / `nxt` 적재 처리

## 운영 원칙

1. 모든 기준 문서는 항상 `graphify`를 우선 참조한다.
2. 로직에 수정이 생기면 반드시 `graphify`도 함께 업데이트한다.
3. 구현보다 문서와 기준을 먼저 맞추고, 변경 사항은 일관되게 반영한다.

## 다른 컨테이너에서 데이터 참조

이 프로젝트의 `data/` 디렉터리는 호스트 경로 `./data`를 컨테이너에 바인드 마운트해서 사용한다.
당일 종가 파일 `preday_result.csv`는 별도의 공유 경로 `../sise_data/data`에서 읽어와 `./data/preday_result.csv`로 갱신한다.

다른 컨테이너에서 `data/krx`와 `data/nxt`를 참조하려면 다음 조건이 필요하다.

- 같은 호스트의 `./data` 경로를 마운트해야 한다.
- `preday_result.csv`까지 같이 읽으려면 `../sise_data/data`도 읽기 전용으로 마운트해야 한다.
- 읽기 전용으로만 사용할 경우 `:ro` 옵션을 붙이는 편이 안전하다.
- 수집 중인 활성 CSV는 계속 append 되므로, 실시간 참조보다는 `backup/YYYY-MM-DD/` 아래의 백업 파일을 읽는 쪽이 더 안정적이다.
- 당일 종가 파일 `preday_result.csv`는 `data/` 루트에 저장되며, 다른 컨테이너는 같은 공유 볼륨을 마운트해서 읽을 수 있다.

예시:

```yaml
volumes:
  - ./data:/app/data:ro
```

같은 호스트 경로를 공유하지 않으면 다른 컨테이너는 이 프로젝트의 `data/krx`, `data/nxt` 파일을 볼 수 없다.

예시 `docker-compose.yml`:

```yaml
services:
  other-service:
    image: your-image:latest
    volumes:
      - ./data:/app/data:ro
      - ../sise_data/data:/shared_sise_data/data:ro
```

이렇게 마운트하면 다른 컨테이너에서 다음 경로를 읽을 수 있다.

- `/app/data/krx`
- `/app/data/nxt`
- `/app/data/krx/backup`
- `/app/data/nxt/backup`
- `/app/data/preday_result.csv`
- `/shared_sise_data/data/preday_result.csv`

## Notes

- 코드 구조를 볼 때는 `graphify-out/GRAPH_REPORT.md`가 가장 빠른 진입점입니다.
- 구조나 로직을 수정했다면 `graphify-out/`를 먼저 갱신한 뒤 README와 문서를 함께 업데이트하세요.
- 모든 주석은 최대한 한글로 작성합니다.
