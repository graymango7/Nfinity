"""프로젝트 아카이브 PDF 생성기.

공모전 제출이 끝난 뒤 이 프로젝트를 통째로 보관하기 위한 스크립트다.
저장소의 실제 내용(README, 소스, 커밋 이력)에서 문서를 만들어 내므로,
코드가 바뀌면 다시 돌리기만 하면 아카이브도 함께 갱신된다.

만들어지는 것 (docs/archive/):
  00_프로젝트_개요.pdf    표지 · 무엇을 만들었는지 · 결과 수치 · 개발 타임라인
  01_기획서.pdf           제출본 (docs/기획서.html)
  02_기능명세서.pdf       제출본 (docs/기능명세서.html)
  03_시스템_설계.pdf      API 목록 · 데이터 모델 · 알고리즘 · 보안 조치
  04_개발기록.pdf         nfinity/README.md (개발 과정 전체 기록)
  05_소스코드.pdf         전체 소스 (줄 번호 포함)
  06_검증결과.pdf         성능 재측정 · 백테스트 · 보안 점검 · 화면 캡처

실행: python docs/build_archive.py
필요: markdown 패키지, Chrome (헤드리스 PDF 변환)
"""
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "archive"
OUT.mkdir(exist_ok=True)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"

CSS = """
@page { size: A4; margin: 16mm 14mm; }
* { box-sizing: border-box; }
body { margin:0; color:#14181f; background:#fff; font-size:10pt; line-height:1.6;
  font-family:"Pretendard",-apple-system,"Malgun Gothic",sans-serif; letter-spacing:-0.01em; }
.doc { max-width:186mm; margin:0 auto; padding:8mm 0; }
h1 { font-size:20pt; letter-spacing:-0.03em; margin:0 0 3mm; }
h2 { font-size:13pt; margin:7mm 0 2.5mm; padding-bottom:1.5mm; border-bottom:1.5px solid #14181f;
  letter-spacing:-0.02em; page-break-after:avoid; }
h3 { font-size:11pt; margin:5mm 0 1.5mm; letter-spacing:-0.02em; page-break-after:avoid; }
h4 { font-size:10pt; margin:4mm 0 1mm; }
p,li { margin:0 0 1.6mm; }
ul,ol { margin:0 0 2.4mm; padding-left:5mm; }
table { width:100%; border-collapse:collapse; margin:2mm 0 3mm; font-size:9pt; }
th,td { border:1px solid #c9ced6; padding:1.8mm 2.2mm; vertical-align:top; text-align:left; }
th { background:#f2f4f6; font-weight:600; }
code { font-family:Consolas,monospace; font-size:8.6pt; background:#f2f4f6; padding:0 1mm; border-radius:1mm; }
pre { background:#f7f8fa; border:1px solid #e5e8eb; border-radius:2mm; padding:2.5mm 3mm;
  font-family:Consolas,monospace; font-size:7.6pt; line-height:1.45; white-space:pre-wrap;
  word-break:break-all; margin:1.5mm 0 3mm; }
pre code { background:none; padding:0; font-size:inherit; }
blockquote { border-left:3px solid #8b95a1; background:#f7f8fa; margin:2mm 0; padding:2mm 3mm; }
.cover { text-align:center; padding:45mm 0 0; page-break-after:always; }
.cover h1 { font-size:32pt; margin-bottom:4mm; }
.cover .sub { font-size:13pt; color:#4e5968; margin-bottom:20mm; }
.cover table { width:120mm; margin:0 auto; font-size:10pt; }
.kpi { display:flex; gap:3mm; margin:3mm 0; }
.kpi div { flex:1; border:1px solid #c9ced6; border-radius:2mm; padding:2.5mm; text-align:center; }
.kpi b { display:block; font-size:13pt; letter-spacing:-0.03em; }
.kpi span { font-size:8.4pt; color:#4e5968; }
.note { background:#f7f8fa; border-left:3px solid #8b95a1; padding:2.5mm 3mm; margin:2.5mm 0; font-size:9pt; }
.file { page-break-inside:avoid; margin-bottom:4mm; }
.file h3 { background:#eef1f4; padding:1.5mm 2.5mm; border-radius:1.5mm; font-family:Consolas,monospace;
  font-size:9pt; margin:0 0 1.5mm; }
.meta { font-size:8.4pt; color:#8b95a1; margin-bottom:1mm; }
img { max-width:100%; border:1px solid #e5e8eb; border-radius:2mm; margin:2mm 0; }
section { page-break-inside:avoid; }
"""


