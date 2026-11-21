# Ztare AI Local KB MCP

Локальная база знаний для ИИ-агентов и людей. Плагин Jupyter Server отдаёт базу ИИ через MCP, а человек читает и правит те же статьи в JupyterLab. Всё работает на своей машине — ПК или телефоне (Termux), без облака.

## Зачем

ИИ-агентам вроде Claude Code нужна долговременная память: документация проектов, решения, заметки, которые живут дольше одной сессии. Если раскладывать их `.md`-файлами по репозиториям, знания расползаются, повторяются и устаревают, а человеку неудобно их просматривать.

Здесь одна общая база на все проекты:
- **ИИ** работает с ней через MCP: ищет, читает, дописывает;
- **человек** видит и правит то же самое в браузере, в JupyterLab, без ИИ;
- **индексатор** (сильная модель через отдельный endpoint) поддерживает порядок: теги, заголовки, шапки, структура, отчёт о дублях и устаревшем;
- **данные** — обычные файлы на диске, с автоматической историей правок для отката.

## Возможности

- **Два MCP-endpoint** на порту Jupyter, авторизация по его токену:
  - `/mcp/kb` — работа с базой; профили `lite` / `std` / `full` под разную силу моделей, от слабой локальной до Claude;
  - `/mcp/kb-admin` — обслуживание для индексатора: все метаданные, запись статьи целиком, защита от одновременной правки.
- **Хранение:** ноутбуки `.ipynb` только из Markdown-ячеек. Дерево «область → тема → подтема → статья → секция», служебные данные — в метаданных ячеек, индексы строятся в памяти.
- **Поиск** по подстрокам, регулярным выражениям и тегам.
- **Ответы для моделей:** плоский текст вместо JSON, ошибки с подсказкой следующего шага и готовыми вариантами.
- **История:** автокоммиты в git (Dulwich).
- **Переносимость:** Windows и Android (Termux); Jupyter ставится рядом с проектом, система не засоряется.
- **Навык `kb-index` для Claude Code:** хранится статьёй в самой базе, агент ставит его сам.

## Установка

