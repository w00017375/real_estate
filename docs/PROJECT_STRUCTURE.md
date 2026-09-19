# Структура проекта

```text
estate parser/
├── main.py                    # все источники → SQLite + общий JSON
├── parse_platform.py          # выбранные источники → отдельные JSON, без SQLite
├── seller_deduplication.py    # локальная постобработка sellers
├── olx_apartments.db          # активная база
├── core/                      # общая нормализация и конвейер
├── sources/                   # адаптеры платформ
├── storage/                   # JSON и SQLite
├── standalone_parsers/        # автономные/raw-парсеры
├── output_json/
│   ├── combined/
│   ├── olx/
│   ├── etagi/
│   ├── uybor/
│   ├── realting/
│   ├── realt24/
│   └── uysot/
├── dashboard/                 # карта и локальный сервер
├── tools/                     # миграции и генераторы
├── backups/                   # резервные копии SQLite
├── docs/                      # документация, UML, матрица
└── tests/                     # автоматические тесты
```

JSON каждой платформы должен сохраняться только в соответствующую подпапку
`output_json`. Совмещённый результат `main.py` хранится в
`output_json/combined/estate_listings_full.json`.
