package com.homecamera.app

import android.annotation.SuppressLint
import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.http.SslCertificate
import android.util.Log
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.security.KeyStore
import java.security.MessageDigest
import java.security.cert.X509Certificate
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager

/**
 * Away-from-home access, straight to the PC with no outside service.
 *
 * When the phone is set up (at home), the PC gives it a key of its own and the fingerprint of
 * the PC's certificate. From then on the app only talks to the PC over the encrypted port and
 * only accepts that exact certificate, so nobody can pretend to be the PC, even on public Wi-Fi.
 * At home it uses the PC's home address; elsewhere the home's internet address.
 */
object Remote {
    private const val TAG = "Remote"

    enum class Route { PLAIN, HOME, AWAY }

    /** Which address is in use now. Updated by [choose]. */
    @Volatile var route = Route.HOME
    @Volatile private var checkedAt = 0L
    /** Why the last check couldn't reach the PC, or "" if it did. Shown instead of a blank page. */
    @Volatile var problem = ""
    @Volatile private var pin = ""
    private var installed = false

    // ---- the PC's certificate -----------------------------------------------------------------

    fun sha256Hex(der: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(der).joinToString("") { "%02x".format(it) }

    fun matchesPin(der: ByteArray?): Boolean = pin.isNotEmpty() && der != null && sha256Hex(der) == pin

    private fun matchesPin(cert: X509Certificate?): Boolean = cert != null && matchesPin(cert.encoded)

    /** For the WebView: is this the PC's own certificate? */
    fun matchesPin(cert: SslCertificate?): Boolean {
        if (cert == null) return false
        val der = SslCertificate.saveState(cert)?.getByteArray("x509-certificate")
        return matchesPin(der)
    }

    /**
     * Makes every HTTPS connection in the app accept the PC's own certificate (by fingerprint),
     * as well as ordinary certificates that Android trusts. Called whenever Prefs is made.
     */
    @SuppressLint("CustomX509TrustManager", "BadHostnameVerifier")
    @Synchronized
    fun install(prefs: Prefs) {
        pin = prefs.remotePin
        if (installed) return
        installed = true
        try {
            val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm())
            tmf.init(null as KeyStore?)
            val system = tmf.trustManagers.first { it is X509TrustManager } as X509TrustManager
            val pinned = object : X509TrustManager {
                override fun checkClientTrusted(chain: Array<out X509Certificate>, authType: String) =
                    system.checkClientTrusted(chain, authType)

                override fun checkServerTrusted(chain: Array<out X509Certificate>, authType: String) {
                    if (chain.isNotEmpty() && matchesPin(chain[0])) return
                    system.checkServerTrusted(chain, authType)
                }

                override fun getAcceptedIssuers(): Array<X509Certificate> = system.acceptedIssuers
            }
            val ctx = SSLContext.getInstance("TLS")
            ctx.init(null, arrayOf(pinned), null)
            HttpsURLConnection.setDefaultSSLSocketFactory(ctx.socketFactory)
            val normal = HttpsURLConnection.getDefaultHostnameVerifier()
            // The PC's certificate has no name to check: the fingerprint is the check.
            HttpsURLConnection.setDefaultHostnameVerifier(HostnameVerifier { host, session ->
                val leaf = try { session.peerCertificates.firstOrNull() as? X509Certificate } catch (_: Exception) { null }
                matchesPin(leaf) || normal.verify(host, session)
            })
        } catch (e: Exception) {
            Log.w(TAG, "Couldn't set up certificate checking: ${e.message}")
        }
    }

    // ---- pairing ----------------------------------------------------------------------------

    /** Saves what the PC sent from /api/remote/pair. Returns a message for the user. */
    fun pair(prefs: Prefs, json: String): String {
        val j = JSONObject(json)
        val key = j.getString("key")
        val fp = j.getString("fingerprint").lowercase().filter { it in "0123456789abcdef" }
        require(key.length >= 32 && fp.length == 64) { "The PC sent something unexpected." }
        prefs.remoteKey = key
        prefs.remotePin = fp
        prefs.remoteServerId = j.optString("server_id")
        saveAddresses(prefs, j)
        install(prefs)
        checkedAt = 0
        return "This phone is set up. It can now see your cameras away from home."
    }

    fun unpair(prefs: Prefs) {
        prefs.remoteKey = ""
        prefs.remotePin = ""
        prefs.remoteHome = ""
        prefs.remoteAway = ""
        pin = ""
        route = Route.PLAIN
        checkedAt = 0
    }

    private fun saveAddresses(prefs: Prefs, j: JSONObject) {
        j.optString("home_url").takeIf { it.startsWith("https://") }?.let { prefs.remoteHome = it }
        // Empty means the PC doesn't know its internet address yet: keep the last one.
        j.optString("away_url").takeIf { it.startsWith("https://") }?.let { prefs.remoteAway = it }
    }

