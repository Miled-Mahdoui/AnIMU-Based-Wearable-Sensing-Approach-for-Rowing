#include <Wire.h>
#include <SPI.h>
#include <SD.h>

#include <Adafruit_Sensor.h>
#include <Adafruit_LSM6DS33.h>
#include <Adafruit_LSM6DS3TRC.h>
#include <bluefruit.h>

// =============================
// Gerätekonfiguration
// =============================

// Role identifier written into each CSV row and used for the BLE name.
// Use "SEAT" for the seat-mounted unit and "BOAT" for the reference unit.
const char DEVICE_ID[] = "BOAT";

constexpr uint8_t SD_CS_PIN = 10;

// 100 Hz = eine Messung alle 10.000 Mikrosekunden
constexpr uint32_t SAMPLE_RATE_HZ = 100;
constexpr uint32_t SAMPLE_INTERVAL_US =
    1000000UL / SAMPLE_RATE_HZ;

// Nicht bei jeder Zeile flushen.
// Häufiges flush() kann Messintervalle stören.
constexpr uint16_t FLUSH_EVERY_N_SAMPLES = 100;

// BLE sendet dieselben CSV-Zeilen wie USB-Serial.
// 2 bedeutet: bei 100 Hz Messrate werden ca. 50 BLE-Zeilen/s gesendet.
constexpr bool ENABLE_BLE_STREAM = true;
constexpr uint16_t BLE_STREAM_EVERY_N_SAMPLES = 2;

// =============================
// Sensorobjekte
// =============================

Adafruit_LSM6DS33 imu33;
Adafruit_LSM6DS3TRC imu3trc;

enum class ImuType {
  NONE,
  LSM6DS33,
  LSM6DS3TRC
};

ImuType detectedImu = ImuType::NONE;

// =============================
// Bluetooth LE
// =============================

BLEUart bleuart;

// =============================
// SD und Timing
// =============================

File logFile;

uint32_t sequenceNumber = 0;
uint32_t nextSampleTimeUs = 0;
uint16_t samplesSinceFlush = 0;

// =============================
// Hilfsfunktionen
// =============================

void stopWithError(const char* message) {
  Serial.println(message);

  while (true) {
    digitalWrite(LED_BUILTIN, HIGH);
    delay(150);
    digitalWrite(LED_BUILTIN, LOW);
    delay(150);
  }
}

bool initializeImu() {
  /*
   * Erst den neueren Sensor versuchen.
   * Falls er nicht gefunden wird, den älteren LSM6DS33 versuchen.
   */

  if (imu3trc.begin_I2C()) {
    detectedImu = ImuType::LSM6DS3TRC;
    Serial.println("IMU erkannt: LSM6DS3TR-C");
    return true;
  }

  if (imu33.begin_I2C()) {
    detectedImu = ImuType::LSM6DS33;
    Serial.println("IMU erkannt: LSM6DS33");
    return true;
  }

  detectedImu = ImuType::NONE;
  return false;
}

void configureImu() {
  /*
   * Einstellungen zunächst bewusst moderat:
   *
   * Accelerometer: ±4 g
   * Gyroskop:      ±500 deg/s
   * Datenrate:     104 Hz
   *
   * Die Bibliotheken liefern Beschleunigung später in m/s²
   * und Drehrate in rad/s.
   */

  if (detectedImu == ImuType::LSM6DS3TRC) {
    imu3trc.setAccelRange(LSM6DS_ACCEL_RANGE_4_G);
    imu3trc.setGyroRange(LSM6DS_GYRO_RANGE_500_DPS);
    imu3trc.setAccelDataRate(LSM6DS_RATE_104_HZ);
    imu3trc.setGyroDataRate(LSM6DS_RATE_104_HZ);
  } else if (detectedImu == ImuType::LSM6DS33) {
    imu33.setAccelRange(LSM6DS_ACCEL_RANGE_4_G);
    imu33.setGyroRange(LSM6DS_GYRO_RANGE_500_DPS);
    imu33.setAccelDataRate(LSM6DS_RATE_104_HZ);
    imu33.setGyroDataRate(LSM6DS_RATE_104_HZ);
  }
}

void readImu(
    sensors_event_t& acceleration,
    sensors_event_t& gyroscope,
    sensors_event_t& temperature) {

  if (detectedImu == ImuType::LSM6DS3TRC) {
    imu3trc.getEvent(
        &acceleration,
        &gyroscope,
        &temperature
    );
  } else {
    imu33.getEvent(
        &acceleration,
        &gyroscope,
        &temperature
    );
  }
}

void startBleAdvertising() {
  Bluefruit.Advertising.addFlags(
      BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE
  );
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(bleuart);
  Bluefruit.ScanResponse.addName();

  Bluefruit.Advertising.restartOnDisconnect(true);
  Bluefruit.Advertising.setInterval(32, 244);
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);
}

void initializeBle() {
  if (!ENABLE_BLE_STREAM) {
    return;
  }

  char bleName[32];
  snprintf(
      bleName,
      sizeof(bleName),
      "Rowing-%s",
      DEVICE_ID
  );

  Bluefruit.autoConnLed(true);
  Bluefruit.configPrphBandwidth(BANDWIDTH_MAX);
  Bluefruit.begin();
  Bluefruit.setTxPower(4);
  Bluefruit.setName(bleName);

  bleuart.begin();
  startBleAdvertising();

  Serial.print("BLE UART advertising as: ");
  Serial.println(bleName);
}

String createUniqueFilename() {
  /*
   * Erzeugt LOG000.CSV bis LOG999.CSV.
   * Kurze Dateinamen vermeiden Kompatibilitätsprobleme.
   */

  char filename[13];

  for (uint16_t index = 0; index < 1000; index++) {
    snprintf(filename, sizeof(filename), "LOG%03u.CSV", index);

    if (!SD.exists(filename)) {
      return String(filename);
    }
  }

  return String();
}

