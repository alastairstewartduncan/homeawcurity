# Home Camera

Motion recording, live view and playback for an IP camera (built for the Hiseeu WHC905), with a
Windows recorder and an Android app.

- **Records only when something happens.** The last few seconds of video are kept in memory, so
  each clip starts just before the motion. Nothing is written to disk while the scene is still.
- **Person detection** so trees, rain, cars and cats don't fill the disk (switchable per camera).
- **Areas to ignore**: outline a road or a hedge and movement there is ignored.
- **Live view and recordings** on the PC (in its own window) and on the phone, by day, with
  thumbnails, a "People" filter, download and delete.
- **Phone alerts** with a snapshot when a recording starts, sent straight from your PC to the app.
- **Pan and tilt** from the PC and phone, if the camera supports ONVIF PTZ.
- **Looks after the disk**: deletes clips older than 30 days, and the oldest first if space runs low.
- **Password protected**, with optional HTTPS for access from outside the house.

```
 Camera ──RTSP──▶ PC recorder (pc/) ──▶ C:\CameraRecordings
                   │  web viewer + API on port 8080
                   ├──▶ "Home Camera" window on the PC
                   └──▶ Android app (android/): live, recordings, alerts
```

The camera's video is copied, not re-encoded, so clips keep full quality and the PC barely works.
The small second stream from the camera is used for motion and person detection and for live view.

## 1. Set up the PC

You need Windows 10 or 11 and about 10 minutes.

1. Download this repository (green **Code** button, then **Download ZIP**) and unzip it somewhere
   permanent, such as `C:\HomeCamera`.
2. Open the `pc\scripts` folder, right-click `install.ps1` and choose **Run with PowerShell**.
   Allow it to make changes when Windows asks. If Windows blocks the script, run this once in an
   admin PowerShell window first: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
3. It installs Python and FFmpeg if needed, then asks you to:
   - **choose a password** for the viewer and phone app (at least 8 characters);
   - **enter the camera's IP address and user name.** It then tries the usual stream addresses
     and prints the ones that work. Find the IP in the Hiseeu app's device info, or your router's
     list of devices. The camera password is typed into the PowerShell window only.
4. Notepad opens `config.json`. Paste the `main_url` and `sub_url` it printed, and fill in the
   `onvif` section (same IP, port 8899 on most Hiseeu cameras, same user name and password) for
   pan and tilt. Save and close Notepad.
5. Done. Use the **Home Camera** icon on the desktop. The recorder starts by itself with Windows,
   even before anyone signs in.

After editing `config.json` by hand, run `pc\scripts\restart.ps1`. Settings changed in the
viewer's Settings tab apply straight away.

### Camera tips

- In the Hiseeu app, give the camera a fixed IP address (or reserve one in your router) so the
  address doesn't change.
- If the PC viewer shows clips but they won't play, the camera is probably sending H.265 and
  your PC lacks the free **HEVC Video Extensions** (Microsoft Store). Alternatively set the camera
  to H.264 in its app.
- Keep the camera's own SD card recording on. It's a second copy if the PC is ever off.

### Settings in config.json

| Setting | Meaning |
| --- | --- |
| `storage.path` | Where clips go. Default `C:/CameraRecordings`. |
| `storage.retention_days` | Delete clips older than this. `0` keeps them until space runs low. |
| `storage.min_free_gb` | Always leave this much space free, deleting the oldest clips if needed. |
| `server.port` | Port for the viewer and the app. Default 8080. |
| `server.https_cert`, `server.https_key` | Paths to a certificate and key to switch on HTTPS. |
| `cameras[].motion.person_only` | `true` to record only when a person is seen. |
| `cameras[].motion.pre_seconds` / `post_seconds` | Seconds kept before and after the motion. |

To add a second camera, copy the camera block in `cameras` and give it its own `id` (letters,
numbers and dashes only).

## 2. Install the Android app

1. Open this repository's **Actions** tab on GitHub, open the latest **Build** run, and download
   **HomeCamera-apk** at the bottom. Unzip it to get the `.apk`.
   (Or open the `android` folder in Android Studio and build it yourself.)
2. Copy the `.apk` to your phone and open it. Allow installing from that source when asked.
3. Open **Home Camera**, enter the PC's address (for example `192.168.1.20:8080`) and sign in with
   your password. Allow notifications, and allow it to run without battery restrictions so alerts
   arrive while the phone is asleep.

The app shows a quiet "Watching for alerts" notification while it listens. That's Android's rule
for apps that keep a connection open; you can hide it by long-pressing it and turning off the
"Background connection" category.

## 3. Watching from outside the house

Point the app at an address that reaches the PC's port 8080 from the internet (for example a
port forward on your router plus a dynamic DNS name). Because the recorder is then reachable by
anyone, **use HTTPS**: set `server.https_cert` and `server.https_key` to a real certificate (such as
a free Let's Encrypt one for your DNS name). Without one, your password and video cross the
internet unencrypted. Five wrong passwords from one address locks it out for 15 minutes.

## Troubleshooting

- **Logs:** `pc\logs\camhub.log`
- **Find the stream again:** `pc\.venv\Scripts\python -m camhub find-stream 192.168.1.50 --user admin`
- **Change the password:** `pc\.venv\Scripts\python -m camhub set-password`, then restart.
  This signs out every device, including the phone.
- **No pan and tilt buttons:** the camera didn't answer ONVIF on the given port. Check the
  `onvif` section and that ONVIF is on in the camera's settings.

## For developers

- Recorder: Python 3.10+, FastAPI, OpenCV, FFmpeg. `pc/camhub/camera.py` holds the recording logic.
  Run tests with `python -m pytest pc/tests`.
- Viewer: plain HTML and JavaScript in `pc/camhub/static`, served by the recorder.
- Android: Kotlin, a WebView for the viewer plus a foreground service for alerts. Build with
  `cd android && ./gradlew assembleRelease`.