    // ---- home or away? ----------------------------------------------------------------------

    /** Re-checks which address to use if the last check is old. Network: call off the main thread. */
    fun ensure(prefs: Prefs) {
        if (System.currentTimeMillis() - checkedAt > 120_000) choose(prefs)
    }

    fun forget() {
        checkedAt = 0
    }

    /**
     * Tries the PC's home address, then the away address, and uses the first that answers as
     * this PC (right certificate, right key). Network: call off the main thread.
     */
    @Synchronized
    fun choose(prefs: Prefs): Route {
        if (!prefs.paired) {
            route = Route.PLAIN
            checkedAt = System.currentTimeMillis()
            return route
        }
        // The home address can only work on Wi-Fi (or a cable), so on mobile data go straight to away.
        val local = onLocalNetwork(prefs.appContext)
        for ((r, base, timeout) in listOf(
            Triple(Route.HOME, prefs.remoteHome, 2_500),
            Triple(Route.AWAY, prefs.remoteAway, 8_000),
        )) {
            if (base.isEmpty() || (r == Route.HOME && !local)) continue
            when (hello(prefs, base, timeout)) {
                200 -> {
                    route = r
                    problem = ""
                    checkedAt = System.currentTimeMillis()
                    return r
                }
                // The right PC (its certificate checked out) doesn't know this phone any more.
                404 -> {
                    Log.w(TAG, "The PC no longer knows this phone")
                    unpair(prefs)
                    return route
                }
            }
        }
        // Neither answered: keep trying the away address (the PC or the internet may be down).
        route = if (prefs.remoteAway.isNotEmpty() && !local) Route.AWAY
            else if (prefs.remoteHome.isNotEmpty()) Route.HOME else Route.AWAY
        problem = when {
            prefs.remoteAway.isEmpty() && !local ->
                "This phone doesn't know your home's internet address yet, so it can only reach the PC on " +
                    "your home Wi-Fi. On the PC, open Settings > Away from home: if it says the router didn't " +
                    "open the port, turn on UPnP on the router or forward port 8443 to the PC by hand, and " +
                    "if no internet address is shown, type it under Your own address and press Save. Then " +
                    "open this app once on your home Wi-Fi so it picks the address up."
            !local ->
                "Couldn't reach your PC at ${prefs.remoteAway.removePrefix("https://")}. Check the PC is on, and " +
                    "that your router forwards that port to the PC (Settings > Away from home on the PC shows " +
                    "whether the router accepted it). Some providers share one internet address between " +
                    "customers, which blocks this: the same page warns about that."
            else ->
                "Couldn't reach your PC on this Wi-Fi or from outside. Check the PC is on and A.S.D Home " +
                    "Security is running on it."
        }
        checkedAt = System.currentTimeMillis()
        return route
    }

    /** True on Wi-Fi or a cable, where the PC's home address may answer. */
    fun onLocalNetwork(context: Context): Boolean = try {
        val cm = context.getSystemService(ConnectivityManager::class.java)
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        caps == null || caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
    } catch (_: Exception) {
        true
    }

    /** HTTP status of /api/remote/hello, or -1 if it couldn't connect. */
    private fun hello(prefs: Prefs, base: String, timeout: Int): Int = try {
        val conn = URL("$base/api/remote/hello").openConnection() as HttpURLConnection
        conn.connectTimeout = timeout
        conn.readTimeout = timeout
        conn.setRequestProperty("Authorization", "Bearer ${prefs.remoteKey}")
        try {
            val code = conn.responseCode
            // Only believe the answer if it came from the PC's own certificate.
            val leaf = (conn as? HttpsURLConnection)?.serverCertificates?.firstOrNull() as? X509Certificate
            if (!matchesPin(leaf)) {
                -1
            } else if (code == 200) {
                val j = JSONObject(conn.inputStream.bufferedReader().readText())
                val id = j.optString("server_id")
                if (prefs.remoteServerId.isNotEmpty() && id != prefs.remoteServerId) {
                    -1
                } else {
                    saveAddresses(prefs, j)
                    200
                }
            } else {
                code
            }
        } finally {
            conn.disconnect()
        }
    } catch (e: Exception) {
        Log.i(TAG, "No answer from $base: ${e.message}")
        -1
    }

    /** For Settings > Away from home in the app. */
    fun info(context: Context): JSONObject {
        val p = Prefs(context)
        return JSONObject()
            .put("paired", p.paired)
            .put("route", when (route) { Route.AWAY -> "away"; Route.HOME -> "home"; else -> "plain" })
            .put("home", p.remoteHome)
            .put("away", p.remoteAway)
    }
}
