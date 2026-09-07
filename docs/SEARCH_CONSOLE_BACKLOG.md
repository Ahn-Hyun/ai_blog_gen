# 영어 블로그 Search Console 편집 목록

`scripts/search_console_backlog.py`는 검색 성과를 읽고 기존 발행 글의 검토 목록을 만든다. 본문·제목·발행일은 수정하지 않는다. 글 생성 워크플로와 독립적이므로 검색 수집 실패가 발행을 막지 않는다.

## 수집과 판단

- 영어 `ship-write.com/blog/`만 대상으로 Web 검색을 조회한다. 전체 국가이며 영어 검색어만 걸러낸 수치는 아니다.
- PT 기준 오늘에서 3일 전을 종료일로 삼고 최근 28일과 이전 28일을 비교한다. `dataState=final`; 필요하면 `--end-date YYYY-MM-DD`로 재현한다.
- 페이지 합계, 페이지·검색어, 페이지·국가·기기, 날짜를 두 기간에 각각 조회한다. 한 요청당 25,000행 이후에는 페이지네이션한다.
- 검색어 합계는 익명 검색어 누락 때문에 페이지 합계가 아니다. 국가·기기 행도 페이지 합계와 합산하지 않는다. API가 반환하는 상위 행은 전체 데이터와 다를 수 있다.
- 발행된 로컬 MDX에 연결되는 페이지 중 노출 100 이상인 글을 검토한다. CTR 2% 미만·평균 순위 20 이내, 순위 8–20, 또는 이전 클릭 10 이상에서 30% 이상 하락한 글을 후보로 삼는다. 현재 노출순으로 정렬한다.
- 이 수치는 초기 편집 규칙이며 통계적 유의성, 보편적인 적정 CTR, 예상 추가 클릭을 뜻하지 않는다. 반환되지 않은 이전 기간 값은 0으로 채우지 않는다.
- 보고서에는 기존 제목·원고 경로·기간별 지표·검색어·국가/기기·검토할 작업이 들어간다. 실제 문장 수정은 편집 단계에서 근거와 함께 검토한다.

## 인증 연결 — 최초 한 번

Gemini API 키는 Search Console 권한이 아니다. 로컬 기본 ADC도 자동 사용하지 않는다.

1. 사용할 Google Cloud 프로젝트에서 **Google Search Console API**를 활성화한다.
2. 영어 블로그용 서비스 계정을 준비하고, Search Console의 `ship-write.com` 속성 설정 → 사용자 및 권한에서 그 계정 이메일에 검색 성과 읽기 권한을 부여한다. 기존 전용 OAuth `authorized_user` JSON이 있으면 그것도 지원한다.
3. 자격증명 JSON은 저장소 밖의 비공개 경로에 둔다. 채팅이나 Git에 키를 붙이지 않는다.
4. 실제 등록된 속성이 도메인 속성이면 `sc-domain:ship-write.com`, URL-prefix 속성이면 `https://ship-write.com/`를 지정한다. 등록되지 않은 속성을 대신 만들거나 한국어 속성으로 대체하지 않는다.

```sh
export GOOGLE_APPLICATION_CREDENTIALS=/absolute/private/path/shipwrite-search-console.json
export GSC_SITE_URL=sc-domain:ship-write.com
.venv/bin/python scripts/search_console_backlog.py
```

성공하면 `data/search-console/<실행시각>/`에 `snapshot.json`, `backlog.json`, `backlog.md`를 만든다. 해당 디렉터리는 gitignored다. 인증·권한·API 오류는 실패 종료하고 새 보고서를 만들지 않는다. 권한이 있어도 반환 데이터가 없으면 `no_data` 보고서를 만든다.

## GitHub Actions 연결

저장소가 공개이므로 성과 보고서 원문을 로그·Step Summary·아티팩트에 공개하지 않는다. Actions에서는 AES-GCM으로 암호화한 `report.enc`만 90일간 보관한다. 암호화 키는 자격증명과 별개의 GitHub secret이다.

```sh
gh secret set GSC_CREDENTIALS_JSON --repo Ahn-Hyun/ai_blog_gen < "$GOOGLE_APPLICATION_CREDENTIALS"
gh variable set GSC_SITE_URL --repo Ahn-Hyun/ai_blog_gen --body "$GSC_SITE_URL"
```

`GSC_REPORT_KEY`는 무작위 32바이트의 base64 값이다. 비공개 로컬 파일에 보관한 뒤 stdin으로 GitHub secret에 등록한다. 분실하면 과거 암호화 보고서를 복호화할 수 없다. 키 값을 로그나 채팅에 출력하지 않는다.

```sh
gh secret set GSC_REPORT_KEY --repo Ahn-Hyun/ai_blog_gen < /absolute/private/path/gsc-report-key.txt
gh workflow run search_console.yml --repo Ahn-Hyun/ai_blog_gen
```

수동 실행에서 실제 API 수집과 로컬 복호화를 확인한 후에만 `GSC_ENABLED=true`를 설정한다. 그 전에는 예약 실행을 건너뛴다. 활성화 후 월요일 15:20 UTC(화요일 00:20 KST)에 실행한다. GitHub 예약은 지연되거나 장기 미활동으로 비활성화될 수 있다.

```sh
gh variable set GSC_ENABLED --repo Ahn-Hyun/ai_blog_gen --body true
# 비활성화: 위 명령의 값을 false로 변경
```

아티팩트 다운로드 후 로컬에서 복호화한다. 키 읽기 예시는 zsh/bash 기준이다.

```sh
export GSC_REPORT_KEY="$(cat /absolute/private/path/gsc-report-key.txt)"
.venv/bin/python scripts/search_console_backlog.py --decrypt /absolute/path/report.enc --output data/search-console/review
```

보고서의 검토 목록에 담당·변경 내용·변경일·후속 확인일을 남긴다. 단순 전후 차이를 인과효과로 해석하지 않는다. 실제 클릭 증가나 검색량은 인증 후 수집 전에는 주장하지 않는다.

## 검사

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

28일 경계, API 페이지네이션·재시도, 인증 실패/데이터 없음 구분, 페이지 합계와 검색어 합계 분리, 표본 기준, 영어 발행 글 연결, 보고서 출력 이스케이프, 암호화·변조 거절을 검사한다. 합성 데이터 검사는 실제 Search Console 연결 검증을 대신하지 않는다.

참고: [Search Analytics query](https://developers.google.com/webmaster-tools/v1/searchanalytics/query), [인증 범위](https://developers.google.com/webmaster-tools/v1/how-tos/authorizing), [속성 목록](https://developers.google.com/webmaster-tools/v1/sites/list).
