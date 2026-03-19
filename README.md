# DOI-to-Zotero Auto-Crawler

DOI 리스트를 입력하면 논문 메타데이터 해석, PDF 다운로드, Zotero 라이브러리 등록까지 자동으로 수행하는 웹 기반 데스크톱 앱.

> 마치 온라인 쇼핑카트에 논문을 담듯이, DOI만 넣으면 Zotero 라이브러리가 자동으로 채워집니다.

---

## 실행 방법

```bash
# 1. Zotero를 완전히 종료한다 (DB 잠금 방지)
# 2. 앱 실행
python3 doi2zotero_app.py

# 3. 브라우저가 자동으로 열림 → http://localhost:8765
# 4. DOI 목록 입력 → [실행] 클릭
```

### 의존성

- Python 3.8 이상
- `requests` 라이브러리 (없으면 자동 설치)
- 추가 GUI 라이브러리 불필요 (tkinter 등 없어도 됨)

```bash
pip install requests
```

---

## 작동 원리

### 전체 파이프라인

```
DOI 입력 → CrossRef 메타데이터 → PDF 다운로드 (3단계) → Zotero DB 기록
```

**Step 1. DOI 파싱**
- 텍스트, 파일(.txt/.csv) 어디서든 `10.XXXX/...` 패턴을 자동 추출
- 중복 제거 후 순서 유지

**Step 2. 메타데이터 해석 (CrossRef API)**
- DOI → 제목, 저자, 저널, 연도, volume, issue, pages, abstract
- item type 자동 판별: journalArticle, book, bookSection, conferencePaper

**Step 3. PDF 다운로드 (3단계 전략)**

| 순서 | 전략 | 설명 |
|------|------|------|
| 1차 | **Unpaywall** | 합법적 Open Access PDF URL 제공 (무료 API) |
| 2차 | **Sci-Hub** | 미러 사이트 순회, iframe/embed에서 PDF URL 파싱 |
| 3차 | **Direct Publisher** | DOI 리다이렉트 → `citation_pdf_url` 메타 태그 또는 출판사별 URL 패턴 |

지원 출판사별 URL 패턴:
- Elsevier/ScienceDirect (`/pdfft` 경로)
- Springer/Nature (`/content/pdf/`)
- Wiley (`/pdfdirect/`)
- PLOS (`/article/file?type=printable`)
- PNAS, Oxford Academic (`.full.pdf`)
- Frontiers, MDPI (`/pdf`)
- Elsevier linkinghub 리다이렉트 자동 추적

**Step 4. Zotero SQLite 직접 기록**
- `items` 테이블에 서지 레코드 생성
- `itemData` / `itemDataValues`에 필드 데이터 삽입
- `creators` / `itemCreators`에 저자 정보 등록
- `collectionItems`에 컬렉션 할당
- `itemAttachments`에 PDF 첨부 레코드 생성
- PDF 파일을 `~/Zotero/storage/{key}/`에 복사

---

## 목적

연구자가 논문을 발견한 뒤 Zotero에 등록하는 과정은 반복적이고 시간이 많이 걸린다:

1. DOI로 논문 페이지 방문
2. 서지정보를 수동 입력하거나 Zotero Connector로 가져오기
3. PDF를 별도로 다운로드하여 첨부
4. 접근 제한된 논문은 Sci-Hub 등을 통해 우회

이 앱은 **DOI 목록만 입력하면** 위 과정 전체를 자동으로 수행한다. 학회, 리뷰 작업, 또는 대량의 참고문헌 수집이 필요한 상황에서 수십~수백 편의 논문을 한 번에 Zotero 라이브러리로 가져올 수 있다.

---

## GUI 사용법

앱을 실행하면 브라우저에 웹 인터페이스가 열린다 (`http://localhost:8765`).

### 입력 필드

- **DOI List**: 한 줄에 하나씩 DOI를 입력하거나 `.txt` / `.csv` 파일 경로를 입력
- **Zotero Data Directory**: Zotero 데이터 폴더 경로 (기본값: `~/Zotero`)
- **Collection Name**: 새로 생성할 컬렉션 이름 (비워두면 컬렉션 없이 추가)

### 실행 과정

1. [실행] 버튼 클릭
2. 진행 바와 색상 코드 로그로 실시간 상태 확인:
   - 🟢 녹색: 성공 (메타데이터 해석, PDF 다운로드 완료)
   - 🟡 노란색: 경고 (PDF 다운로드 실패, 메타데이터만 등록)
   - 🔴 빨간색: 오류 (DOI 해석 실패 등)
3. [중지] 버튼으로 실행 중 취소 가능
4. 완료 후 Zotero를 재시작하면 등록된 논문 확인 가능

---

## 유의사항

### 필수 전제조건

- **Zotero를 반드시 종료한 후 실행할 것**: Zotero가 실행 중이면 SQLite DB가 잠겨서 오류 발생
- Zotero DB 경로가 기본 위치(`~/Zotero`)가 아닌 경우, GUI에서 직접 경로를 수정

### 안전장치

- 실행 시 자동으로 `zotero.sqlite` 백업 생성 (`zotero_backup_{timestamp}.sqlite`)
- 기존 DB 레코드는 수정하지 않음 (INSERT only)
- PDF 검증: `%PDF` 헤더 확인, 최소 5KB 이상 파일만 첨부

### 법적 고려사항

- **Unpaywall**: 합법적 Open Access PDF만 제공 — 문제 없음
- **Sci-Hub**: 저작권 보호 논문의 무단 다운로드에 해당할 수 있음. 사용자 책임 하에 운용
- **Publisher Direct**: robots.txt 및 이용약관에 따라 자동 다운로드가 차단될 수 있음
- 본 도구는 **개인 연구 목적**으로만 사용할 것을 권장

### 동기화 관련

- 이 앱은 **로컬 Zotero SQLite에 직접 기록**하는 방식이므로, Zotero Sync와 충돌할 수 있음
- 대량 등록 후 Zotero 동기화를 수행하면 서버 측과 병합이 필요할 수 있음
- 가능하면 동기화 전에 결과를 확인하고, 필요시 백업에서 복원

### 네트워크 관련

- CrossRef API는 rate limit이 있으므로 대량 요청 시 지연이 발생할 수 있음
- Sci-Hub 미러는 가용성이 변동적이므로 실패할 수 있음
- 일부 출판사는 자동 다운로드를 IP 차단할 수 있으므로 주의

---

## 프로젝트 구조

```
shopping-cart-for-zotero/
├── doi2zotero_app.py   # 메인 앱 (단일 파일, 서버+GUI+로직 통합)
├── README.md           # 이 문서
└── LICENSE             # MIT License
```

---

## 트러블슈팅

| 증상 | 원인 | 해결 |
|------|------|------|
| `database is locked` | Zotero가 실행 중 | Zotero 완전 종료 후 재시도 |
| PDF 다운로드 0% | 네트워크 문제 또는 Sci-Hub 미러 차단 | VPN 변경 또는 시간대 변경 후 재시도 |
| 브라우저가 열리지 않음 | 포트 8765 충돌 | `lsof -i :8765`로 확인 후 해당 프로세스 종료 |
| `requests` 모듈 없음 | pip 미설치 | `pip install requests` 실행 |
| Zotero에 논문이 보이지 않음 | Zotero 재시작 필요 | Zotero를 종료 후 다시 실행 |

---

## 라이선스

MIT License — 자유롭게 수정, 배포 가능.
