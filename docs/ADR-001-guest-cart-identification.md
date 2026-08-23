# ADR-001: Guest Cart Identification Strategy

## Context

Guest users (unauthenticated) must be able to add items to a shopping cart. When they log in, their guest cart must be merged into their authenticated account. The system needs a reliable way to identify and persist guest carts across sessions.

## Decision

We use the **`X-Session-Id` header** to identify guest carts.

When a guest first interacts with the cart API, the frontend generates a UUID and sends it in the `X-Session-Id` header on every request. This ID is used as `user_id` in the `cart_items` table. Upon login, the frontend sends both `guest_user_id` (X-Session-Id) and `auth_user_id` (JWT subject) to `POST /cart/merge`, which merges MAX(guest, auth) and returns the combined cart.

## Options Considered

### Option 1: `X-Session-Id` header (chosen)
- **Про +:** Мобильные клиенты (iOS/Android) не имеют cookie-хранилища в нативных WebView, но работают с заголовками HTTP. Фронтенд легко генерирует и persistует UUID в localStorage / secure storage.
- **Про:** Нет риска CSRF (заголовки не отправляются браузером автоматически).
- **Минус:** Риск подделки идентификатора — любой клиент может сгенерировать произвольный UUID.
- **Митигация подделки:** Подделка идентификатора означает только возможность "подменить" чужую корзину, что не критично (нет PII, нет платежа). Для merge-операции требуется оба ID, поэтому украсть чужую корзину нельзя без знания X-Session-Id.

### Option 2: HTTP-only cookie
- **Про:** Невозможно подделать через JS, защита от XSS-кражи.
- **Минус:** Не работает для нативных мобильных приложений (нет механизма set-cookie в API-ответах, которые можно сохранить в cookie-хранилище). Требует дополнительной логики для передачи в мобильных клиентах.

### Option 3: Временный JWT
- **Про:** Стандартный подход в JWT-архитектурах, не требует отдельного заголовка.
- **Минус:** Избыточная сложность для задачи идентификации сессии. Требует подписи и валидации на каждом запросе, что добавляет latency. Мобильные клиенты должны управлять жизненным циклом токена (refresh, expiry).

## Rationale

`X-Session-Id` выбран по двум критериям:

1. **Совместимость с мобильными клиентами** — единственная стратегия, которая работает из коробки для веб (cookie), SPA (header) и нативных мобильных приложений (header) без дополнительной инфраструктуры.
2. **Допустимый риск подделки** — идентификатор корзины не содержит чувствительных данных. Подделка X-Session-Id позволяет только получить доступ к "пустой" или случайно угаданной корзине, что не наносит ущерба. Merge-логика (требует оба ID) предотвращает несанкционированный доступ к чужим корзинам.

## Consequences

- Все эндпоинты корзины принимают `user_id` через query parameter, который заполняется либо из JWT (аутентифицированные), либо из `X-Session-Id` (гости).
- При логине фронтенд вызывает `POST /cart/merge?guest_user_id=...&auth_user_id=...` для объединения.
- ID не валидируется сервером — это просто ключ для ассоциации записей.
