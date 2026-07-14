package com.superalpha.sideload.bridge

import android.util.Log
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.asSharedFlow

object NativeLog {
    private const val TAG = "NativeLog"
    private val _lines = MutableSharedFlow<String>(extraBufferCapacity = 500)
    val lines = _lines.asSharedFlow()

    fun emit(line: String) {
        Log.i(TAG, line)
        _lines.tryEmit(line)
    }

    /** Ghi một dòng log đơn giản (không có tag) — dùng bởi UI/Activity. */
    fun log(message: String) = emit(message)

    @JvmStatic
    fun log(tag: String, message: String) = emit("[$tag] $message")
}
