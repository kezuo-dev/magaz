-- Диагностика: почему на WB ничего не уходит в корзину с пятницы 15.08.
-- Запуск на Timeweb (в папке проекта):
--   docker compose --env-file .env.prod exec db psql -U magaz -d magaz -f /app/scripts/diag_wb_trash_prod.sql
-- (или вставьте SQL прямо в psql). Один блок = один ответ на вопрос.

-- 1) Все ли колонки корзины есть на проде? Если trash_failures/trash_blocked
--    отсутствуют — контейнер ещё на старом коде, где этих колонок не было.
\echo '=== 1. Есть ли колонки корзины (lists проматится только при наличии) ==='
SELECT column_name FROM information_schema.columns
WHERE table_name = 'listings' AND column_name IN ('trash_failures', 'trash_blocked');

-- 2) Что реально лежит в очереди корзины прямо сейчас.
\echo '=== 2. Очередь корзины: кандидаты (SOLD/WITHDRAWN, WB не в корзине) ==='
SELECT count(*) AS candidate_books
FROM books b
WHERE b.status IN ('sold','withdrawn')
  AND EXISTS (
    SELECT 1 FROM listings l
    WHERE l.book_id = b.id
      AND l.marketplace = 'wildberries'
      AND l.status <> 'trashed'
  );

-- По типам external_id — vendorCode или nmID?
\echo '=== 3. WB-лоты вне корзины по типу external_id ==='
SELECT
  CASE WHEN l.external_id ~ '^[0-9]+$' THEN 'nmID (цифры)'
       WHEN l.external_id ~ '^[A-Za-zА-Яа-я]' THEN 'vendorCode (буквы)'
       ELSE 'пусто/иное' END AS ext_type,
  count(*) AS cnt
FROM listings l
JOIN books b ON b.id = l.book_id
WHERE l.marketplace = 'wildberries' AND l.status <> 'trashed'
GROUP BY 1 ORDER BY cnt DESC;

-- 4) В завале ли очередь после обнуления — или решения просто нет?
\echo '=== 4. Свежие кандидаты (сняты/проданы за 7 дней) ==='
SELECT b.sku, b.status AS book_status,
       l.status AS listing_status, l.external_id, l.trash_failures, l.trash_blocked,
       b.updated_at
FROM books b
JOIN listings l ON l.book_id = b.id AND l.marketplace = 'wildberries'
WHERE b.status IN ('sold','withdrawn') AND l.status <> 'trashed'
  AND b.updated_at >= now() - interval '7 days'
ORDER BY b.updated_at
LIMIT 20;

-- 5) Что программы записывали в журнал (самый главный блок).
\echo '=== 5. Журнал wb_trash за последние 4 дня ==='
SELECT created_at AT TIME ZONE 'utc' AS created_utc, ok, left(message, 220) AS message
FROM sync_log
WHERE action = 'wb_trash'
  AND created_at >= now() - interval '4 days'
ORDER BY created_at DESC
LIMIT 40;

-- 6) Были ли свежие продажи после пятницы (что их не было — не должна быть)
\echo '=== 6. Последние 10 записей продаж по журналу ==='
SELECT created_at AT TIME ZONE 'utc' AS created_utc, ok, book_id, left(message, 140) AS message
FROM sync_log
WHERE action IN ('order_sold','sell')
  AND created_at >= now() - interval '10 days'
ORDER BY created_at DESC
LIMIT 10;

-- 7) Логи битых карточек (заблокированные) — если колонки на месте
\echo '=== 7. Заблокированные битые карточки ==='
SELECT b.sku, l.external_id, l.trash_failures, left(l.last_error,120) AS last_error
FROM listings l JOIN books b ON b.id = l.book_id
WHERE l.marketplace = 'wildberries' AND l.trash_blocked
LIMIT 15;

-- 8) Лоты WB в статусе ERROR (слетевшие снятия) и активные, но книги SOLD
\echo '=== 8. Лоты WB в статусе ERROR/WITHDRAWN с внешним ID ==='
SELECT l.status, count(*) AS cnt FROM listings l
WHERE l.marketplace = 'wildberries' GROUP BY l.status ORDER BY cnt DESC;