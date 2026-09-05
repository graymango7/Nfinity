-- SideGig AI - 초기 DB 스키마 (해커톤 MVP 버전)
-- 이 파일은 docker-compose로 PostgreSQL 컨테이너가 처음 뜰 때 자동 실행되거나,
-- (9/2 추가) app/startup_seed.py가 앱 시작 시 직접 실행합니다 — 관리형 Postgres에
-- 배포해도 스키마가 자동으로 만들어지도록 하기 위함입니다.

-- (참고) gen_random_uuid()는 PostgreSQL 13부터 core에 내장돼 있어서 별도 확장 설치가
-- 필요 없습니다. 대부분의 관리형 Postgres(Railway/Render/Neon/Supabase 등)는 이미
-- PG14 이상이라 그대로 동작합니다 — 혹시 이보다 오래된 Postgres에 연결해서 아래 테이블
-- 생성이 실패한다면 가장 먼저 이 함수 지원 여부를 확인하세요.

-- 1. 사용자 (페르소나별 유저)
CREATE TABLE IF NOT EXISTS users (
    user_id         VARCHAR(50) PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    persona         VARCHAR(100),           -- 예: '김지훈-프리랜서', '김민철-생계형투잡러'
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

-- 2. 사용자 리스크 프로파일
--    실제로는 UserProfileBuilder가 과거 거래를 분석해 채워주는 값이지만,
--    Day1에서는 기본값으로 미리 만들어두고 나중에 배치job으로 갱신합니다.
CREATE TABLE IF NOT EXISTS user_risk_profiles (
    user_id                    VARCHAR(50) PRIMARY KEY REFERENCES users(user_id),
    avg_transaction_amount     NUMERIC(14, 2) DEFAULT 0,
    std_transaction_amount     NUMERIC(14, 2) DEFAULT 0,
    single_transaction_limit   NUMERIC(14, 2) DEFAULT 500000,   -- 단일 거래 한도(기본값)
    daily_limit                NUMERIC(14, 2) DEFAULT 1000000,  -- 일일 누적 한도(기본값)
    avg_daily_transactions     NUMERIC(10, 2) DEFAULT 0,
    allowed_countries          TEXT[] DEFAULT ARRAY['KR'],      -- 허용 국가 코드 목록
    updated_at                 TIMESTAMP NOT NULL DEFAULT now()
);

-- 3. 거래 내역 (B가 만드는 mock_transactions.csv가 최종적으로 여기로 들어옵니다)
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         VARCHAR(50) NOT NULL REFERENCES users(user_id),
    amount          NUMERIC(14, 2) NOT NULL,
    merchant_id     VARCHAR(50),
    merchant_name   VARCHAR(200),
    mcc_code        VARCHAR(10),
    category        VARCHAR(50),            -- C(하은)의 카테고리 분류기가 채워주는 필드
    timestamp       TIMESTAMP NOT NULL,
    latitude        DOUBLE PRECISION,
    longitude       DOUBLE PRECISION,
    country         VARCHAR(5) DEFAULT 'KR',
    device_id       VARCHAR(100),
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_transactions_user_time ON transactions (user_id, timestamp);

-- 4. 리스크 이벤트 (룰 엔진 / AI 모델이 이상 거래를 잡아내면 여기에 기록)
CREATE TABLE IF NOT EXISTS risk_events (
    event_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_id  UUID REFERENCES transactions(transaction_id),
    user_id         VARCHAR(50) NOT NULL REFERENCES users(user_id),
    rule_id         VARCHAR(20),            -- 예: 'R001' ~ 'R007'
    severity        VARCHAR(20),            -- LOW / MEDIUM / HIGH / CRITICAL
    description     TEXT,
    action          VARCHAR(30),            -- ALLOW / FLAG_FOR_REVIEW / REQUIRE_CONFIRMATION / BLOCK
    score           INTEGER,                -- 0~100 종합 리스크 점수
    resolved        BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

-- 5. 예산 설정 (예산통제 기능용)
--    8/26 백엔드 구축 데이에 UNIQUE 제약을 추가했습니다: 한 유저가 같은 카테고리에
--    같은 기간(monthly 등) 예산을 두 번 설정하면 새 행이 또 생기는 게 아니라
--    기존 값을 덮어써야 하기 때문입니다 (app/routers/budgets.py의 ON CONFLICT가
--    이 제약을 필요로 합니다). 이미 테이블이 있는 사람도 이 파일을 다시 실행하면
--    안전하게 제약만 추가됩니다(IF NOT EXISTS 없이 그냥 실행하면 이미 있다는 에러가
--    나니, docker compose down -v 후 새로 올리거나, 아래 ALTER문만 따로 실행하세요).
CREATE TABLE IF NOT EXISTS budgets (
    budget_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              VARCHAR(50) NOT NULL REFERENCES users(user_id),
    category             VARCHAR(50) NOT NULL,
    period                VARCHAR(20) NOT NULL DEFAULT 'monthly',  -- weekly/monthly/quarterly
    limit_amount          NUMERIC(14, 2) NOT NULL,
    alert_thresholds      NUMERIC(4, 2)[] DEFAULT ARRAY[0.5, 0.8, 0.9, 1.0, 1.2],
    rollover_enabled       BOOLEAN DEFAULT FALSE,
    created_at            TIMESTAMP NOT NULL DEFAULT now(),
    updated_at            TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE(user_id, category, period)
);

-- 이미 budgets 테이블이 만들어져 있는 상태에서 이 스크립트를 다시 돌릴 사람들을 위한
-- 보강용 ALTER (제약이 이미 있으면 조용히 건너뜁니다).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'budgets_user_category_period_key'
    ) THEN
        ALTER TABLE budgets ADD CONSTRAINT budgets_user_category_period_key
            UNIQUE (user_id, category, period);
    END IF;
END $$;

-- 6. 카테고리 매핑 사전 (C가 만드는 가맹점명 키워드 -> 카테고리 매핑을 여기 저장)
CREATE TABLE IF NOT EXISTS category_mapping (
    keyword     VARCHAR(100) PRIMARY KEY,
    category    VARCHAR(50) NOT NULL
);

-- 7. 수입 플랫폼 연결 (9/2 추가)
--    실제 오픈뱅킹/마이데이터 연동은 이번 MVP 범위 밖이라, "계좌를 연결하면 그 플랫폼의
--    수입이 보인다"는 흐름을 더미 데이터로 재연합니다. connected가 false인 동안은
--    프론트가 income_events를 보여주지 않다가, POST /api/v1/income/sources/{id}/connect를
--    호출하면(=연결 버튼) true로 바뀌면서 이미 DB에 있던 실제(mock) 데이터가 그제서야
--    화면에 드러나는 방식입니다 — 진짜 계좌 연동과 사용자 체감이 최대한 비슷하도록.
CREATE TABLE IF NOT EXISTS income_sources (
    source_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         VARCHAR(50) NOT NULL REFERENCES users(user_id),
    platform_name   VARCHAR(100) NOT NULL,      -- '배달의민족', '크몽', '유튜브' 등
    platform_type   VARCHAR(30) NOT NULL,       -- '배달' / '프리랜서' / '콘텐츠' / '커머스' / '본업'
    icon_emoji      VARCHAR(10) NOT NULL DEFAULT '💰',
    connected       BOOLEAN NOT NULL DEFAULT FALSE,
    connected_at    TIMESTAMP,
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (user_id, platform_name)
);

-- 8. 플랫폼별 수입/정산 내역 (income_sources 하나당 여러 건)
CREATE TABLE IF NOT EXISTS income_events (
    income_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id       UUID NOT NULL REFERENCES income_sources(source_id),
    user_id         VARCHAR(50) NOT NULL REFERENCES users(user_id),
    amount          NUMERIC(14, 2) NOT NULL,
    memo            VARCHAR(200),
    status          VARCHAR(20) NOT NULL DEFAULT '정산완료',  -- 정산완료 / 정산예정
    settled_at      TIMESTAMP NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_income_events_user_time ON income_events (user_id, settled_at);
CREATE INDEX IF NOT EXISTS idx_income_events_source ON income_events (source_id);

-- 9. 사용자 설정 (9/2 세 번째 업데이트 — Risk Shield용 최소 안전잔액 온보딩)
--    기획서 7-1의 "온보딩: 최소 안전잔액 입력"을 반영합니다. 값이 없으면
--    app/cashflow.py의 DEFAULT_MIN_SAFETY_BALANCE가 대신 쓰입니다.
CREATE TABLE IF NOT EXISTS user_settings (
    user_id             VARCHAR(50) PRIMARY KEY REFERENCES users(user_id),
    min_safety_balance  NUMERIC(14, 2),
    current_balance     NUMERIC(14, 2),
    updated_at          TIMESTAMP NOT NULL DEFAULT now()
);

-- 9/5: current_balance 추가. 이전에는 Risk Shield의 시작 잔액을
-- "연결된 플랫폼 수입 합계 - 전체 지출 합계"로 유도했는데, 수입은 (의도적으로) 연결된
-- 플랫폼만 집계하고 지출은 전부 집계하다 보니 잔액이 구조적으로 음수가 나왔습니다
-- (5명 중 3명이 마이너스). 오픈뱅킹이 없는 이 MVP에서 정직한 모델은 "사용자가 알려준
-- 현재 잔액"을 기준점으로 잡고 거기서부터 앞으로를 시뮬레이션하는 것이라, 컬럼을 두고
-- 데모 페르소나는 시드로 채웁니다. 이미 테이블이 있는 환경을 위한 보강문입니다.
ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS current_balance NUMERIC(14, 2);

-- 샘플 시드 데이터 (동작 확인용 - 나중에 지워도 됩니다)
INSERT INTO users (user_id, name, persona) VALUES
    ('u_001', '김지훈', '프리랜서 마케터/데이터분석가'),
    ('u_002', '김민철', '생계형 배달 투잡러')
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO user_risk_profiles (user_id) VALUES
    ('u_001'), ('u_002')
ON CONFLICT (user_id) DO NOTHING;
