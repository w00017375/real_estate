import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const outputDir = "docs";
const outputPath = `${outputDir}/source_db_matrix.xlsx`;

const colors = {
  ink: "#1F2937",
  muted: "#5B6673",
  header: "#1F4E5F",
  header2: "#DCEEF2",
  used: "#E7F5EC",
  computed: "#FFF4D6",
  discarded: "#FCE8E6",
  missing: "#F1F3F5",
  border: "#CCD6DA",
  white: "#FFFFFF",
};

const commonRows = [
  ["Сохраняется", "source", "listings.source_name", "Название источника; используется вместе с id для уникальности."],
  ["Сохраняется", "id", "listings.platform_listing_id", "Идентификатор публикации на площадке."],
  ["Сохраняется", "title", "listings.title", "Заголовок объявления."],
  ["Сохраняется", "price", "listings.price", "Цена объявления."],
  ["Сохраняется", "currency", "listings.currency", "Валюта цены."],
  ["Сохраняется", "url", "listings.url", "Уникальная ссылка объявления."],
  ["Сохраняется", "published_at", "listings.published_at", "Дата публикации, если извлечена."],
  ["Сохраняется", "description_length", "listings.description_length", "Длина описания; полный текст не хранится."],
  ["Вычисляется", "(служебное)", "listings.first_seen_at", "Время первоначального сохранения объявления в базе."],
  ["Вычисляется", "(служебное)", "listings.publication_status", "Статус выводится программой из данных объявления/профиля."],
  ["Сохраняется", "transaction_type", "listings.transaction_type", "Тип операции: rent или sale."],
  ["Нормализуется", "housing.city, housing.district", "listings.city, listings.district", "Город и район также дублируются в listings для фильтрации."],
  ["Нормализуется", "housing.rooms, total_area_m2, floor, floors_total", "listings.rooms, listings.total_area_m2, listings.floor, listings.floors_total", "Ключевые параметры жилья дублируются в listings."],
  ["Сохраняется", "housing.city, district, street", "housing.city, housing.district, housing.street", "Адресные части нормализуются; street может быть объединенным полем."],
  ["Сохраняется", "housing.building_type, is_new_building", "housing.building_type, housing.is_new_building", "Тип фонда, если его удалось определить."],
  ["Сохраняется", "housing.foundation_type", "housing.foundation_type", "Материал/тип строения."],
  ["Сохраняется", "housing.residential_complex_name", "housing.residential_complex_name", "Название ЖК, если доступно."],
  ["Сохраняется", "housing.rooms, total_area_m2, floor, floors_total", "housing.rooms, housing.total_area_m2, housing.floor, housing.floors_total", "Основные характеристики жилья."],
  ["Сохраняется", "housing.furnished", "housing.furnished", "Наличие мебели."],
  ["Вычисляется", "housing.monthly_rent, rent_currency", "housing.monthly_rent, housing.rent_currency", "Для аренды берутся из цены объявления, если источник не дает отдельные значения."],
  ["Вычисляется", "housing.price_per_m2", "housing.price_per_m2", "Для аренды: monthly_rent / total_area_m2."],
  ["Сохраняется", "housing.latitude, longitude", "housing.latitude, housing.longitude", "Координаты, если источник их предоставляет."],
  ["Сохраняется", "amenities[]", "amenities + listing_amenities", "Справочник удобств и связь с объявлением."],
  ["Сохраняется", "nearby[]", "nearby_places + listing_nearby", "Справочник объектов рядом и связь с объявлением."],
  ["Сохраняется", "seller.name, profile_url, phone", "sellers.name, sellers.profile_url, sellers.phone", "Контактные данные продавца; телефон хранится с phone_source."],
  ["Сохраняется", "seller.seller_role / seller_type", "sellers.sellers_type / sellers.seller_type", "Роль продавца: owner, agent, realtor, agency и т. п."],
  ["Сохраняется", "seller.realtor_probability, comment", "sellers.realtor_probability, sellers.assessment_comment", "Оценка продавца и комментарий."],
  ["Сохраняется", "seller.active_*_listings, unique_listing_count, distinct_housing_listings", "sellers.*", "Статистика активности, если рассчитана источником/анализатором."],
  ["Вычисляется", "(служебное)", "scrape_runs", "История запусков парсинга и лимитов."],
];

