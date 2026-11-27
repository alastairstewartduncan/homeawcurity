package com.homecamera.app

import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.webkit.CookieManager
import android.widget.Toast
import java.io.File
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL

/**
 * Saves a recording from the PC into Downloads. Used instead of Android's download manager
 * when the phone is set up for away access, because the download manager runs outside the
 * app and wouldn't accept the PC's own certificate.
 */
object Downloads {
    fun save(context: Context, prefs: Prefs, url: String, name: String, mime: String?) {
        val app = context.applicationContext
        Thread {
            val message = try {
                val conn = URL(url).openConnection() as HttpURLConnection
                conn.setRequestProperty("Authorization", "Bearer ${prefs.token}")
                CookieManager.getInstance().getCookie(url)?.let { conn.setRequestProperty("Cookie", it) }
                conn.connectTimeout = 15_000
                conn.readTimeout = 60_000
                try {
                    if (conn.responseCode != 200) throw IllegalStateException("the PC answered ${conn.responseCode}")
                    val where = conn.inputStream.use { input -> write(app, name, mime ?: "application/octet-stream") { input.copyTo(it, 64 * 1024) } }
                    "Saved $name to $where"
                } finally {
                    conn.disconnect()
                }
            } catch (e: Exception) {
                "Couldn't download $name: ${e.message}"
            }
            Handler(Looper.getMainLooper()).post { Toast.makeText(app, message, Toast.LENGTH_LONG).show() }
        }.start()
    }

    private fun write(context: Context, name: String, mime: String, copy: (OutputStream) -> Unit): String {
        if (Build.VERSION.SDK_INT >= 29) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, mime)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = context.contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IllegalStateException("no room in Downloads")
            try {
                resolver.openOutputStream(uri)?.use(copy) ?: throw IllegalStateException("couldn't write")
                values.clear()
                values.put(MediaStore.Downloads.IS_PENDING, 0)
                resolver.update(uri, values, null, null)
            } catch (e: Exception) {
                resolver.delete(uri, null, null)
                throw e
            }
            return "Downloads"
        }
        // Android 8 and 9: the app's own Downloads folder, which needs no permission.
        val dir = context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: context.filesDir
        val file = File(dir, name)
        file.outputStream().use(copy)
        return file.absolutePath
    }
}
