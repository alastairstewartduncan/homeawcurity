package com.homecamera.app

import android.content.Context
import android.util.Log
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.mediarouter.app.MediaRouteChooserDialog
import androidx.mediarouter.media.MediaRouteSelector
import com.google.android.gms.cast.CastMediaControlIntent
import com.google.android.gms.cast.MediaInfo
import com.google.android.gms.cast.MediaLoadRequestData
import com.google.android.gms.cast.MediaMetadata
import com.google.android.gms.cast.framework.CastContext
import com.google.android.gms.cast.framework.CastOptions
import com.google.android.gms.cast.framework.CastSession
import com.google.android.gms.cast.framework.OptionsProvider
import com.google.android.gms.cast.framework.SessionProvider
import com.google.android.gms.cast.framework.SessionManagerListener

/** Tells the Cast library to use Google's standard player on the TV (no app of our own needed). */
class CastOptionsProvider : OptionsProvider {
    override fun getCastOptions(context: Context): CastOptions =
        CastOptions.Builder()
            .setReceiverApplicationId(CastMediaControlIntent.DEFAULT_MEDIA_RECEIVER_APPLICATION_ID)
            .build()

    override fun getAdditionalSessionProviders(context: Context): List<SessionProvider>? = null
}

/**
 * Shows the cameras (or a night summary) on a Chromecast or Google TV.
 *
 * The PC gives the page a link with a key in it, because the TV can't sign in; the page passes
 * that link here. The TV then fetches the video straight from the PC over the home network.
 */
class TvCast(private val activity: AppCompatActivity) {

    private data class Media(val url: String, val title: String, val live: Boolean)

    private var pending: Media? = null

    private val cast: CastContext? = try {
        CastContext.getSharedInstance(activity)
    } catch (e: Exception) {
        Log.w(TAG, "Cast isn't available: ${e.message}")
        null
    }

    private val listener = object : SessionManagerListener<CastSession> {
        override fun onSessionStarted(session: CastSession, sessionId: String) = sendPending(session)
        override fun onSessionResumed(session: CastSession, wasSuspended: Boolean) = sendPending(session)
        override fun onSessionStartFailed(session: CastSession, error: Int) {
            if (pending != null) toast("Couldn't connect to the TV. Check it's on and on the same Wi-Fi.")
            pending = null
        }
        override fun onSessionStarting(session: CastSession) {}
        override fun onSessionEnding(session: CastSession) {}
        override fun onSessionEnded(session: CastSession, error: Int) {}
        override fun onSessionResuming(session: CastSession, sessionId: String) {}
        override fun onSessionResumeFailed(session: CastSession, error: Int) {}
        override fun onSessionSuspended(session: CastSession, reason: Int) {}
    }

    init {
        cast?.sessionManager?.addSessionManagerListener(listener, CastSession::class.java)
    }

    fun release() {
        cast?.sessionManager?.removeSessionManagerListener(listener, CastSession::class.java)
    }

    /** Called when the person presses TV (or Play on TV). Asks which TV the first time. */
    fun show(url: String, title: String, live: Boolean) {
        val c = cast
        if (c == null) {
            toast("Casting needs Google Play services on this phone.")
            return
        }
        val media = Media(url, title, live)
        val session = c.sessionManager.currentCastSession
        if (session != null && session.isConnected) {
            val tv = session.castDevice?.friendlyName ?: "the TV"
            AlertDialog.Builder(activity)
                .setTitle("Showing on $tv")
                .setItems(arrayOf("Show \"$title\" instead", "Stop showing on $tv")) { _, which ->
                    if (which == 0) load(session, media) else c.sessionManager.endCurrentSession(true)
                }
                .setNegativeButton("Cancel", null)
                .show()
            return
        }
        pending = media
        val selector = c.mergedSelector ?: MediaRouteSelector.Builder()
            .addControlCategory(CastMediaControlIntent.categoryForCast(CastMediaControlIntent.DEFAULT_MEDIA_RECEIVER_APPLICATION_ID))
            .build()
        MediaRouteChooserDialog(activity).apply {
            routeSelector = selector
            setTitle("Show on which TV?")
            show()
        }
    }

    private fun sendPending(session: CastSession) {
        val m = pending ?: return
        pending = null
        load(session, m)
    }

    private fun load(session: CastSession, m: Media) {
        val client = session.remoteMediaClient ?: run {
            toast("The TV isn't ready yet. Try again.")
            return
        }
        val meta = MediaMetadata(MediaMetadata.MEDIA_TYPE_GENERIC).apply {
            putString(MediaMetadata.KEY_TITLE, m.title)
            putString(MediaMetadata.KEY_SUBTITLE, "Home Camera")
        }
        val info = MediaInfo.Builder(m.url)
            .setStreamType(if (m.live) MediaInfo.STREAM_TYPE_LIVE else MediaInfo.STREAM_TYPE_BUFFERED)
            .setContentType(if (m.live) "application/x-mpegURL" else "video/mp4")
            .setMetadata(meta)
            .build()
        client.load(MediaLoadRequestData.Builder().setMediaInfo(info).setAutoplay(true).build())
            .setResultCallback { r ->
                if (!r.status.isSuccess) {
                    toast("The TV couldn't play it (${r.status.statusCode}). It needs to reach the PC on your home Wi-Fi.")
                }
            }
        val tv = session.castDevice?.friendlyName ?: "the TV"
        toast("Showing on $tv. ${if (m.live) "It's a few seconds behind live. " else ""}To stop, press the TV button again.")
    }

    private fun toast(text: String) {
        activity.runOnUiThread { Toast.makeText(activity, text, Toast.LENGTH_LONG).show() }
    }

    companion object {
        private const val TAG = "TvCast"
    }
}
