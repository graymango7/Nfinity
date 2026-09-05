/* SideGig AI 데모 웹앱 — API 호출 공통 헬퍼
   백엔드와 같은 origin에서 서빙되므로 상대 경로로 fetch합니다 (CORS 불필요).
   app/security.py의 X-API-Key 게이트가 켜진 배포本이라면, 배포 시 이 파일의
   API_KEY 값을 채워서 다시 배포하세요 (README 6단계 참고). 로컬 개발 중에는
   비워둬도 됩니다. */
const API_KEY = "";

async function apiGet(path) {
  return apiRequest(path, "GET");
}

// 9/2 세 번째 업데이트: connect.html이 그동안 자체적으로 만들어 쓰던 POST 헬퍼를
// 여기로 옮기고, Risk Shield 설정(PUT)에 쓸 apiPut도 추가했습니다 — 페이지마다
// fetch 로직을 따로 두지 않도록 공통화.
async function apiPost(path, body) {
  return apiRequest(path, "POST", body);
}

async function apiPut(path, body) {
  return apiRequest(path, "PUT", body);
}

async function apiRequest(path, method, body) {
  const headers = {};
  if (API_KEY) headers["X-API-Key"] = API_KEY;
  const opts = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let respBody = null;
  try {
    respBody = await res.json();
  } catch (_) {
    /* 본문이 JSON이 아닌 경우(거의 없음) */
  }
  if (!res.ok) {
    const message = respBody?.error?.message || `요청에 실패했어요 (HTTP ${res.status})`;
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return respBody;
}

function qs(name, fallback = "") {
  return new URLSearchParams(location.search).get(name) ?? fallback;
}

function withPersonaParams(href) {
  const params = new URLSearchParams({
    user_id: qs("user_id"),
    name: qs("name"),
    emoji: qs("emoji"),
    job: qs("job"),
  });
  return `${href}?${params.toString()}`;
}

function fmtWon(n) {
  return `${Math.round(n).toLocaleString("ko-KR")}원`;
}

function fmtPct(rate) {
  return `${Math.round(rate * 100)}%`;
}

function renderPersonaBar() {
  const uid = qs("user_id");
  if (!uid) {
    location.href = "/index.html";
    return;
  }
  document.querySelectorAll(".persona-name").forEach((el) => (el.textContent = qs("name", "게스트")));
  document.querySelectorAll(".persona-job").forEach((el) => (el.textContent = qs("job", "")));
  document.querySelectorAll(".persona-avatar").forEach((el) => (el.textContent = qs("emoji", "🙂")));
  document.querySelectorAll(".nav-item").forEach((el) => {
    if (!el.dataset.nav) return;
    el.href = withPersonaParams(el.dataset.nav);
    el.classList.toggle("active", el.dataset.nav === location.pathname);
  });
  const switchLink = document.querySelector(".switch-link");
  if (switchLink) switchLink.href = "/index.html";
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function errorHtml(message) {
  return `<div class="state-box error">${escapeHtml(message)}<br/><span style="font-size:11px; color:var(--ink-faint); font-weight:500;">데모 서버가 켜져 있는지 확인해주세요.</span></div>`;
}

// 9/2 세 번째 업데이트: 기획서 8번이 요구하는 AI 결과 면책 문구를 공통 컴포넌트로.
// text를 안 넘기면 가장 일반적인 문구를 씁니다.
function disclaimerHtml(text) {
  const msg =
    text ||
    "AI가 계산한 참고용 추정치예요. 세무 신고, 보험료 고지, 신용·대출 심사에는 사용되지 않아요.";
  return `<div class="disclaimer-box">
    <svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2"/><path d="M12 8V13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="12" cy="16.2" r="1.1" fill="currentColor"/></svg>
    <span>${escapeHtml(msg)}</span>
  </div>`;
}
