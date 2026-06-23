# TODO for Tomorrow

## 1. Finish the casing / printing first

- [ ] Finalize the two enclosure variants.
- [ ] Variant A: case with a groove/notch to make it easy to check whether both boxes are aligned on one straight line.
- [ ] Variant B: case/mounting setup where one box can be fixed to the sliding seat with a C-clamp.
- [ ] Add clear axis markings on both cases:
  - X = forward/back along the rowing direction.
  - Y = left/right.
  - Z = vertical.
- [ ] Check that the board cannot move inside the case.
- [ ] Check that USB, power, and SD access are still possible or document why access is limited.
- [ ] Take photos:
  - open case with board,
  - closed case,
  - both cases next to each other,
  - alignment feature,
  - C-clamp seat mounting,
  - final mounted setup.

## 2. Test and fix the program

- [ ] Compile the Arduino sketch.
- [ ] Flash both units if possible.
- [ ] Confirm one unit is `SEAT` and the other is `BOAT`.
- [ ] Record a short CSV on SD card.
- [ ] Check CSV header:
  `device_id,sequence,time_us,acc_x_ms2,acc_y_ms2,acc_z_ms2,gyro_x_rads,gyro_y_rads,gyro_z_rads`
- [ ] Run offline analysis on a test file.
- [ ] Check:
  - sampling rate,
  - sequence gaps,
  - stroke count,
  - SEAT minus BOAT,
  - BOAT speed proxy,
  - relative speed proxy,
  - smoothness.
- [ ] Start the live dashboard.
- [ ] Test USB or BLE mode.
- [ ] Connect Bluetooth headphones to the Mac.
- [ ] Enable voice feedback in the dashboard.
- [ ] Check that the dashboard speaks useful metrics and does not speak too often.

## 3. Thesis update after casing

- [ ] Replace future-tense wording with past-tense wording where the work is finished.
- [ ] Add explicit hardware section:
  - board,
  - IMU,
  - SD card,
  - BLE/USB,
  - power,
  - cost if known.
- [ ] Add system architecture figure.
- [ ] Add CSV data format table.
- [ ] Add enclosure photos.
- [ ] Add a short design comparison between the two case variants.
- [ ] Explain the alignment groove/notch.
- [ ] Explain the C-clamp seat mounting.
- [ ] Convert `Evaluation Plan` into `Evaluation` once the tests are done.
- [ ] Add `Discussion` or combine it with evaluation.
- [ ] Add final `Conclusion`.

## 4. Message to supervisor

Draft:

Dear [Supervisor Name],

I wanted to give you a short update on the current state of my bachelor thesis. The IMU-based rowing prototype is now implemented with SD logging, USB/BLE streaming, offline analysis, and a live dashboard. The current analysis focuses on relative and stroke-level metrics instead of exact absolute seat position, because this is more realistic for a low-cost IMU prototype.

I am currently finalizing the enclosure. I plan to test two mounting/case variants: one variant with an alignment feature to check whether both boxes are mounted on one straight line, and one variant using a C-clamp to attach one box to the sliding seat. After that I will run another technical test and add the final enclosure and evaluation section to the thesis.

At the moment the thesis draft already includes the introduction, related work, system requirements, prototype development, metrics, and an evaluation plan. I will revise the wording after the final tests and add the conclusion.

Best regards,
[Your Name]

## 5. My current assessment

This can become a good bachelor thesis if the final version stays honest and concrete.

Strong parts:

- Clear practical prototype.
- Good motivation for low-cost IMU sensing.
- Good related work basis.
- Clear argument for relative metrics instead of exact seat position.
- Real code, real logging, dashboard, and report.
- Enclosure connects mechanical design to data quality.

Risks to fix:

- Some wording still sounds like a plan instead of completed work.
- Hardware details need to be more explicit.
- Evaluation must show what was actually tested.
- Single-IMU and dual-IMU metrics should be separated clearly.
- Speed values must stay labelled as proxies, not true speed.

Target:

- A solid thesis does not need to prove a perfect rowing measurement system.
- It should show a complete development path from requirements to prototype and honest evaluation.