def html_page(title: str, body: str) -> str:
    return f"<title>{title}</title><style>{CSS}</style><div class='doc'>{body}</div>"


def to_pdf(name: str, html: str) -> None:
    tmp = OUT / f"_{name}.html"
    tmp.write_text(html, encoding="utf-8")
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={OUT / (name + '.pdf')}", str(tmp)],
        capture_output=True,
    )
    tmp.unlink(missing_ok=True)
    print(f"  생성: {name}.pdf")


def md_to_html(text: str) -> str:
    import markdown
    return markdown.markdown(text, extensions=["tables", "fenced_code", "nl2br"])


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def git(*args) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True, encoding="utf-8").stdout


# ── 00. 개요 ───────────────────────────────────────────────────────────
def build_overview():
    commits = [l for l in git("log", "--reverse", "--format=%ad|%s",
                              "--date=format:%m/%d").splitlines() if l.strip()]
    rows = "".join(
        f"<tr><td style='width:14mm'>{c.split('|',1)[0]}</td><td>{esc(c.split('|',1)[1])}</td></tr>"
        for c in commits
    )
    body = f"""
<div class="cover">
  <h1>SideGig AI</h1>
  <div class="sub">N잡러의 현금흐름·세금 예측 서비스<br>2026 금융 AI Challenge 출품작 아카이브</div>
  <table>
    <tr><th>팀명</th><td>Nfinity</td></tr>
    <tr><th>구성원</th><td>조소현, 이은겸, 최하은</td></tr>
    <tr><th>배포 URL</th><td>https://nfinity-ai-o2ok.onrender.com</td></tr>
    <tr><th>저장소</th><td>github.com/graymango7/Nfinity</td></tr>
    <tr><th>아카이브 일자</th><td>{date.today().isoformat()}</td></tr>
  </table>
</div>

<h2>이 아카이브에 담긴 것</h2>
<table>
  <tr><th style="width:44mm">문서</th><th>내용</th></tr>
  <tr><td>00_프로젝트_개요</td><td>무엇을 만들었는지, 결과 수치, 개발 타임라인 (이 문서)</td></tr>
  <tr><td>01_기획서</td><td>공모전 제출 기획서</td></tr>
  <tr><td>02_기능명세서</td><td>공모전 제출 기능명세서</td></tr>
  <tr><td>03_시스템_설계</td><td>API 목록, 데이터 모델, 알고리즘, 보안 조치</td></tr>
  <tr><td>04_개발기록</td><td>개발 과정 전체 기록 (발견한 버그와 그 원인 포함)</td></tr>
  <tr><td>05_소스코드</td><td>전체 소스 (줄 번호 포함)</td></tr>
  <tr><td>06_검증결과</td><td>성능 재측정, 예측 백테스트, 보안 점검, 화면</td></tr>
</table>

<h2>한 문단 요약</h2>
<p>배달·프리랜서·콘텐츠 등 <b>여러 플랫폼에서 불규칙하게 수입을 얻는 N잡러</b>를 위한 웹 서비스다.
플랫폼마다 다른 정산 주기를 학습해 <b>앞으로 45일의 잔액을 하루 단위로 예측</b>하고, 잔고가 최소
안전선을 밑도는 날짜를 특정한다. 여기에 2026년 세법·건강보험료 산식을 적용해 내년 5월 종합소득세와
월 보험료를 추정하고, 연 2,000만원 경계를 넘기 전에 경고한다. 규칙 엔진과 이상탐지 모델로 본인이
하지 않은 결제를 걸러내고, 생성형 AI가 계산 결과를 문장으로 설명하며 업무경비를 분류한다.</p>
<p>기존 금융 앱이 <b>과거 지출을 집계</b>하는 데 반해, 이 서비스는 은행이 볼 수 없는
<b>플랫폼 정산 데이터로 미래를 계산</b>한다는 점이 다르다.</p>

<h2>최종 상태</h2>
<div class="kpi">
  <div><b>4,978건</b><span>실거래 재현 검증</span></div>
  <div><b>67.7%</b><span>이상거래 탐지율</span></div>
  <div><b>4.9%</b><span>정상거래 오탐률</span></div>
  <div><b>45일</b><span>일 단위 예측</span></div>
</div>
<table>
  <tr><th style="width:38mm">항목</th><th>내용</th></tr>
  <tr><td>구현 기능</td><td>현금흐름 예측, 세금·건보료 추정, 이상거래 탐지, 업무경비 자동 분류,
    AI 브리핑, 대응 가이드, 직접 입력 시뮬레이션, 예산 관리, Gig Score, 예측 정확도 검증 API</td></tr>
  <tr><td>기술 구성</td><td>FastAPI · PostgreSQL · Redis · scikit-learn(Isolation Forest) ·
    Gemini · 외부 라이브러리 없는 단일 HTML 프론트엔드</td></tr>
  <tr><td>배포</td><td>Render 무료 플랜(Docker). GitHub main 푸시 시 자동 재배포.
    유휴 절전 방지를 위한 자가 핑, 5분 주기 시연 상태 복원 내장</td></tr>
  <tr><td>커밋 수</td><td>{len(commits)}건 (9/5 ~ 9/6)</td></tr>
</table>

<h2>개발 과정에서 스스로 발견해 고친 것</h2>
<p>이 프로젝트의 기록에서 가장 남길 만한 부분이다. 화면상으로는 정상이지만 실제로는 틀렸던 것들이다.</p>
<table>
  <tr><th style="width:42mm">발견</th><th>내용</th></tr>
  <tr><td>IDOR 3건</td><td>① 경로 파라미터로 타인 조회 ② <code>user_id</code>를 생략하면 검사 자체를
    우회(전체 5명 76건 노출을 실제로 재현) ③ 요청 본문으로 타인 예산 변경. 모두 차단·재검증</td></tr>
  <tr><td>예측 기능이 죽어 있었음</td><td>잔액을 "연결된 플랫폼 수입 − 전체 지출"로 유도해
    5명 중 3명이 음수. 그 결과 전원 "1일 후 부족"만 반환 — 예측이 아니라 이미 파산한 상태였다</td></tr>
  <tr><td>정기결제 투사 0건</td><td>25~35일 간격만 정기결제로 인정해 주간·격주 결제를 전부 놓쳐,
    45일 곡선에 지출 이벤트가 하나도 없었다</td></tr>
  <tr><td>AI가 조용히 꺼져 있었음</td><td>모델 서비스 종료로 모든 호출이 404였는데 폴백 응답이
    정상과 똑같이 생겨 밖에서 구분할 수 없었다. <code>/health</code>가 실제 호출 성패를 보고하도록 수정</td></tr>
  <tr><td>성능 수치 과장</td><td>"정상거래 오탐률 1.0%"는 이상탐지 모델 단독 수치였다.
    배포 코드와 동일한 방식으로 재측정해 문서를 정정</td></tr>
  <tr><td>시연 상태 오염</td><td>관람자가 "연결하기"를 누르면 다음 사람이 다른 수치를 보게 되어,
    문서의 확인 절차와 화면이 어긋났다. 5분 주기 자동 복원으로 해결</td></tr>
</table>

<h2>개발 타임라인</h2>
<table><tr><th>일자</th><th>커밋</th></tr>{rows}</table>

<div class="note">
<b>남은 과제</b> — 오픈뱅킹·마이데이터 연동(현재 잔액은 사용자 입력 기준), 세션 기반 인가
(현재는 공개 데모 화이트리스트), 세금 추정의 인적·세액공제 반영, Gig Score의 실제 신용사건 대비 검증,
일일 한도 규칙(R002)·신규 가맹점 규칙(R003) 임계값 완화를 통한 오탐 감소.
</div>
"""
    to_pdf("00_프로젝트_개요", html_page("SideGig AI 아카이브", body))


