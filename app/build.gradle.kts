/*
 * ĐÃ SỬA v9: Chuyển cấu hình Chaquopy pip ra file python-config.gradle (Groovy DSL)
 * vì Kotlin DSL không thể giải quyết extension `python {}` mà Chaquopy thêm động
 * vào DefaultConfig (không có type-safe accessor được sinh ra).
 * Xem: app/python-config.gradle
 *
 * USB/lockdown vẫn dùng native C. Chỉ mux_usb.py + device_link.py đã port sang
 * native; các file Python khác chạy qua Chaquopy như trước.
 */
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.superalpha.sideload"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.superalpha.sideload"
        minSdk = 26
        targetSdk = 34
        versionCode = 4
        versionName = "1.0.3"
        ndk { abiFilters += listOf("arm64-v8a") }
        externalNativeBuild {
            cmake { cppFlags(""); arguments("-DANDROID_STL=c++_shared") }
        }
    }

    externalNativeBuild {
        cmake { path = file("src/main/cpp/CMakeLists.txt"); version = "3.22.1" }
    }

    ndkVersion = "25.2.9519653"

    buildTypes {
        release { isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro") }
        debug { isDebuggable = true }
    }

    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true }
    composeOptions { kotlinCompilerExtensionVersion = "1.5.14" }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
            excludes += "META-INF/versions/9/OSGI-INF/MANIFEST.MF"
        }
        jniLibs { useLegacyPackaging = true }
    }

    androidResources {
        noCompress += listOf("zsign_deps/libssl.so.3", "zsign_deps/libcrypto.so.3", "zsign_deps/libc++_shared.so")
    }
}

dependencies {
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.2")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.6")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.6")
    implementation("androidx.navigation:navigation-compose:2.8.0")

    val composeBom = platform("androidx.compose:compose-bom:2024.09.03")
    implementation(composeBom)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-extended")

    // BouncyCastle — dùng bởi CertHelper.kt
    implementation("org.bouncycastle:bcprov-jdk18on:1.78.1")
    implementation("org.bouncycastle:bcpkix-jdk18on:1.78.1")

    // OkHttp — dùng cho listAnisetteServers() trong PythonBridge.kt
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    debugImplementation("androidx.compose.ui:ui-tooling")
}

// Cấu hình Chaquopy pip đặt trong file Groovy riêng vì Kotlin DSL không thể
// giải quyết extension `python {}` mà Chaquopy thêm động vào DefaultConfig.
// Groovy DSL dispatch động nên tìm thấy extension ngay khi chạy.
apply(from = "python-config.gradle")
