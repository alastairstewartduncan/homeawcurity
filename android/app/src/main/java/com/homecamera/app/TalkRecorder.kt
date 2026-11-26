package com.homecamera.app

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.webkit.CookieManager
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Records the phone's microphone while Hold to talk is pressed and sends it to the recorder,
 * which plays it through the camera's speaker. The web page can't do this itself in the app,
 * because Android only lets secure (https) pages use the microphone.
 *
 * Sends 8 kHz 16-bit mono sound in pieces of a fifth of a second, like the PC page does.
 */
class TalkRecorder(
    private val prefs: Prefs,
    private val camId: String,
    private val onProblem: (String) -> Unit,
) {
    @Volatile private var running = false
    private var thread: Thread? = null

    @SuppressLint("MissingPermission") // checked by MainActivity before starting
    fun start() {
        running = true
        thread = Thread({
            val rate = 8000
            val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            val rec = try {
                AudioRecord(MediaRecorder.AudioSource.VOICE_COMMUNICATION, rate, AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT, maxOf(min, rate))
            } catch (e: Exception) {
                onProblem("The microphone couldn't be opened (${e.message})")
                return@Thread
            }
            if (rec.state != AudioRecord.STATE_INITIALIZED) {
                onProblem("The microphone couldn't be opened")
                rec.release()
                return@Thread
            }
            val piece = ByteArray(rate / 5 * 2)
            var seq = 0
            var reported = false
            try {
                rec.startRecording()
                while (running) {
                    var got = 0
                    while (got < piece.size && running) {
                        val n = rec.read(piece, got, piece.size - got)
                        if (n <= 0) break
                        got += n
                    }
                    if (got <= 0) continue
                    val problem = send(piece.copyOf(got), seq++)
                    if (problem != null && !reported) {
                        reported = true
                        onProblem(problem)
                    }
                }
            } finally {
                try { rec.stop() } catch (_: Exception) {}
                rec.release()
            }
        }, "talk").apply { start() }
    }

    fun stop() {
        running = false
    }

    /** Returns a problem to show, or null when the recorder took the sound. */
    private fun send(data: ByteArray, seq: Int): String? {
        val url = "${prefs.serverUrl}/api/cameras/$camId/talk?seq=$seq"
        return try {
            val conn = (URL(url).openConnection() as HttpURLConnection).apply {
                requestMethod = "POST"
                doOutput = true
                connectTimeout = 5000
                readTimeout = 10000
                setRequestProperty("Content-Type", "application/octet-stream")
                if (prefs.token.isNotEmpty()) setRequestProperty("Authorization", "Bearer ${prefs.token}")
                CookieManager.getInstance().getCookie(prefs.serverUrl)?.let { setRequestProperty("Cookie", it) }
                setFixedLengthStreamingMode(data.size)
            }
            conn.outputStream.use { it.write(data) }
            val code = conn.responseCode
            if (code in 200..299) {
                conn.inputStream.use { it.readBytes() }
                conn.disconnect()
                null
            } else {
                val text = conn.errorStream?.use { String(it.readBytes()) } ?: ""
                conn.disconnect()
                try { JSONObject(text).optString("detail") } catch (_: Exception) { "" }.ifEmpty { "The recorder answered $code" }
            }
        } catch (e: Exception) {
            "Couldn't reach the recorder (${e.message})"
        }
    }
}
