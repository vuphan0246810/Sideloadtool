package com.superalpha.sideload.bridge

import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.asSharedFlow

/**
 * One-way log pipe from Python to the Compose UI. Every ported Python module still
 * calls `print(...)` internally (kept as-is to minimize the diff against the original
 * CLI tool); `sideload_core.py` monkey-patches `builtins.print` at start-up to forward
 * every line here instead of to a (nonexistent, on Android) stdout console.
 *
 * Python side usage:
 *     from com.superalpha.sideload.bridge import NativeLog
 *     NativeLog.log("some line")
 */
object NativeLog {
    // replay=200: a screen that (re)subscribes after rotation/navigation still sees
    // recent history instead of starting blank.
    private val _lines = MutableSharedFlow<String>(replay = 200, extraBufferCapacity = 64)
    val lines = _lines.asSharedFlow()

    @JvmStatic
    fun log(message: String) {
        _lines.tryEmit(message)
    }
}
