<p align="center"><img src="branding/logo-horizontal.svg" alt="A.S.D Home Security" width="300"></p>

# A.S.D Home Security

Motion recording, live view and playback for an IP camera (built for the Hiseeu WHC905), with a
Windows recorder and an Android app.

- **Records only when something happens.** The last few seconds of video are kept in memory, so
  each clip starts just before the motion. Nothing is written to disk while the scene is still.
- **Person detection** so trees, rain, cars and cats don't fill the disk (switchable per camera).
- **Areas to ignore**: outline a road or a hedge and movement there is ignored.
- **Timeline** under each live picture: drag back through the day to find a recording. Clips show
  as marks with gaps between them (nothing is recorded when nothing moves); a gap jumps to the next
  recording, and playback carries on through the day's clips. Zoom to 6 or 1 hours, or step to
  earlier days.
- **Night summary** each morning: one short sped-up video of everything recorded overnight, from
  every camera, with the time on the picture. Your phone gets a notification when it's ready.
- **Camera grid** for two or more cameras, with one combined timeline: drag back and every camera
  plays from that moment together.
- **Watch on TV**: show the cameras live on a Chromecast, Google TV or a smart TV's web browser.
- **Privacy areas**: cover a neighbour's window or the street with blur or black. They are hidden in
  the live picture, saved pictures, phone alerts and new recordings (Settings > Privacy).
- **Live view and recordings** on the PC (in its own window) and on the phone, by day, with
  thumbnails, a "People" filter, download and delete. Tap **Select** (or press and hold a recording on the phone) to tick several, or **Select all** for the day, and delete them in one go.
- **Phone alerts** with a snapshot when a recording starts, sent straight from your PC to the app.
- **Pan and tilt** from the PC and phone, if the camera supports ONVIF PTZ.
- **Looks after the disk**: deletes clips older than 30 days, and the oldest first if space runs low.
- **Password protected**, with encrypted away-from-home access for phones you set up at home.

```
 Camera ──RTSP──▶ PC recorder (pc/) ──▶ C:\CameraRecordings
                   │  web viewer + API on port 8080
                   ├──▶ "A.S.D Home Security" window on the PC
                   └──▶ Android app (android/): live, recordings, alerts
```

The camera's video is copied, not re-encoded, so clips keep full quality and the PC barely works.
The small second stream from the camera is used for motion and person detection and for live view.

## 1. Set up the PC

You need Windows 10 or 11 (64-bit). The PC should stay switched on.

1. Download **HomeCameraSetup.exe** from this repository's **Releases** (A.S.D Home Security, latest build)
   and run it. Windows may warn that the app is from an unknown publisher: choose
   **More info**, then **Run anyway**.
2. Follow the wizard:
   - **Choose a password** for the viewer and phone app (at least 8 characters).
   - **Your camera**: its IP address (in the Hiseeu app's device info, or your router's list of
     devices), user name and password. Setup searches the camera for its video and saves what it
     finds, including the pan and tilt (ONVIF) details. Leave the IP empty to do this later.
   - **Where to keep recordings**: default `C:\CameraRecordings`.
3. Done. Open **A.S.D Home Security** from the desktop or Start menu. It starts by itself with Windows,
   even before anyone signs in, and records in the background with no window open.
   When you sign in, its logo appears in the system tray by the clock: click it to open the
   window, or right-click for **Open**, **Restart recorder** and **Quit**. Closing the window
   (or choosing Quit) doesn't stop recording.

**To add a camera or change its IP address or login later**, open A.S.D Home Security, go to
**Settings > Cameras**, fill in the details and press **Find and save**. It searches the camera
and starts using it straight away, with no restart needed. You can add more cameras there too.

The Start menu also has **Find camera again** (the same search, from a command window), **Change
password**, **Restart A.S.D Home Security**, and shortcuts to the settings and recordings folders.
Settings live in `C:\ProgramData\HomeCamera\config.json`; after editing that file by hand, use
**Restart A.S.D Home Security**. Settings changed in the viewer's Settings tab apply straight away.

To remove it, uninstall **A.S.D Home Security** from Windows Settings > Apps. Your recordings are kept.

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

