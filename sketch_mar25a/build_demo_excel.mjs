import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const inputCsv = "/Users/mahdoui/Downloads/LOG002.CSV";
const outputDir = "/Users/mahdoui/Downloads";
const outputPath = `${outputDir}/rowing_two_imu_demo.xlsx`;

const csvText = await fs.readFile(inputCsv, "utf8");
const lines = csvText.trim().split(/\r?\n/);
const headers = lines[0].split(",");
const rows = lines.slice(1).map((line) => {
  const parts = line.split(",");
  const row = {};
  headers.forEach((header, index) => {
    row[header] = index < 3 ? parts[index] : Number(parts[index]);
  });
  row.sequence = Number(row.sequence);
  row.time_us = Number(row.time_us);
  return row;
});

const maxRows = Math.min(rows.length, 1200);
const seatRows = rows.slice(0, maxRows);
const quietRows = rows.slice(0, Math.min(250, rows.length));

function mean(values) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function std(values) {
  const avg = mean(values);
  return Math.sqrt(values.reduce((sum, value) => sum + (value - avg) ** 2, 0) / values.length);
}

const channels = [
  "acc_x_ms2",
  "acc_y_ms2",
  "acc_z_ms2",
  "gyro_x_rads",
  "gyro_y_rads",
  "gyro_z_rads",
];

const quietMean = Object.fromEntries(
  channels.map((channel) => [channel, mean(quietRows.map((row) => row[channel]))])
);
const quietStd = Object.fromEntries(
  channels.map((channel) => [channel, std(quietRows.map((row) => row[channel]))])
);

function syntheticBoatValue(rowIndex, channel) {
  const row = seatRows[rowIndex];
  const slowWave = Math.sin(rowIndex / 85) * quietStd[channel] * 0.35;
  const tinyRepeatableNoise = Math.sin(row.sequence * 0.071) * quietStd[channel] * 0.18;
  return quietMean[channel] + slowWave + tinyRepeatableNoise;
}

const workbook = Workbook.create();
const dashboard = workbook.worksheets.add("Dashboard");
const assumptions = workbook.worksheets.add("Assumptions");
const comparison = workbook.worksheets.add("Comparison");
const chartData = workbook.worksheets.add("ChartData");
const rawSeat = workbook.worksheets.add("Raw_SEAT_sample");

function setValues(sheet, range, values) {
  sheet.getRange(range).values = values;
}

function setFormulas(sheet, range, formulas) {
  sheet.getRange(range).formulas = formulas;
}

function styleHeader(range) {
  range.format.fill.color = "#dce9e3";
  range.format.font.bold = true;
  range.format.font.color = "#173b35";
  range.format.borders.bottom.color = "#7c928a";
}

function styleTitle(range) {
  range.format.fill.color = "#0f766e";
  range.format.font.color = "#ffffff";
  range.format.font.bold = true;
  range.format.font.size = 16;
}

setValues(assumptions, "A1:D1", [["Two-IMU Demo Assumptions", "", "", ""]]);
styleTitle(assumptions.getRange("A1:D1"));
setValues(assumptions, "A3:D14", [
  ["Item", "Value", "Meaning", "Notes"],
  ["Dataset type", "DEMO / simulated reference", "BOAT is generated from quiet SEAT samples", "Do not present this as real BOAT measurement."],
  ["Input SEAT CSV", inputCsv, "Measured SEAT data", "First rows are used as example window."],
  ["BOAT reference source", "Mean of first quiet samples", "Stationary reference approximation", "Small repeatable noise added so the chart is readable."],
  ["Mounting convention", "X = forward/back", "seat_forward_acc = acc_x_ms2", "Same convention should be used for SEAT and BOAT."],
  ["Relative signal", "SEAT - BOAT", "Estimated motion relative to reference", "Works best after real synchronized BOAT measurement."],
  ["acc_x_ms2", quietMean.acc_x_ms2, "BOAT forward acceleration baseline", "From quiet SEAT section."],
  ["acc_y_ms2", quietMean.acc_y_ms2, "BOAT lateral acceleration baseline", "From quiet SEAT section."],
  ["acc_z_ms2", quietMean.acc_z_ms2, "BOAT vertical acceleration baseline", "From quiet SEAT section."],
  ["gyro_x_rads", quietMean.gyro_x_rads, "BOAT roll-rate baseline", "From quiet SEAT section."],
  ["gyro_y_rads", quietMean.gyro_y_rads, "BOAT pitch-rate baseline", "From quiet SEAT section."],
  ["gyro_z_rads", quietMean.gyro_z_rads, "BOAT yaw-rate baseline", "From quiet SEAT section."],
]);
styleHeader(assumptions.getRange("A3:D3"));

