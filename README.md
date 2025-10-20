# Hermes Rate Limit Tester

Hermes 프로젝트에서 API 한계 (Rate Limit)을 빠르게 확인할 수 있도록 구성한 GUI/CLI 통합 도구입니다. 기존 사내 도구를 기반으로 구성 요소를 보강했습니다.

## 설치

```bash
pip install -r requirements.txt
```

## GUI 실행

```bash
python -m hermes.tools.rate_limit_tester
```

- URL, RPS, 실행 시간, 타임아웃, HTTP 메서드를 지정하고 **시작**을 누르면 요청을 전송합니다.
- 헤더와 본문은 텍스트 영역에서 바로 수정하거나 파일로 불러올 수 있습니다.
- 실행 후 `요약 보기`로 통계를 확인하고, `로그 저장`으로 실행 로그를 파일로 보관할 수 있습니다.
- 구성(JSON)은 저장/불러오기 기능으로 재사용할 수 있습니다.

## CLI(headless) 실행

```bash
python -m hermes.tools.rate_limit_tester --headless --url https://example.com/health --rps 10 --duration 60 --timeout 5 --print-log
```

주요 옵션:

- `--preset`: GUI에서 저장한 JSON 구성을 사용합니다.
- `--headers-path`, `--body-path`: 파일에서 헤더/본문을 불러옵니다.
- `--log-file`: 실행 로그를 파일로 저장합니다.
- `--summary-json`: 실행 요약을 JSON으로 저장합니다.

## 변경 사항 요약

- 실행 구성(`RunConfig`)과 결과(`RunSummary`)를 JSON으로 주고받을 수 있도록 정리했습니다.
- GUI에 구성 저장/불러오기, 로그 저장, 요약 JSON/클립보드 복사 기능을 추가했습니다.
- CLI 모드(headless)를 지원해 배치 작업에서도 동일한 로직을 재사용할 수 있습니다.