### Off-site backup (OneDrive or Google Drive)

So you keep the footage if the PC is stolen, open **Settings > Off-site backup**, tick
**Back up recordings**, pick your OneDrive or Google Drive folder and press **Save**.

- The picture from the start of each recording is copied within seconds; the video as soon as
  the recording ends. The OneDrive or Google Drive app on the PC then uploads it.
- Everything goes into a folder called `Home Camera recordings` inside the folder you pick.
- Deleting a clip in the viewer does not delete its backup.
- Old backups are removed after the number of days you choose, and the oldest are removed first
  to stay under the size limit (free OneDrive has 5 GB, free Google Drive 15 GB).
- OneDrive comes with Windows: open it from the Start menu and sign in. For Google Drive, install
  Google Drive for desktop and in its settings choose **Mirror files**, so it has a normal folder
  (usually `C:\Users\<you>\My Drive`) that A.S.D Home Security can find.

### Settings in config.json

| Setting | Meaning |
| --- | --- |
| `storage.path` | Where clips go. Default `C:/CameraRecordings`. |
| `storage.retention_days` | Delete clips older than this. `0` keeps them until space runs low. (Also in Settings.) |
| `storage.min_free_gb` | Always leave this much space free, deleting the oldest clips if needed. |
| `server.port` | Port for the viewer and the app. Default 8080. |
| `server.https_cert`, `server.https_key` | Paths to a certificate and key to switch on HTTPS. |
| `backup.enabled`, `backup.folder` | Copy recordings off-site into this folder. (Also in Settings.) |
| `backup.keep_days`, `backup.max_gb` | How long to keep backups, and the most space they may use. |
| `cameras[].motion.person_only` | `true` to record only when a person is seen. |
| `cameras[].motion.pre_seconds` / `post_seconds` | Seconds kept before and after the motion. |

To change retention or free space, edit these in `C:\ProgramData\HomeCamera\config.json`
(needs admin rights, for example open Notepad with "Run as administrator").

To add a second camera, copy the camera block in `cameras` and give it its own `id` (letters,
numbers and dashes only).

## 2. Install the Android app

1. On your phone, open this repository's **Releases** on GitHub (right-hand side of the main
   page), open **A.S.D Home Security (latest build)** and download **HomeCamera.apk**.
   (Or open the `android` folder in Android Studio and build it yourself.)
2. Open the downloaded file. Allow installing from that source when asked.
3. Open **A.S.D Home Security**, enter the PC's address (for example `192.168.1.20:8080`) and sign in with
   your password. Allow notifications, and allow it to run without battery restrictions so alerts
   arrive while the phone is asleep.

The app shows a quiet "Watching for alerts" notification while it listens. That's Android's rule
for apps that keep a connection open; you can hide it by long-pressing it and turning off the
"Background connection" category.

### Home screen widget

Long-press an empty spot on your home screen, choose **Widgets**, and drag **A.S.D Home Security**
onto the screen. It shows:

- the camera's latest picture (tap it, or the play button, to open live view; tap the refresh
  button for a new picture). With more than one camera, tap the camera's name for the next one.
- **Home** or **Away** (tap to switch between automatic, Home and Away; see Home and away below)
- the last alert, with its picture (tap it to watch the recording)

The widget refreshes every 30 minutes, when you tap refresh, and straight away when an alert
arrives. It uses the same connection as the app, so it works away from home too once the phone is
set up for that.

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
Android's notification settings for A.S.D Home Security ("Can't reach the PC").

## AI agents and skills (AI tab)

The **AI** tab lets you add your own AI helpers that look at the camera picture when something happens.

- **AI sources**: choose who does the thinking. Anthropic (Claude), OpenAI, Google Gemini, Ollama running on your
  own PC, or any OpenAI-compatible service (LM Studio, OpenRouter, Groq and others). Paste your API key, press
  **Load models** and pick a model that can look at pictures. Keys stay in `config.json` on the PC and are never sent
  back to the browser or the phone. Add as many sources as you like; each agent can use a different one and model.
