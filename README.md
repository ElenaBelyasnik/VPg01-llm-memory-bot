# VPg01-llm-memory-bot — базовый Telegram AI-бот без памяти

Базовая версия Telegram-бота на Python 3.11, отвечающего через OpenAI-совместимый API.
Это **шаг 1** серии работ над «LLM-memory bot»: память диалога здесь отсутствует — каждое
сообщение обрабатывается независимо (системный промпт + текущее сообщение).

## Возможности

- Приём текстовых сообщений из Telegram (`python-telegram-bot` v21+, long polling).
- Отправка запроса к LLM-модели через OpenAI-совместимый клиент с фиксированным
  системным промптом: `Ты полезный AI-ассистент. Отвечай по делу.`
- Команда `/start` — короткое приветствие.
- Обработка всех ошибок (сеть, API, парсинг): логирование через `logging` и вежливое
  сообщение об ошибке пользователю.
- Готовая контейнеризация (Docker / docker compose) с непривилегированным пользователем.

## Структура проекта

```
VPg01-llm-memory-bot/
├── main.py               # Логика бота: загрузка env, логирование, обработчики, polling
├── requirements.txt      # Зависимости (python-telegram-bot[job-queue], openai, python-dotenv)
├── .env.example          # Шаблон переменных окружения
├── Dockerfile            # Образ python:3.11-slim, непривилегированный пользователь
├── docker-compose.yml    # Сервис base-bot + сеть telegram-bots-network
├── README.md             # Этот файл
└── ProgressOfWork.md     # Ход работ (для передачи задачи в другой чат)
```

## Переменные окружения

Скопируйте `.env.example` в `.env` и заполните значения:

| Переменная | Назначение | Пример |
|---|---|---|
| `OPENAI_API_KEY` | API-ключ модели | `sk-...` |
| `OPENAI_BASE_URL` | Базовый URL OpenAI-совместимого API | `https://api.proxyapi.ru/openai/v1` |
| `OPENAI_MODEL` | Имя модели | `gpt-5.6-luna` |
| `TEMPERATURE` | Температура генерации (клампится в [0, 2]) | `1` |
| `LOG_LEVEL` | Уровень логирования | `INFO` |
| `BOT_TOKEN` | Токен Telegram-бота от @BotFather | `123456:ABC-DEF...` |

> Реальный `.env` не коммитится. В репозитории лежит только `.env.example`.

## Локальный запуск

```bash
# Установка зависимостей
pip install -r requirements.txt

# Подготовка конфигурации
cp .env.example .env   # затем отредактируйте .env

# Запуск
python main.py
```

## Запуск через Docker

```bash
docker compose up --build
```

Сервис `base-bot` собирается из текущей директории, подхватывает `.env` через `env_file`,
перезапускается при падении (`restart: unless-stopped`) и работает в изолированной bridge-сети
`telegram-bots-network`.

## Технические детали

- Синхронный вызов OpenAI SDK выполняется в отдельном потоке
  (`asyncio.to_thread`), чтобы не блокировать event loop PTB.
- Формат логов: `%(asctime)s - %(levelname)s - %(name)s - %(message)s`, вывод в stdout.
- Приложение завершает работу с кодом `1`, если отсутствует обязательная переменная
  или polling упал с необработанной ошибкой.
