package com.homecamera.app

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.net.Uri
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
import android.webkit.URLUtil
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
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
    private var tv: TvCast? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        root = FrameLayout(this).apply { setBackgroundColor(Color.parseColor("#0F1216")) }
        setContentView(root)
        WindowCompat.setDecorFitsSystemWindows(window, true)

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

        if (prefs.serverUrl.isEmpty()) showSetup(null) else showWeb(intent.getStringExtra(MonitorService.EXTRA_CLIP))
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        val clip = intent.getStringExtra(MonitorService.EXTRA_CLIP) ?: return
        if (web != null) web?.loadUrl(pageFor(clip)) else showWeb(clip)
    }

    // ---- first run: where is the recorder? -----------------------------------

    private fun showSetup(problem: String?) {
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
        box.addView(text("Home Camera", 26f, "#E8ECF1"))
        box.addView(text(
            "Enter the address of the PC running Home Camera. At home that's its local address, " +
                "for example 192.168.1.20:8080. Away from home use the address you set up on your router.",
            15f, "#95A0AE"
        ))
        if (problem != null) box.addView(text(problem, 15f, "#FF6B6B"))
        val input = EditText(this).apply {
            hint = "192.168.1.20:8080"
            setText(prefs.serverUrl.removePrefix("http://"))
            inputType = InputType.TYPE_TEXT_VARIATION_URI or InputType.TYPE_CLASS_TEXT
            setTextColor(Color.WHITE)
            setHintTextColor(Color.GRAY)
            isSingleLine = true
        }
        box.addView(input)
        box.addView(Button(this).apply {
            text = "Connect"
            setOnClickListener {
                val url = Prefs.normalise(input.text.toString())
                if (!URLUtil.isValidUrl(url)) {
                    Toast.makeText(this@MainActivity, "That doesn't look like an address", Toast.LENGTH_SHORT).show()
                    return@setOnClickListener
                }
                prefs.serverUrl = url
                showWeb(null)
            }
        })
        root.addView(box, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT, Gravity.TOP))
    }

    // ---- the recorder's pages -------------------------------------------------

    @SuppressLint("SetJavaScriptEnabled")
    private fun showWeb(clip: String?) {
        root.removeAllViews()
        CookieManager.getInstance().setAcceptCookie(true)
        val w = WebView(this)
        web = w
        w.setBackgroundColor(Color.parseColor("#0F1216"))
        w.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            mediaPlaybackRequiresUserGesture = false
            cacheMode = WebSettings.LOAD_DEFAULT
            allowFileAccess = false
            allowContentAccess = false
        }
        w.addJavascriptInterface(Bridge(), "HomeCamera")
        w.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
                // Stay on the recorder; open anything else in the browser.
                val u = request.url
                if (u.toString().startsWith(prefs.serverUrl)) return false
                startActivity(Intent(Intent.ACTION_VIEW, u))
                return true
            }

            override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
                if (request.isForMainFrame) {
                    showSetup("Couldn't reach ${prefs.serverUrl} (${error.description}). Check the PC is on and the address is right.")
                }
            }
        }
        w.webChromeClient = object : WebChromeClient() {
            override fun onShowCustomView(view: View, callback: CustomViewCallback) {
                fullView = view
                fullCallback = callback
                root.addView(view, FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
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
        window.clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
    }

    override fun onPause() {
        super.onPause()
        talk?.stop()
        talk = null
        web?.onPause()
        CookieManager.getInstance().flush()
    }

    override fun onResume() {
        super.onResume()
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
            .setMessage("So that motion alerts arrive while your phone is asleep, let Home Camera run without battery restrictions. It uses very little power.")
            .setPositiveButton("Allow") { _, _ ->
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            }
            .setNegativeButton("Not now", null)
            .show()
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
                    "So Home Camera can notice when you come and go, even with the app closed, choose " +
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
                            locationResult(true, "Home is set. This phone now tells Home Camera when you come and go.")
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
            prefs.token = ""
            runOnUiThread { MonitorService.stop(this@MainActivity) }
        }

        @JavascriptInterface
        fun appVersion(): String = BuildConfig.VERSION_NAME

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