- **Skills**: reusable know-how in plain words, such as "Spot deliveries". Three examples are included.
- **Agents**: say what to watch for and when to tell you, choose the skills, when it runs (person seen, recording
  starts or finishes, or every few minutes), which cameras, and the AI source and model. The agent sends a phone
  alert (with the picture) when it decides you should know. **Test now** shows what it would do on the current picture.
- **Create with AI**: describe what you want and the AI writes the agent and any new skills for you to check.
  Editors also have **Change it with AI**.
- **What the agents saw** lists every time an agent looked, with its picture and reasoning. A copy is kept in
  `ai_activity.jsonl` next to `config.json`.

- **Recording descriptions**: turn on "Describe each new recording" (AI tab > Recordings and morning digest) and
  each clip gets a one-line description, such as "A courier in a yellow jacket leaves a parcel", shown under it.
  **Describe the last day's recordings** fills in older clips, and the player has **Describe with AI**.
- **Morning digest**: at the time you choose, your phone gets a summary of the last 24 hours, such as
  "2 deliveries, 1 unknown person", with the best clip.
- **Ask your recordings**: type a question at the top of the Recordings tab, such as "when did the postman come?",
  and jump straight to the matching clips. It works from the descriptions, so turn those on first.

Each look is one AI request, so a cloud AI is charged per look. Use the quiet time and the "Every few minutes"
setting to keep costs down, or run a local model with Ollama for free.

## Night summaries, camera grid and TV

- **Night summary**: at 07:30 each morning the PC makes one video of last night's recordings (21:00 to
  07:00), played back to back and sped up so it lasts about a minute. Busier nights play faster (4× to
  120×). Find it at the top of the **Recordings** tab; under the video, tap a time to jump to it, or
  double-click (long-press on the phone) to open the full recording. Change the hours, length and how
  long to keep them in **Settings > Night summary**, or press **Make last night's now**. Summaries are
  kept in a `_summaries` folder inside the recordings folder (30 days by default).
- **Camera grid**: with two or more cameras, the **Live** tab has **Each camera | Grid**. The grid
  shows every camera at once, with one timeline below that has a row (and colour) per camera. Drag or
  tap it to go back: every camera plays its recording from that moment together, at 1×, 2× or 4×, and
  times when no camera recorded anything are skipped. A camera that recorded nothing at that moment
  says so. **Back to live** returns to the live pictures. Works the same in the phone app.
- **Watch on TV**:
  - **Chromecast or Google TV**: in the phone app, press **📺 TV** on the Live tab and choose a camera
    (or all cameras together), then your TV. Night summaries have a **Play on TV** button. The TV
    fetches the video straight from the PC, so the phone, TV and PC need to be on the same home
    network. Live view on the TV is a few seconds behind. While casting, the PC turns the live
    picture into a TV-friendly video in memory (nothing is written to disk) and stops when the TV
    stops watching. TV links expire after 12 hours unused.
  - **Smart TV with a web browser** (Samsung, LG, Fire TV and others): open
    `http://<PC address>:8080/tv` in the TV's browser and sign in once. Every camera shows full
    screen; use the remote's arrows and OK to enlarge one, and Back to return.
  - **From the PC**: open the TV page in Google Chrome, then Chrome's menu > **Cast…**.

## Smart alerts (Smart tab)

Everything here runs on the PC. No pictures are sent anywhere.

- **Family and friends.** Add the people who live here (a few photos each, or name faces from
  "Faces seen lately"). Turn on *Recognise faces* and "person seen" alerts become **stranger**
  alerts, with no alert for the people you've named. If no face can be seen, you choose whether
  it still alerts.
- **Learns as you go.** When a face looks like someone you've named but it isn't sure, you get
  an **"Is this Sarah?"** notification with the picture instead of a stranger alert. Tap *Yes*
  and that face is added to Sarah; *No* and a face like it is never taken for her again (it goes
  to "Faces seen lately" to name); *Someone else* opens the Smart tab to pick or type a name.
  Faces it is very sure of, that show someone in a new light (glasses, night), are also kept by
  themselves, marked ✦, and never replace your own photos. Several unclear looks at the same
  person are put together, which is surer than any one look. Both can be turned off.
- **Sharper faces.** Small, dark or grainy faces are enlarged, brightened (at night) and
  sharpened on the PC before matching, and the sharpest of the last few full quality pictures
  is used.
