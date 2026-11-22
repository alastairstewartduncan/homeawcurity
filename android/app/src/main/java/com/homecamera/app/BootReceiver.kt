package com.homecamera.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Starts the alert service again after the phone restarts or the app is updated. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (Prefs(context).token.isNotEmpty()) MonitorService.start(context)
    }
}
