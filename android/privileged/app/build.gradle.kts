plugins {
    id("com.android.application")
}

android {
    namespace = "dev.zara.privcompanion"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.zara.privcompanion"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
