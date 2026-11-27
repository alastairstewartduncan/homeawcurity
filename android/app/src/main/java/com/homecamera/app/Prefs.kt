package com.homecamera.app

import android.content.Context

/** Where the recorder is and the sign-in token for notifications. */
class Prefs(context: Context) {
    val appContext: Context = context.applicationContext
    private val sp = context.getSharedPreferences("home_camera", Context.MODE_PRIVATE)

    init {
        Remote.install(this)
    }

    /** The address typed in at setup (the PC at home, plain http). */
    var homeAddress: String
        get() = sp.getString("server_url", "") ?: ""
        set(v) = sp.edit().putString("server_url", v.trim().trimEnd('/')).apply()

    /** The sign-in token from the password page at home. */
    var loginToken: String
        get() = sp.getString("token", "") ?: ""
        set(v) = sp.edit().putString("token", v).apply()

    /**
     * Where to reach the PC now. Once the phone is set up for away access this is always the
     * PC's encrypted port: its home address on the home network, the internet address elsewhere.
     */
    var serverUrl: String
        get() = when {
            !paired -> homeAddress
            Remote.route == Remote.Route.AWAY && remoteAway.isNotEmpty() -> remoteAway
            remoteHome.isNotEmpty() -> remoteHome
            else -> remoteAway
        }
        set(v) {
            homeAddress = v
        }

    /** The key sent with each request: this phone's own key once it is set up for away access. */
    var token: String
        get() = if (paired) remoteKey else loginToken
        set(v) {
            loginToken = v
        }

    // ---- away from home (see Remote.kt) -------------------------------------------------------

    val paired: Boolean
        get() = remoteKey.isNotEmpty() && remotePin.isNotEmpty()

    /** This phone's own key, given by the PC when it was set up. */
    var remoteKey: String
        get() = sp.getString("remote_key", "") ?: ""
        set(v) = sp.edit().putString("remote_key", v).apply()

    /** SHA-256 of the PC's certificate, as hex. Nothing else is accepted on the encrypted port. */
    var remotePin: String
        get() = sp.getString("remote_pin", "") ?: ""
        set(v) = sp.edit().putString("remote_pin", v).apply()

    var remoteServerId: String
        get() = sp.getString("remote_server_id", "") ?: ""
        set(v) = sp.edit().putString("remote_server_id", v).apply()

    /** https://<PC's home address>:<port> */
    var remoteHome: String
        get() = sp.getString("remote_home", "") ?: ""
        set(v) = sp.edit().putString("remote_home", v.trimEnd('/')).apply()

    /** https://<home's internet address>:<port> */
    var remoteAway: String
        get() = sp.getString("remote_away", "") ?: ""
        set(v) = sp.edit().putString("remote_away", v.trimEnd('/')).apply()

    var lastEventId: Long
        get() = sp.getLong("last_event", 0)
        set(v) = sp.edit().putLong("last_event", v).apply()

    var askedBattery: Boolean
        get() = sp.getBoolean("asked_battery", false)
        set(v) = sp.edit().putBoolean("asked_battery", v).apply()

    // ---- app updates -------------------------------------------------------------

    var updateCheckedAt: Long
        get() = sp.getLong("update_checked_at", 0)
        set(v) = sp.edit().putLong("update_checked_at", v).apply()

    /** Why the last update didn't install, shown in Settings > Updates. */
    var updateProblem: String
        get() = sp.getString("update_problem", "") ?: ""
        set(v) = sp.edit().putString("update_problem", v).apply()

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
