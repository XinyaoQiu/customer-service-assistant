-- Seed data covering each review outcome.

DELETE FROM escalations;
DELETE FROM article_stats;
DELETE FROM article_reviews;
DELETE FROM articles;
DELETE FROM publishers;

INSERT INTO publishers VALUES
  ('pub_001', 'Bay Area Local News', 'standard', 'en', '2025-03-14 10:00:00'),
  ('pub_002', 'Cocina Del Valle',    'premium',  'es', '2024-11-02 09:30:00'),
  ('pub_003', 'Quiet Account',       'standard', 'en', '2026-08-01 12:00:00');

INSERT INTO articles VALUES
  ('art_101','pub_001','Downtown parking rates rise in September','2026-08-20 09:12:00',NULL,'local'),
  ('art_102','pub_001','Ten best taco trucks, ranked','2026-08-19 14:40:00',NULL,'food'),
  ('art_103','pub_001','City council approves budget','2026-08-18 08:05:00','2026-08-18 11:20:00','local'),
  ('art_104','pub_001','Breaking: freeway closure','2026-08-17 16:30:00',NULL,'local'),
  ('art_105','pub_001','Weekend farmers market guide','2026-08-12 07:00:00','2026-08-12 09:15:00','local'),
  ('art_106','pub_001','Five more parking updates','2026-08-11 22:10:00',NULL,'local'),
  ('art_201','pub_002','Receta de mole poblano','2026-08-21 11:00:00',NULL,'food'),
  ('art_202','pub_002','Los mejores restaurantes 2026','2026-08-15 10:00:00','2026-08-15 12:00:00','food');

INSERT INTO article_reviews VALUES
  -- Pending inside SLA: the most common real case.
  ('art_101','pending_review',NULL,NULL,FALSE),
  ('art_102','rejected','duplicate_content','2026-08-19 18:00:00',TRUE),
  ('art_103','published',NULL,'2026-08-18 11:00:00',FALSE),
  ('art_104','rejected','copyright_unlicensed','2026-08-17 19:45:00',TRUE),
  ('art_105','published',NULL,'2026-08-12 09:00:00',FALSE),
  ('art_106','rejected','rate_limit_exceeded','2026-08-11 22:11:00',FALSE),
  ('art_201','rejected','spam_behavior','2026-08-21 15:30:00',TRUE),
  ('art_202','published',NULL,'2026-08-15 11:30:00',FALSE);

INSERT INTO article_stats VALUES
  ('art_103', 48210, 1520, '2026-08-22 00:00:00'),
  ('art_105', 51044, 1789, '2026-08-22 00:00:00'),
  ('art_202',  3120,   61, '2026-08-22 00:00:00');
