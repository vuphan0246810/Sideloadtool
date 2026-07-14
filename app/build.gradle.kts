/*
 * ĐÃ SỬA v8: Khôi phục Chaquopy Python cho apple_auth/developer_api/sideload_core.
 * USB/lockdown vẫn dùng native C (libsideloadnative.so).
 * Chỉ mux_usb.py và device_link.py được port sang native — các file Python khác
 * (apple_auth.py, developer_api.py, sideload_core.py, config_manager.py, utils.py)
 * chạy qua Chaquopy như trước.
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
        versionCode = 1
        versionName = "1.0.0"
        ndk { abiFilters += listOf("arm64-v8a") }
        externalNativeBuild {
            cmake { cppFlags(""); arguments("-DANDROID_STL=c++_shared") }
        }

        // ── Chaquopy Python runtime ─────────────────────────────────────────
        python {
            // Các gói Python cần thiết:
            //   requests    — HTTP client cho apple_auth.py / developer_api.py
            //   cryptography— RSA/SSL cho device_link SSL temp cert và signing
            //   srp         — SRP auth cho apple_auth.py (đăng nhập Apple ID)
            pip {
                install("requests")
                install("cryptography")
                install("srp")
            }
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
