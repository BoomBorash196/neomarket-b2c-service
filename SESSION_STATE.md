# NeoMarket M1 - Состояние сессии

## Дата: 2026-05-22

---

## ✅ Завершено

### B2B Service (порт 8001)
- ✅ Запущен через docker-compose
- ✅ PostgreSQL БД (neomarket_b2b)
- ✅ Миграции применены
- ✅ Endpoints: /health, /api/v1/products/batch, /api/v1/skus/batch, /api/v1/reserve

### B2C Service (порт 8000)
- ✅ Запущен через docker-compose
- ✅ PostgreSQL БД (neomarket_b2c) на порту 5433
- ✅ Миграции применены (alembic upgrade head)
- ✅ Интеграция с B2B настроена (extra_hosts: host.docker.internal:host-gateway)

### Интеграция
- ✅ B2B_API_URL: http://host.docker.internal:8001/api/v1
- ✅ Конфигурация: src/config.py
- ✅ Docker: extra_hosts добавлен в docker-compose.yml

---

## 📊 Готовность M1

| Компонент | Статус |
|-----------|--------|
| B2B сервис | ✅ Работает (8001) |
| B2C сервис | ✅ Работает (8000) |
| B2B БД | ✅ PostgreSQL |
| B2C БД | ✅ PostgreSQL (порт 5433) |
| Миграции B2B | ✅ Применены |
| Миграции B2C | ✅ Применены |
| Интеграция | ✅ Настроена |

**Общая готовность: 100%**

---

## 🚀 Команды для запуска

```bash
# B2B Service
cd /home/marceline/neomarket-b2b-service
docker-compose up -d

# B2C Service
cd /home/marceline/neomarket-b2c-service
docker-compose up -d

# Проверка здоровья
curl http://localhost:8000/health
curl http://localhost:8001/health

# Проверка каталога
curl http://localhost:8000/api/v1/catalog/products

# Проверка категорий
curl http://localhost:8000/api/v1/catalog/categories
```

---

## 📝 Заметки

- B2C PostgreSQL порт: 5433 (не 5432, так как 5432 занят B2B)
- B2B API URL: http://host.docker.internal:8001/api/v1
- extra_hosts добавлен для работы host.docker.internal в Linux Docker

---

*Создано: 2026-05-22*