void writeCsvHeader() {
  logFile.println(
      "device_id,"
      "sequence,"
      "time_us,"
      "acc_x_ms2,"
      "acc_y_ms2,"
      "acc_z_ms2,"
      "gyro_x_rads,"
      "gyro_y_rads,"
      "gyro_z_rads"
  );

  logFile.flush();
}

void writeMeasurementToStream(
    Print& output,
    uint32_t timestampUs,
    const sensors_event_t& acceleration,
    const sensors_event_t& gyroscope) {

  // Keep the USB, BLE, and SD formats identical so Python can parse every
  // stream with the same CSV parser.
  output.print(DEVICE_ID);
  output.print(',');
  output.print(sequenceNumber);
  output.print(',');
  output.print(timestampUs);
  output.print(',');

  output.print(acceleration.acceleration.x, 6);
  output.print(',');
  output.print(acceleration.acceleration.y, 6);
  output.print(',');
  output.print(acceleration.acceleration.z, 6);
  output.print(',');

  output.print(gyroscope.gyro.x, 6);
  output.print(',');
  output.print(gyroscope.gyro.y, 6);
  output.print(',');
  output.println(gyroscope.gyro.z, 6);
}

void writeMeasurement(
    uint32_t timestampUs,
    const sensors_event_t& acceleration,
    const sensors_event_t& gyroscope) {

  writeMeasurementToStream(
      logFile,
      timestampUs,
      acceleration,
      gyroscope
  );

  writeMeasurementToStream(
      Serial,
      timestampUs,
      acceleration,
      gyroscope
  );

  if (
      ENABLE_BLE_STREAM &&
      bleuart.notifyEnabled() &&
      sequenceNumber % BLE_STREAM_EVERY_N_SAMPLES == 0
  ) {
    // BLE is intentionally downsampled compared with SD/USB logging. This
    // improves live stability while preserving full-rate data on the SD card.
    writeMeasurementToStream(
        bleuart,
        timestampUs,
        acceleration,
        gyroscope
    );
  }
}

// =============================
// Setup
// =============================

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Serial.begin(115200);

  // Nur begrenzt auf USB-Verbindung warten.
  // Dadurch startet das Gerät später auch ohne angeschlossenen PC.
  uint32_t serialWaitStart = millis();

  while (!Serial && millis() - serialWaitStart < 3000) {
    delay(10);
  }

  Serial.println();
  Serial.println("Rowing IMU Logger");
  Serial.print("Device: ");
  Serial.println(DEVICE_ID);

  initializeBle();

  Wire.begin();

  if (!initializeImu()) {
    stopWithError(
        "Fehler: Kein LSM6DS33 oder LSM6DS3TR-C gefunden."
    );
  }

  configureImu();

  if (!SD.begin(SD_CS_PIN)) {
    stopWithError(
        "Fehler: SD-Karte konnte nicht initialisiert werden."
    );
  }

  String filename = createUniqueFilename();

  if (filename.length() == 0) {
    stopWithError(
        "Fehler: Kein freier Log-Dateiname gefunden."
    );
  }

  logFile = SD.open(filename.c_str(), FILE_WRITE);

  if (!logFile) {
    stopWithError(
        "Fehler: Logdatei konnte nicht geöffnet werden."
    );
  }

  writeCsvHeader();

  Serial.print("Logdatei: ");
  Serial.println(filename);
  Serial.println("Messung startet.");

  sequenceNumber = 0;
  samplesSinceFlush = 0;
  nextSampleTimeUs = micros();

  digitalWrite(LED_BUILTIN, HIGH);
}

// =============================
// Messschleife
// =============================

void loop() {
  const uint32_t currentTimeUs = micros();

  /*
   * Differenz mit signed integer auswerten.
   * Dadurch bleibt der Vergleich auch beim micros()-Überlauf korrekt.
   */
  if ((int32_t)(currentTimeUs - nextSampleTimeUs) < 0) {
    return;
  }

  /*
   * Den nächsten Zeitpunkt auf Basis des Sollrasters bestimmen.
   * Nicht einfach delay(10), weil sonst Rechen- und Schreibzeiten
   * die Messfrequenz zunehmend verschieben.
   */
  nextSampleTimeUs += SAMPLE_INTERVAL_US;

  const uint32_t measurementTimestampUs = micros();

  sensors_event_t acceleration;
  sensors_event_t gyroscope;
  sensors_event_t imuTemperature;

  readImu(
      acceleration,
      gyroscope,
      imuTemperature
  );

  writeMeasurement(
      measurementTimestampUs,
      acceleration,
      gyroscope
  );

  sequenceNumber++;
  samplesSinceFlush++;

  /*
   * Nach ungefähr einer Sekunde Daten auf die Karte schreiben.
   * Bei Stromverlust kann dadurch maximal etwa die letzte Sekunde
   * fehlen. Für spätere Versionen können wir das optimieren.
   */
  if (samplesSinceFlush >= FLUSH_EVERY_N_SAMPLES) {
    logFile.flush();
    samplesSinceFlush = 0;
  }

  /*
   * Falls Schreiben oder Serial-Ausgabe länger als ein Intervall
   * dauert, nicht tausende alte Messzeitpunkte nachholen.
   */
  const uint32_t afterMeasurementUs = micros();

  if ((int32_t)(afterMeasurementUs - nextSampleTimeUs) >
      (int32_t)SAMPLE_INTERVAL_US) {

    nextSampleTimeUs =
        afterMeasurementUs + SAMPLE_INTERVAL_US;
  }
}
