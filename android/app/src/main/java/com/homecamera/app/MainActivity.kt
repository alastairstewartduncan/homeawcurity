package com.homecamera.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.SslErrorHandler
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import com.google.android.gms.location.LocationServices
import com.google.android.gms.location.Priority
import com.google.android.gms.tasks.CancellationTokenSource
import org.json.JSONObject

/**
 * Shows the recorder's pages (live view, recordings, settings) in a WebView, and adds
 * what a web page can't do on its own: alerts in the background, downloads and full screen.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var prefs: Prefs
    private lateinit var root: FrameLayout
    private var web: WebView? = null
    private var fullView: View? = null
    private var fullCallback: WebChromeClient.CustomViewCallback? = null
    private var locationRadius = 150
    private var talk: TalkRecorder? = null
    /** Showing "couldn't reach the PC": try again by itself when the phone's network changes. */
    private var waitingForNetwork = false
    private var lastNetwork: android.net.Network? = null
    private val networkWatch = object : android.net.ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: android.net.Network) {
            runOnUiThread {
                if (network == lastNetwork) return@runOnUiThread
                lastNetwork = network
                if (waitingForNetwork && prefs.serverUrl.isNotEmpty() && awayAddressProblem(prefs.homeAddress) == null) {
                    Remote.forget()
                    showWeb(null)
                }
            }
        }
    }
    private var tv: TvCast? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        root = FrameLayout(this).apply { setBackgroundColor(Color.parseColor("#0B1020")) }
        setContentView(root)
        WindowCompat.setDecorFitsSystemWindows(window, true)
        // Android 15 draws apps under the status bar and the gesture bar whatever we ask, so keep
        // the pages clear of them ourselves (otherwise the bottom tabs sit under the gesture bar).
        ViewCompat.setOnApplyWindowInsetsListener(root) { v, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout() or WindowInsetsCompat.Type.ime()
            )
            if (fullView == null) v.setPadding(bars.left, bars.top, bars.right, bars.bottom) else v.setPadding(0, 0, 0, 0)
            insets
        }
        // Light status bar and gesture bar icons on the dark background.
        WindowInsetsControllerCompat(window, root).apply {
            isAppearanceLightStatusBars = false
            isAppearanceLightNavigationBars = false
        }

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                val w = web
                when {
                    fullView != null -> exitFullScreen()
                    w != null && w.url?.contains("#clip=") == true ->
                        w.evaluateJavascript("document.getElementById('close').click()", null)
                    w != null && w.canGoBack() -> w.goBack()
                    else -> finish()
                }
            }
        })

        val saved = awayAddressProblem(prefs.homeAddress)
        when {
            prefs.serverUrl.isEmpty() -> showSetup(null)
            saved != null -> showSetup(saved) // the away address was typed in before this check existed
            else -> showWeb(intent.getStringExtra(MonitorService.EXTRA_CLIP))
        }
        if (intent.getBooleanExtra(Updater.EXTRA_INSTALL, false)) installUpdate()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.getBooleanExtra(Updater.EXTRA_INSTALL, false)) {
            installUpdate()
            return
        }
        val clip = intent.getStringExtra(MonitorService.EXTRA_CLIP) ?: return
        if (web != null) web?.loadUrl(pageFor(clip)) else showWeb(clip)
    }

    // ---- first run: where is the recorder? -----------------------------------

    private fun showSetup(problem: String?) {
        waitingForNetwork = problem != null
        web?.destroy()
        web = null
        root.removeAllViews()
        val pad = (24 * resources.displayMetrics.density).toInt()
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad * 3, pad, pad)
        }
        fun text(t: String, size: Float, color: String) = TextView(this).apply {
            text = t; textSize = size; setTextColor(Color.parseColor(color)); setPadding(0, 0, 0, pad / 2)
        }
        val density = resources.displayMetrics.density
        fun rounded(fill: String, stroke: String?) = GradientDrawable().apply {
            setColor(Color.parseColor(fill))
            cornerRadius = 14 * density
            if (stroke != null) setStroke((1 * density).toInt(), Color.parseColor(stroke))
        }
        box.addView(ImageView(this).apply {
            setImageResource(R.drawable.logo_mark)
            contentDescription = null
        }, LinearLayout.LayoutParams((64 * density).toInt(), (64 * density).toInt()).apply {
            bottomMargin = pad / 2
        })
        box.addView(text("A.S.D", 30f, "#E9EEF7").apply { typeface = android.graphics.Typeface.DEFAULT_BOLD; setPadding(0, 0, 0, 0) })
        box.addView(text("HOME SECURITY", 12f, "#60A5FA").apply { letterSpacing = 0.25f })
        box.addView(text(
            "While you're on your home Wi-Fi, enter the PC's home address, for example 192.168.1.20:8080 " +
                "(shown in the viewer on the PC). To use the app away from home, sign in here at home, then " +
                "go to Settings > Away from home and press Set up this phone. After that the app finds the " +
                "PC by itself wherever you are: you never type the away address.",
            15f, "#8E9BB3"
        ))
        if (problem != null) box.addView(text(problem, 15f, "#FF6B6B"))
        if (prefs.paired) {
            box.addView(text("This phone is set up for away from home, so it finds the PC by itself.", 15f, "#8E9BB3"))
            box.addView(Button(this).apply {
                text = "Try again"
                setOnClickListener { showWeb(null) }
            })
        }
        val input = EditText(this).apply {
            hint = "192.168.1.20:8080"
            setText(prefs.homeAddress.removePrefix("http://"))
            inputType = InputType.TYPE_TEXT_VARIATION_URI or InputType.TYPE_CLASS_TEXT
            setTextColor(Color.WHITE)
            setHintTextColor(Color.parseColor("#5B6781"))
            isSingleLine = true
            background = rounded("#121A2B", "#26324A")
            setPadding(pad * 2 / 3, pad / 2, pad * 2 / 3, pad / 2)
        }
        box.addView(input, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT).apply {
            topMargin = pad / 2
            bottomMargin = pad / 2
        })
        box.addView(Button(this).apply {
            text = "Connect"
            isAllCaps = false
            textSize = 16f
            setTextColor(Color.WHITE)
            background = rounded("#3B82F6", null)
            setOnClickListener {
                val url = Prefs.normalise(input.text.toString())
                if (!URLUtil.isValidUrl(url)) {
                    Toast.makeText(this@MainActivity, "That doesn't look like an address", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }
                awayAddressProblem(url)?.let {
                    showSetup(it)
                    return@setOnClickListener
                }
                prefs.serverUrl = url
                showWeb(null)
            }
        })
        root.addView(box, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP))
    }

    /**
     * The encrypted away port only answers phones that were set up at home, so typing it here
     * can't work (it used to give a blank screen). Explain instead.
     */
    private fun awayAddressProblem(url: String): String? {
        if (prefs.paired || url.isEmpty()) return null
        val u = Uri.parse(url)
        if (u.scheme != "https" && u.port != 8443) return null
        return "$url is the away-from-home address. It only works for a phone that has been set up, " +
            "so it can't be typed in here. On your home Wi-Fi, enter the PC's home address instead " +
            "(for example 192.168.1.20:8080), sign in, then go to Settings > Away from home and press " +
            "Set up this phone. From then on the app connects by itself when you're out."
    }

    // ---- the recorder's pages -------------------------------------------------

    /** Set up for away access: work out home or away first (network), then show the pages. */
    private fun showWeb(clip: String?) {
        if (!prefs.paired) {
            openWeb(clip)
            return
        }
        showConnecting()
        Thread {
            Remote.choose(prefs)
            runOnUiThread {
                if (isFinishing) return@runOnUiThread
                // Neither address answered: say why now, rather than a long black screen and a timeout.
                val problem = Remote.problem
                if (prefs.paired && problem.isNotEmpty()) showSetup(problem) else openWeb(clip)
            }
        }.start()
    }

    /** Shown while the app works out whether the PC answers at home or from outside. */
    private fun showConnecting() {
        waitingForNetwork = false
        web?.let { root.removeView(it); it.destroy() }
        web = null
        root.removeAllViews()
        val pad = (24 * resources.displayMetrics.density).toInt()
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(pad, pad, pad, pad)
        }
        box.addView(android.widget.ProgressBar(this))
        box.addView(TextView(this).apply {
            text = if (Remote.onLocalNetwork(this@MainActivity)) "Connecting to your PC…"
                else "Connecting to your PC from outside…"
            textSize = 16f
            setTextColor(Color.parseColor("#8E9BB3"))
            gravity = Gravity.CENTER
            setPadding(0, pad / 2, 0, 0)
        })
        root.addView(box, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
    }

    /** Is this one of the PC's own addresses? */
    private fun ownAddress(url: String): Boolean =
        listOf(prefs.serverUrl, prefs.homeAddress, prefs.remoteHome, prefs.remoteAway)
            .any { it.isNotEmpty() && (url == it || url.startsWith("$it/")) }

    @SuppressLint("SetJavaScriptEnabled")
    private fun openWeb(clip: String?) {
        waitingForNetwork = false
        val old = web
        web = null
        root.removeAllViews()
        old?.destroy()
        CookieManager.getInstance().setAcceptCookie(true)
        if (prefs.paired) {
            // The page's own requests (live picture, sound, clips) carry this phone's key as a cookie.
            for (base in listOf(prefs.remoteHome, prefs.remoteAway).filter { it.isNotEmpty() }) {
                CookieManager.getInstance().setCookie(
                    base, "camhub_device=${prefs.remoteKey}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=31536000"
                )
            }
        }
        val w = WebView(this)
        web = w
        w.setBackgroundColor(Color.parseColor("#0B1020"))
        w.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_DEFAULT
            allowFileAccess = false
            allowContentAccess = false
        }
        w.addJavascriptInterface(Bridge(), "HomeCamera")
        val loadingUrl = prefs.serverUrl
        var pageArrived = false
        // A PC that accepts the connection but never answers left a blank screen. Say so instead.
        root.postDelayed({
            if (web === w && !pageArrived) {
                showSetup("$loadingUrl didn't answer. Check the PC is on and that this is its home address " +
                    "(for example 192.168.1.20:8080).")
            }
        }, 25_000)
        w.webViewClient = object : WebViewClient() {
            override fun onPageCommitVisible(view: WebView, url: String) {
                pageArrived = true
            }

            override fun onPageFinished(view: WebView, url: String) {
                pageArrived = true
            }

            override fun onReceivedHttpError(view: WebView, request: WebResourceRequest, response: WebResourceResponse) {
                // The away port answers "not found" to a phone it doesn't know.
                if (request.isForMainFrame && response.statusCode == 404 && request.url.scheme == "https" && !prefs.paired) {
                    showSetup(awayAddressProblem(request.url.toString()))
                }
            }

            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                // Stay on the recorder; open anything else in the browser.
                val u = request.url
                if (ownAddress(u.toString())) return false
                startActivity(Intent(Intent.ACTION_VIEW, u))
                return true
            }

            /** The PC's own certificate isn't signed by anyone else: accept it only by its fingerprint. */
            @SuppressLint("WebViewClientOnReceivedSslError")
            override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError) {
                if (prefs.paired && ownAddress(error.url ?: "") && Remote.matchesPin(error.certificate)) {
                    handler.proceed()
                } else {
                    handler.cancel()
                    showSetup(
                        if (!prefs.paired) awayAddressProblem(error.url ?: "https://")
                        else "The connection to ${error.url} wasn't safe (its certificate isn't your PC's), so it was stopped."
                    )
                }
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) {
                    Remote.forget()
                    showSetup("Couldn't reach ${prefs.serverUrl} (${error.description}). Check the PC is on and the address is right.")
                }
            }
        }
        w.webChromeClient = object : WebChromeClient() {
            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                fullView = view
                fullCallback = callback
                root.addView(view, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
                ViewCompat.requestApplyInsets(root)
                WindowInsetsControllerCompat(window, root).apply {
                    hide(WindowInsetsCompat.Type.systemBars())
                    systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
                }
                window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
            }

            override fun onHideCustomView() = exitFullScreen()
        }
        w.setDownloadListener { url, userAgent, contentDisposition, mimeType, _ ->
            val name = URLUtil.guessFileName(url, contentDisposition, mimeType)
            if (prefs.paired && url.startsWith("https://")) {
                // Android's download manager wouldn't accept the PC's own certificate, so the app saves it.
                Downloads.save(this, prefs, url, name, mimeType)
                Toast.makeText(this, "Downloading $name", Toast.LENGTH_SHORT).show()
                return@setDownloadListener
            }
            val req = DownloadManager.Request(Uri.parse(url))
                .addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url) ?: "")
                .addRequestHeader("User-Agent", userAgent)
                .setMimeType(mimeType)
                .setTitle(name)
                .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
            getSystemService(DownloadManager::class.java).enqueue(req)
            Toast.makeText(this, "Downloading $name", Toast.LENGTH_SHORT).show()
        }
        root.addView(w, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        w.loadUrl(if (clip != null) pageFor(clip) else prefs.serverUrl + "/")
        if (prefs.token.isNotEmpty()) startAlerts()
    }

    /** A notification opens a recording, or (for "summary=<date>") a night summary. */
    private fun pageFor(target: String) =
        if (target.startsWith("summary=")) "${prefs.serverUrl}/#$target" else "${prefs.serverUrl}/#clip=$target"

    private fun exitFullScreen() {
        val v = fullView ?: return
        root.removeView(v)
        fullView = null
        fullCallback?.onCustomViewHidden()
        fullCallback = null
        WindowInsetsControllerCompat(window, root).show(WindowInsetsCompat.Type.systemBars())
        ViewCompat.requestApplyInsets(root)
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    override fun onPause() {
        super.onPause()
        try {
            getSystemService(android.net.ConnectivityManager::class.java).unregisterNetworkCallback(networkWatch)
        } catch (_: Exception) {
        }
        Updater.activityVisible = false
        talk?.stop()
        talk = null
        web?.onPause()
        CookieManager.getInstance().flush()
    }

    override fun onResume() {
        super.onResume()
        try {
            val cm = getSystemService(android.net.ConnectivityManager::class.java)
            lastNetwork = cm.activeNetwork // only a change from this one counts
            cm.registerDefaultNetworkCallback(networkWatch)
        } catch (_: Exception) {
        }
        Updater.activityVisible = true
        offerUpdate()
        web?.onResume()
        // Coming back from the location settings screen: refresh the Home and away card.
        web?.evaluateJavascript("window.onPhoneLocation && window.onPhoneLocation('{}')", null)
    }

    override fun onDestroy() {
        tv?.release()
        tv = null
        web?.destroy()
        super.onDestroy()
    }

    // ---- alerts -----------------------------------------------------------------

    private fun startAlerts() {
        if (Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
        MonitorService.start(this)
        askBatteryExemption()
    }

    @SuppressLint("BatteryLife")
    private fun askBatteryExemption() {
        val pm = getSystemService(PowerManager::class.java)
        if (prefs.askedBattery || pm.isIgnoringBatteryOptimizations(packageName)) return
        prefs.askedBattery = true
        AlertDialog.Builder(this)
            .setTitle("Allow alerts in the background?")
            .setMessage("So that motion alerts arrive while your phone is asleep, let A.S.D Home Security run without battery restrictions. It uses very little power.")
            .setPositiveButton("Allow") { _, _ ->
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            }
            .setNegativeButton("Not now", null)
            .show()
    }

    // ---- updates ------------------------------------------------------------------------

    /** Now and then, on opening the app: if the PC has a newer phone app, offer it. */
    private fun offerUpdate() {
        if (prefs.token.isEmpty() || System.currentTimeMillis() - prefs.updateCheckedAt < 6 * 3600_000L) return
        prefs.updateCheckedAt = System.currentTimeMillis()
        Thread {
            val info = try { Updater.newerOnPc(prefs) } catch (_: Exception) { null } ?: return@Thread
            runOnUiThread {
                if (isFinishing) return@runOnUiThread
                AlertDialog.Builder(this)
                    .setTitle("Update A.S.D Home Security?")
                    .setMessage("Version ${info.version} of the phone app is ready (you have ${BuildConfig.VERSION_NAME}). " +
                        "The app closes for a few seconds while it updates. Your settings are kept.")
                    .setPositiveButton("Update now") { _, _ -> installUpdate() }
                    .setNegativeButton("Later", null)
                    .show()
            }
        }.start()
    }

    /** Installs the PC's newer phone app, first asking Android to allow it if needed. */
    private fun installUpdate() {
        if (!Updater.canInstall(this)) {
            AlertDialog.Builder(this)
                .setTitle("Allow updates")
                .setMessage("So A.S.D Home Security can update itself, turn on \"Allow from this source\" on the next screen, then come back and press Update again.")
                .setPositiveButton("Continue") { _, _ ->
                    startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
                }
                .setNegativeButton("Not now", null)
                .show()
            return
        }
        Updater.checkAndInstall(this, manual = true) { ok, message -> phoneUpdateResult(ok, message) }
    }

    private fun phoneUpdateResult(ok: Boolean, message: String) {
        val json = JSONObject().put("ok", ok).put("message", message).toString()
        runOnUiThread {
            Toast.makeText(this, message, Toast.LENGTH_LONG).show()
            web?.evaluateJavascript("window.onPhoneUpdate && window.onPhoneUpdate(${JSONObject.quote(json)})", null)
        }
    }

    // ---- home and away: this phone's location ---------------------------------------

    /** Step 1: location while using the app. Step 2: "Allow all the time". Step 3: set home. */
    private fun setUpLocation() {
        if (!HomePresence.available(this)) {
            locationResult(false, "This phone needs Google Play services to share its location.")
            return
        }
        when (HomePresence.permission(this)) {
            "none" -> requestPermissions(
                arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION),
                REQ_LOCATION
            )
            "foreground" -> AlertDialog.Builder(this)
                .setTitle("Allow location all the time?")
                .setMessage(
                    "So A.S.D Home Security can notice when you come and go, even with the app closed, choose " +
                        "\"Allow all the time\" on the next screen. Only \"home\" or \"out\" is sent to your PC, " +
                        "never where you are."
                )
                .setPositiveButton("Continue") { _, _ ->
                    requestPermissions(arrayOf(Manifest.permission.ACCESS_BACKGROUND_LOCATION), REQ_LOCATION_ALWAYS)
                }
                .setNegativeButton("Not now") { _, _ -> setHomeHere() }
                .show()
            else -> setHomeHere()
        }
    }

    @Deprecated("Uses the platform permission result, which is all this needs")
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        when (requestCode) {
            REQ_LOCATION ->
                if (HomePresence.permission(this) == "none") {
                    locationResult(false, "Location wasn't allowed, so this phone can't switch home and away.")
                } else {
                    setUpLocation()
                }
            REQ_LOCATION_ALWAYS -> {
                setHomeHere()
                if (HomePresence.permission(this) != "always") offerLocationSettings()
            }
        }
    }

    /** Uses where the phone is now as home, draws the circle, and tells the PC "home". */
    @SuppressLint("MissingPermission")
    private fun setHomeHere() {
        if (HomePresence.permission(this) == "none") {
            locationResult(false, "Location isn't allowed.")
            return
        }
        Toast.makeText(this, "Finding where you are…", Toast.LENGTH_SHORT).show()
        try {
            LocationServices.getFusedLocationProviderClient(this)
                .getCurrentLocation(Priority.PRIORITY_HIGH_ACCURACY, CancellationTokenSource().token)
                .addOnSuccessListener { loc ->
                    if (loc == null) {
                        locationResult(false, "Couldn't find where you are. Check location is switched on, then try again.")
                        return@addOnSuccessListener
                    }
                    prefs.homeLat = loc.latitude
                    prefs.homeLon = loc.longitude
                    prefs.homeRadius = locationRadius
                    prefs.homeSet = true
                    prefs.locationOn = true
                    HomePresence.report(this, true)
                    HomePresence.register(this) { ok, _ ->
                        if (ok) {
                            locationResult(true, "Home is set. This phone now tells A.S.D Home Security when you come and go.")
                        } else {
                            locationResult(
                                true,
                                "Home is set, but location isn't allowed all the time, so it can't switch by itself. Press Fix."
                            )
                        }
                    }
                }
                .addOnFailureListener { e -> locationResult(false, "Couldn't find where you are: ${e.message}") }
        } catch (e: Exception) {
            locationResult(false, "Couldn't find where you are: ${e.message}")
        }
    }

    /** Android only asks once; after that "Allow all the time" is changed in the app's settings. */
    private fun offerLocationSettings() {
        AlertDialog.Builder(this)
            .setTitle("Change it in Settings")
            .setMessage("In the next screen choose Permissions, then Location, then \"Allow all the time\". Then come back here.")
            .setPositiveButton("Open Settings") { _, _ ->
                startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
            }
            .setNegativeButton("Not now", null)
            .show()
    }

    private fun locationResult(ok: Boolean, message: String) {
        val json = JSONObject().put("ok", ok).put("message", message).toString()
        runOnUiThread {
            web?.evaluateJavascript("window.onPhoneLocation && window.onPhoneLocation(${JSONObject.quote(json)})", null)
        }
    }

    /** Called from the recorder's pages (login.html and app.js). */
    inner class Bridge {
        @JavascriptInterface
        fun setToken(token: String) {
            prefs.token = token
            prefs.lastEventId = 0
            runOnUiThread { startAlerts() }
        }

        @JavascriptInterface
        fun signOut() {
            val wasPaired = prefs.paired
            prefs.loginToken = ""
            if (wasPaired) Remote.unpair(prefs)
            runOnUiThread {
                MonitorService.stop(this@MainActivity)
                if (wasPaired) {
                    CookieManager.getInstance().removeAllCookies(null)
                    showWeb(null)
                }
            }
        }

        // ---- away from home -------------------------------------------------------------

        @JavascriptInterface
        fun deviceName(): String = listOf(Build.MANUFACTURER.replaceFirstChar { it.uppercase() }, Build.MODEL)
            .distinct().joinToString(" ").take(40)

        @JavascriptInterface
        fun remoteInfo(): String = Remote.info(this@MainActivity).toString()

        /** Settings > Away from home > Set up this phone: what /api/remote/pair answered. */
        @JavascriptInterface
        fun pairRemote(json: String) {
            val message = try {
                Remote.pair(prefs, json)
            } catch (e: Exception) {
                val r = JSONObject().put("ok", false).put("message", "Couldn't set up: ${e.message}").toString()
                runOnUiThread { web?.evaluateJavascript("window.onRemotePaired && window.onRemotePaired(${JSONObject.quote(r)})", null) }
                return
            }
            prefs.lastEventId = 0
            runOnUiThread {
                Toast.makeText(this@MainActivity, message, Toast.LENGTH_LONG).show()
                MonitorService.stop(this@MainActivity)
                showWeb(null)
            }
        }

        /** This phone was removed in Settings: back to the home address and password. */
        @JavascriptInterface
        fun unpairRemote() {
            Remote.unpair(prefs)
            runOnUiThread {
                CookieManager.getInstance().removeAllCookies(null)
                MonitorService.stop(this@MainActivity)
                showWeb(null)
            }
        }

        @JavascriptInterface
        fun appVersion(): String = BuildConfig.VERSION_NAME

        /** For Settings > Updates: {"version", "code", "can_install", "problem"}. */
        @JavascriptInterface
        fun updateInfo(): String = JSONObject()
            .put("version", BuildConfig.VERSION_NAME)
            .put("code", BuildConfig.VERSION_CODE)
            .put("can_install", Updater.canInstall(this@MainActivity))
            .put("problem", prefs.updateProblem)
            .toString()

        /** The Update phone app button. The result comes back through window.onPhoneUpdate. */
        @JavascriptInterface
        fun updatePhoneApp() {
            runOnUiThread { installUpdate() }
        }

        /** Hold to talk pressed: record the microphone and send it to the camera. */
        @JavascriptInterface
        fun startTalk(camId: String) {
            runOnUiThread {
                if (ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO)
                    != PackageManager.PERMISSION_GRANTED
                ) {
                    requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 2)
                    Toast.makeText(this@MainActivity, "Allow the microphone, then hold the button again", Toast.LENGTH_LONG).show()
                    return@runOnUiThread
                }
                talk?.stop()
                talk = TalkRecorder(prefs, camId) { problem ->
                    runOnUiThread { Toast.makeText(this@MainActivity, "Talk: $problem", Toast.LENGTH_LONG).show() }
                }.also { it.start() }
            }
        }

        @JavascriptInterface
        fun stopTalk() {
            runOnUiThread {
                talk?.stop()
                talk = null
            }
        }

        @JavascriptInterface
        fun locationInfo(): String = HomePresence.info(this@MainActivity).toString()

        @JavascriptInterface
        fun locationSetHome(radiusMeters: Int) {
            locationRadius = radiusMeters.coerceIn(50, 2000)
            runOnUiThread { setUpLocation() }
        }

        @JavascriptInterface
        fun locationOff() {
            HomePresence.turnOff(this@MainActivity)
        }

        /** Shows a camera, all cameras or a night summary on a Chromecast or Google TV. */
        @JavascriptInterface
        fun castMedia(url: String, title: String, live: Boolean) {
            runOnUiThread {
                val t = tv ?: TvCast(this@MainActivity).also { tv = it }
                t.show(url, title, live)
            }
        }

        @JavascriptInterface
        fun changeServer() {
            runOnUiThread { showSetup(null) }
        }
    }

    companion object {
        private const val REQ_LOCATION = 20
        private const val REQ_LOCATION_ALWAYS = 21
    }
}
