package com.homecamera.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import android.util.Log
import androidx.core.content.ContextCompat
import com.google.android.gms.common.ConnectionResult
import com.google.android.gms.common.GoogleApiAvailability
import com.google.android.gms.location.Geofence
import com.google.android.gms.location.GeofencingEvent
import com.google.android.gms.location.GeofencingRequest
import com.google.android.gms.location.LocationServices
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * Home and away: a circle around the house. When the phone enters or leaves it, the PC is told
 * "home" or "out" (never where the phone is), and it holds back motion alerts while anyone is home.
 */
object HomePresence {
    private const val TAG = "HomePresence"
    private const val FENCE_ID = "home"
    /** The PC stops counting a phone it hasn't heard from in 12 hours, so check in more often. */
    private const val CHECK_IN_MS = 3 * 3600_000L

    fun available(context: Context): Boolean = try {
        GoogleApiAvailability.getInstance().isGooglePlayServicesAvailable(context) == ConnectionResult.SUCCESS
    } catch (_: Throwable) {
        false
    }

    /** "none", "foreground" (only while the app is open) or "always". */
    fun permission(context: Context): String {
        val fine = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED
        if (!fine) return "none"
        if (Build.VERSION.SDK_INT < 29) return "always"
        val bg = ContextCompat.checkSelfPermission(context, Manifest.permission.ACCESS_BACKGROUND_LOCATION) ==
            PackageManager.PERMISSION_GRANTED
        return if (bg) "always" else "foreground"
    }

    fun phoneName(context: Context): String {
        val n = try {
            Settings.Global.getString(context.contentResolver, Settings.Global.DEVICE_NAME)
        } catch (_: Exception) {
            null
        }
        return (n?.takeIf { it.isNotBlank() } ?: Build.MODEL ?: "Phone").take(40)
    }

    /** For the Settings page: what this phone is doing. */
    fun info(context: Context): JSONObject {
        val p = Prefs(context)
        return JSONObject()
            .put("available", available(context))
            .put("on", p.locationOn)
            .put("homeSet", p.homeSet)
            .put("atHome", if (p.atHome < 0) JSONObject.NULL else p.atHome == 1)
            .put("permission", permission(context))
            .put("radius", p.homeRadius)
            .put("phoneId", p.phoneId)
            .put("name", phoneName(context))
    }

    private fun fenceIntent(context: Context): PendingIntent {
        val i = Intent(context, GeofenceReceiver::class.java)
        // Must be mutable: Google Play services adds the details of what happened.
        val flags = PendingIntent.FLAG_UPDATE_CURRENT or
            (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
        return PendingIntent.getBroadcast(context, 7, i, flags)
    }

    /** Draws (or redraws) the circle around home. Returns false if location isn't allowed. */
    @SuppressLint("MissingPermission")
    fun register(context: Context, done: ((Boolean, String?) -> Unit)? = null) {
        val p = Prefs(context)
        if (!p.locationOn || !p.homeSet) {
            done?.invoke(false, "Home isn't set")
            return
        }
        if (permission(context) != "always") {
            done?.invoke(false, "Location isn't allowed all the time")
            return
        }
        val fence = Geofence.Builder()
            .setRequestId(FENCE_ID)
            .setCircularRegion(p.homeLat, p.homeLon, p.homeRadius.toFloat())
            .setExpirationDuration(Geofence.NEVER_EXPIRE)
            .setTransitionTypes(Geofence.GEOFENCE_TRANSITION_ENTER or Geofence.GEOFENCE_TRANSITION_EXIT)
            // Wait a little before saying "left", so a walk to the end of the road doesn't count.
            .setNotificationResponsiveness(60_000)
            .build()
        val request = GeofencingRequest.Builder()
            // Report straight away whether the phone is inside or outside now.
            .setInitialTrigger(GeofencingRequest.INITIAL_TRIGGER_ENTER or GeofencingRequest.INITIAL_TRIGGER_EXIT)
            .addGeofence(fence)
            .build()
        try {
            val client = LocationServices.getGeofencingClient(context)
            client.removeGeofences(listOf(FENCE_ID)).addOnCompleteListener {
                client.addGeofences(request, fenceIntent(context))
                    .addOnSuccessListener { done?.invoke(true, null) }
                    .addOnFailureListener { e ->
                        Log.w(TAG, "Couldn't set the home area: ${e.message}")
                        done?.invoke(false, e.message)
                    }
            }
        } catch (e: Exception) {
            Log.w(TAG, "Couldn't set the home area: ${e.message}")
            done?.invoke(false, e.message)
        }
    }

    fun turnOff(context: Context) {
        val p = Prefs(context)
        p.locationOn = false
        p.homeSet = false
        p.atHome = -1
        p.presencePending = false
        try {
            LocationServices.getGeofencingClient(context).removeGeofences(listOf(FENCE_ID))
        } catch (_: Exception) {
        }
    }

    /** Remember home/out and tell the PC (in the background; retried later if it fails). */
    fun report(context: Context, home: Boolean, then: (() -> Unit)? = null) {
        val p = Prefs(context)
        p.atHome = if (home) 1 else 0
        p.presencePending = true
        Thread({
            send(context)
            then?.invoke()
        }, "presence").start()
    }

    /** Called by the alert service now and then: resend if the PC missed it, or check in. */
    fun sendIfDue(context: Context) {
        val p = Prefs(context)
        if (!p.locationOn || p.atHome < 0) return
        if (p.presencePending || System.currentTimeMillis() - p.presenceSentAt > CHECK_IN_MS) {
            Thread({ send(context) }, "presence").start()
        }
    }

    @Synchronized
    private fun send(context: Context): Boolean {
        val p = Prefs(context)
        if (p.serverUrl.isEmpty() || p.token.isEmpty() || p.atHome < 0) return false
        return try {
            val conn = URL("${p.serverUrl}/api/presence/report").openConnection() as HttpURLConnection
            conn.requestMethod = "POST"
            conn.doOutput = true
            conn.connectTimeout = 15_000
            conn.readTimeout = 15_000
            conn.setRequestProperty("Authorization", "Bearer ${p.token}")
            conn.setRequestProperty("Content-Type", "application/json")
            val body = JSONObject()
                .put("phone", p.phoneId)
                .put("name", phoneName(context))
                .put("home", p.atHome == 1)
            conn.outputStream.use { it.write(body.toString().toByteArray()) }
            val ok = conn.responseCode == 200
            conn.disconnect()
            if (ok) {
                p.presencePending = false
                p.presenceSentAt = System.currentTimeMillis()
            }
            ok
        } catch (e: Exception) {
            Log.w(TAG, "Couldn't tell the PC: ${e.message}")
            false
        }
    }
}

/** Google Play services calls this when the phone enters or leaves the home area. */
class GeofenceReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val event = GeofencingEvent.fromIntent(intent) ?: return
        if (event.hasError()) return
        val home = when (event.geofenceTransition) {
            Geofence.GEOFENCE_TRANSITION_ENTER -> true
            Geofence.GEOFENCE_TRANSITION_EXIT -> false
            else -> return
        }
        if (!Prefs(context).locationOn) return
        val pending = goAsync()
        HomePresence.report(context, home) { pending.finish() }
    }
}
