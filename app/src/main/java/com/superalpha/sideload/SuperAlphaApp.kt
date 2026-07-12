package com.superalpha.sideload

import android.app.Application
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Application entry point. Starts the embedded Python interpreter exactly once per
 * process. Everything else (USB, UI) is initialized lazily from MainActivity.
 */
class SuperAlphaApp : Application() {
    override fun onCreate() {
        super.onCreate()
        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(this))
        }
    }
}
