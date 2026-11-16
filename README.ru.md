<!-- translated-from: 8a7c81a -->
# readme2demo — проверенные (verified) туториалы и демо-видео из вашего README

[English](README.md) · **Русский**

[![CI tests status](https://github.com/alphacrack/readme2demo/actions/workflows/ci.yml/badge.svg)](https://github.com/alphacrack/readme2demo/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/alphacrack/readme2demo/blob/main/LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://github.com/alphacrack/readme2demo/blob/main/pyproject.toml)

<!-- HERO:START — scripts/self-demo.sh rewrites this block with a fresh self-run.
     Run it locally (needs Docker + VHS + a model credential):
       ./scripts/self-demo.sh
     It runs readme2demo on this very repo, following docs/step-by-step.md,
     and drops the verified demo.gif in right here. -->
[![readme2demo running on its own repo — verified demo](https://raw.githubusercontent.com/alphacrack/readme2demo/main/docs/demo.gif)](https://github.com/alphacrack/readme2demo/tree/main/examples/readme2demo)

<sub>▶ readme2demo генерирует собственный туториал: AI-агент выполняет README этого репозитория в песочнице (sandbox), свежий контейнер повторяет каждый шаг, затем рендерится демо. Полный результат self-run — в <a href="examples/readme2demo/">examples/readme2demo</a> · запуск на другом проекте — в <a href="examples/toolhive/">examples/toolhive</a>.</sub>
<!-- HERO:END -->

**Генератор туториалов и демо-видео с AI-проверкой (verified).** Укажите репозиторий. AI-агент читает README и реально запускает его внутри усиленной Docker-песочницы (sandbox). Только после успешного чистого прогона (clean-room replay) рендерится демо-видео (VHS) и публикуются туториал, пошаговое руководство и документ по устранению неполадок.

Ценность не в том, что «AI написал туториал», — а в том, что туториал **действительно выполнялся дважды**, прежде чем вы его увидели.

**Посмотрите в действии:** [проверенные (verified) примеры запусков](https://github.com/alphacrack/readme2demo/tree/main/examples) — реальные туториалы, пошаговые гайды и демо-видео; каждый независимо воспроизведён в чистом контейнере перед публикацией.

## Как это работает

```
repo URL → ingest/plan → agent run (in Docker) → normalize transcript
        → distill minimal path → VERIFY replay in fresh container
        → generate tutorial.md + troubleshooting.md → render VHS video
```

Подробнее об архитектуре — [architecture/README.md](https://github.com/alphacrack/readme2demo/blob/main/architecture/README.md).

## Требования

- Python ≥ 3.10, Docker
- Аутентификация, один из вариантов:
  - **Подписка Claude (без API-ключа):** локальная установка Claude Code. Проходы planner/distiller/tutorial идут по подписке через `--llm-backend claude-cli` (`claude -p`), агент в песочнице (sandbox) аутентифицируется через `CLAUDE_CODE_OAUTH_TOKEN` (создать: `claude setup-token`). Полностью поддерживается для **самостоятельного хостинга одним оператором** на своих репозиториях — планы Pro/Max включают месячный кредит Agent SDK для `claude -p`.
  - `ANTHROPIC_API_KEY` — поминутная оплата API; лучше для масштаба и параллелизма, **обязательна при хостинге readme2demo как сервиса для других** (по условиям Anthropic, подписка может не покрывать мультитenant-продукт — см. [ROADMAP.md](https://github.com/alphacrack/readme2demo/blob/main/ROADMAP.md)). Добавьте `--anthropic [model]`, чтобы агент в песочнице работал на движке OpenHands с моделью Claude вместо claude-code.
  - **Google Gemini (`--gemini [model]`):** один `GEMINI_API_KEY` переводит всю сессию с Claude — проходы planner/distiller/tutorial на Gemini, агент в песочнице на OpenHands (тоже Gemini). Имя модели не зашито (Google снимает старые с жёстким 404): указывайте на каждый запуск (`--gemini gemini-3.5-flash`) или один раз `export GEMINI_MODEL`. Дополнение: `pip install 'readme2demo[gemini]'`.
  - **OpenAI (`--openai [model]`):** та же схема, что у Gemini — один `OPENAI_API_KEY` для проходов и агента OpenHands, имя модели не зашито (`--openai gpt-5.1` или `export OPENAI_MODEL`). Дополнение: `pip install 'readme2demo[openai]'`.
- Опционально: `LLM_API_KEY` + `LLM_MODEL` для `--engine openhands` (эксперимент) с любым провайдером litellm — пресеты выше заполняют их автоматически

```bash
# run on your Claude subscription (no API key) — supported for self-hosted runs
claude setup-token        # interactive: approve in browser, then COPY the
                          # sk-ant-oat01-... token it prints (do NOT use $(...))
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
readme2demo run <repo-url> --llm-backend claude-cli

# run on metered API billing (scale, concurrency, or hosting for others)
export ANTHROPIC_API_KEY=sk-ant-...
readme2demo run <repo-url>              # --llm-backend auto picks api

# run the whole session on Google Gemini (OpenHands agent + Gemini passes)
pip install 'readme2demo[gemini]'
docker build -t readme2demo/openhands:latest images/openhands   # one-time: OpenHands sandbox image
export GEMINI_API_KEY=...
readme2demo run <repo-url> --gemini gemini-3.5-flash   # model named per run
export GEMINI_MODEL=gemini-3.5-flash                   # ...or set once, then:
readme2demo run <repo-url> --gemini                    # bare flag reads GEMINI_MODEL

# run the whole session on OpenAI (OpenHands agent + OpenAI passes)
pip install 'readme2demo[openai]'
export OPENAI_API_KEY=sk-...
readme2demo run <repo-url> --openai gpt-5.1            # or export OPENAI_MODEL once

# run the OpenHands agent with a Claude model on API billing
export ANTHROPIC_API_KEY=sk-ant-...
readme2demo run <repo-url> --anthropic                 # uses the config model by default
```

## Установка

```bash
pip install -e ".[dev]"
docker build -t readme2demo/base:latest images/base/
docker build -t readme2demo/openhands:latest images/openhands/   # only for --engine openhands / --gemini / --openai / --anthropic
```

## Использование

```bash
readme2demo run https://github.com/example/tool
readme2demo run -gr https://github.com/example/tool             # same, via the flag
readme2demo run -s my_guide.md                                  # guide-only: no repo, your guide is self-contained
readme2demo run -gr https://github.com/example/tool -s my_guide.md   # both: your guide drives everything
readme2demo run https://github.com/example/tool --gemini gemini-3.5-flash  # run on Google Gemini (needs GEMINI_API_KEY; uses the OpenHands agent; bare --gemini reads GEMINI_MODEL)
readme2demo run https://github.com/example/tool --openai gpt-5.1           # run on OpenAI (needs OPENAI_API_KEY; uses the OpenHands agent; bare --openai reads OPENAI_MODEL)
readme2demo run https://github.com/example/tool --anthropic                # OpenHands agent with a Claude model on ANTHROPIC_API_KEY
readme2demo run https://github.com/example/tool --allow-docker-socket  # for tools that manage containers (SECURITY TRADEOFF: pierces sandbox isolation — trusted repos only)
readme2demo run https://github.com/example/tool --skip-video --budget-usd 3
readme2demo resume runs/tool-20260702-... --from-stage render
readme2demo report runs/tool-20260702-...
```

Репозиторий **необязателен**: передайте URL позиционно или через `-gr/--github-repo`, укажите гайд через `-s/--step-by-step` или оба варианта. Нужен хотя бы один. Только с гайдом репозиторий не клонируется — гайд должен быть самодостаточным (установка опубликованного пакета или явный clone в шагах); повтор в свежем контейнере по-прежнему проверяет каждую команду.

Результаты попадают в `runs/<run-id>/`: `tutorial.md`, `step_by_step.md`, `troubleshooting.md`, `commands.sh`, `demo.tape`, `demo.mp4`, `demo.gif`, `howto.jsonld`, плюс `manifest.json` со статусами этапов и общей стоимостью.

## GitHub Action — проверка README в CI

Красный крестик, когда README перестаёт работать. Composite action в корне репозитория ставит readme2demo из закреплённого checkout, собирает образ песочницы (sandbox), прогоняет полный конвейер (pipeline) по URL вашего README и **падает, если повтор в свежем контейнере (clean-room) не прошёл**:

```yaml
name: readme-check
on:
  push:
    branches: [main]        # url mode tests the default branch HEAD — see the caveat below
    paths: ["README.md"]
  schedule:
    - cron: "0 6 * * 1"     # weekly: catch the world changing under an unchanged README

permissions:
  contents: read

jobs:
  verify-readme:
    runs-on: ubuntu-latest
    steps:
      - uses: alphacrack/readme2demo@main   # pin a tag or SHA once released
        with:
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          skip-video: "true"
```

> **⚠ Только режим URL — PR heads пока не проверяются.** Action клонирует **удалённый HEAD ветки по умолчанию** для `repo-url` (по умолчанию — репозиторий, где запущен workflow); ingestion принимает только https URL, `--depth 1`, без pin ref. На `pull_request` проверился бы README *базовой* ветки — не PR; не подключайте к PR, ожидая вердикт до merge. Пока не выйдет [#74](https://github.com/alphacrack/readme2demo/issues/74) (local-path ingestion), честные триггеры — `on: push` в default branch и cron; `repo-path` для проверки head PR придёт вместе с этим.

**Стоимость:** каждый запуск тратит реальные деньги агента с вашего `ANTHROPIC_API_KEY` — обычно несколько долларов, жёсткий потолок `budget-usd` (по умолчанию `"5"`; при превышении run прерывается). Фильтр `paths:` плюс cron держат расход пропорциональным изменениям README, а `skip-video: "true"` сокращает время (рендер API не стоит в любом случае).

Проверка падает двумя различимыми способами, они названы в логе шага: **README сломан** (pipeline завершился, clean-room replay не прошёл — через `readme2demo report --json`, потому что `readme2demo run` намеренно выходит 0 при завершённом, но непроверенном run) и **инфра action сломалась** (ненулевой exit pipeline: preflight, budget, Docker). Выходы: `verified` (`"true"`/`"false"`) и `run-dir`; `tutorial.md`, `step_by_step.md`, `verify.log` (и `demo.gif` при включённом видео) загружаются артефактом `readme2demo-run`.

## step_by_step.md — источник видео

Демо-видео всегда строится **из** `step_by_step.md`: шаги разбираются, каждая безопасная для демо команда с привязкой к контексту (grounding) становится набираемой командой в видео, заголовок шага — комментарий на экране. Три способа появления файла, по приоритету:

1. **Вы передаёте свой**: `readme2demo run <url> -s my_guide.md` — подставляется в clone как авторитетный гайд; planner и agent следуют ему, видео воспроизводит его. `<url>` здесь необязателен: `readme2demo run -s my_guide.md` — guide-only в пустой песочнице (sandbox).
2. **В репозитории уже есть** (`step_by_step.md` / `step-by-step.md` в корне или `docs/`, любой регистр): то же автоматически.
3. **Ничего нет**: конвейер (pipeline) *генерирует* подробный `step_by_step.md` — каждая команда из проверенного (verified) `commands.sh` как нумерованный шаг с реальным выводом — и строит видео из него. Готово к contribution обратно в репо.

Шаги настройки (clone, install, build) описаны в гайде, но не попадают в видео — оно играет против уже собранного, проверенного worktree, показывая результат.

Каждый туториал несёт значок проверки (badge): `✅ Verified on <date> · image <digest> · commit <sha>` — или громкое `⚠ UNVERIFIED`, если replay не прошёл. Непроверенный вывод никогда не публикуется молча.

## Конфигурация

Флаги CLI > `readme2demo.toml` > значения по умолчанию:

```toml
engine = "claude-code"      # or "openhands"
model = "claude-sonnet-5"   # planner/distiller/tutorial passes
llm_backend = "auto"        # auto | api | claude-cli | gemini | openai
max_turns = 60
budget_usd = 5.0
base_image = "readme2demo/base:latest"
formats = ["demo", "gif"]
skip_video = false
```

## Разработка

```bash
python -m pytest tests/ -q            # comprehensive unit test suite, no docker/network needed
ruff check src/ tests/               # correctness lint (matches CI)
python -m pytest -m integration      # requires docker + API keys (none yet)
```

## Модель безопасности

README — недоверенный код. Агент работает *внутри* усиленного контейнера (cap-drop ALL, no-new-privileges, лимиты memory/cpu/pids, non-root) — контейнер и есть граница полномочий. Известный компромисс MVP: API-ключ попадает в песочницу (sandbox); используйте отдельный ключ с низким лимитом. Планируется egress-proxy на хосте с инъекцией ключа (Milestone 4).

Полная модель угроз и приватное сообщение об уязвимостях: [SECURITY.md](https://github.com/alphacrack/readme2demo/blob/main/SECURITY.md).

## Проект и сообщество

- [Examples](https://github.com/alphacrack/readme2demo/tree/main/examples) — проверенный (verified) вывод закоммичен как доказательство
- [Roadmap](https://github.com/alphacrack/readme2demo/blob/main/ROADMAP.md) — куда движемся (включая экспериментальный hosted/SaaS)
- [Contributing](https://github.com/alphacrack/readme2demo/blob/main/CONTRIBUTING.md) — единственное непреложное правило и как настроиться
- [Security policy](https://github.com/alphacrack/readme2demo/blob/main/SECURITY.md) · [Code of Conduct](https://github.com/alphacrack/readme2demo/blob/main/CODE_OF_CONDUCT.md)
- [Architecture](https://github.com/alphacrack/readme2demo/blob/main/architecture/README.md) — границы этапов и диаграммы

Лицензия MIT. CLI и конвейер проверки (pipeline) остаются и будут оставаться бесплатными и open source.

## Участники

Огромное спасибо всем, кто внёс вклад в readme2demo!

[![Contributor count](https://img.shields.io/github/contributors/alphacrack/readme2demo?style=flat-square)](https://github.com/alphacrack/readme2demo/graphs/contributors)

<a href="https://github.com/alphacrack/readme2demo/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=alphacrack/readme2demo" alt="Contributor avatars" />
</a>

