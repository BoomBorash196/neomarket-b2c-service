# NeoMarket B2C Service

Модуль **B2C Buyer** для платформы NeoMarket. Реализует функционал для покупателей: каталог, корзина, заказы, избранное.

## Команда

**Синдикат:** QA Corps  
**Команда:** Синдикат потерянных

## Модуль

- **B2C Buyer** — каталог товаров, корзина, оформление заказов, избранное, рекомендации

## Стек

- **Backend:** Python 3.11, FastAPI
- **Database:** PostgreSQL 16
- **Async:** asyncio, asyncpg
- **ORM:** SQLAlchemy 2.0
- **Migrations:** Alembic
- **Containerization:** Docker, docker-compose

## Структура проекта

```
neomarket-b2c-service/
├── src/
│   ├── __init__.py
│   ├── main.py              # FastAPI application
│   ├── config.py            # Settings
│   ├── database.py          # DB connection
│   ├── models/              # SQLAlchemy models
│   │   └── __init__.py
│   ├── routes/              # API endpoints
│   │   ├── cart.py
│   │   ├── order.py
│   │   ├── wishlist.py
│   │   ├── catalog.py
│   │   ├── home.py
│   │   └── recommendations.py
│   └── schemas/             # Pydantic schemas
│       └── __init__.py
├── migrations/              # Database migrations (Alembic)
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
└── README.md
```

## Запуск

### Требования

- Docker
- Docker Compose

### Инструкция

1. **Клонируйте репозиторий** (если ещё не сделали):
   ```bash
   git clone <your-repo-url>
   cd neomarket-b2c-service
   ```

2. **Запустите сервисы**:
   ```bash
   docker compose up --build
   ```

   Это поднимет:
   - PostgreSQL на порту `5432`
   - API на порту `8000`

3. **Проверьте здоровье**:
   ```bash
   curl http://localhost:8000/health
   ```

4. **Откройте Swagger UI**:
   ```
   http://localhost:8000/docs
   ```

## API Endpoints

### Каталог
- `GET /api/v1/catalog/categories` — получить дерево категорий
- `GET /api/v1/catalog/products` — получить список товаров (с фильтрами)
- `GET /api/v1/catalog/products/{product_id}` — детали товара
- `GET /api/v1/catalog/categories/{category_id}/filters` — доступные фильтры

### Корзина
- `GET /api/v1/cart` — получить корзину
- `POST /api/v1/cart` — добавить товар
- `PUT /api/v1/cart/{cart_item_id}` — изменить количество
- `DELETE /api/v1/cart/{cart_item_id}` — удалить товар
- `DELETE /api/v1/cart` — очистить корзину

### Заказы
- `POST /api/v1/orders` — создать заказ
- `GET /api/v1/orders` — список заказов
- `GET /api/v1/orders/{order_id}` — детали заказа
- `POST /api/v1/orders/{order_id}/cancel` — отменить заказ

### Избранное
- `GET /api/v1/wishlist` — получить избранное
- `POST /api/v1/wishlist` — добавить товар
- `DELETE /api/v1/wishlist/{product_id}` — удалить товар

### Главная
- `GET /api/v1/home` — данные для главной страницы
- `GET /api/v1/home/banners` — активные баннеры
- `GET /api/v1/home/collections` — подборки товаров

### Рекомендации
- `GET /api/v1/recommendations/products/{product_id}` — похожие товары

## Зависимости от B2B

Сервис зависит от **B2B Seller Cabinet** модуля. Переменная `B2B_API_URL` в `docker-compose.yml` указывает на API B2B.

Необходимые эндпоинты B2B:
- `GET /api/v1/products/{product_id}` — детали товара
- `GET /api/v1/products` — список товаров с фильтрами
- `GET /api/v1/skus/{sku_id}` — детали SKU
- `POST /api/v1/skus/batch` — батч-запрос SKU
- `GET /api/v1/categories` — дерево категорий
- `POST /api/v1/reserve` — резервирование остатков

## Разработка

### Локальный запуск (без Docker)

1. Установите зависимости:
   ```bash
   pip install -e .
   ```

2. Запустите PostgreSQL (локально или через Docker):
   ```bash
   docker run -d \
     -e POSTGRES_USER=neomarket \
     -e POSTGRES_PASSWORD=neomarket_pass \
     -e POSTGRES_DB=neomarket_b2c \
     -p 5432:5432 \
     postgres:16-alpine
   ```

3. Запустите сервер:
   ```bash
   uvicorn src.main:app --reload
   ```

## Тестирование

Для M1 тесты не обязательны, но рекомендуются:

```bash
pytest tests/ -v
```

## Roadmap

### M1 (Current)
- [x] Базовая структура проекта
- [x] Модели данных
- [ ] Все эндпоинты из спецификации
- [ ] Валидация запросов/ответов
- [ ] Обработка ошибок
- [ ] Docker-контейнеризация

### M2 (Integration)
- [ ] Интеграция с B2B
- [ ] Событийная архитектура
- [ ] Асинхронные операции

### M3 (Resilience)
- [ ] Ретраи
- [ ] Circuit breakers
- [ ] Load testing

## Документация

- [NeoMarket Student Guide](https://github.com/tochka-public/NeoMarket---Student-Guide)
- [API Specifications (Swagger)](https://urfu2026-neomarket.github.io/neomarket-protocols/)

## Автор

Команда "Синдикат потерянных", QA Corps