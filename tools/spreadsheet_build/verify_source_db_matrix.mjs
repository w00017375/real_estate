import fs from "node:fs/promises";
import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const input = await FileBlob.load("../../docs/source_db_matrix.xlsx");
const workbook = await SpreadsheetFile.importXlsx(input);
const sheets = ["README", "Общая схема", "OLX", "Этажи", "Uybor", "Realting", "Пробелы БД"];
await fs.mkdir("../../docs/source_db_matrix_previews", { recursive: true });
for (const sheetName of sheets) {
  const sheet = workbook.worksheets.getItem(sheetName);
  const used = sheet.getUsedRange();
  const preview = await workbook.render({ sheetName, autoCrop: "all", scale: 1, format: "png" });
  await fs.writeFile(`../../docs/source_db_matrix_previews/${sheetName}.png`, new Uint8Array(await preview.arrayBuffer()));
  console.log(`${sheetName}: ${used.address}`);
}
const check = await workbook.inspect({ kind: "sheet,table", maxChars: 3500, tableMaxRows: 2, tableMaxCols: 4 });
console.log(check.ndjson);
