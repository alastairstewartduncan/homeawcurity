plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.homecamera.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.homecamera.app"
        minSdk = 26
        targetSdk = 35
        // The build passes -PappVersionCode / -PappVersionName so each download has its own number.
        versionCode = (project.findProperty("appVersionCode") as String?)?.toInt() ?: 1
        versionName = (project.findProperty("appVersionName") as String?) ?: "dev"
    }

    // Every build must be signed with the same key, or Android refuses to install it over the
    // last one (and the app can't update itself). The build gets the key from the
    // ANDROID_KEYSTORE_B64 secret on GitHub (see the README). Without it, the build machine's
    // throwaway debug key is used, which changes with every build.
    val keystore = System.getenv("HOMECAMERA_KEYSTORE")?.let { file(it) }?.takeIf { it.exists() }
    signingConfigs {
        if (keystore != null) {
            create("homecamera") {
                storeFile = keystore
                storePassword = System.getenv("HOMECAMERA_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("HOMECAMERA_KEY_ALIAS") ?: "homecamera"
                keyPassword = System.getenv("HOMECAMERA_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName(if (keystore != null) "homecamera" else "debug")
        }
    }
    buildFeatures {
        buildConfig = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.9.3")
    implementation("androidx.webkit:webkit:1.12.1")
    // Home and away: the home area (geofence) and finding where the phone is.
    implementation("com.google.android.gms:play-services-location:21.3.0")
    // Watch on TV: Chromecast and Google TV.
    implementation("com.google.android.gms:play-services-cast-framework:21.5.0")
    implementation("androidx.mediarouter:mediarouter:1.7.0")
}
