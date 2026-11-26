package com.homecamera.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Keeps a connection open to the recorder's event stream and shows a notification,
 * with a snapshot, whenever a recording starts or a person is seen.
 */
class MonitorService : Service() {

    @Volatile private var running = false
    private var worker: Thread? = null
    @Volatile private var connection: HttpURLConnection? = null
    /** Last time the PC answered, and whether we've warned that it can't be reached. */
    @Volatile private var lastContact = System.currentTimeMillis()
    @Volatile private var warnedUnreachable = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannels(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            stopSelf()
            return START_NOT_STICKY
        }
        val ongoing = ongoingNotification()
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(ID_SERVICE, ongoing, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(ID_SERVICE, ongoing)
        }
        if (!running) {
            running = true
            worker = Thread(::loop, "events").also { it.start() }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        connection?.disconnect()
        worker?.interrupt()
        super.onDestroy()
    }

    private fun ongoingNotification(): Notification {
        val text = Prefs(this).presenceText
        return NotificationCompat.Builder(this, CHANNEL_SERVICE)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle("Watching for alerts")
            .setContentText(text.ifEmpty { "You'll be told when the camera records something" })
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_MIN)
            .setContentIntent(openApp(this, null))
            .build()
    }

    private fun loop() {
        var backoff = 2_000L
        while (running) {
            val prefs = Prefs(this)
            if (prefs.serverUrl.isEmpty() || prefs.token.isEmpty()) {
                stopSelf()
                return
            }
            try {
                val url = URL("${prefs.serverUrl}/api/events?since=${prefs.lastEventId}")
                val conn = (url.openConnection() as HttpURLConnection).apply {
                    setRequestProperty("Authorization", "Bearer ${prefs.token}")
                    setRequestProperty("Accept", "text/event-stream")
                    connectTimeout = 15_000
                    readTimeout = 60_000 // the server sends a keep-alive every 20 s
                }
                connection = conn
                if (conn.responseCode == 401) {
                    showSignedOut()
                    prefs.token = ""
                    stopSelf()
                    return
                }
                if (conn.responseCode != 200) throw IllegalStateException("HTTP ${conn.responseCode}")
                backoff = 2_000L
                reachedPc()
                BufferedReader(InputStreamReader(conn.inputStream)).use { reader ->
                    val data = StringBuilder()
                    while (running) {
                        val line = reader.readLine() ?: break
                        lastContact = System.currentTimeMillis() // includes the keep-alive every 20 s
                        when {
                            line.startsWith("data:") -> data.append(line.substring(5).trim())
                            line.isEmpty() && data.isNotEmpty() -> {
                                handle(JSONObject(data.toString()))
                                data.clear()
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                Log.w(TAG, "Event stream: ${e.message}")
            } finally {
                connection?.disconnect()
                connection = null
            }
            if (!running) return
            checkUnreachable()
            try {
                Thread.sleep(backoff)
            } catch (_: InterruptedException) {
                return
            }
            backoff = (backoff * 2).coerceAtMost(60_000L)
        }
    }

    private fun handle(e: JSONObject) {
        val prefs = Prefs(this)
        val seq = e.optLong("seq", 0)
        // Remember where we got to, so alerts missed while reconnecting are caught up.
        if (seq > 0) prefs.lastEventId = seq
        val type = e.optString("type")
        if (type == "health") {
            handleHealth(e)
            return
        }
        if (type == "presence") {
            prefs.presenceText = presenceLine(e)
            notify(ID_SERVICE, ongoingNotification())
            return
        }
        if (type == "ai_alert") {
            handleAiAlert(prefs, e)
            return
        }
        if (type == "ai_digest") {
            handleDigest(prefs, e)
            return
        }
        if (type == "night_summary") {
            handleNightSummary(prefs, e)
            return
        }
        if (type != "clip_started" && type != "person") return
        // Home and away: the PC says whether to alert (someone's home, so it's held back).
        if (e.has("alert") && !e.optBoolean("alert", true)) return
        val clip = e.optString("clip")
        val cam = e.optString("camera_name", e.optString("camera"))
        val person = type == "person" || e.optBoolean("person")
        val time = try {
            val t = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.UK).parse(e.optString("time"))
            SimpleDateFormat("HH:mm", Locale.UK).format(t ?: Date())
        } catch (_: Exception) {
            SimpleDateFormat("HH:mm", Locale.UK).format(Date())
        }
        val picture = fetchThumb(prefs, clip)
        val n = NotificationCompat.Builder(this, if (person) CHANNEL_PERSON else CHANNEL_MOTION)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle(if (person) "$cam: person seen" else "$cam: movement")
            .setContentText("Recording started at $time. Tap to watch.")
            .setAutoCancel(true)
            .setOnlyAlertOnce(false)
            .setWhen(System.currentTimeMillis())
            .setCategory(NotificationCompat.CATEGORY_EVENT)
            .setPriority(if (person) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
            .setContentIntent(openApp(this, clip))
        if (picture != null) {
            n.setLargeIcon(picture)
            n.setStyle(NotificationCompat.BigPictureStyle().bigPicture(picture).bigLargeIcon(null as Bitmap?))
        }
        notify(clip.hashCode(), n.build())
    }

    private fun presenceLine(e: JSONObject): String {
        val where = if (e.optString("where") == "home") "Home" else "Away"
        val alerts = when (e.optString("alerts")) {
            "all" -> "alerts on"
            "people" -> "alerts for people only"
            else -> "motion alerts paused"
        }
        return "$where: $alerts"
    }

    /** Camera offline, disk nearly full, recording failed, and when each is fixed. */
    private fun handleHealth(e: JSONObject) {
        val id = ("health:" + e.optString("key")).hashCode()
        when (e.optString("state")) {
            "cleared" -> {
                NotificationManagerCompat.from(this).cancel(id)
                return
            }
            "problem", "ok" -> {}
            else -> return
        }
        val problem = e.optString("state") == "problem"
        val title = e.optString("title").ifEmpty { "Home Camera" }
        val message = e.optString("message")
        val n = NotificationCompat.Builder(this, CHANNEL_HEALTH)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle(title)
            .setContentText(message)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setAutoCancel(true)
            .setWhen(System.currentTimeMillis())
            .setCategory(if (problem) NotificationCompat.CATEGORY_ERROR else NotificationCompat.CATEGORY_STATUS)
            .setPriority(if (problem) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_LOW)
            .setSilent(!problem)
            .setContentIntent(openApp(this, null))
        notify(id, n.build())
    }

    /** The PC answered: clear any "can't reach" warning and catch up on home/away. */
    private fun reachedPc() {
        lastContact = System.currentTimeMillis()
        HomePresence.sendIfDue(this)
        if (warnedUnreachable) {
            warnedUnreachable = false
            val n = NotificationCompat.Builder(this, CHANNEL_UNREACHABLE)
                .setSmallIcon(R.drawable.ic_stat_camera)
                .setContentTitle("Home Camera PC is back")
                .setContentText("Your phone can reach the recorder again.")
                .setAutoCancel(true)
                .setSilent(true)
                .setTimeoutAfter(6 * 3600_000L)
                .setContentIntent(openApp(this, null))
                .build()
            notify(ID_UNREACHABLE, n)
        }
    }

    /**
     * The PC can't send anything when it's off or has lost its internet, so the phone warns by
     * itself. Only when the phone itself is online, so a flat signal doesn't set it off.
     */
    private fun checkUnreachable() {
        if (warnedUnreachable) return
        if (System.currentTimeMillis() - lastContact < UNREACHABLE_MS) return
        if (!phoneOnline()) return
        warnedUnreachable = true
        val mins = (System.currentTimeMillis() - lastContact) / 60_000
        val text = "Your phone hasn't been able to reach Home Camera for $mins minutes, so recordings " +
            "and alerts may have stopped. The PC may be off, asleep or without internet, or there may " +
            "have been a power cut."
        val n = NotificationCompat.Builder(this, CHANNEL_UNREACHABLE)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle("Can't reach Home Camera")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setCategory(NotificationCompat.CATEGORY_ERROR)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(openApp(this, null))
            .build()
        notify(ID_UNREACHABLE, n)
    }

    private fun phoneOnline(): Boolean = try {
        val cm = getSystemService(ConnectivityManager::class.java)
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        caps != null && caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
    } catch (_: Exception) {
        false
    }

    /** An AI agent on the PC decided this is worth telling the owner about. */
    private fun handleAiAlert(prefs: Prefs, e: JSONObject) {
        val cam = e.optString("camera_name", e.optString("camera"))
        val agent = e.optString("agent_name", "AI agent")
        val message = e.optString("message").ifEmpty { e.optString("summary") }
        val clip = e.optString("clip")
        val run = e.optString("run")
        val picture = if (run.isNotEmpty()) fetchPicture(prefs, "/api/ai/runs/$run/picture.jpg") else null
        val n = NotificationCompat.Builder(this, CHANNEL_AI)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle("$cam: $message")
            .setContentText(e.optString("summary").ifEmpty { agent })
            .setSubText(agent)
            .setAutoCancel(true)
            .setWhen(System.currentTimeMillis())
            .setCategory(NotificationCompat.CATEGORY_EVENT)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setContentIntent(openApp(this, clip.ifEmpty { null }))
        if (picture != null) {
            n.setLargeIcon(picture)
            n.setStyle(NotificationCompat.BigPictureStyle().bigPicture(picture).bigLargeIcon(null as Bitmap?)
                .setSummaryText(message))
        } else {
            n.setStyle(NotificationCompat.BigTextStyle().bigText(e.optString("summary").ifEmpty { message }))
        }
        notify(("ai$run").hashCode(), n.build())
    }

    /** The morning summary of last night's recordings. */
    private fun handleDigest(prefs: Prefs, e: JSONObject) {
        val text = e.optString("text")
        val lines = text.split("\n", limit = 2)
        val clip = e.optString("clip")
        val picture = fetchThumb(prefs, clip)
        val n = NotificationCompat.Builder(this, CHANNEL_AI)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle(lines[0])
            .setContentText(if (lines.size > 1) lines[1] else "Tap to see the recordings")
            .setSubText("Morning digest")
            .setAutoCancel(true)
            .setWhen(System.currentTimeMillis())
            .setContentIntent(openApp(this, clip.ifEmpty { null }))
        if (picture != null) n.setLargeIcon(picture)
        n.setStyle(NotificationCompat.BigTextStyle().bigText(text))
        notify(ID_DIGEST, n.build())
    }

    /** Each morning: the night's recordings as one short video. Tap to watch it. */
    private fun handleNightSummary(prefs: Prefs, e: JSONObject) {
        val date = e.optString("date")
        val count = e.optInt("count", 0)
        val picture = if (count > 0) fetchPicture(prefs, "/api/summaries/$date/picture.jpg") else null
        val n = NotificationCompat.Builder(this, CHANNEL_SUMMARY)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle(if (count > 0) "Your night in one short video" else "Quiet night")
            .setContentText(e.optString("text"))
            .setAutoCancel(true)
            .setWhen(System.currentTimeMillis())
            .setContentIntent(openApp(this, "summary=$date"))
        if (picture != null) {
            n.setLargeIcon(picture)
            n.setStyle(NotificationCompat.BigPictureStyle().bigPicture(picture).bigLargeIcon(null as Bitmap?)
                .setSummaryText(e.optString("text")))
        } else {
            n.setStyle(NotificationCompat.BigTextStyle().bigText(e.optString("text")))
        }
        notify(ID_SUMMARY, n.build())
    }

    private fun fetchThumb(prefs: Prefs, clip: String): Bitmap? {
        if (clip.isEmpty()) return null
        return fetchPicture(prefs, "/api/clips/$clip/thumb")
    }

    private fun fetchPicture(prefs: Prefs, path: String): Bitmap? {
        repeat(3) { attempt ->
            try {
                val conn = URL("${prefs.serverUrl}$path").openConnection() as HttpURLConnection
                conn.setRequestProperty("Authorization", "Bearer ${prefs.token}")
                conn.connectTimeout = 10_000
                conn.readTimeout = 15_000
                if (conn.responseCode == 200) return conn.inputStream.use { BitmapFactory.decodeStream(it) }
                conn.disconnect()
            } catch (_: Exception) {
            }
            Thread.sleep(700L * (attempt + 1))
        }
        return null
    }

    private fun showSignedOut() {
        val n = NotificationCompat.Builder(this, CHANNEL_MOTION)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle("Home Camera alerts are off")
            .setContentText("Open the app and sign in again to get alerts.")
            .setAutoCancel(true)
            .setContentIntent(openApp(this, null))
            .build()
        notify(ID_SIGNED_OUT, n)
    }

    private fun notify(id: Int, n: Notification) {
        try {
            NotificationManagerCompat.from(this).notify(id, n)
        } catch (_: SecurityException) {
            // Notifications not allowed by the user.
        }
    }

    companion object {
        private const val TAG = "MonitorService"
        const val CHANNEL_SERVICE = "service"
        const val CHANNEL_PERSON = "person"
        const val CHANNEL_MOTION = "motion"
        const val CHANNEL_HEALTH = "health"
        const val CHANNEL_UNREACHABLE = "unreachable"
        const val ID_UNREACHABLE = 4
        private const val UNREACHABLE_MS = 15 * 60_000L
        const val CHANNEL_AI = "ai"
        const val ID_SERVICE = 1
        const val ID_SIGNED_OUT = 2
        const val ID_DIGEST = 3
        const val ID_SUMMARY = 5
        const val CHANNEL_SUMMARY = "summary"
        const val ACTION_STOP = "stop"
        const val EXTRA_CLIP = "clip"

        fun start(context: Context) {
            ContextCompat.startForegroundService(context, Intent(context, MonitorService::class.java))
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, MonitorService::class.java))
        }

        fun createChannels(context: Context) {
            val nm = context.getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_PERSON, "Person seen", NotificationManager.IMPORTANCE_HIGH)
                    .apply { description = "A person was seen and recording started" }
            )
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_MOTION, "Movement", NotificationManager.IMPORTANCE_DEFAULT)
                    .apply { description = "Movement was seen and recording started" }
            )
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_HEALTH, "Camera and PC problems", NotificationManager.IMPORTANCE_HIGH)
                    .apply { description = "A camera is offline, the disk is nearly full, or a recording failed" }
            )
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_UNREACHABLE, "Can't reach the PC", NotificationManager.IMPORTANCE_HIGH)
                    .apply { description = "Your phone hasn't been able to reach the Home Camera PC for a while" }
            )
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_AI, "AI agent alerts", NotificationManager.IMPORTANCE_HIGH)
                    .apply { description = "One of your AI agents spotted something you asked to hear about" }
            )
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_SUMMARY, "Night summary", NotificationManager.IMPORTANCE_DEFAULT)
                    .apply { description = "Each morning, the night's recordings as one short video" }
            )
            nm.createNotificationChannel(
                NotificationChannel(CHANNEL_SERVICE, "Background connection", NotificationManager.IMPORTANCE_MIN)
                    .apply { description = "Shown while the app listens for alerts"; setShowBadge(false) }
            )
        }

        fun openApp(context: Context, clip: String?): PendingIntent {
            val i = Intent(context, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            if (!clip.isNullOrEmpty()) i.putExtra(EXTRA_CLIP, clip)
            return PendingIntent.getActivity(
                context, clip?.hashCode() ?: 0, i,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
        }
    }
}