const sourceSheets = {
  OLX: [
    ["Сохраняется", "source, id, title, price, currency, url, published_at, description_length", "listings", "Канонический блок объявления."],
    ["Сохраняется", "housing.city, housing.district", "listings.city/district + housing.city/district", "Город/район извлекаются из JSON-LD, location и текста; для области возможна пара область + поселение."],
    ["Сохраняется", "housing.rooms, total_area_m2, floor, floors_total, furnished", "listings + housing", "Берутся из параметров OLX и резервно из текста карточки."],
    ["Нормализуется", "housing.address, street, zone, house_number", "housing.street", "Объединяются в одну строку: zone, street, house_number."],
    ["Сохраняется", "housing.building_type, is_new_building, foundation_type, residential_complex_name", "housing", "Сохраняются при наличии соответствующих параметров."],
    ["Сохраняется", "amenities, nearby", "listing_amenities, listing_nearby", "Получаются из параметров «В квартире есть» и «Рядом есть»."],
    ["Сохраняется", "seller.name, phone, phone_source, profile_url", "sellers", "Телефон извлекается со страницы объявления, если OLX его раскрывает."],
    ["Сохраняется", "seller.is_official_seller, official_complex_name", "sellers", "Официальный профиль определяется доменом/URL профиля."],
    ["Сохраняется", "seller.active_publications / closed_publications (агрегаты)", "sellers.*_listings, unique_listing_count, distinct_housing_listings", "Сами карточки публикаций не записываются после удаления seller_publications; сохраняются агрегаты."],
    ["Отбрасывается", "parameters (полный словарь), _location, _description", "—", "Используются для извлечения значений, но не сохраняются как JSON."],
    ["Отбрасывается", "housing.commission, housing.repair", "—", "Соответствующие колонки удалены из актуальной схемы."],
    ["Отбрасывается", "полный текст, фотографии, служебные поля профиля", "—", "В БД хранится только длина описания и основные атрибуты."],
    ["Не хватает", "latitude, longitude", "housing.latitude/longitude", "OLX обычно не передает надежные координаты в текущем адаптере."],
    ["Не хватает", "phone при скрытом номере", "sellers.phone", "Требуется ручное/разрешенное раскрытие номера на странице."],
    ["Не хватает", "полные данные закрытых публикаций", "sellers activity", "OLX может не показывать закрытые карточки в профиле."],
  ],
  "Этажи": [
    ["Сохраняется", "source, _ticket_id/object_id, title, price, currency, transaction_type, url, date_update", "listings", "Каноническая проекция объекта Etagi."],
    ["Сохраняется", "flat.meta.city, city_district/district, street", "listings + housing", "Город, район и улица; адресные части объединяются в housing.street."],
    ["Сохраняется", "flat.rooms, square, floor, floors", "listings + housing", "Площадь берется из структурированного square."],
    ["Сохраняется", "flat.la, flat.lo", "housing.latitude/longitude", "Координаты объекта."],
    ["Вычисляется", "flat.price, rental_period, realtyType", "housing.monthly_rent, rent_currency, price_per_m2", "Цена аренды и цена за м²."],
    ["Сохраняется", "flat.newhouses_name", "housing.residential_complex_name", "Название ЖК, если есть."],
    ["Сохраняется", "flat.metro_stations[].name", "nearby_places + listing_nearby", "Сохраняются названия станций метро."],
    ["Сохраняется", "realtor.fio, realtor.phone, realtor.id", "sellers", "Все карточки Etagi считаются публикациями риелторов."],
    ["Отбрасывается", "flat.notes, plate_notes", "—", "Полный текст не хранится, в listings остается description_length."],
    ["Отбрасывается", "flat.additional_meta", "—", "Техника, парковка, лифт, санузлы и другие флаги не имеют отдельных полей/справочника в БД."],
    ["Отбрасывается", "square_kitchen, deposit, rental_period, beds_count, animals_allowed, children_allowed, for_students", "—", "Нет соответствующих колонок в текущей схеме."],
    ["Отбрасывается", "main_photo, media, visual, has_videos", "—", "Медиа не сохраняются."],
    ["Отбрасывается", "адресные ID, metro_station_id, расстояния до метро, wall_id", "—", "В БД оставляются нормализованные значения, а не технические ID."],
    ["Не хватает", "amenities[]", "amenities + listing_amenities", "Адаптер сейчас передает пустой список удобств."],
    ["Не хватает", "closed/active публикации риелтора", "sellers activity", "Сохраняется только минимальная оценка активности карточки."],
  ],
  Uybor: [
    ["Сохраняется", "id, operationType, price, priceCurrency, createdAt, url, description_length", "listings", "Канонический блок объявления."],
    ["Сохраняется", "region, district, street, zone, address", "listings + housing", "Адресные части объединяются в housing.street; район хранится отдельно."],
    ["Сохраняется", "room, square, floor, floorTotal", "listings + housing", "Комнаты, площадь и этажность."],
    ["Сохраняется", "isNewBuilding, foundation", "housing", "Тип фонда и материал здания."],
    ["Сохраняется", "lat, lng", "housing.latitude/longitude", "Координаты из Next.js JSON."],
    ["Вычисляется", "price, square, operationType", "housing.monthly_rent, rent_currency, price_per_m2", "Месячная аренда и цена за м²."],
    ["Сохраняется", "facilities[].name", "amenities + listing_amenities", "Удобства жилья."],
    ["Сохраняется", "user.displayName/firstName/lastName, user.role", "sellers", "Имя и роль agent/owner."],
    ["Сохраняется", "телефон из authenticated_page", "sellers.phone", "Телефон после авторизованного раскрытия; сохраняется phone_source."],
    ["Отбрасывается", "userId, ID региона/района/улицы/зоны, residentialComplexId", "—", "Технические идентификаторы не имеют колонок в актуальной БД."],
    ["Отбрасывается", "media, views, favorites, isPremium, isVip, dates of expiry", "—", "Явно перечислены в source_fields_not_in_canonical или не имеют колонок."],
    ["Отбрасывается", "repair", "—", "Колонка repair удалена из housing."],
    ["Отбрасывается", "source_data и исходный JSON", "—", "Сохраняются только в standalone JSON, не в SQLite."],
    ["Не хватает", "nearby/metro", "nearby_places + listing_nearby", "Поле metro есть в источнике, но текущий адаптер передает nearby=[] ."],
    ["Не хватает", "количество объявлений продавца", "sellers.*_listings", "Uybor показывает роль, но не надежную статистику публикаций."],
    ["Не хватает", "profile_url, official_complex_name", "sellers", "Персональные URL Uybor намеренно не сохраняются."],
  ],
  Realting: [
    ["Сохраняется", "housing_source_data.ID, Заголовок, Цена, URL", "listings", "Из сырого Realting JSON формируется каноническая запись."],
    ["Сохраняется", "Цена.Отображаемое значение + атрибуты", "listings.price/currency", "Цена и валюта нормализуются."],
    ["Сохраняется", "Дата публикации", "listings.published_at", "Берется только явно указанная структурированная дата."],
    ["Сохраняется", "Местонахождение.Город, Район", "listings + housing", "Город и район."],
    ["Сохраняется", "Параметры квартиры/здания: комнаты, площадь, этаж, этажность", "listings + housing", "Основные характеристики объекта."],
    ["Сохраняется", "Исходные скрипты координат", "housing.latitude/longitude", "Координаты извлекаются из singleMarker/LOCATION_YANDEX_MAP."],
    ["Вычисляется", "Цена + площадь", "housing.monthly_rent, price_per_m2", "Для каталога аренды рассчитываются месячная цена и цена за м²."],
    ["Сохраняется", "seller_source_data.name, profile_url, phone", "sellers", "Контакт агентства объявления, если блок агентства найден."],
    ["Отбрасывается", "Описание, Адрес под заголовком, Местонахождение на карте", "—", "В listings остается только description_length; street не заполняется текущим адаптером."],
    ["Отбрасывается", "Фотографии, Встроенный JSON, Мета-данные, исходный HTML", "—", "Нужны для аудита, но не имеют таблиц в основной БД."],
    ["Отбрасывается", "email, telegram_url, служебные raw_json_responses", "—", "Не входят в sellers и listing."],
    ["Отбрасывается", "нежилые записи каталога", "—", "Отсеиваются до сохранения через _is_residential()."],
    ["Не хватает", "street/точный адрес", "housing.street", "В сыром JSON адрес виден, но текущий _canonical_listing не переносит его в housing.street."],
    ["Не хватает", "building_type, is_new_building, foundation_type", "housing", "В текущем адаптере не извлекаются из характеристик."],
    ["Не хватает", "residential_complex_name, furnished, amenities, nearby", "housing + junction tables", "Данные могут быть в описании/HTML, но не преобразуются в канонические поля."],
    ["Не хватает", "активность агентства и закрытые публикации", "sellers activity", "Каталог агентств пока не объединен автоматически с карточками."],
  ],
};

