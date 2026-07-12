package com.superalpha.sideload.bridge

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import java.util.concurrent.SynchronousQueue

/**
 * Bridges apple_auth.py's blocking `input()` calls (used for 2FA codes, and rarely a
 * manual DSID fallback) to a Compose dialog, since there is no console on Android.
 *
 * Python calls [requestInput] on its background IO thread (see PythonBridge, which
 * runs all Python calls on Dispatchers.IO) and blocks — exactly like the original
 * CLI's `input()` blocked the terminal thread. The Compose layer observes [prompt]
 * (see ui/PromptDialog.kt) and calls [submitResponse] when the user taps confirm,
 * which unblocks Python with the typed value.
 */
object UiPrompt {
    private val _prompt = MutableStateFlow<String?>(null)
    val prompt = _prompt.asStateFlow()

    private val responseQueue = SynchronousQueue<String>()

    @JvmStatic
    fun requestInput(promptText: String): String {
        _prompt.value = promptText
        val value = responseQueue.take()
        _prompt.value = null
        return value
    }

    /** Called from the Compose dialog's confirm button. */
    fun submitResponse(value: String) {
        responseQueue.put(value)
    }
}