# ── 03. 시스템 설계 ────────────────────────────────────────────────────
def build_design():
    # 라우터에서 실제 엔드포인트를 추출한다 (문서와 코드가 어긋나지 않도록)
    eps = []
    for py in sorted((ROOT / "nfinity" / "app" / "routers").glob("*.py")):
        src = py.read_text(encoding="utf-8")
        prefix = re.search(r'prefix="([^"]+)"', src)
        base = prefix.group(1) if prefix else ""
        for m in re.finditer(r'@router\.(get|post|put|delete)\("([^"]*)"', src):
            fn = re.search(r"def (\w+)\(", src[m.end():])
            doc = re.search(r'"""(.*?)(?:\n|""")', src[m.end():], re.S)
            eps.append((m.group(1).upper(), base + m.group(2), py.stem,
                        (doc.group(1).strip()[:70] if doc else "")))
    rows = "".join(
        f"<tr><td>{mth}</td><td><code>{esc(path)}</code></td><td>{mod}</td><td>{esc(d)}</td></tr>"
        for mth, path, mod, d in eps
    )

    tables = []
    sql = (ROOT / "nfinity" / "sql" / "init.sql").read_text(encoding="utf-8")
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\);", sql, re.S):
        cols = [c.strip() for c in m.group(2).split("\n") if c.strip() and not c.strip().startswith("--")]
        tables.append((m.group(1), cols))
    tbl_html = "".join(
        f"<h4>{name}</h4><pre>{esc(chr(10).join(cols))}</pre>" for name, cols in tables
    )

    body = f"""
<h1>시스템 설계</h1>

<h2>구성</h2>
<pre>[브라우저] 단일 HTML (외부 라이브러리 없음, 인라인 SVG 차트)
     |
     v  HTTPS
[FastAPI]  라우터 12개 · 미들웨어 4단(보안헤더 / 요청제한 / 데모가드 / CORS)
     |                |                |
     v                v                v
[PostgreSQL]      [Redis]         [Gemini API]
 거래·정산·예산   예산 카운터      브리핑 · 경비분류
 리스크이벤트     LLM 결과 캐시    (실패 시 규칙 폴백)</pre>

<h2>API 목록 (코드에서 자동 추출, {len(eps)}개)</h2>
<table><tr><th style="width:14mm">메서드</th><th style="width:62mm">경로</th>
<th style="width:20mm">모듈</th><th>설명</th></tr>{rows}</table>

<h2>데이터 모델</h2>
{tbl_html}

<h2>핵심 알고리즘</h2>
<h3>1. 45일 현금흐름 예측 (app/cashflow.py)</h3>
<ul>
  <li><b>수입 투사</b>: 플랫폼별 정산 간격의 <b>중앙값</b>과 최근 4회 평균 금액으로 다음 입금을 배치.
      표본이 적어 이상치에 강한 중앙값을 택했다.</li>
  <li><b>지출 투사</b>: 반복 결제를 탐지해(간격 중앙값의 ±25% 이내, 5~40일) 미래 지출로 배치하고,
      나머지는 최근 60일 지출을 일 단위로 균등 배분.</li>
  <li><b>부족일 특정</b>: 시작 잔액에서 하루씩 누적해 최소 안전잔액을 처음 밑도는 날짜를 찾는다.</li>
  <li><b>시나리오</b>: 정산 10일 지연 / 수입 30% 감소를 적용해 부족 시점의 변화를 계산.</li>
</ul>

<h3>2. 세금·건강보험료 추정 (app/tax.py)</h3>
<pre>연 사업소득(90일 실적 × 4)
  → 단순경비율 차감 (업종별)
  → 기본공제 150만원
  → 과세표준
  → 8단계 누진세율 적용 (6%~45%)
  → 지방소득세 10% 가산
  → 기납부 원천징수 3.3% 차감
  = 5월 정산액 (음수면 환급)

건강보험료 = 대상소득 × 7.19% ÷ 12 + 장기요양보험료(건보료 × 12.95%)
  · 직장가입자: 급여 외 소득 중 2,000만원 초과분만 대상 (전액 본인 부담)
  · 지역가입자: 소득 전체 대상
  · 2,000만원 경계 도달 전 남은 금액을 함께 경고</pre>

<h3>3. 이상거래 탐지 (app/rule_engine.py + app/anomaly_model.py)</h3>
<ul>
  <li><b>규칙 7종</b>: 단건 한도(R001) · 일일 누적 한도(R002) · 신규 가맹점(R003) ·
      이동 속도(R004) · 심야 고액(R005) · 단기 반복(R006) · 허용되지 않은 국가(R007)</li>
  <li><b>Isolation Forest</b>: 금액 z-score, 신규 기기 여부, 직전 거래와의 거리, 가맹점 희소성 4개 피처.
      규칙이 설계상 잡지 못하는 유형(위치 급변·신규 기기)을 담당</li>
  <li><b>앙상블</b>: 확률적 OR — <code>1 − (1−규칙확률) × (1−이상확률)</code>.
      규칙이 이미 최고 위험이면 결과는 그대로 유지된다</li>
</ul>

<h3>4. 생성형 AI 적용 (app/gemini_client.py)</h3>
<ul>
  <li><b>브리핑</b>: 계산이 끝난 수치만 전달하고 2~3문장 설명과 권고 한 줄을 생성.
      새로운 숫자를 만들지 말라고 지시해 환각을 차단</li>
  <li><b>경비 분류</b>: 직업·가맹점·시간대·금액으로 인정 가능성(%)과 태그를 생성</li>
  <li><b>장애 대응</b>: 모델 체인을 순서대로 시도하고, 실패 시 쿨다운을 두어 지연을 제한하며,
      최종 실패 시 규칙·템플릿으로 자동 강등. 결과는 Redis에 영구 캐싱</li>
</ul>

<h2>보안 조치</h2>
<table>
  <tr><th style="width:40mm">항목</th><th>내용</th></tr>
  <tr><td>SQL 인젝션</td><td>전 쿼리 SQLAlchemy <code>text()</code> + 바인드 파라미터.
    문자열 조합으로 값을 끼워넣는 곳 없음</td></tr>
  <tr><td>XSS</td><td>프론트의 모든 동적 텍스트에 <code>escapeHtml()</code> 적용
    (LLM 출력이 들어오는 필드가 있어 특히 중요)</td></tr>
  <tr><td>접근 제어</td><td><code>DemoUserGuardMiddleware</code>가 경로·쿼리의 user_id를 검사하고,
    본문으로 받는 엔드포인트는 라우터에서 직접 확인</td></tr>
  <tr><td>보안 헤더</td><td>CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy,
    Permissions-Policy, HSTS(HTTPS 시). 처리되지 않은 500 응답에도 부착</td></tr>
  <tr><td>요청 제한</td><td>IP당 분당 120회 (<code>/api/v1/*</code>)</td></tr>
  <tr><td>컨테이너</td><td>비루트 사용자(appuser)로 실행</td></tr>
  <tr><td>비밀 관리</td><td><code>.env</code>는 커밋 제외. 키는 배포 환경변수로만 주입</td></tr>
  <tr><td>의존성</td><td>pip-audit로 취약점 35건 확인 후 패치</td></tr>
</table>
"""
    to_pdf("03_시스템_설계", html_page("시스템 설계", body))