const rawHeader = headers;
setValues(rawSeat, "A1:L1", [rawHeader]);
styleHeader(rawSeat.getRange("A1:L1"));
setValues(
  rawSeat,
  `A2:L${seatRows.length + 1}`,
  seatRows.map((row) => rawHeader.map((header) => row[header]))
);

const comparisonHeaders = [
  "time_s",
  "sequence",
  "seat_forward_acc",
  "boat_forward_acc_demo",
  "relative_forward_acc",
  "seat_lateral_acc",
  "boat_lateral_acc_demo",
  "relative_lateral_acc",
  "seat_vertical_acc",
  "boat_vertical_acc_demo",
  "relative_vertical_acc",
  "seat_pitch_rate",
  "boat_pitch_rate_demo",
  "relative_pitch_rate",
  "seat_yaw_rate",
  "boat_yaw_rate_demo",
  "relative_yaw_rate",
];

setValues(comparison, "A1:Q1", [comparisonHeaders]);
styleHeader(comparison.getRange("A1:Q1"));

const comparisonValues = seatRows.map((row, index) => {
  const timeS = (row.time_us - seatRows[0].time_us) / 1_000_000;
  const boatForward = syntheticBoatValue(index, "acc_x_ms2");
  const boatLateral = syntheticBoatValue(index, "acc_y_ms2");
  const boatVertical = syntheticBoatValue(index, "acc_z_ms2");
  const boatPitch = syntheticBoatValue(index, "gyro_y_rads");
  const boatYaw = syntheticBoatValue(index, "gyro_z_rads");
  return [
    timeS,
    row.sequence,
    row.acc_x_ms2,
    boatForward,
    row.acc_x_ms2 - boatForward,
    row.acc_y_ms2,
    boatLateral,
    row.acc_y_ms2 - boatLateral,
    row.acc_z_ms2,
    boatVertical,
    row.acc_z_ms2 - boatVertical,
    row.gyro_y_rads,
    boatPitch,
    row.gyro_y_rads - boatPitch,
    row.gyro_z_rads,
    boatYaw,
    row.gyro_z_rads - boatYaw,
  ];
});
setValues(comparison, `A2:Q${comparisonValues.length + 1}`, comparisonValues);

setValues(chartData, "A1:G1", [[
  "time_s",
  "seat_forward_acc",
  "boat_forward_acc_demo",
  "relative_forward_acc",
  "relative_lateral_acc",
  "relative_vertical_acc",
  "relative_pitch_rate",
]]);
styleHeader(chartData.getRange("A1:G1"));
const chartEvery = Math.max(1, Math.floor(comparisonValues.length / 300));
const chartValues = comparisonValues
  .filter((_, index) => index % chartEvery === 0)
  .map((row) => [row[0], row[2], row[3], row[4], row[7], row[10], row[13]]);
setValues(chartData, `A2:G${chartValues.length + 1}`, chartValues);

