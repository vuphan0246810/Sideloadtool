package com.superalpha.sideload

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import com.superalpha.sideload.bridge.AppPaths
import com.superalpha.sideload.bridge.NativeLog
import com.superalpha.sideload.ui.AppNavHost
import com.superalpha.sideload.ui.HomeViewModel
import com.superalpha.sideload.ui.theme.SuperAlphaTheme

class MainActivity : ComponentActivity() {
    private val viewModel: HomeViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        AppPaths.init(applicationContext)
        NativeLog.log("SUPER ALPHA Sideload đã khởi động.")

        setContent {
            SuperAlphaTheme {
                Surface(modifier = Modifier.fillMaxSize()) {
                    AppNavHost(viewModel = viewModel)
                    com.superalpha.sideload.ui.PromptDialogHost()
                }
            }
        }
    }
}
