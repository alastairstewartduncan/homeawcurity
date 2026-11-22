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

    companion object {
        /** Adds http:// when missing, removes a trailing slash. */
        fun normalise(input: String): String {
            var s = input.trim().trimEnd('/')
            if (s.isNotEmpty() && !s.startsWith("http://") && !s.startsWith("https://")) s = "http://$s"
            return s
        }
    }
}