setValues(dashboard, "A1:H1", [["Two-IMU Rowing Demo Workbook", "", "", "", "", "", "", ""]]);
styleTitle(dashboard.getRange("A1:H1"));
setValues(dashboard, "A3:H9", [
  ["Metric", "Value", "Interpretation", "", "Metric", "Value", "Interpretation", ""],
  ["Rows used", comparisonValues.length, "Demo sample count", "", "Duration (s)", comparisonValues.at(-1)[0], "Window length", ""],
  ["Mean relative forward acc", `=AVERAGE(Comparison!E2:E${comparisonValues.length + 1})`, "SEAT minus synthetic BOAT", "", "Std relative forward acc", `=STDEV.P(Comparison!E2:E${comparisonValues.length + 1})`, "Movement intensity", ""],
  ["Max relative forward acc", `=MAX(Comparison!E2:E${comparisonValues.length + 1})`, "Strongest positive relative acceleration", "", "Min relative forward acc", `=MIN(Comparison!E2:E${comparisonValues.length + 1})`, "Strongest negative relative acceleration", ""],
  ["Mean relative pitch", `=AVERAGE(Comparison!N2:N${comparisonValues.length + 1})`, "Pitch relative to reference", "", "Std relative pitch", `=STDEV.P(Comparison!N2:N${comparisonValues.length + 1})`, "Pitch variability", ""],
  ["Demo limitation", "Synthetic BOAT", "Replace with real BOAT CSV later", "", "Next step", "Two real sensors", "Synchronize and subtract real signals", ""],
  ["Main formula", "relative = SEAT - BOAT", "Transparent in Comparison sheet", "", "Marker support", "Use live dashboard", "Records markers beside captures", ""],
]);
styleHeader(dashboard.getRange("A3:H3"));

// Use formulas in summary cells where appropriate.
setFormulas(dashboard, "B5:B7", [
  [`=AVERAGE(Comparison!E2:E${comparisonValues.length + 1})`],
  [`=MAX(Comparison!E2:E${comparisonValues.length + 1})`],
  [`=AVERAGE(Comparison!N2:N${comparisonValues.length + 1})`],
]);
setFormulas(dashboard, "F5:F7", [
  [`=STDEV.P(Comparison!E2:E${comparisonValues.length + 1})`],
  [`=MIN(Comparison!E2:E${comparisonValues.length + 1})`],
  [`=STDEV.P(Comparison!N2:N${comparisonValues.length + 1})`],
]);

for (const sheet of [dashboard, assumptions, comparison, chartData, rawSeat]) {
  sheet.getRange("A1:Q2000").format.font.name = "Arial";
}

// Column widths keep the workbook readable in Excel/Numbers.
dashboard.getRange("A:H").format.columnWidth = 21;
assumptions.getRange("A:D").format.columnWidth = 28;
comparison.getRange("A:Q").format.columnWidth = 17;
chartData.getRange("A:G").format.columnWidth = 20;
rawSeat.getRange("A:L").format.columnWidth = 16;

// Number formatting.
comparison.getRange(`A2:A${comparisonValues.length + 1}`).format.numberFormat = "0.000";
comparison.getRange(`C2:Q${comparisonValues.length + 1}`).format.numberFormat = "0.000";
chartData.getRange(`A2:G${chartValues.length + 1}`).format.numberFormat = "0.000";
dashboard.getRange("B4:F7").format.numberFormat = "0.000";

// Native charts on dashboard.
dashboard.charts.add("line", {
  range: `ChartData!A1:D${chartValues.length + 1}`,
  title: "Forward acceleration: SEAT vs synthetic BOAT vs relative",
  position: { row: 10, col: 0 },
  widthPx: 760,
  heightPx: 300,
});

dashboard.charts.add("line", {
  range: `ChartData!A1:A${chartValues.length + 1},ChartData!E1:G${chartValues.length + 1}`,
  title: "Relative signals overview",
  position: { row: 27, col: 0 },
  widthPx: 760,
  heightPx: 300,
});

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

await workbook.render({ sheetName: "Dashboard", range: "A1:H44", scale: 1 });
await fs.mkdir(outputDir, { recursive: true });
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);
console.log(outputPath);
