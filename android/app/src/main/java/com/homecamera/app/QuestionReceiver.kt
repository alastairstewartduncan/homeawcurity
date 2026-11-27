package com.homecamera.app

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * The Yes and No buttons on an "Is this Sarah?" notification. The answer goes straight to the
 * PC, which learns from it, and the notification is replaced by a short "Thanks".
 */
class QuestionReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ACTION_ANSWER) return
        val question = intent.getStringExtra(EXTRA_QUESTION) ?: return
        val answer = intent.getStringExtra(EXTRA_ANSWER) ?: return
        val id = intent.getIntExtra(EXTRA_NOTIFICATION, 0)
        val app = context.applicationContext
        val pending = goAsync()
        Thread({
            try {
                val said = send(app, question, answer)
                done(app, id, said)
            } catch (e: Exception) {
                Log.w(TAG, "Couldn't send the answer", e)
                done(app, id, "Couldn't reach the PC, so the answer wasn't saved. Open the app's Smart tab to answer there.")
            } finally {
                pending.finish()
            }
        }, "answer").start()
    }

    private fun send(context: Context, question: String, answer: String): String {
        val prefs = Prefs(context)
        if (prefs.paired) Remote.ensure(prefs)
        val conn = URL("${prefs.serverUrl}/api/smart/questions/$question").openConnection() as HttpURLConnection
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = 8_000
            conn.readTimeout = 15_000
            conn.setRequestProperty("Authorization", "Bearer ${prefs.token}")
            conn.setRequestProperty("Content-Type", "application/json")
            conn.doOutput = true
            conn.outputStream.use { it.write(JSONObject().put("answer", answer).toString().toByteArray()) }
            if (conn.responseCode == 404) return "That was already answered."
            if (conn.responseCode != 200) throw IllegalStateException("HTTP ${conn.responseCode}")
            val body = conn.inputStream.bufferedReader().use { it.readText() }
            return JSONObject(body).optString("said", "Thanks! It will remember.")
        } finally {
            conn.disconnect()
        }
    }

    private fun done(context: Context, id: Int, text: String) {
        val n = NotificationCompat.Builder(context, MonitorService.CHANNEL_SMART)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle("A.S.D Home Security")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setSilent(true)
            .setTimeoutAfter(15_000)
            .setContentIntent(MonitorService.openApp(context, "smart=questions"))
            .build()
        try {
            NotificationManagerCompat.from(context).notify(id, n)
        } catch (_: SecurityException) {
        }
    }

    companion object {
        private const val TAG = "QuestionReceiver"
        const val ACTION_ANSWER = "com.homecamera.app.ANSWER"
        const val EXTRA_QUESTION = "question"
        const val EXTRA_ANSWER = "answer"
        const val EXTRA_NOTIFICATION = "notification"

        fun answer(context: Context, question: String, answer: String, notification: Int): PendingIntent =
            PendingIntent.getBroadcast(
                context, (question + answer).hashCode(),
                Intent(context, QuestionReceiver::class.java)
                    .setAction(ACTION_ANSWER)
                    .putExtra(EXTRA_QUESTION, question)
                    .putExtra(EXTRA_ANSWER, answer)
                    .putExtra(EXTRA_NOTIFICATION, notification),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
    }
}
