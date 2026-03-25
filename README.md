# An-IMU-Based-Wearable-Sensing-Approach-for-Rowing

This project is part of a bachelor thesis focused on developing a low-cost wearable sensing system for rowing technique analysis using inertial measurement units (IMUs).

## Overview

Rowing performance depends on precise coordination between the athlete and the boat. However, existing measurement systems are often expensive, complex, or not suitable for continuous on-water training.

This project explores a simplified sensing approach using two IMU-based units:
- one mounted on the boat
- one mounted on the sliding seat

By comparing both signals, the system aims to investigate how relative motion between the seat and the boat can be used to derive qualitative indicators of rowing technique.

## Goals

The main objectives of this project are:

- develop a low-cost and portable sensing system for rowing
- implement reliable IMU data acquisition and logging
- explore relative motion estimation between seat and boat
- evaluate system performance in real training conditions
- design a robust hardware solution suitable for wet environments

## System Concept

The system consists of two sensor units:

### Seat Unit
- microcontroller (Adafruit Feather nRF52840 Sense)
- onboard IMU
- battery-powered
- transmits motion data via Bluetooth

### Boat Unit
- microcontroller (Adafruit Feather nRF52840 Sense or similar)
- onboard IMU
- SD card for data logging
- display for feedback
- receives data from seat unit

The boat unit combines both data streams to compute relative motion and calulate qualitative training/ rowing data.

## Development Roadmap

The prototype is developed in incremental stages:

1. single-board IMU logger  
2. SD card data logging  
3. second independent sensor unit  
4. offline comparison of both IMU signals  
5. Bluetooth communication between units  
6. real-time dual-IMU processing  
7. stroke phase detection  

## Repository Structure

IDK, update later



## Current Status

- literature review in progress (Zotero)
- hardware acquired (Feather nRF52840 Sense, SD module)
- repository initialized
- next step: basic IMU data logger

## Technologies

- idk, I will use C++ with arduino framework for hardware and data evaluatiion
- probably pyhton for data visualization

## Notes

This repository is used both for development and as a technical portfolio project.  
The goal is to document not only results but also design decisions, challenges, and system evolution.

## Author

Miled Mahdoui


