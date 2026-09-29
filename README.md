# 키움 REST 기반 주식 모의투자 프로그램

Python으로 구현한 개인 학습 프로젝트입니다. 키움 REST API로 시세를 수집하고 SQLite에 저장한 데이터를 활용해 종목 후보 평가, 로컬 모의체결, 위험 관리와 웹 대시보드를 제공합니다. 생성형 AI를 활용하여 개발했습니다.

**현재 주문 실행은 로컬 SQLite 모의체결입니다. 실제 증권사 매수·매도 주문 전송은 구현되어 있지 않습니다.**

## 주요 구성

- `data/`, `kiwoom_rest_client.py`: 시세·캔들·계좌 조회와 API 호출 관리
- `screening/`, `strategy/`: 종목 후보 평가와 진입 전략
- `execution/`, `paper/`, `risk/`: 모의체결과 위험 관리
- `db/`, `domain/`: SQLite 저장과 데이터 모델
- `backtest/`: 저장된 데이터 기반 백테스트 코드
- `dashboard/`, `operations/`: 로컬 웹 대시보드와 운영 제어
- `information/`, `ai_shadow/`, `notifications/`: 선택적 뉴스·공시·AI 관찰·Telegram 기능
- `tests/`: unittest 테스트 코드

## 기술

Python 3.10 이상, requests, SQLite, unittest, HTML/CSS/JavaScript를 사용합니다. 대시보드는 Python `http.server`로 실행됩니다.

## 설치 및 실행

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --help

$env:AUTO_TRADER_DATA_DIR = 'runtime/demo'
$env:TELEGRAM_ENABLED = 'false'
$env:AI_SHADOW_ENABLED = 'false'
.\.venv\Scripts\python.exe main.py dashboard --no-browser
```

대시보드 주소는 `http://127.0.0.1:8765`입니다. 인터넷 공개 서버로 사용하기 위한 구조는 아닙니다.

`.env.example`은 설정 참고용이며 `.env` 파일을 자동으로 읽지는 않습니다. 외부 조회 기능을 사용하려면 `KIWOOM_APP_KEY`, `KIWOOM_SECRET_KEY` 등 필요한 값을 자신의 환경변수로 설정해야 합니다. 키와 토큰은 소스나 Git에 기록하지 마세요. 선택적 PDF 점검 도구의 의존성은 `requirements-tools.txt`에 분리되어 있습니다.

## 테스트 실행

별도 환경에서 실제 서비스 자격증명을 제거하고 실행하세요.

```powershell
python -m unittest discover -s tests -v
```

## 공개 범위

이 저장소에는 소스·테스트 코드와 설정 예제·실행 안내만 포함합니다. 실제 API 키, 계좌·거래 데이터, 운영 DB, 로그, 백업, 생성 결과, 분석·검증 보고서, 개인 학습·이력서 문서 및 키움 API 원본 PDF/발췌는 포함하지 않습니다. 백테스트와 보고서 생성 모듈은 코드만 제공하며 실행 결과는 제공하지 않습니다.

수익성과 실전 운용 안전성을 검증한 서비스가 아닌 학습용 모의투자 프로젝트입니다.
