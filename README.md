# clip-pipeline

Конвейер нарезки длинных видео в вертикальные клипы 9:16 под CPM-кампании (Whop, DareBay, Clipping.net).
Всё до публикации делает конвейер: расшифровка речи, подбор моментов, кадрирование по лицу, субтитры,
хук-заголовок, баннер кампании, плашка «Подпишись», подписи и хештеги под каждую площадку, чек-лист правил,
учёт публикаций и просмотров, недельный отчёт. Публикация — вручную с телефона: площадки не любят автоматику.

Версия 0.1.0 (5 ноября 2026). Проверена на Linux; целевая машина — домашний ПК на Windows 11.

## Установка на Windows

1. Python 3.11 или новее: https://www.python.org/downloads/ (при установке отметить «Add python.exe to PATH»).
2. ffmpeg: в PowerShell `winget install Gyan.FFmpeg`, затем перезапустить терминал и проверить `ffmpeg -version`.
3. Распаковать архив, например в `D:\clip-pipeline`, открыть PowerShell в этой папке и выполнить:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m pipeline init
python -m pipeline doctor
```

`doctor` должен показать OK у ffmpeg, ffprobe, шрифта, faster-whisper, opencv и yt-dlp.
Жирный шрифт берётся из `C:\Windows\Fonts\arialbd.ttf` автоматически.

Первый запуск скачает модель Whisper `small` (около 500 МБ) с Hugging Face и закэширует её.
Для русской речи качественнее `medium` — задайте `"whisper_model": "medium"` в `pipeline.json`
(в 2–3 раза медленнее; на 16 потоках час подкаста расшифровывается примерно за 15–25 минут).

Необязательно: если задать переменную окружения `ANTHROPIC_API_KEY`, фрагменты и хуки оценивает
языковая модель (`llm_model` в `pipeline.json`). Без ключа работают эвристики, а хуки правятся в файле
кандидатов — это и есть основной режим: кандидатов смотрит Умид или Claude в сессии.

## Как работать каждый день

1. Завести кампанию: скопировать `campaigns/example_en.json` или `example_ru.json`, заполнить ставку, порог,
   потолок, дедлайн, площадки, хештеги, пометку рекламы, правила с карточки кампании и путь к баннеру (PNG).
2. Получить клипы одной командой:

```powershell
python -m pipeline clip --campaign <id кампании> --source <ссылка или файл> --n 5
```

   Или по шагам, с ручным отбором:

```powershell
python -m pipeline ingest --campaign <id> --source <ссылка или файл>      # → source_id
python -m pipeline candidates --source-id 1 --n 10                       # → data/candidates/<id>_s1.json
#  в файле поставить chosen: true у нужных, поправить hook и caption_line
python -m pipeline render --selection data\candidates\<id>_s1.json
```

3. Готовые клипы лежат в `out/<кампания>/<дата>/`: рядом с каждым `.mp4` — `.txt` с подписями под TikTok,
   Instagram и YouTube (скопировать с телефона), `.json` со всеми полями и `.checklist.md` с правилами.
4. После публикации записать ссылку, позже — просмотры:

```powershell
python -m pipeline post --clip-id 12 --account ru_tiktok --platform tiktok --url https://...
python -m pipeline stats --post-id 7 --views 4200 --likes 120
python -m pipeline stats --import views.csv        # колонки: post_id,views[,likes,comments,shares,ts]
python -m pipeline payout --campaign <id> --amount 42.5 --method usdt-ton
python -m pipeline report --since 2026-11-10       # две главные цифры, кампании, аккаунты, топ-5
python -m pipeline export                          # CSV-таблицы трекера для Google Drive
```

## Что внутри

- `pipeline/config.py` — настройки (`pipeline.json`, переменные `CLIP_*`), проверка окружения
- `pipeline/campaign.py` — спецификация кампании и примеры
- `pipeline/download.py` — исходник: локальный файл или ссылка через yt-dlp
- `pipeline/transcribe.py` — звук → слова с метками времени (faster-whisper; запасной бэкенд sherpa-onnx)
- `pipeline/audio_features.py` — громкость по окнам как признак «живых» фрагментов
- `pipeline/segment.py` — предложения, окна 15–60 с, рейтинг по хуку, плотности, энергии и завершённости
- `pipeline/score_llm.py` — оценка моделью и файл ручного отбора
- `pipeline/subtitles.py` — ASS-субтитры по 2–3 слова
- `pipeline/render.py` — ffmpeg: кадрирование по лицу или размытый фон, субтитры, хук, баннер, плашка, громкость
- `pipeline/captions.py` — подписи, хештеги, чек-лист
- `pipeline/tracker.py` — SQLite-учёт: исходники, клипы, публикации, замеры, выплаты
- `pipeline/report.py` — недельный отчёт в markdown
- `pipeline/cli.py` — команды

## Ограничения версии 0.1

- Субтитры без подсветки слова; пословные метки точные только у faster-whisper.
- Кадрирование по одному (самому крупному) лицу; для двух собеседников в кадре используйте `"frame_mode": "blur"`.
- Исходники только те, которые кампания прикладывает или разрешает нарезать.
- Публикация ручная; сбор просмотров — вручную или импортом CSV.

## Правило

Конвейер не предназначен для чужого контента без права на нарезку. Фильмы, сериалы и ролики без разрешения
автора снимают с площадок, а аккаунт — блокируют.