const gapsRows = [
  ["Фотографии и видео", "Все источники", "Нет таблиц media/photos; URLs теряются после канонической проекции."],
  ["Полное описание", "Все источники", "В listings хранится только description_length."],
  ["История цен", "Все источники", "Нет таблицы price_history."],
  ["История статусов", "Все источники", "publication_status хранит текущее состояние, но не историю переходов."],
  ["Адресные ID", "Этажи, Uybor, OLX частично", "В актуальной housing оставлен объединенный street без address_id/street_id/house_id/zone_id."],
  ["Залог и период аренды", "Этажи", "deposit и rental_period есть в source_data, но нет колонок housing."],
  ["Расстояние до метро", "Этажи", "Станции сохраняются как nearby, расстояния и station_id — нет."],
  ["Удобства", "Этажи, Realting", "В источниках могут присутствовать, но адаптеры передают пустой/неполный amenities."],
  ["Объекты рядом", "Uybor, Realting", "Uybor metro не переносится в nearby; Realting метро не канонизируется."],
  ["Количество публикаций продавца", "Uybor, Realting", "Нет надежной статистики на карточке/в текущем адаптере."],
  ["Полные закрытые публикации", "OLX, Этажи, Uybor, Realting", "В БД сохраняются агрегаты, но не история карточек продавца."],
  ["Атрибуты квартиры", "Этажи, Uybor", "Площадь кухни, санузлы, балкон, парковка, животные, дети и т. п. не имеют колонок."],
];

