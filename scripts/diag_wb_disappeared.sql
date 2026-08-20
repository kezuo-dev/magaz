SELECT l.status, count(*) AS cnt
FROM listings l
JOIN books b ON b.id = l.book_id
WHERE l.marketplace = 'wildberries'
GROUP BY l.status ORDER BY cnt DESC;

-- Кандидаты на «пропавшие»: WB-лоты не WITHDRAWN/TRASHED, у книги нет активного лота WB (статус книги IN_STOCK/ACTIVE?)
SELECT b.status AS book_status, l.status AS listing_status, count(*) AS cnt
FROM listings l JOIN books b ON b.id = l.book_id
WHERE l.marketplace = 'wildberries'
  AND l.status NOT IN ('withdrawn','trashed')
GROUP BY 1,2 ORDER BY cnt DESC LIMIT 30;
