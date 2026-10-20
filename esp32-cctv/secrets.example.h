// Copy this file to secrets.h and fill in your own values.
// secrets.h is gitignored so your WiFi password and upload URL stay local.

#pragma once

// ---- your WiFi (must be the same network as the camera, and 2.4 GHz:
//      the classic ESP32 cannot join a 5 GHz network) ----
#define WIFI_SSID      "YOUR_WIFI_NAME"
#define WIFI_PASSWORD  "YOUR_WIFI_PASSWORD"

// ---- camera ----
#define CAM_HOST       "192.168.1.50"
#define CAM_RTSP_PORT  554
#define CAM_RTSP_PATH  "/live/ch00_1"   // sub stream: small and cheap to carry

// ---- Oracle Object Storage pre-authenticated request ----
// Create a bucket, then a PAR with "permit object reads and writes".
// Split the URL it gives you into host and path.
#define OCI_HOST  "objectstorage.<region>.oraclecloud.com"
#define OCI_PATH  "/p/<token>/n/<namespace>/b/<bucket>/o/"
