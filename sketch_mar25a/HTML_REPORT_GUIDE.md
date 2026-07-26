# Creating the Offline HTML Report

This project can generate a self-contained HTML report from recorded IMU CSV files. The report can be opened directly in a browser and shared without Python or the original CSV files.

## 1. Prepare the files

Copy the SEAT and BOAT CSV files into one folder.

Example folder:

```text
/Users/mahdoui/Desktop/rowing_Miled1/
  LOG001.CSV   # SEAT
  LOG000.CSV   # BOAT
```

The script can auto-detect SEAT and BOAT from the CSV content if the first column contains the device name.

## 2. Recommended recording workflow

For a coach session, both IMUs should be powered on and recording before the athletes start rowing.

Recommended workflow:

1. Flash one IMU as `SEAT` and one IMU as `BOAT`.
2. Mount both IMUs in the same forward direction.
3. Start both IMUs as close together as practical.
4. Let athlete 1 row first, then athlete 2, then athlete 3, and so on.
5. Write down the approximate time window for every athlete measured from the start of the recording.
6. Copy the CSV files from both SD cards into one folder.
7. Generate one HTML report from the folder.
8. Use the HTML report to segment each athlete by time and export/share athlete-specific reports if needed.

The two IMUs do not have to start at exactly the same millisecond. The offline report aligns the recordings by their recorded sample time and compares the SEAT and BOAT signals over the shared time range. Starting them close together still makes the later athlete segmentation easier, because the written notes match the report timeline more directly.

Example notes during training:

```text
Recording started: both IMUs powered on
Athlete 1: 00:10 to 01:15
Athlete 2: 01:30 to 02:40
Athlete 3: 02:55 to 04:05
```

## 3. Generate the report from a folder

From the repository folder, run:

```bash
/usr/local/bin/python3 /Users/mahdoui/Desktop/Arduino/AnIMU-Based-Wearable-Sensing-Approach-for-Rowing/sketch_mar25a/analyze_log.py \
  /Users/mahdoui/Desktop/rowing_Miled1 \
  --output /Users/mahdoui/Desktop/rowing_Miled1_report.html \
  --label rowing_Miled1 \
  --min-peak-ms 800 \
  --smooth-window 10
```

Open the generated file:

```text
/Users/mahdoui/Desktop/rowing_Miled1_report.html
```

## 4. Generate the report from explicit files

If auto-detection is not desired, pass the SEAT file first and the BOAT file second:

```bash
/usr/local/bin/python3 /Users/mahdoui/Desktop/Arduino/AnIMU-Based-Wearable-Sensing-Approach-for-Rowing/sketch_mar25a/analyze_log.py \
  /path/to/SEAT.CSV \
  /path/to/BOAT.CSV \
  --output /path/to/report.html \
  --label athlete_test \
  --min-peak-ms 800 \
  --smooth-window 10
```

## 5. Calibration settings

Use the first 1-2 minutes of steady rowing to tune stroke detection.

1. Write down visible drive-start times: drive start 1, drive start 2, drive start 3, and so on.
2. Calculate the time between neighboring drive starts.
3. Find the smallest real time between two neighboring drive starts.
4. Set `--min-peak-ms` slightly smaller than that value in milliseconds.
5. Keep `--smooth-window 10` for the first test.

Example:

```text
drive starts: 0.80 s, 2.05 s, 3.28 s, 4.50 s
gaps:         1.25 s, 1.23 s, 1.22 s
smallest gap: 1.22 s
recommended --min-peak-ms: about 1100
```

## 6. Current stroke segmentation method

The current offline report uses a velocity-based stroke segmentation method.

Earlier versions relied more directly on smoothed acceleration peaks. The current version estimates a seat movement speed proxy from smoothed acceleration and detects the change into positive drive movement. A stroke is then measured from one detected drive start to the next detected drive start.

This makes the stroke window represent the rowing stroke more naturally than a peak-to-peak acceleration definition.

In practical terms:

- Older logic: a stroke could be treated more like acceleration peak to acceleration peak.
- Current logic: a stroke is treated more like drive start to next drive start.
- The current method uses the change into positive seat movement velocity, which should be more intuitive for rowing coaches because it follows when the seat starts moving through the drive instead of only asking where the largest acceleration spike occurred.

## 7. Useful command for Joanna

After downloading or cloning the repository, the most direct command is:

```bash
/usr/local/bin/python3 sketch_mar25a/analyze_log.py /path/to/folder_with_csv_files --output rowing_report.html --label rowing_report --min-peak-ms 800 --smooth-window 10
```

Then open:

```text
rowing_report.html
```
