CREATE DATABASE IF NOT EXISTS mes_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE mes_db;

-- 설비별 기본 파라미터 (product_code 독립 — 어떤 제품을 분석해도 동일한 설비 목록 조회)
CREATE TABLE IF NOT EXISTS schedules (
    id                  INT AUTO_INCREMENT PRIMARY KEY,
    machine_id          VARCHAR(20) NOT NULL UNIQUE,
    daily_working_hours INT NOT NULL,
    current_product_code VARCHAR(20),
    INDEX idx_machine (machine_id)
);

CREATE TABLE IF NOT EXISTS scheduled_periods (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    schedule_id INT NOT NULL,
    period_from DATE NOT NULL,
    period_to   DATE NOT NULL,
    from_mold   VARCHAR(20),
    to_mold     VARCHAR(20),
    FOREIGN KEY (schedule_id) REFERENCES schedules(id)
);

-- 설비×제품별 UPH (여기에 행이 없으면 해당 설비로 그 제품 생산 불가)
CREATE TABLE IF NOT EXISTS machine_product_uph (
    machine_id   VARCHAR(20) NOT NULL,
    product_code VARCHAR(20) NOT NULL,
    uph          INT NOT NULL,
    PRIMARY KEY (machine_id, product_code)
);

CREATE TABLE IF NOT EXISTS molds (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    product_code    VARCHAR(20) NOT NULL UNIQUE,
    mold_id         VARCHAR(20) NOT NULL,
    usage_count     INT NOT NULL,
    max_usage_count INT NOT NULL,
    cavity_count    INT NOT NULL,
    yield_rate      DECIMAL(4,3) NOT NULL DEFAULT 1.000,
    INDEX idx_product (product_code)
);

CREATE TABLE IF NOT EXISTS machines (
    machine_id   VARCHAR(20) PRIMARY KEY,
    current_mold VARCHAR(20) NOT NULL
);

-- ═══════════════════════════════════════════════════════════════════════
-- machines: 사출성형기 3대 (현재 장착 금형)
-- ═══════════════════════════════════════════════════════════════════════
INSERT INTO machines (machine_id, current_mold) VALUES
  ('MCH-01', 'MOLD-02'),  -- 현재 MOLD-02 장착
  ('MCH-02', 'MOLD-01'),  -- 현재 MOLD-01 장착
  ('MCH-03', 'MOLD-03');  -- PROD-C01 타겟과 동일 → 교체 불필요

CREATE TABLE IF NOT EXISTS mold_change_times (
    from_mold VARCHAR(20) NOT NULL,
    to_mold   VARCHAR(20) NOT NULL,
    time_min  INT NOT NULL,
    PRIMARY KEY (from_mold, to_mold)
);

CREATE TABLE IF NOT EXISTS trajectories (
    id                 INT AUTO_INCREMENT PRIMARY KEY,
    trajectory_id      VARCHAR(64) NOT NULL UNIQUE,
    product_code       VARCHAR(20) NOT NULL,
    required_quantity  INT NOT NULL,
    deadline           DATE NOT NULL,
    judgment           TINYINT(1) NOT NULL,
    recommended_machine VARCHAR(20),
    selection_reason   TEXT,
    full_state         JSON,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ═══════════════════════════════════════════════════════════════════════
-- schedules: 사출성형기 3대 기본 파라미터 (id 1~3 고정)
-- ═══════════════════════════════════════════════════════════════════════
--   MCH-01  8h/day   범용 중속
--   MCH-02 16h/day   범용 중속
--   MCH-03 24h/day   고속 (3교대)

INSERT INTO schedules (machine_id, daily_working_hours, current_product_code) VALUES
  ('MCH-01',  8, 'PROD-B01'),
  ('MCH-02', 16, 'PROD-A01'),
  ('MCH-03', 24, 'PROD-C01');

-- scheduled_periods: 설비별 현재 가동 스케줄
INSERT INTO scheduled_periods (schedule_id, period_from, period_to) VALUES
  (1, '2026-06-01', '2026-06-04'),  -- MCH-01
  (2, '2026-06-01', '2026-06-02'),  -- MCH-02
  (3, '2026-06-01', '2026-06-08');  -- MCH-03 장기

-- ═══════════════════════════════════════════════════════════════════════
-- machine_product_uph: 설비×제품별 UPH (3×3 = 9행)
-- ═══════════════════════════════════════════════════════════════════════
INSERT INTO machine_product_uph (machine_id, product_code, uph) VALUES
  ('MCH-01', 'PROD-A01', 267), ('MCH-01', 'PROD-B01', 320), ('MCH-01', 'PROD-C01', 150),
  ('MCH-02', 'PROD-A01', 300), ('MCH-02', 'PROD-B01', 380), ('MCH-02', 'PROD-C01', 180),
  ('MCH-03', 'PROD-A01', 300), ('MCH-03', 'PROD-B01', 420), ('MCH-03', 'PROD-C01', 200);

-- ── molds: 금형 3종 ──────────────────────────────────────────────────
INSERT INTO molds (product_code, mold_id, usage_count, max_usage_count, cavity_count, yield_rate) VALUES
  ('PROD-A01', 'MOLD-01', 12400, 15000, 4, 0.950),  -- 잔여 수명 빠듯
  ('PROD-B01', 'MOLD-02',  2000, 20000, 8, 0.970),  -- 신금형 고UPH
  ('PROD-C01', 'MOLD-03',  8500, 12000, 2, 0.910);  -- 저cavity, 수율 낮음

-- ── mold_change_times: 금형 조합별 교체 소요시간 (3×3 = 9행) ─────────
INSERT INTO mold_change_times (from_mold, to_mold, time_min) VALUES
  ('MOLD-01', 'MOLD-01',  0),
  ('MOLD-01', 'MOLD-02', 25),
  ('MOLD-01', 'MOLD-03', 50),

  ('MOLD-02', 'MOLD-01', 30),
  ('MOLD-02', 'MOLD-02',  0),
  ('MOLD-02', 'MOLD-03', 55),

  ('MOLD-03', 'MOLD-01', 45),
  ('MOLD-03', 'MOLD-02', 35),
  ('MOLD-03', 'MOLD-03',  0);
