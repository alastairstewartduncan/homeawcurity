package com.homecamera.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * Keeps the phone app up to date. The PC downloads each new HomeCamera.apk from GitHub; the
 * phone asks the PC for it (it already signs in there), checks it, and installs it over itself.
 * Settings, sign-in and the home area are kept, as it's the same app.
 *
 * The first update asks "Do you want to install this update?". From Android 12 on, later ones
 * install by themselves.
 */
object Updater {
    private const val TAG = "Updater"
    private const val CHANNEL = "updates"
    private const val ID_UPDATE = 10
    private const val CHECK_EVERY_MS = 6 * 3600_000L
    const val EXTRA_INSTALL = "install_update"

    data class Info(val version: String, val code: Long, val sha256: String, val size: Long)

    @Volatile private var busy = false

    /** Set while the app is on screen, so an install can ask straight away. */
    @Volatile var activityVisible = false

    fun canInstall(context: Context): Boolean = context.packageManager.canRequestPackageInstalls()

    /** What the PC offers, or null if it has nothing newer than this app. Network: call off the main thread. */
    fun newerOnPc(prefs: Prefs): Info? {
        if (prefs.serverUrl.isEmpty() || prefs.token.isEmpty()) return null
        val conn = open(prefs, "/api/updates/android")
        try {
            if (conn.responseCode == 404) return null
            if (conn.responseCode != 200) throw IllegalStateException("The PC answered ${conn.responseCode}")
            val j = JSONObject(conn.inputStream.bufferedReader().readText())
            val info = Info(j.getString("version"), j.getLong("code"), j.getString("sha256"), j.optLong("size"))
            return if (info.code > BuildConfig.VERSION_CODE) info else null
        } finally {
            conn.disconnect()
        }
    }

    private fun open(prefs: Prefs, path: String): HttpURLConnection {
        if (prefs.paired) Remote.ensure(prefs)
        return (URL(prefs.serverUrl + path).openConnection() as HttpURLConnection).apply {
            setRequestProperty("Authorization", "Bearer ${prefs.token}")
            connectTimeout = 15_000
            readTimeout = 60_000
        }
    }

    private fun download(context: Context, prefs: Prefs, info: Info): File {
        val dir = File(context.cacheDir, "updates").apply { mkdirs() }
        val file = File(dir, "HomeCamera.apk")
        val conn = open(prefs, "/api/updates/android/HomeCamera.apk")
        try {
            if (conn.responseCode != 200) throw IllegalStateException("The PC answered ${conn.responseCode}")
            val md = MessageDigest.getInstance("SHA-256")
            conn.inputStream.use { input ->
                file.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        md.update(buf, 0, n)
                        out.write(buf, 0, n)
                    }
                }
            }
            val got = md.digest().joinToString("") { "%02x".format(it) }
            if (!got.equals(info.sha256, ignoreCase = true)) {
                file.delete()
                throw IllegalStateException("The download was damaged, so it wasn't used")
            }
            return file
        } finally {
            conn.disconnect()
        }
    }

    /** Hands the app file to Android to install over this copy. */
    private fun install(context: Context, apk: File) {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
            if (Build.VERSION.SDK_INT >= 31) {
                setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
            }
        }
        val id = installer.createSession(params)
        installer.openSession(id).use { session ->
            session.openWrite("HomeCamera.apk", 0, apk.length()).use { out ->
                apk.inputStream().use { it.copyTo(out) }
                session.fsync(out)
            }
            val result = PendingIntent.getBroadcast(
                context, id, Intent(context, InstallReceiver::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
            )
            session.commit(result.intentSender)
        }
    }

    /**
     * Checks the PC and installs a newer app. [report] gets a short message for the person
     * (called on a background thread). [manual] means the person pressed the button.
     */
    fun checkAndInstall(context: Context, manual: Boolean, report: (ok: Boolean, message: String) -> Unit) {
        val app = context.applicationContext
        if (busy) {
            report(true, "Already checking for an update.")
            return
        }
        busy = true
        Thread({
            val prefs = Prefs(app)
            try {
                prefs.updateCheckedAt = System.currentTimeMillis()
                val info = newerOnPc(prefs)
                if (info == null) {
                    report(true, "The phone app is up to date (version ${BuildConfig.VERSION_NAME}).")
                    return@Thread
                }
                if (!canInstall(app)) {
                    report(false, "Version ${info.version} is ready. First allow A.S.D Home Security to install updates.")
                    if (!manual) notifyReady(app, info, needsPermission = true)
                    return@Thread
                }
                val apk = download(app, prefs, info)
                prefs.updateProblem = ""
                report(true, "Installing version ${info.version}. The app will close and open again by itself.")
                install(app, apk)
            } catch (e: Exception) {
                Log.w(TAG, "Update", e)
                report(false, "Couldn't update the phone app: ${e.message}")
            } finally {
                busy = false
            }
        }, "update").start()
    }

    /** From the alert service: look for an update now and then, and install it quietly. */
    fun backgroundCheck(context: Context) {
        val prefs = Prefs(context)
        if (System.currentTimeMillis() - prefs.updateCheckedAt < CHECK_EVERY_MS) return
        // Don't close the app under the person while they're using it; it asks instead.
        if (activityVisible) return
        checkAndInstall(context, manual = false) { _, _ -> }
    }

    fun notifyReady(context: Context, info: Info?, needsPermission: Boolean, problem: String? = null) {
        val nm = context.getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL, "App updates", NotificationManager.IMPORTANCE_DEFAULT)
                .apply { description = "A new version of A.S.D Home Security is ready to install" }
        )
        val open = Intent(context, MainActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
            .putExtra(EXTRA_INSTALL, true)
        val pi = PendingIntent.getActivity(context, 77, open,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val text = problem ?: if (needsPermission) {
            "Tap to allow A.S.D Home Security to install its own updates. You only need to do this once."
        } else {
            "Tap to install it."
        }
        val n = NotificationCompat.Builder(context, CHANNEL)
            .setSmallIcon(R.drawable.ic_stat_camera)
            .setContentTitle(if (problem != null) "A.S.D Home Security couldn't update" else
                "A.S.D Home Security ${info?.version ?: "update"} is ready")
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setAutoCancel(true)
            .setContentIntent(pi)
            .build()
        try {
            NotificationManagerCompat.from(context).notify(ID_UPDATE, n)
        } catch (_: SecurityException) {
        }
    }

    /** Hears back from Android about an install. */
    class InstallReceiver : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
            when (status) {
                PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                    @Suppress("DEPRECATION")
                    val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT) ?: return
                    if (activityVisible) {
                        context.startActivity(confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    } else {
                        notifyReady(context, null, needsPermission = false)
                    }
                }
                PackageInstaller.STATUS_SUCCESS -> Unit
                PackageInstaller.STATUS_FAILURE_ABORTED -> Unit   // "Cancel" pressed
                else -> {
                    val conflict = status == PackageInstaller.STATUS_FAILURE_CONFLICT ||
                        status == PackageInstaller.STATUS_FAILURE_INCOMPATIBLE
                    val problem = if (conflict) {
                        "This copy of the app was made before automatic updates. Uninstall A.S.D Home Security " +
                            "from the phone, then install the newest HomeCamera.apk from the download page " +
                            "once. Updates are automatic after that."
                    } else {
                        "Android said: " + (intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE) ?: "status $status")
                    }
                    Prefs(context).updateProblem = problem
                    notifyReady(context, null, needsPermission = false, problem = problem)
                }
            }
        }
    }
}