- **Follow and zoom** (pan, tilt and zoom cameras). Turn on *Follow people across the property*
  and the camera turns to keep someone in the middle as they cross your property, so you get one
  clip of them. Turn on *Zoom in on faces* and, when a face is too small or unclear, it points at
  their head and zooms in for a few seconds, then back out (a camera without an optical zoom just
  centres on them). It only moves for people standing on **Our property** (so draw it first),
  never for anyone in the street, a privacy area or an ignore zone. Privacy areas move with the
  picture while it turns, in the live view and the recording; if it can't tell where it's
  pointing, the whole picture is covered and it goes straight back. Afterwards it returns to
  exactly where it was (saved in one of the camera's own position slots). Steering it by hand
  stops it at once. Lines, loitering areas, parcels and cars wait until it's back.
- **Known from behind.** Turn on *Also know them from behind or in the dark* and family are
  also recognised by build, height, clothes and walk (OpenCV Zoo's YoutuReID model, on the PC).
  It learns by itself: each time someone's face is recognised it keeps a picture of their build
  and clothes. A body match only replaces "person seen, face not visible"; a clear face it
  doesn't know is always a stranger. Height needs the camera kept still for a while to learn how
  tall people look at each spot.
- **Visitors.** Turn on *Remember visitors* and people you haven't named are remembered by face,
  or by build, height and clothes when no face was seen. Someone back 3 times in 7 days (you can
  change both) gives a **returning stranger** alert, and the Smart tab shows their visits side
  by side with the clips. Mark someone as a regular (the postman) to stop alerts, say who they
  are to make them family, or forget them. Visitors not seen for 30 days are forgotten.
- **Our property.** Draw your drive, garden and doorstep on each camera. Bodies and visitors are
  only learnt from people standing there, never from anyone in a privacy area or ignore zone,
  which keeps neighbours and people on the pavement out of it (UK GDPR).
- **Your cars.** Park your car in view and press *Add car* (add a night picture too). Turn on
  *Alert when an unknown vehicle stops* to hear about vehicles that stop and aren't yours. Cars
  driving past don't count. Cars are told apart by colour, so a car just like yours can fool it.
- **Parcel watch.** Draw the doorstep. After someone visits, a box left there gives a
  *parcel delivered* alert; if it's then taken, *parcel taken* (an alert when nobody's home,
  or always if you choose).
- **Lines.** Draw a line, such as across the drive, and get an alert when someone (or a car)
  crosses it, either way or only in the arrow's direction.
- **Loitering areas.** Draw an area, such as around the car, and get an alert when someone stays
  there longer than the time you pick.

Smart alerts follow home and away like other alerts. Recordings show who and what was seen.
Lines, areas and the doorstep stay in place on the picture, so keep the camera still (patrol off).

## Who's been (people and number plate log)

The **Who's been** tab keeps a log of everyone and every vehicle seen on your property, with the
best picture of each (their face when it was seen, otherwise their build and clothes) and the
recording they're in. Turn it on under *Log settings and privacy* in that tab. It all runs on the
PC: nothing is sent anywhere.

- **Who has been before.** Each person is put with their earlier sightings by face (or build and
  clothes when no face was seen), so you see "Seen 5 times on 4 days", and family by name. Give
  anyone a name (the postman) and you can search for it.
- **Number plates.** When a vehicle stops on your property its plate is read (two small OpenCV
  Zoo models, PP-OCRv3 and CRNN) and checked against UK plate shapes. Search a plate even with a
  character or two wrong, or only part of it. Add your own cars' plates in the Smart tab.
  Vehicles driving past aren't read.
- **Search** by name or plate, by date and camera, or with a photo of someone: the face (or the
  whole person) in it is compared with everyone in the log. Tap anyone to see every time they
  were here and play the recordings one after another. Works the same in the phone app.
- **Privacy (UK GDPR).** Nothing is logged by a camera until you draw *Our property* on it in the
  Smart tab, and nobody in a privacy area is logged. Entries are deleted after 30 days (you can
  choose 7 to 365), and you can delete one entry, everything about one person or plate, or the
  whole log. People need *Recognise faces* on; plates work on their own and don't send alerts.

## Automatic updates

The PC app checks the download page every few hours. A newer HomeCameraSetup.exe is downloaded,
checked against its checksum and installed quietly at night (2 to 5 AM by default, never while a
camera is recording). Settings and recordings are kept. The PC also keeps the newest phone app,
and the phone app fetches it from the PC and installs it over itself (the first time Android asks
you to allow this). Everything is in **Settings > Updates**, including **Check now** and
**Install now**.

The project is public on GitHub, so no key is needed. If it is ever made private, the PC needs a
read-only GitHub key to see the download page: a fine-grained personal access token for this
repository only, with **Contents: Read-only**, pasted into Settings > Updates.

The phone app must be signed with the same key in every build, or Android won't install an update
over it. The build reads that key from the `ANDROID_KEYSTORE_B64` Actions secret (the keystore file,
base64-encoded, password `homecamera`, alias `homecamera`). Without the secret the phone app still
builds, but can't update itself. After the secret is added, uninstall the old phone app once and
install the new one; updates are automatic from then on.

## 3. Watching from outside the house

Use **Settings > Away from home**. The phone app then connects straight to the PC, with no outside
company, cloud service, VPN app or other extra software in between.

1. On the PC (or the phone, at home), tick **Allow away-from-home access** and press **Save**.
2. The PC asks your router to open the port (UPnP). If your router doesn't do that, the card shows
   the port-forwarding rule to add by hand: TCP port 8443 to the PC.
3. On each phone, while it's on your home Wi-Fi, open the app, go to **Settings > Away from home**
   and press **Set up this phone for away from home**.

From then on the phone uses the PC's home address when it's home and your home's internet address
when it's out, switching by itself.

How it is kept safe:
- Away access uses its own port, which is **encrypted only** (TLS 1.2 or 1.3). It uses a certificate
  this PC makes for itself. When a phone is set up it is given the certificate's fingerprint, and it
  accepts nothing else, so nobody can pretend to be your PC, even on public Wi-Fi.
- Each phone gets **its own key** (only a hash is kept on the PC). Remove a lost phone in Settings and
  it can't connect any more. You're told on your phones whenever a new phone is set up.
- The away port **never shows the password page**. Without a phone's key every request gets a plain
  "not found", and an address that keeps trying is blocked for an hour. Settings for away access
  can only be changed at home.
- The home's internet address is read from your router, so no website is asked. If your provider
  changes it, phones pick up the new one the next time they're home. To avoid that, ask your provider
  for a fixed address, or enter a name your router keeps up to date under **Your own address**.
- Some providers share one internet address between customers (carrier-grade NAT). Then nothing from
  outside can reach your router, and the card says so. Ask your provider for a public IPv4 address.

The older way still works for a web browser: a port forward to port 8080 with your own certificate
in `server.https_cert` and `server.https_key`.

## Troubleshooting

- **Logs:** `C:\ProgramData\HomeCamera\logs\camhub.log` (Start menu > A.S.D Home Security > Settings and logs folder)
- **Camera not found, or its address changed:** Settings > Cameras in the viewer, then **Find and save**.
- **Change the password:** Start menu > A.S.D Home Security > Change password. This signs out every
  device, including the phone.
- **No pan and tilt buttons:** the camera didn't answer ONVIF on the given port. Check the
  `onvif` section and that ONVIF is on in the camera's settings.

## For developers

The logo files are in `branding/` (`logo-mark.svg` is the app icon, `logo-horizontal.svg` the
logo with the name). The installer icon and side pictures in `pc/installer` were made from them.


- Installer: `pc/installer` (PyInstaller recipe and Inno Setup script), built and test-installed
  on Windows by GitHub Actions.
- Recorder: Python 3.10+, FastAPI, OpenCV, FFmpeg. `pc/camhub/camera.py` holds the recording logic.
  Run tests with `python -m pytest pc/tests`.
- Viewer: plain HTML and JavaScript in `pc/camhub/static`, served by the recorder.
- Android: Kotlin, a WebView for the viewer plus a foreground service for alerts. Build with
  `cd android && ./gradlew assembleRelease`.
