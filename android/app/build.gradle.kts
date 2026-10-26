plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "app.handyreminder"
    compileSdk = 34

    defaultConfig {
        applicationId = "app.handyreminder"
        minSdk = 26            // AlarmManager 精确闹钟的起点; 26 以下要另写兼容分支, 先不铺
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
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
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.core:core-ktx:1.13.1")

    // 纯 JVM 单元测试里没有 Android 的 org.json, 补一个同 API 的实现
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}
