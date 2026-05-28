CREATE DATABASE IF NOT EXISTS capa_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE capa_db;

-- 사출기 기본 정보 (고정)
CREATE TABLE IF NOT EXISTS machines (
    machine_id   VARCHAR(20) PRIMARY KEY,
    tons         INT NOT NULL,
    cycle_sec    INT NOT NULL,
    daily_cap    INT NOT NULL
);

-- 시나리오별 가동 상태 (Scheduler가 1시간마다 업데이트)
CREATE TABLE IF NOT EXISTS schedules (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    scenario_type  CHAR(1) NOT NULL,
    machine_id     VARCHAR(20) NOT NULL,
    current_load   FLOAT NOT NULL,
    available_days INT NOT NULL,
    updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- 경합 수주 (시나리오 C 전용)
CREATE TABLE IF NOT EXISTS competing_orders (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    scenario_type  CHAR(1) NOT NULL DEFAULT 'C',
    order_id       VARCHAR(30) NOT NULL,
    quantity       INT NOT NULL,
    deadline       DATE NOT NULL,
    priority       INT NOT NULL
);

-- Trajectory 저장
CREATE TABLE IF NOT EXISTS trajectories (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    order_id       VARCHAR(30) NOT NULL,
    scenario_type  VARCHAR(10) NOT NULL,
    goal           JSON,
    plan           JSON,
    action         JSON,
    state          JSON,
    result         JSON,
    recovery       JSON,
    created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    completed_at   DATETIME
);

-- 금형 정보
CREATE TABLE IF NOT EXISTS molds (
    mold_id        VARCHAR(20) PRIMARY KEY,
    product_code   VARCHAR(20) NOT NULL,
    scenario_type  CHAR(1) NOT NULL,
    machine_id     VARCHAR(20) NOT NULL,
    usage_count    INT NOT NULL DEFAULT 0,
    max_usage      INT NOT NULL DEFAULT 500000,
    setup_hours    FLOAT NOT NULL DEFAULT 2.0,
    status         VARCHAR(20) NOT NULL DEFAULT 'ok'
);

-- ── 초기 데이터 ──

INSERT IGNORE INTO machines VALUES
    ('INJ-01', 320, 28, 8200),
    ('INJ-02', 250, 35, 6400),
    ('INJ-03', 180, 42, 5100),
    ('INJ-05', 440, 25, 7800);

-- 시나리오 A: CAPA 여유
INSERT IGNORE INTO schedules (scenario_type, machine_id, current_load, available_days) VALUES
    ('A', 'INJ-01', 0.45, 10),
    ('A', 'INJ-02', 0.30, 10);

-- 시나리오 B: CAPA 부족
INSERT IGNORE INTO schedules (scenario_type, machine_id, current_load, available_days) VALUES
    ('B', 'INJ-01', 0.90, 10),
    ('B', 'INJ-02', 0.85, 10),
    ('B', 'INJ-03', 0.70, 10),
    ('B', 'INJ-05', 0.20, 10);

-- 시나리오 C: 경합
INSERT IGNORE INTO schedules (scenario_type, machine_id, current_load, available_days) VALUES
    ('C', 'INJ-01', 0.50, 10),
    ('C', 'INJ-02', 0.50, 10);

INSERT IGNORE INTO competing_orders (scenario_type, order_id, quantity, deadline, priority) VALUES
    ('C', 'ORD-C-0921', 30000, DATE_ADD(CURDATE(), INTERVAL 14 DAY), 1),
    ('C', 'ORD-C-0930', 40000, DATE_ADD(CURDATE(), INTERVAL 17 DAY), 2);

-- 시나리오 A: 금형 정상, 현재 사출기에 장착 중
INSERT IGNORE INTO molds VALUES ('MDL-320-A', 'P-320-BLK', 'A', 'INJ-01', 200000, 500000, 2.0, 'ok');
-- 시나리오 B: 금형 수명 임박 + 다른 기계에 장착 → 셋업 지연
INSERT IGNORE INTO molds VALUES ('MDL-440-B', 'P-440-WHT', 'B', 'INJ-03', 480000, 500000, 4.0, 'maintenance_needed');
-- 시나리오 C: 금형 다른 기계에 장착 중 → 이동 셋업 필요
INSERT IGNORE INTO molds VALUES ('MDL-320-C', 'P-320-BLK', 'C', 'INJ-02', 350000, 500000, 2.0, 'ok');
