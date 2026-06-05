USE mes_db;

-- ── 1. machines 테이블 (3대 기계, 현재 장착 금형) ──────────────────────
CREATE TABLE IF NOT EXISTS machines (
    machine_id   VARCHAR(20) PRIMARY KEY,
    current_mold VARCHAR(20) NOT NULL
);

INSERT INTO machines (machine_id, current_mold) VALUES
  ('MCH-01', 'MOLD-02'),
  ('MCH-02', 'MOLD-01'),
  ('MCH-03', 'MOLD-03')   -- PROD-C01 타겟과 동일 → 교체 불필요
ON DUPLICATE KEY UPDATE current_mold = VALUES(current_mold);

-- ── 2. mold_change_times 재설계 (금형 조합 기준, 기계 무관, 3×3 = 9행) ─
DROP TABLE IF EXISTS mold_change_times;

CREATE TABLE mold_change_times (
    from_mold VARCHAR(20) NOT NULL,
    to_mold   VARCHAR(20) NOT NULL,
    time_min  INT NOT NULL,
    PRIMARY KEY (from_mold, to_mold)
);

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

SELECT COUNT(*) AS machines_count FROM machines;
SELECT COUNT(*) AS mold_change_count FROM mold_change_times;
