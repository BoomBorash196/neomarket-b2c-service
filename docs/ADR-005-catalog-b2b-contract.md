# ADR-005: US-CAT-01 — Каталог с фильтрами и фасетами (B2B-контракт)

## Контекст
Реализация US-CAT-01: каталог с фильтрами, фасетами и пагинацией для neomarket-b2c-service.
Требуется полное соответствие контракту с B2B OpenAPI (параметры, поля ответов, schema).

## Решение

### 1. Параметры запроса к B2B
- `limit` / `offset` вместо `page` / `page_size`
- `sort` (строка: `price_asc`, `price_desc`, `popularity`, `new`) вместо `sort_by` + `sort_order`
- `filters[brand]`, `filters[in_stock]` (deepObject) вместо flat `brand` / `in_stock`
- `filter[category_id]`, `filter[price_min]`, `filter[price_max]`, `q` для B2C-запросов

### 2. Ответ B2B
- `{items: [...], total_count: int}` вместо `{products: [...], total: int, page: int, page_size: int}`
- Продукт: `id`, `title`, `main_image_url`, `min_price`, `active_quantity`, `stock_quantity`, `description`, `images[]`, `characteristics`, `skus[]`
- SKU: `sku_id`, `price`, `stock_quantity`, `active_quantity`, `is_active`, `color`, `size`, `discount`

### 3. B2C-безопасность SKU
Исключены из ответа: `cost_price`, `reserved_quantity`, `internal_note`, `margin`.
Переименованы: `stock_quantity` → `available_quantity`, `active_quantity` → `is_active`.

### 4. DeepObject-кодирование
`_encode_deep_object()` преобразует `{"filters": {"brand": "a"}}` в `{"filters[brand]": "a"}` для httpx.

### 5. Images schema
`images` — `List[object]` с полями `{id, url, ordering}` (как в B2B OpenAPI).

## Альтернативы

| Подход | Плюсы | Минусы |
|--------|-------|--------|
| DeepObject для filters | Соответствует B2B OpenAPI | Требует ручного кодирования |
| Flat filters | Проще | Несовместимо с B2B |
| Кэш фасетов | Быстрее | eventual consistency, доп. инфраструктура |

## Итог
Выбран прямой прокси-подход с deepObject-кодированием и строгим маппингом полей B2B → B2C.
