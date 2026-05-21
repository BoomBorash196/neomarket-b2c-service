# Testing Guide

## Запуск тестов

### Требования
```bash
pip install pytest pytest-asyncio httpx aiosqlite
```

### Запустить все тесты
```bash
pytest tests/ -v
```

### Запустить конкретный файл
```bash
pytest tests/test_cart.py -v
```

### Запустить с покрытием
```bash
pytest tests/ -v --cov=src --cov-report=html
```

## Структура тестов

- `test_health.py` — проверки health endpoint
- `test_cart.py` — проверки корзины
- `test_order.py` — проверки заказов (создайте по аналогии)
- `test_wishlist.py` — проверки избранного (создайте по аналогии)

## Примечания

Тесты используют SQLite в памяти для скорости. B2B API должен быть запущен для полных тестов.

Для интеграционных тестов с B2B:
```bash
# Запустить B2B в одном терминале
cd ../neomarket-b2b-service && docker compose up

# Запустить тесты в другом
pytest tests/ -v -k "integration"
```
