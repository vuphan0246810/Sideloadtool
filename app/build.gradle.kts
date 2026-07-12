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
        // Chaquopy requires minSdk >= 24. USB Host API works from 21+, so 24 is not a
        // real-world restriction here.
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"

        ndk {
            // The bundled zsign binary (shipped as jniLibs/arm64-v8a/libzsign.so) is only
            // built for arm64. Restricting the ABI here keeps Chaquopy's own native
            // download to a single ABI too, which keeps CI build times reasonable.
            abiFilters += listOf("arm64-v8a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
        debug {
            isDebuggable = true
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }
    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.14"
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
        // zsign is shipped as a "fake" .so (it is really a standalone ELF executable,
        // not a shared library) purely so the Android build/packaging system installs
        // it into the app's native library directory, from which execution is allowed
        // without root. Prevent the linker/packager from trying to be clever about it.
        jniLibs {
            useLegacyPackaging = true
        }
    }

    // libzsign.so (built for Termux) dynamically needs libssl.so.3/libcrypto.so.3/
    // libc++_shared.so at runtime — see AppPaths.nativeDepsDir() for why these can't
    // live in jniLibs/ like libzsign.so itself (their versioned filenames don't match
    // the lib*.so pattern the installer extracts into nativeLibraryDir). They are
    // shipped as plain assets instead and unpacked to filesDir at runtime, so force
    // them to be stored uncompressed here — avoids relying on AAPT's default
    // by-extension compression heuristics for an unusual extension like ".3".
    androidResources {
        noCompress += listOf("zsign_deps/libssl.so.3", "zsign_deps/libcrypto.so.3", "zsign_deps/libc++_shared.so")
    }
}

chaquopy {
    defaultConfig {
        version = "3.11"
        pip {
            // requests: HTTP client used by apple_auth.py / developer_api.py.
            install("requests")
            // cryptography: AES/PBKDF2/CSR+X.509 generation (Chaquopy ships a prebuilt
            // Android wheel for this, so no native toolchain is needed at build time).
            install("cryptography")
            // srp: SRP-6a client used for the Apple ID GSA/SRP handshake. apple_auth.py
            // imports the pure-Python `srp._pysrp` submodule directly, so this installs
            // cleanly even though the package also ships an optional C accelerator.
            install("srp")
        }
    }
    sourceSets {
        getByName("main") {
            srcDir("src/main/python")
        }
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

    debugImplementation("androidx.compose.ui:ui-tooling")
}