function setBaseStyle(sheet, lastColumn, lastRow) {
  sheet.showGridLines = false;
  const used = sheet.getRange(`A1:${lastColumn}${lastRow}`);
  used.format.font = { name: "Aptos", size: 10, color: colors.ink };
  used.format.verticalAlignment = "top";
  used.format.wrapText = true;
}

function addTableSheet(workbook, name, title, subtitle, headers, rows, widths) {
  const sheet = workbook.worksheets.add(name);
  const lastColumn = String.fromCharCode(64 + headers.length);
  sheet.mergeCells(`A1:${lastColumn}1`);
  sheet.getRange("A1").values = [[title]];
  sheet.getRange("A1").format = { fill: colors.header, font: { name: "Aptos Display", size: 16, bold: true, color: colors.white }, horizontalAlignment: "left", verticalAlignment: "center" };
  sheet.getRange(`A1:${lastColumn}1`).format.rowHeight = 30;
  sheet.mergeCells(`A2:${lastColumn}2`);
  sheet.getRange("A2").values = [[subtitle]];
  sheet.getRange("A2").format = { fill: colors.header2, font: { italic: true, color: colors.muted }, wrapText: true, verticalAlignment: "center" };
  sheet.getRange(`A2:${lastColumn}2`).format.rowHeight = 32;
  const start = 4;
  sheet.getRange(`A${start}:${lastColumn}${start + rows.length}`).values = [headers, ...rows];
  const headerRange = sheet.getRange(`A${start}:${lastColumn}${start}`);
  headerRange.format = { fill: colors.header, font: { bold: true, color: colors.white }, horizontalAlignment: "left", verticalAlignment: "center", wrapText: true, borders: { preset: "all", style: "thin", color: colors.border } };
  headerRange.format.rowHeight = 26;
  const body = sheet.getRange(`A${start + 1}:${lastColumn}${start + rows.length}`);
  body.format.borders = { insideHorizontal: { style: "thin", color: colors.border }, outside: { style: "thin", color: colors.border } };
  body.format.rowHeight = 34;
  const statusRange = sheet.getRange(`A${start + 1}:A${start + rows.length}`);
  statusRange.format.font = { bold: true, color: colors.ink };
  // Apply a compact semantic palette to the first/status column.
  for (let i = 0; i < rows.length; i += 1) {
    const status = rows[i][0];
    const fill = status === "Сохраняется" ? colors.used : status === "Вычисляется" || status === "Нормализуется" ? colors.computed : status === "Отбрасывается" ? colors.discarded : colors.missing;
    sheet.getRange(`A${start + 1 + i}`).format.fill = fill;
  }
  const table = sheet.tables.add(`A${start}:${lastColumn}${start + rows.length}`, true, `${name.replace(/[^A-Za-z0-9]/g, "") || "Matrix"}Table`);
  table.style = "TableStyleMedium2";
  table.showFilterButton = true;
  table.showBandedRows = true;
  sheet.freezePanes.freezeRows(start);
  widths.forEach((width, index) => {
    sheet.getRange(`${String.fromCharCode(65 + index)}:${String.fromCharCode(65 + index)}`).format.columnWidth = width;
  });
  setBaseStyle(sheet, lastColumn, start + rows.length);
  // Reapply title/header formatting after base font setup.
  sheet.getRange("A1").format.font = { name: "Aptos Display", size: 16, bold: true, color: colors.white };
  sheet.getRange(`A${start}:${lastColumn}${start}`).format.font = { bold: true, color: colors.white };
  return sheet;
}

