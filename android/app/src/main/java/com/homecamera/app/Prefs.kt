package com.homecamera.app

import android.content.Context

/** Where the recorder is and the sign-in token for notifications. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("home_camera", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = sp.getString("server_url", "") ?: ""
        set(v) = sp.edit().putString("server_url", v.trim().trimEnd('/')).apply()

    var token: String
        get() = sp.getString("token", "") ?: ""
        set(v) = sp.edit().putString("token", v).apply()

    var lastEventId: Long
        get() = sp.getLong("last_event", 0)
        set(v) = sp.edit().putLong("last_event", v).apply()

    var askedBattery: Boolean
        get() = sp.getBoolean("asked_battery", false)
        set(v) = sp.edit().putBoolean("asked_battery", v).apply()

    // ---- home and away -----------------------------------------------------------

    /** A random id for this phone, so the PC can tell several phones apart. */
    val phoneId: String
        get() {
            sp.getString("phone_id", null)?.let { return it }
            val id = java.util.UUID.randomUUID().toString()
            sp.edit().putString("phone_id", id).apply()
            return id
        }

    /** This phone tells the PC when it arrives home or leaves. */
    var locationOn: Boolean
        get() = sp.getBoolean("location_on", false)
        set(v) = sp.edit().putBoolean("location_on", v).apply()

    var homeLat: Double
        get() = java.lang.Double.longBitsToDouble(sp.getLong("home_lat", 0))
        set(v) = sp.edit().putLong("home_lat", java.lang.Double.doubleToRawLongBits(v)).apply()

    var homeLon: Double
        get() = java.lang.Double.longBitsToDouble(sp.getLong("home_lon", 0))
        set(v) = sp.edit().putLong("home_lon", java.lang.Double.doubleToRawLongBits(v)).apply()

    var homeSet: Boolean
        get() = sp.getBoolean("home_set", false)
        set(v) = sp.edit().putBoolean("home_set", v).apply()

    var homeRadius: Int
        get() = sp.getInt("home_radius", 150)
        set(v) = sp.edit().putInt("home_radius", v).apply()

    /** Last known: 1 at home, 0 out, -1 not known yet. */
    var atHome: Int
        get() = sp.getInt("at_home", -1)
        set(v) = sp.edit().putInt("at_home", v).apply()

    /** The PC hasn't been told the latest home/out yet. */
    var presencePending: Boolean
        get() = sp.getBoolean("presence_pending", false)
        set(v) = sp.edit().putBoolean("presence_pending", v).apply()

    var presenceSentAt: Long
        get() = sp.getLong("presence_sent_at", 0)
        set(v) = sp.edit().putLong("presence_sent_at", v).apply()

    /** What the PC last said about home/away, shown in the ongoing notification. */
    var presenceText: String
        get() = sp.getString("presence_text", "") ?: ""
        set(v) = sp.edit().putString("presence_text", v).apply()

    companion object {
        /** Adds http:// when missing, removes a trailing slash. */
        fun normalise(input: String): String {
            var s = input.trim().trimEnd('/')
            if (s.isNotEmpty() && !s.startsWith("http://") && !s.startsWith("https://")) s = "http://$s"
            return s
        }
    }
}