# ── 04. 개발기록 / 인터뷰 가이드 ───────────────────────────────────────
def build_devlog():
    md = (ROOT / "nfinity" / "README.md").read_text(encoding="utf-8")
    guide = (DOCS / "인터뷰_가이드.md")
    body = "<h1>개발 기록</h1><div class='note'>저장소 <code>nfinity/README.md</code>의 내용이다. " \
           "개발 중 발견한 문제와 그 원인, 판단 근거가 시간순으로 기록되어 있다.</div>"
    body += md_to_html(md)
    if guide.exists():
        body += "<h1 style='page-break-before:always'>부록 · 사용자 인터뷰 가이드</h1>"
        body += md_to_html(guide.read_text(encoding="utf-8"))
    to_pdf("04_개발기록", html_page("개발 기록", body))


# ── 05. 소스코드 ───────────────────────────────────────────────────────
def build_source():
    groups = [
        ("애플리케이션", sorted((ROOT / "nfinity" / "app").rglob("*.py"))),
        ("데이터 파이프라인", sorted((ROOT / "nfinity" / "data_pipeline").glob("*.py"))),
        ("스크립트", sorted((ROOT / "nfinity" / "scripts").glob("*.py"))),
        ("테스트", sorted((ROOT / "nfinity" / "tests").glob("*.py"))),
        ("스키마 · 배포", [ROOT / "nfinity" / "sql" / "init.sql", ROOT / "nfinity" / "Dockerfile",
                        ROOT / "nfinity" / "docker-compose.yml", ROOT / "nfinity" / "requirements.txt",
                        ROOT / "render.yaml"]),
        ("프론트엔드", [ROOT / "nfinity" / "frontend" / "index.html"]),
    ]
    body = "<h1>소스코드</h1><div class='note'>주석을 포함한 전체 소스다. " \
           "이 프로젝트는 '왜 그렇게 했는지'를 주석에 남기는 방식으로 작성되어, 코드만으로도 " \
           "판단 근거를 따라갈 수 있다.</div>"
    total = 0
    for title, files in groups:
        files = [f for f in files if f.exists() and f.stat().st_size > 0]
        if not files:
            continue
        body += f"<h2>{title}</h2>"
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            lines = text.split("\n")
            total += len(lines)
            numbered = "\n".join(f"{i:4d}  {esc(l)}" for i, l in enumerate(lines, 1))
            rel = f.relative_to(ROOT).as_posix()
            body += f"<div class='file'><h3>{rel}</h3>" \
                    f"<div class='meta'>{len(lines)}줄</div><pre>{numbered}</pre></div>"
    body = body.replace("<h1>소스코드</h1>", f"<h1>소스코드</h1><p>총 {total:,}줄</p>", 1)
    to_pdf("05_소스코드", html_page("소스코드", body))