**Windows**
1. Установите [Python 3.11](https://www.python.org/downloads/). По умолчанию скрипт вызывает его как `py -3.11`; другой интерпретатор: `set PY=C:\путь\к\python.exe`.
2. Скачайте репозиторий: `git clone …` или архивом, в любую папку.

**Android (Termux)**
1. Установите [Termux](https://termux.dev) и в нём один раз выполните: `pkg install python rust clang make libzmq`.
2. По желанию поставьте termux-api: тогда процессы не засыпают с выключенным экраном.
3. Скачайте репозиторий в Termux. Другой Python: `PY=python3.12 sh start_jupyter.sh`.

При первом запуске нужен интернет: скрипт поставит `jupyterlab` и `dulwich` через pip в `jupyter_bin/<win|android>/`.

## Запуск

```
start_jupyter.bat        # Windows
sh start_jupyter.sh      # Android / Termux
```

Скрипт только готовит окружение и запускает JupyterLab с корнем `jupyter_data/`. Остальное делает плагин: создаёт токен (`jupyter_data/config/token.txt`, постоянный; сменить — удалить файл и перезапустить) и после старта печатает адреса и готовые команды подключения:

```
[kb] token: <токен>  (…/jupyter_data/config/token.txt)
[kb] web:   http://127.0.0.1:8888/lab?token=<токен>
[kb] mcp:   http://127.0.0.1:8888/mcp/kb?profile=lite&token=<токен>
[kb] admin: http://127.0.0.1:8888/mcp/kb-admin?token=<токен>
[kb] Claude Code (run in the project folder; …):
  claude mcp add --transport http -s local kb "http://127.0.0.1:8888/mcp/kb?profile=full&lang=ru&off=structured&token=<токен>"
  …
```

- Адрес `web` открывает JupyterLab: база лежит в папке `kb/`.
- Сервер слушает только `127.0.0.1:8888`; порт и ip задаются в `jupyter_data/config/jupyter_server_config.json`.
- MCP работает, пока запущен Jupyter.

## Подключение MCP

Проще всего скопировать нужную команду `claude mcp add …` из консоли: токен и порт там уже подставлены. Выполнять её нужно в каталоге того проекта, где будете работать с базой.

**Claude Code — работа с базой** (сервер `kb`):
```
claude mcp add --transport http -s local kb "http://127.0.0.1:8888/mcp/kb?profile=full&lang=ru&off=structured&token=<токен>"
```
**Claude Code — индексатор** (сервер `kb-admin`, по желанию):
```
claude mcp add --transport http -s local kb-admin "http://127.0.0.1:8888/mcp/kb-admin?lang=ru&off=structured&token=<токен>"
```
Затем скажите агенту: «установи навык из `development/mcp-kb/skills/kb-index`». Он соберёт навык сам, после чего индексатор вызывается командой `/kb-index` или фразой «проиндексируй базу».

Параметры адреса:

| Параметр | Значения | Зачем |
|---|---|---|
| `profile` | `full` (по умолчанию) / `std` / `lite` | набор команд под силу модели: `full` — Claude и подобные, `lite` — слабые локальные модели |
| `lang` | `en` (по умолчанию) / `ru` | язык описаний инструментов и сообщений |
| `off` | `structured`, `instructions` | отключить возможности MCP, с которыми клиент работает плохо; Claude Code нужен `off=structured` |
| `token` | токен из консоли | авторизация (или заголовок `Authorization: token …`) |

- `-s local` подключает сервер только к текущему проекту, `-s user` — ко всем сразу. Чтобы переподключить с новыми параметрами: `claude mcp remove kb -s local`, затем команда заново.
- **Другие MCP-клиенты** подключаются так же — по HTTP-адресу (Streamable HTTP).
- Агентам, которые только пользуются базой, давайте один `kb`; `kb-admin` — человеку или индексатору.
- База на телефоне, а агент на ПК — см. статью `development/mcp-kb/remote` (SSH-туннель или adb).

Подробнее — в статьях базы `development/mcp-kb/install`, `development/mcp-kb/connect` и `development/mcp-kb/api`.

## Документация

Документация ведётся в самой базе знаний, а не в файлах репозитория.

**Стартовая статья:** `development/mcp-kb/overview` — цели, решения, статус и карта остальных статей.

Где её читать:
- **Через MCP** (сервер `kb`, работает, пока запущен Jupyter): `get("development/mcp-kb/overview")`.
- **В JupyterLab:** `jupyter_data/kb/development/mcp-kb/overview.ipynb`.
- **Без Jupyter:** тот же файл `.ipynb` — обычный ноутбук из Markdown-ячеек.

## Сделано ИИ

Весь проект — код плагина, тесты, скрипты, тексты инструментов и документация — написан ИИ (Claude Code на моделях Claude от Anthropic) по постановке и под руководством человека. Человек не написал вручную ни одной строки: он ставил задачи, принимал решения и проверял результат.

## Лицензия и отказ от ответственности

Проект передан в общественное достояние по [Unlicense](LICENSE): его можно копировать, менять, распространять и использовать в любых целях, в том числе коммерческих, без каких-либо условий.

Программа предоставляется **«как есть», без каких-либо гарантий**. Авторы не несут ответственности за любой ущерб, потерю данных или иные последствия её использования. Перед применением на важных данных делайте резервные копии.

---

*Ztare AI Local KB MCP — a local knowledge base for AI agents and humans: a Jupyter Server plugin that serves Markdown notebooks over MCP while people read and edit them in JupyterLab. AI-generated: all code and documentation in this repository were written by AI (Claude Code, Anthropic) under human direction; no line was written by hand. Released into the public domain under the [Unlicense](LICENSE) — provided "as is", without warranty of any kind.*
