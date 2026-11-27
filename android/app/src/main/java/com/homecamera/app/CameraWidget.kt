package com.homecamera.app

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Log
import android.view.View
import android.widget.RemoteViews
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The home screen widget: the camera's latest picture, whether the house is set to home or away
 * (tap to change it), the last alert (tap to watch it), and buttons to refresh or open live view.
 *
 * It talks to the PC the same way as the alert service (same address and key), and only when it
 * refreshes: every 30 minutes, when a button is tapped, and when an alert arrives.
 */
class CameraWidget : AppWidgetProvider() {

    override fun onUpdate(context: Context, manager: AppWidgetManager, ids: IntArray) {
        refresh(context, null)
    }

    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        when (intent.action) {
            ACTION_REFRESH -> refresh(context, null)
            ACTION_MODE -> refresh(context, Change.MODE)
            ACTION_NEXT_CAMERA -> refresh(context, Change.CAMERA)
        }
    }

    private enum class Change { MODE, CAMERA }

    /** Fetches the picture and status in the background, then redraws. */
    private fun refresh(context: Context, change: Change?) {
        val app = context.applicationContext
        val st = state(app)
        st.edit().putBoolean(K_BUSY, true).apply()
        draw(app)
        val pending = goAsync()
        Thread({
            try {
                fetch(app, change)
            } finally {
                st.edit().putBoolean(K_BUSY, false).apply()
                draw(app)
                pending.finish()
            }
        }, "widget").start()
    }

    private fun fetch(context: Context, change: Change?) {
        val prefs = Prefs(context)
        val st = state(context)
        if (prefs.serverUrl.isEmpty() || prefs.token.isEmpty()) {
            st.edit().putString(K_ERROR, "Open the app and sign in to see your camera").apply()
            return
        }
        try {
            if (prefs.paired) Remote.ensure(prefs)
            if (change == Change.MODE) {
                // Auto (from the phones) -> Home -> Away -> back to Auto.
                val next = when (st.getString(K_MODE, "auto")) {
                    "auto" -> "home"
                    "home" -> "away"
                    else -> "auto"
                }
                val body = JSONObject().put("mode", next).toString()
                savePresence(st, JSONObject(request(prefs, "/api/presence", "PUT", body)))
            }
            val status = JSONObject(request(prefs, "/api/status", "GET", null))
            status.optJSONObject("presence")?.let { savePresence(st, it) }
            val cams = status.optJSONArray("cameras")
            if (cams == null || cams.length() == 0) {
                st.edit().putString(K_ERROR, "No cameras set up yet").apply()
                return
            }
            var index = st.getInt(K_CAMERA_INDEX, 0)
            if (change == Change.CAMERA) index++
            index = Math.floorMod(index, cams.length())
            val cam = cams.getJSONObject(index)
            st.edit()
                .putInt(K_CAMERA_INDEX, index)
                .putString(K_CAMERA_NAME, cam.optString("name", cam.optString("id")))
                .putBoolean(K_MANY, cams.length() > 1)
                .apply()
            val picture = fetchBitmap(prefs, "/api/cameras/${cam.optString("id")}/snapshot.jpg")
            if (picture != null) {
                savePicture(context, picture)
                st.edit().putLong(K_PICTURE_AT, System.currentTimeMillis()).putString(K_PICTURE_KIND, "live").apply()
            }
            st.edit().remove(K_ERROR).apply()
        } catch (e: Exception) {
            Log.w(TAG, "Widget refresh: ${e.message}")
            Remote.forget()
            st.edit().putString(K_ERROR, "Can't reach the PC just now").apply()
        }
    }

    companion object {
        private const val TAG = "CameraWidget"
        const val ACTION_REFRESH = "com.homecamera.app.widget.REFRESH"
        const val ACTION_MODE = "com.homecamera.app.widget.MODE"
        const val ACTION_NEXT_CAMERA = "com.homecamera.app.widget.NEXT_CAMERA"

        private const val K_BUSY = "busy"
        private const val K_ERROR = "error"
        private const val K_MODE = "mode"
        private const val K_WHERE = "where"
        private const val K_ALERTS = "alerts"
        private const val K_CAMERA_INDEX = "camera_index"
        private const val K_CAMERA_NAME = "camera_name"
        private const val K_MANY = "many_cameras"
        private const val K_PICTURE_AT = "picture_at"
        private const val K_PICTURE_KIND = "picture_kind"
        private const val K_ALERT_TEXT = "alert_text"
        private const val K_ALERT_AT = "alert_at"
        private const val K_ALERT_CLIP = "alert_clip"
        private const val PICTURE_FILE = "widget_picture.jpg"
        private const val MAX_WIDTH = 720

        private fun state(context: Context) = context.getSharedPreferences("widget", Context.MODE_PRIVATE)

        private fun hasWidgets(context: Context): Boolean = try {
            AppWidgetManager.getInstance(context)
                .getAppWidgetIds(ComponentName(context, CameraWidget::class.java)).isNotEmpty()
        } catch (_: Exception) {
            false
        }

        /** Called by the alert service: show this alert (and its picture) on the widget. */
        fun alert(context: Context, text: String, clip: String?, picture: Bitmap?) {
            val app = context.applicationContext
            val st = state(app)
            st.edit()
                .putString(K_ALERT_TEXT, text)
                .putLong(K_ALERT_AT, System.currentTimeMillis())
                .putString(K_ALERT_CLIP, clip ?: "")
                .apply()
            if (picture != null && hasWidgets(app)) {
                savePicture(app, picture)
                st.edit().putLong(K_PICTURE_AT, System.currentTimeMillis()).putString(K_PICTURE_KIND, "alert").apply()
            }
            draw(app)
        }

        /** Called by the alert service when the PC says home/away changed. */
        fun presence(context: Context, where: String, alerts: String) {
            val app = context.applicationContext
            state(app).edit().putString(K_WHERE, where).putString(K_ALERTS, alerts).apply()
            draw(app)
        }

        private fun savePresence(st: android.content.SharedPreferences, p: JSONObject) {
            st.edit()
                .putString(K_MODE, p.optString("mode", "auto"))
                .putString(K_WHERE, p.optString("where", ""))
                .putString(K_ALERTS, p.optString("alerts", ""))
                .apply()
        }

        private fun request(prefs: Prefs, path: String, method: String, body: String?): String {
            val conn = URL("${prefs.serverUrl}$path").openConnection() as HttpURLConnection
            try {
                conn.requestMethod = method
                conn.connectTimeout = 8_000
                conn.readTimeout = 10_000
                conn.setRequestProperty("Authorization", "Bearer ${prefs.token}")
                if (body != null) {
                    conn.doOutput = true
                    conn.setRequestProperty("Content-Type", "application/json")
                    conn.outputStream.use { it.write(body.toByteArray()) }
                }
                if (conn.responseCode != 200) throw IllegalStateException("HTTP ${conn.responseCode}")
                return conn.inputStream.bufferedReader().use { it.readText() }
            } finally {
                conn.disconnect()
            }
        }

        private fun fetchBitmap(prefs: Prefs, path: String): Bitmap? {
            val conn = URL("${prefs.serverUrl}$path").openConnection() as HttpURLConnection
            return try {
                conn.connectTimeout = 8_000
                conn.readTimeout = 10_000
                conn.setRequestProperty("Authorization", "Bearer ${prefs.token}")
                if (conn.responseCode == 200) conn.inputStream.use { BitmapFactory.decodeStream(it) } else null
            } catch (_: Exception) {
                null
            } finally {
                conn.disconnect()
            }
        }

        /** Keeps one small copy of the picture, so the widget still shows it after a restart. */
        private fun savePicture(context: Context, picture: Bitmap) {
            val scaled = if (picture.width > MAX_WIDTH) {
                Bitmap.createScaledBitmap(picture, MAX_WIDTH, picture.height * MAX_WIDTH / picture.width, true)
            } else {
                picture
            }
            val tmp = File(context.filesDir, "$PICTURE_FILE.tmp")
            FileOutputStream(tmp).use { scaled.compress(Bitmap.CompressFormat.JPEG, 85, it) }
            tmp.renameTo(File(context.filesDir, PICTURE_FILE))
        }

        private fun loadPicture(context: Context): Bitmap? {
            val f = File(context.filesDir, PICTURE_FILE)
            return if (f.exists()) BitmapFactory.decodeFile(f.path) else null
        }

        private fun time(ms: Long): String {
            val today = SimpleDateFormat("yyyyMMdd", Locale.UK)
            val d = Date(ms)
            val clock = SimpleDateFormat("HH:mm", Locale.UK).format(d)
            return if (today.format(d) == today.format(Date())) clock
            else SimpleDateFormat("EEE HH:mm", Locale.UK).format(d)
        }

        private fun broadcast(context: Context, action: String, code: Int): PendingIntent =
            PendingIntent.getBroadcast(
                context, code,
                Intent(context, CameraWidget::class.java).setAction(action),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

        /** Redraws every copy of the widget from what's saved. */
        fun draw(context: Context) {
            val manager = AppWidgetManager.getInstance(context)
            val ids = manager.getAppWidgetIds(ComponentName(context, CameraWidget::class.java))
            if (ids.isEmpty()) return
            val st = state(context)
            val v = RemoteViews(context.packageName, R.layout.widget_camera)

            val picture = loadPicture(context)
            val error = st.getString(K_ERROR, null)
            if (picture != null) {
                v.setImageViewBitmap(R.id.w_picture, picture)
                v.setViewVisibility(R.id.w_empty, View.GONE)
            } else {
                v.setViewVisibility(R.id.w_empty, View.VISIBLE)
                v.setTextViewText(R.id.w_empty, error ?: context.getString(R.string.widget_loading))
            }

            val many = st.getBoolean(K_MANY, false)
            val name = st.getString(K_CAMERA_NAME, null) ?: context.getString(R.string.app_name)
            v.setTextViewText(R.id.w_camera, if (many) "$name  ›" else name)
            val pictureAt = st.getLong(K_PICTURE_AT, 0)
            val sub = when {
                st.getBoolean(K_BUSY, false) -> "Updating…"
                error != null && picture != null -> error
                pictureAt > 0 && st.getString(K_PICTURE_KIND, "") == "alert" -> "Alert picture, ${time(pictureAt)}"
                pictureAt > 0 -> "Picture from ${time(pictureAt)}"
                else -> ""
            }
            v.setTextViewText(R.id.w_when, sub)

            val where = st.getString(K_WHERE, "") ?: ""
            val mode = st.getString(K_MODE, "auto") ?: "auto"
            val label = when (where) {
                "home" -> "Home"
                "away" -> "Away"
                else -> "Home / away"
            } + if (mode == "auto" && where.isNotEmpty()) " · auto" else ""
            v.setTextViewText(R.id.w_mode, label)
            v.setInt(
                R.id.w_mode, "setBackgroundResource",
                when (where) {
                    "home" -> R.drawable.widget_pill_home
                    "away" -> R.drawable.widget_pill_away
                    else -> R.drawable.widget_pill_idle
                }
            )
            v.setContentDescription(R.id.w_mode, "$label. Tap to change between auto, home and away.")

            val alertText = st.getString(K_ALERT_TEXT, "") ?: ""
            val alertAt = st.getLong(K_ALERT_AT, 0)
            v.setTextViewText(
                R.id.w_alert,
                if (alertText.isEmpty()) context.getString(R.string.widget_no_alerts) else "$alertText · ${time(alertAt)}"
            )

            val clip = st.getString(K_ALERT_CLIP, "") ?: ""
            v.setOnClickPendingIntent(R.id.w_picture, MonitorService.openApp(context, null))
            v.setOnClickPendingIntent(R.id.w_empty, MonitorService.openApp(context, null))
            v.setOnClickPendingIntent(R.id.w_open, MonitorService.openApp(context, null))
            v.setOnClickPendingIntent(R.id.w_alert_box, MonitorService.openApp(context, clip.ifEmpty { null }))
            v.setOnClickPendingIntent(R.id.w_refresh, broadcast(context, ACTION_REFRESH, 1))
            v.setOnClickPendingIntent(R.id.w_mode, broadcast(context, ACTION_MODE, 2))
            v.setOnClickPendingIntent(R.id.w_camera_box, broadcast(context, ACTION_NEXT_CAMERA, 3))
            manager.updateAppWidget(ids, v)
        }
    }
}