# ── 06. 검증결과 ───────────────────────────────────────────────────────
def build_verification():
    imgs = ""
    for p, cap in [("img/01_landing.png", "첫 화면 — 서비스 소개와 시연 인물 선택"),
                   ("img/02_dashboard.png", "대시보드 — 상황 요약 · 할 일 · 45일 예측 차트")]:
        if (DOCS / p).exists():
            imgs += f"<h3>{cap}</h3><img src='{p}'>"

    body = f"""
<h1>검증 결과</h1>
<div class="note">모두 배포본 또는 배포 코드와 동일한 방식으로 직접 측정한 결과다.
측정 스크립트를 다시 돌리면 같은 값이 재현된다.</div>

<h2>1. 이상거래 탐지 성능</h2>
<p>가상 거래 <b>4,978건</b>(이상 263건 · 정상 4,715건)을 시간순으로 재현해,
배포 코드와 동일한 프로파일 산식·이력 윈도우(180일)·앙상블 방식으로 측정했다.</p>
<table>
  <tr><th style="width:44mm">지표</th><th>값</th></tr>
  <tr><td>탐지율 (Recall)</td><td><b>67.7%</b> — 이상 263건 중 178건 포착</td></tr>
  <tr><td>정상거래 오탐률</td><td><b>4.9%</b> — 정상 4,715건 중 230건</td></tr>
  <tr><td>정밀도 (Precision)</td><td>43.6%</td></tr>
  <tr><td>오탐 기여 규칙</td><td>신규 가맹점(R003) 95건 · 일일 누적 한도(R002) 90건 —
    두 규칙의 임계값이 보수적으로 설정된 것이 원인. 완화가 다음 과제</td></tr>
  <tr><td>이상탐지 모델 단독</td><td>규칙이 놓친 유형을 <b>20건 추가 포착</b>, 단독 오탐 46건(1.0%)</td></tr>
</table>
<div class="note">초기 문서에는 "탐지율 73.4% / 오탐률 1.0%"로 기재되어 있었다.
재측정 결과 73.4%는 현재 코드에서 재현되지 않았고, 1.0%는 <b>이상탐지 모델 단독</b> 수치를
시스템 전체 성능으로 잘못 제시한 것이었다. 제출 전 위 값으로 정정했다.</div>

<h2>2. 예측 정확도 (홀드아웃 검증)</h2>
<p>각 플랫폼·가맹점의 <b>마지막 발생 건을 감춘 뒤</b> 그 이전 기록만으로 예측해 실제와 비교했다.
배포본의 <code>GET /api/v1/validation/forecast</code>에서 직접 확인할 수 있다.</p>
<table>
  <tr><th style="width:44mm">대상</th><th>결과</th></tr>
  <tr><td>정산 금액 예측</td><td>평균 절대 오차 <b>18.9%</b> (표본 9)</td></tr>
  <tr><td>반복 지출 발생일</td><td>평균 오차 <b>4.3일</b>, ±3일 이내 적중 <b>50%</b> (표본 4)</td></tr>
</table>
<div class="note">정산 <i>날짜</i> 오차는 0일로 나오지만, 시연 데이터가 고정 주기로 생성된 결과이므로
예측력의 근거로 제시하지 않는다 — 구현이 의도대로 동작함을 확인한 수준으로만 본다.
확정적인 수치는 실제 정산 데이터를 연동한 뒤 재측정해야 한다.</div>

<h2>3. 보안 점검</h2>
<table>
  <tr><th style="width:52mm">검사</th><th style="width:20mm">결과</th><th>내용</th></tr>
  <tr><td>비데모 user_id 조회</td><td><b>403</b></td><td>shield · tax · gig-score · brief · guidance ·
    risk · budgets · income 전 경로</td></tr>
  <tr><td>user_id 생략 우회</td><td><b>422</b></td><td>이전에는 전체 5명 76건이 무인증 노출됐다</td></tr>
  <tr><td>본문 user_id 쓰기</td><td><b>403</b></td><td>budgets · risk/assess · shield/settings</td></tr>
  <tr><td>음수·과대 limit</td><td><b>422</b></td><td>이전에는 500 오류 또는 LLM 호출 폭주</td></tr>
  <tr><td>정상 요청</td><td><b>200</b></td><td>3인 × 13개 엔드포인트 전부 정상</td></tr>
</table>

<h2>4. 화면</h2>
{imgs}
"""
    to_pdf("06_검증결과", html_page("검증 결과", body))


def copy_submission():
    """제출 문서는 기존 HTML을 그대로 다시 PDF로 굽는다."""
    for src, name in [("기획서.html", "01_기획서"), ("기능명세서.html", "02_기능명세서")]:
        p = DOCS / src
        if not p.exists():
            print(f"  건너뜀: {src} 없음")
            continue
        subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={OUT / (name + '.pdf')}", str(p)],
            capture_output=True,
        )
        print(f"  생성: {name}.pdf")


if __name__ == "__main__":
    print(f"아카이브 생성 → {OUT}")
    build_overview()
    copy_submission()
    build_design()
    build_devlog()
    build_source()
    build_verification()
    print("\n완료:")
    for f in sorted(OUT.glob("*.pdf")):
        print(f"  {f.name:28} {f.stat().st_size // 1024:>5} KB")