const workbook = Workbook.create();
const common = addTableSheet(workbook, "Общая схема", "Канонический JSON → SQLite", "Общие правила передачи данных из адаптеров источников в актуальную реляционную схему.", ["Статус", "JSON-путь", "Таблица/колонка БД", "Бизнес-назначение"], commonRows, [22, 42, 38, 68]);

for (const [name, rows] of Object.entries(sourceSheets)) {
  addTableSheet(workbook, name, `${name}: входной JSON и БД`, "Матрица полей текущего адаптера. «Отбрасывается» означает, что значение может быть прочитано, но не записывается в SQLite.", ["Статус", "JSON-путь/группа", "Куда попадает", "Комментарий"], rows, [22, 50, 32, 78]);
}

addTableSheet(workbook, "Пробелы БД", "Что не покрывает текущая БД", "Сводные поля и сущности, которые встречаются в источниках или нужны для полноты, но не имеют полноценного хранения в текущей схеме.", ["Недостающая сущность/поле", "Источники", "Причина и влияние"], gapsRows, [34, 26, 90]);

const readme = workbook.worksheets.add("README");
readme.showGridLines = false;
readme.mergeCells("A1:F1");
readme.getRange("A1").values = [["Матрица источников данных и базы недвижимости"]];
readme.getRange("A1:F1").format = { fill: colors.header, font: { name: "Aptos Display", size: 18, bold: true, color: colors.white }, verticalAlignment: "center" };
readme.getRange("A1:F1").format.rowHeight = 34;
readme.getRange("A3:B9").values = [
  ["Назначение", "Сопоставить входные JSON-поля OLX, Этажи, Uybor и Realting с актуальными таблицами SQLite."],
  ["Главное правило", "В SQLite записывается каноническая проекция; неизвестные поля исходного JSON не сохраняются."],
  ["Статусы", "Сохраняется / Нормализуется / Вычисляется / Отбрасывается / Не хватает."],
  ["Актуальные таблицы", "sellers, listings, housing, amenities, listing_amenities, nearby_places, listing_nearby, scrape_runs и scrape_run_listings."],
  ["Важное ограничение", "Полное source_data доступно в standalone JSON Этажей и Uybor, но не переносится в основную БД."],
  ["Дедупликация", "Для объявлений используется source + platform_listing_id или URL; объекты жилья сравниваются по city, district, rooms, area, floor, floors_total."],
  ["Дата анализа", "31.08.2026"],
];
readme.getRange("A3:A9").format = { fill: colors.header2, font: { bold: true, color: colors.ink }, verticalAlignment: "top", wrapText: true };
readme.getRange("A3:B9").format.borders = { insideHorizontal: { style: "thin", color: colors.border }, outside: { style: "thin", color: colors.border } };
readme.getRange("A3:B9").format.rowHeight = 34;
readme.getRange("A:A").format.columnWidth = 25;
readme.getRange("B:B").format.columnWidth = 115;
readme.getRange("A12:B16").values = [
  ["Лист", "Содержание"],
  ["Общая схема", "Полная каноническая карта JSON → БД."],
  ["OLX / Этажи / Uybor / Realting", "Источник-специфичная матрица использованных и отброшенных полей."],
  ["Пробелы БД", "Что требуется добавить для более полной модели данных."],
  ["README", "Методика чтения файла и ключевые ограничения."],
];
readme.getRange("A12:B12").format = { fill: colors.header, font: { bold: true, color: colors.white } };
readme.getRange("A13:B16").format.borders = { insideHorizontal: { style: "thin", color: colors.border }, outside: { style: "thin", color: colors.border } };
readme.getRange("A12:B16").format.wrapText = true;
readme.getRange("A12:B16").format.rowHeight = 28;
readme.freezePanes.freezeRows(2);

await fs.mkdir(outputDir, { recursive: true });
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);

const check = await workbook.inspect({ kind: "sheet,table", maxChars: 2500, tableMaxRows: 3, tableMaxCols: 4 });
console.log(check.ndjson);
console.log(`Saved ${outputPath}`);
