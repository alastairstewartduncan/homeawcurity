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
        if (web != null) web?.loadUrl("${prefs.serverUrl}/#clip=$clip") else showWeb(clip)
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
        w.loadUrl(prefs.serverUrl + if (clip != null) "/#clip=$clip" else "/")
        if (prefs.token.isNotEmpty()) startAlerts()
    }

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
        web?.onPause()
        CookieManager.getInstance().flush()
    }

    override fun onResume() {
        super.onResume()
        web?.onResume()
    }

    override fun onDestroy() {
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

        @JavascriptInterface
        fun changeServer() {
            runOnUiThread { showSetup(null) }
        }
    }
}
