package com.superalpha.sideload.bridge

import android.content.Context
import java.io.File

/**
 * Exposes app-private filesystem locations to Python via Chaquopy's Java-interop
 * (`from com.superalpha.sideload.bridge import AppPaths`). The original Termux-based
 * tool used relative paths in the current working directory (config.json, cert.pem,
 * .sideload_state.json, ./zsign); on Android there is no meaningful "cwd", so every
 * ported Python module reads these paths instead.
 *
 * [init] MUST be called once (from MainActivity/SuperAlphaApp) before any Python code
 * that touches these paths runs, since a Kotlin `object` has no constructor arguments.
 */
object AppPaths {
    private lateinit var appContext: Context

    fun init(context: Context) {
        appContext = context.applicationContext
    }

    /** App-private persistent storage directory, e.g. /data/data/<pkg>/files */
    @JvmStatic
    fun filesDir(): String = appContext.filesDir.absolutePath

    /** Scratch directory for IPA extraction/repacking, wiped and recreated per run. */
    @JvmStatic
    fun workDir(): String {
        val dir = File(appContext.filesDir, "sideload_work")
        dir.mkdirs()
        return dir.absolutePath
    }

    /** Directory containing bundled native "libraries" — this is where Android installs
     * jniLibs/arm64-v8a/libzsign.so, and it is one of the few directories on Android
     * where the app is allowed to `exec()` a file it ships (App Bundle/Play policy
     * requires such files to be named lib*.so and to live here). */
    @JvmStatic
    fun nativeLibDir(): String = appContext.applicationInfo.nativeLibraryDir

    /** Full path to the bundled zsign binary. */
    @JvmStatic
    fun zsignPath(): String = File(nativeLibDir(), "libzsign.so").absolutePath
}
