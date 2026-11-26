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

You need Windows 10 or 11 (64-bit). The PC should stay switched on.

1. Download **HomeCameraSetup.exe** from this repository's **Releases** (Home Camera, latest build)
   and run it. Windows may warn that the app is from an unknown publisher: choose
   **More info**, then **Run anyway**.
2. Follow the wizard:
   - **Choose a password** for the viewer and phone app (at least 8 characters).
   - **Your camera**: its IP address (in the Hiseeu app's device info, or your router's list of
     devices), user name and password. Setup searches the camera for its video and saves what it
     finds, including the pan and tilt (ONVIF) details. Leave the IP empty to do this later.
   - **Where to keep recordings**: default `C:\CameraRecordings`.
3. Done. Open **Home Camera** from the desktop or Start menu. It starts by itself with Windows,
   even before anyone signs in.

**To add a camera or change its IP address or login later**, open Home Camera, go to
**Settings > Cameras**, fill in the details and press **Find and save**. It searches the camera
and starts using it straight away, with no restart needed. You can add more cameras there too.

The Start menu also has **Find camera again** (the same search, from a command window), **Change
password**, **Restart Home Camera**, and shortcuts to the settings and recordings folders.
Settings live in `C:\ProgramData\HomeCamera\config.json`; after editing that file by hand, use
**Restart Home Camera**. Settings changed in the viewer's Settings tab apply straight away.

To remove it, uninstall **Home Camera** from Windows Settings > Apps. Your recordings are kept.

<details>
<summary>Running from source instead (for developers)</summary>

Download the repository, right-click `pc\scripts\install.ps1` and choose **Run with
PowerShell**. It installs Python and FFmpeg, asks for a password and the camera, opens
`config.json` in Notepad, and registers the startup task. After editing `config.json`, run
`pc\scripts\restart.ps1`.
</details>

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
| `storage.retention_days` | Delete clips older than this. `0` keeps them until space runs low. (Also in Settings.) |
| `storage.min_free_gb` | Always leave this much space free, deleting the oldest clips if needed. |
| `server.port` | Port for the viewer and the app. Default 8080. |
| `server.https_cert`, `server.https_key` | Paths to a certificate and key to switch on HTTPS. |
| `cameras[].motion.person_only` | `true` to record only when a person is seen. |
| `cameras[].motion.pre_seconds` / `post_seconds` | Seconds kept before and after the motion. |

To change retention or free space, edit these in `C:\ProgramData\HomeCamera\config.json`
(needs admin rights, for example open Notepad with "Run as administrator").

To add a second camera, copy the camera block in `cameras` and give it its own `id` (letters,
numbers and dashes only).

## 2. Install the Android app

1. On your phone, open this repository's **Releases** on GitHub (right-hand side of the main
   page), open **Home Camera (latest build)** and download **HomeCamera.apk**.
   (Or open the `android` folder in Android Studio and build it yourself.)
2. Open the downloaded file. Allow installing from that source when asked.
3. Open **Home Camera**, enter the PC's address (for example `192.168.1.20:8080`) and sign in with
   your password. Allow notifications, and allow it to run without battery restrictions so alerts
   arrive while the phone is asleep.

The app shows a quiet "Watching for alerts" notification while it listens. That's Android's rule
for apps that keep a connection open; you can hide it by long-pressing it and turning off the
"Background connection" category.

## Home and away

Phone alerts for motion are held back while anyone is home, and come back on as soon as everyone
has left. Recording carries on exactly the same either way.

- **Set it up on each phone**: in the app, open **Settings > Home and away** while you're at home and
  press **Use this phone's location**. Allow location **all the time** when asked. The phone then
  tells the PC "home" or "out" as it comes and goes. Your actual location is never sent.
- **Switch by hand**: press the **Home / Away** button at the top of the screen, then choose
  **Home** or **Away**. It goes back to **Automatic** the next time a phone arrives home or leaves.
- **While someone is home** you can still be told about people (not just any movement).
- **Alert hours**: times when alerts are always on, even if you're home, such as overnight.
- A phone that hasn't checked in for 12 hours stops counting, so a lost or flat phone can't keep
  alerts switched off. With no phones set up, alerts are always on, as before.

## Health alerts

Your phone is told, whether you're home or away, when:

- a camera has sent no video for 5 minutes (and again when it's back),
- the recordings disk is nearly full, or the recordings folder can't be reached (for example an
  unplugged USB drive),
- a recording couldn't be saved.

The phone also warns by itself if it can't reach the PC for 15 minutes while the phone has
internet, for example after a power cut. Problems also show as a banner on the Live tab. Change the
minutes or turn each alert off in **Settings > Health alerts**; turn the phone's own warning off in
Android's notification settings for Home Camera ("Can't reach the PC").

## 3. Watching from outside the house

Point the app at an address that reaches the PC's port 8080 from the internet (for example a
port forward on your router plus a dynamic DNS name). Because the recorder is then reachable by
anyone, **use HTTPS**: set `server.https_cert` and `server.https_key` to a real certificate (such as
a free Let's Encrypt one for your DNS name). Without one, your password and video cross the
internet unencrypted. Five wrong passwords from one address locks it out for 15 minutes.

## Troubleshooting

- **Logs:** `C:\ProgramData\HomeCamera\logs\camhub.log` (Start menu > Home Camera > Settings and logs folder)
- **Camera not found, or its address changed:** Settings > Cameras in the viewer, then **Find and save**.
- **Change the password:** Start menu > Home Camera > Change password. This signs out every
  device, including the phone.
- **No pan and tilt buttons:** the camera didn't answer ONVIF on the given port. Check the
  `onvif` section and that ONVIF is on in the camera's settings.

## For developers

- Installer: `pc/installer` (PyInstaller recipe and Inno Setup script), built and test-installed
  on Windows by GitHub Actions.
- Recorder: Python 3.10+, FastAPI, OpenCV, FFmpeg. `pc/camhub/camera.py` holds the recording logic.
  Run tests with `python -m pytest pc/tests`.
- Viewer: plain HTML and JavaScript in `pc/camhub/static`, served by the recorder.
- Android: Kotlin, a WebView for the viewer plus a foreground service for alerts. Build with
  `cd android && ./gradlew assembleRelease`.
